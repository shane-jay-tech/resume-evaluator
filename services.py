"""共享服务层 — 消除 main.py ↔ app_routes.py 循环导入。

所有被多处引用的函数集中在此：
- process_resume: 简历处理流程
- add_to_pending: 加入待分配池
- _generate_pdf: PDF 报告生成
- load_references: 标杆简历加载
"""

import json
import os
import time
from datetime import datetime
from pathlib import Path

from parser import extract_text, file_hash
from evaluator import evaluate
from reporter import generate_markdown
from sse import SSEManager, EVENT_EVAL_COMPLETE, EVENT_PENDING_UPDATE, EVENT_STATUS_CHANGE
from metrics import metrics

# v4: 通知聚合（批量处理时不弹窗轰炸）
import threading as _threading
_notif_buffer: list[dict] = []
_notif_batch_start: float = 0.0
_notif_lock = _threading.Lock()
_NOTIF_BATCH_WINDOW = 5.0  # 5秒内的处理合并为一条通知


def _send_batched_notification(notify_func):
    """发送聚合通知（线程安全）。"""
    global _notif_buffer
    with _notif_lock:
        if not _notif_buffer:
            return
        buf = _notif_buffer[:]  # 快照
        _notif_buffer = []
    count = len(buf)
    star_count = sum(1 for n in buf if n.get("is_star"))
    if count == 1:
        n = buf[0]
        if notify_func:
            notify_func(n["title"], n["subtitle"], n["message"])
    else:
        verdicts = {}
        for n in buf:
            v = n.get("verdict", "其他")
            verdicts[v] = verdicts.get(v, 0) + 1
        v_text = "，".join(f"{k} {v}人" for k, v in verdicts.items())
        if notify_func:
            notify_func(
                f"简历评估完成 ({count}份)",
                f"强烈推荐 {star_count}人",
                f"{v_text}。点击面板查看详情"
            )


def _queue_notification(title: str, subtitle: str, message: str,
                        verdict: str = "", is_star: bool = False,
                        logger=None):
    """将通知加入缓冲区，批量发送（线程安全）。"""
    global _notif_buffer, _notif_batch_start
    import time as _time
    now = _time.time()
    with _notif_lock:
        if not _notif_buffer:
            _notif_batch_start = now
        _notif_buffer.append({
            "title": title, "subtitle": subtitle, "message": message,
            "verdict": verdict, "is_star": is_star,
        })
        should_flush = (now - _notif_batch_start > _NOTIF_BATCH_WINDOW or len(_notif_buffer) >= 5)
    return should_flush  # 需要刷新的信号


