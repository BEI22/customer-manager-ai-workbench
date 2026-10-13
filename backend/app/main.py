# -*- coding: utf-8 -*-
"""FastAPI 入口：提供 Agent 流式运行接口（SSE）。

启动：cd backend && uvicorn app.main:app --reload --port 8000
"""
from __future__ import annotations

import json
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from . import agent, coach, customers, llm, memory, tools, visits

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


class AuditRequest(BaseModel):
    caseId: str = ""
    customer: str = ""
    riskLevel: str = ""
    decision: str = ""  # confirm / modify / reject
    note: str = ""
    reviewer: str = ""


class JobAssistantRequest(BaseModel):
    mode: str = "reply"  # reply / greeting / self_intro
    message: str = ""
    profile: dict = {}


class CoachStartRequest(BaseModel):
    text: str = ""
    profile: dict = {}
    difficulty: str = "实战"
    case_id: str = ""


class CoachReplyRequest(BaseModel):
    session_id: str = ""
    user_talk: str = ""
    want_hint: bool = False


class CoachHarvestRequest(BaseModel):
    session_id: str = ""


class VisitBatchRequest(BaseModel):
    records: list | None = None
    mode: str = "merge"  # merge / replace


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


def _job_profile_text(profile: dict) -> str:
    keys = ["姓名", "电话", "求职意向", "期望薪资", "期望城市", "技能", "项目经历", "教育", "优势"]
    lines = []
    for k in keys:
        v = profile.get(k)
        if v:
            lines.append(f"{k}：{v}")
    return "；".join(lines) or "（未填写档案）"


def _job_rule_fallback(mode: str, message: str, profile: dict) -> str:
    text = message or ""
    name = profile.get("姓名") or "我"
    skills = profile.get("技能") or ""
    intent = profile.get("求职意向") or "AI 智能体开发"
    matched = [s for s in skills.split("，") if s and s in text] if skills else []
    matched_text = "、".join(matched[:4]) if matched else "AI Agent / RAG / Python"
    if mode == "greeting":
        return f"您好，我对您发布的{intent}岗位很感兴趣。我做过 AI 智能体与 RAG 项目（{matched_text}），其中一个已部署上线可在线查看。方便进一步沟通吗？"
    if mode == "self_intro":
        return f"您好，我是{name}，求职{intent}。2 个月从零完成 AI 智能体与 RAG 项目并部署上线，熟练 LangGraph / DeepSeek / FastAPI。期待有机会和您交流。"
    return f"您好，感谢回复。我这边的情况和岗位比较匹配（{matched_text}），随时可以约面试或补充作品细节，期待您的进一步安排。"


AUDIT_FILE = Path(__file__).resolve().parent.parent / "data" / "audits.jsonl"


