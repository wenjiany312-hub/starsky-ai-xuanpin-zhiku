"""1688 整品归档：纯解析 + 媒体下载 + 全页扫描 + 报告渲染。

设计文档：docs/superpowers/specs/2026-06-20-1688-product-archiver-design.md
所有解析函数对缺失字段安全降级，绝不抛异常。
"""
import json
import os
import re

import requests
import urllib3

urllib3.disable_warnings()

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


def _dig(d, path, default=None):
    """按点号路径安全取值，任一层缺失返回 default。"""
    cur = d
    for key in path.split("."):
        if isinstance(cur, dict) and key in cur:
            cur = cur[key]
        else:
            return default
    return cur


def parse_product_data(data: dict) -> dict:
    """把 window.context.result.data 映射为结构化字段（纯函数，空值容错）。"""
    g = _dig(data, "gallery.fields", {}) or {}

    price_tiers = []
    for t in _dig(data, "mainPrice.fields.priceModel.currentPrices", []) or []:
        price_tiers.append({
            "begin_amount": t.get("beginAmount"),
            "price": t.get("price", ""),
        })

    sku_props = []
    for prop in _dig(data, "Root.fields.dataJson.skuModel.skuProps", []) or []:
        sku_props.append({
            "prop": prop.get("prop", ""),
            "values": [
                {"name": v.get("name", ""), "image_url": v.get("imageUrl", "")}
                for v in (prop.get("value") or [])
            ],
        })

    cpv = g.get("CpvEnhance") or {}
    attributes = []
    for a in (cpv.get("decisionCpv") or []) + (cpv.get("normalCpv") or []):
        attributes.append({"name": a.get("name", ""), "values": a.get("values") or []})

    pws = []
    for s in _dig(data, "productPackInfo.fields.pieceWeightScale.pieceWeightScaleInfo", []) or []:
        pws.append({
            "sku1": s.get("sku1", ""), "sku2": s.get("sku2", ""),
            "length": s.get("length"), "width": s.get("width"), "height": s.get("height"),
            "weight": s.get("weight"), "volume": s.get("volume"),
        })

    video = g.get("video") or {}

    return {
        "offer_id": str(g.get("offerId", "")),
        "title": g.get("subject", "") or "",
        "main_images": list(g.get("offerImgList") or g.get("mainImage") or []),
        "video": {"video_url": video.get("videoUrl", ""), "cover_url": video.get("coverUrl", "")},
        "price_tiers": price_tiers,
        "price_range": _dig(data, "mainPrice.fields.finalPriceModel.tradeWithoutPromotion.offerPriceDisplay", "") or "",
        "sku_props": sku_props,
        "attributes": attributes,
        "piece_weight_scale": pws,
        "company_name": _dig(data, "Root.fields.dataJson.tempModel.companyName", "") or "",
        "seller_login_id": _dig(data, "Root.fields.dataJson.tempModel.sellerLoginId", "") or "",
        "shop_url": _dig(data, "Root.fields.dataJson.offerBaseInfo.sellerWinportUrl", "") or "",
        "location": _dig(data, "shippingServices.fields.location", "") or "",
        "delivery_text": _dig(data, "shippingServices.fields.deliveryLimitText", "") or "",
        "sale_num": _dig(data, "productTitle.fields.saleNum", "") or "",
        "rate_info": _dig(data, "productTitle.fields.rateInfo", {}) or {},
        "detail_url": _dig(data, "description.fields.detailUrl", "") or "",
    }


def safe_name(s: str, n: int = 20) -> str:
    """标题转安全文件夹名：非法字符与空白替换为下划线，截断到 n 字符。"""
    s = re.sub(r'[\\/:*?"<>|\s]+', "_", s or "product")
    return s[:n] or "product"


def _download(url: str, path: str, referer: str = "https://detail.1688.com/"):
    """下载单个资源，成功返回字节数，失败返回 'FAIL: ...' 字符串（不抛）。"""
    try:
        r = requests.get(url, headers={"User-Agent": _UA, "Referer": referer},
                         timeout=30, verify=False)
        r.raise_for_status()
        with open(path, "wb") as f:
            f.write(r.content)
        return len(r.content)
    except Exception as e:  # noqa: BLE001 — 单图失败不阻断整体
        return f"FAIL: {e}"


def fetch_detail_html(detail_url: str) -> str:
    """抓取 detailUrl 的原始响应文本（含详情长图）。

    detailUrl（如 itemcdn.tmall.com/1688offer/icoss...）返回的是
    `var offer_details={"content":"...<img src=...>..."}` 这类 JSONP/JS 文本，
    用浏览器导航只会被重定向到 1688 导航页，必须直接用 requests 抓原始响应。
    失败返回空字符串（不阻断整体归档）。
    """
    if not detail_url:
        return ""
    try:
        r = requests.get(detail_url,
                         headers={"User-Agent": _UA, "Referer": "https://detail.1688.com/"},
                         timeout=30, verify=False)
        r.raise_for_status()
        r.encoding = "utf-8"
        return r.text
    except Exception:  # noqa: BLE001 — 详情抓取失败不阻断整体
        return ""


