"""会员、订单与模拟支付数据层。"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "membership.db"
LOCK = threading.Lock()

PLAN_DAYS = int(os.getenv("MEMBERSHIP_PLAN_DAYS", "30"))
PRICE_CENTS = int(os.getenv("MEMBERSHIP_PRICE_CENTS", "990"))
CURRENCY = "CNY"
PLAN_NAME = os.getenv("MEMBERSHIP_PLAN_NAME", "30天会员")

ALLOWED_CHANNELS = {"alipay", "wechat", "simulate"}


def _now() -> int:
    return int(time.time())


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    cols = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
    if column not in cols:
        conn.execute(ddl)


def init_db() -> None:
    with LOCK:
        conn = _connect()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS members (
                    token TEXT PRIMARY KEY,
                    user_id TEXT UNIQUE,
                    expires_at INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS orders (
                    id TEXT PRIMARY KEY,
                    user_token TEXT NOT NULL,
                    user_id TEXT,
                    plan TEXT NOT NULL,
                    amount_cents INTEGER NOT NULL,
                    channel TEXT NOT NULL,
                    status TEXT NOT NULL,
                    out_trade_no TEXT NOT NULL UNIQUE,
                    provider_trade_no TEXT,
                    created_at INTEGER NOT NULL,
                    paid_at INTEGER,
                    expires_at INTEGER
                );

                CREATE INDEX IF NOT EXISTS idx_orders_user_token ON orders(user_token);

                CREATE TABLE IF NOT EXISTS payment_callbacks (
                    id TEXT PRIMARY KEY,
                    order_id TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    payload TEXT,
                    created_at INTEGER NOT NULL
                );
                """
            )
            _ensure_column(
                conn,
                "members",
                "user_id",
                "ALTER TABLE members ADD COLUMN user_id TEXT",
            )
            _ensure_column(
                conn,
                "orders",
                "user_id",
                "ALTER TABLE orders ADD COLUMN user_id TEXT",
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_user_id ON orders(user_id)")
            conn.commit()
        finally:
            conn.close()


def _member_row(conn: sqlite3.Connection, token: str | None = None, user_id: str | None = None):
    if user_id:
        return conn.execute("SELECT * FROM members WHERE user_id = ?", (user_id,)).fetchone()
    if token:
        return conn.execute("SELECT * FROM members WHERE token = ?", (token,)).fetchone()
    return None


def is_member_active(token: str | None = None, user_id: str | None = None) -> bool:
    return bool(get_member_status(token=token, user_id=user_id).get("member"))


def get_member_status(token: str | None = None, user_id: str | None = None) -> dict[str, Any]:
    token = (token or "").strip()
    user_id = (user_id or "").strip()
    with LOCK:
        conn = _connect()
        try:
            row = _member_row(conn, token or None, user_id or None)
            if not row or row["expires_at"] <= _now():
                return {
                    "member": False,
                    "expires_at": row["expires_at"] if row else None,
                    "plan_name": PLAN_NAME,
                    "price_cents": PRICE_CENTS,
                    "currency": CURRENCY,
                }
            return {
                "member": True,
                "expires_at": row["expires_at"],
                "plan_name": PLAN_NAME,
                "price_cents": PRICE_CENTS,
                "currency": CURRENCY,
            }
        finally:
            conn.close()


def create_order(token: str, channel: str, user_id: str | None = None) -> dict[str, Any]:
    token = (token or "").strip()
    user_id = (user_id or "").strip() or None
    if not token and not user_id:
        raise ValueError("缺少会员身份标识")
    channel = (channel or "").strip().lower()
    if channel not in ALLOWED_CHANNELS:
        raise ValueError("不支持的支付渠道")

    order_id = uuid.uuid4().hex[:16]
    out_trade_no = f"SIM{_now()}{uuid.uuid4().hex[:12]}"
    now = _now()

    with LOCK:
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO orders (id, user_token, user_id, plan, amount_cents, channel, status, out_trade_no, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (order_id, token, user_id, PLAN_NAME, PRICE_CENTS, channel, "pending", out_trade_no, now),
            )
            conn.commit()
        finally:
            conn.close()

    return {
        "id": order_id,
        "out_trade_no": out_trade_no,
        "plan": PLAN_NAME,
        "amount_cents": PRICE_CENTS,
        "currency": CURRENCY,
        "channel": channel,
        "status": "pending",
        "pay_url": f"/api/pay/simulate/{order_id}",
        "created_at": now,
    }


def get_order(order_id: str) -> dict[str, Any] | None:
    with LOCK:
        conn = _connect()
        try:
            row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
            if not row:
                return None
            return {
                "id": row["id"],
                "plan": row["plan"],
                "amount_cents": row["amount_cents"],
                "currency": CURRENCY,
                "channel": row["channel"],
                "status": row["status"],
                "out_trade_no": row["out_trade_no"],
                "created_at": row["created_at"],
                "paid_at": row["paid_at"],
                "expires_at": row["expires_at"],
            }
        finally:
            conn.close()


def mark_order_paid(order_id: str, channel: str, provider_trade_no: str) -> dict[str, Any]:
    order_id = (order_id or "").strip()
    channel = (channel or "").strip().lower()
    now = _now()

    with LOCK:
        conn = _connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
            if not row:
                conn.rollback()
                raise ValueError("订单不存在")

            if row["status"] == "paid":
                result = {
                    "id": row["id"],
                    "plan": row["plan"],
                    "amount_cents": row["amount_cents"],
                    "currency": CURRENCY,
                    "channel": row["channel"],
                    "status": "paid",
                    "paid_at": row["paid_at"],
                    "expires_at": row["expires_at"],
                }
                conn.commit()
                return result

            if channel not in ALLOWED_CHANNELS or row["channel"] != channel:
                conn.rollback()
                raise ValueError("支付渠道不匹配")

            cur = conn.execute(
                "UPDATE orders SET status = ?, paid_at = ?, provider_trade_no = ? WHERE id = ? AND status = ?",
                ("paid", now, provider_trade_no or "", order_id, "pending"),
            )
            if cur.rowcount != 1:
                conn.rollback()
                return get_order(order_id) or {}

            user_id = row["user_id"]
            token = row["user_token"]
            member = _member_row(conn, token or None, user_id or None)
            if member:
                base = max(now, member["expires_at"])
                expires_at = base + PLAN_DAYS * 86400
                conn.execute(
                    "UPDATE members SET expires_at = ?, updated_at = ? WHERE token = ?",
                    (expires_at, now, member["token"]),
                )
            else:
                expires_at = now + PLAN_DAYS * 86400
                member_token = token or f"u_{user_id}" if user_id else uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO members (token, user_id, expires_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (member_token, user_id, expires_at, now, now),
                )

            conn.execute(
                "UPDATE orders SET expires_at = ? WHERE id = ?",
                (expires_at, order_id),
            )
            conn.commit()
            return {
                "id": order_id,
                "plan": row["plan"],
                "amount_cents": row["amount_cents"],
                "currency": CURRENCY,
                "channel": channel,
                "status": "paid",
                "paid_at": now,
                "expires_at": expires_at,
            }
        finally:
            conn.close()


def generate_member_token() -> str:
    return uuid.uuid4().hex
