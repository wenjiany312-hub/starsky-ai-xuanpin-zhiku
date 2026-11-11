"""把一次已完成的选品运行，导出成星空 S1 可读的标准产物目录。

设计原则只有一条：**只搬事实，不造数据。**
每个轴的分数、证据值、来源、时间窗都从这次运行自己的
``facts.json`` / ``verdict.json`` / ``evidence.jsonl`` 里取；取不到就写 null
并说明缺什么，绝不按印象补分——星空的 ``evaluate_scores`` 闸门也是这么要求的。

产出目录（产品根由 ``settings.STARSKY_ROOT_DIR`` 决定）::

    <YYYY-MM-DD>_<标题>/
      00A_quick_select.yaml
      00_总控/starsky-fullchain-workflow/00A_选品需求确认单.md
      00_总控/starsky-fullchain-workflow/来源_AI选品智库.json
      workflow_state.yaml
      S1_选品板块/jingpu-product-selection/02_方向初筛.yaml
      S1_选品板块/jingpu-product-selection/02_方向初筛.md
      S1_选品板块/jingpu-product-selection/00_product_selection.yaml
      S1_选品板块/jingpu-product-selection/证据/来源快照.json
      S1_选品板块/jingpu-product-selection/来源_运行/<task_id>/…
"""
from __future__ import annotations

import datetime
import json
import math
import re
import shutil
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from app.core import report_html
from app.core.config import settings
from app.core.jsonio import write_json, write_text
from app.core.scoring import AXES, AXIS_LABELS, DEFAULT_WEIGHTS

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

S1_DIR = "S1_选品板块/jingpu-product-selection"
SELECTION_YAML = f"{S1_DIR}/02_方向初筛.yaml"
SELECTION_MD = f"{S1_DIR}/02_方向初筛.md"
SELECTION_CANONICAL = f"{S1_DIR}/00_product_selection.yaml"
SNAPSHOT_REL = f"{S1_DIR}/证据/来源快照.json"
RUN_COPY_DIR = f"{S1_DIR}/来源_运行"
INTAKE_MD = "00_总控/starsky-fullchain-workflow/00A_选品需求确认单.md"
MARKER = "00_总控/starsky-fullchain-workflow/来源_AI选品智库.json"

MARGIN_NOTE = (
    "广告前目标毛利只是参考值，不是能不能做的门槛：定高一点，前期利润更厚、压力更小、"
    "成功把握更大；实际能接受多少由你根据经验和资金决定，有经验的卖家用更低毛利也可以做。"
)

# 每个轴在拿不到规则实测值时，按这个顺序回落取事实。
FACT_PRIORITY: Dict[str, Tuple[str, ...]] = {
    "market_capacity": ("search_volume_weekly", "visible_asin_count"),
    "competition": ("top3_click_share", "median_reviews", "visible_asin_count"),
    "trend": ("search_volume_trend_pct", "weeks_to_peak"),
    "differentiation": ("young_listing_share", "price_iqr_ratio", "median_reviews"),
    "margin_barrier": ("gross_margin_pct", "ceiling_headroom", "supply_price_cny_median"),
}

FACT_UNIT = {
    "search_volume_weekly": "次/周",
    "visible_asin_count": "个",
    "median_reviews": "条",
    "weeks_to_peak": "周",
    "supply_price_cny_median": "CNY",
    "ceiling_headroom": "USD",
}

