"""
选品应用服务层：对外接口（表单发起运行 / AI 助手对话）与运行引擎之间的薄层。

- execute_task：创建任务并交给 selection_engine 后台运行，立即返回（前端轮询进度）。
- chat_selection：知识卡片 + 赛道包 + 实时数据（如有）注入后由模型作答；
  模型不可用时如实告知，并附上已拿到的数据，绝不再回落成固定模板文案。
"""
import datetime
import json
import re
import uuid
from typing import Any, Dict, List, Optional

from app.core import facts as facts_mod
from app.core.connectors import connector_registry
from app.core.llm_gateway import LLMError, llm_gateway
from app.core.logger import logger
from app.model.domain import SelectionTask
from app.repository.chat_store import chat_session_store
from app.schema.selection import CreateSelectionTaskRequest
from app.service.category_pack_service import category_pack_service
from app.service.knowledge_service import knowledge_service
from app.service.method_pack_service import method_pack_service
from app.service.selection_engine import selection_engine

_PACK_HINTS = (
    ("auto_parts", ("刹车", "汽配", "f-150", "f150", "brake", "car ", "汽车")),
    ("curtains_home", ("窗帘", "家纺", "遮光", "curtain", "drape")),
    ("apparel", ("衣服", "服装", "裙", "裤", "shirt", "dress")),
    ("pet_supplies", ("宠物", "狗", "猫", "pet", "dog", "cat")),
)


