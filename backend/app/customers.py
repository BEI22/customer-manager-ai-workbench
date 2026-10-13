# -*- coding: utf-8 -*-
"""客户台账：Excel/CSV 导入 → JSON 存储 + 统计。

字段口径（导入模板表头支持中英文别名）：
    客户名称* / 区域(市) / 客户类型 / 主营品类(逗号分隔) / 关注点 / 供应商状态
    付款方式 / 账期天数 / 月采购量 / 开发状态 / 备注
区域会归一化为「XX市」；导入支持 replace（替换演示数据）/ append（按名称+区域去重追加）。
"""
from __future__ import annotations

import csv
import datetime
import io
import json
import os
import re
import uuid
from pathlib import Path

DATA_DIR = Path(os.path.dirname(os.path.abspath(__file__))).parent / "data"
CUSTOMERS_FILE = DATA_DIR / "customers.json"

VALID_TYPES = ["批发商", "批发客户", "餐饮连锁", "餐饮客户", "商超", "团餐", "农贸档口", "农贸", "食品加工", "其他"]
REGION_CITIES = ["郑州", "开封", "洛阳", "平顶山", "安阳", "鹤壁", "新乡", "焦作", "濮阳", "许昌", "漯河", "三门峡", "南阳", "商丘", "信阳", "周口", "驻马店", "济源"]

HEADER_ALIAS = {
    "name": ["客户名称", "客户名", "名称", "姓名", "customer", "客户"],
    "region": ["区域", "地区", "城市", "市", "所在地", "region", "city"],
    "type": ["客户类型", "类型", "渠道", "业态", "type"],
    "products": ["主营品类", "品类", "产品", "主营产品", "品项", "products"],
    "concern": ["关注点", "顾虑", "最关心", "主要顾虑", "concern"],
    "supplier": ["供应商状态", "现供应商", "供应商", "supplier"],
    "payMethod": ["付款方式", "结算方式", "pay"],
    "termDays": ["账期天数", "账期", "term"],
    "creditLimit": ["授信额度", "额度", "credit"],
    "monthlyVolume": ["月采购量", "采购量", "月销量", "volume"],
    "status": ["开发状态", "状态", "开发阶段", "status"],
    "note": ["备注", "说明", "note"],
}

STATUS_GROUP = {"已开发": "developed", "开发中": "developing", "未开发": "undeveloped", "空白": "blank"}
STATUS_VALUES = list(STATUS_GROUP) + ["待确认"]


def load_customers() -> list[dict]:
    if not CUSTOMERS_FILE.exists():
        return []
    try:
        return json.loads(CUSTOMERS_FILE.read_text(encoding="utf-8-sig"))
    except Exception:
        return []


