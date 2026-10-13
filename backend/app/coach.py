# -*- coding: utf-8 -*-
"""话术演练室（对话式陪练）：基于成功案例库让 AI 扮演"最难搞的相似客户"。

- 配置 DEEPSEEK_API_KEY：DeepSeek 扮演客户 + 教练（角色人设取自检索到的成功案例）；
- 未配置：规则引擎兜底（把案例 process 里的真实对话拆成客户/经理台词，
  按异议类型推进剧情、检测经理是否化解、给参考话术）。两种模式都能跑通完整多轮演练。
"""
from __future__ import annotations

import json
import re
import time
import uuid

from . import llm as llm_mod
from . import memory

# ---------- 会话存储（原型用内存，正式版可换 Redis） ----------
SESSIONS: dict[str, dict] = {}
SESSION_TTL = 3600  # 秒

DIFFICULTY = {"新手": {"init_mood": 35, "goal": 65, "need": 1, "max_rounds": 10},
              "实战": {"init_mood": 28, "goal": 72, "need": 2, "max_rounds": 10},
              "地狱": {"init_mood": 20, "goal": 80, "need": 2, "max_rounds": 12}}

# 异议类型关键词（按优先级排列，先命中先算）
OBJECTION_RULES = [
    ("credit", r"账期|欠|结款|回款|月结|现款|手头|宽限|赊|没结|未结|拖|下个月结"),
    ("price", r"价格|便宜|贵|压价|让一让|比价|报价|低一点|成本高"),
    ("quality", r"品质|质量|出成率|损耗高|怕损耗|新鲜|保质|标准|稳定|不踏实"),
    ("delivery", r"配送|到货|时效|物流|冷链|断货|送得|距离|车"),
    ("supplier", r"供应商|别家|一直在用|用了多年|换掉|稳定供应|断供|老关系"),
]

# case.concern -> 异议类型（案例没台词可析时的回退）
CONCERN_OBJECTION = {"品质稳定": "quality", "品质": "quality", "质量": "quality",
                     "价格敏感": "price", "价格": "price",
                     "配送时效": "delivery", "售后响应": "quality", "稳定供应": "supplier"}

# 化解动作关键词：key 与 OBJECTION_RULES 的 key 对应
DEFUSE_RULES = {
    "credit": r"先结|结一半|分期|一部分|现款|本周|周[一二三四五六日天]|月底|申请|上报|审批|授信|承诺|台账|回款计划",
    "price": r"试单|试销|小批量|样品|先来一批|综合成本|折算|规格|损耗|配送|售后|免费|补贴|政策",
    "quality": r"批次|检测|检验|检疫|溯源|标准|规格稳定|出成率|抽检|报告|证件|损耗率",
    "delivery": r"配送|时效|冷链|自提|加急|准时|提前|库存|保供|路线|半径",
    "supplier": r"不着急|先了解|补充|备选|多一个|试单|试销|小批量|样品|验证|对比|再决定",
}
# 通用强推进动作（任何异议下，提"小范围验证/试单"都算有效推进）
PUSH_ANY = r"试单|试销|小批量|样品|先来一批|先做一批|验证|试用|试一把"

