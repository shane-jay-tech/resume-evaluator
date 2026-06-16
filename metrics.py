"""Prometheus 格式指标 —— 手动文本渲染，不引入外部依赖。"""

import time
import threading
from collections import defaultdict


class Counter:
    def __init__(self, name: str, help_text: str, labels: list[str] | None = None):
        self.name = name
        self.help = help_text
        self.label_names = labels or []
        self._values: dict[tuple, int] = defaultdict(int)
        self._lock = threading.Lock()

    def inc(self, value: int = 1, **labels):
        key = tuple(labels.get(k, "") for k in self.label_names)
        with self._lock:
            self._values[key] += value

    def render(self) -> str:
        lines = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} counter"]
        with self._lock:
            for key, val in self._values.items():
                label_parts = ",".join(f'{k}="{v}"' for k, v in zip(self.label_names, key))
                label_str = f"{{{label_parts}}}" if label_parts else ""
                lines.append(f"{self.name}{label_str} {val}")
        return "\n".join(lines) + "\n"


class Gauge:
    def __init__(self, name: str, help_text: str, labels: list[str] | None = None):
        self.name = name
        self.help = help_text
        self.label_names = labels or []
        self._values: dict[tuple, float] = defaultdict(float)
        self._lock = threading.Lock()

    def set(self, value: float, **labels):
        key = tuple(labels.get(k, "") for k in self.label_names)
        with self._lock:
            self._values[key] = value

    def render(self) -> str:
        lines = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} gauge"]
        with self._lock:
            for key, val in self._values.items():
                label_parts = ",".join(f'{k}="{v}"' for k, v in zip(self.label_names, key))
                label_str = f"{{{label_parts}}}" if label_parts else ""
                lines.append(f"{self.name}{label_str} {val}")
        return "\n".join(lines) + "\n"


class Histogram:
    """手动分桶 Histogram。"""

    def __init__(self, name: str, help_text: str, buckets: list[float], labels: list[str] | None = None):
        self.name = name
        self.help = help_text
        self.buckets = sorted(buckets)
        self.label_names = labels or []
        self._buckets: dict[tuple, dict[float, int]] = defaultdict(lambda: defaultdict(int))
        self._sums: dict[tuple, float] = defaultdict(float)
        self._counts: dict[tuple, int] = defaultdict(int)
        self._lock = threading.Lock()

    def observe(self, value: float, **labels):
        key = tuple(labels.get(k, "") for k in self.label_names)
        with self._lock:
            self._sums[key] += value
            self._counts[key] += 1
            for b in self.buckets:
                if value <= b:
                    self._buckets[key][b] += 1

    def render(self) -> str:
        lines = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} histogram"]
        with self._lock:
            all_keys = set(list(self._buckets.keys()) + list(self._sums.keys()))
            for key in all_keys:
                base_labels = ",".join(f'{k}="{v}"' for k, v in zip(self.label_names, key))
                sum_val = self._sums.get(key, 0)
                count_val = self._counts.get(key, 0)
                bucket_map = self._buckets.get(key, {})
                for b in self.buckets:
                    le_label = f'le="{b}"'
                    all_labels = f"{base_labels},{le_label}" if base_labels else le_label
                    lines.append(f'{self.name}_bucket{{{all_labels}}} {bucket_map.get(b, 0)}')
                le_inf = f'{base_labels},le="+Inf"' if base_labels else 'le="+Inf"'
                lines.append(f'{self.name}_bucket{{{le_inf}}} {count_val}')
                if base_labels:
                    lines.append(f"{self.name}_sum{{{base_labels}}} {sum_val}")
                    lines.append(f"{self.name}_count{{{base_labels}}} {count_val}")
                else:
                    lines.append(f"{self.name}_sum {sum_val}")
                    lines.append(f"{self.name}_count {count_val}")
        return "\n".join(lines) + "\n"


class MetricsRegistry:
    """指标注册中心（模块级单例）。"""

    def __init__(self):
        self.eval_requests = Counter(
            "eval_requests_total",
            "Total number of resume evaluations",
            ["status"],
        )
        self.status_changes = Counter(
            "pipeline_status_changes_total",
            "Total number of pipeline status changes",
            ["from_status", "to_status"],
        )
        self.queue_size = Gauge(
            "queue_size",
            "Number of items in queues",
            ["type"],
        )
        self.eval_duration = Histogram(
            "eval_duration_seconds",
            "Evaluation duration in seconds",
            [5, 10, 30, 60, 120, 300],
        )
        self._start_time = time.time()

    def render_all(self) -> str:
        """渲染所有指标为 Prometheus 文本格式。"""
        lines = [
            "# UPGRADE-P3+: Prometheus metrics endpoint",
            f"# process_uptime_seconds {time.time() - self._start_time:.0f}",
        ]
        for attr in ["eval_requests", "status_changes", "queue_size", "eval_duration"]:
            metric = getattr(self, attr)
            lines.append(metric.render())
        return "\n".join(lines)


# 全局单例
metrics = MetricsRegistry()
