# -*- coding: utf-8 -*-
"""Agent 工具集：账期风控 / 竞品比价 / 成功案例检索 / 消息推送。

规则逻辑与前端原型一致（可离线运行、零成本），正式版可替换为真实接口。
"""
from __future__ import annotations

import json
import re
from typing import Any

import httpx

from . import memory


# ---------- 账期与信用（PRD 7.17 / 7.18） ----------
def infer_pay_method(text: str) -> str:
    rules = [
        ("现款", r"现款|现结|现金|款到发货|先款|打款再发|款到再发"),
        ("预付款", r"预付|定金|订金"),
        ("月结", r"月结|按月结"),
        ("滚动结", r"滚动结"),
        ("账期", r"账期|赊销|先货后款|压款|欠着|挂账"),
    ]
    for name, pat in rules:
        if re.search(pat, text or ""):
            return name
    return "待确认"


def infer_term_days(text: str) -> int | None:
    s = text or ""
    m = re.search(r"(?:账期|月结|结款|付款|回款)[^\n\r]{0,10}(\d{1,3})\s*天", s) or \
        re.search(r"(\d{1,3})\s*天\s*(?:账期|结|付款|回款)", s)
    if m:
        return min(180, int(m.group(1)))
    if re.search(r"一个月(?:账期|结)?|月结", s):
        return 30
    if re.search(r"一个季度|季度结", s):
        return 90
    return None


def credit_risk_factors(text: str) -> list[str]:
    s = text or ""
    factors = []
    if re.search(r"逾期|欠款|欠着|没结|没回款|货款还没|拖", s):
        factors.append("历史欠款或逾期")
    if re.search(r"手头紧|资金紧张|周转不开|资金紧|最近紧张|先欠着|等回款|下个月再结|宽限", s):
        factors.append("资金紧张信号")
    if re.search(r"(?:账期|结款)[^\n\r]{0,8}(?:延长|多给|放宽|加长)|要.*月结|给.*账期", s):
        factors.append("账期延长诉求")
    if re.search(r"联系不上|不接电话|失联|找不到人|关机", s):
        factors.append("失联或催收困难")
    if re.search(r"生意不好|不好做|关店|转让|亏钱|经营困难|歇业", s):
        factors.append("经营恶化信号")
    if re.search(r"超授信|超出额度|授信不够|超过额度", s):
        factors.append("接近或超过授信额度")
    return list(dict.fromkeys(factors))


def assess_credit_risk(text: str) -> dict[str, Any]:
    factors = credit_risk_factors(text)
    pay = infer_pay_method(text)
    s = text or ""
    m_days = re.search(r"逾期[^\n\r]{0,6}(\d{1,3})\s*天", s)
    overdue_days = int(m_days.group(1)) if m_days else None
    score = 8
    for f in factors:
        if f in ("失联或催收困难", "经营恶化信号"):
            score += 30
        elif f == "历史欠款或逾期":
            score += 28
        elif f == "接近或超过授信额度":
            score += 22
        elif f == "资金紧张信号":
            score += 20
        elif f == "账期延长诉求":
            score += 16
    if overdue_days:
        score += min(15, round(overdue_days / 2))
    if pay in ("账期", "月结"):
        score += 6
    one_click = bool(re.search(r"失联|找不到人|明确不还|就是不给|赖账|关店|转让", s))
    risk_score = min(100, round(score))
    if one_click or risk_score >= 81:
        level, color = "极高", "red"
    elif risk_score >= 61:
        level, color = "高", "orange"
    elif risk_score >= 31:
        level, color = "中", "gold"
    else:
        level, color = "低", "green"
    action = (
        ["暂停新增赊销额度", "改为现款或短账期（7 天内）", "本周确认回款计划并上报主管"]
        if level in ("极高", "高")
        else ["控制账期天数，不超授信", "先确认回款计划再继续发货", "每笔对账留书面确认"]
        if level == "中"
        else ["按正常账期执行", "保持对账和回款记录"]
    )
    return {"score": risk_score, "level": level, "color": color, "factors": factors, "action": action,
            "payMethod": pay, "termDays": overdue_days}


# ---------- 竞品报价解析与比价 ----------
PRODUCT_RULES = [
    ("气调产品", r"气调|锁鲜|盒装|冷鲜肉礼盒|保鲜包装"),
    ("白条", r"白条|白条肉|整边|胴体"),
    ("五花肉", r"五花肉|五花"),
    ("前后腿", r"前后腿|前后"),
    ("猪前腿", r"猪前腿|前腿|前夹"),
    ("猪后腿", r"猪后腿|后腿|后座"),
    ("猪蹄", r"猪蹄|蹄膀|猪手"),
    ("排骨", r"排骨|肋排"),
    ("里脊", r"里脊"),
    ("冷鲜肉", r"冷鲜|鲜品"),
    ("冻品", r"冻品|冻肉|冷冻"),
    ("分割品", r"分割品|分割|肉品分割|腿肉|肉品"),
]