# 异议台词库：推进后客户软化 / 未化解时客户坚持（按异议类型）
SOFT_LINES = {
    "credit": "……行吧，那你说说怎么个结法，要是能接受我就安排。",
    "price": "价格这块你要是能给我个说法，账期上我也不是不能配合。",
    "quality": "你要是能拿出稳定供货的证据，我倒愿意试试你们。",
    "delivery": "配送要是真能跟上，换一家试试也不是不行。",
    "supplier": "嗯……你说得也有点道理，但我得先看看你们的诚意。",
}
HOLD_LINES = {
    "credit": "不是我难为你，我这现金都压在货上了。账期再宽宽，对大家都好，你说呢？",
    "price": "光说没用啊，人家给的价格摆在那，你这边能不能再让让？",
    "quality": "我不是不信你，是之前吃过亏。品质这事，光靠嘴说我不踏实。",
    "delivery": "我们这边对到货时间卡得严，耽误一天就是一天的钱，你拿什么保证？",
    "supplier": "我们跟老供应商合作好多年了，感情都在，换人风险太大。",
}
WIN_LINES = [
    "行，那就先按你说的，来一小批试试，看看实际效果。",
    "成，这批你安排吧，走货没问题后面再谈加量。",
    "好吧，我把回款时间记下了，按约定来，别再让我为难。",
]
LOSE_LINES = [
    "今天就先这样吧，你再回去想想，想清楚了咱们再聊。",
    "算了算了，你这说法我没法跟老板交代，先不聊了。",
    "那这事就先搁着吧，我们还得再看看别家。",
]
OPENING_LINES = [
    "（今天客户看起来心情一般）来啦？坐。你们家最近什么情况，我正好有点事想跟你说道说道。",
    "（客户开门见山）正好你来了，我最近正琢磨着调整下供货，有几个事想问问你。",
]

STAGE_LABEL = {0: "破冰开场", 1: "挖掘需求", 2: "异议交锋", 3: "促成收尾"}


# ---------- 台词解析：从成功案例 process 拆出 客户台词 / 经理台词 ----------
def _is_client_speaker(speaker: str, customer_name: str) -> bool:
    s = speaker.strip()
    if not s:
        return False
    if s == "客户":
        return True
    if "客户经理" in s or s.startswith("小") or s in ("我", "经理", "客户经理"):
        return False
    name = (customer_name or "").replace("客户", "").strip()
    if name and (name in s or s in name):
        return True
    # 张老板 / 李总 / 王经理（非"客户经理"）这类称呼都当客户
    if re.search(r"^[\u4e00-\u9fa5]{1,3}(老板|总|经理)$", s):
        return True
    return False


def parse_dialog(process: str, customer_name: str) -> tuple[list[str], list[str]]:
    """返回 (客户台词列表, 经理台词列表)，按原文顺序。"""
    client_lines, mgr_lines = [], []
    if not process:
        return client_lines, mgr_lines
    for raw in re.split(r"[\n\r]+", process):
        line = raw.strip()
        if not line:
            continue
        m = re.match(r"^([^：:]{1,12})[：:](.+)$", line)
        if not m:
            continue
        speaker, content = m.group(1).strip(), m.group(2).strip()
        if len(content) < 2:
            continue
        if _is_client_speaker(speaker, customer_name):
            client_lines.append(content)
        else:
            mgr_lines.append(content)
    return client_lines, mgr_lines


# ---------- 异议分类与化解评分 ----------
def classify_objection(text: str) -> str | None:
    for key, pat in OBJECTION_RULES:
        if re.search(pat, text or ""):
            return key
    return None


def defuse_score(user_talk: str, objection: str | None, text_all: str = "") -> int:
    """0=空话  1=沾边/部分  2=有效化解。text_all 用于结合整轮客户台词判断。"""
    t = user_talk or ""
    if not objection:
        return 1 if re.search(PUSH_ANY, t) else 0
    score = 0
    pat = DEFUSE_RULES.get(objection, "")
    if pat and re.search(pat, t):
        score += 1
    if re.search(PUSH_ANY, t):
        score += 1
    # 全文中出现让步/承诺数字类信号（如"这周先结一半"）额外加分
    if objection == "credit" and re.search(r"(一半|几成|\d+[%％]|先转|这周|明天|后天)", t):
        score += 1
    if objection == "price" and re.search(r"(含税|含运费|综合|折算|资金成本|账期折算)", t):
        score += 1
    return min(score, 3)


