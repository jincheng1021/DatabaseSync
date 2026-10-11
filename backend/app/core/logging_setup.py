"""运行日志框架（T1.7 / §4.7）：按天滚动，保留天数可配。"""
from __future__ import annotations

import logging
import logging.handlers

from app.config import LOG_DIR, LOG_RETENTION_DAYS


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger("dsync")
    if logger.handlers:
        return logger
    logger.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")

    # 按天滚动，保留 N 天
    file_handler = logging.handlers.TimedRotatingFileHandler(
        LOG_DIR / "app.log", when="midnight", backupCount=LOG_RETENTION_DAYS,
        encoding="utf-8")
    file_handler.setFormatter(fmt)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger.addHandler(file_handler)
    logger.addHandler(console)
    logger.propagate = False
    return logger
