"""Agent 运行记录的数据层。

这个模块使用独立的 SQLite 文件，避免改动现有会员数据库和已有业务表。
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit


from .paths import PROJECT_ROOT as BASE_DIR, database_path
DB_PATH = database_path("agent.db")
LOCK = threading.Lock()


def _now() -> int:
    return int(time.time())


def canonicalize_url(value: str) -> str:
    """保留视频定位参数，同时消除协议、主机大小写和片段差异。"""
    parsed = urlsplit((value or "").strip())
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit((scheme, netloc, path, parsed.query, ""))


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _dump(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def _load(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _refresh_rolling_summary_locked(conn: sqlite3.Connection, conversation_id: str) -> None:
    summaries = conn.execute(
        """
        SELECT memory_summary_json
        FROM agent_runs
        WHERE conversation_id = ? AND status = 'completed'
          AND memory_summary_json IS NOT NULL
        ORDER BY created_at DESC, id DESC
        LIMIT 6
        """,
        (conversation_id,),
    ).fetchall()
    digest_parts: list[str] = []
    for summary_row in reversed(summaries):
        summary = _load(summary_row["memory_summary_json"], {})
        if not isinstance(summary, dict):
            continue
        text = str(summary.get("fact_summary") or summary.get("summary") or "").strip()
        if text:
            digest_parts.append(text)
    rolling_summary = "\n".join(f"- {item}" for item in digest_parts)[-1600:]
    conn.execute(
        "UPDATE agent_conversations SET rolling_summary = ?, updated_at = ? WHERE id = ?",
        (rolling_summary, _now(), conversation_id),
    )


def _memory_index_content(question: str, memory_summary: dict[str, Any]) -> str:
    return " ".join(
        [
            question or "",
            str(memory_summary.get("summary") or ""),
            str(memory_summary.get("fact_summary") or ""),
            str(memory_summary.get("conversation_summary") or ""),
            " ".join(str(item) for item in memory_summary.get("keywords") or []),
            " ".join(str(item) for item in memory_summary.get("unresolved") or []),
        ]
    ).strip()


def _ensure_memory_fts_locked(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS agent_memory_fts USING fts5(
                run_id UNINDEXED,
                conversation_id UNINDEXED,
                content
            )
            """
        )
        return True
    except sqlite3.OperationalError:
        return False


def _upsert_memory_fts_locked(
    conn: sqlite3.Connection,
    run_id: str,
    conversation_id: str | None,
    question: str,
    memory_summary: dict[str, Any],
) -> None:
    if not conversation_id or not _ensure_memory_fts_locked(conn):
        return
    conn.execute("DELETE FROM agent_memory_fts WHERE run_id = ?", (run_id,))
    conn.execute(
        "INSERT INTO agent_memory_fts(run_id, conversation_id, content) VALUES (?, ?, ?)",
        (run_id, conversation_id, _memory_index_content(question, memory_summary)),
    )


def _fts_memory_ids_locked(
    conn: sqlite3.Connection,
    conversation_id: str,
    query: str,
    limit: int,
) -> list[str]:
    if not query or not _ensure_memory_fts_locked(conn):
        return []
    terms = re.findall(r"[a-zA-Z0-9_]{2,}|[\u4e00-\u9fff]{2,}", query.lower())
    if not terms:
        return []
    safe_terms = [term.replace('"', "") for term in terms[:12] if term.replace('"', "")]
    if not safe_terms:
        return []
    fts_query = " OR ".join('"' + term + '"' for term in safe_terms)
    try:
        rows = conn.execute(
            """
            SELECT run_id
            FROM agent_memory_fts
            WHERE conversation_id = ? AND agent_memory_fts MATCH ?
            ORDER BY bm25(agent_memory_fts)
            LIMIT ?
            """,
            (conversation_id, fts_query, max(1, min(limit, 50))),
        ).fetchall()
        return [row["run_id"] for row in rows]
    except sqlite3.OperationalError:
        return []


