import json
import os
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock, patch

from app import agent_core
from app import ai_summary
from app import subtitle
from app import video_cache as cache
from app import video_enhancement as video
from app import qwen_client as qwen
from app import video_retrieval as retrieval
from app.video_retrieval import SelectionError, select_windows
from app.video_media import build_windows


class RetrievalTests(unittest.TestCase):
    def test_uncertain_visual_records_are_read_instead_of_silently_discarded(self):
        windows = build_windows([], 10)
        analyses = {windows[0]["id"]: {"observations": [], "uncertainties": ["手持物体被遮挡，不能辨认型号"]}}
        with patch.object(qwen, "_complete", return_value={"data": {"selected_ids": [windows[0]["id"]]}}) as complete:
            selected = select_windows(windows, analyses, "它拿的什么", max_windows=1)
        self.assertEqual(selected, windows)
        self.assertIn("手持物体被遮挡", complete.call_args.args[0][1]["content"])

    def test_colloquial_question_selects_visual_evidence_without_shared_keywords(self):
        windows = build_windows([], 60)
        analyses = {windows[4]["id"]: {"observations": [{"description": "玩家手持带瞄准镜的黑色步枪"}]}}
        with patch.object(qwen, "_complete", return_value={"data": {"selected_ids": [windows[4]["id"]]}}) as complete:
            selected = select_windows(windows, analyses, "手里拿的什么枪")
        request = complete.call_args.args[0][1]["content"]
        self.assertIn("手里拿的什么枪", request)
        self.assertIn("玩家手持带瞄准镜的黑色步枪", request)
        self.assertEqual([item["start_seconds"] for item in selected], [30, 40, 50])
        with patch.object(qwen, "_complete", return_value={"data": {"selected_ids": []}}):
            self.assertEqual(select_windows(windows, analyses, "飞机起飞"), [])
        with patch.object(qwen, "_complete", return_value={"data": {"selected_ids": [windows[4]["id"]]}}):
            self.assertEqual([item["start_seconds"] for item in select_windows(windows, analyses, "型号", max_windows=1)], [40])

    def test_subtitle_matches_and_visual_matches_both_survive(self):
        windows = build_windows([{"start": 1, "end": 5, "text": "杯子用于喝水"}], 40)
        analyses = {windows[3]["id"]: {"observations": [{"description": "红色杯子"}]}}
        with patch.object(qwen, "_complete", return_value={"data": {"selected_ids": [windows[0]["id"], windows[3]["id"]]}}) as complete:
            selected = select_windows(windows, analyses, "杯子", top_k=2, max_windows=2)
        content = complete.call_args.args[0][1]["content"]
        self.assertIn("杯子用于喝水", content)
        self.assertIn("红色杯子", content)
        self.assertEqual([item["start_seconds"] for item in selected], [0, 30])

    def test_unknown_duplicate_and_malformed_ids_fail_instead_of_returning_no_match(self):
        windows = build_windows([{"start": 0, "end": 5, "text": "证据"}], 10)
        for ids in (["fake"], [windows[0]["id"], windows[0]["id"]], None, [["bad"]]):
            with self.subTest(ids=ids), patch.object(qwen, "_complete", return_value={"data": {"selected_ids": ids}}):
                with self.assertRaises(SelectionError):
                    select_windows(windows, {}, "问题")

    def test_long_index_reads_every_window_and_compares_candidates_across_batches(self):
        windows = build_windows([], 80)
        analyses = {w["id"]: {"observations": [{"description": "物体细节" * 30}]} for w in windows}
        seen = set()
        def select(question, candidates, limit):
            seen.update(item["id"] for item in candidates)
            return [item["id"] for item in candidates[-limit:]]
        with patch.object(retrieval, "SELECTION_BATCH_CHARS", 1100), patch.object(qwen, "select_clip_ids", side_effect=select) as model:
            selected = select_windows(windows, analyses, "它拿着啥", top_k=1, max_windows=1)
        self.assertEqual(seen, {w["id"] for w in windows})
        self.assertEqual(selected[0]["id"], windows[-1]["id"])
        self.assertGreater(model.call_count, 1)

    def test_selection_service_failure_propagates_through_prepare(self):
        with patch.object(video, "configuration", return_value=("key", "https://example.test", "vl")), \
                patch.object(video, "enhance", side_effect=SelectionError("模型选择失败")), \
                patch.object(cache, "session", return_value=nullcontext("key")):
            with self.assertRaises(SelectionError):
                with video.prepare("https://example.test/v", {}, "问题", [], Mock()):
                    pass

    def test_final_comparison_preserves_configured_number_of_core_clips(self):
        windows = build_windows([{"start": 0, "end": 80, "text": "字幕"}], 80)
        def split(candidates):
            # 首轮两批各有两个相关候选；最终四个候选足以放入一个请求。
            return [candidates[:4], candidates[4:]] if len(candidates) == 8 else [candidates]
        with patch.object(retrieval, "_batches", side_effect=split), \
                patch.object(qwen, "select_clip_ids", side_effect=[
                    [windows[0]["id"], windows[1]["id"]],
                    [windows[4]["id"], windows[5]["id"]],
                    [windows[0]["id"], windows[1]["id"], windows[4]["id"]],
                ]) as model:
            selected = select_windows(windows, {}, "问题", top_k=3, max_windows=3)
        self.assertEqual(len(selected), 3)
        self.assertEqual(model.call_args.args[2], 3)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.patches = [
            patch.object(cache, "CACHE_DIR", root / "cache"),
            patch.object(cache, "DB_PATH", root / "cache.db"),
            patch.object(cache, "probe_video", return_value=40),
            patch.object(cache, "_download", side_effect=self.download),
            patch.object(cache, "get_clip", side_effect=lambda source, window: {**window, "clip_path": "not-sent.mp4"}),
            patch.object(video, "configuration", return_value=("test-key", "https://example.test/v1", "test-vl")),
            patch.object(video, "analyze_clip", side_effect=self.analyze),
            patch.dict(os.environ, {"VIDEO_INDEX_MAX_CHUNKS_PER_RUN": "180", "VIDEO_ANALYSIS_ENABLED": "true"}),
            patch.object(qwen, "select_clip_ids", side_effect=lambda query, candidates, limit: [
                next((item["id"] for item in candidates if item["id"] == "chunk-000002"), candidates[0]["id"])
            ]),
        ]
        self.mocks = [item.start() for item in self.patches]
        self.url = "https://example.test/video?p=2"
        self.calls = []
        self.fail_at = None
        self.progress = Mock()
        self.subtitles = {"title": "t", "transcript": "讲解内容", "segments": [{"start": 0, "end": 40, "text": "讲解内容"}]}

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def download(self, url, directory):
        path = directory / "source.mp4"
        path.write_bytes(b"fake video content for hashing")
        return path, "测试视频"

    def analyze(self, clip, question):
        self.calls.append((clip["id"], question))
        if self.fail_at == clip["id"]:
            raise RuntimeError("模拟服务失败")
        description = "红色杯子放在显示器右侧" if clip["start_seconds"] == 20 else "绿色树林"
        return {"observations": [{"start_seconds": clip["start_seconds"], "end_seconds": clip["end_seconds"], "description": description}],
                "uncertainties": [], "usage": {"total_tokens": 10}, "model": "test-vl"}

    def run_video(self, question="红色杯子在哪里？"):
        return video.enhance(self.url, self.subtitles, question, [], self.progress)

    def test_pipeline_reuses_index_and_prepares_raw_clips_without_question_analysis(self):
        first = self.run_video()
        self.assertEqual(first["metadata"]["status"], "ready")
        self.assertEqual(first["metadata"]["indexed_chunks"], 4)
        self.assertEqual(first["metadata"]["refined_chunks"], 0)
        self.assertEqual(len(first["clips"]), 3)
        self.assertTrue(any(item["source"] == "video" for item in first["evidence"]))
        self.assertEqual(len(self.calls), 4)
        self.run_video()
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(self.mocks[3].call_count, 1)
        self.run_video("红色杯子被放在显示器哪一侧？")
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(cache.asset_key(self.url), cache.asset_key(self.url + "&vd_source=x"))
        self.assertNotEqual(cache.asset_key(self.url), cache.asset_key("https://example.test/video?p=1"))

    def test_interrupted_index_is_reused_when_resuming(self):
        self.fail_at = "chunk-000001"
        partial = self.run_video()
        self.assertEqual(partial["metadata"]["indexed_chunks"], 1)
        self.assertEqual(partial["metadata"]["coverage"], [{"start_seconds": 0, "end_seconds": 10}])
        self.fail_at = None
        self.calls.clear()
        result = self.run_video()
        self.assertEqual(result["metadata"]["indexed_chunks"], 4)
        self.assertNotIn(("chunk-000000", video.INDEX_PROMPT), self.calls)

    def test_cap_is_reported_and_next_question_continues(self):
        with patch.dict(os.environ, {"VIDEO_INDEX_MAX_CHUNKS_PER_RUN": "1"}):
            first = self.run_video()
            second = self.run_video()
        self.assertEqual(first["metadata"]["indexed_chunks"], 1)
        self.assertEqual(second["metadata"]["indexed_chunks"], 2)
        self.assertLess(second["metadata"]["indexed_chunks"], second["metadata"]["total_chunks"])

    def test_model_configuration_change_rebuilds_index(self):
        self.run_video()
        self.calls.clear()
        with patch.object(video, "configuration", return_value=("test-key", "https://example.test/v1", "new-model")):
            self.run_video()
        self.assertEqual(sum(question == video.INDEX_PROMPT for _, question in self.calls), 4)

    def test_global_question_is_flagged_even_with_complete_index(self):
        result = self.run_video("红色杯子一共出现几次？")
        self.assertEqual(result["metadata"]["indexed_chunks"], 4)
        self.assertEqual(result["metadata"]["status"], "global_limited")

    def test_provider_failure_stops_uncached_requests_in_same_run(self):
        self.subtitles["segments"][0]["text"] = "红色杯子"
        self.fail_at = "chunk-000001"
        result = self.run_video()
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(result["metadata"]["indexed_chunks"], 1)
        self.assertEqual(result["metadata"]["refined_chunks"], 0)
        self.assertNotEqual(result["metadata"]["status"], "ready")
        self.assertIn("chunk-000001", result["metadata"]["reason"])
        self.assertIn("模拟服务失败", result["metadata"]["reason"])

    def test_tracking_ids_reuse_cache_while_page_numbers_remain_distinct(self):
        self.assertEqual(cache.asset_key(self.url), cache.asset_key(self.url + "&trackid=first&spm_id_from=nav"))
        self.assertEqual(cache.asset_key(self.url + "&trackid=second"), cache.asset_key(self.url))
        self.assertNotEqual(cache.asset_key(self.url), cache.asset_key("https://example.test/video?p=1"))

    def test_out_of_range_subtitles_keep_mismatch_status(self):
        self.subtitles["segments"][0]["end"] = 400
        result = self.run_video()
        self.assertEqual(result["metadata"]["subtitle_alignment"], "mismatch")
        self.assertTrue(all(not clip["text"] for clip in result["clips"]))

    def test_cleanup_protects_active_asset_and_deletes_old_idle_asset(self):
        with cache.session(self.url) as first:
            cache.get_source(first, self.url)
        other_url = "https://example.test/other"
        with cache.session(other_url) as second:
            cache.get_source(second, other_url)
        with cache._connect() as conn:
            conn.execute("UPDATE video_assets SET last_used=0")
        with cache.session(self.url):
            cache.cleanup()
            self.assertTrue(cache._directory(first).is_dir())
            self.assertFalse(cache._directory(second).exists())
        self.assertFalse(cache._directory(first).exists())


