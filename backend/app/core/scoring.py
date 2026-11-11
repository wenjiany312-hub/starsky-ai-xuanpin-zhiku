"""
五维打分 + 四档结论内核（确定性）。

判定顺序固定：数据覆盖 → 硬闸门 → 加权总分 → 降档修正。
模型只负责写叙述，不能改判定；模型失败 → 结论标“未研判”，避免拿模板糊弄人。
"""
from typing import Any, Dict, List, Optional

from app.core import rules as rules_mod
from app.core.rules import FAIL, NO_DATA, PASS, WARN, RuleOutcome

AXES = ("competition", "differentiation", "margin_barrier", "market_capacity", "trend")
AXIS_LABELS = {
    "market_capacity": "市场容量",
    "competition": "竞争强度",
    "trend": "趋势走向",
    "differentiation": "差异化空间",
    "margin_barrier": "利润与供应链",
}
DEFAULT_WEIGHTS = {
    "market_capacity": 0.25,
    "competition": 0.25,
    "trend": 0.20,
    "differentiation": 0.20,
    "margin_barrier": 0.10,
}
VERDICTS = ("做", "有条件做", "暂缓", "不做")
TIER_KEY = {"做": "go", "有条件做": "conditional", "暂缓": "defer", "不做": "no_go", "未研判": "unknown"}

_RULE_SCORE = {PASS: 4.2, WARN: 2.8, FAIL: 1.4}


def _clamp(v: float, lo: float = 1.0, hi: float = 5.0) -> float:
    return round(max(lo, min(hi, v)), 1)


