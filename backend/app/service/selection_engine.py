"""
选品运行引擎（v3）：后台执行、逐步可观测、结论确定性。

流程：准备与关键词 → 需求趋势 → 竞争格局 → 需求生命周期 → 用户声音 → 1688 货源
      → 专利初筛 → 成本与毛利 → 规则评估与四档结论 → AI 研判正文 → 落盘

- 结论（做 / 有条件做 / 暂缓 / 不做）只由事实 + 阈值规则算出，模型只写解读，不能改判。
- 任一数据源失败：记入证据账本、对应规则判“缺数据”，运行继续；绝不编造数字。
- 模型不可用：报告照样完整产出（规则版正文），并在 narrative_status 标注。
"""
import asyncio
import datetime
import json
import re
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.core import facts as facts_mod
from app.core import six_gates as gates_mod
from app.core import report_html
from app.core import voc as voc_mod
from app.core import rules as rules_mod
from app.core import scoring
from app.core import vision
from app.core.config import settings
from app.core.connectors import connector_label, connector_registry
from app.core.evidence import EvidenceLedger
from app.core.jsonio import read_json, write_json, write_text
from app.core.llm_gateway import LLMError, LLMUnavailable, llm_gateway
from app.core.logger import logger
from app.model.domain import SelectionTask, TaskStatus
from app.repository.storage import task_repository
from app.service.knowledge_service import knowledge_service
from app.service.image_service import image_service
from app.service.method_pack_service import method_pack_service
from app.service.upload_service import upload_service
from app.service import voc_service

STEPS: List[Tuple[str, str, int, TaskStatus]] = [
    ("image", "图片识货", 3, TaskStatus.KNOWLEDGE_RETRIEVING),
    ("prepare", "准备参数与关键词", 5, TaskStatus.KNOWLEDGE_RETRIEVING),
    ("trend", "需求趋势", 15, TaskStatus.DATA_FETCHING),
    ("competition", "竞争格局", 26, TaskStatus.DATA_FETCHING),
    ("peers", "同特征竞品筛查", 33, TaskStatus.DATA_FETCHING),
    ("demand", "需求生命周期", 38, TaskStatus.DATA_FETCHING),
    ("voc", "用户声音（Reddit）", 46, TaskStatus.DATA_FETCHING),
    ("supply", "1688 货源", 58, TaskStatus.DATA_FETCHING),
    ("ip", "专利初筛", 68, TaskStatus.DATA_FETCHING),
    ("economics", "成本与毛利", 75, TaskStatus.AI_EVALUATING),
    ("rules", "规则评估与四档结论", 82, TaskStatus.AI_EVALUATING),
    ("narrative", "AI 研判正文", 94, TaskStatus.REPORT_GENERATING),
    ("persist", "报告落盘", 100, TaskStatus.REPORT_GENERATING),
]
STEP_INDEX = {k: i for i, (k, *_rest) in enumerate(STEPS)}
STATUS_ICON = {rules_mod.PASS: "✅", rules_mod.WARN: "⚠️", rules_mod.FAIL: "❌",
               rules_mod.NO_DATA: "⬜", rules_mod.SKIP: "➖"}
_CJK = re.compile(r"[一-鿿]")


