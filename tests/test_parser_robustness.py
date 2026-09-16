# -*- coding: utf-8 -*-
"""parser.py 解析鲁棒性特征化测试（916p-a4-011，只钉现状不改判定）。

不重复 test_parser.py 既有 4 条（file_hash/作品集链接/去重/extra_domains 主路径）。
全部离线、tmp 隔离。
可复跑：python -m pytest tests/test_parser_robustness.py -q
"""
import pytest

from parser import extract_portfolio_links, extract_text, file_hash


# ── extract_text 未知后缀现状 ──

def test_unknown_suffixes_raise_value_error(tmp_path):
    """.txt/.rtf/无后缀 → ValueError（:36-37 else 分支）。"""
    for name in ("a.txt", "b.rtf", "noext"):
        p = tmp_path / name
        p.write_text("x", encoding="utf-8")
        with pytest.raises(ValueError):
            extract_text(str(p))


def test_uppercase_pdf_suffix_passes_gate(tmp_path):
    """大写 .PDF 经 suffix.lower() 命中 pdf 分支 → 进 _extract_pdf（非 ValueError；
    空伪 pdf 的下游异常另行覆盖）。"""
    p = tmp_path / "c.PDF"
    p.write_bytes(b"%PDF-1.4 broken")
    with pytest.raises(Exception) as ei:
        extract_text(str(p))
    assert not isinstance(ei.value, ValueError)


# ── 空文件 / 0 字节伪 PDF 现状 ──

def test_zero_byte_pdf_raises_pinned(tmp_path):
    """0 字节伪 PDF → 实测抛异常并钉住类型（现状：pdfminer 空 document 异常族）。"""
    p = tmp_path / "empty.pdf"
    p.write_bytes(b"")
    with pytest.raises(Exception) as ei:
        extract_text(str(p))
    assert not isinstance(ei.value, ValueError)


# ── extract_portfolio_links ──

def test_no_url_text_returns_empty():
    """无 URL 文本 → 空列表。"""
    assert extract_portfolio_links("完全没有链接的普通文本介绍") == []


def test_trailing_punctuation_stripped():
    """URL 结尾标点被剥离（:52 rstrip ".,;:!?"）。"""
    text = "我的 GitHub：https://github.com/octocat。欢迎围观"
    out = extract_portfolio_links(text)
    assert out == ["https://github.com/octocat"]


def test_same_url_deduped():
    """同一 URL 多次出现只留一次（seen 集合）。"""
    text = "https://github.com/octocat 和 https://github.com/octocat 再来一次"
    assert extract_portfolio_links(text) == ["https://github.com/octocat"]


def test_extra_domains_none_vs_empty_equivalent():
    """extra_domains=None 与 [] 行为一致（:50 if extra_domains 才扩展）。"""
    text = "看看 https://notion.so/我的页面"
    assert extract_portfolio_links(text, None) == extract_portfolio_links(text, [])
    assert len(extract_portfolio_links(text, [])) == 1


# ── 子串误匹配取证（:53 if domain in url 的误报面）──

def test_substring_false_positives_pinned():
    """子串误匹配三条样例实测输出原样钉住（evil 域命中白名单子串 → 误报进结果）。

    - https://evil-github.com/x   含子串 github.com → 误匹配
    - https://github.com.evil.com/y 含子串 github.com → 误匹配
    - https://notion.so.evil.com  含子串 notion.so → 误匹配
    这是 :53 `if domain in url` 子串匹配带来的误报面（改动需另行评审，本单只取证）。
    """
    for text in ("https://evil-github.com/x",
                 "https://github.com.evil.com/y",
                 "https://notion.so.evil.com"):
        out = extract_portfolio_links(text)
        assert len(out) == 1, (text, out)


# ── file_hash ──

def test_file_hash_same_content_different_path(tmp_path):
    """同内容不同路径 → 同哈希。"""
    a = tmp_path / "a.bin"
    b = tmp_path / "sub"
    b.mkdir()
    c = b / "b.bin"
    for p in (a, c):
        p.write_bytes(b"same-bytes")
    assert file_hash(str(a)) == file_hash(str(c))


def test_file_hash_empty_file_known_sha(tmp_path):
    """空文件 → SHA256 空串标准值 e3b0c442…b855。"""
    p = tmp_path / "empty.bin"
    p.write_bytes(b"")
    assert file_hash(str(p)) == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
