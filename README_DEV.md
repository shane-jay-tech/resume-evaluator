# 简历评估系统 — 开发文档

## 系统架构

```
main.py 入口 → app_routes.py API路由 → evaluator.py LLM评估
                ↓                         ↓
           services.py 业务层       utils/llm_client.py
                ↓                         ↓
           database.py 存储          prompts/system_prompt.md
                ↓
           file_watcher.py 文件监控 + sse.py 实时推送
```

**技术栈**: Python 3.14, SQLite, http.server, DeepSeek API, Watchdog

---

## 目录结构

```
resume_evaluator/
├── main.py                  # 入口: 启动服务、文件监控、定时任务
├── app_routes.py            # API路由表 (GET_ROUTES / POST_ROUTES)
├── evaluator.py             # 评估核心: 岗位匹配 + 三阶段prompt评分
├── database.py              # SQLite数据层 (14张表, WAL模式)
├── services.py              # 共享服务: process_resume, 通知聚合
├── parser.py                # 文件解析: PDF/DOCX/图片OCR
├── reporter.py              # Markdown报告生成
├── file_watcher.py          # Watchdog文件监控 (含Windows兼容)
├── sse.py                   # SSE实时推送 (EventSource)
├── cross_validator.py       # Claude交叉校验 (高分复核)
├── scoring_audit.py         # 评分质量审计
├── metrics.py               # Prometheus监控指标
├── archive.py               # 数据归档 (90天)
├── backup.py                # 数据库自动备份
├── cleanup.py               # 自动清理 (5天周期)
├── build.spec               # PyInstaller打包配置
├── .env                     # API Key配置 (首次启动引导页自动生成)
├── config.yaml              # 主配置文件 (66KB, 16岗位配置)
├── requirements.txt         # Python依赖
├── utils/
│   ├── config.py            # YAML + .env 加载
│   ├── llm_client.py        # LLM统一客户端 (重试/超时/JSON校验)
│   ├── logger.py            # 日志 (按天轮转)
│   └── paths.py             # 跨平台路径 (Mac/Win)
├── prompts/
│   ├── system_prompt.md     # 外部评估提示模板
│   └── templates.yaml       # 提示词模板 (可在此自定义)
├── rules/
│   └── screening_rules.yaml # 硬性筛选规则
├── data/
│   ├── recruitment.db       # SQLite数据库
│   └── resumes/             # 简历永久存储
├── tests/                   # 测试套件 (37个用例)
└── references/              # 标杆简历 (按岗位)
```

---

## 核心流程

### 简历处理流水线

```
简历文件 (PDF/DOCX/图片)
    ↓ parser.py 提取文本
    ↓ file_watcher.py 稳定性检测
    ↓ database.py 去重 (SHA256 + 文件名)
    ↓ evaluator.py 岗位匹配
    │  ├─ 文件名关键词匹配 (精确/别名)
    │  └─ LLM智能匹配 (低置信度→待分配池)
    ↓ evaluator.py 三阶段评估
    │  ├─ 阶段1: 硬性门槛检查 (hard_gates)
    │  ├─ 阶段2: 证据优先评分 (4维度×10分)
    │  └─ 阶段3: 交叉自检 (自洽性/消极偏向)
    ↓ cross_validator.py 高分复核 (≥85分 → Claude二次校验)
    ↓ reporter.py 生成评估报告 (Markdown)
    ↓ sse.py 推送结果到前端
```

### 评分算法

```
综合得分 = sum(维度_i 得分 × 维度_i 权重) × 10
结论: ≥80 强烈推荐 / 65-79 推荐 / 50-64 待定 / <50 不推荐
```

### 配置驱动

16个岗位的评估标准全部通过 `config.yaml` 配置，包括：
- `dimensions`: 4个评估维度及其权重
- `must_have` / `nice_to_have`: 硬性/加分条件
- `scoring`: 每个维度的评分标准文本
- `routing_rules`: 跨岗位路由规则

---

## 添加/修改岗位

### 方法一：通过 Web 界面
1. 打开 Dashboard → 评估标准
2. 点击左侧岗位，编辑右侧详情
3. 保存（自动存到 localStorage + 触发后端同步）

### 方法二：直接编辑配置文件
编辑 `config.yaml` 中的 `positions` 部分：

```yaml
positions:
  新岗位名:
    name: "新岗位名"
    aliases: ["别名1", "别名2"]
    enabled: true
    education: "本科及以上"
    experience: "3年以上游戏行业"
    dimensions:
      能力维度1: {weight: 0.35, desc: "评估描述"}
      能力维度2: {weight: 0.30, desc: "..."}
      经验匹配: {weight: 0.20, desc: "..."}
      综合评价: {weight: 0.15, desc: "..."}
    must_have:
      - "必备条件1"
      - "必备条件2"
    nice_to_have:
      - "加分项1"
    scoring: |
      各维度评分标准文本（会注入到LLM prompt中）
```

修改后保存，系统会自动热加载（无需重启）。

---

## 自定义评估提示词

编辑 `prompts/system_prompt.md` 可以自定义评估的系统提示词。

模板变量：
- `{dimensions}` — 维度名称列表（如"游戏理解、创意策划、…"）
- `{dimensions_json}` — 维度JSON格式

如果该文件存在，系统会自动加载并替换内置模板。

---

## API 接口

所有接口在 `http://127.0.0.1:18980` 下：

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/health` | GET | 健康检查 |
| `/api/results` | GET | 评估结果列表 |
| `/api/pending` | GET | 待分配池 |
| `/api/positions` | GET | 岗位列表 |
| `/api/upload` | POST | 上传简历 (multipart) |
| `/api/assign` | POST | 手动分配岗位并评估 |
| `/api/delete` | POST | 删除候选人(软删除,可撤销) |
| `/api/export/pdf` | GET | 导出PDF报告 |
| `/api/events` | GET | SSE事件流 |

认证方式：Bearer Token（`.env`中的 `AUTH_TOKEN`）

---

## 数据库结构

SQLite WAL 模式，14张表：

| 表名 | 说明 |
|------|------|
| `results` | 评估结果(含维度JSON、pipeline状态) |
| `pending` | 待分配任务 |
| `processed` | 已处理文件哈希(防重复) |
| `status_history` | Pipeline状态变更历史 |
| `evaluation_dimensions` | 结构化维度评分 |
| `interview_feedback` | 面试反馈(校准评分) |
| `resume_texts` + `_fts` | 简历原文 + FTS5全文搜索 |
| `task_queue` | 任务队列(断点续传) |
| `cross_validation` | Claude交叉校验结果 |
| `config_history` | 配置版本历史 |
| `duplicate_queue` | 重复检测队列 |
| `eval_regression` | 回归测试结果 |

---

## 打包分发

```bash
# 安装打包工具
pip install pyinstaller

# 打包
pyinstaller build.spec

# 输出在 dist/简历评估/
# 将整个文件夹压缩为 ZIP 发给同事
```

Windows 额外步骤：将 Tesseract 便携版解压到 `dist/简历评估/tesseract/`

---

## 运行测试

```bash
python3 -m pytest tests/ -v
```

---

## 日志

日志文件在 `logs/app.log`，按天轮转，保留 7 天。
日志格式: `时间 [级别] 模块名: 消息`