def _migrate_legacy_conversations_locked(conn: sqlite3.Connection) -> None:
    """把升级前只有 agent_runs 的历史记录迁移到会话表。"""
    legacy_rows = conn.execute(
        """
        SELECT url, owner_user_id, owner_token_hash,
               MIN(created_at) AS first_created, MAX(created_at) AS last_updated
        FROM agent_runs
        WHERE conversation_id IS NULL
        GROUP BY url, owner_user_id, owner_token_hash
        """
    ).fetchall()
    for legacy in legacy_rows:
        url = canonicalize_url(legacy["url"])
        owner_user_id = legacy["owner_user_id"]
        owner_token_hash = legacy["owner_token_hash"]
        where, params = _conversation_where(owner_user_id, owner_token_hash)
        row = conn.execute(
            f"SELECT id FROM agent_conversations WHERE url = ? AND {where} ORDER BY created_at LIMIT 1",
            (url, *params),
        ).fetchone()
        if row:
            conversation_id = row["id"]
        else:
            conversation_id = uuid.uuid4().hex[:16]
            conn.execute(
                """
                INSERT INTO agent_conversations
                    (id, url, owner_user_id, owner_token_hash, rolling_summary, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    conversation_id,
                    url,
                    owner_user_id,
                    owner_token_hash,
                    "",
                    legacy["first_created"] or _now(),
                    legacy["last_updated"] or _now(),
                ),
            )
        conn.execute(
            """
            UPDATE agent_runs
            SET conversation_id = ?, url = ?
            WHERE conversation_id IS NULL AND url = ?
              AND owner_user_id IS ? AND owner_token_hash IS ?
            """,
            (
                conversation_id,
                url,
                legacy["url"],
                owner_user_id,
                owner_token_hash,
            ),
        )
        _refresh_rolling_summary_locked(conn, conversation_id)


def init_db() -> None:
    with LOCK:
        conn = _connect()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS agent_conversations (
                    id TEXT PRIMARY KEY,
                    url TEXT NOT NULL,
                    owner_user_id TEXT,
                    owner_token_hash TEXT,
                    rolling_summary TEXT,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_agent_conversations_user_url
                    ON agent_conversations(owner_user_id, url);

                CREATE INDEX IF NOT EXISTS idx_agent_conversations_token_url
                    ON agent_conversations(owner_token_hash, url);

                CREATE TABLE IF NOT EXISTS agent_runs (
                    id TEXT PRIMARY KEY,
                    url TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    question TEXT,
                    conversation_id TEXT,
                    owner_user_id TEXT,
                    owner_token_hash TEXT,
                    status TEXT NOT NULL,
                    result_json TEXT,
                    memory_summary_json TEXT,
                    error TEXT,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    started_at INTEGER,
                    finished_at INTEGER
                );

                CREATE INDEX IF NOT EXISTS idx_agent_runs_owner_user
                    ON agent_runs(owner_user_id);

                CREATE TABLE IF NOT EXISTS agent_steps (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    step_no INTEGER NOT NULL,
                    tool_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    input_json TEXT,
                    output_json TEXT,
                    error TEXT,
                    created_at INTEGER NOT NULL,
                    finished_at INTEGER,
                    FOREIGN KEY(run_id) REFERENCES agent_runs(id)
                );

                CREATE INDEX IF NOT EXISTS idx_agent_steps_run
                    ON agent_steps(run_id, step_no);
                """
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(agent_runs)").fetchall()}
            if "conversation_id" not in columns:
                conn.execute("ALTER TABLE agent_runs ADD COLUMN conversation_id TEXT")
            if "memory_summary_json" not in columns:
                conn.execute("ALTER TABLE agent_runs ADD COLUMN memory_summary_json TEXT")
            if "attempt" not in columns:
                conn.execute("ALTER TABLE agent_runs ADD COLUMN attempt INTEGER NOT NULL DEFAULT 0")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_agent_runs_conversation ON agent_runs(conversation_id, created_at)"
            )
            _migrate_legacy_conversations_locked(conn)
            if _ensure_memory_fts_locked(conn):
                memory_rows = conn.execute(
                    """
                    SELECT id, conversation_id, question, memory_summary_json
                    FROM agent_runs
                    WHERE memory_summary_json IS NOT NULL
                    """
                ).fetchall()
                for row in memory_rows:
                    summary = _load(row["memory_summary_json"], {})
                    if isinstance(summary, dict):
                        _upsert_memory_fts_locked(
                            conn,
                            row["id"],
                            row["conversation_id"],
                            row["question"] or "",
                            summary,
                        )
            conn.commit()
        finally:
            conn.close()


