import re
import yaml
from pathlib import Path
from typing import Dict, Any, List, Optional
from app.core.config import settings
from app.core.logger import logger
from app.model.domain import CategoryPackMetadata
from app.schema.knowledge import CategoryPackDetailResponse, KnowledgeCardSummary

class CategoryPackService:
    """
    垂直细分赛道包服务层，管理各类目专属的选品规则与资料
    """

    def __init__(self):
        self.packs_dir = settings.KNOWLEDGE_HUB_DIR / "category_packs"
        self._ensure_category_packs_dir()

    def _ensure_category_packs_dir(self) -> None:
        self.packs_dir.mkdir(parents=True, exist_ok=True)

    def list_packs(self) -> List[CategoryPackMetadata]:
        """
        列出所有已挂载的垂直赛道包
        """
        packs: List[CategoryPackMetadata] = []
        if not self.packs_dir.exists():
            return packs

        for item in self.packs_dir.iterdir():
            if item.is_dir():
                info_file = item / "pack_info.yaml"
                rules_file = item / "rules.yaml"
                cards_dir = item / "cards"
                
                name = item.name
                desc = f"{item.name} 细分赛道选品包"
                cat_path = item.name
                enabled = True

                if info_file.exists():
                    try:
                        with open(info_file, "r", encoding="utf-8") as f:
                            data = yaml.safe_load(f) or {}
                            name = data.get("name", name)
                            desc = data.get("description", desc)
                            cat_path = data.get("category_path", cat_path)
                            enabled = data.get("enabled", True)
                    except Exception as exc:
                        logger.warning("解析赛道包信息异常 %s: %s", item.name, str(exc))

                rules_count = 0
                if rules_file.exists():
                    try:
                        with open(rules_file, "r", encoding="utf-8") as f:
                            rules_data = yaml.safe_load(f) or {}
                            rules_count = len(rules_data)
                    except Exception:
                        pass

                cards_count = len(list(cards_dir.glob("*.md"))) if cards_dir.exists() else 0

                packs.append(CategoryPackMetadata(
                    pack_id=item.name,
                    name=name,
                    description=desc,
                    category_path=cat_path,
                    sensitive_rules_count=rules_count,
                    cards_count=cards_count,
                    enabled=enabled
                ))
        return packs

    def get_pack_detail(self, pack_id: str) -> Optional[CategoryPackDetailResponse]:
        """
        获取单个赛道包的完整规则与指南
        """
        # pack_id 来自请求体，必须是赛道包目录下的直接子目录，不能 ../ 跳出去读别处文件
        if not pack_id or not re.fullmatch(r"[A-Za-z0-9_\-一-鿿]{1,64}", pack_id):
            return None
        target_dir = (self.packs_dir / pack_id).resolve()
        if target_dir.parent != self.packs_dir.resolve() or not target_dir.is_dir():
            return None

        rules: Dict[str, Any] = {}
        rules_file = target_dir / "rules.yaml"
        if rules_file.exists():
            try:
                with open(rules_file, "r", encoding="utf-8") as f:
                    rules = yaml.safe_load(f) or {}
            except Exception as exc:
                logger.error("读取赛道包规则错误: %s", str(exc))

        supplier_guidelines = ""
        guide_file = target_dir / "supplier_guidelines.md"
        if guide_file.exists():
            try:
                supplier_guidelines = guide_file.read_text(encoding="utf-8")
            except Exception:
                pass

        cards: List[KnowledgeCardSummary] = []
        cards_dir = target_dir / "cards"
        if cards_dir.exists():
            for c in cards_dir.glob("*.md"):
                c_content = c.read_text(encoding="utf-8")
                # 尝试解析卡片标题与置信度
                c_title = f"专属卡: {c.stem}"
                c_conf = 0.9
                c_auth = "B"
                
                title_match = re.search(r'title:\s*["\']?([^"\']+)["\']?', c_content)
                if title_match:
                    c_title = title_match.group(1).strip()
                
                conf_match = re.search(r'confidence:\s*([0-9.]+)', c_content)
                if conf_match:
                    try:
                        c_conf = float(conf_match.group(1))
                    except Exception:
                        c_conf = 0.9

                auth_match = re.search(r'authority:\s*([A-Za-z])', c_content)
                if auth_match:
                    c_auth = auth_match.group(1).strip()

                cards.append(KnowledgeCardSummary(
                    card_id=c.stem,
                    title=c_title,
                    phase="赛道深度规则",
                    mode=pack_id,
                    authority=c_auth,
                    confidence=c_conf,
                    tags=["垂直细分", pack_id]
                ))

        return CategoryPackDetailResponse(
            pack_id=pack_id,
            name=rules.get("name", pack_id),
            description=rules.get("description", "垂直细分赛道专属包"),
            category_path=rules.get("category_path", pack_id),
            rules=rules,
            cards=cards,
            supplier_guidelines=supplier_guidelines
        )

category_pack_service = CategoryPackService()