# 规则里 value 为空的（NO_DATA）不能当作证据。
_USABLE_RULE_STATUS = ("PASS", "WARN", "FAIL")
_RULE_ORDER = {"PASS": 0, "WARN": 1, "FAIL": 2}


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _num(value: Any) -> Optional[float]:
    """只认真正的数字；bool 和字符串一律不当数。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(float(value)) else None
    return None


def _today() -> str:
    return datetime.date.today().isoformat()


def _iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _safe_name(text: str, limit: int = 48) -> str:
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", str(text or "")).strip()
    s = re.sub(r"\s+", " ", s).strip(". ")
    return (s[:limit] or "selection").strip()


def _unique_dir(root: Path, base: str) -> Path:
    candidate = root / base
    n = 2
    while candidate.exists():
        candidate = root / f"{base}-{n}"
        n += 1
    return candidate


def _yaml_dump(doc: Any) -> str:
    return yaml.safe_dump(doc, allow_unicode=True, sort_keys=False)


def _rel(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


# ---------------------------------------------------------------------------
# 星空插件与模板
# ---------------------------------------------------------------------------

def _plugin_root() -> Optional[Path]:
    """优先用显式配置，其次取已安装插件里最新的一个版本目录。"""
    configured = (settings.STARSKY_PLUGIN_DIR or "").strip()
    if configured:
        p = Path(configured).expanduser().resolve()
        if (p / "templates" / "workflow_state.example.yaml").is_file():
            return p
        if (p / "workflow_state.example.yaml").is_file():
            return p.parent
    cache = Path.home() / ".claude" / "plugins" / "cache" / "starskyclaude" / "starsky-amazon-agent"
    if not cache.is_dir():
        return None
    found = [d for d in cache.iterdir() if (d / "templates" / "workflow_state.example.yaml").is_file()]
    if not found:
        return None
    return max(found, key=lambda d: d.stat().st_mtime)


def _load_template() -> Tuple[Dict[str, Any], Optional[Path]]:
    plugin = _plugin_root()
    if plugin:
        for path in (plugin / "templates" / "workflow_state.example.yaml",
                     plugin / "workflow_state.example.yaml"):
            if path.is_file():
                doc = yaml.safe_load(path.read_bytes()) or {}
                if isinstance(doc, dict) and doc:
                    return deepcopy(doc), path
    return _minimal_state(), None


def _minimal_state() -> Dict[str, Any]:
    """没装插件时的最小骨架：字段名与模板一致，缺的留空，不假装完整。"""
    return {
        "schema_version": "3.7",
        "edition": "",
        "release_tag": "",
        "scenario": "S",
        "entry": {"mode": "workflow", "task": "", "routing_status": "pending"},
        "single_point_progress": {"state": "not_started", "active_skill": "", "completed_skills": [],
                                  "user_requested_next_skill": "", "available_followups": []},
        "product_id": "",
        "product_name": "",
        "marketplace": "US",
        "current_step": "intake_interview",
        "completed_steps": [],
        "artifacts": {},
        "stage_acceptance": {f"S{i}": {"status": "pending", "evidence_mode": "", "accepted_at": "", "notes": ""}
                             for i in range(1, 6)},
        "next_step": {"step_id": "condition_select", "skill": "starsky-fullchain-workflow",
                      "instruction": "用 MCP 按条件出候选方向并落盘 00_选品推荐报告.md"},
        "updated_at": "",
    }


# ---------------------------------------------------------------------------
# 读运行产物
# ---------------------------------------------------------------------------

def _read_json(path: Path) -> Dict[str, Any]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _read_ledger(path: Path) -> Dict[str, Dict[str, Any]]:
    items: Dict[str, Dict[str, Any]] = {}
    if not path.is_file():
        return items
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict) and item.get("id"):
            items[str(item["id"])] = item
    return items


# ---------------------------------------------------------------------------
# 五轴权重：从运行结论的五轴份额反推回完整五轴
# ---------------------------------------------------------------------------

def _weights(raw: Dict[str, Any]) -> Dict[str, float]:
    """把运行时的权重还原成星空要求的完整五轴（缺的轴用默认权重补），再归一。

    星空闸门要求 ``set(weights) == set(AXES)`` 且合计为 1；运行引擎的
    ``weights`` 可能只覆盖有数据的轴，所以这里不能直接照抄。
    """
    out: Dict[str, float] = {}
    for axis in AXES:
        v = _num((raw or {}).get(axis))
        out[axis] = v if v is not None and v > 0 else float(DEFAULT_WEIGHTS[axis])
    total = sum(out.values())
    if total <= 0:
        out = {a: float(DEFAULT_WEIGHTS[a]) for a in AXES}
        total = sum(out.values())
    w = {a: round(out[a] / total, 6) for a in AXES}
    diff = round(1.0 - sum(w.values()), 6)
    if diff:
        pivot = max(w, key=lambda a: w[a])
        w[pivot] = round(w[pivot] + diff, 6)
    return w


# ---------------------------------------------------------------------------
# 每轴证据：规则 → 事实 → 引擎分，三级回落
# ---------------------------------------------------------------------------

def _rule_ids(meta: Dict[str, Any]) -> List[str]:
    raw = meta.get("rules") or []
    ids: List[str] = []
    for item in raw:
        if isinstance(item, str):
            ids.append(item)
        elif isinstance(item, dict) and item.get("id"):
            ids.append(str(item["id"]))
    return ids


def _axis_rule(axis: str, meta: Dict[str, Any], rules: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    ids = _rule_ids(meta)
    pool = [r for r in rules if str(r.get("id")) in ids] if ids else []
    pool = pool or [r for r in rules if r.get("axis") == axis]
    pool = [
        r for r in pool
        if str(r.get("status", "")).upper() in _USABLE_RULE_STATUS
        and r.get("evidence_id")
        and _num(r.get("value")) is not None
    ]
    if not pool:
        return None
    pool.sort(key=lambda r: _RULE_ORDER.get(str(r.get("status", "")).upper(), 9))
    return pool[0]


def _ref_id(refs: Any, key: str) -> Optional[str]:
    if not isinstance(refs, dict):
        return None
    v = refs.get(key)
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        for item in v:
            if isinstance(item, str):
                return item
    return None


def _resolve_axis(
    axis: str,
    score: Optional[float],
    meta: Dict[str, Any],
    rules: List[Dict[str, Any]],
    facts: Dict[str, Any],
    evidence: Dict[str, Dict[str, Any]],
    default_tw: str,
) -> Optional[Dict[str, Any]]:
    rule = _axis_rule(axis, meta, rules)
    if rule:
        eid = str(rule.get("evidence_id"))
        item = evidence.get(eid) or {}
        return {
            "observed": rule.get("value"),
            "fact": str(rule.get("id")),
            "fact_label": rule.get("name") or str(rule.get("id")),
            "unit": rule.get("unit") or "",
            "rule": str(rule.get("id")),
            "evidence_id": eid,
            "connector": item.get("connector"),
            "tool": item.get("tool"),
            "origin": "rules",
            "time_window": str(item.get("fetched_at") or default_tw)[:10],
        }

    for key in FACT_PRIORITY.get(axis, ()):
        value = _num(facts.get(key))
        if value is None:
            continue
        eid = _ref_id(facts.get("evidence_refs"), key)
        item = evidence.get(eid) if eid else {}
        return {
            "observed": facts.get(key),
            "fact": key,
            "fact_label": key,
            "unit": FACT_UNIT.get(key, ""),
            "rule": None,
            "evidence_id": eid,
            "connector": (item or {}).get("connector"),
            "tool": (item or {}).get("tool"),
            "origin": "facts",
            "time_window": str((item or {}).get("fetched_at") or default_tw)[:10],
        }

    if score is not None:
        return {
            "observed": round(float(score), 1),
            "fact": None,
            "fact_label": "引擎五轴分",
            "unit": "分",
            "rule": None,
            "evidence_id": None,
            "connector": None,
            "tool": None,
            "origin": "engine",
            "time_window": default_tw,
        }
    return None


def _missing_reason(axis: str) -> str:
    need = "、".join(FACT_PRIORITY.get(axis, ()))
    return (
        f"{AXIS_LABELS.get(axis, axis)}缺可核验数据：本次运行既没有拿到该轴的规则实测值，"
        f"也没有 {need} 中的任何一项，因此不按印象给分。"
    )


# ---------------------------------------------------------------------------
# 方向表
# ---------------------------------------------------------------------------

def _build_candidates(
    title: str,
    extra_names: List[str],
    verdict_doc: Dict[str, Any],
    rules: List[Dict[str, Any]],
    facts: Dict[str, Any],
    evidence: Dict[str, Dict[str, Any]],
    default_tw: str,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[str], int]:
    """返回 (yaml candidates, snapshot candidates, warnings, 有分的轴数)。"""
    warnings: List[str] = []
    axes_meta = verdict_doc.get("axes") or {}
    rows: List[Dict[str, Any]] = []
    snapshot: List[Dict[str, Any]] = []
    scored = 0

    primary_name = (title or "").strip() or "本次方向"
    cleaned: List[str] = []
    seen = {primary_name}
    for raw in extra_names or []:
        name = str(raw or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        cleaned.append(name)
    if len(cleaned) > 4:
        warnings.append(f"方向最多 5 个，已按前 4 个补充方向导出，其余 {len(cleaned) - 4} 个未收录。")
        cleaned = cleaned[:4]

    for index, name in enumerate([primary_name] + cleaned, 1):
        cid = f"D{index}"
        scores: Dict[str, Any] = {}
        snap_axes: Dict[str, Any] = {}
        for axis in AXES:
            meta = axes_meta.get(axis) or {}
            if index == 1:
                score = _num(meta.get("score"))
                if score is not None and not 1 <= score <= 5:
                    warnings.append(f"{axis} 引擎分 {score} 超出 1–5，已截断后导出。")
                    score = min(5.0, max(1.0, score))
            else:
                score, meta = None, {}

            if score is None:
                scores[axis] = {"score": None, "evidence": None, "missing_reason": _missing_reason(axis)
                                if index == 1 else
                                "这是用户补充的方向，本次运行没有为它取数；要用同一套阈值判断，请单独跑一次运行。"}
                continue

            resolved = _resolve_axis(axis, score, meta, rules, facts, evidence, default_tw)
            if resolved is None:  # 理论上不会发生：score 有值就给引擎分
                scores[axis] = {"score": None, "evidence": None, "missing_reason": _missing_reason(axis)}
                continue
            scored += 1
            scores[axis] = {
                "score": round(score, 1),
                "evidence": {
                    "value": resolved["observed"],
                    "source": {
                        "path": SNAPSHOT_REL,
                        "selector": f"/candidates/{cid}/axes/{axis}/observed",
                        "note": " · ".join(str(x) for x in (
                            resolved["origin"], resolved["fact"], resolved["connector"], resolved["tool"]) if x),
                    },
                    "time_window": resolved["time_window"],
                },
                "missing_reason": "",
            }
            snap_axes[axis] = resolved

        rows.append({
            "n": index,
            "candidate_id": cid,
            "name": name,
            "scores": scores,
        })
        snapshot.append({"candidate_id": cid, "n": index, "name": name, "axes": snap_axes})

    if len(rows) < 3:
        warnings.append(
            f"星空 S1 第 2 步要求 3–5 个候选方向，本次只有 {len(rows)} 个（可在导出时补充 2–4 个方向名）。"
        )
    return rows, snapshot, warnings, scored


def _evaluate_scores(candidates: List[Dict[str, Any]]) -> List[str]:
    """照抄星空 ``evaluate_scores`` 的判定，导出前先自己过一遍。"""
    errors: List[str] = []
    for row in candidates:
        scores = row.get("scores") or {}
        for axis in AXES:
            cell = scores.get(axis) or {}
            if cell.get("score") is None:
                if not str(cell.get("missing_reason") or "").strip():
                    errors.append(f"{row.get('name')} {axis}: null 缺原因")
                continue
            score = cell["score"]
            ev = cell.get("evidence") or {}
            if (_num(score) is None or not 1 <= score <= 5
                    or _num(ev.get("value")) is None
                    or not ev.get("source") or not ev.get("time_window")):
                errors.append(f"{row.get('name')} {axis}: 分数须 1–5 且有数字、来源和时间窗")
    if not candidates:
        errors.append("缺方向评分表")
    return errors


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------

def _cell(value: Optional[float]) -> str:
    return "—" if value is None else f"{value:g}"


def _selection_markdown(
    title: str,
    marketplace: str,
    verdict_doc: Dict[str, Any],
    candidates: List[Dict[str, Any]],
    snapshot: List[Dict[str, Any]],
    warnings: List[str],
) -> str:
    verdict = verdict_doc.get("verdict") or "未研判"
    total = _num(verdict_doc.get("total_score"))
    lines = [
        f"# 方向初筛 · {title}",
        "",
        f"> 由 AI 选品智库运行 `{title}` 导出。每个数字都能在 `{SNAPSHOT_REL}` 里按 selector 追到；"
        "取不到的写缺什么，不给印象分。",
        "",
        f"- 站点：{marketplace}",
        f"- 裁定：**{verdict}**" + (f"（{total:.1f}/100）" if total is not None else ""),
        f"- 方向数：{len(candidates)}（星空第 2 步要求 3–5 个，并需你本人选定 1 个）",
        "",
        "| 方向 | " + " | ".join(AXIS_LABELS[a] for a in AXES) + " | 加权分 |",
        "|" + "---|" * (len(AXES) + 2),
    ]
    for row in candidates:
        cells = [_cell(_num(row["scores"][a].get("score"))) for a in AXES]
        lines.append(f"| {row['n']}. {row['name']} | " + " | ".join(cells) + " | 待补数 |")
    lines.append("")

    lines.append("## 每个方向一句话")
    for row in candidates:
        if row["n"] == 1:
            lines.append(f"- **{row['name']}**：{verdict_doc.get('reason') or '规则引擎未给出理由。'}")
        else:
            lines.append(f"- **{row['name']}**：补充方向，本次没有取数，未研判。")
    lines.append("")

    lines.append("## 每个轴的依据")
    for item in snapshot:
        if not item["axes"]:
            continue
        lines.append(f"### {item['n']}. {item['name']}")
        for axis in AXES:
            got = item["axes"].get(axis)
            if not got:
                continue
            lines.append(
                f"- {AXIS_LABELS[axis]}：{got['observed']} {got['unit']}".rstrip()
                + f"（{got['origin']}"
                + (f" · {got['fact']}" if got.get("fact") else "")
                + (f" · {got['connector']}/{got['tool']}" if got.get("connector") and got.get("tool") else "")
                + f" · {got['time_window']}）"
            )
        lines.append("")

    missing = [f"- {row['name']}：{AXIS_LABELS[a]}" for row in candidates for a in AXES
               if row["scores"][a].get("score") is None]
    if missing:
        lines.append("## 还没拿到的数")
        lines.extend(missing[:20])
        lines.append("")

    if warnings:
        lines.append("## 导出提示")
        lines.extend(f"- {w}" for w in warnings)
        lines.append("")

    lines.append("## 下一步")
    lines.append("")
    lines.append("在星空 S1 第 2 步里选定 1 个方向（回复方向编号或写一句自己的选择）。"
                 "项目不会替你选；选定后再进入需求验证。")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 接入单
# ---------------------------------------------------------------------------

def _intake_doc(
    title: str,
    task_id: str,
    marketplace: str,
    price_band: str,
    margin: float,
    budget: Optional[float],
    currency: str,
    marketplace_attested: bool,
    price_attested: bool,
    margin_attested: bool,
) -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "user_reply": title,
        "marketplace": marketplace,
        "price_band": price_band,
        "templates": [],
        "target_margin_pre_ad": margin,
        "initial_budget": (
            {"value": float(budget), "currency": currency, "status": "user_attested"}
            if budget is not None else None),
        "field_sources": {
            "marketplace": "user_attested" if marketplace_attested else "default_assumption",
            "price_band": "user_attested" if price_attested else "default_assumption",
            "target_margin_pre_ad": "user_attested" if margin_attested else "default_assumption",
        },
        "provenance": {
            "produced_by": "ai-selection-lab",
            "task_id": task_id,
            "user_reply_origin": "用户在 AI 选品智库新建运行时填写的产品概念；不是星空接入问答的原话",
        },
    }


def _intake_markdown(doc: Dict[str, Any], title: str, root: Path, task_id: str) -> str:
    src = doc.get("field_sources") or {}
    budget = doc.get("initial_budget")
    budget_text = f"{budget['value']:g} {budget['currency']}" if budget else "未填写（可选）"
    lines = [
        "# 选品条件",
        "",
        f"站点：{doc['marketplace']}；价带：{doc['price_band']}；"
        f"广告前目标毛利：{round(float(doc['target_margin_pre_ad']) * 100)}%"
        f"（{'你指定' if src.get('target_margin_pre_ad') == 'user_attested' else '系统默认，可改'}）",
        "",
        f"> {MARGIN_NOTE}",
        "",
        f"首批预算：{budget_text}",
        "",
        f"📁 产品文件夹：{root}",
        "",
        f"用户输入：{title}",
        "",
        "> 本单由 AI 选品智库按星空 S1 的格式投影生成（运行 `" + task_id + "`），"
        "未经星空的接入问答流程；站点/价带/毛利若为系统默认，可在此直接改。",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# workflow_state
# ---------------------------------------------------------------------------

def _patch_state(
    state: Dict[str, Any],
    title: str,
    task_id: str,
    marketplace: str,
    artifacts: Dict[str, str],
) -> Dict[str, Any]:
    doc = deepcopy(state)
    doc["scenario"] = "S"
    entry = doc.get("entry") if isinstance(doc.get("entry"), dict) else {}
    doc["entry"] = {
        **entry,
        "mode": "workflow",
        "task": "full_chain",
        "routing_status": "active",
    }
    doc["product_id"] = doc.get("product_id") or f"aisl-{task_id}"
    doc["product_name"] = title
    doc["marketplace"] = marketplace
    doc["current_step"] = "condition_select"
    doc["completed_steps"] = ["intake_interview"]
    doc["next_step"] = {
        "step_id": "condition_select",
        "skill": "starsky-fullchain-workflow",
        "instruction": "方向初筛已由 AI 选品智库导出，等用户选定 1 个方向后再进入需求验证",
    }
    merged = doc.get("artifacts") if isinstance(doc.get("artifacts"), dict) else {}
    merged.update(artifacts)
    doc["artifacts"] = merged
    doc["updated_at"] = _iso()
    # 故意不写 s1_execution：那是星空自己的执行回执，本次不是它跑的；
    # 写了却没有真实 user_reply / user_choice，闸门照样不过。
    return doc


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

class StarskyExportService:
    def config(self) -> Dict[str, Any]:
        root = Path(settings.STARSKY_ROOT_DIR).expanduser()
        plugin = _plugin_root()
        products: List[Dict[str, Any]] = []
        if root.is_dir():
            for d in sorted(root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)[:12]:
                if d.is_dir():
                    products.append({"name": d.name, "path": str(d),
                                     "modified": datetime.datetime.fromtimestamp(
                                         d.stat().st_mtime).isoformat(timespec="minutes")})
        return {
            "root_dir": str(root),
            "root_exists": root.is_dir(),
            "plugin_dir": str(plugin) if plugin else None,
            "template_found": bool(plugin and (plugin / "templates" / "workflow_state.example.yaml").is_file()),
            "recent_products": products,
        }

    @staticmethod
    def _produce_canonical(product_dir: Path) -> Tuple[Optional[str], Optional[str]]:
        """调星空自己的 s1_selection.py，把 02_方向初筛.yaml 投影成标准选品文件。

        返回 (相对路径 或 None, 需要告诉用户的一句话 或 None)。
        插件不在或没跑通时只给提示、不拦导出——星空接手后会自己再生成一遍。
        """
        plugin = _plugin_root()
        if plugin is None:
            return None, ("没找到星空插件，没生成标准 00_product_selection.yaml；"
                          "星空接手跑第 2 步时会自己生成，不影响 02_方向初筛。")
        script = plugin / "scripts" / "s1_selection.py"
        if not script.is_file():
            return None, f"星空插件里没有 scripts/s1_selection.py（{plugin}），没生成标准选品文件。"
        source = product_dir / SELECTION_YAML
        if not source.is_file():
            return None, "没写出 02_方向初筛.yaml，跳过标准选品文件。"
        try:
            proc = subprocess.run(
                [sys.executable, str(script), "--root", str(product_dir), "--input", str(source)],
                cwd=str(plugin), capture_output=True, text=True, timeout=120,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return None, (f"调用星空 s1_selection.py 没跑起来（{type(exc).__name__}），"
                          "标准选品文件交给星空自己生成。")
        target = product_dir / SELECTION_CANONICAL
        if proc.returncode != 0 or not target.is_file():
            lines = [ln for ln in ((proc.stderr or "") + "\n" + (proc.stdout or "")).splitlines() if ln.strip()]
            tail = f"：{lines[-1].strip()[:180]}" if lines else ""
            return None, (f"星空 s1_selection.py 没产出 00_product_selection.yaml"
                          f"（exit={proc.returncode}）{tail}")
        return SELECTION_CANONICAL, None

    def export(
        self,
        task: Any,
        *,
        candidate_names: Optional[List[str]] = None,
        product_name: Optional[str] = None,
        marketplace: Optional[str] = None,
        price_band: Optional[str] = None,
        target_margin_pre_ad: Optional[float] = None,
        initial_budget: Optional[float] = None,
        budget_currency: str = "CNY",
    ) -> Dict[str, Any]:
        task_id = str(getattr(task, "task_id", "") or "")
        title = str(product_name or getattr(task, "title", "") or "").strip()
        if not task_id:
            raise ValueError("缺 task_id")
        if not title:
            raise ValueError("缺产品名/标题")

        run_dir = Path(getattr(task, "run_dir", "") or (settings.RUNS_DIR / task_id)).expanduser()
        if not run_dir.is_absolute():
            run_dir = settings.RUNS_DIR / task_id
        facts_doc = _read_json(run_dir / "facts.json")
        verdict_doc = _read_json(run_dir / "verdict.json")
        if not facts_doc and not verdict_doc:
            raise FileNotFoundError(f"运行产物不存在或为空：{run_dir}")

        facts = facts_doc.get("facts") or {}
        decided = verdict_doc.get("verdict") or {}
        rules = verdict_doc.get("rules") or []
        weights = _weights(decided.get("weights") or verdict_doc.get("weights") or {})
        evidence = _read_ledger(run_dir / "evidence.jsonl")
        run_meta = _read_json(run_dir / "run.json")
        finished = str(run_meta.get("finished_at") or "")[:10] or _today()

        params = getattr(task, "parameters", None) or {}
        market = (marketplace or getattr(task, "target_market", "") or params.get("marketplace")
                  or params.get("target_market") or "US").upper()
        band = str(price_band or params.get("price_band") or params.get("price_range") or "15-25")
        margin = _num(target_margin_pre_ad if target_margin_pre_ad is not None else params.get("target_margin_pre_ad"))
        if margin is None:
            margin = 0.40
        elif margin > 1:
            margin = margin / 100.0
        if not 0 < margin < 1:
            raise ValueError("广告前目标毛利必须在 0% 与 100% 之间")
        budget = _num(initial_budget if initial_budget is not None else params.get("initial_budget"))

        candidates, snapshot, warnings, scored = _build_candidates(
            title, list(candidate_names or []), decided, rules, facts, evidence, finished)
        # 把快照里的候选键改成 D1..DN（JSON Pointer 不许出现 / 和 ~）
        snap_candidates = {item["candidate_id"]: {"name": item["name"], "axes": item["axes"]}
                           for item in snapshot}

        root = Path(settings.STARSKY_ROOT_DIR).expanduser()
        root.mkdir(parents=True, exist_ok=True)
        product_dir = _unique_dir(root, f"{_today()}_{_safe_name(title)}")

        doc = {
            "schema_version": 1,
            "generated_at": _iso(),
            "task_id": task_id,
            "product": {"name": title, "marketplace": market, "verdict": decided.get("verdict"),
                        "total_score": decided.get("total_score")},
            "scoring_weights": weights,
            "candidates": candidates,
        }
        errors = _evaluate_scores(candidates)
        if errors:
            raise ValueError("方向评分表未通过星空闸门：" + "; ".join(errors))

        written: Dict[str, str] = {}

        def put(rel: str, text: str) -> Path:
            path = product_dir / rel
            write_text(path, text)
            written[rel] = str(path)
            return path

        # 1) 接入单
        intake = _intake_doc(
            title, task_id, market, band, margin, budget, (budget_currency or "CNY").upper(),
            marketplace_attested=bool(marketplace), price_attested=bool(price_band),
            margin_attested=target_margin_pre_ad is not None,
        )
        put("00A_quick_select.yaml", _yaml_dump(intake))
        put(INTAKE_MD, _intake_markdown(intake, title, product_dir, task_id))

        # 2) 证据快照（先写，02 的方向证据指向它）
        snapshot_doc = {
            "schema_version": 1,
            "generated_at": _iso(),
            "task_id": task_id,
            "note": "本文件是导出时的取值快照，selector 用 JSON Pointer；"
                    "值来自该次运行的 facts.json / verdict.json / evidence.jsonl。",
            "facts": {k: v for k, v in facts.items() if k != "evidence_refs"},
            "candidates": snap_candidates,
            "evidence": {eid: {k: item.get(k) for k in ("connector", "tool", "fetched_at", "ok", "label")}
                         for eid, item in evidence.items()},
        }
        put(SNAPSHOT_REL, json.dumps(snapshot_doc, ensure_ascii=False, indent=1))

        # 3) 方向初筛 yaml + md
        put(SELECTION_YAML, _yaml_dump(doc))
        put(SELECTION_MD, _selection_markdown(title, market, decided, candidates, snapshot, warnings))

        # 4) 标准 00_product_selection.yaml：由星空自己的 s1_selection.py 生成。
        #    投影件的排序、权重来源、封印都由插件自己算；我们只递上 02_方向初筛.yaml 这个输入。
        #    自己写一份的话，与插件重算出来的字节不一致，会被它的 sealed_projection 校验判成冲突。
        canonical_rel, canonical_note = self._produce_canonical(product_dir)
        if canonical_note:
            warnings.append(canonical_note)
        if canonical_rel:
            written[canonical_rel] = str(product_dir / canonical_rel)

        # 5) 运行产物副本。老记录的目录里只有 report.md，导出前就地补一份 HTML，
        #    不然导出的包里缺掉最该给人看的那份。
        html_src, md_src = run_dir / "report.html", run_dir / "report.md"
        if not html_src.is_file() and md_src.is_file():
            try:
                html_src.write_text(report_html.render(md_src.read_text(encoding="utf-8")), encoding="utf-8")
            except OSError as exc:
                warnings.append(f"HTML 报告未能生成，导出包里只有 Markdown：{exc}")
        copied: List[str] = []
        for name in ("report.html", "report.md", "facts.json", "verdict.json", "run.json", "evidence.jsonl"):
            src = run_dir / name
            if not src.is_file():
                continue
            dest = product_dir / RUN_COPY_DIR / task_id / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)
            copied.append(_rel(dest, product_dir))

        # 6) workflow_state
        state, template_path = _load_template()
        if template_path is None:
            warnings.append("没有找到星空插件模板，workflow_state.yaml 用了最小骨架，字段可能不全。")
        artifacts = {
            "intake_brief": INTAKE_MD,
            "selection_yaml": SELECTION_YAML,
            "selection_md": SELECTION_MD,
            "selection_provenance": SNAPSHOT_REL,
        }
        if canonical_rel:
            artifacts["selection_projection"] = canonical_rel
        state_doc = _patch_state(state, title, task_id, market, artifacts)
        put("workflow_state.yaml", _yaml_dump(state_doc))

        # 7) 来源标记
        marker = {
            "schema_version": 1,
            "produced_by": "ai-selection-lab",
            "app": settings.APP_NAME,
            "app_version": settings.APP_VERSION,
            "task_id": task_id,
            "run_dir": str(run_dir),
            "exported_at": _iso(),
            "verdict": {k: decided.get(k) for k in
                        ("verdict", "tier", "total_score", "confidence_level", "reason")},
            "method_pack": verdict_doc.get("method_pack"),
            "scoring_weights": weights,
            "template": str(template_path) if template_path else None,
            "run_files_copied": copied,
            "warnings": warnings,
            "notes": [
                "本目录由 AI 选品智库按星空 S1 的产物格式投影生成，不是星空自身的接入问答。",
                "02_方向初筛.yaml 中的候选仍需在星空 S1 第 2 步由你本人选定 1 个；项目没有代选。",
                "00_product_selection.yaml 是调星空自己的 scripts/s1_selection.py 生成的，不是本工具自己拼的投影件。",
                "workflow_state.yaml 未写 s1_execution：本次执行不是星空跑的，不伪造执行回执。",
            ],
        }
        put(MARKER, json.dumps(marker, ensure_ascii=False, indent=1))

        return {
            "product_dir": str(product_dir),
            "files": written,
            "candidates": [{"n": r["n"], "candidate_id": r["candidate_id"], "name": r["name"]}
                           for r in candidates],
            "scored_count": scored,
            "warnings": warnings,
            "verdict": decided.get("verdict"),
            "weights": weights,
        }


starsky_export_service = StarskyExportService()
