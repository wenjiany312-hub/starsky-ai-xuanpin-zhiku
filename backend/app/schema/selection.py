import re
from typing import Dict, Any, List, Optional
from pydantic import BaseModel, Field, field_validator
from app.model.domain import WorkflowType, TaskStatus

_MARKETS = {"US", "UK", "DE", "FR", "IT", "ES", "CA", "JP", "MX", "AU", "AE", "SA", "BR", "IN"}


class CreateSelectionTaskRequest(BaseModel):
    """
    发起选品分析任务的请求体
    """
    workflow_type: WorkflowType = Field(..., description="选品工作流类型")
    title: str = Field(..., min_length=2, max_length=100, description="任务标题或产品概念")
    category_pack_id: Optional[str] = Field(None, description="挂载的垂直赛道包ID(可选)")
    target_market: str = Field("US", description="目标亚马逊站点(如 US, UK, DE)")

    @field_validator("title")
    @classmethod
    def _title_has_content(cls, v: str) -> str:
        v = v.strip()
        # 只有空格/标点的标题会被清成空串，模型再凭空编一个关键词去花钱取数
        if len(re.sub(r"[\W_]+", "", v)) < 2:
            raise ValueError("请用至少两个字描述产品，例如「猫抓板」或 cat scratcher")
        return v

    @field_validator("target_market")
    @classmethod
    def _known_market(cls, v: str) -> str:
        v = (v or "US").strip().upper()
        if v == "GB":
            v = "UK"
        if v not in _MARKETS:
            raise ValueError(f"不支持的站点 {v}，可选：{'、'.join(sorted(_MARKETS))}")
        return v
    parameters: Dict[str, Any] = Field(default_factory=dict, description="业务定制参数(如预算区间、类目ID、竞品ASIN等)")
    llm_provider: Optional[str] = Field(None, description="研判用模型服务商；留空=设置里“综合研判”角色的模型（默认 MagPie）")
    llm_model: Optional[str] = Field(None, description="模型名称；留空跟随角色设置")
    method_pack_id: Optional[str] = Field(None, description="方法论包 ID；留空=星空 S1 快速版")

class SelectionTaskResponse(BaseModel):
    """
    选品任务详情返回体
    """
    task_id: str
    workflow_type: WorkflowType
    title: str
    category_pack_id: Optional[str] = None
    target_market: str
    status: TaskStatus
    progress: int
    created_at: str
    updated_at: str
    results_summary: Optional[Dict[str, Any]] = None
    report_file_path: Optional[str] = None
    parameters: Dict[str, Any] = Field(default_factory=dict)
    run_dir: Optional[str] = None
    method_pack_id: Optional[str] = None
    llm_provider: Optional[str] = None
    llm_model: Optional[str] = None
    steps: List[Dict[str, Any]] = Field(default_factory=list)
    verdict: Optional[Dict[str, Any]] = None
    error: Optional[str] = None

class SelectionTaskItemResponse(BaseModel):
    """
    选品任务简要列表项
    """
    task_id: str
    workflow_type: WorkflowType
    title: str
    status: TaskStatus
    progress: int
    created_at: str
    target_market: str = "US"
    method_pack_id: Optional[str] = None
    verdict: Optional[Dict[str, Any]] = None
    has_report: bool = Field(False, description="运行目录里是否真的有一份 report.md（老记录可能没有）")

class ChatSelectionRequest(BaseModel):
    """
    选品对话提问请求体
    """
    message: str = Field(..., min_length=1, description="用户自然语言提问或产品描述")
    history: List[Dict[str, str]] = Field(default_factory=list, description="对话历史上下文")
    session_id: Optional[str] = Field(None, description="会话ID(为空则服务端自动创建新会话)")
    category_pack_id: Optional[str] = Field(None, description="指定赛道包(可选)")
    llm_provider: Optional[str] = Field(None, description="指定模型提供商")
    llm_model: Optional[str] = Field(None, description="指定模型名称")

class ChatSelectionResponse(BaseModel):
    """
    选品对话智能回复体
    """
    reply: str
    session_id: Optional[str] = None
    matched_pack: Optional[Dict[str, Any]] = None
    matched_cards: List[Dict[str, Any]] = Field(default_factory=list)
    suggested_actions: List[str] = Field(default_factory=list)
    provider: Optional[str] = Field(None, description="实际作答的模型服务商（模型不可用时为空）")
    model: Optional[str] = Field(None, description="实际作答的模型名称（模型不可用时为空）")

class ChatSessionSummary(BaseModel):
    """
    对话会话列表项
    """
    id: str
    title: str
    updated_at: Optional[str] = None
    message_count: int = 0

class ChatSessionDetail(BaseModel):
    """
    对话会话详情（含全部消息）
    """
    id: str
    title: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    messages: List[Dict[str, Any]] = Field(default_factory=list)


class StarskyExportRequest(BaseModel):
    """导出成星空 S1 标准产物目录时的可选项（都可留空，留空用运行时的取值）。"""

    candidate_names: List[str] = Field(
        default_factory=list,
        description="补充方向名（不含主方向）。星空第 2 步要求 3–5 个方向，主方向已自动占 1 个。")
    product_name: Optional[str] = Field(None, description="产品名/目录名；留空用运行标题")
    marketplace: Optional[str] = Field(None, description="站点，如 US；留空用运行时的站点")
    price_band: Optional[str] = Field(None, description="价带，如 15-25；留空用默认 15-25")
    target_margin_pre_ad: Optional[float] = Field(
        None, description="广告前目标毛利，0–1 或 0–100；留空用 0.40")
    initial_budget: Optional[float] = Field(None, description="首批预算（可选）")
    budget_currency: str = Field("CNY", description="首批预算币种")


class StarskyExportResponse(BaseModel):
    """导出结果：落了哪些文件、方向表长什么样、还缺什么。"""

    product_dir: str
    files: Dict[str, str] = Field(default_factory=dict)
    candidates: List[Dict[str, Any]] = Field(default_factory=list)
    scored_count: int = 0
    warnings: List[str] = Field(default_factory=list)
    verdict: Optional[str] = None
    weights: Dict[str, float] = Field(default_factory=dict)
