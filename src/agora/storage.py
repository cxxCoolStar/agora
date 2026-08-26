from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.lock = asyncio.Lock()
        self.connection.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE IF NOT EXISTS agents (id TEXT PRIMARY KEY, name TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT NOT NULL, agent_id TEXT NOT NULL, title TEXT, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, PRIMARY KEY(id, agent_id)
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id TEXT NOT NULL, session_id TEXT NOT NULL,
                role TEXT NOT NULL, content TEXT, metadata TEXT, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS session_events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, agent_id TEXT NOT NULL, session_id TEXT NOT NULL,
                type TEXT NOT NULL, data TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, session_id TEXT NOT NULL,
                state TEXT NOT NULL, created_at TEXT NOT NULL, finished_at TEXT
            );
            CREATE TABLE IF NOT EXISTS tool_executions (
                id TEXT PRIMARY KEY, run_id TEXT NOT NULL, name TEXT NOT NULL, arguments TEXT NOT NULL,
                result TEXT, state TEXT NOT NULL, created_at TEXT NOT NULL, finished_at TEXT
            );
            """
        )
        self.connection.execute("INSERT OR IGNORE INTO agents(id, name) VALUES ('default', 'Agora')")
        self.connection.commit()

    async def transact(self, sql: str, values: tuple[Any, ...] = ()) -> None:
        async with self.lock:
            self.connection.execute(sql, values)
            self.connection.commit()

    async def fetchall(self, sql: str, values: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        async with self.lock:
            return [dict(row) for row in self.connection.execute(sql, values).fetchall()]

    async def ensure_session(self, agent_id: str, session_id: str, now: str) -> None:
        await self.transact(
            "INSERT OR IGNORE INTO sessions(id,agent_id,title,created_at,updated_at) VALUES(?,?,?,?,?)",
            (session_id, agent_id, None, now, now),
        )

    async def add_message(self, agent_id: str, session_id: str, role: str, content: str, now: str, metadata: dict | None = None) -> None:
        await self.transact(
            "INSERT INTO messages(agent_id,session_id,role,content,metadata,created_at) VALUES(?,?,?,?,?,?)",
            (agent_id, session_id, role, content, json.dumps(metadata or {}), now),
        )
        await self.transact("UPDATE sessions SET updated_at=? WHERE id=? AND agent_id=?", (now, session_id, agent_id))

    async def add_event(self, agent_id: str, session_id: str, event_type: str, data: dict, now: str) -> dict:
        async with self.lock:
            cursor = self.connection.execute(
                "INSERT INTO session_events(agent_id,session_id,type,data,created_at) VALUES(?,?,?,?,?)",
                (agent_id, session_id, event_type, json.dumps(data), now),
            )
            self.connection.commit()
            return {"type": event_type, "seq": cursor.lastrowid, "data": data}

    async def start_tool_execution(self, execution_id: str, run_id: str, name: str, arguments: str, now: str) -> None:
        await self.transact(
            "INSERT INTO tool_executions(id,run_id,name,arguments,state,created_at) VALUES(?,?,?,?,?,?)",
            (execution_id, run_id, name, arguments, "running", now),
        )

    async def finish_tool_execution(self, execution_id: str, result: str, state: str, now: str) -> None:
        await self.transact(
            "UPDATE tool_executions SET result=?,state=?,finished_at=? WHERE id=?",
            (result, state, now, execution_id),
        )

    async def events_since(self, agent_id: str, session_id: str, since: int) -> list[dict]:
        rows = await self.fetchall(
            "SELECT seq,type,data FROM session_events WHERE agent_id=? AND session_id=? AND seq>? ORDER BY seq",
            (agent_id, session_id, since),
        )
        return [{"type": row["type"], "seq": row["seq"], "data": json.loads(row["data"])} for row in rows]

    async def history(self, agent_id: str, session_id: str) -> list[dict]:
        rows = await self.fetchall(
            "SELECT role,content,metadata FROM messages WHERE agent_id=? AND session_id=? ORDER BY id",
            (agent_id, session_id),
        )
        return [{"role": row["role"], "content": row["content"], "metadata": json.loads(row["metadata"] or "{}")} for row in rows]