def extract_detail_image_urls(detail_html: str) -> list:
    """从 detailUrl 原始响应里正则抽取产品图 URL，清洗转义斜杠 + 去重保序。

    只收 cbu01.alicdn.com/img/ibank 产品图，剔除 gw.alicdn.com 平台图标与 .svg。
    """
    found = re.findall(
        r'https?:\\?/\\?/[^"\')\s]*?alicdn\.com[^"\')\s]*?\.(?:jpg|jpeg|png|gif|webp)',
        detail_html or "",
    )
    out, seen = [], set()
    for u in found:
        u = u.replace("\\/", "/")
        if "gw.alicdn.com" in u or u.endswith(".svg"):
            continue
        if "cbu01.alicdn.com" not in u and "/img/ibank" not in u:
            continue
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def download_assets(parsed: dict, out_dir: str, detail_html: str = "") -> dict:
    """建子目录并下载全部媒体，返回 manifest。单项失败记录后跳过。"""
    dirs = {k: os.path.join(out_dir, k)
            for k in ("main_images", "sku_images", "detail_images", "video")}
    for p in dirs.values():
        os.makedirs(p, exist_ok=True)

    files = []
    counts = {"main_images": 0, "sku_images": 0, "detail_images": 0, "video": 0}

    def record(url, local_rel, local_abs):
        res = _download(url, local_abs)
        ok = not isinstance(res, str)
        files.append({"local": local_rel, "url": url, "ok": ok,
                      "bytes": res if ok else None, "error": None if ok else res})
        return ok

    seen_urls = set()
    # 主副图
    for i, u in enumerate(parsed.get("main_images") or [], 1):
        if u in seen_urls:
            continue
        seen_urls.add(u)
        rel = os.path.join("main_images", f"main_{i:02d}.jpg")
        if record(u, rel, os.path.join(out_dir, rel)):
            counts["main_images"] += 1

    # SKU 变体图
    sku_idx = 0
    for prop in parsed.get("sku_props") or []:
        for v in prop.get("values") or []:
            u = v.get("image_url")
            if not u or u in seen_urls:
                continue
            seen_urls.add(u)
            sku_idx += 1
            rel = os.path.join("sku_images", f"sku_{sku_idx:02d}.jpg")
            if record(u, rel, os.path.join(out_dir, rel)):
                counts["sku_images"] += 1

    # 视频 + 封面
    video = parsed.get("video") or {}
    if video.get("video_url"):
        rel = os.path.join("video", "product_video.mp4")
        if record(video["video_url"], rel, os.path.join(out_dir, rel)):
            counts["video"] += 1
    if video.get("cover_url"):
        rel = os.path.join("video", "cover.jpg")
        record(video["cover_url"], rel, os.path.join(out_dir, rel))

    # 详情长图（来自 detailUrl 原始响应，是规格表/卖点的权威来源）
    for i, u in enumerate(extract_detail_image_urls(detail_html), 1):
        if u in seen_urls:
            continue
        seen_urls.add(u)
        rel = os.path.join("detail_images", f"detail_{i:02d}.jpg")
        if record(u, rel, os.path.join(out_dir, rel)):
            counts["detail_images"] += 1

    return {"files": files, "counts": counts}


def sweep_page(page) -> dict:
    """全页扫描第②层：滚动触发懒加载后取整页 innerText（热门推荐竞品 + 详情文案）。

    page: DrissionPage 的 ChromiumPage。返回 {"text": str}，下游交给 parse_page_sweep。
    详情图（规格表/卖点的权威来源）走 detailUrl 原始响应，不在这里采集，
    避免把页面里的竞品推荐缩略图混进规格图。
    """
    import time as _time

    for _ in range(6):
        try:
            page.run_js("window.scrollBy(0, document.body.scrollHeight/6)")
        except Exception:
            break
        _time.sleep(1.2)
    _time.sleep(2)

    try:
        text = page.run_js("return document.body.innerText;") or ""
    except Exception:
        text = ""
    return {"text": text}


_PLATFORM_BOILERPLATE = [
    "【平台活动下价格】", "【非平台活动下价格】", "【发布价】", "【全网销量】",
    "活动前价格", "前述价格未计算", "前述商品销售价格", "划线价格",
    "未划线价格", "内容声明", "阿里巴巴中国站为第三方", "阿里巴巴提醒您",
    "阿里巴巴中国站存在海量", "指同款商品在多个平台",
    "指商品在1688平台上", "同款：指商品名称",
    "活动前的时间通常为预热期", "前述说明仅适用于",
    "*注：", "该价格不包含平台",
]


def _is_boilerplate(line: str) -> bool:
    return any(kw in line for kw in _PLATFORM_BOILERPLATE)


_REVIEW_TAG_PATTERN = re.compile(
    r"^([一-鿿]{2,6})(\d{1,3})$"
)


def _extract_reviews(text: str) -> dict:
    """从全页文本里提取评价摘要。"""
    info = {"score": "", "count": "", "positive_rate": "", "tags": [], "highlights": []}
    m = re.search(r"商品评价\s*([\d.]+)", text)
    if m:
        info["score"] = m.group(1)
    m = re.search(r"(\d+\+?)条评价", text)
    if m:
        info["count"] = m.group(1)
    m = re.search(r"好评率\s*([\d.]+%)", text)
    if m:
        info["positive_rate"] = m.group(1)
    review_block = ""
    if "商品评价" in text:
        parts = text.split("商品评价")
        for part in parts[1:]:
            if "条评价" in part or "好评率" in part:
                review_block = part.split("查看全部评价")[0] if "查看全部评价" in part else part[:2000]
                break
    seen_tags = set()
    for line in review_block.splitlines():
        line = line.strip()
        tm = _REVIEW_TAG_PATTERN.match(line)
        if tm and tm.group(1) not in seen_tags:
            seen_tags.add(tm.group(1))
            info["tags"].append({"label": tm.group(1), "count": int(tm.group(2))})
    for marker in ["回头客", "1件"]:
        if marker in review_block:
            after = review_block.split(marker, 1)[1]
            for line in after.splitlines():
                line = line.strip()
                if (len(line) > 15 and line not in ("预览", "查看全部评价")
                        and not _is_boilerplate(line)
                        and line not in info["highlights"]):
                    info["highlights"].append(line)
                    break
    return info


