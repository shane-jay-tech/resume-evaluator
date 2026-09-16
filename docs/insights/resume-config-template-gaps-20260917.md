# resume-evaluator config.yaml.example 缺口补齐（916p-a4-009，2026-09-17）

## 三张表

### ① 代码读取点（任务书点名域，去重后）

| 节.键 | 读取点 |
|---|---|
| archive.enabled / archive.retention_days / archive.archive_dir | archive.py:40/44/45 |
| cross_validation.api_key_env / api_key / base_url / model / enabled / min_score_trigger / max_tokens / timeout / max_retries | cross_validator.py:49-58 |
| cleanup.enabled / cleanup.interval_hours / cleanup.retention_days / cleanup.eliminated_retention_days / cleanup.cleanup_dir | cleanup.py:20-27＋main.py:377 |
| server.cors_origin | main.py:186-188 |
| server.pdf_fonts | services.py:385 |

### ② 差集（补齐前）

- 代码读、模板无：**archive 整节（3 键）＋cross_validation 整节（9 键）＋cleanup.retention_days/eliminated_retention_days/cleanup_dir（3 键）＋server.cors_origin/pdf_fonts（2 键）**——补齐前共 17 键缺口（其中 4 键已由 d916-01 前轮补齐的为 backup 节，不重复）。
- 模板有、代码不读：0（现有节均有消费方）。

### ③ 补齐后

```
$ rg -nE '^archive:|^cross_validation:|^cleanup:' config.yaml.example → 3 节命中
$ python -c "import yaml…yaml.safe_load…" → sorted 顶层 15 节（archive/cross_validation 在列）
$ python -m pytest tests -q → 37 passed（零影响）
```

补齐内容（只加键与中文注释，密钥类仅注明环境变量来源，无真实密钥）：

- `archive:` enabled/retention_days(90)/archive_dir
- `cross_validation:` enabled(false)/api_key_env/api_key(注释占位)/base_url/model/min_score_trigger(85)/max_tokens/timeout/max_retries
- `cleanup:` 追加 retention_days(7)/eliminated_retention_days(1)/cleanup_dir

## 验收对照

- ✅ 补齐后三节名 rg 命中 ≥3。
- ✅ 「代码读、模板无」归零（全部进模板）。
- ✅ YAML 可解析（15 顶层节打印同段）。
- ✅ pytest 37 passed。- ✅ git diff --stat 只含 config.yaml.example（README 为 a4-008 已提交项，另行提交）。
