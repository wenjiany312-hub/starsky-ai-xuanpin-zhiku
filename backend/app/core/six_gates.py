"""
六道门风控定档——引擎内的确定性实现。

本文件纯计算，不联网、不执行方法论包里的任何脚本；包只提供阈值与研判视角。

门序：合规 → 体量 → 盈利 → 趋势 → 产品原因 → 发货风险；fail 优先于 unknown；
运营难度只调节目标与备货，**不否决**。

定档口径：**只有发货风险这一道门失败时给「小批量试款」，其余门失败才是「不做」。**
"""
from typing import Any, Dict, List, Optional

GATE_NAMES = ("合规门", "体量门", "盈利门", "趋势门", "产品原因门", "发货风险门")

# 阈值默认值；真正生效的是 rules.BUILTIN_THRESHOLDS["six_gates"] 叠加方法包覆盖
DEFAULTS: Dict[str, float] = {
    "volume_pass_min": 3.0,        # 头部 ÷ 目标 ≥ 3
    "roi_pass_min": 1.20,          # ROI ≥ 120%
    "profit_pass_usd": 5.0,        # 且单件利润 > $5
    "profit_relax_usd": 10.0,      # 或单件利润 > $10 直接过
    "trend_pass_min": 0.7,         # 目标月 ÷ 当前月 ≥ 0.7（下滑超 30% 不做）
    "unsaturation_pass_min": 0.15,  # 低评论平均月销 ÷ 100–200 评论参照中位 ≥ 0.15
    "difficulty_hard_below": 10.0,  # 难度 <10% 困难
    "difficulty_easy_above": 15.0,  # >15% 简单，中间为适中
    "ref_reviews_min": 100,
    "ref_reviews_max": 200,
    "first_batch_months": 2.0,     # 首批 S × k × 月数
    "first_batch_k": 1.0,
    "difficulty_hard_batch_factor": 0.5,  # 困难档首批 ×0.5（经验口径，待核对）
    "trial_batch_max": 100,        # 试款 50–100 件
    "price_floor_usd": 15.0,       # 初筛价格线
    "commission": 0.15,            # 平台佣金
    "cny_usd": 7.2,                # 汇率
    "target_orders_default": 200,  # 目标月销默认值（美国站月销下限 200）
}

_GATE_RESULT_TO_VERDICT = {
    "做": "做",
    "做（降目标、少量首批）": "有条件做",
    "小批量试款": "有条件做",
    "观察": "暂缓",
    "不做": "不做",
}


def thresholds(t: Optional[Dict[str, Any]] = None) -> Dict[str, float]:
    out = dict(DEFAULTS)
    group = (t or {}).get("six_gates") if isinstance(t, dict) else None
    if isinstance(group, dict):
        for k, v in group.items():
            if k in out and isinstance(v, (int, float)) and not isinstance(v, bool):
                out[k] = float(v)
    return out


