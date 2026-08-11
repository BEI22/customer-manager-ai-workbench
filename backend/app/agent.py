# -*- coding: utf-8 -*-
"""LangGraph 智能体：解析 → 规划（工具调用）→ 执行 → 综合生成。

- 配置 DEEPSEEK_API_KEY 时走 LangGraph + DeepSeek（真实模型，function calling）；
- 未配置时使用规则模式兜底（流程相同，零成本可演示）。
"""
from __future__ import annotations

import json
import operator
import re
from typing import Annotated, Any, Iterator, TypedDict

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.graph import END, START, StateGraph

from . import llm as llm_mod
from . import tools

AGENT_SYSTEM = """你是“牧原客户经理AI助手”，帮助生鲜客户经理完成客户拜访纪要、账期风控、竞品比价和销售话术的智能体。

工作方式：
1. 你会先调用工具获取事实与依据（账期风险、竞品比价、相似成功案例）。
2. 最后综合所有工具结果，输出一份结构化 JSON 报告。

工具：
- assess_credit_risk_tool：评估客户账期拖欠风险等级与管控动作。
- compare_prices_tool：解析竞品报价并与牧原报价比价，计算含账期资金成本的综合价差。
- search_success_cases_tool：检索与当前客户最相似的已开发成功客户案例，用于复用开发思路和话术。
- push_todo_message_tool：把高优先级待办推送到钉钉/飞书群机器人（按需）。

输出要求：
- 所有结论必须基于工具返回的事实，不得编造客户没说过的话。
- 无法确认的信息标注“待确认”。
- 风险提醒必须包含：账期、授信和价格承诺以公司授权口径为准。
"""

SYNTH_PROMPT = """请综合以上所有工具结果，输出最终结构化报告（JSON），字段：
summary, purpose, customerProfile, creditRisk, benchmark, similarCases, talk, actions, riskReminders

- customerProfile 为 {type, concern}
- benchmark 直接引用 compare_prices_tool 返回的 rows/count
- similarCases 直接引用 search_success_cases_tool 返回的 hits/count
- actions 为 [{title, deadline}]
- talk 给出一段可直接使用的销售话术，融入相似案例打法和账期说明
- 不得编造工具未返回的事实，无法确认的标注“待确认”
"""


# ---------- LangChain 工具 ----------
@tool
def assess_credit_risk_tool(text: str) -> str:
    """评估客户账期拖欠风险等级（低/中/高/极高）与管控动作。"""
    return json.dumps(tools.assess_credit_risk(text), ensure_ascii=False)


@tool
def compare_prices_tool(competitor_text: str, muyuan_items: list | None = None, muyuan_days: int = 0) -> str:
    """解析竞品报价并与牧原报价比价，计算含账期资金成本的综合价差。"""
    return json.dumps(tools.compare_prices(
        muyuan_items=muyuan_items or [],
        competitor_text=competitor_text,
        muyuan_days=int(muyuan_days or 0),
    ), ensure_ascii=False)


@tool
def search_success_cases_tool(query: str, top_n: int = 3) -> str:
    """检索与目标客户最相似的已开发成功客户案例，用于复用开发思路和话术。"""
    return json.dumps(tools.search_success_cases(query, top_n=int(top_n or 3)), ensure_ascii=False)


@tool
def push_todo_message_tool(channel: str, webhook: str, title: str, detail: str = "") -> str:
    """把高优先级待办推送到钉钉或飞书群机器人。"""
    return json.dumps(tools.push_message(channel, webhook, title, detail), ensure_ascii=False)


AGENT_TOOLS = [assess_credit_risk_tool, compare_prices_tool, search_success_cases_tool, push_todo_message_tool]


