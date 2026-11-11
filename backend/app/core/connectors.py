"""
数据连接器注册表（能力路由层）。

设计原则
- 业务代码只认“能力”（如 market.keyword_trend），不认某个具体 MCP 服务；
  换/加数据源只改本文件的连接器定义，运行引擎不动。
- 每个能力有主源与备源，按顺序尝试；全部失败就如实返回失败，
  绝不编造数字（降级只能降为“缺数据”，不能降为“估算值”）。
- 状态与额度实时探测并缓存，界面显示真实连通性，而不是“永远在线”。
"""
import asyncio
import datetime
import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.core.logger import logger
from app.core.mcp_client import mcp_client_pool, dig
from app.core.redact import redact
from app.core.settings_store import settings_store


# ---------------- 能力词表 ----------------

CAPABILITY_LABELS: Dict[str, str] = {
    "market.keyword_trend": "关键词需求趋势（ABA 搜索量/排名/长尾结构）",
    "market.keyword_competition": "关键词竞争格局（集中度/头部竞品/进入难度）",
    "market.keyword_info": "关键词基础指标（搜索量/CPC/转化率/难度）",
    "market.keyword_demand": "关键词需求生命周期（增长/萎缩/季节性）",
    "market.keyword_history": "关键词历史搜索量明细",
    "market.keyword_opportunities": "词根下机会词筛选",
    "market.root_competitors": "词根竞品榜单（买家点击视角）",
    "market.asin_profile": "ASIN 基础档案（价格/评分/BSR/上架时间）",
    "market.asin_traffic": "Listing 流量结构（自然 vs 广告）",
    "market.asin_keywords": "ASIN 反查流量词与排名",
    "market.category_report": "类目大盘报告（销量/价格带/集中度/退货）",
    "market.category_products": "类目 Top100 商品榜单",
    "market.product_search": "商品条件筛选",
    "market.product_detail": "商品详情",
    "market.breakout_products": "潜力爆款发现",
    "market.cost_ceiling": "采购成本上限反推（盈亏平衡）",
    "ops.asin_sales": "ASIN 销量趋势",
    "supply.1688_search": "1688 货源搜索（报价/MOQ/产业带）",
    "supply.1688_image_search": "1688 以图搜同款（只收公网图片 URL）",
    "supply.1688_offer": "1688 单品详情（SKU/阶梯价/主图）",
    "supply.1688_media": "1688 图片与视频下载",
    "ip.patent_keyword": "专利关键词检索",
    "ip.patent_image": "外观专利以图检索",
    "social.reddit": "Reddit 真实用户讨论",
    "social.quora": "Quora 真实用户提问",
}

# 能力 → 连接器优先级（主源在前，备源在后）
# 关键词数据：西柚（免费公开版）优先、SIF（付费点数）回落——省 SIF 额度。
# 例外：竞争格局保持 SIF 优先，六道门要的月销/评论档字段只有 SIF top_competitors 带全，西柚格局没有销量。
# 市场/商品筛选（六道门的同特征竞品筛查）：有卖家精灵用卖家精灵，没有回落 Sorftime，再西柚。
CAPABILITY_ROUTES: Dict[str, List[str]] = {
    "market.keyword_trend": ["xydc", "sif"],
    "market.keyword_competition": ["sif", "xydc"],
    "market.keyword_info": ["xydc", "sif", "sellersprite"],
    "market.keyword_demand": ["sif"],
    "market.keyword_history": ["xydc", "sif"],
    "market.keyword_opportunities": ["sif"],
    "market.root_competitors": ["sif", "xydc"],
    "market.asin_profile": ["sif", "xydc", "sorftime"],
    "market.asin_traffic": ["sif", "xydc"],
    "market.asin_keywords": ["sif", "xydc"],
    "market.category_report": ["sorftime"],
    "market.category_products": ["sorftime"],
    "market.product_search": ["sellersprite", "sorftime", "xydc"],
    "market.product_detail": ["sorftime", "xydc"],
    "market.breakout_products": ["xydc"],
    "market.cost_ceiling": ["sif"],
    "ops.asin_sales": ["sif"],
    "supply.1688_search": ["sorftime", "1688_scraper"],
    "supply.1688_image_search": ["sorftime"],
    "supply.1688_offer": ["1688_scraper", "sorftime"],
    "supply.1688_media": ["1688_scraper"],
    "ip.patent_keyword": ["sorftime"],
    "ip.patent_image": ["sorftime"],
    "social.reddit": ["sorftime"],
    "social.quora": ["sorftime"],
}


