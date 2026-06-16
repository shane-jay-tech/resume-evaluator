"""调用 LLM 评估简历 —— 多岗位自动匹配 + 动态维度评估。

v2: 统一 LLMClient、输出校验、配置化路由规则。
"""

import json
import logging
import os
import time
from functools import wraps

from utils.llm_client import LLMClient, parse_json

logger = logging.getLogger(__name__)

# ── 默认维度（向后兼容无 dimensions 配置的岗位） ──
DEFAULT_DIMENSIONS = [
    {"name": "技能匹配", "key": "skill_match", "weight": 0.3},
    {"name": "经验匹配", "key": "experience_match", "weight": 0.3},
    {"name": "学历匹配", "key": "education_match", "weight": 0.1},
    {"name": "综合评价", "key": "overall", "weight": 0.3},
]

# ── 必需的 JSON 字段（用于校验 LLM 输出） ──
MATCH_REQUIRED_FIELDS = ["position_index", "position_name", "confidence", "reason"]
MULTI_MATCH_REQUIRED_FIELDS = ["rankings", "best_position", "best_index", "confidence"]
EVAL_REQUIRED_FIELDS = [
    "candidate_name", "matched_position", "match_score",
    "score_reasoning", "verdict", "summary", "dimensions",
    "highlights", "risks", "interview_suggestions",
    "hard_gate_checks", "cross_check",  # v5: 三阶段字段
    "matching_evidence", "gaps", "tailored_questions",  # v5: 三段式展示
]

MATCH_SYSTEM = """你是一位经验丰富的招聘专家。请阅读候选人简历，判断他/她最适合以下哪个岗位。

重要：请仔细区分不同岗位的差异。如果候选人的技能和经验与所有岗位都不太匹配，请将 confidence 设为 "low"，不要勉强匹配。

只返回一个 JSON，不要输出其他内容：
{
  "position_index": 0,
  "position_name": "岗位名称",
  "confidence": "high/medium/low",
  "reason": "判断依据"
}

confidence 标准：
- high: 候选人技能和经验与岗位高度吻合
- medium: 有一定相关性但存在明显差异
- low: 候选人与所有岗位匹配度都不高，或简历内容不足以判断"""

MULTI_MATCH_SYSTEM = """你是一位经验丰富的招聘专家。以下有多个岗位共享同一简历来源，请阅读候选人简历，对每个岗位进行匹配度评分，选出最适合的一个。

重要：深入理解简历内容，对比每个岗位的JD要求，给出每个岗位的匹配度评分。

只返回一个 JSON，不要输出其他内容：
{
  "rankings": [
    {"position_name": "岗位A", "score": 85, "reason": "匹配理由：候选人的XX经验与岗位A的YY要求吻合…"},
    {"position_name": "岗位B", "score": 65, "reason": "匹配理由：…"}
  ],
  "best_position": "岗位A",
  "best_index": 0,
  "confidence": "high/medium/low"
}

评分规则：
- score 为 0-100 的匹配度分数
- 必须按 score 从高到低排序
- 每个 reason 必须具体，引用简历内容和岗位JD对比
- 如果候选人明显不适合某个岗位，score 应显著低于其他岗位"""


# ═══════════════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════════════

