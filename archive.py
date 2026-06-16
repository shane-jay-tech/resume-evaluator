"""数据归档 —— 将终态旧记录归档到 JSON 文件并硬删除。"""

import json
import logging
import os
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


def _get_quarter(date_str: str) -> str:
    """从日期字符串推断所属季度，如 '2026-Q1'。"""
    if not date_str:
        return "unknown"
    try:
        dt = datetime.strptime(date_str[:10], "%Y-%m-%d")
    except ValueError:
        # 尝试其他格式
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                dt = datetime.strptime(date_str[:19], fmt)
                break
            except ValueError:
                continue
        else:
            return "unknown"
    q = (dt.month - 1) // 3 + 1
    return f"{dt.year}-Q{q}"


def archive_old_results(store, config: dict) -> int:
    """
    归档旧的终态记录：
    - 条件: deleted=0, pipeline_status IN ('已淘汰','已通过'), timestamp < retention_days 以前
    - 按季度保存为 JSON
    - 硬删除已归档记录
    - 返回归档数量
    """
    archive_cfg = config.get("archive", {})
    if not archive_cfg.get("enabled", True):
        logger.info("[archive] 归档已禁用")
        return 0

    retention_days = archive_cfg.get("retention_days", 90)
    archive_dir = archive_cfg.get("archive_dir", "archives")
    if not os.path.isabs(archive_dir):
        archive_dir = os.path.join(os.getcwd(), archive_dir)

    cutoff_date = (datetime.now() - timedelta(days=retention_days)).strftime("%Y-%m-%d")

    # 查询需要归档的记录
    rows = store.conn.execute(
        """SELECT * FROM results
           WHERE deleted = 0
           AND pipeline_status IN ('已淘汰', '已通过')
           AND timestamp != ''
           AND timestamp < ?""",
        (cutoff_date,),
    ).fetchall()

    if not rows:
        logger.info("[archive] 无符合归档条件的记录（%d天前终态）", retention_days)
        return 0

    records = [store._row_to_dict(r) for r in rows]

    # 按季度分组
    quarters = {}
    for rec in records:
        q = _get_quarter(rec.get("timestamp", ""))
        quarters.setdefault(q, []).append(rec)

    total_archived = 0
    for q, q_records in quarters.items():
        q_dir = os.path.join(archive_dir, q)
        os.makedirs(q_dir, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        fpath = os.path.join(q_dir, f"archive_{ts}.json")
        # 只保留关键字段
        slim = []
        keep_keys = ["id", "candidate_name", "matched_position", "match_score",
                     "verdict", "dimensions", "highlights", "risks",
                     "pipeline_status", "timestamp", "notes", "report_file",
                     "eval_metadata", "match_method", "match_confidence"]
        for rec in q_records:
            slim.append({k: rec.get(k) for k in keep_keys if k in rec})

        with open(fpath, "w", encoding="utf-8") as f:
            json.dump(slim, f, ensure_ascii=False, indent=2)

        # v3: 软删除（数据保留在数据库，导出可选）。原为硬删除，会永久丢失数据。
        # v5: 仅设 deleted=1，不再额外设 pipeline_status，避免前端不识别的状态组合
        ids = [r["id"] for r in q_records if r.get("id")]
        if ids:
            store.delete_results(ids, soft=True)

        logger.info("[archive] %s: %d 条 → %s", q, len(q_records), fpath)
        total_archived += len(q_records)

    return total_archived
