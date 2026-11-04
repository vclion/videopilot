"""独立字幕提取接口，不调用大模型，只负责获取并展示完整字幕。"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any
from pathlib import Path
from urllib.parse import quote

from . import auth
from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .subtitle import SubtitleError, fetch_subtitles


router = APIRouter(prefix="/api/subtitles", tags=["subtitles"])
SUBTITLE_TASKS: dict[str, dict[str, Any]] = {}
SUBTITLE_TASKS_LOCK = threading.Lock()


class SubtitleRequest(BaseModel):
    url: str = Field(min_length=4, max_length=2048)


def _now() -> int:
    return int(time.time())


def _new_task_id() -> str:
    return uuid.uuid4().hex[:16]


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


def _can_access(task: dict[str, Any], token: str | None, user: dict[str, Any] | None) -> bool:
    if user:
        return task.get("user_id") == user.get("id")
    return task.get("member_token") == (token or "").strip()


def _update_task(task_id: str, **fields: Any) -> None:
    with SUBTITLE_TASKS_LOCK:
        task = SUBTITLE_TASKS.get(task_id)
        if task:
            task.update(fields)


def _run_subtitle_task(task_id: str, url: str) -> None:
    try:
        _update_task(task_id, status="fetching_subtitle")
        subtitles = fetch_subtitles(
            url,
            on_stage=lambda status: _update_task(task_id, status=status),
        )
        transcript = (subtitles.get("transcript") or "").strip()
        if not transcript:
            raise SubtitleError("字幕内容为空")
        transcript_file = subtitles.get("transcript_file")
        segments_file = subtitles.get("segments_file")
        if not transcript_file:
            from .subtitle_store import persist_transcript

            transcript_file, segments_file = persist_transcript(
                subtitles.get("title") or "video-subtitle",
                subtitles.get("language") or "",
                transcript,
                subtitles.get("segments") or [],
                source=subtitles.get("source") or "subtitle",
            )
        _update_task(
            task_id,
            status="completed",
            source=subtitles.get("source"),
            language=subtitles.get("language"),
            title=subtitles.get("title"),
            result={
                "transcript": transcript,
                "segments": subtitles.get("segments") or [],
                "transcript_file": transcript_file,
                "segments_file": segments_file,
                "transcript_path": subtitles.get("transcript_path"),
                "transcript_url": (
                    f"/api/subtitles/file/{quote(Path(transcript_file).name)}"
                    if transcript_file
                    else None
                ),
            },
            finished_at=_now(),
        )
    except (SubtitleError, RuntimeError) as exc:
        _update_task(task_id, status="failed", error=str(exc), finished_at=_now())
    except Exception as exc:
        _update_task(task_id, status="failed", error=f"字幕提取失败：{exc}", finished_at=_now())


@router.post("")
def create_subtitle_task(
    payload: SubtitleRequest,
    x_member_token: str | None = Header(default=None),
    user: dict[str, Any] | None = Depends(auth.get_optional_user),
) -> dict[str, Any]:
    url = payload.url.strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(status_code=400, detail="请输入有效的 http/https 链接")
    task_id = _new_task_id()
    task: dict[str, Any] = {
        "id": task_id,
        "url": url,
        "member_token": (x_member_token or "").strip(),
        "user_id": user["id"] if user else None,
        "status": "queued",
        "source": None,
        "language": None,
        "title": None,
        "error": None,
        "result": None,
        "created_at": _now(),
        "finished_at": None,
    }
    with SUBTITLE_TASKS_LOCK:
        SUBTITLE_TASKS[task_id] = task

    thread = threading.Thread(
        target=_run_subtitle_task,
        args=(task_id, url),
        name=f"subtitle-task-{task_id}",
        daemon=True,
    )
    thread.start()
    return _public_task(task)


@router.get("/task/{task_id}")
def get_subtitle_task(
    task_id: str,
    x_member_token: str | None = Header(default=None),
    user: dict[str, Any] | None = Depends(auth.get_optional_user),
) -> dict[str, Any]:
    with SUBTITLE_TASKS_LOCK:
        task = SUBTITLE_TASKS.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="字幕任务不存在")
    if not _can_access(task, x_member_token, user):
        raise HTTPException(status_code=403, detail="无权访问该字幕任务")
    return _public_task(task)


@router.get("/file/{filename:path}")
def get_transcript_file(filename: str) -> FileResponse:
    """打开已保存的字幕文本，只允许访问转写目录中的 txt 文件。"""
    from .subtitle_store import TRANSCRIPT_DIR

    root = TRANSCRIPT_DIR.resolve()
    target = (root / filename).resolve()
    if root not in target.parents or target.suffix.lower() != ".txt" or not target.is_file():
        raise HTTPException(status_code=404, detail="字幕文件不存在")
    return FileResponse(
        target,
        media_type="text/plain; charset=utf-8",
        filename=target.name,
    )