def _build_eval_system(dimensions: list, feedback_calibration: str = "") -> str:
    """根据岗位维度动态生成评估系统提示。v4: 支持外部模板 + 面试反馈校准。"""
    dim_names = "、".join(d["name"] for d in dimensions)
    dim_fields = []
    for d in dimensions:
        dim_fields.append(
            f'    "{d["key"]}": {{"score": 8, "comment": "详细评分理由：候选人在XX方面的具体表现为…与标准对比…加分项是…不足之处是…"}}'
        )
    dim_json = ",\n".join(dim_fields)

    # v4: 尝试从文件加载 prompt 模板（开发模式 + PyInstaller 打包兼容）
    template = None
    # 查找顺序：cwd → 资源目录
    search_dirs = [os.getcwd()]
    try:
        from utils.paths import get_resource_dir
        search_dirs.append(get_resource_dir())
    except ImportError:
        pass
    for base_dir in search_dirs:
        prompt_file = os.path.join(base_dir, "prompts", "system_prompt.md")
        if os.path.exists(prompt_file):
            try:
                with open(prompt_file, "r", encoding="utf-8") as f:
                    template = f.read()
                break
            except Exception:
                pass

    if template:
        system_prompt = template.replace("{dimensions}", dim_names).replace("{dimensions_json}", dim_json)
    else:
        # 内置后备模板
        system_prompt = f"""你是一位专业的 HR 简历评估专家。请根据提供的评估标准，对候选人简历进行深度评估。

评估维度：{dim_names}

评估规则：
1. 仔细阅读简历内容，提取候选人的关键信息
2. 对照评估标准中的每一个维度进行打分（1-10分），并对每一个评分给出详细理由
3. 给出综合匹配度评分（1-100分），并说明综合评分的计算逻辑
4. 注意岗位定位：如果标准中明确标明"执行岗/执行层（非管理岗）"，则管理经验不应作为加分项，重点评估独立执行和制作交付能力；有管理经验但缺乏实操能力的应降低评分
5. 区分「表面经验匹配」与「实际能力深度」：平台标签不等于能力强，需关注在岗时长、职责范围、独立产出复杂度

要求：
- 每个维度的 comment 必须详细，包含：候选人该维度的具体表现、与标准的对比分析、加分点和不足之处
- 评分理由要具体到简历中的实际内容，不要泛泛而谈
- 综合评分需要解释各维度权重如何计算得出

verdict 只能是：强烈推荐 / 推荐 / 待定 / 不推荐

请严格按照以下 JSON 格式返回（不要输出其他内容）：
{{
  "candidate_name": "候选人姓名",
  "matched_position": "岗位名称",
  "match_score": 85,
  "score_reasoning": "综合评分详细计算说明，列出各维度得分×权重=最终分数的计算过程",
  "verdict": "推荐",
  "summary": "一句话综合评价",
  "dimensions": {{
{dim_json}
  }},
  "highlights": ["亮点1", "亮点2"],
  "risks": ["风险点1"],
  "interview_suggestions": ["面试建议1", "面试建议2"]
}}"""

    # v5: 面试反馈校准已移至用户 prompt 的阶段二
    # _build_dimension_section() 负责注入，避免了 system+user 双重注入
    return system_prompt


def _get_dimensions(pos: dict) -> list:
    """获取岗位的评估维度配置。"""
    dims = pos.get("dimensions")
    if dims and isinstance(dims, list) and len(dims) > 0:
        result = []
        for i, d in enumerate(dims):
            name = d.get("name", f"维度{i+1}")
            key = d.get("key", f"dim_{i}")
            weight = d.get("weight", 0.25)
            result.append({"name": name, "key": key, "weight": weight})
        return result
    return DEFAULT_DIMENSIONS


def log_api_call(func):
    """装饰器：记录 API 调用耗时和结果。"""
    @wraps(func)
    def wrapper(*args, **kwargs):
        t0 = time.time()
        try:
            result = func(*args, **kwargs)
            elapsed = time.time() - t0
            logger.info("LLM 调用完成: %s (%.1fs)", func.__name__, elapsed)
            return result
        except Exception as e:
            elapsed = time.time() - t0
            logger.error("LLM 调用失败: %s (%.1fs) — %s", func.__name__, elapsed, e)
            raise
    return wrapper


def _normalize_score(score, max_val: int = 10) -> int:
    """归一化评分到合理范围。"""
    try:
        s = int(score)
        return max(1, min(max_val, s))
    except (TypeError, ValueError):
        return max_val // 2  # 默认中位数


# ═══════════════════════════════════════════════════════════
# 路由规则（配置驱动）
# ═══════════════════════════════════════════════════════════

def _build_routing_rules(candidates: list, positions: list, config: dict) -> str:
    """从岗位配置中提取路由规则，构建 prompt 指令。"""
    cand_names = [c["name"] for c in candidates]
    rules_text = ""

    # 遍历所有岗位的 routing_rules 配置
    for pos in positions:
        rules = pos.get("routing_rules", [])
        for rule in rules:
            source = rule.get("source_position", "")
            # 只有当 source 岗位也在候选列表中时才激活规则
            if source in cand_names:
                target = pos["name"]
                if target in cand_names:
                    conditions = []
                    if rule.get("max_years"):
                        conditions.append(f"{rule['max_years']}年以下")
                    if rule.get("required_background"):
                        conditions.append(f"{rule['required_background']}方向")
                    cond_str = " + ".join(conditions) if conditions else ""

                    rules_text += (
                        f"\n\n⚠️ 路由规则：当候选人来自{source}背景时，"
                        f"{target}仅考虑 {cond_str} {source}经验的候选人。"
                        f"不满足条件的{source}候选人应归入{source}岗位。"
                    )

    return rules_text


