"""
stdio 传输的 MCP 客户端。

用于本地 MCP 服务（如已封装好的 `1688_scraper`）：后端按配置拉起子进程，
以 JSON-RPC 2.0 换行分隔报文（LSP 风格）通过 stdin/stdout 通信。

要点：
- 懒启动、按 server_id 复用一个进程；进程意外退出、输出流读坏时，挂起请求立刻失败，下次调用自动重启。
- 单行报文上限 32MB（asyncio 默认只有 64KiB，1688 返回几十条带图链接的结果就会超）。
- stderr 单独排空，避免管道写满导致子进程卡死。
- 请求-响应按 id 配对；服务器主动推送的通知直接丢弃（当前不需要）。
- 子进程放进独立进程组，关闭时连同它拉起的孙进程（npx → node、浏览器等）一起结束。

本文件也提供打码工具（redact_text / redact_url / mask_args），mcp_client 复用，
保证这一层抛出的异常和日志里不带密钥。
"""
import asyncio
import json
import os
import re
import signal
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlsplit, urlunsplit

from app.core.logger import logger

# 单行 JSON 上限。超过这个长度的输出无法完整读出，会让本地数据源整体失效并在下次调用时重启。
STDIO_LINE_LIMIT = 32 * 1024 * 1024
_STOP_GRACE_SECONDS = 3.0


# ---------------- 打码工具 ----------------

_SENSITIVE_WORDS = ("key", "token", "secret", "password", "passwd", "pwd", "auth", "credential", "cookie")
_URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://[^\s'\"<>`]+")
_QUERY_VALUE_RE = re.compile(r"([?&][A-Za-z0-9_.\-\[\]%]+=)([^&#\s'\"<>`]+)")
_BEARER_RE = re.compile(r"(?i)\b(bearer|basic|token)\s+[A-Za-z0-9._\-~+/=]{8,}")
_TOKENISH_RE = re.compile(r"^(?=.*\d)(?=.*[A-Za-z])[A-Za-z0-9_\-.]*[A-Za-z0-9]{20,}[A-Za-z0-9_\-.]*$")
_MASK = "***"


def _looks_sensitive_name(name: str) -> bool:
    low = name.lower()
    return any(w in low for w in _SENSITIVE_WORDS)


def _looks_like_token(value: str) -> bool:
    """路径、包名不算；一长串字母数字混合（≥24 位）多半是令牌。"""
    return 24 <= len(value) <= 512 and "/" not in value and bool(_TOKENISH_RE.match(value))


def redact_url(url: str) -> str:
    """去掉 userinfo，query 参数值一律换成 ***，路径里像令牌的段也打码。"""
    try:
        parts = urlsplit(url)
    except ValueError:
        return _QUERY_VALUE_RE.sub(lambda m: m.group(1) + _MASK, url)
    netloc = parts.netloc.rsplit("@", 1)[-1]
    path = "/".join(_MASK if _looks_like_token(seg) else seg for seg in parts.path.split("/"))
    query = "&".join(
        (kv.split("=", 1)[0] + "=" + _MASK) if "=" in kv else kv
        for kv in parts.query.split("&")
    ) if parts.query else ""
    return urlunsplit((parts.scheme, netloc, path, query, ""))


def url_host(url: str) -> str:
    """只留 scheme://host[:port]，用于报错时告诉用户是哪个数据源。"""
    try:
        parts = urlsplit(url)
        host = parts.netloc.rsplit("@", 1)[-1]
        return f"{parts.scheme}://{host}" if host else "（地址无法解析）"
    except ValueError:
        return "（地址无法解析）"