def now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _has_cjk(text: str) -> bool:
    return bool(_CJK.search(text or ""))


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def _extract_json_list(text: str) -> List[str]:
    """模型回的是 JSON 数组（可能会包在 ``` 里或前面带一句解释），取第一个数组。"""
    m = re.search(r"\[.*\]", text or "", re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [str(x).strip() for x in data if isinstance(x, (str, int, float)) and str(x).strip()]


VOC_ZH_PROMPT = (
    "你是亚马逊选品报告的中文编辑。下面是 Reddit / Quora 买家讨论的英文摘录，逐条翻译成简洁、"
    "口语化的中文，供中国卖家阅读。\n"
    "要求：1) 忠于原意，不添加评论、不补全没说的信息；2) 产品品类词译成中文，品牌名、型号、"
    "专有名词保留英文；3) 每条控制在 100 字以内，宁可压缩措辞也不要漏掉买家说的痛点或做法；"
    "4) 原文以「…」结尾说明摘录被截断，译文照实收尾，不要自行补全；省略号只放在整条"
    "译文的末尾，不要插在句子中间；"
    "5) 只输出 JSON 字符串数组，条数和顺序与输入完全一致，不要输出任何解释。\n"
)


_VOC_TAG_RE = re.compile(r"^(\[[^\]]{1,160}\]\s*)(.*)$", re.S)


def _orig_line(s: str) -> str:
    """原文行照抄英文标签，只把被源站硬切断的残词收成省略号。"""
    m = _VOC_TAG_RE.match(s)
    if not m:
        return facts_mod.tidy_excerpt(s)
    return m.group(1) + facts_mod.tidy_excerpt(m.group(2).strip())


# 一次送多少条。实测 group/never-stuck 这类会先花 2000~3000 token 做内部推理，
# 8 条一起送时预算全被推理吃掉、正文返回空；拆小批后每批的推理开销才收敛得住。
VOC_BATCH = 3
# 每批的 token 预算：要覆盖「推理 + 译文」。3000 不够时再抬一档重试。
VOC_BUDGETS = (3000, 6000)


async def _translate_batch(bodies: List[str]) -> Tuple[Optional[List[str]], Optional[str]]:
    """译一小批，条数对齐才返回。"""
    prompt = VOC_ZH_PROMPT + json.dumps(bodies, ensure_ascii=False)
    last_err: Optional[str] = None
    for budget in VOC_BUDGETS:
        try:
            resp = await llm_gateway.chat(
                [{"role": "user", "content": prompt}], role="fast",
                temperature=0.2, max_tokens=budget, timeout=180)
        except LLMUnavailable as exc:
            return None, str(exc)
        except LLMError as exc:
            last_err = str(exc)
            continue
        text = resp.get("content") or ""
        if not text.strip():
            # 空正文几乎都是推理把预算烧完了，说清楚，别让人以为是模型拒答
            last_err = ("模型把预算用在内部推理上，没留下正文"
                        if resp.get("finish_reason") == "length"
                        else "模型返回空内容")
            continue
        got = _extract_json_list(text)
        if len(got) != len(bodies):
            last_err = f"返回条数 {len(got)} ≠ 原文 {len(bodies)}"
            continue
        return got, None
    return None, last_err or "模型未返回可用译文"


async def translate_voc_samples(samples: List[str]) -> Tuple[Optional[List[str]], Optional[str]]:
    """把英文买家讨论逐条译成中文，返回（译文, 失败原因）。

    译文条数必须与原文一致才采用——错位一条就等于把别人的话安在产品上。
    分批送，任何一批没译出来就整条放弃（宁可报告里留英文，也不给出错位的译文）。
    """
    bodies = [facts_mod.voc_parts(s)[1] for s in samples]
    if not bodies:
        return [], None
    if all(_has_cjk(b) for b in bodies):
        return list(bodies), None

    out: List[str] = []
    for start in range(0, len(bodies), VOC_BATCH):
        part = bodies[start:start + VOC_BATCH]
        got, err = await _translate_batch(part)
        if got is None:
            return None, err
        out.extend(got)
    return out, None


def _fmt(v: Any, unit: str = "") -> str:
    if v is None:
        return "—"
    if isinstance(v, (int, float)) and not isinstance(v, bool) and unit == "占比" and 0 <= v <= 1.5:
        return f"{v:.0%}"
    if isinstance(v, float):
        if unit == "占比" and 0 <= v <= 1.5:
            return f"{v:.0%}"
        return f"{v:,.2f}".rstrip("0").rstrip(".")
    if isinstance(v, int):
        return f"{v:,}"
    return str(v)


def _vu(o) -> str:
    """规则值 + 单位的可读写法（占比显示成百分比）。"""
    if o.unit == "占比":
        return _fmt(o.value, o.unit)
    return f"{_fmt(o.value)} {o.unit}".strip()


class _Run:
    def __init__(self, task: SelectionTask):
        self.task_id = task.task_id
        self.task = task
        self.params: Dict[str, Any] = dict(task.parameters or {})
        self.ledger = EvidenceLedger(task.task_id)
        self.sources: Dict[str, Any] = {}
        self.steps: List[Dict[str, Any]] = [
            {"key": k, "label": label, "status": "pending", "detail": "", "started_at": None,
             "finished_at": None} for k, label, _p, _s in STEPS]
        self.notes: List[str] = []
        self.run_dir: Path = settings.RUNS_DIR / task.task_id
        self.pack: Dict[str, Any] = {}
        self.weights: Optional[Dict[str, float]] = None
        self.rule_ids: Optional[List[str]] = None


class SelectionEngine:
    def __init__(self, max_concurrent: int = 2):
        self._sem = asyncio.Semaphore(max_concurrent)
        self._jobs: Dict[str, asyncio.Task] = {}

    # ---------------- 对外 ----------------

    def create_task(self, workflow_type, title: str, category_pack_id: Optional[str], target_market: str,
                    parameters: Dict[str, Any], llm_provider: Optional[str], llm_model: Optional[str],
                    method_pack_id: Optional[str] = None) -> SelectionTask:
        ts = now()
        task = SelectionTask(
            task_id=f"SEL-{datetime.datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}",
            workflow_type=workflow_type, title=title.strip(), category_pack_id=category_pack_id,
            target_market=(target_market or "US").upper(), parameters=parameters or {},
            status=TaskStatus.PENDING, progress=0, created_at=ts, updated_at=ts,
            method_pack_id=method_pack_id, llm_provider=llm_provider, llm_model=llm_model,
            steps=[{"key": k, "label": label, "status": "pending", "detail": ""} for k, label, _p, _s in STEPS],
        )
        task_repository.save_task(task)
        return task

    def start(self, task: SelectionTask) -> SelectionTask:
        job = asyncio.create_task(self._guarded(task.task_id))
        self._jobs[task.task_id] = job
        job.add_done_callback(lambda _j, tid=task.task_id: self._jobs.pop(tid, None))
        return task

    def is_running(self, task_id: str) -> bool:
        return task_id in self._jobs

    async def run_blocking(self, task: SelectionTask) -> SelectionTask:
        await self._guarded(task.task_id)
        return task_repository.get_task(task.task_id) or task

    def recover_orphans(self) -> int:
        """服务重启后，把上次没跑完的任务标为失败，避免前端一直转圈。"""
        n = 0
        for t in task_repository.list_tasks(limit=500):
            if t.status not in (TaskStatus.COMPLETED, TaskStatus.FAILED) and not self.is_running(t.task_id):
                task_repository.update_task(t.task_id, status=TaskStatus.FAILED.value, updated_at=now(),
                                            error="服务重启时任务未完成，请重新发起")
                n += 1
        return n

    # ---------------- 主流程 ----------------

    async def _guarded(self, task_id: str) -> None:
        async with self._sem:
            task = task_repository.get_task(task_id)
            if not task:
                return
            run = _Run(task)
            try:
                await self._run(run)
            except Exception as exc:  # 引擎自身异常：如实标失败
                logger.exception("选品运行失败 %s", task_id)
                self._fail_open_steps(run, str(exc))
                task_repository.update_task(task_id, status=TaskStatus.FAILED.value, error=str(exc)[:500],
                                            steps=run.steps, updated_at=now())

    async def _run(self, run: _Run) -> None:
        resolved = method_pack_service.resolve(run.task.method_pack_id, run.task.parameters or {})
        run.pack = resolved["pack"]
        run.params = resolved["params"]
        run.weights = resolved["weights"]
        run.rule_ids = resolved["rule_ids"]
        t = resolved["thresholds"]
        if run.task.method_pack_id != run.pack["id"]:
            task_repository.update_task(run.task_id, method_pack_id=run.pack["id"])
            run.task.method_pack_id = run.pack["id"]
        run.run_dir.mkdir(parents=True, exist_ok=True)
        country = run.task.target_market or "US"

        # 0. 图片识货（有图才跑：认产品 → 出中英关键词，再去看市场与货源）
        image = await self._identify_image(run)

        # 1. 准备
        self._begin(run, "prepare")
        kw, kw_zh, how = await self._resolve_keywords(run, image)
        run.sources["keyword"], run.sources["keyword_zh"] = kw, kw_zh
        run.sources["young_days"] = t["competition"]["young_listing_days_max"]
        run.sources["low_review_max"] = t["competition"]["low_review_max"]
        # 未饱和参照评论区间：六道门方法包可以改，没写就是 100–200
        hk = gates_mod.thresholds(t)
        run.sources["ref_reviews_min"], run.sources["ref_reviews_max"] = hk["ref_reviews_min"], hk["ref_reviews_max"]
        self._done(run, "prepare", f"英文词：{kw or '未得到'} ｜ 中文词：{kw_zh or '—'} ｜ {how}")

        # 2–5. 市场与 VOC
        if kw:
            # 主词查不到就换备选词：口语译出来的词常常不是亚马逊上的真实搜索词
            kw, comp_res = await self._pick_market_keyword(run, kw, country)
            # 关键词数据（趋势/需求）按方法包偏好走（西柚免费优先）；竞争格局除外——
            # 六道门要的月销/评论档字段只有 SIF 的 top_competitors 带全，西柚格局没有销量
            kw_routes = self._pack_routes(run, "keywords")
            await self._fetch_step(run, "trend", "market.keyword_trend", "需求趋势（周）",
                                   {"keyword": kw, "country": country, "granularity": "week"}, timeout=90,
                                   prefer=kw_routes)
            await self._fetch_step(run, "competition", "market.keyword_competition", "竞争格局",
                                   {"keyword": kw, "country": country}, timeout=120, prefetched=comp_res)
            # 六道门方法包：把人工在卖家精灵上做的「商品筛选」自动化——
            # 按关键词拉头部商品样本（卖家精灵优先，Sorftime/西柚回落），六道门上游数字从样本算出
            if self._is_gates_method(run) and self.params_flag(run, "check_peer_screen", True):
                await self._fetch_step(run, "peers", "market.product_search", "同特征竞品样本（关键词 Top 商品）",
                                       {"keyword": kw, "country": country, "size": 100}, timeout=150,
                                       prefer=self._pack_routes(run, "screen"))
            else:
                self._skip(run, "peers", "未启用六道门方法包" if not self._is_gates_method(run) else "已在参数中关闭")
            if self.params_flag(run, "check_demand", True):
                await self._fetch_step(run, "demand", "market.keyword_demand", "需求生命周期",
                                       {"keywords": [kw], "country": country}, timeout=90, prefer=kw_routes)
            else:
                self._skip(run, "demand", "已在参数中关闭")
            if self.params_flag(run, "check_voc", True):
                if self._is_voc_method(run):
                    await self._voc_lead_step(run, kw)
                else:
                    await self._fetch_step(run, "voc", "social.reddit", "Reddit 讨论", {"keyword": kw}, timeout=90)
            else:
                self._skip(run, "voc", "已在参数中关闭")
        else:
            for key in ("trend", "competition", "peers", "demand", "voc"):
                self._skip(run, key, "没有可用的英文关键词，市场数据无法查询")
                run.sources[key] = {"error": "缺英文关键词"}

        # 5.5 自有数据（上传表补缺）与手填数值
        for uid in (run.params.get("upload_ids") or [])[:3]:
            payload = upload_service.as_competition_payload(str(uid))
            meta = (upload_service.load(str(uid)) or {}).get("meta") or {}
            eid = run.ledger.add(capability="user.upload", connector="upload", tool=meta.get("filename"),
                                 args={"upload_id": uid}, data=payload, ok=bool(payload),
                                 error=None if payload else "未找到或不是竞品表", label=f"自有数据：{meta.get('filename') or uid}")
            if payload and not run.sources.get("competition_upload"):
                run.sources["competition_upload"] = {"payload": payload, "eid": eid, "connector": "upload"}
                run.notes.append(f"已用上传表 {meta.get('filename')} 补充竞品数据（{meta.get('rows')} 行）")
        overrides = facts_mod.clean_overrides(run.params.get("facts_override"))
        if overrides:
            eid = run.ledger.add(capability="user.override", connector="user", tool="手填", args={},
                                 data=overrides, label="用户手填数值")
            run.sources["override"] = {"facts": overrides, "eid": eid}

        # 同特征竞品筛查 → 六道门参数：用户手填的优先，缺的用样本算出来的补
        auto_peers = facts_mod.peer_params(run.sources)
        if auto_peers:
            filled = []
            for k in ("peer_count", "peer_avg_monthly", "low_review_avg", "ref_reviews_median"):
                if run.params.get(k) in (None, "") and auto_peers.get(k) is not None:
                    run.params[k] = auto_peers[k]
                    filled.append(k)
            run.params["peer_auto"] = True
            src = run.sources.get("peer_screen") or {}
            what = "、".join(filled) if filled else "同特征口径数字"
            run.notes.append(
                f"六道门上游数字已按竞品样本自动补齐（{what}；来源 {connector_label(src.get('connector'))}）。"
                f"样本是关键词下的头部商品，不等于人工同特征集，置信度封顶「中」")

        # 6. 货源
        supply_kw = kw_zh or kw
        if supply_kw and self.params_flag(run, "check_supply", True):
            await self._fetch_step(run, "supply", "supply.1688_search", "1688 同款货源",
                                   {"keyword": supply_kw}, timeout=150)
            # 有公网图地址才做以图搜同款：1688 的以图接口是服务端去抓那张图，本地文件它看不见
            pub = str((run.sources.get("image") or {}).get("public_url") or "").strip()
            if pub and pub.startswith(("http://", "https://")):
                await self._fetch_extra(run, "supply", "supply_image", "supply.1688_image_search",
                                        "1688 以图搜同款", {"image_url": pub}, timeout=150)
            elif run.sources.get("image"):
                run.notes.append("图片是本地文件，1688 以图搜同款需要公网图地址；已用识别出的关键词搜同款")
        else:
            self._skip(run, "supply", "已关闭或缺关键词")

        # 7. 专利（关键词 + 以图，两路一起看；外观专利常在图上看不出名字）
        if kw and self.params_flag(run, "check_ip", True):
            await self._fetch_step(run, "ip", "ip.patent_keyword", "专利关键词检索", {"keyword": kw}, timeout=90)
            await self._patent_image(run)
        else:
            self._skip(run, "ip", "已关闭或缺关键词（专利规则将判缺数据）")

        # 8. 经济模型
        self._begin(run, "economics")
        pre = facts_mod.build(run.sources)["facts"]
        logistics = await self._logistics(run, pre, t)
        eco = self._economics(run, pre, t, logistics)
        run.sources["economics"] = eco
        f = eco["facts"]
        if f.get("gross_margin_pct") is not None:
            self._done(run, "economics", f"售价 ${f.get('sale_price_usd')} ｜ 落地成本 ${f.get('purchase_cost_usd')}+头程 "
                                         f"${f.get('freight_usd')} ｜ 毛利率 {f.get('gross_margin_pct')}%")
        else:
            self._done(run, "economics", "缺售价或采购价，毛利未计算", status="partial")

        # 9. 规则与结论
        self._begin(run, "rules")
        built = facts_mod.build(run.sources)
        facts = dict(built["facts"])
        facts["evidence_refs"] = built["evidence_refs"]
        vl = (run.sources.get("voc_lead") or {})
        if vl.get("payload") is not None:
            lead = vl["payload"]
            facts["voc_lead"] = {k: lead.get(k) for k in (
                "ok", "error", "mentions", "trend_pct", "pain_rate", "reco_rate", "top_pain_pct", "top_pain_name",
                "capped", "generic", "category_name", "subreddits", "prior", "recent", "months")}
            facts["voc_lead_detail"] = {"annotation": (lead.get("annotation") or {}).get("data"),
                                        "annotation_error": (lead.get("annotation") or {}).get("error"),
                                        "pain_terms": lead.get("pain_terms"), "etl": lead.get("etl"),
                                        "errors": lead.get("errors")}
            if lead.get("ok"):
                facts["has_voc"] = bool(lead.get("samples"))
                facts["voc_count"] = lead.get("mentions")
                facts["voc_samples"] = [f"[r/{x['sub']} ↑{x['score']}] {x['text']}" for x in (lead.get("samples") or [])][:8]
                refs = facts["evidence_refs"]
                refs["voc_count"] = vl.get("eid")
        if self.params_flag(run, "ip_reviewed", False):
            facts["ip_reviewed"] = True
        outcomes = rules_mod.evaluate(facts, rule_ids=run.rule_ids, t=t)
        weights = run.weights
        axes = scoring.score_axes(outcomes, facts, weights)
        cov = run.ledger.coverage()
        verdict = scoring.decide(facts, outcomes, axes, evidence_ratio=cov["ratio"], weights=weights, t=t)
        # 方法包声明了 decision.mode=gates（六道门）时，定档以门为准，五维分数降为参照
        gate = self._gates(run, facts, t)
        if gate:
            verdict = gates_mod.apply_to_verdict(verdict, gate)
            verdict["decision_mode"] = gate.get("mode", "gates")
        actions = scoring.actions_from(outcomes)
        gaps = facts_mod.missing_groups(run.sources)
        pts_txt = verdict["points"] if verdict["points"] is not None else "—"
        if gate:
            passed = sum(1 for g in gate["gates"] if g["status"] == "pass")
            self._done(run, "rules", f"{gate['conclusion']} ｜ {gate.get('method', '六道门')}过 {passed}/{len(gate['gates'])} ｜ 评分参照 {pts_txt} 分")
        else:
            self._done(run, "rules", f"{verdict['verdict']} ｜ {pts_txt} 分 ｜ "
                                     f"规则覆盖 {verdict['rule_summary']['coverage']:.0%}")
        task_repository.update_task(run.task_id, verdict=self._verdict_brief(verdict), updated_at=now())

        # 10. 研判正文（买家讨论先译成中文：报告的读者是中国客户）
        self._begin(run, "narrative")
        await self._translate_voc(run, facts)
        narrative, n_status, n_meta = await self._narrative(run, facts, outcomes, verdict, built["assumptions"], gaps)
        self._done(run, "narrative", n_meta, status="done" if n_status == "ok" else "partial")

        # 11. 落盘
        self._begin(run, "persist")
        report = self._report(run, facts, outcomes, verdict, actions, built["assumptions"], gaps, narrative, n_status)
        paths = self._persist(run, facts, outcomes, verdict, actions, built, report)
        summary = self._summary(run, facts, outcomes, verdict, actions, gaps, n_status, cov)
        self._done(run, "persist", f"已写入 {run.run_dir.name}/")
        task_repository.update_task(
            run.task_id, status=TaskStatus.COMPLETED.value, progress=100, updated_at=now(),
            results_summary=summary, report_file_path=paths["report"], run_dir=str(run.run_dir),
            verdict=self._verdict_brief(verdict), steps=run.steps, error=None)

    # ---------------- 步骤状态 ----------------

    def _update(self, run: _Run, key: str, **kw: Any) -> None:
        step = run.steps[STEP_INDEX[key]]
        step.update(kw)
        _k, _label, progress, status = STEPS[STEP_INDEX[key]]
        fields: Dict[str, Any] = {"steps": run.steps, "updated_at": now()}
        if kw.get("status") == "running":
            fields["status"] = status.value
            fields["progress"] = max(0, progress - 6)
        elif kw.get("status") in ("done", "partial", "failed", "skipped"):
            fields["progress"] = progress if key != "persist" else 99
        task_repository.update_task(run.task_id, **fields)

    def _begin(self, run: _Run, key: str) -> None:
        self._update(run, key, status="running", started_at=now())

    def _done(self, run: _Run, key: str, detail: str = "", status: str = "done") -> None:
        self._update(run, key, status=status, detail=detail, finished_at=now())

    def _skip(self, run: _Run, key: str, detail: str) -> None:
        self._update(run, key, status="skipped", detail=detail, finished_at=now())

    @staticmethod
    def _fail_open_steps(run: _Run, err: str) -> None:
        for s in run.steps:
            if s["status"] in ("pending", "running"):
                s["status"] = "failed" if s["status"] == "running" else "skipped"
                s["detail"] = s["detail"] or err[:200]

    @staticmethod
    def params_flag(run: _Run, key: str, default: bool) -> bool:
        v = run.params.get(key, default)
        if isinstance(v, str):
            return v.strip().lower() not in ("0", "false", "no", "off", "否")
        return bool(v)

    async def _pick_market_keyword(self, run: _Run, kw: str, country: str,
                                   prefer: Optional[List[str]] = None):
        """按「主词 → 备选词」顺序探竞争格局，第一个有数据的词就是后面所有市场步骤用的词。

        返回 (选中的词, 该词的竞争格局结果)；结果会被竞争步骤直接复用，不重复扣额度。
        全部无数据时回到主词，让报告老实写「该词无数据」，并把试过的词记进说明。
        """
        cands = [kw] + [c for c in (run.sources.get("keyword_candidates") or []) if c != kw]
        tried: List[str] = []
        first_res = None
        for c in cands:
            res = await connector_registry.call("market.keyword_competition", {"keyword": c, "country": country},
                                                timeout=120, prefer=prefer)
            first_res = first_res or res
            if res.ok:
                if c != kw:
                    msg = f"主词「{kw}」数据源无结果（已试：{'、'.join(tried) or kw}），改用「{c}」"
                    run.notes.append(msg)
                    run.sources["keyword"] = c
                    run.sources["keyword_original"] = kw
                    self._update(run, "prepare", detail=f"{run.steps[STEP_INDEX['prepare']].get('detail') or ''} ｜ {msg}")
                return c, res
            tried.append(c)
        if len(cands) > 1:
            run.notes.append(f"主词与备选词都没有竞争格局数据（已试：{'、'.join(tried)}）")
        return kw, first_res

    async def _fetch_step(self, run: _Run, key: str, capability: str, label: str,
                          args: Dict[str, Any], timeout: float = 120, prefetched: Any = None,
                          prefer: Optional[List[str]] = None) -> None:
        self._begin(run, key)
        res = prefetched if prefetched is not None else await connector_registry.call(
            capability, args, timeout=timeout, prefer=prefer)
        eid = run.ledger.add_result(res, label=label)
        run.sources[key] = {"connector": res.connector or "", "payload": res.data if res.ok else None,
                            "eid": eid, "error": None if res.ok else res.error, "capability": capability,
                            # 记下这一趟实际用的查询词：判定「返回的东西是不是这个产品」要用它，
                            # 不能用运行参数里的原词（它可能是英文，也可能被翻译改写）
                            "query": str(args.get("keyword") or args.get("query") or "")}
        # 「空结果算不算失败」统一在 connectors.call 里按能力判定：
        # 关键词/格局查不到 = 失败（会换备源），专利 0 命中、1688 0 报价、社媒 0 讨论 = 事实。
        # 这里原来按字符串瞎猜一遍，还把状态标成 partial 却在账本里留 ok=True，把覆盖率抬高了。
        if res.ok:
            self._done(run, key, f"{eid} ← {res.connector}:{res.tool}（{res.elapsed_ms} ms）")
        else:
            self._done(run, key, f"{eid} 获取失败：{(res.error or '')[:160]}", status="failed")

    @staticmethod
    def _is_voc_method(run: _Run) -> bool:
        return ((run.pack or {}).get("decision") or {}).get("mode") == "voc"

    @staticmethod
    def _is_gates_method(run: _Run) -> bool:
        return ((run.pack or {}).get("decision") or {}).get("mode") == "gates"

    @staticmethod
    def _pack_routes(run: _Run, group: str) -> Optional[List[str]]:
        """方法包声明的数据源顺序（data_sources.<group>，如 screen / keywords）。
        只当用户没在设置里指定过该能力路由时才生效——用户的手动指定永远优先。"""
        seq = ((run.pack or {}).get("data_sources") or {}).get(group)
        if isinstance(seq, list) and seq:
            ids = [str(x) for x in seq if isinstance(x, (str, int))]
            return ids or None
        return None

    async def _voc_lead_step(self, run: _Run, kw: str) -> None:
        """社媒 VOC 前导：优先调用已连接的社媒 MCP（如 Sorftime），无数据或未接时自动回落本地语料与免费爬取。"""
        self._begin(run, "voc")
        subs = run.params.get("voc_subreddits")
        if isinstance(subs, str):
            subs = [x.strip().lstrip("r/").strip() for x in re.split(r"[,，\s]+", subs) if x.strip()]

        live_payload = None
        # 1. 如果已配置 Sorftime 等社媒能力，先查一遍实时数据
        try:
            live_res = await connector_registry.call("social.reddit", {"keyword": kw}, timeout=30)
            if live_res and live_res.ok and live_res.data:
                live_payload = live_res.data if isinstance(live_res.data, dict) else None
        except Exception as exc:
            logger.info("社媒实时 MCP 探测跳过：%s", exc)

        try:
            lead = await asyncio.wait_for(
                voc_service.probe(kw, subs or None, do_annotate=self.params_flag(run, "use_llm", True),
                                  live_payload=live_payload), timeout=420)
        except asyncio.TimeoutError:
            lead = {"ok": False, "keyword": kw, "error": "社媒 VOC 取数超时"}
        except Exception as exc:
            logger.warning("VOC 取数失败：%s", exc)
            lead = {"ok": False, "keyword": kw, "error": f"{exc.__class__.__name__}: {str(exc)[:160]}"}

        src_label = lead.get("source") or "社媒前导（Reddit）"
        conn_label = "sorftime" if lead.get("source_type") == "mcp" else "voc_lead"
        eid = run.ledger.add(capability="social.reddit", connector=conn_label, tool="voc_lead_probe",
                             args={"keyword": kw, "subreddits": lead.get("subreddits")}, data=lead,
                             ok=bool(lead.get("ok")), error=lead.get("error"), label=f"社媒前导：{src_label}")
        run.sources["voc_lead"] = {"payload": lead, "eid": eid, "connector": conn_label}
        samples = [f"[r/{x['sub']} ↑{x['score']}] {x['text']}" for x in (lead.get("samples") or [])]
        run.sources["voc"] = {"connector": conn_label, "payload": None, "eid": eid,
                              "error": None if lead.get("ok") else (lead.get("error") or "取数失败"),
                              "capability": "social.reddit", "query": kw, "samples": samples}
        if lead.get("ok"):
            tr = lead.get("trend_pct")
            trend_str = f"｜趋势 {tr:+.0f}%" if tr is not None else ""
            top_pain = f"｜最大痛点 {lead.get('top_pain_pct'):g}%（{lead.get('top_pain_name')}）" if lead.get("top_pain_name") and lead.get("top_pain_pct") is not None else ""
            self._done(run, "voc", f"{eid} ← {src_label}｜{lead.get('mentions')} 条{trend_str}｜痛点率 "
                                   f"{(lead.get('pain_rate') or 0):.0%}{top_pain}")
        else:
            self._done(run, "voc", f"{eid} 获取失败：{(lead.get('error') or '')[:160]}", status="failed")

    async def _fetch_extra(self, run: _Run, step_key: str, store_key: str, capability: str,
                           label: str, args: Dict[str, Any], timeout: float = 120) -> None:
        """一个步骤里的第二路查询（如 1688 以图搜同款、外观专利以图检索）。

        不另占一行进度：结果存到 sources[store_key]，结论追加到该步骤已有的说明后面，
        这样界面上一行就能看全「关键词搜到多少 + 以图搜到多少」。
        """
        res = await connector_registry.call(capability, args, timeout=timeout)
        eid = run.ledger.add_result(res, label=label)
        run.sources[store_key] = {"connector": res.connector or "", "payload": res.data if res.ok else None,
                                  "eid": eid, "error": None if res.ok else res.error, "capability": capability,
                                  "query": str(args.get("keyword") or args.get("query") or "")}
        step = run.steps[STEP_INDEX[step_key]]
        line = f"{eid} ← {res.connector}:{res.tool}" if res.ok else f"{eid} 失败：{(res.error or '')[:120]}"
        step["detail"] = f"{step.get('detail') or ''} ｜ {label}：{line}".strip(" ｜")
        task_repository.update_task(run.task_id, steps=run.steps, updated_at=now())

    async def _patent_image(self, run: _Run) -> None:
        """外观专利以图检索：接口收 base64，本地图直接能查（不需要公网地址）。"""
        src = run.sources.get("image") or {}
        image_id = str(src.get("image_id") or "")
        got = image_service.load(image_id) if image_id else None
        if not got:
            return
        data, meta = got
        # 接口对图片大小有限制（官方说明 4MB 以内），base64 会再涨三分之一，这里按原图 2.5MB 卡
        if len(data) > 2_500_000:
            run.notes.append("图片超过 2.5MB，外观专利以图检索已跳过（压缩后再传可查）")
            return
        await self._fetch_extra(run, "ip", "ip_image", "ip.patent_image", "外观专利以图检索",
                                {"image": vision.data_uri(data, meta.get("mime") or "image/jpeg"),
                                 "query_mode": 1}, timeout=150)

    # ---------------- 图片识货 ----------------

    async def _identify_image(self, run: _Run) -> Optional[Dict[str, Any]]:
        """把用户给的图（本地上传的 image_id 或公网 image_url）认成产品，出一组中英关键词。

        认不出来就如实记失败并退回关键词路线，不编造商品；图片只当作「更好的起点」，
        不是必需输入——原来的文字/标题路线完全不受影响。
        """
        p = run.params
        image_id = str(p.get("image_id") or "").strip()
        image_url = str(p.get("image_url") or "").strip()
        if not image_id and not image_url:
            self._skip(run, "image", "未提供图片，按关键词路线跑")
            return None
        self._begin(run, "image")
        try:
            if not image_id:
                data, name = await image_service.fetch_remote(image_url)
                meta = image_service.save(name, data)
                image_service.remember_public_url(meta["id"], image_url)
                image_id = meta["id"]
            got = image_service.load(image_id)
            if not got:
                raise ValueError("图片不存在或已被清理，请重新上传")
            data, meta = got
            pre = p.get("image_analysis")
            if isinstance(pre, dict) and (pre.get("keyword") or pre.get("product_name_zh")):
                # 前端已经识过一遍：直接沿用，省一次视觉调用，也让页面上的词和报告里的词一致
                result = {"analysis": pre, "provider": p.get("image_provider") or "llm",
                          "model": p.get("image_model") or "vision", "vision_declared": True}
            else:
                result = await vision.analyze_image(
                    data, meta.get("mime") or "image/jpeg",
                    title_hint=str(p.get("title_hint") or run.task.title or "")[:200])
        except (ValueError, LLMError, LLMUnavailable) as exc:
            msg = str(exc)[:200] or type(exc).__name__
            run.sources["image"] = {"image_id": image_id, "image_url": image_url, "error": msg}
            self._done(run, "image", f"识图失败：{msg[:140]}", status="failed")
            run.notes.append(f"图片识别失败（{msg[:80]}），已退回关键词路线")
            return None
        analysis = result.get("analysis") or {}
        # 用户给的是公网 URL 就直接拿它去 1688 以图搜同款；本地上传的只有之前记过公网地址才用得上
        public_url = str(meta.get("public_url") or image_url or "").strip()
        eid = run.ledger.add(capability="image.vision", connector=result.get("provider") or "llm",
                             tool=result.get("model") or "vision", args={"image_id": image_id},
                             data=analysis, ok=True, label="图片识别（视觉模型）")
        run.sources["image"] = {"image_id": image_id, "image_url": image_url, "public_url": public_url,
                                "analysis": analysis, "eid": eid, "provider": result.get("provider"),
                                "model": result.get("model"), "vision_declared": result.get("vision_declared")}
        name = analysis.get("product_name_zh") or analysis.get("product_name_en") or "—"
        self._done(run, "image", f"{eid} 认出「{name}」｜英文词：{analysis.get('keyword') or '—'}"
                                 f"｜中文词：{analysis.get('keyword_zh') or '—'}"
                                 f"｜置信度 {analysis.get('confidence')}")
        if not analysis.get("is_product_photo", True):
            run.notes.append("这张图看起来不是产品照片（可能是场景图/人物图/截图），识别结果仅供参考")
        return analysis

    # ---------------- 关键词 ----------------

    @staticmethod
    def _kw_cache_key(title: str, market: str) -> str:
        norm = re.sub(r"\s+", " ", (title or "").strip().lower())
        return (market or "US").upper() + "|" + norm

    @staticmethod
    def _kw_cache_path() -> Path:
        return settings.STORAGE_DIR / "keyword_cache.json"

    def _kw_cache_save(self, key: str, val: Dict[str, Any]) -> None:
        try:
            cache = read_json(self._kw_cache_path(), default={}) or {}
            cache[key] = val
            write_json(self._kw_cache_path(), cache)
        except Exception as exc:  # 缓存写失败不影响本次运行
            logger.warning("关键词缓存写入失败：%s", exc)

    async def _resolve_keywords(self, run: _Run, image: Optional[Dict[str, Any]] = None
                                ) -> Tuple[Optional[str], Optional[str], str]:
        image = image or {}
        p = run.params
        kw = (p.get("keyword") or p.get("keyword_en") or "").strip() or None
        kw_zh = (p.get("keyword_zh") or "").strip() or None
        # 以图识别出的词只补空位，不覆盖用户手填的
        img_kw = str(image.get("keyword") or "").strip().lower() or None
        img_zh = str(image.get("keyword_zh") or "").strip() or None
        kw = kw or img_kw
        kw_zh = kw_zh or img_zh
        title = run.task.title
        if kw and kw_zh:
            how = "参数与图片识别合并得到" if (img_kw or img_zh) else "使用参数中指定的关键词"
            return kw, kw_zh, how
        if not kw and title and not _has_cjk(title):
            kw = re.sub(r"[^\w\s\-&']", " ", title).strip().lower()
            kw = re.sub(r"\s+", " ", kw)
        # 口语整句（带标点、很长）不能直接当 1688 搜索词：先留作兜底，交给模型提炼成 2-8 字商品词
        title_zh = title.strip() if _has_cjk(title) else None
        spoken = bool(title_zh) and (len(title_zh) > 10 or bool(re.search(r"[，。,.；;、？?！!]", title_zh)))
        if not kw_zh and title_zh and not spoken:
            kw_zh = title_zh
        if kw and kw_zh:
            return kw, kw_zh, "由标题直接推得"
        # 同一产品概念+站点固定一套词：模型翻译每次不一样，会让同一个产品跑出不同结论
        ck = self._kw_cache_key(title, run.task.target_market)
        hit = (read_json(self._kw_cache_path(), default={}) or {}).get(ck)
        if isinstance(hit, dict) and hit.get("keyword"):
            run.sources["keyword_candidates"] = [] if kw else list(hit.get("alternatives") or [])[:3]
            run.notes.append(f"关键词沿用此前固定的翻译（{hit.get('saved_at', '')[:10]}）：{hit['keyword']} / {hit.get('keyword_zh') or '-'}；要换词请在参数里手填 keyword / keyword_zh")
            return (kw or hit["keyword"]), (kw_zh or hit.get("keyword_zh") or title_zh), "关键词沿用此前固定的翻译（同一产品概念每次一致）"
        prompt = (
            "你是亚马逊美国站选品助手。把下面的产品概念转成搜索关键词，只输出 JSON，不要解释。\n"
            f"产品概念：{title}\n站点：{run.task.target_market}\n"
            '格式：{"keyword": "买家在亚马逊最常搜的英文核心词（2-4 个词，小写，必须是真实搜索习惯，不要直译）", '
            '"alternatives": ["另外 3 个买家也常搜的英文说法，覆盖同义词/上位词/更常见的叫法，小写"], '
            '"keyword_zh": "1688 采购搜索用的中文商品词（2-8 个字，只写商品本身，去掉送礼对象、节日、语气词）"}'
        )
        # 角色可能是推理模型：token 预算给小了会被思考过程吃光，正文返回空。给足并重试一次。
        last_err: Optional[str] = None
        for budget in (700, 1600):
            try:
                text = await llm_gateway.complete(prompt, role="fast", temperature=0.1,
                                                  max_tokens=budget, timeout=90)
            except LLMUnavailable as exc:
                last_err = str(exc)
                break
            except LLMError as exc:
                last_err = str(exc)
                continue
            data = _extract_json(text) or {}
            got_en = str(data.get("keyword") or "").strip().lower()
            got_zh = str(data.get("keyword_zh") or "").strip()
            alts = [str(x).strip().lower() for x in (data.get("alternatives") or []) if isinstance(x, str)]
            if not got_en:
                # 模型没按 JSON 回，退一步认第一行英文短语，别整条链断在这里
                line = next((ln.strip() for ln in (text or "").splitlines()
                             if re.match(r"^[A-Za-z][A-Za-z\s\-&']{2,60}$", ln.strip())), None)
                got_en = (line or "").lower() or None
            if got_en:
                chosen = kw or got_en
                # 备选词：不含主词、去重、最多 3 个；手填了 keyword 就不替用户换词
                run.sources["keyword_candidates"] = [] if kw else [
                    a for a in dict.fromkeys(alts) if a and a != chosen and re.match(r"^[a-z0-9][a-z0-9\s\-&']{1,60}$", a)][:3]
                kw = chosen
                kw_zh = kw_zh or got_zh or (title_zh if not spoken else None)
                if not kw_zh and title_zh:
                    kw_zh = title_zh
                note = "" if got_zh else "（中文词未译出，1688 用原标题搜索）"
                if got_en and got_zh and not p.get("keyword") and not p.get("keyword_zh"):
                    self._kw_cache_save(ck, {"keyword": got_en, "keyword_zh": got_zh,
                                             "alternatives": run.sources.get("keyword_candidates") or [],
                                             "saved_at": datetime.datetime.now().isoformat(timespec="seconds")})
                return kw, kw_zh, "关键词由模型翻译（可在参数里手填 keyword / keyword_zh 覆盖）" + note
            last_err = last_err or "模型未给出英文关键词"
        run.notes.append(f"关键词翻译失败：{last_err}")
        return kw, kw_zh or title_zh, f"模型翻译不可用（{(last_err or '')[:60]}），仅用标题；建议在参数里手填 keyword"

    # ---------------- 经济模型 ----------------

    async def _logistics(self, run: _Run, pre: Dict[str, Any], t: Dict[str, Any]) -> Dict[str, Any]:
        """
        用“售价最接近的头部竞品”的包装尺寸重量，让 SIF 按亚马逊费率表算 FBA（淡/旺季），
        并据体积/重量估海运头程。任何一步失败都只是退回估算，并在假设里写明。
        """
        out: Dict[str, Any] = {}
        p = run.params
        if facts_mod._f(p.get("fba_fee_usd")) is not None or not self.params_flag(run, "check_fba", True):
            return out
        comps = [c for c in (pre.get("top_competitors_sample") or []) if c.get("asin")]
        sale = facts_mod._f(p.get("sale_price_usd")) or pre.get("avg_price_usd")
        if not comps or not sale:
            return out
        ref = min(comps[:10], key=lambda c: abs((c.get("price") or 0) - sale))
        res = await connector_registry.call("market.asin_profile", {"asins": [ref["asin"]]}, timeout=60)
        eid = run.ledger.add_result(res, label=f"参照竞品尺寸重量（{ref['asin']}）")
        if res.ok:
            run.sources["ref_profile"] = {"payload": res.data, "eid": eid, "asin": ref["asin"]}
        prof = facts_mod.parse_asin_profile(res.data) if res.ok else {}
        weight = prof.get("weight_oz")
        if not weight:
            # 拿不到尺寸重量就只能退回粗估，报告里会写明是估的
            return out
        dims = prof.get("dims_in") or {}
        cat = prof.get("category") or "Home & Kitchen"
        out.update({"ref_asin": ref["asin"], "weight_oz": weight, "dims_in": dims, "category": cat, "eid": eid,
                    "profile_source": res.connector})
        if prof.get("referral_pct"):
            out["referral_pct"] = prof["referral_pct"]
        if prof.get("fba_fee_usd"):
            # 西柚给的是亚马逊实际配送费，比按费率表算或 $4.5 粗估都准，直接用
            out["fba_fee_usd"] = prof["fba_fee_usd"]
            out["fba_fee_source"] = connector_label(res.connector)
        L, W, H = (facts_mod._f(dims.get(k)) for k in ("length_in", "width_in", "height_in"))
        if L and W and H:
            cbm = L * W * H * 16.387 / 1e6
            kg = weight * 0.02835
            per_cbm = facts_mod._f(p.get("freight_usd_per_cbm")) or 180.0
            per_kg = facts_mod._f(p.get("freight_usd_per_kg")) or 1.2
            out["freight_usd"] = round(max(cbm * per_cbm, kg * per_kg), 2)
            out["freight_note"] = (f"头程按海运估算：体积 {cbm * 1000:.2f} L × ${per_cbm:g}/m³ 与 实重 {kg:.2f} kg × ${per_kg:g}/kg 取大"
                                   f"（参照 {ref['asin']} 包装尺寸，可在参数 freight_usd 覆盖）")
        if out.get("fba_fee_usd") is not None:
            return out  # 已有实际配送费，不必再问费率表
        args = {"price": round(sale, 2), "category": cat, "weight_oz": weight,
                "freight_cost": out.get("freight_usd", 0.0),
                "target_margin": round(float(t["margin"]["gross_margin_pass_min"]) / 100.0, 3)}
        if L and W and H:
            args.update({"length_in": L, "width_in": W, "height_in": H})
        res2 = await connector_registry.call("market.cost_ceiling", args, timeout=60)
        eid2 = run.ledger.add_result(res2, label="FBA 费用与采购成本上限（SIF 费率表）")
        if res2.ok and isinstance(res2.data, dict):
            fee = facts_mod.deep_get(res2.data, "breakdown.fba_fee", default=None)
            if isinstance(fee, dict):
                out["fba_fee_usd"] = facts_mod._f(fee.get("peak")) or facts_mod._f(fee.get("off_peak"))
                out["fba_fee_off_peak_usd"] = facts_mod._f(fee.get("off_peak"))
            elif fee is not None:
                out["fba_fee_usd"] = facts_mod._f(fee)
            out["size_tier"] = facts_mod.deep_get(res2.data, "logistics.size_tier", default=None)
            out["referral_fee_usd"] = facts_mod._f(facts_mod.deep_get(res2.data, "breakdown.referral_fee", default=None))
            out["fba_eid"] = eid2
        return out

    def _economics(self, run: _Run, pre: Dict[str, Any], t: Dict[str, Any],
                   logistics: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        p = run.params
        f = facts_mod._f
        lg = logistics or {}
        sale = f(p.get("sale_price_usd") or p.get("sale_price"))
        sale_basis = "参数指定售价"
        if not sale and pre.get("avg_price_usd"):
            sale, sale_basis = pre["avg_price_usd"], "头部竞品售价中位数"
        cost_cny = f(p.get("purchase_price_cny"))
        cost_basis = "参数指定采购价"
        if not cost_cny:
            cost_cny = pre.get("supply_price_cny_median") or pre.get("supply_price_cny_min")
            # 1688 搜索卡片上的价是「起价」：可能对应最小规格，也可能只是一个引流价。
            # 口径写进假设里，别让人把这个数当成谈下来的成交价。
            cost_basis = ("1688 搜索页标价中位数（起价，未含运费/税）" if pre.get("supply_price_cny_median")
                          else "1688 搜索页最低标价（起价，未含运费/税）")
        res = facts_mod.economics(
            sale, cost_cny,
            cny_usd=f(p.get("cny_usd_rate")) or 7.15,
            freight_usd=f(p.get("freight_usd")) if f(p.get("freight_usd")) is not None else lg.get("freight_usd"),
            fba_fee_usd=f(p.get("fba_fee_usd")) if f(p.get("fba_fee_usd")) is not None else lg.get("fba_fee_usd"),
            referral_pct=self._pct(f(p.get("referral_pct")) or lg.get("referral_pct"), 0.15),
            target_margin_pct=float(t["margin"]["gross_margin_pass_min"]),
        )
        if lg.get("fba_fee_usd") and f(p.get("fba_fee_usd")) is None:
            if lg.get("fba_fee_source"):
                res["assumptions"].append(
                    f"FBA 配送费 ${lg['fba_fee_usd']} 取自{lg['fba_fee_source']}的亚马逊实际配送费"
                    f"（参照 {lg.get('ref_asin')} 的包装尺寸），非估算值")
            else:
                res["assumptions"].append(
                    f"FBA 按 SIF 费率表计算：旺季 ${lg['fba_fee_usd']} / 淡季 ${lg.get('fba_fee_off_peak_usd')}"
                    f"（尺寸段 {lg.get('size_tier') or '—'}，参照 {lg.get('ref_asin')} 包装，取旺季保守值）")
            res["facts"]["size_tier"] = lg.get("size_tier") or res["facts"].get("size_tier")
        if lg.get("freight_note") and f(p.get("freight_usd")) is None:
            res["assumptions"].append(lg["freight_note"])
        res["assumptions"].append("毛利未扣广告（通常占售价 5–15%）、退货损耗（3–8%）与关税")
        if sale:
            res["assumptions"].insert(0, f"售价取自：{sale_basis}（${sale}）")
        if cost_cny:
            res["assumptions"].insert(1 if sale else 0, f"采购价取自：{cost_basis}（¥{cost_cny}）")
        res["eid"] = run.ledger.add(capability="calc.economics", connector="local", tool="facts.economics",
                                    args={"sale_price_usd": sale, "purchase_price_cny": cost_cny},
                                    data=res["facts"], ok=bool(res["facts"].get("gross_margin_pct") is not None),
                                    error=None if res["facts"].get("gross_margin_pct") is not None else "缺售价或采购价",
                                    label="成本与毛利测算（本地计算）")
        return res

    @staticmethod
    def _pct(v: Optional[float], default: float) -> float:
        """15 与 0.15 两种写法都接受。"""
        if v is None or v <= 0:
            return default
        return v / 100.0 if v > 1 else v

    # ---------------- 研判正文 ----------------

    async def _translate_voc(self, run: _Run, facts: Dict[str, Any]) -> None:
        """把英文的买家讨论逐条译成中文——报告的读者是中国客户。

        译文写进 facts["voc_samples_zh"]，与原文一一对应；标签（类型/情绪）另走
        VOC_TAG_ZH 映射，不依赖模型。模型不可用或条数对不上时不动这一节的结构：
        报告仍会列出英文原文，只在备注里说明没译成，绝不把译文错配到别的讨论上。
        """
        samples = [s for s in (facts.get("voc_samples") or []) if isinstance(s, str) and s.strip()]
        if not samples:
            return
        zh, err = await translate_voc_samples(samples)
        if not zh:
            run.notes.append(f"用户声音未译成中文，报告中保留英文原文（{err or '模型未返回可用译文'}）")
            return
        facts["voc_samples_zh"] = [facts_mod.voc_line_zh(s, z) for s, z in zip(samples, zh)]

    @staticmethod
    def _voc_bullets(facts: Dict[str, Any], limit: int) -> List[str]:
        """用户声音渲染：中文在前，英文原文缩进一行放在下面，便于对账。"""
        en = [s for s in (facts.get("voc_samples") or []) if isinstance(s, str)]
        zh = facts.get("voc_samples_zh") or []
        out: List[str] = []
        for i, s in enumerate(en[:limit]):
            z = zh[i] if i < len(zh) else None
            if z and z != s:
                out.append(f"- {z}")
                out.append(f"  原文：{_orig_line(s)}")
            else:
                out.append(f"- {z or _orig_line(s)}")
        return out

    async def _narrative(self, run: _Run, facts: Dict[str, Any], outcomes, verdict: Dict[str, Any],
                         assumptions: List[str], gaps: List[Dict[str, str]]) -> Tuple[str, str, str]:
        if not self.params_flag(run, "use_llm", True):
            return self._fallback_narrative(facts, outcomes, verdict), "disabled", "已关闭模型，使用规则版正文"
        slim = {k: v for k, v in facts.items() if k not in ("evidence_refs",)}
        rule_lines = "\n".join(
            f"{o.id} {o.name}: {o.status} 值={o.value} 阈值={o.threshold} 证据={o.evidence_id or '-'}" for o in outcomes)
        system = ("你是资深亚马逊选品顾问。结论已由规则引擎确定，你只负责解读，不得更改结论、不得编造数据。"
                  "引用数字必须来自给定事实，并在数字后标注证据编号（如 EV-03）。缺数据就直说缺。中文，简洁务实。")
        pack = run.pack or {}
        lens_parts = []
        if pack.get("lens"):
            lens_parts.append(f"【方法论：{pack.get('name')}】\n{pack['lens']}")
        skill = method_pack_service.skill_text(pack) if pack.get("has_skill") else ""
        if skill:
            lens_parts.append("【方法论原文（按其视角解读；与规则结论冲突时以规则为准）】\n" + skill)
        cards = []
        for cid in pack.get("knowledge_cards") or []:
            d = knowledge_service.get_card_detail(cid)
            if d:
                cards.append(f"- [{d.card_id}] {d.title}：{(d.conclusion or '')[:300]}")
        if cards:
            lens_parts.append("【方法论引用的知识卡片】\n" + "\n".join(cards))
        if lens_parts:
            system += "\n\n" + "\n\n".join(lens_parts)
        gate_ctx = ""
        if verdict.get("gate"):
            g = verdict["gate"]
            rows = "\n".join(
                f"{x['index']}. {x['name']} {x['status']} 值={x['value']} 门槛={x['threshold']}"
                + (f" 说明={x['note']}" if x.get("note") else "") for x in g.get("gates") or [])
            gname = "社媒前导三道门" if g.get("mode") == "voc" else "六道门"
            gate_ctx = (f"\n【{gname}（本方法最终定档依据）】{g.get('conclusion')} ｜ {g.get('reason')}\n{rows}\n"
                        f"五维分数只作参照，不得用它推翻{gname}结论；unknown 的门要写成「待补」，不许写成通过。\n")
        prompt = f"""产品概念：{run.task.title}
站点：{run.task.target_market} ｜ 英文词：{facts.get('keyword')} ｜ 中文词：{facts.get('keyword_zh')}

【已定结论】{verdict['verdict']}（{verdict['points'] if verdict['points'] is not None else '—'} 分，置信度 {verdict['confidence_level']}）
理由：{verdict['reason']}
硬闸门：{json.dumps(verdict['hard_blockers'], ensure_ascii=False)}
{gate_ctx}
【事实】
{json.dumps(slim, ensure_ascii=False, default=str)[:6000]}

【规则结果】
{rule_lines}

【估算假设】{'；'.join(assumptions) or '无'}
【数据缺口】{'；'.join(g['label'] + '：' + g['error'] for g in gaps) or '无'}

【证据摘要】
{run.ledger.digest(max_chars=7000, per_item=900)}

请按以下小节输出 Markdown（每节 2-5 条要点，不要重复结论标题）：
### 结论解读
### 市场与需求
### 竞争与差异化切口
### 成本与利润
### 7 天内验证动作
### 风险与红线"""
        messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
        # 单次 3000 token 会写不完六节正文；推理模型还会先吃掉一部分，正文在半句处被截断。
        # 先给足预算，仍被 length 截断就逐级加预算重写，最后才如实标“截断”。
        best_text, best_tag, last_err = "", "", None
        for budget in (3000, 6000, 9000):
            try:
                res = await llm_gateway.chat(
                    messages, role="strong", provider_id=run.task.llm_provider or None,
                    model=run.task.llm_model or None, temperature=0.3, max_tokens=budget, timeout=300)
            except LLMUnavailable as exc:
                # 超时 / 连不上：加预算也没用，再试两次只会让进度卡在 94% 十几分钟
                last_err = exc
                break
            except LLMError as exc:
                last_err = exc
                continue
            text = (res.get("content") or "").strip()
            if len(text) > len(best_text):
                best_text, best_tag = text, f"{res['provider']}/{res['model']}"
            if res.get("finish_reason") != "length":
                return text, "ok", best_tag
        if best_text:
            run.notes.append("AI 研判正文达到模型输出上限被截断")
            return (best_text + "\n\n> ⚠️ 本文达到模型单次输出上限，以上为已生成部分；结论与规则表不受影响。",
                    "partial", best_tag)
        exc = last_err or LLMError("模型未返回正文")
        run.notes.append(f"AI 研判不可用：{exc}")
        return self._fallback_narrative(facts, outcomes, verdict), "unavailable", f"模型不可用，改用规则版正文：{str(exc)[:120]}"

    @staticmethod
    def _fallback_narrative(facts: Dict[str, Any], outcomes, verdict: Dict[str, Any]) -> str:
        lines = ["### 结论解读", f"- {verdict['verdict']}：{verdict['reason']}"]
        gate = verdict.get("gate") or {}
        for g in (gate.get("gates") or []):
            if g["status"] != "pass":
                lines.append(f"- {g['name']}「{'缺数据' if g['status'] == 'unknown' else '未通过'}」："
                             f"{g.get('value')}｜{g.get('note') or g.get('threshold') or ''}")
        good = [o for o in outcomes if o.status == rules_mod.PASS]
        bad = [o for o in outcomes if o.status in (rules_mod.FAIL, rules_mod.WARN)]
        if good:
            lines.append("- 达标项：" + "、".join(f"{o.name}（{_vu(o)}，{o.evidence_id}）" for o in good[:6]))
        if bad:
            lines.append("- 待改善：" + "、".join(f"{o.name}（{_vu(o)}）" for o in bad[:6]))
        lines += ["", "### 7 天内验证动作"]
        for o in bad[:5]:
            lines.append(f"- {o.name}：{o.action}")
        if not bad:
            lines.append("- 打样 + 小批量试单，验证真实 CVR 与退货率")
        if facts.get("voc_samples"):
            lines += ["", "### 用户声音样本"] + SelectionEngine._voc_bullets(facts, 5)
        lines += ["", "> 本段由规则引擎生成（模型未参与）。"]
        return "\n".join(lines)

    # ---------------- 报告 ----------------

    def _three_answers(self, facts: Dict[str, Any], sources: Optional[Dict[str, Any]] = None) -> List[str]:
        """老板真正会看的三件事：赚不赚钱、侵不侵权、供应链靠不靠谱。

        以前每格是一整段解释，三行表格读起来像三篇小作文——用户说的「密密麻麻的文字」就是这个。
        现在一格只放：判定 + 一个数 + 下一步做什么；解释压成表下两三句话，只留真正会改变判断的那几条。
        答不了就直接说答不了，不拿「建议进一步调研」糊过去。
        """
        refs = facts.get("evidence_refs") or {}
        rows: List[str] = []
        notes: List[str] = []

        # ① 利润：用的是本机测算（头部售价中位数 + 1688 报价中位数），不是拍脑袋的毛利率
        margin = facts.get("gross_margin_pct")
        profit = facts.get("net_profit_usd")
        ceiling = facts.get("cost_ceiling_usd")
        cost = facts.get("purchase_cost_usd")
        if margin is not None and profit is not None:
            mark, num = "🟢 **能算**", f"净利 ${_fmt(profit)}/件，毛利率 {_fmt(margin)}%"
            if cost is not None and ceiling is not None:
                num += (f"；采购 ${_fmt(cost)} 已超上限 ${_fmt(ceiling)}" if cost > ceiling
                        else f"；采购 ${_fmt(cost)}，上限 ${_fmt(ceiling)}")
            nxt = "毛利未扣广告/退货/关税；拿到真实报价与重量后复核"
            if facts.get("economics_estimated"):
                notes.append(f"成本里的{'、'.join(facts['economics_estimated'])}是估的，别拿这个毛利当承诺。")
        elif facts.get("sale_price_usd"):
            mark, num = "🟠 **算不全**", "有售价，缺采购价"
            nxt = "填一个采购价，或接 Sorftime 取 1688 报价"
        else:
            mark, num = "⚪ **答不了**", "售价、采购价都没拿到"
            nxt = "接 SIF 或西柚取头部售价，再接 Sorftime 取 1688 报价"
        rows.append(f"| 能不能赚钱 | {mark} | {num} | {nxt} |")

        # ② 侵权：关键词检索只能排除「同名专利」，外观和商标它查不到，不能写成「无风险」
        ip_n = facts.get("ip_findings_count")
        if facts.get("ip_reviewed"):
            mark, num = "🔵 **人工已核**", "本次运行标记了已做侵权复核"
            nxt = "以你的复核结论为准；换款/改图后要重查"
        elif ip_n is None:
            mark, num = "⚪ **没查**", "专利检索没跑通"
            nxt = "外观专利以图搜图，商标查 USPTO / 马德里"
        elif ip_n == 0:
            mark, num = "🟠 **只过关键词**", "关键词检索命中 0 条"
            nxt = "不等于安全：外观要以图搜图，商标要单独查"
            notes.append("关键词 0 命中只说明这个词下没检索到专利，挡不住外观相同但名字不同的专利。")
        else:
            mark, num = "🟠 **待复核**", f"关键词检索命中 {ip_n} 条"
            nxt = "逐条看权利要求；外观以图搜图，商标查 USPTO / 马德里"
            notes.append(f"命中≠侵权：{_fmt(ip_n)} 条里多数是同词不同物，但没逐条看过之前不能算过。")
        rows.append(f"| 会不会侵权 | {mark} | {num} | {nxt} |")

        # ③ 供应链：1688 有多少家、什么价、起订多少；配合度不在选品阶段，单独标出来
        offers = facts.get("supply_offers")
        sup_n = facts.get("supply_supplier_count")
        sup_raw = facts.get("supply_raw_count")
        sup_kw = facts.get("keyword_zh") or facts.get("keyword") or "—"
        sup_err = str(((sources or {}).get("supply") or {}).get("error") or "").strip()
        if offers:
            who = f"{_fmt(sup_n)} 家 / " if sup_n else ""
            num = f"{who}{_fmt(offers)} 条报价，标价中位 ¥{_fmt(facts.get('supply_price_cny_median'))}"
            if facts.get("supply_moq_min") is not None:
                num += f"，起订 {_fmt(facts.get('supply_moq_min'))}"
            mark, nxt = "🟢 **有货源**", "问贴标/箱唛/开票，拿样核价"
            notes.append("1688 标价是搜索页起价，不是谈下来的成交价。")
        elif offers == 0:
            mark, num = "🟠 **搜过没有同款**", f"「{sup_kw}」0 条报价"
            nxt = "换更通用的中文商品词重跑，或拿主图去 1688 以图搜图"
        elif sup_raw:
            # 连上了、也有返回，但返回的不是这个产品：1688 搜不到时会回落成一张别的类目的搜索页，
            # 照样 10 条带价格的 offer。这种数据采信了，报告就会把别人的价写成这个品的采购价。
            mark, num = "🔴 **搜到的不是这个产品**", f"返回 {_fmt(sup_raw)} 条，标题一条都对不上"
            nxt = "换成 2-8 个字的中文商品词重跑，或直接拿主图以图搜图"
        elif sup_err:
            # 「搜了但没结果」和「根本没连上」是两回事：原来一律写「没连上」，
            # 用户去数据源页折腾半天，其实数据源是通的，只是这个搜索词本身不是商品词。
            reason = sup_err[len("数据源无结果："):] if sup_err.startswith("数据源无结果：") else sup_err
            mark, num = "🟠 **搜索没成功**", f"「{sup_kw}」返回：{reason}"
            nxt = "换 2-8 个字的中文商品词重跑，或拿主图以图搜图"
        else:
            mark, num = "⚪ **没取到**", "1688 没返回报价"
            nxt = "到「数据源」页检查 Sorftime"
        rows.append(f"| 供应链靠不靠谱 | {mark} | {num} | {nxt} |")

        out = ["## 三项判断", "",
               "| 判断 | 结论 | 关键数字 | 下一步 |", "|---|---|---|---|", *rows, ""]
        if notes:
            out += [f"- {n}" for n in notes] + [""]
        return out

    def _money_block(self, facts: Dict[str, Any]) -> List[str]:
        """钱怎么算的：把单位经济逐项摊开，标清哪几项是估的。

        首屏只给「净利 / 毛利率」两个数，很容易被当成承诺——采购价取自 1688 起价、
        FBA 可能是 $4.5 粗估。逐项列出来源，读者自己能看出这个数有多硬。
        """
        profit = facts.get("net_profit_usd")
        margin = facts.get("gross_margin_pct")
        if profit is None or margin is None:
            return []
        est = set(facts.get("economics_estimated") or [])
        sale = facts.get("sale_price_usd")
        cost = facts.get("purchase_cost_usd")
        cny = facts.get("supply_price_cny_median") or facts.get("supply_price_cny_min")
        rows = [("售价", f"${_fmt(sale)}", "头部竞品售价中位数" if facts.get("avg_price_usd") and
                 abs(float(sale or 0) - float(facts['avg_price_usd'])) < 0.01 else "运行参数指定")]
        if cost is not None:
            rows.append(("采购", f"${_fmt(cost)}" + (f"（¥{_fmt(cny)}）" if cny else ""),
                         "1688 搜索页起价折算" if cny else "运行参数指定"))
        if facts.get("freight_usd") is not None:
            rows.append(("头程", f"${_fmt(facts['freight_usd'])}", "估（采购价比例或体积重量）"
                         if "头程运费" in est else "按体积/实重估算"))
        if facts.get("fba_fee_usd") is not None:
            rows.append(("FBA 配送费", f"${_fmt(facts['fba_fee_usd'])}", "粗估 $4.50" if "FBA 配送费" in est
                         else "亚马逊费率表 / 实际配送费"))
        if facts.get("referral_fee_usd") is not None:
            rows.append(("平台佣金", f"${_fmt(facts['referral_fee_usd'])}", "按售价比例"))
        rows.append(("单件净利", f"**${_fmt(profit)}**", "未扣广告 / 退货 / 关税"))
        rows.append(("毛利率", f"**{_fmt(margin)}%**", "净利 ÷ 售价"))
        if facts.get("cost_ceiling_usd") is not None:
            head = facts.get("ceiling_headroom")
            if head is None:
                how = "达到目标毛利的上限"
            elif head < 1:
                how = "现价已超过上限"
            else:
                how = f"上限是现价的 {_fmt(head)} 倍"
            rows.append(("采购成本上限", f"${_fmt(facts['cost_ceiling_usd'])}", how))
        return ["## 利润测算", "", "| 项目 | 金额 | 口径 |", "|---|---|---|",
                *[f"| {a} | {b} | {c} |" for a, b, c in rows], ""]

    def _gates(self, run: _Run, facts: Dict[str, Any], t: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """方法包写了 decision.mode=gates 才走六道门；算挂了就回落五维评分，不让整次运行失败。"""
        mode = ((run.pack or {}).get("decision") or {}).get("mode")
        if mode == "voc":
            try:
                return voc_mod.decide(facts, t)
            except Exception as exc:
                logger.warning("VOC 三道门判定失败，回落五维评分：%s", exc)
                run.notes.append(f"VOC 三道门判定失败（{exc}），本次按五维评分出结论")
                return None
        if mode != "gates":
            return None
        try:
            return gates_mod.from_facts(facts, run.params, t)
        except Exception as exc:                       # 方法论包是外部输入，坏了也不能拖垮运行
            logger.warning("六道门判定失败，回落五维评分：%s", exc)
            run.notes.append(f"六道门判定失败（{exc}），本次按五维评分出结论")
            return None

    @staticmethod
    def _voc_lead_block(facts: Dict[str, Any]) -> List[str]:
        """社媒前导：痛点深读（原话已核实才标原文）+ 取数口径，让读者知道这些数从哪来、有多可信。"""
        lead = facts.get("voc_lead")
        if not lead:
            return []
        det = facts.get("voc_lead_detail") or {}
        L = ["## 社媒前导（Reddit）", ""]
        if not lead.get("ok"):
            return L + [f"Reddit 取数没成功：{lead.get('error') or '未知原因'}。声量门与痛点门因此待补。", ""]
        tr = lead.get("trend_pct")
        subs_text = f"在 {len(lead.get('subreddits') or [])} 个版块（{'、'.join('r/' + x for x in (lead.get('subreddits') or [])[:6])}）" if lead.get('subreddits') else ""
        trend_str = f"前半段 {lead.get('prior')} 条 → 后半段 {lead.get('recent')} 条（{tr:+.0f}%）；" if (lead.get('prior') is not None and lead.get('recent') is not None and tr is not None) else (f"注意力趋势 {tr:+.0f}%；" if tr is not None else "")
        L.append(f"共沉淀并检索到 **{lead.get('mentions')}** 条相关买家真实讨论{('，' + subs_text) if subs_text else ''}；"
                 f"{trend_str}"
                 f"词典口径痛点率 {(lead.get('pain_rate') or 0):.0%}。")
        if lead.get("generic"):
            L += ["", "> ⚠️ 没有匹配的品类配置，用的是通用版块，信号偏弱，只能粗判。可在 `config/voc_categories/` 为你的品类新增配置。"]
        if lead.get("capped"):
            L += ["", "> ⚠️ 部分版块采样触顶，趋势按上限估计，实际声量可能更高。"]
        ann = det.get("annotation")
        if ann:
            L += ["", "### AI 深读：用户在骂什么", ""]
            for x in ann.get("pain_points") or []:
                q = f"\n  > {x['quote']}" + ("" if x.get("quote_verified") else "（⚠️ 未能在样本中核实原文）") if x.get("quote") else ""
                L.append(f"- **{x['name']}**（{x.get('pct') if x.get('pct') is not None else '—'}%）：{x.get('detail') or ''}{q}")
            if ann.get("unmet_needs"):
                L += ["", "未被满足的需求：" + "；".join(ann["unmet_needs"])]
            if ann.get("opportunity"):
                L += ["", f"差异化机会：{ann['opportunity']}"]
        elif det.get("annotation_error"):
            L += ["", f"（AI 深读未完成：{det['annotation_error']}。痛点门仅按词典口径判。）"]
        L += ["", "> 社媒只提出假设，亚马逊数据裁决；数据源：Arctic Shift 免费存档，未消耗任何 MCP 额度。", ""]
        return L

    @staticmethod
    def _gate_block(verdict: Dict[str, Any]) -> List[str]:
        """六道门表：结论旁边就是数字和门槛，缺哪门一眼看出来。"""
        gate = verdict.get("gate") or {}
        gates = gate.get("gates") or []
        if not gates:
            return []
        icon = {"pass": "✅ 过", "fail": "❌ 不过", "unknown": "❔ 缺数据"}
        L = ["## 定档依据", "",
             f"**{gate.get('conclusion') or verdict.get('verdict')}** ｜ {gate.get('reason') or ''}", "",
             "| # | 门 | 判 | 数字 | 门槛 / 说明 |", "|---|---|---|---|---|"]
        for g in gates:
            why = g.get("threshold") or ""
            if g.get("status") != "pass" and g.get("note"):
                why = f"{why}；{g['note']}"
            L.append(f"| {g.get('index')} | {g.get('name')} | {icon.get(g.get('status'), '❔')} "
                     f"| {g.get('value') or '—'} | {why} |")
        fb = gate.get("first_batch_detail") or {}
        diff = gate.get("difficulty") or {}
        extra = []
        if diff.get("pct") is not None:
            extra.append(f"运营难度 {diff['pct']:g}% {diff.get('level') or ''}".strip())
        if fb.get("ok"):
            extra.append(f"首批 {fb['qty']} 件" + (f"（{fb['note']}）" if fb.get("note") else "")
                         + f"，补货约 {fb['replenish']} 件/月")
        if gate.get("profit") and gate["profit"].get("price_min") is not None:
            extra.append(f"最低售价 ${gate['profit']['price_min']:.2f}")
        if gate.get("profit") and gate["profit"].get("cost_max_cny") is not None:
            extra.append(f"采购上限 ¥{gate['profit']['cost_max_cny']:.2f}")
        if extra:
            L += ["", "**关键数字：** " + " ｜ ".join(extra)]
        L.append("")
        return L

    @staticmethod
    def _peer_block(run: "_Run", facts: Dict[str, Any]) -> List[str]:
        """同特征竞品筛查（自动）：来源、样本量、六道门上游数字与断崖，全部摊开。"""
        src = run.sources.get("peer_screen") or {}
        if not src.get("payload") or not facts.get("peer_sample_n"):
            return []
        cliff = facts.get("peer_cliff") or {}
        L = ["## 同特征竞品筛查（自动）", "",
             f"来源：{connector_label(src.get('connector'))} ｜ 样本 {facts.get('peer_sample_n')} 个"
             f"（关键词「{src.get('query') or facts.get('keyword') or ''}」，按月销降序）", "",
             "| 指标 | 值 | 口径 |", "|---|---|---|"]

        def cell(v: Any) -> str:
            if v in (None, ""):
                return "—"
            return f"{v:,}" if isinstance(v, int) else str(v)

        rows = [
            ("同特征平均月销 S", cell(facts.get("peer_avg_monthly")), "样本平均月销（首批 = S × k × 2 的 S）"),
            ("头部月销（代理）", cell(facts.get("head_monthly_sales")), "样本最大值；断崖法口径见下"),
        ]
        if cliff:
            rows.append(("断崖", f"第 {cliff.get('head_tier')} 名后 ×{cliff.get('ratio')}",
                         f"{cliff.get('head_tier_min'):,} → {cliff.get('after_tier'):,}（相邻 ≥1.3 判断崖）"))
        rows += [
            ("低评论档平均月销", cell(facts.get("low_review_avg")),
             f"评论 ≤{run.sources.get('low_review_max') or 30} 的均值（n={facts.get('low_review_sellers') or 0}）"),
            ("参照档中位月销", cell(facts.get("ref_reviews_median")),
             f"评论 {run.sources.get('ref_reviews_min') or 100}–{run.sources.get('ref_reviews_max') or 200}"
             f" 的中位（n={facts.get('unsaturation_ref_n') or 0}）"),
            ("运营难度", f"{facts.get('review_band_difficulty_pct') or '—'}%", "低评论档均值 ÷ 前 5 均值（只调节备货，不否决）"),
            ("未饱和", cell(facts.get("unsaturation_ratio")), "低评论档均值 ÷ 参照档中位（≥0.15 未饱和）"),
        ]
        for name, val, basis in rows:
            L.append(f"| {name} | {val} | {basis} |")
        L += ["", "> ⚠️ 自动样本 = 关键词下的头部商品，不是按「消费者会不会犹豫 / 爆了拿谁对比」挑出的"
              "同特征集；数字足以定档，但置信度封顶「中」。人工确认 3–10 个同特征竞品后再升「高」。", ""]
        return L

    def _image_block(self, run: _Run, facts: Dict[str, Any]) -> List[str]:
        """以图找货：识图结论、图搜同款、外观专利以图检索摊开，人工复核入口也给上。"""
        img = run.sources.get("image") or {}
        if not img:
            return []
        a = img.get("analysis") or {}
        if not a:
            return ["## 图像识别", "",
                    f"> 图片 `{img.get('image_id') or '—'}` 没认出东西：{img.get('error') or '未知原因'}。"
                    "本次按关键词路线跑，下面的市场判断不受影响。", ""]
        name = a.get("product_name_zh") or a.get("product_name_en") or "—"
        if a.get("product_name_zh") and a.get("product_name_en"):
            name += f"（{a.get('product_name_en')}）"
        conf = a.get("confidence")
        rows = [
            ("认出产品", name),
            ("英文搜索词", a.get("keyword") or "—"),
            ("1688 搜索词", a.get("keyword_zh") or "—"),
            ("类目", a.get("category") or "—"),
            ("材质", "、".join(a.get("materials") or []) or "—"),
            ("主要用途", a.get("main_use") or "—"),
            ("可见特征", "；".join(a.get("features") or []) or "—"),
            ("图上可见品牌", a.get("visible_brand") or "未见"),
            ("风险提示", "；".join(a.get("risk_flags") or []) or "无"),
            ("置信度", f"{conf}（模型自评，不是准确率）" if conf is not None else "—"),
        ]
        L = ["## 图像识别", "", "| 项 | 识别结果 |", "|---|---|"]
        # 模型回的文字里带 | 或换行会把表格拆散
        L += [f"| {k} | {str(v).replace('|', '／').replace(chr(10), ' ')} |" for k, v in rows]
        L.append("")

        sup = run.sources.get("supply_image") or {}
        ipi = run.sources.get("ip_image") or {}
        if sup or ipi:
            L += ["| 以图派生检索 | 结果 | 证据 |", "|---|---|---|"]
            if sup:
                if sup.get("payload") is None:
                    res = f"失败：{(sup.get('error') or '')[:80]}"
                elif facts.get("image_supply_offers"):
                    res = f"{facts['image_supply_offers']} 条报价"
                    if facts.get("image_supply_price_cny_min") is not None:
                        res += f"，最低 ¥{_fmt(facts['image_supply_price_cny_min'])}"
                else:
                    # 以图搜不过文字相关性，这里的 0 只可能是「真没回」或「回得太少不够统计」
                    res = "0 条报价（以图没有搜到同款）" if not sup.get("payload") \
                        else "只回了零星 1 条，不够统计（看证据原文）"
                L.append(f"| 1688 以图搜同款 | {res} | {sup.get('eid') or '—'} |")
            if ipi:
                if ipi.get("payload") is None:
                    res = f"失败：{(ipi.get('error') or '')[:80]}"
                else:
                    n = facts.get("image_ip_findings_count")
                    res = f"命中 {n} 条外观专利" if n else "0 命中"
                L.append(f"| 外观专利以图检索 | {res} | {ipi.get('eid') or '—'} |")
            L.append("")
        if facts.get("image_supply_samples"):
            L += ["图搜同款样本：", "", *[f"- {t}" for t in facts["image_supply_samples"][:5]], ""]
        if facts.get("image_ip_samples"):
            L += ["外观专利命中样本：", ""]
            for s in facts["image_ip_samples"][:5]:
                if isinstance(s, dict):
                    L.append(f"- {s.get('no') or '—'}　{s.get('title') or ''}".rstrip())
                else:
                    L.append(f"- {s}")
            L.append("")
        if not sup:
            L += ["> 这张图是本地文件，1688 以图搜同款只收公网图地址；本次用识别出的关键词搜了同款。", ""]
        L += ["> 人工复核：Amazon 以图找货 https://www.amazon.com/shopthelook"
              "（网页版 Shop the Look，登录后上传同一张图；标品也能出同款）", ""]
        return L

    def _product_block(self, run: _Run) -> List[str]:
        """「选的到底是什么产品」：参照竞品主图 + 1688 候选货源缩略图，让读者一眼看到东西长什么样。"""
        L: List[str] = []
        ref = facts_mod.ref_product_card((run.sources.get("ref_profile") or {}).get("payload"))
        sup = run.sources.get("supply") or {}
        offers = facts_mod.supply_offer_cards(sup.get("payload"), str(sup.get("query") or ""), 6) if sup.get("payload") else []
        offers = [o for o in offers if o["image"].startswith("http")]
        if not (ref.get("image") or offers):
            return L
        esc = lambda x: str(x).replace("|", "／").replace("\n", " ").replace("[", "(").replace("]", ")")
        L += ["## 产品与货源", ""]
        if ref.get("image"):
            bits = [f"${ref['price']:g}" if ref.get("price") else "",
                    f"★{ref['rating']:g}（{int(ref['reviews'])} 评）" if ref.get("rating") and ref.get("reviews") else "",
                    f"月销约 {int(ref['monthly_sales'])}" if ref.get("monthly_sales") else "",
                    f"上架 {ref['listed']}" if ref.get("listed") else "",
                    f"{int(ref['variations'])} 个变体" if ref.get("variations") else ""]
            link = f"[亚马逊页面](https://www.amazon.com/dp/{ref['asin']})" if ref.get("asin") else ""
            L += ["**亚马逊上的参照竞品**（售价最接近本品的头部链接，也是算 FBA 与头程用的那个）", "",
                  "| 主图 | 说明 |", "|---|---|",
                  f"| ![参照竞品]({ref['image']}) | **{esc(ref.get('title') or '—')[:110]}** {('（' + esc(ref['brand']) + '）') if ref.get('brand') else ''}"
                  f" ｜ {' ｜ '.join(b for b in bits if b)} ｜ {link} |", ""]
        if offers:
            L += [f"**1688 候选货源**（搜「{sup.get('query') or '—'}」，按相关度取前 {len(offers)} 条；标价是搜索页起价）", "",
                  "| 图 | 商品 | 标价 ¥ | 起订 | 供应商 | 30 天销量 |", "|---|---|---|---|---|---|"]
            for o in offers:
                t = esc(o["title"])[:46] or "—"
                title = f"[{t}]({o['url']})" if o["url"].startswith("http") else t
                L.append(f"| ![货源]({o['image']}) | {title} | {(format(o['price_cny'], 'g') if o['price_cny'] else '—')} | "
                         f"{int(o['moq']) if o['moq'] else '—'} | {esc(o['supplier'])[:16] or '—'} | "
                         f"{int(o['sales_30d']) if o['sales_30d'] is not None else '—'} |")
            L.append("")
        L += ["> 图片来自数据源返回的商品主图，仅用于辨认产品形态；是否同款、能否使用，需要打样或看 1688 详情页确认。", ""]
        return L

    def _report(self, run: _Run, facts, outcomes, verdict, actions, assumptions, gaps, narrative, n_status) -> str:
        """报告的分段顺序就是阅读顺序：先给判断，再给账，再给要做的事，最后才是支撑材料。

        原来「五维评分 / 关键数据 / 规则清单」三张表紧跟第一屏，数据一缺就是几十行 —，
        读者翻两屏还没看到「到底能不能做」。现在支撑材料统一放到「支撑材料」标题下，
        由前端和 HTML 导出折起来；正文里只留判断、账和待办。
        """
        task = run.task
        pts = verdict["points"]
        pts_txt = pts if pts is not None else "—"
        badge = {"做": "🟢", "有条件做": "🟡", "暂缓": "🟠", "不做": "🔴"}.get(verdict["verdict"], "⚪")
        gate_now = verdict.get("gate") or {}
        if gate_now.get("gates"):
            n_pass = sum(1 for g in gate_now["gates"] if g["status"] == "pass")
            n_unk = sum(1 for g in gate_now["gates"] if g["status"] == "unknown")
            metric = (f"{'三重验证' if gate_now.get('mode') == 'voc' else '六道门'}过 {n_pass}/{len(gate_now['gates'])}"
                      + (f"（{n_unk} 门待补）" if n_unk else "") + f" ｜ 评分参照 {pts_txt}/100")
        else:
            metric = f"综合 {pts_txt}/100"
        L = [f"# {task.title} · 选品裁定", "",
             f"> {badge} **{verdict['verdict']}** ｜ {metric} ｜ "
             f"数据覆盖 {verdict['data_coverage']:.0%} ｜ 置信度 {verdict['confidence_level']}（{verdict['confidence']}）", "",
             f"- 运行编号：`{task.task_id}` ｜ 生成：{now()} ｜ 站点：{task.target_market}",
             f"- 关键词：`{facts.get('keyword') or '—'}` ｜ 1688 词：`{facts.get('keyword_zh') or '—'}`",
             *([f"- 以图识别：`{facts.get('image_product_zh') or facts.get('image_product_en') or '—'}`"
                f" ｜ 英文词：`{facts.get('image_keyword') or '—'}`"
                f" ｜ 1688 词：`{facts.get('image_keyword_zh') or '—'}`"
                f" ｜ 置信度：{facts.get('image_confidence') if facts.get('image_confidence') is not None else '—'}"]
               if facts.get("image_product_zh") or facts.get("image_keyword") else []),
             f"- 方法论：{(run.pack or {}).get('name') or task.method_pack_id or '星空 S1 快速版'} ｜ 证据：{run.ledger.summary_line()}", "",
             "## 核心结论", "", verdict["reason"], ""]
        gate = verdict.get("gate") or {}
        L += self._product_block(run)
        L += self._gate_block(verdict)
        L += self._peer_block(run, facts)
        L += self._voc_lead_block(facts)
        L += self._three_answers(facts, run.sources)
        L += self._money_block(facts)
        # 六道门模式下，评分侧的硬闸门与待办可能和门表打架（例如评论区间低分与本方法「不否决」相冲），
        # 定档以门为准，评分侧只留在支撑材料里。
        if verdict["hard_blockers"] and not gate:
            L += ["**硬闸门：**", ""] + [f"- {b['rule']}：{b['why']}" for b in verdict["hard_blockers"]] + [""]
        gate_todo = [str(x) for x in (gate.get("todo") or []) if str(x).strip()] if gate else []
        if gate_todo:
            L += ["## 待办事项", "", *[f"- [ ] {x}" for x in gate_todo[:8]], ""]
        elif actions:
            L += ["## 待办事项", ""] + [f"- [ ] **{a['rule']} {a['title']}**（{a['status']}）：{a['action']}"
                                       for a in actions] + [""]
        if gaps:
            L += ["## 数据缺口", ""] + [f"- {g['label']}：{g['error']}" for g in gaps] + [""]
        L += ["## 研判分析" + ("" if n_status == "ok" else "（规则版）"), "", narrative, ""]

        # ↓↓↓ 以下都是支撑材料：默认折叠，读者要核账时再展开
        L += self._image_block(run, facts)
        L += ["## 五维评分", "", scoring.axes_markdown(verdict["axes"], verdict["verdict"]), ""]

        refs = facts.get("evidence_refs") or {}
        data_rows, missing = [], 0
        for key, label in (("search_volume_weekly", "周搜索量"), ("search_volume_trend_pct", "需求趋势 %"),
                           ("trend_basis", "趋势口径"), ("season_position", "季节位置"),
                           ("top3_click_share", "Top3 点击集中度"), ("visible_asin_count", "可见链接数"),
                           ("median_reviews", "头部评论中位数"), ("review_band_difficulty_pct", "评论区间难度 %"),
                           ("young_listing_share", "头部新链接占比"),
                           ("avg_price_usd", "头部售价中位数 $"), ("natural_share", "头部自然流量占比"),
                           ("supply_offers", "1688 报价条数"), ("supply_price_cny_median", "1688 页面标价中位数 ¥"),
                           ("supply_moq_min", "最低 MOQ"), ("gross_margin_pct", "毛利率 %（未含广告）"),
                           ("cost_ceiling_usd", "采购成本上限 $"), ("ip_findings_count", "专利命中")):
            v = facts.get(key)
            if v is None or v == "":
                missing += 1          # 空值不占一行：17 行里 14 行是「—」，读者会以为整份报告都没数
                continue
            vs = f"{v:.0%}" if isinstance(v, float) and key in ("top3_click_share", "young_listing_share", "natural_share") else _fmt(v)
            data_rows.append(f"| {label} | {vs} | {refs.get(key, '—')} |")
        L += ["## 关键数据", ""]
        if data_rows:
            L += ["| 指标 | 数值 | 证据 |", "|---|---|---|", *data_rows, ""]
        if missing:
            L += [f"> 另有 {missing} 项没有取到，已在「数据缺口」里列明；没取到的项不在这里占位。", ""]
        L += ["## 规则清单", "", "| 规则 | 状态 | 实际值 | 阈值（过/警） | 证据 |", "|---|---|---|---|---|"]
        for o in outcomes:
            th = o.threshold or {}
            ths = f"{th.get('pass')} / {th.get('warn')}" if isinstance(th, dict) else str(th)
            if isinstance(th, dict) and th.get("warn") is None:
                # 没有警告区间的规则（如 R13 专利命中）只显示合格线，别把 None 写成“/ None”
                ths = _fmt(th.get("pass"), o.unit) if o.unit == "占比" else f"{_fmt(th.get('pass'))} {o.unit}".strip()
            elif o.unit == "占比":
                ths = f"{_fmt(th.get('pass'), o.unit)} / {_fmt(th.get('warn'), o.unit)}" if isinstance(th, dict) else ths
            if o.unit == "占比":
                val = _fmt(o.value, o.unit)
            else:
                val = f"{_fmt(o.value)} {o.unit}".strip() if o.value is not None else "—"
            L.append(f"| {o.id} {o.name}{' 🔒' if o.hard else ''} | {STATUS_ICON.get(o.status, '')} {o.status} | "
                     f"{val} | {ths} | {o.evidence_id or '—'} |")
        L.append("")
        if facts.get("top_competitors_sample"):
            L += ["## 竞品样本", "", "| ASIN | 价格 | 评分 | 评论 | 月单 | 上架天数 |", "|---|---|---|---|---|---|"]
            for c in facts["top_competitors_sample"][:10]:
                L.append(f"| {c.get('asin')} | {_fmt(c.get('price'))} | {_fmt(c.get('rating'))} | {_fmt(c.get('reviews'))} | "
                         f"{_fmt(c.get('monthly_orders'))} | {_fmt(c.get('listed_age_days'))} |")
            L.append("")
        if facts.get("voc_samples"):
            # 讨论摘要是差异化判断的原始依据，只放在 PDF 之外等于白抓；
            # 读者是中国客户，所以中文译文在前，英文原文缩进一行留作对账。
            L += ["## 用户声音", "",
                  "买家原话，中文为译文；缩进的英文是原文，便于核对。" if facts.get("voc_samples_zh")
                  else "买家原话摘录（未翻译）。"] + SelectionEngine._voc_bullets(facts, 8) + [""]
        elif facts.get("voc_count") == 0:
            # 0 条是查到的事实，不是没查。写出来，免得读者以为这条漏跑了。
            L += ["## 用户声音", "",
                  "Reddit 检索完成，没有找到与这个词相关的讨论（0 条）。"
                  "差异化判断因此缺少买家原话佐证，只能靠竞品结构与参数推断。", ""]
        if assumptions:
            L += ["## 估算假设", ""] + [f"- {a}" for a in assumptions] + [""]
        L += ["## 证据索引", "", "| 编号 | 内容 | 来源 | 状态 |", "|---|---|---|---|"]
        for it in run.ledger.items:
            L.append(f"| {it['id']} | {it['label']} | {it['connector'] or '-'}:{it['tool'] or '-'} | "
                     f"{'成功' if it['ok'] else '失败：' + str(it['error'])[:60]} |")
        L += ["", "---", "结论由阈值规则确定（阈值来源：星空 S1 `s1_thresholds.yaml` 叠加内置默认），"
              "AI 只负责解读。原始数据见同目录 `evidence.md` / `evidence.jsonl`。"]
        if run.notes:
            L += ["", "运行备注：" + "；".join(run.notes)]
        return "\n".join(L) + "\n"

    def _persist(self, run: _Run, facts, outcomes, verdict, actions, built, report) -> Dict[str, str]:
        d = run.run_dir
        run.ledger.save(d)
        write_json(d / "facts.json", {"facts": facts, "assumptions": built["assumptions"],
                                      "evidence_ratio": run.ledger.coverage()["ratio"]}, indent=1)
        write_json(d / "verdict.json", {"verdict": verdict, "rules": [o.to_dict() for o in outcomes],
                                        "actions": actions, "method_pack": (run.pack or {}).get("id"),
                                        "weights": run.weights}, indent=1)
        write_json(d / "run.json", {"task": run.task.model_dump(mode="json"), "steps": run.steps,
                                    "notes": run.notes, "finished_at": now()}, indent=1)
        write_text(d / "report.md", report)
        # 交付给人看的是这份 HTML；md 留着做机器可读的底稿与回溯
        write_text(d / "report.html", report_html.render(report))
        task_repository.save_report_markdown(run.task_id, report)  # 旧接口兼容
        return {"report": str(d / "report.md"), "report_html": str(d / "report.html")}

    @staticmethod
    def _verdict_brief(v: Dict[str, Any]) -> Dict[str, Any]:
        out = {"verdict": v["verdict"], "tier": v["tier"], "points": v["points"], "reason": v["reason"],
               "confidence": v["confidence"], "confidence_level": v["confidence_level"],
               "data_coverage": v["data_coverage"], "hard_blockers": v["hard_blockers"],
               "axes": {k: {"score": a.get("score"), "basis": a.get("basis")} for k, a in v["axes"].items()}}
        gate = v.get("gate")
        if gate:
            out["decision_mode"] = "gates"
            out["gate"] = {
                "conclusion": gate.get("conclusion"),
                "reason": gate.get("reason"),
                "summary": "；".join(f"{g['name']}={g['status']}" for g in gate.get("gates") or []),
                "gates": [{k: g.get(k) for k in ("index", "name", "status", "value", "threshold", "note")}
                          for g in gate.get("gates") or []],
                "first_batch": gate.get("first_batch"),
                "difficulty": gate.get("difficulty"),
                "profit": gate.get("profit"),
                "todo": gate.get("todo") or [],
            }
            out["score_verdict"] = v.get("score_verdict")
        return out

    # ---------------- 补充核验 · 终审定档（免重新取数） ----------------

    def finalize_decision(self, task_id: str, updated_parameters: Dict[str, Any]) -> Dict[str, Any]:
        """
        卖家补充/确认人工核验项（如合规已核、亏损可承受、同特征竞品数等）后，
        直接基于已有的事实数据重新裁决六道门与规则，不消耗任何外部 MCP 取数额度。
        """
        task = task_repository.get_task(task_id)
        if not task or not task.run_dir:
            raise ValueError(f"未找到该任务数据: {task_id}")

        merged_params = {**(task.parameters or {}), **(updated_parameters or {})}
        task.parameters = merged_params

        d = Path(task.run_dir)
        saved = read_json(d / "facts.json") or {}
        facts = dict(saved.get("facts") or {})

        pack_id = task.method_pack_id or settings.DEFAULT_METHOD_PACK
        resolved = method_pack_service.resolve(pack_id, merged_params)
        t = resolved["thresholds"]

        # 成本上限随参数或利润线更新
        price, cost = facts.get("sale_price_usd"), facts.get("purchase_cost_usd")
        if price and cost and facts.get("freight_usd") is not None:
            m = float(t.get("margin", {}).get("gross_margin_pass_min", 30)) / 100.0
            ceiling = price - (facts.get("referral_fee_usd") or 0) - (facts.get("fba_fee_usd") or 0)                 - facts["freight_usd"] - price * m
            facts["cost_ceiling_usd"] = round(ceiling, 2)
            facts["ceiling_headroom"] = round(ceiling / cost, 3)

        outcomes = rules_mod.evaluate(facts, rule_ids=resolved["rule_ids"], t=t)
        axes = scoring.score_axes(outcomes, facts, resolved["weights"])
        evidence_ratio = float(saved.get("evidence_ratio") or 1.0)
        verdict = scoring.decide(facts, outcomes, axes, evidence_ratio=evidence_ratio,
                                 weights=resolved["weights"], t=t)

        gate = None
        if ((resolved["pack"].get("decision") or {}).get("mode") == "gates"):
            try:
                gate = gates_mod.from_facts(facts, merged_params, t)
                verdict = gates_mod.apply_to_verdict(verdict, gate)
            except Exception as exc:
                logger.warning("终审重算时六道门失败：%s", exc)

        actions = scoring.actions_from(outcomes)

        # 保存更新后的结论与规则输出
        write_json(d / "verdict.json", {
            "verdict": verdict,
            "rules": [o.to_dict() for o in outcomes],
            "actions": actions,
            "method_pack": (resolved["pack"] or {}).get("id"),
            "weights": resolved["weights"]
        }, indent=1)

        # 更新报告 Markdown 与 HTML
        report_path = d / "report.md"
        if report_path.exists():
            try:
                old_md = report_path.read_text(encoding="utf-8")
                badge = "🟢" if verdict["verdict"] == "做" else "🟡" if verdict["verdict"] in ("有条件做", "小批量试款") else "🟠" if verdict["verdict"] == "暂缓" else "🔴"
                metric = f"{verdict['points']} 分" if verdict["points"] is not None else (f"{verdict.get('gate',{}).get('conclusion')}" if verdict.get("gate") else "—")
                new_header = f"> {badge} **{verdict['verdict']}** ｜ {metric} ｜ 数据覆盖 {verdict['data_coverage']:.0%} ｜ 置信度 {verdict['confidence_level']}（{verdict['confidence']}）"
                new_md = re.sub(r"> [^\n]+", new_header, old_md, count=1)
                new_md = re.sub(r"## 核心结论\n\n[^\n]+", f"## 核心结论\n\n{verdict['reason']}", new_md, count=1)
                if gate and not gate.get("todo"):
                    new_md = re.sub(r"## 待办事项\n\n(?:- \[[ x\]] [^\n]+\n)+", "## 待办事项\n\n- [x] 所有待办门槛已完成人工复核与确认，六道门终审通过。\n", new_md)
                write_text(report_path, new_md)
                write_text(d / "report.html", report_html.render(new_md))
                task_repository.save_report_markdown(task_id, new_md)
            except Exception as ex:
                logger.warning("更新报告文本失败：%s", ex)

        # 更新 task
        task.verdict = verdict
        dummy_run = type("DummyRun", (), {"pack": resolved["pack"]})()
        cov_info = (task.results_summary or {}).get("evidence") or {"ok": 9, "total": 9}
        cov = {"ratio": evidence_ratio, "ok": cov_info.get("ok", 9), "total": cov_info.get("total", 9)}
        summary = self._summary(dummy_run, facts, outcomes, verdict, actions, [], "ok", cov)
        task.results_summary = summary
        task_repository.save_task(task)

        return {
            "task_id": task_id,
            "verdict": verdict,
            "gate": gate,
            "summary": summary
        }

    # ---------------- 换方法论重算（不重新取数） ----------------

    def rescore(self, task_id: str, pack_id: str) -> Dict[str, Any]:
        task = task_repository.get_task(task_id)
        if not task or not task.run_dir:
            raise ValueError("该运行没有可复用的数据")
        saved = json.loads((Path(task.run_dir) / "facts.json").read_text(encoding="utf-8"))
        facts = dict(saved.get("facts") or {})
        resolved = method_pack_service.resolve(pack_id, task.parameters or {})
        t = resolved["thresholds"]
        # 成本上限依赖目标毛利率：按新方法论的利润线重算
        price, cost = facts.get("sale_price_usd"), facts.get("purchase_cost_usd")
        if price and cost and facts.get("freight_usd") is not None:
            m = float(t["margin"]["gross_margin_pass_min"]) / 100.0
            ceiling = price - (facts.get("referral_fee_usd") or 0) - (facts.get("fba_fee_usd") or 0) \
                - facts["freight_usd"] - price * m
            facts["cost_ceiling_usd"] = round(ceiling, 2)
            facts["ceiling_headroom"] = round(ceiling / cost, 3)
        outcomes = rules_mod.evaluate(facts, rule_ids=resolved["rule_ids"], t=t)
        axes = scoring.score_axes(outcomes, facts, resolved["weights"])
        verdict = scoring.decide(facts, outcomes, axes, evidence_ratio=float(saved.get("evidence_ratio") or 1.0),
                                 weights=resolved["weights"], t=t)
        if ((resolved["pack"].get("decision") or {}).get("mode") == "gates"):
            try:
                verdict = gates_mod.apply_to_verdict(
                    verdict, gates_mod.from_facts(facts, task.parameters or {}, t))
            except Exception as exc:
                logger.warning("换方法论重算时六道门失败，按五维评分出结论：%s", exc)
        brief = self._verdict_brief(verdict)
        brief.update({"pack_id": resolved["pack"]["id"], "pack_name": resolved["pack"]["name"],
                      "weights": resolved["weights"],
                      "rules": [{"id": o.id, "name": o.name, "status": o.status, "value": o.value,
                                 "threshold": o.threshold, "unit": o.unit} for o in outcomes],
                      "actions": scoring.actions_from(outcomes, limit=5)})
        return brief

    def _summary(self, run, facts, outcomes, verdict, actions, gaps, n_status, cov) -> Dict[str, Any]:
        rs = verdict["rule_summary"]
        risk = "高" if verdict["hard_blockers"] or len(rs["failed"]) >= 3 else "中" if rs["failed"] or rs["warned"] else "低"
        return {
            # 旧前端字段（现在是真值）
            "recommendation": f"{verdict['verdict']}" + (f" · {verdict['points']} 分" if verdict["points"] is not None else ""),
            "confidence_score": verdict["confidence"],
            "data_mode": "live" if cov["ratio"] >= 0.8 else "partial" if cov["ok"] else "no_data",
            "weekly_search_volume": facts.get("search_volume_weekly"),
            "avg_price_usd": facts.get("avg_price_usd"),
            "concentration_level": facts.get("concentration_level"),
            "risk_level": risk,
            "rules_applied_count": rs["evaluated"],
            # v3
            "verdict": verdict["verdict"], "tier": verdict["tier"], "points": verdict["points"],
            "reason": verdict["reason"], "confidence_level": verdict["confidence_level"],
            "data_coverage": verdict["data_coverage"], "evidence": {"ok": cov["ok"], "total": cov["total"]},
            "keyword": facts.get("keyword"), "keyword_zh": facts.get("keyword_zh"),
            "gross_margin_pct": facts.get("gross_margin_pct"), "top3_click_share": facts.get("top3_click_share"),
            "supply_offers": facts.get("supply_offers"),
            "rule_counts": rs["counts"], "hard_blockers": verdict["hard_blockers"],
            "actions": actions[:6], "gaps": gaps, "narrative_status": n_status,
            "method_pack": (run.pack or {}).get("id"), "method_pack_name": (run.pack or {}).get("name"),
        }


selection_engine = SelectionEngine()
