"""Claude 交叉校验模块 — 对主评估结果进行二次校验。

触发条件：match_score >= 85（强烈推荐级别）。
对比两个模型的评分差异，标注需要人工关注的案例。
"""

import json
import logging
import os
import time
from datetime import datetime

logger = logging.getLogger(__name__)

# Claude 校验 prompt
CLAUDE_VERIFY_SYSTEM = """你是一位严格的招聘质量审核专家。你的任务是**独立重新评估**一份候选人简历，然后与另一个 AI 系统的评估结果对比。

请仔细阅读：
1. 岗位 JD 和评分标准
2. 候选人简历
3. 另一个 AI 的评估结果（包括各维度分数和理由）

然后独立给出你的评估。请注意：
- 不要受另一个 AI 评分的影响，独立判断
- 如果另一个 AI 的评分偏高，请给出你认为合理的分数
- 如果候选人简历信息不足（如只有1页、缺少关键项目细节），应在评分时体现
- 如果候选人与岗位的核心要求有明显差距但另一个 AI 给了高分，请明确指出

只返回以下 JSON，不要其他内容：
{
  "match_score": 82,
  "verdict": "推荐",
  "score_reasoning": "你的综合评分计算逻辑…",
  "dimensions": {
    "dim1": {"score": 7, "comment": "该维度的独立评估…"},
    "dim2": {"score": 8, "comment": "…"}
  },
  "agreement_with_primary": "agree/overrated/underrated",
  "disagreement_reason": "如果与主评估不一致，说明原因：主评估在XX方面偏高/偏低，因为…",
  "key_observations": ["与你不同的重要发现1", "发现2"]
}"""