def pick_mgr_hint(sess: dict) -> str:
    """从案例原文里挑一句最能化解当前异议的经理台词当参考话术。"""
    case = sess["case"]
    objection = sess["objection"]
    mgr_lines = sess.get("mgr_lines") or []
    if mgr_lines:
        if objection:
            pat = DEFUSE_RULES.get(objection, "")
            for line in mgr_lines:
                if pat and re.search(pat, line):
                    return "📖 案例原文打法（" + (case.get("customerName") or "同类客户") + " 场景）：" + line
        # 找不到匹配 → 取最长的实质回应（跳过寒暄/短客套）
        best = max(mgr_lines, key=len) if mgr_lines else ""
        return "📖 案例原文打法（" + (case.get("customerName") or "同类客户") + " 场景）：" + best
    return _reference_talk(objection, case)


def _coach_note(score: int, objection: str | None, difficulty: str, need: int) -> str:
    if objection == "credit":
        base = "客户在账期/回款上明显有顾虑，别只讲道理，要给到'具体怎么结'的安排：先结一部分 + 明确时间点，再谈申请账期。"
    elif objection == "price":
        base = "客户在压价。别急着跟着降价，把话题引到'综合成本'上：规格、损耗、配送、账期折算，最好提出小批量试单验证。"
    elif objection == "quality":
        base = "客户担心品质。拿事实和机制说话：批次检测、溯源、规格稳定、损耗数据，再给一个可验证的小批量承诺。"
    elif objection == "delivery":
        base = "客户卡在配送时效。给出可兑现的承诺：配送半径/时效/应急预案，或先用短周期小批量验证到货。"
    elif objection == "supplier":
        base = "客户有稳定老供应商。别贬低对手、别逼单，先做'补充型'定位：多一个备选、先小批量试单，用结果说话。"
    else:
        base = "客户在释放需求信号，多问主销规格、日采购量和最担心的问题，把需求挖具体。"
    if score >= need:
        return "✅ 这轮接得不错：" + base.split("别")[0] + " 对方态度有松动迹象，继续把承诺落到具体动作上。"
    if score == 1:
        return "⚠️ 方向沾边但不够具体：" + base
    return "❌ 这轮没接住客户的话：" + base


# ---------- 客户台词池（规则模式用） ----------
def _objection_opening(objection: str | None, profile: dict) -> str:
    concern = profile.get("concern") or ""
    if objection == "credit":
        return "你们要是能赊账，我倒是可以考虑加点量——不过话说前面，我这边现金周转一直紧。"
    if objection == "price":
        return "我直说了吧，别家给的价格比你们低，我这量又不小，你们能压到多少？"
    if objection == "quality":
        return "说实话，我一直担心你们鲜品的损耗和出成率，之前听同行说过不太稳定。"
    if objection == "delivery":
        return "我们这要货时间卡得死，你们配送能保证准时到吗？晚了耽误我生意。"
    if objection == "supplier":
        return "我们现在供应商合作得挺好的，凭什么换你们？说说你们有什么不一样。"
    return f"我这边{profile.get('type') or '客户'}，最看重的就是{concern or '供货省心'}，你打算怎么跟我合作？"


def _fallback_client_lines(case: dict) -> list[str]:
    profile = {"type": case.get("type"), "concern": case.get("concern")}
    objs = ["credit", "price", "quality", "delivery", "supplier"]
    lines = []
    for o in objs:
        lines.append(_objection_opening(o, profile))
        lines.append(HOLD_LINES[o])
    return lines


# ---------- 会话创建 ----------
def _prune_sessions() -> None:
    now = time.time()
    dead = [k for k, v in SESSIONS.items() if now - v.get("ts", 0) > SESSION_TTL]
    for k in dead:
        SESSIONS.pop(k, None)


def _pick_case(text: str, profile: dict, case_id: str = "") -> dict | None:
    if case_id:
        for c in memory.load_cases():
            if c.get("id") == case_id:
                return c
        # 指定案例不在后端库（可能是本地种子/已删除）→ 用描述文本检索兜底
    hits = memory.search_similar(text or "", profile or {}, top_n=1)
    return hits[0]["case"] if hits else None


