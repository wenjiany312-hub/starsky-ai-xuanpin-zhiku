"""
社媒 VOC 前导选品（Reddit × 亚马逊三重验证）——确定性部分。

核心思路：
  社媒是前导（需求的表达），平台数据是滞后（需求的结果）；社媒提出假设，平台数据裁决。
  三条同时成立才算蓝海：① 声量存在且在涨 ② 痛点集中且现有产品不解决 ③ 亚马逊需求增长且格局未锁死。

本文件纯计算，不联网：清洗 / 词典聚合 / 三道门判定。取数在 voc_source，调度与 LLM 标注在 voc_service。
"""
import datetime
import re
from collections import Counter
from typing import Any, Dict, Iterable, List, Optional

# ---------------------------------------------------------------- 词典
# 痛点词：用户在骂 / 产品没解决。故意偏宽（宁可放进来让 LLM 标注兜底，也不要误杀真信号）
PAIN_RE = re.compile(
    r"\b(leak(s|ed|ing|y)?|smell(s|ed|y|ing)?|stink(s|y)?|broke(n)?|break(s|ing)?|fell apart|falls apart|"
    r"doesn'?t (work|fit|last|hold)|didn'?t (work|fit|last)|does not (work|fit|last)|won'?t (work|fit|stay|stop)|"
    r"waste of (money|time)|wasted|useless|junk|cheap(ly made)?|flimsy|defective|returned|return(ing)? it|"
    r"disappoint(ed|ing)?|terrible|awful|horrible|worst|hate|hated|annoying|frustrat(ed|ing)|struggl(e|es|ed|ing)|"
    r"too (small|big|loud|heavy|tight|short|long|hard|flimsy)|escape(s|d)?|chew(ed|s)? (through|up)|"
    r"fell off|came off|slips?|rust(ed|s|y)?|mold(y)?|stain(s|ed)?|noisy|stuck|jam(med|s)?|clog(s|ged)?|"
    r"can'?t find|cannot find|looking for (something|a better)|any(one)? (know|have) (a|any) (better|alternative)|"
    r"scared|afraid|worried|unsafe|dangerous|toxic|recall(ed)?|allerg(y|ic))\b", re.I)

# 推荐词：口碑好 / 已验证有效
RECO_RE = re.compile(
    r"\b(recommend(ed|s)?|worth it|worth every|game ?changer|life ?saver|lifesaver|love(d|s)? (it|this|my)|"
    r"works? (great|well|perfectly)|best (purchase|thing|investment)|holy grail|swear by|highly rate|"
    r"changed (my|our) life|must[- ]have|10/10|so happy with|obsessed)\b", re.I)

_JUNK = {"[deleted]", "[removed]", ""}
_BOTS = {"automoderator", "bot", "remindmebot"}
_STOP = {"for", "the", "and", "with", "of", "in", "to", "a", "an", "on", "best", "new", "set", "pack", "pcs", "pc"}


