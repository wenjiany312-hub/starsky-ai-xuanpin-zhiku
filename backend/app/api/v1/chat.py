from fastapi import APIRouter, HTTPException

from app.repository.chat_store import chat_session_store
from app.schema.selection import ChatSessionSummary, ChatSessionDetail

router = APIRouter(prefix="/chat", tags=["选品对话会话"])


@router.get("/sessions", response_model=list[ChatSessionSummary], summary="列出历史对话会话")
async def list_chat_sessions(limit: int = 50):
    return chat_session_store.list_sessions(limit=limit)


@router.get("/sessions/{session_id}", response_model=ChatSessionDetail, summary="获取指定会话完整消息")
async def get_chat_session(session_id: str):
    session = chat_session_store.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    return session


@router.delete("/sessions/{session_id}", summary="删除指定会话")
async def delete_chat_session(session_id: str):
    ok = chat_session_store.delete_session(session_id)
    if not ok:
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"status": "deleted", "id": session_id}
