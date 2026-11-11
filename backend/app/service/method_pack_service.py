"""
方法论包（Method Pack）：把一位博主/一门课的选品方法装进来，选中即可用它跑数据。

一个包 = 一个文件夹（或 zip），最少一份 pack.yaml；也可以只有一份 SKILL.md（自动包装成 skill 型包）。

pack.yaml 字段（全部可选，只有 id/name 建议填）：
  id, name, version, author, description, tags
  type: declarative | skill | mcp
  weights:     五维权重，如 {market_capacity: 0.3, competition: 0.25, ...}
  thresholds:  覆盖阈值组，如 {competition: {top3_click_share_pass_max: 0.4}, price: {...}}
  rules:       {enabled: [R01, ...]} 或 {disabled: [R13]}
  defaults:    运行参数默认值，如 {check_ip: true, check_voc: true}
  lens:        研判视角提示词（模型按这套方法论解读，但不能改结论）
  knowledge_cards: [KC-QUANT-02, ...]  引用知识库卡片
  mcp_servers: 方法论依赖的额外数据源（只登记，不自动启用；启用需在数据源页确认）

安全：zip 只解压白名单扩展名，拒绝绝对路径 / .. / 符号链接，限制大小与文件数；
包里的任何脚本都不会被执行。
"""
import copy
import hashlib
import io
import json
import re
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from app.core import rules as rules_mod
from app.core.config import settings
from app.core.logger import logger
from app.core.scoring import AXES, DEFAULT_WEIGHTS

BUILTIN_DIR = settings.METHOD_PACKS_DIR / "builtin"
USER_DIR = settings.METHOD_PACKS_DIR / "user"
DEFAULT_PACK_ID = "starsky-s1-quick"

_ALLOWED_EXT = {".yaml", ".yml", ".md", ".json", ".txt", ".csv", ".png", ".jpg", ".jpeg", ".webp", ".svg"}
_MAX_ZIP_BYTES = 20 * 1024 * 1024
_MAX_FILES = 400
_ID_RE = re.compile(r"[^a-z0-9\-]+")
_RULE_ID = re.compile(r"^R\d{2}$")


class PackError(ValueError):
    pass


def _slug(text: str) -> str:
    s = _ID_RE.sub("-", (text or "").lower()).strip("-")
    return s[:48] or "pack"


def _read_yaml(path: Path) -> Dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        raise PackError(f"{path.name} 解析失败：{exc}") from exc


def _frontmatter(md: str) -> Tuple[Dict[str, Any], str]:
    if md.startswith("---"):
        parts = md.split("---", 2)
        if len(parts) >= 3:
            try:
                meta = yaml.safe_load(parts[1]) or {}
                return (meta if isinstance(meta, dict) else {}), parts[2]
            except Exception:
                pass
    return {}, md


def _clean_weights(raw: Any) -> Optional[Dict[str, float]]:
    if not isinstance(raw, dict):
        return None
    w = {}
    for k, v in raw.items():
        if k in AXES:
            try:
                w[k] = max(0.0, float(v))
            except (TypeError, ValueError):
                continue
    total = sum(w.values())
    if not w or total <= 0:
        return None
    return {k: round(v / total, 4) for k, v in w.items()}


# 阈值单位不统一：毛利率、趋势、评论区间难度是「百分数」（35 = 35%），
# 集中度、自然流量、成本占比是「小数」（0.35 = 35%）。写反了不会报错，只会让标准形同虚设
# （毛利线写成 0.35 就是 0.35%，什么品都过）——所以在入口统一纠正并提示。
PERCENT_KEYS = {("trend", "growing_pct"), ("trend", "warn_decline_pct"),
                ("competition", "review_band_pass_min"), ("competition", "review_band_warn_min"),
                ("margin", "gross_margin_pass_min"), ("margin", "gross_margin_warn_min"),
                ("margin", "gross_margin_fatal_below")}


def _is_fraction_key(group: str, key: str) -> bool:
    return any(s in key for s in ("share", "_ratio_", "ratio_pass", "ratio_warn")) or \
        (group == "verdict" and key.startswith("coverage"))


def normalize_unit(group: str, key: str, v: float) -> Tuple[float, Optional[str]]:
    """把明显写错单位的阈值换算回来，返回（值, 提示）。"""
    if (group, key) in PERCENT_KEYS and 0 < abs(v) <= 1:
        return round(v * 100, 4), f"{group}.{key} 写成了 {v:g}，按百分数换算为 {v * 100:g}"
    if _is_fraction_key(group, key) and 1 < v <= 100:
        return round(v / 100, 4), f"{group}.{key} 写成了 {v:g}，按占比换算为 {v / 100:g}"
    return v, None


