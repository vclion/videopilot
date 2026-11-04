"""AI 总结：独立路由与内存任务管理（只新增，不影响现有下载接口）。

接口：
- POST /api/ai/summarize      创建总结任务（立即返回 task_id）
- GET  /api/ai/task/{task_id} 轮询任务状态与结果
- GET  /api/ai/export/{task_id} 导出 Markdown / 纯文本总结
"""

from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote

from . import auth
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from . import membership
from . import subtitle_store
from .ai_summary import generate_summary, is_configured
from .subtitle import SubtitleError, fetch_subtitles


router = APIRouter(prefix="/api/ai", tags=["ai"])

AI_TASKS: dict[str, dict[str, Any]] = {}
AI_TASKS_LOCK = threading.Lock()


class SummarizeRequest(BaseModel):
    url: str = Field(min_length=4, max_length=2048)


def _now() -> int:
    return int(time.time())


def _new_task_id() -> str:
    return uuid.uuid4().hex[:16]


def _update_task(task_id: str, **fields: Any) -> None:
    with AI_TASKS_LOCK:
        task = AI_TASKS.get(task_id)
        if task:
            task.update(fields)


def _public_task(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": task.get("id"),
        "url": task.get("url"),
        "status": task.get("status"),
        "source": task.get("source"),
        "language": task.get("language"),
        "title": task.get("title"),
        "error": task.get("error"),
        "result": task.get("result"),
        "created_at": task.get("created_at"),
        "finished_at": task.get("finished_at"),
    }


@router.post("/summarize")
def create_summarize(payload: SummarizeRequest, x_member_token: str | None = Header(default=None), user: dict | None = Depends(auth.get_optional_user)) -> dict[str, Any]:
    url = payload.url.strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(status_code=400, detail="请输入有效的 http/https 链接")
    if not membership.is_member_active(x_member_token, user_id=user["id"] if user else None):
        raise HTTPException(status_code=403, detail="会员专属功能，请先开通会员")
    if not is_configured():
        raise HTTPException(
            status_code=400,
            detail="尚未配置 DeepSeek API Key，请在项目根目录的 .env 中填写 DEEPSEEK_API_KEY 后重试",
        )

    task_id = _new_task_id()
    task: dict[str, Any] = {
        "id": task_id,
        "user_token": (x_member_token or "").strip(),
        "user_id": user["id"] if user else None,
        "url": url,
        "status": "queued",
        "source": None,
        "language": None,
        "title": None,
        "error": None,
        "result": None,
        "created_at": _now(),
        "finished_at": None,
    }

    with AI_TASKS_LOCK:
        AI_TASKS[task_id] = task

    thread = threading.Thread(
        target=_run_summarize,
        args=(task_id, url),
        name=f"ai-summary-{task_id}",
        daemon=True,
    )
    thread.start()
    return _public_task(task)


def _run_summarize(task_id: str, url: str) -> None:
    try:
        _update_task(task_id, status="fetching_subtitle")
        subtitles = fetch_subtitles(
            url,
            on_stage=lambda status: _update_task(task_id, status=status),
        )

        transcript = (subtitles.get("transcript") or "").strip()
        if not transcript:
            raise SubtitleError("字幕内容为空，无法生成总结")

        _update_task(
            task_id,
            status="summarizing",
            source=subtitles.get("source"),
            language=subtitles.get("language"),
            title=subtitles.get("title"),
        )

        result = generate_summary(subtitles.get("title", ""), transcript)
        try:
            subtitle_store.save_summary(
                url=url,
                title=subtitles.get("title", ""),
                source=subtitles.get("source", ""),
                language=subtitles.get("language", ""),
                result=result,
            )
        except Exception:
            # 总结已生成时，缓存写入失败不应让用户丢失本次结果。
            pass
        _update_task(task_id, status="completed", result=result, finished_at=_now())
    except (SubtitleError, RuntimeError) as exc:
        _update_task(task_id, status="failed", error=str(exc), finished_at=_now())
    except Exception as exc:
        _update_task(task_id, status="failed", error=f"总结失败：{exc}", finished_at=_now())


@router.get("/task/{task_id}")
def get_summarize_task(task_id: str, x_member_token: str | None = Header(default=None), user: dict | None = Depends(auth.get_optional_user)) -> dict[str, Any]:
    with AI_TASKS_LOCK:
        task = AI_TASKS.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if not _can_access_task(task, x_member_token, user):
        raise HTTPException(status_code=403, detail="会员专属功能，请先开通会员")
    return _public_task(task)


