"""测试 parser.py —— 文本提取、文件哈希、作品集链接提取。"""

import hashlib
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from parser import file_hash, extract_portfolio_links


def test_file_hash():
    with tempfile.NamedTemporaryFile(delete=False, suffix=".txt") as f:
        f.write(b"hello world")
        fpath = f.name
    try:
        h1 = file_hash(fpath)
        h2 = file_hash(fpath)
        assert h1 == h2, "同一文件哈希应相同"
        assert len(h1) == 64, "SHA256 应为 64 字符"
    finally:
        os.unlink(fpath)


def test_portfolio_links():
    text = """
    我的作品集在 https://github.com/user/repo 和 https://artstation.com/artist/123
    还有一个在 https://www.google.com 不是作品集域名
    """
    links = extract_portfolio_links(text)
    assert len(links) == 2, f"应提取 2 个链接，实际 {len(links)}"
    assert any("github.com" in l for l in links)
    assert any("artstation.com" in l for l in links)
    assert not any("google.com" in l for l in links)


def test_portfolio_links_dedup():
    text = "https://github.com/a https://github.com/a"
    links = extract_portfolio_links(text)
    assert len(links) == 1, "相同链接应去重"


def test_portfolio_links_extra_domains():
    text = "https://myportfolio.com/work"
    links = extract_portfolio_links(text, extra_domains=["myportfolio.com"])
    assert len(links) == 1


if __name__ == "__main__":
    test_file_hash()
    test_portfolio_links()
    test_portfolio_links_dedup()
    test_portfolio_links_extra_domains()
    print("✅ 所有 parser 测试通过")
