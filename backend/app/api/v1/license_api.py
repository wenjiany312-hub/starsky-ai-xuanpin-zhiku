"""授权码接口：查状态、激活、解除绑定。到期后这里仍然可用，只是拦截跑数入口。"""
from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.core import license as license_service

router = APIRouter(prefix="/license", tags=["授权"])


class ActivateRequest(BaseModel):
    key: str = Field(..., min_length=8, description="发码人给的一整段授权码")


@router.get("/status", summary="授权状态（授权 / 试用剩余 / 到期只读）")
async def license_status():
    return license_service.status()


@router.post("/activate", summary="输入授权码并绑定本机")
async def activate(req: ActivateRequest):
    return license_service.activate(req.key)


@router.post("/deactivate", summary="移除本机授权码（换码时用）")
async def deactivate():
    return license_service.deactivate()


@router.get("/device", summary="本机设备码（发给发码人换取绑定码）")
async def device():
    return {"device": license_service.device_code(), "trial": license_service.trial_meta()}