# ═══════════════════════════════════════════════════════════
# 岗位匹配
# ═══════════════════════════════════════════════════════════

def match_position_by_filename(filename: str, positions: list) -> dict:
    """根据文件名关键词匹配岗位。多个岗位命中同一关键词时返回 None 以触发 LLM 判断。"""
    fname_lower = filename.lower()
    matches = []

    for idx, pos in enumerate(positions):
        if pos.get("enabled") is False:
            continue
        name = pos["name"]
        core = name.split("（")[0].split("(")[0].strip() if "（" in name or "(" in name else name

        keywords = [name, core]
        aliases = pos.get("aliases", [])
        if aliases:
            keywords.extend(aliases)

        for kw in keywords:
            if kw.lower() in fname_lower:
                matches.append((idx, name, kw))
                break

    if len(matches) == 0:
        return None

    if len(matches) == 1:
        idx, name, kw = matches[0]
        return {
            "position_index": idx,
            "position_name": name,
            "confidence": "high",
            "reason": f"文件名匹配: 关键词「{kw}」命中",
            "matched_by": "filename",
        }

    logger.info("文件名「%s」命中 %d 个岗位 (%s)，交给 LLM 评分判断",
                filename, len(matches), ", ".join(m[1] for m in matches))
    return {
        "position_index": -1,
        "multi_match": True,
        "candidates": [{"index": idx, "name": name} for idx, name, kw in matches],
        "matched_by": "filename_multi",
        "confidence": "pending",
    }


def match_position(resume_text: str, positions: list, config: dict, filename: str = "") -> dict:
    """判断简历所属岗位：优先文件名匹配，多岗位冲突时 LLM 评分选最优。"""
    llm = LLMClient(config)

    if filename:
        result = match_position_by_filename(filename, positions)
        if result:
            if not result.get("multi_match"):
                return result
            return _match_by_scoring(resume_text, result["candidates"], positions, llm, config)

    active_positions = [p for p in positions if p.get("enabled") is not False]
    if len(active_positions) == 1:
        p = active_positions[0]
        idx = positions.index(p)
        return {
            "position_index": idx,
            "position_name": p["name"],
            "confidence": "high",
            "reason": "仅有一个启用的岗位",
            "matched_by": "fallback",
        }

    pos_desc = "\n".join(
        f"{positions.index(p)}. {p['name']}: {p.get('experience', '')}, 必备: {', '.join(p.get('must_have', []))}"
        for p in active_positions
    )

    content = llm.chat(
        [
            {"role": "system", "content": MATCH_SYSTEM},
            {"role": "user", "content": f"岗位列表：\n{pos_desc}\n\n候选人简历：\n{resume_text[:3000]}"},
        ],
        temperature=0.2,
        max_tokens=512,
        validate_json=MATCH_REQUIRED_FIELDS,
    )
    result = parse_json(content)
    result["matched_by"] = "llm"

    if result.get("confidence") == "low":
        result["needs_review"] = True

    return result


