"""SQLite DataStore —— 替代 JSON 文件存储，支持迁移和事务。"""

import json
import logging
import os
import sqlite3
import threading
import uuid
import time
from datetime import datetime

logger = logging.getLogger(__name__)


# ── schema 语句解析辅助（_init_db 增量建表用） ──
import re as _re


def _schema_statements() -> list:
    """把 SCHEMA 文本按 ';' 拆成可独立执行的语句（跳过注释与空行）。"""
    out, buf = [], []
    for line in SCHEMA.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        buf.append(line)
        if stripped.endswith(";"):
            out.append("\n".join(buf))
            buf = []
    if "".join(buf).strip():
        out.append("\n".join(buf))
    return out


def _stmt_object_name(stmt: str) -> str | None:
    """提取 CREATE TABLE/INDEX/VIEW 语句的对象名。"""
    m = _re.match(
        r"CREATE\s+(?:VIRTUAL\s+)?(?:TABLE|INDEX)\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"']?(\w+)",
        stmt, _re.IGNORECASE,
    )
    return m.group(1) if m else None

SCHEMA = """
CREATE TABLE IF NOT EXISTS results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_name TEXT NOT NULL DEFAULT '未知',
    matched_position TEXT DEFAULT '',
    match_score INTEGER DEFAULT 0,
    verdict TEXT DEFAULT '',
    original_verdict TEXT DEFAULT '',
    original_score INTEGER DEFAULT 0,
    summary TEXT DEFAULT '',
    score_reasoning TEXT DEFAULT '',
    dimensions TEXT DEFAULT '{}',
    highlights TEXT DEFAULT '[]',
    risks TEXT DEFAULT '[]',
    interview_suggestions TEXT DEFAULT '[]',
    matching_evidence TEXT DEFAULT '[]',
    gaps TEXT DEFAULT '[]',
    tailored_questions TEXT DEFAULT '[]',
    match_method TEXT DEFAULT 'unknown',
    match_confidence TEXT DEFAULT 'unknown',
    notes TEXT DEFAULT '',
    pipeline_status TEXT DEFAULT '待筛选',
    resume_file TEXT DEFAULT '',
    report_file TEXT DEFAULT '',
    portfolio_links TEXT DEFAULT '[]',
    timestamp TEXT DEFAULT '',
    deleted INTEGER DEFAULT 0,
    deleted_at TEXT DEFAULT '',
    rank_order INTEGER DEFAULT 0,
    last_status_change TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS pending (
    id TEXT PRIMARY KEY,
    candidate_name TEXT DEFAULT '未知',
    matched_position TEXT DEFAULT '未识别',
    llm_guess TEXT DEFAULT '',
    llm_reason TEXT DEFAULT '',
    match_confidence TEXT DEFAULT 'low',
    resume_file TEXT DEFAULT '',
    filepath TEXT DEFAULT '',
    fhash TEXT DEFAULT '',
    timestamp TEXT DEFAULT '',
    status TEXT DEFAULT 'pending',
    assigned_position TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS processed (
    fhash TEXT PRIMARY KEY,
    timestamp TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS status_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    result_id INTEGER NOT NULL,
    from_status TEXT DEFAULT '',
    to_status TEXT DEFAULT '',
    time TEXT DEFAULT '',
    FOREIGN KEY (result_id) REFERENCES results(id)
);

CREATE TABLE IF NOT EXISTS reference_approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position TEXT NOT NULL,
    candidate_name TEXT NOT NULL,
    result_id INTEGER,
    approved INTEGER DEFAULT 0,
    approved_at TEXT DEFAULT '',
    FOREIGN KEY (result_id) REFERENCES results(id)
);

CREATE INDEX IF NOT EXISTS idx_results_position ON results(matched_position);
CREATE INDEX IF NOT EXISTS idx_results_deleted ON results(deleted);
CREATE INDEX IF NOT EXISTS idx_results_pipeline ON results(pipeline_status);
CREATE INDEX IF NOT EXISTS idx_pending_status ON pending(status);
CREATE INDEX IF NOT EXISTS idx_processed_fhash ON processed(fhash);
-- 复合索引提升常用查询性能
CREATE INDEX IF NOT EXISTS idx_results_deleted_sort ON results(deleted, rank_order DESC, match_score DESC);
CREATE INDEX IF NOT EXISTS idx_results_position_active ON results(matched_position, deleted, match_score DESC);
CREATE INDEX IF NOT EXISTS idx_results_deleted_ts ON results(deleted, timestamp);
CREATE INDEX IF NOT EXISTS idx_results_resume_file ON results(resume_file);

-- UPGRADE-P2+: 标杆特征标签
CREATE TABLE IF NOT EXISTS reference_features (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position TEXT NOT NULL,
    candidate_name TEXT NOT NULL,
    result_id INTEGER,
    features TEXT DEFAULT '{}',
    extracted_at TEXT DEFAULT '',
    FOREIGN KEY (result_id) REFERENCES results(id)
);
CREATE INDEX IF NOT EXISTS idx_ref_features_position ON reference_features(position);

-- UPGRADE-P3+: 重复检测队列（hash 命中时入队，用户决定重新评估或忽略）
CREATE TABLE IF NOT EXISTS duplicate_queue (
    fhash TEXT PRIMARY KEY,
    resume_file TEXT DEFAULT '',
    filepath TEXT DEFAULT '',
    original_result_id INTEGER,
    original_candidate_name TEXT DEFAULT '',
    original_position TEXT DEFAULT '',
    original_score INTEGER DEFAULT 0,
    original_verdict TEXT DEFAULT '',
    status TEXT DEFAULT 'pending_review',
    timestamp TEXT DEFAULT '',
    FOREIGN KEY (original_result_id) REFERENCES results(id)
);
CREATE INDEX IF NOT EXISTS idx_dup_queue_status ON duplicate_queue(status);

-- v2: 结构化维度评分表（可查询、可统计）
CREATE TABLE IF NOT EXISTS evaluation_dimensions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    result_id INTEGER NOT NULL,
    dimension_name TEXT NOT NULL,
    dimension_key TEXT NOT NULL,
    weight REAL DEFAULT 0,
    score INTEGER DEFAULT 0,
    comment TEXT DEFAULT '',
    FOREIGN KEY (result_id) REFERENCES results(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_eval_dim_result ON evaluation_dimensions(result_id);
CREATE INDEX IF NOT EXISTS idx_eval_dim_key_score ON evaluation_dimensions(dimension_key, score);

-- v4: 面试反馈结构化存储
CREATE TABLE IF NOT EXISTS interview_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    result_id INTEGER NOT NULL,
    accuracy TEXT NOT NULL DEFAULT '',        -- "accurate" | "overrated" | "underrated"
    interview_score INTEGER DEFAULT 0,        -- 1-10 面试评分
    note TEXT DEFAULT '',                      -- 面试备注
    system_score INTEGER DEFAULT 0,           -- 系统原始评分（冗余，方便对比）
    system_verdict TEXT DEFAULT '',            -- 系统原始结论
    dimensions_feedback TEXT DEFAULT '{}',     -- 各维度面试实际表现 vs 系统评估对比
    recorded_at TEXT DEFAULT '',
    FOREIGN KEY (result_id) REFERENCES results(id)
);
CREATE INDEX IF NOT EXISTS idx_interview_feedback_result ON interview_feedback(result_id);
CREATE INDEX IF NOT EXISTS idx_interview_feedback_accuracy ON interview_feedback(accuracy);

-- v4: 简历原文存储（用于全文搜索）
CREATE TABLE IF NOT EXISTS resume_texts (
    result_id INTEGER PRIMARY KEY,
    resume_text TEXT DEFAULT '',
    parsed_at TEXT DEFAULT '',
    FOREIGN KEY (result_id) REFERENCES results(id)
);

-- v4: FTS5 全文搜索虚拟表
CREATE VIRTUAL TABLE IF NOT EXISTS resume_texts_fts USING fts5(
    candidate_name,
    resume_text,
    content='resume_texts',
    content_rowid='result_id'
);

-- v4: 任务队列表（断点续传）
CREATE TABLE IF NOT EXISTS task_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filepath TEXT NOT NULL,
    filename TEXT DEFAULT '',
    fhash TEXT DEFAULT '',
    status TEXT DEFAULT 'queued',         -- queued | processing | completed | failed
    retry_count INTEGER DEFAULT 0,
    max_retries INTEGER DEFAULT 3,
    error_message TEXT DEFAULT '',
    created_at TEXT DEFAULT '',
    started_at TEXT DEFAULT '',
    completed_at TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_task_queue_status ON task_queue(status);

-- v4: 评估回归测试结果
CREATE TABLE IF NOT EXISTS eval_regression (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    result_id INTEGER NOT NULL,
    position_name TEXT NOT NULL,
    old_score INTEGER DEFAULT 0,
    new_score INTEGER DEFAULT 0,
    old_verdict TEXT DEFAULT '',
    new_verdict TEXT DEFAULT '',
    old_dimensions TEXT DEFAULT '{}',
    new_dimensions TEXT DEFAULT '{}',
    version_tag TEXT DEFAULT '',          -- 标识本次标准变更
    run_at TEXT DEFAULT '',
    FOREIGN KEY (result_id) REFERENCES results(id)
);
CREATE INDEX IF NOT EXISTS idx_eval_regression_version ON eval_regression(version_tag);

-- v4: Claude 交叉校验结果
CREATE TABLE IF NOT EXISTS cross_validation (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    result_id INTEGER NOT NULL,
    primary_model TEXT DEFAULT 'deepseek',
    primary_score INTEGER DEFAULT 0,
    primary_verdict TEXT DEFAULT '',
    claude_score INTEGER DEFAULT 0,
    claude_verdict TEXT DEFAULT '',
    score_diff INTEGER DEFAULT 0,          -- claude - primary
    agreement TEXT DEFAULT '',             -- "agree" | "minor_diff" | "major_diff"
    claude_reasoning TEXT DEFAULT '',
    claude_dimensions TEXT DEFAULT '{}',
    validated_at TEXT DEFAULT '',
    FOREIGN KEY (result_id) REFERENCES results(id)
);
CREATE INDEX IF NOT EXISTS idx_cross_validation_result ON cross_validation(result_id);
CREATE INDEX IF NOT EXISTS idx_cross_validation_agreement ON cross_validation(agreement);

-- v5: 岗位配置版本历史
CREATE TABLE IF NOT EXISTS config_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_name TEXT NOT NULL,
    config_snapshot TEXT NOT NULL,        -- JSON: 完整岗位配置快照
    changed_fields TEXT DEFAULT '[]',     -- JSON: ["dimensions", "must_have"]
    change_summary TEXT DEFAULT '',       -- 人类可读摘要
    change_source TEXT DEFAULT 'manual',  -- manual | ai_assist | restore
    created_at TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_config_history_position ON config_history(position_name, created_at DESC);
"""


