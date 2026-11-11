"""
1688 整品归档服务：驱动本地真实 Chromium 抓取 1688 商品页，
下载主图/SKU图/详情长图/视频，生成 14 节中文商品档案 MD。

实现方式：subprocess 调用 tools/ali1688_archiver/runner.py（DrissionPage），
解析 ===RESULT=== JSON。登录态持久化在 tools/ali1688_archiver/drission_user_data/。
"""
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.core.config import settings
from app.core.logger import logger

ARCHIVER_DIR = settings.BASE_DIR / "backend" / "tools" / "ali1688_archiver"
RUNNER_PATH = ARCHIVER_DIR / "runner.py"
# Windows 的虚拟环境是 Scripts\python.exe；都没有时用当前解释器（就是跑后端的那个）
_VENV = settings.BASE_DIR / "backend" / ".venv"
PYTHON_BIN = next((p for p in (_VENV / "bin" / "python", _VENV / "Scripts" / "python.exe") if p.exists()),
                  Path(sys.executable))
ARCHIVES_DIR = settings.STORAGE_DIR / "supply_archives"

_RESULT_SPLIT_RE = re.compile(r"===RESULT===\s*", re.M)


def _parse_runner_output(stdout: str) -> Optional[Dict[str, Any]]:
    parts = _RESULT_SPLIT_RE.split(stdout)
    if len(parts) < 2:
        return None
    tail = parts[-1].strip()
    try:
        return json.loads(tail)
    except json.JSONDecodeError:
        # 尝试截取首个完整 JSON 对象
        match = re.search(r"\{[\s\S]*\}", tail)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                return None
    return None


class Ali1688ArchiverService:

    async def _run_runner(self, *args: str, timeout: float = 240.0) -> Dict[str, Any]:
        proc = await asyncio.create_subprocess_exec(
            str(PYTHON_BIN), str(RUNNER_PATH), *args,
            cwd=str(ARCHIVER_DIR),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return {"status": "ERROR", "message": f"归档超时（>{timeout:.0f}s），浏览器可能被风控挂起"}
        text = stdout.decode("utf-8", errors="replace")
        result = _parse_runner_output(text)
        if result is None:
            logger.warning("runner 输出无法解析: %s", text[-500:])
            return {"status": "ERROR", "message": "归档器输出异常（可能浏览器启动失败）", "raw_tail": text[-400:]}
        return result

    async def archive_product(self, url: str) -> Dict[str, Any]:
        """整品归档：抓页面 + 下载全部媒体 + 生成报告（30~120 秒，需本机弹出浏览器）"""
        if not RUNNER_PATH.exists():
            return {"status": "ERROR", "message": "归档器未安装: backend/tools/ali1688_archiver/runner.py"}
        if not PYTHON_BIN.exists():
            return {"status": "ERROR", "message": "后端虚拟环境缺失: backend/.venv"}
        if "detail.1688.com/offer/" not in url:
            return {"status": "ERROR", "message": "仅支持 1688 商品详情页链接（detail.1688.com/offer/xxx.html）"}

        logger.info("启动 1688 整品归档: %s", url)
        result = await self._run_runner("scrape", url)

        if result.get("status") == "NEED_VERIFY":
            result["hint"] = "浏览器窗口已停在验证/登录页，请人工完成登录后重试；登录态会持久保存，后续无需重复。"
        return result

    async def start_login(self) -> Dict[str, Any]:
        """打开 1688 登录页浏览器窗口（后台最长等待 5 分钟），用户手动扫码/登录。"""
        if not RUNNER_PATH.exists():
            return {"status": "ERROR", "message": "归档器未安装"}

        async def _bg_login():
            try:
                await self._run_runner("login", timeout=330.0)
            except Exception as exc:
                logger.warning("1688 登录辅助异常: %s", exc)

        asyncio.create_task(_bg_login())
        return {
            "status": "LAUNCHED",
            "message": "已打开浏览器到 1688 登录页，请在窗口中完成登录（扫码/账密）。登录态将持久保存。",
        }

    async def login_state(self) -> Dict[str, Any]:
        """检测当前浏览器对 1688 的访问与登录态。"""
        if not RUNNER_PATH.exists():
            return {"status": "ERROR", "message": "归档器未安装"}
        return await self._run_runner("state", "https://www.1688.com/", timeout=60.0)

    def list_archives(self) -> List[Dict[str, Any]]:
        """列出已归档商品目录。"""
        if not ARCHIVES_DIR.exists():
            return []
        items = []
        for folder in sorted(ARCHIVES_DIR.iterdir(), reverse=True):
            if not folder.is_dir():
                continue
            report = folder / "product_report.md"
            raw = folder / "raw_data.json"
            if not report.exists():
                continue
            main_imgs = len(list((folder / "main_images").glob("*"))) if (folder / "main_images").exists() else 0
            detail_imgs = len(list((folder / "detail_images").glob("*"))) if (folder / "detail_images").exists() else 0
            items.append({
                "folder": folder.name,
                "title": folder.name.rsplit("_", 1)[0],
                "date": folder.name.rsplit("_", 1)[-1],
                "main_images": main_imgs,
                "detail_images": detail_imgs,
                "report_exists": True,
                "raw_exists": raw.exists(),
            })
        return items

    def get_report(self, folder: str) -> Optional[str]:
        """读取指定归档的中文报告 MD 全文。"""
        safe = Path(folder).name  # 防目录穿越
        report = ARCHIVES_DIR / safe / "product_report.md"
        if not report.exists():
            return None
        return report.read_text(encoding="utf-8")


ali1688_archiver_service = Ali1688ArchiverService()