def secret_values(*sources: Any) -> List[str]:
    """从 URL / 请求头 / 环境变量 / 启动参数里收集「已知的密钥原文」，用于在任意文本里精确替换。"""
    found: List[str] = []

    def _add(v: Any) -> None:
        s = str(v or "").strip()
        if len(s) >= 6:
            found.append(s)

    def _from_url(u: str) -> None:
        try:
            parts = urlsplit(u)
        except ValueError:
            return
        if "@" in parts.netloc:
            userinfo = parts.netloc.rsplit("@", 1)[0]
            for piece in userinfo.split(":"):
                _add(piece)
        for kv in parts.query.split("&"):
            if "=" in kv:
                _add(kv.split("=", 1)[1])
        for seg in parts.path.split("/"):
            if _looks_like_token(seg):
                _add(seg)

    for src in sources:
        if not src:
            continue
        if isinstance(src, str):
            if "://" in src:
                _from_url(src)
            continue
        if isinstance(src, dict):  # 请求头 / 环境变量：值都当密钥看
            for v in src.values():
                v = str(v or "")
                _add(v)
                if " " in v:  # "Bearer xxx" 里的 xxx
                    _add(v.split(" ", 1)[1])
            continue
        if isinstance(src, (list, tuple)):  # 启动参数
            prev_sensitive = False
            for arg in src:
                arg = str(arg)
                if prev_sensitive and not arg.startswith("-"):
                    _add(arg)
                prev_sensitive = False
                if "://" in arg:
                    _from_url(arg.split("=", 1)[1] if arg.startswith("-") and "=" in arg else arg)
                if arg.startswith("-") and "=" in arg:
                    name, val = arg.split("=", 1)
                    if _looks_sensitive_name(name) or len(val) >= 16:
                        if "://" not in val:
                            _add(val)
                elif arg.startswith("-") and _looks_sensitive_name(arg):
                    prev_sensitive = True
                elif _looks_like_token(arg):
                    _add(arg)
    # 长的先替换，避免短串把长串切碎
    return sorted(set(found), key=len, reverse=True)


def redact_text(text: Any, secrets: Iterable[str] = ()) -> str:
    """把文本里的已知密钥、URL 的 query 值 / userinfo、Bearer 令牌换成 ***。"""
    s = "" if text is None else str(text)
    if not s:
        return s
    for secret in secrets:
        if secret and secret in s:
            s = s.replace(secret, _MASK)

    def _url(m: "re.Match[str]") -> str:
        raw = m.group(0)
        tail = ""
        while raw and raw[-1] in ".,;:)]}，。；）":
            tail = raw[-1] + tail
            raw = raw[:-1]
        return redact_url(raw) + tail

    s = _URL_RE.sub(_url, s)
    s = _QUERY_VALUE_RE.sub(lambda m: m.group(1) + (m.group(2) if m.group(2) == _MASK else _MASK), s)
    s = _BEARER_RE.sub(lambda m: m.group(1) + " " + _MASK, s)
    return s


def mask_args(args: Iterable[Any]) -> List[str]:
    """启动参数打码后再进日志：含 key/token/secret/password 字样的参数、= 后 ≥16 位的值、URL 里的 query 值。"""
    out: List[str] = []
    prev_sensitive = False
    for raw in args:
        arg = str(raw)
        if prev_sensitive and not arg.startswith("-"):
            out.append(_MASK)
            prev_sensitive = False
            continue
        prev_sensitive = False
        if "=" in arg and not arg.startswith(("http://", "https://")):
            name, val = arg.split("=", 1)
            if _looks_sensitive_name(name) or (len(val) >= 16 and "://" not in val):
                out.append(f"{name}={_MASK}")
            elif "://" in val:
                out.append(f"{name}={redact_url(val)}")
            else:
                out.append(arg)
            continue
        if "://" in arg:
            out.append(redact_url(arg))
            continue
        if arg.startswith("-") and _looks_sensitive_name(arg):
            prev_sensitive = True
            out.append(arg)
            continue
        out.append(_MASK if _looks_like_token(arg) else arg)
    return out


# ---------------- 客户端 ----------------

