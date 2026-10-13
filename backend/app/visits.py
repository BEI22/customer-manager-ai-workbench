# -*- coding: utf-8 -*-
"""拜访记录服务端存储：visits.json。

前端原把拜访历史放 localStorage（换机即丢）。现在：
- 保存时双写（本地缓存 + POST /api/visits）
- 进入系统时前端拉 GET /api/visits 合并/覆盖本地 → 看板统计以服务端为准
字段与前端 visit record 对齐（兼容旧记录）。
"""
from __future__ import annotations

import datetime
import json
import os
import uuid
from pathlib import Path

DATA_DIR = Path(os.path.dirname(os.path.abspath(__file__))).parent / "data"
VISITS_FILE = DATA_DIR / "visits.json"
MAX_VISITS = 300


def load_visits() -> list[dict]:
    if not VISITS_FILE.exists():
        return []
    try:
        return json.loads(VISITS_FILE.read_text(encoding="utf-8-sig"))
    except Exception:
        return []


def save_visits(visits: list[dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    VISITS_FILE.write_text(json.dumps(visits[:MAX_VISITS], ensure_ascii=False, indent=1), encoding="utf-8")


def _record_id(record: dict) -> str:
    return str(record.get("id") or record.get("recordId") or record.get("historyId") or "")


def add_visit(record: dict) -> dict:
    visits = load_visits()
    rid = _record_id(record)
    if not rid:
        rid = f"V-{uuid.uuid4().hex[:10]}"
        record = {**record, "id": rid, "historyId": rid, "recordId": rid}
    # 同 id 覆盖（保存草稿场景），否则头部插入
    visits = [v for v in visits if _record_id(v) != rid]
    record = {**record, "updatedAt": datetime.datetime.now().isoformat(timespec="seconds")}
    visits.insert(0, record)
    save_visits(visits)
    return {"ok": True, "id": rid, "count": len(visits)}


def merge_visits(records: list[dict]) -> dict:
    """把前端本地记录批量合并进来（按 id 去重，以传入为准）。"""
    visits = load_visits()
    seen = {_record_id(v) for v in visits}
    added = 0
    for rec in records or []:
        if not rec or not any(str(rec.get(k) or "").strip() for k in ("transcript", "summaryText", "customerName", "fileName")):
            continue
        rid = _record_id(rec)
        if rid and rid in seen:
            visits = [rec if _record_id(v) == rid else v for v in visits]
            continue
        if not rid:
            rid = f"V-{uuid.uuid4().hex[:10]}"
            rec = {**rec, "id": rid, "historyId": rid, "recordId": rid}
        visits.insert(0, rec)
        seen.add(rid)
        added += 1
    save_visits(visits)
    return {"ok": True, "added": added, "count": len(visits)}


def delete_visit(rid: str) -> dict:
    visits = load_visits()
    before = len(visits)
    visits = [v for v in visits if _record_id(v) != rid]
    save_visits(visits)
    return {"ok": True, "deleted": before - len(visits)}


def clear_visits() -> dict:
    save_visits([])
    return {"ok": True, "count": 0}
