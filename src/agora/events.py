from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import AsyncIterator

from .storage import Store


class EventBus:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.subscribers: dict[tuple[str, str], set[asyncio.Queue]] = defaultdict(set)
        self.lock = asyncio.Lock()

    async def publish(self, agent_id: str, session_id: str, event_type: str, data: dict, now: str) -> dict:
        event = await self.store.add_event(agent_id, session_id, event_type, data, now)
        async with self.lock:
            queues = list(self.subscribers[(agent_id, session_id)])
        for queue in queues:
            queue.put_nowait(event)
        return event

    async def stream(self, agent_id: str, session_id: str, since: int) -> AsyncIterator[dict]:
        # Registration precedes replay so events created during replay remain queued.
        queue: asyncio.Queue = asyncio.Queue()
        key = (agent_id, session_id)
        async with self.lock:
            self.subscribers[key].add(queue)
        try:
            for event in await self.store.events_since(agent_id, session_id, since):
                yield event
            while True:
                yield await queue.get()
        finally:
            async with self.lock:
                self.subscribers[key].discard(queue)
