"""开跑前体检：这台机器现在能不能跑出一份像样的选品结论，缺什么、去哪修。

为什么要有它：数据源一个没接、或本机模型网关没开时，运行照样能跑完，
最后给一个「未研判」。新用户分不清是品不行还是自己没配好——所以要在开跑前就说清楚。
"""
import asyncio
import datetime
import time
from typing import Any, Dict, List

from fastapi import APIRouter

from app.core.connectors import connector_registry
from app.core.llm_gateway import llm_gateway
from app.core.redact import redact

router = APIRouter(prefix="/system", tags=["系统"])

# 一次完整选品需要的数据，按「能力组」而不是按某个牌子要求：有其一即可
_GROUPS: List[Dict[str, Any]] = [
    {"key": "keyword", "label": "关键词与竞争数据", "any_of": ["sif", "xydc"], "required": True,
     "lost": "拿不到搜索量、趋势和竞争格局，结论只能是「未研判」",
     "fix": "在「数据源」页接入 SIF 或西柚任意一个（填各自官网给的 MCP 地址/Key）"},
    {"key": "market", "label": "评论、买家讨论与专利", "any_of": ["sorftime"], "required": False,
     "lost": "报告会缺少用户声音（Reddit/Quora）、专利排查和云端 1688 同款",
     "fix": "在「数据源」页接入 Sorftime"},
    {"key": "supply", "label": "1688 货源", "any_of": ["1688_scraper", "sorftime"], "required": False,
     "lost": "算不出采购价，毛利率只能用估算值",
     "fix": "接入 Sorftime，或启用本机 1688 抓取（需登录 1688）"},
]

_model_cache: Dict[str, Any] = {}
_MODEL_TTL = 120


async def _model_check(force: bool) -> Dict[str, Any]:
    sel = llm_gateway.role_model("strong")
    key = f"{sel['provider']}/{sel['model']}"
    hit = _model_cache.get(key)
    if hit and not force and time.time() - hit["at"] < _MODEL_TTL:
        return hit["res"]
    res = await llm_gateway.test_connection(sel["provider"], sel["model"])
    out = {"provider": sel["provider"], "model": sel["model"], "ok": res.get("status") == "success",
           "latency_ms": res.get("latency_ms"), "message": redact(res.get("message") or "")[:200]}
    _model_cache[key] = {"at": time.time(), "res": out}
    return out


@router.get("/preflight", summary="开跑前体检：数据源与模型是否就绪，缺什么、怎么修")
async def preflight(force: bool = False):
    health, model = await asyncio.gather(connector_registry.health_all(force=force), _model_check(force))
    online = {h["id"] for h in health if isinstance(h, dict) and h.get("connected")}
    groups, blockers, warnings = [], [], []
    for g in _GROUPS:
        have = [c for c in g["any_of"] if c in online]
        item = {"key": g["key"], "label": g["label"], "required": g["required"], "ok": bool(have),
                "via": have, "lost": g["lost"], "fix": g["fix"]}
        groups.append(item)
        if not have:
            (blockers if g["required"] else warnings).append(
                {"key": g["key"], "title": f"{g['label']}：未接通", "detail": g["lost"], "fix": g["fix"],
                 "link": "/data"})
    if not model["ok"]:
        warnings.append({
            "key": "model", "title": f"模型连不上（{model['provider']} · {model['model']}）",
            "detail": "中文想法没法自动翻成英文关键词（需手填英文词），报告的 AI 解读也会缺失；规则判定不受影响。"
                      + (f" 原因：{model['message']}" if model["message"] else ""),
            "fix": "到「模型设置」换一个能用的模型，或启动本机 MagPie 网关", "link": "/settings"})
    return {
        "ready": not blockers,
        "blockers": blockers,
        "warnings": warnings,
        "groups": groups,
        "model": model,
        "online": sorted(online),
        "checked_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }
