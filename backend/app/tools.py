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
_NEGATION = re.compile(r"(没有|没|无|未|不存在)\s*(逾期|欠款|拖欠|超期|违约|资金紧张)")


def strip_negation(text: str) -> str:
    """去掉“没有逾期/无欠款”等否定表达，避免误判为风险信号。"""
    return _NEGATION.sub("", text or "")

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
    m = re.search(r"(?:账期|月结|结款|付款|回款)[^\d\n\r]{0,10}(\d{1,3})\s*天", s) or \
        re.search(r"(\d{1,3})\s*天\s*(?:账期|结|付款|回款)", s)
    if m:
        return min(180, int(m.group(1)))
    if re.search(r"一个月(?:账期|结)?|月结", s):
        return 30
    if re.search(r"一个季度|季度结", s):
        return 90
    return None


def credit_risk_factors(text: str) -> list[str]:
    s = strip_negation(text or "")
    factors = []
    if re.search(r"逾期|超期|欠款|欠着|没结|没回款|货款还没|货款一直|还没给|没付|拖着|压着货款", s):
        factors.append("历史欠款或逾期")
    if re.search(r"手头紧|资金紧张|资金紧|现金紧|现金流|周转不开|周转困难|账上没钱|最近紧张|先欠着|等回款|下个月再结|宽限", s):
        factors.append("资金紧张信号")
    if re.search(r"(?:账期|结款)[^\n\r]{0,8}(?:延长|多给|放宽|加长|缓)|缓一缓|缓几天|再给几天|延后|要.*月结|给.*账期", s):
        factors.append("账期延长诉求")
    if re.search(r"联系不上|联系不到|电话打不通|不接电话|微信不回|失联|找不到人|关机|躲着", s):
        factors.append("失联或催收困难")
    if re.search(r"生意不好|生意差|不好做|要关门|关门|关店|转让|转出去|亏钱|亏本|经营困难|歇业", s):
        factors.append("经营恶化信号")
    if re.search(r"超授信|超出额度|超额度|额度用完|授信不够|超过额度", s):
        factors.append("接近或超过授信额度")
    if re.search(r"退换|退货增多|客诉|投诉", s) and re.search(r"货款|结款|账期|欠", s):
        factors.append("售后纠纷叠加付款问题")
    return list(dict.fromkeys(factors))


def assess_credit_risk(text: str) -> dict[str, Any]:
    """确定性规则引擎：输出风险等级/分/因子（唯一事实来源）。

    注意：termDays = 客户要求的账期天数；overdueDays = 已逾期天数，两者含义不同，不可混用。
    """
    factors = credit_risk_factors(text)
    pay = infer_pay_method(text)
    term_days = infer_term_days(text)
    s = strip_negation(text or "")
    m_days = re.search(r"逾期[^\d\n\r]{0,6}(\d{1,3})\s*天", s)
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
        elif f == "售后纠纷叠加付款问题":
            score += 14
    if overdue_days:
        score += min(15, round(overdue_days / 2))
    if pay in ("账期", "月结"):
        score += 6
    # 信息不足兜底：没有任何风险信号 + 无付款方式/账期/逾期信息 → 标注待确认，不强行判高风险
    insufficient = (not factors) and pay == "待确认" and term_days is None and overdue_days is None
    one_click = bool(re.search(r"失联|找不到人|明确不还|就是不给|赖账|关店|要关门|转让|转出去", s))
    risk_score = min(100, round(score))
    if insufficient:
        level, color = "低", "green"
    elif one_click or risk_score >= 81:
        level, color = "极高", "red"
    elif risk_score >= 61:
        level, color = "高", "orange"
    elif risk_score >= 31:
        level, color = "中", "gold"
    else:
        level, color = "低", "green"
    if level in ("极高", "高"):
        action = ["暂停新增赊销额度", "改为现款或短账期（7 天内）", "本周确认回款计划并上报主管"]
    elif level == "中":
        action = ["控制账期天数，不超授信", "先确认回款计划再继续发货", "每笔对账留书面确认"]
    else:
        action = ["按正常账期执行", "保持对账和回款记录"]
    return {
        "score": risk_score,
        "level": level,
        "color": color,
        "factors": factors,
        "action": action,
        "payMethod": pay,
        "termDays": term_days,
        "overdueDays": overdue_days,
        "insufficientInfo": insufficient,
        "note": "信息不足：未识别到付款方式 / 账期 / 欠款信息，建议补充后再定级" if insufficient else "",
    }


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


# 「待确认」是「抽取失败」的占位符，不是真实品项。比价时不能拿它当匹配依据，
# 否则两个都没识别出品项的行会互相匹配上，报出一个凭空的价差。
UNKNOWN_PRODUCT = "待确认"

# 明确不是价格的数字后缀：天数 / 折扣点数 / 计量单位 / 日期。
# 「账期还想压到45天」「竞品低3个点」「再加2吨」「20号拜访」都不是报价。
_NON_PRICE_TAIL = re.compile(r"\s*(?:天|个月|月|年|周|个点|点|折|%|％|次|吨|箱|件|提|人|公里|号|日|kg|公斤|斤)")
_PRICE_HEAD = re.compile(r"[个点折]\s*$")

