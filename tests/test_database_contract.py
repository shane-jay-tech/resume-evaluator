# -*- coding: utf-8 -*-
"""database.py 初始化幂等与去重契约特征化测试（916p-a4-012）。

全部 tmp_path 下的 sqlite 文件，绝不碰真实 data/recruitment.db。
可复跑：python -m pytest tests/test_database_contract.py -q
"""
import sqlite3

import pytest

from database import DataStore


def test_double_init_idempotent(tmp_path):
    """同一路径连续构造两次 DataStore：不抛异常，表集合相同（幂等）。"""
    db = str(tmp_path / "a.db")
    s1 = DataStore(db)
    tables1 = {r[0] for r in s1.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    s2 = DataStore(db)
    tables2 = {r[0] for r in s2.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables1 == tables2
    assert {"results", "pending", "processed", "status_history"} <= tables1


def test_core_tables_present(tmp_path):
    """核心表存在（以 DDL 实际建表为准：results/pending/processed/status_history/
    duplicate_queue/evaluation_dimensions/interview_feedback/resume_texts/task_queue/
    eval_regression/cross_validation/config_history/reference_approvals/reference_features）。"""
    s = DataStore(str(tmp_path / "b.db"))
    tables = {r[0] for r in s.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("results", "pending", "processed", "status_history", "duplicate_queue",
              "evaluation_dimensions", "interview_feedback", "resume_texts",
              "task_queue", "eval_regression", "cross_validation", "config_history",
              "reference_approvals", "reference_features"):
        assert t in tables, t


def test_wal_mode_pinned(tmp_path):
    """WAL 模式现状钉住：journal_mode 返回 'wal'（db.ts:280 同口径）。"""
    s = DataStore(str(tmp_path / "c.db"))
    mode = s.conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert str(mode).lower() == "wal"


def test_verdict_defaults_from_ddl(tmp_path):
    """关键列 DDL 默认值：verdict=''/original_verdict=''/pipeline_status='待筛选'。"""
    s = DataStore(str(tmp_path / "d.db"))
    cols = {r[1]: r[4] for r in s.conn.execute("PRAGMA table_info(results)")}
    assert cols["verdict"] == "''"            # DDL 里 DEFAULT ''，dflt_value 存字面 ''
    assert cols["original_verdict"] == "''"
    assert cols["pipeline_status"] == "'待筛选'"


def test_empty_db_queries_do_not_raise(tmp_path):
    """空库查询：get_all_results/get_stats/is_resume_processed 空库不抛异常。"""
    s = DataStore(str(tmp_path / "e.db"))
    assert s.get_all_results() == []
    assert isinstance(s.get_stats(), dict)
    assert s.is_resume_processed("不存在.pdf") is False


def test_add_result_then_query_by_filename(tmp_path):
    """插入最小结果记录后，按文件名可查回（resume_file 列）。"""
    s = DataStore(str(tmp_path / "f.db"))
    rid = s.add_result({
        "candidate_name": "张三",
        "resume_file": "张三_简历.pdf",
        "verdict": "推荐",
        "total_score": 72,
    })
    assert isinstance(rid, int)
    row = s.get_result_by_filename("张三_简历.pdf")
    assert row is not None
    assert row["candidate_name"] == "张三"


def test_dedup_key_is_fhash_not_filename(tmp_path):
    """去重键口径钉死：processed/duplicate 走 fhash（SHA256），
    is_resume_processed 走文件名——两条独立通道，证据 database.py:552/:593。"""
    s = DataStore(str(tmp_path / "g.db"))
    s.add_processed("deadbeef" * 8)
    assert s.is_processed("deadbeef" * 8) is True
    assert s.try_add_processed("deadbeef" * 8) is False  # 同 fhash 幂等
    # 文件名通道与 fhash 通道互不影响
    assert s.is_resume_processed("deadbeef") is False


def test_soft_delete_and_undo(tmp_path):
    """软删/恢复契约：delete_results(soft) 置 deleted=1，undo_delete 还原。"""
    s = DataStore(str(tmp_path / "h.db"))
    rid = s.add_result({"candidate_name": "李四", "resume_file": "l.pdf"})
    s.delete_results([rid], soft=True)
    assert s.get_result_by_id(rid)["deleted"] == 1
    assert s.undo_delete(rid) is True
    assert s.get_result_by_id(rid)["deleted"] == 0
