from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

import requests


UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/16.0 Mobile/15E148 Safari/604.1"
)
HEADERS = {
    "User-Agent": UA,
    "Referer": "https://www.douyin.com/",
}


def is_douyin_url(url: str) -> bool:
    host = (urlparse(url.strip()).hostname or "").lower()
    return (
        host in {"douyin.com", "iesdouyin.com"}
        or host.endswith(".douyin.com")
        or host.endswith(".iesdouyin.com")
    )


def _extract_id_from_path_or_query(url: str) -> tuple[str | None, str | None]:
    parsed = urlparse(url.strip())
    path = parsed.path or ""

    for key in ("modal_id", "aweme_id", "item_id", "video_id", "note_id"):
        values = parse_qs(parsed.query).get(key) or []
        for value in values:
            if re.fullmatch(r"\d{15,}", value):
                return value, "note" if key == "note_id" else "video"

    for content_type in ("video", "note"):
        match = re.search(rf"/({content_type})/(\d{{15,}})", path)
        if match:
            return match.group(2), content_type

    match = re.search(r"/share/(?:video|note)/(\d{15,})", path)
    if match:
        return match.group(1), "video"

    match = re.search(r"/(\d{15,})", path)
    if match:
        return match.group(1), "video"

    return None, None


def normalize_url(url: str) -> tuple[str, str]:
    """返回 (content_id, content_type)。"""
    url = url.strip()
    content_id, content_type = _extract_id_from_path_or_query(url)
    if content_id:
        return content_id, content_type or "video"

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host.endswith("v.douyin.com") or host.endswith(".douyin.com"):
        try:
            response = requests.get(url, headers=HEADERS, allow_redirects=True, timeout=15)
            if response.url and response.url != url:
                return normalize_url(response.url)
        except Exception:
            pass

    match = re.search(r"(\d{15,})", url)
    if match:
        return match.group(1), "video"

    raise ValueError("无法从链接中识别抖音视频 ID")


def _register_ttwid() -> str | None:
    try:
        session = requests.Session()
        session.headers.update({"User-Agent": UA})
        payload = {
            "region": "cn",
            "aid": 1768,
            "needFid": False,
            "service": "www.ixigua.com",
            "migrate_info": {},
            "cbUrlProtocol": "https",
            "union": True,
        }
        response = session.post(
            "https://ttwid.bytedance.com/ttwid/union/register/",
            json=payload,
            timeout=15,
        )
        redirect_url = response.json().get("redirect_url", "")
        ticket = re.search(r"ticket=([^&]+)", redirect_url)
        if not ticket:
            return None

        session.get(
            "https://www.ixigua.com/ttwid/union/register/callback/"
            f"?aid=1768&ticket={ticket.group(1)}",
            timeout=15,
            allow_redirects=False,
        )
        for cookie in session.cookies:
            if cookie.name == "ttwid":
                return cookie.value
    except Exception:
        return None
    return None


def _get_detail(content_id: str) -> dict[str, Any] | None:
    api_url = (
        "https://www.douyin.com/aweme/v1/web/aweme/detail/"
        f"?aweme_id={content_id}"
        "&device_platform=webapp&aid=6383&channel=channel_pc_web"
    )
    request_headers = dict(HEADERS)

    for attempt in range(2):
        try:
            response = requests.get(api_url, headers=request_headers, timeout=20)
            if response.status_code == 200 and response.text.strip():
                data = response.json()
                if data.get("aweme_detail"):
                    return data
        except Exception:
            pass

        if attempt == 0:
            ttwid = _register_ttwid()
            if ttwid:
                request_headers["Cookie"] = f"ttwid={ttwid}"

    return None