class AgentIntegrationTests(unittest.TestCase):
    def test_missing_video_configuration_does_not_download_or_call_provider(self):
        fallback = [{"id": "c1", "text": "字幕"}]
        with patch.object(video, "configuration", side_effect=ValueError("未配置")), \
                patch.object(cache, "get_source") as source, patch.object(video, "analyze_clip") as analyze:
            result = video.enhance("https://example.test/v", {"transcript": "字幕"}, "问题", fallback, Mock())
        source.assert_not_called()
        analyze.assert_not_called()
        self.assertEqual(result["evidence"], fallback)
        self.assertEqual(result["metadata"]["status"], "unavailable")

    def test_disabled_path_never_calls_video_provider(self):
        with patch.object(agent_core, "fetch_subtitles", return_value={"title": "t", "transcript": "字幕证据"}), \
                patch.object(video, "enabled", return_value=False), \
                patch.object(video, "enhance") as enhance, \
                patch.object(agent_core, "answer_question", return_value={"answer": "原回答"}) as answer:
            result = agent_core.execute_agent("https://example.test/v", "question", "问题", Mock())
        enhance.assert_not_called()
        self.assertNotIn("video_context", answer.call_args.kwargs)
        self.assertNotIn("video_analysis", result["result"])

    def test_no_subtitles_can_use_raw_clips_and_qwen_without_deepseek(self):
        metadata = {"status": "ready", "indexed_chunks": 1, "total_chunks": 1, "has_visual_evidence": True}
        evidence = [{"id": "video-c1", "text": "视频片段", "source": "video", "start_seconds": 0, "end_seconds": 10}]
        clips = [{"id": "c1", "clip_path": "not-sent.mp4", "start_seconds": 0, "end_seconds": 10}]
        with patch.object(video, "enabled", return_value=True), \
                patch.object(agent_core, "fetch_subtitles", side_effect=subtitle.SubtitleError("没有字幕")), \
                patch.object(video, "prepare", return_value=nullcontext({"clips": clips, "evidence": evidence, "metadata": metadata})), \
                patch.object(agent_core.qwen, "answer_video_question", return_value={"answer": "红色", "_input_evidence": evidence}) as answer, \
                patch.object(agent_core, "answer_question") as deepseek:
            result = agent_core.execute_agent("https://example.test/v", "question", "什么颜色？", Mock())
        self.assertEqual(answer.call_args.args[2], clips)
        self.assertEqual(answer.call_args.args[3], evidence)
        deepseek.assert_not_called()
        self.assertEqual(result["result"]["evidence"][0]["start_seconds"], 0)
        self.assertEqual(result["result"]["video_analysis"]["status"], "ready")
        self.assertNotIn("_input_evidence", result["result"])

    def test_summary_never_invokes_video_analysis_even_when_enabled(self):
        with patch.object(video, "enabled", return_value=True), \
                patch.object(agent_core, "fetch_subtitles", return_value={"transcript": "总结字幕"}), \
                patch.object(video, "enhance") as enhance, \
                patch.object(agent_core, "generate_summary", return_value={"summary": "总结"}):
            agent_core.execute_agent("https://example.test/v", "summary", None, Mock())
        enhance.assert_not_called()

    def test_no_video_fallback_still_uses_qwen_and_reports_subtitles_only(self):
        prepared = {"clips": [], "evidence": [{"id": "s1", "text": "字幕证据"}],
                    "metadata": video.unavailable("本次未取得视频片段")}
        with patch.object(video, "enabled", return_value=True), \
                patch.object(agent_core, "fetch_subtitles", return_value={"transcript": "字幕证据"}), \
                patch.object(video, "prepare", return_value=nullcontext(prepared)), \
                patch.object(agent_core.qwen, "answer_video_question", return_value={"answer": "字幕回答"}) as answer, \
                patch.object(agent_core, "answer_question") as deepseek:
            result = agent_core.execute_agent("https://example.test/v", "question", "问题", Mock())
        self.assertEqual(answer.call_args.kwargs["video_context"]["status"], "unavailable")
        self.assertEqual(result["result"]["video_analysis"]["status"], "unavailable")
        deepseek.assert_not_called()

    def test_qwen_failure_is_not_silently_answered_by_deepseek(self):
        prepared = {"clips": [], "evidence": [], "metadata": video.unavailable("未取得画面")}
        with patch.object(video, "enabled", return_value=True), \
                patch.object(agent_core, "fetch_subtitles", return_value={"transcript": "字幕"}), \
                patch.object(video, "prepare", return_value=nullcontext(prepared)), \
                patch.object(agent_core.qwen, "answer_video_question", side_effect=RuntimeError("HTTP 401")), \
                patch.object(agent_core, "answer_question") as deepseek:
            with self.assertRaisesRegex(RuntimeError, "HTTP 401"):
                agent_core.execute_agent("https://example.test/v", "question", "问题", Mock())
        deepseek.assert_not_called()