@dataclass
class ConnectorDef:
    id: str
    name: str
    description: str
    hints: List[str]
    tools: Dict[str, str]                      # capability -> MCP tool name
    quota_tool: Optional[str] = None
    quota_parser: Optional[str] = None         # sif / sorftime / xydc
    paid: bool = False                         # 单次调用有明显计费
    notes: str = ""
    # 能力 → 参数改名表：业务层只传统一参数，各数据源字段名不同在这里适配
    arg_adapters: Dict[str, Dict[str, str]] = field(default_factory=dict)
    # 运行时填充
    server_id: Optional[str] = None
    transport: str = ""
    health: Dict[str, Any] = field(default_factory=dict)


_CONNECTORS: List[ConnectorDef] = [
    ConnectorDef(
        id="sif", name="SIF 关键词与广告反查",
        description="自然/广告流量拆解、ABA 足迹、词根生命周期、竞争格局、盈亏平衡反推",
        hints=["sif"],
        tools={
            "market.keyword_trend": "market_get_keyword_root_trend",
            "market.keyword_competition": "market_get_keyword_competition",
            "market.keyword_demand": "market_get_keyword_demand",
            "market.keyword_history": "market_get_keyword_history",
            "market.keyword_opportunities": "market_screen_keyword_opportunities",
            "market.root_competitors": "market_get_keyword_root_competitors",
            "market.asin_profile": "market_get_asin_profile",
            "market.asin_traffic": "ops_get_listing_traffic_overview",
            "market.asin_keywords": "market_get_asin_keyword_signals",
            "market.cost_ceiling": "market_estimate_profit_threshold",
            "ops.asin_sales": "ops_get_asin_sales_trend",
        },
        quota_tool="query_quota", quota_parser="sif",
    ),
    ConnectorDef(
        id="sorftime", name="Sorftime 市场大盘 · 社媒 · 1688",
        description="类目全景与商品库、Reddit/Quora 社媒洞察、专利检索、1688 云端货源",
        hints=["sorftime"],
        tools={
            "market.category_report": "category_report",
            "market.category_products": "category_products",
            "market.product_search": "product_search",
            "market.product_detail": "product_detail",
            "market.asin_profile": "product_detail",
            "market.keyword_info": "keyword_detail",
            "market.asin_keywords": "product_traffic_terms",
            "supply.1688_search": "ali1688_similar_product",
            "supply.1688_image_search": "ali1688_product_search_from_image",
            "supply.1688_offer": "ali1688_product_request",
            "ip.patent_keyword": "patent_search_from_keyword",
            "ip.patent_image": "patent_search_from_image",
            "social.reddit": "reddit_post_search",
            "social.quora": "quora_question_search",
        },
        arg_adapters={"supply.1688_search": {"keyword": "search_name"},
                      "social.reddit": {"keyword": "search_name"},
                      "social.quora": {"keyword": "search_name"},
                      # sorftime product_search 只认 amz_site（US/GB/DE…），一页固定 20 条
                      "market.product_search": {"keyword": "keyword", "country": "amz_site"}},
        quota_tool="common_usage_query", quota_parser="sorftime",
    ),
    ConnectorDef(
        id="xydc", name="西柚洞察（多 ASIN 对比）",
        description="关键词机会筛选、多 ASIN 关键词矩阵、潜力爆款发现、类目洞察",
        hints=["xydc", "西柚"],
        tools={
            "market.keyword_info": "get_keyword_info",
            "market.keyword_trend": "get_keyword_aba_trends",
            "market.keyword_history": "get_keyword_aba_trends",
            "market.asin_keywords": "get_asin_keywords",
            "market.keyword_competition": "get_keyword_asin_analysis",
            "market.root_competitors": "get_keyword_asin_analysis",
            "market.product_search": "search_products",
            "market.product_detail": "get_asin_info",
            "market.asin_profile": "get_asin_info",
            "market.asin_traffic": "get_asin_traffic",
            "market.breakout_products": "discover_breakout_products",
        },
        quota_tool="get_account_credit_quota", quota_parser="xydc",
    ),
    ConnectorDef(
        id="1688_scraper", name="1688 本地抓取（stdio）",
        description="本机封装的 1688 MCP：真实 offer 搜索、单品详情、图片视频下载",
        hints=["1688"],
        tools={
            "supply.1688_search": "search_offers",
            "supply.1688_offer": "get_offer",
            "supply.1688_media": "download_media",
        },
        notes="首次使用需在弹出窗口登录 1688",
    ),
    ConnectorDef(
        id="sellersprite", name="卖家精灵",
        description="月搜索购买比、SPR 推广难度、竞品出单词反查（需自行开通）",
        hints=["sellersprite", "卖家精灵"],
        tools={
            "market.keyword_info": "keyword_research",
            # 反查 ASIN 流量词用 traffic_keyword；competitor_lookup 是「商品列表筛选」，不是这个能力
            "market.asin_keywords": "traffic_keyword",
            # 六道门的商品/市场筛选优先走卖家精灵：competitor_lookup 按关键词拉商品列表，
            # 可按月销/评论/价格/上架时间排序过滤，正是「同特征竞品筛查」的主路
            "market.product_search": "competitor_lookup",
        },
        paid=True,
    ),
]