def _match_by_scoring(
    resume_text: str,
    candidates: list,
    positions: list,
    llm: LLMClient,
    config: dict,
) -> dict:
    """多个岗位共享简历池时：LLM 对每个岗位评分，选最高分。"""
    cand_descs = []
    for c in candidates:
        pos = positions[c["index"]]
        desc = f"{c['index']}. {pos['name']}\n"
        desc += f"   经验要求: {pos.get('experience', '未指定')}\n"
        desc += f"   必备条件: {'; '.join(pos.get('must_have', []))}\n"
        desc += f"   加分项: {'; '.join(pos.get('nice_to_have', []))}\n"
        if pos.get('other_requirements'):
            desc += f"   其他要求: {pos['other_requirements']}"
        cand_descs.append(desc)

    pos_desc = "\n\n".join(cand_descs)

    # 配置驱动的路由规则
    routing_rules = _build_routing_rules(candidates, positions, config)
    if routing_rules:
        pos_desc += routing_rules

    content = llm.chat(
        [
            {"role": "system", "content": MULTI_MATCH_SYSTEM},
            {"role": "user", "content": f"候选岗位：\n\n{pos_desc}\n\n候选人简历：\n{resume_text[:3000]}"},
        ],
        temperature=0.2,
        max_tokens=1024,
        validate_json=MULTI_MATCH_REQUIRED_FIELDS,
    )
    result = parse_json(content)

    best_idx = result.get("best_index", 0)
    if 0 <= best_idx < len(candidates):
        best = candidates[best_idx]
    else:
        best = candidates[0]

    rankings = result.get("rankings", [])
    reason_lines = []
    for r in rankings:
        reason_lines.append(f"[{r.get('score', '?')}分] {r['position_name']}: {r.get('reason', '')}")

    return {
        "position_index": best["index"],
        "position_name": best["name"],
        "confidence": result.get("confidence", "medium"),
        "reason": " | ".join(reason_lines) if reason_lines else "LLM 评分匹配",
        "matched_by": "llm_scoring",
        "multi_scores": rankings,
    }


# ═══════════════════════════════════════════════════════════
# 评估 Prompt
# ═══════════════════════════════════════════════════════════
# v5: 三阶段 prompt 构建
# ═══════════════════════════════════════════════════════════

def _build_hard_gate_section(pos: dict) -> str:
    """阶段一：硬性门槛检查。

    优先使用 hard_gates 结构化字段；若不存在则降级为 must_have 文本列表，
    默认每条不满足时「评分上限60」。
    """
    hard_gates = pos.get("hard_gates")
    must_have = pos.get("must_have", [])

    lines = [
        "【阶段一：硬性门槛检查 —— 最先执行】",
        "",
        "逐条判断以下条件。任一条不满足，综合评分上限将被自动限制。",
        "请在进入阶段二评分前，先完成所有条件的判断。",
        "",
    ]

    if hard_gates and isinstance(hard_gates, list) and len(hard_gates) > 0:
        lines.append("硬性条件清单：")
        for i, gate in enumerate(hard_gates, 1):
            cond = gate.get("condition", str(gate))
            cons = gate.get("consequence", "评分上限60")
            lines.append(f"{i}. {cond}")
            lines.append(f"   → 不满足则：{cons}")
    elif must_have:
        lines.append("硬性条件清单（从岗位必备条件中提取）：")
        for i, item in enumerate(must_have, 1):
            lines.append(f"{i}. {item}")
            lines.append(f"   → 不满足则：评分上限60")
    else:
        return ""  # 无硬性条件，跳过阶段一

    lines.append("")
    lines.append("请逐条判断（满足/不满足），引用简历证据。")
    lines.append("结果填入 hard_gate_checks 字段。")
    return "\n".join(lines)


def _build_dimension_section(pos: dict, feedback_calibration: str = "") -> str:
    """阶段二：分维度证据优先评分。"""
    dimensions = _get_dimensions(pos)
    scoring = pos.get("scoring", {})

    lines = [
        "【阶段二：分维度证据优先评分】",
        "",
        "强制规则：对每个维度，先列证据（引用简历原文）→ 再列差距 → 最后评分。",
        "禁止先列差距后列证据 —— 这会引入消极偏向。",
        "",
        "【岗位信息】",
        f"岗位名称：{pos['name']}",
        f"学历要求：{pos.get('education', '未指定')}",
        f"经验要求：{pos.get('experience', '未指定')}",
    ]

    must_have = pos.get("must_have", [])
    nice_to_have = pos.get("nice_to_have", [])
    if must_have:
        lines.append(f"必备条件：{'；'.join(must_have)}")
    if nice_to_have:
        lines.append(f"加分项：{'；'.join(nice_to_have)}")
    if pos.get("other_requirements"):
        lines.append(f"其他要求：{pos['other_requirements']}")

    lines.append("")
    lines.append("【评估维度与评分标准】")
    lines.append("对以下维度严格按「①证据 → ②差距 → ③评分」评估：")

    for d in dimensions:
        key = d["key"]
        desc = scoring.get(key, f"综合评估{d['name']}")
        lines.append(f"\n### {d['name']}（权重 {d['weight']}）")
        lines.append(f"标准：{desc}")
        lines.append("步骤：①从简历中找匹配证据（至少2条）→ ②列差距 → ③综合评分（1-10）")

    if feedback_calibration:
        lines.append(f"\n---\n⚠️ 历史面试反馈校准：{feedback_calibration}")

    return "\n".join(lines)