def _stem(w: str) -> str:
    w = w.lower()
    for suf in ("ies", "es", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            return w[: -len(suf)] + ("y" if suf == "ies" else "")
    return w


def keyword_tokens(keyword: str) -> List[str]:
    toks = [_stem(t) for t in re.findall(r"[A-Za-z0-9']+", keyword or "")]
    return [t for t in toks if len(t) > 2 and t not in _STOP]


def mentions(text: str, keyword: str) -> bool:
    """这条讨论是不是在说这个产品：≤2 个核心词全中，更多时中 60% 以上（与终端 Reddit 判相关同口径）。"""
    core = keyword_tokens(keyword)
    if not core:
        return True
    low = (text or "").lower()
    hit = sum(1 for t in core if re.search(r"(?<![a-z0-9])" + re.escape(t), low))
    need = len(core) if len(core) <= 2 else -(-len(core) * 6 // 10)
    return hit >= need


# ---------------------------------------------------------------- 清洗（ETL 四道过滤）

def doc_text(d: Dict[str, Any]) -> str:
    if d.get("type") == "comment" or "body" in d:
        return str(d.get("body") or "")
    return f"{d.get('title') or ''}\n{d.get('selftext') or d.get('text') or ''}".strip()


def normalize(raw: Dict[str, Any], kind: str, subreddit: str) -> Dict[str, Any]:
    if kind == "posts":
        text = f"{raw.get('title') or ''}\n{raw.get('selftext') or ''}".strip()
    else:
        text = str(raw.get("body") or "")
    return {"id": raw.get("id"), "type": "post" if kind == "posts" else "comment", "subreddit": subreddit,
            "text": text, "title": raw.get("title") or "", "score": int(raw.get("score") or 0),
            "created_utc": int(raw.get("created_utc") or 0), "author": raw.get("author") or "",
            "num_comments": int(raw.get("num_comments") or 0),
            "permalink": raw.get("permalink") or ""}


def _ascii_ratio(s: str) -> float:
    return sum(1 for c in s if ord(c) < 128) / len(s) if s else 0.0


def etl(docs: Iterable[Dict[str, Any]], keyword_regex: Optional[re.Pattern] = None,
        min_len: int = 25) -> Dict[str, Any]:
    """去重 → 删无效（已删除/过短/机器人）→ 语言（ASCII>90%）→ 相关性（可选）。返回干净语料与各关卡留存。"""
    stats = {"raw": 0, "dedup": 0, "valid": 0, "english": 0, "relevant": 0}
    seen = set()
    out: List[Dict[str, Any]] = []
    for d in docs:
        stats["raw"] += 1
        did = d.get("id") or str(stats["raw"])
        if did in seen:
            continue
        seen.add(did)
        stats["dedup"] += 1
        t = d.get("text") or ""
        au = str(d.get("author") or "").lower()
        if t.strip() in _JUNK or t.strip().lower() in ("[deleted]", "[removed]") or len(t.strip()) < min_len \
                or au in _BOTS or au == "automoderator":
            continue
        stats["valid"] += 1
        if _ascii_ratio(t) <= 0.9:
            continue
        stats["english"] += 1
        if keyword_regex is not None and not keyword_regex.search(t):
            continue
        stats["relevant"] += 1
        out.append(d)
    stats["retention"] = round(stats["relevant"] / stats["raw"], 3) if stats["raw"] else None
    return {"docs": out, "stats": stats}


# ---------------------------------------------------------------- 聚合

def month_key(ts: int) -> str:
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m")


def scan_text(text: str) -> Dict[str, bool]:
    return {"pain": bool(PAIN_RE.search(text)), "reco": bool(RECO_RE.search(text))}


def aggregate(docs: List[Dict[str, Any]], products: Dict[str, str]) -> List[Dict[str, Any]]:
    """全量词典聚合：每个品类 提及量 / 痛点率 / 推荐率 / 月度趋势。products: {品类id: 正则串}。"""
    compiled = {k: re.compile(v, re.I) for k, v in products.items()}
    rows: Dict[str, Dict[str, Any]] = {k: {"id": k, "mentions": 0, "pain": 0, "reco": 0,
                                          "months": Counter(), "pain_docs": []} for k in compiled}
    totals: Counter = Counter()
    for d in docs:
        if d.get("created_utc"):
            totals[month_key(d["created_utc"])] += 1
        t = d.get("text") or ""
        flags = None
        for k, rx in compiled.items():
            if rx.search(t):
                if flags is None:
                    flags = scan_text(t)
                r = rows[k]
                r["mentions"] += 1
                r["pain"] += flags["pain"]
                r["reco"] += flags["reco"]
                if d.get("created_utc"):
                    r["months"][month_key(d["created_utc"])] += 1
                if flags["pain"]:
                    r["pain_docs"].append(d)
    out = []
    for k, r in rows.items():
        n = r["mentions"]
        months = dict(sorted(r["months"].items()))
        share = {m: months.get(m, 0) / totals[m] for m in sorted(totals) if totals[m] >= 30}
        out.append({"id": k, "mentions": n,
                    "pain_rate": round(r["pain"] / n, 3) if n else None,
                    "reco_rate": round(r["reco"] / n, 3) if n else None,
                    "trend_pct": half_split_trend(share),
                    "months": months,
                    "pain_docs": sorted(r["pain_docs"], key=lambda x: -(x.get("score") or 0))})
    return sorted(out, key=lambda x: -x["mentions"])


def half_split_trend(months: Dict[str, float]) -> Optional[float]:
    """后半段月均 ÷ 前半段月均 − 1（百分数）。样本不足两个月返回 None。"""
    keys = sorted(months)
    if len(keys) < 2:
        return None
    mid = len(keys) // 2
    a = sum(months[k] for k in keys[:mid]) / max(mid, 1)
    b = sum(months[k] for k in keys[mid:]) / max(len(keys) - mid, 1)
    if a <= 0:
        return None
    return round((b / a - 1) * 100, 1)


def top_terms(docs: List[Dict[str, Any]], n: int = 8) -> List[Dict[str, Any]]:
    """统计痛点词命中的具体词形。"""
    c: Counter = Counter()
    for d in docs:
        for m in PAIN_RE.finditer(d.get("text") or ""):
            c[m.group(0).lower()] += 1
    tot = sum(c.values()) or 1
    return [{"term": k, "count": v, "share": round(v / tot, 3)} for k, v in c.most_common(n)]


def pick_pain_samples(docs: List[Dict[str, Any]], n: int = 40, max_chars: int = 600) -> List[Dict[str, Any]]:
    """痛点词共现 × 高赞排序取前 n 条，交给 LLM 深读。"""
    ranked = sorted(docs, key=lambda d: -(d.get("score") or 0))
    out = []
    for d in ranked[:n]:
        out.append({"id": d.get("id"), "sub": d.get("subreddit"), "score": d.get("score"),
                    "text": (d.get("text") or "")[:max_chars]})
    return out


ANNOTATE_PROMPT = (
    "你是亚马逊选品分析师。下面是 Reddit 上与「{kw}」相关、带抱怨/求助的真实讨论（英文）。"
    "请读完后归纳，只输出一个 JSON 对象，不要任何解释：\n"
    '{{"pain_points":[{{"name":"中文痛点名","pct":占样本百分比整数,"detail":"一句话说明","quote":"英文原话（原文摘录，不得改写）"}}],'
    '"unmet_needs":["现有产品没满足的需求"],"brand_complaints":[{{"brand":"品牌","issue":"问题"}}],'
    '"opportunity":"差异化机会判断，一两句"}}\n'
    "要求：pain_points 3-6 条按占比降序；pct 只统计样本里明确提到的，加总可小于 100；"
    "quote 必须是样本里真实出现的原话；没有就写空数组，不得编造。\n样本：\n"
)


def parse_annotation(obj: Any) -> Optional[Dict[str, Any]]:
    """校验 LLM 标注：结构不对就当没有，绝不拿残缺 JSON 冒充结论。"""
    if not isinstance(obj, dict) or not isinstance(obj.get("pain_points"), list):
        return None
    pts = []
    for p in obj["pain_points"][:8]:
        if isinstance(p, dict) and p.get("name"):
            try:
                pct = float(p.get("pct"))
            except (TypeError, ValueError):
                pct = None
            pts.append({"name": str(p["name"])[:40], "pct": pct, "detail": str(p.get("detail") or "")[:200],
                        "quote": str(p.get("quote") or "")[:300]})
    if not pts:
        return None
    return {"pain_points": pts,
            "unmet_needs": [str(x)[:120] for x in (obj.get("unmet_needs") or []) if x][:6],
            "brand_complaints": [b for b in (obj.get("brand_complaints") or []) if isinstance(b, dict)][:6],
            "opportunity": str(obj.get("opportunity") or "")[:400]}


# ---------------------------------------------------------------- 三道门

DEFAULTS: Dict[str, float] = {
    "min_mentions": 15,          # 讨论样本数下限（声量真实存在）
    "fail_mentions": 5,          # 低于此 = 没人在聊
    "trend_pass_min": 0.0,       # 讨论趋势（%）≥ 0 才算在涨（如果有趋势数据）
    "trend_fail_below": -30.0,   # 低于此 = 退潮
    "pain_rate_pass_min": 0.20,  # 痛点率（词典口径）
    "pain_rate_warn_min": 0.12,
    "top_pain_pct_pass_min": 25.0,   # LLM 深读：最大单一痛点占比
    "platform_trend_pass_min": 0.0,  # 亚马逊搜索量同比 %
    "platform_trend_fail_below": -10.0,
    "platform_top3_pass_max": 0.45,  # Top3 点击集中度
    "platform_top3_fail": 0.60,      # ≥ 此 = 格局已锁死
    "platform_min_weekly": 1000,
    "platform_fail_weekly": 300,     # 低于此 = 需求不走亚马逊
}

GATE_NAMES = ("声量门", "痛点门", "平台门")


def thresholds(t: Optional[Dict[str, Any]] = None) -> Dict[str, float]:
    out = dict(DEFAULTS)
    g = (t or {}).get("voc") if isinstance(t, dict) else None
    if isinstance(g, dict):
        for k, v in g.items():
            if k in out and isinstance(v, (int, float)) and not isinstance(v, bool):
                out[k] = float(v)
    return out


def _num(v: Any) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _g(i: int, name: str, status: str, value: str, threshold: str, note: str = "", action: str = "") -> Dict[str, Any]:
    return {"index": i, "name": name, "status": status, "value": value, "threshold": threshold,
            "note": note, "action": action}


def decide(facts: Dict[str, Any], t: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """三道门定档。fail 优先（证伪与证实一样值钱），其次 unknown→暂缓，其次 warn→有条件做，全过才「做」。"""
    th = thresholds(t)
    lead = facts.get("voc_lead") or {}
    gates: List[Dict[str, Any]] = []

    # ① 声量门：需求的「表达」真实存在，且没有退潮
    n = _num(lead.get("mentions"))
    tr = _num(lead.get("trend_pct"))
    if not lead.get("ok") or n is None:
        gates.append(_g(1, "声量门", "unknown", "—", f"≥{th['min_mentions']:g} 条讨论",
                        lead.get("error") or "Reddit 取数没跑通", "检查社媒数据源或扩宽关键词"))
    else:
        trend_txt = f"{tr:+.0f}%" if tr is not None else ""
        val = f"{int(n)} 条讨论" + (f" ｜ 注意力趋势 {trend_txt}" if trend_txt else "")
        thr = f"≥{th['min_mentions']:g} 条" + (f" ｜ 趋势≥{th['trend_pass_min']:g}%" if tr is not None else "")
        if n < th["fail_mentions"]:
            gates.append(_g(1, "声量门", "fail", val, thr, "社媒几乎无讨论：需求缺乏前导表达", "换更口语的英文词，或确认这是否为伪需求"))
        elif tr is not None and tr < th["trend_fail_below"]:
            gates.append(_g(1, "声量门", "fail", val, thr, "社媒讨论明显退潮：赛道热度降温", "别追，列入证伪记录"))
        elif n >= th["min_mentions"]:
            if tr is not None and tr < th["trend_pass_min"]:
                gates.append(_g(1, "声量门", "warn", val, thr, "声量充足但趋势持平略降", "持续观察近两月声量变化"))
            else:
                gates.append(_g(1, "声量门", "pass", val, thr, "声量真实存在，前导需求明确"))
        else:
            gates.append(_g(1, "声量门", "warn", val, thr, "样本偏少（5~15条），信号略弱", "建议扩大版块范围或补看同类词"))

    # ② 痛点门：用户在骂，且集中
    pr = _num(lead.get("pain_rate"))
    top = _num(lead.get("top_pain_pct"))
    top_name = lead.get("top_pain_name") or ""
    if not lead.get("ok") or pr is None:
        gates.append(_g(2, "痛点门", "unknown", "—", f"痛点率≥{th['pain_rate_pass_min']:.0%} 或最大痛点≥{th['top_pain_pct_pass_min']:g}%",
                        "没有可分析的讨论", ""))
    else:
        val = f"痛点率 {pr:.0%}" + (f" ｜ 最大痛点 {top:g}%（{top_name}）" if top is not None else " ｜ 未做 AI 深读")
        thr = f"痛点率≥{th['pain_rate_pass_min']:.0%} 或最大痛点≥{th['top_pain_pct_pass_min']:g}%"
        if (top is not None and top >= th["top_pain_pct_pass_min"]) or pr >= th["pain_rate_pass_min"]:
            note = f"痛点集中度高（{top_name or '抱怨集中'}），现有产品存在改良缝隙" if top is not None else "词典痛点率达标，存在普遍不满"
            gates.append(_g(2, "痛点门", "pass", val, thr, note))
        elif pr >= th["pain_rate_warn_min"]:
            gates.append(_g(2, "痛点门", "warn", val, thr, "有抱怨但不集中", "深入阅读买家原话，看能否聚焦出单点突破口"))
        else:
            gates.append(_g(2, "痛点门", "fail", val, thr, "极少抱怨：现有产品满意度高，缺乏差异化空间", "寻找其他切入场景，或放弃"))

    # ③ 平台门：亚马逊数据裁决（需求在涨且格局没锁死）
    wk = _num(facts.get("search_volume_weekly"))
    ptr = _num(facts.get("search_volume_trend_pct"))
    t3 = _num(facts.get("top3_click_share"))
    parts = []
    if wk is not None:
        parts.append(f"周搜 {wk:,.0f}")
    if ptr is not None:
        parts.append(f"同比 {ptr:+.0f}%")
    if t3 is not None:
        parts.append(f"Top3 {t3:.0%}")
    val = " ｜ ".join(parts) or "—"
    thr = (f"周搜≥{th['platform_min_weekly']:g}、趋势≥{th['platform_trend_pass_min']:g}%、"
           f"Top3<{th['platform_top3_pass_max']:.0%}")
    fails = []
    if wk is not None and wk < th["platform_fail_weekly"]:
        fails.append(f"周搜仅 {wk:,.0f}：需求不走亚马逊平台")
    if ptr is not None and ptr < th["platform_trend_fail_below"]:
        fails.append(f"搜索量同比 {ptr:+.0f}%：平台赛道萎缩/退潮")
    if t3 is not None and t3 >= th["platform_top3_fail"]:
        fails.append(f"Top3 集中度 {t3:.0%}：头部大牌已垄断")
    if wk is None and ptr is None and t3 is None:
        gates.append(_g(3, "平台门", "unknown", "—", thr, "亚马逊数据（SIF/西柚）未获取，社媒假设无法交叉验证",
                        "接通 SIF 或西柚数据源后重新验证"))
    elif fails:
        gates.append(_g(3, "平台门", "fail", val, thr, "；".join(fails), "社媒热但平台不买账：列入证伪记录"))
    else:
        missing = [n for n, v in (("周搜索量", wk), ("搜索趋势", ptr), ("Top3 集中度", t3)) if v is None]
        ok = ((wk is not None and wk >= th["platform_min_weekly"]) and
              (ptr is not None and ptr >= th["platform_trend_pass_min"]) and
              (t3 is not None and t3 < th["platform_top3_pass_max"]))
        if ok:
            gates.append(_g(3, "平台门", "pass", val, thr, "需求保持增长，竞争格局未锁死，平台验证通过"))
        elif missing:
            gates.append(_g(3, "平台门", "unknown", val, thr, "待补：" + "、".join(missing), "补齐平台指标后裁决"))
        else:
            gates.append(_g(3, "平台门", "warn", val, thr, "指标未完全过通过线，需关注竞争格局或趋势"))

    failed = [g for g in gates if g["status"] == "fail"]
    unknown = [g for g in gates if g["status"] == "unknown"]
    warn = [g for g in gates if g["status"] == "warn"]
    if failed:
        verdict, why = "不做", "；".join(f"{g['name']}未通过：{g['note']}" for g in failed)
    elif unknown:
        verdict, why = "暂缓", "；".join(f"{g['name']}待补：{g['note']}" for g in unknown)
    elif warn:
        verdict, why = "有条件做", "；".join(f"{g['name']}需关注：{g['note']}" for g in warn)
    else:
        verdict, why = "做", "三重验证全部通过：社媒声量真实且在涨、痛点集中且现有竞品未满足、亚马逊需求增长且格局未锁死"
    todo = [g["action"] for g in gates if g["status"] in ("fail", "unknown", "warn") and g.get("action")]
    todo.append("把痛点翻成产品改良点和 listing 卖点；核对供应链打样价与毛利率")
    return {"mode": "voc", "method": "社媒 VOC 前导选品（三重验证）", "gates": gates,
            "conclusion": verdict, "verdict": verdict, "reason": why,
            "failed": [g["name"] for g in failed], "unknown": [g["name"] for g in unknown], "todo": todo}
