# 简历评估系统 — 全面体检与性能优化（2026-08-15）

## 元数据

- 任务类型: review + impl（系统体检、bug 修复、性能优化、人性化设计）
- 难度评分: 8 分 → **D3 较难**（影响面2 + 风险领域2 + 歧义0 + 新颖1 + 不可逆1 + 长程2）
- 调用模型: DeepSeek V4 Pro（总指挥实现 + 仲裁）；2 个审查 subagent（后端路由 / 前端页面，均为 Pro 审查）
- GPT-5.6: **未参与**。双实现触发（认证/并发/解析器/公共 API 签名）已按规则尝试 Codex CLI 直连 2 次，均无输出（本环境出网受限 SSL EOF），按规则降级单干
- 档位: quick（Pro reasoning high）
- 测试: 37/37 通过；启动实测 1.3s 就绪；端到端 HTTP 验证通过
- 是否返工: 否
- override 次数: 0

## 原始需求

全面了解系统状态，寻找 bug 并优化：重点性能（启动/加载速度）与人性化设计（拒绝反人类设计）。

## 审查发现汇总（backend subagent + frontend subagent + 总指挥）

CRITICAL（必修，共 7 条）:
1. post_setup 引用未定义变量 data → 首次设置必 500（总指挥发现）
2. post_upload_resume 签名与分发调用不匹配 → 上传必崩
3. 前端删除候选人按下标传后端、后端按全量列表下标解析 → **删错人**（dashboard）
4. 周报删除同样错位 + data.deleted 字段不存在 → 删错人 + 前端抛异常
5. compare.html 全程无转义 → 简历投毒可 XSS
6. dashboard 重复队列渲染无转义 → XSS
7. 认证硬编码 token + 前端 fallback 死代码

MAJOR 修到（共 14 条，其中修 12 条、接受风险 2 条）:
- 路由前缀匹配歧义（已修）、POST 死代码（已修）
- SQLite 无 busy_timeout 并发写锁库（已修）
- SSE broadcast 持锁阻塞写（已修）、事件计数锁外递增（已修）
- /api/health/deep 免认证+真打 LLM（已修：纳入认证）
- /api/setup、/api/test-connection 初始化后免认证覆盖配置（已修：仅未初始化时免认证）
- 硬编码 AUTH_TOKEN（已修：不再写入 .env，默认免认证，用户可自行配置加锁）
- 批量评估阻塞 HTTP 线程（已修：后台异步 + SSE 进度）
- 上传同步阻塞评估（已修：后台线程 + 大小限制 + 类型白名单）
- post_save N+1 与 count 虚报（已修）
- int() 参数未防护（已修关键端点）、test-connection 错误码 200（已修）
- 路径前缀检查无分隔符（已修）
- 前端 verdictClass 重复定义致徽章失效（已修）
- SSE 重连定时器堆积（dashboard/pipeline/quality 均已修）
- 接受风险: ① /api/export 用 window.open 无法带 Authorization（AUTH_TOKEN 配置后导出受限，默认免认证场景无影响）；②「对比篮与批量勾选共用状态」拆分属较大前端重构，本轮未做

性能优化:
- 启动扫描后台化：监控目录存量简历异步评估，面板 1.3s 立即可用（原来启动会被串行 LLM 评估阻塞数分钟）
- 新增简历处理线程池（llm.concurrency=2），监控回调不阻塞 watcher 线程
- 文件名已匹配岗位时跳过结构化提取（每份简历省 1 次 LLM 调用，llm.pre_analysis=auto）
- LLMClient 按连接参数缓存复用（不再每请求新建 OpenAI 连接池）
- SQLite 启动时跳过已存在的 schema（增量建表）
- Windows PollingObserver 轮询间隔 1s→3s
- 前端：CDN 依赖本地化（htmx 全站未使用已移除；chart.js 以自研 16KB vendor/charts.js 替代，覆盖 bar/line/radar/doughnut/堆叠/双轴）；SSE 健康时轮询 15s→60s；搜索 300ms 防抖；雷达图实例销毁防泄漏
- 图片简历 OCR 组件缺失时给出中文友好提示（原为英文 ModuleNotFoundError 处理失败）

人性化:
- 上传/批量评估立即返回，进度实时出现在面板（原来前端干等数分钟）
- 删除按 id 精准删除 + 撤销提示正常（原来会删错人）
- server.auto_open_browser 可关闭；monitor.auto_process_existing 可关闭
- WATCH_DIR 环境变量接入监控目录（用户在设置页选的目录现在真的生效）
- 首次设置写入 .env 不再依赖 python-dotenv（内置解析器兜底），重启后 key 一定生效
- 删除调试页 test_pending.html

## 修改文件

后端: main.py, app_routes.py, services.py, evaluator.py, database.py, file_watcher.py, parser.py, sse.py, utils/config.py, utils/llm_client.py, build.spec, requirements.txt, config.yaml, config.yaml.example
前端: dashboard.html, pipeline.html, quality.html, weekly_report.html, compare.html
新增: vendor/charts.js（本地图表库）
删除: test_pending.html（调试页）
归档: 本文件

## 验证

- pytest 37/37 通过
- py_compile 全绿；node --check vendor/charts.js 通过
- 端到端实测：启动就绪 1.30s；全部页面 200；vendor/charts.js 200；CDN 引用清零；POST /api/setup 空 key 返回 400（原为 500）；POST /api/upload 非 multipart 返回 400（原为 TypeError）；尾斜杠路由正确分发
- 未验证（需用户实机确认）: ① 图表渲染效果与原 Chart.js 的视觉一致性；② 真实简历的完整评估链路（本环境无 API key）

## 残留风险

1. 本环境无 DeepSeek API key，评估链路（LLM 调用、结构化提取跳过策略）只经过逻辑审查与单元测试，未做真实端到端评估
2. vendor/charts.js 为自研轻量实现，若某页面用到未覆盖的 Chart.js 特性会渲染降级
3. 「对比篮/批量勾选」状态共用、「写操作按钮 loading 防重复」等前端交互改进未纳入本轮
4. 默认无认证（绑定 127.0.0.1 的本机工具）；如需加锁，用户在 .env 配置 AUTH_TOKEN 即可，但前端固定 token 场景（导出下载）有限制