def _clean_thresholds(raw: Any, notes: Optional[List[str]] = None) -> Dict[str, Dict[str, float]]:
    """只接受内置阈值表里已存在的组与数值键，防止包里写错字段悄悄不生效；单位写反的就地纠正。"""
    out: Dict[str, Dict[str, float]] = {}
    if not isinstance(raw, dict):
        return out
    base = rules_mod.BUILTIN_THRESHOLDS
    for group, kv in raw.items():
        if group not in base or not isinstance(kv, dict):
            continue
        for k, v in kv.items():
            if k in base[group] and isinstance(v, (int, float)) and not isinstance(v, bool):
                val, note = normalize_unit(group, k, float(v))
                if note and notes is not None:
                    notes.append(note)
                out.setdefault(group, {})[k] = val
    return out


def _clean_decision(raw: Any, warnings: Optional[List[str]] = None) -> Dict[str, Any]:
    """定档方式：默认按五维分数出四档；gates = 六道门；voc = 社媒 VOC 前导三道门。"""
    if not isinstance(raw, dict):
        return {"mode": "score"}
    mode = str(raw.get("mode") or "score").strip().lower()
    if mode not in ("score", "gates", "voc"):
        if warnings is not None:
            warnings.append(f"decision.mode 只认 score / gates / voc，收到 {mode!r}，按 score 处理")
        mode = "score"
    return {"mode": mode}


def _clean_data_sources(raw: Any) -> Dict[str, List[str]]:
    """data_sources：{组名: [连接器 id, ...]}，如 screen: [sellersprite, sorftime]。
    只保留「字符串列表」的组；连接器 id 在引擎用的时候再校验，这里不做跨模块导入。"""
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, List[str]] = {}
    for group, seq in raw.items():
        if isinstance(seq, list):
            ids = [str(x) for x in seq if isinstance(x, (str, int))]
            if ids:
                out[str(group)] = ids
    return out