def _f(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if value is None or value == "":
            return default
        v = float(value)
        return v if v == v else default   # NaN → 缺
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- 单项计算

def profit(price: Any, cost_cny: Any, head_cny: Any = 0.0, *, fba: Any = 0.0,
           commission: Any = 0.15, discount: Any = 0.0, rate: Any = 7.2,
           target_profit: Any = 5.0) -> Dict[str, Any]:
    """单件经济：到岸成本、净售价、佣金、净利、毛利率、ROI、最低售价、采购上限、120% 定价。"""
    price_f, cost_f = _f(price), _f(cost_cny)
    if price_f is None or price_f <= 0:
        return {"ok": False, "missing": "售价"}
    if cost_f is None or cost_f < 0:
        return {"ok": False, "missing": "采购价"}
    head_f = _f(head_cny, 0.0) or 0.0
    fba_f = _f(fba, 0.0) or 0.0
    rate_f = _f(rate, 7.2) or 7.2
    if rate_f <= 0:
        return {"ok": False, "missing": "汇率"}
    comm = min(max(_f(commission, 0.15) or 0.0, 0.0), 1.0)
    disc = min(max(_f(discount, 0.0) or 0.0, 0.0), 0.95)
    keep = (1 - comm) * (1 - disc)

    landed = round((cost_f + head_f) / rate_f, 4)
    net_price = round(price_f * (1 - disc), 4)
    referral = round(net_price * comm, 4)
    payout = round(net_price - referral - fba_f, 4)
    net = round(payout - landed, 4)
    margin = round(net / net_price, 4) if net_price > 0 else None
    roi = round(net / landed, 4) if landed > 0 else None
    return {
        "ok": True,
        "price": round(price_f, 4),
        "cost_cny": round(cost_f, 2),
        "head_cny": round(head_f, 2),
        "rate": rate_f,
        "commission": comm,
        "discount": disc,
        "fba": round(fba_f, 4),
        "landed": landed,
        "net_price": net_price,
        "referral": referral,
        "payout": payout,
        "profit": net,
        "margin": margin,
        "roi": roi,
        "price_min": round((landed + fba_f) / keep, 4) if keep > 0 else None,
        "cost_max_cny": round((net_price * (1 - comm) - fba_f - (_f(target_profit, 5.0) or 5.0)) * rate_f - head_f, 2),
        "price_120": round((landed * 2.2 + fba_f) / (1 - comm), 4) if comm < 1 else None,
        "acos_break_even": round(net / net_price, 4) if net_price > 0 else None,
    }


def difficulty(pct: Any, th: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
    """评论区间难度：A（低评论档平均月销）÷ B（自然前 5 平均月销）。只调目标与备货，不否决。"""
    th = th or thresholds()
    p = _f(pct)
    if p is None:
        return {"ok": False, "level": "unknown", "pct": None, "note": "缺评论区间样本"}
    if p < th["difficulty_hard_below"]:
        level, note = "困难", "低评论档打不过头部，目标与备货按困难下调"
    elif p <= th["difficulty_easy_above"]:
        level, note = "适中", ""
    else:
        level, note = "简单", ""
    if p > 100:
        note = "该档月销已超过头部均值，数值偏大属正常"
    return {"ok": True, "pct": round(p, 2), "level": level, "note": note}


def volume(head_sales: Any, target_orders: Any, th: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
    """体量门：断崖法找到的站内头部月销 ÷ 目标月销 ≥ 3。"""
    th = th or thresholds()
    head, target = _f(head_sales), _f(target_orders)
    if head is None or target is None or target <= 0:
        return {"ok": False, "ratio": None, "note": "缺头部月销或目标月销"}
    return {"ok": True, "ratio": round(head / target, 2), "head": head, "target": target,
            "pass": head / target >= th["volume_pass_min"]}


def trend(target_month: Any, current_month: Any, th: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
    """趋势门：目标月 ÷ 当前月 ≥ 0.7，即下滑超过 30% 直接不做。"""
    th = th or thresholds()
    target, cur = _f(target_month), _f(current_month)
    if target is None or cur is None or cur <= 0:
        return {"ok": False, "ratio": None, "note": "缺趋势两期销量"}
    return {"ok": True, "ratio": round(target / cur, 3), "pass": target / cur >= th["trend_pass_min"]}


def unsaturation(low_avg: Any, ref_median: Any, th: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
    """未饱和：低评论档平均月销 ÷ 100–200 评论参照中位销量 ≥ 0.15。与运营难度不是同一指标。"""
    th = th or thresholds()
    low, ref = _f(low_avg), _f(ref_median)
    if low is None or ref is None or ref <= 0:
        return {"ok": False, "ratio": None, "note": "缺低评论档或参照档销量"}
    return {"ok": True, "ratio": round(low / ref, 3), "pass": low / ref >= th["unsaturation_pass_min"]}


def first_batch(peer_avg: Any, target_orders: Any, difficulty_level: str = "",
                th: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
    """首批 = S × k × 2 个月。S 取同特征、目标评论档的平均月销（v1.0 §10.3）；
    没给 S 时才回落到目标月销，并标注偏高。困难档 ×0.5；高成本不设系数（无依据，由人定）。"""
    th = th or thresholds()
    s = _f(peer_avg)
    if s is not None and s > 0:
        base, basis = s, "同特征平均月销 S"
    else:
        base = _f(target_orders)
        if base is None or base <= 0:
            return {"ok": False, "qty": None, "note": "缺同特征平均月销与目标月销"}
        basis = "目标月销（未给同特征均值 S，首批可能偏高，需人工改）"
    qty = base * th["first_batch_k"] * th["first_batch_months"]
    factor = 1.0
    notes: List[str] = []
    if difficulty_level == "困难":
        qty *= th["difficulty_hard_batch_factor"]
        factor *= th["difficulty_hard_batch_factor"]
        notes.append("困难档首批下调")
    return {"ok": True, "qty": int(round(qty)), "factor": round(factor, 3), "basis": basis,
            "replenish": int(round(base)), "note": "；".join(notes)}


def _anomaly_list(value: Any) -> List[str]:
    """运营异常标签：接受列表或「、，,;；」分隔的字符串。"""
    if not value:
        return []
    if isinstance(value, str):
        import re
        parts = re.split(r"[、，,;；\n]+", value)
    else:
        parts = [str(x) for x in value]
    return [p.strip() for p in parts if p and p.strip()]


# ---------------------------------------------------------------- 六道门

def _gate(index: int, name: str, status: str, value: Any, threshold: Any,
          note: str = "", action: str = "") -> Dict[str, Any]:
    return {"index": index, "name": name, "status": status, "value": value,
            "threshold": threshold, "note": note, "action": action}


def _flag(value: Any) -> Optional[bool]:
    """参数里的是/否：表单和接口常把布尔传成字符串，"false" 不能因为非空就算「是」。"""
    if value is None or value == "":
        return None
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("1", "true", "yes", "y", "on", "是", "可承受", "已核"):
            return True
        if s in ("0", "false", "no", "n", "off", "否", "不可承受", "未核"):
            return False
        return None
    return bool(value)


def _status(ok: Optional[bool]) -> str:
    return "unknown" if ok is None else ("pass" if ok else "fail")


def decide(inp: Dict[str, Any], t: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """按六道门出结论。inp 的字段见 from_facts()；缺值一律 unknown，不猜。"""
    th = thresholds(t)
    inp = inp or {}
    gates: List[Dict[str, Any]] = []

    # 1 合规门
    comp = inp.get("compliance") or {}
    comp_status = comp.get("status")
    if comp_status not in ("pass", "fail", "unknown"):
        comp_status = "unknown"
    gates.append(_gate(1, GATE_NAMES[0], comp_status,
                       comp.get("label") or "—",
                       "IP / 受限 / 证书三项都要有人核过",
                       comp.get("note") or "合规未检索，最高只能到观察",
                       "外观专利以图搜图、商标查 USPTO、类目证书按后台要求核"))

    # 2 体量门
    vol = volume(inp.get("head_sales"), inp.get("target_orders"), th)
    if not vol["ok"]:
        gates.append(_gate(2, GATE_NAMES[1], "unknown", "—", f"头部÷目标 ≥ {th['volume_pass_min']:g}",
                           vol["note"], "补站内头部月销（断崖法第一档）与目标月销"))
    else:
        label = f"{vol['ratio']:g}×（头部 {vol['head']:g} ÷ 目标 {vol['target']:g}）"
        note = "" if vol["pass"] else f"头部撑不起目标：{vol['ratio']:g}× 低于 {th['volume_pass_min']:g}×"
        gates.append(_gate(2, GATE_NAMES[1], _status(vol["pass"]), label,
                           f"头部÷目标 ≥ {th['volume_pass_min']:g}", note,
                           "换更大的头部，或把目标月销降到头部 ÷ 3 以内"))
    target_now = _f(inp.get("target_orders"))
    if target_now is not None and target_now < th["target_orders_default"]:
        gates[-1]["note"] = (gates[-1]["note"] + "；" if gates[-1]["note"] else "") + \
            f"目标 {target_now:g}/月 低于月销下限 {th['target_orders_default']:g}"

    # 3 盈利门
    pf = inp.get("profit_calc") or {}
    price_f = _f(inp.get("price"))
    if not pf.get("ok"):
        gates.append(_gate(3, GATE_NAMES[2], "unknown", "—",
                           f"ROI ≥ {th['roi_pass_min']:g} 且利润 > ${th['profit_pass_usd']:g}",
                           f"缺{pf.get('missing') or '售价或采购价'}", "补真实采购价与到岸成本后重算，不猜"))
    else:
        roi, net = pf.get("roi"), pf.get("profit")
        ok = (roi is not None and roi >= th["roi_pass_min"] and net is not None
              and net > th["profit_pass_usd"]) or (net is not None and net > th["profit_relax_usd"])
        parts = [f"净利 ${net:+.2f}/件" if net is not None else "净利 —",
                 f"ROI {roi * 100:.0f}%" if roi is not None else "ROI —",
                 f"最低售价 ${pf['price_min']:.2f}" if pf.get("price_min") is not None else "",
                 f"采购上限 ¥{pf['cost_max_cny']:.2f}" if pf.get("cost_max_cny") is not None else ""]
        notes = []
        if not ok:
            notes.append(f"低于利润线（ROI {th['roi_pass_min'] * 100:g}% 且净利 > ${th['profit_pass_usd']:g}，"
                         f"或净利 > ${th['profit_relax_usd']:g}）")
        if price_f is not None and price_f < th["price_floor_usd"]:
            notes.append(f"售价 ${price_f:.2f} 低于初筛线 ${th['price_floor_usd']:g}")
        if inp.get("profit_estimated"):
            notes.append(f"{inp['profit_estimated']}是估的，别当承诺")
        gates.append(_gate(3, GATE_NAMES[2], _status(ok), "；".join(p for p in parts if p),
                           f"ROI ≥ {th['roi_pass_min'] * 100:g}% 且净利 > ${th['profit_pass_usd']:g}，或净利 > ${th['profit_relax_usd']:g}",
                           "；".join(notes),
                           "砍采购价 / 抬售价 / 减头程，三选一，重算到过线"))

    # 4 趋势门
    tr = inp.get("trend")
    if isinstance(tr, dict) and tr.get("ratio") is not None:
        ok_tr = bool(tr.get("pass"))
        gates.append(_gate(4, GATE_NAMES[3], _status(ok_tr), f"{tr['ratio']:g}×",
                           f"目标月÷当前月 ≥ {th['trend_pass_min']:g}",
                           "需求下滑超过 30%" if not ok_tr else "",
                           "等趋势回到 0.7 以上，或换季前再上"))
    else:
        gates.append(_gate(4, GATE_NAMES[3], "unknown", "—",
                           f"目标月÷当前月 ≥ {th['trend_pass_min']:g}",
                           (tr or {}).get("note") or "缺趋势数据", "补搜索量或月销两期数据"))

    # 5 产品原因门
    reason = str(inp.get("product_reason") or "").strip()
    uns = inp.get("unsaturation") or {}
    if reason:
        gates.append(_gate(5, GATE_NAMES[4], "pass", reason,
                           "热卖元素 / 性价比 / 创新 / 未饱和 / 季节，至少一条有人认账",
                           "人工判定，需附证据", ""))
    elif uns.get("ratio") is not None:
        ok_u = bool(uns.get("pass"))
        gates.append(_gate(5, GATE_NAMES[4], _status(ok_u), f"未饱和 {uns['ratio']:g}",
                           f"低评论平均月销 ÷ 参照中位 ≥ {th['unsaturation_pass_min']:g}",
                           "代理指标，需人工复核" + ("" if ok_u else "；尾部出不了单，得另找产品原因"),
                           "指出具体是哪条产品原因，并给出竞品证据"))
    else:
        gates.append(_gate(5, GATE_NAMES[4], "unknown", "—",
                           "热卖元素 / 性价比 / 创新 / 未饱和 / 季节",
                           "没有可用的产品原因证据", "看低评论档为什么能出单，或想清楚差异点"))

    # 6 发货风险门
    max_loss_ok = _flag(inp.get("max_loss_ok"))
    if max_loss_ok is None:
        gates.append(_gate(6, GATE_NAMES[5], "unknown", "—",
                           "首批最大亏损在可承受范围内", "没给资金上限，无法判断",
                           "填可承受的首批亏损上限（max_loss_ok）"))
    elif max_loss_ok:
        gates.append(_gate(6, GATE_NAMES[5], "pass", f"首批 {inp.get('first_batch') or '—'} 件",
                           "首批最大亏损可承受", "", ""))
    else:
        gates.append(_gate(6, GATE_NAMES[5], "fail", f"首批 {inp.get('first_batch') or '—'} 件",
                           "首批最大亏损可承受", "发货后风险偏高",
                           "降到试款量（50–100 件）先验证"))

    failed = [g for g in gates if g["status"] == "fail"]
    unknown = [g for g in gates if g["status"] == "unknown"]
    diff_level = str((inp.get("difficulty") or {}).get("level") or "")

    if failed:
        hard_failed = [g for g in failed if g["name"] != GATE_NAMES[5]]
        if hard_failed:
            conclusion = "不做"
            why = "未通过：" + "、".join(g["name"] for g in failed)
        elif unknown:
            # 只有发货风险不过、其余门却还缺数据：缺数据的封顶规则照样生效，不能跳到「小批量试款」
            conclusion = "观察"
            why = ("发货风险偏高，且存在未完成/缺失项（" + "、".join(g["name"] for g in unknown)
                   + "），最高只能到观察")
        else:
            conclusion = "小批量试款"
            why = "其余五门通过，但发货风险偏高 → 降到试款量"
    elif unknown:
        conclusion = "观察"
        why = "存在未完成/缺失项（" + "、".join(g["name"] for g in unknown) + "），最高只能到观察"
    elif diff_level == "困难":
        conclusion = "做（降目标、少量首批）"
        why = "六道门全部通过；运营难度困难，目标与备货下调"
    else:
        conclusion = "做"
        why = "六道门全部通过"

    # 方法论 §3.2 / §11：运营异常、同特征竞品不足，都把「做 / 小批量试款」降为「观察」
    peer_count = _f(inp.get("peer_count"))
    anomalies = _anomaly_list(inp.get("anomalies"))
    caps: List[str] = []
    if anomalies:
        caps.append("运营异常（" + "、".join(anomalies) + "）：销量口径可能被污染，降级观察；"
                    "要坚持做须人工逐条核出真实销量")
    if peer_count is not None and peer_count < 2:
        caps.append(f"同特征竞品仅 {peer_count:g} 个，置信度低，口径要求只能观察")
    if caps and conclusion in ("做", "做（降目标、少量首批）", "小批量试款"):
        conclusion = "观察"
        why = "；".join(caps)

    conf = "高" if not unknown and not failed else ("中" if len(unknown) <= 1 else "低")
    if peer_count is not None:
        if peer_count < 2:
            conf = "低"
        elif peer_count < 3 and conf == "高":
            conf = "中"   # 高 = ≥3 个同特征竞品 + 当天数据 + 两个原因交叉验证
    peer_auto = _flag(inp.get("peer_auto"))
    if peer_auto and conf == "高":
        # 自动样本是「关键词下的头部商品」，不是按特征判据挑的同特征集：可以出数，但封顶「中」
        conf = "中"
    todo = [g["action"] for g in gates if g["status"] in ("fail", "unknown") and g.get("action")]
    if peer_count is None or peer_count < 3:
        todo.append("同特征竞品补到 ≥3 个（填 peer_count）")
    if peer_auto:
        todo.append("竞品数字来自自动筛查（关键词样本）：按「消费者会不会犹豫 / 爆了拿谁对比」人工确认 3–10 个同特征竞品，置信度才能到「高」")
    if anomalies:
        todo.append("逐条核查运营异常：合并评论、刷评、站外、品牌流量、老代新、烧广告")
    return {
        "mode": "gates",
        "method": "六道门风控定档",
        "gates": gates,
        "conclusion": conclusion,
        "verdict": _GATE_RESULT_TO_VERDICT.get(conclusion, "暂缓"),
        "reason": why,
        "confidence": conf,
        "failed": [g["name"] for g in failed],
        "unknown": [g["name"] for g in unknown],
        "difficulty": inp.get("difficulty") or {},
        "first_batch": inp.get("first_batch"),
        "profit": pf if pf.get("ok") else None,
        "volume": vol,
        "trend": tr if isinstance(tr, dict) else None,
        "unsaturation": uns if uns.get("ratio") is not None else None,
        "todo": todo,
        "peer_count": peer_count,
        "peer_auto": peer_auto,
        "anomalies": anomalies,
    }


# ---------------------------------------------------------------- 事实 → 门

def _compliance(facts: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
    """合规门只认「有人核过」：关键词检索过了不等于外观和商标查过了。"""
    if _flag(params.get("restricted_fail")) or _flag(params.get("certificates_failed")):
        return {"status": "fail", "label": "受限/证书未过", "note": "类目受限或缺证书，先别做"}
    if _flag(params.get("ip_reviewed")) or facts.get("ip_reviewed") is True:
        return {"status": "pass", "label": "人工已核", "note": "以复核结论为准；换款/改图后要重查"}
    hits = _f(facts.get("ip_findings_count"))
    if hits is None:
        return {"status": "unknown", "label": "专利检索没跑通", "note": "合规未检索，最高只能到观察"}
    if hits > 0:
        return {"status": "unknown", "label": f"关键词命中 {int(hits)} 条",
                "note": "命中≠侵权，但没逐条看过不能算过；外观与商标还没查"}
    return {"status": "unknown", "label": "仅关键词 0 命中",
            "note": "关键词没命中只挡得住同名专利；外观要以图搜图，商标要单独查"}


def from_facts(facts: Dict[str, Any], params: Optional[Dict[str, Any]] = None,
               t: Optional[Dict[str, Any]] = None,
               competitors: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """把引擎已有的事实映射成六道门的输入。参数优先于事实，缺值留着 unknown。"""
    facts = facts or {}
    p = params or {}
    th = thresholds(t)

    # 汇率优先跟随本机利润测算用的那个（facts 里记录的），否则用方法论的默认值
    # 表单 / 引擎用的参数名是 purchase_price_cny、cny_usd_rate；cost_cny、cny_usd 是方法论原名，两种都认
    rate = _f(p.get("cny_usd"), _f(p.get("cny_usd_rate"), _f(facts.get("cny_usd_rate"), th["cny_usd"])))
    price = _f(p.get("sale_price_usd"), _f(facts.get("sale_price_usd")))
    # 采购价顺序：用户填的 → 利润测算实际用的（报告「钱怎么算的」那一份）→ 1688 中位价兜底
    cost_cny = _f(p.get("cost_cny"), _f(p.get("purchase_price_cny")))
    if cost_cny is None and _f(facts.get("purchase_cost_usd")) is not None:
        cost_cny = round(_f(facts.get("purchase_cost_usd")) * rate, 2)
    if cost_cny is None:
        cost_cny = _f(facts.get("supply_price_cny_median"))
    head_cny = _f(p.get("head_cny"))
    if head_cny is None and _f(facts.get("freight_usd")) is not None:
        head_cny = round(_f(facts.get("freight_usd")) * rate, 2)
    fba = _f(p.get("fba_fee_usd"), _f(facts.get("fba_fee_usd")))
    commission = _f(p.get("commission"), _f(facts.get("referral_pct")) or th["commission"])
    discount = _f(p.get("discount"), 0.0) or 0.0

    pf = profit(price, cost_cny, head_cny or 0.0, fba=fba or 0.0, commission=commission,
                discount=discount, rate=rate, target_profit=th["profit_pass_usd"])
    estimated = [*([x for x in (facts.get("economics_estimated") or []) if x in ("FBA 配送费", "头程运费")])]

    # 体量：站内头部月销优先取参数，其次取数据层已经算好的头部月销，最后取头部竞品样本最大值（代理指标）
    head_sales = _f(p.get("head_sales"))
    head_is_proxy = False
    if head_sales is None:
        head_sales = _f(facts.get("head_monthly_sales"))
        if head_sales is not None:
            head_is_proxy = True
    if head_sales is None:
        pool = [_f(c.get("monthly_orders")) for c in (competitors or facts.get("top_competitors_sample") or [])
                if isinstance(c, dict)]
        pool = [x for x in pool if x]
        if pool:
            head_sales, head_is_proxy = max(pool), True
    target_orders = _f(p.get("target_orders"), th["target_orders_default"])

    # 趋势：优先两期销量，其次用搜索量同比换算成倍数
    tr: Dict[str, Any]
    if _f(p.get("trend_target")) is not None and _f(p.get("trend_current")) is not None:
        tr = trend(p.get("trend_target"), p.get("trend_current"), th)
        tr["basis"] = "参数给定两期销量"
    else:
        g = _f(facts.get("search_volume_trend_pct"))
        if g is None:
            tr = {"ok": False, "ratio": None, "note": "缺趋势数据"}
        else:
            ratio = 1 + g / 100.0
            tr = {"ok": True, "ratio": round(ratio, 3), "pass": ratio >= th["trend_pass_min"],
                  "basis": f"搜索量同比 {g:+.1f}%（{facts.get('trend_basis') or '周搜索量'}）"}

    uns = unsaturation(_f(p.get("low_review_avg")), _f(p.get("ref_reviews_median")), th)
    if uns.get("ratio") is None and _f(facts.get("unsaturation_ratio")) is not None:
        uns = {"ok": True, "ratio": _f(facts.get("unsaturation_ratio")),
               "pass": _f(facts.get("unsaturation_ratio")) >= th["unsaturation_pass_min"],
               "basis": "低评论档平均月销 ÷ 100–200 评论参照中位（代理指标）"}

    diff = difficulty(facts.get("review_band_difficulty_pct"), th)
    peer_avg = _f(p.get("peer_avg_monthly"))
    fb = first_batch(peer_avg, target_orders, diff.get("level") or "", th)

    inp = {
        "peer_count": p.get("peer_count"),
        "peer_auto": p.get("peer_auto"),
        "anomalies": p.get("anomalies"),
        "price": price,
        "profit_calc": pf,
        "profit_estimated": "、".join(estimated),
        "head_sales": head_sales,
        "head_is_proxy": head_is_proxy,
        "target_orders": target_orders,
        "compliance": _compliance(facts, p),
        "trend": tr,
        "unsaturation": uns,
        "product_reason": p.get("product_reason") or facts.get("product_reason"),
        "max_loss_ok": p.get("max_loss_ok"),
        "difficulty": diff,
        "first_batch": (fb.get("qty") if fb.get("ok") else None),
        "cost_cny": cost_cny,
        "rate": rate,
    }
    out = decide(inp, t)
    out["inputs"] = inp
    if head_is_proxy:
        proxy = "头部月销是代理指标（取自竞品样本，没用断崖法确认），需人工复核"
        out["volume"]["note"] = "；".join(x for x in (out["volume"].get("note"), proxy) if x)
        # 门表里也要写：方法论要求代理指标必须标注，只写在附属字段里报告上看不到
        g2 = next((g for g in out["gates"] if g["index"] == 2), None)
        if g2 and g2["status"] != "unknown":
            g2["note"] = "；".join(x for x in (g2.get("note"), proxy) if x)
    out["first_batch_detail"] = fb
    return out


def apply_to_verdict(verdict: Dict[str, Any], gate: Dict[str, Any]) -> Dict[str, Any]:
    """六道门结论覆盖分数结论；五维分数留作支撑材料，不参与定档。"""
    out = dict(verdict)
    out["score_verdict"] = verdict.get("verdict")
    out["decision_mode"] = "gates"
    out["gate"] = gate
    if verdict.get("verdict") == "未研判":
        # 数据源没接上时，六道门也全是 unknown——那种「观察」是假的，保持「未研判」
        out["gate_note"] = "有效数据不足，六道门没有形成结论"
        return out
    out["verdict"] = gate["verdict"]
    out["tier"] = "gates"
    out["reason"] = f"{gate['conclusion']}｜{gate['reason']}（评分参照：{verdict.get('verdict')}"
    pts = verdict.get("points")
    out["reason"] += f" {pts} 分）" if pts is not None else "）"
    return out
