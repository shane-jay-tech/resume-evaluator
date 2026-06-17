"""简历自动评估系统 v2 — 多岗位匹配 + 桌面通知 + 汇总面板。"""

import json
import os
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

if sys.platform == "win32":
    from watchdog.observers.polling import PollingObserver as Observer
else:
    from watchdog.observers import Observer

from database import DataStore
from sse import SSEManager
from archive import archive_old_results
from cleanup import cleanup_old_candidates
from backup import start_backup_scheduler
from metrics import metrics
from utils.logger import setup_logging
from utils.config import load_config as load_yaml_config, get_auth_token
from app_routes import GET_ROUTES, POST_ROUTES, ctx as route_ctx, _respond_json
from file_watcher import ResumeHandler, scan_existing as _scan_existing
from services import process_resume, add_to_pending, load_references, generate_pdf

# ── 全局变量（在 main 中初始化） ──
store: DataStore = None
config: dict = {}
references: dict = {}
logger = None
project_dir: str = ""
sse_manager: SSEManager = None

_start_time = None
last_archive_count = 0
last_cleanup_result = {}


def notify(title: str, subtitle: str, message: str):
    """发送 macOS 系统通知。"""
    script = f'''
    display notification "{message}" with title "{title}" subtitle "{subtitle}" sound name "Glass"
    '''
    try:
        subprocess.run(["osascript", "-e", script], check=False)
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════
# 文件监控（使用 file_watcher 模块）
# ═══════════════════════════════════════════════════════════════════

def scan_existing(watch_dir: str):
    """委托给 file_watcher 模块。"""
    _scan_existing(watch_dir, store, _process_resume_wrapper)

def _process_resume_wrapper(filepath: str):
    """包装器: 传递全局变量给 services.process_resume。"""
    process_resume(filepath, store, config, references, sse_manager, logger, notify)


# ═══════════════════════════════════════════════════════════════════
# HTTP 服务器 + API
# ═══════════════════════════════════════════════════════════════════

def _check_auth(handler) -> bool:
    """Bearer token 认证。"""
    token = get_auth_token()
    if not token:
        return True  # 未配置 token 时跳过认证
    auth = handler.headers.get("Authorization", "")
    expected = f"Bearer {token}"
    return auth == expected


class APIHandler(SimpleHTTPRequestHandler):
    """REST API Handler — 使用路由表分发请求。"""

    extensions_map = {**SimpleHTTPRequestHandler.extensions_map}
    extensions_map.update({
        ".md": "text/plain; charset=utf-8",
        ".html": "text/html; charset=utf-8",
        ".json": "application/json; charset=utf-8",
        ".js": "application/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".csv": "text/csv; charset=utf-8",
    })

    # CORS origin 由 start_dashboard_server() 从配置注入
    cors_origin = "http://127.0.0.1:18980"

    def end_headers(self):
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", APIHandler.cors_origin)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    # ── GET ──────────────────────────────────────────

    def do_GET(self):
        path = self.path.split("?")[0]

        # v4: 数据敏感端点需要认证
        # 注意：/api/results、/api/pending、/api/positions、/api/position-config
        # 是 dashboard 核心数据端点，前端 GET 请求不传 token，保持在 localhost 下免认证
        sensitive_prefixes = ("/api/resume-text", "/api/resume-full",
                              "/api/export", "/api/search/resumes",
                              "/api/deleted", "/api/approvals", "/api/duplicates",
                              "/api/stats", "/api/audit",
                              "/api/cross-validation", "/api/feedback", "/api/regression")
        needs_auth = any(path.startswith(p) for p in sensitive_prefixes)
        if needs_auth and not _check_auth(self):
            _respond_json(self, {"ok": False, "error": "未授权"}, 401)
            return

        # 路由表查找
        handler = GET_ROUTES.get(path)
        if handler:
            handler(self)
            return

        # API 路径前缀匹配（兼容 /api/resume-text?file=xxx 等带子路径的）
        for route_path, handler in GET_ROUTES.items():
            if path.startswith(route_path):
                handler(self)
                return

        # fallback 到静态文件
        super().do_GET()

    # ── POST ─────────────────────────────────────────

    def do_POST(self):
        path = self.path.split("?")[0]

        # 认证（写操作需要 token）
        if path != "/api/health" and not _check_auth(self):
            _respond_json(self, {"ok": False, "error": "未授权"}, 401)
            return

        body = self._read_body()

        handler = POST_ROUTES.get(path)
        if handler:
            handler(self, body)
            return

        # 兼容子路径
        for route_path, handler in POST_ROUTES.items():
            if path.startswith(route_path):
                handler(self, body)
                return

        _respond_json(self, {"ok": False, "error": "unknown endpoint"}, 404)

    # ── Helpers ──────────────────────────────────────

    def _read_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length > 1_000_000:
                return {}
            return json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, ValueError):
            return {}

    def log_message(self, format, *args):
        pass  # 静默 HTTP 日志


