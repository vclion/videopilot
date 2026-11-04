import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tools import video_compare
from app import video_media as media
from app import qwen_client as models


class WindowTests(unittest.TestCase):
    def test_cross_boundary_exact_boundary_empty_and_tail(self):
        windows = media.build_windows([
            {"start": 9, "end": 11, "text": "跨界"},
            {"start": 10, "end": 20, "text": "第二块"},
        ], 31)
        self.assertEqual([item["text"] for item in windows], ["跨界", "跨界\n第二块", "", ""])
        self.assertEqual((windows[-1]["start_seconds"], windows[-1]["end_seconds"]), (30, 31))

    def test_bad_times_are_rejected(self):
        for start, end in [(-1, 10), (float("nan"), 10), (1, float("inf")), (3, 2), (True, 3)]:
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                media.build_windows([{"start": start, "end": end, "text": "x"}], 20)

    def test_timestamp_formats_and_legacy_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "subtitle.vtt"
            path.write_text("WEBVTT\n\nNOTE not a cue\nignored\n\n00:09.000 --> 00:11.000 align:start\n<b>跨界</b> &amp; 字幕\n", encoding="utf-8")
            self.assertEqual(media.load_segments(path), [{"start": 9, "end": 11, "text": "跨界 & 字幕"}])
            path = Path(folder) / "subtitle.srt"
            path.write_text("1\n00:00:10,100 --> 00:00:12,500\n测试\n", encoding="utf-8")
            self.assertEqual(media.load_segments(path)[0]["start"], 10.1)
            path = Path(folder) / "subtitle.json"
            path.write_text(json.dumps({"body": [{"from": 9, "to": 11, "content": "B站"}]}), encoding="utf-8")
            self.assertEqual(media.load_segments(path)[0]["text"], "B站")
            path.write_text('{"transcript":"只有正文","segments":[]}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "时间戳"):
                media.load_segments(path)


class ModelContractTests(unittest.TestCase):
    def test_invalid_tail_analysis_is_corrected_once_using_actual_video(self):
        clip = {"start_seconds": 310, "end_seconds": 311.166625}
        invalid = {"observations": [{"start_seconds": 0, "end_seconds": 2, "description": "画面"}], "uncertainties": []}
        valid = {"observations": [{"start_seconds": 0, "end_seconds": 1.167, "description": "画面"}], "uncertainties": []}
        with patch.object(models, "video_block", return_value={"type": "video_url", "video_url": {"url": "fixture"}}), \
                patch.object(models, "_complete", side_effect=[{"data": invalid}, {"data": valid}]) as complete:
            result = models.analyze_clip(clip, "记录画面")
        self.assertEqual(complete.call_count, 2)
        self.assertEqual(result["observations"][0]["end_seconds"], clip["end_seconds"])
        self.assertIn("1.166625", complete.call_args.args[0][-1]["content"])
        with patch.object(models, "video_block", return_value={"type": "video_url"}), \
                patch.object(models, "_complete", side_effect=[{"data": invalid}, {"data": invalid}]) as complete:
            with self.assertRaisesRegex(RuntimeError, "时间戳"):
                models.analyze_clip(clip, "记录画面")
        self.assertEqual(complete.call_count, 2)

    def test_fractional_tail_accepts_millisecond_rounding_without_exposing_padding(self):
        clip = {"start_seconds": 310, "end_seconds": 311.166625}
        data = {"observations": [{"start_seconds": 0, "end_seconds": 1.167, "description": "尾片段画面"}], "uncertainties": []}
        result = models.normalize_analysis(data, clip)
        self.assertEqual(result["observations"][0]["end_seconds"], clip["end_seconds"])
        for end in (1.168, 2):
            data["observations"][0]["end_seconds"] = end
            with self.subTest(end=end), self.assertRaises(ValueError):
                models.normalize_analysis(data, clip)

    def test_fractional_tail_citations_clamp_rounding_and_reject_real_overflow(self):
        evidence = [{"id": "video-tail", "source": "video", "start_seconds": 310, "end_seconds": 311.166625}]
        data = {"answer": "画面内容", "evidence": [
            {"chunk_id": "video-tail", "start_seconds": 310, "end_seconds": 311.167},
            {"chunk_id": "video-tail", "start_seconds": 310, "end_seconds": 311.168},
            {"chunk_id": "video-tail", "start_seconds": 311.167, "end_seconds": 312},
        ]}
        result = models.normalize_answer(data, evidence, "问题")
        self.assertEqual(len(result["evidence"]), 1)
        self.assertEqual(result["evidence"][0]["end_seconds"], evidence[0]["end_seconds"])

    def test_relative_observations_map_to_source_and_reject_padding(self):
        clip = {"start_seconds": 20, "end_seconds": 21}
        data = {"observations": [{"start_seconds": 0, "end_seconds": 1, "description": "绿色"}], "uncertainties": []}
        result = models.normalize_analysis(data, clip)
        self.assertEqual(result["observations"][0]["start_seconds"], 20)
        self.assertEqual(result["observations"][0]["end_seconds"], 21)
        data["observations"][0]["end_seconds"] = 2
        with self.assertRaises(ValueError):
            models.normalize_analysis(data, clip)

    def test_video_payload_and_error_do_not_expose_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.mp4"
            path.write_bytes(b"video bytes")
            clip = {"clip_path": str(path), "start_seconds": 0, "end_seconds": 10}
            response = Mock(status_code=401)
            response.text = "sensitive-key"
            with patch.object(models, "configuration", return_value=("sensitive-key", "https://example.test/v1", "test-model")), patch.object(models.requests, "post", return_value=response) as post:
                with self.assertRaisesRegex(RuntimeError, "HTTP 401") as caught:
                    models.analyze_clip(clip, "颜色？")
                self.assertNotIn("sensitive-key", str(caught.exception))
                self.assertFalse(post.call_args.kwargs["allow_redirects"])
                content = post.call_args.kwargs["json"]["messages"][1]["content"]
                self.assertTrue(content[0]["video_url"]["url"].startswith("data:video/mp4;base64,"))
                self.assertEqual(content[0]["fps"], 2)

    def test_both_answers_share_subtitles_and_reference_answer_is_not_sent(self):
        case = {
            "title": "测试", "question": "是什么颜色？", "expected_answer": "不应发送的人工答案",
            "subtitle_evidence": [{"id": "s1", "text": "字幕"}],
            "clips": [{"id": "c1", "start_seconds": 10, "end_seconds": 20}],
            "visual_analyses": [],
        }
        answer = Mock(return_value={"answer": "回答"})
        analyze = Mock(return_value={"observations": [{"start_seconds": 10, "end_seconds": 20, "description": "蓝色"}], "uncertainties": []})
        video_compare.compare_case(case, answer, analyze)
        first = answer.call_args_list[0].args[2]
        second = answer.call_args_list[1].args[2]
        self.assertEqual(first, case["subtitle_evidence"])
        self.assertEqual(second[0], first[0])
        self.assertEqual(second[1]["id"], "visual-c1")
        self.assertNotIn(case["expected_answer"], repr(answer.call_args_list) + repr(analyze.call_args_list))
        self.assertEqual(case["status"], "completed")

    def test_visual_failure_never_becomes_a_successful_enhanced_answer(self):
        case = {"title": "t", "question": "q", "subtitle_evidence": [], "clips": [{}], "visual_analyses": [], "enhanced": None}
        answer = Mock(return_value={"answer": "基线"})
        with self.assertRaises(RuntimeError):
            video_compare.compare_case(case, answer, Mock(side_effect=RuntimeError("失败")))
        self.assertEqual(case["baseline"]["result"]["answer"], "基线")
        self.assertIsNone(case["enhanced"])
        self.assertEqual(answer.call_count, 1)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "需要 FFmpeg")
