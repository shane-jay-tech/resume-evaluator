"""API 路由表 — 替代 main.py 中的长 if/elif 链。

每个路由映射为 (method, path) → handler_function。
新增端点只需在此注册，不需要修改 main.py。
"""

import json
import os
import time
from datetime import datetime
from urllib.parse import urlparse, parse_qs, quote

from evaluator import evaluate as do_evaluate
from evaluator import extract_reference_features, _get_dimensions, _build_eval_system, rank_candidates
from evaluator import build_eval_prompt
from utils.llm_client import LLMClient, parse_json
from reporter import generate_markdown
from parser import extract_text
from sse import SSEClient, EVENT_EVAL_COMPLETE, EVENT_STATUS_CHANGE, EVENT_PENDING_UPDATE
from metrics import metrics
from services import process_resume as _process_resume, generate_pdf
from scoring_audit import audit_all as _audit_all


def _proc(filepath):
    """便捷包装器：用 RouteContext 调用 process_resume。"""
    _process_resume(filepath, ctx.store, ctx.config, ctx.references,
                    ctx.sse_manager, ctx.logger, ctx.notify)


class RouteContext:
    """路由上下文 — 所有 handler 共享的状态。"""

    def __init__(self):
        self.store = None
        self.config = {}
        self.references = {}
        self.project_dir = ""
        self.sse_manager = None
        self.logger = None
        self.notify = None
        self._start_time = time.time()
        self.last_archive_count = 0
        self.last_cleanup_result = {}

    @property
    def uptime(self):
        return time.time() - self._start_time


# 全局上下文（由 main.py 初始化）
ctx = RouteContext()


# ═══════════════════════════════════════════════════════════
# v5: BOSS 直聘文件名模糊匹配（薪资字段可能缺失）
# ═══════════════════════════════════════════════════════════

def _find_resume_file(stored_filename: str) -> str | None:
    """查找简历文件。优先查永久存储目录，再回退 ~/Downloads。

    支持 BOSS 直聘文件名变体（薪资字段可能缺失）。
    """

    def _search_in_dir(directory: str, filename: str) -> str | None:
        """在指定目录中搜索文件，先精确匹配再模糊匹配。"""
        exact = os.path.join(directory, filename)
        if os.path.isfile(exact) and os.path.realpath(exact).startswith(directory):
            return exact
        # 模糊匹配
        key = filename.rsplit(".", 1)[0] if "." in filename else filename
        if "】" in key:
            key = key.split("】", 1)[1].strip()
        key = key.replace(" ", "").lower()
        if not key:
            return None
        try:
            for f in os.listdir(directory):
                fpath = os.path.join(directory, f)
                if not os.path.isfile(fpath):
                    continue
                fnorm = f.rsplit(".", 1)[0] if "." in f else f
                if "】" in fnorm:
                    fnorm = fnorm.split("】", 1)[1].strip()
                fnorm = fnorm.replace(" ", "").lower()
                if fnorm == key and os.path.realpath(fpath).startswith(directory):
                    return fpath
        except Exception:
            pass
        return None

    # 1. 优先查永久存储目录
    perm_dir = os.path.join(os.getcwd(), "data", "resumes")
    result = _search_in_dir(perm_dir, stored_filename)
    if result:
        return result

    # 2. 回退到 ~/Downloads
    return _search_in_dir(os.path.expanduser("~/Downloads"), stored_filename)


def _respond_json(handler, data, code=200):
    """统一 JSON 响应。"""
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _read_body(handler) -> str:
    """读取 HTTP 请求体。"""
    content_length = int(handler.headers.get("Content-Length", "0"))
    if content_length == 0:
        return ""
    return handler.rfile.read(content_length).decode("utf-8", errors="replace")


# ═══════════════════════════════════════════════════════════
# GET 路由
# ═══════════════════════════════════════════════════════════

def get_health(handler):
    qs = parse_qs(urlparse(handler.path).query)
    include_deleted = qs.get("include_deleted", ["1"])[0] == "1"  # v3: 默认全量

    stats = ctx.store.get_stats(include_deleted=include_deleted)
    positions = ctx.config.get("positions", [])
    pos_stats = {}
    for p in positions:
        pos_stats[p["name"]] = ctx.store.get_stats(p["name"], include_deleted=include_deleted)

    # 更新 gauge
    metrics.queue_size.set(stats.get("total", 0), type="results")
    pending = ctx.store.get_all_pending()
    metrics.queue_size.set(len(pending), type="pending")

    # drift 检测
    drift_warning = ""
    try:
        trends_7d = ctx.store.get_eval_trends(7)
        trends_30d = ctx.store.get_eval_trends(30)
        if trends_7d and trends_30d:
            avg_7d = sum(t["avg_score"] for t in trends_7d) / len(trends_7d)
            avg_30d = sum(t["avg_score"] for t in trends_30d) / len(trends_30d)
            if avg_30d > 0 and abs(avg_7d - avg_30d) / avg_30d > 0.15:
                direction = "高于" if avg_7d > avg_30d else "低于"
                drift_warning = f"7日均分({avg_7d:.1f}){direction}30日均分({avg_30d:.1f})超15%，建议关注评估标准一致性"
    except Exception:
        pass

    last_archive = ctx.last_archive_count if ctx.last_archive_count else 0
    cleanup_info = {
        "last_cleanup": ctx.last_cleanup_result if ctx.last_cleanup_result else {},
        "config": ctx.config.get("cleanup", {}),
    }

    _respond_json(handler, {
        "ok": True, "status": "running", "uptime": ctx.uptime,
        "stats": stats, "position_stats": pos_stats,
        "drift_warning": drift_warning,
        "last_archive_count": last_archive,
        "cleanup": cleanup_info,
    })


def get_results(handler):
    qs = parse_qs(urlparse(handler.path).query)
    position = qs.get("position", [None])[0]
    include_deleted = qs.get("include_deleted", ["0"])[0] == "1"
    status_filter = qs.get("status", [None])[0]
    if position:
        results = ctx.store.get_results_by_position(position, include_deleted)
    else:
        results = ctx.store.get_all_results(include_deleted)
    if status_filter:
        allowed = set(s.strip() for s in status_filter.split(","))
        results = [r for r in results if r.get("pipeline_status", "待筛选") in allowed]
    _respond_json(handler, results)


def get_pending(handler):
    _respond_json(handler, ctx.store.get_all_pending())


def get_positions(handler):
    _respond_json(handler, [p["name"] for p in ctx.config.get("positions", [])])


def get_position_config(handler):
    pos_configs = []
    for p in ctx.config.get("positions", []):
        pos_configs.append({
            "name": p.get("name", ""),
            "enabled": p.get("enabled", True),
            "aliases": p.get("aliases", []),
            "dimensions": [{"name": d["name"], "weight": d["weight"]} for d in p.get("dimensions", [])],
            "must_have": p.get("must_have", []),
            "nice_to_have": p.get("nice_to_have", []),
            "education": p.get("education", ""),
            "experience": p.get("experience", ""),
            "other_requirements": p.get("other_requirements", ""),
            "scoring": p.get("scoring", {}),
        })
    _respond_json(handler, pos_configs)


def get_deleted(handler):
    _respond_json(handler, ctx.store.get_deleted_results())


def get_approvals(handler):
    _respond_json(handler, ctx.store.get_pending_approvals())


def get_duplicates(handler):
    qs = parse_qs(urlparse(handler.path).query)
    status = qs.get("status", [""])[0] if qs else ""
    _respond_json(handler, {
        "items": ctx.store.get_duplicates(status if status else None),
        "count": ctx.store.get_duplicate_count(),
    })


def get_export_pdf(handler):
    qs = parse_qs(urlparse(handler.path).query)
    position = qs.get("position", [None])[0]
    if not position:
        _respond_json(handler, {"ok": False, "error": "需要 position 参数"}, 400)
        return
    include_deleted = qs.get("include_deleted", ["0"])[0] == "1"
    results = ctx.store.get_all_results(include_deleted) if position == "all" else ctx.store.get_results_by_position(position, include_deleted)

    pdf_bytes = generate_pdf(results, position, ctx.config)
    fname = f'招聘数据_{position}_{datetime.now().strftime("%Y%m%d")}.pdf'
    handler.send_response(200)
    handler.send_header("Content-Type", "application/pdf")
    handler.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(fname)}")
    handler.send_header("Content-Length", str(len(pdf_bytes)))
    handler.end_headers()
    handler.wfile.write(pdf_bytes)