_BY_ID: Dict[str, ConnectorDef] = {c.id: c for c in _CONNECTORS}
_HEALTH_TTL = 180.0


def connector_label(connector_id: Optional[str]) -> str:
    """
    报告里要写数据来自谁，得给人看的名字，不能把内部 id 原样丢进去。

    "xydc" 出现在「FBA 配送费取xydc 提供的…」这种句子里，用户根本不知道 xydc 是什么；
    注册表里本来就有中文名，取括号前的部分就够（"西柚洞察（多 ASIN 对比）" → "西柚洞察"）。
    没注册的（比如本地自建）就退回 id 本身，不编造名字。
    """
    cid = (connector_id or "").strip()
    if not cid:
        return "未知数据源"
    c = _BY_ID.get(cid)
    if not c:
        return cid
    return (c.name or cid).split("（")[0].strip() or cid


class CapabilityResult:
    def __init__(self, capability: str, ok: bool, data: Any = None, connector: Optional[str] = None,
                 tool: Optional[str] = None, error: Optional[str] = None, elapsed_ms: int = 0,
                 attempts: Optional[List[Dict[str, Any]]] = None, args: Optional[Dict[str, Any]] = None):
        self.capability = capability
        self.ok = ok
        self.data = data
        self.connector = connector
        self.tool = tool
        self.error = error
        self.elapsed_ms = elapsed_ms
        self.attempts = attempts or []
        self.args = args or {}

    def to_dict(self) -> Dict[str, Any]:
        return {"capability": self.capability, "ok": self.ok, "connector": self.connector,
                "tool": self.tool, "args": self.args, "error": self.error,
                "elapsed_ms": self.elapsed_ms, "attempts": self.attempts}

    def __repr__(self) -> str:
        return f"<CapabilityResult {self.capability} ok={self.ok} via={self.connector}>"


_PAYLOAD_KEYS = {"data", "list", "items", "result", "results", "profiles", "rows", "records"}
_ERR_CODE_HINTS = ("QUOTA", "EXCEED", "UNAUTHOR", "FORBIDDEN", "INVALID", "ERROR", "LIMIT", "DENIED", "EXPIRED")


def _error_body(data: Any) -> Optional[str]:
    """识别「正文里写着出错、却没标 isError」的返回，返回中文可读原因；正常数据返回 None。

    只认小而没有数据载荷的对象（真实成功返回都带 list/profiles/data 等载荷），宁可漏判也不误杀。
    """
    obj = data
    if isinstance(data, str):
        s = data.strip()
        if not s.startswith("{") or len(s) > 4000:
            return None
        try:
            obj = json.loads(s)
        except ValueError:
            return None
    if not isinstance(obj, dict) or len(obj) > 6 or _PAYLOAD_KEYS & set(obj):
        return None
    msg = obj.get("error") or obj.get("errorMessage") or obj.get("error_message") or obj.get("err_msg")
    if isinstance(msg, dict):
        msg = msg.get("message") or msg.get("msg") or json.dumps(msg, ensure_ascii=False)
    note = obj.get("message") or obj.get("msg") or ""
    code = obj.get("code") if obj.get("code") is not None else obj.get("status")
    if msg:
        return redact(str(msg))[:200]
    if obj.get("success") is False or obj.get("ok") is False:
        return redact(str(note or code or "success=false"))[:200]
    if isinstance(code, str) and any(h in code.upper() for h in _ERR_CODE_HINTS):
        return redact(f"{code} {note}".strip())[:200]
    if isinstance(code, int) and code >= 400 and note:
        return redact(f"{code} {note}")[:200]
    return None


# 这些能力「0 条」等于「没查到」，不能算取数成功：
# 关键词没收录、格局里没有可见 ASIN，都是数据源给不出判断依据，而不是「市场真的为零」。
# 专利命中 0 条、1688 没找到同款、Reddit 没人讨论则相反——0 本身是有意义的事实，不能判失败。
_EMPTY_IS_FAILURE = {
    "market.keyword_trend", "market.keyword_competition", "market.keyword_info",
    "market.keyword_demand", "market.keyword_history", "market.root_competitors",
    "market.asin_keywords", "market.asin_profile", "market.product_search",
    "market.category_report", "market.category_products",
}

# 反过来：这些能力「0 条」本身就是结论，不能判失败，也不能因此去掉备源（本来就没有第二家）。
# Reddit 回一句 “No data available” 是「没人讨论」，不是接口坏了——判失败会让报告缺一块，
# 也会让「0 讨论」这个可用于判差异化的信号凭空消失。
_EMPTY_IS_FACT = {
    "supply.1688_search", "supply.1688_image_search", "supply.1688_offer",
    "ip.patent_keyword", "ip.patent_image",
    "social.reddit", "social.quora",
}
_EMPTY_HINTS = ("未找到", "无数据", "暂无数据", "没有数据", "查无", "no data", "no result", "not found")


