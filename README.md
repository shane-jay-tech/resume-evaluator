# 简历评估系统

本地优先的 AI 简历评估引擎，适合个人求职准备和小团队招聘筛选。系统从 PDF、DOCX 或图片中提取简历内容，按岗位标准进行匹配、评分、复核并生成报告，全程数据保存在本机。

## 技术架构

- Python 3.14 + `http.server` 轻量服务
- SQLite（WAL 模式）本地存储
- Watchdog 文件监控 + SSE 实时推送
- DeepSeek 兼容的 OpenAI API 用于评估与交叉校验
- Prometheus 指标、Markdown 报告与 PyInstaller 打包

简历原文、数据库、报告、日志、备份与环境文件均被 Git 忽略，不会上传到 GitHub。

## 功能

- PDF、DOCX 和图片简历解析（含 OCR 路径）
- 多岗位配置、硬性门槛和加分项
- 证据优先的多维度评分（4 维度 × 10 分，按权重汇总）
- 低置信度简历进入待分配池
- 高分结果可触发二次交叉复核
- Markdown 报告、评分质量审计和统计指标
- 下载目录文件监控与实时页面更新
- SQLite 本地存储、每日自动备份、90 天归档与 5 天清理

## 评分流程

```text
简历文件 (PDF/DOCX/图片)
    → parser.py 提取文本
    → file_watcher.py 稳定性检测
    → database.py 去重（SHA256 + 文件名）
    → evaluator.py 岗位匹配（文件名关键词 + LLM 智能匹配）
    → evaluator.py 三阶段评估
        ├ 硬性门槛检查
        ├ 证据优先评分
        └ 交叉自检（自洽性 / 消极偏向）
    → cross_validator.py 高分复核（≥85 分触发）
    → reporter.py 生成 Markdown 报告
    → sse.py 推送结果到前端
```

评分口径：综合得分 = Σ(维度得分 × 维度权重) × 10；≥80 强烈推荐 / 65–79 推荐 / 50–64 待定 / <50 不推荐。

## 安装与启动

### Windows

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python main.py
```

或双击 `install.bat` 创建桌面快捷方式。

### macOS / Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

首次启动在浏览器完成 API 凭据配置；页面资源全部内置，只有 AI 评估本身需要联网。

## 配置与安全

- 岗位、维度、权重、硬性条件和路由规则全部由 `config.yaml` 驱动。
- API key 保存在本地 `.env`，不要提交到 Git。
- 前端认证 token 必须显式配置；无 token 时请求按未认证拒绝，不回退到任何内置默认值。
- 评估结果只应在本机或受控内网使用，不应直接暴露到公网。

## 验证

```powershell
pytest                       # 测试套件
python scoring_audit.py      # 评分质量审计
```

## 文档

- [`README_USER.md`](README_USER.md)：安装、使用与常见问题
- [`README_DEV.md`](README_DEV.md)：架构、目录、核心流程与岗位扩展说明
- `CHANGELOG_v2.md`：版本变更记录

## 目录

```text
main.py               入口：服务、文件监控与定时任务
app_routes.py         API 路由表
evaluator.py          评估核心
services.py           共享业务服务
parser.py             文件解析
database.py           SQLite 数据层
file_watcher.py       文件监控
sse.py                SSE 实时推送
cross_validator.py    高分交叉复核
scoring_audit.py      评分质量审计
reporter.py           Markdown 报告
archive.py / backup.py / cleanup.py   归档、备份与清理
config.yaml           岗位与评分配置
rules/                硬性筛选规则
prompts/              提示词模板
tests/                测试套件
```

## 使用边界

评估结果是辅助信息，不是自动录用决定。使用者应核对原始简历、岗位要求和评分证据，并遵守个人信息保护与招聘公平要求。