def start_dashboard_server() -> ThreadingHTTPServer:
    """启动多线程 HTTP 服务器，支持 SSE 长连接。"""  # UPGRADE-P1+: HTTPServer → ThreadingHTTPServer
    host = config.get("server", {}).get("host", "127.0.0.1")
    port = config.get("server", {}).get("port", 18980)
    cors_origin = config.get("server", {}).get("cors_origin", "http://127.0.0.1:18980")
    APIHandler.cors_origin = cors_origin  # 注入到类级别，所有请求共享
    # 端口被占用时尝试下一个端口
    for offset in range(10):
        try:
            server = ThreadingHTTPServer((host, port + offset), APIHandler)
            if offset > 0:
                logger.warning("端口 %d 被占用，使用端口 %d", port, port + offset)
                config["server"]["_actual_port"] = port + offset
            t = threading.Thread(target=server.serve_forever, daemon=True)
            t.start()
            return server
        except OSError:
            continue
    raise OSError("无法启动服务器：18980-18989 端口均被占用")


# ═══════════════════════════════════════════════════════════════════
# 进程管理
# ═══════════════════════════════════════════════════════════════════

def cleanup_pidfile(pidfile: str):
    try:
        os.remove(pidfile)
    except Exception:
        pass


def kill_old_instance(pidfile: str):
    if not os.path.exists(pidfile):
        return
    try:
        with open(pidfile, "r") as f:
            old_pid = int(f.read().strip())
        os.kill(old_pid, signal.SIGTERM)
        logger.info("已终止旧进程 PID=%d", old_pid)
        time.sleep(1)
    except (ProcessLookupError, PermissionError):
        pass
    except Exception as e:
        logger.warning("清理旧进程失败: %s", e)


# ═══════════════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════════════


def start_cleanup_scheduler(interval_hours: int = 24):
    """后台定时清理线程。"""
    def _run():
        time.sleep(300)
        while True:
            try:
                logger.info("[cleanup] 开始定时清理检查…")
                result = cleanup_old_candidates(store, config)
                route_ctx.last_cleanup_result = result
                total = result.get("results_cleaned", 0) + result.get("pending_cleaned", 0)
                if total > 0:
                    logger.info("[cleanup] 定时清理完成: results %d, pending %d",
                               result["results_cleaned"], result["pending_cleaned"])
            except Exception as e:
                logger.warning("[cleanup] 定时清理失败: %s", e)
            time.sleep(interval_hours * 3600)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    logger.info("[cleanup] 定时清理已启动（间隔 %d 小时）", interval_hours)


