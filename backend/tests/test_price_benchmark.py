# -*- coding: utf-8 -*-
"""竞品比价规则测试集。

运行方式：
    python tests/test_price_benchmark.py      # 直接运行
    pytest tests/test_price_benchmark.py      # 或用 pytest

覆盖本次修复的 4 个缺陷（每个都先写「旧行为会怎样」）：
  1. 账期天数被当成价格          （「压到45天」-> 45.0）
  2. 折扣点数被当成价格          （「低3个点」-> 3.0）
  3. 整句话被当成公司名
  4. 综合价差符号反              （偏差恰好 2×(竞品资金成本−我方资金成本)）
  5. 附带：「待确认」品项不再互相匹配

⚠️ 第 4 条不照抄实现的公式，而是按语义重算一遍（客户视角的总成本之差），
   否则测试只是在复述实现，证明不了公式是对的。
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


# ---------------------------------------------------------------- 1 / 2
def _prices(text: str) -> list[float]:
    return [r["price"] for r in tools.parse_competitor_lines(text)]


CASES = [
    {
        # 旧行为：extract_price 接受任意 1~100 的裸数字 -> 「45」（账期天数）
        "name": "1. 账期天数不当价格（压到45天）",
        "check": lambda: _prices("账期还想压到45天") == []
        and tools.extract_price("账期还想压到45天") is None,
        "detail": lambda: f"extract_price={tools.extract_price('账期还想压到45天')} rows={_prices('账期还想压到45天')}",
    },
    {
        "name": "2. 折扣点数不当价格（低3个点）",
        "check": lambda: tools.extract_price("竞品低3个点") is None
        and tools.extract_price("比我们低5个点") is None,
        "detail": lambda: f"extract_price={tools.extract_price('竞品低3个点')}",
    },
    {
        "name": "3. 计量单位/日期不当价格（2吨 / 20号）",
        "check": lambda: tools.extract_price("这个月再加2吨") is None
        and tools.extract_price("20号去拜访客户") is None,
        "detail": lambda: f"2吨={tools.extract_price('这个月再加2吨')} 20号={tools.extract_price('20号去拜访客户')}",
    },
    {
        # 旧行为：整句进 company 字段
        "name": "4. 公司名不是一整句话",
        "check": lambda: (
            len(tools.parse_competitor_lines("河南客户张老板，餐饮连锁，嫌我们报价高，竞品白条报价20.4元")[0]["company"]) <= 12
            and "嫌" not in tools.parse_competitor_lines("河南客户张老板，餐饮连锁，嫌我们报价高，竞品白条报价20.4元")[0]["company"]
        ),
        "detail": lambda: repr(tools.parse_competitor_lines("河南客户张老板，餐饮连锁，嫌我们报价高，竞品白条报价20.4元")[0]["company"]),
    },
    {
        "name": "5. 抽不出名字时用统一占位符，不硬猜",
        "check": lambda: tools.parse_competitor_lines("他说别家给20.4元")[0]["company"] == "竞品（口头）",
        "detail": lambda: repr(tools.parse_competitor_lines("他说别家给20.4元")[0]["company"]),
    },
    {
        # ⚠️ 两侧都必须抽不出品项才能触发旧 bug：
        #    旧的比较条件是 `m["product"] in r["product"] or r["product"] in m["product"]`，
        #    当两边都是「待确认」时互相包含 -> 判定为同一品项 -> 报出凭空的价差。
        #    （只用一侧是「待确认」是测不出来的 —— 那样本来就匹配不上。）
        "name": "6. 「待确认」品项不参与匹配",
        "check": lambda: tools.compare_prices(
            [{"product": "待确认", "price": 20.8}], "他说给20.4元，规格还没说清", 0, 0, 0.06
        )["rows"][0]["companyPrice"] is None,
        "detail": lambda: repr(tools.compare_prices(
            [{"product": "待确认", "price": 20.8}], "他说给20.4元，规格还没说清", 0, 0, 0.06
        )["rows"][0]),
    },
    {
        "name": "7. 正常报价仍能抽到（别家报价19.6 / 20.4元/kg 不回归）",
        "check": lambda: _prices("别家报价19.6") == [19.6]
        and _prices("竞品白条20.4元/kg") == [20.4],
        "detail": lambda: f"{_prices('别家报价19.6')} {_prices('竞品白条20.4元/kg')}",
    },
]


def check_gap_adj() -> tuple[bool, str]:
    """综合价差：按语义重算，不看实现公式。

    场景：我方白条 20.8、现款（0 天）；竞品 20.4、给 14 天账期；资金成本率 6%。
      我方综合成本 = 20.8 − 0                       = 20.800
      竞品综合成本 = 20.4 − 20.4×14/365×0.06        = 20.353
      综合价差     = 20.800 − 20.353               = 0.447 -> 0.45

    旧实现给 0.35（符号反），偏差 = 2 × 竞品资金成本 = 0.094。
    方向性不变量：竞品账期更长时，综合价差必须 **大于** 表面价差。
    """
    comp_days, rate = 14, 0.06
    res = tools.compare_prices([{"product": "白条", "price": 20.8}],
                              "竞品白条报价20.4元，给14天账期", 0, 0, rate)
    row = res["rows"][0]
    comp_cap = 20.4 * comp_days / 365 * rate
    expected = round(20.8 - (20.4 - comp_cap), 2)          # 语义重算
    old_value = round((20.8 - 20.4) - (comp_cap - 0.0), 2)  # 旧公式，应被排除
    ok = (
        row["gapAdj"] == expected
        and row["gapAdj"] != old_value
        and row["surfaceGap"] == 0.4
        and row["gapAdj"] > row["surfaceGap"]      # 方向性不变量
        and row["competitorPrice"] == 20.4
        and row["companyPrice"] == 20.8
        and row["compDays"] == 14
    )
    return ok, (f"gapAdj={row['gapAdj']} 期望={expected}（旧公式={old_value}） "
                f"surfaceGap={row['surfaceGap']} compDays={row['compDays']}")


def check_gap_adj_symmetry() -> tuple[bool, str]:
    """反向场景：我方给账期、竞品现款 → 综合价差应 **小于** 表面价差。"""
    rate = 0.06
    res = tools.compare_prices([{"product": "白条", "price": 20.8}],
                              "竞品白条报价20.4元", 30, 0, rate)   # 我方 30 天账期
    row = res["rows"][0]
    company_cap = 20.8 * 30 / 365 * rate
    # 语义重算：我方综合成本 20.8−company_cap，竞品综合成本 20.4−0
    expected = round((20.8 - company_cap) - 20.4, 2)
    ok = row["gapAdj"] == expected and row["gapAdj"] < row["surfaceGap"]
    return ok, f"gapAdj={row['gapAdj']} 期望={expected} surfaceGap={row['surfaceGap']}"


def run_all(verbose: bool = True) -> int:
    passed = failed = 0
    for case in CASES:
        try:
            ok = bool(case["check"]())
        except Exception as exc:  # noqa: BLE001
            ok = False
            case = {**case, "detail": lambda: f"异常：{type(exc).__name__}: {exc}"}
        if ok:
            passed += 1
            print(f"  [PASS] {case['name']}")
        else:
            failed += 1
            print(f"  [FAIL] {case['name']}")
            try:
                print(f"         实际：{case['detail']()}")
            except Exception:  # noqa: BLE001
                pass

    for label, fn in (("8. 综合价差公式（符号 + 方向性）", check_gap_adj),
                      ("9. 综合价差反向场景", check_gap_adj_symmetry)):
        try:
            ok, detail = fn()
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, f"异常：{type(exc).__name__}: {exc}"
        if ok:
            passed += 1
            print(f"  [PASS] {label}")
        else:
            failed += 1
            print(f"  [FAIL] {label}\n         实际：{detail}")

    print(f"\n结果：通过 {passed} / {passed + failed}，失败 {failed}")
    return 0 if failed == 0 else 1


# pytest 兼容
def test_price_benchmark_cases():
    assert run_all(verbose=False) == 0


if __name__ == "__main__":
    print("竞品比价规则测试：")
    sys.exit(run_all())
