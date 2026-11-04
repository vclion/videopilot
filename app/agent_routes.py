"""兼容现有业务的 Video Intelligence Agent 接口。"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from typing import Any, Literal

from . import auth
from . import agent_store
from . import membership
from .agent_core import execute_agent
from .ai_summary import is_configured
from . import video_enhancement
from . import qwen_client as qwen
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field


router = APIRouter(prefix="/api/agent", tags=["agent"])
try:
    MAX_AGENT_ATTEMPTS = max(1, min(int(os.getenv("AGENT_MAX_ATTEMPTS", "2")), 3))
except ValueError:
    MAX_AGENT_ATTEMPTS = 2


class AgentRunRequest(BaseModel):
    url: str = Field(min_length=4, max_length=2048)
    mode: Literal["summary", "question"] = "summary"
    question: str | None = Field(default=None, max_length=2000)
    conversation_id: str | None = Field(default=None, max_length=64)


def _token_hash(value: str | None) -> str | None:
    value = (value or "").strip()
    return hashlib.sha256(value.encode("utf-8")).hexdigest() if value else None


def _public_run(run: dict[str, Any]) -> dict[str, Any]:
    return {
        key: run.get(key)
        for key in (
            "id",
            "url",
            "mode",
            "question",
            "conversation_id",
            "status",
            "result",
            "error",
            "created_at",
            "started_at",
            "finished_at",
            "steps",
        )
    }


def _can_access(run: dict[str, Any], token: str | None, user: dict[str, Any] | None) -> bool:
    if run.get("owner_user_id"):
        return bool(
            user
            and user.get("id") == run.get("owner_user_id")
            and membership.is_member_active(user_id=user["id"])
        )
    return bool(
        token
        and _token_hash(token) == run.get("owner_token_hash")
        and membership.is_member_active(token=token)
    )


def _can_access_conversation(
    conversation: dict[str, Any] | None,
    token: str | None,
    user: dict[str, Any] | None,
) -> bool:
    if not conversation:
        return False
    if conversation.get("owner_user_id"):
        return bool(
            user
            and user.get("id") == conversation.get("owner_user_id")
            and membership.is_member_active(user_id=user["id"])
        )
    return bool(
        token
        and _token_hash(token) == conversation.get("owner_token_hash")
        and membership.is_member_active(token=token)
    )


def _run_agent(run_id: str, url: str, mode: str, question: str | None) -> None:
    agent_store.update_run(run_id, status="running", started_at=agent_store._now(), attempt=0)
    run = agent_store.get_run(run_id) or {}
    conversation_context = None
    if run.get("conversation_id"):
        conversation_context = agent_store.get_conversation_context(
            run["conversation_id"],
            exclude_run_id=run_id,
            query=question or "",
        )

    attempt = 0
    def record_step(
        tool_name: str,
        status: str,
        input_data: dict[str, Any],
        output_data: dict[str, Any] | None,
        error: str | None,
    ) -> None:
        run = agent_store.get_run(run_id)
        step_no = len(run.get("steps", [])) + 1 if run else 1
        payload = {**input_data, "attempt": attempt}
        agent_store.add_step(run_id, step_no, tool_name, status, payload, output_data, error)

    def retryable(error: Exception) -> bool:
        text = str(error).lower()
        return any(
            marker in text
            for marker in (
                "无法连接",
                "请求过于频繁",
                "接口错误（5",
                "timed out",
                "timeout",
                "temporarily",
                "http 5",
                "qwen 返回无效",
            )
        )

    last_error: Exception | None = None
    while attempt < MAX_AGENT_ATTEMPTS:
        attempt += 1
        agent_store.update_run(run_id, status="running", attempt=attempt)
        try:
            result = execute_agent(
                url,
                mode,
                question,
                record_step,
                conversation_context=conversation_context,
            )
            agent_store.update_run(
                run_id,
                status="completed",
                attempt=attempt,
                result=result,
                finished_at=agent_store._now(),
            )
            return
        except Exception as exc:
            last_error = exc
            if attempt >= MAX_AGENT_ATTEMPTS or not retryable(exc):
                break
            time.sleep(min(2 ** attempt, 4))

    agent_store.update_run(
        run_id,
        status="failed",
        attempt=attempt,
        error=str(last_error or "Agent 执行失败"),
        finished_at=agent_store._now(),
    )


@router.post("/runs")
def create_agent_run(
    payload: AgentRunRequest,
    x_member_token: str | None = Header(default=None),
    user: dict[str, Any] | None = Depends(auth.get_optional_user),
) -> dict[str, Any]:
    url = payload.url.strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(status_code=400, detail="请输入有效的 http/https 链接")
    if payload.mode == "question" and not (payload.question or "").strip():
        raise HTTPException(status_code=400, detail="问答模式必须提供 question")
    if not membership.is_member_active(x_member_token, user_id=user["id"] if user else None):
        raise HTTPException(status_code=403, detail="Agent 功能需要会员权限")
    if payload.mode == "question" and video_enhancement.enabled():
        try:
            qwen.configuration()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
    elif not is_configured():
        raise HTTPException(status_code=400, detail="尚未配置 DeepSeek API Key")
    if payload.conversation_id:
        conversation = agent_store.get_conversation(payload.conversation_id)
        if not _can_access_conversation(conversation, x_member_token, user):
            raise HTTPException(status_code=403, detail="无权访问该会话")
        if conversation and conversation.get("url") != agent_store.canonicalize_url(url):
            raise HTTPException(status_code=400, detail="会话与视频链接不匹配")

    run_id = agent_store.create_run(
        url=url,
        mode=payload.mode,
        question=(payload.question or "").strip() or None,
        owner_user_id=user["id"] if user else None,
        owner_token_hash=None if user else _token_hash(x_member_token),
        conversation_id=payload.conversation_id,
    )
    thread = threading.Thread(
        target=_run_agent,
        args=(run_id, url, payload.mode, (payload.question or "").strip() or None),
        name=f"agent-run-{run_id}",
        daemon=True,
    )
    thread.start()
    return _public_run(agent_store.get_run(run_id) or {})


@router.get("/history")
def get_agent_history(
    url: str,
    conversation_id: str | None = None,
    x_member_token: str | None = Header(default=None),
    user: dict[str, Any] | None = Depends(auth.get_optional_user),
) -> dict[str, Any]:
    url = url.strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(status_code=400, detail="请输入有效的 http/https 链接")
    if not membership.is_member_active(x_member_token, user_id=user["id"] if user else None):
        raise HTTPException(status_code=403, detail="Agent 功能需要会员权限")

    runs = agent_store.list_runs(
        url=url,
        owner_user_id=user["id"] if user else None,
        owner_token_hash=None if user else _token_hash(x_member_token),
        conversation_id=conversation_id,
    )
    return {"url": url, "runs": [_public_run(run) for run in runs]}


@router.get("/conversations")
def get_agent_conversations(
    url: str,
    x_member_token: str | None = Header(default=None),
    user: dict[str, Any] | None = Depends(auth.get_optional_user),
) -> dict[str, Any]:
    url = url.strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(status_code=400, detail="请输入有效的 http/https 链接")
    if not membership.is_member_active(x_member_token, user_id=user["id"] if user else None):
        raise HTTPException(status_code=403, detail="Agent 功能需要会员权限")
    conversations = agent_store.list_conversations(
        url=url,
        owner_user_id=user["id"] if user else None,
        owner_token_hash=None if user else _token_hash(x_member_token),
    )
    return {"url": url, "conversations": conversations}


@router.post("/conversations")
def create_agent_conversation(
    url: str,
    x_member_token: str | None = Header(default=None),
    user: dict[str, Any] | None = Depends(auth.get_optional_user),
) -> dict[str, Any]:
    url = url.strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(status_code=400, detail="请输入有效的 http/https 链接")
    if not membership.is_member_active(x_member_token, user_id=user["id"] if user else None):
        raise HTTPException(status_code=403, detail="Agent 功能需要会员权限")
    conversation = agent_store.create_conversation(
        url=url,
        owner_user_id=user["id"] if user else None,
        owner_token_hash=None if user else _token_hash(x_member_token),
    )
    return conversation


@router.delete("/conversations/{conversation_id}")
def delete_agent_conversation(
    conversation_id: str,
    x_member_token: str | None = Header(default=None),
    user: dict[str, Any] | None = Depends(auth.get_optional_user),
) -> dict[str, Any]:
    conversation = agent_store.get_conversation(conversation_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="会话不存在")
    if not _can_access_conversation(conversation, x_member_token, user):
        raise HTTPException(status_code=403, detail="无权删除该会话")
    if not membership.is_member_active(x_member_token, user_id=user["id"] if user else None):
        raise HTTPException(status_code=403, detail="Agent 功能需要会员权限")
    agent_store.delete_conversation(conversation_id)
    return {"deleted": True, "id": conversation_id}


@router.get("/runs/{run_id}")
def get_agent_run(
    run_id: str,
    x_member_token: str | None = Header(default=None),
    user: dict[str, Any] | None = Depends(auth.get_optional_user),
) -> dict[str, Any]:
    run = agent_store.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Agent 任务不存在")
    if not _can_access(run, x_member_token, user):
        raise HTTPException(status_code=403, detail="无权访问该 Agent 任务")
    return _public_run(run)


@router.delete("/runs/{run_id}")
def delete_agent_run(
    run_id: str,
    x_member_token: str | None = Header(default=None),
    user: dict[str, Any] | None = Depends(auth.get_optional_user),
) -> dict[str, Any]:
    run = agent_store.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Agent 任务不存在")
    if not _can_access(run, x_member_token, user):
        raise HTTPException(status_code=403, detail="无权删除该 Agent 任务")
    agent_store.delete_run(run_id)
    return {"deleted": True, "id": run_id}
