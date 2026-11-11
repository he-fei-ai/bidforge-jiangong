# -*- coding: utf-8 -*-
"""R214（2026-11-10）· 事实/正文/审核三模块遗留项收口 —— 后端加固护栏。

锁定行为契约（意图保留、不锁实现；源码字符串断言仅用于「接线必须在位」）：
  F1（R210 D-2）  facts_patches 补丁改值后**强制重派生** FactItem 四维
                 （chapter / fact_attr / source_kind / is_shared）—— value 变了
                 但维度停在提取期旧值 ⇒ 九大章节视图 / 按章精选 / 属性统计错配。
                 与 apply_fact_dimensions「已有值不覆盖」刻意区分：中间对象无
                 人工归类，保留旧值只会错配。
  F2（R212 D-5）  export.py 导出预检 409 文案本地化 —— 回退链不再把**裸英文
                 type 键**拼进 detail（display_title → title → 子类中文标签 →
                 type 四级），display_title 空标题时不再出现前导点号。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import app.routers.export as E
from app.services import facts_patches as fpt
from app.services.facts_classification import classify_fact_dimensions

# 新增后端测试文件按 R208 先例使用 LF 行尾；生产源文件保持 CRLF。


# ---------------------------------------------------------------------------
# F1（R210 D-2）：补丁改值后强制重派生四维
# ---------------------------------------------------------------------------


def test_replace_patch_rederives_fact_dimensions():
    """replace 补丁改 value 后，四维必须与**新值**一致（强制覆盖旧维度）。

    刻意用 classify_fact_dimensions 的**自身输出**作期望，避免测试与实现耦合
    具体映射表；同时用「肯定不同」的哨兵旧值预置，防止断言恒真。
    """
    name = "模板支撑体系"
    new_value = "承插型盘扣式钢管支架，立杆间距不大于1.2m"
    expected = classify_fact_dimensions(name, new_value, "", "", "", "", "")

    def pick(key: str) -> str:
        return "zzz" if expected[key] != "zzz" else "yyy"

    target = SimpleNamespace(
        name=name,
        value="旧值（提取期）",
        category="",
        fact_type="",
        key="",
        source="",
        source_ref="",
        chapter=pick("chapter"),
        fact_attr=pick("fact_attr"),
        source_kind=pick("source_kind"),
        is_shared=not bool(expected["is_shared"]),
    )
    patch = fpt.FactPatch(title=name, content=new_value, mode="replace", create=False)
    out = fpt.apply_patches_to_fact_items([target], [patch])
    got = out[0]
    assert got.value == new_value, "replace 补丁必须把 value 更新为新内容"
    assert got.chapter == expected["chapter"], f"chapter 未按新值重派生：{got.chapter} != {expected['chapter']}"
    assert got.fact_attr == expected["fact_attr"], f"fact_attr 未按新值重派生：{got.fact_attr}"
    assert got.source_kind == expected["source_kind"], f"source_kind 未按新值重派生：{got.source_kind}"
    assert bool(got.is_shared) == bool(expected["is_shared"]), f"is_shared 未按新值重派生：{got.is_shared}"


def test_append_patch_also_rederives_dimensions():
    """append 补丁改 value 同样触发重派生（与 replace 同出口，防止只修一半）。

    期望值从**落定后的 value** 派生（append 产物是旧+新合并串，格式由
    apply_patch_mode 决定，非测试可预测）—— 不变式是「四维恒等于
    classify_fact_dimensions(最终 value)」，与 replace 测试的独立预测互补。
    """
    name = "钢筋连接"
    new_value = "（增加）直螺纹套筒连接，接头错开 35d"
    target = SimpleNamespace(
        name=name,
        value="绑扎搭接",
        category="",
        fact_type="",
        key="",
        source="",
        source_ref="",
        chapter="zzz",
        fact_attr="zzz",
        source_kind="zzz",
        is_shared=True,
    )
    patch = fpt.FactPatch(title=name, content=new_value, mode="append", create=False)
    out = fpt.apply_patches_to_fact_items([target], [patch])
    got = out[0]
    assert new_value.split("，", 1)[0] in str(got.value), "append 补丁必须把新内容并入 value"
    # 预置维度是「肯定不同」的哨兵（zzz/True）——若重派生未发生，下列断言必红
    expected = classify_fact_dimensions(name, str(got.value), "", "", "", "", "")
    assert got.chapter == expected["chapter"], f"chapter 未按新值重派生：{got.chapter} != {expected['chapter']}"
    assert got.fact_attr == expected["fact_attr"], f"fact_attr 未按新值重派生：{got.fact_attr}"
    assert got.source_kind == expected["source_kind"], f"source_kind 未按新值重派生：{got.source_kind}"


# ---------------------------------------------------------------------------
# F2（R212 D-5）：导出预检 409 文案本地化
# ---------------------------------------------------------------------------


def test_export_blocked_detail_localized_when_title_empty():
    """rule 未登记（title 空）且无 display_title 时，409 detail 用中文子类标签。

    旧实现 ``or b['type']`` 把**裸英文键**（review_pending）拼进用户可见文案；
    现按 display_title → title → 子类中文标签 → type 四级回退，任何情况下
    不出现裸英文键（子类标签存在时）。
    """
    blockers = [
        {"type": "review_pending", "rule_id": "DLV-09", "severity": "high", "count": 6, "title": ""}
    ]
    resp = E._export_blocked_response(blockers)
    body = json.loads(resp.body)
    detail = body["detail"]
    assert "章节待审核" in detail, f"detail 未本地化：{detail}"
    assert "review_pending（" not in detail, f"裸英文键泄漏进 detail：{detail}"


def test_high_issue_blockers_display_title_no_leading_dot(monkeypatch):
    """同 rule_id 多 subtype 且 rule 未登记 → display_title 取子类标签、无前导点。

    旧实现 ``f\"{title}·{sub}\"`` 在 title 空时产出「·章节待审核」（前导点）；
    现空 title 直接取 sub，且 409 detail 不再出现前导点 / 裸英文键。
    """
    monkeypatch.setattr("app.services.audit_rules.get_rule", lambda *a, **k: None)
    issues = [
        {"type": "review_pending", "count": 6},
        {"type": "review_rejected", "count": 4},
    ]
    blockers = E._export_high_issue_blockers(issues)
    by_type = {b["type"]: b for b in blockers}
    assert by_type["review_pending"]["title"] == "", "rule 未登记时 title 应为空（测试前提）"
    assert by_type["review_pending"]["display_title"] == "章节待审核"
    assert not by_type["review_pending"]["display_title"].startswith("·")
    resp = E._export_blocked_response(blockers)
    body = json.loads(resp.body)
    assert "章节待审核" in body["detail"]
    assert "·章节待审核" not in body["detail"], "前导点号泄漏进 409 detail"
    assert "review_pending（" not in body["detail"], "裸英文键泄漏进 409 detail"