class CrossValidator:
    """使用 Claude 对 DeepSeek 评估结果进行交叉校验。"""

    def __init__(self, config: dict):
        cv_cfg = config.get("cross_validation", {})
        # api_key 优先从环境变量读取
        api_key_env = cv_cfg.get("api_key_env", "CLAUDE_API_KEY")
        api_key = cv_cfg.get("api_key") or os.getenv(api_key_env, "") or config.get("llm", {}).get("api_key")
        base_url = cv_cfg.get("base_url", "")
        self.model = cv_cfg.get("model", "claude-opus-4-6")
        self.enabled = cv_cfg.get("enabled", False)
        self.min_score_trigger = cv_cfg.get("min_score_trigger", 85)
        self.max_tokens = cv_cfg.get("max_tokens", 4096)
        self.timeout = cv_cfg.get("timeout", 180.0)
        self.max_retries = cv_cfg.get("max_retries", 2)

        if not api_key:
            self.enabled = False
            logger.warning("未配置 Claude API Key，交叉校验已禁用")
            self._client = None
            return

        if not base_url:
            self.enabled = False
            logger.warning("未配置 cross_validation.base_url，交叉校验已禁用")
            self._client = None
            return

        try:
            import anthropic
            import httpx
            client_kwargs = {"api_key": api_key, "timeout": httpx.Timeout(self.timeout)}
            if base_url:
                client_kwargs["base_url"] = base_url
            self._client = anthropic.Anthropic(**client_kwargs)
        except ImportError:
            self.enabled = False
            logger.warning("anthropic 库未安装，交叉校验已禁用")
            self._client = None
        except Exception as e:
            self.enabled = False
            logger.warning("Claude 客户端初始化失败: %s", e)
            self._client = None

    @property
    def is_enabled(self) -> bool:
        return self.enabled and self._client is not None

    def should_validate(self, match_score: int) -> bool:
        """判断是否需要触发交叉校验。

        v5: 上界（≥85）全部校验，下界（40-65）随机抽样 20%，
            防止漏掉「系统评低但实际很强」的候选人A类案例。
        """
        if not self.is_enabled:
            return False
        if match_score >= self.min_score_trigger:
            return True
        # 下界抽样：40-65分区间，20% 概率触发
        if 40 <= match_score <= 65:
            import random
            return random.random() < 0.20
        return False

    def validate(self, resume_text: str, primary_result: dict, pos: dict) -> dict | None:
        """执行 Claude 交叉校验。

        Args:
            resume_text: 简历原文
            primary_result: DeepSeek 的评估结果（含 dimensions、highlights、risks 等）
            pos: 岗位配置

        Returns:
            交叉校验结果 dict，失败返回 None
        """
        if not self.is_enabled:
            return None

        # 构建评估标准描述
        must_have_text = "\n".join(f"- {item}" for item in pos.get("must_have", [])[:5])
        nice_to_have_text = "\n".join(f"- {item}" for item in pos.get("nice_to_have", [])[:3])

        # 将主评估的维度和分数转换为 text
        dims = primary_result.get("dimensions", {})
        primary_dims_text = "\n".join(
            f"- {k}: {v.get('score', '?')}/10 — {v.get('comment', '无')[:200]}"
            for k, v in dims.items() if isinstance(v, dict)
        )

        user_prompt = f"""【岗位信息】
岗位名称：{pos['name']}
经验要求：{pos.get('experience', '未指定')}
必备条件：
{must_have_text}
加分项：
{nice_to_have_text}

【候选人简历】
{resume_text[:5000]}

【主评估的结果】
综合评分：{primary_result.get('match_score', 0)}/100
结论：{primary_result.get('verdict', '')}
评分理由：{primary_result.get('score_reasoning', '')}
各维度评分：
{primary_dims_text}

请你独立重新评估此候选人，并与上述主评估结果对比。"""

        # 指数退避重试
        attempts = self.max_retries + 1
        last_error = None

        for attempt in range(attempts):
            try:
                t0 = time.time()
                resp = self._client.messages.create(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    system=CLAUDE_VERIFY_SYSTEM,
                    messages=[{"role": "user", "content": user_prompt}],
                )
                content = resp.content[0].text if resp.content and hasattr(resp.content[0], "text") else ""
                elapsed = time.time() - t0
                logger.info("Claude 交叉校验完成 (%.1fs)", elapsed)

                # 解析 Claude 响应
                result = self._parse_response(content)
                if not result:
                    if attempt < attempts - 1:
                        wait = 2 ** attempt
                        logger.warning("Claude 响应解析失败，%ds 后重试 (%d/%d)", wait, attempt + 1, attempts)
                        time.sleep(wait)
                        continue
                    return None

                # 计算差异
                claude_score = result.get("match_score", 0)
                primary_score = primary_result.get("match_score", 0)
                score_diff = claude_score - primary_score
                abs_diff = abs(score_diff)

                # 协议分类
                if abs_diff <= 5:
                    agreement = "agree"
                elif abs_diff <= 15:
                    agreement = "minor_diff"
                else:
                    agreement = "major_diff"

                return {
                    "primary_score": primary_score,
                    "primary_verdict": primary_result.get("verdict", ""),
                    "claude_score": claude_score,
                    "claude_verdict": result.get("verdict", ""),
                    "score_diff": score_diff,
                    "agreement": agreement,
                    "claude_reasoning": result.get("score_reasoning", ""),
                    "claude_dimensions": result.get("dimensions", {}),
                    "agreement_with_primary": result.get("agreement_with_primary", agreement),
                    "disagreement_reason": result.get("disagreement_reason", ""),
                    "key_observations": result.get("key_observations", []),
                    "validated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
            except Exception as e:
                last_error = e
                if attempt < attempts - 1:
                    wait = 2 ** attempt
                    logger.warning(
                        "Claude 交叉校验失败，%ds 后重试 (%d/%d): %s",
                        wait, attempt + 1, attempts, e,
                    )
                    time.sleep(wait)

        logger.error("Claude 交叉校验全部失败（%d 次尝试）: %s", attempts, last_error)
        return None

    @staticmethod
    def _parse_response(content: str) -> dict | None:
        """解析 Claude 响应 JSON。"""
        content = content.strip()
        if content.startswith("```"):
            lines = content.split("\n")
            lines = [l for l in lines if not l.startswith("```")]
            content = "\n".join(lines)

        first_brace = content.find("{")
        if first_brace == -1:
            return None
        content = content[first_brace:]

        try:
            decoder = json.JSONDecoder()
            result, _ = decoder.raw_decode(content)
            return result
        except json.JSONDecodeError:
            logger.warning("Claude 响应 JSON 解析失败: %s", content[:200])
            return None
