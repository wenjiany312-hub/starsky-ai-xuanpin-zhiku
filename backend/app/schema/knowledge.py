from typing import Dict, Any, List, Optional
from pydantic import BaseModel, Field

class KnowledgeCardSummary(BaseModel):
    """
    选品知识卡片摘要
    """
    card_id: str
    title: str
    phase: str
    mode: str
    authority: str = "B"
    confidence: float = 0.5
    tags: List[str] = Field(default_factory=list)

class KnowledgeCardDetail(BaseModel):
    """
    选品知识卡片完整详情（包含 Frontmatter、七硬门槛参数、结论、出处引用）
    """
    card_id: str
    title: str
    knowledge_type: str = "threshold"
    authority: str = "B"
    confidence: float = 0.5
    selection_mode: List[str] = Field(default_factory=list)
    entry_path: List[str] = Field(default_factory=list)
    selection_phase: List[str] = Field(default_factory=list)
    parameters: Dict[str, Any] = Field(default_factory=dict)
    conclusion: str = ""
    mechanism: str = ""
    operation: str = ""
    sources: List[Dict[str, Any]] = Field(default_factory=list)
    full_markdown: str = ""

class PlaybookItem(BaseModel):
    """
    选品实操场景手册简要
    """
    file_name: str
    title: str
    mode: str
    entry_path: str
    phase: str
    size_bytes: int

class KnowledgeStatusResponse(BaseModel):
    """
    知识库连接与健康状态响应
    """
    is_connected: bool
    source_path: str
    total_cards: int
    total_playbooks: int
    tools_available: List[str]
    gaps_count: int

class CategoryPackDetailResponse(BaseModel):
    """
    垂直细分赛道包详情响应
    """
    pack_id: str
    name: str
    description: str
    category_path: str
    rules: Dict[str, Any]
    cards: List[KnowledgeCardSummary]
    supplier_guidelines: Optional[str] = None
