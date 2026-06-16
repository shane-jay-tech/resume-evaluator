"""简历文本提取：支持 PDF、DOCX、JPEG/PNG 格式，含 OCR 预处理和作品集链接提取。"""

import hashlib
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# 作品集/在线简历链接域名
PORTFOLIO_DOMAINS = [
    "artstation.com", "github.com", "behance.net", "zcool.com.cn",
    "gitee.com", "notion.so", "figma.com", "dribbble.com",
    "linkedin.com", "cake.me", "cakeresume.com", "yourator.co",
]


def file_hash(filepath: str) -> str:
    """计算文件 SHA256，用于去重。"""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def extract_text(filepath: str, ocr_config: dict = None) -> str:
    """根据扩展名提取文件文本内容。"""
    ext = Path(filepath).suffix.lower()
    if ext == ".pdf":
        return _extract_pdf(filepath)
    elif ext in (".docx", ".doc"):
        return _extract_docx(filepath)
    elif ext in (".jpeg", ".jpg", ".png"):
        return _extract_image(filepath, ocr_config)
    else:
        raise ValueError(f"不支持的文件格式: {ext}")


def extract_portfolio_links(text: str, extra_domains: list = None) -> list:
    """从文本中提取作品集/在线简历链接。"""
    domains = list(PORTFOLIO_DOMAINS)
    if extra_domains:
        domains.extend(extra_domains)

    pattern = r'https?://[^\s<>"\'\)\]，,。；;]+'
    urls = re.findall(pattern, text)

    result = []
    seen = set()
    for url in urls:
        url = url.rstrip(".,;:!?")
        for domain in domains:
            if domain in url and url not in seen:
                seen.add(url)
                result.append(url)
                break
    return result


def _extract_pdf(filepath: str) -> str:
    import pdfplumber

    text_parts = []
    with pdfplumber.open(filepath) as pdf:
        for page in pdf.pages:
            t = page.extract_text()
            if t:
                text_parts.append(t)
    return "\n".join(text_parts)


def _extract_docx(filepath: str) -> str:
    from docx import Document

    doc = Document(filepath)
    return "\n".join(p.text for p in doc.paragraphs if p.text.strip())


def _extract_image(filepath: str, ocr_config: dict = None) -> str:
    import pytesseract
    from PIL import Image, ImageFilter, ImageOps

    img = Image.open(filepath)
    cfg = ocr_config or {}

    if cfg.get("preprocess_enabled", True):
        img = _preprocess_image(img, cfg.get("threshold", 128))

    return pytesseract.image_to_string(img, lang="chi_sim+eng")


def _preprocess_image(img, threshold: int = 128):
    """OCR 预处理：灰度 → 对比度增强 → 二值化 → 去噪。"""
    from PIL import ImageFilter, ImageOps

    img = img.convert("L")  # 灰度
    img = ImageOps.autocontrast(img, cutoff=2)  # 自动对比度
    img = img.point(lambda p: 255 if p > threshold else 0)  # 二值化
    img = img.filter(ImageFilter.MedianFilter(3))  # 中值滤波去噪
    return img
