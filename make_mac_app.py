# -*- coding: utf-8 -*-
"""从仓库生成 macOS 桌面启动器：AI选品智库.app（可复现，不入库）。

    backend/.venv/bin/python make_mac_app.py            生成到 dist/AI选品智库.app
    backend/.venv/bin/python make_mac_app.py --install  再覆盖安装到 ~/Desktop

生成物（AppleScript 小程序 + 一段启动脚本）：
  Contents/MacOS/applet                    每次双击都跑一遍，跑完立刻退出（Dock 里不挂图标）
  Contents/Resources/server.sh             真正干活：没起就起、等 /health、打开页面
  Contents/Resources/app_icon.icns         与网页 favicon 同一套图形
安装时额外在桌面放一个「停止AI选品智库.command」。

为什么是 AppleScript 小程序 + `do shell script`：
  从 Dock/访达双击时，应用直接拉起的子进程属于这个应用的进程组，应用一退出系统就把整组
  收掉（setsid/execv 也逃不掉），表现是「健康检查刚过、页面马上打不开」。
  `do shell script "... &"` 交给 AppleScript 自己的进程去执行，后端因此不在本应用的
  进程组里——应用退出、后端照常活着。每次双击重新跑一遍也让「已开着再双击」自然变成
  「只把页面顶出来」，不需要常驻应用。

  但应用不能「一秒就退」：项目在移动硬盘上，系统第一次会弹「想访问可移除卷宗」的授权框，
  应用要是已经退出，这个框没有归属、点了也没人接，读文件就永久卡在 open() 上，
  表现成「后端启动超时」。所以 applet 会一直等到后端健康（最多 75 秒）再退。
  启动脚本里另有一次 10 秒的探测：读不到项目文件就当权限问题，立刻给出处理办法。

图标用仓库里的 PIL 画，不依赖外部设计文件；PIL 缺失时回退 assets/app_icon_mac.png。
"""
import argparse
import plistlib
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
DIST_DIR = ROOT_DIR / "dist"
APP_NAME = "AI选品智库"
BUNDLE_ID = "com.aixuanpin.zhiku"
VERSION = "3.0.0"
FALLBACK_ICON = ROOT_DIR / "assets" / "app_icon_mac.png"

# 与 frontend/public/favicon.svg 同一套比例与配色
INK = (20, 25, 32)
INK_TOP = (29, 36, 46)
INK_BOTTOM = (13, 17, 23)
AMBER = (238, 150, 42)
PAPER = (244, 242, 237)
TEAL = (43, 179, 163)


