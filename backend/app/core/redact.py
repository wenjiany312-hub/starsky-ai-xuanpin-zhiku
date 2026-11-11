"""错误文本打码：数据源报错时，URL 里的 `?key=...`、Bearer token 之类不能跟着进日志、报告和导出包。

为什么放在一个地方：错误信息会流到日志、任务步骤详情、证据账本、星空导出包好几处，
每处各自打码迟早漏一处。统一在数据源调用出口（connectors）用这里的 redact 一次。
"""
from __future__ import annotations

import re

_SENSITIVE = r"(?:api[_-]?key|apikey|access[_-]?key|secret|token|password|passwd|auth|sign|signature|key)"
# URL query：敏感参数名的值一律打码；其他参数值很长（≥16）的也打码（多半是令牌）
_QS = re.compile(r"([?&])([^=&#\s]+)=([^&#\s'\"<>)]+)")
# 文本里的 `api_key: xxx` / `token=xxx` / `--api-key xxx`
_KV = re.compile(rf"(?i)(\b{_SENSITIVE}\b[\"']?\s*[:=]\s*[\"']?|--{_SENSITIVE}[\s=]+)([^\s,;'\"&<>]{{6,}})")
_BEARER = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-~+/=]{8,}")
_USERINFO = re.compile(r"(https?://)[^/\s:@]+:[^/\s@]+@")


_NAME_PARTS = ("apikey", "api_key", "api-key", "access_key", "accesskey", "secret", "token",
               "password", "passwd", "signature", "authorization")


def _sensitive_name(name: str) -> bool:
    n = name.lower()
    # 注意别把 keyword= 当成 key= 打掉：那是排查时最需要看到的参数
    return n in {"key", "sign", "auth", "sk", "ak"} or n.endswith(("_key", "-key")) \
        or any(p in n for p in _NAME_PARTS)


def _mask_qs(m: "re.Match[str]") -> str:
    sep, name, value = m.group(1), m.group(2), m.group(3)
    if _sensitive_name(name) or len(value) >= 24:
        return f"{sep}{name}=***"
    return m.group(0)


def redact(text: object) -> str:
    """把文本里看起来像凭据的部分换成 ***，其余原样保留，便于排查。"""
    s = "" if text is None else str(text)
    if not s:
        return s
    s = _USERINFO.sub(r"\1***@", s)
    s = _QS.sub(_mask_qs, s)
    s = _BEARER.sub(r"\1 ***", s)
    s = _KV.sub(lambda m: m.group(1) + "***", s)
    return s