def _num(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- 五维

def score_axes(outcomes: List[RuleOutcome], facts: Dict[str, Any],
               weights: Optional[Dict[str, float]] = None) -> Dict[str, Dict[str, Any]]:
    by_axis: Dict[str, List[RuleOutcome]] = {}
    for o in outcomes:
        if o.status in (PASS, WARN, FAIL):
            by_axis.setdefault(o.axis, []).append(o)

    out: Dict[str, Dict[str, Any]] = {}
    for axis in AXES:
        items = by_axis.get(axis) or []
        if items:
            base = sum(_RULE_SCORE[o.status] for o in items) / len(items)
            basis = "规则：" + "、".join(f"{o.id}={o.status}" for o in items)
            confidence = "高" if all(o.status != NO_DATA for o in items) else "中"
        else:
            base, basis, confidence = None, "无可评估规则", "低"
        out[axis] = {"score": base, "basis": basis, "confidence": confidence,
                     "rules": [o.id for o in items], "weight": (weights or DEFAULT_WEIGHTS).get(axis, 0)}

    # 事实层的幅度修正（只在有规则分时生效，避免无凭据加分）
    vol = _num(facts.get("search_volume_weekly"))
    if out["market_capacity"]["score"] is not None and vol is not None:
        s = out["market_capacity"]["score"]
        if vol >= 5000:
            s += 0.5
        elif vol >= 1500:
            s += 0.3
        elif vol < 100:
            s -= 0.4
        out["market_capacity"]["score"] = _clamp(s)
        out["market_capacity"]["basis"] += f"；周搜索量 {int(vol)}"

    weeks = _num(facts.get("weeks_to_peak"))
    if out["trend"]["score"] is not None and weeks is not None:
        s = out["trend"]["score"]
        if 4 <= weeks <= 20:
            s += 0.3
            out["trend"]["basis"] += f"；距旺季 {int(weeks)} 周（窗口临近）"
        elif weeks > 30:
            s -= 0.2
            out["trend"]["basis"] += f"；距旺季 {int(weeks)} 周（资金压得久）"
        out["trend"]["score"] = _clamp(s)

    young = _num(facts.get("young_listing_share"))
    if out["competition"]["score"] is not None and young is not None:
        s = out["competition"]["score"]
        if young >= 0.5:
            s += 0.4
        elif young <= 0.1:
            s -= 0.3
        out["competition"]["score"] = _clamp(s)
        out["competition"]["basis"] += f"；新品占比 {young:.0%}"

    margin = _num(facts.get("gross_margin_pct"))
    if out["margin_barrier"]["score"] is not None and margin is not None:
        s = out["margin_barrier"]["score"]
        if margin >= 45:
            s += 0.5
        elif margin < 20:
            s -= 0.4
        out["margin_barrier"]["score"] = _clamp(s)
        out["margin_barrier"]["basis"] += f"；毛利率 {margin:.1f}%"

    out["differentiation"] = _differentiation(facts)
    return out


def _differentiation(facts: Dict[str, Any]) -> Dict[str, Any]:
    """
    差异化空间没有直接数据源（需要 VOC / 评论痛点），这里只用可观测代理指标，
    并明确标注“代理指标”，方便前端提示用户补评论数据或交给方法论包细化。
    """
    score, notes = 3.0, []
    spread = _num(facts.get("price_iqr_ratio"))
    if spread is not None:
        if spread >= 0.6:
            score += 0.6
            notes.append(f"价格带分散（四分位跨度 {spread:.2f}）")
        elif spread <= 0.2:
            score -= 0.4
            notes.append("价格带高度集中，只剩同质竞争")
    young = _num(facts.get("young_listing_share"))
    if young is not None:
        if young >= 0.3:
            score += 0.4
            notes.append(f"头部有 {young:.0%} 新链接成功")
        elif young <= 0.1:
            score -= 0.3
            notes.append("头部几乎全是老链接")
    reviews = _num(facts.get("median_reviews"))
    if reviews is not None:
        if reviews <= 100:
            score += 0.4
            notes.append("评论门槛浅，可用内容和图差异化突围")
        elif reviews >= 800:
            score -= 0.3
            notes.append("评论墙高，差异化需要更强证据")
    if facts.get("has_voc"):
        notes.append("含评论/VOC 证据")
        score += 0.3
    if not notes:
        # 一个代理指标都没拿到时，3.0 是凭空给的分，会让人把“不知道”读成“中性”。
        # 干脆不计分，由加权总分按缺项处理（axis_coverage 下降 → 置信度如实下降）。
        return {
            "score": None,
            "basis": "代理指标不足（缺价格带宽度/新链接占比/评论门槛/VOC），本项不计分",
            "confidence": "低（无代理指标）",
            "rules": [],
            "weight": DEFAULT_WEIGHTS["differentiation"],
        }
    return {
        "score": _clamp(score),
        "basis": "代理指标：" + "；".join(notes),
        "confidence": "低（代理指标）" if facts.get("has_voc") else "低（代理指标，建议补 VOC）",
        "rules": [],
        "weight": DEFAULT_WEIGHTS["differentiation"],
    }


# ---------------------------------------------------------------- 结论

def _hard_blockers(outcomes: List[RuleOutcome], facts: Dict[str, Any], t: Dict[str, Any]) -> List[Dict[str, Any]]:
    """硬闸门：命中任一条即“不做”。阈值取自 facts 原值与阈值表，不依赖 PASS/FAIL 粒度。"""
    blockers: List[Dict[str, Any]] = []
    comp, margin = t["competition"], t["margin"]

    share = _num(facts.get("top3_click_share"))
    if share is not None and share > comp["top3_click_share_fatal"]:
        blockers.append({"rule": "R03", "why": f"Top3 点击集中度 {share:.0%} 超过致命线 {comp['top3_click_share_fatal']:.0%}"})

    # FBA 按 $4.5、头程按采购价 35% 估出来的毛利，大件/抛货会偏差很大——不配一票否决
    estimated = bool(facts.get("economics_estimated"))
    gross = _num(facts.get("gross_margin_pct"))
    if gross is not None and gross < margin["gross_margin_fatal_below"] and not estimated:
        blockers.append({"rule": "R11", "why": f"毛利率 {gross:.1f}% 低于生存线 {margin['gross_margin_fatal_below']}%"})

    headroom = _num(facts.get("ceiling_headroom"))
    if headroom is not None and headroom < 1.0 and not estimated:
        blockers.append({"rule": "R12", "why": f"实际采购价已高于成本上限（余量 {headroom:.2f} 倍）"})

    band = _num(facts.get("review_band_difficulty_pct"))
    natural = _num(facts.get("natural_share"))
    if band is not None and band < _num(comp["review_band_warn_min"]) and \
            natural is not None and natural < _num(t["ad"]["natural_share_pass_min"]):
        blockers.append({"rule": "R14", "why": f"评论区间难度 {band}% 属“困难”，且广告效率法也未通过（头部自然流量仅 {natural:.0%}）"})

    vol = _num(facts.get("search_volume_weekly"))
    if vol is not None and vol < _num(t["demand"]["warn_weekly_search_volume"]):
        blockers.append({"rule": "R01", "why": f"周搜索量 {int(vol)} 低于可做底盘 {t['demand']['warn_weekly_search_volume']}"})
    return blockers


def weighted_total(axes: Dict[str, Dict[str, Any]], weights: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
    w = dict(weights or DEFAULT_WEIGHTS)
    avail = {k: v for k, v in axes.items() if v.get("score") is not None and w.get(k, 0) > 0}
    wsum = sum(w[k] for k in avail)
    if not avail or wsum <= 0:
        # points 也要给 None：decide() 会无条件读它，缺键就是「一点数据都没取到」时整个运行崩掉
        return {"score": None, "points": None, "weighted": None, "axis_coverage": 0.0, "available": []}
    weighted = sum(axes[k]["score"] * w[k] for k in avail) / wsum
    return {
        "score": round(weighted, 2),                 # 1–5
        "points": round((weighted - 1) / 4 * 100, 1),  # 0–100，便于展示
        "weighted": {k: round(w[k] / wsum, 3) for k in avail},
        "axis_coverage": round(wsum, 3),
        "available": sorted(avail.keys()),
    }


def decide(facts: Dict[str, Any], outcomes: List[RuleOutcome], axes: Dict[str, Dict[str, Any]],
           evidence_ratio: float = 1.0, weights: Optional[Dict[str, float]] = None,
           t: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    t = t or rules_mod.thresholds()
    v = t["verdict"]
    summary = rules_mod.summarize(outcomes)
    # 覆盖率以「规则能不能判」为主：证据取回来了，但大部分规则因为缺关键字段判不了，
    # 就说明这份数据撑不起结论。原来 50/50 平均会让「证据 6/7 成功、规则只过 19%」
    # 算出 52% 覆盖率，进而给出「有条件做」——数字都是空的，结论却是成立的，这是假结论。
    # 现在：规则全过、证据全到 = 100%；规则只过 19% 时最高也就 19%，直接落到「未研判」。
    rule_cov = max(0.0, min(1.0, summary["coverage"]))
    evidence_cov = max(0.0, min(1.0, evidence_ratio))
    coverage = round(rule_cov * (0.5 + 0.5 * evidence_cov), 3)
    hard = _hard_blockers(outcomes, facts, t)
    total = weighted_total(axes, weights)

    if total["score"] is None or coverage < v["coverage_fatal_min"]:
        # 不能给「暂缓」：新用户会读成「这个品不行」，其实是数据源没接上、根本没判断
        verdict, reason = "未研判", "有效数据不足，没有形成结论（多半是数据源未连接或关键词没取到数，请先到「数据源」页检查）"
    elif hard:
        verdict, reason = "不做", "触发硬闸门：" + "；".join(b["why"] for b in hard)
    elif coverage < v["coverage_min"]:
        verdict, reason = "暂缓", f"数据覆盖 {coverage:.0%} 低于 {v['coverage_min']:.0%}，结论稳定性不够"
    else:
        pts = total["points"]
        # 起点只看分数；软性未过项在下面统一降一档。原来这里还要求「没有未过项才给做」，
        # 加上下面的降档，等于同一条未过项罚了两档
        if pts >= v["do_min"]:
            verdict = "做"
        elif pts >= v["conditional_min"]:
            verdict = "有条件做"
        elif pts >= v["defer_min"]:
            verdict = "暂缓"
        else:
            verdict = "不做"
        # 有软性 FAIL 就降一档，不管几条都只降一档：这些规则已经在五维分数里扣过分了，
        # 每条再降一档等于同一个问题罚两遍（74.9 分的品因为两条软项直接落到「不做」）。
        # 硬规则没过、但因数据是估算而没触发闸门的，也按软性处理。
        blocked = {b["rule"] for b in hard}
        drops = len([o for o in outcomes if o.status == FAIL and (not o.hard or o.id not in blocked)])
        if drops:
            idx = VERDICTS.index(verdict)
            verdict = VERDICTS[min(idx + 1, len(VERDICTS) - 1)]
        reason = f"加权总分 {pts}/100（规则覆盖 {summary['coverage']:.0%}，证据覆盖 {evidence_ratio:.0%}）" + \
                 (f"，软性未过项 {drops} 条，降一档" if drops else "")
        est = facts.get("economics_estimated")
        if est and any(o.status == FAIL and o.id in ("R11", "R12") for o in outcomes):
            reason += f"；毛利未过线，但{'、'.join(est)}是估算值，未作一票否决——请拿到真实尺寸重量和报价后复核"
        # 专利命中只做“附条件”：关键词检索噪声大，命中≠侵权，但复核前不能给“做”
        ip_hits = _num(facts.get("ip_findings_count"))
        if ip_hits and facts.get("ip_reviewed") is not True and verdict == "做":
            verdict = "有条件做"
            reason += f"；专利检索命中 {int(ip_hits)} 条待人工复核，复核通过前按“有条件做”处理"
        elif ip_hits is None and facts.get("ip_reviewed") is not True and verdict == "做":
            verdict = "有条件做"
            reason += "；尚未做专利排查，排查前最高给“有条件做”"

    # 「未研判」就是没有结论，分数一并撤掉：两个维度算得出的 55 分摆在「未研判」旁边，
    # 用户只会记住 55 分，等于用能算的部分冒充了整体判断。
    if verdict == "未研判":
        total = dict(total, score=None, points=None)
    # 这里的“置信度”＝规则覆盖×证据覆盖，衡量的是“该有的数据有没有”，不是“结论有多确定”。
    # 封顶 90：数据全齐也只说明没有缺项，报告里再出现 100.0 就是把覆盖率冒充成确定性。
    confidence = round(min(90.0, coverage * 100 * (0.9 if total["axis_coverage"] < 0.9 else 1.0)), 1)
    return {
        "verdict": verdict,
        "tier": TIER_KEY.get(verdict, "unknown"),
        "reason": reason,
        "total_score": total["score"],
        "points": total["points"],
        "weights": total["weighted"] or dict(weights or DEFAULT_WEIGHTS),
        "axes": axes,
        "axis_coverage": total["axis_coverage"],
        "data_coverage": coverage,
        "rule_summary": summary,
        "hard_blockers": hard,
        "confidence": confidence,
        "confidence_level": "高" if confidence >= 75 else "中" if confidence >= 55 else "低",
    }


def actions_from(outcomes: List[RuleOutcome], limit: int = 8) -> List[Dict[str, str]]:
    """把未过项翻译成待办；优先硬闸门与 FAIL，其次 WARN。"""
    order = {FAIL: 0, WARN: 1, NO_DATA: 2}
    picked = sorted([o for o in outcomes if o.status in order], key=lambda o: (not o.hard, order[o.status]))
    out = []
    for o in picked[:limit]:
        action = o.action if o.status != NO_DATA else f"补齐数据后重跑：{o.name}（{o.note}）"
        out.append({"rule": o.id, "status": o.status, "title": o.name, "action": action,
                    "evidence": o.evidence_id})
    return out


def axes_markdown(axes: Dict[str, Dict[str, Any]], verdict: Optional[str] = None) -> str:
    """五维评分表。

    结论是「未研判」时先声明这张表不构成结论：否则一行 2.8 分摆在「未研判」旁边，
    读者只会记住那个数字——用一条能判的规则冒充整个维度的判断，是这套报告最容易被误读的地方。
    单条规则撑起来的维度也一样标出来，让人知道这个分有多薄。
    """
    lines: List[str] = []
    if verdict == "未研判":
        lines += ["> 下面只是各维度**能算的部分**，不是结论——整体判定为「未研判」，有效数据不足。", ""]
    lines += ["| 维度 | 得分 | 依据 |", "|---|---|---|"]
    for axis in AXES:
        a = axes.get(axis) or {}
        score = a.get("score")
        basis = a.get("basis", "")
        n = len(a.get("rules") or [])
        if score is not None and n <= 1:
            basis += "（只有 1 条规则可判，样本太薄）"
        lines.append(f"| {AXIS_LABELS[axis]} | {score if score is not None else '缺数据'} | {basis} |")
    return "\n".join(lines)
