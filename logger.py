# -*- coding: utf-8 -*-
"""
查尔斯信号监控系统 · 统一日志模块
- 输出到 logs/<name>.log（按 2MB 轮转，保留 5 个备份）与控制台（可选）
- 时间统一北京时间（GitHub runner 默认 UTC，须先设 TZ 再 tzset）
- 级别可通过环境变量 CHARLES_LOG_LEVEL 或 setup(level=...) 覆盖

用法：
    import logger
    log = logger.setup("monitor")          # monitor.py 等入口脚本调用一次
    log = logging.getLogger(__name__)      # 其他模块直接继承根配置
"""
import logging
import os
import time
from logging.handlers import RotatingFileHandler

_LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
_FORMAT = "%(asctime)s [%(levelname)s] [%(name)s] %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_LEVELS = {"DEBUG": logging.DEBUG, "INFO": logging.INFO, "WARNING": logging.WARNING,
           "ERROR": logging.ERROR, "CRITICAL": logging.CRITICAL}


def setup(name: str = "charles", log_dir: str = None, level: int = None,
          console: bool = True) -> logging.Logger:
    """配置根 logger（所有模块的 getLogger(__name__) 自动继承）。
    name 仅用于日志文件名；level 未传时读环境变量 CHARLES_LOG_LEVEL，默认 INFO。"""
    # 统一使用北京时间
    os.environ.setdefault("TZ", "Asia/Shanghai")
    time.tzset()

    if level is None:
        env = os.environ.get("CHARLES_LOG_LEVEL", "INFO").strip().upper()
        level = _LEVELS.get(env, logging.INFO)

    log_dir = log_dir or _LOG_DIR
    os.makedirs(log_dir, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(level)
    # 清掉旧 handler，避免重复添加（多次 setup 幂等）
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()

    fmt = logging.Formatter(_FORMAT, datefmt=_DATE_FORMAT)

    fh = RotatingFileHandler(os.path.join(log_dir, name + ".log"),
                             maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)

    if console:
        ch = logging.StreamHandler()
        ch.setFormatter(fmt)
        root.addHandler(ch)

    logging.getLogger(__name__).debug("日志系统就绪：%s（level=%s, console=%s）",
                                      os.path.join(log_dir, name + ".log"),
                                      logging.getLevelName(level), console)
    return root