def recover_stale_runs(max_age: int = 900) -> int:
    """服务重启后清理已经没有工作线程负责的旧任务，避免永久停在 running。"""
    cutoff = _now() - max(60, max_age)
    with LOCK:
        conn = _connect()
        try:
            cursor = conn.execute(
                """
                UPDATE agent_runs
                SET status = 'failed', error = ?, finished_at = ?
                WHERE status IN ('queued', 'running') AND created_at < ?
                """,
                ("服务重启后任务已过期，请重新提交", _now(), cutoff),
            )
            conn.commit()
            return cursor.rowcount
        finally:
            conn.close()


def _conversation_where(
    owner_user_id: str | None,
    owner_token_hash: str | None,
) -> tuple[str, tuple[Any, ...]]:
    if owner_user_id:
        return "owner_user_id = ? AND owner_token_hash IS NULL", (owner_user_id,)
    if owner_token_hash:
        return "owner_user_id IS NULL AND owner_token_hash = ?", (owner_token_hash,)
    return "1 = 0", ()


def _get_or_create_conversation_locked(
    conn: sqlite3.Connection,
    url: str,
    owner_user_id: str | None,
    owner_token_hash: str | None,
) -> str:
    where, params = _conversation_where(owner_user_id, owner_token_hash)
    row = conn.execute(
        f"SELECT id FROM agent_conversations WHERE url = ? AND {where} ORDER BY created_at LIMIT 1",
        (url, *params),
    ).fetchone()
    if row:
        conversation_id = row["id"]
    else:
        conversation_id = uuid.uuid4().hex[:16]
        now = _now()
        conn.execute(
            """
            INSERT INTO agent_conversations
                (id, url, owner_user_id, owner_token_hash, rolling_summary, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (conversation_id, url, owner_user_id, owner_token_hash, "", now, now),
        )

    # 将升级前按 URL 保存的旧记录接入新会话，保证历史不会断开。
    conn.execute(
        f"UPDATE agent_runs SET conversation_id = ? WHERE conversation_id IS NULL AND url = ? AND {where}",
        (conversation_id, url, *params),
    )
    return conversation_id


def get_or_create_conversation(
    url: str,
    owner_user_id: str | None,
    owner_token_hash: str | None,
) -> str:
    url = canonicalize_url(url)
    with LOCK:
        conn = _connect()
        try:
            conversation_id = _get_or_create_conversation_locked(
                conn, url, owner_user_id, owner_token_hash
            )
            conn.commit()
            return conversation_id
        finally:
            conn.close()


def create_run(
    url: str,
    mode: str,
    question: str | None,
    owner_user_id: str | None,
    owner_token_hash: str | None,
    conversation_id: str | None = None,
) -> str:
    url = canonicalize_url(url)
    run_id = uuid.uuid4().hex[:16]
    now = _now()
    with LOCK:
        conn = _connect()
        try:
            if conversation_id:
                where, params = _conversation_where(owner_user_id, owner_token_hash)
                row = conn.execute(
                    f"SELECT id FROM agent_conversations WHERE id = ? AND url = ? AND {where}",
                    (conversation_id, url, *params),
                ).fetchone()
                if not row:
                    raise ValueError("会话不存在或无权访问")
            else:
                conversation_id = _get_or_create_conversation_locked(
                    conn, url, owner_user_id, owner_token_hash
                )
            conn.execute(
                """
                INSERT INTO agent_runs
                    (id, url, mode, question, conversation_id, owner_user_id,
                     owner_token_hash, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    url,
                    mode,
                    question,
                    conversation_id,
                    owner_user_id,
                    owner_token_hash,
                    "queued",
                    now,
                ),
            )
            conn.commit()
        finally:
            conn.close()
    return run_id


