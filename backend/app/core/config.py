from pathlib import Path
from typing import List, Optional
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """
    系统全局配置类，基于 Pydantic BaseSettings 自动加载环境变量。
    用户在界面里改的设置（模型 Key、角色模型、数据源开关）不在这里，见 settings_store.py。
    """
    APP_NAME: str = "AI 选品智库"
    APP_VERSION: str = "3.0.1"
    API_V1_PREFIX: str = "/api/v1"
    DEBUG: bool = False
    SERVER_HOST: str = "127.0.0.1"
    SERVER_PORT: int = 8006

    # 路径配置
    BASE_DIR: Path = Path(__file__).resolve().parent.parent.parent.parent
    KNOWLEDGE_HUB_DIR: Path = BASE_DIR / "knowledge_hub"
    STORAGE_DIR: Path = BASE_DIR / "data_storage"
    RUNS_DIR: Path = BASE_DIR / "data_storage" / "runs"
    UPLOADS_DIR: Path = BASE_DIR / "data_storage" / "uploads"
    LOCAL_CONFIG_DIR: Path = BASE_DIR / "config" / "local"
    METHOD_PACKS_DIR: Path = BASE_DIR / "method_packs"

    # 星空 Agent 产物目录：导出时在 STARSKY_ROOT_DIR/<日期>_<标题>/ 下落一套 S1 标准文件。
    # 默认与星空 Codex/Claude 版一致（~/Starsky-Claude/product_runs）；
    # 有工作区时用 STARSKY_WORKSPACE 指向工作区根目录，会自动补 product_runs。
    STARSKY_ROOT_DIR: Path = Path.home() / "Starsky-Claude" / "product_runs"
    # 星空插件安装目录；留空则自动发现已安装的最新版本（用来读 workflow_state 模板）
    STARSKY_PLUGIN_DIR: str = ""

    # 本地已有选品知识库候选路径（自动发现与挂接）
    # Mac 布局: 本项目位于 封装的MCP/AI选品智库，母库位于同级 封装的MCP/06-选品知识库
    PRIMARY_SELECTION_KB_PATH: str = str((BASE_DIR.parent / "06-选品知识库").resolve())
    SECONDARY_SELECTION_KB_PATH: str = r"D:\06-星空的星球\11-Cursor\01-选品知识库"

    # 模型网关（可被 config/local/settings.json 覆盖）
    DEEPSEEK_API_KEY: Optional[str] = None
    DEEPSEEK_BASE_URL: str = "https://api.deepseek.com/v1"
    GLM_API_KEY: Optional[str] = None
    GLM_BASE_URL: str = "https://open.bigmodel.cn/api/paas/v4"
    OPENAI_API_KEY: Optional[str] = None
    OPENAI_BASE_URL: str = "https://api.openai.com/v1"
    OLLAMA_BASE_URL: str = "http://localhost:11434/v1"
    # MagPie 本地模型网关（OpenAI 兼容，自动路由到已配置的账号或 API，免 Key）
    MAGPIE_BASE_URL: str = "http://127.0.0.1:3425/v1"

    # 只允许本机前端访问（开发态 Vite 5173 + 一体化 8006）
    CORS_ORIGINS: List[str] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:8006",
        "http://127.0.0.1:8006",
    ]

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = True


settings = Settings()