class SelectionWorkflowService:

    # ---------------- 表单运行 ----------------

    async def execute_task(self, req: CreateSelectionTaskRequest, wait: bool = False) -> SelectionTask:
        params = dict(req.parameters or {})
        task = selection_engine.create_task(
            workflow_type=req.workflow_type, title=req.title, category_pack_id=req.category_pack_id,
            target_market=req.target_market, parameters=params,
            llm_provider=req.llm_provider or None, llm_model=req.llm_model or None,
            method_pack_id=getattr(req, "method_pack_id", None) or params.get("method_pack_id"),
        )
        if wait:
            return await selection_engine.run_blocking(task)
        return selection_engine.start(task)

    # ---------------- AI 助手 ----------------

    _EN_STOPWORDS = {
        "FBA", "CPC", "ABA", "ACOS", "MOQ", "SKU", "ASIN", "BSR", "FBM", "TACOS", "ROI", "IP", "APP",
        "API", "MCP", "AI", "LLM", "GPT", "GLM", "TikTok", "Reddit", "Temu", "Amazon", "Sorftime", "SIF",
        "PPC", "CVR", "CTR", "SP", "SB", "SD", "VOC", "OK",
    }
    _ASIN_RE = re.compile(r"\bB0[A-Z0-9]{8}\b", re.IGNORECASE)

    # 打招呼 / 客套 / 对助手本身的指令：都不是选品对象。
    _CHITCHAT_RE = re.compile(
        r"^\s*(hi|hello|hey|yo|你好|您好|在吗|在不在|谢谢|多谢|感谢|thanks|thank\s+you|ok|okay|收到|好的|"
        r"继续|测试|test|早上好|下午好|晚上好|哈喽|嗨)[\s!！。.,，?？~～、]*$",
        re.IGNORECASE,
    )
    # 单个英文词若不是产品，就不该拿去查数据源——一次竞争格局查询就是 2 点额度。
    _NON_PRODUCT_WORDS = {
        "hi", "hello", "hey", "yo", "thanks", "thank", "please", "yes", "no", "ok", "okay", "test", "testing",
        "help", "you", "your", "me", "my", "the", "and", "is", "are", "was", "were", "what", "how", "why",
        "which", "when", "who", "can", "could", "would", "should", "do", "does", "did", "this", "that", "it",
        "product", "products", "item", "items", "thing", "things", "goods", "price", "prices", "review",
        "reviews", "seller", "sellers", "listing", "listings", "keyword", "keywords", "model", "models",
        "image", "images", "picture", "pictures", "prompt", "prompts", "and", "or", "but", "not",
    }

    @classmethod
    def _detect_market_query(cls, message: str) -> Optional[str]:
        """ASIN → 原样返回；含英文产品词 → 返回最长英文短语（≤4 词）；闲聊 → None。"""
        if not message or cls._CHITCHAT_RE.match(message):
            return None
        m = cls._ASIN_RE.search(message)
        if m:
            return m.group(0).upper()
        best: List[str] = []
        for phrase in re.findall(r"[A-Za-z][A-Za-z\-]*(?:\s+[A-Za-z][A-Za-z\-]*)*", message):
            words = [w for w in phrase.split() if w not in cls._EN_STOPWORDS and w.upper() not in cls._EN_STOPWORDS]
            if len(words) > len(best):
                best = words
        if not best:
            return None
        query = " ".join(best[:4]).lower()
        # 单词查询要像产品：太短、或是客套/通用词，一律不当成关键词。
        if len(best) == 1 and (len(query) < 3 or query in cls._NON_PRODUCT_WORDS):
            return None
        return query

    @staticmethod
    def _match_pack(message: str, explicit: Optional[str]) -> Optional[Dict[str, Any]]:
        pack_id = explicit
        if not pack_id:
            low = message.lower()
            pack_id = next((pid for pid, keys in _PACK_HINTS if any(k in low for k in keys)), None)
        if not pack_id:
            return None
        detail = category_pack_service.get_pack_detail(pack_id)
        if not detail:
            return None
        items: List[str] = []
        if isinstance(detail.rules, dict):
            for k, v in list(detail.rules.items())[:6]:
                items.append(f"{k}: {v.get('description', v) if isinstance(v, dict) else v}")
        elif isinstance(detail.rules, list):
            items = [str(r) for r in detail.rules[:6]]
        return {"id": detail.pack_id, "name": detail.name, "rules": items}

    # 这些标签太泛，单独命中不代表用户在问这套方法论
    _GENERIC_TAGS = {"通用", "默认", "蓝海", "精铺", "硬门槛", "初筛", "批量", "低客单", "中高客单", "高客单"}

    @classmethod
    def _match_method_packs(cls, message: str) -> List[Dict[str, Any]]:
        """用户点名某套方法论（名字 / 编号 / 专有标签）时，把这套方法论的口径带进上下文。
        不带的话，助手只认识知识卡片，会对着已经装好的方法论说「没获取到规则」。"""
        low = (message or "").lower()
        hits: List[Dict[str, Any]] = []
        for p in method_pack_service.list():
            name = str(p.get("name") or "")
            keys = [name, str(p.get("id") or "")] + [str(x) for x in (p.get("tags") or [])
                                                     if str(x) not in cls._GENERIC_TAGS]
            keys += [m for m in re.findall(r"[A-Z]{2,}-?\d+[A-Z\-0-9]*", name)]  # PB-02、KC-QUANT-02
            if any(k and len(k) >= 2 and k.lower() in low for k in keys):
                hits.append(p)
        return hits[:2]

    async def _quick_evidence(self, query: str) -> Dict[str, Any]:
        """对话里的轻量取数：关键词只查竞争格局一项，ASIN 只查基础档案，控制额度与耗时。"""
        if query.upper().startswith("B0") and len(query) == 10:
            res = await connector_registry.call("market.asin_profile", {"asins": [query]}, timeout=60)
            if not res.ok:
                return {"ok": False, "error": res.error, "query": query}
            return {"ok": True, "query": query, "source": f"{res.connector}:{res.tool}",
                    "digest": facts_mod.deep_get(res.data, "list.0", default=res.data)}
        res = await connector_registry.call("market.keyword_competition", {"keyword": query, "country": "US"}, timeout=90)
        if not res.ok:
            return {"ok": False, "error": res.error, "query": query}
        parsed = facts_mod.parse_sif_competition(res.data) if res.connector == "sif" else \
            facts_mod.parse_xydc_competition(res.data)
        parsed.pop("top_competitors_sample", None)
        return {"ok": True, "query": query, "source": f"{res.connector}:{res.tool}", "digest": parsed}

    async def chat_selection(self, message: str, history: Optional[List[Dict[str, str]]] = None,
                             session_id: Optional[str] = None, category_pack_id: Optional[str] = None,
                             llm_provider: Optional[str] = None, llm_model: Optional[str] = None) -> Dict[str, Any]:
        pack = self._match_pack(message, category_pack_id)
        method_packs = self._match_method_packs(message)

        matched_cards: List[Dict[str, Any]] = []
        card_blocks: List[str] = []
        for c in knowledge_service.search_cards_by_text(message, limit=6):
            matched_cards.append({"card_id": c.card_id, "title": c.title, "phase": c.phase, "authority": c.authority})
            detail = knowledge_service.get_card_detail(c.card_id)
            if detail:
                block = f"- [{detail.card_id}] {detail.title}：{(detail.conclusion or '')[:220]}"
                if detail.parameters:
                    block += f"\n  参数：{json.dumps(detail.parameters, ensure_ascii=False)[:300]}"
                card_blocks.append(block)

        evidence: Optional[Dict[str, Any]] = None
        query = self._detect_market_query(message)
        if query:
            try:
                evidence = await self._quick_evidence(query)
            except Exception as exc:
                evidence = {"ok": False, "error": str(exc)[:200], "query": query}
                logger.warning("对话取数失败: %s", exc)

        ctx: List[str] = []
        for mp in method_packs:
            mode = "六道门定档（分数只作参照）" if ((mp.get("decision") or {}).get("mode") == "gates") else "五维加权评分定档"
            ctx.append(f"【已安装的方法论：{mp.get('name')}（{mp.get('id')}）· 定档方式：{mode}】\n"
                       f"{(mp.get('description') or '').strip()}\n解读口径：\n{(mp.get('lens') or '').strip()[:1500]}")
        if pack:
            ctx.append(f"【命中赛道包：{pack['name']}】\n" + "\n".join(f"- {r}" for r in pack["rules"]))
        if card_blocks:
            ctx.append("【知识卡片（引用时标注编号）】\n" + "\n".join(card_blocks))
        if evidence and evidence.get("ok"):
            ctx.append(f"【实时数据：{evidence['query']}，来源 {evidence['source']}】\n"
                       + json.dumps(evidence["digest"], ensure_ascii=False, default=str)[:2500])
        elif evidence:
            ctx.append(f"【实时数据获取失败：{evidence.get('query')}】{evidence.get('error')}（回答时如实说明缺这部分数据）")

        system = ("你是亚马逊选品顾问，熟悉星空 S1 选品方法（市场容量/竞争/趋势/差异化/利润五维 + 做/有条件做/暂缓/不做四档）。\n"
                  "纪律：1) 优先依据下方知识卡片与实时数据作答，引用卡片标注编号；2) 没有数据的维度直说“未获取”，"
                  "绝不编造数字；3) 判断能不能做要给量化依据；需要完整结论时，建议用户在「新建选品」发起一次运行；"
                  "4) 用户问到某套已安装的方法论时，按下方【已安装的方法论】的口径回答，不要拿星空 S1 的五维去套。\n\n"
                  + ("\n\n".join(ctx) if ctx else "（本轮没有注入外部数据）"))
        messages: List[Dict[str, Any]] = [{"role": "system", "content": system}]
        for h in (history or [])[-8:]:
            role = h.get("role")
            if role in ("user", "assistant") and (h.get("content") or "").strip():
                messages.append({"role": role, "content": h["content"][:4000]})
        messages.append({"role": "user", "content": message})

        reply_ok = True
        model_provider: Optional[str] = None
        model_used: Optional[str] = None
        try:
            res = await llm_gateway.chat(messages, role="strong", provider_id=llm_provider or None,
                                         model=llm_model or None, temperature=0.4, max_tokens=3000, timeout=180)
            reply = res["content"]
            model_provider = res["provider"]
            model_used = res["model"]
        except LLMError as exc:
            reply_ok = False
            parts = [f"⚠️ 模型暂时不可用：{exc}", "", "请到「设置 → 模型」检查 MagPie 是否在运行，或切换其他服务商。"]
            if evidence and evidence.get("ok"):
                parts += ["", f"已获取的实时数据（{evidence['source']}）：", "```json",
                          json.dumps(evidence["digest"], ensure_ascii=False, indent=1, default=str)[:2000], "```"]
            if matched_cards:
                parts += ["", "相关知识卡片：" + "、".join(f"{c['card_id']} {c['title']}" for c in matched_cards[:5])]
            reply = "\n".join(parts)

        suggested = []
        if query:
            suggested.append(f"对「{query}」发起完整选品运行")
        if matched_cards:
            suggested.append(f"查看知识卡片 {matched_cards[0]['card_id']}")
        suggested.append("打开方法论库，选择方法后运行")

        ts = datetime.datetime.now().strftime("%H:%M")
        persisted = chat_session_store.append_messages(session_id, [
            {"id": f"u-{uuid.uuid4().hex[:10]}", "role": "user", "content": message, "timestamp": ts},
            {"id": f"a-{uuid.uuid4().hex[:10]}", "role": "assistant", "content": reply, "timestamp": ts,
             "matched_pack": pack, "matched_cards": matched_cards, "suggested_actions": suggested,
             "provider": model_provider, "model": model_used, "ok": reply_ok,
             "evidence": {k: evidence.get(k) for k in ("ok", "query", "source", "error")} if evidence else None},
        ])
        return {"reply": reply, "session_id": persisted["id"], "matched_pack": pack,
                "matched_cards": matched_cards, "suggested_actions": suggested,
                "provider": model_provider, "model": model_used}


selection_workflow_service = SelectionWorkflowService()