def update_run(run_id: str, **fields: Any) -> None:
    allowed = {
        "status",
        "result_json",
        "memory_summary_json",
        "error",
        "attempt",
        "started_at",
        "finished_at",
    }
    changes = {key: value for key, value in fields.items() if key in allowed}
    if not changes:
        return

    if "result" in fields:
        changes["result_json"] = _dump(fields["result"])
        result = fields["result"] if isinstance(fields["result"], dict) else {}
        answer_result = result.get("result") if isinstance(result.get("result"), dict) else result
        memory_summary = answer_result.get("memory_summary")
        if isinstance(memory_summary, dict):
            changes["memory_summary_json"] = _dump(memory_summary)
    changes.pop("result", None)

    assignments = ", ".join(f"{key} = ?" for key in changes)
    values = list(changes.values()) + [run_id]
    with LOCK:
        conn = _connect()
        try:
            conn.execute(f"UPDATE agent_runs SET {assignments} WHERE id = ?", values)
            if changes.get("memory_summary_json"):
                row = conn.execute(
                    "SELECT conversation_id, question FROM agent_runs WHERE id = ?", (run_id,)
                ).fetchone()
                if row and row["conversation_id"]:
                    memory_summary = _load(changes["memory_summary_json"], {})
                    if isinstance(memory_summary, dict):
                        _upsert_memory_fts_locked(
                            conn,
                            run_id,
                            row["conversation_id"],
                            row["question"] or "",
                            memory_summary,
                        )
                    _refresh_rolling_summary_locked(conn, row["conversation_id"])
            conn.commit()
        finally:
            conn.close()


