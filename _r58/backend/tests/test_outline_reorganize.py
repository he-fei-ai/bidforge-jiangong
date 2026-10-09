"""目录整理为标准结构服务测试"""
import json

import pytest
from app.services.outline_reorganize import (
    _group_of,
    _strip_number,
    reorganize_to_standard,
)


def test_strip_number():
    assert _strip_number("一、工程简介") == "工程简介"
    assert _strip_number("1.1 项目概况") == "项目概况"
    assert _strip_number("（一）编制说明") == "编制说明"
    assert _strip_number("第3章 施工工艺") == "施工工艺"
    assert _strip_number("五、安全保障") == "安全保障"


def test_strip_number_not_destructive():
    """✅ BUG 修复（2026-10-05 · D1）：剥离编号时不得误伤正文标题。

    旧实现自带一份 `_NUM_RE` 正则（与 numbering.py 分叉），中文编号与点分
    编号两个分支过度激进，把正常标题切碎，且剥离结果经 _strip_tree 写入
    description / 补充章节标题后随 outline_json 落库（用户可见的数据损坏）。
    下列用例锁定修复后的行为。
    """
    # 中文数字开头但并非编号：必须原样保留
    assert _strip_number("十二层平面布置") == "十二层平面布置"
    assert _strip_number("三层梁板施工") == "三层梁板施工"
    assert _strip_number("十个人") == "十个人"
    # 点分编号必须一次吃满整条路径，不得回退成 "3钢筋工程"
    assert _strip_number("1.2.3钢筋工程") == "钢筋工程"
    # 单段数字 + 单位（非编号）：保留
    assert _strip_number("2层作业平台") == "2层作业平台"
    # 年份前缀：保留
    assert _strip_number("2024年度计划") == "2024年度计划"
    # 标题本身即纯编号：原样返回
    assert _strip_number("1.2.3") == "1.2.3"


def test_strip_number_matches_canonical():
    """✅ D1 收口：本模块剥离必须与 numbering 的唯一实现逐字一致，
    防止后续再次分叉出第三份正则副本。"""
    from app.services.numbering import strip_outline_numbering

    cases = [
        "十二层平面布置", "三层梁板施工", "十个人", "2层作业平台",
        "一、编制依据", "第五章 施工计划", "1.2.3钢筋工程", "2024年度计划",
        "（一）编制说明", "五、安全保障", "1.1 项目概况", "第3章 施工工艺",
        "1.2.3", "", "   ", "钻孔灌注桩",
    ]
    for c in cases:
        assert _strip_number(c) == strip_outline_numbering(c), f"剥离口径分叉: {c!r}"


def test_max_outline_depth_is_canonical():
    """✅ D3 收口：本模块的深度上限必须引用唯一事实源 outline_utils.MAX_OUTLINE_DEPTH，
    不得再自定义第二份常量（否则调整目录上限时本模块仍按旧值裁剪、静默截断）。"""
    import app.services.outline_reorganize as mod
    from app.services.outline_utils import MAX_OUTLINE_DEPTH as CANONICAL

    assert mod.MAX_OUTLINE_DEPTH == CANONICAL
    assert mod.MAX_OUTLINE_DEPTH is CANONICAL


def test_group_of():
    assert _group_of("工程概况") == "工程概况"
    assert _group_of("一、工程简介") == "工程概况"
    assert _group_of("五、安全保障") == "施工安全保证措施"
    assert _group_of("施工总体部署") == "施工计划"
    assert _group_of("BIM应用管理") is None


def test_preserves_standard_framework():
    """导入野路子目录，整理后仍保留标准 9 章框架 + 编写要点"""
    raw = [
        {"title": "一、工程简介", "level": 2, "children": [
            {"title": "1.1 项目概况", "level": 3, "children": []},
        ]},
        {"title": "二、编制说明", "level": 2, "children": []},
        {"title": "三、施工总体部署", "level": 2, "children": []},
        {"title": "四、主要施工方法", "level": 2, "children": [
            {"title": "4.1 钻孔灌注桩", "level": 3, "children": []},
        ]},
        {"title": "五、安全保障", "level": 2, "children": []},
        {"title": "六、项目信息化管理（BIM）", "level": 2, "children": []},
    ]
    res = reorganize_to_standard(raw, "深基坑工程专项施工方案")
    out = res["outline"]
    tops = [n["title"] for n in out]
    # 标准顶层 9 章必须全在（监测方案是子章，不入顶层）
    for must in ["工程概况", "编制依据", "施工计划", "施工工艺技术",
                 "施工安全保证措施", "验收要求", "应急处置措施", "计算书及相关图纸"]:
        assert any(must in t for t in tops), f"标准章节缺失: {must}"
    # 监测方案子章保留在施工安全保证措施下
    safety = next(n for n in out if n["title"].startswith("施工安全保证措施"))
    assert any("监测" in c["title"] for c in safety["children"])
    # 用户真实子章节被保留
    flat = __import__("json").dumps(out, ensure_ascii=False)
    assert "钻孔灌注桩" in flat
    # 未归位内容进入补充章节
    assert any("补充章节" in t for t in tops)
    # 每个标准节点都带描述（与软件生成标准一致）
    assert all(n.get("description") for n in out if n["title"] not in ("补充章节（导入补充）",))


def test_preserve_unmatched_false_drops_extras():
    raw = [{"title": "野路子章节", "level": 1, "children": []}]
    res = reorganize_to_standard(raw, "塔吊安装专项施工方案", preserve_unmatched=False)
    tops = [n["title"] for n in res["outline"]]
    assert not any("补充章节" in t for t in tops)


def test_unmatched_children_titles_merged_not_dropped():
    """✅ BUG 修复（2026-09-16）：标准骨架没有对应分支时，真实子章节标题
    必须并入描述（"（含：…）"）而不是被静默丢弃。

    旧实现在 `_reorg_children` 里只保留"能对上标准骨架"的真实子章节，
    标准模板只到三级、用户上传的是四级时，最细一层标题整体消失。
    """
    raw = [{"title": "第五章 安全保障", "children": [
        {"title": "5.1 安全技术交底流程", "children": [
            {"title": "交底记录归档要求", "children": []}]},
        {"title": "5.2 危险源辨识清单", "children": []},
    ]}]
    res = reorganize_to_standard(raw, "深基坑工程专项施工方案")
    flat = json.dumps(res["outline"], ensure_ascii=False)
    assert "安全技术交底流程" in flat
    assert "危险源辨识清单" in flat
    assert "交底记录归档要求" in flat, "更深层级的标题应并入父节点描述"


def test_reorganize_output_never_exceeds_three_levels():
    """整理结果本身仍受三级约束（深层内容以并入描述的方式保留）。"""
    raw = [{"title": "第一章 工程概况", "children": [
        {"title": "1.1 工程基本情况", "children": [
            {"title": "1.1.1 建设规模", "children": [
                {"title": "建筑面积", "children": []}]}]}]}]
    res = reorganize_to_standard(raw, "深基坑工程专项施工方案")

    def depth(nodes, d=1):
        if not nodes:
            return d - 1
        return max(depth(n.get("children") or [], d + 1) for n in nodes)

    assert depth(res["outline"]) <= 3