class MethodPackService:
    def __init__(self):
        USER_DIR.mkdir(parents=True, exist_ok=True)

    # ---------------- 读取 ----------------

    def _load_dir(self, d: Path, source: str) -> Optional[Dict[str, Any]]:
        pack_file = d / "pack.yaml"
        skill_file = d / "SKILL.md"
        try:
            if pack_file.exists():
                raw = _read_yaml(pack_file)
            elif skill_file.exists():
                meta, _ = _frontmatter(skill_file.read_text(encoding="utf-8", errors="replace"))
                raw = {"id": meta.get("name") or d.name, "name": meta.get("name") or d.name,
                       "description": str(meta.get("description") or "")[:400], "type": "skill"}
            else:
                return None
        except PackError as exc:
            logger.warning("方法论包 %s 无法读取: %s", d, exc)
            return None
        pid = _slug(str(raw.get("id") or d.name))
        skill_text = ""
        if skill_file.exists():
            _meta, body = _frontmatter(skill_file.read_text(encoding="utf-8", errors="replace"))
            skill_text = body.strip()
        rules_cfg = raw.get("rules") if isinstance(raw.get("rules"), dict) else {}
        enabled = [r for r in (rules_cfg.get("enabled") or []) if isinstance(r, str) and _RULE_ID.match(r)]
        disabled = [r for r in (rules_cfg.get("disabled") or []) if isinstance(r, str) and _RULE_ID.match(r)]
        # 写错的阈值/权重不能悄悄不生效：包作者以为自己在调标准，其实一条都没进判定
        warnings: List[str] = []
        bad: List[str] = []
        raw_th = raw.get("thresholds") if isinstance(raw.get("thresholds"), dict) else {}
        for group, kv in raw_th.items():
            if group not in rules_mod.BUILTIN_THRESHOLDS:
                bad.append(str(group))
            elif isinstance(kv, dict):
                bad += [f"{group}.{k}" for k in kv if k not in rules_mod.BUILTIN_THRESHOLDS[group]]
        if bad:
            warnings.append("这些阈值组名/键名引擎不认识，没有生效：" + "、".join(bad[:8]))
        if raw.get("weights") and not _clean_weights(raw.get("weights")):
            warnings.append("权重没对上五维名称（market_capacity/competition/trend/differentiation/margin_barrier），已用默认权重")
        unit_notes: List[str] = []
        thresholds = _clean_thresholds(raw.get("thresholds"), unit_notes)
        if unit_notes:
            warnings.append("单位疑似写反，已自动换算（请核对并改正 pack.yaml）：" + "；".join(unit_notes[:6]))
        pack = {
            "id": pid,
            "name": str(raw.get("name") or pid),
            "version": str(raw.get("version") or "1.0"),
            "author": str(raw.get("author") or ""),
            "description": str(raw.get("description") or ""),
            "tags": [str(t) for t in (raw.get("tags") or [])][:12],
            "type": raw.get("type") if raw.get("type") in ("declarative", "skill", "mcp") else (
                "skill" if skill_text else "declarative"),
            "weights": _clean_weights(raw.get("weights")),
            "thresholds": thresholds,
            "rules": {"enabled": enabled, "disabled": disabled},
            "defaults": raw.get("defaults") if isinstance(raw.get("defaults"), dict) else {},
            "lens": str(raw.get("lens") or "")[:4000],
            "knowledge_cards": [str(c) for c in (raw.get("knowledge_cards") or [])][:12],
            "mcp_servers": raw.get("mcp_servers") if isinstance(raw.get("mcp_servers"), list) else [],
            "decision": _clean_decision(raw.get("decision"), warnings),
            # 数据源偏好（如 screen: [sellersprite, sorftime]、keywords: [xydc, sif]）：
            # 组名自由，值必须是连接器 id 列表；用户在设置里指定的路由永远优先于它
            "data_sources": _clean_data_sources(raw.get("data_sources")),
            "source": source,
            "path": str(d),
            "has_skill": bool(skill_text),
            "skill_chars": len(skill_text),
            "files": sorted(p.name for p in d.iterdir() if p.is_file())[:50],
            "warnings": warnings,
        }
        return pack

    def list(self) -> List[Dict[str, Any]]:
        packs: Dict[str, Dict[str, Any]] = {}
        for base, source in ((BUILTIN_DIR, "builtin"), (USER_DIR, "user")):
            if not base.exists():
                continue
            for d in sorted(base.iterdir()):
                if d.is_dir() and not d.name.startswith("."):
                    pack = self._load_dir(d, source)
                    if pack:
                        packs[pack["id"]] = pack  # 用户包可覆盖同名内置包
        out = list(packs.values())
        out.sort(key=lambda p: (p["id"] != DEFAULT_PACK_ID, p["source"] != "builtin", p["name"]))
        return out

    def get(self, pack_id: Optional[str]) -> Optional[Dict[str, Any]]:
        pid = _slug(pack_id or DEFAULT_PACK_ID)
        return next((p for p in self.list() if p["id"] == pid), None)

    def skill_text(self, pack: Dict[str, Any], max_chars: int = 8000) -> str:
        f = Path(pack["path"]) / "SKILL.md"
        if not f.exists():
            return ""
        _meta, body = _frontmatter(f.read_text(encoding="utf-8", errors="replace"))
        body = body.strip()
        return body if len(body) <= max_chars else body[:max_chars] + "\n…（已截断）"

    # ---------------- 应用到一次运行 ----------------

    def resolve(self, pack_id: Optional[str], params: Dict[str, Any]) -> Dict[str, Any]:
        """返回本次运行用的 {pack, thresholds, weights, rule_ids, params}。"""
        pack = self.get(pack_id) or self.get(DEFAULT_PACK_ID) or {
            "id": DEFAULT_PACK_ID, "name": "星空 S1 快速版（内置）", "thresholds": {}, "weights": None,
            "rules": {"enabled": [], "disabled": []}, "defaults": {}, "lens": "", "knowledge_cards": [],
            "type": "declarative", "has_skill": False}
        t = copy.deepcopy(rules_mod.thresholds())
        for group, kv in (pack.get("thresholds") or {}).items():
            t.setdefault(group, {}).update(kv)
        all_ids = [rd["id"] for rd in rules_mod._rule_defs(t)]
        enabled = pack["rules"].get("enabled") or all_ids
        rule_ids = [r for r in enabled if r in all_ids and r not in (pack["rules"].get("disabled") or [])]
        merged_params = {**(pack.get("defaults") or {}), **(params or {})}
        weights = _clean_weights(merged_params.get("weights")) or pack.get("weights") or dict(DEFAULT_WEIGHTS)
        return {"pack": pack, "thresholds": t, "weights": weights, "rule_ids": rule_ids, "params": merged_params}

    # ---------------- 导入 / 删除 ----------------

    def _install(self, src: Path, overwrite: bool) -> Dict[str, Any]:
        root = src
        if not (root / "pack.yaml").exists() and not (root / "SKILL.md").exists():
            subs = [d for d in root.iterdir() if d.is_dir() and not d.name.startswith((".", "__MACOSX"))]
            if len(subs) == 1:
                root = subs[0]
        probe = self._load_dir(root, "user")
        if not probe:
            raise PackError("没有找到 pack.yaml 或 SKILL.md，无法识别为方法论包")
        dest = USER_DIR / probe["id"]
        if any(p["id"] == probe["id"] and p["source"] == "builtin" for p in self.list()) and not overwrite:
            raise PackError(f"与内置包同名（{probe['id']}），请改 id 或勾选覆盖")
        if dest.exists():
            if not overwrite:
                raise PackError(f"已存在同名方法论包：{probe['id']}（可勾选覆盖）")
            shutil.rmtree(dest)
        shutil.copytree(root, dest, symlinks=False,
                        ignore=lambda _d, names: [n for n in names if n.startswith(".") or n == "__MACOSX"])
        pack = self._load_dir(dest, "user")
        logger.info("已导入方法论包 %s → %s", pack["id"], dest)
        return pack

    def import_zip(self, data: bytes, overwrite: bool = False) -> Dict[str, Any]:
        if len(data) > _MAX_ZIP_BYTES:
            raise PackError("压缩包超过 20MB")
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as exc:
            raise PackError("不是有效的 zip 文件") from exc
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if len(infos) > _MAX_FILES:
            raise PackError(f"文件数超过 {_MAX_FILES}")
        total = 0
        with tempfile.TemporaryDirectory(prefix="mpack-") as tmp:
            base = Path(tmp).resolve()
            for info in infos:
                name = info.filename.replace("\\", "/")
                if name.startswith("/") or ".." in Path(name).parts or "__MACOSX" in name:
                    continue
                if (info.external_attr >> 16) & 0o170000 == 0o120000:  # 符号链接
                    continue
                if Path(name).suffix.lower() not in _ALLOWED_EXT:
                    continue
                total += info.file_size
                if total > _MAX_ZIP_BYTES * 3:
                    raise PackError("解压后体积过大")
                target = (base / name).resolve()
                if base not in target.parents:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as fsrc, open(target, "wb") as fdst:
                    shutil.copyfileobj(fsrc, fdst)
            return self._install(base, overwrite)

    def import_folder(self, folder: str, overwrite: bool = False) -> Dict[str, Any]:
        src = Path(folder).expanduser().resolve()
        if not src.is_dir():
            raise PackError("文件夹不存在")
        if settings.METHOD_PACKS_DIR.resolve() in (src, *src.parents):
            raise PackError("该文件夹已在方法论库里")
        with tempfile.TemporaryDirectory(prefix="mpack-") as tmp:
            dst = Path(tmp) / src.name
            dst.mkdir()
            count = 0
            for f in src.rglob("*"):
                if f.is_symlink() or not f.is_file() or f.suffix.lower() not in _ALLOWED_EXT:
                    continue
                if any(part.startswith(".") for part in f.relative_to(src).parts):
                    continue
                count += 1
                if count > _MAX_FILES:
                    raise PackError(f"文件数超过 {_MAX_FILES}")
                out = dst / f.relative_to(src)
                out.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, out)
            return self._install(dst, overwrite)

    def delete(self, pack_id: str) -> bool:
        d = (USER_DIR / _slug(pack_id)).resolve()
        if USER_DIR.resolve() not in d.parents or not d.is_dir():
            return False
        shutil.rmtree(d)
        return True