def get_resume_text(handler):
    qs = parse_qs(urlparse(handler.path).query)
    fname = qs.get("file", [None])[0]
    if not fname:
        _respond_json(handler, {"ok": False, "error": "需要 file 参数"}, 400)
        return
    safe_name = os.path.basename(fname)
    if safe_name != fname or ".." in fname:
        _respond_json(handler, {"ok": False, "error": "非法文件名"}, 400)
        return
    fpath = _find_resume_file(safe_name)
    if not fpath:
        _respond_json(handler, {"ok": False, "error": "文件不存在"}, 404)
        return
    try:
        text = extract_text(fpath)
        _respond_json(handler, {"ok": True, "text": text[:5000], "full_length": len(text)})
    except Exception as e:
        _respond_json(handler, {"ok": False, "error": str(e)}, 500)


def get_eval_trends(handler):
    qs = parse_qs(urlparse(handler.path).query)
    days = int(qs.get("days", [7])[0])
    trends = ctx.store.get_eval_trends(days)
    _respond_json(handler, {"ok": True, "days": days, "trends": trends})


def get_eval_reliability(handler):
    reliability = ctx.store.get_eval_reliability()
    _respond_json(handler, {"ok": True, "reliability": reliability})


def get_metrics(handler):
    text = metrics.render_all()
    body = text.encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "text/plain; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


# ═══════════════════════════════════════════════════════════
# v4: 新增 GET 端点
# ═══════════════════════════════════════════════════════════


def get_search_resumes(handler):
    """FTS5 全文搜索简历。"""
    qs = parse_qs(urlparse(handler.path).query)
    query = qs.get("q", [""])[0].strip()
    limit = int(qs.get("limit", ["20"])[0])
    if not query:
        _respond_json(handler, {"ok": False, "error": "需要 q 参数（搜索关键词）"}, 400)
        return
    try:
        results = ctx.store.search_resumes(query, limit=limit)
        _respond_json(handler, {"ok": True, "query": query, "total": len(results), "results": results})
    except RuntimeError as e:
        _respond_json(handler, {"ok": False, "error": f"搜索失败: {e}"}, 500)


def get_compare(handler):
    """候选人横向对比。"""
    qs = parse_qs(urlparse(handler.path).query)
    ids = qs.get("ids", [""])[0]
    if not ids:
        _respond_json(handler, {"ok": False, "error": "需要 ids 参数（逗号分隔的 result_id）"}, 400)
        return
    id_list = [int(x.strip()) for x in ids.split(",") if x.strip().isdigit()][:5]
    if len(id_list) < 2:
        _respond_json(handler, {"ok": False, "error": "至少需要2个候选人 ID"}, 400)
        return

    candidates = []
    for rid in id_list:
        r = ctx.store.get_result_by_id(rid)
        if r:
            # 获取维度分数明细
            dim_detail = ctx.store.get_dimension_scores(rid)
            # 获取面试反馈
            feedback = ctx.store.get_interview_feedback(rid)
            # 获取交叉校验
            cv = ctx.store.get_cross_validation(rid)
            candidates.append({
                "id": r["id"],
                "candidate_name": r.get("candidate_name", ""),
                "matched_position": r.get("matched_position", ""),
                "match_score": r.get("match_score", 0),
                "verdict": r.get("verdict", ""),
                "score_reasoning": r.get("score_reasoning", ""),
                "dimensions": r.get("dimensions", {}),
                "dimension_details": dim_detail,
                "highlights": r.get("highlights", []),
                "risks": r.get("risks", []),
                "summary": r.get("summary", ""),
                "interview_feedback": feedback[0] if feedback else None,
                "cross_validation": cv[0] if cv else None,
            })

    _respond_json(handler, {"ok": True, "candidates": candidates})


def get_cross_validation_stats(handler):
    """获取交叉校验统计。"""
    stats = ctx.store.get_cross_validation_stats()
    _respond_json(handler, {"ok": True, "stats": stats})


def get_cross_validation_list(handler):
    """获取交叉校验记录列表。"""
    qs = parse_qs(urlparse(handler.path).query)
    result_id = qs.get("result_id", [None])[0]
    if result_id:
        result_id = int(result_id)
    results = ctx.store.get_cross_validation(result_id)
    _respond_json(handler, {"ok": True, "validations": results})


def get_feedback_accuracy(handler):
    """获取面试反馈准确率统计。"""
    qs = parse_qs(urlparse(handler.path).query)
    position = qs.get("position", [None])[0]
    stats = ctx.store.get_feedback_accuracy_stats(position)
    _respond_json(handler, {"ok": True, "stats": stats})


def get_regression_list(handler):
    """获取回归测试记录。"""
    qs = parse_qs(urlparse(handler.path).query)
    version_tag = qs.get("version", [None])[0]
    summary = ctx.store.get_regression_summary(version_tag)
    _respond_json(handler, {"ok": True, "regression": summary})


def get_health_deep(handler):
    """深度健康检查 — DB 连接、LLM API、磁盘空间。"""
    # DB 健康
    db_health = ctx.store.check_connection()

    # 磁盘空间
    disk_info = {}
    try:
        import shutil
        usage = shutil.disk_usage(ctx.project_dir)
        disk_info = {
            "total_gb": round(usage.total / (1024**3), 1),
            "used_gb": round(usage.used / (1024**3), 1),
            "free_gb": round(usage.free / (1024**3), 1),
            "percent_used": round(usage.used / usage.total * 100, 1),
        }
    except Exception:
        disk_info = {"error": "无法获取磁盘信息"}

    # LLM API 健康检查
    llm_health = {"ok": True}
    try:
        llm = LLMClient(ctx.config)
        t0 = time.time()
        resp = llm.chat(
            [{"role": "user", "content": "回复 ok"}],
            max_tokens=10, timeout=10.0, retries=0,
        )
        llm_health["latency_ms"] = round((time.time() - t0) * 1000)
        llm_health["model"] = llm.model_name
    except Exception as e:
        llm_health["ok"] = False
        llm_health["error"] = str(e)[:200]

    # 任务队列
    task_stats = ctx.store.get_task_stats()

    overall_ok = db_health["ok"] and llm_health["ok"]
    _respond_json(handler, {
        "ok": overall_ok,
        "status": "healthy" if overall_ok else "degraded",
        "uptime": ctx.uptime,
        "database": db_health,
        "disk": disk_info,
        "llm_api": llm_health,
        "task_queue": task_stats,
    })


def get_reference_features(handler):
    qs = parse_qs(urlparse(handler.path).query)
    position = qs.get("position", [None])[0]
    features = ctx.store.get_reference_features(position)
    _respond_json(handler, {"ok": True, "features": features})


def get_dimension_stats(handler):
    """v2: 维度统计 API — 查询某岗位某个维度的统计信息。"""
    qs = parse_qs(urlparse(handler.path).query)
    dimension_key = qs.get("key", [None])[0]
    position = qs.get("position", [None])[0]
    if not dimension_key:
        _respond_json(handler, {"ok": False, "error": "需要 key 参数（维度key）"}, 400)
        return
    stats = ctx.store.get_dimension_stats(dimension_key, position)
    _respond_json(handler, {"ok": True, "stats": stats})


def get_sse_events(handler):
    qs = parse_qs(urlparse(handler.path).query)
    token = (qs.get("token", [""])[0]) if qs else ""
    from utils.config import get_auth_token
    expected = get_auth_token()
    if expected and token != expected:
        _respond_json(handler, {"ok": False, "error": "未授权"}, 401)
        return

    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("Connection", "keep-alive")
    # CORS 由 main.py 的 RequestHandler.end_headers() 统一设置
    handler.end_headers()

    client = SSEClient(handler.wfile)
    ctx.sse_manager.register(client)
    try:
        client.send_heartbeat()
        while not client.stopped:
            time.sleep(15)  # 15s heartbeat (was 30s)
            if not client.stopped:
                client.send_heartbeat()
    finally:
        ctx.sse_manager.unregister(client)


# ═══════════════════════════════════════════════════════════
# POST 路由
# ═══════════════════════════════════════════════════════════

def post_save(handler, data):
    try:
        updates = data.get("updates", [])
        now_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for upd in updates:
            idx = upd.get("index")
            rid = upd.get("id")
            result = None
            if rid:
                result = ctx.store.get_result_by_id(rid)
            elif idx is not None:
                results = ctx.store.get_all_results()
                if 0 <= idx < len(results):
                    result = results[idx]
            if not result:
                continue
            rid = result["id"]
            for key in ("verdict", "match_score", "notes", "pipeline_status"):
                if key in upd:
                    old_val = result.get(key)
                    new_val = upd[key]
                    if key == "pipeline_status" and new_val != old_val:
                        ctx.store.add_status_history(rid, old_val or "", new_val)
                        ctx.store.update_result(rid, {key: new_val, "last_status_change": now_ts})
                    else:
                        ctx.store.update_result(rid, {key: new_val})
        _respond_json(handler, {"ok": True, "count": len(updates)})
    except Exception as e:
        _respond_json(handler, {"ok": False, "error": str(e)}, 500)


