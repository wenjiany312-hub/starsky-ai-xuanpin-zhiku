import json
import threading
import uuid
import datetime
from typing import Any, Dict, List, Optional

from app.core.config import settings
from app.core.jsonio import read_json, write_json
from app.core.logger import logger


class ChatSessionStore:
    """
    对话会话本地 JSON 持久化（data_storage/chat_sessions.json，进程内锁保护）
    结构: {session_id: {id, title, created_at, updated_at, messages: [...]}}
    """

    def __init__(self):
        self._file = settings.STORAGE_DIR / "chat_sessions.json"
        self._lock = threading.RLock()
        settings.STORAGE_DIR.mkdir(parents=True, exist_ok=True)
        self._sessions: Dict[str, Dict[str, Any]] = self._load()

    def _load(self) -> Dict[str, Dict[str, Any]]:
        # 坏文件由 read_json 改名留档并回退到 .bak，不会被下一次写入悄悄覆盖
        data = read_json(self._file, default={})
        if not isinstance(data, dict):
            logger.warning("对话会话文件格式不对，已从空白开始（原文件未删除）")
            return {}
        return data

    def _flush(self) -> None:
        with self._lock:
            write_json(self._file, self._sessions)

    def list_sessions(self, limit: int = 50) -> List[Dict[str, Any]]:
        items = sorted(
            self._sessions.values(),
            key=lambda s: s.get("updated_at", ""),
            reverse=True,
        )[:limit]
        return [
            {
                "id": s["id"],
                "title": s.get("title") or "新对话",
                "updated_at": s.get("updated_at"),
                "message_count": len(s.get("messages", [])),
            }
            for s in items
        ]

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        return self._sessions.get(session_id)

    def append_messages(self, session_id: Optional[str], messages: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        追加消息；session_id 为空时自动创建新会话（标题取首条用户消息）。
        """
        with self._lock:
            return self._append(session_id, messages)

    def _append(self, session_id: Optional[str], messages: List[Dict[str, Any]]) -> Dict[str, Any]:
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        session = self._sessions.get(session_id or "")
        if session is None:
            session = {
                "id": str(uuid.uuid4())[:8],
                "title": "",
                "created_at": now,
                "updated_at": now,
                "messages": [],
            }
            self._sessions[session["id"]] = session

        session["messages"].extend(messages)
        # 标题：取首条用户消息
        if not session.get("title"):
            for m in messages:
                if m.get("role") == "user":
                    session["title"] = (m.get("content") or "")[:30]
                    break
        # 防无限膨胀：单会话最多保留 200 条
        if len(session["messages"]) > 200:
            session["messages"] = session["messages"][-200:]
        session["updated_at"] = now
        self._flush()
        return session

    def delete_session(self, session_id: str) -> bool:
        with self._lock:
            if session_id in self._sessions:
                del self._sessions[session_id]
                self._flush()
                return True
            return False


chat_session_store = ChatSessionStore()
