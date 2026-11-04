import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app import agent_core
from app import agent_routes
from app import ai_summary
from app import video_enhancement as video
from app import qwen_client as qwen
from fastapi import HTTPException


class DirectVideoAnswerTests(unittest.TestCase):
    def test_final_request_contains_actual_videos_subtitles_history_and_source_time(self):
        with tempfile.TemporaryDirectory() as folder:
            clips = []
            for index in (2, 4):
                path = Path(folder) / f"clip-{index}.mp4"
                path.write_bytes(f"actual-video-{index}".encode())
                clips.append({"id": f"c{index}", "clip_path": str(path),
                              "start_seconds": index * 10, "end_seconds": index * 10 + 10})
            evidence = [{"id": "video-" + clip["id"], "source": "video", "text": "直接输入视频",
                         "start_seconds": clip["start_seconds"], "end_seconds": clip["end_seconds"]} for clip in clips]
            evidence.append({"id": "subtitle-c2", "source": "subtitle", "text": "字幕原文：演示操作"})
            data = {"answer": "红色杯子在显示器右侧", "evidence": [{"chunk_id": "video-c2", "quote": "杯子在显示器右侧", "start_seconds": 21, "end_seconds": 24}],
                    "memory_summary": {"summary": "杯子位置", "source_chunk_ids": ["video-c2", "invented"]}}
            response = Mock(status_code=200)
            response.json.return_value = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(data)}}],
                                          "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}}
            with patch.object(qwen, "configuration", return_value=("test-key", "https://qwen.example/v1", "test-vl")), \
                    patch.object(ai_summary, "API_KEY", ""), \
                    patch.object(qwen.requests, "post", return_value=response) as post:
                result = qwen.answer_video_question("测试标题", "它在哪里？", clips, evidence, context="上一轮讨论红色杯子", rewritten_question="红色杯子在哪里？")
            self.assertEqual(post.call_count, 1)
            self.assertEqual(post.call_args.args[0], "https://qwen.example/v1/chat/completions")
            content = post.call_args.kwargs["json"]["messages"][1]["content"]
            videos = [item for item in content if item["type"] == "video_url"]
            self.assertEqual(len(videos), 2)
            self.assertEqual(base64.b64decode(videos[0]["video_url"]["url"].split(",", 1)[1]), b"actual-video-2")
            text = "\n".join(item["text"] for item in content if item["type"] == "text")
            self.assertIn("片段内 0 秒映射为原视频 20 秒", text)
            self.assertIn("上一轮讨论红色杯子", text)
            self.assertIn("字幕原文：演示操作", text)
            self.assertEqual(result["answer_provider"], "qwen")
            self.assertEqual(result["memory_summary"]["source_chunk_ids"], ["video-c2"])
            self.assertEqual(result["evidence"][0]["start_seconds"], 21)
            self.assertEqual(result["usage"]["total_tokens"], 150)

    def test_citations_outside_actual_clip_and_unknown_sources_are_rejected(self):
        evidence = [{"id": "video-c1", "source": "video", "start_seconds": 20, "end_seconds": 21, "text": "尾片"}]
        data = {"answer": "回答", "evidence": [
            {"chunk_id": "video-c1", "start_seconds": 20, "end_seconds": 22},
            {"chunk_id": "fake"}, {"chunk_id": ["bad-format"]},
            {"chunk_id": "video-c1", "start_seconds": 20, "end_seconds": 21, "quote": "观察"},
        ], "memory_summary": {"source_chunk_ids": ["fake"], "fact_summary": "不受支持的事实"}}
        result = qwen.normalize_answer(data, evidence, "问题")
        self.assertEqual(len(result["evidence"]), 1)
        self.assertEqual(result["memory_summary"]["fact_summary"], "")

    def test_context_rewrite_uses_qwen(self):
        response = {"data": {"question": "红色杯子被放在哪里？"}}
        with patch.object(qwen, "_complete", return_value=response) as complete, patch.object(agent_core, "rewrite_question") as deepseek:
            rewritten = qwen.rewrite_question("它在哪里？", "此前讨论了红色杯子。")
        self.assertEqual(rewritten, "红色杯子被放在哪里？")
        complete.assert_called_once()
        deepseek.assert_not_called()

    def test_missing_qwen_configuration_rejected_before_creating_task(self):
        with patch.object(video, "enabled", return_value=True), \
                patch.object(agent_routes.membership, "is_member_active", return_value=True), \
                patch.object(qwen, "configuration", side_effect=ValueError("请配置 QWEN_VL_API_KEY")), \
                patch.object(agent_routes.agent_store, "create_run") as create:
            with self.assertRaises(HTTPException) as caught:
                agent_routes.create_agent_run(agent_routes.AgentRunRequest(url="https://example.test/v", mode="question", question="问题"), None, None)
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("QWEN", caught.exception.detail)
        create.assert_not_called()

    def test_qwen_question_route_does_not_require_deepseek_key(self):
        with patch.object(video, "enabled", return_value=True), \
                patch.object(agent_routes.membership, "is_member_active", return_value=True), \
                patch.object(qwen, "configuration", return_value=("test", "https://example.test/v1", "vl")), \
                patch.object(agent_routes, "is_configured", return_value=False) as deepseek_check, \
                patch.object(agent_routes.agent_store, "create_run", return_value="r1") as create, \
                patch.object(agent_routes.agent_store, "get_run", return_value={"id": "r1"}), \
                patch.object(agent_routes.threading, "Thread"):
            result = agent_routes.create_agent_run(agent_routes.AgentRunRequest(url="https://example.test/v", mode="question", question="问题"), None, None)
        self.assertEqual(result["id"], "r1")
        create.assert_called_once()
        deepseek_check.assert_not_called()

    def test_budget_never_drops_source_ids_for_videos_actually_sent(self):
        items = [{"id": f"video-{i}", "source": "video", "text": "片段来源"} for i in range(6)]
        items = [value for pair in zip(items, [{"id": f"subtitle-{i}", "text": "正文" * 1000} for i in range(6)]) for value in pair]
        selected = video._budget(items, max_chars=1500)
        self.assertEqual(len([item for item in selected if item.get("source") == "video"]), 6)


if __name__ == "__main__":
    unittest.main()
