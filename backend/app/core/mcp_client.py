"""
MCP 客户端与注册表发现层。

支持两种传输：
- Streamable HTTP（SIF / Sorftime / 西柚 / 卖家精灵等云端数据源）
- stdio（本地已封装的 1688_scraper 等子进程服务）

注册表按优先级合并三个来源，同名时后者覆盖前者：
1. ~/.claude.json 的 mcpServers（Claude Code 里配置的服务器，含 stdio）
2. ~/.zcode/cli/config.json 的 mcp.servers（ZCode/星空客户端）
3. 本项目 config/local/settings.json 的 connectors.custom（界面里手动添加）

凭据不落源码：只读取上述客户端配置，或在界面里写入本地私有配置。
"""
import asyncio
import atexit
import json
import os
import re
import signal
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from app.core.config import settings
from app.core.logger import logger
from app.core.mcp_stdio import MCPStdioClient, redact_text, secret_values, url_host

ZCODE_CONFIG_PATH = Path.home() / ".zcode" / "cli" / "config.json"
CLAUDE_CONFIG_PATH = Path.home() / ".claude.json"


# ---------------- 注册表发现 ----------------

def _normalize_http(spec: Dict[str, Any], source: str) -> Optional[Dict[str, Any]]:
    url = spec.get("url")
    if not url:
        return None
    return {
        "transport": "http",
        "url": url,
        "headers": spec.get("headers") or {},
        "source": source,
        "description": spec.get("description") or "",
    }


def _normalize_stdio(spec: Dict[str, Any], source: str) -> Optional[Dict[str, Any]]:
    command = spec.get("command")
    if not command:
        return None
    return {
        "transport": "stdio",
        "command": command,
        "args": [str(a) for a in (spec.get("args") or [])],
        "env": {str(k): str(v) for k, v in (spec.get("env") or {}).items()},
        "cwd": spec.get("cwd"),
        "source": source,
        "description": spec.get("description") or "",
    }


def _parse_server_spec(spec: Dict[str, Any], source: str) -> Optional[Dict[str, Any]]:
    if not isinstance(spec, dict):
        return None
    stype = str(spec.get("type") or spec.get("transport") or "").lower()
    if spec.get("enabled") is False or spec.get("disabled") is True:
        return None
    if stype in ("http", "streamable-http", "sse", "streamable_http") or spec.get("url"):
        return _normalize_http(spec, source)
    if stype in ("stdio", "local") or spec.get("command"):
        return _normalize_stdio(spec, source)
    return None


def _load_json(path: Path) -> Dict[str, Any]:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("读取 MCP 配置失败 %s: %s", path, exc)
    return {}


def discover_mcp_servers() -> Dict[str, Dict[str, Any]]:
    """合并三个来源的 MCP 服务器配置，返回 {server_id: spec}。"""
    registry: Dict[str, Dict[str, Any]] = {}

    seen: Dict[str, str] = {}  # 端点指纹 -> 已用 server_id，避免同一服务重复注册

    def _fingerprint(norm: Dict[str, Any]) -> str:
        if norm["transport"] == "http":
            return "http:" + norm["url"].split("?")[0].rstrip("/").lower()
        return "stdio:" + norm["command"] + " " + " ".join(norm.get("args") or [])

    def _add(sid: str, norm: Dict[str, Any], override: bool = False) -> None:
        fp = _fingerprint(norm)
        if fp in seen and not override:
            return
        seen[fp] = sid
        registry[sid] = norm

    claude = _load_json(CLAUDE_CONFIG_PATH)
    for sid, spec in (claude.get("mcpServers") or {}).items():
        norm = _parse_server_spec(spec, "claude")
        if norm:
            _add(sid, norm)

    zcode = _load_json(ZCODE_CONFIG_PATH)
    for sid, spec in ((zcode.get("mcp") or {}).get("servers") or {}).items():
        norm = _parse_server_spec(spec, "zcode")
        if norm:
            _add(sid, norm)

    try:
        from app.core.settings_store import settings_store
        for spec in settings_store.get("connectors", "custom", default=[]) or []:
            sid = spec.get("id") or spec.get("name")
            if not sid:
                continue
            norm = _parse_server_spec(spec, "local")
            if norm:
                norm["name"] = spec.get("name") or sid
                if spec.get("connector"):
                    norm["connector"] = spec["connector"]
                _add(sid, norm, override=True)
    except Exception as exc:  # 配置损坏不应阻断发现
        logger.warning("读取本地自定义连接器失败: %s", exc)

    if registry:
        logger.info("MCP 注册表: %s", ", ".join(f"{k}({v['transport']})" for k, v in registry.items()))
    else:
        logger.warning("未发现任何 MCP 服务器配置（检查 ~/.claude.json / ~/.zcode/cli/config.json）")
    return registry


