"""社媒 VOC 接口：品类配置 / 品类扫描 / 单词探针。取数全走 Arctic Shift 免费存档。"""
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.service import voc_service

router = APIRouter(prefix="/voc", tags=["社媒VOC"])


class ScanRequest(BaseModel):
    category_id: str = Field(..., description="config/voc_categories/ 里的品类配置 id")
    months: Optional[int] = Field(None, ge=1, le=12, description="回看月数，默认取配置")
    subreddits: Optional[List[str]] = None
    annotate_top: int = Field(5, ge=0, le=12, description="对前 N 个品类做 AI 深读（花 token）")


class ProbeRequest(BaseModel):
    keyword: str = Field(..., min_length=2, description="英文关键词")
    subreddits: Optional[List[str]] = None
    annotate: bool = False


@router.get("/categories", summary="可用的 VOC 品类配置")
async def categories():
    return [{"id": c["id"], "name": c.get("name"), "description": c.get("description", ""),
             "subreddits": c["subreddits"], "products": len(c.get("products") or {}),
             "months": c.get("months", 6), "scannable": bool(c.get("products"))}
            for c in voc_service.load_categories()]


@router.post("/scans", summary="发起品类扫描（后台执行，立即返回）")
async def start_scan(req: ScanRequest):
    try:
        return voc_service.start_scan(req.category_id, req.months, req.subreddits, req.annotate_top)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.get("/scans", summary="扫描记录")
async def scans():
    return voc_service.list_scans()


@router.get("/scans/{scan_id}", summary="扫描进度与结果")
async def scan_detail(scan_id: str):
    st = voc_service.get_scan(scan_id)
    if not st:
        raise HTTPException(404, "没有这次扫描")
    return st


@router.get("/scans/{scan_id}/report", summary="扫描报告（Markdown）")
async def scan_report(scan_id: str):
    st = voc_service.get_scan(scan_id)
    f = voc_service._scan_dir(scan_id) / "report.md" if st else None
    if not f or not f.exists():
        raise HTTPException(404, "报告还没生成")
    return {"scan_id": scan_id, "markdown": f.read_text(encoding="utf-8")}


@router.post("/probe", summary="单个关键词的社媒前导信号（不走选品流程，调试与快速看一眼）")
async def probe(req: ProbeRequest):
    return await voc_service.probe(req.keyword, req.subreddits, do_annotate=req.annotate)