def _can_access_task(task: dict[str, Any], token: str | None, user: dict | None) -> bool:
    if user:
        return task.get("user_id") == user["id"] and membership.is_member_active(user_id=user["id"])
    return task.get("user_token") == (token or "").strip() and membership.is_member_active(token=token)


def _cached_subtitle(url: str) -> dict[str, Any] | None:
    cached = subtitle_store.get(url)
    if not cached:
        return None
    transcript_file = cached.get("transcript_file") or ""
    filename = Path(transcript_file).name
    result = {
        "transcript_file": transcript_file,
        "segments_file": cached.get("segments_file"),
        "transcript_path": cached.get("transcript_path"),
        "transcript_url": f"/api/subtitles/file/{quote(filename)}" if filename else None,
        "segments": cached.get("segments") or [],
    }
    return {
        "source": cached.get("source"),
        "language": cached.get("language"),
        "title": cached.get("title"),
        "result": result,
        "created_at": cached.get("created_at"),
        "updated_at": cached.get("updated_at"),
    }


@router.get("/history")
def get_cached_ai_history(
    url: str,
    x_member_token: str | None = Header(default=None),
    user: dict | None = Depends(auth.get_optional_user),
) -> dict[str, Any]:
    """解析视频后恢复历史字幕和总结；总结仍遵循会员权限。"""
    url = url.strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(status_code=400, detail="请输入有效的 http/https 链接")

    summary = None
    if membership.is_member_active(x_member_token, user_id=user["id"] if user else None):
        summary = subtitle_store.get_summary(url)
    return {
        "url": url,
        "subtitle": _cached_subtitle(url),
        "summary": summary,
    }


def _render_markdown(task: dict[str, Any]) -> str:
    result = task.get("result") or {}
    lines = [
        f"# {task.get('title') or '视频总结'}",
        "",
        f"> {result.get('summary', '')}",
        "",
    ]

    if result.get("key_points"):
        lines.append("## 核心观点")
        lines.extend(f"- {point}" for point in result["key_points"])
        lines.append("")

    if result.get("chapters"):
        lines.append("## 章节要点")
        for chapter in result["chapters"]:
            lines.append(f"### {chapter.get('title', '')}")
            if chapter.get("content"):
                lines.append(chapter["content"])
            lines.append("")

    if result.get("highlights"):
        lines.append("## 金句与亮点")
        lines.extend(f"- {highlight}" for highlight in result["highlights"])
        lines.append("")

    if result.get("action_items"):
        lines.append("## 行动建议")
        lines.extend(f"- {item}" for item in result["action_items"])

    return "\n".join(lines)


@router.get("/export/{task_id}")
def export_summary(
    task_id: str,
    fmt: str = Query(default="md", pattern="^(md|txt)$"),
    x_member_token: str | None = Header(default=None),
    user: dict | None = Depends(auth.get_optional_user),
) -> PlainTextResponse:
    with AI_TASKS_LOCK:
        task = AI_TASKS.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if not _can_access_task(task, x_member_token, user):
        raise HTTPException(status_code=403, detail="会员专属功能，请先开通会员")
    if task.get("status") != "completed" or not task.get("result"):
        raise HTTPException(status_code=409, detail="总结尚未完成")

    if fmt == "md":
        content = _render_markdown(task)
        filename = "video-summary.md"
    else:
        content = (task.get("result") or {}).get("summary", "")
        filename = "video-summary.txt"

    return PlainTextResponse(
        content=content,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/export-cache")
def export_cached_summary(
    url: str,
    fmt: str = Query(default="md", pattern="^(md|txt)$"),
    x_member_token: str | None = Header(default=None),
    user: dict | None = Depends(auth.get_optional_user),
) -> PlainTextResponse:
    if not membership.is_member_active(x_member_token, user_id=user["id"] if user else None):
        raise HTTPException(status_code=403, detail="会员专属功能，请先开通会员")
    cached = subtitle_store.get_summary(url.strip())
    if not cached:
        raise HTTPException(status_code=404, detail="历史总结不存在")

    task = {"title": cached.get("title"), "result": cached.get("result") or {}}
    if fmt == "md":
        content = _render_markdown(task)
        filename = "video-summary.md"
    else:
        content = task["result"].get("summary", "")
        filename = "video-summary.txt"
    return PlainTextResponse(
        content=content,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