def parse_page_sweep(raw_text: str) -> dict:
    """从扫描全文提炼下游可用内容：热门推荐竞品表 + 评价摘要 + 清洗后详情文字（纯函数）。"""
    text = raw_text or ""
    recommendations = []
    if "热门推荐" in text:
        block = text.split("热门推荐", 1)[1]
        for line in block.splitlines():
            line = line.strip()
            if not line:
                continue
            m = re.search(r"(.+?)\s*¥\s*([\d.]+)", line)
            if m:
                sales = re.search(r"(月销[\d.]+[万件]*|销量[\d.]+)", line)
                recommendations.append({
                    "name": m.group(1).strip(),
                    "price": "¥" + m.group(2),
                    "sales": sales.group(1) if sales else "",
                })

    reviews = _extract_reviews(text)

    detail_text = ""
    if "商品详情" in text:
        seg = text
        for section_start in ["商品评价", "商品属性", "包装信息", "商品件重尺"]:
            if section_start in seg:
                before = seg.split(section_start, 1)[0]
                after_parts = seg.split(section_start, 1)[1]
                next_section = ""
                for ns in ["商品属性", "包装信息", "商品件重尺", "商品详情", "热门推荐"]:
                    if ns != section_start and ns in after_parts:
                        next_section = ns
                        break
                if next_section:
                    after = after_parts.split(next_section, 1)[1]
                    seg = before + next_section + after
                else:
                    seg = before

        if "商品详情" in seg:
            seg = seg.split("商品详情", 1)[1]
        seg = seg.split("热门推荐", 1)[0]

        lines = seg.splitlines()
        cleaned = []
        seen = set()
        for line in lines:
            line = line.strip()
            if not line or len(line) < 4:
                continue
            if _is_boilerplate(line):
                continue
            if re.match(r"^[\d.]+[%]?$", line):
                continue
            if line in seen:
                continue
            seen.add(line)
            cleaned.append(line)
        detail_text = "\n".join(cleaned)

    return {"recommendations": recommendations, "detail_text": detail_text, "reviews": reviews}


# 规格字段 → 识别文本里的中文标签别名（顺序无关，取第一处命中）
_SPEC_LABELS = {
    "material": ["材质", "材料"],
    "size": ["产品尺寸", "外形尺寸", "规格尺寸", "整体尺寸", "尺寸"],
    "product_name": ["品名", "产品名称", "商品名称"],
    "net_weight": ["产品净重", "净重", "单件净重"],
    "carton_qty": ["装箱数量", "装箱数", "每箱数量", "装箱"],
    "inner_box": ["内盒尺寸", "内盒"],
    "outer_box": ["大箱尺寸", "外箱尺寸", "外箱", "大箱"],
    "carton_gross_weight": ["大箱毛重", "毛重"],
    "carton_net_weight": ["大箱净重", "箱净重"],
    "standard": ["执行标准"],
    "patent": ["专利号", "专利"],
}
# 卖点标语关键词：命中即收集该行
_SELLING_HINTS = ["理由", "防水", "防潮", "防尘", "加厚", "加大", "收纳",
                  "一物多用", "颜值", "稳固", "便捷"]

# 预计算 (别名, 字段) 列表，按别名长度降序——保证 "大箱毛重" 早于宽松前缀 "大箱" 命中
_SPEC_ALIAS_PAIRS = sorted(
    ((alias, field) for field, aliases in _SPEC_LABELS.items() for alias in aliases),
    key=lambda pair: len(pair[0]), reverse=True,
)


def _match_spec_label(label: str):
    """把一个 label 文本解析为 spec 字段名。优先精确等于，回退最长前缀匹配。"""
    for alias, field in _SPEC_ALIAS_PAIRS:
        if label == alias:
            return field
    for alias, field in _SPEC_ALIAS_PAIRS:
        if label.startswith(alias):
            return field
    return None


def parse_spec_text(text: str) -> dict:
    """把一张规格图的识别文本解析为规格字段（纯函数）。命中不到的字段返回 ""。

    支持两种布局：
    1. 同行："材质：ABS+GPPS" / "材质 ABS+GPPS"
    2. 跨行（1688 详情图常见，标签独占一行 + 英文翻译行 + 值行）：
       材质：
       material quality
       麻绳、密度板
    跨行时向下扫描 3 行，跳过纯英文短词（标签的英文翻译），取首个非标签行作为值。
    """
    spec = {k: "" for k in _SPEC_LABELS}
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]

    i = 0
    while i < len(lines):
        line = lines[i]

        # case 1：同行 "label：value"
        m_inline = re.match(r"^([一-龥A-Za-z/]+)\s*[:：]\s*(.+)$", line)
        if m_inline:
            label = m_inline.group(1).strip()
            value = m_inline.group(2).strip()
            chosen = _match_spec_label(label)
            if chosen is not None and not spec[chosen]:
                spec[chosen] = value
            i += 1
            continue

        # case 2：跨行 — 当前行只是 "label：" 或 "label"，下面找值
        m_label = re.match(r"^([一-龥A-Za-z/]+)\s*[:：]?\s*$", line)
        if m_label:
            label = m_label.group(1).strip()
            chosen = _match_spec_label(label)
            if chosen is not None and not spec[chosen]:
                for j in range(i + 1, min(i + 4, len(lines))):
                    cand = lines[j]
                    # 跳过纯英文短词（label 的英文翻译，如 "Product Name"）
                    if re.match(r"^[A-Za-z\s]+$", cand) and len(cand) < 30:
                        continue
                    # 遇到另一个 label，本字段放弃
                    if re.match(r"^[一-龥A-Za-z/]+\s*[:：]\s*$", cand):
                        break
                    spec[chosen] = cand
                    break

        # case 3：无冒号但有值，沿用老的宽松匹配（兼容旧 fixture）
        m_loose = re.match(r"^([一-龥A-Za-z/]+)\s+(.+)$", line)
        if m_loose:
            label = m_loose.group(1).strip()
            value = m_loose.group(2).strip()
            chosen = _match_spec_label(label)
            if chosen is not None and not spec[chosen]:
                spec[chosen] = value

        i += 1
    return spec


