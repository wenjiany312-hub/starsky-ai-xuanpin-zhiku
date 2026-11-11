"""
事实归一化层：把各数据源的原始返回翻译成规则引擎唯一认的 facts 字典。

三条硬规矩
1. 只做算术与结构转换，不做“拍脑袋估值”；任何假设都单独列进 assumptions，
   让报告读的人一眼看到哪个数是估的。
2. 每个事实都记下证据编号（evidence_refs），报告里的数字能一路回溯到原始返回。
3. 算不出来就留空（None），规则会判成 NO_DATA；绝不填默认值冒充真实数据。
"""
import re
from statistics import median
from typing import Any, Dict, Iterable, List, Optional, Tuple

# 缺失事实的显式清单：引擎据此告诉用户“还差什么数据”
FACT_KEYS = (
    "keyword", "keyword_zh", "search_volume_weekly", "search_volume_trend_pct",
    "top3_click_share", "visible_asin_count", "median_reviews", "young_listing_share",
    "avg_price_usd", "price_iqr_ratio", "natural_share", "supply_offers",
    "supply_moq_min", "supply_price_cny_min", "gross_margin_pct", "ceiling_headroom",
    "ip_findings_count", "weeks_to_peak", "seasonality", "has_voc", "voc_count",
    "sale_price_usd", "purchase_cost_usd", "freight_usd", "fba_fee_usd",
    "referral_fee_usd", "net_profit_usd", "cost_ceiling_usd", "review_band_difficulty_pct",
    "landed_cost_ratio", "fba_fee_ratio", "size_tier", "cny_usd_rate",
    # 以图识货：模型看图得到的产品描述与关键词，以及 1688 以图搜同款 / 外观专利以图检索的命中
    "image_product_zh", "image_product_en", "image_keyword", "image_keyword_zh",
    "image_confidence", "image_category", "image_features", "image_risk_flags",
    "image_url", "image_supply_offers", "image_supply_price_cny_min",
    "image_supply_samples", "image_ip_findings_count", "image_ip_samples",
)


# ---------------- 基础工具 ----------------