# ---------- 基础信息抽取（规则，快速且零成本） ----------
def extract_facts(text: str) -> dict[str, Any]:
    s = text or ""
    customer = "待确认"
    m = re.search(r"([\u4e00-\u9fa5])(老板|总|经理)", s)
    if m:
        customer = m.group(1) + m.group(2)
    if customer == "待确认":
        m = re.search(r"(?:拜访|回访|联系|跟进)([\u4e00-\u9fa5]{2,6}客户)", s)
        if m:
            customer = m.group(1)
    if customer == "待确认":
        m = re.search(r"([\u4e00-\u9fa5]{2,6}客户)", s)
        if m and not re.search(r"拜访|回访|批发|商超|餐饮|团餐|农贸|档口|开发|潜|新客|目标", m.group(1)):
            customer = m.group(1)
    purpose = "待确认"
    if re.search(r"投诉|质量|客诉", s):
        purpose = "质量投诉处理"
    elif re.search(r"复购|继续合作|加量|补货|走货好|再下单", s):
        purpose = "老客复购"
    elif re.search(r"报价|价格|比价|竞品|账期", s):
        purpose = "报价沟通"
    elif re.search(r"回访|售后|处理结果", s):
        purpose = "售后回访"
    elif re.search(r"首次|新客|开发", s):
        purpose = "新客开发"
    profile_type = "客户类型待确认"
    for t, pat in [("批发商", r"批发"), ("餐饮连锁", r"餐饮|快餐"), ("商超", r"商超|超市"), ("团餐", r"团餐"), ("农贸档口", r"农贸|档口")]:
        if re.search(pat, s):
            profile_type = t
            break
    concern = "需求待确认"
    for c, pat in [("配送时效", r"配送|到货|时效"), ("品质标准", r"品质|质量|标准|出成率"), ("价格敏感", r"价格|便宜|压价"), ("售后响应", r"售后|服务"), ("稳定供应", r"稳定|供应")]:
        if re.search(pat, s):
            concern = c
            break
    return {
        "customer": customer,
        "purpose": purpose,
        "product": tools.infer_product(s),
        "profile": {"type": profile_type, "concern": concern},
        "payMethod": tools.infer_pay_method(s),
        "termDays": tools.infer_term_days(s),
    }