def _objection_from_lines(lines: list[str]) -> str | None:
    """只从客户台词里找异议点（避免把 process 正文/经理台词当噪声）。"""
    for line in lines:
        o = classify_objection(line)
        if o:
            return o
    return None


def start_session(text: str = "", profile: dict | None = None, difficulty: str = "实战", case_id: str = "") -> dict:
    """创建演练会话：检索最相似成功案例 → 组装客户人设与台词 → 返回开场。"""
    _prune_sessions()
    difficulty = difficulty if difficulty in DIFFICULTY else "实战"
    case = _pick_case(text or "", profile or {}, case_id)
    if not case:
        return {"ok": False, "reason": "no_case",
                "message": "没找到可参考的成功案例。请先在「拜访助手/成功记忆库」沉淀至少一条案例，或用案例库中的客户名演练。"}
    client_lines, mgr_lines = parse_dialog(case.get("process", ""), case.get("customerName", ""))
    if not client_lines:
        client_lines = _fallback_client_lines(case)
    # 开场白独立出来
    opening = client_lines.pop(0) if client_lines else OPENING_LINES[0]
    # 异议判定优先级：客户台词 -> 描述文本 -> case.concern 映射 -> 开场台词
    objection = _objection_from_lines(client_lines)
    if not objection:
        objection = classify_objection(text or "")
    if not objection:
        objection = CONCERN_OBJECTION.get(case.get("concern") or "")
    if not objection:
        objection = classify_objection(opening)
    cfg = DIFFICULTY[difficulty]
    sid = uuid.uuid4().hex[:10]
    SESSIONS[sid] = {
        "id": sid, "case": case, "difficulty": difficulty,
        "client_lines": client_lines, "mgr_lines": mgr_lines,
        "client_idx": 0, "mgr_idx": 0,
        "objection": objection,
        "mood": cfg["init_mood"], "goal": cfg["goal"], "need": cfg["need"],
        "rounds": 0, "max_rounds": cfg["max_rounds"],
        "history": [{"who": "client", "text": opening}],
        "done": False, "outcome": None,
        "opening": opening, "ts": time.time(),
        "mode": "deepseek" if llm_mod.available() else "rule",
    }
    case_card = {
        "id": case.get("id"), "customerName": case.get("customerName"),
        "type": case.get("type"), "product": case.get("product"),
        "concern": case.get("concern"), "breakthrough": case.get("breakthrough"),
        "result": case.get("result"), "purpose": case.get("purpose"),
        "process": (case.get("process") or "")[:400],
    }
    mode = "deepseek" if llm_mod.available() else "rule"
    return {"ok": True, "session_id": sid, "difficulty": difficulty, "mode": mode,
            "case": case_card, "mood": SESSIONS[sid]["mood"], "goal": cfg["goal"],
            "stage": 0, "stage_label": STAGE_LABEL[0],
            "client_talk": opening,
            "coach_note": "🎭 演练开始。AI 正在扮演" + (case.get("customerName") or "目标客户") +
                          "（" + (case.get("type") or "待确认") + "｜" + (case.get("concern") or "待确认") +
                          "）。原型打法参考：" + (case.get("breakthrough") or "待确认") + "。"}


# ---------- 规则模式单轮 ----------
def _stage_of(rounds: int) -> int:
    if rounds < 1:
        return 0
    if rounds < 2:
        return 1
    if rounds < 5:
        return 2
    return 3