def save_customers(customers: list[dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CUSTOMERS_FILE.write_text(json.dumps(customers, ensure_ascii=False, indent=1), encoding="utf-8")


def normalize_region(raw: str) -> str:
    """归一化区域为「XX市」，尽量落到 18 个地市之一。"""
    s = (raw or "").strip().replace("河南省", "").replace("河南", "")
    s = s.replace("市", "").replace("地区", "").replace("自治州", "")
    for city in REGION_CITIES:
        if city in s:
            return city + "市"
    if s:
        return s + "市"
    return "区域待确认"


def normalize_type(raw: str) -> str:
    s = (raw or "").strip()
    for t in VALID_TYPES:
        if t in s or s in t:
            return t if t != "批发客户" else "批发商"
    return s or "待确认"


def _split_products(raw) -> list[str]:
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    parts = re.split(r"[、,，;；/| ]+", str(raw or ""))
    return [p.strip() for p in parts if p.strip()][:6]


def _num(raw, default: int = 0) -> int:
    s = str(raw or "").strip()
    if not s or s == "-":
        return default
    m = re.search(r"\d+(\.\d+)?", s)
    return int(float(m.group(0))) if m else default


def _norm_status(raw: str) -> str:
    s = (raw or "").strip()
    for k in STATUS_GROUP:
        if k in s:
            return k
    return s or "待确认"


def clean_row(row: dict, line_no: int = 0) -> dict:
    name = str(row.get("name") or "").strip()
    region = normalize_region(row.get("region") or "")
    products = _split_products(row.get("products") or "")
    status = _norm_status(row.get("status"))
    return {
        "id": f"C-{uuid.uuid4().hex[:10]}",
        "name": name or f"未命名客户{line_no}",
        "region": region,
        "type": normalize_type(row.get("type") or ""),
        "products": products,
        "concern": str(row.get("concern") or "待确认").strip(),
        "supplier": str(row.get("supplier") or "待确认").strip(),
        "payMethod": str(row.get("payMethod") or "待确认").strip(),
        "termDays": _num(row.get("termDays")),
        "creditLimit": str(row.get("creditLimit") or "").strip(),
        "monthlyVolume": str(row.get("monthlyVolume") or "").strip(),
        "status": status,
        "note": str(row.get("note") or "").strip(),
        "source": "台账导入",
        "updatedAt": datetime.datetime.now().isoformat(timespec="seconds"),
    }


def _map_header(header: list[str]) -> dict:
    """表头 -> 内部字段名。返回 {col_index: field}。"""
    mapping: dict[int, str] = {}
    for idx, h in enumerate(header):
        hs = re.sub(r"\s|\*|（.*?）|\(.*?\)", "", str(h or "").strip())
        for field, aliases in HEADER_ALIAS.items():
            if hs in aliases or any(hs == a for a in aliases):
                mapping[idx] = field
                break
    return mapping


def parse_csv_bytes(data: bytes) -> list[dict]:
    text = data.decode("utf-8-sig", errors="replace")
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return []
    mapping = _map_header(rows[0])
    if not mapping:
        return []
    out = []
    for i, line in enumerate(rows[1:], start=2):
        if not any(str(c).strip() for c in line):
            continue
        row = {field: (line[idx].strip() if idx < len(line) else "") for idx, field in mapping.items()}
        if row.get("name"):
            out.append(clean_row(row, line_no=i))
    return out


def parse_xlsx_bytes(data: bytes) -> list[dict]:
    import openpyxl  # noqa: PLC0415
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    ws = wb.active
    rows_iter = ws.iter_rows(values_only=True)
    header = [str(c) if c is not None else "" for c in next(rows_iter, ())]
    mapping = _map_header(header)
    if not mapping:
        return []
    out = []
    i = 1
    for values in rows_iter:
        i += 1
        if not any(v is not None and str(v).strip() for v in values):
            continue
        row = {field: (str(values[idx]).strip() if idx < len(values) and values[idx] is not None else "") for idx, field in mapping.items()}
        if row.get("name"):
            out.append(clean_row(row, line_no=i))
    wb.close()
    return out


def parse_upload(filename: str, data: bytes) -> tuple[list[dict], str]:
    low = (filename or "").lower()
    if low.endswith(".csv"):
        return parse_csv_bytes(data), "csv"
    if low.endswith((".xlsx", ".xlsm")):
        return parse_xlsx_bytes(data), "xlsx"
    if low.endswith(".xls"):
        # 旧版 .xls 浏览器难解析，提示另存
        raise ValueError("暂不支持旧版 .xls，请在 Excel 中另存为 .xlsx 或 .csv 后重试")
    # 无扩展名时按内容嗅探
    if data[:2] == b"PK":
        return parse_xlsx_bytes(data), "xlsx"
    return parse_csv_bytes(data), "csv"


def import_customers(filename: str, data: bytes, mode: str = "replace") -> dict:
    """mode: replace=先清空再导入（默认，替换演示数据） / append=按名称+区域去重追加"""
    parsed, _fmt = parse_upload(filename, data)
    if not parsed:
        return {"ok": False, "reason": "empty", "message": "没有解析到有效客户行：请确认首行是表头（客户名称/区域/类型…）且至少有一行数据。"}
    if mode == "replace":
        existing: list[dict] = []
    else:
        existing = load_customers()
    seen = {(c.get("name"), c.get("region")) for c in existing}
    imported, skipped = 0, 0
    for c in parsed:
        key = (c["name"], c["region"])
        if key in seen:
            skipped += 1
            continue
        seen.add(key)
        existing.append(c)
        imported += 1
    save_customers(existing)
    return {"ok": True, "imported": imported, "skipped": skipped, "count": len(existing),
            "mode": mode, "sample": parsed[:3]}


def clear_customers() -> dict:
    save_customers([])
    return {"ok": True, "count": 0}


def region_stats(customers: list[dict] | None = None) -> dict:
    customers = load_customers() if customers is None else customers
    by_region: dict[str, dict] = {}
    for c in customers:
        region = c.get("region") or "区域待确认"
        st = by_region.setdefault(region, {"developed": 0, "developing": 0, "undeveloped": 0, "blank": 0, "total": 0})
        st["total"] += 1
        group = STATUS_GROUP.get(c.get("status"), "undeveloped")
        st[group] = st.get(group, 0) + 1
    return by_region
