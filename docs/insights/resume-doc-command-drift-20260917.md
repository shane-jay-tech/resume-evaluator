# resume-evaluator 三份指南启动/测试命令口径统一（916p-a4-013，2026-09-17）

## 一、命令抓取（rg 命中 5 行，三列表）

| 文件:行号 | 原文 | 实测结果 | 订正后 |
|---|---|---|---|
| README.md:61 | python3 -m venv .venv | `python3 --version` →「Python was not found…App execution aliases」（Windows 无 python3） | python -m venv .venv |
| README_DEV.md:208 | python3 -m pytest tests/ -v | 同上解释器缺失 | python -m pytest tests/ -v |
| 升级后验证清单.md:5 | python3 main.py config.yaml | 解释器缺失；参数形态合法——main.py:267-268 `sys.argv[1]` 实读 config 路径（只读源码判定，未启动服务） | python main.py config.yaml |
| 升级后验证清单.md:145 | python3 tests/test_parser.py && python3 tests/test_evaluator.py | 两测试文件均含 `__main__` 入口（grep 各 1 命中） | python tests/test_parser.py && python tests/test_evaluator.py |
| 升级部署检查清单.md:67 | python3 main.py config.yaml | 同 :5（参数形态合法） | python main.py config.yaml |

## 二、未真跑的执行类命令披露

`main.py config.yaml`（无论新旧写法）**均未真实启动**——入口参数形态仅以源码判定（main.py:258 def main、:267-268 sys.argv[1] 取 config 路径）；本班未产生新 logs/ 文件（检查：`git status --porcelain | grep logs` 空）。

## 三、口径统一理由

本机为 Windows（python3 缺失实证原文：Python was not found…），统一改为 `python …`；保留跨平台写法的豁免额度（≤2 条）未动用。

## 四、验收对照

- ✅ 三列表 5 行＝rg 计数 5。
- ✅ 订正后 `rg -n "python3 " 五文件` 命中 0（exit 1）。
- ✅ git diff --stat 只含 4 个 Markdown（README.md/README_DEV.md/升级后验证清单/升级部署检查清单），零 .py。
- ✅ 全程未启动服务（logs/ 零新增同段）。