def _rule_reply(sess: dict, user_talk: str, want_hint: bool = False) -> dict:
    case = sess["case"]
    objection = sess["objection"]
    score = defuse_score(user_talk, objection)
    need = sess["need"]
    sess["rounds"] += 1
    rounds = sess["rounds"]

    passed = score >= need
    if passed:
        sess["mood"] += 16 if score >= need + 1 else 10
        mood_note = "客户态度明显松动" if score >= need + 1 else "客户态度松动"
    else:
        sess["mood"] -= 6 if score == 0 else 3
        mood_note = "客户有点失望" if score == 0 else "客户还在观望"

    client_lines = sess["client_lines"]
    idx = sess["client_idx"]
    outcome = None
    done = False
    if sess["mood"] >= sess["goal"]:
        outcome = "win"
        done = True
        client_talk = WIN_LINES[(rounds + sess["mood"]) % len(WIN_LINES)]
        coach_note = "🎉 谈成了！对方已经松口（试单/复购/回款承诺达成）。记得把过程沉淀成新案例，明天复盘还能复用。"
    elif sess["mood"] <= 0 or rounds >= sess["max_rounds"]:
        outcome = "lose"
        done = True
        client_talk = LOSE_LINES[(rounds + sess["mood"]) % len(LOSE_LINES)]
        coach_note = "😅 这单暂时谈崩了。别气馁——回看参考打法，调整策略后点「重新演练」。"
    else:
        if passed and client_lines:
            client_talk = client_lines[idx] if idx < len(client_lines) else SOFT_LINES.get(objection, "……行吧，那先试试。")
            sess["client_idx"] += 1
        elif passed:
            client_talk = SOFT_LINES.get(objection, "……行吧，那先试试。")
        else:
            client_talk = HOLD_LINES.get(objection, "你再想想办法吧，光这样说我没法答应你。")
        coach_note = _coach_note(score, objection, sess["difficulty"], need)

    # 参考话术：优先取案例里经理当时的真实回应
    hint = ""
    if not done:
        hint = pick_mgr_hint(sess)
    if want_hint and not done:
        # 提示不消耗剧情轮次，仅扣一点印象分
        sess["mood"] -= 2
        hint = pick_mgr_hint(sess)
        coach_note = "💡 提示已给：参考话术就在下方，照着意思说一遍，客户会重新给你机会。"

    sess["history"].append({"who": "me", "text": user_talk, "score": score})
    if not done:
        sess["history"].append({"who": "client", "text": client_talk})
    sess["done"] = done
    sess["outcome"] = outcome
    sess["ts"] = time.time()
    return {"ok": True, "client_talk": client_talk, "mood": sess["mood"], "goal": sess["goal"],
            "coach_note": coach_note + ("（" + mood_note + "）" if not done else ""),
            "hint": hint if (want_hint or score < need) else "",
            "done": done, "outcome": outcome,
            "rounds": rounds, "stage": _stage_of(rounds), "stage_label": STAGE_LABEL[_stage_of(rounds)],
            "score": score}


def _reference_talk(objection: str | None, case: dict) -> str:
    bt = case.get("breakthrough") or "小批量试单"
    name = case.get("customerName") or "同类客户"
    if objection == "credit":
        return f"💡 参考打法：参照 {name} 的「{bt}」，把回款拆成可执行计划——先结一部分+明确时间点，账期申请给答复时限，别让客户觉得你在拖。"
    if objection == "price":
        return f"💡 参考打法：参照 {name} 的「{bt}」，不直接拼低价，讲综合成本（损耗/规格/配送/账期折算），提小批量试单验证。"
    if objection == "quality":
        return f"💡 参考打法：参照 {name} 的「{bt}」，给可验证的品质承诺：批次检测/溯源/规格稳定，先小批量试单用数据说话。"
    if objection == "delivery":
        return f"💡 参考打法：参照 {name} 的「{bt}」，明确配送时效与应急方案，先按短周期小批量验证到货稳定性。"
    if objection == "supplier":
        return f"💡 参考打法：参照 {name} 的「{bt}」，做补充型定位：不抢独供，先确认主销规格与日销量，小批量试单再谈放量。"
    return f"💡 参考打法：参照 {name} 的「{bt}」，先确认需求再推进，用小批量验证代替口头承诺。"


