"""视频及逐片分析缓存；与用户会话、字幕数据库独立。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import yt_dlp

from .asr import _bilibili_headers
from .subtitle_store import normalize_url
from .video_media import extract_clip, probe_video


from .paths import PROJECT_ROOT as BASE_DIR, database_path
CACHE_DIR = BASE_DIR / "data" / "video_cache"
DB_PATH = database_path("video_cache.db")
_LOCK = threading.RLock()
_STRIPES = [threading.RLock() for _ in range(64)]
_ACTIVE: dict[str, int] = {}


def setting(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(maximum, int(os.getenv(name, str(default)))))
    except ValueError:
        return default


def asset_key(url: str) -> str:
    return hashlib.sha256(normalize_url(url).encode()).hexdigest()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS video_assets (
            asset_key TEXT PRIMARY KEY, source_name TEXT NOT NULL,
            content_hash TEXT NOT NULL, duration REAL NOT NULL, title TEXT,
            last_used REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS video_analyses (
            asset_key TEXT NOT NULL REFERENCES video_assets(asset_key) ON DELETE CASCADE,
            content_hash TEXT NOT NULL, config_id TEXT NOT NULL, chunk_id TEXT NOT NULL,
            query_hash TEXT NOT NULL, result_json TEXT NOT NULL,
            PRIMARY KEY (asset_key, content_hash, config_id, chunk_id, query_hash)
        );
    """)
    return conn


@contextmanager
def session(url: str):
    key = asset_key(url)
    with _LOCK:
        _ACTIVE[key] = _ACTIVE.get(key, 0) + 1
    try:
        with _STRIPES[int(key[:8], 16) % len(_STRIPES)]:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            yield key
    finally:
        with _LOCK:
            _ACTIVE[key] -= 1
            if not _ACTIVE[key]:
                del _ACTIVE[key]
        try:
            cleanup()
        except (OSError, sqlite3.Error):
            pass


