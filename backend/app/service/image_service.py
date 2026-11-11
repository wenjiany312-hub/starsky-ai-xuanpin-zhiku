"""
产品图上传与取回。

表格上传走 upload_service（CSV/XLSX → 竞品记录）；图片是另一条路：存原图、算指纹、能取回，
识别与找货在 vision.py / 选品引擎里做。图片不进 upload_service 的记录索引，
否则「上传表」列表里会混进一堆点开就报错的图片。

同名同内容只存一份（按 sha256 命名），重复上传不会堆垃圾。
"""
import hashlib
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.core.config import settings
from app.core.jsonio import read_json, write_json

MAX_BYTES = 8 * 1024 * 1024

_MAGIC: List[Tuple[bytes, str, str]] = [
    (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", "image/png", ".png"),
    (b"GIF87a", "image/gif", ".gif"),
    (b"GIF89a", "image/gif", ".gif"),
]
# WebP 是 RIFF....WEBP
_ID_RE = re.compile(r"IMG-[0-9a-f]{12}")


def sniff(data: bytes) -> Optional[Tuple[str, str]]:
    """按文件头判类型，不信扩展名（改名的 .heic/.avif 会在前端变成一张破图）。"""
    for magic, mime, ext in _MAGIC:
        if data.startswith(magic):
            return mime, ext
    if len(data) > 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    return None


async def _assert_public_host(url: str) -> None:
    """只许取公网地址：本机、局域网、链路本地一律拒（每一跳重定向都查）。
    这个地址还会交给 1688 去抓，内网地址它本来也抓不到，留着只会让人拿本软件去探内网。"""
    import asyncio
    import ipaddress
    import socket
    from urllib.parse import urlsplit

    host = urlsplit(url).hostname or ""
    if not host:
        raise ValueError("图片地址里没有域名")
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError(f"图片地址的域名解析不了：{host}") from exc
    # 198.18.0.0/15 是 Clash / Surge 等代理 fake-ip 模式发的占位地址：开着代理时所有域名都解析到这里，
    # 真实去向由代理决定。不放行的话，开代理的用户（跨境卖家很常见）连亚马逊图片都取不了。
    # 但用户直接填 198.18.x.x 这种 IP 字面量不放行（那不是代理发的占位，是指名道姓访问）
    fake_ip = ipaddress.ip_network("198.18.0.0/15")
    try:
        ipaddress.ip_address(host)
        literal = True
    except ValueError:
        literal = False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not literal and ip.version == 4 and ip in fake_ip:
            continue
        if not ip.is_global or ip.is_multicast:
            raise ValueError("只能填公网图片地址（本机 / 局域网地址取不了，1688 也看不到）")


async def fetch_remote(url: str) -> Tuple[bytes, str]:
    """取公网图片（1688 以图搜同款只认 URL，所以先存一份再搜）。"""
    import asyncio
    import httpx

    u = (url or "").strip()
    if not re.match(r"^https?://", u, re.I):
        raise ValueError("请填公网 http(s) 图片地址")
    too_big = ValueError(f"图片超过 {MAX_BYTES // 1024 // 1024}MB，请换一张或先压缩")

    async def _get() -> bytes:
        # 重定向自己跟：每一跳都要过公网检查；边收边数，超过上限立刻断，不把大文件整个读进内存
        target = u
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=False) as client:
            for _ in range(5):
                await _assert_public_host(target)
                async with client.stream("GET", target, headers={"User-Agent": "Mozilla/5.0"}) as resp:
                    if resp.is_redirect and resp.headers.get("location"):
                        target = str(resp.url.join(resp.headers["location"]))
                        continue
                    if resp.status_code != 200:
                        raise ValueError(f"图片取不回来（HTTP {resp.status_code}）")
                    declared = resp.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > MAX_BYTES:
                        raise too_big
                    buf = bytearray()
                    async for chunk in resp.aiter_bytes():
                        buf += chunk
                        if len(buf) > MAX_BYTES:
                            raise too_big
                    return bytes(buf)
        raise ValueError("图片地址重定向次数太多")

    try:
        data = await asyncio.wait_for(_get(), timeout=45.0)
    except ValueError:
        raise
    except asyncio.TimeoutError as exc:
        raise ValueError("图片取不回来：45 秒内没下载完") from exc
    except Exception as exc:
        raise ValueError(f"图片取不回来：{type(exc).__name__}") from exc
    name = (u.rsplit("/", 1)[-1].split("?")[0] or "url-image.jpg")[:80]
    return data, name


class ImageService:
    def __init__(self):
        self.dir = settings.UPLOADS_DIR / "images"
        self.index = self.dir / "index.json"

    def _ensure(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)

    def save(self, filename: str, data: bytes) -> Dict[str, Any]:
        if not data:
            raise ValueError("图片是空的")
        if len(data) > MAX_BYTES:
            raise ValueError(f"图片超过 {MAX_BYTES // 1024 // 1024}MB，请压缩后再传")
        kind = sniff(data)
        if not kind:
            raise ValueError("这不是 JPG/PNG/WebP/GIF 图片（HEIC 请先导出成 JPG）")
        mime, ext = kind
        self._ensure()
        digest = hashlib.sha256(data).hexdigest()
        image_id = f"IMG-{digest[:12]}"
        path = self.dir / f"{image_id}{ext}"
        if not path.exists():
            path.write_bytes(data)
        safe_name = re.sub(r"[^\w.\-一-鿿]+", "_", Path(filename or "").name)[:80] or f"{image_id}{ext}"
        meta = {
            "id": image_id,
            "filename": safe_name,
            "mime": mime,
            "bytes": len(data),
            "sha256": digest,
            "path": path.name,
            "url": f"/api/v1/workflows/images/{image_id}",
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        write_json(self.dir / f"{image_id}.json", meta)
        items = [m for m in (read_json(self.index, default=[]) or []) if m.get("id") != image_id]
        items.insert(0, {k: meta[k] for k in ("id", "filename", "mime", "bytes", "url", "created_at")})
        write_json(self.index, items[:200])
        return meta

    def load(self, image_id: str) -> Optional[Tuple[bytes, Dict[str, Any]]]:
        if not _ID_RE.fullmatch(image_id or ""):
            return None
        meta = read_json(self.dir / f"{image_id}.json", default=None)
        if not meta:
            return None
        path = self.dir / str(meta.get("path") or "")
        if not path.is_file():
            return None
        return path.read_bytes(), meta

    def meta(self, image_id: str) -> Optional[Dict[str, Any]]:
        if not _ID_RE.fullmatch(image_id or ""):
            return None
        return read_json(self.dir / f"{image_id}.json", default=None)

    def resolve(self, meta: Dict[str, Any]) -> Optional[Path]:
        """meta["path"] 只存文件名，真正路径得拼回图片目录。"""
        p = self.dir / str(meta.get("path") or "")
        return p if p.is_file() else None

    def remember_public_url(self, image_id: str, url: str) -> None:
        """公网图 URL 记下来：1688 以图搜同款要的是 URL，不是本地文件。"""
        meta = self.meta(image_id)
        if not meta:
            return
        meta["public_url"] = url
        write_json(self.dir / f"{image_id}.json", meta)
        items = read_json(self.index, default=[]) or []
        for it in items:
            if it.get("id") == image_id:
                it["public_url"] = url
        write_json(self.index, items[:200])

    def list(self) -> List[Dict[str, Any]]:
        return read_json(self.index, default=[]) or []


image_service = ImageService()
