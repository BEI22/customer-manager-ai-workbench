# -*- coding: utf-8 -*-
"""成功记忆库：本地 JSON 存储 + 特征向量检索（原型口径）。

正式版将这里替换为真实 embedding API + 向量数据库（pgvector / Chroma / Milvus），
对外接口保持 search_similar() 不变。
"""
from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path

DATA_DIR = Path(os.path.dirname(os.path.abspath(__file__))).parent / "data"
CASES_FILE = DATA_DIR / "success_cases.json"


def load_cases() -> list[dict]:
    if not CASES_FILE.exists():
        return []
    try:
        return json.loads(CASES_FILE.read_text(encoding="utf-8-sig"))
    except Exception:
        return []


def save_cases(cases: list[dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CASES_FILE.write_text(json.dumps(cases[:200], ensure_ascii=False, indent=2), encoding="utf-8")


def add_case(case: dict) -> None:
    cases = [c for c in load_cases() if c.get("id") != case.get("id")]
    cases.insert(0, case)
    save_cases(cases)


def case_text(c: dict) -> str:
    # ⚠️ 用 `or []` 而不是 `.get("actions", [])`：
    # `.get` 的默认值只在**键不存在**时生效；数据里存在 `"actions": null` 的记录
    # （SC-f44005b787），此时返回 None，`for a in None` 会抛 TypeError，
    # 而调用它的 search_similar 是在 SSE 生成器里跑的 —— 一崩整条对话流就断。
    actions = c.get("actions") or []
    if not isinstance(actions, (list, tuple)):
        actions = [actions] if actions else []
    action_text = " ".join(
        a if isinstance(a, str) else (a or {}).get("title", "")
        for a in actions
        if isinstance(a, (str, dict))
    )
    parts = [
        c.get("customerName", ""), c.get("type", ""), c.get("product", ""),
        c.get("concern", ""), c.get("breakthrough", ""), c.get("result", ""),
        c.get("process", ""), c.get("purpose", ""),
        action_text,
    ]
    return " ".join(p for p in parts if p)


def embed_text(text: str) -> list[float]:
    """原型口径：18 维业务特征向量。正式版替换为 embedding API 返回的向量。"""
    s = text or ""
    features = [
        1 if re.search(r"批发", s) else 0,
        1 if re.search(r"餐饮", s) else 0,
        1 if re.search(r"商超", s) else 0,
        1 if re.search(r"团餐", s) else 0,
        1 if re.search(r"农贸", s) else 0,
        1 if re.search(r"白条", s) else 0,
        1 if re.search(r"分割", s) else 0,
        1 if re.search(r"气调", s) else 0,
        1 if re.search(r"鲜品|冷鲜", s) else 0,
        1 if re.search(r"冻品", s) else 0,
        1 if re.search(r"价格|报价|成本|比价", s) else 0,
        1 if re.search(r"品质|质量|标准", s) else 0,
        1 if re.search(r"配送|到货|时效", s) else 0,
        1 if re.search(r"售后|服务|投诉", s) else 0,
        1 if re.search(r"试单|试销|小批量", s) else 0,
        1 if re.search(r"复购|加量|继续合作|补货", s) else 0,
        1 if re.search(r"新客|开发", s) else 0,
        1 if re.search(r"账期|现款|回款|月结", s) else 0,
    ]
    return [float(x) for x in features]


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def similar_diff(a: dict, b: dict) -> dict:
    similar, diff = [], []
    ta, tb = a.get("type", ""), b.get("type", "")
    if ta and tb and (tb.replace("商", "") in ta or ta.replace("商", "") in tb):
        similar.append(f"同为{tb or ta}类型")
    elif ta and tb:
        diff.append(f"类型：{ta} vs {tb}")
    pa, pb = a.get("product", ""), b.get("product", "")
    if pa and pb and (pb in pa or pa in pb):
        similar.append(f"主营{pb or pa}")
    elif pa and pb:
        diff.append(f"品类：{pa} vs {pb}")
    ca, cb = a.get("concern", ""), b.get("concern", "")
    if ca and cb and ca == cb:
        similar.append(f"关注点一致（{ca}）")
    if b.get("breakthrough"):
        similar.append(f"打法：{b['breakthrough']}")
    return {"similar": similar[:3], "diff": diff[:3]}


def search_similar(query_text: str, profile: dict | None = None, top_n: int = 3) -> list[dict]:
    """检索与目标客户最相似的成功案例。返回 [{case, score, similar, diff}]。"""
    query = (query_text or "") + " " + ((profile or {}).get("type", "") + " " + (profile or {}).get("concern", ""))
    q_vec = embed_text(query)
    scored = []
    for c in load_cases():
        vec = embed_text(case_text(c))
        score = round(cosine(q_vec, vec) * 100)
        t = (profile or {}).get("type", "")
        if t and c.get("type") and (c["type"].replace("商", "") in t or t.replace("商", "") in c["type"]):
            score += 12
        if (profile or {}).get("concern") and c.get("concern") == (profile or {}).get("concern"):
            score += 8
        if re.search(r"试单|复购|加量|继续合作", case_text(c)):
            score += 6
        scored.append({"case": c, "score": min(99, score)})
    scored = [s for s in scored if s["score"] >= 30]
    scored.sort(key=lambda s: s["score"], reverse=True)
    out = []
    for s in scored[:top_n]:
        c = s["case"]
        sd = similar_diff(profile or {}, c)
        out.append({**s, "similar": sd["similar"], "diff": sd["diff"]})
    return out