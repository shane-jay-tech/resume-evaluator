"""数据库自动备份 —— 定时复制 SQLite 到 backups/ 目录。"""

import logging
import os
import shutil
import time
from datetime import datetime

logger = logging.getLogger(__name__)


def backup_database(db_path: str, backup_dir: str = "backups", keep_days: int = 30) -> str:
    """
    复制 SQLite 数据库到备份目录。
    - db_path: 数据库文件路径
    - backup_dir: 备份目标目录
    - keep_days: 保留天数，超过的自动删除
    - 返回备份文件路径，失败返回空串
    """
    if not os.path.exists(db_path):
        logger.warning("[backup] 数据库文件不存在: %s", db_path)
        return ""

    if not os.path.isabs(backup_dir):
        backup_dir = os.path.join(os.getcwd(), backup_dir)

    os.makedirs(backup_dir, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = f"recruitment_backup_{ts}.db"
    dest = os.path.join(backup_dir, fname)

    try:
        # 使用 SQLite 在线备份确保一致性
        import sqlite3
        src_conn = sqlite3.connect(db_path)
        dst_conn = sqlite3.connect(dest)
        src_conn.backup(dst_conn)
        src_conn.close()
        dst_conn.close()
    except Exception:
        # 回退到文件复制
        shutil.copy2(db_path, dest)

    # 清理过期备份
    cutoff = time.time() - keep_days * 86400
    for f in os.listdir(backup_dir):
        fpath = os.path.join(backup_dir, f)
        if f.startswith("recruitment_backup_") and f.endswith(".db") and os.path.getmtime(fpath) < cutoff:
            try:
                os.remove(fpath)
                logger.info("[backup] 清理过期备份: %s", f)
            except OSError:
                pass

    file_size = os.path.getsize(dest)
    logger.info("[backup] 数据库备份完成: %s (%.1f KB)", dest, file_size / 1024)
    return dest


def start_backup_scheduler(db_path: str, interval_hours: int = 6, keep_days: int = 30, backup_dir: str = "backups"):
    """启动后台定时备份线程。"""
    def _run():
        time.sleep(600)  # 启动后等 10 分钟再开始
        while True:
            try:
                backup_database(db_path, backup_dir, keep_days)
            except Exception as e:
                logger.warning("[backup] 备份失败: %s", e)
            time.sleep(interval_hours * 3600)

    import threading
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    logger.info("[backup] 定时备份已启动（间隔 %d 小时，保留 %d 天）", interval_hours, keep_days)
    return t
