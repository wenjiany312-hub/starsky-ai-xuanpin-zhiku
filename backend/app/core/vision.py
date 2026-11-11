"""
以图识货：把一张产品图变成选品链路能用的中英文关键词。

为什么这里不是「以图搜图」
- 亚马逊没有公开、免登录、可编程调用的以图搜图接口（Lens 页面只供人工上传）；
- 1688 以图搜图（sorftime ali1688_product_search_from_image）收的是**公网图片 URL**，
  由数据方服务端自己去取，本机 127.0.0.1 上的图片它取不到；
- 外观专利以图检索（sorftime patent_search_from_image）支持 URL 或 base64，本地图也能查。

所以链路是：
  任何图片 → 视觉模型识别 → 中文品名/英文品名/亚马逊核心词/1688 采购词
      → 走完整选品链路（关键词市场数据 + 1688 关键词找货）
  公网图片再追加：1688 以图搜同款
  任何图片都追加：外观专利以图检索（base64）
识别不出来就如实报失败，绝不编一个品名往下跑。
"""
import base64
import json
import re
from typing import Any, Dict, Optional, Tuple

from app.core.llm_gateway import LLMError, LLMUnavailable, llm_gateway
from app.core.logger import logger

MAX_IMAGE_BYTES = 8 * 1024 * 1024

_SUPPORTED_MIME = {"image/jpeg", "image/png", "image/webp", "image/gif"}

