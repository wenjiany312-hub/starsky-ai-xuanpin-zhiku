import os
import json
import re
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from app.core.config import settings
from app.core.logger import logger
from app.api.router import api_router

# 孤立的 UTF-16 代理半区（非法 UTF-8，常见于前端按码元截断 emoji 产生的损坏字符串）
_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")


def _clean_surrogates(value):
    """递归清除 JSON 结构中所有字符串里的孤立代理字符"""
    if isinstance(value, str):
        return _SURROGATE_RE.sub("", value)
    if isinstance(value, list):
        return [_clean_surrogates(v) for v in value]
    if isinstance(value, dict):
        return {k: _clean_surrogates(v) for k, v in value.items()}
    return value


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="整合选品方法论、多源数据 MCP (1688/Sorftime/SIF/西柚)、异构大模型网关与垂直细分赛道包的商业化选品终端",
    docs_url="/docs",
    redoc_url="/redoc"
)


@app.middleware("http")
async def sanitize_body_surrogates(request: Request, call_next):
    """
    请求体净化中间件：清除 JSON 请求体中的孤立代理字符，
    避免 Pydantic 校验失败后回显非法字符串导致 500。
    """
    if request.method in ("POST", "PUT", "PATCH"):
        body = await request.body()
        if body and "json" in request.headers.get("content-type", ""):
            try:
                cleaned = _clean_surrogates(json.loads(body))
                new_body = json.dumps(cleaned, ensure_ascii=False).encode("utf-8")
                request._body = new_body
                request.headers.__dict__["_list"] = [
                    (b"content-length", str(len(new_body)).encode("ascii"))
                    if k.lower() == b"content-length" else (k, v)
                    for k, v in request.headers.raw
                ]
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass  # 非 JSON 或损坏更严重时交由后续正常流程报错
    return await call_next(request)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """兜底：校验错误回显内容做代理字符清洗，保证错误响应可编码"""
    return JSONResponse(
        status_code=422,
        content={"detail": json.loads(json.dumps(
            [{"loc": e.get("loc"), "msg": e.get("msg"), "type": e.get("type"),
              "input": str(e.get("input", ""))[:200]}
             for e in exc.errors()],
            ensure_ascii=True,
        ))},
    )

# 跨域设置
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 挂载 API 路由
app.include_router(api_router, prefix=settings.API_V1_PREFIX)


@app.on_event("startup")
async def _recover_runs():
    from app.service.selection_engine import selection_engine
    n = selection_engine.recover_orphans()
    if n:
        logger.info("已将 %s 个上次未完成的选品运行标记为失败", n)


@app.on_event("shutdown")
async def _close_mcp():
    # 本机数据源（如 1688 抓取）是子进程，后端退出时一并关掉，避免残留浏览器占住登录目录
    from app.core.mcp_client import mcp_client_pool
    try:
        await mcp_client_pool.close_all()
    except Exception as exc:  # noqa: BLE001
        logger.warning("关闭数据源子进程时出错: %s", exc)

@app.get("/health", summary="系统健康探针")
async def health_check():
    return {
        "status": "healthy",
        "app_name": settings.APP_NAME,
        "version": settings.APP_VERSION
    }

@app.get("/dashboard", include_in_schema=False)
async def serve_dashboard():
    dashboard_path = settings.BASE_DIR / "多智能体协同作战指挥台.html"
    if dashboard_path.exists():
        return FileResponse(dashboard_path)
    return FileResponse(settings.BASE_DIR / "frontend" / "dist" / "index.html")

# 前端 SPA 静态资源挂载
frontend_dist = settings.BASE_DIR / "frontend" / "dist"
if frontend_dist.exists():
    app.mount("/assets", StaticFiles(directory=str(frontend_dist / "assets")), name="assets")

@app.get("/{full_path:path}", include_in_schema=False)
async def serve_spa(full_path: str):
    if full_path == "dashboard":
        return await serve_dashboard()
    # 排除 API 与系统路径
    if full_path.startswith("api/") or full_path.startswith("docs") or full_path.startswith("openapi.json"):
        return JSONResponse(status_code=404, content={"detail": "Not Found"})
    file_path = (frontend_dist / full_path).resolve()
    if file_path.is_file() and frontend_dist.resolve() in file_path.parents:
        return FileResponse(file_path)
    # index.html 不缓存，保证前端重新构建后刷新即生效（assets 文件名带 hash 可长缓存）
    return FileResponse(frontend_dist / "index.html", headers={"Cache-Control": "no-cache"})

if __name__ == "__main__":
    import uvicorn
    logger.info("正在启动 %s 服务，监听端口: %s...", settings.APP_NAME, settings.SERVER_PORT)
    uvicorn.run("main:app", host=settings.SERVER_HOST, port=settings.SERVER_PORT, reload=False)
