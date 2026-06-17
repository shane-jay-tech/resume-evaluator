# 简历评估系统 v2 完整变更日志

**优化日期**: 2026-06-15  
**优化方式**: 四模型协作系统（GPT-5.5 + Claude + DeepSeek + Kimi）  
**优化轮次**: 3 轮（Round 1 修复 → Round 2 重构 → Round 3 打磨）

---

## 总体成果

| 指标 | v1 | v2 | 变化 |
|------|----|----|------|
| main.py 行数 | 1702 | ~340 | **-80%** |
| 模块数 | 8 | 14 | +6 |
| API 路由方式 | if/elif 链 (O(n)) | 字典路由表 (O(1)) | 升级 |
| 循环导入 | 无 | 无 | 保持 |
| 测试覆盖 | 0 | **26** | +26 |
| LLM 客户端 | 散落 3 处 | 统一 LLMClient | 升级 |
| 数据库表 | 7 | 8 (+evaluation_dimensions) | +1 |
| 维度查询 | 不支持 | 支持 SQL 查询 | 新增 |

---

## Round 1: 关键 Bug 修复 (P0)

1. **`_handle_assign` 崩溃 bug** — 手动分配岗位引用不存在的 `EVAL_SYSTEM`
2. **`_start_time` NameError** — 请求在初始化前到达时崩溃
3. **`eval_metadata` 未持久化** — 静默丢弃评估元数据

## Round 1: 工程化改进 (P1)

4. 新建 `utils/llm_client.py` — 统一 LLM 客户端（超时/重试/校验/统计）
5. LLM 输出 JSON 校验 — 每次调用验证必需字段
6. 评分归一化 — 防止 LLM 返回异常分值
7. 配置驱动路由规则 — 替代硬编码的关卡×发行创意逻辑
8. 文件稳定性检测 — 替代 `time.sleep(2)`
9. PDF 字体路径配置化
10. SSE 事件 ID + 15s 心跳
11. `evaluation_dimensions` 结构化表
12. 评估元数据 (`eval_metadata`)

## Round 2: 架构重构

13. 新建 `app_routes.py` — 路由表（37 个端点）
14. 新建 `services.py` — 共享服务层，**消除 main.py ↔ app_routes.py 循环导入**
15. 新建 `file_watcher.py` — 文件监控模块
16. main.py 从 1702 → ~340 行
17. 清理所有 `import main as _m` 循环引用
18. cleanup.py 改用 DataStore 公共 API
19. archive.py 补充 eval_metadata 字段

## Round 3: 打磨

20. JSON 解析 `raw_decode()` 处理 trailing 内容
21. SSE 事件使用字符串常量（非硬编码）
22. app_routes.py process_resume 调用通过 `_proc()` wrapper
23. 26 个单元测试（JSON/评分/维度/匹配/路由/数据库/SSE）
24. 14 个源文件零语法错误、零循环导入

---

## 文件变更

| 文件 | 状态 | 说明 |
|------|------|------|
| `utils/llm_client.py` | **新建** | 统一 LLM 客户端 |
| `app_routes.py` | **新建** | API 路由表（16 GET + 21 POST） |
| `services.py` | **新建** | 共享服务层 |
| `file_watcher.py` | **新建** | 稳定性检测文件监控 |
| `tests/test_v2.py` | **新建** | 26 个测试用例 |
| `evaluator.py` | **重写** | LLMClient + 校验 + 路由规则 |
| `database.py` | **修改** | +evaluation_dimensions + eval_metadata |
| `main.py` | **精简** | 1702 → 340 行 |
| `sse.py` | **修改** | 事件 ID |
| `cleanup.py` | **修改** | 使用公共 API |
| `archive.py` | **修改** | 补充字段 |
| `config.yaml` | **微调** | 路由规则配置化 |

---

## 验证结果

```
✅ 14 源文件语法检查通过
✅ 零循环导入
✅ 37 个路由全部保留
✅ 26/26 测试通过
✅ 向后兼容（自动 DB 升级）
```
