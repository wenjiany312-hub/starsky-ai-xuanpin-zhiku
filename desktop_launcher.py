# -*- coding: utf-8 -*-
"""AI选品智库 本机启动器（macOS / Windows 通用）。

界面入口：
  AI选品智库.app               macOS，由 make_mac_app.py 从仓库生成
  启动AI选品智库.bat           Windows

命令行：
  python desktop_launcher.py                    启动后端并打开浏览器
  python desktop_launcher.py --status           看服务是否在跑
  python desktop_launcher.py --stop             停掉由本启动器拉起的后端
  python desktop_launcher.py --create-shortcut  Windows 桌面快捷方式
  python desktop_launcher.py --make-app --install  macOS 生成并安装 .app

只用标准库，不依赖后端依赖；优先用 backend/.venv 里的解释器起服务。
"""
import argparse
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = ROOT_DIR / "backend"
LOG_DIR = ROOT_DIR / "data_storage" / "logs"
LOG_PATH = LOG_DIR / "server.log"
PID_PATH = LOG_DIR / "launcher.pid"
ICON_PATH = ROOT_DIR / "assets" / "app_icon.ico"

PORT = 8006
BASE_URL = f"http://127.0.0.1:{PORT}"
HEALTH_URL = f"{BASE_URL}/health"
START_TIMEOUT = 40.0


def _say(msg: str = "") -> None:
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:  # Windows 控制台非 UTF-8 时的兜底
        print(msg.encode("utf-8", "replace").decode("utf-8", "replace"), flush=True)


def backend_python() -> str:
    """优先用后端自己的虚拟环境，避免系统 Python 缺依赖。"""
    candidates = [
        BACKEND_DIR / ".venv" / "bin" / "python",
        BACKEND_DIR / ".venv" / "Scripts" / "python.exe",
    ]
    for path in candidates:
        if path.is_file():
            return str(path)
    return sys.executable


REQ_PATH = BACKEND_DIR / "requirements.txt"
VENV_DIR = BACKEND_DIR / ".venv"
DIST_INDEX = ROOT_DIR / "frontend" / "dist" / "index.html"
PIP_MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"


def _req_hash() -> str:
    import hashlib
    return hashlib.sha256(REQ_PATH.read_bytes()).hexdigest()[:16] if REQ_PATH.exists() else ""


def ensure_env() -> bool:
    """首次启动 / 更新后自动装好后端依赖。拿到软件的人不该需要懂 venv 和 pip。

    依赖清单变了（git pull 更新后）会自动补装；国内网络装不动时自动换清华镜像重试。
    """
    if sys.version_info < (3, 10):
        _say(f"[ERR] 需要 Python 3.10 或更新版本，当前是 {sys.version.split()[0]}。")
        _say("      macOS：到 python.org 下载安装；Windows：安装时勾选「Add python.exe to PATH」。")
        return False
    if not DIST_INDEX.is_file():
        _say("[ERR] 界面文件缺失（frontend/dist）。请重新下载完整安装包，不要只下源码。")
        return False
    py_venv = Path(backend_python())
    stamp = VENV_DIR / ".requirements.sha"
    fresh = not str(py_venv).startswith(str(VENV_DIR))
    if not fresh and stamp.exists() and stamp.read_text().strip() == _req_hash():
        return True

    if fresh:
        _say("[准备] 第一次启动：正在创建运行环境（只需一次，约 1–3 分钟）…")
        r = subprocess.run([sys.executable, "-m", "venv", str(VENV_DIR)])
        if r.returncode != 0:
            _say("[ERR] 创建运行环境失败。Linux 可能需要先装 python3-venv。")
            return False
        py_venv = Path(backend_python())
    else:
        _say("[准备] 检测到更新，正在补装依赖…")

    base = [str(py_venv), "-m", "pip", "install", "--disable-pip-version-check", "-q", "-r", str(REQ_PATH)]
    for extra, note in (([], "默认源"), (["-i", PIP_MIRROR], "清华镜像")):
        _say(f"[准备] 安装依赖（{note}）…")
        if subprocess.run(base + extra).returncode == 0:
            stamp.write_text(_req_hash())
            _say("[OK] 运行环境就绪。")
            return True
    _say("[ERR] 依赖没装上，多半是网络问题。检查网络后重新双击启动即可（会接着装）。")
    return False


def is_healthy(timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=timeout) as resp:
            return b"healthy" in resp.read(256)
    except Exception:
        return False


