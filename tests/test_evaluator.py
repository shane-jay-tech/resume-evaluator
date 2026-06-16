"""测试 evaluator.py —— 文件名匹配、JSON 解析、动态维度。"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from evaluator import match_position_by_filename, _get_dimensions
from utils.llm_client import parse_json


def test_match_position_by_filename():
    positions = [
        {"name": "国内游戏广告创意策划", "aliases": ["广告创意策划"]},
        {"name": "Playable试玩广告开发工程师", "aliases": ["Playable"]},
        {"name": "游戏AI广告设计师", "aliases": ["AI广告设计"]},
    ]

    # 精确匹配
    result = match_position_by_filename("张三-Playable试玩广告开发工程师.pdf", positions)
    assert result is not None
    assert result["position_name"] == "Playable试玩广告开发工程师"
    assert result["matched_by"] == "filename"

    # 别名匹配
    result = match_position_by_filename("李四-广告创意策划简历.docx", positions)
    assert result is not None
    assert result["position_name"] == "国内游戏广告创意策划"

    # 不匹配
    result = match_position_by_filename("王五-简历.pdf", positions)
    assert result is None


def testparse_json_clean():
    raw = '{"name": "test", "score": 85}'
    result = parse_json(raw)
    assert result["name"] == "test"
    assert result["score"] == 85


def testparse_json_with_markdown():
    raw = '```json\n{"name": "test"}\n```'
    result = parse_json(raw)
    assert result["name"] == "test"


def testparse_json_with_prefix():
    raw = '这是分析结果：\n{"name": "test", "score": 90}'
    result = parse_json(raw)
    assert result["name"] == "test"
    assert result["score"] == 90


def testparse_json_invalid():
    try:
        parse_json("没有 JSON 内容")
        assert False, "应抛出异常"
    except ValueError:
        pass


def test_get_dynamic_dimensions():
    pos_with_dims = {
        "name": "测试岗位",
        "dimensions": [
            {"name": "技术能力", "key": "tech_skill", "weight": 0.4},
            {"name": "沟通能力", "key": "comm_skill", "weight": 0.3},
            {"name": "综合评价", "key": "overall", "weight": 0.3},
        ]
    }
    dims = _get_dimensions(pos_with_dims)
    assert len(dims) == 3
    assert dims[0]["name"] == "技术能力"
    assert dims[0]["key"] == "tech_skill"
    assert dims[0]["weight"] == 0.4


def test_get_default_dimensions():
    pos_without_dims = {"name": "无维度岗位"}
    dims = _get_dimensions(pos_without_dims)
    assert len(dims) == 4  # default 4 dimensions
    assert dims[0]["key"] == "skill_match"


if __name__ == "__main__":
    test_match_position_by_filename()
    testparse_json_clean()
    testparse_json_with_markdown()
    testparse_json_with_prefix()
    testparse_json_invalid()
    test_get_dynamic_dimensions()
    test_get_default_dimensions()
    print("✅ 所有 evaluator 测试通过")