def post_delete(handler, data):
    try:
        indices = data.get("indices", [])
        ids = data.get("ids", [])
        if not indices and not ids:
            _respond_json(handler, {"ok": False, "error": "需要 indices 或 ids"}, 400)
            return
        if ids:
            ctx.store.delete_results(ids, soft=True)
            _respond_json(handler, {"ok": True, "deleted_ids": ids, "count": len(ids)})
        else:
            results = ctx.store.get_all_results()
            ids_to_del = []
            for idx in sorted(indices, reverse=True):
                if 0 <= idx < len(results):
                    ids_to_del.append(results[idx]["id"])
            ctx.store.delete_results(ids_to_del, soft=True)
            _respond_json(handler, {"ok": True, "deleted_ids": ids_to_del, "count": len(ids_to_del)})
    except Exception as e:
        _respond_json(handler, {"ok": False, "error": str(e)}, 500)


def post_undo(handler, data):
    try:
        rid = data.get("id")
        if not rid:
            _respond_json(handler, {"ok": False, "error": "需要 id"}, 400)
            return
        ok = ctx.store.undo_delete(rid)
        _respond_json(handler, {"ok": ok})
    except Exception as e:
        _respond_json(handler, {"ok": False, "error": str(e)}, 500)


def post_assign(handler, data):
    """手动分配岗位并重新评估。修复了 EVAL_SYSTEM 不存在的 bug。"""
    try:
        pending_id = data.get("id")
        position_name = data.get("position")
        if not pending_id or not position_name:
            _respond_json(handler, {"ok": False, "error": "需要 id 和 position"}, 400)
            return

        target = ctx.store.get_pending_by_id(pending_id)
        if not target:
            _respond_json(handler, {"ok": False, "error": "未找到该待分配记录"}, 404)
            return

        if not os.path.exists(target["filepath"]):
            ctx.store.update_pending(pending_id, {"status": "file_missing"})
            _respond_json(handler, {"ok": False, "error": "文件已不存在"}, 404)
            return

        positions = ctx.config.get("positions", [])
        pos_idx = None
        for i, p in enumerate(positions):
            if p["name"] == position_name:
                pos_idx = i
                break

        if pos_idx is None:
            _respond_json(handler, {"ok": False, "error": f"未找到岗位: {position_name}"}, 400)
            return

        text = extract_text(target["filepath"])
        result = do_evaluate(text, ctx.config, ctx.references, filename=target["resume_file"])

        if result.get("skipped"):
            # 手动分配，直接强制评估（绕过岗位匹配）
            llm = LLMClient(ctx.config)
            pos = positions[pos_idx]
            ref_text = ctx.references.get(pos["name"], "") if ctx.references else ""
            dimensions = _get_dimensions(pos)

            # v5: 查询面试反馈校准数据
            fb_cal = ""
            try:
                fb_data = ctx.store.get_feedback_for_calibration(pos["name"], limit=10) if ctx.store else []
                if fb_data:
                    overrated = sum(1 for f in fb_data if f.get("accuracy") == "overrated")
                    underrated = sum(1 for f in fb_data if f.get("accuracy") == "underrated")
                    accurate = sum(1 for f in fb_data if f.get("accuracy") == "accurate")
                    biases = [f.get("system_score", 0) - f.get("interview_score", 0) * 10 for f in fb_data]
                    avg_bias = round(sum(biases) / len(biases), 1) if biases else 0
                    fb_cal = (f"该岗位历史面试反馈（共{len(fb_data)}条）：系统评分偏高{overrated}条、"
                              f"准确{accurate}条、偏低{underrated}条，平均偏差{avg_bias}分。")
            except Exception:
                pass

            eval_system = _build_eval_system(dimensions)
            content = llm.chat(
                [
                    {"role": "system", "content": eval_system},
                    {"role": "user", "content": build_eval_prompt(text, pos, ref_text, fb_cal)},
                ],
                temperature=0.3,
                max_tokens=4096,
            )
            result = parse_json(content)
            if "match_score" in result:
                result["match_score"] = int(result["match_score"])
            for dim in result.get("dimensions", {}).values():
                if "score" in dim:
                    dim["score"] = int(dim["score"])
            result["matched_position"] = pos["name"]
            result["match_method"] = "manual"
            result["match_confidence"] = "high"
            result["_meta"] = {
                "model_name": llm.model_name,
                "prompt_version": "v5-three-stage",
                "position_name": pos["name"],
                "evaluated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }

        report_dir = os.path.expanduser(ctx.config.get("report_dir", "./reports"))
        report_path = generate_markdown(result, target["resume_file"], report_dir)

        result["resume_file"] = target["resume_file"]
        result["report_file"] = os.path.basename(report_path)
        result["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        new_id = ctx.store.add_result(result)

        # v5: 保存简历文本到 FTS 索引（手动分配路径遗漏了此步骤）
        try:
            ctx.store.save_resume_text(new_id, text)
        except Exception:
            pass

        ctx.store.update_pending(pending_id, {
            "status": "assigned",
            "assigned_position": position_name,
        })

        if ctx.notify:
            ctx.notify(
                "简历评估完成（手动分配）",
                f"{result.get('candidate_name', '未知')} → {position_name}",
                f"匹配度 {result.get('match_score', '-')}/100 — {result.get('verdict', '')}"
            )

        _respond_json(handler, {
            "ok": True,
            "candidate_name": result.get("candidate_name"),
            "match_score": result.get("match_score"),
            "verdict": result.get("verdict"),
        })
    except Exception as e:
        ctx.logger.error("assign 失败: %s", e) if ctx.logger else None
        _respond_json(handler, {"ok": False, "error": str(e)}, 500)


def post_dismiss(handler, data):
    try:
        pid = data.get("id")
        pids = data.get("ids", [])
        if pid:
            pids = [pid]
        if not pids:
            _respond_json(handler, {"ok": False, "error": "需要 id 或 ids"}, 400)
            return
        for p in pids:
            item = ctx.store.get_pending_by_id(p)
            if item and item.get("fhash"):
                ctx.store.add_processed(item["fhash"])
        count = ctx.store.dismiss_pending(pids)
        _respond_json(handler, {"ok": True, "dismissed": pids, "count": count})
    except Exception as e:
        _respond_json(handler, {"ok": False, "error": str(e)}, 500)


def post_rank(handler, data):
    try:
        position = data.get("position", "")
        results = ctx.store.get_results_by_position(position) if position else ctx.store.get_all_results()
        if not results:
            _respond_json(handler, {"ok": False, "error": "无候选人数据"}, 400)
            return
        rankings = rank_candidates(results, position, ctx.config)
        for i, item in enumerate(rankings):
            idx = item.get("index", 0) - 1
            if 0 <= idx < len(results):
                ctx.store.update_result(results[idx]["id"], {"rank_order": len(rankings) - i})
        _respond_json(handler, {"ok": True, "rankings": rankings})
    except Exception as e:
        _respond_json(handler, {"ok": False, "error": str(e)}, 500)


def post_transition(handler, data):
    try:
        rid = data.get("id")
        new_status = data.get("status")
        if not rid or not new_status:
            _respond_json(handler, {"ok": False, "error": "需要 id 和 status"}, 400)
            return
        result = ctx.store.get_result_by_id(rid)
        if not result:
            _respond_json(handler, {"ok": False, "error": "候选人不存在"}, 404)
            return
        valid_statuses = ["待筛选", "面试中", "已通过", "已淘汰"]
        if new_status not in valid_statuses:
            _respond_json(handler, {"ok": False, "error": f"无效状态，可选: {valid_statuses}"}, 400)
            return

        old_status = result.get("pipeline_status", "待筛选")
        now_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ctx.store.add_status_history(rid, old_status, new_status)
        ctx.store.update_result(rid, {
            "pipeline_status": new_status,
            "last_status_change": now_ts,
        })

        if ctx.notify:
            ctx.notify("Pipeline 状态变更", f"{result['candidate_name']}", f"{old_status} → {new_status}")

        if ctx.sse_manager:
            ctx.sse_manager.broadcast(EVENT_STATUS_CHANGE, {
                "id": rid, "candidate_name": result["candidate_name"],
                "from": old_status, "to": new_status, "timestamp": now_ts,
            })

        metrics.status_changes.inc(from_status=old_status, to_status=new_status)
        _respond_json(handler, {"ok": True, "from": old_status, "to": new_status})
    except Exception as e:
        _respond_json(handler, {"ok": False, "error": str(e)}, 500)


def post_promote_reference(handler, data):
    try:
        rid = data.get("id")
        if not rid:
            _respond_json(handler, {"ok": False, "error": "需要 id"}, 400)
            return
        result = ctx.store.get_result_by_id(rid)
        if not result:
            _respond_json(handler, {"ok": False, "error": "候选人不存在"}, 404)
            return
        ctx.store.add_reference_approval(result["matched_position"], result["candidate_name"], rid)
        _respond_json(handler, {"ok": True, "message": "已提交标杆审批"})
    except Exception as e:
        _respond_json(handler, {"ok": False, "error": str(e)}, 500)


def post_approve_reference(handler, data):
    try:
        approval_id = data.get("id")
        if not approval_id:
            _respond_json(handler, {"ok": False, "error": "需要 id"}, 400)
            return
        ctx.store.approve_reference(approval_id)
        _respond_json(handler, {"ok": True, "message": "标杆审批通过"})
    except Exception as e:
        _respond_json(handler, {"ok": False, "error": str(e)}, 500)


def post_verify(handler, data):
    try:
        pending_id = data.get("id")
        position_name = data.get("position")
        if not pending_id:
            _respond_json(handler, {"ok": False, "error": "需要 id"}, 400)
            return
        target = ctx.store.get_pending_by_id(pending_id)
        if not target:
            _respond_json(handler, {"ok": False, "error": "未找到该记录"}, 404)
            return
        if position_name:
            post_assign(handler, data)
            return
        if not os.path.exists(target["filepath"]):
            _respond_json(handler, {"ok": False, "error": "文件已不存在"}, 404)
            return
        text = extract_text(target["filepath"])
        _respond_json(handler, {
            "ok": True, "pending": target,
            "resume_text": text[:5000],
            "positions": [p["name"] for p in ctx.config.get("positions", [])],
        })
    except Exception as e:
        _respond_json(handler, {"ok": False, "error": str(e)}, 500)


def post_batch_evaluate(handler, data):
    try:
        pending = ctx.store.get_all_pending()
        if not pending:
            _respond_json(handler, {"ok": False, "error": "无待分配简历"}, 400)
            return
        from concurrent.futures import ThreadPoolExecutor, as_completed
        pending_to_process = [p for p in pending if os.path.exists(p["filepath"])]
        ctx.logger.info("开始并发评估 %d 份简历…", len(pending_to_process)) if ctx.logger else None
        results = []
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {executor.submit(_proc, p["filepath"]): p for p in pending_to_process}
            for future in as_completed(futures):
                p = futures[future]
                try:
                    future.result()
                    results.append({"file": p["resume_file"], "status": "ok"})
                except Exception as e:
                    results.append({"file": p["resume_file"], "status": "error", "error": str(e)})
        _respond_json(handler, {"ok": True, "results": results})
    except Exception as e:
        _respond_json(handler, {"ok": False, "error": str(e)}, 500)


def post_queue(handler, data):
    try:
        order = data.get("order", [])
        if not order:
            _respond_json(handler, {"ok": False, "error": "需要 order"}, 400)
            return
        base_ts = time.time()
        for i, pid in enumerate(order):
            ts = datetime.fromtimestamp(base_ts - i).strftime("%Y-%m-%d %H:%M:%S")
            ctx.store.update_pending(pid, {"timestamp": ts})
        _respond_json(handler, {"ok": True})
    except Exception as e:
        _respond_json(handler, {"ok": False, "error": str(e)}, 500)


def post_duplicates_reevaluate(handler, data):
    try:
        fhashes = data.get("fhashes", [])
        if not fhashes:
            _respond_json(handler, {"ok": False, "error": "需要 fhashes"}, 400)
            return
        processed = []
        for fh in fhashes:
            dupes = ctx.store.get_duplicates("pending_review")
            match = next((d for d in dupes if d["fhash"] == fh), None)
            if not match:
                continue
            filepath = match.get("filepath", "")
            if filepath and os.path.exists(filepath):
                ctx.store.update_duplicate_status(fh, "reevaluating")
                try:
                    _proc(filepath)
                    # v5: 评估成功后再清理，防止崩溃后文件被重复评估
                    ctx.store.remove_processed(fh)
                    ctx.store.remove_duplicate(fh)
                except Exception as e:
                    ctx.logger.error("重新评估失败: %s — %s", filepath, e) if ctx.logger else None
                    ctx.store.update_duplicate_status(fh, "pending_review")  # 恢复为待处理
            else:
                ctx.store.update_duplicate_status(fh, "ignored")
            processed.append(fh)
        _respond_json(handler, {"ok": True, "processed": processed, "remaining": ctx.store.get_duplicate_count()})
    except Exception as e:
        _respond_json(handler, {"ok": False, "error": str(e)}, 500)


def post_duplicates_ignore(handler, data):
    try:
        fhashes = data.get("fhashes", [])
        if not fhashes:
            _respond_json(handler, {"ok": False, "error": "需要 fhashes"}, 400)
            return
        for fh in fhashes:
            ctx.store.update_duplicate_status(fh, "ignored")
            ctx.store.remove_duplicate(fh)
        _respond_json(handler, {"ok": True, "remaining": ctx.store.get_duplicate_count()})
    except Exception as e:
        _respond_json(handler, {"ok": False, "error": str(e)}, 500)


def post_backup(handler, data):
    try:
        backup_cfg = ctx.config.get("backup", {})
        db_path = ctx.config.get("database", {}).get("path", "data/recruitment.db")
        from backup import backup_database
        result = backup_database(
            db_path=db_path,
            backup_dir=backup_cfg.get("backup_dir", "backups"),
            keep_days=backup_cfg.get("keep_days", 30),
        )
        if result:
            _respond_json(handler, {"ok": True, "backup_file": result})
        else:
            _respond_json(handler, {"ok": False, "error": "备份失败：数据库文件不存在"}, 500)
    except Exception as e:
        _respond_json(handler, {"ok": False, "error": str(e)}, 500)


def post_cleanup(handler, data):
    try:
        from cleanup import cleanup_old_candidates
        ctx.logger.info("[cleanup] 手动触发清理…") if ctx.logger else None
        result = cleanup_old_candidates(ctx.store, ctx.config)
        ctx.last_cleanup_result = result
        total = result.get("results_cleaned", 0) + result.get("pending_cleaned", 0)
        _respond_json(handler, {
            "ok": True,
            "results_cleaned": result["results_cleaned"],
            "pending_cleaned": result["pending_cleaned"],
            "total_cleaned": total,
            "export_dir": result["export_dir"],
        })
    except Exception as e:
        _respond_json(handler, {"ok": False, "error": str(e)}, 500)


def post_funnel_analysis(handler, data):
    try:
        results = ctx.store.get_all_results(include_deleted=True)
        if not results:
            _respond_json(handler, {"ok": False, "error": "无数据可供分析"}, 400)
            return

        total = len(results)
        interviewing = sum(1 for r in results if r.get("pipeline_status") == "面试中")
        passed = sum(1 for r in results if r.get("pipeline_status") == "已通过")
        eliminated = sum(1 for r in results if r.get("pipeline_status") == "已淘汰")
        advanced = interviewing + passed + eliminated
        screening_to_interview = advanced / total if total > 0 else 0
        completed = passed + eliminated
        interview_to_passed = passed / completed if completed > 0 else 0

        baselines = ctx.config.get("funnel", {}).get("baselines", {})
        bl_s2i = baselines.get("待筛选_to_面试中", 0.40)
        bl_i2p = baselines.get("面试中_to_已通过", 0.35)

        gaps = [
            ("待筛选 → 面试中", screening_to_interview, bl_s2i, screening_to_interview - bl_s2i),
            ("面试中 → 已通过", interview_to_passed, bl_i2p, interview_to_passed - bl_i2p),
        ]
        gaps.sort(key=lambda x: x[3])
        bottleneck = gaps[0]

        analysis = ""
        suggestions = []
        try:
            llm = LLMClient(ctx.config)
            prompt = f"""你是一位资深招聘流程优化专家。请分析以下招聘漏斗数据并给出改进建议。

数据：总评估 {total} 人，面试中 {interviewing} 人，已通过 {passed} 人，已淘汰 {eliminated} 人。
转化率：待筛选→面试中 {screening_to_interview:.0%}（基线 {bl_s2i:.0%}），面试中→已通过 {interview_to_passed:.0%}（基线 {bl_i2p:.0%}）。
最大瓶颈：{bottleneck[0]}，偏离基线 {bottleneck[3]:.0%}。

请简短分析瓶颈可能原因（2-3句）并给出1-3条具体可操作建议。返回JSON：
{{"analysis": "瓶颈原因分析", "suggestions": ["建议1", "建议2"]}}"""
            content = llm.chat([{"role": "user", "content": prompt}], temperature=0.5, max_tokens=1024)
            ai_result = parse_json(content)
            analysis = ai_result.get("analysis", "")
            suggestions = ai_result.get("suggestions", [])
        except Exception as e:
            ctx.logger.warning("漏斗 LLM 分析失败: %s", e) if ctx.logger else None
            analysis = "AI 分析暂时不可用"
            suggestions = ["请稍后重试或在管理后台手动分析"]

        _respond_json(handler, {
            "ok": True, "total": total,
            "stages": {
                "screening": {"count": total - interviewing - passed - eliminated, "label": "待筛选"},
                "interviewing": {"count": interviewing, "label": "面试中"},
                "passed": {"count": passed, "label": "已通过"},
                "eliminated": {"count": eliminated, "label": "已淘汰"},
            },
            "conversions": [
                {"stage": "待筛选 → 面试中", "current_rate": round(screening_to_interview, 3),
                 "baseline_rate": bl_s2i, "gap": round(screening_to_interview - bl_s2i, 3)},
                {"stage": "面试中 → 已通过", "current_rate": round(interview_to_passed, 3),
                 "baseline_rate": bl_i2p, "gap": round(interview_to_passed - bl_i2p, 3)},
            ],
            "bottleneck_stage": bottleneck[0],
            "bottleneck_gap": round(bottleneck[3], 3),
            "analysis": analysis,
            "suggestions": suggestions,
        })
    except Exception as e:
        _respond_json(handler, {"ok": False, "error": str(e)}, 500)


def post_extract_features(handler, data):
    try:
        rid = data.get("id")
        if not rid:
            _respond_json(handler, {"ok": False, "error": "需要 id"}, 400)
            return
        result = ctx.store.get_result_by_id(rid)
        if not result:
            _respond_json(handler, {"ok": False, "error": "候选人不存在"}, 404)
            return
        filename = os.path.basename(result.get("resume_file", ""))
        if not filename:
            _respond_json(handler, {"ok": False, "error": "原始简历文件名无效"}, 400)
            return
        download_path = _find_resume_file(filename)
        if not download_path:
            _respond_json(handler, {"ok": False, "error": "原始简历文件已不存在"}, 404)
            return

        resume_text = extract_text(download_path)
        position_name = result["matched_position"]
        pos_config = next((p for p in ctx.config.get("positions", []) if p["name"] == position_name), {})
        features = extract_reference_features(resume_text, pos_config, ctx.config)
        ctx.store.add_reference_features(position_name, result["candidate_name"], rid, features)

        _respond_json(handler, {"ok": True, "features": features, "position": position_name,
                               "candidate_name": result["candidate_name"]})
    except Exception as e:
        _respond_json(handler, {"ok": False, "error": str(e)}, 500)


def post_pc_save(handler, data):
    try:
        pos_name = data.get("position_name")
        updates = data.get("updates", {})
        change_summary = data.get("change_summary", "")
        change_source = data.get("change_source", "manual")
        if not pos_name or not updates:
            _respond_json(handler, {"ok": False, "error": "需要 position_name 和 updates"}, 400)
            return
        pos = next((p for p in ctx.config.get("positions", []) if p["name"] == pos_name), None)
        if not pos:
            _respond_json(handler, {"ok": False, "error": f"岗位不存在: {pos_name}"}, 404)
            return

        # v5: 保存前记录版本历史
        changed_fields = list(updates.keys())
        try:
            ctx.store.save_config_history(
                pos_name, dict(pos),
                changed_fields=changed_fields,
                change_summary=change_summary or f"修改了: {', '.join(changed_fields)}",
                change_source=change_source,
            )
        except Exception:
            pass  # 历史记录失败不影响保存

        allowed = {"dimensions", "must_have", "nice_to_have", "education", "experience", "other_requirements", "scoring"}
        for key, val in updates.items():
            if key in allowed:
                pos[key] = val
        from utils.config import save_config
        config_path = os.path.join(ctx.project_dir, "config.yaml")
        save_config(ctx.config, config_path)
        _respond_json(handler, {"ok": True, "position_name": pos_name})
    except Exception as e:
        _respond_json(handler, {"ok": False, "error": str(e)}, 500)


def post_pc_toggle(handler, data):
    try:
        pos_name = data.get("position_name")
        enabled = data.get("enabled", True)
        if not pos_name:
            _respond_json(handler, {"ok": False, "error": "需要 position_name"}, 400)
            return
        pos = next((p for p in ctx.config.get("positions", []) if p["name"] == pos_name), None)
        if not pos:
            _respond_json(handler, {"ok": False, "error": f"岗位不存在: {pos_name}"}, 404)
            return
        pos["enabled"] = enabled
        from utils.config import save_config
        config_path = os.path.join(ctx.project_dir, "config.yaml")
        save_config(ctx.config, config_path)
        _respond_json(handler, {"ok": True, "position_name": pos_name, "enabled": enabled})
    except Exception as e:
        _respond_json(handler, {"ok": False, "error": str(e)}, 500)


def post_pc_llm_edit(handler, data):
    try:
        pos_name = data.get("position_name")
        instruction = data.get("instruction", "")
        if not pos_name or not instruction:
            _respond_json(handler, {"ok": False, "error": "需要 position_name 和 instruction"}, 400)
            return
        pos = next((p for p in ctx.config.get("positions", []) if p["name"] == pos_name), None)
        if not pos:
            _respond_json(handler, {"ok": False, "error": f"岗位不存在: {pos_name}"}, 404)
            return

        import json as _json
        current = {
            "name": pos["name"],
            "dimensions": [{"name": d["name"], "weight": d["weight"]} for d in pos.get("dimensions", [])],
            "must_have": pos.get("must_have", []),
            "nice_to_have": pos.get("nice_to_have", []),
            "education": pos.get("education", ""),
            "experience": pos.get("experience", ""),
            "other_requirements": pos.get("other_requirements", ""),
            "scoring": pos.get("scoring", {}),
        }

        llm = LLMClient(ctx.config)
        prompt = f"""你是一位招聘标准制定专家。以下是「{pos_name}」岗位当前的评估标准：

{_json.dumps(current, ensure_ascii=False, indent=2)}

用户要求对此标准进行以下修改："{instruction}"

请根据用户的要求，生成修改后的完整评估标准。注意：
1. 只修改用户要求的部分，其他部分保持原样
2. must_have、nice_to_have 等列表项保持序号逻辑
3. scoring 中的评分指南要与 must_have/nice_to_have 的变更保持一致

请返回一个 JSON，格式为：
{{
  "changes_summary": "修改了什么，一句话说明",
  "updated": {{ ... 完整的修改后配置（和上面 current 一样的结构） }}
}}"""

        content = llm.chat(
            [
                {"role": "system", "content": "你是一位招聘标准制定专家。只返回 JSON，不要其他内容。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
            max_tokens=4096,
        )
        content = content.strip()
        if content.startswith("```"):
            lines = content.split("\n")
            lines = [l for l in lines if not l.startswith("```")]
            content = "\n".join(lines)
        proposal = _json.loads(content)

        _respond_json(handler, {"ok": True, "proposal": proposal})
    except Exception as e:
        _respond_json(handler, {"ok": False, "error": str(e)}, 500)


# ═══════════════════════════════════════════════════════════
# v3: 新增功能端点
# ═══════════════════════════════════════════════════════════


def get_scoring_audit(handler):
    """评分质量审计 API — 检查 LLM 评分质量和一致性。"""
    report = _audit_all(ctx.store)
    _respond_json(handler, {"ok": True, "report": report})


def get_reference_list(handler):
    """列出所有标杆简历。"""
    import os as _os
    refs_root = _os.path.join(ctx.project_dir, "references")
    result = {}
    for pos_name in _os.listdir(refs_root) if _os.path.isdir(refs_root) else []:
        pos_dir = _os.path.join(refs_root, pos_name)
        if _os.path.isdir(pos_dir):
            files = [f for f in _os.listdir(pos_dir) if _os.path.isfile(_os.path.join(pos_dir, f))]
            result[pos_name] = {"count": len(files), "files": files}
    _respond_json(handler, {"ok": True, "references": result})


def post_reference_delete(handler, data):
    """删除标杆简历文件。"""
    import os as _os
    pos_name = data.get("position")
    filename = data.get("filename")
    if not pos_name or not filename:
        _respond_json(handler, {"ok": False, "error": "需要 position 和 filename"}, 400)
        return
    refs_root = _os.path.join(ctx.project_dir, "references")
    filepath = _os.path.join(refs_root, pos_name, filename)
    if not _os.path.realpath(filepath).startswith(_os.path.realpath(refs_root)):
        _respond_json(handler, {"ok": False, "error": "非法路径"}, 400)
        return
    if _os.path.exists(filepath):
        _os.remove(filepath)
        _respond_json(handler, {"ok": True, "deleted": filename})
    else:
        _respond_json(handler, {"ok": False, "error": "文件不存在"}, 404)


def post_interview_feedback(handler, data):
    """记录面试反馈 — v4: 写入结构化表 interview_feedback。"""
    try:
        rid = data.get("id")
        accuracy = data.get("accuracy", "")  # "accurate" | "overrated" | "underrated"
        interview_score = data.get("interview_score", 0)  # 1-10
        note = data.get("note", "")
        dimensions_feedback = data.get("dimensions_feedback", {})

        if not rid or not accuracy:
            _respond_json(handler, {"ok": False, "error": "需要 id 和 accuracy"}, 400)
            return

        result = ctx.store.get_result_by_id(rid)
        if not result:
            _respond_json(handler, {"ok": False, "error": "候选人不存在"}, 404)
            return

        # v4: 写入结构化表
        feedback_id = ctx.store.add_interview_feedback(rid, {
            "accuracy": accuracy,
            "interview_score": interview_score,
            "note": note,
            "system_score": result.get("match_score", 0),
            "system_verdict": result.get("verdict", ""),
            "dimensions_feedback": dimensions_feedback,
            "recorded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })

        # 同时保留旧版兼容（notes 字段）
        import json as _json
        existing_notes = result.get("notes", "")
        feedback_legacy = {
            "accuracy": accuracy,
            "interview_score": interview_score,
            "note": note,
            "system_score": result.get("match_score", 0),
            "system_verdict": result.get("verdict", ""),
            "recorded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        new_notes = existing_notes + "\n" if existing_notes else ""
        new_notes += f"[面试反馈] {_json.dumps(feedback_legacy, ensure_ascii=False)}"
        ctx.store.update_result(rid, {"notes": new_notes})

        _respond_json(handler, {"ok": True, "feedback_id": feedback_id,
                                "feedback": feedback_legacy})
    except Exception as e:
        _respond_json(handler, {"ok": False, "error": str(e)}, 500)


def get_export_csv(handler):
    """导出候选人数据为 CSV（兼容 Excel 直接打开）。支持 include_deleted 参数。"""
    qs = parse_qs(urlparse(handler.path).query)
    position = qs.get("position", [None])[0]
    search = qs.get("search", [None])[0]
    include_deleted = qs.get("include_deleted", ["0"])[0] == "1"

    if position:
        results = ctx.store.get_results_by_position(position, include_deleted=include_deleted)
    else:
        results = ctx.store.get_all_results(include_deleted=include_deleted)

    # 搜索过滤
    if search:
        s = search.lower()
        results = [r for r in results if s in r.get("candidate_name", "").lower()
                   or s in r.get("matched_position", "").lower()]

    import csv, io
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["候选人", "岗位", "匹配度", "评估结论", "Pipeline状态",
                     "技能匹配", "经验匹配", "学历匹配", "综合评价", "评估时间",
                     "亮点", "风险点"])
    for r in sorted(results, key=lambda x: x.get("match_score", 0), reverse=True):
        dims = r.get("dimensions", {})
        writer.writerow([
            r.get("candidate_name", ""),
            r.get("matched_position", ""),
            r.get("match_score", ""),
            r.get("verdict", ""),
            r.get("pipeline_status", ""),
            dims.get("skill_match", {}).get("score", ""),
            dims.get("experience_match", {}).get("score", ""),
            dims.get("education_match", {}).get("score", ""),
            dims.get("overall", {}).get("score", ""),
            (r.get("timestamp", "") or "")[:10],
            "; ".join(r.get("highlights", [])),
            "; ".join(r.get("risks", [])),
        ])

    body = output.getvalue().encode("utf-8-sig")  # BOM for Excel
    fname = f'候选人数据_{datetime.now().strftime("%Y%m%d")}.csv'
    handler.send_response(200)
    handler.send_header("Content-Type", "text/csv; charset=utf-8")
    handler.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(fname)}")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def get_search_candidates(handler):
    """候选人搜索 API — 支持姓名模糊搜索 + 含已删除。"""
    qs = parse_qs(urlparse(handler.path).query)
    query = qs.get("q", [""])[0].strip()
    position = qs.get("position", [None])[0]
    limit = int(qs.get("limit", ["50"])[0])
    include_deleted = qs.get("include_deleted", ["0"])[0] == "1"

    if position:
        results = ctx.store.get_results_by_position(position, include_deleted=include_deleted)
    else:
        results = ctx.store.get_all_results(include_deleted=include_deleted)

    if query:
        q = query.lower()
        results = [r for r in results if q in r.get("candidate_name", "").lower()
                   or q in r.get("matched_position", "").lower()
                   or q in r.get("summary", "").lower()]

    # 按匹配度排序，限制数量
    results = sorted(results, key=lambda x: x.get("match_score", 0), reverse=True)[:limit]
    _respond_json(handler, {"ok": True, "total": len(results), "results": results})


def get_resume_full_text(handler):
    """获取简历完整文本（v3: 可配置截断长度）。"""
    qs = parse_qs(urlparse(handler.path).query)
    fname = qs.get("file", [None])[0]
    max_chars = int(qs.get("max_chars", ["10000"])[0])
    if not fname:
        _respond_json(handler, {"ok": False, "error": "需要 file 参数"}, 400)
        return
    safe_name = os.path.basename(fname)
    if safe_name != fname or ".." in fname:
        _respond_json(handler, {"ok": False, "error": "非法文件名"}, 400)
        return
    fpath = _find_resume_file(safe_name)
    if not fpath:
        _respond_json(handler, {"ok": False, "error": "文件不存在"}, 404)
        return
    try:
        text = extract_text(fpath)
        _respond_json(handler, {"ok": True, "text": text[:max_chars], "full_length": len(text),
                                "truncated": len(text) > max_chars})
    except Exception as e:
        _respond_json(handler, {"ok": False, "error": str(e)}, 500)


# ═══════════════════════════════════════════════════════════
# v4: 新增 POST 端点
# ═══════════════════════════════════════════════════════════


def post_regression_run(handler, data):
    """触发评估标准回归测试 — 用历史简历对比新旧标准。"""
    try:
        position = data.get("position", "")
        days = int(data.get("days", 30))
        version_tag = data.get("version_tag", datetime.now().strftime("reg_%Y%m%d_%H%M%S"))

        if not position:
            _respond_json(handler, {"ok": False, "error": "需要 position 参数"}, 400)
            return

        # 获取该岗位最近 N 天已评估的候选人
        results = ctx.store.get_results_by_position(position)
        now = datetime.now()
        recent = []
        for r in results:
            ts = r.get("timestamp", "")
            if ts:
                try:
                    rt = datetime.strptime(ts[:10], "%Y-%m-%d")
                    if (now - rt).days <= days:
                        recent.append(r)
                except ValueError:
                    pass

        if not recent:
            _respond_json(handler, {"ok": False, "error": f"最近{days}天该岗位无评估记录"}, 400)
            return

        # 获取当前岗位配置
        pos = next((p for p in ctx.config.get("positions", []) if p["name"] == position), None)
        if not pos:
            _respond_json(handler, {"ok": False, "error": f"岗位不存在: {position}"}, 404)
            return

        # 重新评估每个候选人
        llm = LLMClient(ctx.config)
        ref_text = ctx.references.get(position, "") if ctx.references else ""
        reg_results = []
        score_changes = []

        for r in recent:
            # 尝试读取原始简历
            fname = r.get("resume_file", "")
            fpath = _find_resume_file(fname)
            if not fpath:
                continue

            try:
                text = extract_text(fpath)
                dimensions = _get_dimensions(pos)
                eval_system = _build_eval_system(dimensions)
                content = llm.chat(
                    [
                        {"role": "system", "content": eval_system},
                        {"role": "user", "content": build_eval_prompt(text, pos, ref_text)},
                    ],
                    temperature=0.3,
                    max_tokens=4096,
                )
                new_result = parse_json(content)
                dims = new_result.get("dimensions", {})
            except Exception as e:
                ctx.logger.warning("回归评估失败: %s — %s", r.get("candidate_name", ""), e) if ctx.logger else None
                continue

            old_dims = r.get("dimensions", {})
            old_score = r.get("match_score", 0)
            new_score = new_result.get("match_score", 0) or 0
            if new_score > 100:
                new_score = min(new_score, 100)

            reg_data = {
                "result_id": r["id"],
                "position_name": position,
                "old_score": old_score,
                "new_score": new_score,
                "old_verdict": r.get("verdict", ""),
                "new_verdict": new_result.get("verdict", ""),
                "old_dimensions": {k: v.get("score", 0) for k, v in old_dims.items()} if isinstance(old_dims, dict) else {},
                "new_dimensions": {k: v.get("score", 0) for k, v in dims.items()} if isinstance(dims, dict) else {},
                "version_tag": version_tag,
            }
            rid = ctx.store.save_regression_result(reg_data)
            score_changes.append(new_score - old_score)
            reg_results.append({"candidate": r.get("candidate_name", ""),
                                "old_score": old_score, "new_score": new_score,
                                "change": new_score - old_score})

        avg_change = round(sum(score_changes) / len(score_changes), 1) if score_changes else 0
        verdict_changes = sum(1 for x in reg_results if x["old_score"] != x["new_score"] and
                              (x["old_score"] >= 85) != (x["new_score"] >= 85))

        _respond_json(handler, {
            "ok": True,
            "version_tag": version_tag,
            "position": position,
            "tested": len(reg_results),
            "avg_score_change": avg_change,
            "verdict_impact_count": verdict_changes,
            "details": reg_results,
        })
    except Exception as e:
        _respond_json(handler, {"ok": False, "error": str(e)}, 500)


def post_cross_validation_trigger(handler, data):
    """手动触发单个候选人的 Claude 交叉校验。"""
    try:
        rid = data.get("id")
        if not rid:
            _respond_json(handler, {"ok": False, "error": "需要 id"}, 400)
            return

        result = ctx.store.get_result_by_id(rid)
        if not result:
            _respond_json(handler, {"ok": False, "error": "候选人不存在"}, 404)
            return

        from cross_validator import CrossValidator
        validator = CrossValidator(ctx.config)
        if not validator.is_enabled:
            _respond_json(handler, {"ok": False, "error": "Claude 交叉校验未启用（检查 cross_validation 配置）"}, 400)
            return

        fname = result.get("resume_file", "")
        fpath = _find_resume_file(fname)
        if not fpath:
            _respond_json(handler, {"ok": False, "error": "原始简历文件不存在"}, 404)
            return

        text = extract_text(fpath)
        pname = result.get("matched_position", "")
        pos = next((p for p in ctx.config.get("positions", []) if p["name"] == pname), None)
        if not pos:
            _respond_json(handler, {"ok": False, "error": f"岗位不存在: {pname}"}, 404)
            return

        cv = validator.validate(text, result, pos)
        if not cv:
            _respond_json(handler, {"ok": False, "error": "Claude 校验失败（查看日志）"}, 500)
            return

        cv["result_id"] = rid
        cv_id = ctx.store.save_cross_validation(cv)

        _respond_json(handler, {
            "ok": True,
            "cv_id": cv_id,
            "claude_score": cv["claude_score"],
            "claude_verdict": cv["claude_verdict"],
            "score_diff": cv["score_diff"],
            "agreement": cv["agreement"],
        })
    except Exception as e:
        _respond_json(handler, {"ok": False, "error": str(e)}, 500)


# ═══════════════════════════════════════════════════════════
# v5: 标准编辑体验升级端点
# ═══════════════════════════════════════════════════════════


def get_pc_history(handler):
    """获取岗位配置版本历史。"""
    qs = parse_qs(urlparse(handler.path).query)
    pos_name = qs.get("position", [None])[0]
    if not pos_name:
        _respond_json(handler, {"ok": False, "error": "需要 position 参数"}, 400)
        return
    history = ctx.store.get_config_history(pos_name)
    _respond_json(handler, {"ok": True, "position": pos_name, "history": history})


def get_pc_version(handler):
    """获取某个版本的完整配置快照。"""
    qs = parse_qs(urlparse(handler.path).query)
    version_id = qs.get("id", [None])[0]
    if not version_id:
        _respond_json(handler, {"ok": False, "error": "需要 id 参数"}, 400)
        return
    snapshot = ctx.store.get_config_version(int(version_id))
    if not snapshot:
        _respond_json(handler, {"ok": False, "error": "版本不存在"}, 404)
        return
    _respond_json(handler, {"ok": True, "snapshot": snapshot})


def post_pc_restore(handler, data):
    """恢复到指定版本。"""
    try:
        pos_name = data.get("position_name")
        version_id = data.get("history_id")
        if not pos_name or not version_id:
            _respond_json(handler, {"ok": False, "error": "需要 position_name 和 history_id"}, 400)
            return
        snapshot = ctx.store.get_config_version(int(version_id))
        if not snapshot:
            _respond_json(handler, {"ok": False, "error": "版本不存在"}, 404)
            return
        pos = next((p for p in ctx.config.get("positions", []) if p["name"] == pos_name), None)
        if not pos:
            _respond_json(handler, {"ok": False, "error": f"岗位不存在: {pos_name}"}, 404)
            return

        # 恢复前保存当前版本
        try:
            ctx.store.save_config_history(pos_name, dict(pos),
                                          changed_fields=["_restore"],
                                          change_summary=f"恢复至版本 {version_id} 前的自动备份",
                                          change_source="restore")
        except Exception:
            pass

        # 用快照覆盖当前配置
        snap_config = snapshot.get("config_snapshot", {})
        for key in ("dimensions", "must_have", "nice_to_have", "education",
                     "experience", "other_requirements", "scoring"):
            if key in snap_config:
                pos[key] = snap_config[key]

        from utils.config import save_config
        config_path = os.path.join(ctx.project_dir, "config.yaml")
        save_config(ctx.config, config_path)
        _respond_json(handler, {"ok": True, "position_name": pos_name, "restored_from": version_id})
    except Exception as e:
        _respond_json(handler, {"ok": False, "error": str(e)}, 500)


def get_pc_preview_prompt(handler):
    """预览当前评估标准生成的 prompt（不含简历，仅评估框架部分）。"""
    qs = parse_qs(urlparse(handler.path).query)
    pos_name = qs.get("position", [None])[0]
    if not pos_name:
        _respond_json(handler, {"ok": False, "error": "需要 position 参数"}, 400)
        return
    pos = next((p for p in ctx.config.get("positions", []) if p["name"] == pos_name), None)
    if not pos:
        _respond_json(handler, {"ok": False, "error": f"岗位不存在: {pos_name}"}, 404)
        return

    from evaluator import build_eval_prompt as _build
    dimensions = _get_dimensions(pos)
    eval_system = _build_eval_system(dimensions)
    user_prompt = _build("【候选人简历将在此处插入】", pos)

    _respond_json(handler, {
        "ok": True,
        "position": pos_name,
        "system_prompt": eval_system,
        "user_prompt": user_prompt,
        "total_chars": len(eval_system) + len(user_prompt),
    })


# ═══════════════════════════════════════════════════════════
# 新增: 引导页 & 简历上传 (v6 — 同事共享)
# ═══════════════════════════════════════════════════════════

_SETUP_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>简历评估系统 — 初始设置</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
       background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
       min-height: 100vh; display: flex; align-items: center; justify-content: center; }
.card { background: white; border-radius: 16px; padding: 40px;
        max-width: 480px; width: 90%; box-shadow: 0 20px 60px rgba(0,0,0,0.3); }
h1 { font-size: 24px; margin-bottom: 8px; color: #333; text-align: center; }
.sub { text-align: center; color: #888; font-size: 14px; margin-bottom: 28px; }
label { display: block; font-weight: 600; margin: 16px 0 6px; color: #444; }
input, select { width: 100%; padding: 12px; border: 2px solid #e0e0e0;
                border-radius: 8px; font-size: 15px; transition: border-color .2s; }
input:focus, select:focus { outline: none; border-color: #667eea; }
.btn { width: 100%; padding: 14px; background: linear-gradient(135deg, #667eea, #764ba2);
       color: white; border: none; border-radius: 8px; font-size: 16px; font-weight: 600;
       cursor: pointer; margin-top: 24px; transition: transform .1s; }
.btn:hover { transform: scale(1.02); }
.btn:disabled { opacity: 0.5; cursor: not-allowed; }
.hint { font-size: 12px; color: #999; margin-top: 4px; }
.success { color: #2ecc71; text-align: center; margin-top: 16px; display: none; }
.error { color: #e74c3c; text-align: center; margin-top: 16px; display: none; }
</style>
</head>
<body>
<div class="card">
<h1>🚀 简历评估系统</h1>
<p class="sub">首次使用，请完成基本设置</p>
<form id="setupForm">
  <label>🔑 DeepSeek API Key <span style="color:red">*</span></label>
  <input type="password" id="apiKey" placeholder="sk-..." required>
  <p class="hint">从 platform.deepseek.com 获取</p>

  <label>👤 你的名字</label>
  <input type="text" id="userName" placeholder="张三">

  <label>📁 简历监控目录</label>
  <input type="text" id="watchDir" placeholder="自动检测">
  <p class="hint">留空则使用默认下载目录。简历文件放这里自动评估。</p>

  <button type="submit" class="btn" id="submitBtn">开始使用</button>
  <p class="success" id="successMsg">✅ 设置保存成功，正在跳转...</p>
  <p class="error" id="errorMsg"></p>
</form>
</div>
<script>
document.getElementById('setupForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const btn = document.getElementById('submitBtn');
  btn.disabled = true; btn.textContent = '保存中...';
  document.getElementById('errorMsg').style.display = 'none';
  try {
    const resp = await fetch('/api/setup', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        api_key: document.getElementById('apiKey').value,
        user_name: document.getElementById('userName').value,
        watch_dir: document.getElementById('watchDir').value,
      })
    });
    const data = await resp.json();
    if (data.ok) {
      document.getElementById('successMsg').style.display = 'block';
      setTimeout(() => { window.location.href = '/dashboard.html'; }, 1500);
    } else {
      document.getElementById('errorMsg').textContent = data.error || '保存失败';
      document.getElementById('errorMsg').style.display = 'block';
      btn.disabled = false; btn.textContent = '开始使用';
    }
  } catch(err) {
    document.getElementById('errorMsg').textContent = '网络错误: ' + err.message;
    document.getElementById('errorMsg').style.display = 'block';
    btn.disabled = false; btn.textContent = '开始使用';
  }
});
// 预填默认监控目录
(async () => {
  try {
    const resp = await fetch('/api/setup/defaults');
    const data = await resp.json();
    if (data.watch_dir) {
      document.getElementById('watchDir').placeholder = data.watch_dir;
    }
  } catch(e) {}
})();
</script>
</body>
</html>"""


def get_setup_page(handler):
    """首次启动引导页。如果已配置则重定向到 dashboard。"""
    from utils.config import get_auth_token
    # 检查是否已配置（token 存在即认为已配置）
    token = get_auth_token()
    if token:
        handler.send_response(302)
        handler.send_header("Location", "/dashboard.html")
        handler.end_headers()
        return
    body = _SETUP_HTML.encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def get_setup_defaults(handler):
    """返回默认配置值（监控目录等）。"""
    from utils.paths import get_downloads_dir
    _respond_json(handler, {
        "ok": True,
        "watch_dir": get_downloads_dir(),
    })


def post_setup(handler):
    """保存首次设置到 .env。"""
    body = _read_body(handler)
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        _respond_json(handler, {"ok": False, "error": "JSON 格式错误"}, 400)
        return

    api_key = data.get("api_key", "").strip()
    if not api_key:
        _respond_json(handler, {"ok": False, "error": "API Key 不能为空"}, 400)
        return

    user_name = data.get("user_name", "").strip()
    watch_dir = data.get("watch_dir", "").strip()

    # 写入 .env
    from utils.paths import get_env_path, get_downloads_dir
    env_path = get_env_path()
    watch = watch_dir or get_downloads_dir()
    env_content = f"""# 简历评估系统配置
DEEPSEEK_API_KEY={api_key}
AUTH_TOKEN=resume_eval_{hash(api_key) % 900000 + 100000:06d}
USER_NAME={user_name}
WATCH_DIR={watch}
"""
    with open(env_path, "w", encoding="utf-8") as f:
        f.write(env_content)

    ctx.logger and ctx.logger.info("初始化设置完成 (user=%s, watch=%s)", user_name or "未填", watch)
    _respond_json(handler, {"ok": True, "message": "设置保存成功"})


def post_upload_resume(handler):
    """网页上传简历文件。"""
    content_type = handler.headers.get("Content-Type", "")
    if "multipart/form-data" not in content_type:
        _respond_json(handler, {"ok": False, "error": "需要 multipart/form-data"}, 400)
        return

    # 解析 multipart
    boundary = None
    for part in content_type.split(";"):
        part = part.strip()
        if part.startswith("boundary="):
            boundary = part[len("boundary="):].strip('"')
            break
    if not boundary:
        _respond_json(handler, {"ok": False, "error": "缺少 boundary"}, 400)
        return

    content_length = int(handler.headers.get("Content-Length", "0"))
    raw_body = handler.rfile.read(content_length)
    boundary_bytes = boundary.encode("utf-8")

    # 简单 multipart 解析（只取第一个文件）
    parts = raw_body.split(b"--" + boundary_bytes)
    for part in parts:
        if b"Content-Disposition" not in part:
            continue
        if b"filename=" not in part:
            continue

        # 提取文件名
        header_end = part.find(b"\r\n\r\n")
        if header_end == -1:
            continue
        header_section = part[:header_end].decode("utf-8", errors="ignore")
        file_data = part[header_end + 4:]
        # 去掉末尾的 \r\n-- 或 \r\n
        if file_data.endswith(b"\r\n"):
            file_data = file_data[:-2]

        # 提取原始文件名
        import re
        match = re.search(r'filename="([^"]*)"', header_section)
        if not match:
            continue
        orig_filename = match.group(1)

        # 保存文件
        from utils.paths import get_resumes_dir
        import time as _time
        safe_name = f"upload_{int(_time.time() * 1000)}_{orig_filename}"
        filepath = os.path.join(get_resumes_dir(), safe_name)
        os.makedirs(get_resumes_dir(), exist_ok=True)
        with open(filepath, "wb") as f:
            f.write(file_data)

        ctx.logger and ctx.logger.info("网页上传: %s -> %s (%d bytes)", orig_filename, safe_name, len(file_data))

        # 触发处理
        try:
            _proc(filepath)
        except Exception as e:
            ctx.logger and ctx.logger.error("上传处理失败: %s", e)

        _respond_json(handler, {
            "ok": True,
            "filename": orig_filename,
            "saved_as": safe_name,
            "size": len(file_data),
        })
        return

    _respond_json(handler, {"ok": False, "error": "未找到上传文件"}, 400)


# ═══════════════════════════════════════════════════════════
# 路由表
# ═══════════════════════════════════════════════════════════

GET_ROUTES = {
    "/api/health": get_health,
    "/api/health/deep": get_health_deep,
    "/api/results": get_results,
    "/api/pending": get_pending,
    "/api/positions": get_positions,
    "/api/position-config": get_position_config,
    "/api/deleted": get_deleted,
    "/api/approvals": get_approvals,
    "/api/duplicates": get_duplicates,
    "/api/export/pdf": get_export_pdf,
    "/api/export/csv": get_export_csv,
    "/api/search": get_search_candidates,
    "/api/search/resumes": get_search_resumes,
    "/api/resume-text": get_resume_text,
    "/api/resume-full": get_resume_full_text,
    "/api/compare": get_compare,
    "/api/stats/eval-trends": get_eval_trends,
    "/api/stats/eval-reliability": get_eval_reliability,
    "/metrics": get_metrics,
    "/api/references/features": get_reference_features,
    "/api/references/list": get_reference_list,
    "/api/stats/dimensions": get_dimension_stats,
    "/api/audit/scores": get_scoring_audit,
    "/api/events": get_sse_events,
    "/api/cross-validation/stats": get_cross_validation_stats,
    "/api/cross-validation/list": get_cross_validation_list,
    "/api/feedback/accuracy": get_feedback_accuracy,
    "/api/regression/list": get_regression_list,
    "/api/position-config/history": get_pc_history,
    "/api/position-config/version": get_pc_version,
    "/api/position-config/preview-prompt": get_pc_preview_prompt,
    "/setup": get_setup_page,
    "/api/setup/defaults": get_setup_defaults,
}

POST_ROUTES = {
    "/api/save": post_save,
    "/api/delete": post_delete,
    "/api/undo": post_undo,
    "/api/assign": post_assign,
    "/api/pending/dismiss": post_dismiss,
    "/api/rank": post_rank,
    "/api/transition": post_transition,
    "/api/promote-to-reference": post_promote_reference,
    "/api/approve-reference": post_approve_reference,
    "/api/verify": post_verify,
    "/api/evaluate/batch": post_batch_evaluate,
    "/api/queue": post_queue,
    "/api/duplicates/reevaluate": post_duplicates_reevaluate,
    "/api/duplicates/ignore": post_duplicates_ignore,
    "/api/backup": post_backup,
    "/api/cleanup": post_cleanup,
    "/api/insights/funnel-analysis": post_funnel_analysis,
    "/api/references/extract-features": post_extract_features,
    "/api/position-config/save": post_pc_save,
    "/api/position-config/toggle": post_pc_toggle,
    "/api/position-config/llm-edit": post_pc_llm_edit,
    "/api/references/delete": post_reference_delete,
    "/api/feedback": post_interview_feedback,
    "/api/regression/run": post_regression_run,
    "/api/cross-validation/trigger": post_cross_validation_trigger,
    "/api/position-config/restore": post_pc_restore,
    "/api/setup": post_setup,
    "/api/upload": post_upload_resume,
}