def _build_cross_validation_section() -> str:
    """阶段三：交叉自检。"""
    return """【阶段三：交叉自检 —— 输出前必须完成】

1. 自洽性：各维度评分是否矛盾？如有矛盾修正并记录
2. 消极偏向：是否充分识别优势？列出3+显著优势；信息不足的维度是否被过度惩罚
3. 门槛一致性：阶段一不满足的条件是否在总分中体现？全部满足是否仍被无故压低

自检结果填入 cross_check 字段。修正内容在 score_reasoning 中注明。"""


def build_eval_prompt(resume_text: str, pos: dict, reference_text: str = "",
                      feedback_calibration: str = "") -> str:
    """v5: 三阶段结构化评估 prompt。

    阶段一：硬性门槛检查 → 阶段二：证据优先评分 → 阶段三：交叉自检。
    每个阶段之间用明确分隔符区隔，确保 LLM 按顺序处理。
    """
    sections = []

    # 阶段一：硬性门槛检查
    gate_section = _build_hard_gate_section(pos)
    if gate_section:
        sections.append(gate_section)

    # 阶段二：分维度证据优先评分
    sections.append(_build_dimension_section(pos, feedback_calibration))

    # 标杆简历参考（可选）
    if reference_text:
        sections.append(
            "【标杆简历参考】\n"
            "以下为该岗位理想候选人的标杆简历，请将候选人与此标杆对标：\n"
            f"{reference_text}"
        )

    # 候选人简历
    sections.append(f"【候选人简历】\n{resume_text}")

    # 阶段三：交叉自检
    sections.append(_build_cross_validation_section())

    return "\n\n---\n\n".join(sections)


# ═══════════════════════════════════════════════════════════
# v5: 简历结构化提取（帮助评估 LLM 精准定位关键信息）
# ═══════════════════════════════════════════════════════════

