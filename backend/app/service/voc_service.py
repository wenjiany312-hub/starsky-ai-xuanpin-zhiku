"""
社媒 VOC 服务：多源前导信号采集与痛点深度分析。

数据源多层调度：
1) 实时 MCP 源：Sorftime 等已连接的社媒 MCP（有额度且已连接时优先，实时性高）
2) 本地语料库：voc-data 沉淀语料与离线样本（0 费用、0 延迟、覆盖 30+ 品类 50 万条历史语料）
3) 免费网络爬虫：Arctic Shift 公开 Reddit 存档（开源免费备源）

产出：
- 真实买家原话与讨论样本
- 词典口径痛点率、推荐率与注意力趋势
- AI 深度归纳：核心痛点分布（占比、说明、原文引用核对）、未满足需求、品牌吐槽、差异化机会
- 供三道门定档引擎（声量门/痛点门/平台门）裁决
"""
import asyncio
import datetime
import json
import re
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.core import voc as voc_mod
from app.core.config import settings
from app.core.jsonio import read_json, write_json, write_text
from app.core.logger import logger
from app.core.voc_source import ArcticShift, VocSourceError

CONF_DIR = settings.BASE_DIR / "config" / "voc_categories"
VOC_DIR = settings.STORAGE_DIR / "voc"
VOC_DATA_DIR = settings.BASE_DIR.parent / "voc-data"
GENERAL_ID = "general-us"
MONTH = 30 * 86400


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def load_categories() -> List[Dict[str, Any]]:
    out = []
    for f in sorted(CONF_DIR.glob("*.json")):
        try:
            c = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("VOC 品类配置 %s 读不了: %s", f.name, exc)
            continue
        if isinstance(c, dict) and c.get("id") and c.get("subreddits"):
            out.append(c)
    return out


def get_category(cid: str) -> Optional[Dict[str, Any]]:
    return next((c for c in load_categories() if c["id"] == cid), None)


def match_category(keyword: str) -> Dict[str, Any]:
    """按关键词里的词挑品类配置；挑不到用兜底并在结果里标明。"""
    low = (keyword or "").lower()
    best, score = None, 0
    for c in load_categories():
        s = sum(1 for m in c.get("match") or [] if re.search(r"(?<![a-z])" + re.escape(m.lower()), low))
        if s > score:
            best, score = c, s
    return best or get_category(GENERAL_ID) or {"id": GENERAL_ID, "name": "兜底", "subreddits": ["BuyItForLife", "Frugal"]}


# ---------------------------------------------------------------- 本地语料库发现