def _render_icon(png: Path, size: int = 1024) -> Path | None:
    try:
        from PIL import Image, ImageDraw, ImageFilter
    except ImportError:
        if FALLBACK_ICON.is_file():
            shutil.copyfile(FALLBACK_ICON, png)
            print(f"[WARN] 没有 PIL，图标回退到 {FALLBACK_ICON.name}")
            return png
        return None

    ss = 3  # 超采样，曲线更干净
    S = size * ss
    # macOS 图标规范：圆角方块约占画布 80%，四周留白 + 投影，Dock 里才不会显得比别人大
    pad = S * 0.098
    sq = S - 2 * pad
    radius = int(sq * 0.225)

    def X(v: float) -> float:  # 图形坐标按方块内部的 0–1 比例换算
        return pad + v * sq

    # 1. 投影
    shadow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        [X(0), X(0) + S * 0.014, X(1), X(1) + S * 0.014],
        radius=radius, fill=(0, 0, 0, 80))
    shadow = shadow.filter(ImageFilter.GaussianBlur(int(S * 0.022)))
    canvas = Image.alpha_composite(Image.new("RGBA", (S, S), (0, 0, 0, 0)), shadow)

    # 2. 深墨底：方块内竖向渐变
    sq_i = int(sq) + 1
    bg = Image.new("RGBA", (sq_i, sq_i), (0, 0, 0, 0))
    bd = ImageDraw.Draw(bg)
    for y in range(sq_i):
        t = y / max(1, sq_i - 1)
        rgb = tuple(int(INK_TOP[i] + (INK_BOTTOM[i] - INK_TOP[i]) * t) for i in range(3))
        bd.line([(0, y), (sq_i, y)], fill=rgb + (255,))
    bg_mask = Image.new("L", (sq_i, sq_i), 0)
    ImageDraw.Draw(bg_mask).rounded_rectangle([0, 0, sq_i - 1, sq_i - 1],
                                              radius=radius, fill=255)
    bg.putalpha(bg_mask)
    canvas.alpha_composite(bg, (int(pad), int(pad)))

    # 3. 镜片后的暖光
    glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse([X(0.16), X(0.12), X(0.76), X(0.72)], fill=AMBER + (42,))
    glow = glow.filter(ImageFilter.GaussianBlur(int(S * 0.06)))
    clip = Image.new("L", (S, S), 0)
    ImageDraw.Draw(clip).rounded_rectangle([X(0), X(0), X(1), X(1)], radius=radius, fill=255)
    glow.putalpha(Image.composite(glow.getchannel("A"), Image.new("L", (S, S), 0), clip))
    canvas = Image.alpha_composite(canvas, glow)
    d = ImageDraw.Draw(canvas)

    # 4. 放大镜
    cx, cy, r, ring = X(0.4375), X(0.4375), sq * 0.234, sq * 0.078
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=AMBER + (255,), width=int(ring))

    hx1, hy1 = X(0.61), X(0.61)
    hx2, hy2 = X(0.781), X(0.781)
    hw = int(sq * 0.094)
    d.line([(hx1, hy1), (hx2, hy2)], fill=AMBER + (255,), width=hw)
    for hx, hy in ((hx1, hy1), (hx2, hy2)):
        d.ellipse([hx - hw / 2, hy - hw / 2, hx + hw / 2, hy + hw / 2], fill=AMBER + (255,))

    # 5. 镜片里的三根柱（两根纸白、一根青，最后一根是“机会”）
    bw = sq * 0.0625
    base = X(0.5625)
    bars = [((0.3125, 0.4531), PAPER), ((0.4063, 0.3906), PAPER), ((0.5000, 0.3125), TEAL)]
    for (bx, by), color in bars:
        x0 = X(bx)
        y0 = X(by)
        d.rounded_rectangle([x0, y0, x0 + bw, base], radius=int(bw * 0.28), fill=color + (255,))

    # 6. 回贴一次蒙版，确保边缘干净（ImageDraw 不混合 alpha，描边一律走独立图层）
    out = canvas.resize((size, size), Image.LANCZOS)
    out.save(png, format="PNG")
    return png


def _make_icns(png: Path, icns: Path) -> Path:
    iconset = png.parent / "app_icon.iconset"
    if iconset.exists():
        shutil.rmtree(iconset)
    iconset.mkdir(parents=True)
    variants = [
        (16, "icon_16x16.png"), (32, "icon_16x16@2x.png"),
        (32, "icon_32x32.png"), (64, "icon_32x32@2x.png"),
        (128, "icon_128x128.png"), (256, "icon_128x128@2x.png"),
        (256, "icon_256x256.png"), (512, "icon_256x256@2x.png"),
        (512, "icon_512x512.png"), (1024, "icon_512x512@2x.png"),
    ]
    for px, name in variants:
        subprocess.run(["sips", "-z", str(px), str(px), str(png), "--out", str(iconset / name)],
                       check=True, capture_output=True)
    subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(icns)],
                   check=True, capture_output=True)
    shutil.rmtree(iconset)
    return icns


def _apple_script() -> str:
    """每次双击都跑一遍 server.sh：没起就起，起好了就只把页面顶出来。

    刻意不编译成常驻（osacompile -s）：这里必须走 `do shell script`，它会通过
    AppleScript 自己的进程去执行，后端因此脱离本应用的进程组——应用退出后后端照常活着，
    Dock 里也不会挂着一个空空的应用图标。

    但本应用要活到后端健康为止：系统第一次会为「读移动硬盘」弹授权框，应用退出这个框就
    没人接，读文件永远卡住。健康检查通不过也会在 75 秒后自动退出，不会一直挂着。
    """
    return """
on run
	set base to POSIX path of (path to me)
	if base does not end with "/" then set base to base & "/"
	set launcherPath to base & "Contents/Resources/server.sh"
	try
		do shell script "/bin/bash " & quoted form of launcherPath & " start > /dev/null 2>&1 &"
	end try
	-- 陪着后端起来：权限框需要本应用还在，健康了立刻退出（正常 5 秒内）
	set deadline to (current date) + 75
	repeat
		try
			do shell script "/usr/bin/curl -s -m 2 http://127.0.0.1:8006/health | /usr/bin/grep -q healthy"
			exit repeat
		end try
		if (current date) > deadline then exit repeat
		delay 1
	end repeat
end run
"""


