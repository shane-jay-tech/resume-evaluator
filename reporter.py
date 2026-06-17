"""生成 Markdown 评估报告和维护 results.json（向后兼容层）。"""

import json
import os
from datetime import datetime
from pathlib import Path


def generate_markdown(result: dict, resume_filename: str, report_dir: str) -> str:
    """生成 Markdown 报告文件（支持动态维度）。"""
    os.makedirs(report_dir, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = Path(resume_filename).stem
    report_path = os.path.join(report_dir, f"{safe_name}_评估报告_{ts}.md")

    verdict_emoji = {
        "强烈推荐": "⭐",
        "推荐": "✅",
        "待定": "⚠️",
        "不推荐": "❌",
    }
    emoji = verdict_emoji.get(result.get("verdict", ""), "")

    dims = result.get("dimensions", {})
    highlights = result.get("highlights", [])
    risks = result.get("risks", [])
    suggestions = result.get("interview_suggestions", [])
    portfolio_links = result.get("portfolio_links", [])

    md = f"""# 📋 简历评估报告

**候选人**: {result.get('candidate_name', '未知')}
**匹配岗位**: {result.get('matched_position', '-')}
**简历文件**: {resume_filename}
**评估时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

---

## 综合评分

| 项目 | 分数 |
|------|------|
| 综合匹配度 | **{result.get('match_score', '-')}/100** |
| 评估结论 | {emoji} {result.get('verdict', '-')} |

> {result.get('summary', '')}

---

## 评分理由

{result.get('score_reasoning', '无')}

---

## 各维度详细评分

| 维度 | 分数 | 评分理由 |
|------|------|------|
"""
    for key, dim in dims.items():
        name = dim.get("name", key) if isinstance(dim, dict) else key
        score = dim.get("score", "-") if isinstance(dim, dict) else dim
        comment = dim.get("comment", "") if isinstance(dim, dict) else ""
        md += f"| {name} | {score}/10 | {comment} |\n"

    md += """
---

## 🌟 亮点
"""
    for h in highlights:
        md += f"- {h}\n"

    md += "\n---\n\n## ⚠️ 风险点\n"
    for r in risks:
        md += f"- {r}\n"

    md += "\n---\n\n## 💡 面试建议\n"
    for s in suggestions:
        md += f"- {s}\n"

    if portfolio_links:
        md += "\n---\n\n## 🔗 作品集链接\n"
        for link in portfolio_links:
            md += f"- [{link}]({link})\n"

    md += "\n---\n\n*报告由 AI 自动生成，仅供参考*\n"

    with open(report_path, "w", encoding="utf-8") as f:
        f.write(md)

    return report_path


# ── 向后兼容层：以下函数供旧代码过渡使用 ──

def load_results(results_file: str) -> list:
    if not os.path.exists(results_file):
        return []
    with open(results_file, "r", encoding="utf-8") as f:
        return json.load(f)


def save_results(results: list, results_file: str):
    with open(results_file, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)


def add_result(result: dict, resume_filename: str, report_path: str, results_file: str) -> list:
    """向后兼容：写入 results.json（DataStore 已替代此功能）。"""
    results = load_results(results_file)

    verdict = result.get("verdict", "-")
    score = int(result.get("match_score", 0))

    results.append({
        "candidate_name": result.get("candidate_name", "未知"),
        "matched_position": result.get("matched_position", "-"),
        "match_score": score,
        "verdict": verdict,
        "original_verdict": verdict,
        "original_score": score,
        "summary": result.get("summary", ""),
        "score_reasoning": result.get("score_reasoning", ""),
        "dimensions": result.get("dimensions", {}),
        "highlights": result.get("highlights", []),
        "risks": result.get("risks", []),
        "interview_suggestions": result.get("interview_suggestions", []),
        "match_method": result.get("match_method", "unknown"),
        "match_confidence": result.get("match_confidence", "unknown"),
        "notes": "",
        "pipeline_status": "待筛选",
        "resume_file": resume_filename,
        "report_file": os.path.basename(report_path),
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })
    results.sort(key=lambda x: x["match_score"], reverse=True)

    save_results(results, results_file)
    return results