@app.post("/api/audit")
def create_audit(payload: AuditRequest):
    """记录人工审核结果（高风险业务的确认 / 修改 / 驳回），用于审计追溯。"""
    import datetime
    rec = {**payload.model_dump(), "time": datetime.datetime.now().isoformat(timespec="seconds")}
    AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(AUDIT_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return {"ok": True, "record": rec}


@app.get("/api/audits")
def list_audits(limit: int = 20):
    if not AUDIT_FILE.exists():
        return {"records": [], "count": 0}
    lines = AUDIT_FILE.read_text(encoding="utf-8").splitlines()[-limit:]
    records = []
    for line in lines:
        try:
            records.append(json.loads(line))
        except Exception:
            continue
    return {"records": records, "count": len(records)}


@app.post("/api/job-assistant")
def job_assistant(payload: JobAssistantRequest):
    profile_text = _job_profile_text(payload.profile)
    mode = payload.mode
    mode_label = {"reply": "回复招聘方消息", "greeting": "给招聘方打招呼", "self_intro": "自我介绍"}.get(mode, "回复")
    if not llm.available():
        return {"reply": _job_rule_fallback(mode, payload.message, payload.profile), "mode": "rule"}
    try:
        model = llm.get_json_model(temperature=0.6)
        system = "你是求职助手。请基于求职者档案，帮其生成发给招聘方的专业、自然、真诚的中文消息。突出与岗位的匹配点，不编造经历，语气得体不浮夸。"
        user = (
            f"求职者档案：{profile_text}\n"
            f"任务：{mode_label}\n"
            f"招聘方消息或岗位描述：{payload.message or '（无）'}\n"
            f"请输出 JSON：{{ \"reply\": \"主回复（150-250字，可直接发送）\", \"alternatives\": [\"2 个简短备选开头\"] }}"
        )
        resp = model.invoke([{"role": "system", "content": system}, {"role": "user", "content": user}])
        import json
        data = json.loads(resp.content or "{}")
        return {"reply": data.get("reply") or _job_rule_fallback(mode, payload.message, payload.profile), "alternatives": data.get("alternatives", []), "mode": "llm"}
    except Exception:
        return {"reply": _job_rule_fallback(mode, payload.message, payload.profile), "mode": "rule"}


@app.get("/api/customers")
def list_customers():
    """客户台账列表。"""
    customers_list = customers.load_customers()
    return {"customers": customers_list, "count": len(customers_list), "mode": "real" if customers_list else "empty"}


@app.post("/api/customers/import")
async def import_customers_file(file: UploadFile = File(...), mode: str = Form("replace")):
    """Excel/CSV 导入客户台账（replace=替换演示数据 / append=去重追加）。"""
    data = await file.read()
    if len(data) > 8 * 1024 * 1024:
        return {"ok": False, "reason": "big", "message": "文件超过 8MB，请精简后重试。"}
    try:
        return customers.import_customers(file.filename or "", data, mode=mode)
    except ValueError as exc:
        return {"ok": False, "reason": "format", "message": str(exc)}
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        return {"ok": False, "reason": "parse", "message": f"解析失败：{exc}。请确认文件首行是表头（客户名称/区域/客户类型…）。"}


@app.delete("/api/customers")
def clear_customers():
    """清空台账（前端将回到演示数据模式）。"""
    return customers.clear_customers()


@app.get("/api/visits")
def list_visits():
    """服务端拜访记录（P0-2 上移后端）。"""
    items = visits.load_visits()
    return {"visits": items, "count": len(items)}


@app.post("/api/visits/merge")
def merge_visits(payload: VisitBatchRequest):
    """合并前端本地拜访记录到服务端。"""
    return visits.merge_visits(payload.records or [])


@app.delete("/api/visits/{rid}")
def delete_visit(rid: str):
    return visits.delete_visit(rid)


@app.delete("/api/visits")
def clear_visits():
    return visits.clear_visits()


@app.get("/api/stats")
def api_stats():
    """首页/看板动态指标。"""
    customers_list = customers.load_customers()
    visits_list = visits.load_visits()
    by_region = customers.region_stats(customers_list)
    region_rows = sorted(by_region.items(), key=lambda kv: kv[1]["total"], reverse=True)
    return {
        "customers": len(customers_list),
        "visits": len(visits_list),
        "customer_mode": "real" if customers_list else "empty",
        "regions": region_rows[:18],
        "products": sorted({p for c in customers_list for p in (c.get("products") or []) if p})[:12],
    }


@app.post("/api/coach/start")
def coach_start(payload: CoachStartRequest):
    """话术演练：根据拜访资料/画像检索最相似成功案例，创建客户陪练会话。"""
    return coach.start_session(text=payload.text, profile=payload.profile or {},
                               difficulty=payload.difficulty, case_id=payload.case_id)


@app.post("/api/coach/reply")
def coach_reply(payload: CoachReplyRequest):
    """话术演练：提交客户经理台词，返回客户回应 + 教练点评 + 参考话术。"""
    return coach.reply(payload.session_id, payload.user_talk, want_hint=payload.want_hint)


@app.post("/api/coach/harvest")
def coach_harvest(payload: CoachHarvestRequest):
    """话术演练：把已成交的演练会话生成成功案例草稿（确认后由 /api/cases 入库）。"""
    return coach.harvest(payload.session_id)


@app.post("/api/push")
def api_push(payload: PushRequest):
    """服务端转发消息到钉钉/飞书机器人（绕开浏览器 CORS 限制）。"""
    return tools.push_message(payload.channel, payload.webhook, payload.title, payload.detail)


@app.delete("/api/cases/{case_id}")
def delete_case(case_id: str):
    before = memory.load_cases()
    memory.save_cases([c for c in before if c.get("id") != case_id])
    return {"ok": True, "deleted": len(before) - len(memory.load_cases())}