def add_step(
    run_id: str,
    step_no: int,
    tool_name: str,
    status: str,
    input_data: Any = None,
    output_data: Any = None,
    error: str | None = None,
) -> None:
    now = _now()
    with LOCK:
        conn = _connect()
        try:
            if not conn.execute("SELECT 1 FROM agent_runs WHERE id = ?", (run_id,)).fetchone():
                return
            conn.execute(
                """
                INSERT INTO agent_steps
                    (run_id, step_no, tool_name, status, input_json,
                     output_json, error, created_at, finished_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    step_no,
                    tool_name,
                    status,
                    _dump(input_data),
                    _dump(output_data),
                    error,
                    now,
                    now if status in {"completed", "failed"} else None,
                ),
            )
            conn.commit()
        finally:
            conn.close()


def list_runs(
    url: str,
    owner_user_id: str | None,
    owner_token_hash: str | None,
    limit: int = 50,
    conversation_id: str | None = None,
) -> list[dict[str, Any]]:
    """按视频和归属身份读取问答历史，不返回执行步骤以减少前端负担。"""
    url = canonicalize_url(url)
    if owner_user_id:
        where = "url = ? AND owner_user_id = ?"
        params: tuple[Any, ...] = (url, owner_user_id)
    elif owner_token_hash:
        where = "url = ? AND owner_user_id IS NULL AND owner_token_hash = ?"
        params = (url, owner_token_hash)
    else:
        return []
    if conversation_id:
        where += " AND conversation_id = ?"
        params += (conversation_id,)

    with LOCK:
        conn = _connect()
        try:
            rows = conn.execute(
                f"""
                SELECT id, url, mode, question, status, result_json, error,
                       created_at, started_at, finished_at
                FROM agent_runs
                WHERE {where}
                ORDER BY created_at ASC, id ASC
                LIMIT ?
                """,
                (*params, max(1, min(limit, 100))),
            ).fetchall()
            return [
                {
                    "id": row["id"],
                    "url": row["url"],
                    "mode": row["mode"],
                    "question": row["question"],
                    "status": row["status"],
                    "result": _load(row["result_json"]),
                    "error": row["error"],
                    "created_at": row["created_at"],
                    "started_at": row["started_at"],
                    "finished_at": row["finished_at"],
                    "steps": [],
                }
                for row in rows
            ]
        finally:
            conn.close()


def _conversation_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "url": row["url"],
        "rolling_summary": row["rolling_summary"] or "",
        "run_count": row["run_count"] if "run_count" in row.keys() else 0,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _conversation_record(row: sqlite3.Row) -> dict[str, Any]:
    conversation = _conversation_dict(row)
    conversation["owner_user_id"] = row["owner_user_id"]
    conversation["owner_token_hash"] = row["owner_token_hash"]
    return conversation


def create_conversation(
    url: str,
    owner_user_id: str | None,
    owner_token_hash: str | None,
) -> dict[str, Any]:
    url = canonicalize_url(url)
    with LOCK:
        conn = _connect()
        try:
            conversation_id = uuid.uuid4().hex[:16]
            now = _now()
            conn.execute(
                """
                INSERT INTO agent_conversations
                    (id, url, owner_user_id, owner_token_hash, rolling_summary, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (conversation_id, url, owner_user_id, owner_token_hash, "", now, now),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM agent_conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
            return _conversation_dict(row)
        finally:
            conn.close()


def get_conversation(conversation_id: str) -> dict[str, Any] | None:
    with LOCK:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT * FROM agent_conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
            return _conversation_record(row) if row else None
        finally:
            conn.close()


def list_conversations(
    url: str,
    owner_user_id: str | None,
    owner_token_hash: str | None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    url = canonicalize_url(url)
    where, params = _conversation_where(owner_user_id, owner_token_hash)
    with LOCK:
        conn = _connect()
        try:
            rows = conn.execute(
                f"""
                SELECT c.*, COUNT(r.id) AS run_count
                FROM agent_conversations AS c
                LEFT JOIN agent_runs AS r ON r.conversation_id = c.id
                WHERE c.url = ? AND {where.replace('owner_user_id', 'c.owner_user_id').replace('owner_token_hash', 'c.owner_token_hash')}
                GROUP BY c.id
                ORDER BY updated_at DESC, id DESC
                LIMIT ?
                """,
                (url, *params, max(1, min(limit, 50))),
            ).fetchall()
            return [_conversation_dict(row) for row in rows]
        finally:
            conn.close()


def delete_conversation(conversation_id: str) -> bool:
    with LOCK:
        conn = _connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            exists = conn.execute(
                "SELECT 1 FROM agent_conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
            if not exists:
                conn.rollback()
                return False
            conn.execute(
                "DELETE FROM agent_steps WHERE run_id IN (SELECT id FROM agent_runs WHERE conversation_id = ?)",
                (conversation_id,),
            )
            if _ensure_memory_fts_locked(conn):
                conn.execute(
                    "DELETE FROM agent_memory_fts WHERE conversation_id = ?",
                    (conversation_id,),
                )
            conn.execute("DELETE FROM agent_runs WHERE conversation_id = ?", (conversation_id,))
            conn.execute("DELETE FROM agent_conversations WHERE id = ?", (conversation_id,))
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def get_conversation_context(
    conversation_id: str,
    exclude_run_id: str | None = None,
    recent_limit: int = 4,
    memory_limit: int = 50,
    query: str | None = None,
) -> dict[str, Any]:
    """读取同一会话的短期对话、摘要记忆和滚动摘要。"""
    with LOCK:
        conn = _connect()
        try:
            conversation = conn.execute(
                "SELECT rolling_summary FROM agent_conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
            if not conversation:
                return {
                    "rolling_summary": "",
                    "recent_turns": [],
                    "memories": [],
                    "memory_fts_ids": [],
                }

            exclude_sql = "AND id != ?" if exclude_run_id else ""
            exclude_params: tuple[Any, ...] = (exclude_run_id,) if exclude_run_id else ()
            rows = conn.execute(
                f"""
                SELECT id, question, result_json, created_at
                FROM agent_runs
                WHERE conversation_id = ? AND mode = 'question' AND status = 'completed' {exclude_sql}
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (conversation_id, *exclude_params, max(1, min(recent_limit, 10))),
            ).fetchall()
            memory_rows = conn.execute(
                f"""
                SELECT id, question, memory_summary_json, created_at
                FROM agent_runs
                WHERE conversation_id = ? AND mode = 'question' AND status = 'completed'
                  AND memory_summary_json IS NOT NULL {exclude_sql}
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (conversation_id, *exclude_params, max(1, min(memory_limit, 100))),
            ).fetchall()

            recent_turns: list[dict[str, Any]] = []
            for row in rows:
                result = _load(row["result_json"], {})
                if not isinstance(result, dict):
                    result = {}
                answer_result = result.get("result") if isinstance(result.get("result"), dict) else result
                recent_turns.append(
                    {
                        "run_id": row["id"],
                        "question": row["question"] or "",
                        "answer": str(answer_result.get("answer") or "")[:800],
                        "created_at": row["created_at"],
                    }
                )

            memories: list[dict[str, Any]] = []
            for row in memory_rows:
                summary = _load(row["memory_summary_json"], {})
                if not isinstance(summary, dict):
                    continue
                memories.append(
                    {
                        "run_id": row["id"],
                        "question": row["question"] or "",
                        "summary": str(summary.get("summary") or "")[:600],
                        "fact_summary": str(summary.get("fact_summary") or "")[:600],
                        "conversation_summary": str(summary.get("conversation_summary") or "")[:600],
                        "confidence": str(summary.get("confidence") or "")[:20],
                        "keywords": summary.get("keywords") or [],
                        "unresolved": summary.get("unresolved") or [],
                        "source_chunk_ids": summary.get("source_chunk_ids") or [],
                        "created_at": row["created_at"],
                    }
                )
            return {
                "rolling_summary": conversation["rolling_summary"] or "",
                "recent_turns": recent_turns,
                "memories": memories,
                "memory_fts_ids": _fts_memory_ids_locked(
                    conn, conversation_id, query or "", memory_limit
                ),
            }
        finally:
            conn.close()


def delete_run(run_id: str) -> bool:
    """删除一条问答记录及其执行步骤。"""
    with LOCK:
        conn = _connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT conversation_id FROM agent_runs WHERE id = ?", (run_id,)
            ).fetchone()
            conn.execute("DELETE FROM agent_steps WHERE run_id = ?", (run_id,))
            if _ensure_memory_fts_locked(conn):
                conn.execute("DELETE FROM agent_memory_fts WHERE run_id = ?", (run_id,))
            cursor = conn.execute("DELETE FROM agent_runs WHERE id = ?", (run_id,))
            if row and row["conversation_id"]:
                _refresh_rolling_summary_locked(conn, row["conversation_id"])
            conn.commit()
            return cursor.rowcount > 0
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def _step_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "step_no": row["step_no"],
        "tool": row["tool_name"],
        "status": row["status"],
        "input": _load(row["input_json"], {}),
        "output": _load(row["output_json"], {}),
        "error": row["error"],
        "created_at": row["created_at"],
        "finished_at": row["finished_at"],
    }


def get_run(run_id: str) -> dict[str, Any] | None:
    with LOCK:
        conn = _connect()
        try:
            row = conn.execute("SELECT * FROM agent_runs WHERE id = ?", (run_id,)).fetchone()
            if not row:
                return None
            steps = conn.execute(
                "SELECT * FROM agent_steps WHERE run_id = ? ORDER BY step_no, id",
                (run_id,),
            ).fetchall()
            return {
                "id": row["id"],
                "url": row["url"],
                "mode": row["mode"],
                "question": row["question"],
                "conversation_id": row["conversation_id"],
                "owner_user_id": row["owner_user_id"],
                "owner_token_hash": row["owner_token_hash"],
                "status": row["status"],
                "result": _load(row["result_json"]),
                "memory_summary": _load(row["memory_summary_json"], {}),
                "attempt": row["attempt"],
                "error": row["error"],
                "created_at": row["created_at"],
                "started_at": row["started_at"],
                "finished_at": row["finished_at"],
                "steps": [_step_dict(step) for step in steps],
            }
        finally:
            conn.close()