def process_resume(filepath: str, store, config: dict, references: dict,
                   sse_manager: SSEManager | None = None, logger=None,
                   notify_func=None):
    """处理单份简历：解析 → 匹配岗位 → 评估 → 报告 → 通知。"""
    fname = os.path.basename(filepath)
    if logger:
        logger.info("开始处理: %s", fname)
    start_time = time.time()

    # 先快速检查文件名（避免大文件 hash 计算）
    if store.is_resume_processed(fname):
        if logger:
            logger.info("已处理过（文件名），跳过: %s", fname)
        return
    fhash = file_hash(filepath)
    # v5: 原子 CAS —— 先插入 processed 记录，如果已存在则说明被抢先处理了
    if not store.try_add_processed(fhash):
        if logger:
            logger.info("已处理过（hash=抢先），加入重复检测队列: %s", fname)
        orig = store.get_result_by_filename(fname)
        store.add_duplicate(fhash, fname, filepath, orig)
        if sse_manager:
            sse_manager.broadcast("dup_update", {
                "resume_file": fname,
                "original_score": orig.get("match_score", 0) if orig else 0,
                "original_verdict": orig.get("verdict", "") if orig else "",
                "count": store.get_duplicate_count(),
            })
        return

    try:
        text = extract_text(filepath)
        if not text.strip():
            if logger:
                logger.warning("未能提取文本: %s", fname)
            store.add_processed(fhash)
            return

        if logger:
            logger.info("文本提取成功 (%d 字符)，开始 LLM 评估…", len(text))

        # v5: 结构化提取（辅助评估 LLM 精准定位关键信息）
        from evaluator import extract_resume_structure
        try:
            structure = extract_resume_structure(text, config)
            if structure and any(structure.get(k) for k in ("tech_stack", "companies", "key_achievements")):
                # 将结构化信息注入简历文本头部，帮助评估 LLM 注意到细节
                struct_header = "【系统辅助：简历结构化提取】\n"
                if structure.get("years_of_experience"):
                    struct_header += f"- 工作年限：约 {structure['years_of_experience']} 年\n"
                if structure.get("education_level"):
                    struct_header += f"- 学历：{structure['education_level']}\n"
                if structure.get("tech_stack"):
                    struct_header += f"- 技能/工具：{', '.join(structure['tech_stack'])}\n"
                if structure.get("companies"):
                    struct_header += f"- 曾任职公司：{', '.join(structure['companies'])}\n"
                if structure.get("recent_role"):
                    struct_header += f"- 最近职位：{structure['recent_role']}\n"
                if structure.get("key_achievements"):
                    struct_header += "- 关键成果：" + "; ".join(structure['key_achievements']) + "\n"
                struct_header += "\n---\n\n"
                text = struct_header + text  # 注入到简历文本前方
                if logger:
                    logger.debug("结构化提取完成: 年限=%s, 技能=%s",
                                structure.get("years_of_experience"),
                                len(structure.get("tech_stack", [])))
        except Exception as e:
            if logger:
                logger.debug("结构化提取跳过: %s", e)

        result = evaluate(text, config, references, filename=fname, store=store)

        # 低置信度匹配 → 加入待分配池
        if result.get("skipped"):
            add_to_pending(result, filepath, fhash, store, sse_manager, logger, notify_func)
            metrics.eval_requests.inc(status="skipped")
            if notify_func:
                notify_func(
                    "⚠️ 简历已加入待分配池",
                    f"{fname}",
                    f"LLM 推测: {result.get('matched_position', '未知')}。请到面板手动分配岗位。"
                )
            return

        report_dir = os.path.expanduser(config.get("report_dir", "./reports"))
        report_path = generate_markdown(result, fname, report_dir)

        result["resume_file"] = fname
        result["report_file"] = os.path.basename(report_path)
        result["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # v5: 复制简历到永久存储目录（防止 ~/Downloads 清理后文件丢失）
        try:
            from utils.paths import get_resumes_dir
            perm_dir = get_resumes_dir()
            os.makedirs(perm_dir, exist_ok=True)
            import shutil
            perm_path = os.path.join(perm_dir, fname)
            shutil.copy2(filepath, perm_path)
            result["permanent_path"] = perm_path
            if logger:
                logger.debug("简历已归档: %s", perm_path)
        except Exception as e:
            if logger:
                logger.warning("简历归档失败: %s", e)

        # v2: 保存评估元数据
        result["eval_metadata"] = result.pop("_meta", {})
        new_id = store.add_result(result)

        # v4: 保存简历原文（全文搜索）
        try:
            store.save_resume_text(new_id, text)
        except Exception as e:
            if logger:
                logger.debug("保存简历文本失败: %s", e)

        # v4: Claude 交叉校验（高分候选人）
        cross_validation = None
        try:
            from cross_validator import CrossValidator
            validator = CrossValidator(config)
            if validator.should_validate(result.get("match_score", 0)):
                if logger:
                    logger.info("触发 Claude 交叉校验: %s (评分 %d)", fname, result.get("match_score"))
                # _matched_idx 不在 evaluate() 返回值中，直接按名称匹配
                pos = None
                if not pos:
                    # fallback: 根据 matched_position 找岗位配置
                    pname = result.get("matched_position", "")
                    pos = next((p for p in config.get("positions", []) if p["name"] == pname), None)
                if pos:
                    cross_validation = validator.validate(text, result, pos)
                    if cross_validation:
                        cross_validation["result_id"] = new_id
                        cv_id = store.save_cross_validation(cross_validation)
                        if logger:
                            diff = cross_validation["score_diff"]
                            direction = "偏高" if diff > 0 else ("偏低" if diff < 0 else "一致")
                            logger.info("Claude 校验: %s%d (%s) | 一致性: %s",
                                        "+" if diff > 0 else "", diff, direction,
                                        cross_validation["agreement"])
        except Exception as e:
            if logger:
                logger.warning("Claude 交叉校验失败: %s", e)

        # v2: 保存结构化维度分数
        dims = result.get("dimensions", {})
        if dims:
            try:
                store.save_dimension_scores(new_id, dims)
            except Exception as e:
                if logger:
                    logger.debug("保存维度分数失败: %s", e)

        store.add_processed(fhash)

        # SSE 实时推送
        if sse_manager:
            sse_manager.broadcast(EVENT_EVAL_COMPLETE, {
                "candidate_name": result.get("candidate_name", "未知"),
                "matched_position": result.get("matched_position", "-"),
                "match_score": result.get("match_score", 0),
                "verdict": result.get("verdict", ""),
                "id": new_id,
                "timestamp": result.get("timestamp", ""),
            })

        if logger:
            logger.info("报告: %s", report_path)
            logger.info("→ %s | 岗位: %s | 匹配度: %s/100 | %s",
                        result.get("candidate_name", "未知"),
                        result.get("matched_position", "-"),
                        result.get("match_score", "-"),
                        result.get("verdict", "-"))

        verdict_text = result.get('verdict', '')
        if result.get("match_confidence") == "medium":
            verdict_text += " (岗位匹配置信度: 中)"

        if notify_func:
            is_star = result.get('verdict') == '强烈推荐'
            flush = _queue_notification(
                "简历评估完成",
                f"{result.get('candidate_name', '未知')} → {result.get('matched_position', '')}",
                f"匹配度 {result.get('match_score', '-')}/100 — {verdict_text}",
                verdict=result.get('verdict', ''),
                is_star=is_star,
                logger=logger,
            )
            if flush:
                _send_batched_notification(notify_func)

        metrics.eval_requests.inc(status="success")
        elapsed = time.time() - start_time
        metrics.eval_duration.observe(elapsed)

    except Exception as e:
        if logger:
            logger.error("处理失败: %s — %s: %s", fname, type(e).__name__, e)
            import traceback
            logger.debug(traceback.format_exc())
        metrics.eval_requests.inc(status="error")
        # v3: SSE 推送系统错误通知
        if sse_manager:
            sse_manager.broadcast("system_notification", {
                "type": "error",
                "message": f"处理 {fname} 失败: {e}",
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })


def add_to_pending(result: dict, filepath: str, fhash: str, store,
                   sse_manager: SSEManager | None = None, logger=None,
                   notify_func=None):
    """将岗位不明确的简历加入待分配池。"""
    import uuid
    if store.pending_exists_by_fhash(fhash):
        if logger:
            logger.info("待分配池已存在相同文件，跳过: %s", os.path.basename(filepath))
        return
    pid = uuid.uuid4().hex[:8]
    store.add_pending({
        "id": pid,
        "candidate_name": result.get("candidate_name", "未知"),
        "matched_position": result.get("matched_position", "未识别"),
        "llm_guess": result.get("matched_position", "未识别"),
        "llm_reason": result.get("summary", ""),
        "match_confidence": result.get("match_confidence", "low"),
        "resume_file": os.path.basename(filepath),
        "filepath": filepath,
        "fhash": fhash,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "status": "pending",
    })
    if logger:
        logger.info("已加入待分配池: %s", os.path.basename(filepath))

    if sse_manager:
        sse_manager.broadcast(EVENT_PENDING_UPDATE, {
            "id": pid,
            "candidate_name": result.get("candidate_name", "未知"),
            "llm_guess": result.get("matched_position", "未识别"),
            "confidence": result.get("match_confidence", "low"),
        })


def load_references(project_dir: str, positions: list) -> dict:
    """加载标杆简历。references/<岗位名>/ 目录下的文件会被读取。"""
    refs = {}
    refs_root = os.path.join(project_dir, "references")
    if not os.path.isdir(refs_root):
        return refs

    import logging
    service_logger = logging.getLogger(__name__)

    for pos in positions:
        pos_dir = os.path.join(refs_root, pos["name"])
        if not os.path.isdir(pos_dir):
            continue
        texts = []
        for f in sorted(os.listdir(pos_dir)):
            fpath = os.path.join(pos_dir, f)
            if not os.path.isfile(fpath):
                continue
            ext = Path(fpath).suffix.lower()
            if ext not in {".pdf", ".docx", ".doc", ".txt"}:
                continue
            try:
                if ext == ".txt":
                    with open(fpath, "r", encoding="utf-8") as rf:
                        t = rf.read()
                else:
                    t = extract_text(fpath)
                if t.strip():
                    texts.append(f"[标杆简历 {len(texts)+1}: {f}]\n{t[:3000]}")
            except Exception:
                pass
        if texts:
            refs[pos["name"]] = "\n\n".join(texts)
            service_logger.info("   ✅ %s: %d 份标杆简历", pos["name"], len(texts))
        else:
            service_logger.info("   ⚠️ %s: 无标杆简历", pos["name"])
    return refs


def flush_notifications(notify_func):
    """强制发送缓冲区中所有积压的通知。（供外部调用）"""
    _send_batched_notification(notify_func)


def generate_pdf(results: list, position: str, config: dict) -> bytes:
    """用 fpdf2 生成招聘数据 PDF 报告。"""
    from fpdf import FPDF

    # 从配置读取字体路径
    font_config = config.get("server", {}).get("pdf_fonts", [])
    FONT_CANDIDATES = font_config if font_config else [
        # macOS
        "/Library/Fonts/Arial Unicode.ttf",
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        # Windows
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simsun.ttc",
        "C:/Windows/Fonts/msgothic.ttc",
    ]
    font_path = None
    for fp in FONT_CANDIDATES:
        if os.path.exists(fp):
            font_path = fp
            break
    if not font_path:
        pdf = FPDF()
        pdf.add_page()
        pdf.set_font("Helvetica", "", 12)
        pdf.cell(0, 10, "No Chinese font found on system", align="C")
        return pdf.output()

    pdf = FPDF()
    pdf.add_font("cjk", "", font_path)
    pdf.add_font("cjk", "B", font_path)
    pdf.set_auto_page_break(True, 15)

    company = config.get("report", {}).get("company_name", "游戏公司")

    # ── Cover page ──
    pdf.add_page()
    pdf.ln(40)
    pdf.set_font("cjk", "B", 28)
    pdf.cell(0, 16, f"{company}招聘数据报告", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(6)
    pdf.set_font("cjk", "", 16)
    pdf.cell(0, 10, f"岗位: {position}", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 10, f"导出时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(12)
    total = len(results)
    star = sum(1 for r in results if r.get("verdict") == "强烈推荐")
    pdf.set_font("cjk", "", 13)
    pdf.cell(0, 8, f"共 {total} 位候选人  |  强烈推荐 {star} 人", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(20)
    pdf.set_font("cjk", "", 10)
    pdf.cell(0, 8, "本报告由 AI 自动生成，仅供内部招聘决策参考", align="C")

    # ── Summary page ──
    pdf.add_page()
    pdf.set_font("cjk", "B", 18)
    pdf.cell(0, 12, f"招聘数据报告", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("cjk", "", 11)
    pdf.cell(0, 8, f"岗位: {position}    导出: {datetime.now().strftime('%Y-%m-%d %H:%M')}", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(6)

    total = len(results)
    star = sum(1 for r in results if r.get("verdict") == "强烈推荐")
    rec = sum(1 for r in results if r.get("verdict") == "推荐")
    pend = sum(1 for r in results if r.get("verdict") == "待定")
    rej = sum(1 for r in results if r.get("verdict") == "不推荐")
    avg_score = sum(r.get("match_score", 0) for r in results) / total if total > 0 else 0
    interview = sum(1 for r in results if r.get("pipeline_status") == "面试中")
    passed = sum(1 for r in results if r.get("pipeline_status") == "已通过")
    eliminated = sum(1 for r in results if r.get("pipeline_status") == "已淘汰")
    screening = sum(1 for r in results if r.get("pipeline_status", "待筛选") == "待筛选")

    pdf.set_font("cjk", "B", 14)
    pdf.cell(0, 10, "数据概览", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("cjk", "", 10)

    stats = [
        ("候选人总数", str(total)), ("平均匹配度", f"{avg_score:.1f}/100"),
        ("强烈推荐", f"{star} 人"), ("推荐", f"{rec} 人"),
        ("待定", f"{pend} 人"), ("不推荐", f"{rej} 人"),
        ("待筛选", f"{screening} 人"), ("面试中", f"{interview} 人"),
        ("已通过", f"{passed} 人"), ("已淘汰", f"{eliminated} 人"),
    ]
    for i, (label, value) in enumerate(stats):
        x = (i % 5) * 38
        y_offset = (i // 5) * 10
        pdf.set_xy(10 + x, pdf.get_y() + y_offset)
        pdf.set_font("cjk", "B", 10)
        pdf.cell(18, 7, label + ":")
        pdf.set_font("cjk", "", 10)
        pdf.cell(15, 7, value)
    pdf.ln(22)

    # ── Candidate Table ──
    if not results:
        pdf.set_font("cjk", "", 12)
        pdf.cell(0, 10, "暂无候选人数据", align="C")
    else:
        pdf.set_font("cjk", "B", 14)
        pdf.cell(0, 10, "候选人明细", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("cjk", "B", 9)

        col_w = [44, 24, 28, 36, 28, 30]
        headers = ["候选人", "匹配度", "评估结论", "Pipeline", "技能/经验/学历", "评估时间"]
        for i, h in enumerate(headers):
            pdf.cell(col_w[i], 8, h, border=1, align="C")
        pdf.ln()

        pdf.set_font("cjk", "", 8)
        for r in sorted(results, key=lambda x: x.get("match_score", 0), reverse=True):
            if pdf.get_y() > 260:
                pdf.add_page()
                pdf.set_font("cjk", "B", 9)
                for i, h in enumerate(headers):
                    pdf.cell(col_w[i], 8, h, border=1, align="C")
                pdf.ln()
                pdf.set_font("cjk", "", 8)

            name = r.get("candidate_name", "-")[:8]
            score = str(r.get("match_score", "-"))
            verdict = r.get("verdict", "-")
            pipeline = r.get("pipeline_status", "待筛选")
            dims = r.get("dimensions", {})
            s_skill = dims.get("skill_match", {}).get("score", "-")
            s_exp = dims.get("experience_match", {}).get("score", "-")
            s_edu = dims.get("education_match", {}).get("score", "-")
            dim_str = f"{s_skill}/{s_exp}/{s_edu}"
            ts = (r.get("timestamp", "") or "")[:10]

            row_data = [name, score, verdict, pipeline, dim_str, ts]
            for i, val in enumerate(row_data):
                pdf.cell(col_w[i], 7, str(val), border=1, align="C")
            pdf.ln()

    return pdf.output()
