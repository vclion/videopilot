"""Qwen 问答素材链路：全片建索引、模型选择片段、准备直接输入的候选视频。"""

from __future__ import annotations

import hashlib
import os
from contextlib import contextmanager
from typing import Callable

from . import video_cache as cache
from .subtitle import _resolve_bilibili_short, fetch_subtitles
from .video_retrieval import SelectionError, global_question, select_windows
from .video_media import build_windows, normalize_segments
from .qwen_client import analyze_clip, configuration


INDEX_PROMPT = (
    "为这个片段建立可检索的画面记录。记录主要物体及颜色、人物动作、状态变化、"
    "物体间的空间关系、屏幕或场景中可辨认的原文文字，以及镜头场景。"
    "不要只输出笼统摘要，不确定的对象和文字不要补写。"
)
INDEX_VERSION = "visual-index-v1-fps2"


def enabled() -> bool:
    return os.getenv("VIDEO_ANALYSIS_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}


def unavailable(reason: str) -> dict:
    return {"status": "unavailable", "reason": reason, "indexed_chunks": 0, "total_chunks": 0,
            "coverage": [], "refined_chunks": 0, "has_visual_evidence": False}


def _budget(items: list[dict], max_chars: int = 5000) -> list[dict]:
    # 每个实际传入的视频必须有对应来源 ID，不能因字幕过长丢掉末尾视频的引用标识。
    result = [item for item in items if item.get("source") == "video"]
    for item in items:
        if item.get("source") == "video":
            continue
        remaining = max_chars - sum(len(value["text"]) for value in result)
        if remaining <= 0:
            break
        size = min(900, remaining)
        result.append({**item, "text": item["text"][:size], "truncated": len(item["text"]) > size})
    return result


def enhance(url: str, subtitle: dict, question: str, fallback_evidence: list[dict],
            progress: Callable[[str, str, dict, dict | None, str | None], None],
            resolved_url: str | None = None) -> dict:
    try:
        _, base, model = configuration()
    except ValueError:
        return {"clips": [], "evidence": fallback_evidence, "metadata": unavailable("视频模型尚未配置。")}
    config_id = hashlib.sha256((INDEX_VERSION + base + model).encode()).hexdigest()
    resolved_url = resolved_url or _resolve_bilibili_short(url)
    with cache.session(resolved_url) as key:
        source = cache.get_source(key, resolved_url)
        alignment = "aligned"
        timed = subtitle
        if subtitle.get("transcript") and not subtitle.get("segments"):
            try:
                timed = fetch_subtitles(url, require_timestamps=True)
            except Exception:
                alignment = "unavailable"
        try:
            segments = normalize_segments(timed.get("segments") or [])
        except ValueError:
            segments = []
            alignment = "unavailable"
        # 明显超出视频时长的字幕不能静默截断后被视为可靠对齐。
        if segments and max(item["end"] for item in segments) > source["duration"] + 2:
            segments = []
            alignment = "mismatch"
        if not segments and alignment != "mismatch":
            alignment = "missing" if not subtitle.get("transcript") else "unavailable"
        windows = build_windows(segments, source["duration"])
        analyses = {}
        hits = 0
        for window in windows:
            value = cache.get_analysis(source, config_id, window["id"], "index")
            if value is not None:
                analyses[window["id"]] = value
                hits += 1
        index_error = None
        calls = 0
        max_calls = cache.setting("VIDEO_INDEX_MAX_CHUNKS_PER_RUN", 180, 1, 1080)
        for window in windows:
            if window["id"] in analyses:
                continue
            if calls >= max_calls:
                index_error = "本次达到索引处理上限，后续提问会从未完成的片段继续。"
                break
            progress("index_video", "running", {},
                     {"indexed_chunks": len(analyses), "total_chunks": len(windows), "cache_hits": hits}, None)
            try:
                clip = cache.get_clip(source, window)
                value = analyze_clip(clip, INDEX_PROMPT)
                cache.save_analysis(source, config_id, window["id"], "index", value)
                analyses[window["id"]] = value
                calls += 1
            except (RuntimeError, ValueError, OSError) as exc:
                index_error = (f"片段 {window['id']}（{window['start_seconds']}–{window['end_seconds']} 秒）"
                               f"分析失败：{exc}。已完成片段会保留。")
                break
        progress("index_video", "completed" if len(analyses) == len(windows) else "failed", {},
                 {"indexed_chunks": len(analyses), "total_chunks": len(windows), "cache_hits": hits}, index_error)
        progress("retrieve_video", "running", {"query": question, "method": "qwen"}, None, None)
        try:
            selected = select_windows(
                windows, analyses, question,
                top_k=cache.setting("VIDEO_RETRIEVAL_TOP_K", 3, 1, 6),
                max_windows=cache.setting("VIDEO_MAX_CLIPS_PER_QUESTION", 6, 1, 12),
            )
        except SelectionError as exc:
            progress("retrieve_video", "failed", {"method": "qwen"}, None, str(exc))
            raise
        progress("retrieve_video", "completed", {"query": question, "method": "qwen"},
                 {"selected_ids": [window["id"] for window in selected]}, None)
        evidence = []
        clips = []
        clip_errors = 0
        for window in selected:
            try:
                clip = cache.get_clip(source, window)
                clips.append(clip)
                evidence.append({
                    "id": "video-" + window["id"], "source": "video",
                    "text": f"直接输入的视频片段，原视频 {window['start_seconds']}–{window['end_seconds']} 秒。",
                    "start_seconds": window["start_seconds"], "end_seconds": window["end_seconds"],
                })
            except Exception:
                clip_errors += 1
            if window["text"]:
                evidence.append({
                    "id": "subtitle-" + window["id"], "source": "subtitle",
                    "start_seconds": window["start_seconds"], "end_seconds": window["end_seconds"],
                    "text": f"字幕原文，原视频 {window['start_seconds']}–{window['end_seconds']} 秒：\n{window['text']}",
                })
        # 未提供时间戳时仅补原有文本证据，不能伪造对应视频位置。
        if not segments or not any(item.get("source") == "subtitle" for item in evidence):
            evidence.extend({**item, "source": "subtitle"} for item in fallback_evidence)
        has_visual = bool(clips)
        complete = len(analyses) == len(windows)
        status = "ready" if complete and has_visual and not clip_errors else "partial"
        reason = index_error or ("部分候选视频无法读取，只能查看已成功准备的片段。" if clip_errors else "")
        if not selected:
            status = "no_match"
            reason = "Qwen 阅读字幕与已有画面记录后未选出相关片段；这不能证明全片不存在相关内容。"
            if index_error:
                reason += " " + index_error
        if not analyses and not has_visual:
            status = "unavailable"
        if global_question(question):
            status = "global_limited"
            reason = "当前使用采样画面和候选片段，不能可靠证明全片次数、首次或最后一次出现。"
        metadata = {
            "status": status, "reason": reason, "indexed_chunks": len(analyses), "total_chunks": len(windows),
            "coverage": [{"start_seconds": window["start_seconds"], "end_seconds": window["end_seconds"]}
                         for window in windows if window["id"] in analyses],
            "selected_ranges": [{"start_seconds": clip["start_seconds"], "end_seconds": clip["end_seconds"]}
                                for clip in clips],
            "refined_chunks": 0, "has_visual_evidence": has_visual,
            "answer_mode": "video" if clips else "subtitle_only", "selection_method": "qwen",
            "subtitle_alignment": alignment, "sampling_fps": 2, "model": model,
        }
        return {"clips": clips, "evidence": _budget(evidence), "metadata": metadata, "title": source["title"]}


@contextmanager
def prepare(url: str, subtitle: dict, question: str, fallback_evidence: list[dict], progress: Callable):
    """一直保护素材到最终 Qwen 请求完成，防止缓存清理或其他请求删除输入文件。"""
    configuration()
    resolved = _resolve_bilibili_short(url)
    with cache.session(resolved):
        try:
            result = enhance(url, subtitle, question, fallback_evidence, progress, resolved_url=resolved)
        except SelectionError:
            raise
        except Exception:
            result = {"clips": [], "evidence": fallback_evidence,
                      "metadata": unavailable("本次无法获取视频片段，Qwen 只能使用已有字幕证据。")}
        yield result
