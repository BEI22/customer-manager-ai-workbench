# -*- coding: utf-8 -*-
"""账期风控规则引擎测试集（P0）。

运行方式：
    python tests/test_credit_risk.py      # 直接运行
    pytest tests/test_credit_risk.py      # 或用 pytest

覆盖清单中的 8 条边界用例：账期 / 逾期 / 组合因子 / 极高风险 / 信息不足。
"""
from __future__ import annotations

import os
import sys

# GBK 控制台直接 print 中文会乱码，导致测试输出读不出来（证据失效）。
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import tools  # noqa: E402

CASES = [
    {
        "name": "1. 正常现款 → 低风险",
        "text": "客户一直现款结算，款到发货，没有欠款。",
        "check": lambda r: r["level"] == "低" and r["overdueDays"] is None,
    },
    {
        "name": "2. 历史逾期20天 → 风险上升 + overdueDays=20",
        "text": "上次那批货款逾期20天，客户拖了一段时间才结清。",
        "check": lambda r: r["level"] in ("中", "高", "极高") and r["overdueDays"] == 20,
    },
    {
        "name": "3. 要求30天账期 → termDays=30",
        "text": "客户要求给30天账期，先货后款。",
        "check": lambda r: r["termDays"] == 30 and r["overdueDays"] is None,
    },
    {
        "name": "4. 要求60天账期且未逾期 → termDays=60、overdueDays=None",
        "text": "客户要求60天账期，目前没有逾期。",
        "check": lambda r: r["termDays"] == 60 and r["overdueDays"] is None and "历史欠款或逾期" not in r["factors"],
    },
    {
        "name": "5. 资金紧张 + 延长账期 → 组合加分",
        "text": "客户说最近手头紧，想把账期再延长一个月。",
        "check": lambda r: r["score"] >= 40
        and "资金紧张信号" in r["factors"]
        and "账期延长诉求" in r["factors"]
        and r["level"] in ("中", "高", "极高"),
    },
    {
        "name": "6. 失联 + 欠款 → 极高风险",
        "text": "客户已经失联，电话打不通，还欠着货款八万五没结。",
        "check": lambda r: r["level"] == "极高",
    },
    {
        "name": "7. 关店/转让 + 欠款 → 极高风险",
        "text": "客户说生意不好做，准备转让店面，之前还欠着两万货款没结。",
        "check": lambda r: r["level"] == "极高",
    },
    {
        "name": "8. 信息不足 → 待确认，不强行判高风险",
        "text": "今天拜访了客户，聊了聊近况，整体沟通比较顺利。",
        "check": lambda r: r["insufficientInfo"] is True and r["level"] == "低",
    },
]


def run_all(verbose: bool = True) -> int:
    passed, failed = 0, 0
    for case in CASES:
        result = tools.assess_credit_risk(case["text"])
        try:
            ok = bool(case["check"](result))
        except Exception as exc:  # noqa: BLE001
            ok = False
            result = {**result, "_error": str(exc)}
        if ok:
            passed += 1
            print(f"  [PASS] {case['name']}")
        else:
            failed += 1
            print(f"  [FAIL] {case['name']}")
            print(f"         实际：level={result.get('level')} score={result.get('score')} "
                  f"termDays={result.get('termDays')} overdueDays={result.get('overdueDays')} "
                  f"factors={result.get('factors')} insufficient={result.get('insufficientInfo')}")
        if verbose and ok:
            print(f"         level={result['level']} score={result['score']} "
                  f"termDays={result['termDays']} overdueDays={result['overdueDays']} "
                  f"factors={'、'.join(result['factors']) or '无'}")
    print(f"\n结果：通过 {passed} / {len(CASES)}，失败 {failed}")
    return 0 if failed == 0 else 1


# pytest 兼容
def test_credit_risk_cases():
    assert run_all(verbose=False) == 0


if __name__ == "__main__":
    print("账期风控规则测试：")
    sys.exit(run_all())
