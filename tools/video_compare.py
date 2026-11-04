"""运行 python -m tools.video_compare --help。默认只准备本地素材，不调用模型。"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from app.video_media import build_windows, extract_clip, load_segments, probe_video, subtitle_evidence


def load_cases(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    cases = data.get("cases") if isinstance(data, dict) else None
    if not isinstance(cases, list) or not 1 <= len(cases) <= 10:
        raise ValueError("配置需包含 1–10 个 cases")
    result = []
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("每个 case 必须是对象")
        question = case.get("question")
        indexes = case.get("windows")
        if not isinstance(question, str) or not question.strip() or len(question) > 2000:
            raise ValueError("每个 case 需填写 1–2000 字的问题")
        if (not isinstance(indexes, list) or not 1 <= len(indexes) <= 3
                or any(type(index) is not int or index < 0 for index in indexes)
                or len(set(indexes)) != len(indexes)):
            raise ValueError("windows 需包含 1–3 个不重复的非负整数；0 表示 0–10 秒")
        video = case.get("video")
        subtitles = case.get("subtitles")
        if not isinstance(video, str) or not video.strip():
            raise ValueError("每个 case 需填写本地 video 路径")
        if subtitles is not None and (not isinstance(subtitles, str) or not subtitles.strip()):
            raise ValueError("subtitles 应为文件路径或 null")
        result.append({
            "video": (path.parent / video).resolve(),
            "subtitles": (path.parent / subtitles).resolve() if subtitles else None,
            "question": question.strip(), "windows": sorted(indexes),
            "title": str(case.get("title") or "待验证视频"),
            "expected_answer": str(case.get("expected_answer") or ""),
        })
    return result


def prepare_cases(cases: list[dict], output: Path) -> list[dict]:
    prepared = []
    for number, case in enumerate(cases, 1):
        duration = probe_video(case["video"])
        windows = build_windows(load_segments(case["subtitles"]), duration)
        if max(case["windows"]) >= len(windows):
            raise ValueError(f"case {number} 选择了超出视频范围的窗口")
        clips = []
        for index in case["windows"]:
            window = windows[index]
            target = output / f"case-{number:02d}" / (window["id"] + ".mp4")
            clips.append(extract_clip(case["video"], window, target))
        prepared.append({
            **case, "video": str(case["video"]),
            "subtitles": str(case["subtitles"]) if case["subtitles"] else None,
            "duration_seconds": duration, "clips": clips,
            "subtitle_evidence": subtitle_evidence(clips),
            "baseline": None, "visual_analyses": [], "enhanced": None,
            "status": "prepared", "error": None,
            "review": {"baseline_correct": None, "enhanced_correct": None, "visual_errors": "", "notes": ""},
        })
        print(f"已准备 case {number}：{len(clips)} 个片段", flush=True)
    return prepared


def compare_case(case: dict, answer, analyze) -> None:
    """复用线上 DeepSeek 回答函数；人工参考答案不传给任何模型。"""
    from app.qwen_client import visual_evidence

    def timed_answer(evidence):
        started = time.perf_counter()
        result = answer(case["title"], case["question"], evidence)
        return {"result": result, "elapsed_seconds": round(time.perf_counter() - started, 3)}

    evidence = case["subtitle_evidence"]
    case["baseline"] = timed_answer(evidence)
    combined = list(evidence)
    for clip in case["clips"]:
        analysis = analyze(clip, case["question"])
        case["visual_analyses"].append({"chunk_id": clip["id"], **analysis})
        combined.append(visual_evidence(clip, analysis))
    # 避免原函数静默裁剪过量证据，使 A/B 看起来成功但未真正收到画面内容。
    from app.ai_summary import estimate_tokens
    if estimate_tokens("\n\n".join(f"[{item['id']}] {item['text']}" for item in combined)) > 7600:
        raise ValueError("对比证据超过现有问答预算，请减少选取的窗口数量")
    case["enhanced"] = timed_answer(combined)
    case["status"] = "completed"


def write_report(output: Path, cases: list[dict], live: bool) -> None:
    report = {
        "mode": "live" if live else "offline",
        "scope": "人工指定片段的能力对比；不是全片检索评测，也不写入业务数据库",
        "chunk_seconds": 10,
        "cost_note": "usage 记录服务返回的 token 用量；没有实时计费价格，不估算金额；不自动判定正确率。",
        "cases": cases,
    }
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# 视频问答对比验证", "", report["scope"], "",
             "模式：" + ("真实模型对比" if live else "离线素材准备（没有调用模型）"), "",
             "基线与增强回答使用同一组窗口的字幕、同一问题和相同 DeepSeek 函数；仅增强组增加画面观察。",
             "人工参考答案仅用于复核，不发送给模型。模型回答不是人工确认的事实。",
             "Token 用量见 report.json；金额和正确率待实测复核。", ""]
    for index, case in enumerate(cases, 1):
        lines.extend([f"## Case {index}", "", "问题：" + case["question"], "", "状态：" + case["status"], ""])
        for clip in case["clips"]:
            relative = Path(clip["clip_path"]).relative_to(output).as_posix()
            lines.extend([
                f"- [片段 {clip['start_seconds']}–{clip['end_seconds']} 秒]({relative})"
                + (f"（尾帧补时 {clip['padding_seconds']} 秒，不是新事件）" if clip["padding_seconds"] else ""),
                "",
                "字幕：" + (clip["text"] or "（该窗口没有提供字幕证据）"), "",
            ])
        for field, label in (("baseline", "仅字幕回答"), ("enhanced", "字幕 + 画面回答")):
            value = case.get(field)
            lines.extend([f"### {label}", "", value["result"]["answer"] if value else "尚未生成。", ""])
        if case["error"]:
            lines.extend(["错误：" + case["error"], ""])
        lines.extend(["人工参考答案：" + (case["expected_answer"] or "待填写"), "",
                      "人工复核：基线是否正确 / 增强是否正确 / 画面误判 / 备注（填写 report.json 的 review 字段）。", ""])
    (output / "report.md").write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True, help="对比案例 JSON；媒体路径相对于该 JSON")
    parser.add_argument("--output", type=Path, required=True, help="新的输出目录，不能已存在")
    parser.add_argument("--live", action="store_true", help="真实调用 Qwen 和 DeepSeek，产生 API 费用；默认离线")
    args = parser.parse_args(argv)
    try:
        cases = load_cases(args.cases.resolve())
        if args.live:
            from app import ai_summary
            from app.qwen_client import analyze_clip, configuration
            configuration()
            if not ai_summary.is_configured():
                raise ValueError("真实对比需配置原有 DEEPSEEK_API_KEY")
        output = args.output.resolve()
        output.mkdir(parents=True, exist_ok=False)
        prepared = prepare_cases(cases, output)
        write_report(output, prepared, args.live)
        if args.live:
            for number, case in enumerate(prepared, 1):
                print(f"正在对比 case {number}/{len(prepared)}", flush=True)
                try:
                    compare_case(case, ai_summary.answer_question, analyze_clip)
                except Exception:
                    # 原回答函数的异常可能带服务端响应，报告只保存固定信息。
                    case["status"] = "failed"
                    case["error"] = "模型对比未完成，请检查服务配置、输入预算或重试；已完成阶段保留。"
                write_report(output, prepared, True)
        print(f"报告：{output / 'report.md'}", flush=True)
        return 1 if any(case["status"] == "failed" for case in prepared) else 0
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"验证准备失败：{exc}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
