"""把历史选品记录里的英文买家讨论补译成中文，并重排 report.md / report.html。

为什么要单独一个脚本：中文译文是后加的能力，之前的记录里 only English。
用户打开旧记录还是满屏英文，等于这个功能没生效。

用法：
    backend/.venv/bin/python -I backend/scripts/backfill_voc_zh.py            # 全部记录
    backend/.venv/bin/python -I backend/scripts/backfill_voc_zh.py SEL-xxx    # 指定记录

只处理「有 voc_samples 但还没有 voc_samples_zh」的记录；译文条数与原文不一致时
整条放弃（错位一条就等于把别人的话安在产品上），并如实打印原因。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.core import facts as facts_mod  # noqa: E402
from app.core.report_html import render  # noqa: E402
from app.service.selection_engine import SelectionEngine, translate_voc_samples  # noqa: E402

RUNS = BACKEND.parent / "data_storage" / "runs"
SEC = "## 用户声音样本（公开讨论摘要）"


def patch_report_md(path: Path, facts: dict) -> bool:
    """沿用引擎的排版函数，避免回填版式和新生成的报告对不上。"""
    if not path.exists():
        return False
    lines = path.read_text(encoding="utf-8").splitlines()
    try:
        i = next(k for k, ln in enumerate(lines) if ln.strip() == SEC)
    except StopIteration:
        return False
    j = i + 1
    while j < len(lines) and not lines[j].startswith("## "):
        j += 1
    intro = "买家原话，中文为译文；缩进的英文是原文，便于核对。" if facts.get("voc_samples_zh") \
        else "买家原话摘录（未翻译）。"
    lines[i + 1:j] = ["", intro] + SelectionEngine._voc_bullets(facts, 8) + [""]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


async def one(run_dir: Path) -> str:
    fj, mj = run_dir / "facts.json", run_dir / "report.md"
    if not fj.exists():
        return "skip 无 facts.json"
    data = json.loads(fj.read_text(encoding="utf-8"))
    f = data.get("facts") or {}
    samples = [s for s in (f.get("voc_samples") or []) if isinstance(s, str) and s.strip()]
    if not samples:
        return "skip 无用户声音"
    if f.get("voc_samples_zh"):
        # 已有译文：不重复花钱调模型，只按当前版式重排一遍（幂等）
        patch_report_md(mj, f)
        if mj.exists():
            (run_dir / "report.html").write_text(
                render(mj.read_text(encoding="utf-8")), encoding="utf-8")
        return "OK 重排"

    zh, err = await translate_voc_samples(samples)
    if not zh:
        return f"FAIL {err}"
    f["voc_samples_zh"] = [facts_mod.voc_line_zh(s, z) for s, z in zip(samples, zh)]
    data["facts"] = f
    fj.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    patch_report_md(mj, f)
    if mj.exists():
        (run_dir / "report.html").write_text(
            render(mj.read_text(encoding="utf-8")), encoding="utf-8")
    return f"OK {len(zh)} 条"


async def main() -> int:
    targets = sys.argv[1:]
    dirs = [RUNS / t for t in targets] if targets else sorted(RUNS.iterdir())
    bad = 0
    for d in dirs:
        if not d.is_dir():
            print(f"{d.name}: 目录不存在")
            bad += 1
            continue
        msg = await one(d)
        print(f"{d.name}: {msg}")
        if msg.startswith("FAIL"):
            bad += 1
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
