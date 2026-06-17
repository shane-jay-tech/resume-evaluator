r"""跨平台路径处理。

统一 macOS 和 Windows 的路径差异：
- 监控目录: ~/Downloads (Mac) / %USERPROFILE%\Downloads (Win)
- 数据目录: 可执行文件同级的 data/
- 配置目录: 可执行文件同级
"""

import os
import sys
from pathlib import Path


def get_platform() -> str:
    """返回 'darwin' (macOS) 或 'win32' (Windows)。"""
    return sys.platform


def is_windows() -> bool:
    return sys.platform == "win32"


def is_macos() -> bool:
    return sys.platform == "darwin"


def get_downloads_dir() -> str:
    """获取用户下载目录。"""
    home = Path.home()
    if is_windows():
        # Windows: %USERPROFILE%\Downloads
        downloads = home / "Downloads"
    else:
        # macOS / Linux: ~/Downloads
        downloads = home / "Downloads"
    return str(downloads)


def get_resource_dir() -> str:
    """获取只读资源目录（配置文件、提示词、规则等）。

    PyInstaller 打包后，资源文件在 sys._MEIPASS 中。
    开发模式下就是项目根目录。
    """
    if getattr(sys, 'frozen', False):
        return sys._MEIPASS
    else:
        return str(Path(__file__).resolve().parent.parent)


def get_user_dir() -> str:
    """获取可写用户目录（数据、日志、.env 等）。

    使用 OS 标准应用数据目录，确保 .app bundle 放在 /Applications 也能写。
    - macOS: ~/Library/Application Support/简历评估/
    - Windows: %APPDATA%/简历评估/
    - Linux: ~/.local/share/简历评估/
    """
    home = Path.home()
    if sys.platform == "darwin":
        base = home / "Library" / "Application Support" / "简历评估"
    elif sys.platform == "win32":
        base = Path(os.getenv("APPDATA", str(home / "AppData" / "Roaming"))) / "简历评估"
    else:
        base = home / ".local" / "share" / "简历评估"
    base.mkdir(parents=True, exist_ok=True)
    return str(base)


def get_data_dir() -> str:
    """获取数据目录。"""
    return str(Path(get_user_dir()) / "data")


def get_config_path() -> str:
    """获取配置文件路径（优先用户目录，回退资源目录）。"""
    user_config = str(Path(get_user_dir()) / "config.yaml")
    if Path(user_config).exists():
        return user_config
    return str(Path(get_resource_dir()) / "config.yaml")


def get_env_path() -> str:
    """获取 .env 文件路径。"""
    return str(Path(get_user_dir()) / ".env")


def get_resumes_dir() -> str:
    """获取简历存储目录。"""
    return str(Path(get_data_dir()) / "resumes")


def get_logs_dir() -> str:
    """获取日志目录。"""
    return str(Path(get_user_dir()) / "logs")


def ensure_dirs():
    """确保所有必要目录存在。"""
    for d in [get_data_dir(), get_resumes_dir(), get_logs_dir()]:
        Path(d).mkdir(parents=True, exist_ok=True)