def _clean_ocr_lines(text: str) -> list:
    """把一张图的 OCR 文本清成可读中文行：去重、去纯英文装饰/翻译行、去碎符号。

    用途/使用场景/使用对象 的归纳依据就藏在详情图营销话术里（"大尺寸肥猫无忧"
    "理想宠物沙发怎么选"…），这些行不该像卖点过滤那样被关键词白名单丢掉。
    """
    out, seen = [], set()
    for ln in (text or "").splitlines():
        ln = ln.strip()
        if not ln or ln in seen:
            continue
        # 纯 ASCII（英文翻译行 / 装饰字母 / 符号）对中文归纳没价值，跳过
        if re.match(r"^[\x00-\x7f]+$", ln):
            continue
        # 至少含一个中文字符才保留
        if not re.search(r"[一-龥]", ln):
            continue
        seen.add(ln)
        out.append(ln)
    return out


def read_image_content(image_paths: list, recognized: dict = None) -> dict:
    """第④层读图层。

    recognized 为 None：返回待读图清单骨架，交由 Claude 在对话层逐张读图后回填。
    recognized 为 {path: 识别文本}：逐张 parse_spec_text，多图并集，冲突字段并列保留；
    收集卖点标语行；并保留每张详情图的 OCR 中文原文（detail_texts），
    供调用方模型据此归纳用途/使用场景/使用对象。
    """
    image_paths = list(image_paths or [])
    if not recognized:
        return {"images_to_read": image_paths, "spec_fields": {},
                "selling_points": [], "detail_texts": []}

    spec_fields = {}
    selling_points = []
    detail_texts = []
    for _path, text in recognized.items():
        spec = parse_spec_text(text)
        for field, value in spec.items():
            if not value:
                continue
            bucket = spec_fields.setdefault(field, [])
            if value not in bucket:
                bucket.append(value)
        for line in (text or "").splitlines():
            line = line.strip()
            if line and any(h in line for h in _SELLING_HINTS):
                if line not in selling_points:
                    selling_points.append(line)
        lines = _clean_ocr_lines(text)
        if lines:
            detail_texts.append({"image": os.path.basename(_path), "lines": lines})
    return {"images_to_read": image_paths, "spec_fields": spec_fields,
            "selling_points": selling_points, "detail_texts": detail_texts}


_AI_DISCLAIMER = "⚠️ 本节为 AI 推断，非页面原文，需二次核实。"
_AUDIENCE_DIMENSIONS = [
    "用途", "使用场景", "使用对象",
    "性别年龄", "身份场景", "核心痛点", "购买动机",
    "亚马逊关键词方向", "适配站点",
]


_SKIP_ATTRS_FOR_SP = {
    "颜色", "规格", "货号", "箱装数量", "包装方式", "产品类别",
    "加工方式", "适用对象", "打样周期",
}

_HIGHLIGHT_ATTRS = {
    "材质", "品牌", "风格", "产地", "适用场景",
    "主要销售地区", "主要下游平台",
    "是否跨境出口专供货源", "是否专利货源",
}


def build_ai_analysis(parsed: dict, competitors: list) -> dict:
    """生成第 2/3 节 AI 骨架。只保留有选品价值的属性，过滤纯标识字段。"""
    selling_points = []
    for attr in parsed.get("attributes") or []:
        name = attr.get("name", "")
        values = attr.get("values") or []
        if not name or not values:
            continue
        if name in _SKIP_ATTRS_FOR_SP:
            continue
        joined = "、".join(str(v) for v in values)
        if len(joined) > 80:
            joined = joined[:77] + "…"
        selling_points.append({
            "claim": name,
            "evidence": joined,
            "source_attr": name,
            "highlight": name in _HIGHLIGHT_ATTRS,
        })

    audience = [{"dimension": d, "inference": "", "basis": ""} for d in _AUDIENCE_DIMENSIONS]

    return {
        "selling_points": selling_points,
        "audience": audience,
        "competitors": competitors or [],
        "disclaimer": _AI_DISCLAIMER,
    }


def apply_audience_inferences(ai: dict, inferences: dict) -> dict:
    """把调用方模型归纳的 用途/使用场景/使用对象 等维度回填进 ai['audience']。

    inferences: {维度名: "推断文本"} 或 {维度名: {"inference": str, "basis": str}}。
    只更新 _AUDIENCE_DIMENSIONS 里已存在的维度，未知维度忽略；原地修改并返回 ai。
    """
    if not ai or not inferences:
        return ai
    by_dim = {d.get("dimension"): d for d in (ai.get("audience") or [])}
    for dim, val in inferences.items():
        target = by_dim.get(dim)
        if not target:
            continue
        if isinstance(val, dict):
            if val.get("inference") is not None:
                target["inference"] = str(val["inference"]).strip()
            if val.get("basis") is not None:
                target["basis"] = str(val["basis"]).strip()
        elif val is not None:
            target["inference"] = str(val).strip()
    return ai


def _spec_or_pending(spec_fields: dict, key: str) -> str:
    vals = (spec_fields or {}).get(key) or []
    if not vals:
        return "待供应商核实"
    return " / ".join(vals)


def _attr_lookup(parsed: dict, names) -> str:
    """从商品参数(attributes)里按名字取值，返回第一个命中项（多值用、连接）。"""
    attrs = (parsed or {}).get("attributes") or []
    for want in names:
        for a in attrs:
            if a.get("name") == want:
                vals = a.get("values") or []
                if vals:
                    return "、".join(str(v) for v in vals)
    return ""


def _spec_or_attr_or_pending(spec_fields: dict, key: str,
                             parsed: dict, attr_names) -> str:
    """优先详情图 OCR；OCR 没有再回退商品参数；都没有才标待核实。

    材质 / 执行标准 这类字段往往商品参数里就有，不该因为 OCR 没扫到就标"待核实"。
    箱规类（装箱数量/箱尺寸/毛净重）只在详情图里，仍走 _spec_or_pending。
    """
    vals = (spec_fields or {}).get(key) or []
    if vals:
        return " / ".join(vals)
    fallback = _attr_lookup(parsed, attr_names)
    if fallback:
        return fallback
    return "待供应商核实"


