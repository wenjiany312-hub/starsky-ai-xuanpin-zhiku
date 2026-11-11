from enum import Enum
from typing import Dict, Any, List, Optional
from pydantic import BaseModel, Field

class WorkflowType(str, Enum):
    """
    选品工作流类型枚举
    """
    ZERO_TO_ONE = "zero_to_one"              # 从0到1破局选品
    FACTORY_SUPPLY = "factory_supply"        # 垂直工厂/供应链选品
    VERTICAL_CATEGORY = "vertical_category"  # 垂直细分类目深入选品 (挂载赛道包)
    BLUE_OCEAN_VOC = "blue_ocean_voc"        # 红海突围与蓝海痛点创新
    TREND_HOTSPOT = "trend_hotspot"          # 热点/社媒裂变选品 (Reddit/TikTok)
    COMPETITOR_STORE = "competitor_store"    # 竞品店铺全店穿透选品
    JINGPU_MATRIX = "jingpu_matrix"          # 精铺与多变体长尾选品

class TaskStatus(str, Enum):
    """
    选品任务生命周期状态
    """
    PENDING = "pending"
    KNOWLEDGE_RETRIEVING = "knowledge_retrieving"
    DATA_FETCHING = "data_fetching"
    AI_EVALUATING = "ai_evaluating"
    REPORT_GENERATING = "report_generating"
    COMPLETED = "completed"
    FAILED = "failed"

class CategoryPackMetadata(BaseModel):
    """
    垂直赛道包元数据
    """
    pack_id: str
    name: str
    description: str
    category_path: str
    sensitive_rules_count: int = 0
    cards_count: int = 0
    enabled: bool = True

class SelectionTask(BaseModel):
    """
    选品分析任务实体
    """
    task_id: str
    workflow_type: WorkflowType
    title: str
    category_pack_id: Optional[str] = None
    target_market: str = "US"
    parameters: Dict[str, Any] = Field(default_factory=dict)
    status: TaskStatus = TaskStatus.PENDING
    progress: int = 0
    created_at: str
    updated_at: str
    results_summary: Optional[Dict[str, Any]] = None
    report_file_path: Optional[str] = None
    # v3：可观测的运行信息（旧前端不受影响，全部带默认值）
    run_dir: Optional[str] = None
    method_pack_id: Optional[str] = None
    llm_provider: Optional[str] = None
    llm_model: Optional[str] = None
    steps: List[Dict[str, Any]] = Field(default_factory=list)
    verdict: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