_LOCAL_KEYWORD_MAP = {
    "food_topper.jsonl": ("topper", "freeze-dried", "freeze dried", "food topper", "picky", "kibble topper", "dehydrated", "meal mixer"),
    "litter_box.jsonl": ("litter box", "litter", "automatic litter", "self cleaning litter", "litter robot", "cat box", "litter pan"),
    "cat_litter.jsonl": ("cat litter", "clumping litter", "tofu litter", "crystal litter", "pine pellets"),
    "cat_tree_scratcher.jsonl": ("cat tree", "scratcher", "scratching post", "cat tower", "climbing tree", "cat condo"),
    "gate_door.jsonl": ("cat gate", "pet gate", "dog gate", "baby gate", "stair gate", "playpen", "fence", "cat door", "pet door", "anti climb", "tall cat gate", "door gate"),
    "auto_feeder.jsonl": ("automatic feeder", "auto feeder", "timed feeder", "pet feeder", "feeder"),
    "slow_feeder_puzzle.jsonl": ("slow feeder", "puzzle feeder", "snuffle mat", "lick mat", "puzzle bowl", "slow bowl"),
    "water_fountain.jsonl": ("water fountain", "pet fountain", "water dispenser", "cat fountain", "dog fountain"),
    "carrier.jsonl": ("pet carrier", "cat carrier", "dog carrier", "travel carrier", "backpack", "airline approved"),
    "car_travel.jsonl": ("car seat", "seat cover", "dog hammock", "travel harness", "crash test"),
    "crate_kennel.jsonl": ("crate", "kennel", "dog crate", "heavy duty crate", "wire crate"),
    "leash.jsonl": ("dog leash", "leash", "retractable leash", "hands free leash", "rope leash"),
    "collar.jsonl": ("collar", "dog collar", "gps collar", "martingale", "tactical collar"),
    "harness.jsonl": ("harness", "no pull harness", "dog harness", "step in harness"),
    "pet_bed.jsonl": ("pet bed", "dog bed", "cat bed", "orthopedic bed", "calming bed", "bolster bed"),
    "calming.jsonl": ("calming", "anxiety", "pheromone", "thundershirt", "calming chew", "calming collar"),
    "dental.jsonl": ("dental chew", "dog toothbrush", "tartar", "water additive", "dental treat"),
    "toys.jsonl": ("chew toy", "indestructible toy", "tough toy", "interactive toy", "kong", "squeaky toy"),
    "grooming_tools.jsonl": ("grooming", "brush", "deshedding", "slicker brush", "nail grinder", "undercoat"),
    "flea_tick.jsonl": ("flea", "tick", "flea prevention", "flea collar", "heartworm"),
    "poop_bags.jsonl": ("poop bag", "waste bag", "biodegradable poop", "dog bags"),
    "gps_tracker.jsonl": ("gps tracker", "pet tracker", "airtag", "whistle", "fi collar"),
}