class MCPStdioClient:
    def __init__(self, server_id: str, command: str, args: Optional[List[str]] = None,
                 env: Optional[Dict[str, str]] = None, cwd: Optional[str] = None,
                 timeout: float = 120.0):
        self.server_id = server_id
        self.command = command
        self.args = list(args or [])
        self.env = dict(env or {})
        self.cwd = cwd
        self.timeout = timeout
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._pending: Dict[int, asyncio.Future] = {}
        self._next_id = 1
        self._initialized = False
        # 非 None 表示当前进程已不可用（读循环退出 / 写入失败 / 初始化失败），下次调用会重启
        self._broken_reason: Optional[str] = None
        # 被客户端池换下（数据源配置刷新）或程序退出后置 True，此后不再拉起进程
        self._closed = False
        self._lock = asyncio.Lock()
        self._stderr_tail: List[str] = []
        # 只把「名字像密钥」或「值像令牌」的环境变量当密钥，免得 NODE_ENV=production 之类也被打掉
        secret_env = {k: v for k, v in self.env.items() if _looks_sensitive_name(k) or _looks_like_token(str(v))}
        self._secrets = secret_values(secret_env, self.args)

    # ---------------- 进程生命周期 ----------------

    def _alive(self) -> bool:
        return bool(self._proc and self._proc.returncode is None and self._broken_reason is None)

    def _redact(self, text: Any) -> str:
        return redact_text(text, self._secrets)

    def _closed_error(self) -> RuntimeError:
        return RuntimeError(f"本地数据源 {self.server_id} 已关闭（数据源配置刚刷新或程序正在退出），请重试一次")

    async def _ensure_started(self) -> None:
        if self._closed:
            raise self._closed_error()
        if self._alive():
            return
        old = self._proc
        if old is not None:
            # 旧进程可能还活着但输出流已读坏 / 卡死：先确保它退出再拉新的
            logger.info("重启本地数据源 %s（原因：%s）", self.server_id,
                        self._broken_reason or f"进程已退出 exit={old.returncode}")
            await self._stop_process(old, self._reader_task, self._stderr_task)
        if not os.path.exists(self.command) and not _which(self.command):
            raise RuntimeError(f"本地数据源 {self.server_id}：找不到启动程序 {self.command}，请检查数据源配置")
        merged_env = {**os.environ, **self.env}
        merged_env.setdefault("PYTHONUNBUFFERED", "1")
        logger.info("启动 stdio MCP %s: %s %s", self.server_id, self.command, " ".join(mask_args(self.args)))
        try:
            proc = await asyncio.create_subprocess_exec(
                self.command, *self.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=merged_env,
                cwd=self.cwd,
                limit=STDIO_LINE_LIMIT,
                start_new_session=(os.name == "posix"),
            )
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"本地数据源 {self.server_id} 启动失败（{type(exc).__name__}）："
                               f"{self._redact(exc)}") from None
        if self._closed:  # 启动期间被池换下：立刻收掉，别留孤儿进程
            await self._stop_process(proc, None, None)
            raise self._closed_error()
        self._proc = proc
        self._pending = {}
        self._broken_reason = None
        self._initialized = False
        self._stderr_tail = []
        self._reader_task = asyncio.create_task(self._read_stdout(proc, self._pending))
        self._stderr_task = asyncio.create_task(self._drain_stderr(proc))

    async def _read_stdout(self, proc: asyncio.subprocess.Process,
                           pending: Dict[int, asyncio.Future]) -> None:
        """读循环与具体进程绑定：只会结束本进程的挂起请求，不会误伤重启后的新进程。"""
        reason: Optional[str] = None
        try:
            assert proc.stdout is not None
            while True:
                try:
                    line = await proc.stdout.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    reason = (f"本地数据源 {self.server_id} 返回的单条数据超过 "
                              f"{STDIO_LINE_LIMIT // (1024 * 1024)}MB 上限，无法读取")
                    break
                if not line:
                    reason = await self._exit_reason(proc)
                    break
                self._dispatch(proc, pending, line)
        except asyncio.CancelledError:
            reason = reason or ("CLOSED" if self._closed else
                                f"本地数据源 {self.server_id} 的读取已被中断（数据源正在重启）")
            raise
        except Exception as exc:
            reason = f"本地数据源 {self.server_id} 读取输出出错（{type(exc).__name__}）"
        finally:
            self._on_reader_exit(proc, pending, reason)

    def _dispatch(self, proc: asyncio.subprocess.Process, pending: Dict[int, asyncio.Future],
                  line: bytes) -> None:
        text = line.decode("utf-8", "replace").strip()
        if not text:
            return
        try:
            msg = json.loads(text)
        except json.JSONDecodeError:
            logger.debug("MCP %s 非 JSON 输出: %s", self.server_id, self._redact(text[:200]))
            return
        if not isinstance(msg, dict):
            return
        msg_id = msg.get("id")
        if "method" in msg:
            if msg_id is not None:
                # 服务器主动请求（如 sampling/elicitation）：回一个不支持，避免对方等待
                self._write_nowait(proc, {"jsonrpc": "2.0", "id": msg_id,
                                          "error": {"code": -32601, "message": "client does not support this request"}})
            return
        fut = pending.pop(msg_id, None) if msg_id is not None else None
        if fut and not fut.done():
            fut.set_result(msg)

    def _write_nowait(self, proc: asyncio.subprocess.Process, payload: Dict[str, Any]) -> None:
        try:
            if proc.stdin and not proc.stdin.is_closing():
                proc.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        except Exception as exc:
            logger.debug("MCP %s 回复服务器请求失败: %s", self.server_id, type(exc).__name__)

    async def _exit_reason(self, proc: asyncio.subprocess.Process) -> str:
        try:  # 进程正在退出时稍等一下，拿到退出码和最后几行 stderr；仍活着就不多等
            await asyncio.wait_for(proc.wait(), timeout=1)
        except (asyncio.TimeoutError, Exception):
            pass
        if self._stderr_task and self._proc is proc and not self._stderr_task.done():
            await asyncio.wait({self._stderr_task}, timeout=0.5 if proc.returncode is not None else 0.1)
        code = proc.returncode
        tail = "\n".join(self._stderr_tail[-5:])
        if code is None:
            head = f"本地数据源 {self.server_id} 关闭了输出通道（进程仍在运行，已结束它）"
        else:
            head = f"本地数据源 {self.server_id} 进程已退出(exit={code})"
        return head + (f"：{tail}" if tail else "")

    def _on_reader_exit(self, proc: asyncio.subprocess.Process, pending: Dict[int, asyncio.Future],
                        reason: Optional[str]) -> None:
        closing = reason == "CLOSED" or self._closed
        if closing:
            message = self._closed_error().args[0] + "（本次调用已中断）"
        else:
            reason = reason or f"本地数据源 {self.server_id} 输出意外结束"
            message = f"{reason}；下次调用会自动重启该数据源"
        if self._proc is proc and not closing:
            self._broken_reason = reason
            self._initialized = False
            logger.warning("本地数据源 %s 已不可用：%s", self.server_id, reason)
            if proc.returncode is None:
                # 进程还活着但协议流已坏：先发结束信号，真正的等待 / 强杀在下次启动前完成
                _signal_process(proc, signal.SIGTERM)
        for fut in list(pending.values()):
            if not fut.done():
                fut.set_exception(RuntimeError(message))
        pending.clear()

    async def _drain_stderr(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stderr is not None
        while True:
            try:
                line = await proc.stderr.readline()
            except (ValueError, asyncio.LimitOverrunError):
                continue  # 超长的 stderr 行直接丢掉，继续排空，别让子进程写满管道卡死
            except (asyncio.CancelledError, Exception):
                break
            if not line:
                break
            text = line.decode("utf-8", "replace").rstrip()
            if text:
                self._stderr_tail = (self._stderr_tail + [self._redact(text[:500])])[-20:]

    async def _stop_process(self, proc: Optional[asyncio.subprocess.Process],
                            *tasks: Optional[asyncio.Task]) -> None:
        """terminate → 等一小会儿 → 不退就 kill；连同进程组里的孙进程一起结束。"""
        for task in tasks:
            if task and not task.done():
                task.cancel()
        if proc is None:
            return
        _signal_process(proc, signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), timeout=_STOP_GRACE_SECONDS)
        except (asyncio.TimeoutError, Exception):
            _signal_process(proc, signal.SIGKILL if hasattr(signal, "SIGKILL") else signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), timeout=2)
            except (asyncio.TimeoutError, Exception):
                logger.warning("本地数据源 %s 进程(pid=%s)未能结束", self.server_id, proc.pid)
        # 进程组里可能还有孙进程（npx 拉起的 node、浏览器等）：主进程已退出也再补一刀
        _signal_process(proc, signal.SIGKILL if hasattr(signal, "SIGKILL") else signal.SIGTERM,
                        group_only=True)
        try:
            if proc.stdin and not proc.stdin.is_closing():
                proc.stdin.close()
        except Exception:
            pass

    def abort_nowait(self) -> Optional[int]:
        """同步发出关闭信号（不等待），用于同步上下文里先保证子进程收到 SIGTERM；返回 pid。

        真正的等待与强杀由 close() 完成；没有事件循环可用时由调用方兜底强杀。
        """
        self._closed = True
        self._initialized = False
        for task in (self._reader_task, self._stderr_task):
            try:
                if task and not task.done():
                    task.cancel()
            except Exception:
                pass
        proc = self._proc
        if proc is None:
            return None
        _signal_process(proc, signal.SIGTERM)
        return proc.pid

    async def close(self) -> None:
        self._closed = True
        self._initialized = False
        proc = self._proc
        reader, stderr = self._reader_task, self._stderr_task
        self._reader_task = self._stderr_task = None
        await self._stop_process(proc, reader, stderr)
        if reader is not None:
            await asyncio.wait({reader}, timeout=1)
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(RuntimeError(self._closed_error().args[0] + "（本次调用已中断）"))
        self._pending.clear()
        self._proc = None

    # ---------------- 协议层 ----------------

    def _unavailable_error(self) -> RuntimeError:
        if self._closed:
            return self._closed_error()
        if self._broken_reason:
            return RuntimeError(f"{self._broken_reason}；下次调用会自动重启该数据源")
        code = self._proc.returncode if self._proc else None
        return RuntimeError(f"本地数据源 {self.server_id} 未运行(exit={code})；下次调用会自动重启该数据源")

    async def _write(self, payload: Dict[str, Any]) -> None:
        proc = self._proc
        if not self._alive() or not proc or not proc.stdin:
            raise self._unavailable_error()
        data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            proc.stdin.write(data)
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, RuntimeError, OSError) as exc:
            reason = f"本地数据源 {self.server_id} 写入请求失败（{type(exc).__name__}，进程可能已退出）"
            if self._proc is proc and self._broken_reason is None:
                self._broken_reason = reason
                self._initialized = False
                _signal_process(proc, signal.SIGTERM)
            raise RuntimeError(f"{reason}；下次调用会自动重启该数据源") from None

    async def _rpc(self, method: str, params: Optional[Dict[str, Any]] = None,
                   timeout: Optional[float] = None) -> Dict[str, Any]:
        if not self._alive():
            raise self._unavailable_error()
        msg_id = self._next_id
        self._next_id += 1
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        pending = self._pending  # 属于当前进程；进程读坏时由它的读循环统一结束
        pending[msg_id] = fut
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id, "method": method}
        if params is not None:
            payload["params"] = params
        wait = timeout or self.timeout
        try:
            await self._write(payload)
            reply = await asyncio.wait_for(fut, timeout=wait)
        except asyncio.TimeoutError:
            raise RuntimeError(f"本地数据源 {self.server_id} 调用 {method} 超时({wait:g}s)"
                               + (f"；stderr: {self._stderr_tail[-1]}" if self._stderr_tail else "")) from None
        finally:
            pending.pop(msg_id, None)
        if isinstance(reply, dict) and reply.get("error"):
            raise RuntimeError(f"本地数据源 {self.server_id} 调用 {method} 失败: {self._redact(reply['error'])}")
        return (reply or {}).get("result") or {}

    async def ensure_initialized(self) -> None:
        async with self._lock:
            if self._initialized and self._alive():
                return
            await self._ensure_started()
            proc = self._proc
            try:
                await self._rpc("initialize", {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "ai-selection-terminal", "version": "3.0.0"},
                })
                await self._write({"jsonrpc": "2.0", "method": "notifications/initialized"})
            except Exception as exc:
                # 初始化没走完的进程状态不明：标记不可用，下次调用重启，而不是对着它反复重发
                if self._proc is proc and proc is not None and self._broken_reason is None:
                    self._broken_reason = f"本地数据源 {self.server_id} 初始化失败：{str(exc)[:200]}"
                    _signal_process(proc, signal.SIGTERM)
                raise
            self._initialized = True
            logger.info("stdio MCP 会话已初始化: %s", self.server_id)

    async def list_tools(self) -> List[Dict[str, Any]]:
        await self.ensure_initialized()
        result = await self._rpc("tools/list", {})
        return result.get("tools") or []

    async def call_tool(self, tool_name: str, arguments: Optional[Dict[str, Any]] = None,
                        timeout: Optional[float] = None) -> Any:
        await self.ensure_initialized()
        result = await self._rpc("tools/call", {"name": tool_name, "arguments": arguments or {}},
                                 timeout=timeout)
        if (result or {}).get("isError"):
            # 工具自己报错（如登录墙、参数错）：不能当成数据交出去
            texts = [c.get("text", "") for c in (result.get("content") or []) if isinstance(c, dict)]
            msg = "；".join(t for t in texts if t)[:400] or "工具返回错误"
            raise RuntimeError(f"本地数据源 {self.server_id} 工具 {tool_name} 报错：{self._redact(msg)}")
        return _unwrap_content(result)

    async def ping(self) -> bool:
        try:
            await self.ensure_initialized()
            return True
        except Exception as exc:
            logger.warning("stdio MCP %s 连通性测试失败: %s", self.server_id, exc)
            return False


def _signal_process(proc: asyncio.subprocess.Process, sig: int, group_only: bool = False) -> None:
    """给子进程（POSIX 下是整个进程组）发信号；进程已不存在时静默。"""
    if os.name == "posix":
        try:
            os.killpg(proc.pid, sig)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
    if group_only or proc.returncode is not None:
        return
    try:
        proc.send_signal(sig)
    except (ProcessLookupError, RuntimeError, OSError):
        pass


def _unwrap_content(result: Dict[str, Any]) -> Any:
    """把 tools/call 的 content 还原为 Python 对象：JSON 文本优先。"""
    contents = (result or {}).get("content") or []
    texts = [c.get("text", "") for c in contents if isinstance(c, dict) and c.get("type") == "text"]
    if not texts:
        return result
    merged = "\n".join(texts).strip()
    try:
        return json.loads(merged)
    except (json.JSONDecodeError, ValueError):
        return merged


def _which(cmd: str) -> Optional[str]:
    from shutil import which
    return which(cmd)