_PROMPT = """你是亚马逊美国站的选品买手。看这张产品图，判断它是**什么商品**，并给出找同款要用的词。

只输出 JSON，不要解释文字。字段：
{
  "is_product_photo": true/false（是不是一张可辨认的商品图；截图/纯文字/人像/风景图填 false）,
  "product_name_zh": "中文品名，2-10 个字，国人在 1688 会怎么搜（例如：硅胶烘焙垫）",
  "product_name_en": "英文品名，自然语言（例如：silicone baking mat）",
  "keyword": "买家在亚马逊搜索框里最可能输入的英文核心词，2-4 个词，全小写，不带品牌名",
  "keyword_zh": "1688 采购搜索用的中文词，2-8 个字，不带品牌名",
  "category": "最可能的亚马逊一级/二级类目，中文",
  "materials": ["看得出的主要材质"],
  "features": ["3-6 条能从图中确认的特征：形状/尺寸感/结构/配件/用途"],
  "main_use": "一句话说清主要使用场景",
  "visible_brand": "图中能看清的品牌或 Logo 文字，没有则空字符串",
  "risk_flags": ["合规或侵权上的直观风险，如：明显的卡通 IP 形象、医疗功效暗示、儿童用品小零件；没有则空数组"],
  "confidence": 0.0~1.0（识别把握；图模糊、遮挡、只露出一部分就调低）
}

要求：
- 看不清就不要猜具体型号和尺寸，宁可写空。
- 关键词必须是买家会搜的**通用词**，不要出现品牌名、不要出现"亚马逊""同款"之类。
"""


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    """模型偶尔会用 ```json 包一层或在前后说两句话，这里只摘出第一个 JSON 对象。"""
    if not text:
        return None
    s = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", s, re.S)
    if fenced:
        s = fenced.group(1)
    start = s.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(s)):
        if s[i] == "{":
            depth += 1
        elif s[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(s[start:i + 1])
                    return obj if isinstance(obj, dict) else None
                except json.JSONDecodeError:
                    return None
    return None


async def _pick_model(provider_id: Optional[str], model: Optional[str]) -> Tuple[str, str, bool]:
    """
    挑一个声明支持图片输入的模型。

    网关的 /v1/models 会报 modalities，优先用声明了 image 的模型；
    都没声明（老网关不报）就退回角色模型硬试一次，并让调用方知道「没确认支持图片」。
    """
    pid = provider_id or llm_gateway.role_model("strong")["provider"]
    role = llm_gateway.role_model("strong")
    preferred = model or (role["model"] if role["provider"] == pid else None)
    try:
        models = await llm_gateway.list_models(pid)
    except Exception as exc:  # 列不出模型不代表调不通，退回角色模型
        logger.info("读取模型列表失败，按角色模型试图片识别: %s", exc)
        return pid, preferred or role["model"], False
    if preferred:
        for m in models:
            if m["id"] == preferred:
                return pid, preferred, bool(m.get("vision"))
        return pid, preferred, False
    for m in models:
        if m.get("vision"):
            return pid, m["id"], True
    return pid, (models[0]["id"] if models else role["model"]), False


def data_uri(data: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def normalize(analysis: Dict[str, Any]) -> Dict[str, Any]:
    """把模型返回收拾成固定形状：缺的字段补空，类型不对的丢掉。"""
    def _s(key: str) -> str:
        v = analysis.get(key)
        return str(v).strip() if isinstance(v, (str, int, float)) else ""

    def _list(key: str, limit: int) -> list:
        v = analysis.get(key)
        if isinstance(v, str):
            v = [x.strip() for x in re.split(r"[;,、]", v) if x.strip()]
        if not isinstance(v, list):
            return []
        return [str(x).strip() for x in v if str(x).strip()][:limit]

    try:
        conf = float(analysis.get("confidence"))
    except (TypeError, ValueError):
        conf = None
    if conf is not None and conf > 1:  # 有的模型给 85 而不是 0.85
        conf = conf / 100
    return {
        "is_product_photo": bool(analysis.get("is_product_photo", True)),
        "product_name_zh": _s("product_name_zh"),
        "product_name_en": _s("product_name_en"),
        "keyword": _s("keyword").lower(),
        "keyword_zh": _s("keyword_zh"),
        "category": _s("category"),
        "materials": _list("materials", 5),
        "features": _list("features", 6),
        "main_use": _s("main_use"),
        "visible_brand": _s("visible_brand"),
        "risk_flags": _list("risk_flags", 5),
        "confidence": round(conf, 2) if conf is not None else None,
    }


async def analyze_image(data: bytes, mime: str, *, title_hint: str = "",
                        provider_id: Optional[str] = None,
                        model: Optional[str] = None) -> Dict[str, Any]:
    """
    识别一张图。返回 {"analysis": {...}, "provider", "model", "vision_declared", "elapsed_hint"}。
    失败抛 LLMError / LLMUnavailable，由调用方记成证据失败——不返回假结果。
    """
    if not data:
        raise LLMError("图片是空的")
    if len(data) > MAX_IMAGE_BYTES:
        raise LLMError(f"图片超过 {MAX_IMAGE_BYTES // 1024 // 1024}MB，请压缩后再传")
    if mime not in _SUPPORTED_MIME:
        raise LLMError(f"不支持的图片格式：{mime}（支持 JPG/PNG/WebP/GIF）")
    if not mime.startswith("image/"):
        raise LLMError("这不是图片文件")

    pid, mid, declared = await _pick_model(provider_id, model)
    prompt = _PROMPT
    if title_hint.strip():
        prompt += f"\n补充信息（用户填的标题，仅作参考，仍以图上看到的为准）：{title_hint.strip()[:200]}"
    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": data_uri(data, mime)}},
        ],
    }]
    last: Optional[str] = None
    for budget in (1200, 2400):
        reply = await llm_gateway.chat(messages, provider_id=pid, model=mid,
                                       temperature=0.1, max_tokens=budget, timeout=120)
        text = reply.get("content") or ""
        parsed = _extract_json(text)
        if parsed:
            return {"analysis": normalize(parsed), "provider": pid,
                    "model": reply.get("model") or mid, "vision_declared": declared}
        last = f"模型没有返回可解析的 JSON（返回了 {len(text)} 字）"
    raise LLMError(last or "图片识别失败")
