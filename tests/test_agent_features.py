import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import agent_core
from app import agent_store
from app import ai_summary


class FakeResponse:
    status_code = 200
    text = ""

    def __init__(self, content: str, usage: dict | None = None):
        self.content = content
        self.usage = usage or {}

    def json(self):
        return {
            "choices": [{"message": {"content": self.content}}],
            "usage": self.usage,
        }


class AgentFeatureTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = agent_store.DB_PATH
        agent_store.DB_PATH = Path(self.temp_dir.name) / "agent.db"
        agent_store.init_db()

    def tearDown(self):
        agent_store.DB_PATH = self.db_path
        self.temp_dir.cleanup()

    def test_conversation_isolated_and_memory_indexed(self):
        conversation = agent_store.create_conversation(
            "HTTPS://Example.COM/video/#fragment", "user-a", None
        )
        run_id = agent_store.create_run(
            conversation["url"], "question", "为什么升级？", "user-a", None, conversation["id"]
        )
        agent_store.update_run(
            run_id,
            status="completed",
            result={
                "result": {
                    "answer": "为了兼容标准。",
                    "memory_summary": {
                        "summary": "讨论了升级原因。",
                        "fact_summary": "升级是为了兼容标准。",
                        "keywords": ["升级", "兼容"],
                    },
                }
            },
        )
        context = agent_store.get_conversation_context(conversation["id"], query="兼容标准")
        self.assertEqual(context["recent_turns"][0]["answer"], "为了兼容标准。")
        self.assertEqual(context["memories"][0]["fact_summary"], "升级是为了兼容标准。")
        self.assertTrue(not context["memory_fts_ids"] or run_id in context["memory_fts_ids"])
        self.assertEqual(
            agent_store.list_runs(conversation["url"], "user-b", None, conversation_id=conversation["id"]),
            [],
        )

    def test_legacy_runs_are_attached_to_a_conversation(self):
        agent_store.DB_PATH.unlink(missing_ok=True)
        conn = agent_store._connect()
        conn.execute(
            """
            CREATE TABLE agent_runs (
                id TEXT PRIMARY KEY, url TEXT, mode TEXT, question TEXT,
                owner_user_id TEXT, owner_token_hash TEXT, status TEXT,
                result_json TEXT, memory_summary_json TEXT, error TEXT,
                created_at INTEGER, started_at INTEGER, finished_at INTEGER
            )
            """
        )
        conn.execute(
            """
            INSERT INTO agent_runs
                (id, url, mode, question, owner_user_id, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("legacy", "https://example.com/video/", "question", "第一问", "user-a", "completed", 1),
        )
        conn.commit()
        conn.close()
        agent_store.init_db()
        conversations = agent_store.list_conversations("https://example.com/video", "user-a", None)
        self.assertEqual(len(conversations), 1)
        runs = agent_store.list_runs(
            "https://example.com/video", "user-a", None, conversation_id=conversations[0]["id"]
        )
        self.assertEqual(runs[0]["question"], "第一问")

    def test_evidence_budget_prefers_relevant_chunk(self):
        chunks = [
            {
                "id": str(index),
                "start": index,
                "end": index + 100,
                "text": ("目标内容 " * 40) if index == 7 else ("普通内容 " * 40),
            }
            for index in range(10)
        ]
        selected = agent_core.select_evidence(chunks, "目标内容", limit=8, max_chars=350)
        self.assertEqual(selected[0]["id"], "7")
        self.assertLessEqual(sum(len(item["text"]) for item in selected), 350)

    def test_rewrite_and_token_budget(self):
        response = FakeResponse('{"question":"系统为什么要升级？"}')
        with patch.object(ai_summary, "API_KEY", "test"), patch.object(
            ai_summary.requests, "post", return_value=response
        ):
            rewritten = ai_summary.rewrite_question("为什么？", "上一轮提到系统升级。")
        self.assertEqual(rewritten, "系统为什么要升级？")
        self.assertLessEqual(
            ai_summary.estimate_tokens(ai_summary.clip_to_tokens("中文" * 1000, 100)),
            100,
        )

    def test_answer_persists_memory_and_usage_shape(self):
        content = (
            '{"answer":"回答","evidence":[{"chunk_id":"c1","quote":"依据"}],'
            '"memory_summary":{"summary":"摘要","fact_summary":"事实",'
            '"source_chunk_ids":["c1"]}}'
        )
        response = FakeResponse(content, {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
        with patch.object(ai_summary, "API_KEY", "test"), patch.object(
            ai_summary.requests, "post", return_value=response
        ):
            result = ai_summary.answer_question("标题", "问题", [{"id": "c1", "text": "依据"}])
        self.assertEqual(result["memory_summary"]["fact_summary"], "事实")
        self.assertEqual(result["usage"]["total_tokens"], 15)


if __name__ == "__main__":
    unittest.main()
