import re
import shutil
from pathlib import Path
from typing import Any, Dict, List
from pydantic import BaseModel, Field

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse

from app.core import license as license_service
from app.core import report_html
from app.core.config import settings
from app.core.logger import logger
from app.repository.storage import task_repository
from app.schema.selection import (
    ChatSelectionRequest,
    ChatSelectionResponse,
    CreateSelectionTaskRequest,
    SelectionTaskItemResponse,
    SelectionTaskResponse,
    StarskyExportRequest,
    StarskyExportResponse,
)
from app.service.selection_engine import selection_engine
from app.service.selection_workflow_service import selection_workflow_service
from app.service.starsky_export_service import starsky_export_service

router = APIRouter(prefix="/workflows", tags=["选品工作流"])

_RUN_FILES = {"report.html", "report.md", "evidence.md", "evidence.jsonl",
              "facts.json", "verdict.json", "run.json"}


def _task_or_404(task_id: str):
    task = task_repository.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="未找到该选品任务")
    return task


@router.post("/chat", response_model=ChatSelectionResponse, summary="AI 选品助手对话")
async def chat_selection(req: ChatSelectionRequest):
    license_service.require_active("使用 AI 选品助手")
    return await selection_workflow_service.chat_selection(
        message=req.message, history=req.history, session_id=req.session_id,
        category_pack_id=req.category_pack_id, llm_provider=req.llm_provider, llm_model=req.llm_model)


@router.post("/execute", response_model=SelectionTaskResponse, summary="发起选品运行（默认后台执行，立即返回）")
async def execute_selection_workflow(req: CreateSelectionTaskRequest,
                                     wait: bool = Query(False, description="true=等运行结束再返回")):
    license_service.require_active("发起选品运行")
    task = await selection_workflow_service.execute_task(req, wait=wait)
    license_service.note_run()
    return SelectionTaskResponse(**task.model_dump())


@router.get("/tasks", response_model=List[SelectionTaskItemResponse], summary="选品运行列表")
async def list_tasks(limit: int = Query(50, ge=1, le=500)):
    items = []
    for t in task_repository.list_tasks(limit=limit):
        item = SelectionTaskItemResponse(**t.model_dump())
        # 老记录可能只有一条结论、没有落盘报告；前端据此决定给不给下载按钮
        item.has_report = (settings.RUNS_DIR / t.task_id / "report.md").is_file()
        items.append(item)
    return items


@router.get("/tasks/{task_id}", response_model=SelectionTaskResponse, summary="运行详情（含逐步进度）")
async def get_task_detail(task_id: str):
    return SelectionTaskResponse(**_task_or_404(task_id).model_dump())


class FinalizeDecisionRequest(BaseModel):
    parameters: Dict[str, Any] = Field(default_factory=dict, description="补充的人工核验参数")