def _f(value: Any) -> Optional[float]:
    """把 "$1,299.00"、"38.5%"、"1.2万" 这类串转成数字；转不了返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = value.strip().replace(",", "").replace("$", "").replace("¥", "").replace("￥", "").rstrip("+").strip()
        mult = 1.0
        if s.endswith("万"):
            s, mult = s[:-1], 10000.0
        elif s[-1:] in ("k", "K"):
            s, mult = s[:-1], 1000.0
        elif s.endswith("%"):
            s, mult = s[:-1], 1.0
        try:
            return float(s) * mult
        except ValueError:
            return None
    return None


def _ratio(value: Any) -> Optional[float]:
    """占比统一成 0~1；源里混用 42 与 0.42 两种写法，这里按量级归一。"""
    v = _f(value)
    if v is None:
        return None
    if v > 1.5:
        v = v / 100.0
    return round(v, 4)


def _median(values: Iterable[Any]) -> Optional[float]:
    nums = [x for x in (_f(v) for v in values) if x is not None]
    return round(median(nums), 2) if nums else None


def _quantile(nums: List[float], q: float) -> Optional[float]:
    if not nums:
        return None
    s = sorted(nums)
    if len(s) == 1:
        return s[0]
    pos = (len(s) - 1) * q
    lo, hi = int(pos), min(int(pos) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def deep_get(data: Any, *paths: str, default: Any = None) -> Any:
    """防御式取数：依次尝试 "a.b.c" / "a.0.b" 路径。"""
    for path in paths:
        node, ok = data, True
        for seg in path.split("."):
            if isinstance(node, dict) and seg in node:
                node = node[seg]
            elif isinstance(node, list) and seg.lstrip("-").isdigit():
                idx = int(seg)
                if -len(node) <= idx < len(node):
                    node = node[idx]
                else:
                    ok = False
                    break
            else:
                ok = False
                break
        if ok and node not in (None, "", [], {}):
            return node
    return default


def _dicts(obj: Any, depth: int = 0) -> List[Dict[str, Any]]:
    """递归收集所有 dict（限深），用于兼容各数据源不统一的返回结构。"""
    out: List[Dict[str, Any]] = []
    if depth > 5:
        return out
    if isinstance(obj, dict):
        out.append(obj)
        for v in obj.values():
            out.extend(_dicts(v, depth + 1))
    elif isinstance(obj, list):
        for v in obj[:200]:
            out.extend(_dicts(v, depth + 1))
    return out


def _lists(obj: Any, depth: int = 0) -> List[List[Any]]:
    out: List[List[Any]] = []
    if depth > 5:
        return out
    if isinstance(obj, list):
        out.append(obj)
        for v in obj[:50]:
            out.extend(_lists(v, depth + 1))
    elif isinstance(obj, dict):
        for v in obj.values():
            out.extend(_lists(v, depth + 1))
    return out


_ASIN_IN = re.compile(r"\b(B0[A-Z0-9]{8})\b")


def clean_asin(value: Any) -> Optional[str]:
    """SIF 会把 ASIN 包成 [B0xx](https://...) 的 Markdown 链接，这里取回纯 ASIN。"""
    if not value:
        return None
    m = _ASIN_IN.search(str(value))
    return m.group(1) if m else str(value)[:20]


def _first_num(mapping: Dict[str, Any], keys: Tuple[str, ...]) -> Optional[float]:
    for k in keys:
        if k in mapping:
            v = _f(mapping[k])
            if v is not None:
                return v
    return None


# ---------------- 市场：SIF ----------------

def _trend_from_series(vals: List[Optional[float]]) -> Dict[str, Any]:
    """周序列 → 趋势%。够一年用同比（最近 4 周 vs 去年同期 4 周），不足一年用环比并写明会失真。"""
    out: Dict[str, Any] = {}
    if len(vals) < 8:
        return out
    recent = [x for x in vals[-4:] if x is not None]
    if len(vals) >= 56:
        prior = [x for x in vals[-56:-52] if x is not None]
        basis = "近 4 周 vs 去年同期 4 周（同比）"
    else:
        prior = [x for x in vals[-8:-4] if x is not None]
        basis = "近 4 周 vs 前 4 周（环比，历史不足一年，季节品会失真）"
    if recent and prior and sum(prior):
        r, p = sum(recent) / len(recent), sum(prior) / len(prior)
        out["search_volume_trend_pct"] = round((r - p) / p * 100, 1)
        out["trend_basis"] = basis
    return out


def parse_sif_trend(payload: Any) -> Dict[str, Any]:
    """market_get_keyword_root_trend → 周搜索量 + 趋势%。"""
    out: Dict[str, Any] = {}
    latest = (payload or {}).get("latest") if isinstance(payload, dict) else None
    if isinstance(latest, dict):
        out["search_volume_weekly"] = _first_num(
            latest, ("keyword_search_volume", "search_volume", "weeklySearchVolume"))
        out["keyword_rank"] = _first_num(latest, ("keyword_rank", "rank"))
        out["demand_structure"] = deep_get(latest, "exact_keyword_demand_pct", default=None)
        out["longtail_share"] = _ratio(deep_get(latest, "longtail_demand_pct", default=None))
    volumes = None
    if isinstance(payload, dict):
        volumes = payload.get("keyword_search_volumes") or payload.get("search_volumes")
    if isinstance(volumes, list):
        out.update(_trend_from_series([_f(v) for v in volumes]))
    return out


def parse_xydc_trend(payload: Any) -> Dict[str, Any]:
    """
    西柚 get_keyword_aba_trends → 周搜索量 + 趋势%。
    真实结构：data.entities[].trends[] = {reportFromDate, weeklySearchVolume, searchFrequencyRank}
    备源取到数也要能填进事实表，否则「回落成功」只是把覆盖率换个地方造出来。
    """
    out: Dict[str, Any] = {}
    ents = deep_get(payload, "data.entities", "entities", default=None)
    rows: List[Dict[str, Any]] = []
    if isinstance(ents, list):
        for e in ents:
            if isinstance(e, dict) and isinstance(e.get("trends"), list) and e["trends"]:
                rows = [t for t in e["trends"] if isinstance(t, dict)]
                break
    if not rows:
        return out
    vols = [_f(t.get("weeklySearchVolume")) for t in rows]
    vols = [v for v in vols if v is not None]
    if vols:
        out["search_volume_weekly"] = vols[-1]
    ranks = [_f(t.get("searchFrequencyRank")) for t in rows]
    ranks = [r for r in ranks if r is not None]
    if ranks:
        out["keyword_rank"] = ranks[-1]
    out.update(_trend_from_series([_f(t.get("weeklySearchVolume")) for t in rows]))
    return out


def parse_sif_competition(payload: Any, young_days: int = 365, low_review_max: int = 30,
                          ref_reviews_min: int = 100, ref_reviews_max: int = 200) -> Dict[str, Any]:
    """market_get_keyword_competition → 集中度 / 链接数 / 评论门槛 / 新品占比 / 价格带 / 自然流量占比。"""
    out: Dict[str, Any] = {}
    if not isinstance(payload, dict):
        return out
    ms = payload.get("market_structure") or {}
    out["top3_click_share"] = _ratio(deep_get(ms, "top3_click_share", "top3ClickShare", default=None))
    out["concentration_level"] = deep_get(ms, "concentration_level", default=None)
    out["competition_mode"] = deep_get(ms, "competition_mode", default=None)
    out["visible_asin_count"] = _f(deep_get(
        ms, "visible_asin_count", "asin_count", default=None)) or _f(deep_get(
        payload, "supply_profile.latest_total_asin_num", default=None))
    comps = payload.get("top_competitors") or []
    comps = [c for c in comps if isinstance(c, dict)]
    if comps:
        prices = [_f(c.get("price")) for c in comps]
        prices = [p for p in prices if p and p > 0]
        out["avg_price_usd"] = _median(prices)
        if len(prices) >= 4:
            mid = median(prices)
            if mid:
                out["price_iqr_ratio"] = round(
                    (_quantile(prices, 0.75) - _quantile(prices, 0.25)) / mid, 3)
        reviews = [_f(c.get("review_count")) for c in comps]
        out["median_reviews"] = _median(reviews)
        ages = [_f(deep_get(c, "listed_age_days", "listing_age_days", default=None)) for c in comps]
        ages = [a for a in ages if a is not None]
        if ages:
            out["young_listing_share"] = round(sum(1 for a in ages if a <= young_days) / len(ages), 3)
        # 星空评论区间法：A=低评论相似品平均月销，B=前 5 平均月销（SIF 头部按流量份额排序）
        top5 = [_f(c.get("monthly_orders")) for c in comps[:5]]
        top5 = [x for x in top5 if x is not None]
        low = [_f(c.get("monthly_orders")) for c in comps
               if (_f(c.get("review_count")) is not None and _f(c.get("review_count")) <= low_review_max)]
        low = [x for x in low if x is not None]
        if top5 and low and sum(top5) > 0:
            out["review_band_difficulty_pct"] = round(
                (sum(low) / len(low)) / (sum(top5) / len(top5)) * 100, 1)
            out["low_review_sellers"] = len(low)
        # 自然流量占比 = 自然点击份额 ÷ (自然 + SP + 品牌 + 视频)，同分母不重复加总
        organic = sum(v for v in (_f(c.get("organic_share")) for c in comps) if v)
        ad = sum(v for v in (_f(c.get("sp_share")) for c in comps) if v)
        ad += sum(v for v in (_f(c.get("brand_share")) for c in comps) if v)
        ad += sum(v for v in (_f(c.get("video_share")) for c in comps) if v)
        if organic + ad > 0:
            out["natural_share"] = round(organic / (organic + ad), 3)
        # 六道门的体量门与未饱和：站内头部月销取样本最大值（代理指标，非断崖法口径）；
        # 参照档按方法取 100–200 评论区间的中位月销，样本不足就不出这个数。
        orders = [o for o in (_f(c.get("monthly_orders")) for c in comps) if o and o > 0]
        if orders:
            out["head_monthly_sales"] = max(orders)
        ref = [o for o, r in ((_f(c.get("monthly_orders")), _f(c.get("review_count"))) for c in comps)
               if o and o > 0 and r is not None and ref_reviews_min <= r <= ref_reviews_max]
        if ref and low:
            out["unsaturation_ratio"] = round((sum(low) / len(low)) / median(ref), 3)
            out["unsaturation_ref_n"] = len(ref)
        out["top_competitors_sample"] = [
            {"asin": clean_asin(c.get("asin")), "price": _f(c.get("price")), "rating": _f(c.get("rating")),
             "reviews": _f(c.get("review_count")), "monthly_orders": _f(c.get("monthly_orders")),
             "organic_share": _f(c.get("organic_share")), "sp_share": _f(c.get("sp_share")),
             "listed_age_days": _f(deep_get(c, "listed_age_days", "listing_age_days", default=None))}
            for c in comps[:10]
        ]
    return out


def parse_sif_demand(payload: Any, keyword: str = "") -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    profiles = (payload or {}).get("profiles") if isinstance(payload, dict) else None
    if isinstance(profiles, list) and profiles:
        hit = next((p for p in profiles if isinstance(p, dict)
                    and str(p.get("keyword", "")).lower() == keyword.lower()), profiles[0])
        out["weeks_to_peak"] = _f(deep_get(hit, "weeks_to_peak", default=None))
        out["peak_month"] = deep_get(hit, "peak_month", default=None)
        season = deep_get(hit, "seasonality", default=None)
        if isinstance(season, dict):
            out["seasonality"] = deep_get(season, "pattern", "seasonal_pattern", "label", default=None)
        elif season:
            out["seasonality"] = str(season)[:60]
        yoy = _f(deep_get(hit, "trend.yoy_change", default=None))
        if yoy is not None:
            out["search_volume_trend_pct"] = round(yoy * 100 if abs(yoy) <= 20 else yoy, 1)
            out["trend_basis"] = "SIF 需求画像同比（±2 周均值）"
        out["season_position"] = deep_get(hit, "current.season_position", default=None)
        out["annual_growth_pct"] = (lambda v: round(v * 100, 1) if v is not None else None)(
            _f(deep_get(hit, "trend.annual_decay_rate", default=None)))
        out["demand_diagnosis"] = deep_get(hit, "diagnosis", default=None)
        out["demand_interpretation"] = deep_get(hit, "interpretation", default=None)
    return out


# ---------------- 市场：西柚（SIF 不可用时的备源） ----------------

def parse_xydc_keyword_info(payload: Any, keyword: str = "") -> Dict[str, Any]:
    """
    西柚 get_keyword_info → 周搜索量 / ABA 排名 / Top3 点击集中度 / 点击转化率。
    真实结构：data.list[] = {searchTerm, abaReport:{weeklySearchVolume, searchFrequencyRank, topAsins[]},
                            clickConversionRate}
    注意词名在 searchTerm，不在 keyword；数值在 abaReport 里，不在顶层。
    """
    out: Dict[str, Any] = {}
    rows = deep_get(payload, "data.list", "keywords", "list", "data", default=None)
    if not isinstance(rows, list):
        return out
    rows = [r for r in rows if isinstance(r, dict)]
    if not rows:
        return out
    kw = (keyword or "").strip().lower()
    hit = next((r for r in rows
                if str(r.get("searchTerm") or r.get("keyword") or "").strip().lower() == kw), rows[0])
    aba = deep_get(hit, "abaReport", default=None)
    aba = aba if isinstance(aba, dict) else {}
    vol = _first_num(aba, ("weeklySearchVolume", "searchVolume"))
    if vol is None:
        vol = _first_num(hit, ("weeklySearchVolume", "searchVolume", "weekly_search_volume"))
    if vol is not None:
        out["search_volume_weekly"] = vol
    rank = _f(deep_get(aba, "searchFrequencyRank", default=None)) or \
        _f(deep_get(hit, "searchFrequencyRank", "rank", default=None))
    if rank is not None:
        out["keyword_rank"] = rank
    # Top3 点击集中度 = ABA 前三 ASIN 的点击份额之和（与 SIF 的 top3_click_share 同口径）
    top = deep_get(aba, "topAsins", "top3", default=None)
    if isinstance(top, list) and len([x for x in top if isinstance(x, dict)]) >= 3:
        shares = [_ratio(x.get("clickShare")) for x in top[:3] if isinstance(x, dict)]
        shares = [s for s in shares if s is not None]
        if len(shares) == 3:
            out["top3_click_share"] = round(sum(shares), 4)
    rate = _ratio(deep_get(hit, "clickConversionRate", "adjustedClickConversionRate", default=None))
    if rate is not None:
        out["click_conversion_rate"] = rate
    return out


def parse_xydc_competition(payload: Any) -> Dict[str, Any]:
    """
    西柚 get_keyword_asin_analysis → 集中度 / 链接数 / 评论门槛 / 价格带 / 自然流量占比。
    真实结构：data.list[] = {asin, asinInfo:{price, ratings, stars, title},
                            trafficSummary:{traffic:{organic,total}, trafficRatio:{total}}}
    """
    out: Dict[str, Any] = {}
    rows = deep_get(payload, "data.list", "asins", "list", default=None)
    if not isinstance(rows, list):
        return out
    rows = [r for r in rows if isinstance(r, dict)]
    if not rows:
        return out

    shares = [_ratio(deep_get(r, "trafficSummary.trafficRatio.total", "trafficRatio", default=None))
              for r in rows]
    shares = [s for s in shares if s is not None]
    if len(shares) >= 3:
        out["top3_click_share"] = round(sum(sorted(shares, reverse=True)[:3]), 4)
    total = _f(deep_get(payload, "data.total", "total", default=None))
    out["visible_asin_count"] = total or float(len(rows))

    prices = [_f(deep_get(r, "asinInfo.price", "price", default=None)) for r in rows]
    prices = [p for p in prices if p and p > 0]
    if prices:
        out["avg_price_usd"] = _median(prices)
        if len(prices) >= 4:
            mid = median(prices)
            if mid:
                out["price_iqr_ratio"] = round(
                    (_quantile(prices, 0.75) - _quantile(prices, 0.25)) / mid, 3)
    reviews = [_f(deep_get(r, "asinInfo.ratings", "ratings", default=None)) for r in rows]
    if any(r is not None for r in reviews):
        out["median_reviews"] = _median(reviews)
    # 自然流量占比：各竞品的自然 ÷ 总流量得分，取平均（同分母，不重复加总）
    naturals = []
    for r in rows:
        organic = _f(deep_get(r, "trafficSummary.traffic.organic", default=None))
        whole = _f(deep_get(r, "trafficSummary.traffic.total", default=None))
        if organic is not None and whole:
            naturals.append(organic / whole)
    if naturals:
        out["natural_share"] = round(sum(naturals) / len(naturals), 3)
    out["top_competitors_sample"] = [
        {"asin": clean_asin(r.get("asin")),
         "price": _f(deep_get(r, "asinInfo.price", "price", default=None)),
         "rating": _f(deep_get(r, "asinInfo.stars", "stars", default=None)),
         "reviews": _f(deep_get(r, "asinInfo.ratings", "ratings", default=None)),
         "organic_share": _ratio(deep_get(r, "trafficSummary.trafficRatio.organic", default=None)),
         "sp_share": _ratio(deep_get(r, "trafficSummary.trafficRatio.advertising", default=None))}
        for r in rows[:10]
    ]
    return out


# ---------------- ASIN 档案：SIF / 西柚 两种形状归一 ----------------

_UNIT_OZ = {"oz": 1.0, "ounce": 1.0, "ounces": 1.0, "g": 1 / 28.3495, "gram": 1 / 28.3495,
            "grams": 1 / 28.3495, "kg": 35.274, "lb": 16.0, "lbs": 16.0, "pound": 16.0, "pounds": 16.0}
_UNIT_IN = {"in": 1.0, "inch": 1.0, "inches": 1.0, '"': 1.0, "cm": 1 / 2.54, "mm": 1 / 25.4}


def _weight_oz(text: Any) -> Optional[float]:
    """"862 g" / "1.9 pounds" / 862 → 盎司。认不出单位就返回 None，不猜。"""
    if isinstance(text, (int, float)):
        return float(text)
    m = re.search(r"([\d.]+)\s*([a-zA-Z\"″]*)", str(text or ""))
    if not m:
        return None
    unit = (m.group(2) or "oz").lower()
    factor = _UNIT_OZ.get(unit)
    return round(float(m.group(1)) * factor, 2) if factor else None


def _dims_in(text: Any) -> Dict[str, float]:
    """"62.9 x 12.7 x 12.7 cm" → {length_in, width_in, height_in}（英寸）。"""
    s = str(text or "").replace("×", "x").replace("*", "x")
    # 单位取结尾那一个：按位置取会把中间的 "x" 当单位（"62.9 x …" 的第一段单位是 x）
    unit_m = re.search(r"(cm|mm|inches|inch|in|\"|″)\s*$", s, re.I)
    factor = _UNIT_IN.get((unit_m.group(1) if unit_m else "in").lower())
    if not factor:
        return {}
    nums = re.findall(r"\d+(?:\.\d+)?", s)
    if len(nums) < 3:
        return {}
    vals = [round(float(n) * factor, 2) for n in nums[:3]]
    # 最长边当 length，保证 FBA 尺寸分档用的是真实长宽高而不是录入顺序
    vals.sort(reverse=True)
    return {"length_in": vals[0], "width_in": vals[1], "height_in": vals[2]}


def parse_asin_profile(payload: Any) -> Dict[str, Any]:
    """
    ASIN 档案统一成一套字段，供成本模型与报告直接消费。

    SIF：list[0] = {price, star_rating, rating_num, package_dims_in, package_weight_oz, bsr_list[]}
    西柚：data.entities[0] = {price, stars, ratings, size:{package:"62.9 x 12.7 x 12.7 cm"},
                            weight:{package:"862 g"}, fbaFee:"11.97", referralFeeRatio:"0.15",
                            classificationRanks:[{displayName, rank}]}

    西柚直接给了亚马逊实际 FBA 费，比按 $4.5 粗估或另调费率表都准——有就用它，
    没有才回落到别的来源，并在报告里写明是哪个口径。
    """
    out: Dict[str, Any] = {}
    row = deep_get(payload, "list.0", "data.list.0", "data.entities.0", default=None)
    if not isinstance(row, dict):
        return out
    # 趋势行也长在 data.entities 底下：一个档案字段都没有时说明这不是 ASIN 档案，别硬凑
    if not any(k in row for k in ("price", "size", "weight", "fbaFee", "package_weight_oz",
                                  "package_dims_in", "dims_in", "asinInfo")):
        return out
    out["asin"] = clean_asin(row.get("asin"))
    price = _f(row.get("price"))
    if price:
        out["price_usd"] = price
    stars = _f(row.get("star_rating") if row.get("star_rating") is not None else row.get("stars"))
    if stars is not None:
        out["star_rating"] = stars
    reviews = _f(row.get("rating_num") if row.get("rating_num") is not None else row.get("ratings"))
    if reviews is not None:
        out["review_count"] = int(reviews)
    w = row.get("package_weight_oz")
    weight = _f(w) if w is not None else _weight_oz(deep_get(row, "weight.package", "weight.product", default=None))
    if weight:
        out["weight_oz"] = weight
    dims = row.get("package_dims_in") or row.get("dims_in")
    if not isinstance(dims, dict):
        dims = _dims_in(deep_get(row, "size.package", "size.product", default=None))
    if isinstance(dims, dict) and all(_f(dims.get(k)) for k in ("length_in", "width_in", "height_in")):
        out["dims_in"] = {k: _f(dims.get(k)) for k in ("length_in", "width_in", "height_in")}
    fee = _f(row.get("fbaFee") if row.get("fbaFee") is not None else row.get("fba_fee_usd"))
    if fee:
        out["fba_fee_usd"] = fee
    ref = _ratio(row.get("referralFeeRatio") if row.get("referralFeeRatio") is not None
                 else row.get("referral_fee_ratio"))
    if ref:
        out["referral_pct"] = ref
    cat = deep_get(row, "bsr_list.0.category_name", default=None) \
        or deep_get(row, "classificationRanks.0.displayName", default=None) \
        or deep_get(row, "amzCategoryTree.browseName.original", default=None)
    if cat:
        out["category"] = cat
    return out


# ---------------- 供应链：1688 ----------------

_PRICE_KEYS = ("price", "price_min", "price_range_min", "unit_price", "min_price",
               "lowest_price", "priceInfo", "price_range", "price_hint")
# 本地 1688_scraper 的 offer 只有 price_hint（搜索卡片上抓到的价格串）；判定“像报价记录”时认它
_PRICE_STRICT = ("price", "price_min", "price_range_min", "unit_price", "price_hint")
_OFFER_TEXT_KEYS = ("title", "card_text", "name", "subject", "product_title", "offer_title")
_MOQ_KEYS = ("min_order_quantity", "moq", "minOrderQuantity", "start_quantity",
             "min_order", "minQuantity", "begin_amount", "startQuantity")
_SUPPLIER_KEYS = ("company_name", "supplier", "seller_name", "shop_name", "companyName", "store_name")
_TITLE_KEYS = ("title", "name", "subject", "product_title", "offer_title")


def _query_grams(query: str) -> List[str]:
    """把商品词切成能跟 1688 标题比对的片段：中文走 2 字滑窗，英文/数字按词。

    1688 的标题是卖家自己拼的长串，拿整句去比永远对不上；按 2 字片段看「像不像同一个东西」，
    才挡得住「搜不到就回落到别的类目」那种搜索页。
    """
    q = re.sub(r"[^\w一-鿿]+", " ", query or "").strip().lower()
    grams: List[str] = []
    for part in q.split():
        for run in re.findall(r"[一-鿿]+", part):
            if len(run) == 1:
                grams.append(run)
            else:
                grams.extend(run[i:i + 2] for i in range(len(run) - 1))
        grams.extend(re.findall(r"[a-z0-9]{3,}", part))
    return list(dict.fromkeys(grams))


def _offer_relevance(rec: Dict[str, Any], grams: List[str]) -> float:
    """这条 offer 跟搜索词的重合度（0~1）。没有可用搜索词时一律算相关。"""
    if not grams:
        return 1.0
    text = " ".join(str(rec.get(k) or "") for k in _OFFER_TEXT_KEYS).lower()
    if not text.strip():
        return 0.0
    return sum(1 for g in grams if g in text) / len(grams)


def _fetched(src: Dict[str, Any]) -> bool:
    """这一路查成功了（哪怕回的是空）。失败的来源没有 payload 或带着 error，不能当「0 条」记。"""
    return bool(src.get("eid")) and not src.get("error") and "payload" in src


def parse_1688(payload: Any, query: str = "") -> Dict[str, Any]:
    """
    兼容 sorftime ali1688_similar_product 与本地 1688_scraper search_offers 两套字段。
    只统计能确认是“报价记录”的条目：含价格或 MOQ，避免把说明文字当供应商。

    给了 query 还要过一道相关性：1688 搜不到东西时会照常返回一张「别的产品」的搜索页，
    页面 10 条 offer 每条都带价格（实测搜宠物棉签回来的是减压阀，价格还全是 ¥107）。
    不看相关性就采信，报告会把这个价写成该产品的采购价——比承认没数据糟得多。
    """
    grams = _query_grams(query)
    out: Dict[str, Any] = {}
    records: List[Dict[str, Any]] = []
    raw = 0
    for group in _lists(payload):
        priced = [x for x in group if isinstance(x, dict)
                  and _first_num(x, _PRICE_KEYS) is not None
                  and _first_num(x, _PRICE_STRICT) is not None]
        if not priced:
            continue
        raw = max(raw, len(priced))
        hits = [x for x in priced if _offer_relevance(x, grams) >= 0.34]
        if len(hits) >= 2 and len(hits) >= len(records):
            records = hits
    if not records:
        # 有返回但一条都对不上：这是「搜到的不是这个产品」，不是「1688 没连上」
        if raw:
            out["supply_raw_count"] = raw
        return out
    prices, moqs, suppliers, titles = [], [], set(), []
    for rec in records:
        p = _first_num(rec, _PRICE_KEYS)
        if p is not None and p > 0:
            prices.append(p)
        m = _first_num(rec, _MOQ_KEYS)
        if m is not None and m > 0:  # 0 = 源里没填，不是“零起订”
            moqs.append(m)
        sup = next((v for k, v in rec.items() if k in _SUPPLIER_KEYS and isinstance(v, str) and v.strip()), None)
        if sup:
            suppliers.add(sup.strip())
        t = next((v for k, v in rec.items() if k in _TITLE_KEYS and isinstance(v, str) and v.strip()), None)
        if t:
            titles.append(t.strip()[:80])
    out["supply_offers"] = len(records)
    out["supply_supplier_count"] = len(suppliers) or None
    out["supply_price_cny_min"] = round(min(prices), 2) if prices else None
    out["supply_price_cny_median"] = _median(prices)
    out["supply_moq_min"] = min(moqs) if moqs else None
    out["supply_moq_median"] = _median(moqs)
    out["supply_samples"] = titles[:5]
    return out


def supply_offer_cards(payload: Any, query: str = "", limit: int = 6) -> List[Dict[str, Any]]:
    """给报告配图用：挑出与搜索词相关、带主图的 1688 报价，按相关度取前 limit 条。"""
    grams = _query_grams(query)
    best: List[Dict[str, Any]] = []
    for group in _lists(payload):
        priced = [x for x in group if isinstance(x, dict) and _first_num(x, _PRICE_KEYS) is not None]
        hits = [x for x in priced if _offer_relevance(x, grams) >= 0.34]
        if len(hits) > len(best):
            best = hits
    best = sorted(best, key=lambda r: (not (_first_num(r, _PRICE_KEYS) or 0) > 0, -_offer_relevance(r, grams)))[:limit]  # 有标价的排前面
    cards: List[Dict[str, Any]] = []
    for r in best:
        moq = _first_num(r, _MOQ_KEYS)
        if moq is None:
            tiers = r.get("wholesale_price_range")
            if isinstance(tiers, list) and tiers and isinstance(tiers[0], dict):
                m = re.search(r"\d+", str(tiers[0].get("purchase_quantity") or ""))
                moq = float(m.group()) if m else None
        cards.append({
            "image": str(r.get("photo") or r.get("image") or r.get("img") or r.get("pic") or "").strip(),
            "title": next((str(r[k]).strip() for k in _TITLE_KEYS if r.get(k)), ""),
            "price_cny": _first_num(r, _PRICE_KEYS),
            "moq": moq,
            "supplier": next((str(r[k]).strip() for k in _SUPPLIER_KEYS if r.get(k)), ""),
            "sales_30d": _first_num(r, ("sales_of_30d", "sales_30d", "sold_30d")),
            "url": str(r.get("url") or r.get("link") or "").strip(),
        })
    return cards


def ref_product_card(payload: Any) -> Dict[str, Any]:
    """给报告配图用：参照竞品（ASIN 画像）的主图与要点。取不到就返回空。"""
    rec = None
    for group in _lists(payload):
        for x in group:
            if isinstance(x, dict) and (x.get("img") or x.get("image") or x.get("main_image")):
                rec = x
                break
        if rec:
            break
    if not rec:
        return {}
    asin = str(rec.get("asin") or "")
    m = re.search(r"[A-Z0-9]{10}", asin)
    return {
        "asin": m.group() if m else "",
        "image": str(rec.get("img") or rec.get("image") or rec.get("main_image") or "").strip(),
        "title": str(rec.get("title") or "").strip(),
        "brand": str(rec.get("brand") or "").strip(),
        "price": _f(rec.get("price")),
        "rating": _f(rec.get("star_rating") or rec.get("rating")),
        "reviews": _f(rec.get("rating_num") or rec.get("reviews")),
        "monthly_sales": _f(rec.get("bought_in_past_month") or rec.get("monthly_sales")),
        "listed": str(rec.get("first_available_day") or "").strip(),
        "variations": _f(rec.get("variation_num")),
    }


def parse_patents(payload: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if payload is None:
        return out
    rows = None
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = next((payload[k] for k in ("patents", "list", "items", "data", "records")
                     if isinstance(payload.get(k), list)), None)
    if isinstance(rows, list):  # 空列表 = 检索过、0 命中，是有效结果
        out["ip_findings_count"] = len([r for r in rows if isinstance(r, dict)])
        out["ip_samples"] = [
            {"no": deep_get(r, "publication_no", "publicationNo", default=None),
             "title": deep_get(r, "title", "name", default=None)}
            for r in rows[:5] if isinstance(r, dict)
        ]
    elif isinstance(payload, dict):
        total = _f(deep_get(payload, "total", "count", default=None))
        if total is not None:
            out["ip_findings_count"] = total
    return out


def _voc_line(d: Dict[str, Any]) -> Optional[str]:
    """把一条讨论压成一行可引用的话：有标题用标题，没有就用摘要 + 类型/情绪/提及物。"""
    title = next((d[k].strip() for k in ("title", "question", "post_title")
                  if isinstance(d.get(k), str) and d[k].strip()), None)
    if title:
        return title[:140]
    summary = d.get("summary") or d.get("content")
    if not isinstance(summary, str) or not summary.strip():
        return None
    tags = [str(d[k]) for k in ("post_type", "main_sentiment", "main_post_sentiment")
            if isinstance(d.get(k), str) and d[k].strip()]
    product = d.get("product")
    if isinstance(product, str) and product.strip():
        tags.append(f"提及 {product.strip()}")
    head = f"[{'／'.join(tags)}] " if tags else ""
    text = head + summary.strip()
    if len(text) <= 420:
        return text
    # 截在句子边界上：半句话既不好读，翻译出来也是半句
    cut = text[:420]
    for sep in ("。", ". ", "! ", "? "):
        pos = cut.rfind(sep)
        if pos >= 320:
            return cut[:pos + len(sep)].rstrip()
    return cut.rstrip() + "…"


# Reddit/Quora 返回的是英文枚举，报告读者是中文客户——落进报告前先换成人话
VOC_TAG_ZH = {
    "experience_share": "经验分享", "experience sharing": "经验分享",
    "purchase_advice": "购买建议", "purchase_consultation": "购买咨询",
    "complaint": "吐槽", "help_request": "求助", "question": "提问", "inquiry": "咨询",
    "problem": "问题", "discussion": "讨论", "innovation": "创新点子", "other": "其他",
    "review": "评价", "recommendation": "推荐", "comparison": "对比", "showcase": "晒单",
    "positive": "正面", "negative": "负面", "neutral": "中性",
}


_TRUNC_END = "。.!?？!…\"'”’)）」』—"
_DANGLING = {"and", "or", "with", "the", "a", "an", "to", "of", "for", "in", "on",
             "but", "despite", "while", "that", "as", "by", "at", "from", "is", "are"}


def tidy_excerpt(text: str) -> str:
    """承认「这段是摘录」：源站常在 150 字符处硬切，末尾留半个词（and de / trainin）。

    报告里出现「以及 de」只会让读者觉得系统坏了。所以去掉被切断的残词，
    并以省略号收尾——译文也照实收在省略号上，不假装是完整句子。
    """
    t = (text or "").strip()
    if len(t) < 120 or t[-1] in _TRUNC_END:
        return t
    cut = t
    # 先摘掉被切断的残词，再摘掉吊在句尾的虚词（否则中文译成「以及…」）
    head, sep, tail = cut.rpartition(" ")
    if sep:
        cut = head
    while True:
        head, sep, word = cut.rpartition(" ")
        if sep and word.strip(",;:").lower() in _DANGLING:
            cut = head
            continue
        break
    return (cut or t).strip(" ,;:") + "…"


def voc_parts(line: str) -> Tuple[str, str]:
    """把 `[类型／情绪／提及 X] 正文` 拆成（中文标签, 正文）；没有标签时标签为空。"""
    m = re.match(r"^\[([^\]]{1,160})\]\s*(.*)$", line or "", re.S)
    if not m:
        return "", tidy_excerpt((line or "").strip())
    tags: List[str] = []
    for raw in re.split(r"[／/]", m.group(1)):
        t = raw.strip()
        if not t:
            continue
        if t.startswith("提及"):
            tags.append(t)
        else:
            tags.append(VOC_TAG_ZH.get(t.lower().replace("-", "_").replace(" ", "_"), t))
    return "／".join(tags), tidy_excerpt((m.group(2) or "").strip())


def voc_line_zh(line: str, body_zh: Optional[str]) -> str:
    """中文在前：`[经验分享／正面／提及 猫抓板] 译文`；译文缺失时保留英文原文。"""
    tags, body = voc_parts(line)
    text = (body_zh or "").strip() or body
    return f"[{tags}] {text}" if tags else text


_VOC_STOP = {"a", "an", "the", "for", "with", "and", "or", "of", "to", "in", "on", "my", "your", "up", "set",
             "gift", "gifts", "best", "new", "pack", "pcs", "kit"}


def _voc_core_tokens(keyword: str) -> List[str]:
    toks = [t for t in re.findall(r"[a-z0-9]+", (keyword or "").lower()) if t not in _VOC_STOP and len(t) > 1]
    # 简单去复数，让 leash/leashes、bear/bears 互相认得
    return [t[:-1] if len(t) > 3 and t.endswith("s") else t for t in dict.fromkeys(toks)]


def voc_relevant(line: str, keyword: str) -> bool:
    """这条讨论是不是在说这个产品。

    Reddit 是按词搜的，"rose bear gift" 会搜出猎熊、熊游泳，"light up dog leash" 会搜出婴儿牵引绳。
    没有产品词时不过滤（调用方没给词就别乱砍）；核心词 ≤2 个要全中，更多时中 60% 以上。
    """
    core = _voc_core_tokens(keyword)
    if not core:
        return True
    text = (line or "").lower()
    hit = sum(1 for t in core if re.search(r"(?<![a-z0-9])" + re.escape(t), text))
    need = len(core) if len(core) <= 2 else -(-len(core) * 6 // 10)
    return hit >= need


def parse_voc(payload: Any, keyword: str = "") -> Dict[str, Any]:
    """Reddit/Quora 讨论：只取标题/摘要样本，供差异化判断与报告引用。

    Sorftime 的帖子没有 title，只有 summary，早先按 title 取导致 40 条真实讨论
    一条都没进事实，差异化维度因此白白判成“无 VOC”。这里按 summary 兜底。
    给了 keyword 就剔除与产品无关的帖子，剔除条数记在 voc_total / voc_dropped。
    """
    out: Dict[str, Any] = {}
    if isinstance(payload, str):
        return out
    # 返回体顶层的 doc 是字段说明本身，它的 "summary" 也是一句英文说明——别当用户原话抄进报告
    if isinstance(payload, dict):
        payload = {k: v for k, v in payload.items()
                   if str(k).lower() not in ("doc", "docs", "schema", "field_descriptions", "description_map")} or payload
    samples: List[str] = []
    seen = set()
    total = 0
    for d in _dicts(payload):
        line = _voc_line(d)
        if not line or line in seen:
            continue
        seen.add(line)
        total += 1
        if keyword and not voc_relevant(line, keyword):
            continue
        if len(samples) < 8:
            samples.append(line)
    out["voc_total"] = total
    out["voc_dropped"] = total - len(samples) if keyword else 0
    if samples:
        out["has_voc"] = True
        out["voc_samples"] = samples
    return out


# ---------------- 经济模型 ----------------

def economics(sale_price: Optional[float], purchase_cost_cny: Optional[float], *,
              cny_usd: float = 7.15, freight_usd: Optional[float] = None,
              fba_fee_usd: Optional[float] = None, referral_pct: float = 0.15,
              target_margin_pct: float = 35.0,
              freight_multiple: float = 0.35) -> Dict[str, Any]:
    """
    落地成本 → 毛利 → 成本上限余量。所有估算都写进 assumptions。
    返回 {"facts": {...}, "assumptions": [...]}；缺输入时对应事实为 None。
    """
    facts: Dict[str, Any] = {}
    assumptions: List[str] = []
    if not sale_price or sale_price <= 0:
        return {"facts": facts, "assumptions": ["缺少售价，无法计算毛利"]}
    price = float(sale_price)
    referral = round(price * referral_pct, 2)
    fee = fba_fee_usd if fba_fee_usd is not None else 4.5
    if fba_fee_usd is None:
        assumptions.append("FBA 配送费按 $4.50 粗估（未拿到尺寸重量；大件/抛货会明显偏低），实际以亚马逊费用表为准")
    cost = None
    if purchase_cost_cny and purchase_cost_cny > 0:
        cost = round(purchase_cost_cny / cny_usd, 2)
        assumptions.append(f"1688 采购价 {purchase_cost_cny} CNY 按 1 USD={cny_usd} CNY 折算")
    freight = freight_usd
    if freight is None and cost is not None:
        freight = round(cost * freight_multiple, 2)
        assumptions.append(f"头程按采购价的 {int(freight_multiple * 100)}% 估算（未拿到实重/体积）")
    facts.update({"sale_price_usd": round(price, 2), "referral_fee_usd": referral,
                  "fba_fee_usd": fee, "purchase_cost_usd": cost, "cny_usd_rate": cny_usd})
    # 记下哪些成本是估的：估出来的毛利可以参与打分，但不能拿来一票否决（见 scoring._hard_blockers）
    estimated = []
    if fba_fee_usd is None:
        estimated.append("FBA 配送费")
    if freight_usd is None and cost is not None:
        estimated.append("头程运费")
    if estimated:
        facts["economics_estimated"] = estimated
    if fba_fee_usd is not None:  # 粗估的 FBA 不参与“双 20%”判定
        facts["fba_fee_ratio"] = round(fee / price, 3)
    if freight is not None:
        facts["freight_usd"] = round(freight, 2)
    if cost is not None and freight is not None:
        profit = round(price - referral - fee - cost - freight, 2)
        facts["net_profit_usd"] = profit
        facts["landed_cost_ratio"] = round((cost + freight) / price, 3)
        facts["gross_margin_pct"] = round(profit / price * 100, 1)
        margin_rate = target_margin_pct / 100.0
        ceiling = price - referral - fee - freight - price * margin_rate
        facts["cost_ceiling_usd"] = round(ceiling, 2)
        if cost > 0:
            facts["ceiling_headroom"] = round(ceiling / cost, 3)
    return {"facts": facts, "assumptions": assumptions}


# ---------------- 六道门同特征竞品筛查（关键词商品样本 → 六道门上游数字） ----------------
# 人工通常在卖家精灵上做商品/赛道筛选；这里把同一动作自动化：
# 按关键词拉一批头部商品（卖家精灵优先，Sorftime / 西柚回落），
# 从「月销量 + 评论数」两列把六道门上游要的数字算出来。

def _age_days_ms(ts: Any) -> Optional[float]:
    v = _f(ts)
    if v is None or v <= 0:
        return None
    import time as _time
    return round(((_time.time() - v / 1000.0) / 86400.0), 1)


def _age_days_str(s: Any) -> Optional[float]:
    """yyyy-MM-dd / yyyy-MM-dd HH:mm:ss → 上架至今天数。"""
    if not isinstance(s, str) or len(s) < 10:
        return None
    import datetime as _dt
    try:
        d = _dt.datetime.strptime(s[:10], "%Y-%m-%d")
    except ValueError:
        return None
    return round((_dt.datetime.now() - d).total_seconds() / 86400.0, 1)


def _rows_sellersprite(payload: Any) -> List[Dict[str, Any]]:
    items = deep_get(payload, "data.items", "items", default=None) if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return []
    rows = []
    for it in items:
        if not isinstance(it, dict):
            continue
        sales = _f(it.get("units"))
        if sales is None:
            sales = _f(it.get("amzUnit"))   # 没有父体月销时退到子体月销
        rows.append({
            "asin": clean_asin(it.get("asin")),
            "title": str(it.get("title") or "")[:120],
            "price": _f(it.get("price")),
            "rating": _f(it.get("rating")),
            "reviews": _f(it.get("ratings")),
            "monthly_orders": sales,
            "listed_age_days": _age_days_ms(it.get("availableDate")),
            "brand": str(it.get("brand") or "")[:60],
        })
    return rows


def _rows_sorftime(payload: Any) -> List[Dict[str, Any]]:
    items = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return []
    rows = []
    for it in items:
        if not isinstance(it, dict):
            continue
        sales = _f(it.get("monthly_sales_volume"))
        rows.append({
            "asin": clean_asin(it.get("asin")),
            "title": str(it.get("title") or "")[:120],
            "price": _f(it.get("price")),
            "rating": _f(it.get("ratings")),
            "reviews": _f(it.get("rating_count")),
            # sorftime 用 -1 表示「该期无数据」，转成缺值
            "monthly_orders": sales if (sales is not None and sales > 0) else None,
            "listed_age_days": _age_days_str(it.get("online_date")),
            "brand": str(it.get("brand") or "")[:60],
        })
    return rows


def _rows_xydc(payload: Any) -> List[Dict[str, Any]]:
    data = payload.get("data") if isinstance(payload, dict) else None
    items = data.get("list") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    rows = []
    for it in items:
        if not isinstance(it, dict):
            continue
        info = it.get("asinInfo") if isinstance(it.get("asinInfo"), dict) else {}
        sales = None
        for period in ((it.get("asinsTrends") or {}).get("periods") or []):
            if isinstance(period, dict) and str(period.get("period") or "") == "last30days":
                sales = _f(deep_get(period, "sales.value", default=None))
        rows.append({
            "asin": clean_asin(it.get("asin") or info.get("asin")),
            "title": str(info.get("title") or it.get("title") or "")[:120],
            "price": _f(info.get("price")),
            "rating": _f(info.get("stars")),
            "reviews": _f(info.get("ratings")),
            "monthly_orders": sales,
            "listed_age_days": None,
            "brand": str(info.get("brand") or "")[:60],
        })
    return rows


_SCREEN_ROW_PARSERS = {"sellersprite": _rows_sellersprite, "sorftime": _rows_sorftime, "xydc": _rows_xydc}


def screen_rows(payload: Any, connector: str) -> List[Dict[str, Any]]:
    """把某个数据源的商品列表返回统一成 {asin,title,price,rating,reviews,monthly_orders,listed_age_days,brand}。"""
    parse = _SCREEN_ROW_PARSERS.get(str(connector or ""))
    rows = parse(payload) if parse else []
    return [r for r in rows if r.get("asin") or r.get("monthly_orders") is not None]


def peer_metrics(rows: List[Dict[str, Any]], low_review_max: int = 30,
                 ref_reviews_min: int = 100, ref_reviews_max: int = 200,
                 young_days: int = 365) -> Dict[str, Any]:
    """从商品样本算六道门上游数字。样本是「关键词下的头部商品」，不是人工挑的同特征集——
    调用方必须把这个口径写进报告（peer_auto），置信度封顶「中」。"""
    out: Dict[str, Any] = {}
    rows = [r for r in rows if _f(r.get("monthly_orders")) is not None]
    if not rows:
        return out
    orders = sorted((_f(r["monthly_orders"]) for r in rows if _f(r["monthly_orders"]) > 0), reverse=True)
    out["peer_sample_n"] = len(rows)
    out["peer_count"] = len(orders)
    if orders:
        out["peer_avg_monthly"] = round(sum(orders) / len(orders))
        out["head_monthly_sales"] = orders[0]
        # 断崖法（方法论 §5.1）：从上往下找第一个 ≥1.3 倍的断点，断点以上是头部档（代理口径）
        top5 = orders[:5]
        out["top5_avg_monthly"] = round(sum(top5) / len(top5))
        for i in range(len(orders) - 1):
            if orders[i + 1] > 0 and orders[i] / orders[i + 1] >= 1.3:
                out["peer_cliff"] = {"ratio": round(orders[i] / orders[i + 1], 2),
                                     "head_tier": i + 1, "head_tier_min": orders[i],
                                     "after_tier": orders[i + 1]}
                break
    low = [_f(r["monthly_orders"]) for r in rows
           if _f(r.get("reviews")) is not None and _f(r.get("reviews")) <= low_review_max
           and _f(r["monthly_orders"]) is not None]
    low = [x for x in low if x > 0]
    if low:
        out["low_review_avg"] = round(sum(low) / len(low))
        out["low_review_sellers"] = len(low)
    ref = [_f(r["monthly_orders"]) for r in rows
           if _f(r.get("reviews")) is not None and ref_reviews_min <= _f(r.get("reviews")) <= ref_reviews_max
           and _f(r["monthly_orders"]) is not None]
    ref = [x for x in ref if x > 0]
    if ref:
        out["ref_reviews_median"] = round(median(ref))
        out["unsaturation_ref_n"] = len(ref)
    if out.get("low_review_avg") and out.get("top5_avg_monthly"):
        out["review_band_difficulty_pct"] = round(out["low_review_avg"] / out["top5_avg_monthly"] * 100, 1)
    if out.get("low_review_avg") and out.get("ref_reviews_median"):
        out["unsaturation_ratio"] = round(out["low_review_avg"] / out["ref_reviews_median"], 3)
    prices = [p for p in (_f(r.get("price")) for r in rows) if p and p > 0]
    if prices:
        out["avg_price_usd"] = _median(prices)
    reviews = [_f(r.get("reviews")) for r in rows]
    reviews = [r for r in reviews if r is not None]
    if reviews:
        out["median_reviews"] = _median(reviews)
    ages = [a for a in (r.get("listed_age_days") for r in rows) if a is not None]
    if ages:
        out["young_listing_share"] = round(sum(1 for a in ages if a <= young_days) / len(ages), 3)
    out["top_competitors_sample"] = [
        {"asin": r.get("asin"), "title": r.get("title"), "price": _f(r.get("price")),
         "rating": _f(r.get("rating")), "reviews": _f(r.get("reviews")),
         "monthly_orders": _f(r.get("monthly_orders")), "brand": r.get("brand")}
        for r in rows[:10]]
    return out


def peer_params(sources: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """同特征筛查 → 六道门参数（peer_count / S / 低评论档均值 / 参照档中位）。
    引擎只在用户没手填这些值时注入，并带上 peer_auto=True 让置信度封顶「中」。"""
    peer = sources.get("peer_screen") or {}
    if not peer.get("payload"):
        return None
    rows = screen_rows(peer["payload"], str(peer.get("connector") or ""))
    d = peer_metrics(rows,
                     low_review_max=int(sources.get("low_review_max") or 30),
                     ref_reviews_min=int(sources.get("ref_reviews_min") or 100),
                     ref_reviews_max=int(sources.get("ref_reviews_max") or 200))
    out = {k: d[k] for k in ("peer_count", "peer_avg_monthly", "low_review_avg", "ref_reviews_median")
           if d.get(k) is not None}
    if not out:
        return None
    out["peer_auto"] = True
    return out


# ---------------- 汇总 ----------------

def build(sources: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """
    sources: {group: {"connector": str, "payload": Any, "eid": str}}
    返回 {"facts": {...}, "evidence_refs": {fact_key: eid}, "assumptions": [...]}
    """
    facts: Dict[str, Any] = {}
    refs: Dict[str, str] = {}
    assumptions: List[str] = []

    def merge(data: Dict[str, Any], eid: str, keys: Iterable[str]) -> None:
        for k in keys:
            if data.get(k) is not None:
                facts[k] = data[k]
                refs[k] = eid

    for key in ("keyword", "keyword_zh"):
        if sources.get(key):
            facts[key] = sources[key]

    trend = sources.get("trend") or {}
    if trend.get("payload"):
        if trend.get("connector") == "xydc":
            d = parse_xydc_trend(trend["payload"])
        else:
            d = parse_sif_trend(trend["payload"])
        merge(d, trend["eid"], ("search_volume_weekly", "search_volume_trend_pct", "trend_basis",
                                "keyword_rank", "demand_structure", "longtail_share"))

    comp = sources.get("competition") or {}
    if comp.get("payload"):
        if comp.get("connector") == "xydc":
            d = parse_xydc_competition(comp["payload"])
        else:
            d = parse_sif_competition(comp["payload"], young_days=int(sources.get("young_days") or 365),
                                      low_review_max=int(sources.get("low_review_max") or 30),
                                      ref_reviews_min=int(sources.get("ref_reviews_min") or 100),
                                      ref_reviews_max=int(sources.get("ref_reviews_max") or 200))
        merge(d, comp["eid"], ("top3_click_share", "visible_asin_count", "median_reviews",
                               "young_listing_share", "avg_price_usd", "price_iqr_ratio",
                               "natural_share", "concentration_level", "competition_mode",
                               "top_competitors_sample", "review_band_difficulty_pct",
                               "low_review_sellers", "head_monthly_sales", "unsaturation_ratio",
                               "unsaturation_ref_n"))

    up = sources.get("competition_upload") or {}
    up_eid = up.get("eid") or ""
    if up.get("payload"):
        d = parse_sif_competition(up["payload"], young_days=int(sources.get("young_days") or 365),
                                  low_review_max=int(sources.get("low_review_max") or 30),
                                  ref_reviews_min=int(sources.get("ref_reviews_min") or 100),
                                  ref_reviews_max=int(sources.get("ref_reviews_max") or 200))
        # 自有数据只补缺，不覆盖实时数据
        merge({k: v for k, v in d.items() if facts.get(k) is None}, up["eid"],
              ("median_reviews", "young_listing_share", "avg_price_usd", "price_iqr_ratio",
               "top_competitors_sample", "review_band_difficulty_pct", "low_review_sellers",
               "head_monthly_sales", "unsaturation_ratio", "unsaturation_ref_n"))

    # 同特征竞品筛查（关键词商品样本，≤100 行）：比 SIF 竞争格局的 top10 大一个量级，
    # 六道门上游数字以它为准；但用户上传的自有竞品表是人工口径，不让自动样本盖掉。
    peer = sources.get("peer_screen") or {}
    if peer.get("payload"):
        rows = screen_rows(peer["payload"], str(peer.get("connector") or ""))
        d = peer_metrics(rows,
                         low_review_max=int(sources.get("low_review_max") or 30),
                         ref_reviews_min=int(sources.get("ref_reviews_min") or 100),
                         ref_reviews_max=int(sources.get("ref_reviews_max") or 200),
                         young_days=int(sources.get("young_days") or 365))
        user_keys = {k for k, v in refs.items() if v and v == up_eid}
        merge({k: v for k, v in d.items() if k not in user_keys}, peer.get("eid") or "",
              ("head_monthly_sales", "review_band_difficulty_pct", "low_review_sellers",
               "low_review_avg", "ref_reviews_median", "unsaturation_ratio", "unsaturation_ref_n",
               "peer_count", "peer_avg_monthly", "peer_sample_n", "peer_cliff",
               "median_reviews", "avg_price_usd", "young_listing_share", "top_competitors_sample"))
        if d.get("peer_sample_n"):
            facts["peer_screen_source"] = f"{peer.get('connector') or '?'}（{d['peer_sample_n']} 个样本）"
            refs["peer_screen_source"] = peer.get("eid") or ""


    info = sources.get("keyword_info") or {}
    if info.get("payload"):
        d = parse_xydc_keyword_info(info["payload"], facts.get("keyword", ""))
        merge(d, info["eid"], ("search_volume_weekly", "top3_click_share", "keyword_rank",
                               "click_conversion_rate"))

    demand = sources.get("demand") or {}
    if demand.get("payload"):
        d = parse_sif_demand(demand["payload"], facts.get("keyword", ""))
        merge(d, demand["eid"], ("weeks_to_peak", "peak_month", "seasonality", "season_position",
                                 "annual_growth_pct", "search_volume_trend_pct", "trend_basis",
                                 "demand_diagnosis", "demand_interpretation"))

    supply = sources.get("supply") or {}
    if _fetched(supply):
        d = parse_1688(supply.get("payload") or [], str(supply.get("query") or facts.get("keyword_zh")
                                                        or facts.get("keyword") or ""))
        if not d:
            # 查成功、一条报价都没回：1688 0 报价是事实，不是缺数据（回了但全对不上的，parse 会记 raw_count）
            d = {"supply_offers": 0}
        merge(d, supply["eid"], ("supply_offers", "supply_supplier_count", "supply_price_cny_min",
                                 "supply_price_cny_median", "supply_moq_min",
                                 "supply_moq_median", "supply_samples", "supply_raw_count"))

    ip = sources.get("ip") or {}
    if ip.get("payload") is not None:
        d = parse_patents(ip["payload"])
        merge(d, ip["eid"], ("ip_findings_count", "ip_samples"))

    # 图片识货：识别结果是「这个产品是什么」的说明，不参与阈值判定；
    # 以图搜同款、外观专利以图检索的结果才是事实，能补上关键词路线漏掉的东西。
    img = sources.get("image") or {}
    a = img.get("analysis") or {}
    if a:
        merge({"image_product_zh": a.get("product_name_zh"), "image_product_en": a.get("product_name_en"),
               "image_keyword": a.get("keyword"), "image_keyword_zh": a.get("keyword_zh"),
               "image_confidence": a.get("confidence"), "image_category": a.get("category"),
               "image_features": a.get("features"), "image_risk_flags": a.get("risk_flags"),
               "image_url": img.get("image_url") or img.get("public_url")},
              img.get("eid") or "", ("image_product_zh", "image_product_en", "image_keyword",
                                     "image_keyword_zh", "image_confidence", "image_category",
                                     "image_features", "image_risk_flags", "image_url"))

    sup_img = sources.get("supply_image") or {}
    if _fetched(sup_img):
        # 以图搜回来的本身就是同款候选，title 跟图片并不共享字面，别再过文字相关性
        d = parse_1688(sup_img.get("payload") or [], "") or {"supply_offers": 0}
        merge({f"image_supply_{k[len('supply_'):]}": v for k, v in d.items() if k.startswith("supply_")},
              sup_img["eid"], ("image_supply_offers", "image_supply_price_cny_min", "image_supply_samples"))

    ip_img = sources.get("ip_image") or {}
    if ip_img.get("payload") is not None:
        d = parse_patents(ip_img["payload"])
        hits = d.get("ip_findings_count")
        merge({"image_ip_findings_count": hits, "image_ip_samples": d.get("ip_samples")},
              ip_img["eid"], ("image_ip_findings_count", "image_ip_samples"))
        # 关键词和以图两路查专利，取大者：任一路查到就算有风险，不能被另一路的 0 抹掉
        # 关键词那一路没跑通时，以图这一路的结果（含 0 命中）就是唯一的专利检索结论
        kw_hits = facts.get("ip_findings_count")
        if isinstance(hits, (int, float)) and (kw_hits is None or kw_hits < hits):
            facts["ip_findings_count"] = hits
            refs["ip_findings_count"] = ip_img["eid"]

    voc = sources.get("voc") or {}
    if _fetched(voc):
        d = parse_voc(voc.get("payload"), str(voc.get("query") or "")) if voc.get("payload") else {}
        if d.get("voc_dropped"):
            assumptions.append(f"用户声音：Reddit 搜到 {d['voc_total']} 条，其中 {d['voc_dropped']} 条与「{voc.get('query')}」无关，已剔除")
        if d.get("voc_samples"):
            merge(d, voc["eid"], ("has_voc", "voc_samples"))
            facts["voc_count"] = len(d["voc_samples"])
            refs["voc_count"] = voc["eid"]
        else:
            # 取到了、但一条讨论都没有：这是事实（0 条），不是缺数据。
            # 记下来，报告里要写明「没搜到」，别让它跟「没查」混为一谈。
            merge({"has_voc": False, "voc_count": 0}, voc["eid"], ("has_voc", "voc_count"))

    eco = sources.get("economics") or {}
    if eco:
        merge(eco.get("facts") or {}, eco.get("eid", ""), FACT_KEYS)
        assumptions.extend(eco.get("assumptions") or [])

    ov = sources.get("override") or {}
    if ov.get("facts"):
        merge(ov["facts"], ov.get("eid", ""), tuple(ov["facts"].keys()))
        assumptions.append("以下数值由用户手动提供：" + "、".join(ov["facts"].keys()))

    return {"facts": facts, "evidence_refs": refs, "assumptions": assumptions}


# 允许用户手填覆盖的事实（数值型）
OVERRIDABLE = ("search_volume_weekly", "search_volume_trend_pct", "top3_click_share", "visible_asin_count",
               "median_reviews", "young_listing_share", "avg_price_usd", "natural_share", "supply_offers",
               "supply_moq_min", "ip_findings_count", "review_band_difficulty_pct", "weeks_to_peak",
               "head_monthly_sales", "unsaturation_ratio", "unsaturation_ref_n")


def clean_overrides(raw: Any) -> Dict[str, float]:
    out: Dict[str, float] = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            if k in OVERRIDABLE and _f(v) is not None:
                out[k] = _ratio(v) if k in ("top3_click_share", "young_listing_share", "natural_share") else _f(v)
    return out


def missing_groups(sources: Dict[str, Dict[str, Any]]) -> List[Dict[str, str]]:
    """哪些数据组没拿到，用于报告里的“缺口清单”。"""
    out = []
    groups = (("trend", "需求趋势"), ("competition", "竞争格局"),
              ("demand", "需求生命周期"), ("supply", "1688 货源"),
              ("ip", "专利初筛"), ("economics", "成本与毛利"))
    for key, label in groups:
        src = sources.get(key) or {}
        got = bool(src.get("facts")) if key == "economics" else bool(src.get("payload"))
        if not got:
            out.append({"group": key, "label": label, "error": src.get("error") or "未获取"})
    return out
