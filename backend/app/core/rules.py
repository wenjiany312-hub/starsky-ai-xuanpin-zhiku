"""
规则评估：把「事实」按阈值判成 PASS / WARN / FAIL / 缺数据。

- 阈值来源优先级：config/local/s1_thresholds.yaml（用户可覆盖）→ 星空插件 templates/s1_thresholds.yaml → 代码内置默认。
- 规则只做判定，不写结论；判定结果带 rule_id，报告里每个“通过/不通过”都能指回来。
- 缺数据就是 NO_DATA，不参与打分，也不假装通过。
"""
import glob
import re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.core.config import settings

PASS, WARN, FAIL, NO_DATA, SKIP = "PASS", "WARN", "FAIL", "NO_DATA", "SKIP"

# 内置默认阈值（与星空 S1 阈值表同口径；表缺失时用这些）
BUILTIN_THRESHOLDS: Dict[str, Any] = {
    "demand": {"min_weekly_search_volume": 300, "warn_weekly_search_volume": 150},
    "trend": {"growing_pct": 0.0, "warn_decline_pct": -10.0},
    "competition": {
        "top3_click_share_pass_max": 0.35,
        "top3_click_share_warn_max": 0.50,
        "top3_click_share_fatal": 0.60,
        "listing_count_pass_max": 800,
        "listing_count_warn_max": 3000,
        "median_reviews_pass_max": 300,
        "median_reviews_warn_max": 800,
        "young_listing_share_pass_min": 0.30,
        "young_listing_share_warn_min": 0.15,
        "young_listing_days_max": 365,
        # 星空“评论区间法”：难度 = 低评论(≤low_review_max)相似品平均月销 ÷ 自然前 5 平均月销
        "low_review_max": 30,
        "review_band_pass_min": 15.0,   # >15% 简单
        "review_band_warn_min": 10.0,   # 10~15% 适中；<10% 困难
    },
    "price": {"avg_price_pass_min": 15.0, "avg_price_warn_min": 10.0, "avg_price_warn_max": 400.0},
    "ad": {"natural_share_pass_min": 0.20, "natural_share_warn_min": 0.10},
    "supply": {"offers_pass_min": 5, "offers_warn_min": 2, "moq_pass_max": 500, "moq_warn_max": 1000},
    "margin": {
        "gross_margin_pass_min": 35.0,
        "gross_margin_warn_min": 25.0,
        "gross_margin_fatal_below": 15.0,
        "ceiling_headroom_pass_min": 1.25,   # 采购价上限 / 实际采购价
        "ceiling_headroom_warn_min": 1.05,
    },
    # PB-07 / KC-QUANT-02 “双 20%”：采购+头程 ≤ 售价 20%，FBA ≤ 售价 20%
    "unit": {"landed_ratio_pass_max": 0.20, "landed_ratio_warn_max": 0.30,
             "fba_ratio_pass_max": 0.20, "fba_ratio_warn_max": 0.30},
    "verdict": {
        "do_min": 70, "conditional_min": 55, "defer_min": 45,
        "coverage_min": 0.50, "coverage_fatal_min": 0.30,
    },
    # 六道门风控定档：门序 合规→体量→盈利→趋势→产品原因→发货风险
    # 键名要避开 normalize_unit 的百分数/占比改写（含 ratio_pass/ratio_warn 的键会被 ÷100）
    "six_gates": {
        "volume_pass_min": 3.0,          # 站内头部月销 ÷ 目标月销 ≥ 3
        "roi_pass_min": 1.20,            # ROI ≥ 120%
        "profit_pass_usd": 5.0,          # 且单件净利 > $5
        "profit_relax_usd": 10.0,        # 或单件净利 > $10 直接过
        "trend_pass_min": 0.7,           # 目标月 ÷ 当前月 ≥ 0.7（下滑超 30% 不做）
        "unsaturation_pass_min": 0.15,   # 低评论档平均月销 ÷ 100~200 评论参照中位
        "difficulty_hard_below": 10.0,   # 难度 <10% 困难
        "difficulty_easy_above": 15.0,   # >15% 简单，中间适中
        "ref_reviews_min": 100,
        "ref_reviews_max": 200,
        "first_batch_months": 2.0,       # 首批 = S × k × 2 个月
        "first_batch_k": 1.0,
        "difficulty_hard_batch_factor": 0.5,  # 困难档首批 ×0.5（经验口径，待核对）
        "trial_batch_max": 100,          # 试款 50~100 件
        "price_floor_usd": 15.0,         # 初筛价格线
        "commission": 0.15,              # 平台佣金
        "cny_usd": 7.2,                  # 汇率
        "target_orders_default": 200,    # 目标月销默认值（美国站月销下限）
    },
    # 社媒 VOC 前导选品三道门：声量 / 痛点 / 平台。默认值与 core/voc.py 的 DEFAULTS 一致
    "voc": {
        "min_mentions": 60, "fail_mentions": 20,
        "trend_pass_min": 0.0, "trend_fail_below": -30.0,
        "pain_rate_pass_min": 0.20, "pain_rate_warn_min": 0.12, "top_pain_pct_pass_min": 25.0,
        "platform_trend_pass_min": 0.0, "platform_trend_fail_below": -10.0,
        "platform_top3_pass_max": 0.45, "platform_top3_fail": 0.60,
        "platform_min_weekly": 1000, "platform_fail_weekly": 300,
    },
}

