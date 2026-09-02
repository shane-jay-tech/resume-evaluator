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

    # .env 覆盖 LLM 配置（支持中转站 API）
    _load_dotenv()

    llm = config.get("llm", {})
    api_key_env = llm.get("api_key_env", "LLM_API_KEY")
    api_key = os.getenv(api_key_env, "")
    # 兼容旧版 DEEPSEEK_API_KEY 变量名
    if not api_key and api_key_env == "LLM_API_KEY":
        api_key = os.getenv("DEEPSEEK_API_KEY", "")
    if api_key:
        llm["api_key"] = api_key
    else:
        logger.warning("未找到 API Key (环境变量 %s)", api_key_env)

    # 中转站 API 支持：环境变量可覆盖 base_url 和 model
    env_base_url = os.getenv("LLM_BASE_URL", "")
    if env_base_url:
        llm["base_url"] = env_base_url
        logger.info("使用自定义 API 地址: %s", env_base_url.split("/")[2] if "/" in env_base_url else env_base_url)

    env_model = os.getenv("LLM_MODEL", "")
    if env_model:
        llm["model"] = env_model
        logger.info("使用自定义模型: %s", env_model)

    return config


def _parse_env_file(path: str):
    """轻量 .env 解析器（无 python-dotenv 依赖时兜底）。

    支持 KEY=VALUE、引号包裹、注释行、export 前缀。
    已存在的环境变量不被覆盖（与 dotenv 默认行为一致）。
    """
    if not path or not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[7:].strip()
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                    value = value[1:-1]
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError:
        pass


def _load_dotenv():
    """加载 .env 文件：优先用户数据目录，再回退当前目录。

    python-dotenv 是可选依赖；未安装时用内置解析器兜底，
    保证首次设置写入的 .env 在重启后一定生效。
    """
    from utils.paths import get_env_path
    env_candidates = [get_env_path(), os.path.join(os.getcwd(), ".env")]
    seen = set()
    try:
        from dotenv import load_dotenv
        for p in env_candidates:
            p = os.path.abspath(p)
            if p not in seen and os.path.exists(p):
                seen.add(p)
                load_dotenv(p)
    except ImportError:
        for p in env_candidates:
            p = os.path.abspath(p)
            if p not in seen:
                seen.add(p)
                _parse_env_file(p)


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
