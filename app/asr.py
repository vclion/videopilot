"""本地语音转写备用工具。

仅在平台字幕和 yt-dlp 字幕都不可用时调用 faster-whisper。
模型文件由 faster-whisper 缓存在用户本机，不放入项目仓库。
"""

from __future__ import annotations

import os
import threading
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yt_dlp
from dotenv import load_dotenv

from .subtitle_store import persist_transcript


from .paths import PROJECT_ROOT as BASE_DIR
load_dotenv(BASE_DIR / ".env")
# Hugging Face 的 Xet 分片在部分网络环境下会长时间无响应，
# 优先使用普通 HTTP 下载，避免 AI 任务一直停留在“获取字幕”。
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

ASR_ENABLED = os.getenv("ASR_ENABLED", "true").strip().lower() not in {"0", "false", "no", "off"}
ASR_MODEL = os.getenv("ASR_MODEL", "small").strip() or "small"
ASR_DEVICE = os.getenv("ASR_DEVICE", "cpu").strip() or "cpu"
ASR_COMPUTE_TYPE = os.getenv("ASR_COMPUTE_TYPE", "int8").strip() or "int8"
ASR_LANGUAGE = os.getenv("ASR_LANGUAGE", "auto").strip().lower()
ASR_MAX_MINUTES = int(os.getenv("ASR_MAX_MINUTES", "60"))

_MODEL = None
_MODEL_LOCK = threading.Lock()


class ASRError(Exception):
    """语音转写失败。"""


def is_enabled() -> bool:
    return ASR_ENABLED


def _bilibili_headers(url: str) -> dict[str, str]:
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    if host != "bilibili.com" and not host.endswith(".bilibili.com"):
        return {}

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.bilibili.com/",
    }
    sessdata = os.getenv("BILIBILI_SESSDATA", "").strip()
    if sessdata:
        headers["Cookie"] = f"SESSDATA={sessdata}"
    return headers


def _get_model():
    global _MODEL
    if _MODEL is not None:
        return _MODEL
    with _MODEL_LOCK:
        if _MODEL is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:
                raise ASRError(
                    "未安装 faster-whisper，请执行 pip install -r requirements.txt"
                ) from exc
            try:
                _MODEL = WhisperModel(
                    ASR_MODEL,
                    device=ASR_DEVICE,
                    compute_type=ASR_COMPUTE_TYPE,
                )
            except Exception as exc:
                raise ASRError(f"语音模型加载失败：{exc}") from exc
    return _MODEL


def _download_audio(url: str, directory: str) -> tuple[Path, str]:
    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "no_color": True,
        "noplaylist": True,
        "format": "bestaudio/best",
        "outtmpl": str(Path(directory) / "audio.%(ext)s"),
        "socket_timeout": 30,
        "retries": 3,
    }
    headers = _bilibili_headers(url)
    if headers:
        options["http_headers"] = headers

    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info:
                raise ASRError("无法获取视频音频信息")
            duration = float(info.get("duration") or 0)
            if duration and duration > ASR_MAX_MINUTES * 60:
                raise ASRError(f"视频超过语音转写上限（{ASR_MAX_MINUTES} 分钟）")
            ydl.download([url])
            title = info.get("title") or info.get("id") or "未知视频"
    except ASRError:
        raise
    except Exception as exc:
        raise ASRError(f"音频下载失败：{exc}") from exc

    candidates = [
        path
        for path in Path(directory).glob("audio.*")
        if path.is_file() and path.suffix not in {".part", ".ytdl"}
    ]
    if not candidates:
        raise ASRError("音频下载完成，但没有找到音频文件")
    return max(candidates, key=lambda path: path.stat().st_size), title


def transcribe_url(url: str) -> dict[str, Any]:
    """下载音频并返回带时间戳的转写结果。"""
    if not ASR_ENABLED:
        raise ASRError("本地语音转写未启用")

    with tempfile.TemporaryDirectory(prefix="asr-") as directory:
        audio_path, title = _download_audio(url, directory)
        model = _get_model()
        language = None if ASR_LANGUAGE in {"", "auto", "detect"} else ASR_LANGUAGE
        try:
            segments, info = model.transcribe(
                str(audio_path),
                language=language,
                beam_size=5,
                vad_filter=True,
                condition_on_previous_text=False,
            )
            transcript_segments: list[dict[str, Any]] = []
            text_parts: list[str] = []
            for segment in segments:
                text = (segment.text or "").strip()
                if not text:
                    continue
                transcript_segments.append(
                    {
                        "start": round(float(segment.start), 2),
                        "end": round(float(segment.end), 2),
                        "text": text,
                    }
                )
                text_parts.append(text)
        except Exception as exc:
            raise ASRError(f"语音转写失败：{exc}") from exc

    transcript = "\n".join(text_parts).strip()
    if not transcript:
        raise ASRError("音频中没有识别到可用语音")

    language = getattr(info, "language", "") or ""
    try:
        transcript_file, segments_file = persist_transcript(
            title,
            language,
            transcript,
            transcript_segments,
        )
    except OSError as exc:
        raise ASRError(f"语音转写完成，但字幕文件保存失败：{exc}") from exc

    return {
        "source": "faster_whisper",
        "language": language,
        "title": title,
        "transcript": transcript,
        "segments": transcript_segments,
        "transcript_file": transcript_file,
        "segments_file": segments_file,
    }
