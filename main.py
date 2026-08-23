from __future__ import annotations

import glob
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Literal

import yt_dlp
from douyin import (
    download as download_douyin,
    extract_info as extract_douyin_info,
    is_douyin_url,
)
from ai_routes import router as ai_router
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DOWNLOAD_DIR = BASE_DIR / "downloads"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

MAX_HISTORY = 50
TASKS: dict[str, dict[str, Any]] = {}
TASKS_LOCK = threading.Lock()


class ParseRequest(BaseModel):
    url: str = Field(min_length=4, max_length=2048)


class DownloadRequest(BaseModel):
    url: str = Field(min_length=4, max_length=2048)
    format_id: str = "bestvideo+bestaudio/best"
    kind: Literal["video", "audio"] = "video"


app = FastAPI(title="Free Videos Downloader", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _now() -> int:
    return int(time.time())


def _new_task_id() -> str:
    return uuid.uuid4().hex[:16]


def _is_http_url(url: str) -> bool:
    value = url.strip()
    return value.startswith("http://") or value.startswith("https://")


def _update_task(task_id: str, **fields: Any) -> None:
    with TASKS_LOCK:
        task = TASKS.get(task_id)
        if task:
            task.update(fields)


def _public_task(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": task.get("id"),
        "url": task.get("url"),
        "title": task.get("title"),
        "thumbnail": task.get("thumbnail"),
        "status": task.get("status"),
        "kind": task.get("kind"),
        "quality": task.get("quality"),
        "progress": task.get("progress", 0),
        "speed": task.get("speed"),
        "eta": task.get("eta"),
        "error": task.get("error"),
        "file_name": task.get("file_name"),
        "file_size": task.get("file_size"),
        "created_at": task.get("created_at"),
        "finished_at": task.get("finished_at"),
    }


def _extract_info(url: str) -> dict[str, Any]:
    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": 20,
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=False)
        if info is None:
            raise RuntimeError("无法解析该链接")

        if info.get("_type") == "playlist" and info.get("entries"):
            for entry in info["entries"]:
                if entry:
                    info = entry
                    break

        if not info:
            raise RuntimeError("链接中没有找到可下载的内容")

        return info


def _collect_heights(info: dict[str, Any]) -> set[int]:
    heights: set[int] = set()
    for fmt in info.get("formats", []):
        if fmt.get("vcodec") in (None, "none"):
            continue

        height = fmt.get("height")
        if height:
            heights.add(int(height))

        note = str(fmt.get("format_note") or "").strip().lower()
        match = re.search(r"(\d{3,4})p", note)
        if match:
            heights.add(int(match.group(1)))

        resolution = str(fmt.get("resolution") or "")
        if "x" in resolution:
            height_part = resolution.rsplit("x", 1)[-1]
            if height_part.isdigit():
                heights.add(int(height_part))

    return {h for h in heights if 180 <= h <= 4320}


def _build_video_options(info: dict[str, Any]) -> list[dict[str, str]]:
    options: list[dict[str, str]] = [
        {
            "id": "bestvideo+bestaudio/best",
            "label": "最佳画质",
            "hint": "自动选择最高可用画质",
        }
    ]

    heights = sorted(_collect_heights(info), reverse=True)[:5]
    for height in heights:
        label = {
            4320: "8K",
            2160: "4K",
            1440: "2K",
            1080: "1080p",
            720: "720p",
            576: "540p",
            540: "540p",
            480: "480p",
            360: "360p",
        }.get(height, f"{height}p")
        options.append(
            {
                "id": f"bestvideo[height<={height}]+bestaudio/best[height<={height}]",
                "label": label,
                "hint": f"最高不超过 {height}p",
            }
        )

    return options


def _format_duration(duration: int | float | None) -> str:
    if not duration:
        return ""
    total = int(duration)
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "time": _now()}


@app.post("/api/parse")
def parse_video(payload: ParseRequest) -> dict[str, Any]:
    if not _is_http_url(payload.url):
        raise HTTPException(status_code=400, detail="请输入有效的 http/https 链接")

    if is_douyin_url(payload.url):
        try:
            return extract_douyin_info(payload.url)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"解析失败：{exc}") from exc

    try:
        info = _extract_info(payload.url)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"解析失败：{exc}") from exc

    return {
        "id": info.get("id"),
        "title": info.get("title") or info.get("id") or "未知视频",
        "thumbnail": info.get("thumbnail"),
        "duration": _format_duration(info.get("duration")),
        "duration_seconds": info.get("duration"),
        "uploader": info.get("uploader"),
        "extractor": info.get("extractor_key") or info.get("extractor"),
        "webpage_url": info.get("webpage_url") or payload.url,
        "videoOptions": _build_video_options(info),
        "audioOption": {
            "id": "bestaudio/best",
            "label": "MP3 音频",
            "hint": "提取为 MP3，适合播客和音乐",
        },
    }