def load_mcp_registry() -> Dict[str, Dict[str, Any]]:
    """兼容旧接口：只返回 HTTP 型服务器。"""
    return {k: v for k, v in discover_mcp_servers().items() if v["transport"] == "http"}


# ---------------- HTTP 客户端 ----------------

_STATUS_REASONS = {
    400: "请求被数据源拒绝（格式不对或会话无效）",
    401: "密钥无效或无权限",
    403: "密钥无效或无权限",
    404: "接口或会话不存在",
    408: "数据源响应超时",
    409: "会话冲突",
    410: "会话已失效",
    429: "额度用完或请求过频",
}


def _status_reason(code: int) -> str:
    if code in _STATUS_REASONS:
        return _STATUS_REASONS[code]
    if code >= 500:
        return "数据源服务端出错"
    return "请求被数据源拒绝"


# 只有「请求肯定没送到服务端」的失败才自动重连重试：服务端多半刚重启，旧会话不可信，
# 重建会话后再发一次是安全的。
_RECONNECT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout)
# 连接建立后中途断开（含读回响应时）：工具可能已经在服务端跑完，付费数据源已经扣点。
# 这时自动重发就是重复扣费，所以只报错、不重试，把「要不要再来一次」交给人判断。
_MAYBE_EXECUTED_ERRORS = (httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError)
_RECONNECT_HINTS = {
    "ConnectError": "连不上数据源（网络不通、服务未启动或地址不对）",
    "ConnectTimeout": "连接数据源超时（网络慢或被拦截）",
    "RemoteProtocolError": "与数据源的连接被中断（服务端可能刚重启）",
    "ReadError": "与数据源的连接被中断（服务端可能刚重启）",
    "WriteError": "与数据源的连接被中断（服务端可能刚重启）",
}
# 服务端说「会话不存在 / 未初始化」时的常见措辞（MCP 规范 404，部分实现用 400 或 JSON-RPC 错误）
_SESSION_HINT_RE = re.compile(
    r"session[\s_-]*(id|not\s*found|expired|invalid|terminated|missing|unknown)"
    r"|(no\s+valid|invalid|unknown|expired|missing)\s+session"
    r"|not\s+initiali[sz]ed"
    r"|会话.{0,4}(不存在|失效|过期|无效)", re.I)
_TAG_RE = re.compile(r"<[^>]+>")


class MCPRequestError(RuntimeError):
    """mcp_client 这一层抛出的数据源错误，文本已打码（不含密钥 / query 值 / userinfo）。

    继承 RuntimeError：调用方（connectors）原来 `except Exception` 的捕获方式不受影响。
    session_expired=True 表示会话失效、且可以确定工具没被执行（服务端明确说会话不存在），
    重新 initialize 后值得重试一次；连接中途断开的错误刻意不标这个，免得重复扣点。
    """

    def __init__(self, message: str, *, status: Optional[int] = None, session_expired: bool = False):
        super().__init__(message)
        self.status = status
        self.session_expired = session_expired


