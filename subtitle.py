"""AI 总结：字幕获取模块（只新增，不影响现有下载功能）。

策略：
1. B 站优先调用平台字幕 API（.env 配置 BILIBILI_SESSDATA 后可获取 AI 字幕）
2. 其余平台用 yt-dlp 提取字幕（手动字幕 -> 自动生成字幕兜底）
3. 抖音无公开字幕，直接给出明确提示
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
import yt_dlp
from dotenv import load_dotenv

from douyin import is_douyin_url


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

BILIBILI_SESSDATA = os.getenv("BILIBILI_SESSDATA", "").strip()

MAX_TRANSCRIPT_CHARS = 300_000

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
BILI_HEADERS = {
    "User-Agent": UA,
    "Referer": "https://www.bilibili.com/",
}


class SubtitleError(Exception):
    """字幕获取失败（带用户可读提示）。"""


def _bilibili_id(url: str) -> tuple[str, str] | None:
    """从链接中提取 B 站视频标识，返回 (类型, 值)。"""
    text = url.strip()
    bvid = re.search(r"BV[0-9A-Za-z]{10}", text)
    if bvid:
        return "bvid", bvid.group(0)
    aid = re.search(r"av(\d+)", text, re.IGNORECASE)
    if aid:
        return "aid", aid.group(1)
    return None


def _is_bilibili_host(url: str) -> bool:
    host = (urlparse(url.strip()).hostname or "").lower()
    return (
        host in {"bilibili.com", "bilibili.tv"}
        or host.endswith(".bilibili.com")
        or host.endswith(".bilibili.tv")
    )


def _resolve_bilibili_short(url: str) -> str:
    """b23.tv 短链先解析跳转，拿到真实 B 站页面地址。"""
    host = (urlparse(url.strip()).hostname or "").lower()
    if host == "b23.tv" or host.endswith(".b23.tv"):
        try:
            response = requests.get(url, headers=BILI_HEADERS, allow_redirects=True, timeout=15)
            if response.url and response.url != url:
                return response.url
        except Exception:
            pass
    return url


def _pick_subtitle(subtitles: list[dict[str, Any]]) -> dict[str, Any]:
    """优先中文（手动 > 自动），其次英文，最后任意轨道。"""

    def rank(item: dict[str, Any]) -> tuple[int, int]:
        lan = (item.get("lan") or "").lower()
        if lan.startswith("zh"):
            return (0, 0 if not lan.startswith("ai-") else 1)
        if lan.startswith("en"):
            return (1, 0)
        return (2, 0)

    return min(subtitles, key=rank)


def _download_bili_subtitle_body(subtitle_url: str) -> list[dict[str, Any]]:
    url = subtitle_url
    if url.startswith("//"):
        url = "https:" + url
    response = requests.get(url, headers=BILI_HEADERS, timeout=20)
    response.raise_for_status()
    data = response.json()
    body = data.get("body") if isinstance(data, dict) else []
    if not body:
        raise SubtitleError("B 站字幕内容为空")
    return body


def _join_bili_body(body: list[dict[str, Any]]) -> str:
    parts = [str(seg.get("content") or "").strip() for seg in body if isinstance(seg, dict)]
    return "\n".join(part for part in parts if part)


def _fetch_bilibili_with_credential(ident: tuple[str, str]) -> dict[str, Any]:
    """使用 .env 中的 SESSDATA 获取 B 站字幕（可拿到 AI 自动字幕）。"""
    import asyncio

    from bilibili_api import Credential, video as bvideo

    sessdata = BILIBILI_SESSDATA
    if not sessdata:
        raise SubtitleError("未配置 BILIBILI_SESSDATA")

    async def _run() -> tuple[Any, dict[str, Any], dict[str, Any]]:
        credential = Credential(sessdata=sessdata)
        if ident[0] == "bvid":
            video_obj = bvideo.Video(bvid=ident[1], credential=credential)
        else:
            video_obj = bvideo.Video(aid=int(ident[1]), credential=credential)
        cid = await video_obj.get_cid(page_index=0)
        info = await video_obj.get_info()
        subtitle_info = await video_obj.get_subtitle(cid=cid) or {}
        subtitles = (
            subtitle_info.get("subtitles") or []
            if isinstance(subtitle_info, dict)
            else []
        )
        if not subtitles:
            raise SubtitleError("该 B 站视频暂无可用的字幕")
        return video_obj, info, subtitles

    try:
        _, info, subtitles = asyncio.run(_run())
    except SubtitleError:
        raise
    except Exception as exc:
        raise SubtitleError(f"B 站字幕获取失败（登录态）：{exc}") from exc

    picked = _pick_subtitle(subtitles)
    body = _download_bili_subtitle_body(picked["subtitle_url"])
    return {
        "source": "bilibili_api",
        "language": picked.get("lan") or "",
        "title": info.get("title") or "",
        "transcript": _join_bili_body(body),
    }


def _bilibili_anonymous_subtitles(bvid: str, cid: int) -> list[dict[str, Any]]:
    """匿名尝试获取 B 站公开字幕列表（无登录态时通常为空）。"""
    import asyncio

    from bilibili_api.utils.network import _enc_wbi, get_wbi_mixin_key

    base_params = {"bvid": bvid, "cid": cid}

    # 1. 无签名版本
    try:
        response = requests.get(
            "https://api.bilibili.com/x/player/v2",
            params=base_params,
            headers=BILI_HEADERS,
            timeout=15,
        )
        data = response.json()
        subtitles = (
            ((data.get("data") or {}).get("subtitle") or {}).get("subtitles") or []
        )
        if subtitles:
            return subtitles
    except Exception:
        pass

    # 2. WBI 签名版本
    try:
        mixin_key = asyncio.run(get_wbi_mixin_key())
        signed_params = _enc_wbi(dict(base_params), mixin_key)
        response = requests.get(
            "https://api.bilibili.com/x/player/wbi/v2",
            params=signed_params,
            headers=BILI_HEADERS,
            timeout=15,
        )
        data = response.json()
        return (
            ((data.get("data") or {}).get("subtitle") or {}).get("subtitles") or []
        )
    except Exception:
        return []


def _fetch_bilibili_anonymous(ident: tuple[str, str]) -> dict[str, Any]:
    """匿名链路：view 拿 cid -> 尝试字幕列表。"""
    if ident[0] == "bvid":
        view_params = {"bvid": ident[1]}
    else:
        view_params = {"aid": int(ident[1])}

    response = requests.get(
        "https://api.bilibili.com/x/web-interface/view",
        params=view_params,
        headers=BILI_HEADERS,
        timeout=15,
    )
    response.raise_for_status()
    data = (response.json() or {}).get("data") or {}
    cid = data.get("cid")
    bvid = data.get("bvid")
    if not cid or not bvid:
        raise SubtitleError("未能解析该 B 站视频信息")

    subtitles = _bilibili_anonymous_subtitles(bvid, cid)
    if not subtitles:
        raise SubtitleError(
            "该视频暂无公开字幕。B 站 AI 字幕需要登录态："
            "请在项目 .env 中填写 BILIBILI_SESSDATA（登录 bilibili.com 后从 Cookie 中复制）"
        )

    picked = _pick_subtitle(subtitles)
    body = _download_bili_subtitle_body(picked["subtitle_url"])
    return {
        "source": "bilibili_api",
        "language": picked.get("lan") or "",
        "title": data.get("title") or "",
        "transcript": _join_bili_body(body),
    }


def _fetch_bilibili(url: str) -> dict[str, Any]:
    ident = _bilibili_id(url)
    if not ident:
        raise SubtitleError("未能从链接中识别 B 站视频 ID")

    if BILIBILI_SESSDATA:
        try:
            return _fetch_bilibili_with_credential(ident)
        except SubtitleError as exc:
            message = str(exc)
            if "暂无可用的字幕" in message:
                raise SubtitleError(
                    "该 B 站视频暂无可用的字幕（已尝试使用登录态获取；"
                    "若 Cookie 已过期，请更新 .env 中的 BILIBILI_SESSDATA）"
                ) from exc
            if "登录态" in message:
                raise SubtitleError(
                    message + "（请检查 .env 中的 BILIBILI_SESSDATA 是否有效、未过期）"
                ) from exc
            raise

    return _fetch_bilibili_anonymous(ident)


def _rank_subtitle_file(path: Path) -> tuple[int, int, str]:
    name = path.name.lower()
    ext_rank = {"srt": 0, "vtt": 1, "json": 2, "ass": 3, "lrc": 4}.get(
        path.suffix.lstrip("."), 5
    )
    lang_score = 0
    if "zh" in name:
        lang_score -= 100
    elif "en" in name:
        lang_score -= 50
    if ".auto" in name:
        lang_score += 10
    return (ext_rank, lang_score, name)


def _parse_subtitle_file(path: Path) -> str:
    text = path.read_text(encoding="utf-8", errors="ignore")

    # JSON 字幕（部分平台/库会输出）
    try:
        import json

        data = json.loads(text)
        if isinstance(data, list):
            parts = []
            for item in data:
                if isinstance(item, dict):
                    parts.append(str(item.get("content") or item.get("text") or "").strip())
            content = "\n".join(p for p in parts if p)
            if content:
                return content
    except Exception:
        pass

    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("WEBVTT") or line.startswith("NOTE"):
            continue
        if re.match(r"^\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}\s*-->", line):
            continue
        if line.isdigit():
            continue
        cleaned = re.sub(r"<[^>]+>", "", line)
        cleaned = (
            cleaned.replace("&nbsp;", " ")
            .replace("&amp;", "&")
            .replace("&lt;", "<")
            .replace("&gt;", ">")
            .strip()
        )
        if cleaned:
            lines.append(cleaned)
    return "\n".join(lines)


def _fetch_ytdlp(url: str) -> dict[str, Any]:
    """yt-dlp 提取字幕：手动字幕优先，自动生成字幕兜底。"""
    with tempfile.TemporaryDirectory(prefix="subtitle-") as tmp:
        options = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "skip_download": True,
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": ["zh.*", "en.*"],
            "subtitlesformat": "srt/vtt/best",
            "outtmpl": str(Path(tmp) / "sub.%(ext)s"),
            "socket_timeout": 20,
            "retries": 2,
            "no_color": True,
        }
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info:
                raise SubtitleError("无法解析该链接")
            title = info.get("title") or info.get("id") or ""
            ydl.download([url])

        files = sorted(Path(tmp).glob("sub.*"))
        if not files:
            raise SubtitleError("该视频没有可用的字幕（手动字幕和自动生成字幕均未找到）")

        chosen = min(files, key=_rank_subtitle_file)
        transcript = _parse_subtitle_file(chosen)
        if not transcript.strip():
            raise SubtitleError("字幕内容为空，无法生成总结")

        return {
            "source": "yt_dlp",
            "language": _language_from_filename(chosen.name),
            "title": title,
            "transcript": _truncate(transcript),
        }


def _language_from_filename(name: str) -> str:
    parts = name.split(".")
    for part in parts[1:]:
        if part in {"srt", "vtt", "json", "ass", "lrc", "auto", "zh", "en"}:
            continue
        if part:
            return part
    return ""


def _truncate(transcript: str) -> str:
    if len(transcript) <= MAX_TRANSCRIPT_CHARS:
        return transcript
    return transcript[:MAX_TRANSCRIPT_CHARS] + "\n\n（字幕过长，已截断）"


def fetch_subtitles(url: str) -> dict[str, Any]:
    """获取字幕，返回 {source, language, title, transcript}，失败抛 SubtitleError。"""
    url = url.strip()
    if is_douyin_url(url):
        raise SubtitleError("抖音视频没有公开字幕，暂不支持 AI 总结")

    url = _resolve_bilibili_short(url)
    is_bilibili = _is_bilibili_host(url) or bool(_bilibili_id(url))

    if is_bilibili:
        first_error: SubtitleError | None = None
        try:
            return _fetch_bilibili(url)
        except SubtitleError as exc:
            first_error = exc

        # B 站 yt-dlp 通常拿不到真字幕；无登录态时直接给出可操作提示
        message = str(first_error)
        if any(key in message for key in ("登录态", "公开字幕", "无可用的字幕")):
            raise first_error

        try:
            return _fetch_ytdlp(url)
        except SubtitleError:
            pass
        except Exception:
            pass
        raise first_error

    return _fetch_ytdlp(url)