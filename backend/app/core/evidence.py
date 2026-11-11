"""
证据账本：一次选品运行抓到的每条外部数据都留痕。

- 每条证据有稳定编号（EV-01…），报告里的每个数字都必须能指回某条证据。
- 原始返回整条落盘（evidence.jsonl），供复盘与申诉；对模型只给摘要，省上下文。
- 失败也记录（ok=false + error），让“缺数据”成为一等公民，而不是悄悄换成估算值。
"""
import datetime
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.core.jsonio import write_text
from app.core.mcp_client import truncate_text

# 数据源常在返回里塞一段字段说明。它占满摘要额度，模型读到的全是“字段是什么意思”，
# 真实数据被挤出截断线外——摘要前先剔掉。
_SCHEMA_KEYS = ("doc", "docs", "schema", "field_descriptions", "description_map")


def _strip_schema(data: Any) -> Any:
    if isinstance(data, dict):
        kept = {k: v for k, v in data.items() if str(k).lower() not in _SCHEMA_KEYS}
        return kept or data
    return data


class EvidenceLedger:
    def __init__(self, run_id: str = ""):
        self.run_id = run_id
        self.items: List[Dict[str, Any]] = []
        self._seq = 0

    # ---------- 记录 ----------

    def add(self, capability: str, connector: Optional[str], tool: Optional[str],
            args: Optional[Dict[str, Any]] = None, data: Any = None, ok: bool = True,
            error: Optional[str] = None, elapsed_ms: int = 0, label: str = "") -> str:
        self._seq += 1
        eid = f"EV-{self._seq:02d}"
        self.items.append({
            "id": eid,
            "capability": capability,
            "label": label or capability,
            "connector": connector,
            "tool": tool,
            "args": args or {},
            "ok": ok,
            "error": error,
            "elapsed_ms": elapsed_ms,
            "fetched_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "data": data,
        })
        return eid

    def add_result(self, result: Any, label: str = "") -> str:
        """直接吃 CapabilityResult。"""
        return self.add(capability=result.capability, connector=result.connector, tool=result.tool,
                        args=getattr(result, "args", None) or {}, data=result.data, ok=result.ok,
                        error=result.error, elapsed_ms=result.elapsed_ms, label=label)

    # ---------- 读取 ----------

    def ok_items(self) -> List[Dict[str, Any]]:
        return [i for i in self.items if i["ok"]]

    def get(self, eid: str) -> Optional[Dict[str, Any]]:
        return next((i for i in self.items if i["id"] == eid), None)

    def missing(self) -> List[Dict[str, Any]]:
        return [i for i in self.items if not i["ok"]]

    def coverage(self) -> Dict[str, Any]:
        total = len(self.items)
        ok = len(self.ok_items())
        return {"total": total, "ok": ok,
                "ratio": round(ok / total, 3) if total else 0.0,
                "failed": [{"id": i["id"], "label": i["label"], "error": i["error"]} for i in self.missing()]}

    def digest(self, max_chars: int = 9000, per_item: int = 1400) -> str:
        """给模型看的证据摘要：编号 + 来源 + 结构化数据（单条截断）。"""
        blocks = []
        for it in self.items:
            head = f"[{it['id']}] {it['label']} ｜ {it['connector'] or '-'}:{it['tool'] or '-'} ｜ {it['fetched_at']}"
            if not it["ok"]:
                blocks.append(f"{head}\n状态: 获取失败 — {str(it['error'])[:200]}")
                continue
            blocks.append(f"{head}\n{truncate_text(_strip_schema(it['data']), per_item)}")
        text = "\n\n".join(blocks)
        return truncate_text(text, max_chars)

    def summary_line(self) -> str:
        cov = self.coverage()
        return f"证据 {cov['ok']}/{cov['total']} 条成功"

    # ---------- 落盘 ----------

    def save(self, run_dir: Path) -> Dict[str, str]:
        run_dir = Path(run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        jsonl = "\n".join(json.dumps(i, ensure_ascii=False, default=str) for i in self.items)
        write_text(run_dir / "evidence.jsonl", jsonl + ("\n" if jsonl else ""))
        lines = ["# 证据账本", "", f"运行: {self.run_id}", f"生成: {datetime.datetime.now().isoformat(timespec='seconds')}",
                 f"覆盖: {self.summary_line()}", ""]
        for it in self.items:
            lines.append(f"## {it['id']} {it['label']}")
            lines.append(f"- 能力: `{it['capability']}` ｜ 来源: `{it['connector']}:{it['tool']}` ｜ {it['fetched_at']}")
            if not it["ok"]:
                lines.append(f"- 状态: **获取失败** — {str(it['error'])[:300]}")
            else:
                lines.append("- 状态: 成功")
                lines.append("```json")
                lines.append(truncate_text(it["data"], 1200))
                lines.append("```")
            lines.append("")
        write_text(run_dir / "evidence.md", "\n".join(lines))
        return {"jsonl": str(run_dir / "evidence.jsonl"), "md": str(run_dir / "evidence.md")}
