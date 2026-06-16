"""结构化日志模块 —— TimedRotatingFileHandler + 控制台输出。"""

import logging
import os
from logging.handlers import TimedRotatingFileHandler


def setup_logging(config: dict) -> logging.Logger:
    log_cfg = config.get("logging", {})
    level = getattr(logging, log_cfg.get("level", "INFO").upper(), logging.INFO)
    log_path = os.path.expanduser(log_cfg.get("path", "logs/app.log"))
    retention = log_cfg.get("retention_days", 7)

    os.makedirs(os.path.dirname(log_path), exist_ok=True)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(level)

    # 清除已有 handler，避免重复
    root.handlers.clear()

    # 文件 handler：每天轮转，保留 N 天
    fh = TimedRotatingFileHandler(log_path, when="midnight", interval=1, backupCount=retention, encoding="utf-8")
    fh.setLevel(level)
    fh.setFormatter(fmt)
    root.addHandler(fh)

    # 控制台 handler
    ch = logging.StreamHandler()
    ch.setLevel(level)
    ch.setFormatter(fmt)
    root.addHandler(ch)

    return logging.getLogger("resume_evaluator")
