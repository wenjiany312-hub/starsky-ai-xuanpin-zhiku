# -*- coding: utf-8 -*-
"""
1688 整品归档 Runner（Mac 移植版）—— 复用 product_archiver.py，不依赖 MCP 协议。

用法：
    python runner.py login                    # 打开浏览器到1688登录页，轮询检测登录态（最多5分钟）
    python runner.py state [url]              # 打印当前浏览器/登录状态
    python runner.py scrape <商品URL> [输出根目录]   # 整品归档：结构化数据+全部媒体+全页扫描+中文报告MD
    python runner.py detail <商品URL>         # 快速抓取阶梯价/物流/核心属性（不归档）

所有结果以 JSON 打印到 stdout（前缀 ===RESULT=== 便于解析）。
登录态持久化在 drission_user_data/，第二个商品起通常无需再次登录。
"""
import json
import os
import sys
import time
from pathlib import Path

from DrissionPage import ChromiumPage, ChromiumOptions

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import product_archiver as pa  # noqa: E402
import ocr  # noqa: E402

USER_DATA_PATH = str(HERE / 'drission_user_data')
DEFAULT_OUTPUT_BASE = str(HERE.parent.parent.parent / 'data_storage' / 'supply_archives')

# macOS / Windows / Linux Chromium 候选路径（含 Playwright 缓存的 Chrome for Testing）
_BROWSER_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "C:/Program Files/Google/Chrome/Application/chrome.exe",
    "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
]

# Playwright 缓存内的 Chrome for Testing（macOS）
import glob as _glob
for _pw in sorted(_glob.glob(os.path.expanduser(
        "~/Library/Caches/ms-playwright/chromium-*/chrome-mac*/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing")), reverse=True):
    _BROWSER_CANDIDATES.insert(0, _pw)

_browser_instance = None


def _find_browser_path() -> str:
    import shutil
    for name in ("chrome", "google-chrome", "chromium", "msedge"):
        found = shutil.which(name)
        if found:
            return found
    for cand in _BROWSER_CANDIDATES:
        if Path(cand).exists():
            return cand
    return ""


def get_browser() -> ChromiumPage:
    global _browser_instance
    if _browser_instance is not None:
        try:
            _browser_instance.title
            return _browser_instance
        except Exception:
            _browser_instance = None

    co = ChromiumOptions()
    co.set_user_data_path(USER_DATA_PATH)
    co.headless(False)
    browser_path = _find_browser_path()
    if browser_path:
        co.set_browser_path(browser_path)
    co.set_argument('--disable-infobars')
    co.set_argument('--lang=zh-CN')
    co.set_pref('intl.accept_languages', 'zh-CN,zh,en-US,en')
    try:
        co.set_pref('profile.managed_default_content_settings.images', 1)
        co.set_pref('profile.default_content_setting_values.images', 1)
    except Exception:
        pass
    _browser_instance = ChromiumPage(co)
    return _browser_instance


def get_security_block_reason(page) -> str:
    url = (page.url or "").lower()
    title = page.title or ""
    if "login.1688.com" in url or "login.taobao.com" in url:
        return "redirected_to_login"
    if "punish" in url or "deny" in url or "verify" in url:
        return "security_url"
    if "验证码拦截" in title or "安全验证" in title or "登录" in title:
        return "security_title"
    try:
        body = page.ele('tag:body', timeout=1)
        body_text = body.text if body else ""
    except Exception:
        body_text = ""
    for phrase in ["请拖动滑块", "拖动滑块完成拼图", "为了你的账户安全", "访问被拒绝", "检测到异常访问", "请输入验证码"]:
        if phrase in body_text:
            return f"security_text:{phrase}"
    return ""


def check_security_block(page) -> bool:
    return bool(get_security_block_reason(page))


def get_browser_state(page) -> dict:
    try:
        cookies = page.cookies()
    except Exception:
        cookies = []
    cookie_names = []
    for cookie in cookies or []:
        try:
            name = cookie.get('name', '')
            if name:
                cookie_names.append(name)
        except AttributeError:
            pass
    login_cookie_markers = {"_m_h5_tk", "cookie2", "t", "_tb_token_", "unb", "cna", "xlly_s"}
    matched = [n for n in cookie_names if n in login_cookie_markers]
    return {
        "url": page.url,
        "title": page.title,
        "cookie_count": len(cookies or []),
        "matched_login_cookies": matched,
        "has_login_cookie": bool(matched),
        "security_block_reason": get_security_block_reason(page),
        "is_blocked": check_security_block(page),
    }


def emit(obj):
    print("===RESULT===")
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def cmd_login(max_wait=300):
    """打开浏览器到1688登录页，轮询检测登录态。检测到登录cookie或超时后返回（浏览器保持开启）。"""
    page = get_browser()
    page.get("https://login.1688.com/member/signin.htm")
    page.wait.load_start()
    waited = 0
    interval = 5
    while waited < max_wait:
        time.sleep(interval)
        waited += interval
        st = get_browser_state(page)
        if st["has_login_cookie"] and "login.1688.com" not in (page.url or ""):
            emit({"status": "LOGIN_OK", "waited_sec": waited, "state": {k: st[k] for k in ("url", "has_login_cookie", "is_blocked")}})
            return
        print(f"[等待登录中] {waited}s  url={(page.url or '')[:60]}  has_login_cookie={st['has_login_cookie']}", flush=True)
    emit({"status": "LOGIN_TIMEOUT", "waited_sec": waited})


