"""数据源接口：连接器状态（真实探测）、额度、能力地图、启停、自定义 MCP。"""
import re
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.core.connectors import CAPABILITY_LABELS, connector_registry, user_routes
from app.core.mcp_client import mcp_client_pool
from app.core.settings_store import settings_store

router = APIRouter(prefix="/mcp", tags=["数据源"])

_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{2,40}$")


class ToggleRequest(BaseModel):
    enabled: bool


class CustomConnectorRequest(BaseModel):
    id: str = Field(..., description="字母数字下划线，2-40 位")
    name: str = ""
    transport: str = Field("http", description="http 或 stdio")
    url: Optional[str] = None
    headers: Dict[str, str] = Field(default_factory=dict)
    command: Optional[str] = None
    args: List[str] = Field(default_factory=list)
    env: Dict[str, str] = Field(default_factory=dict)
    connector: Optional[str] = Field(None, description="按哪个内置连接器的能力表使用（sif/sorftime/xydc/1688_scraper/sellersprite）")


def _mask(spec: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(spec)
    if out.get("headers"):
        out["headers"] = {k: "••••" for k in out["headers"]}
    if out.get("env"):
        out["env"] = {k: "••••" for k in out["env"]}
    if out.get("url"):
        out["url"] = re.sub(r"([?&](key|token|apikey|api_key|secret)=)[^&]+", r"\1••••", out["url"], flags=re.I)
    return out


class RoutesRequest(BaseModel):
    routes: Dict[str, List[str]] = Field(..., description="能力 → 连接器顺序，如 {\"market.product_search\": [\"sellersprite\", \"sorftime\"]}；空列表删除该条覆盖")


@router.get("/connectors", summary="连接器列表与健康状态（真实探测，3 分钟缓存）")
async def list_connectors(force: bool = Query(False)):
    return {"connectors": await connector_registry.health_all(force=force),
            "capabilities": CAPABILITY_LABELS,
            "available": connector_registry.available_capabilities(),
            "routes": connector_registry.capability_map(),
            "user_routes": user_routes()}


@router.put("/connectors/routes", summary="指定各能力的连接器顺序（留空=恢复默认）")
async def set_routes(req: RoutesRequest):
    cleaned = {}
    for cap, seq in (req.routes or {}).items():
        ids = [str(x) for x in seq if isinstance(x, (str, int))]
        if ids:
            cleaned[cap] = ids
    settings_store.set("connectors", "routes", cleaned)
    return {"routes": cleaned, "effective": connector_registry.capability_map()}


@router.get("/quotas", summary="各数据源剩余额度")
async def list_quotas(force: bool = Query(False)):
    return await connector_registry.quotas(force=force)


@router.post("/connectors/{connector_id}/toggle", summary="启用 / 停用某个数据源")
async def toggle_connector(connector_id: str, req: ToggleRequest):
    if not connector_registry.get(connector_id):
        raise HTTPException(status_code=404, detail="未知连接器")
    overrides = settings_store.get("connectors", "overrides", default={}) or {}
    overrides[connector_id] = {**(overrides.get(connector_id) or {}), "enabled": req.enabled}
    settings_store.set("connectors", "overrides", overrides)
    return {"id": connector_id, "enabled": req.enabled}


@router.get("/connectors/{connector_id}/health", summary="立即重新探测某个数据源")
async def connector_health(connector_id: str):
    return await connector_registry.health(connector_id, force=True)


@router.get("/servers", summary="已发现的 MCP 服务器（凭据打码）")
async def list_servers():
    return [{"id": sid, **_mask(spec)} for sid, spec in mcp_client_pool.registry.items()]


@router.post("/servers/refresh", summary="重新读取 ~/.claude.json、ZCode 与本地自定义配置")
async def refresh_servers():
    reg = mcp_client_pool.refresh_registry()
    return {"count": len(reg), "servers": list(reg)}


_MASK = "••••"


def _keep_secrets(new: Dict[str, Any], old: Dict[str, Any]) -> None:
    """把表单里仍是打码占位的 headers / env / url 参数还原成原值；没有原值的占位直接丢弃。"""
    for field in ("headers", "env"):
        vals = new.get(field) or {}
        prev = old.get(field) or {}
        new[field] = {k: (prev[k] if v in (_MASK, "") and k in prev else v)
                      for k, v in vals.items() if not (v in (_MASK, "") and k not in prev)}
    url = new.get("url") or ""
    if _MASK in url and old.get("url"):
        # 按参数名逐个还原：打码的 query 值换回旧 URL 里的真值
        def restore(m: "re.Match[str]") -> str:
            old_m = re.search(re.escape(m.group(1)) + r"([^&]+)", old["url"])
            return m.group(1) + (old_m.group(1) if old_m else m.group(2))
        new["url"] = re.sub(r"([?&](?:key|token|apikey|api_key|secret)=)(" + _MASK + ")", restore, url, flags=re.I)


@router.get("/servers/{server_id}", summary="取一条 MCP 的配置用于编辑（密钥打码）")
async def get_server(server_id: str):
    spec = mcp_client_pool.registry.get(server_id)
    if not spec:
        raise HTTPException(status_code=404, detail="未找到该 MCP")
    custom = next((c for c in (settings_store.get("connectors", "custom", default=[]) or []) if c.get("id") == server_id), {})
    return {"id": server_id, **_mask(spec), "name": custom.get("name") or spec.get("name") or server_id,
            "connector": custom.get("connector") or spec.get("connector") or "",
            "source": spec.get("source", "local")}


@router.post("/servers/custom", summary="添加 / 更新自定义 MCP（付费数据源等）")
async def upsert_custom(req: CustomConnectorRequest):
    if not _ID_RE.match(req.id):
        raise HTTPException(status_code=400, detail="id 只能包含字母、数字、下划线、短横线")
    if req.transport == "http" and not (req.url or "").startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="HTTP 连接器需要 http(s):// 地址")
    if req.transport == "stdio" and not req.command:
        raise HTTPException(status_code=400, detail="stdio 连接器需要启动命令")
    all_customs = settings_store.get("connectors", "custom", default=[]) or []
    customs = [c for c in all_customs if c.get("id") != req.id]
    new_spec = req.model_dump(exclude_none=True)
    # 编辑：表单里没动过的密钥显示为 ••••，保存时沿用原值；原值取本地自定义，没有就取 Claude / ZCode 里读到的
    old = next((c for c in all_customs if c.get("id") == req.id), None) or mcp_client_pool.registry.get(req.id) or {}
    _keep_secrets(new_spec, old)
    customs.append(new_spec)
    settings_store.set("connectors", "custom", customs)
    mcp_client_pool.refresh_registry()
    return {"status": "success", "id": req.id}


@router.delete("/servers/custom/{server_id}", summary="删除自定义 MCP")
async def delete_custom(server_id: str):
    customs = settings_store.get("connectors", "custom", default=[]) or []
    left = [c for c in customs if c.get("id") != server_id]
    if len(left) == len(customs):
        raise HTTPException(status_code=404, detail="未找到")
    settings_store.set("connectors", "custom", left)
    mcp_client_pool.refresh_registry()
    return {"deleted": server_id}


# ---- 旧前端兼容 ----

@router.get("/sources", summary="（兼容）数据源列表")
async def list_mcp_sources():
    items = await connector_registry.health_all()
    return [{"source_id": c["id"], "name": c["name"], "description": c["description"],
             "is_active": bool(c.get("connected")) and c.get("enabled", True),
             "status": "online" if c.get("connected") else "offline",
             "capabilities": c.get("capabilities", []), "error": c.get("error")} for c in items]


@router.post("/sources/refresh", summary="（兼容）刷新数据源")
async def refresh_mcp_sources():
    mcp_client_pool.refresh_registry()
    return await list_mcp_sources()
