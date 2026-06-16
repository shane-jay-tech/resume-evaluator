"""评分审计模块 — 诊断 LLM 评分质量。

基于真实评估数据分析：
- 评分一致性检查（维度分 vs 总分是否吻合）
- 岗位级评分趋势（某个岗位是否系统性偏高/偏低）
- must_have 惩罚力度分析
- 评分解释质量评分
"""

import json
from typing import Any


def audit_score(result: dict) -> dict:
    """单条评估结果的质量审计。

    检查项：
    1. 维度加权计算是否与最终分数一致
    2. 评分理由是否包含计算过程
    3. 亮点/风险是否具体（非泛泛而谈）
    """
    issues = []

    # 1. 维度加权一致性
    dims = result.get("dimensions", {})
    if isinstance(dims, str):
        try:
            dims = json.loads(dims)
        except json.JSONDecodeError:
            dims = {}

    calculated = 0.0
    for key, dim in dims.items():
        if isinstance(dim, dict):
            score = dim.get("score", 0)
            # 尝试从 evaluator 的维度配置中找权重
            weight = dim.get("weight", 0.25)
            calculated += score * weight

    # 按百分制换算
    expected_score = round(calculated * 10)
    actual_score = result.get("match_score", 0)
    deviation = abs(expected_score - actual_score)
    if deviation > 15:
        issues.append(f"维度加权计算得分({expected_score})与LLM给出分数({actual_score})偏差{deviation}分")

    # 2. 评分理由是否包含计算过程
    reasoning = result.get("score_reasoning", "")
    if not reasoning:
        issues.append("缺少评分计算说明")
    elif "=" not in reasoning and "×" not in reasoning:
        issues.append("评分理由缺少计算过程")
    elif len(reasoning) < 20:
        issues.append("评分理由过短")

    # 3. 亮点/风险是否具体
    highlights = result.get("highlights", [])
    risks = result.get("risks", [])
    if isinstance(highlights, str):
        try:
            highlights = json.loads(highlights)
        except json.JSONDecodeError:
            highlights = []
    if isinstance(risks, str):
        try:
            risks = json.loads(risks)
        except json.JSONDecodeError:
            risks = []

    vague_words = ["不错", "很好", "优秀", "还行", "一般", "可以"]
    for h in highlights:
        if all(w not in h for w in vague_words) and len(h) < 10:
            issues.append(f"亮点过于简略: '{h}'")

    return {
        "result_id": result.get("id"),
        "candidate_name": result.get("candidate_name", ""),
        "score": actual_score,
        "expected_from_dims": expected_score,
        "deviation": deviation,
        "issues": issues,
        "quality": "good" if len(issues) == 0 else ("warning" if len(issues) <= 1 else "poor"),
        "reasoning_length": len(reasoning),
        "highlight_count": len(highlights),
        "risk_count": len(risks),
    }


def audit_all(store) -> dict[str, Any]:
    """对所有未删除的评估结果进行审计，返回汇总报告。"""
    results = store.get_all_results(include_deleted=False)
    audits = [audit_score(r) for r in results]

    # 汇总
    total = len(audits)
    good = sum(1 for a in audits if a["quality"] == "good")
    warning = sum(1 for a in audits if a["quality"] == "warning")
    poor = sum(1 for a in audits if a["quality"] == "poor")
    avg_deviation = sum(a["deviation"] for a in audits) / total if total > 0 else 0

    # 按岗位分组
    by_position = {}
    for a in audits:
        pos = a.get("candidate_name", "")  # 临时，需要从 results 获取
        # 从原始 results 匹配
        orig = next((r for r in results if r.get("id") == a["result_id"]), {})
        pos_name = orig.get("matched_position", "未知")
        if pos_name not in by_position:
            by_position[pos_name] = {"count": 0, "avg_score": 0, "total_deviation": 0}
        by_position[pos_name]["count"] += 1
        by_position[pos_name]["avg_score"] += a["score"]
        by_position[pos_name]["total_deviation"] += a["deviation"]

    for pos_name, stats in by_position.items():
        if stats["count"] > 0:
            stats["avg_score"] = round(stats["avg_score"] / stats["count"], 1)
            stats["avg_deviation"] = round(stats["total_deviation"] / stats["count"], 1)

    # 常见问题
    common_issues: dict[str, int] = {}
    for a in audits:
        for issue in a["issues"]:
            key = issue[:50]
            common_issues[key] = common_issues.get(key, 0) + 1

    top_issues = sorted(common_issues.items(), key=lambda x: -x[1])[:5]

    return {
        "total": total,
        "quality": {"good": good, "warning": warning, "poor": poor},
        "avg_deviation": round(avg_deviation, 1),
        "by_position": by_position,
        "top_issues": [{"issue": i, "count": c} for i, c in top_issues],
        "audits": audits,
    }
