from app.api.v1 import category_packs
from app.api.v1 import chat
from app.api.v1 import knowledge
from app.api.v1 import license_api
from app.api.v1 import method_packs
from app.api.v1 import mcp
from app.api.v1 import models
from app.api.v1 import supply
from app.api.v1 import system
from app.api.v1 import voc
from app.api.v1 import workflows
from fastapi import APIRouter

api_router = APIRouter()

api_router.include_router(workflows.router)
api_router.include_router(category_packs.router)
api_router.include_router(knowledge.router)
api_router.include_router(method_packs.router)
api_router.include_router(models.router)
api_router.include_router(mcp.router)
api_router.include_router(supply.router)
api_router.include_router(chat.router)
api_router.include_router(system.router)
api_router.include_router(voc.router)
api_router.include_router(license_api.router)