class MediaIntegrationTests(unittest.TestCase):
    def test_offline_pipeline_real_cut_boundaries_and_short_tail(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.mp4"
            subprocess.run([
                "ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "color=red:s=64x64:r=12:d=10",
                "-f", "lavfi", "-i", "color=blue:s=64x64:r=12:d=10", "-f", "lavfi", "-i", "color=lime:s=64x64:r=12:d=1",
                "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]", "-map", "[v]",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source),
            ], check=True, capture_output=True, timeout=30)
            subtitles = root / "subtitles.json"
            subtitles.write_text(json.dumps({"segments": [{"start": 9, "end": 11, "text": "跨界字幕"}]}), encoding="utf-8")
            cases = root / "cases.json"
            cases.write_text(json.dumps({"cases": [{"video": "source.mp4", "subtitles": "subtitles.json", "windows": [0, 1, 2], "question": "什么颜色？"}]}), encoding="utf-8")
            with patch("requests.post", side_effect=AssertionError("离线模式不应联网")):
                self.assertEqual(video_compare.main(["--cases", str(cases), "--output", str(root / "out")]), 0)
            report = json.loads((root / "out/report.json").read_text(encoding="utf-8"))
            case = report["cases"][0]
            self.assertIsNone(case["baseline"])
            self.assertEqual(case["clips"][2]["padding_seconds"], 1)
            self.assertAlmostEqual(case["clips"][2]["media_seconds"], 2, places=1)
            self.assertEqual(case["clips"][2]["end_seconds"], 21)
            # 核验真实切片第一帧的颜色，避免只测试命令参数或文件存在。
            for index, channel in [(0, 0), (1, 2), (2, 1)]:
                raw = subprocess.run([
                    "ffmpeg", "-nostdin", "-v", "error", "-i", case["clips"][index]["clip_path"],
                    "-frames:v", "1", "-vf", "scale=1:1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
                ], check=True, capture_output=True, timeout=30).stdout
                self.assertGreater(raw[channel], 200)
                self.assertTrue(all(raw[c] < 30 for c in range(3) if c != channel))


if __name__ == "__main__":
    unittest.main()