def _info_plist_overrides() -> dict:
    """osacompile 生成的 applet 默认叫 applet、用系统图标，这里补齐成我们自己的。"""
    return {
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleIconFile": "app_icon",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "LSMinimumSystemVersion": "12.0",
        "NSHighResolutionCapable": True,
        "NSHumanReadableCopyright": "AI选品智库",
    }


def _launcher_script() -> str:
    root = shlex.quote(str(ROOT_DIR))
    return f"""#!/bin/bash
# 由 make_mac_app.py 生成，不要手改；改仓库里的脚本后重新生成。
# 用法：server.sh start（双击启动）｜server.sh open（只顶出页面）｜server.sh stop（停服务）
APP_DIR={root}
PORT=8006
BASE="http://127.0.0.1:$PORT"
MODE="${{1:-start}}"

# 日志/pid 一律放用户目录。2026-11-03 实测：从 Dock/访达双击（LaunchServices）启动时，
# 本脚本对外置硬盘的写入会被系统隐私保护拦掉，`>> 外置盘` 直接失败，后端根本没起来，
# 表现成「后端启动超时」。把启动器的 IO 全部挪到 ~/Library/Logs 即可绕开。
LOG_DIR="$HOME/Library/Logs/AI选品智库"
mkdir -p "$LOG_DIR" 2>/dev/null
LOG="$LOG_DIR/launcher.log"
BACKEND_LOG="$LOG_DIR/backend.log"
PID="$LOG_DIR/launcher.pid"
LOCK_DIR="$LOG_DIR/start.lock"

log() {{ printf '%s %s\\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG" 2>/dev/null; }}

msg() {{
  /usr/bin/osascript \\
    -e 'on run argv' \\
    -e 'display dialog (item 1 of argv) buttons {{"好"}} default button 1 with title "AI选品智库"' \\
    -e 'end run' \\
    "$1" >/dev/null 2>&1
}}

healthy() {{ /usr/bin/curl -s -m 2 "$BASE/health" 2>/dev/null | /usr/bin/grep -q healthy; }}

# 先摸一下移动硬盘。系统第一次会弹「想访问可移除卷宗」的授权框；没授权时读文件不是报错，
# 而是一直卡在 open() 里（2026-11-03 实测后端就停在这里，看起来像「启动超时」）。
# 10 秒内读到就算通，读不到就按权限问题处理，别让用户白等 60 秒。
volume_probe() {{
  local marker="$LOG_DIR/.volume_probe.$$"
  /bin/rm -f "$marker"
  ( /bin/cat "$APP_DIR/backend/.venv/pyvenv.cfg" >/dev/null 2>&1; /usr/bin/touch "$marker" ) &
  local p=$! i
  for i in $(seq 1 10); do
    [ -f "$marker" ] && {{ /bin/rm -f "$marker"; return 0; }}
    sleep 1
  done
  /usr/bin/pkill -9 -P "$p" 2>/dev/null
  /bin/kill -9 "$p" 2>/dev/null
  /bin/rm -f "$marker"
  return 2
}}

# 端口上的后端 pid：只认「既是 uvicorn、又确实是本端口的实例」的进程。
# 光看 pid 文件不够：换端口调试时 pid 文件可能指向另一个实例，会误杀（2026-11-03 踩过）。
# 但也不能只认「正在监听」：卡在文件访问上的半启动实例不监听端口，照样得能停掉。
backend_pids() {{
  local pids="" p cmd
  [ -f "$PID" ] && pids=$(/bin/cat "$PID" 2>/dev/null)
  pids="$pids $(/usr/sbin/lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null)"
  for p in $pids; do
    cmd=$(/bin/ps -p "$p" -o command= 2>/dev/null) || continue
    case "$cmd" in *uvicorn*) ;; *) continue ;; esac
    case "$cmd" in
      *"--port $PORT"*) echo "$p"; continue ;;
    esac
    /usr/sbin/lsof -p "$p" -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1 && echo "$p"
  done | /usr/bin/sort -u
}}

if [ "$MODE" = "stop" ]; then
  KILLED=$(backend_pids)
  if [ -z "$KILLED" ]; then log "停止：端口 $PORT 上没有在跑的后端"; exit 0; fi
  for p in $KILLED; do /bin/kill "$p" 2>/dev/null && log "已停止后端 pid $p"; done
  sleep 1
  for p in $KILLED; do /bin/kill -0 "$p" 2>/dev/null && /bin/kill -9 "$p" 2>/dev/null; done
  /bin/rm -f "$PID" 2>/dev/null
  exit 0
fi

if [ "$MODE" = "open" ]; then
  # 只把页面顶出来；后端万一掉了，就退回完整启动流程
  if healthy; then log "打开 $BASE/"; /usr/bin/open "$BASE/"; exit 0; fi
  MODE=start
fi

if [ ! -d "$APP_DIR" ]; then
  msg "未找到项目目录。请先挂载移动硬盘（Tool Disk）再启动。"
  exit 1
fi

if ! healthy; then
  if ! volume_probe; then
    log "探测项目文件超时（多半在等系统授权），不再尝试启动"
    msg "系统还没允许「AI选品智库」读移动硬盘，后端读不到自己的程序文件，所以起不来。

如果刚才弹过「想访问可移除卷宗」，点「允许」就行。
没看到的话：系统设置 → 隐私与安全性 → 完全磁盘访问权限，
把「AI选品智库」加进去并打开开关，然后再双击一次图标。"
    exit 1
  fi
fi

CHILD=""
if ! healthy; then
  # 防连点：两个双击同时进来时只让一个起后端，另一个等它健康检查通过
  if ! /bin/mkdir "$LOCK_DIR" 2>/dev/null; then
    log "另一次启动正在进行，等待它就绪"
    for _ in $(seq 1 60); do sleep 1; healthy && break; done
    if healthy; then log "健康检查通过，打开 $BASE/"; /usr/bin/open "$BASE/"; exit 0; fi
    log "等待超时，接管启动"
    /bin/rmdir "$LOCK_DIR" 2>/dev/null
    /bin/mkdir "$LOCK_DIR" 2>/dev/null
  fi
  trap '/bin/rmdir "$LOCK_DIR" 2>/dev/null' EXIT INT TERM HUP

  PY="$APP_DIR/backend/.venv/bin/python"
  [ -x "$PY" ] || PY="/usr/bin/python3"
  # cd 只是为兼容相对路径；失败也不致命，--app-dir 会把 backend 放进 PYTHONPATH
  cd "$APP_DIR/backend" 2>/dev/null
  log "启动后端：$PY（端口 $PORT）"
  # 这里不做 setsid：脱离子进程组靠的是上层 `do shell script`（见 make_mac_app.py 说明）。
  "$PY" -m uvicorn main:app --app-dir "$APP_DIR/backend" \\
        --host 127.0.0.1 --port "$PORT" --log-level warning \\
        >>"$BACKEND_LOG" 2>&1 < /dev/null &
  CHILD=$!
  echo "$CHILD" > "$PID" 2>/dev/null
  for _ in $(seq 1 60); do
    sleep 1
    healthy && break
    /bin/kill -0 "$CHILD" 2>/dev/null || {{ log "后端进程已退出（pid $CHILD）"; break; }}
  done
  /bin/rmdir "$LOCK_DIR" 2>/dev/null
fi

if healthy; then
  log "健康检查通过，打开 $BASE/"
  /usr/bin/open "$BASE/"
else
  TAIL=$(tail -n 12 "$BACKEND_LOG" 2>/dev/null)
  [ -n "$TAIL" ] || TAIL="（后端没有任何输出，多半是外置硬盘的文件访问被系统拦了）"
  log "启动失败，末段日志：$TAIL"
  msg "后端没能启动（等了 60 秒）。

最常见的原因是系统拦住了本应用对移动硬盘的访问。
处理办法：系统设置 → 隐私与安全性 → 完全磁盘访问权限，
把「AI选品智库」加进去并打开开关，然后再双击一次图标。

最近的日志：
$TAIL

完整日志：$BACKEND_LOG"
  [ -n "$CHILD" ] && /bin/kill "$CHILD" 2>/dev/null
  exit 1
fi

if [ -n "$CHILD" ]; then
  # 这一层守护只负责「后端起不来就收尾」；双击退出后它会留在后台陪着后端，
  # 后端死了它自己结束。要停服务：桌面「停止AI选品智库.command」或 server.sh stop。
  log "后端 pid $CHILD 已就绪"
  wait "$CHILD" 2>/dev/null
  log "后端已退出（pid $CHILD），守护进程结束"
fi
"""


