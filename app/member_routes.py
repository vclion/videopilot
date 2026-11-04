"""会员与模拟支付路由。"""
from __future__ import annotations

import uuid
from typing import Literal

from . import auth
from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from . import membership

router = APIRouter(prefix="/api", tags=["membership"])


class CreateOrderRequest(BaseModel):
    channel: Literal["alipay", "wechat", "simulate"] = "simulate"


def _token_from_header(value: str | None) -> str:
    return (value or "").strip()


@router.get("/member/status")
def member_status(x_member_token: str | None = Header(default=None), user: dict | None = Depends(auth.get_optional_user)):
    return membership.get_member_status(x_member_token, user_id=user["id"] if user else None)


@router.post("/member/init")
def member_init():
    return {"token": membership.generate_member_token()}


@router.post("/member/orders")
def create_member_order(payload: CreateOrderRequest, x_member_token: str | None = Header(default=None), user: dict | None = Depends(auth.get_optional_user)):
    token = _token_from_header(x_member_token)
    if not token and not user:
        raise HTTPException(status_code=400, detail="请先登录或刷新页面后重试")
    try:
        return membership.create_order(token, payload.channel, user_id=user["id"] if user else None)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/member/orders/{order_id}")
def get_member_order(order_id: str):
    order = membership.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")
    return order


def _simulate_page(order: dict) -> str:
    order_id = order["id"]
    amount = order["amount_cents"] / 100
    return f"""<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>模拟支付</title>
    <style>
      body {{
        margin: 0;
        min-height: 100vh;
        display: grid;
        place-items: center;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        background: linear-gradient(135deg, #0b4f9b, #082f5a);
        color: #0f172a;
      }}
      .box {{
        width: min(360px, calc(100% - 40px));
        padding: 28px 24px;
        border-radius: 20px;
        background: #ffffff;
        box-shadow: 0 24px 60px -28px rgba(0, 0, 0, 0.45);
        text-align: center;
      }}
      h1 {{ margin: 0 0 8px; font-size: 22px; }}
      .amount {{ font-size: 30px; font-weight: 800; color: #1777ff; }}
      .tip {{ margin: 16px 0 22px; color: #64748b; font-size: 14px; }}
      button {{
        width: 100%;
        padding: 13px 16px;
        border: 0;
        border-radius: 14px;
        background: linear-gradient(135deg, #1777ff, #0d5ee0);
        color: #ffffff;
        font-weight: 800;
        cursor: pointer;
      }}
      button:disabled {{ opacity: 0.6; cursor: not-allowed; }}
    </style>
  </head>
  <body>
    <div class="box">
      <h1>模拟支付</h1>
      <div class="amount">¥ {amount:.2f}</div>
      <p class="tip">这是开发测试页面，点击按钮代表支付成功。</p>
      <button id="pay-btn" type="button">模拟支付成功</button>
    </div>
    <script>
      const btn = document.getElementById("pay-btn");
      btn.addEventListener("click", async () => {{
        btn.disabled = true;
        btn.textContent = "处理中...";
        try {{
          await fetch("/api/pay/simulate/{order_id}/confirm", {{ method: "POST" }});
          btn.textContent = "支付成功";
          setTimeout(() => window.close(), 800);
        }} catch (error) {{
          btn.disabled = false;
          btn.textContent = "重试";
        }}
      }});
    </script>
  </body>
</html>"""


@router.get("/pay/simulate/{order_id}", include_in_schema=False)
def simulate_pay_page(order_id: str):
    order = membership.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")
    return HTMLResponse(_simulate_page(order))


@router.post("/pay/simulate/{order_id}/confirm")
def simulate_pay_confirm(order_id: str):
    order = membership.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")
    if order["status"] != "pending":
        return {"status": order["status"], "id": order_id}
    paid = membership.mark_order_paid(order_id, order["channel"], f"SIMULATED-{uuid.uuid4().hex[:12]}")
    return {"status": "paid", "id": paid["id"], "expires_at": paid["expires_at"]}