# ---------- DeepSeek 模式单轮 ----------
_LLM_SYSTEM = """你是「客户经理话术演练场」的双角色裁判：
1) 扮演客户：人设来自下面给出的【真实成功案例】——沿用该客户的类型、关注点、说话习惯与异议风格，像真实生意人一样回应客户经理的最新一句话。台词要口语化、简短（40-90字），带一点真实商贩的口气，不要机械。
2) 兼任教练：判断客户经理这轮有没有化解客户异议（化解=给出具体动作/承诺，不是空话套话），输出教练点评与参考话术（参考话术要结合案例里的真实打法 breakthrough/process）。

只输出 JSON，不要输出其他文字：
{"client_talk": "客户回应的台词", "mood_delta": 整数(-15到+20，经理化解得好为正), "coach_note": "教练点评(40-90字)", "hint": "参考话术(50-100字，若经理没化解好才给)"}
"""


def _llm_reply(sess: dict, user_talk: str, want_hint: bool = False) -> dict:
    case = sess["case"]
    objection = sess["objection"]
    sess["rounds"] += 1
    rounds = sess["rounds"]
    client_name = case.get("customerName") or "目标客户"
    scene = (
        f"【真实成功案例】客户：{client_name}（{case.get('type')}｜主营{case.get('product')}｜"
        f"关注：{case.get('concern')}｜有效打法：{case.get('breakthrough')}｜结果：{case.get('result')}）\n"
        f"案例过程摘录：{(case.get('process') or '')[:500]}\n"
        f"当前异议点：{objection or '需求挖掘'}\n"
        f"客户当前态度分：{sess['mood']}（100=成交线 {sess['goal']}）\n"
        f"【对话历史】\n" +
        "\n".join(f"{'客户' if h['who'] == 'client' else '客户经理'}：{h['text']}" for h in sess["history"][-8:]) +
        f"\n【客户经理最新回应】{user_talk}\n"
        f"客户经理请求提示：{'是' if want_hint else '否'}"
    )
    try:
        model = llm_mod.get_json_model(temperature=0.85)
        resp = model.invoke([{"role": "system", "content": _LLM_SYSTEM},
                             {"role": "user", "content": scene}])
        data = json.loads(resp.content or "{}")
        delta = int(data.get("mood_delta") or 0)
        delta = max(-15, min(20, delta))
        sess["mood"] += delta
        client_talk = data.get("client_talk") or "……（客户沉默了一下）你继续说。"
        coach_note = data.get("coach_note") or "这轮先看客户反应。"
        hint = data.get("hint") or ""
        done = sess["mood"] >= sess["goal"] or sess["mood"] <= 0 or rounds >= sess["max_rounds"]
        outcome = "win" if sess["mood"] >= sess["goal"] else ("lose" if (sess["mood"] <= 0 or rounds >= sess["max_rounds"]) else None)
        if outcome == "win":
            client_talk = "行，那就先按你说的试一批吧，走货好再谈后面。"
        elif outcome == "lose":
            client_talk = "先这样吧，我再想想。你也回去合计合计，过两天再说。"
        sess["history"].append({"who": "me", "text": user_talk})
        if not done:
            sess["history"].append({"who": "client", "text": client_talk})
        sess["done"] = done
        sess["outcome"] = outcome
        sess["ts"] = time.time()
        return {"ok": True, "client_talk": client_talk, "mood": sess["mood"], "goal": sess["goal"],
                "coach_note": coach_note, "hint": hint if (want_hint or delta <= 3) else "",
                "done": done, "outcome": outcome, "rounds": rounds,
                "stage": _stage_of(rounds), "stage_label": STAGE_LABEL[_stage_of(rounds)], "score": None}
    except Exception as exc:  # noqa: BLE001
        sess["mode"] = "rule"
        return _rule_reply(sess, user_talk, want_hint)


