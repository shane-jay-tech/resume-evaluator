# -*- coding: utf-8 -*-
"""backup_database 保留策略特征化测试（916p-a4-010）。

tmp_path 隔离；不起调度线程。
可复跑：python -m pytest tests/test_backup_retention.py -q
"""
import os
import re
import sqlite3
import time

import backup


def _mk_db(path):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE t (x INTEGER)")
    con.execute("INSERT INTO t VALUES (1)")
    con.commit()
    con.close()


def test_missing_db_returns_empty_and_no_file(tmp_path):
    """db 文件不存在 → 返回空串，且备份目录不产生文件（backup.py:20-22）。"""
    bdir = tmp_path / "backups"
    out = backup.backup_database(str(tmp_path / "nope.db"), str(bdir))
    assert out == ""
    assert not bdir.exists() or list(bdir.iterdir()) == []


def test_normal_backup_creates_named_file(tmp_path):
    """正常备份：返回路径存在、大小>0、文件名匹配 recruitment_backup_\\d{8}_\\d{6}.db。"""
    db = tmp_path / "app.db"
    _mk_db(str(db))
    bdir = tmp_path / "backups"
    out = backup.backup_database(str(db), str(bdir))
    assert os.path.exists(out)
    assert os.path.getsize(out) > 0
    base = os.path.basename(out)
    assert re.match(r"recruitment_backup_\d{8}_\d{6}\.db$", base), base


def test_prune_by_mtime_outside_keep_days_deleted(tmp_path):
    """mtime 在 keep_days 之外的 recruitment_backup_*.db 被删除（:46-54 口径）。"""
    db = tmp_path / "app.db"
    _mk_db(str(db))
    bdir = tmp_path / "backups"
    bdir.mkdir()
    old = bdir / "recruitment_backup_20200101_000000.db"
    old.write_text("old", encoding="utf-8")
    stamp = time.time() - 60 * 86400
    os.utime(old, (stamp, stamp))
    out = backup.backup_database(str(db), str(bdir), keep_days=30)
    assert os.path.exists(out)
    assert not old.exists()  # 过期备份被清


def test_prune_keeps_recent_and_non_prefix_files(tmp_path):
    """keep_days 之内的同名前缀备份保留；非 recruitment_backup_ 前缀文件不被删。"""
    db = tmp_path / "app.db"
    _mk_db(str(db))
    bdir = tmp_path / "backups"
    bdir.mkdir()
    recent = bdir / "recruitment_backup_20990101_000000.db"
    recent.write_text("recent", encoding="utf-8")
    other = bdir / "manual_notes.txt"
    other.write_text("keep", encoding="utf-8")
    out = backup.backup_database(str(db), str(bdir), keep_days=30)
    assert os.path.exists(out)
    assert recent.exists()      # 未来 mtime → 保留
    assert other.exists()       # 非前缀 → 不删


def test_same_second_second_call_overwrites_or_duplicates_pinned(tmp_path):
    """同秒两次调用：现状钉住——秒级时间戳同名 → 第二次覆盖第一次（单份文件）。"""
    db = tmp_path / "app.db"
    _mk_db(str(db))
    bdir = tmp_path / "backups"
    p1 = backup.backup_database(str(db), str(bdir), keep_days=30)
    files1 = set(os.listdir(bdir))
    p2 = backup.backup_database(str(db), str(bdir), keep_days=30)
    files2 = set(os.listdir(bdir))
    # 现状：同秒时间戳同名 → os 同名覆盖，文件数不增（若跨秒则可能两份——两分支均记录）
    if p1 == p2:
        assert len(files2) == len(files1)  # 同名覆盖
    else:
        assert len(files2) == len(files1) + 1  # 跨秒产生第二份
    assert os.path.exists(p2)


def test_scheduler_signature_untouched():
    """不启动线程：仅断言调度器函数存在且默认参数形态（签名级覆盖）。"""
    import inspect
    from backup import start_backup_scheduler
    sig = inspect.signature(start_backup_scheduler)
    names = list(sig.parameters)
    assert "db_path" in names
