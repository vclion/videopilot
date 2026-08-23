"""AI 总结：调用 DeepSeek 生成结构化视频总结（只新增，不影响现有功能）。

配置（.env）：
- DEEPSEEK_API_KEY：必填
- DEEPSEEK_MODEL：默认 deepseek-v4-flash
- DEEPSEEK_BASE_URL：默认 https://api.deepseek.com
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

API_KEY = os.getenv("DEEPSEEK_API_KEY", "").strip()
BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash").strip()

SYSTEM_PROMPT = (
    "你是一位专业的视频内容分析助手。请根据用户提供的视频标题与字幕，"
    "生成一份高质量的结构化中文总结。必须严格输出 JSON，不要输出任何其他文字。"
    'JSON 结构如下：{"summary": "一句话总结", "key_points": ["核心观点1", "核心观点2"], '
    '"chapters": [{"title": "章节标题", "content": "该部分要点"}], '
    '"highlights": ["金句或亮点1"], "action_items": ["可执行建议1"]}。'
    "要求：summary 不超过 60 字；key_points 3-6 条，每条不超过 40 字；"
    "chapters 按内容自然分段，3-8 段；highlights 精选 2-5 条；"
    "action_items 2-4 条。不要编造字幕中不存在的信息。"
)


def is_configured() -> bool:
    """DeepSeek API Key 是否已配置。"""
    return bool(API_KEY)


def _build_user_prompt(title: str, transcript: str) -> str:
    return f"视频标题：{title or '未知'}\n\n视频字幕：\n{transcript}"


def generate_summary(title: str, transcript: str) -> dict[str, Any]:
    """调用 DeepSeek 生成结构化总结，失败时抛 RuntimeError（含用户可读提示）。"""
    if not API_KEY:
        raise RuntimeError(
            "尚未配置 DeepSeek API Key，请在项目根目录的 .env 中填写 DEEPSEEK_API_KEY 后重试"
        )
    if not transcript or not transcript.strip():
        raise RuntimeError("字幕内容为空，无法生成总结")

    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_prompt(title, transcript)},
        ],
        "temperature": 0.4,
        "max_tokens": 2048,
        "stream": False,
        "response_format": {"type": "json_object"},
    }
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(
            f"{BASE_URL}/chat/completions",
            headers=headers,
            json=payload,
            timeout=300,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"无法连接 DeepSeek 服务：{exc}") from exc

    if response.status_code == 401:
        raise RuntimeError("DeepSeek API Key 无效，请检查 .env 中的 DEEPSEEK_API_KEY")
    if response.status_code == 402:
        raise RuntimeError("DeepSeek 账户余额不足，请前往平台充值后重试")
    if response.status_code == 429:
        raise RuntimeError("DeepSeek 请求过于频繁，请稍后重试")

    if response.status_code >= 400:
        try:
            detail = response.json().get("error", {}).get("message") or response.text[:200]
        except Exception:
            detail = response.text[:200]
        raise RuntimeError(f"DeepSeek 接口错误（{response.status_code}）：{detail}")

    data = response.json()
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("DeepSeek 返回数据格式异常") from exc

    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, TypeError) as exc:
        raise RuntimeError("DeepSeek 返回内容不是有效 JSON，请重试") from exc

    return _normalize(parsed)


def _normalize(data: Any) -> dict[str, Any]:
    """兜底清洗模型输出，保证前端渲染字段齐全。"""
    if not isinstance(data, dict):
        data = {}

    def _string(value: Any, default: str = "") -> str:
        return str(value).strip() if value else default

    def _list(value: Any, default: list[str] | None = None) -> list[str]:
        if isinstance(value, list):
            cleaned = [str(item).strip() for item in value if str(item).strip()]
            return cleaned or (default or [])
        return default or []

    raw_chapters = data.get("chapters") if isinstance(data.get("chapters"), list) else []
    chapters: list[dict[str, str]] = []
    for index, chapter in enumerate(raw_chapters):
        if isinstance(chapter, dict):
            chapters.append(
                {
                    "title": _string(chapter.get("title"), f"要点 {index + 1}"),
                    "content": _string(chapter.get("content"), ""),
                }
            )
        else:
            chapters.append({"title": f"要点 {index + 1}", "content": _string(chapter)})

    return {
        "summary": _string(data.get("summary"), "（未生成摘要）"),
        "key_points": _list(data.get("key_points"), ["（暂无核心观点）"]),
        "chapters": chapters,
        "highlights": _list(data.get("highlights")),
        "action_items": _list(data.get("action_items")),
    }