# ---------------------------------------------------------------- 从原文生成方法包
# 「我学了一个博主的方法论，怎么快速装进来」——让他把笔记/课程稿/帖文直接粘进来，
# 由模型把它翻译成这套引擎认识的旋钮（五维权重 + 阈值覆盖 + 规则开关 + 研判视角），
# 人只需要核对一遍。手写 pack.yaml 对非技术用户是不现实的，SKILL.md 又只能影响解读、
# 影响不了判定，所以这一步才是「方法论真的能改变结论」的入口。

GEN_PROMPT = """你是亚马逊选品引擎的方法论编译器。用户给你一段方法论原文（可能是一位博主/课程的选品打法笔记），
你要把它编译成这台引擎能执行的参数包。只输出一个 JSON 对象，不要输出解释。

可用旋钮（只能从这里选，不能自造）：
1) weights：五维权重，键只能是 market_capacity（容量）、competition（竞争）、trend（趋势）、
   differentiation（差异化）、margin_barrier（利润门槛），值 0~1，会自动归一化。
2) thresholds：只允许下列组与键，值一律是数字、不要带 % 号；每个键括号里写了单位，必须照单位填：
{thresholds}
3) rules.disabled：只有原文明确说「不看 / 不在乎 / 不用管」某项时才把对应规则关掉；原文没提到的规则
   一律保持开启（沿用默认标准，没提到不等于不要）。绝大多数情况这里应该是空数组。规则号：
{rules}
4) lens：一段 200~600 字的研判视角，用中文写，说明「按这套方法论看，什么算机会、什么算否决、
   看数据时最该盯哪几个指标」。这段话只引导解读，不改阈值结论。
5) defaults：运行默认值，可用键 check_voc / check_ip（布尔）。

判定原则：
- 原文明确给出的数字（例如「评论超过 500 就算红海」）优先落到 thresholds 对应键上，不要改数值；
- 原文没给数字、只给倾向（例如「很看重利润」），就通过 weights 调高对应维度，并在 lens 里说明；
- 原文里的英文术语、品牌名原样保留；不要编造原文没有的指标；
- 拿不准就不要填该键，宁缺毋滥。

输出 JSON 结构：
{{"id":"kebab-case 英文 id","name":"中文名（不超过 20 字）","description":"一句话（不超过 60 字）",
"tags":["最多 6 个"],"weights":{{...}},"thresholds":{{"组":{{"键":数字}}}},"rules":{{"disabled":["R01"]}},
"defaults":{{}},"lens":"...","notes":"你做了哪些取舍（一句话）"}}
"""