_SKILL_TEMPLATE_GLOBS = [
    str(settings.LOCAL_CONFIG_DIR / "s1_thresholds.yaml"),
    str(Path.home() / ".claude/plugins/cache/starskyclaude/starsky-amazon-agent/*/templates/s1_thresholds.yaml"),
    str(Path.home() / ".claude/plugins/**/templates/s1_thresholds.yaml"),
]


def _version_key(path: str):
    """按版本号倒序（同名插件装过多版时取最新），同版本再看修改时间。"""
    import re
    m = re.search(r"/(\d+(?:\.\d+)*)-", path)
    version = tuple(int(x) for x in m.group(1).split(".")) if m else (0,)
    try:
        mtime = Path(path).stat().st_mtime
    except OSError:
        mtime = 0
    return (version, mtime)


def _read_yaml(path: str) -> Optional[Dict[str, Any]]:
    try:
        import yaml  # 可选依赖
    except Exception:
        return None
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def load_thresholds() -> Dict[str, Any]:
    """合并三方阈值：内置 < 星空插件表 < 用户本地覆盖。只取我们认识的键，其余原样保留供追溯。"""
    merged = {k: dict(v) for k, v in BUILTIN_THRESHOLDS.items()}
    merged["_source"] = "builtin"
    for pattern in _SKILL_TEMPLATE_GLOBS:
        hits = sorted(glob.glob(pattern), key=_version_key, reverse=True)
        if not hits:
            continue
        raw = _read_yaml(hits[0])
        if not raw:
            continue
        merged["_source"] = "skill:" + hits[0]
        merged["_raw"] = raw
        _apply_skill_table(merged, raw)
        break
    return merged


