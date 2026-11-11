import logging
import sys
from typing import Optional

def setup_logger(name: str = "selection_terminal", level: Optional[str] = None) -> logging.Logger:
    """
    配置并获取标准化日志记录器
    """
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        formatter = logging.Formatter(
            fmt="[%(asctime)s] [%(levelname)s] [%(name)s:%(lineno)d]: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        log_level = getattr(logging, (level or "INFO").upper(), logging.INFO)
        logger.setLevel(log_level)
    return logger

logger = setup_logger()
