from __future__ import annotations

import asyncio
import json
import uuid
from collections import defaultdict
from datetime import UTC, datetime

from .events import EventBus
from .provider import LLMProvider, ModelResponse, ProviderError, ToolCall
from .storage import Store
from .tool_guardrails import ToolGuardrailController, append_guardrail_guidance, guardrail_synthetic_result
from .tool_registry import ToolRegistry
from .tools import ToolError


def now() -> str:
    return datetime.now(UTC).isoformat()


class AgentLoop:
    """FIFO agent loop backed by an injected real model provider."""
    def __init__(self, store: Store, events: EventBus, tools: ToolRegistry, provider: LLMProvider) -> None:
        self.store, self.events, self.tools, self.provider = store, events, tools, provider
        self.queues: dict[tuple[str, str], asyncio.Queue] = defaultdict(asyncio.Queue)
        self.active: dict[str, asyncio.Task] = {}
        self.worker_started: set[tuple[str, str]] = set()
        self.max_tool_rounds = 12

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
        messages: list[dict] = [
            {"role": "system", "content": "You are Agora, a helpful AI agent."},
            *[{"role": item["role"], "content": item["content"] or ""} for item in history],
        ]
        guardrails = ToolGuardrailController()
        response = await self._complete(messages, allow_tools=True)
        rounds = 0
        while response.tool_calls:
            if rounds >= self.max_tool_rounds or guardrails.halt_decision:
                messages.append({"role": "system", "content": "Stop calling tools. Use the available tool results to give a concise, honest final answer."})
                response = await self._complete(messages, allow_tools=False)
                break
            rounds += 1
            messages.extend(response.output_items)
            for call in response.tool_calls:
                tool_result = await self._execute_tool_call(run_id, agent_id, session_id, call, guardrails)
                messages.append({"type": "function_call_output", "call_id": call.id, "output": tool_result})
            if rounds >= self.max_tool_rounds or guardrails.halt_decision:
                messages.append({"role": "system", "content": "Stop calling tools. Use the available tool results to give a concise, honest final answer."})
                response = await self._complete(messages, allow_tools=False)
            else:
                response = await self._complete(messages, allow_tools=True)
        if not response.text:
            raise ProviderError("LLM provider returned no final output text")
        response_text = response.text
        for word in response_text.split(" "):
            await self._transient(agent_id, session_id, word + " ")
            await asyncio.sleep(0)
        await self.store.add_message(agent_id, session_id, "assistant", response_text, now())
        await self.events.publish(agent_id, session_id, "content", {"content": response_text}, now())
        await self.events.publish(agent_id, session_id, "done", {"runId": run_id, "state": "completed"}, now())
        await self.store.transact("UPDATE runs SET state='completed',finished_at=? WHERE id=?", (now(), run_id))

    async def _complete(self, messages: list[dict], *, allow_tools: bool) -> ModelResponse:
        complete_with_tools = getattr(self.provider, "complete_with_tools", None)
        if callable(complete_with_tools):
            return await complete_with_tools(messages, self.tools.definitions() if allow_tools else [])
        text = await self.provider.complete(messages)
        return ModelResponse(text=text)

    async def _execute_tool_call(
        self, run_id: str, agent_id: str, session_id: str, call: ToolCall, guardrails: ToolGuardrailController
    ) -> str:
        # Provider call IDs are useful for model correlation but are not
        # guaranteed to be unique across retries, so do not use them as the
        # durable execution primary key.
        execution_id = f"{run_id}:{uuid.uuid4().hex}"
        await self.store.start_tool_execution(execution_id, run_id, call.name, call.arguments, now())
        await self.events.publish(agent_id, session_id, "tool_call", {"id": call.id, "name": call.name, "arguments": call.arguments}, now())
        try:
            arguments = json.loads(call.arguments)
            if not isinstance(arguments, dict):
                raise ToolError("tool arguments must be an object")
        except (TypeError, json.JSONDecodeError, ToolError) as exc:
            result = json.dumps({"error": f"invalid tool arguments: {exc}"}, ensure_ascii=False)
            await self.store.finish_tool_execution(execution_id, result, "failed", now())
            await self.events.publish(agent_id, session_id, "tool_result", {"id": call.id, "name": call.name, "result": result, "failed": True}, now())
            return result

        decision = guardrails.before_call(call.name, arguments)
        if not decision.allows_execution:
            result = guardrail_synthetic_result(decision)
            await self.store.finish_tool_execution(execution_id, result, "blocked", now())
            await self.events.publish(agent_id, session_id, "tool_result", {"id": call.id, "name": call.name, "result": result, "failed": True, "guardrail": decision.to_metadata()}, now())
            return result

        try:
            raw_result = json.dumps(await self.tools.execute(call.name, arguments), ensure_ascii=False)
            failed = False
        except Exception as exc:
            raw_result = json.dumps({"error": str(exc)}, ensure_ascii=False)
            failed = True
        after = guardrails.after_call(call.name, arguments, raw_result, failed=failed)
        observed = guardrails.observe_call(call.name, arguments, raw_result, tool_call_id=call.id, failed=failed)
        result = observed.stub or raw_result
        if observed.notice:
            result = f"{result}\n\n{observed.notice}"
        result = append_guardrail_guidance(result, after)
        await self.store.finish_tool_execution(execution_id, result, "failed" if failed else "completed", now())
        await self.events.publish(agent_id, session_id, "tool_result", {"id": call.id, "name": call.name, "result": result, "failed": failed, "guardrail": after.to_metadata()}, now())
        return result

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