@router.post("/tasks/{task_id}/finalize-decision", summary="补充人工核验项并终审定档（基于已有事实，免消耗取数额度）")
async def finalize_decision(task_id: str, req: FinalizeDecisionRequest):
    license_service.require_active("终审定档")
    _task_or_404(task_id)
    try:
        res = selection_engine.finalize_decision(task_id, req.parameters)
        task = _task_or_404(task_id)
        return SelectionTaskResponse(**task.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.exception("终审定档失败：%s", e)
        raise HTTPException(status_code=500, detail=f"终审定档失败: {e}")


@router.post("/tasks/{task_id}/rerun", response_model=SelectionTaskResponse, summary="用相同参数重新运行")
async def rerun_task(task_id: str):
    license_service.require_active("重新运行选品")
    old = _task_or_404(task_id)
    req = CreateSelectionTaskRequest(
        workflow_type=old.workflow_type, title=old.title, category_pack_id=old.category_pack_id,
        target_market=old.target_market, parameters=old.parameters, llm_provider=old.llm_provider,
        llm_model=old.llm_model, method_pack_id=old.method_pack_id)
    task = await selection_workflow_service.execute_task(req)
    license_service.note_run()
    return SelectionTaskResponse(**task.model_dump())


def _remove_run_dir(task_id: str) -> None:
    """删记录就连运行目录一起删；只认 RUNS_DIR 下面那一层，防止路径穿越删到别处。"""
    root = settings.RUNS_DIR.resolve()
    target = (root / task_id).resolve()
    if target == root or root not in target.parents:
        return
    shutil.rmtree(target, ignore_errors=True)


def _safe_title(task) -> str:
    safe = re.sub(r'[\\/:*?"<>|\s]+', "-", task.title or "").strip("-")[:60]
    return safe or task.task_id


def _download_name(task, name: str) -> str:
    """下载时给出人看得懂的文件名，而不是一串任务编号。"""
    if name.startswith("report."):
        return f"{_safe_title(task)}-选品报告{Path(name).suffix}"
    return f"{task.task_id}_{name}"


def _ensure_report_html(task_id: str, report_md: Path) -> Path:
    """老记录目录里只有 report.md；按需渲染一份 HTML 落到同目录，下次直接复用。"""
    out = report_md.parent / "report.html"
    # 渲染器升级后，旧缓存也要重出（否则老报告永远是旧版式）
    renderer_mtime = Path(report_html.__file__).stat().st_mtime
    if out.is_file() and out.stat().st_mtime >= max(report_md.stat().st_mtime, renderer_mtime):
        return out
    out.write_text(report_html.render(report_md.read_text(encoding="utf-8")), encoding="utf-8")
    return out


@router.delete("/tasks/{task_id}", summary="删除运行记录（含运行目录里的报告与证据）")
async def delete_task(task_id: str):
    if selection_engine.is_running(task_id):
        raise HTTPException(status_code=409, detail="任务正在运行，结束后再删除")
    if not task_repository.delete_task(task_id):
        raise HTTPException(status_code=404, detail="未找到该选品任务")
    _remove_run_dir(task_id)
    return {"deleted": task_id}


@router.get("/tasks/{task_id}/report", summary="运行报告（Markdown）")
async def get_task_report(task_id: str):
    task = _task_or_404(task_id)
    if not task.report_file_path:
        raise HTTPException(status_code=404, detail="报告尚未生成")
    report_file = Path(task.report_file_path)
    if not report_file.exists():
        raise HTTPException(status_code=404, detail="报告文件丢失")
    return {"task_id": task_id, "content": report_file.read_text(encoding="utf-8")}


@router.get("/tasks/{task_id}/files/{name}", summary="下载运行目录中的产物（报告/证据/事实/结论）")
async def get_run_file(task_id: str, name: str):
    task = _task_or_404(task_id)
    if name not in _RUN_FILES:
        raise HTTPException(status_code=400, detail="不支持的文件")
    base = (settings.RUNS_DIR / task_id).resolve()
    path = (base / name).resolve()
    if base not in path.parents:
        raise HTTPException(status_code=404, detail="文件不存在")
    if name == "report.html" and not path.is_file():
        md = base / "report.md"
        if not md.is_file():
            raise HTTPException(status_code=404, detail="报告尚未生成")
        path = _ensure_report_html(task_id, md)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    if name.endswith(".json"):
        media = "application/json"
    elif name.endswith(".html"):
        media = "text/html; charset=utf-8"
    else:
        media = "text/markdown; charset=utf-8"
    return FileResponse(path, media_type=media, filename=_download_name(task, name),
                        headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


@router.get("/tasks/{task_id}/report.html", response_class=HTMLResponse,
            summary="在浏览器里直接看报告（不是下载）")
async def view_report_html(task_id: str):
    task = _task_or_404(task_id)
    base = (settings.RUNS_DIR / task_id).resolve()
    md = base / "report.md"
    if not md.is_file():
        raise HTTPException(status_code=404, detail="报告尚未生成")
    html_file = _ensure_report_html(task_id, md)
    return HTMLResponse(html_file.read_text(encoding="utf-8"),
                        headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


# ---------------- 导出星空 S1 产物 ----------------


@router.get("/starsky/config", summary="星空 S1 导出目录与插件探测")
async def starsky_config():
    return starsky_export_service.config()


@router.post("/tasks/{task_id}/export-starsky", response_model=StarskyExportResponse,
             summary="把运行结果导出成星空 S1 标准产物目录")
async def export_starsky(task_id: str, req: StarskyExportRequest):
    task = _task_or_404(task_id)
    try:
        result = starsky_export_service.export(task, **req.model_dump())
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return StarskyExportResponse(**result)


# ---------------- 自有数据上传 ----------------

from fastapi import File, UploadFile  # noqa: E402

from app.service.upload_service import upload_service  # noqa: E402


@router.post("/uploads", summary="上传自有数据（CSV/XLSX 竞品表或关键词表），运行时通过 parameters.upload_ids 引用")
async def upload_data(file: UploadFile = File(...)):
    # 先按上限读：一次性 read() 会把超大文件整个吃进内存，之后才报「超过 15MB」
    limit = 15 * 1024 * 1024
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(status_code=400, detail="文件超过 15MB，请拆分后再传")
    try:
        return upload_service.save(file.filename or "upload.csv", data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # 改了扩展名的 .xls、损坏的 xlsx 会抛 BadZipFile 等，不该是 500
        raise HTTPException(status_code=400,
                            detail=f"文件读不出来（{type(exc).__name__}）：请另存为 .xlsx 或 .csv 后重传")


@router.get("/uploads", summary="已上传的数据文件")
async def list_uploads():
    return upload_service.list()


# ---------------- 图片找货：上传 → 识别 → 关键词 ----------------

from pydantic import BaseModel  # noqa: E402

from app.core import vision  # noqa: E402
from app.core.llm_gateway import LLMError, LLMUnavailable  # noqa: E402
from app.service import image_service as image_service_mod  # noqa: E402
from app.service.image_service import image_service  # noqa: E402


class ImageAnalyzeRequest(BaseModel):
    title_hint: str = ""
    provider_id: str | None = None
    model: str | None = None


class ImageUrlRequest(BaseModel):
    image_url: str
    title_hint: str = ""
    provider_id: str | None = None
    model: str | None = None


async def _run_vision(image_id: str, req: ImageAnalyzeRequest) -> dict:
    got = image_service.load(image_id)
    if not got:
        raise HTTPException(status_code=404, detail="图片不存在或已被清理，请重新上传")
    data, meta = got
    try:
        result = await vision.analyze_image(
            data, meta.get("mime") or "image/jpeg",
            title_hint=req.title_hint, provider_id=req.provider_id, model=req.model,
        )
    except LLMUnavailable as exc:
        raise HTTPException(status_code=503, detail=f"识图需要视觉模型，当前不可用：{exc}")
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=f"识图失败：{exc}")
    except Exception as exc:  # 兜底：别让前端只看到 Internal Server Error，原因留在服务端日志
        logger.exception("识图异常")
        raise HTTPException(status_code=502, detail=f"识图失败：{type(exc).__name__}: {exc}")
    return {**meta, "image_url": meta.get("url"), "public_url": meta.get("public_url", ""), **result}


@router.post("/uploads/image", summary="上传一张产品图（≤8MB），返回 IMG-xxx 供识别与以图找货使用")
async def upload_image(file: UploadFile = File(...)):
    data = await file.read(image_service_mod.MAX_BYTES + 1)
    if len(data) > image_service_mod.MAX_BYTES:
        raise HTTPException(status_code=400, detail="图片超过 8MB，请压缩后再传")
    try:
        return image_service.save(file.filename or "image.jpg", data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/images", summary="已上传的图片（最近 200 张）")
async def list_images():
    return image_service.list()


@router.get("/images/{image_id}", summary="取回已上传图片的原图")
async def get_image(image_id: str):
    got = image_service.load(image_id)
    if not got:
        raise HTTPException(status_code=404, detail="图片不存在")
    _, meta = got
    path = image_service.resolve(meta)
    if not path:
        raise HTTPException(status_code=404, detail="图片文件已丢失")
    return FileResponse(path, media_type=meta.get("mime") or "image/jpeg")


@router.post("/images/analyze-url", summary="用公网图片 URL 识图（顺带存下来，供 1688 以图搜同款使用）")
async def analyze_image_url(req: ImageUrlRequest):
    url = (req.image_url or "").strip()
    try:
        data, name = await image_service_mod.fetch_remote(url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        meta = image_service.save(name, data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"这不是可识别的图片：{exc}") from exc
    image_service.remember_public_url(meta["id"], url)
    return await _run_vision(meta["id"], ImageAnalyzeRequest(
        title_hint=req.title_hint, provider_id=req.provider_id, model=req.model))


@router.post("/images/{image_id}/analyze", summary="识别图片里的产品，出中英关键词与风险提示")
async def analyze_image(image_id: str, req: ImageAnalyzeRequest = ImageAnalyzeRequest()):
    return await _run_vision(image_id, req)
