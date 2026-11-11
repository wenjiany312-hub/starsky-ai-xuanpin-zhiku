from typing import Optional, List
from fastapi import APIRouter, Query, HTTPException
from app.service.knowledge_service import knowledge_service
from app.schema.knowledge import (
    KnowledgeStatusResponse,
    KnowledgeCardSummary,
    KnowledgeCardDetail,
    PlaybookItem
)

router = APIRouter(prefix="/knowledge", tags=["选品知识库"])

@router.get("/status", response_model=KnowledgeStatusResponse, summary="获取选品知识库整体健康与覆盖状态")
async def get_knowledge_status():
    return knowledge_service.get_status()

@router.get("/cards", response_model=List[KnowledgeCardSummary], summary="检索适用的选品知识卡片列表")
async def search_cards(
    phase: Optional[str] = Query(None, description="选品阶段"),
    mode: Optional[str] = Query(None, description="选品模式"),
    keyword: Optional[str] = Query(None, description="搜索关键词"),
    # 卡片列表是本地缓存的全量过滤（当前 185 张），不放宽上限前端就拿不到整库。
    # 上限与前端 KnowledgeHub 的请求量保持一致，避免口径打架直接 422。
    limit: int = Query(50, ge=1, le=1000, description="返回数量限制")
):
    return knowledge_service.list_cards(mode=mode, phase=phase, keyword=keyword, limit=limit)

@router.get("/cards/{card_id}", response_model=KnowledgeCardDetail, summary="获取指定知识卡片完整详情")
async def get_card_detail(card_id: str):
    detail = knowledge_service.get_card_detail(card_id)
    if not detail:
        raise HTTPException(status_code=404, detail="未找到该知识卡片")
    return detail

@router.get("/playbooks", response_model=List[PlaybookItem], summary="获取所有 Playbook 实操手册列表")
async def list_playbooks():
    return knowledge_service.list_playbooks()

@router.get("/playbooks/{name}", summary="获取指定选品实操手册全文")
async def get_playbook(name: str):
    content = knowledge_service.get_playbook(name)
    if not content:
        raise HTTPException(status_code=404, detail="未找到该选品手册")
    return {"playbook": name, "content": content}