def _has_data_rows(obj: Dict[str, Any]) -> bool:
    """返回里是否至少有一个非空的数据列表（跳过 _next_step 这类导航字段）。"""
    for k, v in obj.items():
        if str(k).startswith("_") or k == "pagination":
            continue
        if isinstance(v, list) and any(isinstance(x, (dict, str)) for x in v):
            return True
        if isinstance(v, dict) and _has_data_rows(v):
            return True
    return False


def _empty_body(data: Any) -> Optional[str]:
    """识别「调用成功、但一条可用数据都没有」的返回，返回中文原因；有数据返回 None。

    SIF / 西柚这类数据源习惯用 200 + 空列表 + 0 计数表示「这个词没收录」。原样当成功，会出现
    「证据 6/7 成功、关键数据全是 —、却给出有条件做」的空壳结论，还会挡住备源回落
    （SIF 查不到时本可以问西柚）。所以这里判空，交给 call() 换下一家。
    """
    obj = data
    if isinstance(obj, str):
        s = obj.strip()
        if not s:
            return "返回空结果"
        if len(s) > 4000:
            return None  # 大段正文至少带内容，不在这儿猜
        if not s.startswith(("{", "[")):
            low = s.lower()
            if any(h in low for h in _EMPTY_HINTS):
                return f"数据源无结果：{s[:120]}"
            return None
        try:
            obj = json.loads(s)
        except ValueError:
            return None
    if isinstance(obj, list):
        return "数据源返回空列表" if not obj else None
    if not isinstance(obj, dict):
        return None

    # 1) 显式 0 计数（SIF 趋势的 data_points、竞争格局的 total_competitors）
    for key in ("data_points", "total_competitors"):
        val = obj.get(key)
        if isinstance(val, int) and not isinstance(val, bool) and val == 0:
            return f"数据源返回 0 条（{key}=0）"

    # 1b) SIF 趋势：有 103 个日期点、但每周搜索量全是 null = 该词未收录（"管线通、取值空"）。
    #     不判空的话，这条会算「成功」，挡住西柚备源，事实表却一片空白。
    vols = obj.get("keyword_search_volumes")
    if isinstance(vols, list) and not any(isinstance(v, (int, float)) and not isinstance(v, bool) for v in vols):
        return "数据源无结果：趋势每一周搜索量都是空值（该词未收录）"

    # 2) 分页口径说全集为空
    page = obj.get("pagination")
    if isinstance(page, dict):
        total = page.get("totalAvailable")
        returned = page.get("returned")
        if isinstance(total, int) and total == 0 and (returned is None or returned == 0):
            return "数据源返回 0 条（pagination.totalAvailable=0）"

    # 3) 正文直接写「没找到」，且返回里没有任何数据列表
    for key in ("message", "msg"):
        val = obj.get(key)
        if isinstance(val, str) and any(h in val.lower() for h in _EMPTY_HINTS) and not _has_data_rows(obj):
            return f"数据源无结果：{val[:120]}"

    # 4) 需求诊断自己写明「无数据 / 状态待判断」
    profiles = obj.get("profiles")
    if isinstance(profiles, list) and profiles:
        diags = [str((p or {}).get("diagnosis") or "") for p in profiles if isinstance(p, dict)]
        if diags and all(("无数据" in d or "待判断" in d) for d in diags):
            return "数据源无结果：需求诊断返回「无数据」"

    # 5) 西柚把结果包在 data 里：data.list 空数组、data.entities 里的 trends 全空，
    #    都是「这个词没收录」，不是「市场为零」。外层有 data/status 两个键，骗得过上面的通用判空。
    body = obj.get("data")
    if isinstance(body, dict):
        lst = body.get("list")
        if isinstance(lst, list) and not lst:
            return "数据源无结果：该词没有返回任何记录"
        ents = body.get("entities")
        if isinstance(ents, list):
            if not ents:
                return "数据源无结果：该词没有返回任何记录"
            # 「有 52 行」不等于「有数据」：未收录的词会返回满 52 周的空壳，
            # weeklySearchVolume / searchFrequencyRank 全是 null。按行数判空会把它当成功，
            # 事实表照样空白，报告里却显示这条证据取到了——比缺数据更坏。
            # 同一层 envelopes 也用在 ASIN 档案上（每行是商品字段，没有 trends），
            # 所以只在确实是趋势形态时才按空值判，否则有行就算有数据。
            if any(isinstance(e, dict) and isinstance(e.get("trends"), list) for e in ents):
                rows = [t for e in ents if isinstance(e, dict) for t in e["trends"] if isinstance(t, dict)]
                if not rows or not any(
                        t.get("weeklySearchVolume") is not None or t.get("searchFrequencyRank") is not None
                        for t in rows):
                    return "数据源无结果：趋势每一周都是空值（该词未收录）"
    return None


