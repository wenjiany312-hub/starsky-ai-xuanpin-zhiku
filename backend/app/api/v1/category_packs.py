from typing import List
from fastapi import APIRouter, HTTPException
from app.service.category_pack_service import category_pack_service
from app.model.domain import CategoryPackMetadata
from app.schema.knowledge import CategoryPackDetailResponse

router = APIRouter(prefix="/category-packs", tags=["垂直赛道包"])

@router.get("", response_model=List[CategoryPackMetadata], summary="获取所有已挂载的垂直赛道包列表")
async def list_category_packs():
    return category_pack_service.list_packs()

@router.get("/{pack_id}", response_model=CategoryPackDetailResponse, summary="获取指定垂直赛道包的规则与资料详情")
async def get_category_pack_detail(pack_id: str):
    detail = category_pack_service.get_pack_detail(pack_id)
    if not detail:
        raise HTTPException(status_code=404, detail="未找到该赛道包")
    return detail
