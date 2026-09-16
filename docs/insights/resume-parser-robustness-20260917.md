# parser 解析鲁棒性特征化测试（916p-a4-011，2026-09-17，只增测试）

## 验证（命令与数字同段）

```
$ python -m pytest tests/test_parser_robustness.py -q
10 passed in 0.37s        ← ≥8 达标
$ python -m pytest tests -q → 57 passed（≥45，failed=0）
$ git diff --stat -- parser.py → 空（判定逻辑零改动）
```

## 用例 → 覆盖对照（10 条）

| # | 用例 | 覆盖 |
|---|---|---|
| 1 | .txt/.rtf/无后缀 → ValueError | :36-37 else 分支 |
| 2 | 大写 .PDF 经 lower 命中 pdf 分支（非 ValueError） | :15 suffix.lower() |
| 3 | 0 字节伪 PDF 异常族钉住（pdfminer 空文档异常，非 ValueError） | :61-70 |
| 4 | 无 URL 文本 → 空列表 | :48 findall |
| 5 | URL 结尾标点剥离 | :52 rstrip |
| 6 | 同 URL 去重 | :53-55 seen |
| 7 | extra_domains=None 与 [] 等价 | :50 |
| 8 | 子串误匹配三条取证（见下） | :53 `if domain in url` 误报面 |
| 9 | file_hash 同内容异路径同哈希 | :18-22 |
| 10 | 空文件标准 SHA256 e3b0c442…b855 | :18-22 |

## 子串误匹配取证（构造样例 → 实测输出，≥3 条原文）

| 构造样例 | 实测输出 |
|---|---|
| https://evil-github.com/x | ['https://evil-github.com/x']（子串 github.com 误命中） |
| https://github.com.evil.com/y | ['https://github.com.evil.com/y']（后缀贴脸误命中） |
| https://notion.so.evil.com | ['https://notion.so.evil.com']（同上） |

结论：`if domain in url` 子串匹配无域名边界校验，钓鱼域可借白名单子串混入作品集链接。改动需另行评审（本单只取证钉现状）。

## 验收对照

- ✅ 10 passed ≥8。- ✅ 全量 failed=0 passed≥45。- ✅ parser.py 零 diff。- ✅ 误匹配样例 3 条原文。