def find_local_samples(keyword: str) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    """从本地语料库（voc-data / data_storage/voc）查找匹配的高赞 VOC 讨论样本。"""
    kw_low = (keyword or "").lower()
    search_dirs = [
        VOC_DATA_DIR / "pet-us" / "patterns" / "samples",
        VOC_DIR / "corpus",
    ]
    for sdir in search_dirs:
        if not sdir.exists():
            continue
        for fname, keys in _LOCAL_KEYWORD_MAP.items():
            if any(k in kw_low for k in keys):
                p = sdir / fname
                if p.exists():
                    lines = [line.strip() for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
                    docs = []
                    for line in lines:
                        try:
                            d = json.loads(line)
                            docs.append({
                                "id": d.get("id"),
                                "type": d.get("type") or "post",
                                "subreddit": d.get("sub") or d.get("subreddit") or "pets",
                                "text": d.get("text") or d.get("body") or "",
                                "score": int(d.get("score") or 0),
                            })
                        except Exception:
                            continue
                    if docs:
                        return docs, fname
    return None, None


# ---------------------------------------------------------------- 单品验证

def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except ValueError:
        return None


async def annotate(keyword: str, docs: List[Dict[str, Any]], n: int = 40) -> Dict[str, Any]:
    """AI 深读痛点：用 LLM 归纳买家集中抱怨、未满足需求与差异化缝隙。"""
    from app.core.llm_gateway import LLMError, LLMUnavailable, llm_gateway
    samples = voc_mod.pick_pain_samples(docs, n=n)
    if len(samples) < 3:
        return {"ok": False, "data": None, "error": f"痛点样本只有 {len(samples)} 条，不足以归纳"}
    body = "\n".join(f"[{i + 1}] (r/{s['sub']} ↑{s['score']}) {s['text']}" for i, s in enumerate(samples))
    prompt = voc_mod.ANNOTATE_PROMPT.format(kw=keyword) + body
    err = ""
    for budget in (4000, 8000):
        try:
            res = await llm_gateway.chat([{"role": "user", "content": prompt}], role="fast",
                                         temperature=0.2, max_tokens=budget, timeout=180)
        except LLMUnavailable as exc:
            return {"ok": False, "data": None, "error": f"模型不可用：{exc}"}
        except LLMError as exc:
            err = str(exc)
            continue
        data = voc_mod.parse_annotation(_extract_json(res.get("content") or ""))
        if data:
            blob = " ".join(s["text"] for s in samples).lower()
            for p in data["pain_points"]:
                q = (p.get("quote") or "").strip().lower()[:60]
                p["quote_verified"] = bool(q) and q in blob
            return {"ok": True, "data": data, "error": "", "samples": len(samples)}
        err = "模型返回的 JSON 不合格" if res.get("content") else "模型返回空内容（预算被推理耗尽）"
    return {"ok": False, "data": None, "error": err}


async def _probe_from_arctic_shift(keyword: str, subs: List[str], months: int, per_window_cap: int) -> Dict[str, Any]:
    """通过 Arctic Shift 抓取公开 Reddit 存档。"""
    now_ts = int(time.time())
    t0 = now_ts - months * MONTH
    mid = (t0 + now_ts) // 2
    client = ArcticShift()
    t_start = time.monotonic()
    errors: List[str] = []
    docs_a: List[Dict[str, Any]] = []
    docs_b: List[Dict[str, Any]] = []
    capped = False
    try:
        for sub in subs:
            try:
                a = await client.search("comments", sub, text=keyword, after=t0, before=mid, cap=per_window_cap)
                b = await client.search("comments", sub, text=keyword, after=mid, before=now_ts, cap=per_window_cap)
            except VocSourceError as exc:
                errors.append(f"r/{sub}: {exc}")
                continue
            capped = capped or len(a) >= per_window_cap or len(b) >= per_window_cap
            docs_a += [voc_mod.normalize(x, "comments", sub) for x in a]
            docs_b += [voc_mod.normalize(x, "comments", sub) for x in b]
    finally:
        await client.aclose()

    def relevant(ds: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [d for d in ds if voc_mod.mentions(d["text"], keyword)]

    ra, rb = relevant(docs_a), relevant(docs_b)
    cleaned = voc_mod.etl(ra + rb)
    return {
        "docs": cleaned["docs"], "prior": len(ra), "recent": len(rb),
        "requests": client.requests, "elapsed_s": round(time.monotonic() - t_start, 1),
        "capped": capped, "errors": errors, "etl": cleaned["stats"]
    }


async def probe(keyword: str, subreddits: Optional[List[str]] = None, months: int = 12,
                per_window_cap: int = 60, do_annotate: bool = True,
                live_payload: Optional[Any] = None) -> Dict[str, Any]:
    """
    单个关键词的社媒前导信号探针（三层自动适配）：
    1. live_payload（如 Sorftime MCP 实时返回数据）-> 优先解析
    2. 本地沉淀样本（voc-data）-> 0 费用 0 延迟秒级命中
    3. Arctic Shift 免费爬取 -> 兜底无依赖
    """
    cat = match_category(keyword)
    subs = [s for s in (subreddits or cat.get("subreddits") or [])][:6]
    docs: List[Dict[str, Any]] = []
    source_label = ""
    source_type = ""
    trend_pct = None
    prior_cnt = None
    recent_cnt = None
    errors: List[str] = []
    requests_cnt = 0
    t_start = time.monotonic()

    # 1. 尝试从已传入的实时 MCP payload 读取 (Sorftime)
    if live_payload:
        raw_items = live_payload.get("data") if isinstance(live_payload, dict) else None
        if isinstance(raw_items, list) and raw_items:
            for it in raw_items:
                if isinstance(it, dict):
                    t = it.get("summary") or it.get("title") or it.get("text") or ""
                    if t:
                        docs.append({
                            "id": it.get("reddit_id") or it.get("id"),
                            "type": it.get("post_type") or "post",
                            "subreddit": (it.get("subreddit") or "").lstrip("r/"),
                            "text": t,
                            "score": int(it.get("like_count") or it.get("score") or 0),
                            "sentiment": it.get("main_post_sentiment"),
                            "url": it.get("url") or "",
                        })
            if docs:
                source_label = "Sorftime MCP（实时 Reddit 讨论）"
                source_type = "mcp"
                requests_cnt = 1

    # 2. 尝试从本地 VOC 语料库读取
    if not docs:
        local_docs, fname = find_local_samples(keyword)
        if local_docs:
            docs = local_docs
            source_label = f"本地 VOC 语料库（{fname}，0 费用）"
            source_type = "local"
            requests_cnt = 0

    # 3. 如果前两者皆无，通过 Arctic Shift 进行免费公网抓取
    if not docs:
        scrape = await _probe_from_arctic_shift(keyword, subs, months, per_window_cap)
        docs = scrape.get("docs") or []
        prior_cnt, recent_cnt = scrape.get("prior"), scrape.get("recent")
        if prior_cnt and prior_cnt >= 5 and recent_cnt is not None:
            trend_pct = round((recent_cnt / prior_cnt - 1) * 100, 1)
        errors = scrape.get("errors") or []
        requests_cnt = scrape.get("requests", 0)
        source_label = "Arctic Shift（免费 Reddit 存档）"
        source_type = "scraper"

    cleaned = voc_mod.etl(docs)
    valid_docs = cleaned["docs"]
    n = len(valid_docs)

    result: Dict[str, Any] = {
        "ok": n > 0,
        "keyword": keyword,
        "category_id": cat["id"],
        "category_name": cat.get("name"),
        "subreddits": subs,
        "months": months,
        "mentions": n,
        "prior": prior_cnt,
        "recent": recent_cnt,
        "trend_pct": trend_pct,
        "source": source_label or "Reddit 讨论",
        "source_type": source_type,
        "requests": requests_cnt,
        "elapsed_s": round(time.monotonic() - t_start, 1),
        "generic": cat["id"] == GENERAL_ID,
        "errors": errors[:5],
        "fetched_at": _now(),
        "etl": cleaned["stats"],
    }

    if not result["ok"]:
        result["error"] = "；".join(errors[:3]) or "未找到相关的买家讨论"
        return result

    # 识别痛点与好评
    flags = []
    for d in valid_docs:
        # 如果带有明确的 sentiment / post_type，结合正则更精准
        is_pain = d.get("sentiment") == "negative" or d.get("type") in ("complaint", "problem") or bool(voc_mod.PAIN_RE.search(d["text"]))
        is_reco = d.get("sentiment") == "positive" or d.get("type") in ("experience_share", "review") or bool(voc_mod.RECO_RE.search(d["text"]))
        flags.append({"pain": is_pain, "reco": is_reco})

    result["pain_rate"] = round(sum(f["pain"] for f in flags) / n, 3) if n else 0.0
    result["reco_rate"] = round(sum(f["reco"] for f in flags) / n, 3) if n else 0.0

    pain_docs = [d for d, f in zip(valid_docs, flags) if f["pain"]]
    result["pain_terms"] = voc_mod.top_terms(pain_docs)
    result["samples"] = [
        {
            "sub": d.get("subreddit") or "reddit",
            "score": d.get("score") or 0,
            "type": d.get("type") or "post",
            "text": re.sub(r"\s+", " ", d["text"])[:280],
            "url": d.get("url") or (f"https://reddit.com{d['permalink']}" if d.get("permalink") else ""),
        }
        for d in sorted(pain_docs or valid_docs, key=lambda x: -int(x.get("score") or 0))[:8]
    ]

    # AI 深度标注（前导痛点提炼）
    if do_annotate and len(pain_docs) >= 3:
        ann = await annotate(keyword, pain_docs)
        result["annotation"] = ann
        if ann["ok"]:
            top = max(ann["data"]["pain_points"], key=lambda x: x.get("pct") or 0)
            result["top_pain_pct"] = top.get("pct")
            result["top_pain_name"] = top.get("name")
    elif do_annotate:
        result["annotation"] = {"ok": False, "data": None, "error": f"痛点样本只有 {len(pain_docs)} 条，未做 AI 深读"}

    return result


# ---------------------------------------------------------------- 品类扫描

_JOBS: Dict[str, asyncio.Task] = {}


def _scan_dir(scan_id: str) -> Path:
    return VOC_DIR / "scans" / scan_id


def _corpus_dir(cid: str) -> Path:
    return VOC_DIR / "corpus" / cid


def list_scans() -> List[Dict[str, Any]]:
    d = VOC_DIR / "scans"
    out = []
    if d.exists():
        for p in sorted(d.iterdir(), reverse=True):
            st = read_json(p / "status.json", default=None)
            if st:
                out.append(st)
    return out[:50]


def get_scan(scan_id: str) -> Optional[Dict[str, Any]]:
    if not re.fullmatch(r"VOC-[0-9A-Za-z-]+", scan_id or ""):
        return None
    d = _scan_dir(scan_id)
    st = read_json(d / "status.json", default=None)
    if not st:
        return None
    res = read_json(d / "result.json", default=None)
    if res:
        st = {**st, "result": res}
    return st


def start_scan(category_id: str, months: Optional[int] = None, subreddits: Optional[List[str]] = None,
               annotate_top: int = 5) -> Dict[str, Any]:
    cat = get_category(category_id)
    if not cat:
        raise ValueError(f"没有这个品类配置：{category_id}")
    if not cat.get("products"):
        raise ValueError("这个配置没有产品词典（products），无法做品类扫描；请先在 config/voc_categories/ 里补上")
    scan_id = f"VOC-{datetime.datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}"
    d = _scan_dir(scan_id)
    d.mkdir(parents=True, exist_ok=True)
    status = {"scan_id": scan_id, "category_id": category_id, "category_name": cat.get("name"),
              "status": "running", "progress": 0, "stage": "准备", "created_at": _now(), "updated_at": _now(),
              "params": {"months": months or cat.get("months", 6), "subreddits": subreddits or cat["subreddits"],
                         "annotate_top": annotate_top}, "error": None, "log": []}
    write_json(d / "status.json", status)
    _JOBS[scan_id] = asyncio.create_task(_run_scan(scan_id, cat, status))
    return status


def _upd(scan_id: str, st: Dict[str, Any], stage: str, progress: int, **kw: Any) -> None:
    # 形参不能叫 status：收尾时要用 status="done" / "failed" 关键字改状态，重名会直接 TypeError
    st.update(stage=stage, progress=progress, updated_at=_now(), **kw)
    st["log"] = (st.get("log") or [])[-30:] + [f"{_now()[11:]} {stage}"]
    write_json(_scan_dir(scan_id) / "status.json", st)


async def _run_scan(scan_id: str, cat: Dict[str, Any], status: Dict[str, Any]) -> None:
    client = ArcticShift()
    try:
        await _scan(scan_id, cat, status, client)
    except Exception as exc:
        logger.exception("VOC 扫描失败")
        _upd(scan_id, status, "失败", status.get("progress", 0), status="failed", error=str(exc)[:300])
    finally:
        await client.aclose()
        _JOBS.pop(scan_id, None)


async def _scan(scan_id: str, cat: Dict[str, Any], status: Dict[str, Any], client: ArcticShift) -> None:
    p = status["params"]
    subs, months = p["subreddits"], int(p["months"])
    cdir = _corpus_dir(cat["id"])
    (cdir / "raw").mkdir(parents=True, exist_ok=True)
    state_f = cdir / "state.json"
    state = read_json(state_f, default={}) or {}
    raw_f = cdir / "raw" / "corpus.jsonl"
    seen_f = cdir / "seen.txt"
    seen = set(seen_f.read_text().split()) if seen_f.exists() else set()

    now_ts = int(time.time())
    win_start = now_ts - months * MONTH
    pcap, ccap = int(cat.get("post_cap_per_month", 120)), int(cat.get("comment_cap_per_month", 240))
    windows = [(win_start + i * MONTH, min(win_start + (i + 1) * MONTH, now_ts)) for i in range(months)]
    total_jobs = len(subs) * len(windows) * 2
    done = 0
    new_docs = 0
    errors: List[str] = []
    with raw_f.open("a", encoding="utf-8") as fh:
        for sub in subs:
            for (a, b) in windows:
                for kind, cap in (("posts", pcap), ("comments", ccap)):
                    key = f"{sub}|{kind}|{a}"
                    if state.get(key) == "done":
                        done += 1
                        continue
                    try:
                        rows = await client.search(kind, sub, after=a, before=b, cap=cap)
                    except VocSourceError as exc:
                        errors.append(f"r/{sub} {kind}: {str(exc)[:80]}")
                        done += 1
                        continue
                    for r in rows:
                        d = voc_mod.normalize(r, kind, sub)
                        if d["id"] and d["id"] not in seen:
                            seen.add(d["id"])
                            fh.write(json.dumps(d, ensure_ascii=False) + "\n")
                            new_docs += 1
                    if b < now_ts - 2 * 86400:
                        state[key] = "done"
                    done += 1
                    _upd(scan_id, status, f"采集 r/{sub}（{done}/{total_jobs}，新增 {new_docs} 条）",
                         5 + int(55 * done / total_jobs))
        fh.flush()
    write_json(state_f, state)
    seen_f.write_text("\n".join(seen))

    _upd(scan_id, status, "清洗语料", 62)
    raw_docs = [json.loads(line) for line in raw_f.read_text(encoding="utf-8").splitlines() if line.strip()]
    raw_docs = [d for d in raw_docs if d.get("created_utc", 0) >= win_start and d.get("subreddit") in subs]
    union = re.compile("|".join(f"(?:{v['regex']})" for v in cat["products"].values()), re.I)
    cleaned = voc_mod.etl(raw_docs)
    clean_docs = cleaned["docs"]
    relevant_n = sum(1 for d in clean_docs if union.search(d["text"]))
    etl_stats = {**cleaned["stats"], "relevant": relevant_n,
                 "retention": round(relevant_n / cleaned["stats"]["raw"], 3) if cleaned["stats"]["raw"] else None}
    note = None
    if etl_stats["retention"] is not None:
        if etl_stats["retention"] < 0.30:
            note = "相关语料留存率低于 30%：版块偏泛或产品词典太窄，聚合结论代表性有限"
        elif etl_stats["retention"] > 0.80:
            note = "相关语料留存率高于 80%：产品词典可能太松，注意误命中"

    _upd(scan_id, status, "词典聚合", 70)
    agg = voc_mod.aggregate(clean_docs, {k: v["regex"] for k, v in cat["products"].items()})
    rows = []
    for a in agg:
        meta = cat["products"][a["id"]]
        rows.append({"id": a["id"], "zh": meta["zh"], "keyword": meta["keyword"], "mentions": a["mentions"],
                     "pain_rate": a["pain_rate"], "reco_rate": a["reco_rate"], "trend_pct": a["trend_pct"],
                     "months": a["months"], "pain_terms": voc_mod.top_terms(a["pain_docs"][:300], 5)})
    by_id = {a["id"]: a for a in agg}

    ranked = sorted([r for r in rows if r["mentions"] >= 30 and r["pain_rate"] is not None],
                    key=lambda r: -(r["mentions"] * r["pain_rate"]))
    top_n = ranked[: int(p.get("annotate_top") or 0)]
    for i, r in enumerate(top_n):
        _upd(scan_id, status, f"AI 深读 {r['zh']}（{i + 1}/{len(top_n)}）", 75 + int(20 * i / max(len(top_n), 1)))
        ann = await annotate(r["keyword"], by_id[r["id"]]["pain_docs"])
        r["annotation"] = ann
        if ann["ok"]:
            top = max(ann["data"]["pain_points"], key=lambda x: x.get("pct") or 0)
            r["top_pain_pct"], r["top_pain_name"] = top.get("pct"), top.get("name")

    result = {"scan_id": scan_id, "category_id": cat["id"], "category_name": cat.get("name"),
              "months": months, "subreddits": subs, "etl": etl_stats, "etl_note": note,
              "corpus_docs": len(clean_docs), "new_docs": new_docs, "errors": errors[:10],
              "products": rows, "requests": client.requests, "generated_at": _now(),
              "source": "Arctic Shift（免费 Reddit 存档）"}
    write_json(_scan_dir(scan_id) / "result.json", result)
    write_text(_scan_dir(scan_id) / "report.md", render_scan_md(result))
    _upd(scan_id, status, "完成", 100, status="done")


def render_scan_md(r: Dict[str, Any]) -> str:
    e = r["etl"]
    L = [f"# VOC 品类扫描 · {r['category_name']}", "",
         f"> 近 {r['months']} 个月 ｜ 版块 {len(r['subreddits'])} 个 ｜ 语料 {r['corpus_docs']} 条（新增 {r['new_docs']}）｜ "
         f"生成 {r['generated_at']} ｜ 数据源：{r['source']}", "",
         f"清洗留存：原始 {e['raw']} → 去重 {e['dedup']} → 有效 {e['valid']} → 英文 {e['english']} → 命中品类词 {e['relevant']}"
         f"（{(e['retention'] or 0):.0%}；基准 40–60%）。", ""]
    if r.get("etl_note"):
        L += [f"> ⚠️ {r['etl_note']}", ""]
    if r["errors"]:
        L += [f"> ⚠️ {len(r['errors'])} 处采集失败（已跳过）：" + "；".join(r["errors"][:3]), ""]
    L += ["## 品类信号排序", "",
          "社媒只提出假设：声量、痛点率、推荐率只是排序依据，要进「社媒 VOC 前导选品」用亚马逊数据裁决。", "",
          "| 品类 | 提及量 | 痛点率 | 推荐率 | 注意力趋势 | 最大痛点（AI 深读） |", "|---|---|---|---|---|---|"]
    for p in r["products"]:
        tr = f"{p['trend_pct']:+.0f}%" if p.get("trend_pct") is not None else "—"
        pr = f"{p['pain_rate']:.0%}" if p.get("pain_rate") is not None else "—"
        rr = f"{p['reco_rate']:.0%}" if p.get("reco_rate") is not None else "—"
        top = f"{p.get('top_pain_name')}（{p.get('top_pain_pct'):g}%）" if p.get("top_pain_name") and p.get("top_pain_pct") is not None else "—"
        L.append(f"| {p['zh']}（{p['keyword']}） | {p['mentions']} | {pr} | {rr} | {tr} | {top} |")
    L.append("")
    for p in r["products"]:
        ann = (p.get("annotation") or {})
        if not ann.get("ok"):
            continue
        d = ann["data"]
        L += [f"## {p['zh']} · 痛点深读", ""]
        for x in d["pain_points"]:
            mark = "" if x.get("quote_verified") else "（原话未能在样本中核实）"
            L.append(f"- **{x['name']}**（{x.get('pct') if x.get('pct') is not None else '—'}%）：{x['detail']}"
                     + (f"\n  > {x['quote']}{mark}" if x.get("quote") else ""))
        if d.get("unmet_needs"):
            L += ["", "未满足需求：" + "；".join(d["unmet_needs"])]
        if d.get("opportunity"):
            L += ["", f"差异化机会：{d['opportunity']}"]
        L.append("")
    L += ["---", "下一步：把候选词丢进新建选品，方法选「社媒 VOC 前导选品」，由亚马逊需求与竞争数据证实或证伪。"]
    return "\n".join(L) + "\n"
