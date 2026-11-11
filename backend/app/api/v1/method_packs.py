"""方法论库接口：列表 / 详情 / 导入（zip 或本机文件夹）/ 删除 / 同一份数据多方法论对比。"""
from typing import List, Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from app.core import rules as rules_mod
from app.core import license as license_service
from app.service import method_pack_service as mps
from app.service.method_pack_service import DEFAULT_PACK_ID, PackError, method_pack_service
from app.service.selection_engine import selection_engine

router = APIRouter(prefix="/method-packs", tags=["方法论库"])


class FolderImportRequest(BaseModel):
    path: str = Field(..., description="本机文件夹绝对路径（含 pack.yaml 或 SKILL.md）")
    overwrite: bool = False


class CompareRequest(BaseModel):
    task_id: str
    pack_ids: List[str] = Field(..., min_length=1, max_length=8)


class GenerateRequest(BaseModel):
    text: str = Field(..., description="方法论原文（笔记 / 课程稿 / 帖文 / SKILL.md 全文）")
    name: str = ""
    author: str = ""


class InstallDraftRequest(BaseModel):
    draft: dict = Field(..., description="generate 返回的 draft，可先在前端改过再提交")
    source_text: str = ""
    overwrite: bool = False


def _public(pack: dict) -> dict:
    return {k: v for k, v in pack.items() if k != "path"}


@router.get("", summary="方法论包列表")
async def list_packs():
    return {"default": DEFAULT_PACK_ID, "packs": [_public(p) for p in method_pack_service.list()]}


@router.get("/rules", summary="规则目录（供方法论编辑与说明）")
async def rule_catalog():
    t = rules_mod.thresholds()
    return [{"id": rd["id"], "name": rd["name"], "axis": rd["axis"], "unit": rd.get("unit", ""),
             "note": rd.get("note", ""), "hard": bool(rd.get("hard"))} for rd in rules_mod._rule_defs(t)]


@router.get("/{pack_id}", summary="方法论包详情（含生效阈值与 SKILL.md 预览）")
async def get_pack(pack_id: str):
    pack = method_pack_service.get(pack_id)
    if not pack:
        raise HTTPException(status_code=404, detail="未找到该方法论包")
    resolved = method_pack_service.resolve(pack["id"], {})
    skill = method_pack_service.skill_text(pack, max_chars=4000) if pack.get("has_skill") else ""
    return {**_public(pack), "effective_weights": resolved["weights"], "effective_rules": resolved["rule_ids"],
            "effective_thresholds": {k: v for k, v in resolved["thresholds"].items() if not k.startswith("_")},
            "skill_preview": skill}


@router.post("/import/zip", summary="导入方法论包（zip）")
async def import_zip(file: UploadFile = File(...), overwrite: bool = Form(False)):
    data = await file.read()
    try:
        return _public(method_pack_service.import_zip(data, overwrite=overwrite))
    except PackError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/import/folder", summary="从本机文件夹导入方法论包")
async def import_folder(req: FolderImportRequest):
    try:
        return _public(method_pack_service.import_folder(req.path, overwrite=req.overwrite))
    except PackError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.delete("/{pack_id}", summary="删除用户导入的方法论包（内置包不可删）")
async def delete_pack(pack_id: str):
    if not method_pack_service.delete(pack_id):
        raise HTTPException(status_code=404, detail="只能删除自己导入的方法论包")
    return {"deleted": pack_id}


@router.post("/generate", summary="把方法论原文编译成方法包草稿（不落盘，先核对）")
async def generate(req: GenerateRequest):
    license_service.require_active("用模型编译方法论")
    try:
        return await mps.generate_from_text(req.text, name=req.name, author=req.author)
    except PackError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/install-draft", summary="把核对过的草稿装成可切换的方法包")
async def install_draft(req: InstallDraftRequest):
    try:
        return _public(mps.install_draft(req.draft, req.source_text, req.overwrite))
    except PackError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/compare", summary="用同一次运行的数据，换不同方法论重算结论（不额外消耗数据额度）")
async def compare(req: CompareRequest):
    results = []
    for pid in req.pack_ids:
        try:
            results.append(selection_engine.rescore(req.task_id, pid))
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    return {"task_id": req.task_id, "results": results}