def _check_empty(capability: str, data: Any) -> Optional[str]:
    """这一次返回该不该算「没取到数据」；算就返回中文原因。

    两条路：结构化载荷只有白名单里的能力判空（0 条=没查到）；纯文本载荷查一遍，
    因为「未找到相关类目」这种一句话根本解析不出数字，记成成功只会挡住备源。
    供货、专利、社媒不走这条路：它们的 0 条是事实（见 _EMPTY_IS_FACT）。
    """
    if capability in _EMPTY_IS_FACT:
        return None
    if capability in _EMPTY_IS_FAILURE or isinstance(data, str):
        return _empty_body(data)
    return None


def _xydc_keywords(args: Dict[str, Any]) -> List[str]:
    kw = args.get("keywords") or args.get("keyword") or ""
    return kw if isinstance(kw, list) else [str(kw)]


def _xydc_common(args: Dict[str, Any]) -> Dict[str, Any]:
    kws = [k for k in _xydc_keywords(args) if k]
    return {
        "keywords": kws,
        "country": args.get("country") or "US",
        # 西柚要求填任务原话与本次取数目的（服务端会记录），自动运行时给一句实话即可
        "user_task": f"选品裁定：评估「{kws[0] if kws else ''}」值不值得做",
        "intent_summary": "取关键词的搜索量与排名趋势，供选品规则判定使用",
    }


def _xydc_trend(args: Dict[str, Any]) -> Dict[str, Any]:
    """西柚 ABA 趋势要的是周区间，不是 granularity；按最近 52 周取。"""
    end = datetime.date.today()
    start = end - datetime.timedelta(days=364)
    out = _xydc_common(args)
    out.update({"start_week": start.isoformat(), "end_week": end.isoformat()})
    return out


def _xydc_asin(args: Dict[str, Any]) -> Dict[str, Any]:
    """西柚按 ASIN 取数的工具都强制要 country（漏了直接 InvalidArguments），业务层只传 asins。"""
    asins = args.get("asins") or ([args["asin"]] if args.get("asin") else [])
    asins = [str(a).strip().upper() for a in asins if a]
    return {
        "asins": asins,
        "country": args.get("country") or "US",
        "user_task": f"选品裁定：查看竞品 {asins[0] if asins else ''} 的商品档案",
        "intent_summary": "取竞品的价格、评分、BSR、包装尺寸，供成本与竞争判定使用",
    }


def _ss_keyword(args: Dict[str, Any]) -> Dict[str, Any]:
    """卖家精灵 keyword_research：参数全部包在 request 里，站点字段叫 marketplace。"""
    kw = args.get("keywords") or args.get("keyword") or ""
    kw = " ".join(str(k) for k in kw) if isinstance(kw, list) else str(kw)
    return {"request": {"marketplace": args.get("country") or args.get("marketplace") or "US",
                        "keywords": kw.strip(), "page": 1, "size": int(args.get("size") or 20)}}


def _ss_asin(args: Dict[str, Any]) -> Dict[str, Any]:
    """卖家精灵 traffic_keyword（ASIN 反查流量词）：一次只查一个 ASIN。"""
    asins = args.get("asins") or ([args["asin"]] if args.get("asin") else [])
    asin = str(asins[0]).strip().upper() if asins else ""
    return {"request": {"marketplace": args.get("country") or args.get("marketplace") or "US",
                        "asin": asin, "page": 1, "size": int(args.get("size") or 50)}}


def _ss_products(args: Dict[str, Any]) -> Dict[str, Any]:
    """卖家精灵 competitor_lookup：按关键词拉商品列表，月销降序。
    matchType 1 = 词组匹配（模糊匹配会把「mat」拉出烘焙苏打这类同词异品）；variation Y 按官方说明默认。"""
    kw = args.get("keyword") or ""
    size = max(10, min(int(args.get("size") or 100), 100))
    return {"request": {"marketplace": args.get("country") or args.get("marketplace") or "US",
                        "keyword": str(kw).strip(), "matchType": 1,
                        "order": {"field": "total_units", "desc": True},
                        "page": 1, "size": size, "variation": "Y"}}


def _xydc_products(args: Dict[str, Any]) -> Dict[str, Any]:
    """西柚 search_products：和其它西柚工具一样强制 country + user_task/intent_summary。"""
    kw = str(args.get("keyword") or "").strip()
    return {"keyword": kw, "country": args.get("country") or "US",
            "user_task": f"选品裁定：评估「{kw}」值不值得做",
            "intent_summary": "取该词头部商品的价格、评论数与月销量，供六道门判定使用"}