class SubtitleAndPromptTests(unittest.TestCase):
    def test_legacy_cache_refreshes_only_when_timestamps_requested(self):
        cached = {"transcript": "缓存正文", "segments": []}
        fresh = {"transcript": "缓存正文", "segments": [{"start": 0, "end": 1, "text": "正文"}]}
        with patch.object(subtitle.subtitle_store, "get", return_value=cached), \
                patch.object(subtitle, "_fetch_subtitles_uncached", return_value=fresh) as fetch, \
                patch.object(subtitle.subtitle_store, "save", side_effect=lambda url, value: value):
            self.assertEqual(subtitle.fetch_subtitles("https://example.test/v"), cached)
            fetch.assert_not_called()
            self.assertEqual(subtitle.fetch_subtitles("https://example.test/v", require_timestamps=True), fresh)
            self.assertEqual(fetch.call_count, 1)

    def test_bilibili_second_page_uses_second_cid_and_keeps_segments(self):
        response = Mock()
        response.json.return_value = {"data": {"bvid": "BV1234567890", "cid": 11, "pages": [{"cid": 11}, {"cid": 22}]}}
        with patch.object(subtitle.requests, "get", return_value=response), \
                patch.object(subtitle, "_bilibili_anonymous_subtitles", return_value=[{"subtitle_url": "https://example.test/sub", "lan": "zh"}]) as tracks, \
                patch.object(subtitle, "_download_bili_subtitle_body", return_value=[{"from": 9, "to": 11, "content": "跨界字幕"}]):
            result = subtitle._fetch_bilibili_anonymous(("bvid", "BV1234567890"), page_index=1)
        tracks.assert_called_once_with("BV1234567890", 22)
        self.assertEqual(result["segments"][0]["start"], 9)

    def test_video_prompt_preserves_sources_limits_and_received_ids(self):
        response = Mock(status_code=200)
        response.json.return_value = {"choices": [{"message": {"content": '{"answer":"不能确认全片次数"}'}}], "usage": {}}
        evidence = [
            {"id": "subtitle-c1", "text": "字幕：" + "内容" * 4000},
            {"id": "visual-c1", "text": "画面观察：红色杯子" + "细节" * 4000},
        ]
        with patch.object(ai_summary, "API_KEY", "test"), patch.object(ai_summary.requests, "post", return_value=response) as post:
            result = ai_summary.answer_question("t", "一共几次？", evidence, context="历史内容" * 2000,
                                               video_context={"status": "global_limited", "has_visual_evidence": True})
        payload = post.call_args.kwargs["json"]
        system, user = (item["content"] for item in payload["messages"])
        self.assertIn("不能可靠回答全片精确计数", system)
        self.assertIn("[subtitle-c1]", user)
        self.assertIn("[visual-c1]", user)
        self.assertLessEqual(ai_summary.estimate_tokens(system + user), ai_summary.ANSWER_INPUT_TOKEN_BUDGET)
        self.assertTrue(all(item["truncated"] for item in result["_input_evidence"]))


if __name__ == "__main__":
    unittest.main()
