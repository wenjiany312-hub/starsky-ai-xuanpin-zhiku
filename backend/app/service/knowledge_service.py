import re
import yaml
from pathlib import Path
from typing import Dict, Any, List, Optional
from app.core.config import settings
from app.core.logger import logger
from app.schema.knowledge import (
    KnowledgeStatusResponse,
    KnowledgeCardSummary,
    KnowledgeCardDetail,
    PlaybookItem
)

class KnowledgeService:
    """
    选品知识库服务层，深度接管母库 D:\\06-星空的星球\\05-ZCode\\06-选品知识库
    """

    def __init__(self):
        self.kb_root: Optional[Path] = self._resolve_kb_path()
        self._cards_cache: Dict[str, KnowledgeCardDetail] = {}
        self._playbooks_cache: List[PlaybookItem] = []
        self._scenario_rules_cache: Dict[str, Any] = {}
        self._gaps_count = 0
        # 母库没装（买家机器上就是这样）也要挂内置 knowledge_hub，否则随包发的卡片和手册全成摆设
        self._load_cards_index()
        self._load_playbooks_index()
        self._load_scenario_pack()
        self._gaps_count = self._count_open_gaps()

    def _resolve_kb_path(self) -> Optional[Path]:
        """
        自动检测外挂「母库」路径（开发者机器上有，买家机器上通常没有）。
        买家机器只靠随包发布的 knowledge_hub，见 __init__ 里无条件的本地索引加载。
        """
        candidates = [
            Path(settings.PRIMARY_SELECTION_KB_PATH),
            Path(settings.SECONDARY_SELECTION_KB_PATH),
        ]
        for path in candidates:
            if not path.exists():
                continue
            if (path / "cards").exists() or (path / "knowledge_index.yaml").exists():
                logger.info("已成功深度挂载选品知识库: %s", path)
                return path
        logger.info("未检测到外挂母库，使用随包内置知识库: %s", settings.KNOWLEDGE_HUB_DIR)
        return None

    def _count_open_gaps(self) -> int:
        """
        统计仍在开放状态的缺口条目（母库与内置 knowledge_hub 都算）。
        之前这里写死成 3，买家看到的数字和实际台账无关，属于假数据。
        """
        count = 0
        seen = set()
        candidates = []
        if self.kb_root:
            candidates.append(self.kb_root / "gaps.yaml")
        candidates.append(settings.KNOWLEDGE_HUB_DIR / "gaps.yaml")
        for gap_file in candidates:
            if not gap_file.exists():
                continue
            try:
                data = yaml.safe_load(gap_file.read_text(encoding="utf-8")) or {}
            except Exception as exc:
                logger.warning("解析 %s 失败: %s", gap_file, exc)
                continue
            for item in data.get("gaps", []) or []:
                if not isinstance(item, dict):
                    continue
                # 同一个 topic 下可能先关一条、又开一条新缺口（如实操台账里就有），
                # 所以去重键要带上 name，只按 topic 去重会把后来新开的那条吞掉。
                key = (item.get("topic"), item.get("name"))
                if key in seen:
                    continue
                seen.add(key)
                if str(item.get("status", "open")).lower() != "closed":
                    count += 1
        return count

    def _parse_card_markdown(self, file_path: Path) -> Optional[KnowledgeCardDetail]:
        """
        解析单张知识卡片的 Frontmatter 与各章节正文
        """
        try:
            content = file_path.read_text(encoding="utf-8")
            card_id = file_path.stem
            
            frontmatter_match = re.match(r"^---\n(.*?)\n---\n(.*)$", content, re.DOTALL)
            meta: Dict[str, Any] = {}
            body = content
            if frontmatter_match:
                try:
                    meta = yaml.safe_load(frontmatter_match.group(1)) or {}
                    body = frontmatter_match.group(2)
                except Exception:
                    pass

            tags = meta.get("tags", {})
            modes = tags.get("selection_mode", ["通用"])
            entry_paths = tags.get("entry_path", ["大盘扫描"])
            phases = tags.get("selection_phase", ["综合研判"])

            # 提取结论、机制和操作
            conclusion = ""
            mechanism = ""
            operation = ""

            conclusion_match = re.search(r"## 结论[^\n]*\n+([\s\S]*?)(?=\n##|\Z)", body)
            if conclusion_match:
                conclusion = conclusion_match.group(1).strip()

            mechanism_match = re.search(r"## 机制[^\n]*\n+([\s\S]*?)(?=\n##|\Z)", body)
            if mechanism_match:
                mechanism = mechanism_match.group(1).strip()

            op_match = re.search(r"## 操作[^\n]*\n+([\s\S]*?)(?=\n##|\Z)", body)
            if op_match:
                operation = op_match.group(1).strip()

            return KnowledgeCardDetail(
                card_id=card_id,
                title=meta.get("title", card_id),
                knowledge_type=meta.get("knowledge_type", "threshold"),
                authority=str(meta.get("authority", "B")),
                confidence=float(meta.get("confidence", 0.5)),
                selection_mode=modes if isinstance(modes, list) else [str(modes)],
                entry_path=entry_paths if isinstance(entry_paths, list) else [str(entry_paths)],
                selection_phase=phases if isinstance(phases, list) else [str(phases)],
                parameters=meta.get("parameters", {}),
                conclusion=conclusion or meta.get("title", ""),
                mechanism=mechanism,
                operation=operation,
                sources=meta.get("sources", []),
                full_markdown=content
            )
        except Exception as exc:
            logger.debug("解析卡片失败 %s: %s", file_path.name, str(exc))
            return None

    def _load_cards_index(self) -> None:
        """
        初始化卡片索引字典
        """
        # 1. 扫描母库 cards
        if self.kb_root:
            cards_dir = self.kb_root / "cards"
            if cards_dir.exists():
                for card_file in cards_dir.glob("KC-*.md"):
                    parsed = self._parse_card_markdown(card_file)
                    if parsed:
                        self._cards_cache[parsed.card_id] = parsed

        # 2. 扫描项目内部 knowledge_hub/cards
        local_cards_dir = settings.KNOWLEDGE_HUB_DIR / "cards"
        if local_cards_dir.exists():
            for card_file in local_cards_dir.glob("KC-*.md"):
                parsed = self._parse_card_markdown(card_file)
                if parsed:
                    self._cards_cache[parsed.card_id] = parsed

        # 3. 扫描各垂直赛道包内部 cards
        packs_dir = settings.KNOWLEDGE_HUB_DIR / "category_packs"
        if packs_dir.exists():
            for card_file in packs_dir.glob("*/cards/KC-*.md"):
                parsed = self._parse_card_markdown(card_file)
                if parsed:
                    self._cards_cache[parsed.card_id] = parsed

        logger.info("已建立知识库全量卡片索引: %d 张卡片 (含母库与垂直赛道包)", len(self._cards_cache))

    def _load_playbooks_index(self) -> None:
        """
        加载母库及本地 core_playbooks 目录下的真实实操手册
        """
        items_dict = {}

        # 1. 母库手册
        if self.kb_root:
            pb_dir = self.kb_root / "playbooks"
            if pb_dir.exists():
                for pb_file in pb_dir.glob("PB-*.md"):
                    parts = pb_file.stem.split("-")
                    mode = parts[1] if len(parts) > 1 else "通用"
                    entry_path = parts[2] if len(parts) > 2 else "大盘扫描"
                    phase = parts[3] if len(parts) > 3 else "定模式"

                    items_dict[pb_file.name] = PlaybookItem(
                        file_name=pb_file.name,
                        title=pb_file.stem,
                        mode=mode,
                        entry_path=entry_path,
                        phase=phase,
                        size_bytes=pb_file.stat().st_size
                    )

        # 2. 本地 core_playbooks 手册 (如 PB-06, PB-07 等)
        local_pb_dir = settings.KNOWLEDGE_HUB_DIR / "core_playbooks"
        if local_pb_dir.exists():
            for pb_file in local_pb_dir.glob("PB-*.md"):
                parts = pb_file.stem.split("-")
                mode = parts[1] if len(parts) > 1 else "通用"
                entry_path = parts[2] if len(parts) > 2 else "实战立项"
                phase = parts[3] if len(parts) > 3 else "深度测算"

                items_dict[pb_file.name] = PlaybookItem(
                    file_name=pb_file.name,
                    title=pb_file.stem,
                    mode=mode,
                    entry_path=entry_path,
                    phase=phase,
                    size_bytes=pb_file.stat().st_size
                )

        self._playbooks_cache = sorted(list(items_dict.values()), key=lambda x: x.file_name)
        logger.info("已加载全量 Playbooks: %d 部 (含母库与实操手册)", len(self._playbooks_cache))

    def _load_scenario_pack(self) -> None:
        """
        加载 26,000+ 行的导出版大包 knowledge_pack_for_selection.yaml 的 scenarios
        """
        if not self.kb_root:
            return
        pack_file = self.kb_root / "knowledge_pack_for_selection.yaml"
        if pack_file.exists():
            try:
                # 仅轻量加载前缀或通过流式解析，若全量加载则放入缓存
                with open(pack_file, "r", encoding="utf-8") as f:
                    pack_data = yaml.safe_load(f) or {}
                    self._scenario_rules_cache = pack_data.get("scenarios", {})
                logger.info("已挂载 scenarios 场景规则库: %d 组模式分支", len(self._scenario_rules_cache))
            except Exception as exc:
                logger.warning("解析 scenario pack 异常: %s", str(exc))

    def get_status(self) -> KnowledgeStatusResponse:
        """
        获取选品知识库当前的连接与覆盖度状态。
        有没有外挂母库都算「已连接」——随包 knowledge_hub 本身就是一套可用知识库。
        """
        has_local = bool(self._cards_cache or self._playbooks_cache)
        if self.kb_root and self.kb_root.exists():
            source_path = str(self.kb_root)
        elif has_local:
            source_path = "内置知识库（knowledge_hub）"
        else:
            return KnowledgeStatusResponse(
                is_connected=False,
                source_path="未连接",
                total_cards=0,
                total_playbooks=0,
                tools_available=[],
                gaps_count=0
            )

        return KnowledgeStatusResponse(
            is_connected=True,
            source_path=source_path,
            total_cards=len(self._cards_cache),
            total_playbooks=len(self._playbooks_cache),
            tools_available=[
                "kb_status",
                "kb_scenario_rules",
                "kb_query",
                "kb_diagnose",
                "kb_playbook",
                "kb_get_card",
                "kb_list_gaps",
                "kb_record_observation"
            ],
            gaps_count=self._gaps_count
        )

    def list_cards(
        self,
        mode: Optional[str] = None,
        phase: Optional[str] = None,
        keyword: Optional[str] = None,
        limit: int = 50
    ) -> List[KnowledgeCardSummary]:
        """
        条件检索知识卡片列表
        """
        results: List[KnowledgeCardSummary] = []
        for card in self._cards_cache.values():
            if mode and mode not in card.selection_mode and "通用" not in card.selection_mode:
                continue
            if phase and phase not in card.selection_phase and "通用" not in card.selection_phase:
                continue
            if keyword:
                kw = keyword.lower()
                if kw not in card.title.lower() and kw not in card.conclusion.lower():
                    continue

            results.append(KnowledgeCardSummary(
                card_id=card.card_id,
                title=card.title,
                phase="/".join(card.selection_phase),
                mode="/".join(card.selection_mode),
                authority=card.authority,
                confidence=card.confidence,
                tags=card.selection_mode + card.selection_phase
            ))
            if len(results) >= limit:
                break
        return results

    def search_cards_by_text(
        self,
        text: str,
        limit: int = 6,
        min_score: int = 2
    ) -> List[KnowledgeCardSummary]:
        """
        基于消息文本的相关性检索（轻量 BM25 风格打分）：
        - 卡片标题中文二元组命中消息 ×2
        - 卡片模式/阶段标签命中 ×3
        - 消息中的英文实词命中卡片标题或结论 ×2
        返回按得分排序的卡片摘要列表。
        """
        msg = text.lower()
        msg_en_words = {w.lower() for w in re.findall(r"[A-Za-z][A-Za-z\-]{2,}", text)}

        scored = []
        for card in self._cards_cache.values():
            score = 0
            # 标题中文二元组
            bigrams = set()
            for seg in re.findall(r"[\u4e00-\u9fff]+", card.title):
                for i in range(len(seg) - 1):
                    bigrams.add(seg[i:i + 2])
            for bg in bigrams:
                if bg in msg:
                    score += 2
            # 模式/阶段标签
            for tag in card.selection_mode + card.selection_phase:
                if len(tag) >= 2 and tag.lower() in msg:
                    score += 3
            # 英文实词命中卡片内容
            card_text = f"{card.title} {card.conclusion}".lower()
            for w in msg_en_words:
                if len(w) >= 3 and w in card_text:
                    score += 2
            if score >= min_score:
                scored.append((score, card))

        scored.sort(key=lambda x: (-x[0], x[1].card_id))
        return [
            KnowledgeCardSummary(
                card_id=c.card_id,
                title=c.title,
                phase="/".join(c.selection_phase),
                mode="/".join(c.selection_mode),
                authority=c.authority,
                confidence=c.confidence,
                tags=c.selection_mode + c.selection_phase
            )
            for _, c in scored[:limit]
        ]

    def get_card_detail(self, card_id: str) -> Optional[KnowledgeCardDetail]:
        """
        获取指定卡片的完整解析内容
        """
        return self._cards_cache.get(card_id)

    def list_playbooks(self) -> List[PlaybookItem]:
        """
        获取母库所有 Playbook 列表
        """
        return self._playbooks_cache

    def get_playbook(self, playbook_name: str) -> Optional[str]:
        """
        获取选品手册全文（兼容母库与本地 core_playbooks）
        """
        clean_name = playbook_name.replace(".md", "")
        
        # 1. 尝试从母库获取
        if self.kb_root:
            pb_path = self.kb_root / "playbooks" / f"{clean_name}.md"
            if pb_path.exists():
                return pb_path.read_text(encoding="utf-8")
        
        # 2. 尝试从本地 core_playbooks 获取
        local_pb_path = settings.KNOWLEDGE_HUB_DIR / "core_playbooks" / f"{clean_name}.md"
        if local_pb_path.exists():
            return local_pb_path.read_text(encoding="utf-8")
            
        return None

    def get_scenario_rules(self, selection_mode: str, entry_path: str, selection_phase: str) -> List[Dict[str, Any]]:
        """
        从 scenarios 中按模式×入口×阶段调取规则
        """
        key = f"{selection_mode}|{entry_path}|{selection_phase}"
        if key in self._scenario_rules_cache:
            return self._scenario_rules_cache[key].get("rules", [])
        
        # 降级匹配含“通用”的分支
        for k, v in self._scenario_rules_cache.items():
            if selection_phase in k:
                return v.get("rules", [])
        return []

knowledge_service = KnowledgeService()
