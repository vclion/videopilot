"""字幕缓存与字幕文件存储。"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


from .paths import PROJECT_ROOT as BASE_DIR, database_path
DB_PATH = database_path("subtitle_cache.db")
TRANSCRIPT_DIR = BASE_DIR / "data" / "transcripts"
LOCK = threading.Lock()
TRACKING_PARAMS = {
    "trackid",
    "from_spmid",
    "spm_id_from",
    "vd_source",
    "share_source",
    "share_medium",
    "share_plat",
    "share_tag",
}


def _now() -> int:
    return int(time.time())


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with LOCK:
        conn = _connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS subtitle_cache (
                    url_key TEXT PRIMARY KEY,
                    original_url TEXT NOT NULL,
                    source TEXT,
                    language TEXT,
                    title TEXT,
                    transcript TEXT NOT NULL,
                    segments_json TEXT,
                    transcript_file TEXT,
                    segments_file TEXT,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_subtitle_cache_updated "
                "ON subtitle_cache(updated_at DESC)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ai_summary_cache (
                    url_key TEXT PRIMARY KEY,
                    original_url TEXT NOT NULL,
                    source TEXT,
                    language TEXT,
                    title TEXT,
                    result_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ai_summary_cache_updated "
                "ON ai_summary_cache(updated_at DESC)"
            )
            conn.commit()
        finally:
            conn.close()


def normalize_url(url: str) -> str:
    """去掉片段和常见分享追踪参数，避免同一视频重复提取。"""
    value = url.strip()
    parsed = urlsplit(value)
    query = [
        (key, item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in TRACKING_PARAMS
    ]
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            path,
            urlencode(sorted(query)),
            "",
        )
    )


def _safe_title(value: str) -> str:
    title = re.sub(r"[^\w\u4e00-\u9fff.-]+", "-", value or "transcript").strip(".-")
    return (title or "transcript")[:80]


def display_path(relative_path: str) -> str:
    """返回用户可直接定位的本机绝对路径。"""
    absolute_path = (BASE_DIR / relative_path).resolve()
    distro = os.environ.get("WSL_DISTRO_NAME", "") if os.name != "nt" else ""
    if distro:
        windows_path = str(absolute_path).replace("/", "\\")
        return f"\\\\wsl.localhost\\{distro}{windows_path}"
    return str(absolute_path)


def persist_transcript(
    title: str,
    language: str,
    transcript: str,
    segments: list[dict[str, Any]],
    source: str = "faster_whisper",
) -> tuple[str, str]:
    """保存正文和结构化字幕，返回相对项目根目录的路径。"""
    output_dir = TRANSCRIPT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    suffix = uuid.uuid4().hex[:8]
    stem = f"{_safe_title(title)}-{stamp}-{suffix}"
    text_path = output_dir / f"{stem}.txt"
    json_path = output_dir / f"{stem}.json"
    text_path.write_text(transcript.rstrip() + "\n", encoding="utf-8")
    json_path.write_text(
        json.dumps(
            {
                "source": source,
                "title": title,
                "language": language,
                "transcript": transcript,
                "segments": segments,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return text_path.relative_to(BASE_DIR).as_posix(), json_path.relative_to(BASE_DIR).as_posix()


def save(url: str, result: dict[str, Any]) -> dict[str, Any]:
    transcript = (result.get("transcript") or "").strip()
    if not transcript:
        return result

    enriched = dict(result)
    segments = result.get("segments") or []
    transcript_file = result.get("transcript_file")
    segments_file = result.get("segments_file")
    if not transcript_file or not (BASE_DIR / transcript_file).is_file():
        transcript_file, segments_file = persist_transcript(
            result.get("title") or "video-subtitle",
            result.get("language") or "",
            transcript,
            segments,
            source=result.get("source") or "subtitle",
        )
    enriched.update(
        {
            "transcript": transcript,
            "segments": segments,
            "transcript_file": transcript_file,
            "segments_file": segments_file,
            "transcript_path": display_path(transcript_file),
        }
    )
    now = _now()
    with LOCK:
        conn = _connect()
        try:
            conn.execute(
                """
                INSERT INTO subtitle_cache
                    (url_key, original_url, source, language, title, transcript,
                     segments_json, transcript_file, segments_file, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(url_key) DO UPDATE SET
                    original_url = excluded.original_url,
                    source = excluded.source,
                    language = excluded.language,
                    title = excluded.title,
                    transcript = excluded.transcript,
                    segments_json = excluded.segments_json,
                    transcript_file = excluded.transcript_file,
                    segments_file = excluded.segments_file,
                    updated_at = excluded.updated_at
                """,
                (
                    normalize_url(url),
                    url.strip(),
                    enriched.get("source"),
                    enriched.get("language"),
                    enriched.get("title"),
                    transcript,
                    json.dumps(segments, ensure_ascii=False),
                    transcript_file,
                    segments_file,
                    now,
                    now,
                ),
            )
            conn.commit()
        finally:
            conn.close()
    return enriched


def get(url: str) -> dict[str, Any] | None:
    url_key = normalize_url(url)
    with LOCK:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT * FROM subtitle_cache WHERE url_key = ?",
                (url_key,),
            ).fetchone()
        finally:
            conn.close()
    if not row:
        return None

    try:
        segments = json.loads(row["segments_json"] or "[]")
    except json.JSONDecodeError:
        segments = []
    return {
        "source": row["source"] or "subtitle_cache",
        "language": row["language"] or "",
        "title": row["title"] or "",
        "transcript": row["transcript"] or "",
        "segments": segments if isinstance(segments, list) else [],
        "transcript_file": row["transcript_file"],
        "segments_file": row["segments_file"],
        "transcript_path": display_path(row["transcript_file"]) if row["transcript_file"] else None,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "cache_hit": True,
    }


def save_summary(
    url: str,
    title: str,
    source: str,
    language: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    """按视频 URL 持久化 AI 总结，供后续解析时直接恢复。"""
    now = _now()
    url_key = normalize_url(url)
    with LOCK:
        conn = _connect()
        try:
            conn.execute(
                """
                INSERT INTO ai_summary_cache
                    (url_key, original_url, source, language, title, result_json,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(url_key) DO UPDATE SET
                    original_url = excluded.original_url,
                    source = excluded.source,
                    language = excluded.language,
                    title = excluded.title,
                    result_json = excluded.result_json,
                    updated_at = excluded.updated_at
                """,
                (
                    url_key,
                    url.strip(),
                    source or "",
                    language or "",
                    title or "",
                    json.dumps(result, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            conn.commit()
        finally:
            conn.close()
    return {
        "url": url.strip(),
        "source": source or "",
        "language": language or "",
        "title": title or "",
        "result": result,
        "created_at": now,
        "updated_at": now,
    }


def get_summary(url: str) -> dict[str, Any] | None:
    """读取指定视频最近一次 AI 总结。"""
    url_key = normalize_url(url)
    with LOCK:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT * FROM ai_summary_cache WHERE url_key = ?",
                (url_key,),
            ).fetchone()
        finally:
            conn.close()
    if not row:
        return None
    try:
        result = json.loads(row["result_json"] or "{}")
    except json.JSONDecodeError:
        result = {}
    return {
        "url": row["original_url"],
        "source": row["source"] or "",
        "language": row["language"] or "",
        "title": row["title"] or "",
        "result": result if isinstance(result, dict) else {},
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "cache_hit": True,
    }


init_db()
