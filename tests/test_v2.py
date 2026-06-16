"""v2 系统测试 — 覆盖核心功能。"""

import json
import os
import sys
import tempfile
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.llm_client import parse_json, LLMClient
from evaluator import (
    _build_eval_system,
    _get_dimensions,
    _normalize_score,
    _build_routing_rules,
    match_position_by_filename,
)
from database import DataStore


class TestJSONParsing:
    """JSON 解析测试。"""

    def test_parse_plain_json(self):
        result = parse_json('{"a": 1, "b": 2}')
        assert result == {"a": 1, "b": 2}

    def test_parse_markdown_wrapped(self):
        raw = '```json\n{"x": "y"}\n```'
        result = parse_json(raw)
        assert result == {"x": "y"}

    def test_parse_with_extra_text(self):
        raw = '一些解释文字 {"key": "value"} 更多文字'
        result = parse_json(raw)
        assert result == {"key": "value"}

    def test_parse_no_json(self):
        with pytest.raises((ValueError, json.JSONDecodeError)):
            parse_json("这里没有 JSON")


class TestNormalizeScore:
    """评分归一化测试。"""

    def test_normal_score(self):
        assert _normalize_score(8, 10) == 8

    def test_too_high(self):
        assert _normalize_score(15, 10) == 10

    def test_too_low(self):
        assert _normalize_score(-5, 10) == 1

    def test_zero(self):
        assert _normalize_score(0, 10) == 1

    def test_invalid(self):
        assert _normalize_score("abc", 10) == 5  # 默认中位数


class TestEvalSystem:
    """评估系统提示词构建测试。"""

    def test_build_with_dimensions(self):
        dims = [
            {"name": "技能", "key": "skill", "weight": 0.4},
            {"name": "经验", "key": "exp", "weight": 0.3},
            {"name": "学历", "key": "edu", "weight": 0.3},
        ]
        prompt = _build_eval_system(dims)
        assert "技能" in prompt
        assert "经验" in prompt
        assert "学历" in prompt
        assert '"skill"' in prompt
        assert '"exp"' in prompt
        assert '"edu"' in prompt


class TestGetDimensions:
    """维度提取测试。"""

    def test_with_dimensions(self):
        pos = {
            "dimensions": [
                {"name": "A", "key": "a", "weight": 0.5},
                {"name": "B", "key": "b", "weight": 0.5},
            ]
        }
        dims = _get_dimensions(pos)
        assert len(dims) == 2

    def test_default_dimensions(self):
        pos = {}
        dims = _get_dimensions(pos)
        assert len(dims) == 4  # 默认 4 维度


class TestFilenameMatching:
    """文件名岗位匹配测试。"""

    def test_exact_match(self):
        positions = [
            {"name": "游戏策划", "aliases": []},
            {"name": "程序开发", "aliases": []},
        ]
        result = match_position_by_filename("游戏策划_张三.pdf", positions)
        assert result is not None
        assert result["position_name"] == "游戏策划"
        assert result["matched_by"] == "filename"

    def test_alias_match(self):
        positions = [
            {"name": "游戏关卡策划", "aliases": ["关卡策划", "level design"]},
            {"name": "程序开发", "aliases": []},
        ]
        result = match_position_by_filename("level design_李四.docx", positions)
        assert result is not None
        assert result["position_name"] == "游戏关卡策划"

    def test_no_match(self):
        positions = [{"name": "程序开发", "aliases": []}]
        result = match_position_by_filename("神秘文件.pdf", positions)
        assert result is None

    def test_multi_match(self):
        positions = [
            {"name": "游戏关卡策划", "aliases": ["关卡"]},
            {"name": "发行创意策划", "aliases": ["创意"]},
        ]
        result = match_position_by_filename("关卡创意_王五.pdf", positions)
        assert result is not None
        assert result.get("multi_match") is True

    def test_disabled_position(self):
        positions = [
            {"name": "游戏策划", "aliases": [], "enabled": False},
            {"name": "程序开发", "aliases": []},
        ]
        result = match_position_by_filename("游戏策划_简历.pdf", positions)
        # 仅有的匹配岗位被禁用 → 无匹配
        assert result is None


class TestRoutingRules:
    """配置驱动路由规则测试。"""

    def test_generate_rules(self):
        positions = [
            {"name": "游戏关卡策划"},
            {"name": "发行创意策划", "routing_rules": [
                {"source_position": "游戏关卡策划", "max_years": 4, "required_background": "休闲"}
            ]},
        ]
        candidates = [
            {"index": 0, "name": "游戏关卡策划"},
            {"index": 1, "name": "发行创意策划"},
        ]
        rules = _build_routing_rules(candidates, positions, {})
        assert "4年以下" in rules
        assert "休闲" in rules

    def test_no_rules_when_source_not_in_candidates(self):
        positions = [
            {"name": "程序开发"},
            {"name": "发行创意策划", "routing_rules": [
                {"source_position": "游戏关卡策划", "max_years": 4}
            ]},
        ]
        candidates = [
            {"index": 0, "name": "程序开发"},
            {"index": 1, "name": "发行创意策划"},
        ]
        rules = _build_routing_rules(candidates, positions, {})
        assert rules == ""  # 游戏关卡策划不在候选列表中，规则不激活