def build(out_dir: Path | None = None) -> Path:
    out_dir = out_dir or DIST_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    app = out_dir / f"{APP_NAME}.app"
    if app.exists():
        shutil.rmtree(app)

    # 1. 用 osacompile 生成小程序。不加 -s：跑完就退出，Dock 里不留空图标；
    #    后端靠 do shell script 脱离进程组，不依赖应用常驻。
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "main.applescript"
        script.write_text(_apple_script(), encoding="utf-8")
        subprocess.run(["osacompile", "-o", str(app), str(script)],
                       check=True, capture_output=True)

    contents = app / "Contents"
    resources = contents / "Resources"
    resources.mkdir(parents=True, exist_ok=True)

    # 2. 图标
    png = out_dir / "app_icon_1024.png"
    _render_icon(png)
    if png.is_file():
        _make_icns(png, resources / "app_icon.icns")
        png.unlink(missing_ok=True)

    # 3. 真正的启动/守护脚本
    launcher = resources / "server.sh"
    launcher.write_text(_launcher_script(), encoding="utf-8")
    launcher.chmod(0o755)

    # 4. 名字、图标、版本写进 Info.plist。
    #    注意 CFBundleIconName：osacompile 会写 applet，它优先于 CFBundleIconFile，
    #    不删掉的话 Finder/Dock 永远显示脚本编辑器的默认图标（Assets.car 里那份）。
    info_path = contents / "Info.plist"
    info = plistlib.loads(info_path.read_bytes())
    info.update(_info_plist_overrides())
    info.pop("CFBundleIconName", None)
    info_path.write_bytes(plistlib.dumps(info))
    (resources / "Assets.car").unlink(missing_ok=True)
    subprocess.run(["plutil", "-lint", str(info_path)], check=True, capture_output=True)

    # 5. 改过内容后签名必然失效，不重签的话 Apple Silicon 会直接拒绝启动
    signed = subprocess.run(["codesign", "--force", "--deep", "--sign", "-", str(app)],
                            capture_output=True)
    if signed.returncode != 0:
        print(f"[WARN] 临时签名失败（{signed.stderr.decode().strip()[:120]}），"
              "双击可能被系统拦下，可在 系统设置→隐私与安全性 里放行")
    return app


