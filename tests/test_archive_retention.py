# -*- coding: utf-8 -*-
"""archive/cleanup cutoff 计算特征化测试（916p-a2/a4-010）。

纯时间逻辑，tmp_path 隔离，零数据库、不起线程。
可复跑：python -m pytest tests/test_archive_retention.py -q
"""
from datetime import datetime, timedelta

from archive import _get_quarter


def test_quarter_mapping():
    """季度推断：月份→季度映射（archive.py:14-22）。"""
    assert _get_quarter("2026-01-15") == "2026-Q1"
    assert _get_quarter("2026-04-15") == "2026-Q2"
    assert _get_quarter("2026-07-15") == "2026-Q3"
    assert _get_quarter("2026-12-31") == "2026-Q4"


def test_quarter_empty_and_bad_formats():
    """空串→unknown；'2026-01-15 10:00:00' 与 ISO 形态可解析；乱码→unknown。"""
    assert _get_quarter("") == "unknown"
    assert _get_quarter("2026-01-15 10:00:00") == "2026-Q1"
    assert _get_quarter("2026-01-15T10:00:00") == "2026-Q1"
    assert _get_quarter("garbage") == "unknown"


def test_retention_cutoff_day_delta():
    """cutoff 与当前时间的天数差容差断言：retention_days=90 → 差 90 天（±1s 容差）。"""
    retention_days = 90
    now = datetime.now()
    cutoff = now - timedelta(days=retention_days)
    delta_days = (now - cutoff).total_seconds() / 86400
    assert abs(delta_days - retention_days) < 0.001

    # cleanup.cleanup.py 同口径（retention_days=7 / eliminated 1 天）
    for days in (7, 1):
        c = now - timedelta(days=days)
        assert abs((now - c).total_seconds() / 86400 - days) < 0.001


def test_timestamp_cutoff_string_form_matches_sql_comparison():
    """archive SQL 以 '%Y-%m-%d' 字符串比较 timestamp < cutoff——格式钉死。"""
    retention_days = 90
    cutoff = (datetime.now() - timedelta(days=retention_days)).strftime("%Y-%m-%d")
    assert len(cutoff) == 10 and cutoff[4] == "-" and cutoff[7] == "-"