# 公司名里不该出现的口语句子成分。
_COMPANY_NOISE = re.compile(
    r"嫌|说|想|要|还|再|能|可以|但是|不过|而且|我们|我方|我|他们|客户|老板|总|"
    r"需求|觉得|认为|低于|高于|便宜|贵|压|降|涨|让|问|谈|聊|报|别家|他家|对方"
)

# 纯修饰/程度词：删掉后如果什么都不剩，说明这段里根本没有公司名。
# 例：「竞品低3个点」-> 「低个点」-> 全部是修饰词 -> 判为无名，走占位符。
_COMPANY_FILLER = re.compile(r"低|高|偏|更|个点|个|点|分|左右|上下|以上|以下|那边|这边|的话")


def _clean_company(line: str) -> str:
    """从一行竞品描述里抽对方公司名。

    ⚠️ 旧实现把整句话当公司名：输入「河南客户张老板，餐饮连锁，嫌我们报价高，
    竞品低3个点」会得到「河南客户张老板 餐饮连锁 嫌我们高 低个点 还想压到」。
    现在只取第一个子句（公司名通常在分句最前面），再剔除描述性词；
    仍然不像名字就返回空串，由调用方兜底 —— 不硬猜。
    """
    head = re.split(r"[，,、:：!！?？]", line or "")[0]
    s = head
    s = re.sub(r"\d+\s*天(?:账期)?|账期|赊销|现款|现结|月结|款到发货|预付款|滚动结|竞品|报价|走|给", "", s)
    s = re.sub(r"\d+(\.\d+)?\s*(元\s*/?\s*(kg|公斤)|元/公斤|元每公斤|元|/kg|kg)?", "", s, flags=re.I)
    s = re.sub(r"[一二两三四五六七八九十]{1,3}(点[一二两三四五六七八九\d]|块(?:钱)?[一二两三四五六七八九]?|元[一二两三四五六七八九]?)", "", s)
    s = re.sub(r"白条肉|白条|分割品|气调产品|五花肉|猪前腿|猪后腿|前后腿|猪蹄|排骨|里脊|冷鲜肉|冻品|鲜品", "", s)
    s = _COMPANY_NOISE.sub("", s)
    s = _COMPANY_FILLER.sub("", s)          # 「低个点」走到这里会变成空串
    s = re.sub(r"[：:=,，;；|\-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) <= 1 or len(s) > 12:      # 空、单字、或仍是一整句 —— 都不算公司名
        return ""
    return s


def extract_price(line: str) -> float | None:
    """从一行里抽竞品报价（元/kg）。

    ⚠️ 旧实现接受任意 1~100 的裸数字，于是：
        「账期还想压到45天」-> 45.0
        「竞品低3个点」    -> 3.0
    两条都会被当成有效报价进比价表，是「账期天数当价格」的根因。
    现在要求价格标记（元/块/kg/一公斤），裸数字必须排除日期·折点·计量单位后缀。
    区间与前端 extractPriceCandidates 保持一致（5~60）。
    """
    if not line:
        return None
    m = re.search(
        r"(\d+(?:\.\d+)?)\s*(?:元\s*/?\s*(?:kg|公斤|斤)?|元/?公斤|元每公斤|元一公斤|块(?:钱)?|/kg)",
        line, re.I)
    if m:
        val = float(m.group(1))
        return round(val, 2) if 5 <= val <= 60 else None
    for m in re.finditer(r"(\d+(?:\.\d+)?)", line):
        if _NON_PRICE_TAIL.match(line[m.end():m.end() + 4]):
            continue
        if _PRICE_HEAD.search(line[max(0, m.start() - 3):m.start()]):
            continue
        val = float(m.group(1))
        if 5 <= val <= 60:
            return round(val, 2)
    return None


def parse_competitor_lines(text: str) -> list[dict]:
    rows = []
    for line in re.split(r"[\n。；;]", text or ""):
        line = line.strip()
        if not line:
            continue
        price = extract_price(line)
        if price is None:
            continue
        company = _clean_company(line) or "竞品（口头）"
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
            mp = (m.get("product") or "").strip()
            rp = (r.get("product") or "").strip()
            if not mp or not rp or mp == UNKNOWN_PRODUCT or rp == UNKNOWN_PRODUCT:
                continue          # 品项未知时不做匹配，避免报出凭空的价差
            if mp in rp or rp in mp:
                quote = m
                break
        gap = round(quote["price"] - r["price"], 2) if quote else None
        comp_days = r["terms"] if r["terms"] is not None else comp_days_default
        company_cap = quote["price"] * company_days / 365 * capital_rate if quote else 0
        comp_cap = r["price"] * comp_days / 365 * capital_rate
        # 综合价差 = 表面价差 －（我方资金成本 − 竞品资金成本）
        # 客户视角：账期越长，对方实际越划算，所以要减掉资金成本之差。
        # ⚠️ 旧实现写成 gap - (comp_cap - company_cap)，符号是反的 ——
        #    等于把资金成本差算了两遍（偏差恰好 2×(comp_cap−company_cap)）。
        gap_adj = round(gap - (company_cap - comp_cap), 2) if gap is not None else None
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