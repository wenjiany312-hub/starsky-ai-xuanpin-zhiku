"""
Reddit 免费取数：Arctic Shift 公开存档接口（无需密钥、无需 MCP 额度）。

为什么不用 reddit.com 官方接口：数据中心 / 代理 IP 一律 403，换 UA 没用；
为什么不用 PullPush：经常 502。Arctic Shift 数据到当天，且没有「每列表 1000 帖」上限。

约束（都来自实测）：
- 全文检索 `query`（帖子）与 `body`（评论）必须同时给 subreddit 或 author；
- 服务端忙时返回 {"error": "Timeout. Maybe slow down a bit"}，要退避重试，不能当成「没数据」；
- 限速约 1 次/秒，礼貌起见全局串行 + 间隔。
"""
import asyncio
import time
from typing import Any, Dict, List, Optional

import httpx

BASE = "https://arctic-shift.photon-reddit.com/api"
MIN_INTERVAL = 6.0            # 实测 2026-11：这个免费服务的限速比文档严，6s/次 + 退避 10~30s 才稳
PAGE = 100
_UA = "ai-selection-hub/3 (+local research tool)"


class VocSourceError(RuntimeError):
    pass


class ArcticShift:
    """串行限速的轻量客户端。一个实例一次采集，别跨事件循环复用。"""

    def __init__(self, min_interval: float = MIN_INTERVAL, retries: int = 5):
        self._gap = min_interval
        self._retries = retries
        self._last = 0.0
        self._lock = asyncio.Lock()
        self._client = httpx.AsyncClient(timeout=40, headers={"User-Agent": _UA}, follow_redirects=True)
        self.requests = 0

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        last_err = ""
        for attempt in range(self._retries + 1):
            async with self._lock:
                wait = self._gap - (time.monotonic() - self._last)
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last = time.monotonic()
                self.requests += 1
                try:
                    r = await self._client.get(f"{BASE}/{path}", params=params)
                except httpx.HTTPError as exc:
                    last_err = f"网络错误：{exc.__class__.__name__}"
                    r = None
            if r is not None:
                if r.status_code == 200:
                    try:
                        body = r.json()
                    except ValueError:
                        body = {}
                    err = (body or {}).get("error")
                    if not err:
                        return body.get("data") or []
                    last_err = str(err)
                    if "Unknown query parameter" in last_err or "requires one of" in last_err:
                        raise VocSourceError(last_err)          # 参数错不是重试能好的
                elif r.status_code in (400, 404):
                    raise VocSourceError(f"HTTP {r.status_code}：{r.text[:120]}")
                else:
                    last_err = f"HTTP {r.status_code}"
            # 422「slow down a bit」实测 8~30 秒后再试就通；退避别客气
            await asyncio.sleep(min(10.0 * (attempt + 1), 30.0))
        raise VocSourceError(f"Arctic Shift 多次重试仍失败：{last_err}")

    async def search(self, kind: str, subreddit: str, *, text: Optional[str] = None,
                     after: Optional[int] = None, before: Optional[int] = None,
                     cap: int = 200, fields: str = "") -> List[Dict[str, Any]]:
        """按 created_utc 升序游标翻页，最多取 cap 条。kind: posts | comments。"""
        if kind not in ("posts", "comments"):
            raise ValueError(kind)
        out: List[Dict[str, Any]] = []
        cursor = after
        seen = set()
        while len(out) < cap:
            p: Dict[str, Any] = {"subreddit": subreddit, "limit": min(PAGE, cap - len(out)), "sort": "asc"}
            if text:
                p["query" if kind == "posts" else "body"] = text
            if cursor is not None:
                p["after"] = cursor
            if before is not None:
                p["before"] = before
            if fields:
                p["fields"] = fields
            rows = await self._get(f"{kind}/search", p)
            if not rows:
                break
            fresh = [x for x in rows if x.get("id") not in seen]
            for x in fresh:
                seen.add(x.get("id"))
            out.extend(fresh)
            last_ts = max((int(x.get("created_utc") or 0) for x in rows), default=0)
            if len(rows) < p["limit"] or not last_ts or not fresh:
                break
            cursor = last_ts
        return out[:cap]