def extract_resume_structure(resume_text: str, config: dict) -> dict:
    """从简历文本中提取结构化字段，辅助主评估 Prompt 精准定位信息。

    对标 Moka AI 简历解析。提取：年限、技能、公司、学历、关键成果。
    """
    llm = LLMClient(config)
    prompt = f"""从以下简历中提取结构化信息。只返回 JSON，不要其他内容：
{{
  "years_of_experience": 5,
  "education_level": "本科/硕士/大专/不限",
  "tech_stack": ["技能1", "技能2"],
  "companies": ["公司名1", "公司名2"],
  "recent_role": "最近职位",
  "key_achievements": ["成果1", "成果2"]
}}

简历内容：
{resume_text[:3000]}"""

    try:
        content = llm.chat(
            [{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=512,
            retries=0,
        )
        result = parse_json(content)
        # 确保字段存在
        for key in ("years_of_experience", "education_level", "tech_stack",
                     "companies", "recent_role", "key_achievements"):
            if key not in result:
                result[key] = 0 if key == "years_of_experience" else ([] if key in ("tech_stack", "companies", "key_achievements") else "")
        return result
    except Exception:
        return {"years_of_experience": 0, "education_level": "", "tech_stack": [],
                "companies": [], "recent_role": "", "key_achievements": []}


# ═══════════════════════════════════════════════════════════
# 评估入口
# ═══════════════════════════════════════════════════════════

def evaluate(resume_text: str, config: dict, references: dict = None, filename: str = "",
             store=None) -> dict:
    """先匹配岗位（文件名优先），再用对应标准评估（含标杆对比）。

    返回的 dict 包含评估结果和元数据（model_name、prompt_version 等）。
    v5: store 参数用于查询面试反馈校准数据。
    """
    positions = config["positions"]
    llm = LLMClient(config)

    # 1. 岗位匹配
    match = match_position(resume_text, positions, config, filename)
    idx = match.get("position_index", 0)
    if idx < 0 or idx >= len(positions):
        idx = 0  # LLM 返回无效索引时回退到第一个岗位
    pos = positions[idx]

    # LLM 低置信度匹配 → 跳过评估
    if match.get("needs_review"):
        return {
            "candidate_name": "未知",
            "matched_position": pos["name"] if idx < len(positions) else "未识别",
            "match_score": 0,
            "verdict": "岗位待确认",
            "summary": f"LLM 岗位匹配置信度为 low，文件名「{filename}」未命中已知岗位。请确认候选人目标岗位后重新评估。LLM 推测: {match.get('reason', '')}",
            "dimensions": {},
            "highlights": [],
            "risks": [f"岗位匹配不明确，LLM 置信度为 low", f"文件名未命中任何已知岗位关键词: {filename}"],
            "interview_suggestions": [],
            "hard_gate_checks": [],
            "cross_check": {},
            "matching_evidence": [],
            "gaps": [],
            "tailored_questions": [],
            "match_confidence": "low",
            "match_method": match.get("matched_by", "llm"),
            "skipped": True,
        }

    # 2. 标杆简历
    ref_text = ""
    if references and pos["name"] in references:
        ref_text = references[pos["name"]]

    # 3. 动态维度评估
    dimensions = _get_dimensions(pos)

    # v5: 查询面试反馈校准数据
    feedback_calibration = ""
    if store:
        try:
            fb_data = store.get_feedback_for_calibration(pos["name"], limit=10)
            if fb_data:
                overrated = sum(1 for f in fb_data if f.get("accuracy") == "overrated")
                underrated = sum(1 for f in fb_data if f.get("accuracy") == "underrated")
                accurate = sum(1 for f in fb_data if f.get("accuracy") == "accurate")
                avg_bias = 0
                biases = []
                for f in fb_data:
                    try:
                        bias = f.get("system_score", 0) - f.get("interview_score", 0) * 10
                        biases.append(bias)
                    except Exception:
                        pass
                if biases:
                    avg_bias = round(sum(biases) / len(biases), 1)
                feedback_calibration = (
                    f"该岗位历史面试反馈（共{len(fb_data)}条）：系统评分偏高{overrated}条、"
                    f"准确{accurate}条、偏低{underrated}条，"
                    f"平均偏差{avg_bias}分（正=系统偏高，负=系统偏低）。"
                    f"请参考此数据调整评分倾向，但不要盲目套用。"
                )
        except Exception:
            pass

    eval_system = _build_eval_system(dimensions, feedback_calibration)

    content = llm.chat(
        [
            {"role": "system", "content": eval_system},
            {"role": "user", "content": build_eval_prompt(resume_text, pos, ref_text, feedback_calibration)},
        ],
        temperature=0.3,
        max_tokens=4096,
        validate_json=EVAL_REQUIRED_FIELDS,
    )
    result = parse_json(content)

    # 归一化 + 校验
    if "match_score" in result:
        result["match_score"] = _normalize_score(result["match_score"], max_val=100)
    for dim in result.get("dimensions", {}).values():
        if "score" in dim:
            dim["score"] = _normalize_score(dim["score"], max_val=10)

    # v5: 确保三段式字段存在且格式正确
    for field in ["matching_evidence", "gaps", "tailored_questions"]:
        if field not in result or not isinstance(result.get(field), list):
            result[field] = []

    # 注入元信息
    result["matched_position"] = pos["name"]
    result["match_confidence"] = match.get("confidence", "unknown")
    result["match_method"] = match.get("matched_by", "llm")
    result["_meta"] = {
        "model_name": llm.model_name,
        "prompt_version": "v5-three-stage",
        "position_name": pos["name"],
        "evaluated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }

    return result


# ═══════════════════════════════════════════════════════════
# 候选排序
# ═══════════════════════════════════════════════════════════

def rank_candidates(results: list, position: str, config: dict) -> list:
    """AI 排序：调用 LLM 对候选人列表进行综合排名。"""
    if not results:
        return []

    llm = LLMClient(config)

    candidates_text = ""
    for i, r in enumerate(results):
        dims = r.get("dimensions", {})
        dim_summary = ", ".join(f"{k}: {v.get('score', '-')}/10" for k, v in dims.items())
        candidates_text += (
            f"[{i+1}] {r['candidate_name']} | {r['matched_position']} | "
            f"评分: {r['match_score']}/100 | {r['verdict']} | "
            f"维度: {dim_summary}\n"
            f"亮点: {', '.join(r.get('highlights', []))}\n"
            f"风险: {', '.join(r.get('risks', []))}\n\n"
        )

    prompt = f"""你是一位资深招聘专家。请根据以下候选人信息进行综合排序。

招聘岗位：{position}
排序标准：综合匹配度、技能契合度、经验匹配度、面试建议等因素。

候选人列表：
{candidates_text[:6000]}

请返回 JSON，按推荐优先级从高到低排列：
{{"rankings": [{{"index": 1, "reason": "排序理由"}}, ...]}}"""

    content = llm.chat(
        [{"role": "user", "content": prompt}],
        temperature=0.3,
        max_tokens=2048,
        validate_json=["rankings"],
    )
    result = parse_json(content)
    return result.get("rankings", [])


# ═══════════════════════════════════════════════════════════
# 标杆特征提取
# ═══════════════════════════════════════════════════════════

def extract_reference_features(resume_text: str, position_config: dict, config: dict) -> dict:
    """从简历文本中提取结构化标签：技术栈、经验年限、行业、学历、关键能力等。"""
    llm = LLMClient(config)
    prompt = f"""你是一位专业的简历解析专家。请从以下候选人简历中提取结构化信息。

岗位要求参考：
- 必须项：{"; ".join(position_config.get("must_have", []))}
- 加分项：{"; ".join(position_config.get("nice_to_have", []))}

请返回严格的 JSON 格式：
{{
  "tech_stack": ["技能1", "技能2"],
  "years": 3,
  "industry": ["行业1"],
  "education": "学历",
  "skills": ["关键能力1", "关键能力2"],
  "level": "执行层/管理层/技术专家"
}}

简历内容：
{resume_text[:4000]}"""

    content = llm.chat(
        [{"role": "user", "content": prompt}],
        temperature=0.2,
        max_tokens=1024,
        validate_json=["tech_stack", "years", "industry", "education", "skills", "level"],
    )
    return parse_json(content)


def compute_reference_similarity(candidate_features: dict, reference_features_list: list) -> dict:
    """计算候选人与标杆的多维相似度。"""
    if not reference_features_list:
        return {"overall": 0, "dimensions": {}, "best_match": None}

    def jaccard(a: list, b: list) -> float:
        sa, sb = set(a), set(b)
        if not sa and not sb:
            return 1.0
        if not sa or not sb:
            return 0.0
        return len(sa & sb) / len(sa | sb)

    best_sim = 0
    best_ref = None
    all_dims = []

    for ref in reference_features_list:
        rf = ref.get("features", {})
        tech_sim = jaccard(
            candidate_features.get("tech_stack", []),
            rf.get("tech_stack", []),
        )
        industry_sim = jaccard(
            candidate_features.get("industry", []),
            rf.get("industry", []),
        )
        skill_sim = jaccard(
            candidate_features.get("skills", []),
            rf.get("skills", []),
        )
        cand_years = candidate_features.get("years", 0) or 0
        ref_years = rf.get("years", 0) or 0
        exp_sim = 1.0 - min(abs(cand_years - ref_years) / max(cand_years, ref_years, 1), 1.0)

        edu_sim = 1.0 if (rf.get("education", "") and candidate_features.get("education", "")
                          and rf["education"][:2] == candidate_features["education"][:2]) else 0.5

        overall = (tech_sim * 0.3 + industry_sim * 0.2 + skill_sim * 0.25 + exp_sim * 0.15 + edu_sim * 0.1)

        dims = {
            "tech_stack": round(tech_sim, 2),
            "industry": round(industry_sim, 2),
            "skills": round(skill_sim, 2),
            "experience": round(exp_sim, 2),
            "education": round(edu_sim, 2),
        }
        all_dims.append({"ref_name": ref.get("candidate_name", ""), "dimensions": dims, "overall": round(overall, 2)})

        if overall > best_sim:
            best_sim = overall
            best_ref = ref.get("candidate_name", "")

    return {
        "overall": round(best_sim, 2),
        "best_match": best_ref,
        "per_reference": all_dims,
    }