class MCPHttpClient:
    """单个 MCP 服务器的轻量 HTTP 会话客户端（懒初始化，进程内复用）。

    服务端明确说会话失效（404 / 410 / 会话相关 4xx）时自动重置并重新 initialize，原请求只重试一次。
    连接中途断开不重试：工具可能已经执行并扣费，重发会重复扣点。
    """

    def __init__(self, server_id: str, url: str, headers: Optional[Dict[str, str]] = None,
                 timeout: float = 60.0):
        self.server_id = server_id
        self.url = url
        self.headers = dict(headers or {})
        self.timeout = timeout
        self._session_id: Optional[str] = None
        self._initialized = False
        # 每成功初始化一次 +1：并发请求同时撞上会话失效时，只让第一个去重建，其余直接用新会话
        self._generation = 0
        self._init_lock = asyncio.Lock()
        self._next_id = 1
        self._secrets = secret_values(url, self.headers)
        self._host = url_host(url)

    def _redact(self, text: Any) -> str:
        return redact_text(text, self._secrets)

    def _base_headers(self) -> Dict[str, str]:
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **self.headers,
        }
        if self._session_id:
            h["mcp-session-id"] = self._session_id
        return h

    async def _post(self, payload: Dict[str, Any], timeout: Optional[float] = None) -> Dict[str, Any]:
        wait = timeout or self.timeout
        prefix = f"MCP {self.server_id} 请求数据源 {self._host}"
        try:
            async with httpx.AsyncClient(timeout=wait) as client:
                resp = await client.post(self.url, headers=self._base_headers(), json=payload)
        except _RECONNECT_ERRORS as exc:
            name = type(exc).__name__
            detail = self._redact(str(exc)).strip()[:160]
            raise MCPRequestError(f"{prefix} 失败：{_RECONNECT_HINTS.get(name, '连接出错')}（{name}"
                                  + (f"：{detail}" if detail else "") + "）",
                                  session_expired=True) from None
        except _MAYBE_EXECUTED_ERRORS as exc:
            name = type(exc).__name__
            detail = self._redact(str(exc)).strip()[:160]
            raise MCPRequestError(f"{prefix} 失败：{_RECONNECT_HINTS.get(name, '连接中断')}（{name}"
                                  + (f"：{detail}" if detail else "")
                                  + "）。请求可能已在数据源执行，为避免重复扣点，没有自动重试",
                                  session_expired=False) from None
        except httpx.TimeoutException:
            raise MCPRequestError(f"{prefix} 超时（{wait:g} 秒内没有返回）") from None
        except httpx.InvalidURL:
            raise MCPRequestError(f"MCP {self.server_id} 的数据源地址格式不对，请检查数据源配置") from None
        except Exception as exc:  # 其余 httpx / 编码错误：文本里可能带 URL，统一打码后再抛
            detail = self._redact(str(exc)).strip()[:200]
            raise MCPRequestError(f"{prefix} 失败（{type(exc).__name__}" + (f"：{detail}" if detail else "")
                                  + "）") from None
        if resp.status_code >= 400:
            raise self._status_error(resp)
        sid = resp.headers.get("mcp-session-id")
        if sid:
            self._session_id = sid
        if resp.status_code == 202 or not resp.text.strip():
            return {}
        return self._parse_response(resp)

    def _status_error(self, resp: httpx.Response) -> MCPRequestError:
        code = resp.status_code
        try:
            body = resp.text[:2000]
        except Exception:
            body = ""
        note = ""
        try:
            obj = json.loads(body) if body.strip().startswith("{") else None
        except ValueError:
            obj = None
        if isinstance(obj, dict):
            err = obj.get("error")
            note = (err.get("message") if isinstance(err, dict) else err) or obj.get("message") or obj.get("detail") or ""
            note = note if isinstance(note, str) else json.dumps(note, ensure_ascii=False)
        elif body:
            note = _TAG_RE.sub(" ", body)
        note = " ".join(self._redact(note).split())[:150]
        session_hint = bool(_SESSION_HINT_RE.search(body))
        session_expired = code in (404, 410) or (session_hint and code not in (401, 403, 429))
        reason = _status_reason(code)
        if session_expired and code not in (404, 410):
            reason = "会话已失效"
        msg = f"MCP {self.server_id} 请求数据源 {self._host} 失败：HTTP {code} {reason}"
        if note:
            msg += f"（服务端说明：{note}）"
        return MCPRequestError(msg, status=code, session_expired=session_expired)

    @staticmethod
    def _parse_response(resp: httpx.Response) -> Dict[str, Any]:
        content_type = resp.headers.get("content-type", "")
        if "text/event-stream" in content_type:
            last_obj: Dict[str, Any] = {}
            for line in resp.text.splitlines():
                line = line.strip()
                if line.startswith("data:"):
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    try:
                        obj = json.loads(data)
                        if isinstance(obj, dict):
                            last_obj = obj
                    except json.JSONDecodeError:
                        continue
            return last_obj
        try:
            return resp.json()
        except Exception:
            return {}

    async def _rpc(self, method: str, params: Optional[Dict[str, Any]] = None,
                   timeout: Optional[float] = None) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        self._next_id += 1
        if params is not None:
            payload["params"] = params
        result = await self._post(payload, timeout=timeout)
        if "error" in result:
            err = result["error"]
            text = self._redact(err if isinstance(err, str) else json.dumps(err, ensure_ascii=False))[:300]
            expired = method != "initialize" and bool(_SESSION_HINT_RE.search(text))
            raise MCPRequestError(f"MCP {self.server_id} 调用 {method} 失败: {text}", session_expired=expired)
        return result.get("result") or {}

    def _reset_session(self) -> None:
        self._session_id = None
        self._initialized = False

    async def _initialize_locked(self) -> None:
        """调用前必须持有 _init_lock。"""
        self._session_id = None  # 带着失效的会话号发 initialize 会被部分服务端拒绝
        await self._rpc("initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "ai-selection-terminal", "version": "3.0.0"},
        })
        try:
            await self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except Exception:
            pass
        self._initialized = True
        self._generation += 1
        logger.info("MCP 会话已初始化: %s", self.server_id)

    async def ensure_initialized(self) -> None:
        if self._initialized:
            return
        async with self._init_lock:
            if self._initialized:
                return
            await self._initialize_locked()

    async def _reinitialize(self, failed_generation: int) -> None:
        async with self._init_lock:
            if self._initialized and self._generation != failed_generation:
                return  # 并发的另一个请求已经重建过会话，直接用新的
            self._reset_session()
            await self._initialize_locked()

    async def _request(self, method: str, params: Optional[Dict[str, Any]] = None,
                       timeout: Optional[float] = None) -> Dict[str, Any]:
        await self.ensure_initialized()
        generation = self._generation
        try:
            return await self._rpc(method, params, timeout=timeout)
        except MCPRequestError as exc:
            if not exc.session_expired:
                raise
            logger.warning("MCP %s 会话失效（%s），重新初始化后重试一次", self.server_id, exc)
        try:
            await self._reinitialize(generation)
            return await self._rpc(method, params, timeout=timeout)
        except MCPRequestError as exc:
            raise MCPRequestError(f"{exc}（已自动重连并重试 1 次，仍失败）",
                                  status=exc.status, session_expired=exc.session_expired) from None

    async def list_tools(self) -> List[Dict[str, Any]]:
        result = await self._request("tools/list", {})
        return result.get("tools") or []

    async def call_tool(self, tool_name: str, arguments: Optional[Dict[str, Any]] = None,
                        timeout: Optional[float] = None) -> Any:
        result = await self._request("tools/call", {"name": tool_name, "arguments": arguments or {}},
                                     timeout=timeout)
        if (result or {}).get("isError"):
            texts = [c.get("text", "") for c in (result.get("content") or []) if isinstance(c, dict)]
            raise MCPRequestError(self._redact("；".join(t for t in texts if t)[:400]) or "工具返回错误")
        return _unwrap_content(result)

    async def ping(self) -> bool:
        try:
            await self.ensure_initialized()
            return True
        except Exception as exc:
            logger.warning("MCP %s 连通性测试失败: %s", self.server_id, exc)
            return False

    async def close(self) -> None:
        self._reset_session()


