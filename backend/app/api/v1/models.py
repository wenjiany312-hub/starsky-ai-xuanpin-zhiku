"""模型网关接口：服务商配置（Key 只回显掩码）、角色→模型、实时模型列表、连通性测试。"""
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.core.llm_gateway import LLMError, ROLE_LABELS, ROLES, llm_gateway

router = APIRouter(prefix="/models", tags=["模型网关"])


class UpdateProviderRequest(BaseModel):
    provider_id: str
    api_key: Optional[str] = Field(None, description="留空=不改；传空字符串=清除")
    base_url: Optional[str] = None
    # 旧前端兼容：传了 model_name 时顺带把 strong 角色切过去
    model_name: Optional[str] = None


class SetRoleRequest(BaseModel):
    role: str
    provider_id: str
    model: str


class TestRequest(BaseModel):
    provider_id: str
    model_name: Optional[str] = None


@router.get("", summary="服务商列表 + 各角色当前模型")
async def list_model_providers():
    roles = {r: {**llm_gateway.role_model(r), "label": ROLE_LABELS[r]} for r in ROLES}
    strong = roles["strong"]
    return {
        "providers": llm_gateway.providers_summary(),
        "roles": roles,
        # 旧字段
        "current_default_provider": strong["provider"],
        "current_default_model": strong["model"],
    }


@router.get("/{provider_id}/models", summary="读取服务商的实时模型列表（MagPie 显示可读名）")
async def list_provider_models(provider_id: str, refresh: bool = Query(False)):
    try:
        return {"provider": provider_id, "models": await llm_gateway.list_models(provider_id, refresh=refresh)}
    except LLMError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/config", summary="保存服务商 Key / 地址（写入本机私有配置）")
async def update_model_config(req: UpdateProviderRequest):
    try:
        llm_gateway.update_provider(req.provider_id, api_key=req.api_key, base_url=req.base_url)
        if req.model_name:
            llm_gateway.set_role_model("strong", req.provider_id, req.model_name)
    except LLMError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"status": "success", "message": f"{req.provider_id} 配置已保存"}


@router.post("/roles", summary="设置某个角色使用的模型")
async def set_role(req: SetRoleRequest):
    try:
        llm_gateway.set_role_model(req.role, req.provider_id, req.model)
    except LLMError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"status": "success", "role": req.role, **llm_gateway.role_model(req.role)}


@router.post("/test", summary="测试模型连通性与延迟")
async def test_model_connection(req: TestRequest):
    return await llm_gateway.test_connection(req.provider_id, req.model_name)
