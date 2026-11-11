from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.service.ali1688_archiver_service import ali1688_archiver_service

router = APIRouter(prefix="/supply", tags=["1688 供应链归档"])


class ArchiveRequest(BaseModel):
    url: str  # 1688 商品详情页链接


@router.post("/archive", summary="整品归档：抓取 1688 商品页，下载主图/详情长图/视频，生成中文报告 MD")
async def archive_product(req: ArchiveRequest):
    result = await ali1688_archiver_service.archive_product(req.url)
    return result


@router.post("/login", summary="打开浏览器到 1688 登录页（人工登录，登录态持久保存）")
async def start_login():
    return await ali1688_archiver_service.start_login()


@router.get("/login-state", summary="检测当前 1688 登录态与风控状态")
async def login_state():
    return await ali1688_archiver_service.login_state()


@router.get("/archives", summary="列出已归档的 1688 商品档案")
async def list_archives():
    return ali1688_archiver_service.list_archives()


@router.get("/archives/{folder}/report", summary="读取指定归档的中文商品报告 MD 全文")
async def get_archive_report(folder: str):
    content = ali1688_archiver_service.get_report(folder)
    if content is None:
        raise HTTPException(status_code=404, detail="归档不存在或报告未生成")
    return {"folder": folder, "content": content}
