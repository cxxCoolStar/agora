from __future__ import annotations

import asyncio
import uuid
from collections import defaultdict
from datetime import UTC, datetime

from .events import EventBus
from .provider import LLMProvider
from .storage import Store
from .tools import WorkspaceTools


def now() -> str:
    return datetime.now(UTC).isoformat()


class AgentLoop:
    """FIFO agent loop backed by an injected real model provider."""
    def __init__(self, store: Store, events: EventBus, tools: WorkspaceTools, provider: LLMProvider) -> None:
        self.store, self.events, self.tools, self.provider = store, events, tools, provider
        self.queues: dict[tuple[str, str], asyncio.Queue] = defaultdict(asyncio.Queue)
        self.active: dict[str, asyncio.Task] = {}
        self.worker_started: set[tuple[str, str]] = set()

    async def submit(self, agent_id: str, session_id: str, message: str) -> str:
        run_id = uuid.uuid4().hex
        await self.store.ensure_session(agent_id, session_id, now())
        await self.store.add_message(agent_id, session_id, "user", message, now())
        await self.store.transact("INSERT INTO runs(id,agent_id,session_id,state,created_at) VALUES(?,?,?,?,?)", (run_id, agent_id, session_id, "queued", now()))
        key = (agent_id, session_id)
        await self.queues[key].put((run_id, message))
        if key not in self.worker_started:
            self.worker_started.add(key)
            asyncio.create_task(self._worker(key))
        return run_id

    async def _worker(self, key: tuple[str, str]) -> None:
        queue = self.queues[key]
        while True:
            run_id, message = await queue.get()
            task = asyncio.current_task()
            self.active[run_id] = task  # type: ignore[assignment]
            try:
                await self._run(run_id, *key, message)
            except asyncio.CancelledError:
                await self.events.publish(*key, "error", {"message": "run cancelled"}, now())
                await self.events.publish(*key, "done", {"runId": run_id, "state": "cancelled"}, now())
                await self.store.transact("UPDATE runs SET state='cancelled',finished_at=? WHERE id=?", (now(), run_id))
            except Exception as exc:
                await self.events.publish(*key, "error", {"message": str(exc)}, now())
                await self.events.publish(*key, "done", {"runId": run_id, "state": "failed"}, now())
                await self.store.transact("UPDATE runs SET state='failed',finished_at=? WHERE id=?", (now(), run_id))
            finally:
                self.active.pop(run_id, None)
                queue.task_done()
            # Yield once before deciding to retire. This makes the worker handoff
            # atomic with submit() from the event loop's perspective.
            await asyncio.sleep(0)
            if queue.empty():
                self.worker_started.discard(key)
                if queue.empty():
                    return
                self.worker_started.add(key)

    async def _run(self, run_id: str, agent_id: str, session_id: str, message: str) -> None:
        await self.store.transact("UPDATE runs SET state='running' WHERE id=?", (run_id,))
        history = await self.store.history(agent_id, session_id)
        response = await self.provider.complete([
            {"role": "system", "content": "You are Agora, a helpful AI agent."},
            *[{"role": item["role"], "content": item["content"] or ""} for item in history],
        ])
        for word in response.split(" "):
            await self._transient(agent_id, session_id, word + " ")
            await asyncio.sleep(0)
        await self.store.add_message(agent_id, session_id, "assistant", response, now())
        await self.events.publish(agent_id, session_id, "content", {"content": response}, now())
        await self.events.publish(agent_id, session_id, "done", {"runId": run_id, "state": "completed"}, now())
        await self.store.transact("UPDATE runs SET state='completed',finished_at=? WHERE id=?", (now(), run_id))

    async def _transient(self, agent_id: str, session_id: str, delta: str) -> None:
        # Transient deltas are deliberately excluded from durable event history.
        async with self.events.lock:
            queues = list(self.events.subscribers[(agent_id, session_id)])
        event = {"type": "content_delta", "seq": -1, "data": {"delta": delta}}
        for queue in queues:
            queue.put_nowait(event)

    async def cancel(self, run_id: str) -> bool:
        task = self.active.get(run_id)
        if task is None:
            return False
        task.cancel()
        return True
