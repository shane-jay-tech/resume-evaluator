"""SSE (Server-Sent Events) 实时事件推送 —— 替代前端轮询。"""

import json
import logging
import threading
import time

logger = logging.getLogger(__name__)

# Event 类型常量
EVENT_EVAL_COMPLETE = "eval_complete"
EVENT_STATUS_CHANGE = "status_change"
EVENT_PENDING_UPDATE = "pending_update"
EVENT_QUEUE_UPDATE = "queue_update"


class SSEClient:
    """单个 SSE 客户端连接，线程安全。"""

    def __init__(self, wfile):
        self._wfile = wfile
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._event_counter = 0

    def send_event(self, event: str, data: dict | str):
        """发送一条 SSE 事件（含自增 ID 用于断线重连）。"""
        self._event_counter += 1
        payload = json.dumps(data, ensure_ascii=False) if isinstance(data, dict) else data
        msg = f"id: {self._event_counter}\nevent: {event}\ndata: {payload}\n\n"
        with self._lock:
            try:
                self._wfile.write(msg.encode("utf-8"))
                self._wfile.flush()
            except (BrokenPipeError, OSError):
                self._stop_event.set()

    def send_heartbeat(self):
        self.send_event("heartbeat", {"time": int(time.time())})

    @property
    def stopped(self) -> bool:
        return self._stop_event.is_set()

    def stop(self):
        self._stop_event.set()


class SSEManager:
    """管理所有 SSE 客户端连接。"""

    def __init__(self):
        self._clients: set[SSEClient] = set()
        self._lock = threading.Lock()

    def register(self, client: SSEClient):
        with self._lock:
            self._clients.add(client)
        logger.debug("SSE client connected, total=%d", len(self._clients))

    def unregister(self, client: SSEClient):
        with self._lock:
            self._clients.discard(client)
        logger.debug("SSE client disconnected, total=%d", len(self._clients))

    def broadcast(self, event: str, data: dict):
        """向所有连接的客户端广播事件。"""
        with self._lock:
            dead = []
            for client in self._clients:
                if client.stopped:
                    dead.append(client)
                else:
                    client.send_event(event, data)
            for client in dead:
                self._clients.discard(client)

        if event != "heartbeat":
            logger.debug("SSE broadcast: %s → %d clients", event, len(self._clients))

    @property
    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)