# (数据源, 能力) → 参数构造函数。字段名和形状都不一样时在这里一次性对齐，业务层不用管。
_ARG_BUILDERS: Dict[Any, Any] = {
    ("xydc", "market.keyword_trend"): _xydc_trend,
    ("xydc", "market.keyword_history"): _xydc_trend,
    ("xydc", "market.keyword_info"): _xydc_common,
    ("xydc", "market.asin_profile"): _xydc_asin,
    ("xydc", "market.asin_traffic"): _xydc_asin,
    ("xydc", "market.asin_keywords"): _xydc_asin,
    ("xydc", "market.product_detail"): _xydc_asin,
    ("xydc", "market.product_search"): _xydc_products,
    ("sellersprite", "market.keyword_info"): _ss_keyword,
    ("sellersprite", "market.asin_keywords"): _ss_asin,
    ("sellersprite", "market.product_search"): _ss_products,
}


def _adapt_args(conn: "ConnectorDef", capability: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """把业务层的统一参数名换成该数据源工具的字段名。"""
    build = _ARG_BUILDERS.get((conn.id, capability))
    if build:
        return build(args or {})
    mapping = (conn.arg_adapters or {}).get(capability)
    if not mapping:
        return dict(args)
    out: Dict[str, Any] = {}
    for k, v in (args or {}).items():
        out[mapping.get(k, k)] = v
    return out


def user_routes() -> Dict[str, List[str]]:
    """用户在设置里指定的能力路由（connectors.routes：{能力: [连接器id,...]}）。
    只认识已注册的连接器 id，别的写法忽略，不会因为手滑把路由改坏。"""
    raw = settings_store.get("connectors", "routes", default={}) or {}
    out: Dict[str, List[str]] = {}
    if isinstance(raw, dict):
        for cap, seq in raw.items():
            if not isinstance(cap, str) or not isinstance(seq, list):
                continue
            ids = [str(x) for x in seq if isinstance(x, (str, int)) and str(x) in _BY_ID]
            if ids:
                out[cap] = ids
    return out


def resolve_routes(capability: str, prefer: Optional[List[str]] = None) -> List[str]:
    """实际生效的连接器顺序：用户设置 > 方法包/调用方声明 > 内置默认。
    用户在设置里手动指定的顺序永远最先——包作者和内置默认都不能替用户做决定。"""
    user = user_routes().get(capability)
    if user:
        return list(user)
    if prefer:
        ids = [str(x) for x in prefer if str(x) in _BY_ID]
        if ids:
            return ids
    return list(CAPABILITY_ROUTES.get(capability) or [])


def missing_source_error(capability: str, routes: List[str]) -> Optional[str]:
    """一条能力完全无源可用时，把「该接谁」说清楚，而不是留一句「未配置该数据源」。"""
    if not routes:
        return f"能力 {capability} 未定义任何数据源路由"
    names = "、".join(filter(None, (connector_label(cid) for cid in routes)))
    return (f"「{CAPABILITY_LABELS.get(capability) or capability}」没有可用的数据源："
            f"需要接入或启用 {names}（数据源页可配置）")


class ConnectorRegistry:
    def __init__(self):
        self._locks: Dict[str, asyncio.Lock] = {}
        self._tools_cache: Dict[str, Dict[str, Any]] = {}
        self._quota_cache: Dict[str, Dict[str, Any]] = {}

    # ---------- 绑定与状态 ----------

    def _bound(self) -> List[ConnectorDef]:
        """把连接器定义与当前发现的 MCP 服务器绑定（按 id 特征匹配）。"""
        registry = mcp_client_pool.registry
        for conn in _CONNECTORS:
            conn.server_id = None
            conn.transport = ""
            for sid, spec in registry.items():
                low = sid.lower()
                if any(h in low for h in conn.hints):
                    conn.server_id = sid
                    conn.transport = spec.get("transport", "")
                    break
            if conn.server_id is None:
                # 再按配置里显式声明的 connector 字段匹配
                for sid, spec in registry.items():
                    if (spec.get("connector") or "").lower() == conn.id:
                        conn.server_id = sid
                        conn.transport = spec.get("transport", "")
                        break
        return _CONNECTORS

    def _enabled(self, connector_id: str) -> bool:
        overrides = settings_store.get("connectors", "overrides", default={}) or {}
        return bool((overrides.get(connector_id) or {}).get("enabled", True))

    def all(self) -> List[ConnectorDef]:
        return self._bound()

    def get(self, connector_id: str) -> Optional[ConnectorDef]:
        self._bound()
        return _BY_ID.get(connector_id)

    def capability_map(self) -> Dict[str, List[str]]:
        """实际生效的路由（已并入用户设置覆盖），界面按这个展示「这个能力先问谁、再问谁」。"""
        return {cap: resolve_routes(cap) for cap in CAPABILITY_ROUTES}

    def available_capabilities(self) -> Dict[str, List[str]]:
        """当前真正可用的能力 → 连接器列表（已绑定且启用）。"""
        out: Dict[str, List[str]] = {}
        for cap, routes in CAPABILITY_ROUTES.items():
            bound = [cid for cid in routes if (_BY_ID.get(cid) and _BY_ID[cid].server_id and self._enabled(cid))]
            if bound:
                out[cap] = bound
        return out

    # ---------- 调用 ----------

    async def _call_tool(self, server_id: str, tool: str, args: Dict[str, Any],
                         timeout: Optional[float] = None) -> Any:
        client = mcp_client_pool.get_client(server_id)
        if not getattr(client, "_initialized", False):
            lock = self._locks.setdefault(server_id, asyncio.Lock())
            async with lock:
                await client.ensure_initialized()
        return await client.call_tool(tool, args, timeout=timeout)

    async def call(self, capability: str, args: Dict[str, Any], *,
                   prefer: Optional[List[str]] = None,
                   timeout: Optional[float] = None) -> CapabilityResult:
        """
        按能力调用数据源。prefer 可指定连接器顺序（如只用本地 1688）。
        失败会记录每次尝试，返回 ok=False 与原因，绝不伪造数据。
        """
        started = time.perf_counter()
        self._bound()
        # 顺序：用户设置 > 调用方声明（方法包/引擎）> 内置默认
        routes = resolve_routes(capability, prefer)
        attempts: List[Dict[str, Any]] = []
        if not routes:
            return CapabilityResult(capability, False, error=f"未定义能力: {capability}",
                                    elapsed_ms=0, attempts=attempts)
        for cid in routes:
            conn = _BY_ID.get(cid)
            if not conn or not conn.server_id:
                attempts.append({"connector": cid, "ok": False, "error": "未配置该数据源"})
                continue
            if not self._enabled(cid):
                attempts.append({"connector": cid, "ok": False, "error": "已在设置中停用"})
                continue
            tool = conn.tools.get(capability)
            if not tool:
                attempts.append({"connector": cid, "ok": False, "error": f"不支持能力 {capability}"})
                continue
            call_args = _adapt_args(conn, capability, args)
            try:
                t0 = time.perf_counter()
                data = await self._call_tool(conn.server_id, tool, call_args, timeout=timeout)
                if data is None or (isinstance(data, str) and not data.strip()):
                    raise RuntimeError("返回空结果")
                body_err = _error_body(data)
                if body_err:
                    # 服务端把「额度用完 / 参数错」写在正文里却没标 isError，不能记成取数成功
                    raise RuntimeError(f"数据源返回错误：{body_err}")
                # 结构化载荷：只有「0 条=没查到」的能力才判空；专利 0 命中、1688 0 报价是事实。
                # 纯文本载荷例外：正文写着「未找到相关类目」的，解析不出任何数字，算没取到，
                # 才会继续问下一家数据源——1688 就这么回，原来记成成功，报告里写成「没连上」。
                empty = _check_empty(capability, data)
                if empty:
                    # 「查不到」不是「市场为零」。判成失败，才会继续问下一家数据源，
                    # 也才不会让空载荷混进证据账本把覆盖率抬到 86%。
                    raise RuntimeError(empty)
                attempts.append({"connector": cid, "ok": True, "ms": round((time.perf_counter() - t0) * 1000)})
                return CapabilityResult(capability, True, data=data, connector=cid, tool=tool,
                                        elapsed_ms=round((time.perf_counter() - started) * 1000),
                                        attempts=attempts, args=call_args)
            except Exception as exc:
                # 异常文本里可能带完整 URL（?key=...），这里一次打码，后面的日志/证据/导出都干净
                msg = redact(str(exc) or type(exc).__name__)[:300]
                logger.warning("能力 %s 经 %s 调用失败: %s", capability, cid, msg)
                attempts.append({"connector": cid, "ok": False, "error": msg})
        # 全部候选都「没接入 / 被停用」：直接告诉用户该接谁，别让他对着一串内部 id 猜
        cfg_errors = {"未配置该数据源", "已在设置中停用"}
        if attempts and all(a.get("error") in cfg_errors for a in attempts):
            error = missing_source_error(capability, routes)
        else:
            error = attempts[-1]["error"] if attempts else "无可用数据源"
        return CapabilityResult(capability, False, error=error,
                                elapsed_ms=round((time.perf_counter() - started) * 1000),
                                attempts=attempts)

    # ---------- 健康检查 ----------

    async def health(self, connector_id: str, force: bool = False) -> Dict[str, Any]:
        self._bound()
        conn = _BY_ID.get(connector_id)
        if not conn or not conn.server_id:
            return {"id": connector_id, "connected": False, "error": "未发现该 MCP 服务器配置",
                    "checked_at": datetime.datetime.now().isoformat(timespec="seconds")}
        cached = self._tools_cache.get(connector_id)
        if cached and not force and time.time() - cached["at"] < _HEALTH_TTL:
            return cached["health"]
        started = time.perf_counter()
        health: Dict[str, Any] = {"id": connector_id, "server_id": conn.server_id,
                                  "transport": conn.transport,
                                  "checked_at": datetime.datetime.now().isoformat(timespec="seconds")}
        try:
            client = mcp_client_pool.get_client(conn.server_id)
            tools = await client.list_tools()
            names = [t.get("name") for t in tools if isinstance(t, dict)]
            health.update({
                "connected": True,
                "tools_count": len(names),
                "latency_ms": round((time.perf_counter() - started) * 1000),
                "missing_tools": sorted({t for t in conn.tools.values() if t not in names}),
            })
        except Exception as exc:
            health.update({"connected": False, "error": redact(str(exc) or type(exc).__name__)[:300]})
        self._tools_cache[connector_id] = {"at": time.time(), "health": health}
        return health

    async def health_all(self, force: bool = False) -> List[Dict[str, Any]]:
        self._bound()
        results = await asyncio.gather(*[self.health(c.id, force=force) for c in _CONNECTORS],
                                       return_exceptions=True)
        out = []
        for conn, res in zip(_CONNECTORS, results):
            base = {"id": conn.id, "name": conn.name, "description": conn.description,
                    "paid": conn.paid, "notes": conn.notes,
                    "capabilities": [cap for cap in conn.tools if cap in CAPABILITY_LABELS],
                    "enabled": self._enabled(conn.id)}
            if isinstance(res, Exception):
                base.update({"connected": False, "error": str(res)[:200]})
            else:
                base.update(res)
            out.append(base)
        return out

    # ---------- 额度 ----------

    async def quota(self, connector_id: str, force: bool = False) -> Dict[str, Any]:
        self._bound()
        conn = _BY_ID.get(connector_id)
        cached = self._quota_cache.get(connector_id)
        if cached and not force and time.time() - cached["at"] < 60:
            return cached["quota"]
        if not conn or not conn.server_id or not conn.quota_tool:
            return {"id": connector_id, "supported": False}
        try:
            raw = await self._call_tool(conn.server_id, conn.quota_tool, {}, timeout=20)
            quota = _parse_quota(conn.quota_parser, raw)
        except Exception as exc:
            quota = {"id": connector_id, "supported": True, "error": str(exc)[:200]}
        quota["id"] = connector_id
        self._quota_cache[connector_id] = {"at": time.time(), "quota": quota}
        return quota

    async def quotas(self, force: bool = False) -> Dict[str, Dict[str, Any]]:
        ids = [c.id for c in _CONNECTORS if c.quota_tool]
        results = await asyncio.gather(*[self.quota(i, force=force) for i in ids], return_exceptions=True)
        out = {}
        for cid, res in zip(ids, results):
            out[cid] = res if isinstance(res, dict) else {"id": cid, "error": str(res)[:200]}
        return out


def _parse_quota(parser: Optional[str], raw: Any) -> Dict[str, Any]:
    """把各家的额度返回统一成 {label, used, total, remaining, unit}。"""
    if not isinstance(raw, dict):
        return {"supported": True, "raw": str(raw)[:300]}
    if parser == "sif":
        scopes = raw.get("scopes") or []
        first = scopes[0] if scopes and isinstance(scopes[0], dict) else {}
        return {"supported": True, "label": "SIF 点数",
                "total": sum(int(s.get("quotaLimit") or 0) for s in scopes if isinstance(s, dict)) or first.get("quotaLimit"),
                "used": sum(int(s.get("usedCount") or 0) for s in scopes if isinstance(s, dict)),
                "remaining": raw.get("remainingTotal"),
                "reset_at": first.get("periodEnd"), "unit": "点"}
    if parser == "sorftime":
        credits = dig(raw, "data.credit_left", "credit_left", "balance")
        return {"supported": True, "label": "Sorftime 额度", "remaining": credits, "unit": "积分"}
    if parser == "xydc":
        body = raw.get("data") if isinstance(raw.get("data"), dict) else raw
        weekly = body.get("weekly") or {}
        monthly = body.get("monthly") or {}
        return {"supported": True, "label": f"西柚额度（{body.get('planName') or '套餐'}）",
                "remaining": weekly.get("remaining"), "total": monthly.get("limit"),
                "used": monthly.get("used"), "unit": "Credits",
                "reset_at": weekly.get("resetTime"),
                "status": body.get("accountStatus"),
                "restrictions": body.get("restrictions") or []}
    return {"supported": True, "raw": raw}


connector_registry = ConnectorRegistry()