def is_port_in_use(port: int = PORT) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        return sock.connect_ex(("127.0.0.1", port)) == 0


def read_pid() -> int | None:
    try:
        return int(PID_PATH.read_text().strip())
    except Exception:
        return None


def _run(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout or ""
    except Exception:
        return ""


def _listening_pids(port: int = PORT) -> list[int]:
    """占用端口的 pid（POSIX 用 lsof，Windows 走 netstat）。"""
    if os.name == "nt":
        pids = []
        for line in _run(["netstat", "-ano", "-p", "TCP"]).splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[1].endswith(f":{port}") and parts[3] == "LISTENING":
                if parts[4].isdigit():
                    pids.append(int(parts[4]))
        return pids
    out = _run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"])
    return [int(x) for x in out.split() if x.isdigit()]


def _is_our_backend(pid: int) -> bool:
    """命令行是 uvicorn main:app，且工作目录就是本仓库的 backend。"""
    if pid <= 0 or not pid_alive(pid):
        return False
    if os.name == "nt":
        return True  # Windows 上 pid 来自端口占用，已由启动器独占端口
    cmdline = _run(["ps", "-p", str(pid), "-o", "command="])
    if "uvicorn" not in cmdline or "main:app" not in cmdline:
        return False
    cwd = _run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"])
    cwd = next((line[1:] for line in cwd.splitlines() if line.startswith("n")), "")
    return bool(cwd) and Path(cwd).resolve() == BACKEND_DIR.resolve()


def _adopt_pid() -> int | None:
    """pid 文件丢了（手起 / 旧版启动器）时，从端口反查并确认是自己的后端。"""
    for pid in _listening_pids():
        if _is_our_backend(pid):
            PID_PATH.parent.mkdir(parents=True, exist_ok=True)
            PID_PATH.write_text(str(pid))
            return pid
    return None


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                             capture_output=True, text=True)
        return str(pid) in (out.stdout or "")
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def start() -> bool:
    """拉起后端（已在跑就复用），成功返回 True。"""
    if is_healthy():
        if read_pid() is None:
            _adopt_pid()  # 记下 pid，--stop 才找得到
        _say(f"[1/2] 服务已在 {BASE_URL} 运行，直接复用。")
        return True
    if is_port_in_use():
        _say(f"[ERR] 端口 {PORT} 被别的进程占用了，但不是本工具的后端。")
        _say(f"      先确认占用者：lsof -nP -iTCP:{PORT} -sTCP:LISTEN")
        return False
    if not ensure_env():
        return False

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    py = backend_python()
    _say(f"[1/2] 启动后端（端口 {PORT}）…  {py}")
    log = open(LOG_PATH, "ab")
    kwargs: dict = dict(cwd=str(BACKEND_DIR), stdin=subprocess.DEVNULL,
                        stdout=log, stderr=subprocess.STDOUT)
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(
        [py, "-m", "uvicorn", "main:app", "--host", "127.0.0.1",
         "--port", str(PORT), "--log-level", "warning"],
        **kwargs,
    )
    log.close()
    PID_PATH.write_text(str(proc.pid))

    deadline = time.time() + START_TIMEOUT
    while time.time() < deadline:
        if is_healthy(timeout=1.0):
            _say(f"[OK] 后端就绪（{time.time() - (deadline - START_TIMEOUT):.1f}s）。")
            return True
        if proc.poll() is not None:
            _say(f"[ERR] 后端进程已退出（code={proc.returncode}），日志：{LOG_PATH}")
            return False
        time.sleep(0.5)
    _say(f"[ERR] 等待 {START_TIMEOUT:.0f}s 仍未就绪，日志：{LOG_PATH}")
    return False


def stop() -> bool:
    pid = read_pid()
    if pid is None or not pid_alive(pid):
        PID_PATH.unlink(missing_ok=True)
        pid = _adopt_pid()
    if pid is None:
        if is_port_in_use():
            _say(f"[ERR] {PORT} 端口上的服务不是本启动器拉起的，没敢动它。")
            _say(f"      需要手停的话：lsof -nP -iTCP:{PORT} -sTCP:LISTEN")
        else:
            _say("[OK] 服务本来就没在跑。")
        return False

    _say(f"停止后端（pid {pid}）…")
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        else:
            os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        _say(f"[ERR] 发停止信号失败：{exc}")
        return False

    for _ in range(20):
        if not is_port_in_use() and not pid_alive(pid):
            PID_PATH.unlink(missing_ok=True)
            _say("[OK] 已停止。")
            return True
        time.sleep(0.25)
    _say(f"[WARN] 进程 {pid} 还没退干净，可以再等一下或手动 kill。")
    return False