def infer_product(text: str) -> str:
    for name, pat in PRODUCT_RULES:
        if re.search(pat, text or ""):
            return name
    return "待确认"


def _clean_company(line: str) -> str:
    s = re.sub(r"\d+\s*天(?:账期)?|账期|赊销|现款|现结|月结|款到发货|预付款|滚动结|竞品|报价|走|给", "", line)
    s = re.sub(r"\d+(\.\d+)?\s*(元\s*\/?\s*(kg|公斤)|元/公斤|元每公斤|元|/kg|kg)?", "", s, flags=re.I)
    s = re.sub(r"[一二两三四五六七八九十]{1,3}(点[一二两三四五六七八九\d]|块(?:钱)?[一二两三四五六七八九]?|元[一二两三四五六七八九]?)", "", s)
    s = re.sub(r"白条肉|白条|分割品|气调产品|五花肉|猪前腿|猪后腿|前后腿|猪蹄|排骨|里脊|冷鲜肉|冻品|鲜品", "", s)
    s = re.sub(r"[：:=,，;；|\-]", " ", s)
    return s.strip()


def extract_price(line: str) -> float | None:
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:元\s*\/?\s*(?:kg|公斤)|元/公斤|元每公斤|元|/kg|kg)?", line)
    if not m:
        return None
    val = float(m.group(1))
    return val if 1 < val < 100 else None


def parse_competitor_lines(text: str) -> list[dict]:
    rows = []
    for line in re.split(r"[\n。；;]", text or ""):
        line = line.strip()
        if not line:
            continue
        price = extract_price(line)
        if price is None:
            continue
        company = _clean_company(line) or "用户口头报价"
        rows.append({
            "company": company,
            "product": infer_product(line),
            "price": round(price, 2),
            "terms": infer_term_days(line),
        })
    return rows


def compare_prices(company_items: list[dict], competitor_text: str, company_days: int = 0, comp_days_default: int = 0, capital_rate: float = 0.06) -> dict:
    rows = []
    for r in parse_competitor_lines(competitor_text):
        quote = None
        for m in company_items:
            if m.get("product") and r["product"] and (m["product"] in r["product"] or r["product"] in m["product"]):
                quote = m
                break
        gap = round(quote["price"] - r["price"], 2) if quote else None
        comp_days = r["terms"] if r["terms"] is not None else comp_days_default
        company_cap = quote["price"] * company_days / 365 * capital_rate if quote else 0
        comp_cap = r["price"] * comp_days / 365 * capital_rate
        gap_adj = round(gap - (comp_cap - company_cap), 2) if gap is not None else None
        rows.append({
            "company": r["company"], "product": r["product"],
            "companyPrice": quote["price"] if quote else None,
            "competitorPrice": r["price"], "surfaceGap": gap, "gapAdj": gap_adj,
            "companyDays": company_days, "compDays": comp_days,
            "condition": "资料提取价，规格、账期和配送条件需复核" if quote else "已识别竞品价，需补录该品项公司报价",
        })
    return {"rows": rows, "count": len(rows)}


# ---------- 成功案例检索 ----------
def search_success_cases(query: str, profile: dict | None = None, top_n: int = 3) -> dict:
    hits = memory.search_similar(query, profile, top_n)
    return {
        "hits": [
            {
                "customerName": h["case"].get("customerName", ""),
                "type": h["case"].get("type", ""),
                "product": h["case"].get("product", ""),
                "concern": h["case"].get("concern", ""),
                "breakthrough": h["case"].get("breakthrough", ""),
                "result": h["case"].get("result", ""),
                "score": h["score"], "similar": h["similar"], "diff": h["diff"],
            }
            for h in hits
        ],
        "count": len(hits),
    }


# ---------- 推送（占位，正式版服务端中转） ----------
def push_message(channel: str, webhook: str, title: str, detail: str = "") -> dict:
    if not webhook:
        return {"ok": False, "reason": "未配置 Webhook"}
    payload = (
        {"msgtype": "text", "text": {"content": f"【客户经理AI作战台】\n{title}\n{detail}"}}
        if channel == "钉钉"
        else {"msg_type": "text", "content": {"text": f"【客户经理AI作战台】\n{title}\n{detail}"}}
    )
    try:
        resp = httpx.post(webhook, json=payload, timeout=8)
        return {"ok": resp.status_code < 400, "reason": f"HTTP {resp.status_code}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"发送失败：{exc}"}