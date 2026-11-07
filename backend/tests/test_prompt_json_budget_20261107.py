"""R184（2026-11-07）G-STRUCT：prompt JSON 硬切片 → 结构化裁剪。

旧实现两处 ``json.dumps(...)[:30000]`` 会切在 JSON 中间，模型收到无法
``json.loads`` 的半截结构（既解析不了，又可能被当作「输入就到这」而漏章）：

- ``routers/sections.py`` 目录调整 prompt 的 ``current_outline``；
- ``routers/global_facts.py`` 事实调整 prompt 的 ``current_facts``。

本用例锁住三条判据：
1. 交给模型的字符串**必须能被 json.loads 解析**；
2. 保留的每条都是**完整对象**（覆盖率处只落在条目边界，不切字段/不切子树）；
3. 超长**可观测**：省略条目数/字符数/原因写在结构里；单条超预算时
   条目内写 ``__truncated_chars__`` 注明省略字数。
"""

import json

from app.routers.global_facts import _facts_adjust_current_text, _facts_for_adjust_prompt
from app.routers.sections import _outline_adjust_current_text, _slim_tree_for_adjust
from app.services.prompt_governance import serialize_bounded_json_array


def _make_tree(count: int, desc_len: int = 90) -> list:
    """构造 count 个一级章节，每个带 2 个二级节点。"""
    roots = []
    for i in range(count):
        roots.append(
            {
                "id": f"root-{i}",
                "title": f"第{i + 1}章 施工组织",
                "description": "测" * desc_len,
                "children": [
                    {"id": f"leaf-{i}-0", "title": f"{i + 1}.1 编制依据", "description": "资" * 40},
                    {"id": f"leaf-{i}-1", "title": f"{i + 1}.2 施工部署", "description": "料" * 40},
                ],
            }
        )
    return roots


def _make_fact_rows(count: int, value_len: int = 90) -> list:
    return [
        {
            "id": f"fact-{i}",
            "title": f"事实{i}",
            "content": f"取值{i}" + "值" * value_len,
            "category": "material",
            "fact_key": f"key_{i}",
            "scheme_id": "s-1",
        }
        for i in range(count)
    ]


def _assert_truncation_visible(data: dict, *, kept_key: str, total_items: int):
    marker = data.get("truncation")
    assert isinstance(marker, dict), data
    assert marker["truncated"] is True
    assert marker["kept_items"] == len(data[kept_key])
    assert marker["omitted_items"] == total_items - len(data[kept_key])
    assert marker["omitted_items"] > 0
    assert marker["omitted_chars"] > 0
    assert "预算" in marker["reason"]


# ---------------------------------------------------------------------------
# 1. 未超预算：逐字节同旧实现（修复不得顺手改未截断路径）
# ---------------------------------------------------------------------------
def test_sections_under_budget_is_byte_identical():
    nodes = _slim_tree_for_adjust(_make_tree(2))
    text = _outline_adjust_current_text(nodes)
    assert text == json.dumps(nodes, ensure_ascii=False)


def test_facts_under_budget_is_byte_identical():
    items = _facts_for_adjust_prompt(_make_fact_rows(2))
    text = _facts_adjust_current_text(items)
    assert text == json.dumps(items, ensure_ascii=False)


def test_helper_zero_budget_means_unlimited():
    items = [{"a": 1}, {"a": 2}]
    assert serialize_bounded_json_array(items, 0, field="items") == json.dumps(items, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 2. 目录：超预算按一级章节边界裁剪，结构合法且每条完整
# ---------------------------------------------------------------------------
def test_sections_over_budget_is_valid_json_and_complete_items():
    nodes = _slim_tree_for_adjust(_make_tree(8))
    text = _outline_adjust_current_text(nodes, max_chars=700)
    data = json.loads(text)  # 判据 ①：必须可解析
    assert isinstance(data["outline"], list) and data["outline"], data
    # 判据 ②：每条都是完整对象（id/title 齐全，子树两颗且各自带标题）
    for node in data["outline"]:
        assert node["id"] and node["title"], node
        assert len(node["children"]) == 2, node
        for child in node["children"]:
            assert child["id"] and child["title"], child
    # 判据 ③：省略量显式可见
    _assert_truncation_visible(data, kept_key="outline", total_items=len(nodes))


def test_sections_single_huge_node_is_clipped_with_marker():
    nodes = _slim_tree_for_adjust(
        [
            {
                "id": "root-0",
                "title": "第1章 超长章节",
                "description": "巨" * 3000,
                "children": [{"id": "leaf-0", "title": "1.1 依据", "description": "详" * 2000}],
            }
        ]
    )
    text = _outline_adjust_current_text(nodes, max_chars=600)
    data = json.loads(text)  # 单条裁剪后仍必须是合法 JSON
    kept = data["outline"]
    assert len(kept) == 1, kept
    # 结构完整：子树没被切断
    assert kept[0]["id"] and kept[0]["title"]
    assert len(kept[0]["children"]) == 1 and kept[0]["children"][0]["title"]
    # 条目内显式标注省略字数
    assert kept[0]["__truncated_chars__"] > 0, kept[0]
    assert len(text) <= 600, len(text)


# ---------------------------------------------------------------------------
# 3. 事实：超预算按整条事实裁剪，结构合法且每条完整
# ---------------------------------------------------------------------------
def test_facts_over_budget_is_valid_json_and_complete_items():
    items = _facts_for_adjust_prompt(_make_fact_rows(8))
    text = _facts_adjust_current_text(items, max_chars=700)
    data = json.loads(text)
    assert isinstance(data["facts"], list) and data["facts"], data
    for fact in data["facts"]:
        assert fact["fact_id"] and fact["name"] and fact["value"], fact
        assert fact["category"], fact
    _assert_truncation_visible(data, kept_key="facts", total_items=len(items))


def test_facts_single_huge_value_is_clipped_with_marker():
    items = _facts_for_adjust_prompt(_make_fact_rows(1, value_len=4000))
    text = _facts_adjust_current_text(items, max_chars=600)
    data = json.loads(text)
    kept = data["facts"]
    assert len(kept) == 1, kept
    # 定位键永不裁剪，只有文本字段被削并带省略标注
    assert kept[0]["fact_id"] == "fact-0"
    assert kept[0]["category"] == "material"
    assert kept[0]["__truncated_chars__"] > 0, kept[0]
    assert len(text) <= 600, len(text)