def _first_url(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    url_list = value.get("url_list") or []
    for url in url_list:
        if isinstance(url, str) and "douyinvod.com" in url:
            return url
    for url in url_list:
        if isinstance(url, str):
            return url
    return None


def _format_duration(seconds: int | float | None) -> str:
    if not seconds:
        return ""
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _bit_rate_rank(bit_rate: dict[str, Any]) -> tuple[int, int]:
    gear = bit_rate.get("gear_name") or ""
    play_addr = bit_rate.get("play_addr") or {}
    url_key = play_addr.get("url_key") or bit_rate.get("url_key") or ""
    quality = 0 if "normal" in gear else 1 if "low" in gear else 2
    codec = 0 if "h264" in url_key else 1
    return quality, codec


def _best_bit_rate_by_height(video: dict[str, Any]) -> dict[int, dict[str, Any]]:
    grouped: dict[int, dict[str, Any]] = {}
    for bit_rate in video.get("bit_rate") or []:
        play_addr = bit_rate.get("play_addr") or {}
        height = play_addr.get("height")
        if not height or not _first_url(play_addr):
            continue
        height = int(height)
        current = grouped.get(height)
        if current is None or _bit_rate_rank(bit_rate) < _bit_rate_rank(current):
            grouped[height] = bit_rate
    return grouped


def _build_douyin_video_options(video: dict[str, Any]) -> list[dict[str, str]]:
    options: list[dict[str, str]] = []
    seen_labels: set[str] = set()
    for height in sorted(_best_bit_rate_by_height(video), reverse=True):
        label = "540p" if height == 576 else f"{height}p"
        if label in seen_labels:
            continue
        seen_labels.add(label)
        options.append(
            {
                "id": f"douyin_{height}",
                "label": label,
                "hint": f"下载 {label} 视频",
            }
        )
    if not options:
        fallback = _first_url(video.get("play_addr_h264")) or _first_url(video.get("play_addr"))
        if fallback:
            options.append(
                {
                    "id": "douyin_play",
                    "label": "原画质",
                    "hint": "使用抖音默认播放地址下载",
                }
            )
    return options


def _build_info(content_id: str, aweme_detail: dict[str, Any]) -> dict[str, Any]:
    video = aweme_detail.get("video") or {}
    author = aweme_detail.get("author") or {}

    video_options = _build_douyin_video_options(video)

    thumbnail = (
        _first_url(video.get("cover"))
        or _first_url(video.get("origin_cover"))
        or _first_url(aweme_detail.get("cover"))
    )

    duration_ms = video.get("duration") or aweme_detail.get("duration") or 0
    duration_seconds = round(int(duration_ms) / 1000)

    return {
        "id": content_id,
        "title": aweme_detail.get("desc") or f"抖音视频 {content_id}",
        "thumbnail": thumbnail,
        "duration": _format_duration(duration_seconds),
        "duration_seconds": duration_seconds,
        "uploader": author.get("nickname") or "",
        "extractor": "Douyin",
        "webpage_url": f"https://www.douyin.com/video/{content_id}",
        "videoOptions": video_options,
        "audioOption": {
            "id": "douyin_audio",
            "label": "MP3 音频",
            "hint": "下载视频后自动提取为 MP3",
        },
    }


def extract_info(url: str) -> dict[str, Any]:
    content_id, content_type = normalize_url(url)
    if content_type == "note":
        raise RuntimeError("抖音图文暂不支持下载，请换一个视频链接")

    detail = _get_detail(content_id)
    aweme_detail = (detail or {}).get("aweme_detail")
    if not aweme_detail:
        raise RuntimeError(
            "未能获取抖音视频信息，可能视频已删除、设为私密，或当前被风控限流，请稍后重试"
        )
    if aweme_detail.get("images"):
        raise RuntimeError("抖音图文暂不支持下载，请换一个视频链接")

    return _build_info(content_id, aweme_detail)


def _select_video_url(aweme_detail: dict[str, Any], format_id: str) -> str | None:
    video = aweme_detail.get("video") or {}
    by_height = _best_bit_rate_by_height(video)

    if format_id.startswith("douyin_"):
        suffix = format_id.split("_", 1)[1]
        if suffix.isdigit():
            bit_rate = by_height.get(int(suffix))
            if bit_rate:
                url = _first_url(bit_rate.get("play_addr"))
                if url:
                    return url

    return (
        _first_url(video.get("play_addr_h264"))
        or _first_url(video.get("play_addr"))
    )


def _download_file(url: str, dest: Path, on_progress: Callable[[float], None] | None) -> None:
    request_headers = dict(HEADERS)
    response = requests.get(url, headers=request_headers, stream=True, timeout=30)
    if response.status_code == 403:
        request_headers.pop("Referer", None)
        response = requests.get(url, headers=request_headers, stream=True, timeout=30)
    response.raise_for_status()

    total = int(response.headers.get("content-length") or 0)
    downloaded = 0
    with open(dest, "wb") as file:
        for chunk in response.iter_content(chunk_size=1024 * 256):
            if not chunk:
                continue
            file.write(chunk)
            downloaded += len(chunk)
            if total and on_progress:
                on_progress(min(99, downloaded / total * 100))

    if on_progress:
        on_progress(100)


def download(
    url: str,
    format_id: str,
    kind: str,
    out_stem: str,
    dest_dir: Path,
    on_progress: Callable[[float], None] | None = None,
) -> dict[str, Any]:
    dest_dir.mkdir(parents=True, exist_ok=True)

    content_id, content_type = normalize_url(url)
    if content_type == "note":
        raise RuntimeError("抖音图文暂不支持下载，请换一个视频链接")

    detail = _get_detail(content_id)
    aweme_detail = (detail or {}).get("aweme_detail")
    if not aweme_detail:
        raise RuntimeError("未能获取抖音视频信息，请稍后重试")
    if aweme_detail.get("images"):
        raise RuntimeError("抖音图文暂不支持下载，请换一个视频链接")

    info = _build_info(content_id, aweme_detail)
    video_url = _select_video_url(aweme_detail, format_id)
    if not video_url:
        raise RuntimeError("未找到可下载的视频地址")

    video_path = dest_dir / f"{out_stem}.mp4"
    _download_file(video_url, video_path, on_progress)

    if kind == "audio":
        audio_path = dest_dir / f"{out_stem}.mp3"
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(video_path),
                "-vn",
                "-acodec",
                "libmp3lame",
                "-b:a",
                "192k",
                str(audio_path),
            ],
            check=True,
            capture_output=True,
        )
        video_path.unlink(missing_ok=True)
        video_path = audio_path

    return {
        "title": info["title"],
        "thumbnail": info["thumbnail"],
        "file_path": video_path,
        "file_name": video_path.name,
        "file_size": video_path.stat().st_size,
    }