def _md(s) -> str:
    """动态文本在 Markdown / 表格里安全显示。
    本域里 * 恒为尺寸连乘符（50.1*37*36.5cm），转成 × 既正确又好看；
    其余会破坏渲染的字符做转义（| 撑破表格、_ 误判斜体、` 误判代码）。"""
    s = str(s if s is not None else "")
    s = s.replace("*", "×")
    s = s.replace("|", "\\|")
    s = s.replace("_", "\\_")
    s = s.replace("`", "\\`")
    return s


def _price_tiers_view(tiers: list) -> dict:
    """归一化价格档位。1688 常把'最低价/最高价'塞成两条 beginAmount 相同的档，
    这不是真正的起批量阶梯，识别后折叠成价格区间，避免出现两行'≥1 件'。"""
    rows = [(t.get("begin_amount"), str(t.get("price", "")).strip())
            for t in (tiers or []) if str(t.get("price", "")).strip()]
    if not rows:
        return {"kind": "none", "rows": []}
    begins = {b for b, _ in rows}
    if len(begins) <= 1 and len(rows) > 1:
        prices = [p for _, p in rows]
        return {"kind": "range", "low": prices[0], "high": prices[-1], "rows": rows}
    return {"kind": "ladder", "rows": rows}


def render_report_md(parsed, manifest, sweep, ai, image_content, meta) -> str:
    """渲染 14 节中文报告（纯函数）。物理规格置顶、缺据标待核实、AI 节标推断。"""
    sf = (image_content or {}).get("spec_fields", {})
    counts = (manifest or {}).get("counts", {})
    L = []
    A = L.append

    A(f"# 产品归档报告：{parsed.get('title', '')}")
    A("")
    A(f"- 商品 ID：{parsed.get('offer_id', '')}")
    A(f"- 来源链接：{meta.get('url', '')}")
    A(f"- 归档日期：{meta.get('date', '')}")
    A(f"- 归档目录：`{meta.get('folder', '')}`")
    A(f"- 本地路径：`{meta.get('out_dir', '')}`")
    A("")

    # 1. 物理规格 ⭐
    A("## 1. 产品物理规格 ⭐")
    A("")
    A("| 规格项 | 值 | 来源 |")
    A("|---|---|---|")
    A(f"| 材质 | {_md(_spec_or_attr_or_pending(sf, 'material', parsed, ['材质']))} | 详情图/商品参数 |")
    A(f"| 产品尺寸 | {_md(_spec_or_attr_or_pending(sf, 'size', parsed, ['尺寸', '产品尺寸', '规格', '外形尺寸']))} | 详情图/商品参数 |")
    A(f"| 产品净重 | {_md(_spec_or_pending(sf, 'net_weight'))} | 详情图 |")
    A(f"| 装箱数量 | {_md(_spec_or_pending(sf, 'carton_qty'))} | 详情图 |")
    A(f"| 内盒尺寸 | {_md(_spec_or_pending(sf, 'inner_box'))} | 详情图 |")
    A(f"| 外箱尺寸 | {_md(_spec_or_pending(sf, 'outer_box'))} | 详情图 |")
    A(f"| 大箱毛重 | {_md(_spec_or_pending(sf, 'carton_gross_weight'))} | 详情图 |")
    A(f"| 大箱净重 | {_md(_spec_or_pending(sf, 'carton_net_weight'))} | 详情图 |")
    A(f"| 执行标准 | {_md(_spec_or_attr_or_pending(sf, 'standard', parsed, ['执行标准', '执行标准号', '执行质量标准']))} | 详情图/商品参数 |")
    A(f"| 专利号 | {_md(_spec_or_attr_or_pending(sf, 'patent', parsed, ['专利号']))} | 详情图/商品参数 |")
    A("")
    if len(sf.get("material") or []) > 1:
        A(f"> ⚠️ 材质跨来源冲突：{_md(' / '.join(sf['material']))} —— 请向供应商核实。")
        A("")

    # 2. AI 卖点
    sps = ai.get("selling_points", [])
    if sps:
        A("## 2. AI 产品卖点分析 🤖")
        A("")
        A(f"> {ai.get('disclaimer', '')}")
        A("")
        A("| 属性 | 值 |")
        A("|---|---|")
        for sp in sps:
            A(f"| **{sp['claim']}** | {sp['evidence']} |")
        A("")

    # 3. 用途 / 使用场景 / 使用对象（+ 用户画像）
    aud = ai.get("audience", [])
    has_aud = any(d.get("inference") for d in aud)
    A("## 3. 用途 / 使用场景 / 使用对象 🤖")
    A("")
    A(f"> {ai.get('disclaimer', '')}")
    if not has_aud:
        A("> 调用本 MCP 的模型：请基于第 12 节「详情图识别原文（OCR）」逐项归纳下表后回填。")
    A("")
    A("| 维度 | 推断 | 依据 |")
    A("|---|---|---|")
    for d in aud:
        inf = d.get("inference") or "（待归纳）"
        A(f"| {d['dimension']} | {_md(inf)} | {_md(d.get('basis', ''))} |")
    A("")

    # 4. 价格体系
    A("## 4. 价格体系")
    A("")
    A(f"价格区间：{parsed.get('price_range', '')}")
    A("")
    pv = _price_tiers_view(parsed.get("price_tiers"))
    if pv["kind"] == "range":
        A(f"款式价格跨度：¥{pv['low']} ~ ¥{pv['high']}（按款式 / 规格不同）")
        A("")
    elif pv["kind"] == "ladder":
        A("| 起批量 | 单价 |")
        A("|---|---|")
        for b, p in pv["rows"]:
            A(f"| ≥{b} 件 | ¥{p} |")
        A("")

    # 5. SKU
    A("## 5. SKU 库存明细")
    A("")
    for prop in parsed.get("sku_props", []):
        names = "、".join(_md(v.get("name", "")) for v in prop.get("values", []))
        A(f"- {prop.get('prop', '')}：{names}")
    A("")

    # 6. 件重尺
    A("## 6. 商品件重尺全表（单件）")
    A("")
    A("| 变体 | 长 | 宽 | 高 | 重量(g) | 体积(cm³) |")
    A("|---|---|---|---|---|---|")
    for s in parsed.get("piece_weight_scale", []):
        A(f"| {_md(s.get('sku1', ''))} | {s.get('length', '')} | {s.get('width', '')} "
          f"| {s.get('height', '')} | {s.get('weight', '')} | {s.get('volume', '')} |")
    A("")

    # 7. 参数
    A("## 7. 商品参数（完整）")
    A("")
    A("| 参数 | 值 |")
    A("|---|---|")
    for a in parsed.get("attributes", []):
        A(f"| {a.get('name', '')} | {_md('、'.join(str(v) for v in a.get('values', [])))} |")
    A("")

    # 8. 供应商
    A("## 8. 供应商信息")
    A("")
    A(f"- 公司：{parsed.get('company_name', '')}")
    A(f"- 发货地：{parsed.get('location', '')}")
    A(f"- 店铺链接：{parsed.get('shop_url', '')}")
    A("")

    # 9. 物流
    A("## 9. 物流与服务")
    A("")
    A(f"- 发货承诺：{parsed.get('delivery_text', '')}")
    A("")

    # 10. 评价
    A("## 10. 评价标签汇总")
    A("")
    ri = parsed.get("rate_info") or {}
    rev = (sweep or {}).get("reviews", {})
    score = rev.get("score") or ri.get("starLevel", "待核实")
    rate = rev.get("positive_rate") or ri.get("positiveFeedback", "待核实")
    count = rev.get("count", "")
    A(f"- 评分：{score} / 好评率：{rate}" + (f"（{count} 条评价）" if count else ""))
    A("")
    tags = rev.get("tags", [])
    if tags:
        A("| 标签 | 提及次数 |")
        A("|---|---|")
        for t in sorted(tags, key=lambda x: x["count"], reverse=True):
            A(f"| {t['label']} | {t['count']} |")
        A("")
    highlights = rev.get("highlights", [])
    if highlights:
        A("**典型评价摘录：**")
        A("")
        for h in highlights:
            A(f"> {_md(h)}")
            A("")

    # 11. 关联热门推荐
    A("## 11. 关联热门推荐（全页扫描补充）")
    A("")
    A("| 竞品 | 价格 | 销量 |")
    A("|---|---|---|")
    for r in (sweep or {}).get("recommendations", []):
        A(f"| {_md(r.get('name', ''))} | {r.get('price', '')} | {r.get('sales', '')} |")
    A("")

    # 12. 文案素材
    A("## 12. 商品详情文案素材（全页扫描补充）")
    A("")
    dt = (sweep or {}).get("detail_text", "")
    if dt:
        A("> 以下为全页扫描提取的详情文案（已去除平台法律声明等样板文字）：")
        A("")
        for line in dt.splitlines():
            line = line.strip()
            if line:
                A(f"- {_md(line)}")
        A("")
    else:
        A("*（无额外详情文案）*")
        A("")
    selling_pts = (image_content or {}).get("selling_points", [])
    if selling_pts:
        A("**详情图内卖点标语：**")
        A("")
        for s in selling_pts:
            A(f"- {_md(s)}")
        A("")
    detail_texts = (image_content or {}).get("detail_texts", [])
    if detail_texts:
        A("**详情图识别原文（OCR · 用途/场景/对象归纳依据）：**")
        A("")
        for dt in detail_texts:
            A(f"*{dt.get('image', '')}*")
            for ln in dt.get("lines", []):
                A(f"- {_md(ln)}")
            A("")

    # 13. 媒体清单（只给数量+目录）
    A("## 13. 媒体素材清单")
    A("")
    A(f"- 主图：{counts.get('main_images', 0)} 张 → `main_images/`")
    A(f"- SKU 图：{counts.get('sku_images', 0)} 张 → `sku_images/`")
    A(f"- 详情图：{counts.get('detail_images', 0)} 张 → `detail_images/`")
    A(f"- 视频：{counts.get('video', 0)} 个 → `video/`")
    fails = [m for m in (manifest or {}).get("files", []) if not m.get("ok")]
    if fails:
        A("")
        A(f"> ⚠️ 下载失败 {len(fails)} 项：" + "、".join(m["url"] for m in fails))
    A("")

    # 14. 商品标识
    A("## 14. 商品标识汇总")
    A("")
    A(f"- 全网销量：{parsed.get('sale_num', '待核实')}")
    A("")
    A("---")
    A(ai.get("disclaimer", ""))
    return "\n".join(L)