def _apply_skill_table(merged: Dict[str, Any], raw: Dict[str, Any]) -> None:
    """把星空 S1 阈值表里与本系统同义的参数读进来；名字对不上的不硬猜。"""
    band = raw.get("entry_difficulty_review_band") or {}
    if isinstance(band, dict):
        comp = merged["competition"]
        if band.get("primary_band_reviews_max"):
            comp["low_review_max"] = int(band["primary_band_reviews_max"])
        if band.get("moderate_upto_pct") is not None:
            comp["review_band_pass_min"] = float(band["moderate_upto_pct"])
        if band.get("hard_below_pct") is not None:
            comp["review_band_warn_min"] = float(band["hard_below_pct"])
        bands = band.get("compare_bands") or []
        comp["_skill_compare_bands"] = list(bands) if isinstance(bands, list) else []

    reviews = raw.get("reviews") or {}
    if isinstance(reviews, dict) and reviews.get("very_good_count"):
        # “200 条评论已经很好”：头部中位数超过它的 4 倍视为门槛过高
        comp = merged["competition"]
        comp["_skill_reviews_very_good_count"] = int(reviews["very_good_count"])

    efficiency = raw.get("entry_difficulty_ad_efficiency") or {}
    if isinstance(efficiency, dict):
        ad = merged["ad"]
        floors = []
        if efficiency.get("natural_dominant_share_pct_below") is not None:
            floors.append(float(efficiency["natural_dominant_share_pct_below"]) / 100.0)
        if efficiency.get("ad_share_burn_pct") is not None:
            floors.append(1.0 - float(efficiency["ad_share_burn_pct"]) / 100.0)
        if floors:
            ad["natural_share_pass_min"] = round(max(0.05, max(floors)), 3)
            ad["natural_share_warn_min"] = round(ad["natural_share_pass_min"] * 0.5, 3)

    mode = raw.get("mode_and_profit") or {}
    if isinstance(mode, dict):
        m = merged["margin"]
        if mode.get("profit_line_roi_after_ads_pct_min") is not None:
            # ROI≥50% 约等于毛利率 ≥ 33%；按其口径换算，留着原值备查
            roi_min = float(mode["profit_line_roi_after_ads_pct_min"])
            m["gross_margin_pass_min"] = round(100.0 * roi_min / (100.0 + roi_min), 1)
            m["_skill_roi_after_ads_pct_min"] = roi_min
        if mode.get("value_for_money_discount_pct_min") is not None:
            merged["price"]["_skill_value_for_money_discount_pct_min"] = mode["value_for_money_discount_pct_min"]

    jp = mode.get("jingpu_monthly_orders") if isinstance(mode, dict) else None
    if isinstance(jp, list) and len(jp) == 2:
        merged["demand"]["_skill_jingpu_monthly_orders"] = jp


_THRESHOLDS_CACHE: Optional[Dict[str, Any]] = None


def thresholds(refresh: bool = False) -> Dict[str, Any]:
    global _THRESHOLDS_CACHE
    if _THRESHOLDS_CACHE is None or refresh:
        _THRESHOLDS_CACHE = load_thresholds()
    return _THRESHOLDS_CACHE