def main():
    global store, config, references, logger, project_dir, _start_time, sse_manager
    _start_time = time.time()

    sse_manager = SSEManager()

    # ── 配置 ──
    # 优先使用命令行参数指定的路径，否则使用内置配置
    from utils.paths import get_config_path, get_user_dir
    if len(sys.argv) > 1:
        config_path = os.path.abspath(sys.argv[1])
    else:
        config_path = get_config_path()
    # 开发模式: cwd = 项目根目录; PyInstaller: cwd = 可写用户目录
    is_frozen = getattr(sys, 'frozen', False)
    if is_frozen:
        project_dir = get_user_dir()
    else:
        project_dir = os.path.dirname(os.path.abspath(config_path))
    os.chdir(project_dir)

    # PyInstaller 打包后，HTML 页面在资源目录中，复制到用户目录供服务器访问
    if getattr(sys, 'frozen', False):
        import shutil as _shutil
        from utils.paths import get_resource_dir
        res_dir = get_resource_dir()
        for fname in os.listdir(res_dir):
            if fname.endswith('.html') and not os.path.exists(os.path.join(project_dir, fname)):
                _shutil.copy2(os.path.join(res_dir, fname), os.path.join(project_dir, fname))
                logger.debug("复制页面: %s", fname)

    pidfile = os.path.join(project_dir, ".pid")

    if config_path.endswith(".yaml") or config_path.endswith(".yml"):
        try:
            config = load_yaml_config(config_path)
        except FileNotFoundError:
            # 配置缺失时使用内置最小配置（同事解压后也能跑）
            logger.warning("配置文件未找到，使用内置默认配置")
            config = {
                "server": {"host": "127.0.0.1", "port": 18980, "cors_origin": "http://127.0.0.1:18980"},
                "monitor": {"directories": ["~/Downloads"]},
                "database": {"path": "data/recruitment.db"},
                "logging": {"level": "INFO", "retention_days": 7, "path": "logs/app.log"},
                "llm": {"api_key_env": "LLM_API_KEY", "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat", "max_tokens": 4096, "temperature": 0.3, "max_retries": 2, "timeout": 300.0},
            }
    else:
        import json as _json
        with open(config_path, "r", encoding="utf-8") as f:
            config = _json.load(f)

    logger = setup_logging(config)
    kill_old_instance(pidfile)

    # ── 数据库 ──
    db_path = config.get("database", {}).get("path", "data/recruitment.db")
    store = DataStore(db_path)
    store.migrate_from_json(project_dir)

    # 归档
    try:
        archived = archive_old_results(store, config)
        route_ctx.last_archive_count = archived
        if archived:
            logger.info("[archive] 已归档 %d 条旧记录", archived)
    except Exception as e:
        logger.warning("[archive] 归档失败: %s", e)

    # ── 目录 ──
    watch_dirs = config.get("monitor", {}).get("directories", ["~/Downloads"])
    watch_dir = os.path.expanduser(watch_dirs[0])
    report_dir = os.path.expanduser(config.get("report_dir", "./reports"))
    if not os.path.isabs(report_dir):
        report_dir = os.path.join(project_dir, report_dir)
    config["report_dir"] = report_dir

    positions = config.get("positions", [])
    if not positions:
        logger.error("配置文件中未配置岗位")

    with open(pidfile, "w") as f:
        f.write(str(os.getpid()))

    logger.info("=" * 50)
    logger.info("简历自动评估系统启动 v2")
    logger.info("监控目录: %s", watch_dir)
    logger.info("报告目录: %s", report_dir)
    logger.info("数据库: %s", db_path)
    logger.info("配置岗位: %s", ", ".join(p["name"] for p in positions))
    logger.info("=" * 50)

    # ── 标杆简历 ──
    references = load_references(project_dir, positions)

    # ── 设置路由上下文 ──
    route_ctx.store = store
    route_ctx.config = config
    route_ctx.references = references
    route_ctx.project_dir = project_dir
    route_ctx.sse_manager = sse_manager
    route_ctx.logger = logger
    route_ctx.notify = notify

    # ── HTTP 服务 ──
    server = start_dashboard_server()
    host = config.get("server", {}).get("host", "127.0.0.1")
    # 如果端口被占用，start_dashboard_server 会自动选择下一个可用端口
    port = config.get("server", {}).get("_actual_port") or config.get("server", {}).get("port", 18980)
    logger.info("汇总面板: http://%s:%d/dashboard.html", host, port)

    # ── 定时清理 ──
    cleanup_cfg = config.get("cleanup", {})
    if cleanup_cfg.get("enabled", True):
        start_cleanup_scheduler(cleanup_cfg.get("interval_hours", 24))

    # ── 数据库备份 ──
    backup_cfg = config.get("backup", {})
    if backup_cfg.get("enabled", True):
        start_backup_scheduler(
            db_path=db_path,
            interval_hours=backup_cfg.get("interval_hours", 6),
            keep_days=backup_cfg.get("keep_days", 30),
            backup_dir=backup_cfg.get("backup_dir", "backups"),
        )

    # v4: 任务队列——恢复崩溃残留的 processing 任务
    resumed = store.resume_pending_tasks()
    if resumed > 0:
        logger.info("任务队列: 已恢复 %d 个残留任务", resumed)
    pending_tasks = store.get_pending_tasks()
    if pending_tasks:
        logger.info("任务队列: %d 个任务待处理", len(pending_tasks))

    try:
        # 检测是否需要首次引导
        from utils.config import get_auth_token
        token = get_auth_token()
        if token:
            url = f"http://{host}:{port}/dashboard.html"
        else:
            url = f"http://{host}:{port}/setup"
            logger.info("检测到首次启动，打开引导页")
        webbrowser.open(url)
    except Exception:
        pass  # 无 GUI 环境（服务器/SSH）下 webbrowser 可能失败

    # ── 扫描已有文件 + 文件监控 ──
    scan_existing(watch_dir)

    observer = Observer()
    observer.schedule(ResumeHandler(_process_resume_wrapper), watch_dir, recursive=False)
    observer.start()
    logger.info("文件监控已启动（稳定性检测模式），等待新简历… (Ctrl+C 退出)")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
        server.shutdown()
        store.close()
        logger.info("系统已停止")

    observer.join()


if __name__ == "__main__":
    main()