def _threshold_catalog_text() -> str:
    """把每个键的单位和默认值写给模型：单位写反（毛利 35% 写成 0.35）是最容易犯、也最致命的错。"""
    lines = []
    for group, kv in rules_mod.BUILTIN_THRESHOLDS.items():
        parts = []
        for k, v in kv.items():
            if (group, k) in PERCENT_KEYS:
                unit = "百分数，35 表示 35%"
            elif _is_fraction_key(group, k):
                unit = "小数，0.35 表示 35%"
            else:
                unit = "原值"
            parts.append(f"{k}（{unit}，默认 {v:g}）")
        lines.append(f"   - {group}: " + "；".join(parts))
    return "\n".join(lines)


def _rule_catalog_text() -> str:
    t = rules_mod.thresholds()
    return "\n".join(f"   - {d['id']} {d['name']}（{d['axis']}）" for d in rules_mod._rule_defs(t))


def _json_obj(text: str) -> Dict[str, Any]:
    """从模型输出里取第一个 JSON 对象（允许被 ```json 包着）。"""
    s = (text or "").strip()
    if "```" in s:
        s = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", s, flags=re.S).strip()
    start, end = s.find("{"), s.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        data = json.loads(s[start:end + 1])
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _sanitize_draft(raw: Dict[str, Any], fallback_name: str) -> Tuple[Dict[str, Any], List[str]]:
    """把模型输出收成合法参数，并如实报告哪些写错的键被丢掉（不能悄悄不生效）。"""
    warnings: List[str] = []
    weights = _clean_weights(raw.get("weights"))
    if raw.get("weights") and not weights:
        warnings.append("所有权重都没对上五维名称，已改用默认权重")

    thresholds: Dict[str, Dict[str, float]] = {}
    dropped: List[str] = []
    fixed: List[str] = []
    base = rules_mod.BUILTIN_THRESHOLDS
    raw_th = raw.get("thresholds") if isinstance(raw.get("thresholds"), dict) else {}
    for group, kv in raw_th.items():
        if group not in base or not isinstance(kv, dict):
            dropped.append(str(group))
            continue
        for k, v in kv.items():
            if k not in base[group]:
                dropped.append(f"{group}.{k}")
                continue
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                dropped.append(f"{group}.{k}")
                continue
            val, note = normalize_unit(group, k, float(v))
            if note:
                fixed.append(note)
            thresholds.setdefault(group, {})[k] = val
    if dropped:
        warnings.append("这些阈值键引擎不认识，已忽略：" + "、".join(dropped[:8]))
    if fixed:
        warnings.append("单位写反了，已自动换算：" + "；".join(fixed[:6]))

    rules = raw.get("rules") if isinstance(raw.get("rules"), dict) else {}
    disabled = [r for r in (rules.get("disabled") or []) if isinstance(r, str) and _RULE_ID.match(r)]
    defaults = raw.get("defaults") if isinstance(raw.get("defaults"), dict) else {}
    defaults = {k: v for k, v in defaults.items() if k in ("check_voc", "check_ip") and isinstance(v, bool)}

    pid = _slug(str(raw.get("id") or ""))
    if pid == "pack":
        # 中文名 slug 后是空的，所有包都会叫 pack 互相覆盖；用名字的短哈希区分
        seed = str(raw.get("name") or fallback_name)
        pid = "custom-" + hashlib.md5(seed.encode("utf-8")).hexdigest()[:6]
    draft = {
        "id": pid,
        "name": (str(raw.get("name") or fallback_name).strip() or fallback_name)[:24],
        "version": "1.0",
        "type": "declarative",
        "description": str(raw.get("description") or "").strip()[:120],
        "tags": [str(t) for t in (raw.get("tags") or [])][:6],
        "weights": weights,
        "thresholds": thresholds,
        "rules": {"disabled": disabled},
        "defaults": defaults,
        "lens": str(raw.get("lens") or "").strip()[:4000],
        "notes": str(raw.get("notes") or "").strip()[:200],
    }
    return draft, warnings