# ---------- 对外接口 ----------
def reply(session_id: str, user_talk: str, want_hint: bool = False) -> dict:
    sess = SESSIONS.get(session_id)
    if not sess:
        return {"ok": False, "reason": "no_session", "message": "演练会话不存在或已过期，请重新开始演练。"}
    if sess.get("done"):
        return {"ok": False, "reason": "done", "message": "本场演练已结束，点击「重新演练」再来一局。", "outcome": sess.get("outcome")}
    user_talk = (user_talk or "").strip()
    if not user_talk and not want_hint:
        return {"ok": False, "reason": "empty", "message": "先输入你对客户说的话。"}
    if not user_talk and want_hint:
        return _give_hint(sess)
    if sess.get("mode") == "deepseek" and llm_mod.available():
        return _llm_reply(sess, user_talk, want_hint)
    return _rule_reply(sess, user_talk, want_hint)


def _give_hint(sess: dict) -> dict:
    hint = pick_mgr_hint(sess)
    sess["mood"] = max(0, sess["mood"] - 2)
    return {"ok": True, "client_talk": "", "mood": sess["mood"], "goal": sess["goal"],
            "coach_note": "💡 参考话术已给出，照着说一轮，客户会重新给你机会。", "hint": hint,
            "done": False, "outcome": None, "rounds": sess["rounds"],
            "stage": _stage_of(sess["rounds"]), "stage_label": STAGE_LABEL[_stage_of(sess["rounds"])], "score": None}


def _best_move(sess: dict) -> str:
    """从历史里找客户经理最有效的化解话术（规则模式带 score，越高越优先；LLM 模式取最后一句实质台词）。"""
    best, best_score = "", 0
    for h in sess.get("history") or []:
        if h.get("who") == "me" and (h.get("score") or 0) > best_score:
            best, best_score = h.get("text", ""), int(h.get("score") or 0)
    if best:
        return best
    for h in reversed(sess.get("history") or []):
        if h.get("who") == "me" and len(str(h.get("text") or "")) > 8:
            return str(h["text"])
    return sess.get("case", {}).get("breakthrough") or "小批量试单验证"


def harvest(session_id: str) -> dict:
    """把一场演练沉淀为成功案例草稿（用户确认后由前端调 /api/cases 入库）。"""
    sess = SESSIONS.get(session_id)
    if not sess:
        return {"ok": False, "reason": "no_session", "message": "演练会话不存在或已过期，无法沉淀，请重新演练后再试。"}
    case = sess.get("case") or {}
    outcome = sess.get("outcome")
    if outcome != "win":
        return {"ok": False, "reason": "not_win",
                "message": "本场还没谈成。建议先点「重新演练」把客户聊到松口，再沉淀成成功案例（失败场次更适合存本地记录当反面教材回放）。"}
    history = sess.get("history") or []
    lines = []
    for h in history:
        who = "客户" if h.get("who") == "client" else "客户经理"
        text = str(h.get("text") or "").strip()
        if text:
            lines.append(f"{who}：{text}")
    process = "\n".join(lines[-24:])
    best = _best_move(sess)
    src_name = case.get("customerName") or "目标客户"
    draft = {
        "customerName": f"演练·{src_name}",
        "type": case.get("type") or "待确认",
        "product": case.get("product") or "待确认",
        "concern": case.get("concern") or "待确认",
        "breakthrough": (case.get("breakthrough") or "小批量试单验证") + "（演练验证有效）",
        "result": "演练成交（客户松口：试单/复购/回款意向，待实地验证）",
        "purpose": case.get("purpose") or "话术演练",
        "actions": [{"title": (case.get("breakthrough") or "小批量试单验证") + "：" + best[:50],
                       "raw": process[:300], "deadline": "下次拜访", "owner": "客户经理", "priority": "P2"}],
        "process": process,
        "source": "话术演练沉淀",
        "sourceSession": session_id,
        "rounds": sess.get("rounds") or 0,
        "difficulty": sess.get("difficulty") or "实战",
    }
    return {"ok": True, "draft": draft, "outcome": outcome, "rounds": sess.get("rounds") or 0,
            "mode": sess.get("mode") or "rule", "mood": sess.get("mood") or 0}
