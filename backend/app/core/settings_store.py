"""
用户设置持久化：config/local/settings.json（已在 .gitignore 中排除，权限 0600）。
保存：模型服务商 Key/地址、各角色使用的模型、数据源开关与自定义连接器。
"""
import copy
import threading
from typing import Any, Dict

from app.core.config import settings
from app.core.jsonio import read_json, write_json

SETTINGS_FILE = settings.LOCAL_CONFIG_DIR / "settings.json"

DEFAULTS: Dict[str, Any] = {
    "llm": {
        # provider_id -> {api_key, base_url}
        "providers": {},
        # 角色 -> {provider, model}；fast=意图识别/翻译，strong=综合研判与报告，agent=带工具执行
        "roles": {
            "strong": {"provider": "magpie", "model": "group/never-stuck"},
            "fast": {"provider": "magpie", "model": "group/never-stuck"},
            "agent": {"provider": "magpie", "model": "group/never-stuck"},
        },
    },
    "connectors": {
        # connector_id -> {"enabled": bool}
        "overrides": {},
        # 用户手动添加的连接器：[{id, name, transport, url, headers, command, args, env}]
        "custom": [],
    },
}


class SettingsStore:
    def __init__(self):
        self._lock = threading.RLock()
        self._data = self._load()

    def _load(self) -> Dict[str, Any]:
        data = read_json(SETTINGS_FILE, default={}) or {}
        merged = copy.deepcopy(DEFAULTS)
        _deep_merge(merged, data)
        return merged

    def get(self, *path: str, default: Any = None) -> Any:
        with self._lock:
            node: Any = self._data
            for key in path:
                if not isinstance(node, dict) or key not in node:
                    return default
                node = node[key]
            return copy.deepcopy(node)

    def set(self, *path_and_value: Any) -> None:
        *path, value = path_and_value
        with self._lock:
            node = self._data
            for key in path[:-1]:
                node = node.setdefault(key, {})
            node[path[-1]] = value
            write_json(SETTINGS_FILE, self._data, indent=2, private=True)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._data)


def _deep_merge(base: Dict[str, Any], extra: Dict[str, Any]) -> None:
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


settings_store = SettingsStore()