def install(app: Path) -> Path:
    desktop = Path.home() / "Desktop"
    target = desktop / app.name
    if target.exists():
        backup = app.parent / f"{app.stem}-old.app"
        if backup.exists():
            shutil.rmtree(backup)
        shutil.move(str(target), str(backup))
        print(f"[OK] 旧启动器备份到 {backup}")
    shutil.copytree(app, target, symlinks=True)
    subprocess.run(["touch", str(target)], check=False)
    # 让 Finder / Dock 立刻用新图标，而不是继续显示旧缓存
    lsregister = ("/System/Library/Frameworks/CoreServices.framework/Frameworks/"
                  "LaunchServices.framework/Support/lsregister")
    if Path(lsregister).is_file():
        subprocess.run([lsregister, "-f", str(target)], check=False, capture_output=True)

    stop_cmd = desktop / f"停止{APP_NAME}.command"
    stop_cmd.write_text(
        "#!/bin/bash\n"
        f"/bin/bash {shlex.quote(str(target / 'Contents' / 'Resources' / 'server.sh'))} stop\n"
        "echo\necho '按任意键关闭窗口…'\nread -n 1 -s -r\n"
    )
    stop_cmd.chmod(0o755)
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description="生成 macOS 启动器")
    parser.add_argument("--install", action="store_true", help="覆盖安装到桌面")
    args = parser.parse_args()
    app = build()
    print(f"[OK] 已生成 {app}")
    if args.install:
        installed = install(app)
        print(f"[OK] 已安装到 {installed}")
        print(f"[OK] 停止服务：桌面「停止{APP_NAME}.command」，或 bash "
              f"{installed}/Contents/Resources/server.sh stop")


if __name__ == "__main__":
    main()
