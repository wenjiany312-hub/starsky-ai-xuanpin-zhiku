"""离线授权码：到期日 + 设备绑定，自己给自己发码，不需要联网、不需要服务器。

授权码形如 ``XZ1.<base64url 载荷>.<base64url 签名>``：
- 载荷是一小段 JSON（版本 / 套餐 / 客户名 / 到期日 / 绑定设备码）；
- 签名用 RSA-2048 + SHA-256，私钥只在卖家的电脑上（见 tools/license/），
  公钥（模数）编在这里，所以买家手上只有公钥，**伪造不出**新授权码；
- 签名覆盖的是 ``XZ1.<载荷>`` 这段字面量本身，不做 JSON 规范化，改一个字符就失效。

到期后只读：拦住的只是「发起运行 / 让模型干活」这类入口，历史记录、报告、
证据、下载全部照常可看——用户花钱买过的数据不能被锁住。

试用：第一次打开软件自动起算，天数与次数先到先得，默认 7 天 / 3 次。
试用状态记在用户主目录（不是软件目录），换一个软件文件夹不会重置。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import platform
import re
import subprocess
import sys
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from fastapi import HTTPException

from app.core.jsonio import read_json, write_json
from app.core.logger import logger

# ---------------- 发行方密钥（公钥） ----------------
# 模数来自发行方私有密钥（~/.aixp-license-signing/private.pem），私钥不入库、不随软件分发。
PUBLIC_MODULUS_HEX = (
    "BCD3133403B2243A6F361598D3AB6893AA3E2420869F37DE91864DD0B21F646181"
    "B6BCAA178DE5C648B9E1F726A13AA35C944DCE0B880E0DDD2719C7A295BB4CB7"
    "E75D481CB19BDF838F687C26E051FE07BA6C6CEC5017DF1E0603F8E6E33DD7D"
    "7C21B722F1E38FF849D110044BF3CC38DB3DEB24F66F52CD9FF4E09DDF582D6"
    "9CA0D6C40F489C237CCFBB89FC3EF2DEB2FB1067AA0E6575A8FBB2FF51CFC29D"
    "46F8B2FFE9411EFDE2BFC7B016D41032457EEAC07E94F8AB3679EBF74C39F6D"
    "F756EABBBD6A7CB1D6B9536443B7BD9A85E42751A6A1CA9A9ED3E34914C6A8F5"
    "1229941F92B0DAB374BE537190651B0F9169DB9A1E6DB2FC9558729189908A7CD"
)
PUBLIC_EXPONENT = 65537

KEY_PREFIX = "XZ1"
TRIAL_DAYS = int(os.environ.get("AIXP_TRIAL_DAYS") or 7)
TRIAL_RUNS = int(os.environ.get("AIXP_TRIAL_RUNS") or 3)

_DIGEST_INFO_SHA256 = bytes.fromhex("3031300d060960864801650304020105000420")
_N = int(PUBLIC_MODULUS_HEX, 16)
_K = (len(PUBLIC_MODULUS_HEX) + 1) // 2  # 签名长度 256 字节

_device_cache: Optional[str] = None


# ---------------- 目录与文件 ----------------

def license_dir() -> Path:
    """授权与试用状态放用户主目录：换一个软件文件夹、重装到别处都不会重置试用。"""
    raw = os.environ.get("AIXP_LICENSE_DIR")
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".aixp-license"


def _license_file() -> Path:
    return license_dir() / "license.json"


def _trial_file() -> Path:
    return license_dir() / "trial.json"


# ---------------- 设备码 ----------------

def _raw_machine_id() -> str:
    try:
        if sys.platform == "darwin":
            out = subprocess.run(
                ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                capture_output=True, text=True, timeout=5,
            ).stdout
            m = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', out)
            if m:
                return "mac:" + m.group(1)
        elif os.name == "nt":
            import winreg  # noqa: PLC0415  只在 Windows 上用

            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r"SOFTWARE\Microsoft\Cryptography") as key:
                return "win:" + str(winreg.QueryValueEx(key, "MachineGuid")[0])
        else:
            p = Path("/etc/machine-id")
            if p.is_file():
                text = p.read_text(encoding="utf-8").strip()
                if text:
                    return "nix:" + text
    except Exception:  # noqa: BLE001  取系统标识失败就退回下面的通用方案，不能让软件打不开
        pass
    # 兜底：主机名 + 网卡地址。换了网卡或改了主机名会变，届时重新发一次码即可。
    return "fallback:" + platform.node() + ":" + str(uuid.getnode())


def device_code() -> str:
    """稳定、可读的本机设备码（哈希后只取 12 位，看不出原始机器信息）。"""
    global _device_cache
    if _device_cache:
        return _device_cache
    digest = hashlib.sha256(("aixp-license-2026|" + _raw_machine_id()).encode("utf-8")).hexdigest().upper()
    h = digest[:12]
    _device_cache = f"{h[0:4]}-{h[4:8]}-{h[8:12]}"
    return _device_cache


def normalize_device(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z]", "", text or "").upper()


# ---------------- 码的解析与验签 ----------------

def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def rsa_verify(message: bytes, signature: bytes) -> bool:
    """PKCS#1 v1.5 + SHA-256 验签（纯 Python，买家不用另装密码学库）。"""
    if len(signature) != _K:
        return False
    s = int.from_bytes(signature, "big")
    if s >= _N:
        return False
    em = pow(s, PUBLIC_EXPONENT, _N).to_bytes(_K, "big")
    digest = hashlib.sha256(message).digest()
    tail = _DIGEST_INFO_SHA256 + digest
    expected = b"\x00\x01" + b"\xff" * (_K - 3 - len(tail)) + b"\x00" + tail
    return hmac.compare_digest(em, expected)


