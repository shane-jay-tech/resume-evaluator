"""配置加载：YAML 文件 + .env 环境变量。"""

import os
import logging

logger = logging.getLogger(__name__)


def load_config(config_path: str = "config.yaml") -> dict:
    """加载 YAML 配置，.env 中的环境变量覆盖敏感字段。"""
    import yaml

    if not os.path.exists(config_path):
        raise FileNotFoundError(f"配置文件不存在: {config_path}")

    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    # .env 覆盖 LLM 密钥
    _load_dotenv()

    llm = config.get("llm", {})
    api_key_env = llm.get("api_key_env", "DEEPSEEK_API_KEY")
    api_key = os.getenv(api_key_env, "")
    if api_key:
        llm["api_key"] = api_key
    else:
        logger.warning("未找到 API Key (环境变量 %s)", api_key_env)

    return config


def _load_dotenv():
    """加载 .env 文件（python-dotenv 可选依赖）。"""
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass


def save_config(config: dict, config_path: str = "config.yaml"):
    """原子写入配置：先写临时文件，成功后再 rename 替换原文件。"""
    import yaml
    import copy

    # 深拷贝避免改动原始 config
    data = copy.deepcopy(config)

    # 移除 llm.api_key —— 密钥通过 .env 管理，不写入文件
    if "llm" in data and "api_key" in data["llm"]:
        del data["llm"]["api_key"]

    # v5: 原子写入 —— 写入临时文件成功后 rename 替换，防止并发写损坏
    tmp_path = config_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
    os.replace(tmp_path, config_path)  # 原子操作（macOS/Linux）

    logger.info("配置已保存到 %s", config_path)


def get_auth_token() -> str:
    return os.getenv("AUTH_TOKEN", "")
