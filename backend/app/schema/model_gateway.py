from typing import Dict, Any, List, Optional
from pydantic import BaseModel, Field

class UpdateModelConfigRequest(BaseModel):
    """
    更新模型配置请求
    """
    provider_id: str = Field(..., description="提供商ID (deepseek/glm/openai/ollama)")
    api_key: Optional[str] = Field(None, description="API Key")
    base_url: Optional[str] = Field(None, description="API 基础端点地址")
    model_name: Optional[str] = Field(None, description="首选模型名称(如 glm-5.3)")

class TestModelConnectionRequest(BaseModel):
    """
    测试模型连通性请求
    """
    provider_id: str = Field(..., description="提供商ID")
    model_name: Optional[str] = Field(None, description="指定模型名称")

class ModelProviderInfo(BaseModel):
    """
    模型提供商信息
    """
    id: str
    name: str
    base_url: str
    is_configured: bool
    models: List[str]

class ModelProvidersListResponse(BaseModel):
    """
    模型提供商列表响应
    """
    providers: List[ModelProviderInfo]
    current_default_provider: str
    current_default_model: str