class DataStore:
    """线程安全的 SQLite 数据存储。"""

    def __init__(self, db_path: str = "data/recruitment.db"):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.db_path = db_path
        self._local = threading.local()
        self._init_db()

    @property
    def conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(self.db_path, timeout=15)
            conn.row_factory = sqlite3.Row
            # 多线程并发写安全：忙等待 + WAL，避免 "database is locked"
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return self._local.conn

    def _init_db(self):
        conn = sqlite3.connect(self.db_path, timeout=15)
        # 性能：老库跳过整段 schema 重建，只补缺失的表/索引
        try:
            existing = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'index')"
            )}
        except sqlite3.OperationalError:
            existing = set()
        if not existing:
            conn.executescript(SCHEMA)
        else:
            for stmt in _schema_statements():
                name = _stmt_object_name(stmt)
                if name and name not in existing:
                    try:
                        conn.execute(stmt)
                    except sqlite3.OperationalError:
                        pass  # 并发初始化或方言差异，忽略
        # v2: 尝试添加新列（如果已存在则忽略错误）
        try:
            conn.execute("ALTER TABLE results ADD COLUMN eval_metadata TEXT DEFAULT '{}'")
        except sqlite3.OperationalError:
            pass  # 列已存在
        # v5: 三段式字段
        for col in ["matching_evidence", "gaps", "tailored_questions"]:
            try:
                conn.execute(f"ALTER TABLE results ADD COLUMN {col} TEXT DEFAULT '[]'")
            except sqlite3.OperationalError:
                pass
        conn.commit()
        conn.close()

    def migrate_from_json(self, project_dir: str):
        """从旧 JSON 文件迁移数据到 SQLite。"""
        json_files = {
            "results": os.path.join(project_dir, "results.json"),
            "pending": os.path.join(project_dir, "pending.json"),
            "processed": os.path.join(project_dir, "processed.json"),
        }
        for table, fpath in json_files.items():
            if not os.path.exists(fpath):
                continue
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, IOError):
                continue

            if table == "processed":
                hashes = data.get("hashes", []) if isinstance(data, dict) else data
                for h in hashes:
                    self.add_processed(h)
                logger.info("迁移 processed: %d 条", len(hashes))
            elif table == "pending":
                count = 0
                for item in (data if isinstance(data, list) else []):
                    if not self.get_pending_by_id(item.get("id", "")):
                        self.add_pending(item)
                        count += 1
                logger.info("迁移 pending: %d 条", count)
            elif table == "results":
                count = 0
                existing = {r["resume_file"] for r in self.get_all_results(include_deleted=True) if r.get("resume_file")}
                for item in (data if isinstance(data, list) else []):
                    if item.get("resume_file", "") not in existing:
                        self.add_result(item)
                        count += 1
                logger.info("迁移 results: %d 条", count)

    # ── Results CRUD ──

    def get_all_results(self, include_deleted: bool = False) -> list:
        sql = "SELECT * FROM results"
        if not include_deleted:
            sql += " WHERE deleted = 0"
        sql += " ORDER BY match_score DESC"
        rows = self.conn.execute(sql).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get_results_by_position(self, position: str, include_deleted: bool = False) -> list:
        if include_deleted:
            rows = self.conn.execute(
                "SELECT * FROM results WHERE matched_position = ? ORDER BY deleted ASC, match_score DESC",
                (position,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM results WHERE matched_position = ? AND deleted = 0 ORDER BY match_score DESC",
                (position,),
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get_result_by_id(self, result_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM results WHERE id = ?", (result_id,)).fetchone()
        return self._row_to_dict(row) if row else None

    def add_result(self, data: dict) -> int:
        cols = [
            "candidate_name", "matched_position", "match_score", "verdict",
            "original_verdict", "original_score", "summary", "score_reasoning",
            "dimensions", "highlights", "risks", "interview_suggestions",
            "matching_evidence", "gaps", "tailored_questions",
            "match_method", "match_confidence", "notes", "pipeline_status",
            "resume_file", "report_file", "portfolio_links", "timestamp",
            "eval_metadata",
        ]
        values = {}
        for c in cols:
            val = data.get(c)
            if val is not None:
                if isinstance(val, (list, dict)):
                    val = json.dumps(val, ensure_ascii=False)
                values[c] = val

        names = ", ".join(values.keys())
        placeholders = ", ".join("?" for _ in values)
        params = list(values.values())

        cur = self.conn.execute(
            f"INSERT INTO results ({names}) VALUES ({placeholders})", params
        )
        self.conn.commit()
        return cur.lastrowid

    def update_result(self, result_id: int, updates: dict):
        if not updates:
            return
        sets = []
        params = []
        for k, v in updates.items():
            if isinstance(v, (list, dict)):
                v = json.dumps(v, ensure_ascii=False)
            sets.append(f"{k} = ?")
            params.append(v)
        params.append(result_id)
        sql = f"UPDATE results SET {', '.join(sets)} WHERE id = ?"
        self.conn.execute(sql, params)
        self.conn.commit()
        # v5: 候选人姓名变更时同步更新 FTS 索引
        if "candidate_name" in updates:
            try:
                row = self.conn.execute(
                    "SELECT resume_text FROM resume_texts WHERE result_id = ?", (result_id,)
                ).fetchone()
                if row and row["resume_text"]:
                    new_name = updates["candidate_name"]
                    self.conn.execute(
                        "DELETE FROM resume_texts_fts WHERE rowid = ?", (result_id,)
                    )
                    self.conn.execute(
                        "INSERT INTO resume_texts_fts (rowid, candidate_name, resume_text) VALUES (?, ?, ?)",
                        (result_id, new_name, row["resume_text"]),
                    )
                    self.conn.commit()
            except Exception:
                pass

    def delete_results(self, ids: list[int], soft: bool = True):
        if soft:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            placeholders = ",".join("?" for _ in ids)
            self.conn.execute(
                f"UPDATE results SET deleted = 1, deleted_at = ? WHERE id IN ({placeholders})",
                [now] + ids,
            )
        else:
            placeholders = ",".join("?" for _ in ids)
            self.conn.execute(f"DELETE FROM results WHERE id IN ({placeholders})", ids)
        self.conn.commit()

    def undo_delete(self, result_id: int) -> bool:
        row = self.conn.execute("SELECT id FROM results WHERE id = ? AND deleted = 1", (result_id,)).fetchone()
        if not row:
            return False
        self.conn.execute("UPDATE results SET deleted = 0, deleted_at = '' WHERE id = ?", (result_id,))
        self.conn.commit()
        return True

    def get_deleted_results(self) -> list:
        rows = self.conn.execute(
            "SELECT id, candidate_name, matched_position, deleted_at FROM results WHERE deleted = 1 ORDER BY deleted_at DESC LIMIT 20"
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get_result_by_filename(self, filename: str) -> dict | None:
        """根据文件名查找最近一条评估结果（含已删除的）。"""
        row = self.conn.execute(
            "SELECT * FROM results WHERE resume_file = ? ORDER BY id DESC LIMIT 1",
            (filename,)
        ).fetchone()
        return self._row_to_dict(row) if row else None

    def update_rank_orders(self, rankings: list[dict]):
        """批量更新排序: [{id: 1, rank_order: 10}, ...]"""
        for item in rankings:
            self.conn.execute(
                "UPDATE results SET rank_order = ? WHERE id = ?",
                (item.get("rank_order", 0), item["id"]),
            )
        self.conn.commit()

    # ── Pending CRUD ──

    def get_all_pending(self) -> list:
        rows = self.conn.execute(
            "SELECT * FROM pending WHERE status != 'dismissed' ORDER BY timestamp DESC"
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get_pending_by_id(self, pid: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM pending WHERE id = ?", (pid,)).fetchone()
        return self._row_to_dict(row) if row else None

    def add_pending(self, data: dict):
        pid = data.get("id", uuid.uuid4().hex[:8])
        self.conn.execute(
            """INSERT OR REPLACE INTO pending
               (id, candidate_name, matched_position, llm_guess, llm_reason,
                match_confidence, resume_file, filepath, fhash, timestamp, status, assigned_position)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                pid,
                data.get("candidate_name", "未知"),
                data.get("matched_position", "未识别"),
                data.get("llm_guess", ""),
                data.get("llm_reason", ""),
                data.get("match_confidence", "low"),
                data.get("resume_file", ""),
                data.get("filepath", ""),
                data.get("fhash", ""),
                data.get("timestamp", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                data.get("status", "pending"),
                data.get("assigned_position", ""),
            ),
        )
        self.conn.commit()

    def update_pending(self, pid: str, updates: dict):
        if not updates:
            return
        sets = []
        params = []
        for k, v in updates.items():
            sets.append(f"{k} = ?")
            params.append(v)
        params.append(pid)
        self.conn.execute(f"UPDATE pending SET {', '.join(sets)} WHERE id = ?", params)
        self.conn.commit()

    def dismiss_pending(self, ids: list[str]) -> int:
        placeholders = ",".join("?" for _ in ids)
        cur = self.conn.execute(
            f"UPDATE pending SET status = 'dismissed' WHERE id IN ({placeholders}) AND status = 'pending'",
            ids,
        )
        self.conn.commit()
        return cur.rowcount

    def pending_exists_by_fhash(self, fhash: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM pending WHERE fhash = ? AND status = 'pending'", (fhash,)
        ).fetchone()
        return row is not None

    def is_resume_processed(self, filename: str) -> bool:
        """检查文件名是否已在 results 表中（防重复评估）。
        支持 BOSS 直聘文件名模糊匹配：去除【...】前缀和薪资信息后比对。
        """
        # 1. 精确匹配
        row = self.conn.execute(
            "SELECT 1 FROM results WHERE resume_file = ? LIMIT 1", (filename,)
        ).fetchone()
        if row:
            return True

        # 2. 标准化匹配：提取候选人名+年数部分（BOSS直聘文件名中】后面的部分）
        key = self._normalize_resume_filename(filename)
        if not key:
            return False

        rows = self.conn.execute(
            "SELECT resume_file FROM results"
        ).fetchall()
        for (rf,) in rows:
            if self._normalize_resume_filename(rf) == key:
                return True
        return False

    @staticmethod
    def _normalize_resume_filename(filename: str) -> str:
        """从文件名中提取候选人标识部分，忽略 BOSS 直聘的前缀格式变化。"""
        import re
        # 去扩展名
        name = filename.rsplit(".", 1)[0] if "." in filename else filename
        # 如果有 】分隔符，取】后面的部分（这是候选人名+年数的稳定部分）
        if "】" in name:
            name = name.split("】", 1)[1]
        # 去除首尾空白
        name = name.strip()
        # 标准化空白
        name = re.sub(r"\s+", "", name)
        return name

    # ── Processed CRUD ──

    def is_processed(self, fhash: str) -> bool:
        row = self.conn.execute("SELECT 1 FROM processed WHERE fhash = ?", (fhash,)).fetchone()
        return row is not None

    def add_processed(self, fhash: str):
        self.conn.execute(
            "INSERT OR IGNORE INTO processed (fhash, timestamp) VALUES (?, ?)",
            (fhash, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        self.conn.commit()

    def try_add_processed(self, fhash: str) -> bool:
        """原子操作：尝试将 fhash 标记为已处理。返回 True 表示首次插入成功。"""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO processed (fhash, timestamp) VALUES (?, ?)",
            (fhash, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def remove_processed(self, fhash: str):
        """移除 hash 记录，允许重新评估。"""
        self.conn.execute("DELETE FROM processed WHERE fhash = ?", (fhash,))
        self.conn.commit()

    # ── Status History ──

    def add_status_history(self, result_id: int, from_status: str, to_status: str):
        self.conn.execute(
            "INSERT INTO status_history (result_id, from_status, to_status, time) VALUES (?,?,?,?)",
            (result_id, from_status, to_status, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        self.conn.commit()

    def get_status_history(self, result_id: int) -> list:
        rows = self.conn.execute(
            "SELECT * FROM status_history WHERE result_id = ? ORDER BY id ASC", (result_id,)
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    # ── Reference Approvals ──

    def add_reference_approval(self, position: str, candidate_name: str, result_id: int):
        self.conn.execute(
            "INSERT INTO reference_approvals (position, candidate_name, result_id, approved) VALUES (?,?,?,0)",
            (position, candidate_name, result_id),
        )
        self.conn.commit()

    def approve_reference(self, approval_id: int):
        self.conn.execute(
            "UPDATE reference_approvals SET approved = 1, approved_at = ? WHERE id = ?",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), approval_id),
        )
        self.conn.commit()

    def get_pending_approvals(self) -> list:
        rows = self.conn.execute(
            "SELECT * FROM reference_approvals WHERE approved = 0 ORDER BY id ASC"
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    # ── Stats ──

    def get_stats(self, position: str | None = None, include_deleted: bool = True) -> dict:
        """获取统计信息。

        v3: 默认 include_deleted=True，避免幸存者偏差。
        软删除不等于数据不存在，统计应反映全貌。
        """
        # 基础查询条件
        where = "WHERE deleted = 0" if not include_deleted else ""
        params: tuple = ()

        if position:
            prefix = " AND " if where else " WHERE "
            where += f"{prefix}matched_position = ?"
            params = (position,)

        # 全量统计
        row = self.conn.execute(
            f"SELECT COUNT(*), AVG(match_score) FROM results {where}", params
        ).fetchone()
        total_all = row[0] or 0
        avg_all = round(row[1], 1) if row[1] else 0

        # 裁决分布
        verdict_sql = f"SELECT verdict, COUNT(*) FROM results {where} GROUP BY verdict"
        verdicts = {}
        for r in self.conn.execute(verdict_sql, params).fetchall():
            verdicts[r[0]] = r[1]

        # 活跃数（始终计算）
        active_where = "WHERE deleted = 0"
        active_params: tuple = ()
        if position:
            active_where += " AND matched_position = ?"
            active_params = (position,)
        active_row = self.conn.execute(
            f"SELECT COUNT(*) FROM results {active_where}", active_params
        ).fetchone()
        active_count = active_row[0] if active_row else 0
        deleted_count = total_all - active_count

        return {
            "total": total_all,
            "active": active_count,
            "deleted": deleted_count,
            "avg_score": avg_all,
            "verdicts": verdicts,
        }

    # ── Quality monitoring (UPGRADE-P1+) ──

    def get_eval_trends(self, days: int = 7, include_deleted: bool = True) -> list:
        """每日评估趋势：日期、数量、均分、推荐率。

        v3: 默认 include_deleted=True，软删除的也是真实评估数据。
        """
        deleted_filter = "" if include_deleted else "AND deleted = 0"
        sql = f"""
            SELECT date(timestamp) as day, COUNT(*) as cnt,
                   ROUND(AVG(match_score), 1) as avg_score,
                   ROUND(AVG(CASE WHEN verdict IN ('强烈推荐','推荐') THEN 1.0 ELSE 0.0 END), 2) as rec_rate
            FROM results
            WHERE timestamp != '' {deleted_filter}
            AND timestamp >= date('now', '-' || ? || ' days')
            GROUP BY day ORDER BY day ASC
        """
        rows = self.conn.execute(sql, (days,)).fetchall()
        return [{"day": r["day"], "cnt": r["cnt"], "avg_score": r["avg_score"], "rec_rate": r["rec_rate"]} for r in rows]

    def get_eval_reliability(self, include_deleted: bool = True) -> list:
        """评估可靠性：已终态候选人的评估准确率。

        v3: 默认 include_deleted=True。
        """
        deleted_filter = "" if include_deleted else "AND deleted = 0"
        sql = f"""
            SELECT matched_position, COUNT(*) as total,
                   ROUND(AVG(match_score), 1) as avg_score,
                   ROUND(AVG(CASE WHEN verdict IN ('强烈推荐','推荐') THEN 1.0 ELSE 0.0 END), 2) as rec_rate
            FROM results
            WHERE pipeline_status IN ('已通过', '已淘汰') {deleted_filter}
            GROUP BY matched_position
            ORDER BY total DESC
        """
        rows = self.conn.execute(sql).fetchall()
        return [{"position": r["matched_position"], "total": r["total"],
                 "avg_score": r["avg_score"], "rec_rate": r["rec_rate"]} for r in rows]

    def get_last_n_scores(self, n: int = 30, include_deleted: bool = True) -> list:
        """获取最近 N 条记录的匹配度列表（用于 drift 计算）。

        v3: 默认 include_deleted=True。
        """
        deleted_filter = "" if include_deleted else "AND deleted = 0"
        rows = self.conn.execute(
            f"SELECT match_score, timestamp FROM results WHERE timestamp != '' {deleted_filter} ORDER BY timestamp DESC LIMIT ?",
            (n,),
        ).fetchall()
        return [{"score": r["match_score"], "timestamp": r["timestamp"]} for r in rows]

    # ── Reference features (UPGRADE-P2+) ──

    def add_reference_features(self, position: str, candidate_name: str, result_id: int, features: dict):
        self.conn.execute(
            "INSERT INTO reference_features (position, candidate_name, result_id, features, extracted_at) VALUES (?,?,?,?,?)",
            (position, candidate_name, result_id, json.dumps(features, ensure_ascii=False),
             datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        self.conn.commit()

    def get_reference_features(self, position: str | None = None) -> list:
        if position:
            rows = self.conn.execute(
                "SELECT * FROM reference_features WHERE position = ? ORDER BY id ASC", (position,)
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM reference_features ORDER BY id ASC"
            ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            try:
                d["features"] = json.loads(d["features"]) if isinstance(d["features"], str) else d["features"]
            except (json.JSONDecodeError, TypeError):
                pass
            result.append(d)
        return result

    # ── 重复检测队列 ──

    def add_duplicate(self, fhash: str, resume_file: str, filepath: str, original_info: dict = None):
        """将 hash 重复的文件加入检测队列。"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        orig = original_info or {}
        self.conn.execute(
            """INSERT OR REPLACE INTO duplicate_queue
               (fhash, resume_file, filepath, original_result_id, original_candidate_name,
                original_position, original_score, original_verdict, status, timestamp)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending_review', ?)""",
            (fhash, resume_file, filepath,
             orig.get("id"), orig.get("candidate_name", ""),
             orig.get("matched_position", ""), orig.get("match_score", 0),
             orig.get("verdict", ""), now)
        )
        self.conn.commit()

    def get_duplicates(self, status_filter: str = None) -> list:
        """获取重复检测队列。"""
        if status_filter:
            rows = self.conn.execute(
                "SELECT * FROM duplicate_queue WHERE status = ? ORDER BY timestamp DESC",
                (status_filter,)
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM duplicate_queue ORDER BY timestamp DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_duplicate_count(self) -> int:
        """获取待处理的重复数量。"""
        row = self.conn.execute(
            "SELECT COUNT(*) FROM duplicate_queue WHERE status = 'pending_review'"
        ).fetchone()
        return row[0] if row else 0

    def update_duplicate_status(self, fhash: str, status: str):
        """更新重复记录状态（reevaluated / ignored）。"""
        self.conn.execute(
            "UPDATE duplicate_queue SET status = ? WHERE fhash = ?",
            (status, fhash)
        )
        self.conn.commit()

    def remove_duplicate(self, fhash: str):
        """从队列中移除重复记录。"""
        self.conn.execute("DELETE FROM duplicate_queue WHERE fhash = ?", (fhash,))
        self.conn.commit()

    def clear_duplicate_queue(self):
        """清空已处理的重复队列（忽略的条目）。"""
        self.conn.execute("DELETE FROM duplicate_queue WHERE status = 'ignored'")
        self.conn.commit()

    # ── v2: 结构化维度评分 ──

    def save_dimension_scores(self, result_id: int, dimensions: dict):
        """将评估维度分数写入结构化表。"""
        # 先清除旧数据
        self.conn.execute("DELETE FROM evaluation_dimensions WHERE result_id = ?", (result_id,))
        for key, dim in dimensions.items():
            if isinstance(dim, dict):
                self.conn.execute(
                    "INSERT INTO evaluation_dimensions (result_id, dimension_name, dimension_key, weight, score, comment) VALUES (?,?,?,?,?,?)",
                    (result_id, dim.get("name", key), key,
                     dim.get("weight", 0), dim.get("score", 0), dim.get("comment", "")),
                )
        self.conn.commit()

    def get_dimension_scores(self, result_id: int) -> list[dict]:
        """获取某条评估的维度分数。"""
        rows = self.conn.execute(
            "SELECT * FROM evaluation_dimensions WHERE result_id = ? ORDER BY id", (result_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_dimension_stats(self, dimension_key: str, position: str | None = None) -> dict:
        """获取某个维度在所有候选人中的统计。"""
        if position:
            rows = self.conn.execute(
                """SELECT AVG(d.score), MIN(d.score), MAX(d.score), COUNT(*)
                   FROM evaluation_dimensions d
                   JOIN results r ON d.result_id = r.id
                   WHERE d.dimension_key = ? AND r.deleted = 0 AND r.matched_position = ?""",
                (dimension_key, position),
            ).fetchone()
        else:
            rows = self.conn.execute(
                """SELECT AVG(d.score), MIN(d.score), MAX(d.score), COUNT(*)
                   FROM evaluation_dimensions d
                   JOIN results r ON d.result_id = r.id
                   WHERE d.dimension_key = ? AND r.deleted = 0""",
                (dimension_key,),
            ).fetchone()
        if rows:
            return {
                "dimension_key": dimension_key,
                "avg_score": round(rows[0], 1) if rows[0] else 0,
                "min_score": rows[1] or 0,
                "max_score": rows[2] or 0,
                "count": rows[3] or 0,
            }
        return {}

    # ── Helpers ──

    @staticmethod
    def _row_to_dict(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        d = dict(row)
        for key in ("dimensions", "highlights", "risks", "interview_suggestions",
                    "matching_evidence", "gaps", "tailored_questions",
                    "portfolio_links", "eval_metadata"):
            if key in d and isinstance(d[key], str):
                try:
                    d[key] = json.loads(d[key])
                except (json.JSONDecodeError, TypeError):
                    pass
        return d

    # ── 连接健康检查 ──

    def check_connection(self) -> dict:
        """检查数据库连接和状态。"""
        result = {"ok": True, "db_path": self.db_path, "issues": []}
        try:
            row = self.conn.execute("SELECT 1").fetchone()
            if not row:
                result["ok"] = False
                result["issues"].append("连接返回空结果")
        except Exception as e:
            result["ok"] = False
            result["issues"].append(f"数据库连接失败: {e}")

        # 检查 WAL 模式
        try:
            mode = self.conn.execute("PRAGMA journal_mode").fetchone()
            result["journal_mode"] = mode[0] if mode else "unknown"
        except Exception:
            result["journal_mode"] = "unknown"

        # 获取数据库文件大小
        try:
            import os as _os
            if _os.path.exists(self.db_path):
                result["db_size_mb"] = round(_os.path.getsize(self.db_path) / (1024 * 1024), 1)
        except Exception:
            pass

        return result

    def reconnect(self):
        """强制重连数据库。"""
        if hasattr(self._local, "conn") and self._local.conn:
            try:
                self._local.conn.close()
            except Exception:
                pass
            self._local.conn = None
        # 下次访问 conn 属性时自动重连
        _ = self.conn
        logger.info("数据库已重连: %s", self.db_path)

    # ── v4: 面试反馈 ──

    def add_interview_feedback(self, result_id: int, feedback: dict) -> int:
        """记录面试反馈。"""
        cur = self.conn.execute(
            """INSERT INTO interview_feedback
               (result_id, accuracy, interview_score, note, system_score, system_verdict,
                dimensions_feedback, recorded_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                result_id,
                feedback.get("accuracy", ""),
                feedback.get("interview_score", 0),
                feedback.get("note", ""),
                feedback.get("system_score", 0),
                feedback.get("system_verdict", ""),
                json.dumps(feedback.get("dimensions_feedback", {}), ensure_ascii=False),
                feedback.get("recorded_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            ),
        )
        self.conn.commit()
        return cur.lastrowid

    def get_interview_feedback(self, result_id: int = None) -> list:
        """获取面试反馈。不指定 result_id 则返回全部。"""
        if result_id:
            rows = self.conn.execute(
                "SELECT * FROM interview_feedback WHERE result_id = ? ORDER BY id DESC",
                (result_id,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM interview_feedback ORDER BY id DESC"
            ).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            try:
                d["dimensions_feedback"] = json.loads(d["dimensions_feedback"]) if isinstance(d.get("dimensions_feedback"), str) else d.get("dimensions_feedback", {})
            except (json.JSONDecodeError, TypeError):
                d["dimensions_feedback"] = {}
            results.append(d)
        return results

    def get_feedback_accuracy_stats(self, position: str = None) -> dict:
        """获取评估准确率统计：overrated/accurate/underrated 分布。"""
        if position:
            rows = self.conn.execute(
                """SELECT f.accuracy, COUNT(*) as cnt, ROUND(AVG(f.system_score - f.interview_score * 10), 1) as avg_bias
                   FROM interview_feedback f
                   JOIN results r ON f.result_id = r.id
                   WHERE r.matched_position = ?
                   GROUP BY f.accuracy""",
                (position,),
            ).fetchall()
            total_row = self.conn.execute(
                """SELECT COUNT(*), ROUND(AVG(f.system_score - f.interview_score * 10), 1)
                   FROM interview_feedback f
                   JOIN results r ON f.result_id = r.id
                   WHERE r.matched_position = ?""",
                (position,),
            ).fetchone()
        else:
            rows = self.conn.execute(
                """SELECT f.accuracy, COUNT(*) as cnt, ROUND(AVG(f.system_score - f.interview_score * 10), 1) as avg_bias
                   FROM interview_feedback f
                   GROUP BY f.accuracy"""
            ).fetchall()
            total_row = self.conn.execute(
                "SELECT COUNT(*), ROUND(AVG(f.system_score - f.interview_score * 10), 1) FROM interview_feedback f"
            ).fetchone()

        stats = {"total": total_row[0] if total_row else 0,
                 "avg_bias": total_row[1] if total_row and total_row[1] else 0}
        for r in rows:
            stats[r["accuracy"]] = {"count": r["cnt"], "avg_bias": r["avg_bias"]}
        return stats

    def get_feedback_for_calibration(self, position: str = None, limit: int = 20) -> list:
        """获取面试反馈数据，用于 LLM 校准。返回系统评分 vs 面试实际评分的对比。"""
        if position:
            rows = self.conn.execute(
                """SELECT f.*, r.candidate_name, r.matched_position, r.resume_file
                   FROM interview_feedback f
                   JOIN results r ON f.result_id = r.id
                   WHERE r.matched_position = ?
                   ORDER BY f.id DESC LIMIT ?""",
                (position, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """SELECT f.*, r.candidate_name, r.matched_position, r.resume_file
                   FROM interview_feedback f
                   JOIN results r ON f.result_id = r.id
                   ORDER BY f.id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ── v4: 简历文本 + FTS ──

    def save_resume_text(self, result_id: int, text: str):
        """保存简历原文并更新 FTS 索引。"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # 写入主表
        self.conn.execute(
            "INSERT OR REPLACE INTO resume_texts (result_id, resume_text, parsed_at) VALUES (?, ?, ?)",
            (result_id, text, now),
        )
        # 获取候选人姓名
        row = self.conn.execute("SELECT candidate_name FROM results WHERE id = ?", (result_id,)).fetchone()
        name = row["candidate_name"] if row else "未知"
        # 删除旧 FTS 条目后重新插入（FTS content 表更新方式）
        self.conn.execute("DELETE FROM resume_texts_fts WHERE rowid = ?", (result_id,))
        self.conn.execute(
            "INSERT INTO resume_texts_fts (rowid, candidate_name, resume_text) VALUES (?, ?, ?)",
            (result_id, name, text),
        )
        self.conn.commit()

    def search_resumes(self, query: str, limit: int = 20) -> list:
        """FTS5 全文搜索简历。返回匹配的 result_id 和片段。"""
        try:
            # 使用 FTS5 highlight 获取匹配片段
            rows = self.conn.execute(
                """SELECT r.id, r.candidate_name, r.matched_position, r.match_score,
                          r.verdict, r.pipeline_status, r.timestamp,
                          snippet(resume_texts_fts, 2, '<mark>', '</mark>', '...', 40) as snippet
                   FROM resume_texts_fts f
                   JOIN results r ON f.rowid = r.id
                   WHERE resume_texts_fts MATCH ? AND r.deleted = 0
                   ORDER BY rank
                   LIMIT ?""",
                (query, limit),
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.exception("FTS 全文搜索失败 (query=%s): %s", query[:100], e)
            raise RuntimeError(f"全文搜索失败: {e}") from e

    def get_resume_text(self, result_id: int) -> str:
        """获取某条评估的简历原文。"""
        row = self.conn.execute(
            "SELECT resume_text FROM resume_texts WHERE result_id = ?", (result_id,)
        ).fetchone()
        return row["resume_text"] if row else ""

    # ── v4: 任务队列 ──

    def enqueue_task(self, filepath: str, filename: str = "", fhash: str = "") -> int:
        """将评估任务加入队列。"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cur = self.conn.execute(
            """INSERT INTO task_queue (filepath, filename, fhash, status, created_at)
               VALUES (?,?,?,'queued',?)""",
            (filepath, filename or os.path.basename(filepath), fhash, now),
        )
        self.conn.commit()
        return cur.lastrowid

    def claim_task(self) -> dict | None:
        """获取一个待处理任务并标记为 processing（防竞态）。"""
        row = self.conn.execute(
            "SELECT * FROM task_queue WHERE status = 'queued' ORDER BY id ASC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # 原子 CAS: 仅当状态仍为 queued 时才更新，避免多线程竞态
        cur = self.conn.execute(
            "UPDATE task_queue SET status = 'processing', started_at = ? WHERE id = ? AND status = 'queued'",
            (now, row["id"]),
        )
        self.conn.commit()
        if cur.rowcount == 0:
            return None  # 被另一个 worker 抢走了
        return dict(row)

    def complete_task(self, task_id: int):
        """标记任务完成。"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.conn.execute(
            "UPDATE task_queue SET status = 'completed', completed_at = ? WHERE id = ?",
            (now, task_id),
        )
        self.conn.commit()

    def fail_task(self, task_id: int, error: str, retry: bool = True):
        """标记任务失败，可选重试。"""
        row = self.conn.execute("SELECT retry_count, max_retries FROM task_queue WHERE id = ?", (task_id,)).fetchone()
        if not row:
            return
        retry_count = row["retry_count"] + 1
        if retry and retry_count <= row["max_retries"]:
            self.conn.execute(
                "UPDATE task_queue SET status = 'queued', retry_count = ?, error_message = ? WHERE id = ?",
                (retry_count, error, task_id),
            )
        else:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.conn.execute(
                "UPDATE task_queue SET status = 'failed', retry_count = ?, error_message = ?, completed_at = ? WHERE id = ?",
                (retry_count, error, now, task_id),
            )
        self.conn.commit()

    def get_pending_tasks(self) -> list:
        """获取所有待处理/失败的任务。"""
        rows = self.conn.execute(
            "SELECT * FROM task_queue WHERE status IN ('queued', 'failed') ORDER BY id ASC"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_task_stats(self) -> dict:
        """获取任务队列统计。"""
        rows = self.conn.execute(
            "SELECT status, COUNT(*) as cnt FROM task_queue GROUP BY status"
        ).fetchall()
        return {r["status"]: r["cnt"] for r in rows}

    def resume_pending_tasks(self) -> int:
        """将所有处于 processing 状态超过1小时的任务重置为 queued（处理崩溃恢复）。"""
        cur = self.conn.execute(
            """UPDATE task_queue SET status = 'queued'
               WHERE status = 'processing'
               AND started_at < datetime('now', '-1 hour')"""
        )
        self.conn.commit()
        return cur.rowcount  # v5: 使用本次UPDATE影响行数，而非total_changes

    # ── v4: 评估回归测试 ──

    def save_regression_result(self, data: dict) -> int:
        """保存回归测试结果。"""
        cur = self.conn.execute(
            """INSERT INTO eval_regression
               (result_id, position_name, old_score, new_score, old_verdict, new_verdict,
                old_dimensions, new_dimensions, version_tag, run_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                data["result_id"], data["position_name"],
                data["old_score"], data["new_score"],
                data.get("old_verdict", ""), data.get("new_verdict", ""),
                json.dumps(data.get("old_dimensions", {}), ensure_ascii=False),
                json.dumps(data.get("new_dimensions", {}), ensure_ascii=False),
                data.get("version_tag", ""),
                data.get("run_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            ),
        )
        self.conn.commit()
        return cur.lastrowid

    def get_regression_summary(self, version_tag: str = None) -> dict:
        """获取回归测试汇总。"""
        if version_tag:
            rows = self.conn.execute(
                """SELECT position_name, COUNT(*) as cnt,
                          ROUND(AVG(new_score - old_score), 1) as avg_score_change,
                          SUM(CASE WHEN new_verdict != old_verdict THEN 1 ELSE 0 END) as verdict_changes
                   FROM eval_regression
                   WHERE version_tag = ?
                   GROUP BY position_name""",
                (version_tag,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """SELECT version_tag, position_name, COUNT(*) as cnt,
                          ROUND(AVG(new_score - old_score), 1) as avg_score_change,
                          SUM(CASE WHEN new_verdict != old_verdict THEN 1 ELSE 0 END) as verdict_changes
                   FROM eval_regression
                   GROUP BY version_tag, position_name
                   ORDER BY version_tag DESC"""
            ).fetchall()
        return {
            "total_runs": sum(r["cnt"] for r in rows),
            "by_position": [dict(r) for r in rows],
        }

    # ── v4: 交叉校验 ──

    def save_cross_validation(self, data: dict) -> int:
        """保存 Claude 交叉校验结果。"""
        cur = self.conn.execute(
            """INSERT INTO cross_validation
               (result_id, primary_model, primary_score, primary_verdict,
                claude_score, claude_verdict, score_diff, agreement,
                claude_reasoning, claude_dimensions, validated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                data["result_id"], data.get("primary_model", "deepseek"),
                data["primary_score"], data.get("primary_verdict", ""),
                data["claude_score"], data.get("claude_verdict", ""),
                data.get("score_diff", data["claude_score"] - data["primary_score"]),
                data.get("agreement", ""),
                data.get("claude_reasoning", ""),
                json.dumps(data.get("claude_dimensions", {}), ensure_ascii=False),
                data.get("validated_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            ),
        )
        self.conn.commit()
        return cur.lastrowid

    def get_cross_validation(self, result_id: int = None) -> list:
        """获取交叉校验记录。"""
        if result_id:
            rows = self.conn.execute(
                "SELECT * FROM cross_validation WHERE result_id = ? ORDER BY id DESC",
                (result_id,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM cross_validation ORDER BY id DESC LIMIT 50"
            ).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            try:
                d["claude_dimensions"] = json.loads(d["claude_dimensions"]) if isinstance(d.get("claude_dimensions"), str) else d.get("claude_dimensions", {})
            except (json.JSONDecodeError, TypeError):
                d["claude_dimensions"] = {}
            results.append(d)
        return results

    def get_cross_validation_stats(self) -> dict:
        """获取交叉校验统计。"""
        total_row = self.conn.execute("SELECT COUNT(*), ROUND(AVG(ABS(score_diff)), 1), ROUND(AVG(score_diff), 1) FROM cross_validation").fetchone()
        agree_row = self.conn.execute(
            "SELECT agreement, COUNT(*) FROM cross_validation GROUP BY agreement"
        ).fetchall()
        return {
            "total": total_row[0] or 0,
            "avg_abs_diff": total_row[1] or 0,
            "avg_diff": total_row[2] or 0,
            "agreement": {r["agreement"]: r["COUNT(*)"] for r in agree_row},
        }

    # ── v5: 岗位配置版本历史 ──

    def save_config_history(self, position_name: str, config_snapshot: dict,
                            changed_fields: list = None, change_summary: str = "",
                            change_source: str = "manual") -> int:
        """保存配置快照到版本历史。"""
        import json as _json
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cur = self.conn.execute(
            """INSERT INTO config_history
               (position_name, config_snapshot, changed_fields, change_summary,
                change_source, created_at)
               VALUES (?,?,?,?,?,?)""",
            (position_name, _json.dumps(config_snapshot, ensure_ascii=False),
             _json.dumps(changed_fields or [], ensure_ascii=False),
             change_summary, change_source, now),
        )
        self.conn.commit()
        return cur.lastrowid

    def get_config_history(self, position_name: str, limit: int = 20) -> list:
        """获取岗位配置的版本历史列表。"""
        import json as _json
        rows = self.conn.execute(
            """SELECT id, position_name, changed_fields, change_summary,
                       change_source, created_at
               FROM config_history
               WHERE position_name = ?
               ORDER BY id DESC LIMIT ?""",
            (position_name, limit),
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            try:
                d["changed_fields"] = _json.loads(d["changed_fields"]) if isinstance(d.get("changed_fields"), str) else d.get("changed_fields", [])
            except (_json.JSONDecodeError, TypeError):
                d["changed_fields"] = []
            result.append(d)
        return result

    def get_config_version(self, version_id: int) -> dict | None:
        """获取某个版本的完整配置快照。"""
        import json as _json
        row = self.conn.execute(
            "SELECT * FROM config_history WHERE id = ?", (version_id,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            d["config_snapshot"] = _json.loads(d["config_snapshot"]) if isinstance(d.get("config_snapshot"), str) else d.get("config_snapshot", {})
        except (_json.JSONDecodeError, TypeError):
            d["config_snapshot"] = {}
        return d

    def close(self):
        if hasattr(self._local, "conn") and self._local.conn:
            self._local.conn.close()
            self._local.conn = None