_OBS_SKIP_ATTRS = {"颜色", "规格", "箱装数量"}


def render_obsidian_md(parsed, manifest, sweep, ai, image_content, meta) -> str:
    """渲染 Obsidian 风格的精美产品报告。精简去重、空节不渲染。"""
    sf = (image_content or {}).get("spec_fields", {})
    counts = (manifest or {}).get("counts", {})
    rev = (sweep or {}).get("reviews", {})
    ri = parsed.get("rate_info") or {}
    L = []
    A = L.append

    title = parsed.get("title", "未知产品")
    loc = parsed.get("location", "")
    price_range = parsed.get("price_range", "")
    score = rev.get("score") or ri.get("starLevel", "")
    date_str = meta.get("date", "")

    # ── frontmatter ──
    A("---")
    A(f"title: \"{title}\"")
    if len(date_str) == 8:
        A(f"date: {date_str[:4]}-{date_str[4:6]}-{date_str[6:]}")
    A(f"source: \"{meta.get('url', '')}\"")
    A(f"supplier: \"{parsed.get('company_name', '')}\"")
    A(f"location: \"{loc}\"")
    A(f"price: \"{price_range}\"")
    if score:
        A(f"rating: {score}")
    A(f"sale: \"{parsed.get('sale_num', '')}\"")
    A(f"local_path: \"{meta.get('out_dir', '')}\"")
    A("status: 待评估")
    A("tags: [选品库, 1688]")
    A("---")
    A("")

    # ── 快览卡片 ──
    A("> [!info] 快览")
    A(f"> | 价格 | 评分 | 销量 | 发货地 |")
    A(f"> |:--|:--|:--|:--|")
    A(f"> | **{price_range or '-'}** | **{score or '-'}** | **{parsed.get('sale_num', '-')}** | {loc or '-'} |")
    A(f">")
    A(f"> **供应商**：{parsed.get('company_name', '')}")
    shop = parsed.get("shop_url", "")
    if shop:
        A(f"> **店铺**：{shop}")
    A(f"> **本地资料**：`{meta.get('out_dir', '')}`")
    A("")

    # ── 价格 ──
    pv = _price_tiers_view(parsed.get("price_tiers"))
    if pv["kind"] == "range":
        A("## 价格区间")
        A("")
        A(f"- **¥{pv['low']} ~ ¥{pv['high']}**（按款式 / 规格不同）")
        A("")
    elif pv["kind"] == "ladder":
        A("## 价格阶梯")
        A("")
        A("| 起批量 | 单价 |")
        A("|:--|:--|")
        for b, p in pv["rows"]:
            A(f"| ≥{b} 件 | **¥{p}** |")
        A("")

    # ── 物理规格 ──
    A("## 物理规格")
    A("")
    spec_rows = [
        ("材质", "material"), ("产品尺寸", "size"), ("产品净重", "net_weight"),
        ("装箱数量", "carton_qty"), ("内盒尺寸", "inner_box"),
        ("外箱尺寸", "outer_box"), ("大箱毛重", "carton_gross_weight"),
        ("大箱净重", "carton_net_weight"),
    ]
    # 材质/尺寸优先 OCR，没有则回退商品参数（箱规类仍只认 OCR）
    spec_attr_fallback = {
        "material": ["材质"],
        "size": ["尺寸", "产品尺寸", "规格", "外形尺寸"],
    }

    def _spec_val(key):
        if key in spec_attr_fallback:
            return _spec_or_attr_or_pending(sf, key, parsed, spec_attr_fallback[key])
        return _spec_or_pending(sf, key)

    has_real = any(_spec_val(k) != "⏳ 待读图回填" for _, k in spec_rows)
    if has_real:
        A("| 项目 | 值 |")
        A("|:--|:--|")
        for label, key in spec_rows:
            val = _spec_val(key)
            if val != "⏳ 待读图回填":
                A(f"| {label} | {_md(val)} |")
        A("")
    else:
        A("> [!todo] 待读图回填")
        A("> 详情图下载完成后，由 Claude 读图提取装箱数/箱规/毛重等规格。")
        A("")
    if len(sf.get("material") or []) > 1:
        A(f"> [!warning] 材质冲突：{_md(' / '.join(sf['material']))}")
        A("")

    # ── 核心参数（去掉颜色/规格等已在 SKU 节的重复项）──
    attrs = parsed.get("attributes", [])
    core_attrs = [a for a in attrs if a.get("name", "") not in _OBS_SKIP_ATTRS]
    if core_attrs:
        A("## 核心参数")
        A("")
        for a in core_attrs:
            name = a.get("name", "")
            vals = "、".join(str(v) for v in a.get("values", []))
            if len(vals) > 60:
                vals = vals[:57] + "…"
            A(f"- **{name}**：{_md(vals)}")
        A("")

    # ── 用途 / 使用场景 / 使用对象 ──
    aud = ai.get("audience", [])
    usage_dims = ("用途", "使用场景", "使用对象")
    usage_rows = [d for d in aud if d.get("dimension") in usage_dims]
    filled = [d for d in usage_rows if d.get("inference")]
    detail_texts = (image_content or {}).get("detail_texts", [])
    if filled:
        A("## 用途 / 使用场景 / 使用对象")
        A("")
        for d in usage_rows:
            inf = d.get("inference")
            if inf:
                A(f"- **{d['dimension']}**：{_md(inf)}")
        A("")
    elif detail_texts:
        A("## 用途 / 使用场景 / 使用对象")
        A("")
        A("> [!todo] 待归纳")
        A("> 下方为详情图 OCR 原文，由调用模型据此归纳用途 / 使用场景 / 使用对象。")
        A("")
    # ── 详情图文案（OCR 原文，折叠）──
    if detail_texts:
        total_lines = sum(len(dt.get("lines", [])) for dt in detail_texts)
        A(f"> [!quote]- 详情图识别原文（OCR · {total_lines} 行，点击展开）")
        for dt in detail_texts:
            A(f"> **{dt.get('image', '')}**")
            for ln in dt.get("lines", []):
                A(f"> - {_md(ln)}")
        A("")

    # ── SKU（折叠，避免撑爆页面）──
    sku_props = parsed.get("sku_props", [])
    if sku_props:
        total = sum(len(p.get("values", [])) for p in sku_props)
        A("## SKU 款式")
        A("")
        if total > 6:
            A(f"> [!example]- {total} 个变体（点击展开）")
            for prop in sku_props:
                values = [v.get("name", "") for v in prop.get("values", [])]
                A(f"> **{prop.get('prop', '')}**：")
                for v in values:
                    A(f"> - {_md(v)}")
            A("")
        else:
            for prop in sku_props:
                values = [_md(v.get("name", "")) for v in prop.get("values", [])]
                A(f"**{prop.get('prop', '')}**：{' · '.join(values)}")
            A("")

    # ── 件重尺（折叠 + 只显示有实际数据的列）──
    pws = parsed.get("piece_weight_scale", [])
    if pws:
        has_dim = any(int(s.get("length", 0) or 0) > 0 for s in pws)
        A("## 件重尺")
        A("")
        if len(pws) > 6:
            A(f"> [!example]- {len(pws)} 个变体（点击展开）")
            pre = "> "
        else:
            pre = ""
        if has_dim:
            A(f"{pre}| 变体 | 长 | 宽 | 高 | 重量(g) |")
            A(f"{pre}|:--|:--|:--|:--|:--|")
            for s in pws:
                sku = s.get("sku1", "")
                if len(sku) > 25:
                    sku = sku[:22] + "…"
                A(f"{pre}| {_md(sku)} | {s.get('length', '')} | {s.get('width', '')} "
                  f"| {s.get('height', '')} | {s.get('weight', '')} |")
        else:
            A(f"{pre}| 变体 | 重量(g) |")
            A(f"{pre}|:--|:--|")
            for s in pws:
                sku = s.get("sku1", "")
                if len(sku) > 35:
                    sku = sku[:32] + "…"
                A(f"{pre}| {_md(sku)} | {s.get('weight', '')} |")
        A("")

    # ── 评价 ──
    rate = rev.get("positive_rate") or ri.get("positiveFeedback", "")
    count = rev.get("count", "")
    tags = rev.get("tags", [])
    highlights = rev.get("highlights", [])
    if score or tags or highlights:
        A("## 评价")
        A("")
        parts = []
        if score:
            parts.append(f"评分 **{score}**")
        if rate:
            parts.append(f"好评率 **{rate}**")
        if count:
            parts.append(f"{count} 条评价")
        if parts:
            A("　".join(parts))
            A("")
        if tags:
            A(" ".join(f"`{t['label']}({t['count']})`" for t
                       in sorted(tags, key=lambda x: x["count"], reverse=True)))
            A("")
        for h in highlights:
            if len(h) > 15:
                A("> [!quote] 买家说")
                A(f"> {_md(h)}")
                A("")

    # ── 供应商 ──
    A("## 供应商")
    A("")
    A(f"| 项目 | 信息 |")
    A(f"|:--|:--|")
    A(f"| 公司 | {parsed.get('company_name', '')} |")
    A(f"| 发货地 | {loc} |")
    if shop:
        A(f"| 店铺 | {shop} |")
    delivery = parsed.get("delivery_text", "")
    if delivery:
        A(f"| 发货承诺 | {delivery} |")
    A("")

    # ── 竞品 ──
    recs = (sweep or {}).get("recommendations", [])
    if recs:
        A("## 关联竞品")
        A("")
        A("| 竞品 | 价格 | 销量 |")
        A("|:--|:--|:--|")
        for r in recs[:8]:
            name = r.get("name", "")
            if len(name) > 30:
                name = name[:27] + "…"
            A(f"| {name} | {r.get('price', '')} | {r.get('sales', '')} |")
        A("")

    # ── 媒体素材 ──
    A("## 媒体素材")
    A("")
    A(f"主图 **{counts.get('main_images', 0)}** · "
      f"详情图 **{counts.get('detail_images', 0)}** · "
      f"视频 **{counts.get('video', 0)}**")
    A("")

    return "\n".join(L)