@app.post("/api/download")
def create_download(payload: DownloadRequest) -> dict[str, Any]:
    if not _is_http_url(payload.url):
        raise HTTPException(status_code=400, detail="请输入有效的 http/https 链接")

    task_id = _new_task_id()
    task: dict[str, Any] = {
        "id": task_id,
        "url": payload.url,
        "kind": payload.kind,
        "format_id": payload.format_id,
        "quality": "MP3" if payload.kind == "audio" else payload.format_id,
        "status": "queued",
        "title": None,
        "thumbnail": None,
        "progress": 0,
        "speed": None,
        "eta": None,
        "error": None,
        "file_name": None,
        "file_path": None,
        "file_size": None,
        "created_at": _now(),
        "finished_at": None,
    }

    with TASKS_LOCK:
        TASKS[task_id] = task

    thread = threading.Thread(
        target=_run_download,
        args=(task_id, payload),
        name=f"download-{task_id}",
        daemon=True,
    )
    thread.start()
    return _public_task(task)


def _progress_hook(task_id: str) -> Any:
    def hook(status: dict[str, Any]) -> None:
        if status.get("status") != "downloading":
            if status.get("status") == "finished":
                _update_task(task_id, progress=100)
            return

        downloaded = status.get("downloaded_bytes") or 0
        total = status.get("total_bytes") or status.get("total_bytes_estimate") or 0
        progress = 0
        if total:
            progress = min(99, round(downloaded / total * 100, 1))

        _update_task(
            task_id,
            status="downloading",
            progress=progress,
            speed=status.get("speed"),
            eta=status.get("eta"),
        )

    return hook


def _find_downloaded_file(task_id: str, kind: str) -> Path:
    pattern = str(DOWNLOAD_DIR / f"{task_id}.*")
    candidates = [Path(path) for path in glob.glob(pattern)]
    candidates = [p for p in candidates if p.is_file() and p.suffix not in {".part", ".ytdl"}]

    if not candidates:
        raise RuntimeError("下载完成，但没有找到生成的文件")

    if kind == "audio":
        preferred = [p for p in candidates if p.suffix.lower() in {".mp3", ".m4a", ".opus", ".flac"}]
    else:
        preferred = [p for p in candidates if p.suffix.lower() in {".mp4", ".webm", ".mkv"}]

    pool = preferred or candidates
    return max(pool, key=lambda path: path.stat().st_size)


def _run_download(task_id: str, payload: DownloadRequest) -> None:
    _update_task(task_id, status="downloading", progress=0)

    try:
        if is_douyin_url(payload.url):
            result = download_douyin(
                payload.url,
                payload.format_id,
                payload.kind,
                task_id,
                DOWNLOAD_DIR,
                on_progress=lambda progress: _update_task(task_id, progress=progress),
            )
            _update_task(
                task_id,
                title=result["title"],
                thumbnail=result["thumbnail"],
                status="completed",
                progress=100,
                speed=None,
                eta=None,
                file_name=result["file_name"],
                file_path=str(result["file_path"]),
                file_size=result["file_size"],
                finished_at=_now(),
            )
            return

        info = _extract_info(payload.url)
        _update_task(
            task_id,
            title=info.get("title"),
            thumbnail=info.get("thumbnail"),
        )

        options: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "retries": 3,
            "socket_timeout": 30,
            "outtmpl": str(DOWNLOAD_DIR / f"{task_id}.%(ext)s"),
            "progress_hooks": [_progress_hook(task_id)],
        }

        if payload.kind == "audio":
            options["format"] = payload.format_id or "bestaudio/best"
            options["postprocessors"] = [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            ]
        else:
            options["format"] = payload.format_id or "bestvideo+bestaudio/best"
            options["merge_output_format"] = "mp4"

        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.download([payload.url])

        file_path = _find_downloaded_file(task_id, payload.kind)
        _update_task(
            task_id,
            status="completed",
            progress=100,
            speed=None,
            eta=None,
            file_name=file_path.name,
            file_path=str(file_path),
            file_size=file_path.stat().st_size,
            finished_at=_now(),
        )
    except Exception as exc:
        _update_task(
            task_id,
            status="failed",
            error=str(exc),
            finished_at=_now(),
        )


@app.get("/api/task/{task_id}")
def get_task(task_id: str) -> dict[str, Any]:
    with TASKS_LOCK:
        task = TASKS.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    return _public_task(task)


@app.get("/api/history")
def get_history(limit: int = Query(default=20, ge=1, le=MAX_HISTORY)) -> dict[str, Any]:
    with TASKS_LOCK:
        tasks = list(TASKS.values())
    tasks.sort(key=lambda item: item.get("created_at", 0), reverse=True)
    return {"tasks": [_public_task(item) for item in tasks[:limit]]}


@app.get("/api/file/{task_id}")
def download_file(task_id: str) -> FileResponse:
    with TASKS_LOCK:
        task = TASKS.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    if task.get("status") != "completed":
        raise HTTPException(status_code=409, detail="文件尚未准备好")

    file_path = Path(task.get("file_path") or "")
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="文件已被清理")

    return FileResponse(
        path=str(file_path),
        media_type="application/octet-stream",
        filename=task.get("file_name") or file_path.name,
    )


app.include_router(ai_router)


app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