# ---------- 规则模式（未配置 Key 时兜底） ----------
def default_tool_calls(text: str, facts: dict) -> list[dict]:
    calls = []
    idx = 1
    for name, args in [
        ("assess_credit_risk", {"text": text}),
        ("search_success_cases", {"query": f"{facts['profile']['type']} {facts['product']} {facts['profile']['concern']} {text}", "top_n": 3}),
        ("compare_prices", {"competitor_text": text, "muyuan_items": [], "muyuan_days": facts["termDays"] or 0}),
    ]:
        calls.append({"id": f"call_mock_{idx}", "type": "function", "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}})
        idx += 1
    return calls


def dispatch_tool(name: str, args: dict, text: str, context: dict | None = None) -> dict[str, Any]:
    ctx = context or {}
    if name == "assess_credit_risk":
        return tools.assess_credit_risk(args.get("text") or text)
    if name == "compare_prices":
        muyuan_items = args.get("muyuan_items") or ctx.get("muyuan_items") or []
        return tools.compare_prices(
            muyuan_items=muyuan_items,
            competitor_text=args.get("competitor_text") or text,
            muyuan_days=int(args.get("muyuan_days") or 0),
        )
    if name == "search_success_cases":
        return tools.search_success_cases(args.get("query") or text, top_n=int(args.get("top_n") or 3))
    if name == "push_todo_message":
        return tools.push_message(args.get("channel", ""), args.get("webhook", "") or (ctx.get("push") or {}).get("webhook", ""), args.get("title", ""), args.get("detail", ""))
    return {"error": f"未知工具 {name}"}


def tool_summary(name: str, result: dict) -> str:
    n = name.replace("_tool", "")
    if n == "assess_credit_risk":
        return f"风险等级：{result.get('level', '待确认')}（{result.get('score', 0)} 分），触发因子 {len(result.get('factors', []))} 个"
    if n == "compare_prices":
        return f"解析竞品报价 {result.get('count', 0)} 条"
    if n == "search_success_cases":
        return f"检索到相似成功案例 {result.get('count', 0)} 条"
    if n == "push_todo_message":
        return "已推送" if result.get("ok") else "未推送"
    return "完成"


def _actions(text: str, purpose: str) -> list[dict]:
    clauses = [c.strip() for c in re.split(r"[\n。；;，,]", text or "") if c.strip()]
    candidates = []
    for clause in clauses:
        if re.search(r"下次|再约|拜访|回访|发送|报价单|处理|回复|反馈|确认|核实|跟进|联系|回款|欠款|逾期|催收|账期", clause):
            candidates.append(clause[:60])
    if not candidates:
        fallback = {
            "新客开发": "确认客户主销品类、日采购量和下一次拜访时间",
            "报价沟通": "发送公司报价并确认客户反馈截止时间",
            "质量投诉处理": "核实投诉证据、批次和处理时限",
            "老客复购": "确认复购数量、到货时间和补货安排",
            "售后回访": "确认售后处理结果并约定二次回访",
        }
        candidates.append(fallback.get(purpose, "补齐客户信息并安排跟进"))
    return [{"title": c, "deadline": "时间待确认"} for c in list(dict.fromkeys(candidates))[:5]]


def _tool_result(results: dict, *names):
    for n in names:
        if results.get(n):
            return results[n]
    return None


def mock_report(text: str, facts: dict, results: dict) -> dict[str, Any]:
    risk = _tool_result(results, "assess_credit_risk", "assess_credit_risk_tool") or tools.assess_credit_risk(text)
    cases = _tool_result(results, "search_success_cases", "search_success_cases_tool") or {"hits": [], "count": 0}
    benchmark = _tool_result(results, "compare_prices", "compare_prices_tool") or {"rows": [], "count": 0}
    actions = _actions(text, facts["purpose"])
    top = (cases.get("hits") or [{}])[0]
    talk = f"我今天主要负责{facts['profile']['type']}客户的走访。我先确认主销品类、日采购量和当前供应商；"
    if risk.get("level") in ("高", "极高"):
        talk += f"同时要跟进账期/回款问题：{risk['action'][0]}。"
    elif facts["payMethod"] != "待确认":
        talk += f"账期方面按{facts['payMethod']}口径沟通，涉及授信承诺以上报主管为准。"
    if top:
        talk += f"我们之前服务过和您情况类似的客户（{top.get('customerName', '同类客户')}），用“{top.get('breakthrough', '小批量试单')}”切入，先做小批量验证再谈后续合作。"
    talk += "关键价格和账期承诺以公司授权口径为准。"
    risk_reminders = ["客户口头反馈的竞品价格不可直接视为已确认价格。", "无法确认的信息统一标注待确认。"]
    if risk.get("level") in ("高", "极高"):
        risk_reminders.insert(0, "账期拖欠风险较高，暂停新增赊销额度，本周确认回款计划并上报主管。")
    return {
        "summary": f"本次拜访目的为{facts['purpose']}，客户 {facts['customer']}，主营{facts['product']}，关注{facts['profile']['concern']}。"
                   f"账期：{facts['payMethod']}（{facts['termDays'] or '待确认'}天）。"
                   + (f"检测到{len(risk.get('factors', []))}个账期风险信号。" if risk.get("factors") else "未检测到明显账期风险信号。"),
        "purpose": facts["purpose"],
        "customerProfile": facts["profile"],
        "creditRisk": risk,
        "benchmark": benchmark,
        "similarCases": cases,
        "talk": talk,
        "actions": actions,
        "riskReminders": risk_reminders,
        "mode": "rule",
    }


# ---------- LangGraph ----------
class AgentState(TypedDict):
    text: str
    muyuan_items: list
    facts: dict
    messages: Annotated[list, operator.add]
    tool_results: dict
    tool_events: Annotated[list, operator.add]
    report: dict


def parse_node(state: AgentState) -> dict:
    return {"facts": extract_facts(state["text"])}


def plan_node(state: AgentState) -> dict:
    model = llm_mod.get_chat_model().bind_tools(AGENT_TOOLS)
    msgs = state["messages"]
    resp = model.invoke(msgs)
    return {"messages": [resp]}


def execute_node(state: AgentState) -> dict:
    last = state["messages"][-1]
    tool_calls = getattr(last, "tool_calls", None) or []
    results = dict(state.get("tool_results") or {})
    events = []
    tool_msgs = []
    for tc in tool_calls:
        name = tc["name"]
        args = tc.get("args") or {}
        if name == "compare_prices_tool" and not args.get("muyuan_items") and state.get("muyuan_items"):
            args = {**args, "muyuan_items": state["muyuan_items"]}
        lc_tool = next((t for t in AGENT_TOOLS if t.name == name), None)
        try:
            result_str = lc_tool.invoke(args) if lc_tool else "{}"
            result = json.loads(result_str)
        except Exception as exc:  # noqa: BLE001
            result = {"error": f"工具执行失败：{exc}"}
            result_str = json.dumps(result, ensure_ascii=False)
        results[name] = result
        events.append({"name": name, "args": args, "summary": tool_summary(name, result), "result": result})
        tool_msgs.append(ToolMessage(content=result_str, tool_call_id=tc["id"]))
    return {"messages": tool_msgs, "tool_results": results, "tool_events": events}


def normalize_report(report: dict) -> dict:
    """统一 LLM 输出字段，兼容不同命名（action/actions、concern 数组等）。"""
    cr = report.get("creditRisk") or {}
    if cr.get("actions") and "action" not in cr:
        cr["action"] = cr["actions"]
    report["creditRisk"] = cr
    prof = report.get("customerProfile") or {}
    if isinstance(prof.get("concern"), list):
        prof["concern"] = "、".join(prof["concern"])
    report["customerProfile"] = prof
    return report


def should_continue(state: AgentState) -> str:
    last = state["messages"][-1]
    return "execute" if getattr(last, "tool_calls", None) else "synthesize"


def synthesize_node(state: AgentState) -> dict:
    try:
        model = llm_mod.get_json_model()
        msgs = state["messages"] + [HumanMessage(content=SYNTH_PROMPT)]
        resp = model.invoke(msgs)
        content = resp.content or ""
        report = json.loads(content)
        return {"report": normalize_report(report)}
    except Exception:  # noqa: BLE001
        report = mock_report(state["text"], state.get("facts") or extract_facts(state["text"]), state.get("tool_results") or {})
        report["mode"] = "rule"
        return {"report": report}


def build_graph():
    g = StateGraph(AgentState)
    g.add_node("parse", parse_node)
    g.add_node("plan", plan_node)
    g.add_node("execute", execute_node)
    g.add_node("synthesize", synthesize_node)
    g.add_edge(START, "parse")
    g.add_edge("parse", "plan")
    g.add_conditional_edges("plan", should_continue, {"execute": "execute", "synthesize": "synthesize"})
    g.add_edge("execute", "plan")
    g.add_edge("synthesize", END)
    return g.compile()


GRAPH = build_graph()


# ---------- Agent 主入口 ----------
def _rule_mode(text: str, muyuan_items: list | None, push: dict | None) -> Iterator[dict]:
    facts = extract_facts(text)
    yield {"type": "thinking", "step": f"解析拜访资料：客户 {facts['customer']}｜目的 {facts['purpose']}｜产品 {facts['product']}｜账期 {facts['payMethod']}"}
    yield {"type": "thinking", "step": "未配置 DEEPSEEK_API_KEY，使用规则模式执行工具（在 backend/.env 配置 Key 后自动切换真实模型）"}
    results: dict[str, Any] = {}
    for call in default_tool_calls(text, facts):
        name = call["function"]["name"]
        try:
            args = json.loads(call["function"]["arguments"] or "{}")
        except Exception:  # noqa: BLE001
            args = {}
        yield {"type": "tool_start", "name": name, "args": args}
        result = dispatch_tool(name, args, text, {"muyuan_items": muyuan_items, "push": push})
        results[name] = result
        yield {"type": "tool_end", "name": name, "summary": tool_summary(name, result), "result": result}
    yield {"type": "result", "data": mock_report(text, facts, results)}


def _fallback(text: str, muyuan_items: list | None, push: dict | None, reason: str) -> Iterator[dict]:
    facts = extract_facts(text)
    yield {"type": "thinking", "step": reason}
    results: dict[str, Any] = {}
    for call in default_tool_calls(text, facts):
        name = call["function"]["name"]
        try:
            args = json.loads(call["function"]["arguments"] or "{}")
        except Exception:  # noqa: BLE001
            args = {}
        results[name] = dispatch_tool(name, args, text, {"muyuan_items": muyuan_items, "push": push})
    yield {"type": "result", "data": mock_report(text, facts, results)}


def run_agent(text: str, muyuan_items: list[dict] | None = None, push: dict | None = None) -> Iterator[dict[str, Any]]:
    text = (text or "").strip()
    if not text:
        yield {"type": "error", "message": "请先输入拜访资料"}
        return

    if not llm_mod.available():
        yield from _rule_mode(text, muyuan_items, push)
        return

    try:
        inputs: dict[str, Any] = {
            "text": text,
            "muyuan_items": muyuan_items or [],
            "facts": {},
            "messages": [
                SystemMessage(content=AGENT_SYSTEM),
                HumanMessage(content=f"客户拜访资料：\n{text}\n\n请规划需要调用哪些工具，再依次调用。最后会要求你综合结果生成报告。"),
            ],
            "tool_results": {},
            "tool_events": [],
            "report": None,
        }
        emitted_result = False
        for chunk in GRAPH.stream(inputs, stream_mode="updates"):
            for node_name, upd in chunk.items():
                if node_name == "parse":
                    f = upd.get("facts", {})
                    yield {"type": "thinking", "step": f"解析拜访资料：客户 {f.get('customer', '待确认')}｜目的 {f.get('purpose', '待确认')}｜产品 {f.get('product', '待确认')}｜账期 {f.get('payMethod', '待确认')}"}
                elif node_name == "plan":
                    msgs = upd.get("messages", [])
                    last = msgs[-1] if msgs else None
                    calls = getattr(last, "tool_calls", None) if last else None
                    if calls:
                        names = [c["name"] for c in calls]
                        yield {"type": "thinking", "step": f"DeepSeek 规划：调用 {', '.join(names)}"}
                    else:
                        yield {"type": "thinking", "step": "DeepSeek 直接回答（未调用工具）"}
                elif node_name == "execute":
                    for ev in upd.get("tool_events", []):
                        yield {"type": "tool_start", "name": ev["name"], "args": ev["args"]}
                        yield {"type": "tool_end", "name": ev["name"], "summary": ev["summary"], "result": ev["result"]}
                elif node_name == "synthesize":
                    report = upd.get("report")
                    if report:
                        report.setdefault("mode", "llm")
                        yield {"type": "thinking", "step": "DeepSeek 已综合工具结果生成最终报告"}
                        yield {"type": "result", "data": report}
                        emitted_result = True
        if not emitted_result:
            yield from _fallback(text, muyuan_items, push, "LangGraph 未产出结果，使用规则模式兜底")
    except Exception as exc:  # noqa: BLE001
        yield from _fallback(text, muyuan_items, push, f"LangGraph 执行异常，使用规则模式兜底（{exc}）")