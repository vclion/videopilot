"""视频 Agent 的工具编排层。

显式工具链：字幕获取 -> 证据检索 -> 可选视频分析 -> 总结或问答。
工具边界和执行记录已经独立出来，后续可以无痛接入真正的模型规划器。
"""

from __future__ import annotations

import re
from typing import Any, Callable

from .ai_summary import answer_question, generate_summary, rewrite_question
from .subtitle import SubtitleError, fetch_subtitles
from . import video_enhancement
from . import qwen_client as qwen


RecordStep = Callable[[str, str, dict[str, Any], dict[str, Any] | None, str | None], None]


MAX_EVIDENCE_CHARS = 9000
MAX_CONTEXT_CHARS = 5200


def chunk_transcript(transcript: str, size: int = 2400, overlap: int = 240) -> list[dict[str, Any]]:
    text = re.sub(r"\s+", " ", transcript or "").strip()
    if not text:
        return []
    if size <= overlap:
        raise ValueError("分块参数无效")

    chunks: list[dict[str, Any]] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            boundary = max(text.rfind("。", start + size // 2, end), text.rfind("，", start + size // 2, end))
            if boundary > start:
                end = boundary + 1
        chunks.append(
            {
                "id": f"chunk-{len(chunks) + 1:03d}",
                "start": start,
                "end": end,
                "text": text[start:end],
            }
        )
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return chunks


def _query_terms(query: str) -> list[str]:
    return re.findall(r"[a-zA-Z0-9_]{2,}|[\u4e00-\u9fff]{2,}", (query or "").lower())


def select_evidence(
    chunks: list[dict[str, Any]],
    query: str | None = None,
    limit: int = 8,
    max_chars: int = MAX_EVIDENCE_CHARS,
) -> list[dict[str, Any]]:
    terms = _query_terms(query or "")
    if not terms:
        if len(chunks) <= limit:
            selected = chunks
        else:
            indexes = {round(index * (len(chunks) - 1) / (limit - 1)) for index in range(limit)}
            selected = [chunks[index] for index in sorted(indexes)]
    else:
        ranked = []
        for index, chunk in enumerate(chunks):
            text = chunk["text"].lower()
            score = sum(text.count(term) for term in terms)
            ranked.append((score, -index, chunk))
        ranked.sort(reverse=True, key=lambda item: (item[0], item[1]))
        selected = [item[2] for item in ranked[:limit]]

    if max_chars <= 0:
        return selected[:1]

    trimmed: list[dict[str, Any]] = []
    used_chars = 0
    for chunk in selected:
        remaining = max_chars - used_chars
        if remaining <= 0:
            break
        text = chunk["text"]
        if len(text) > remaining and trimmed:
            continue
        if len(text) > remaining:
            chunk = {**chunk, "text": text[:remaining]}
        trimmed.append(chunk)
        used_chars += len(chunk["text"])
    return sorted(trimmed, key=lambda item: item["start"])


def _context(evidence: list[dict[str, Any]]) -> str:
    return "\n\n".join(f"[{item['id']}] {item['text']}" for item in evidence)


def _public_evidence(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "id": item["id"],
            "start_char": item.get("start"),
            "end_char": item.get("end"),
            "excerpt": item["text"][:240],
            **{key: item[key] for key in ("source", "start_seconds", "end_seconds", "analysis_kind", "truncated") if key in item},
        }
        for item in evidence
    ]


def _clip(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _rank_memories(
    memories: list[dict[str, Any]],
    query: str,
    limit: int = 3,
    preferred_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    if not memories:
        return []
    terms = _query_terms(query)
    ranked: list[tuple[int, int, dict[str, Any]]] = []
    for index, memory in enumerate(memories):
        haystack = " ".join(
            [
                str(memory.get("question") or ""),
                str(memory.get("summary") or ""),
                " ".join(str(item) for item in memory.get("keywords") or []),
                " ".join(str(item) for item in memory.get("unresolved") or []),
            ]
        ).lower()
        score = sum(haystack.count(term) for term in terms)
        if preferred_ids and memory.get("run_id") in preferred_ids:
            score += 5
        ranked.append((score, -index, memory))
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    positive = [item[2] for item in ranked if item[0] > 0]
    return (positive or [item[2] for item in ranked])[:limit]


def detect_intent(question: str, rewritten_question: str | None = None) -> str:
    text = (question or "").strip()
    if any(marker in text for marker in ("总结", "概括", "梳理")):
        return "summary"
    if any(marker in text for marker in ("原文", "引用", "哪一句", "依据")):
        return "quote"
    if any(marker in text for marker in ("对比", "区别", "不同", "相同")):
        return "compare"
    if rewritten_question and rewritten_question.strip() != text:
        return "follow_up"
    return "answer"


def _conversation_context(
    context: dict[str, Any] | None,
    selected_memories: list[dict[str, Any]],
    max_chars: int = MAX_CONTEXT_CHARS,
) -> str:
    if not context:
        return ""

    sections: list[str] = []
    rolling = _clip(context.get("rolling_summary"), 1200)
    if rolling:
        sections.append(f"会话滚动摘要：\n{rolling}")

    recent_parts: list[str] = []
    for turn in (context.get("recent_turns") or [])[:4]:
        question = _clip(turn.get("question"), 260)
        answer = _clip(turn.get("answer"), 520)
        if question or answer:
            recent_parts.append(f"用户：{question}\n助手：{answer}")
    if recent_parts:
        sections.append("最近对话：\n" + "\n\n".join(recent_parts))

    memory_parts: list[str] = []
    for memory in selected_memories:
        summary = _clip(memory.get("fact_summary") or memory.get("summary"), 500)
        conversation_summary = _clip(memory.get("conversation_summary"), 360)
        if not summary:
            summary = conversation_summary
        if not summary:
            continue
        question = _clip(memory.get("question"), 220)
        keywords = ", ".join(str(item) for item in memory.get("keywords") or [])[:180]
        extra = f"\n讨论摘要：{conversation_summary}" if conversation_summary and conversation_summary != summary else ""
        memory_parts.append(f"问题：{question}\n事实摘要：{summary}{extra}\n关键词：{keywords}")
    if memory_parts:
        sections.append("相关历史记忆（仅作参考）：\n" + "\n\n".join(memory_parts))

    return "\n\n".join(sections)[:max_chars]


def execute_agent(
    url: str,
    mode: str,
    question: str | None,
    record_step: RecordStep,
    conversation_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    step_number = 0
    use_video = mode == "question" and video_enhancement.enabled()

    def get_subtitle() -> dict:
        try:
            return fetch_subtitles(url)
        except SubtitleError:
            if not use_video:
                raise
            return {"source": "unavailable", "transcript": "", "segments": [], "title": "", "language": ""}

    def run_tool(
        name: str,
        input_data: dict[str, Any],
        operation: Callable[[], Any],
        output_builder: Callable[[Any], dict[str, Any]],
    ) -> Any:
        nonlocal step_number
        step_number += 1
        record_step(name, "running", input_data, None, None)
        try:
            result = operation()
        except Exception as exc:
            record_step(name, "failed", input_data, None, str(exc))
            raise
        record_step(name, "completed", input_data, output_builder(result), None)
        return result

    subtitle = run_tool(
        "fetch_subtitle",
        {"url": url},
        get_subtitle,
        lambda value: {
            "source": value.get("source"),
            "language": value.get("language"),
            "title": value.get("title"),
            "characters": len(value.get("transcript") or ""),
        },
    )
    transcript = (subtitle.get("transcript") or "").strip()
    if not transcript and not use_video:
        raise RuntimeError("字幕内容为空，无法执行 Agent 任务")

    chunks = chunk_transcript(transcript)
    memory_pool = (conversation_context or {}).get("memories") or []
    memory_fts_ids = set((conversation_context or {}).get("memory_fts_ids") or [])
    initial_memories = (
        _rank_memories(memory_pool, question or "", preferred_ids=memory_fts_ids)
        if mode == "question"
        else []
    )
    initial_context = _conversation_context(conversation_context, initial_memories)
    rewritten_question = question or ""
    if mode == "question" and question:
        rewritten_question = run_tool(
            "rewrite_question",
            {"question": question, "context_chars": len(initial_context)},
            lambda: qwen.rewrite_question(question, initial_context) if use_video else rewrite_question(question, initial_context),
            lambda value: {
                "changed": value.strip() != question.strip(),
                "question_chars": len(value),
            },
        )
    selected_memories = (
        _rank_memories(memory_pool, rewritten_question, preferred_ids=memory_fts_ids)
        if mode == "question"
        else []
    )
    history_text = _conversation_context(conversation_context, selected_memories)
    intent = detect_intent(question or "", rewritten_question) if mode == "question" else "summary"
    retrieval_query = rewritten_question if mode == "question" else None
    if mode == "question" and history_text:
        retrieval_query = f"{question}\n{history_text[:1800]}"
    # Qwen 阅读全部带时间的字幕和画面索引；备用正文均匀取样，不按关键词过滤。
    evidence = select_evidence(chunks, None if use_video else retrieval_query)
    run_tool(
        "retrieve_evidence",
        {
            "query": question or "视频整体内容",
            "chunk_count": len(chunks),
            "memory_ids": [item.get("run_id") for item in selected_memories],
        },
        lambda: evidence,
        lambda value: {"selected_ids": [item["id"] for item in value], "selected_count": len(value)},
    )

    context = _context(evidence)
    title = subtitle.get("title") or "未知视频"
    if mode == "summary":
        result = run_tool(
            "generate_summary",
            {"title": title, "evidence_count": len(evidence)},
            lambda: generate_summary(title, context),
            lambda value: {"fields": sorted(value.keys())},
        )
        result["evidence"] = _public_evidence(evidence)
        return {
            "title": title,
            "source": subtitle.get("source"),
            "language": subtitle.get("language"),
            "result": result,
        }

    if not question or not question.strip():
        raise ValueError("问答模式必须提供 question")
    video_metadata = None
    if use_video:
        record_step("prepare_video", "running", {"url": url, "chunk_seconds": 10}, None, None)
        with video_enhancement.prepare(url, subtitle, rewritten_question, evidence, record_step) as prepared:
            evidence = prepared["evidence"]
            video_metadata = prepared["metadata"]
            title = subtitle.get("title") or prepared.get("title") or title
            record_step("prepare_video", "completed", {},
                        {"status": video_metadata["status"], "clip_count": len(prepared["clips"])}, None)
            result = run_tool(
                "answer_video_question",
                {"title": title, "question": question, "clip_count": len(prepared["clips"]), "provider": "qwen"},
                lambda: qwen.answer_video_question(
                    title, question, prepared["clips"], evidence, context=history_text,
                    intent=intent, rewritten_question=rewritten_question, video_context=video_metadata,
                ),
                lambda value: {"fields": sorted(value.keys()), "provider": "qwen"},
            )
    else:
        result = run_tool(
            "answer_question",
            {
                "title": title,
                "question": question,
                "rewritten_question": rewritten_question,
                "intent": intent,
                "evidence_count": len(evidence),
            },
            lambda: answer_question(
                title,
                question,
                evidence,
                context=history_text,
                intent=intent,
                rewritten_question=rewritten_question,
            ),
            lambda value: {"fields": sorted(value.keys())},
        )
    evidence = result.pop("_input_evidence", evidence)
    citations = result.get("evidence") or []
    result["evidence"] = _public_evidence(evidence)
    if video_metadata is not None:
        result["video_analysis"] = video_metadata
        result["citations"] = citations
        for public in result["evidence"]:
            if public.get("source") == "video":
                matching = [citation for citation in citations if citation.get("chunk_id") == public["id"]]
                if matching:
                    public["excerpt"] = matching[0].get("quote") or public["excerpt"]
    result["intent"] = intent
    result["rewritten_question"] = rewritten_question
    return {
        "title": title,
        "source": subtitle.get("source") if transcript else "video_analysis",
        "language": subtitle.get("language"),
        "result": result,
    }