def _directory(key: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{64}", key):
        raise ValueError("视频缓存标识无效")
    root = CACHE_DIR.resolve()
    target = (root / key).resolve()
    if target.parent != root or target == root or target.name != key or (root / key).is_symlink():
        raise ValueError("视频缓存目录超出范围")
    return target


def _bytes(path: Path) -> int:
    size = 0
    if path.exists():
        for item in path.rglob("*"):
            try:
                if item.is_file() and not item.is_symlink():
                    size += item.stat().st_size
            except FileNotFoundError:
                pass  # 其他活动请求的临时下载可能已清理。
    return size


def cleanup() -> None:
    """移除空闲缓存，运行中的素材受保护；范围仅限本模块管理的目录。"""
    limit = setting("VIDEO_CACHE_MAX_MB", 1024, 32, 102400) * 1024 * 1024
    cutoff = time.time() - setting("VIDEO_CACHE_TTL_HOURS", 168, 1, 8760) * 3600
    with _LOCK:
        conn = _connect()
        try:
            rows = conn.execute("SELECT asset_key,last_used FROM video_assets ORDER BY last_used").fetchall()
            total = _bytes(CACHE_DIR)
            for row in rows:
                key = row["asset_key"]
                if key in _ACTIVE or (row["last_used"] >= cutoff and total <= limit):
                    continue
                directory = _directory(key)
                size = _bytes(directory)
                if directory.exists():
                    shutil.rmtree(directory)
                conn.execute("DELETE FROM video_assets WHERE asset_key=?", (key,))
                total -= size
            conn.commit()
        finally:
            conn.close()


class _QuietLogger:
    def debug(self, message):
        pass

    info = debug
    warning = debug
    error = debug


def _download(url: str, directory: Path) -> tuple[Path, str]:
    max_bytes = min(setting("VIDEO_SOURCE_MAX_MB", 300, 1, 10240),
                    setting("VIDEO_CACHE_MAX_MB", 1024, 32, 102400)) * 1024 * 1024
    max_seconds = setting("VIDEO_MAX_MINUTES", 30, 1, 180) * 60

    def progress(value):
        if max(value.get("downloaded_bytes") or 0, value.get("total_bytes") or 0) > max_bytes:
            raise RuntimeError("视频文件超过下载体积上限")

    options = {
        "quiet": True, "no_warnings": True, "no_color": True, "logger": _QuietLogger(),
        "noplaylist": True, "format": "bestvideo[height<=720]/best[height<=720]/best",
        "outtmpl": str(directory / "source.%(ext)s"), "socket_timeout": 30,
        "retries": 2, "max_filesize": max_bytes, "progress_hooks": [progress],
        "http_headers": _bilibili_headers(url),
    }
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
            if not isinstance(info, dict) or info.get("entries") is not None:
                raise RuntimeError("请使用单个视频或具体分 P 的链接")
            if float(info.get("duration") or 0) > max_seconds:
                raise RuntimeError("视频超过 VIDEO_MAX_MINUTES 设置的时长上限")
            ydl.download([url])
    except RuntimeError:
        raise
    except Exception:
        raise RuntimeError("视频下载失败，请检查链接和平台登录态") from None
    files = [path for path in directory.glob("source.*")
             if path.is_file() and path.suffix not in {".part", ".ytdl", ".json"}]
    if len(files) != 1:
        raise RuntimeError("下载结果不是单个视频文件，无法可靠对齐字幕")
    if files[0].stat().st_size > max_bytes:
        raise RuntimeError("视频文件超过下载体积上限")
    return files[0], str(info.get("title") or "未知视频")


def get_source(key: str, url: str) -> dict:
    cleanup()
    directory = _directory(key)
    directory.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        conn = _connect()
        try:
            row = conn.execute("SELECT * FROM video_assets WHERE asset_key=?", (key,)).fetchone()
            if row:
                source = (directory / row["source_name"]).resolve()
                if source.parent == directory and source.is_file():
                    try:
                        duration = probe_video(source)
                    except (ValueError, RuntimeError):
                        duration = 0
                    if abs(duration - row["duration"]) <= 0.25 and duration > 0:
                        conn.execute("UPDATE video_assets SET last_used=? WHERE asset_key=?", (time.time(), key))
                        conn.commit()
                        return {**dict(row), "path": source, "cache_hit": True}
        finally:
            conn.close()
    with tempfile.TemporaryDirectory(prefix="download-", dir=directory) as temporary:
        downloaded, title = _download(url, Path(temporary))
        duration = probe_video(downloaded)
        if duration > setting("VIDEO_MAX_MINUTES", 30, 1, 180) * 60:
            raise RuntimeError("视频超过 VIDEO_MAX_MINUTES 设置的时长上限")
        digest = hashlib.sha256()
        with downloaded.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        content_hash = digest.hexdigest()
        # 无法使用的旧素材及其分析从数据库中移除；先提交新文件，再替换记录。
        # 原视频版本进入文件名，旧片段不会与新下载混用。
        target = directory / (content_hash + downloaded.suffix.lower())
        os.replace(downloaded, target)
    for old in directory.iterdir():
        if old.is_file() and old != target:
            old.unlink()
        elif old.is_dir() and old.name != content_hash and re.fullmatch(r"[a-f0-9]{64}", old.name):
            resolved = old.resolve()
            if resolved.parent != directory or resolved.name != old.name or old.is_symlink():
                raise ValueError("旧视频片段目录超出缓存范围")
            shutil.rmtree(resolved)
    with _LOCK:
        conn = _connect()
        try:
            conn.execute("DELETE FROM video_assets WHERE asset_key=?", (key,))
            conn.execute("INSERT INTO video_assets VALUES (?,?,?,?,?,?)",
                         (key, target.name, content_hash, duration, title, time.time()))
            conn.commit()
        finally:
            conn.close()
    cleanup()
    if _bytes(directory) > setting("VIDEO_CACHE_MAX_MB", 1024, 32, 102400) * 1024 * 1024:
        raise RuntimeError("当前视频缓存超过总容量上限")
    return {"asset_key": key, "content_hash": content_hash, "duration": duration,
            "title": title, "path": target, "cache_hit": False}


def get_clip(source: dict, window: dict) -> dict:
    directory = _directory(source["asset_key"]) / source["content_hash"]
    target = directory / (window["id"] + ".mp4")
    if target.is_file():
        try:
            seconds = probe_video(target)
            if abs(seconds - max(2, window["end_seconds"] - window["start_seconds"])) <= 0.25:
                return {**window, "clip_path": str(target), "cache_hit": True}
        except (ValueError, RuntimeError):
            pass
        target.unlink()
    clip = extract_clip(source["path"], window, target)
    cleanup()
    if _bytes(_directory(source["asset_key"])) > setting("VIDEO_CACHE_MAX_MB", 1024, 32, 102400) * 1024 * 1024:
        target.unlink(missing_ok=True)
        raise RuntimeError("当前视频与片段超过缓存容量上限")
    return {**clip, "cache_hit": False}


def get_analysis(source: dict, config_id: str, chunk_id: str, query_hash: str) -> dict | None:
    with _LOCK:
        conn = _connect()
        try:
            row = conn.execute("""SELECT result_json FROM video_analyses
                WHERE asset_key=? AND content_hash=? AND config_id=? AND chunk_id=? AND query_hash=?""",
                (source["asset_key"], source["content_hash"], config_id, chunk_id, query_hash)).fetchone()
            if row:
                try:
                    result = json.loads(row[0])
                    return result if isinstance(result, dict) else None
                except ValueError:
                    pass
            return None
        finally:
            conn.close()


def save_analysis(source: dict, config_id: str, chunk_id: str, query_hash: str, result: dict) -> None:
    with _LOCK:
        conn = _connect()
        try:
            conn.execute("INSERT OR REPLACE INTO video_analyses VALUES (?,?,?,?,?,?)",
                         (source["asset_key"], source["content_hash"], config_id, chunk_id, query_hash,
                          json.dumps(result, ensure_ascii=False)))
            conn.commit()
        finally:
            conn.close()
