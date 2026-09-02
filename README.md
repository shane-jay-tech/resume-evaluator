# 简历评估系统

本地优先的 AI 简历评估工具，适合个人求职准备和小团队招聘筛选。系统从 PDF、DOCX 或图片中提取简历内容，按岗位标准进行匹配、评分、复核并生成报告。

## 功能

- PDF、DOCX 和图片简历解析
- 多岗位配置、硬性门槛和加分项
- 证据优先的多维度评分
- 低置信度简历进入待分配池
- 高分结果可触发二次复核
- Markdown 报告、质量审计和统计指标
- 下载目录文件监控与实时页面更新
- SQLite 本地存储、备份、归档和清理

## 架构

```text
main.py -> app_routes.py -> services.py -> evaluator.py
                 |                 |             |
           file_watcher.py      database.py   LLM client
                 |
                sse.py -> HTML dashboard
```

## 本地运行

```bash
python -m venv .venv
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python main.py
```

首次运行时通过本地配置提供 AI 服务凭据。不要把 API key、简历原文、候选人信息、数据库文件或报告提交到 Git 仓库。

## 评分说明

评估结果是辅助信息，不是自动录用决定。使用者应核对原始简历、岗位要求和评分证据，并遵守个人信息保护与招聘公平要求。

更详细的开发说明见 [`README_DEV.md`](README_DEV.md)，普通使用说明见 [`README_USER.md`](README_USER.md)。
