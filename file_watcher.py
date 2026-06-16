"""文件监控模块 — 稳定性检测替代硬编码 sleep。

处理流程：
1. 检测到新文件 → 忽略临时后缀
2. 等待文件稳定（连续 N 次检查大小不变）
3. 再交给 process_resume 处理

跨平台：macOS 用 Observer (FSEvents)，Windows 用 PollingObserver。
"""

import logging
import os
import sys
import time
from pathlib import Path

from watchdog.events import FileSystemEventHandler

logger = logging.getLogger(__name__)

# 临时文件后缀（浏览器下载中、Office 临时文件等）
TEMP_SUFFIXES = {".crdownload", ".tmp", ".part", ".download", ".~tmp"}
TEMP_PREFIXES = {".", "~$"}

# 稳定性检测配置
STABLE_CHECKS = 3       # 连续 N 次文件大小不变视为稳定
STABLE_INTERVAL = 1.0   # 每次检查间隔（秒）
MAX_WAIT = 30.0         # 最大等待时间（秒）


class ResumeHandler(FileSystemEventHandler):
    """简历文件监控处理器。"""

    def __init__(self, process_callback):
        super().__init__()
        self.extensions = {".pdf", ".docx", ".doc", ".jpeg", ".jpg", ".png"}
        self.process_callback = process_callback  # process_resume 函数

    def on_created(self, event):
        if event.is_directory:
            return
        filepath = event.src_path
        fname = os.path.basename(filepath)

        # 忽略临时文件和隐藏文件
        if self._is_temp_file(fname):
            logger.debug("忽略临时文件: %s", fname)
            return

        ext = Path(filepath).suffix.lower()
        if ext not in self.extensions:
            return

        logger.info("检测到新文件: %s", fname)

        # 等待文件稳定后再处理
        if self._wait_stable(filepath):
            logger.info("文件已稳定，开始处理: %s", fname)
            self.process_callback(filepath)
        else:
            logger.warning("文件超时未稳定，跳过: %s", fname)

    @staticmethod
    def _is_temp_file(filename: str) -> bool:
        """判断是否为临时文件。"""
        # 前缀判断
        for prefix in TEMP_PREFIXES:
            if filename.startswith(prefix):
                return True
        # 后缀判断
        ext = Path(filename).suffix.lower()
        if ext in TEMP_SUFFIXES:
            return True
        return False

    @staticmethod
    def _wait_stable(filepath: str) -> bool:
        """等待文件大小稳定。

        Returns:
            True: 文件已稳定
            False: 超时未稳定
        """
        elapsed = 0.0
        stable_count = 0
        last_size = -1

        while elapsed < MAX_WAIT:
            try:
                current_size = os.path.getsize(filepath)
            except OSError:
                # 文件可能还在写入中，无法获取大小
                time.sleep(STABLE_INTERVAL)
                elapsed += STABLE_INTERVAL
                continue

            if current_size == last_size and current_size > 0:
                stable_count += 1
                if stable_count >= STABLE_CHECKS:
                    return True
            else:
                stable_count = 0
                last_size = current_size

            time.sleep(STABLE_INTERVAL)
            elapsed += STABLE_INTERVAL

        return False


def scan_existing(watch_dir: str, store, process_callback):
    """扫描目录中已有但未处理的文件。"""
    extensions = {".pdf", ".docx", ".doc", ".jpeg", ".jpg", ".png"}
    from parser import file_hash

    for f in sorted(Path(watch_dir).iterdir()):
        if f.suffix.lower() not in extensions:
            continue
        if f.name.startswith(".") or f.name.startswith("~$"):
            continue
        fpath = str(f)
        fh = file_hash(fpath)
        if store.is_processed(fh) or store.pending_exists_by_fhash(fh):
            continue
        if store.is_resume_processed(f.name):
            store.add_processed(fh)
            continue
        logger.info("发现未处理文件: %s", f.name)
        process_callback(fpath)