@dataclass
class RuleOutcome:
    id: str
    name: str
    axis: str
    status: str
    value: Any = None
    threshold: Any = None
    unit: str = ""
    note: str = ""
    action: str = ""
    evidence_id: str = ""
    hard: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _get(facts: Dict[str, Any], path: str) -> Any:
    node: Any = facts
    for key in path.split("."):
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def _rule_defs(t: Dict[str, Any]) -> List[Dict[str, Any]]:
    demand, trend, comp, price = t["demand"], t["trend"], t["competition"], t["price"]
    ad, supply, margin = t["ad"], t["supply"], t["margin"]
    unit = t.get("unit") or BUILTIN_THRESHOLDS["unit"]
    return [
        {"id": "R01", "name": "周搜索量", "axis": "market_capacity", "path": "search_volume_weekly",
         "kind": "gte", "pass": demand["min_weekly_search_volume"], "warn": demand["warn_weekly_search_volume"],
         "unit": "次/周", "action": "换更主流的词根，或把产品锁定在词根下的高需求细分场景", "note": "需求底盘；低于警戒线不足以支撑冷启动"},
        {"id": "R02", "name": "需求趋势", "axis": "trend", "path": "search_volume_trend_pct",
         "kind": "gte", "pass": trend["growing_pct"], "warn": trend["warn_decline_pct"],
         "unit": "%", "action": "查该词 12 个月趋势，区分季节性低谷与结构性下滑；结构性下滑不建议进", "note": "优先用同比（避开季节高低峰）；持续下滑要警惕结构性萎缩"},
        {"id": "R03", "name": "Top3 点击集中度", "axis": "competition", "path": "top3_click_share",
         "kind": "lte", "pass": comp["top3_click_share_pass_max"], "warn": comp["top3_click_share_warn_max"],
         "fatal": comp["top3_click_share_fatal"], "unit": "", "hard": True,
         "action": "头部固化，优先找长尾/细分变体切入，或换相近赛道", "note": "头部占比越高，新链接可抢的份额越少"},
        {"id": "R04", "name": "在售链接数", "axis": "competition", "path": "visible_asin_count",
         "kind": "lte", "pass": comp["listing_count_pass_max"], "warn": comp["listing_count_warn_max"], "unit": "个",
         "action": "链接数过密，改走差异化规格或组合装，避开同款正面竞争", "note": "链接数过多说明供给拥挤"},
        {"id": "R05", "name": "头部评论门槛", "axis": "competition", "path": "median_reviews",
         "kind": "lte", "pass": comp["median_reviews_pass_max"], "warn": comp["median_reviews_warn_max"],
         "unit": "条", "action": "评论门槛高，准备至少 3 个月的评论积累预算，或选评论更浅的子类目", "note": "中位评论数越低，新品越容易靠内容和广告起量"},
        {"id": "R14", "name": "评论区间难度", "axis": "competition", "path": "review_band_difficulty_pct",
         "kind": "gte", "pass": comp["review_band_pass_min"], "warn": comp["review_band_warn_min"], "unit": "%",
         "action": "低评论新品卖不动头部的零头，需有明确差异化或广告效率法另行证明才可进",
         "note": f"星空评论区间法：≤{comp['low_review_max']} 评论相似品平均月销 ÷ 自然前 5 平均月销"},
        {"id": "R06", "name": "新品可进性", "axis": "competition", "path": "young_listing_share",
         "kind": "gte", "pass": comp["young_listing_share_pass_min"], "warn": comp["young_listing_share_warn_min"],
         "unit": "占比", "action": "头部没有新链接成功案例，需要额外验证这个类目对新人是否友好", "note": f"头部里上架≤{comp['young_listing_days_max']}天的新链接占比，证明新品能挤进来"},
        {"id": "R07", "name": "均价带", "axis": "margin_barrier", "path": "avg_price_usd",
         "kind": "band", "pass_lo": price["avg_price_pass_min"], "pass_hi": price["avg_price_warn_max"],
         "warn_lo": price["avg_price_warn_min"], "warn_hi": price["avg_price_warn_max"] * 1.5,
         "unit": "USD", "action": "价格带不适配广告模型：低价品改走组合/多件装，高客单需先验证真实需求", "note": "太低价走不动广告，超高客单要额外验证需求真实性"},
        {"id": "R08", "name": "自然流量占比", "axis": "margin_barrier", "path": "natural_share",
         "kind": "gte", "pass": ad["natural_share_pass_min"], "warn": ad["natural_share_warn_min"],
         "unit": "占比", "action": "头部靠广告撑量，测算自己跟进时的 CPC 与 ACOS 上限再决定", "note": "头部自然流量占比过低说明全靠广告烧，跟进者成本更高"},
        {"id": "R09", "name": "1688 同款供应", "axis": "margin_barrier", "path": "supply_offers",
         "kind": "gte", "pass": supply["offers_pass_min"], "warn": supply["offers_warn_min"],
         "unit": "条", "action": "扩大 1688 搜索词（含同义/材质/规格），补足候选供应商", "note": "货源家数太少则议价与备选都受限"},
        {"id": "R10", "name": "起订量 MOQ", "axis": "margin_barrier", "path": "supply_moq_min",
         "kind": "lte", "pass": supply["moq_pass_max"], "warn": supply["moq_warn_max"],
         "unit": "件", "action": "与供应商谈小批量试单，或换支持低 MOQ 的工厂", "note": "MOQ 过高会吃掉首批现金流"},
        {"id": "R11", "name": "毛利率", "axis": "margin_barrier", "path": "gross_margin_pct",
         "kind": "gte", "pass": margin["gross_margin_pass_min"], "warn": margin["gross_margin_warn_min"],
         "fatal": margin["gross_margin_fatal_below"], "unit": "%", "hard": True,
         "action": "重算成本：换包装降抛重、换供应商、或用组合装抬高客单", "note": "售价扣除采购、头程、佣金、FBA 后的毛利率"},
        {"id": "R12", "name": "采购价 vs 成本上限", "axis": "margin_barrier", "path": "ceiling_headroom",
         "kind": "gte", "pass": margin["ceiling_headroom_pass_min"], "warn": margin["ceiling_headroom_warn_min"],
         "unit": "倍", "hard": True, "action": "按现价拿货已越过利润红线，必须压价或换货源后再立项", "note": "采购价上限 ÷ 实际采购价，<1 说明按现价拿货就是亏的"},
        {"id": "R15", "name": "采购+头程占售价", "axis": "margin_barrier", "path": "landed_cost_ratio",
         "kind": "lte", "pass": unit["landed_ratio_pass_max"], "warn": unit["landed_ratio_warn_max"], "unit": "占比",
         "action": "谈价或换供应商，压缩包装体积降头程；压不下来就提价或做组合装",
         "note": "双 20% 铁律之一（PB-07 / KC-QUANT-02）"},
        {"id": "R16", "name": "FBA 占售价", "axis": "margin_barrier", "path": "fba_fee_ratio",
         "kind": "lte", "pass": unit["fba_ratio_pass_max"], "warn": unit["fba_ratio_warn_max"], "unit": "占比",
         "action": "改包装压尺寸段（如压进标准件档）、减重，或提高客单价",
         "note": "双 20% 铁律之二；FBA 按旺季费率计"},
        {"id": "R13", "name": "IP 风险", "axis": "competition", "path": "ip_findings_count",
         # 没有“警告区间”：命中 0 条以外一律进待复核（warn=None 等价于 warn=∞，但不显示成 1000000）
         "kind": "lte", "pass": 0, "warn": None, "unit": "条",
         "action": "命中专利需人工复核权利要求与保护范围，复核前不要下单开模", "note": "关键词专利检索命中条数；命中≠侵权，复核前结论最高为“有条件做”"},
    ]


