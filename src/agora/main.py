from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .events import EventBus
from .config import DEFAULT_MODEL
from .runtime import AgentLoop, now
from .storage import Store
from .tools import ToolError, WorkspaceTools


class ChatRequest(BaseModel):
    agentId: str = "default"
    sessionId: str
    message: str = Field(min_length=1, max_length=100_000)


class SteerRequest(ChatRequest):
    pass


class FrontendFiles(StaticFiles):
    """Serve exported Next assets while preserving client-side deep links."""
    async def get_response(self, path: str, scope):
        try:
            response = await super().get_response(path, scope)
        except OSError:
            # Windows rejects wildcard characters such as `**` in os.stat;
            # treat malformed/non-file SPA paths as client-side routes.
            response = None
        if response is None:
            return FileResponse(Path(self.directory) / "index.html")
        if response.status_code != 404:
            return response
        return FileResponse(Path(self.directory) / "index.html")


def create_app(data_dir: Path | None = None, workspace: Path | None = None) -> FastAPI:
    data_dir = data_dir or Path(os.getenv("AGORA_DATA_DIR", ".agora"))
    workspace = workspace or Path(os.getenv("AGORA_WORKSPACE", "."))
    store = Store(data_dir / "agora.sqlite3")
    bus = EventBus(store)
    loop = AgentLoop(store, bus, WorkspaceTools(workspace))

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        store.connection.close()

    app = FastAPI(title="Agora", lifespan=lifespan)
    app.state.store, app.state.bus, app.state.loop = store, bus, loop

    @app.get("/api/status")
    async def status():
        return {
            "configured": True,
            "running": True,
            "port": 8000,
            "mode": "development",
            "version": "0.1.0",
            "uptime": "0s",
            "agents": [{"id": "default", "name": "Agora", "model": DEFAULT_MODEL}],
            "channels": [],
            "provider": {"configured": False},
            "capabilities": {
                "channels": False, "plugins": False, "projectRuntime": False,
                "multiPod": False, "dockerSandbox": False, "mcp": False, "cron": False,
            },
        }

    @app.get("/api/me")
    async def me():
        # Development bootstrap. Authentication becomes a port backed by the
        # tenant identity provider before any non-local deployment.
        return {"ok": True, "authMethod": "development", "user": {"id": "local", "username": "local", "email": "local@agora", "role": "super_admin", "displayName": "Local user", "status": "active", "agentQuota": -1}}

    @app.get("/api/agents")
    async def agents():
        rows = await store.fetchall("SELECT id,name FROM agents ORDER BY id")
        return {"agents": [{"id": row["id"], "name": row["name"], "model": DEFAULT_MODEL, "role": "owner"} for row in rows]}

    @app.get("/api/agents/{agent_id}")
    async def agent(agent_id: str):
        rows = await store.fetchall("SELECT id,name FROM agents WHERE id=?", (agent_id,))
        if not rows:
            raise HTTPException(404, "agent not found")
        row = rows[0]
        return {"agent": {"id": row["id"], "name": row["name"], "model": DEFAULT_MODEL, "role": "owner"}}

    @app.get("/api/chat/history")
    async def history(agentId: str, sessionId: str):
        events = await store.events_since(agentId, sessionId, -1)
        return {"history": await store.history(agentId, sessionId), "latestEventSeq": events[-1]["seq"] if events else -1}

    @app.get("/api/chat/sessions")
    async def sessions(agentId: str):
        rows = await store.fetchall("SELECT id,title,updated_at FROM sessions WHERE agent_id=? ORDER BY updated_at DESC", (agentId,))
        return {"sessions": [{"id": item["id"], "title": item["title"], "updatedAt": item["updated_at"]} for item in rows]}

    @app.post("/api/chat/stream")
    async def chat_stream(body: ChatRequest):
        prior_events = await store.events_since(body.agentId, body.sessionId, -1)
        cursor = prior_events[-1]["seq"] if prior_events else -1
        run_id = await loop.submit(body.agentId, body.sessionId, body.message)
        async def generate():
            async for event in bus.stream(body.agentId, body.sessionId, cursor):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                if event["type"] == "done" and event["data"].get("runId") == run_id:
                    break
        return StreamingResponse(generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/chat/subscribe")
    async def subscribe(agentId: str, sessionId: str, since: int = Query(-1)):
        async def generate():
            async for event in bus.stream(agentId, sessionId, since):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        return StreamingResponse(generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/chat/steer")
    async def steer(body: SteerRequest):
        # M0 does not yet feed steering into a provider mid-turn.
        return {"buffered": False}

    @app.delete("/api/runs/{run_id}")
    async def cancel(run_id: str):
        if not await loop.cancel(run_id):
            raise HTTPException(404, "run is not active")
        return {"ok": True}

    @app.get("/api/tools/list-dir")
    async def list_dir(path: str = ".", offset: int = 0, limit: int = 100):
        try:
            return loop.tools.list_dir(path, offset, limit)
        except ToolError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/tools/read-file")
    async def read_file(path: str, offset: int = 1, limit: int = 500):
        try:
            return loop.tools.read_file(path, offset, limit)
        except ToolError as exc:
            raise HTTPException(400, str(exc)) from exc

    frontend = Path(__file__).resolve().parents[2] / "frontend" / "out"
    if frontend.is_dir():
        # Mount last so API endpoints always win over the SPA fallback.
        app.mount("/", FrontendFiles(directory=frontend, html=True), name="frontend")

    return app


app = create_app()