def _unwrap_content(result: Dict[str, Any]) -> Any:
    contents = (result or {}).get("content") or []
    if (result or {}).get("isError"):
        texts = [c.get("text", "") for c in contents if isinstance(c, dict)]
        raise RuntimeError(redact_text("；".join(t for t in texts if t)[:400]) or "工具返回错误")
    texts: List[str] = [c.get("text", "") for c in contents if isinstance(c, dict) and c.get("type") == "text"]
    if not texts:
        return result
    merged = "\n".join(texts).strip()
    try:
        return json.loads(merged)
    except (json.JSONDecodeError, ValueError):
        match = re.search(r"\{[\s\S]*\}", merged)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
        return merged


# ---------------- 客户端池 ----------------

def _kill_pids_later(pids: List[int], grace: float) -> None:
    """没有事件循环可用时的兜底：等 grace 秒，还没退的进程（组）直接强杀。"""
    import time
    time.sleep(grace)
    sig = getattr(signal, "SIGKILL", signal.SIGTERM)
    for pid in pids:
        try:
            if os.name == "posix":
                os.killpg(pid, sig)
            else:
                os.kill(pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass


class MCPClientPool:
    """服务器客户端池：按 server_id 复用 HTTP 会话 / stdio 子进程。"""

    def __init__(self):
        self._registry: Dict[str, Dict[str, Any]] = discover_mcp_servers()
        self._clients: Dict[str, Any] = {}
        self._closing: set = set()  # 后台关闭任务，持有引用防止被回收
        # 程序正常退出（uvicorn 收到 SIGTERM 后优雅关闭）时，给还活着的 stdio 子进程组发结束信号
        atexit.register(self._signal_all_at_exit)

    def _signal_all_at_exit(self) -> None:
        for client in list(self._clients.values()):
            abort = getattr(client, "abort_nowait", None)
            if abort is None:
                continue
            try:
                abort()
            except Exception:
                pass

    def refresh_registry(self) -> Dict[str, Dict[str, Any]]:
        """重新读取配置，旧客户端全部关闭后再清空（否则每刷新一次就漏一个 stdio 子进程）。

        本函数是同步的（API 层直接调用、不 await），关闭是异步的，所以分两步：
        1. 同步给每个 stdio 子进程（连同进程组）发 SIGTERM，并让旧客户端拒绝再被使用——保证不漏关；
        2. 在当前事件循环里排后台任务做「等退出 → 3 秒不退就 kill」，不阻塞事件循环。
        没有运行中的事件循环时，用后台线程 3 秒后兜底强杀。
        """
        self._registry = discover_mcp_servers()
        old = list(self._clients.values())
        self._clients.clear()
        self._retire(old)
        return self._registry

    def _retire(self, clients: List[Any]) -> None:
        if not clients:
            return
        pids: List[int] = []
        for client in clients:
            abort = getattr(client, "abort_nowait", None)
            if abort is None:
                continue
            try:
                pid = abort()
                if pid:
                    pids.append(pid)
            except Exception as exc:
                logger.warning("通知 MCP %s 退出失败: %s", getattr(client, "server_id", "?"), exc)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            for client in clients:
                task = loop.create_task(self._close_quietly(client))
                self._closing.add(task)
                task.add_done_callback(self._closing.discard)
        elif pids:
            threading.Thread(target=_kill_pids_later, args=(pids, 3.0), daemon=True,
                             name="mcp-stdio-reaper").start()

    @staticmethod
    async def _close_quietly(client: Any) -> None:
        try:
            await client.close()
        except Exception as exc:
            logger.warning("关闭 MCP %s 失败: %s", getattr(client, "server_id", "?"), exc)

    @property
    def registry(self) -> Dict[str, Dict[str, Any]]:
        return self._registry

    def get_client(self, server_id: str):
        spec = self._registry.get(server_id)
        if not spec:
            raise KeyError(f"未注册的 MCP 服务器: {server_id}，可用: {list(self._registry)}")
        client = self._clients.get(server_id)
        if client is None:
            if spec["transport"] == "stdio":
                client = MCPStdioClient(
                    server_id=server_id, command=spec["command"], args=spec.get("args"),
                    env=spec.get("env"), cwd=spec.get("cwd"),
                )
            else:
                client = MCPHttpClient(server_id=server_id, url=spec["url"], headers=spec.get("headers"))
            self._clients[server_id] = client
        return client

    def find_server_by_tool_prefix(self, prefixes: List[str], tools: Optional[List[str]] = None) -> Optional[str]:
        """按 server_id 名称猜服务器。tools 传入时优先匹配工具名里出现的服务器特征。"""
        for sid in self._registry:
            if any(p in sid.lower() for p in prefixes):
                return sid
        if tools:
            joined = " ".join(tools)
            for sid, spec in self._registry.items():
                marker = spec.get("description") or ""
                if marker and marker in joined:
                    return sid
        return None

    async def close_all(self) -> None:
        clients = list(self._clients.values())
        self._clients.clear()
        for client in clients:
            await self._close_quietly(client)
        if self._closing:
            await asyncio.gather(*list(self._closing), return_exceptions=True)


mcp_client_pool = MCPClientPool()


# ---------------- 通用解析辅助 ----------------

def dig(data: Any, *paths: str, default=None) -> Any:
    """防御式取数：依次尝试若干 "a.b.c" 嵌套路径，返回首个命中的值。"""
    for path in paths:
        node = data
        ok = True
        for seg in path.split("."):
            if isinstance(node, dict) and seg in node:
                node = node[seg]
            elif isinstance(node, list) and seg.isdigit() and int(seg) < len(node):
                node = node[int(seg)]
            else:
                ok = False
                break
        if ok and node is not None:
            return node
    return default


def truncate_text(value: Any, max_chars: int = 4000) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=1, default=str)
    return text if len(text) <= max_chars else text[:max_chars] + f"\n...(已截断，原文 {len(text)} 字符)"