def evaluate(facts: Dict[str, Any], rule_ids: Optional[List[str]] = None,
             t: Optional[Dict[str, Any]] = None) -> List[RuleOutcome]:
    t = t or thresholds()
    out: List[RuleOutcome] = []
    for rd in _rule_defs(t):
        if rule_ids and rd["id"] not in rule_ids:
            continue
        raw = _get(facts, rd["path"])
        ev = (facts.get("evidence_refs") or {}).get(rd["path"], "")
        outcome = RuleOutcome(id=rd["id"], name=rd["name"], axis=rd["axis"], status=NO_DATA,
                              value=raw, threshold=_threshold_repr(rd), unit=rd.get("unit", ""),
                              note=rd.get("note", ""), action=rd.get("action", ""),
                              evidence_id=ev, hard=bool(rd.get("hard")))
        if raw is None or (isinstance(raw, str) and raw.strip() == ""):
            out.append(outcome)
            continue
        try:
            val = float(raw)
        except (TypeError, ValueError):
            out.append(outcome)
            continue
        kind = rd["kind"]
        if kind == "gte":
            status = PASS if val >= _num(rd.get("pass")) else WARN if val >= _num(rd.get("warn")) else FAIL
        elif kind == "lte":
            status = PASS if val <= _num(rd.get("pass")) else WARN if val <= _num(rd.get("warn")) else FAIL
        elif kind == "band":
            status = PASS if _num(rd.get("pass_lo")) <= val <= _num(rd.get("pass_hi")) else \
                WARN if _num(rd.get("warn_lo")) <= val <= _num(rd.get("warn_hi")) else FAIL
        else:
            status = NO_DATA
        outcome.status = status
        out.append(outcome)
    return out


def _num(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return float("inf")


def _threshold_repr(rd: Dict[str, Any]) -> Any:
    if rd["kind"] in ("gte", "lte"):
        return {"pass": rd.get("pass"), "warn": rd.get("warn"), "fatal": rd.get("fatal")}
    return {"pass": [rd.get("pass_lo"), rd.get("pass_hi")], "warn": [rd.get("warn_lo"), rd.get("warn_hi")]}


def summarize(outcomes: List[RuleOutcome]) -> Dict[str, Any]:
    counts = {PASS: 0, WARN: 0, FAIL: 0, NO_DATA: 0, SKIP: 0}
    for o in outcomes:
        counts[o.status] = counts.get(o.status, 0) + 1
    evaluated = counts[PASS] + counts[WARN] + counts[FAIL]
    return {
        "counts": counts,
        "evaluated": evaluated,
        "coverage": round(evaluated / len(outcomes), 3) if outcomes else 0.0,
        "hard_failures": [o.id for o in outcomes if o.hard and o.status == FAIL],
        "failed": [o.id for o in outcomes if o.status == FAIL],
        "warned": [o.id for o in outcomes if o.status == WARN],
        "missing": [o.id for o in outcomes if o.status == NO_DATA],
    }