def decode_key(text: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """验签并解析授权码。返回 (载荷, 错误说明)，成功时错误说明为空串。"""
    raw = re.sub(r"\s+", "", text or "")
    if not raw:
        return None, "请先粘贴授权码"
    parts = raw.split(".")
    if len(parts) != 3 or parts[0] != KEY_PREFIX:
        return None, "授权码格式不对（应以 XZ1. 开头，共三段，用点隔开）"
    try:
        payload_bytes = _b64d(parts[1])
        signature = _b64d(parts[2])
    except Exception:  # noqa: BLE001
        return None, "授权码里有无法识别的字符，请整段复制，别手工改"
    if not rsa_verify(f"{KEY_PREFIX}.{parts[1]}".encode("ascii"), signature):
        return None, "授权码校验不通过：可能抄漏了字符，或这不是本软件的授权码"
    try:
        data = json.loads(payload_bytes.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None, "授权码内容读不出来，请让发码人重新生成"
    if not isinstance(data, dict) or int(data.get("v") or 0) != 1:
        return None, "授权码版本不认识，请升级软件或让发码人重新生成"
    try:
        date.fromisoformat(str(data.get("e") or ""))
    except ValueError:
        return None, "授权码里的到期日不合法，请让发码人重新生成"
    return data, ""


# ---------------- 状态 ----------------

def _today() -> date:
    return date.today()


def _read_license() -> Optional[str]:
    data = read_json(_license_file(), default={}, strict=False)
    if not isinstance(data, dict):
        return None
    key = str(data.get("key") or "").strip()
    return key or None


def _read_trial(create: bool = False) -> Dict[str, Any]:
    path = _trial_file()
    data = read_json(path, default={}, strict=False)
    if not isinstance(data, dict) or not data.get("started_at"):
        data = {"started_at": datetime.now().isoformat(timespec="seconds"), "runs_used": 0}
        if create:
            try:
                write_json(path, data, private=True)
            except OSError as exc:
                logger.warning("试用状态写不进去（%s），本次按新试用处理", exc)
    if not isinstance(data.get("runs_used"), int):
        data["runs_used"] = 0
    return data


def _touch_trial(data: Dict[str, Any]) -> None:
    """每天首次打开时记一笔最近日期，用来发现「把系统时间调回去续试用」。"""
    today = _today().isoformat()
    if data.get("last_seen") == today:
        return
    data["last_seen"] = today
    try:
        write_json(_trial_file(), data, private=True)
    except OSError as exc:
        logger.warning("试用状态写不进去：%s", exc)


def _licensed_status(key: str) -> Dict[str, Any]:
    payload, error = decode_key(key)
    if payload is None:
        return {"state": "invalid", "reason": error}
    bound = normalize_device(str(payload.get("d") or ""))
    if bound and bound != normalize_device(device_code()):
        return {"state": "invalid",
                "reason": f"这个授权码绑定的是另一台电脑（{payload.get('d')}），"
                          f"本机设备码是 {device_code()}。换机请找发码人重新发码"}
    exp = date.fromisoformat(str(payload["e"]))
    days_left = (exp - _today()).days
    if days_left < 0:
        return {"state": "expired", "plan": payload.get("p"), "licensee": payload.get("n"),
                "expires_on": exp.isoformat(), "days_left": 0,
                "reason": f"授权已于 {exp.isoformat()} 到期"}
    return {"state": "licensed", "plan": str(payload.get("p") or "pro"),
            "licensee": str(payload.get("n") or ""), "expires_on": exp.isoformat(),
            "days_left": days_left, "reason": ""}


def _trial_status() -> Dict[str, Any]:
    data = _read_trial(create=True)
    try:
        started = datetime.fromisoformat(str(data["started_at"])).date()
    except ValueError:
        started = _today()
        data["started_at"] = datetime.now().isoformat(timespec="seconds")
    days_left = (started + timedelta(days=TRIAL_DAYS) - _today()).days
    runs_left = max(0, TRIAL_RUNS - int(data.get("runs_used") or 0))
    reason = ""
    if days_left <= 0:
        reason = f"{TRIAL_DAYS} 天试用期已结束"
    elif runs_left <= 0:
        reason = f"{TRIAL_RUNS} 次试用运行已用完"
    last_seen = str(data.get("last_seen") or "")
    if last_seen:
        try:
            if _today() < date.fromisoformat(last_seen) - timedelta(days=1):
                days_left, runs_left = 0, 0
                reason = "检测到系统日期被调回，试用已停止"
        except ValueError:
            pass
    _touch_trial(data)
    return {"days_left": max(0, days_left), "runs_left": runs_left,
            "days": TRIAL_DAYS, "runs": TRIAL_RUNS,
            "started_at": str(data.get("started_at") or ""), "reason": reason}


def status() -> Dict[str, Any]:
    """给接口和前端看的完整状态；这个函数本身永远不抛错。"""
    out: Dict[str, Any] = {
        "state": "trial", "can_run": True, "plan": None, "licensee": None,
        "expires_on": None, "days_left": None, "reason": "",
        "device": device_code(), "checked_at": datetime.now().isoformat(timespec="seconds"),
        "trial": None, "licensed_key_tail": None,
    }
    try:
        key = _read_license()
        licensed = _licensed_status(key) if key else None
        out["trial"] = _trial_status()
        if licensed:
            out.update({k: v for k, v in licensed.items() if k != "reason"})
            out["reason"] = licensed.get("reason", "")
            out["can_run"] = licensed["state"] == "licensed"
            out["licensed_key_tail"] = "…" + key[-4:] if key else None
            return out
        out["state"] = "trial" if not out["trial"]["reason"] else "expired"
        out["can_run"] = not out["trial"]["reason"]
        out["reason"] = out["trial"]["reason"]
        out["days_left"] = out["trial"]["days_left"]
        return out
    except Exception as exc:  # noqa: BLE001  授权状态坏了也不能让软件打不开
        logger.error("读取授权状态失败：%s", exc)
        out.update({"state": "trial", "can_run": True, "reason": ""})
        return out


def require_active(action: str = "这项操作") -> Dict[str, Any]:
    """消耗数据额度 / 调用模型的入口统一走这里；到期后抛 402，历史数据不受影响。"""
    st = status()
    if st["can_run"]:
        return st
    raise HTTPException(
        status_code=402,
        detail=(f"{action}需要有效授权：{st['reason']}。"
                f"请在左侧「授权」里输入授权码，或找发码人续期。"
                f"已经跑出来的记录和报告不受影响，仍可查看、导出。"),
    )


def note_run() -> None:
    """成功发起一次选品运行后调用：试用期扣一次次数；已授权的账号不计数。"""
    if status()["state"] != "trial":
        return
    data = _read_trial(create=True)
    data["runs_used"] = int(data.get("runs_used") or 0) + 1
    data["last_run_at"] = datetime.now().isoformat(timespec="seconds")
    try:
        write_json(_trial_file(), data, private=True)
    except OSError as exc:
        logger.warning("试用次数没记上：%s", exc)


def activate(text: str) -> Dict[str, Any]:
    """校验并保存授权码。失败时抛 400（把原因原样给用户），成功返回新状态。"""
    payload, error = decode_key(text)
    if payload is None:
        raise HTTPException(status_code=400, detail=error)
    bound = normalize_device(str(payload.get("d") or ""))
    if bound and bound != normalize_device(device_code()):
        raise HTTPException(
            status_code=400,
            detail=(f"这个授权码绑定的是另一台电脑（{payload.get('d')}），本机设备码是 "
                    f"{device_code()}。请把本机设备码发给发码人换一个码。"),
        )
    exp = date.fromisoformat(str(payload["e"]))
    if (exp - _today()).days < 0:
        raise HTTPException(status_code=400,
                            detail=f"这个授权码已于 {exp.isoformat()} 到期，请找发码人续期")
    key = re.sub(r"\s+", "", text or "")
    write_json(_license_file(), {
        "key": key,
        "activated_at": datetime.now().isoformat(timespec="seconds"),
        "licensee": str(payload.get("n") or ""),
        "expires_on": exp.isoformat(),
    }, private=True)
    logger.info("授权已激活：%s（到期 %s）", payload.get("n") or "未署名", exp.isoformat())
    return status()


def deactivate() -> Dict[str, Any]:
    """清掉本机授权码（换码 / 退机时用）。"""
    path = _license_file()
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"删除授权文件失败：{exc}") from exc
    return status()


def trial_meta() -> Dict[str, Any]:
    return {"days": TRIAL_DAYS, "runs": TRIAL_RUNS}