def status() -> None:
    state = "运行中" if is_healthy() else ("端口被占用但健康检查不通过" if is_port_in_use() else "未运行")
    pid = read_pid()
    _say(f"服务状态：{state}")
    _say(f"地址：    {BASE_URL}")
    if pid:
        _say(f"启动器记录的 pid：{pid}（{'存活' if pid_alive(pid) else '已退出'}）")
    _say(f"日志：    {LOG_PATH}")


def get_desktop_dir() -> Path:
    """真实桌面路径（兼容 OneDrive 备份桌面）。"""
    if os.name == "nt":
        try:
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders")
            desktop_val, _ = winreg.QueryValueEx(key, "Desktop")
            winreg.CloseKey(key)
            expanded = Path(os.path.expandvars(desktop_val))
            if expanded.exists():
                return expanded
        except Exception:
            pass
        for candidate in (Path.home() / "OneDrive" / "Desktop", Path.home() / "Desktop"):
            if candidate.exists():
                return candidate
    else:
        desktop = Path.home() / "Desktop"
        if desktop.exists():
            return desktop
    return Path.home()


def create_desktop_shortcut() -> bool:
    """Windows 桌面快捷方式；macOS 走 make_mac_app.py。"""
    if os.name != "nt":
        _say("macOS 请用：python desktop_launcher.py --make-app --install")
        return False
    desktop = get_desktop_dir()
    shortcut_path = desktop / "AI亚马逊选品智库.lnk"
    bat_target = ROOT_DIR / "启动AI选品智库.bat"
    ps_cmd = f"""
    $WshShell = New-Object -ComObject WScript.Shell
    $Shortcut = $WshShell.CreateShortcut('{shortcut_path}')
    $Shortcut.TargetPath = '{bat_target}'
    $Shortcut.WorkingDirectory = '{ROOT_DIR}'
    $Shortcut.IconLocation = '{ICON_PATH}'
    $Shortcut.Description = 'AI 亚马逊选品智库'
    $Shortcut.Save()
    """
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps_cmd],
                       check=True, capture_output=True)
        _say(f"[OK] 桌面快捷方式已创建：{shortcut_path}")
        return True
    except Exception as exc:
        _say(f"[ERR] 创建快捷方式失败：{exc}")
        return False


def launch_services(open_browser: bool = True) -> None:
    _say("=" * 58)
    _say("  AI 亚马逊选品智库")
    _say("=" * 58)
    if not start():
        _say("启动失败，没有打开浏览器。")
        sys.exit(1)
    _say(f"[2/2] 打开 {BASE_URL}/")
    if open_browser:
        webbrowser.open(f"{BASE_URL}/")
    _say("-" * 58)
    _say("  方法论包 · 多数据源取数 · 证据账本 · 一键送往星空 S1")
    _say(f"  日志：{LOG_PATH}")
    _say(f"  停止：python {Path(__file__).name} --stop")
    _say("=" * 58)


def main() -> None:
    parser = argparse.ArgumentParser(description="AI选品智库 启动器")
    parser.add_argument("--stop", action="store_true", help="停止后端")
    parser.add_argument("--status", action="store_true", help="查看服务状态")
    parser.add_argument("--create-shortcut", action="store_true", help="Windows 桌面快捷方式")
    parser.add_argument("--make-app", action="store_true", help="macOS 生成 .app")
    parser.add_argument("--install", action="store_true", help="配合 --make-app：装到桌面")
    parser.add_argument("--no-browser", action="store_true", help="只起服务，不开浏览器")
    args = parser.parse_args()

    if args.stop:
        sys.exit(0 if stop() else 1)
    if args.status:
        status()
        return
    if args.make_app:
        from make_mac_app import build, install  # 同目录脚本
        path = build()
        _say(f"[OK] 已生成：{path}")
        if args.install:
            _say(f"[OK] 已安装到桌面：{install(path)}")
        return
    if args.create_shortcut:
        sys.exit(0 if create_desktop_shortcut() else 1)
    launch_services(open_browser=not args.no_browser)


if __name__ == "__main__":
    main()
