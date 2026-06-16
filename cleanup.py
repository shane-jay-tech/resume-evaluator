"""自动清理 —— 将一周前的候选人数据导出本地后软删除。"""

import json
import logging
import os
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


def cleanup_old_candidates(store, config: dict) -> dict:
    """
    清理 retention_days 天以前的候选人数据：
    - results 表: 软删除（deleted=1）
    - pending 表: 标记为 dismissed
    - 清理前先导出为 JSON 到 archives/cleanup/ 目录
    - 返回 {"results_cleaned": N, "pending_cleaned": N, "export_dir": "..."}
    """
    cleanup_cfg = config.get("cleanup", {})
    if not cleanup_cfg.get("enabled", True):
        logger.info("[cleanup] 清理功能已禁用")
        return {"results_cleaned": 0, "pending_cleaned": 0, "export_dir": ""}

    retention_days = cleanup_cfg.get("retention_days", 7)
    eliminated_retention_days = cleanup_cfg.get("eliminated_retention_days", 1)  # 已淘汰保留1天
    cleanup_dir = cleanup_cfg.get("cleanup_dir", "archives/cleanup")
    if not os.path.isabs(cleanup_dir):
        cleanup_dir = os.path.join(os.getcwd(), cleanup_dir)

    now = datetime.now()
    default_cutoff = (now - timedelta(days=retention_days)).strftime("%Y-%m-%d %H:%M:%S")
    eliminated_cutoff = (now - timedelta(days=eliminated_retention_days)).strftime("%Y-%m-%d %H:%M:%S")
    date_label = now.strftime("%Y-%m-%d")

    # ── 1. 查询需要清理的 results ──
    # v3 策略：
    #   - 面试中/已通过：永不删除
    #   - 强烈推荐/推荐：永不删除（高分候选人值得保留）
    #   - 已淘汰：保留1天软删除
    #   - 待筛选：保留 N 天
    all_active = store.get_all_results(include_deleted=False)
    results_records = [
        r for r in all_active
        if r.get("timestamp", "") != ""
        and r.get("pipeline_status") not in ("面试中", "已通过")
        and r.get("verdict") not in ("强烈推荐", "推荐")
        and (
            (r.get("pipeline_status") == "已淘汰" and r["timestamp"] < eliminated_cutoff)
            or
            (r.get("pipeline_status") not in ("已淘汰",) and r["timestamp"] < default_cutoff)
        )
    ]

    # ── 2. 查询需要清理的 pending ──
    all_pending = store.get_all_pending()
    pending_records = [
        p for p in all_pending
        if p.get("status") == "pending"
        and p.get("timestamp", "") != ""
        and p["timestamp"] < default_cutoff
    ]


    if not results_records and not pending_records:
        logger.info("[cleanup] 无符合条件的记录（%d天前）", retention_days)
        return {"results_cleaned": 0, "pending_cleaned": 0, "export_dir": ""}

    # ── 3. 导出到本地 JSON ──
    export_dir = os.path.join(cleanup_dir, date_label)
    os.makedirs(export_dir, exist_ok=True)

    if results_records:
        results_path = os.path.join(export_dir, f"results_{date_label}.json")
        # 保留完整字段以便导出
        with open(results_path, "w", encoding="utf-8") as f:
            json.dump(results_records, f, ensure_ascii=False, indent=2)
        logger.info("[cleanup] 已导出 %d 条 results → %s", len(results_records), results_path)

    if pending_records:
        pending_path = os.path.join(export_dir, f"pending_{date_label}.json")
        with open(pending_path, "w", encoding="utf-8") as f:
            json.dump(pending_records, f, ensure_ascii=False, indent=2)
        logger.info("[cleanup] 已导出 %d 条 pending → %s", len(pending_records), pending_path)

    # ── 4. 软删除 results ──
    results_cleaned = 0
    if results_records:
        ids = [r["id"] for r in results_records if r.get("id")]
        if ids:
            store.delete_results(ids, soft=True)
            results_cleaned = len(ids)
            logger.info("[cleanup] 已软删除 %d 条 results", results_cleaned)

    # ── 5. 标记 pending 为 dismissed ──
    pending_cleaned = 0
    if pending_records:
        pids = [p["id"] for p in pending_records if p.get("id")]
        if pids:
            pending_cleaned = store.dismiss_pending(pids)
            logger.info("[cleanup] 已关闭 %d 条 pending", pending_cleaned)

    # ── 6. 写一份汇总清单 ──
    summary = {
        "cleanup_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "retention_days": retention_days,
        "default_cutoff": default_cutoff,
        "eliminated_cutoff": eliminated_cutoff,
        "results_cleaned": results_cleaned,
        "pending_cleaned": pending_cleaned,
        "results_candidates": [
            {"id": r["id"], "name": r.get("candidate_name", ""), "position": r.get("matched_position", ""),
             "timestamp": r.get("timestamp", "")}
            for r in results_records
        ],
        "pending_candidates": [
            {"id": p["id"], "name": p.get("candidate_name", ""), "file": p.get("resume_file", ""),
             "timestamp": p.get("timestamp", "")}
            for p in pending_records
        ],
    }
    summary_path = os.path.join(export_dir, f"summary_{date_label}.json")
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    # ── 7. v5: 清理30天前的 config_history ──
    try:
        history_cutoff = (now - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        cur = store.conn.execute(
            "DELETE FROM config_history WHERE created_at < ?", (history_cutoff,)
        )
        store.conn.commit()
        if cur.rowcount > 0:
            logger.info("[cleanup] 已清理 %d 条旧版本历史", cur.rowcount)
    except Exception as e:
        logger.debug("[cleanup] 清理版本历史失败: %s", e)

    return {
        "results_cleaned": results_cleaned,
        "pending_cleaned": pending_cleaned,
        "export_dir": export_dir,
    }