def draft_yaml(draft: Dict[str, Any]) -> str:
    body = {k: v for k, v in draft.items() if k != "notes" and v not in (None, {}, [], "")}
    return yaml.safe_dump(body, allow_unicode=True, sort_keys=False, default_flow_style=False)


async def generate_from_text(text: str, name: str = "", author: str = "") -> Dict[str, Any]:
    """方法论原文 → 参数包草稿（不落盘，先给人看）。"""
    from app.core.llm_gateway import LLMError, llm_gateway

    src = (text or "").strip()
    if len(src) < 80:
        raise PackError("原文太短，至少 80 字才能提炼出可用的参数")
    if len(src) > 40000:
        src = src[:40000]
    meta, body = _frontmatter(src)
    src = body.strip() or src
    fallback = name or str(meta.get("name") or "") or "自定义方法论"

    prompt = GEN_PROMPT.format(thresholds=_threshold_catalog_text(), rules=_rule_catalog_text())
    prompt += f"\n\n方法论名称（可参考）：{fallback}\n\n原文：\n{src}"
    try:
        out = await llm_gateway.complete(prompt, role="strong", temperature=0.2,
                                         max_tokens=8000, timeout=240)
    except LLMError as exc:
        raise PackError(f"模型没给出可用结果：{exc}") from exc
    raw = _json_obj(out)
    if not raw:
        raise PackError("模型输出不是 JSON，没能解析出参数")
    draft, warnings = _sanitize_draft(raw, fallback)
    if author:
        draft["author"] = author
    return {"draft": draft, "yaml": draft_yaml(draft), "warnings": warnings, "source_chars": len(src)}


def install_draft(draft: Dict[str, Any], source_text: str, overwrite: bool = False) -> Dict[str, Any]:
    """把草稿落成方法包：pack.yaml 定规则，SKILL.md 存原文供报告引用。"""
    pid = _slug(str(draft.get("id") or draft.get("name") or "custom"))
    if not draft.get("weights") and not draft.get("thresholds") and not draft.get("lens"):
        raise PackError("这份草稿没有权重、阈值或研判视角，装进来也不会改变结论")
    if any(p["id"] == pid and p["source"] == "builtin" for p in method_pack_service.list()):
        raise PackError(f"与内置包同名（{pid}），请换一个 id")
    dest = USER_DIR / pid
    if dest.exists():
        if not overwrite:
            raise PackError(f"已存在同名方法论包：{pid}（可勾选覆盖）")
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    body = {k: v for k, v in draft.items() if k not in ("notes",) and v not in (None, {}, [], "")}
    (dest / "pack.yaml").write_text(
        yaml.safe_dump(body, allow_unicode=True, sort_keys=False), encoding="utf-8")
    text = (source_text or "").strip()[:40000]
    if text:
        fm = yaml.safe_dump({"name": draft.get("name") or pid,
                             "description": (draft.get("description") or "")[:200]},
                            allow_unicode=True, sort_keys=False).strip()
        (dest / "SKILL.md").write_text(f"---\n{fm}\n---\n\n{text}\n", encoding="utf-8")
    pack = method_pack_service._load_dir(dest, "user")
    logger.info("已从原文生成方法论包 %s", pid)
    return pack


method_pack_service = MethodPackService()