# ── 渲染落盘 + 缓存 + AI 归纳回填 ────────────────────────────────────────────
_RENDER_CACHE = "_render_cache.json"


def _obsidian_path(name: str) -> str:
    return os.path.join("G:", os.sep, "Obsidian", "选品库", f"{name}.md")


def write_outputs(parsed: dict, manifest: dict, sweep: dict, ai: dict,
                  image_content: dict, meta: dict) -> dict:
    """渲染并落盘 product_report.md + Obsidian 选品库笔记。

    供 scrape 与 update_product_analysis 共用，保证两条路径渲染逻辑一致。
    Obsidian 写入失败（盘不在/无权限）不致命，obsidian_ok 标记结果。
    """
    out_dir = meta["out_dir"]
    report_md = render_report_md(parsed, manifest, sweep, ai, image_content, meta)
    report_path = os.path.join(out_dir, "product_report.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report_md)

    obsidian_md = render_obsidian_md(parsed, manifest, sweep, ai, image_content, meta)
    obsidian_name = f"{safe_name(parsed['title'])}_{meta['date']}"
    obsidian_path = _obsidian_path(obsidian_name)
    obsidian_ok = False
    try:
        os.makedirs(os.path.dirname(obsidian_path), exist_ok=True)
        with open(obsidian_path, "w", encoding="utf-8") as f:
            f.write(obsidian_md)
        obsidian_ok = True
    except Exception:
        pass
    return {
        "report_path": report_path,
        "obsidian_path": obsidian_path,
        "obsidian_name": obsidian_name,
        "obsidian_ok": obsidian_ok,
    }


def save_render_cache(parsed: dict, manifest: dict, sweep: dict, ai: dict,
                      image_content: dict, meta: dict) -> str:
    """把渲染所需的全部中间结果落盘，供 update_product_analysis 免重抓重 OCR 再渲染。"""
    out_dir = meta["out_dir"]
    cache = {
        "parsed": parsed, "manifest": manifest, "sweep": sweep,
        "ai": ai, "image_content": image_content, "meta": meta,
    }
    path = os.path.join(out_dir, _RENDER_CACHE)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)
    return path


def update_analysis_from_cache(out_dir: str, inferences: dict) -> dict:
    """读取渲染缓存 → 回填 AI 归纳 → 重新渲染落盘。

    缓存里同步更新后的 ai，使多次回填可累积。返回 write_outputs 的结果。
    """
    cache_path = os.path.join(out_dir, _RENDER_CACHE)
    if not os.path.exists(cache_path):
        raise FileNotFoundError(cache_path)
    with open(cache_path, encoding="utf-8") as f:
        cache = json.load(f)
    apply_audience_inferences(cache["ai"], inferences or {})
    # meta.out_dir 以实际目录为准（缓存可能从别处拷入）
    cache["meta"]["out_dir"] = out_dir
    save_render_cache(cache["parsed"], cache["manifest"], cache["sweep"],
                      cache["ai"], cache["image_content"], cache["meta"])
    return write_outputs(cache["parsed"], cache["manifest"], cache["sweep"],
                         cache["ai"], cache["image_content"], cache["meta"])
