# -*- coding: utf-8 -*-
"""FastAPI 入口：提供 Agent 流式运行接口（SSE）。

启动：cd backend && uvicorn app.main:app --reload --port 8000
"""
from __future__ import annotations

import json

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from . import agent, memory, tools

load_dotenv()

app = FastAPI(title="客户经理 AI 作战台 - Agent API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class RunRequest(BaseModel):
    text: str
    company_items: list[dict] | None = None


class PushRequest(BaseModel):
    channel: str = "钉钉"
    webhook: str = ""
    title: str = ""
    detail: str = ""


class CasePayload(BaseModel):
    id: str | None = None
    customerName: str = ""
    type: str = "待确认"
    product: str = "待确认"
    concern: str = "待确认"
    breakthrough: str = "待确认"
    result: str = "待确认"
    purpose: str = "待确认"
    actions: list | None = None
    process: str = ""
    source: str = "手动录入"
    createdAt: str | None = None
    updatedAt: str | None = None


@app.get("/api/health")
def health() -> dict:
    from . import llm
    return {"status": "ok", "llm_mode": "deepseek" if llm.available() else "rule"}


@app.post("/api/agent/run")
def agent_run(payload: RunRequest):
    def event_stream():
        for event in agent.run_agent(payload.text, company_items=payload.company_items):
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        yield "event: done\ndata: {}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/cases")
def list_cases():
    cases = memory.load_cases()
    return {"cases": cases, "count": len(cases)}


@app.post("/api/cases")
def create_case(payload: CasePayload):
    import datetime
    import uuid
    data = payload.model_dump()
    data["id"] = payload.id or f"SC-{uuid.uuid4().hex[:10]}"
    data["createdAt"] = payload.createdAt or payload.updatedAt or datetime.datetime.now().isoformat()
    memory.add_case(data)
    return {"ok": True, "case": data}


@app.post("/api/push")
def api_push(payload: PushRequest):
    """服务端转发消息到钉钉/飞书机器人（绕开浏览器 CORS 限制）。"""
    return tools.push_message(payload.channel, payload.webhook, payload.title, payload.detail)


@app.delete("/api/cases/{case_id}")
def delete_case(case_id: str):
    before = memory.load_cases()
    memory.save_cases([c for c in before if c.get("id") != case_id])
    return {"ok": True, "deleted": len(before) - len(memory.load_cases())}