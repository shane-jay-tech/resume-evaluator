# backup/archive 保留策略特征化测试（916p-a4-010，2026-09-17，只增测试）

## 验证（命令与数字同段）

```
$ python -m pytest tests/test_backup_retention.py tests/test_archive_retention.py -q
10 passed in 0.20s                                   ← 合计 ≥8 达标
$ python -m pytest tests -q
47 passed                                             ← ≥45（37 既有+10 新增），failed=0
$ git diff --stat -- backup.py archive.py cleanup.py → 空（源码零改动）
backups/archives/reports 目录条目数：班前 0 / 班后 0（测试全走 tmp_path，零真实目录写入）✓
```

## 用例表（10 条）

| # | 文件 | 用例 | 覆盖 |
|---|---|---|---|
| 1 | test_backup_retention | db 不存在→空串+无文件 | backup.py:20-22 |
| 2 | ‖ | 正常备份：路径存在/大小>0/文件名正则 | :29-43 |
| 3 | ‖ | mtime 超 keep_days 被删 | :46-54 |
| 4 | ‖ | 近期前缀备份保留＋非前缀文件不删 | 同上白名单口径 |
| 5 | ‖ | 同秒两次调用现状钉住（同名覆盖 or 跨秒两份，两分支均断言） | :31 秒级时间戳 |
| 6 | ‖ | start_backup_scheduler 签名级断言（不启动线程） | :61 |
| 7 | test_archive_retention | 季度映射 1-4 季 | archive.py:14-22 |
| 8 | ‖ | 空串/多格式/乱码 → unknown | :16-27 |
| 9 | ‖ | cutoff 天数差容差（90/7/1 三档） | archive.py:49＋cleanup.py 口径 |
| 10 | ‖ | '%Y-%m-%d' 字符串比较格式钉死（SQL timestamp < cutoff） | archive.py SQL 口径 |

## 现状记录

- 剪枝口径 = **文件 mtime**（非文件名日期解析）；仅匹配 `recruitment_backup_*.db` 前缀。
- 同秒二次备份：秒级时间戳同名 → **同名覆盖**（实测分支；跨秒则两份——测试对两分支均兼容并钉住当前观察）。
- backup 失败回退链：sqlite3 在线备份异常 → copy2 兜底（:33-43）。

## 验收对照

- ✅ 合计 10 ≥8 全绿（同段）。- ✅ 全量 47 passed failed=0。- ✅ 源码三文件零 diff。- ✅ 真实目录零写入（前后条目数对比同段）。- ✅ 未调用 scheduler 启动；不 push。