def cmd_state(url="https://www.1688.com/"):
    page = get_browser()
    page.get(url)
    page.wait.load_start()
    time.sleep(3)
    emit(get_browser_state(page))


def cmd_scrape(url: str, output_dir: str = ""):
    """整品归档：结构化数据 + 全部媒体下载 + 全页扫描 + 中文报告 MD（移植自 server.py scrape_1688_product）。"""
    page = get_browser()
    page.get(url)
    page.wait.load_start()
    time.sleep(3)

    if check_security_block(page):
        emit({
            "status": "NEED_VERIFY",
            "reason": get_security_block_reason(page),
            "message": "浏览器已打开商品页，请完成登录/滑块验证后重试（登录态会持久保存）。",
        })
        return

    try:
        raw = page.run_js(
            "return (window.context && window.context.result && window.context.result.data) "
            "? JSON.stringify(window.context.result.data) : null;"
        )
    except Exception:
        raw = None
    if not raw:
        emit({
            "status": "NEED_VERIFY",
            "reason": "window_context_missing",
            "message": "未取到 window.context，可能仍在验证/未登录。请在浏览器完成验证后重试。",
        })
        return

    try:
        data = json.loads(raw)
        parsed = pa.parse_product_data(data)

        base = output_dir or DEFAULT_OUTPUT_BASE
        folder_name = f"{pa.safe_name(parsed['title'])}_{time.strftime('%Y%m%d')}"
        out_dir = os.path.join(base, folder_name)
        os.makedirs(out_dir, exist_ok=True)

        with open(os.path.join(out_dir, "raw_data.json"), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        detail_html = pa.fetch_detail_html(parsed.get("detail_url", ""))
        manifest = pa.download_assets(parsed, out_dir, detail_html=detail_html)

        sweep_raw = pa.sweep_page(page)
        with open(os.path.join(out_dir, "page_text.txt"), "w", encoding="utf-8") as f:
            f.write(sweep_raw.get("text", ""))
        sweep = pa.parse_page_sweep(sweep_raw.get("text", ""))

        ai = pa.build_ai_analysis(parsed, competitors=sweep.get("recommendations", []))

        image_paths = [m["local"] for m in manifest["files"] if m["ok"] and ("images" in m["local"])]
        image_content = pa.read_image_content(image_paths, recognized=None)
        image_content["ocr_used"] = False
        image_content["ocr_count"] = 0

        meta = {"date": time.strftime("%Y%m%d"), "url": url, "folder": folder_name, "out_dir": out_dir}
        pa.save_render_cache(parsed, manifest, sweep, ai, image_content, meta)
        outputs = pa.write_outputs(parsed, manifest, sweep, ai, image_content, meta)
        report_path = outputs["report_path"]

        # 后台 OCR（未安装 PaddleOCR 时自动跳过，回退对话层读图模式）
        if ocr.is_available():
            import threading

            def _run_ocr_bg():
                try:
                    recognized = ocr.ocr_spec_images(image_paths)
                    if not recognized:
                        return
                    ic = pa.read_image_content(image_paths, recognized=recognized)
                    ic["ocr_used"] = True
                    ic["ocr_count"] = len(recognized)
                    pa.save_render_cache(parsed, manifest, sweep, ai, ic, meta)
                    pa.write_outputs(parsed, manifest, sweep, ai, ic, meta)
                except Exception:
                    pass

            threading.Thread(target=_run_ocr_bg, daemon=True).start()

        emit({
            "status": "DONE",
            "folder": out_dir,
            "report_path": report_path,
            "title": parsed.get("title", ""),
            "counts": manifest["counts"],
            "failed": [m["url"] for m in manifest["files"] if not m["ok"]][:10],
            "images_to_read": image_content["images_to_read"],
            "ocr_running_in_background": ocr.is_available(),
        })
    except Exception as e:
        emit({"status": "ERROR", "message": f"归档过程中出错：{e}"})


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    cmd = sys.argv[1]
    if cmd == "login":
        cmd_login()
    elif cmd == "state":
        cmd_state(sys.argv[2] if len(sys.argv) > 2 else "https://www.1688.com/")
    elif cmd == "scrape":
        if len(sys.argv) < 3:
            emit({"status": "ERROR", "message": "用法: python runner.py scrape <商品URL> [输出根目录]"})
            sys.exit(1)
        cmd_scrape(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "")
    elif cmd == "detail":
        emit({"status": "ERROR", "message": "detail 命令请使用 Sorftime ali1688_product_request 替代（云端版更稳定）"})
    else:
        emit({"status": "ERROR", "message": f"未知命令: {cmd}"})
        sys.exit(1)


if __name__ == "__main__":
    main()
