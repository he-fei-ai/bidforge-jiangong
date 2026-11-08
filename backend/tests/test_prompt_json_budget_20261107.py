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


def test_helper_exactly_at_budget_is_byte_identical():
    """基线锁：预算恰好等于整串长度（含中文 / 嵌套 / None）仍逐字节原样返回。

    2026-11-08 逐条裁剪改造的硬要求是「未超预算路径一字节都不动」，本用例直接
    钉住该边界（``len(whole)`` 等于预算、以及预算更大两种情形），不经过任何
    router 包装，避免包装层变化掩盖 helper 回归。
    """
    items = [{"a": "中文取值"}, {"b": [1, 2, {"c": None}]}, {"d": True}]
    whole = json.dumps(items, ensure_ascii=False)
    assert serialize_bounded_json_array(items, len(whole), field="items") == whole
    assert serialize_bounded_json_array(items, len(whole) + 1, field="items") == whole
    assert serialize_bounded_json_array(items, 600, field="items") == whole


# ---------------------------------------------------------------------------
# 1b. 逐条裁剪：每个「自身超预算」条目都必须被单独裁剪（不止 items[0]）
# ---------------------------------------------------------------------------
def test_helper_clips_second_and_third_oversized_items():
    """首条不超预算时，第 2/3 条自身超预算仍要逐条裁剪进输出。

    旧实现只在 ``items[0]`` 自身超预算时进入裁剪分支；首条正常时直接走整条
    贪心，后面两条超长事实被整体省略 —— 模型完全看不到它们。新实现必须把
    两条都裁进输出，并逐条记 ``__truncated_chars__`` 与 ``clipped_details``。
    """
    items = _facts_for_adjust_prompt(_make_fact_rows(3, value_len=20))
    items[1]["value"] = "甲" * 3000
    items[2]["value"] = "乙" * 3000
    text = _facts_adjust_current_text(items, max_chars=1400)
    data = json.loads(text)
    kept = data["facts"]
    assert len(kept) == 3, kept
    assert kept[0].get("__truncated_chars__", 0) == 0, kept[0]
    assert kept[1]["__truncated_chars__"] > 0, kept[1]
    assert kept[2]["__truncated_chars__"] > 0, kept[2]
    marker = data["truncation"]
    assert marker["clipped_items"] == 2, marker
    assert marker["clipped_chars"] > 0, marker
    assert [d["index"] for d in marker["clipped_details"]] == [1, 2], marker
    assert all(d["omitted_chars"] > 0 for d in marker["clipped_details"]), marker
    assert len(text) <= 1400, len(text)


def test_helper_clips_single_middle_oversized_item_and_keeps_tail():
    """只有中间一条超预算时，前后两条完整保留、中间那条被裁并记账。"""
    items = _facts_for_adjust_prompt(_make_fact_rows(3, value_len=20))
    items[1]["value"] = "甲" * 2500
    text = _facts_adjust_current_text(items, max_chars=1200)
    data = json.loads(text)
    kept = data["facts"]
    assert len(kept) == 3, kept
    assert kept[0].get("__truncated_chars__", 0) == 0, kept[0]
    assert kept[1]["__truncated_chars__"] > 0, kept[1]
    assert kept[2].get("__truncated_chars__", 0) == 0, kept[2]
    marker = data["truncation"]
    assert marker["clipped_items"] == 1, marker
    assert marker["clipped_details"][0]["index"] == 1, marker
    assert len(text) <= 1200, len(text)


def test_helper_unshrinkable_item_is_omitted_not_oversized_payload():
    """裁剪收敛定义：裁完仍超预算 → 从输出尾部整条回退，直到可容纳。

    这里用「拒绝收缩」的回调模拟结构键本身已超预算的情形：条目必须被整条
    省略（计入 ``omitted_items``），绝不能把超预算的整条原样塞给模型。
    """

    def _no_shrink(item, max_chars):
        return item, 0

    items = [{"id": "a", "v": "小"}, {"id": "b", "v": "巨" * 3000}]
    text = serialize_bounded_json_array(items, 600, field="items", clip_item=_no_shrink)
    data = json.loads(text)
    assert data["items"] == [items[0]], data
    assert data["truncation"]["omitted_items"] == 1, data["truncation"]
    assert len(text) <= 600, len(text)


def test_helper_budget_below_wrapper_returns_legal_empty_shell():
    """收敛定义的终点：预算连包装都装不下时返回**合法空壳**（仍可 json.loads）。"""

    def _no_shrink(item, max_chars):
        return item, 0

    items = [{"id": "a", "v": "巨" * 3000}]
    text = serialize_bounded_json_array(items, 60, field="items", clip_item=_no_shrink)
    data = json.loads(text)  # 唯一硬判据：任何预算下都必须是合法 JSON
    assert data["items"] == [], data
    assert data["truncation"]["omitted_items"] == 1, data["truncation"]
    assert data["truncation"]["omitted_chars"] == len(json.dumps(items[0], ensure_ascii=False))


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
