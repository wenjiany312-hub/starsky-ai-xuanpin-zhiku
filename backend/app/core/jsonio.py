"""
JSON 文件读写工具：原子写（临时文件 + rename）+ 进程内按路径加锁。
所有落盘的 JSON 统一走这里，避免写到一半崩溃导致文件损坏。
"""
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict

_locks: Dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


def lock_for(path: Path) -> threading.RLock:
    key = str(Path(path).resolve())
    with _locks_guard:
        if key not in _locks:
            _locks[key] = threading.RLock()
        return _locks[key]


def _bak(path: Path) -> Path:
    return path.with_name(path.name + ".bak")


def read_json(path: Path, default: Any = None, strict: bool = False) -> Any:
    """读 JSON。文件坏了不能当成空：调用方拿到 {} 再写回去，整份历史就没了。

    - 解析失败：把坏文件改名留档（*.corrupt-时间戳），改读上一版 .bak；
    - strict=True 时，读不出来（磁盘/权限错误或没有可用备份）直接抛错，宁可报错也不覆盖。
    """
    path = Path(path)
    bak = _bak(path)
    with lock_for(path):
        if not path.exists():
            if bak.exists():  # 两次 rename 之间崩溃：主文件不在，备份还在
                try:
                    return json.loads(bak.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    pass
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except OSError:
            if strict:
                raise
            return default
        except json.JSONDecodeError as exc:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            try:
                os.replace(path, path.with_name(f"{path.name}.corrupt-{stamp}"))
            except OSError:
                pass
            if bak.exists():
                try:
                    return json.loads(bak.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    pass
            if strict:
                raise ValueError(f"{path.name} 已损坏且没有可用备份（坏文件已留档）：{exc}") from exc
            return default


def write_json(path: Path, data: Any, indent: int = 1, private: bool = False) -> None:
    """原子写 JSON，并把上一版留成 .bak；private=True 时文件权限收紧为 0600（含密钥的配置）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=indent, default=str)
    with lock_for(path):
        fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            if private:
                os.chmod(tmp, 0o600)
            if path.exists():
                os.replace(path, _bak(path))
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def write_text(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with lock_for(path):
        fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
