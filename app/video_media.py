"""带时间字幕与 10 秒视频切片工具，由离线验证和线上视频增强共用。"""

from __future__ import annotations

import html
import json
import math
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any


CHUNK_SECONDS = 10


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} 必须是有效秒数")
    try:
        number = float(value)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{label} 必须是有效秒数") from exc
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{label} 必须是非负有限秒数")
    return number


def normalize_segments(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """兼容 ASR 的 start/end/text 和 B 站的 from/to/content。"""
    result = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("字幕片段必须是对象")
        start = _number(item.get("start", item.get("from")), "字幕起点")
        end = _number(item.get("end", item.get("to")), "字幕终点")
        if end <= start:
            raise ValueError("字幕终点必须大于起点")
        text = str(item.get("text", item.get("content", ""))).strip()
        if text:
            result.append({"start": start, "end": end, "text": text})
    return sorted(result, key=lambda item: (item["start"], item["end"]))


def _timestamp(value: str) -> float:
    parts = value.replace(",", ".").split(":")
    if len(parts) not in (2, 3):
        raise ValueError("字幕时间格式无效")
    seconds = _number(parts[-1], "字幕秒数")
    minutes = _number(parts[-2], "字幕分钟")
    if seconds >= 60 or (len(parts) == 3 and minutes >= 60):
        raise ValueError("字幕时间格式无效")
    return seconds + minutes * 60 + (float(parts[0]) * 3600 if len(parts) == 3 else 0)


def load_segments(path: Path | None) -> list[dict[str, Any]]:
    if path is None:
        return []
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() == ".json":
        data = json.loads(text)
        if isinstance(data, dict):
            data = data.get("segments", data.get("body"))
        if not isinstance(data, list) or not data:
            raise ValueError("JSON 字幕缺少带时间戳的 segments/body；不能从纯文本推算时间")
        return normalize_segments(data)
    if path.suffix.lower() not in {".srt", ".vtt"}:
        raise ValueError("仅支持带时间戳的 JSON、SRT 或 VTT 字幕")
    segments = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n")):
        lines = block.strip().splitlines()
        if not lines or lines[0].startswith(("NOTE", "STYLE", "REGION")):
            continue
        for index, line in enumerate(lines):
            match = re.match(r"\s*([\d:.,]+)\s*-->\s*([\d:.,]+)", line)
            if match:
                content = html.unescape(re.sub(r"<[^>]+>", "", "\n".join(lines[index + 1:])))
                segments.append({"start": _timestamp(match[1]), "end": _timestamp(match[2]), "text": content})
                break
    if not segments:
        raise ValueError("未找到有效的字幕时间戳")
    return normalize_segments(segments)


def build_windows(segments: list[dict[str, Any]], duration: float) -> list[dict[str, Any]]:
    duration = _number(duration, "视频时长")
    if duration <= 0:
        raise ValueError("视频时长必须大于零")
    segments = normalize_segments(segments)
    windows = []
    for index in range(math.ceil(duration / CHUNK_SECONDS)):
        start = index * CHUNK_SECONDS
        end = min(start + CHUNK_SECONDS, duration)
        overlapping = [item for item in segments if item["start"] < end and item["end"] > start]
        windows.append({
            "id": f"chunk-{index:06d}", "start_seconds": start, "end_seconds": end,
            "text": "\n".join(dict.fromkeys(item["text"] for item in overlapping)),
        })
    return windows


def _run_media(args: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    if not shutil.which(args[0]):
        raise RuntimeError(f"缺少 {args[0]}，请安装 FFmpeg")
    try:
        return subprocess.run(args, check=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{args[0]} 处理超时") from exc
    except subprocess.CalledProcessError as exc:
        # 不向日志转存进程参数和原始输出。
        raise RuntimeError(f"{args[0]} 处理失败，请检查输入媒体") from exc


def probe_video(path: Path) -> float:
    if not path.is_file():
        raise ValueError("本地视频文件不存在")
    result = _run_media([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "format=duration:stream=codec_type,duration", "-of", "json", str(path),
    ])
    data = json.loads(result.stdout)
    streams = data.get("streams") or []
    if not streams or streams[0].get("codec_type") != "video":
        raise ValueError("文件没有视频轨道")
    duration = streams[0].get("duration")
    if duration in (None, "N/A"):
        duration = data.get("format", {}).get("duration")
    value = _number(duration, "视频时长")
    if value <= 0:
        raise ValueError("视频时长必须大于零")
    return value


def extract_clip(source: Path, window: dict[str, Any], target: Path) -> dict[str, Any]:
    start = _number(window["start_seconds"], "切片起点")
    end = _number(window["end_seconds"], "切片终点")
    duration = end - start
    if duration <= 0 or duration > CHUNK_SECONDS:
        raise ValueError("切片时长必须在 0 到 10 秒之间")
    if end > probe_video(source) + 0.01:
        raise ValueError("切片超出原视频范围")
    if target.exists():
        raise ValueError("切片文件已存在，请使用新的输出目录")
    target.parent.mkdir(parents=True, exist_ok=True)
    padding = max(0, 2 - duration)  # Qwen 视频输入最短 2 秒；仅重复最后一帧。
    filters = (
        f"trim=duration={duration},setpts=PTS-STARTPTS,"
        "scale=w='min(640,iw)':h='min(480,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2,"
        f"setsar=1,fps=12,tpad=stop_mode=clone:stop_duration={padding}"
    )
    try:
        _run_media([
            "ffmpeg", "-nostdin", "-v", "error", "-n", "-ss", str(start), "-i", str(source),
            "-map", "0:v:0", "-an", "-vf", filters, "-t", str(max(2, duration)),
            "-c:v", "libx264", "-preset", "fast", "-crf", "24", "-maxrate", "1M", "-bufsize", "2M",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(target),
        ])
        actual = probe_video(target)
        if abs(actual - max(2, duration)) > 0.25:
            raise RuntimeError("输出切片时长异常")
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return {**window, "clip_path": str(target.resolve()), "padding_seconds": padding, "media_seconds": actual}


def subtitle_evidence(clips: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [{
        "id": "subtitle-" + clip["id"],
        "text": f"字幕原文，原视频 {clip['start_seconds']}–{clip['end_seconds']} 秒：\n{clip['text']}",
    } for clip in clips if clip["text"]]