class TestDataStoreV2:
    """DataStore v2 测试。"""

    def setup_method(self):
        self.tmpfile = os.path.join(tempfile.gettempdir(), "test_v2_recruitment.db")
        self.store = DataStore(self.tmpfile)

    def teardown_method(self):
        self.store.close()
        if os.path.exists(self.tmpfile):
            os.remove(self.tmpfile)

    def test_save_and_get_dimension_scores(self):
        rid = self.store.add_result({
            "candidate_name": "测试", "matched_position": "程序开发",
            "match_score": 85, "verdict": "推荐",
            "dimensions": {"skill": {"score": 8}, "exp": {"score": 7}},
            "timestamp": "2026-06-15 10:00:00",
        })
        self.store.save_dimension_scores(rid, {
            "skill": {"name": "技能", "weight": 0.5, "score": 8, "comment": "很好"},
            "exp": {"name": "经验", "weight": 0.5, "score": 7, "comment": "不错"},
        })

        scores = self.store.get_dimension_scores(rid)
        assert len(scores) == 2
        assert scores[0]["dimension_key"] == "skill"
        assert scores[0]["score"] == 8

    def test_dimension_stats(self):
        # Add 2 results
        rid1 = self.store.add_result({
            "candidate_name": "A", "matched_position": "程序开发",
            "match_score": 90, "verdict": "强烈推荐",
            "timestamp": "2026-06-15 10:00:00",
        })
        rid2 = self.store.add_result({
            "candidate_name": "B", "matched_position": "程序开发",
            "match_score": 70, "verdict": "待定",
            "timestamp": "2026-06-15 11:00:00",
        })
        self.store.save_dimension_scores(rid1, {"skill": {"score": 9}})
        self.store.save_dimension_scores(rid2, {"skill": {"score": 5}})

        stats = self.store.get_dimension_stats("skill", "程序开发")
        assert stats["avg_score"] == 7.0
        assert stats["min_score"] == 5
        assert stats["max_score"] == 9
        assert stats["count"] == 2

    def test_eval_metadata_field(self):
        rid = self.store.add_result({
            "candidate_name": "测试", "matched_position": "程序开发",
            "match_score": 80, "verdict": "待定",
            "eval_metadata": {"model_name": "gpt-5.5", "prompt_version": "v2"},
            "timestamp": "2026-06-15 10:00:00",
        })
        result = self.store.get_result_by_id(rid)
        assert result["eval_metadata"]["model_name"] == "gpt-5.5"
        assert result["eval_metadata"]["prompt_version"] == "v2"


class TestServices:
    """services 模块基础测试。"""

    def test_import(self):
        from services import process_resume, add_to_pending, load_references, generate_pdf
        assert callable(process_resume)
        assert callable(add_to_pending)
        assert callable(load_references)
        assert callable(generate_pdf)


class TestRouteTable:
    """路由表完整性测试。"""

    def test_get_routes_exist(self):
        from app_routes import GET_ROUTES, POST_ROUTES
        # 核心路由必须存在
        assert "/api/health" in GET_ROUTES
        assert "/api/results" in GET_ROUTES
        assert "/api/pending" in GET_ROUTES
        assert "/api/events" in GET_ROUTES
        assert "/api/save" in POST_ROUTES
        assert "/api/assign" in POST_ROUTES
        assert "/api/transition" in POST_ROUTES
        # v2 新增
        assert "/api/stats/dimensions" in GET_ROUTES

    def test_route_count(self):
        from app_routes import GET_ROUTES, POST_ROUTES
        assert len(GET_ROUTES) == 33  # v6: 新增 /setup, /api/setup/defaults
        assert len(POST_ROUTES) == 28  # v6: 新增 /api/setup, /api/upload


class TestSSE:
    """SSE 模块测试。"""

    def test_event_constants(self):
        from sse import EVENT_EVAL_COMPLETE, EVENT_STATUS_CHANGE, EVENT_PENDING_UPDATE, EVENT_QUEUE_UPDATE
        assert EVENT_EVAL_COMPLETE == "eval_complete"
        assert EVENT_STATUS_CHANGE == "status_change"
        assert EVENT_PENDING_UPDATE == "pending_update"
        assert EVENT_QUEUE_UPDATE == "queue_update"
