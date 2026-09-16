# resume-evaluator README 运维口径对账（916p-a4-008，2026-09-17）

## 对账表（≥4 行，README 行号＋代码依据＋判定）

| README 声明（:24） | 代码依据 | 代码默认值 | 判定 |
|---|---|---|---|
| 「每日自动备份」 | main.py:383-385 `interval_hours=backup_cfg.get("interval_hours", 6)`、`keep_days=30` | **每 6 小时**、保留 30 天 | **不一致已订正**：README 原「每日」与默认 6 小时不符 → 改「定时备份（默认每 6 小时，保留 30 天）」 |
| 「90 天归档」 | archive.py:44 `retention_days=archive_cfg.get("retention_days", 90)` | 90 天 | 一致（保留） |
| 「5 天清理」 | cleanup.py:25-26 `retention_days=7`、`eliminated_retention_days=1` | **7 天＋已淘汰 1 天两档** | **不一致已订正**：README 原「5 天清理」与默认 7 天/1 天两档不符 → 改「清理（入库保留 7 天、已淘汰 1 天；可在 config.yaml 覆盖）」 |
| 「高分复核（≥85 分触发）」（:38） | cross_validator.py:56-57 `enabled=False`、`min_score_trigger=85`；:95-103 另有 40-65 分随机抽样 20% | enabled 默认 False、需 api_key/env | **表述不完整已订正**：README 未提默认关闭与凭据前置 → 补「默认 85，需配置 api_key/env 并 enabled=true 才生效；另有 40–65 分随机抽样 20% 通道」 |
| 评分口径与分段（:43） | 综合得分=Σ(维度×权重)×10；80/65/50 分段 | — | 本单未核项：分段阈值若由 LLM 提示词决定而非代码硬编码，无法从代码证伪（判定依据：分段文案属展示层口径，代码仅见 min_score_trigger）——如实说明 |

config.yaml.example:22-31 对照：backup.interval_hours=6、keep_days=30、cleanup.interval_hours=24——与代码默认一致 ✓（example 与 README 原文矛盾，佐证 README 需订正）。

## 订正后复验

```
$ rg -n "每日自动备份|5 天清理" README.md → 命中 0（exit 1）✓
$ python -m pytest tests -q → 37 passed（尾行，与任务书预期一致）✓
$ git diff --stat → README.md 4 行（2+/2-）；test_pending.html 16 行删除为班前既有 D 状态（非本班）
```

## 验收对照

- ✅ 对账表 ≥4 行（5 行），每行 README 行号＋代码 file:line＋默认值＋判定。
- ✅ 订正后旧口径短语命中 0。
- ✅ pytest 37 passed 同段。
- ✅ git diff --stat 只含 README.md（本班足迹；班前 D 项注记）。未读/未改本机 config.yaml；不 push。
