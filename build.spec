# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置 — 简历评估系统。

打包为无命令行窗口的可执行文件：
  Mac:   pyinstaller build.spec → dist/简历评估/
  Win:   pyinstaller build.spec → dist/简历评估/

分发: 将 dist/简历评估/ 文件夹打包为 ZIP 发给同事即可。
"""

import sys
from pathlib import Path

# 项目根目录（此 .spec 文件所在目录）
ROOT = Path(__file__).parent

# ── 入口脚本 ──────────────────────────────────────
MAIN_SCRIPT = str(ROOT / "main.py")

# ── 应用名称 ──────────────────────────────────────
APP_NAME = "简历评估"

# ── 隐藏导入（动态 import 的模块，PyInstaller 检测不到） ──
hiddenimports = [
    "watchdog.observers.polling",
    "pdfplumber",
    "docx",
    "PIL",
    "PIL.Image",
    "openai",
    "yaml",
    "dotenv",
]

# ── 数据文件（打包进可执行文件） ──
datas = [
    (str(ROOT / "config.yaml"), "."),
    (str(ROOT / "prompts"), "prompts"),
    (str(ROOT / "rules"), "rules"),
    # 修复：HTML 页面必须打包，否则打包版打开面板 404
] + [(str(f), ".") for f in sorted(ROOT.glob("*.html"))] + (
    [(str(ROOT / "vendor"), "vendor")] if (ROOT / "vendor").is_dir() else []
)

# ── 排除模块（减小包体积） ──
excludes = [
    "tkinter",
    "matplotlib",
    "numpy",
    "scipy",
    "pandas",
    "jedi",
    "IPython",
    "notebook",
    "sphinx",
]

# ── Analysis ──────────────────────────────────────
a = Analysis(
    [MAIN_SCRIPT],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

# ── PYZ ──────────────────────────────────────────
pyz = PYZ(a.pure)

# ── EXE (单文件入口) ──────────────────────────────
# console=False: Windows 不显示命令行窗口
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

# ── COLLECT (收集所有文件到目录) ──────────────────
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name=APP_NAME,
)

# ── 额外说明 ──────────────────────────────────────
# 打包结果在 dist/{APP_NAME}/ 目录下。
# 同事解压 ZIP 后双击「简历评估」(Mac) 或「简历评估.exe」(Win) 即可。
#
# Windows 特殊注意:
#   1. Tesseract OCR 需手动安装或放入同目录 tesseract/ 文件夹
#   2. 打包前执行: pip install pyinstaller
#   3. 打包命令: pyinstaller build.spec
#
# Mac 特殊注意:
#   1. 打包后 .app 需签名: codesign --force --deep --sign - dist/简历评估/简历评估
#   2. Tesseract 需通过 brew install tesseract 安装（系统级，不打包）
