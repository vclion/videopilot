"""Qwen 根据字幕和画面记录的含义选择视频片段，不使用关键词排名。"""

from __future__ import annotations

import json

from . import qwen_client as qwen
from .ai_summary import clip_to_tokens


SELECTION_BATCH_CHARS = 24000


class SelectionError(RuntimeError):
    """片段选择失败必须显式报告，不能冒充没有相关画面。"""


def _batches(candidates: list[dict]):
    batch = []
    size = 0
    for candidate in candidates:
        length = len(json.dumps(candidate, ensure_ascii=False))
        if batch and size + length > SELECTION_BATCH_CHARS:
            yield batch
            batch, size = [], 0
        batch.append(candidate)
        size += length
    if batch:
        yield batch


def select_windows(windows: list[dict], analyses: dict[str, dict], query: str,
                   top_k: int = 3, max_windows: int = 6) -> list[dict]:
    """所有有记录的窗口都交给模型阅读；长索引分批选择，再统一比较候选。"""
    limit = min(top_k, max_windows)
    if limit <= 0 or not query.strip():
        return []
    candidates = []
    for window in windows:
        analysis = analyses.get(window["id"], {})
        observations = analysis.get("observations", [])
        if not window["text"] and not observations and not analysis.get("uncertainties"):
            continue
        candidates.append({
            "id": window["id"], "start_seconds": window["start_seconds"],
            "end_seconds": window["end_seconds"],
            "subtitle": clip_to_tokens(window["text"], 500),
            "observations": observations,
            "uncertainties": analysis.get("uncertainties", []),
        })
    if not candidates:
        return []
    try:
        finalists = []
        for batch in _batches(candidates):
            ids = qwen.select_clip_ids(query, batch, limit)
            by_id = {item["id"]: item for item in batch}
            finalists.extend(by_id[item] for item in ids)
        while len(finalists) > limit:
            reduced = []
            batches = list(_batches(finalists))
            for batch in batches:
                batch_limit = limit if len(batches) == 1 else min(limit, max(1, len(batch) // 2))
                ids = qwen.select_clip_ids(query, batch, batch_limit)
                by_id = {item["id"]: item for item in batch}
                reduced.extend(by_id[item] for item in ids)
            if len(reduced) >= len(finalists):
                raise RuntimeError("画面索引过大，无法在片段选择预算内完成比较")
            finalists = reduced
        positions = {window["id"]: index for index, window in enumerate(windows)}
        core = [positions[item["id"]] for item in finalists]
    except (RuntimeError, ValueError, KeyError, TypeError) as exc:
        raise SelectionError(f"Qwen 选择视频片段失败：{exc}") from None
    selected = list(core)
    for index in core:
        for neighbor in (index - 1, index + 1):
            if len(selected) >= max_windows:
                break
            if 0 <= neighbor < len(windows) and neighbor not in selected:
                selected.append(neighbor)
    return [windows[index] for index in sorted(selected)]


def global_question(query: str) -> bool:
    return any(term in query for term in ("第一次", "首次", "最后一次", "一共", "总共", "几次", "多少次", "全片", "所有出现"))
