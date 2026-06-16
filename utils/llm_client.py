"""统一 LLM 客户端封装。

集中处理：超时、重试、指数退避、响应校验、token 统计、错误分类。
所有模块通过此客户端调用 LLM，不再直接操作 OpenAI SDK。
"""

import json
import logging
import time
from typing import Any

from openai import OpenAI

logger = logging.getLogger(__name__)


class LLMClient:
    """封装 LLM 调用的统一客户端。"""

    def __init__(self, config: dict):
        llm = config["llm"]
        self.model = llm.get("model", "deepseek-chat")
        self._client = OpenAI(
            api_key=llm.get("api_key"),
            base_url=llm.get("base_url"),
            timeout=llm.get("timeout", 300.0),  # 默认为评估任务留足时间
        )
        self.max_tokens = llm.get("max_tokens", 4096)
        self.temperature = llm.get("temperature", 0.3)
        self.max_retries = llm.get("max_retries", 2)

    # ── 公开方法 ──────────────────────────────────────

    def chat(
        self,
        messages: list[dict],
        temperature: float | None = None,
        max_tokens: int | None = None,
        retries: int | None = None,
        timeout: float | None = None,
        validate_json: dict | None = None,
    ) -> str:
        """非流式对话，返回文本。

        Args:
            messages: 消息列表
            temperature: 温度（默认用配置）
            max_tokens: 最大 token（默认用配置）
            retries: 重试次数（默认用配置）
            timeout: 超时秒数（默认 120）
            validate_json: 如果提供，调用后验证响应 JSON 包含的字段列表

        Returns:
            LLM 响应文本

        Raises:
            RuntimeError: 所有重试均失败
        """
        temp = temperature if temperature is not None else self.temperature
        mt = max_tokens if max_tokens is not None else self.max_tokens
        attempts = (retries if retries is not None else self.max_retries) + 1
        timeout_val = timeout or 300.0  # 默认 300s（评估任务可能较长）

        last_error: Exception | None = None

        for attempt in range(attempts):
            try:
                resp = self._client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=temp,
                    max_tokens=mt,
                    timeout=timeout_val,
                )
                content = resp.choices[0].message.content or ""

                # JSON 校验
                if validate_json:
                    parsed = _parse_json(content)
                    missing = [f for f in validate_json if f not in parsed]
                    if missing:
                        raise ValueError(
                            f"LLM 响应缺少字段: {missing}。响应: {content[:300]}"
                        )

                # Token 统计
                if resp.usage:
                    logger.debug(
                        "LLM: in=%d out=%d model=%s",
                        resp.usage.prompt_tokens,
                        resp.usage.completion_tokens,
                        resp.model,
                    )

                return content

            except Exception as e:
                last_error = e
                if attempt < attempts - 1:
                    wait = 2 ** attempt
                    logger.warning(
                        "LLM 调用失败，%ds 后重试 (%d/%d): %s",
                        wait, attempt + 1, attempts - 1, e,
                    )
                    time.sleep(wait)

        raise RuntimeError(f"LLM 调用失败（已重试 {attempts - 1} 次）: {last_error}")

    def call_json(
        self,
        messages: list[dict],
        required_fields: list[str] | None = None,
        **kwargs,
    ) -> dict:
        """调用 LLM 并返回解析后的 JSON。

        Args:
            messages: 消息列表
            required_fields: 必需的 JSON 字段列表
            **kwargs: 传递给 chat() 的其他参数

        Returns:
            解析后的 dict

        Raises:
            ValueError: JSON 解析失败或缺少必需字段
        """
        kwargs["validate_json"] = required_fields
        content = self.chat(messages, **kwargs)
        result = _parse_json(content)

        if required_fields:
            missing = [f for f in required_fields if f not in result]
            if missing:
                raise ValueError(f"LLM JSON 响应缺少字段: {missing}")

        return result

    @property
    def model_name(self) -> str:
        return self.model


# ═══════════════════════════════════════════════════════════
# JSON 解析工具（从 evaluator.py 提取，统一复用）
# ═══════════════════════════════════════════════════════════


def _parse_json(raw: str) -> dict:
    """从 LLM 响应中提取 JSON，处理 markdown 代码块包裹。"""
    raw = raw.strip()
    # 去掉 markdown ```json ... ``` 包裹
    if raw.startswith("```"):
        lines = raw.split("\n")
        # 移除第一行 ``` 和最后一行 ```
        if lines[-1].strip() == "```":
            raw = "\n".join(lines[1:-1])
        else:
            raw = "\n".join(lines[1:])

    # 找到第一个 JSON 对象/数组
    first_brace = raw.find("{")
    first_bracket = raw.find("[")
    if first_brace == -1 and first_bracket == -1:
        raise ValueError(f"响应中未找到 JSON: {raw[:200]}")

    if first_brace != -1 and (first_bracket == -1 or first_brace < first_bracket):
        start = first_brace
    else:
        start = first_bracket

    raw = raw[start:]

    # 找到 JSON 的结束位置（处理 trailing 内容）
    decoder = json.JSONDecoder()
    try:
        result, end = decoder.raw_decode(raw)
        return result
    except json.JSONDecodeError:
        raise ValueError(f"无法解析 JSON: {raw[:200]}")


def parse_json(raw: str) -> dict:
    """公开的 JSON 解析函数，供其他模块复用。"""
    return _parse_json(raw)
