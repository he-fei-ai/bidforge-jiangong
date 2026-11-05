"""数值一致性主题表（**唯一事实源**）。

✅ B-16（R175，判定源统一）：``preflight_engine._NUM_TOPICS``（CON-01 预检）
与 ``consistency_scanner._NUM_TOPICS``（CON-07 程序预扫描）曾是两份逐字
复制、靠注释「必须同向」维持同步的正则表。任何一侧加主题（如 2026-10-17
补入用工年龄 / 缺陷责任期 / 模板面板厚度 / 钢管壁厚 / 拆模强度）都要人肉
改两处，漏改即出现「预检报 CON-01、扫描不报 CON-07」或反之的口径分叉。

本模块只提供**原始定义**，两侧按各自既有结构派生（编译 / 字符串），
语义与合并前逐字一致：

- ``NUM_TOPIC_DEFINITIONS``：``(主题, 正则源码, 取值捕获组, 对象捕获组)``。
  捕获组下标与 ``re.Match.group(i)`` 一致；``对象捕获组=None`` 表示该主题
  不按对象分组比较。
- ``compiled_num_topics()``：供 ``preflight_engine.numeric_consistency_findings``
  直接消费（编译后的 ``re.Pattern``）。
- ``pattern_num_topics()`` / ``topic_object_group()``：供
  ``consistency_scanner.program_prescan`` 消费（字符串 + 对象组映射）。
"""

from __future__ import annotations

import re

#: 数值一致性主题表（唯一事实源）。
#: 取值捕获组里的 ``0`` 表示「整体匹配」（``m.group(0)``），与既有实现一致。
NUM_TOPIC_DEFINITIONS: tuple[tuple[str, str, tuple[int, ...], int | None], ...] = (
    ("工期", r"(?:总工期|工期|建设工期|施工工期)[^0-9]{0,12}(\d+(?:\.\d+)?)\s*(日历天|天|个月|月)", (0, 1), None),
    ("基坑深度", r"(?:开挖|基坑)[^。\n]{0,8}深度[^0-9]{0,8}(\d+(?:\.\d+)?)\s*(?:m|米|M)", (0,), None),
    ("搭设高度", r"(?:支撑|脚手架|支架|模板)[^。\n]{0,8}高度[^0-9]{0,8}(\d+(?:\.\d+)?)\s*(?:m|米|M)", (0,), None),
    # 可选「构件/部位」前缀作为对象标识（捕获组 1），强度等级为组 2。
    (
        "混凝土强度",
        r"((?:柱|梁|板|墙|基础|承台|桩|楼板|底板|顶板)[^0-9，。；、\n]{0,6}?)?"
        r"\s*(?:混凝土强度等级|混凝土为|砼)\s*[:：]?\s*C(\d{2,3})",
        (2,),
        1,
    ),
    # 设备名（组 1）= 对象标识；数量 + 单位（组 2、3）= 取值
    ("设备数量", r"(塔吊|塔式起重机|施工电梯|升降机|挖掘机|起重机)[^0-9]{0,10}(\d+)\s*(台|套)", (2, 3), 1),
    # 2026-10-17（文档质量报告 六·2 数据矛盾）：报告实证互相矛盾的高价值主题。
    ("用工年龄", r"(一般|危大工程)[^0-9。\n]{0,16}?(\d{2})\s*周岁", (2,), 1),
    ("缺陷责任期", r"(整体工程|防水工程|防水|缺陷责任期)[^0-9。\n]{0,12}?(\d+)\s*个月", (2,), 1),
    (
        "模板面板厚度",
        r"(\d+(?:\.\d+)?)\s*(?:mm|毫米)\s*厚" r"|厚度[^0-9。\n]{0,4}?(\d+(?:\.\d+)?)\s*(?:mm|毫米)",
        (1, 2),
        None,
    ),
    (
        "钢管壁厚",
        r"(?:[φΦ]\s*48(?:\.3)?\s*[×xX*]\s*(\d+(?:\.\d+)?)"
        r"|壁厚(?!偏差)[^0-9。\n]{0,8}?(\d+(?:\.\d+)?)\s*(?:mm|毫米))",
        (1, 2),
        None,
    ),
    ("拆模强度", r"(悬臂构件|悬臂|楼板|板|梁|拱)[^0-9。\n]{0,6}?(?:强度)?\s*[≥>=]{1,2}\s*(\d+)\s*%", (2,), 1),
)


def compiled_num_topics() -> tuple[tuple[str, re.Pattern, tuple[int, ...], int | None], ...]:
    """供预检（CON-01）使用的编译形态（与旧 ``_NUM_TOPICS`` 逐字等价）。"""
    return tuple(
        (topic, re.compile(rx), val_groups, obj_group) for topic, rx, val_groups, obj_group in NUM_TOPIC_DEFINITIONS
    )


def pattern_num_topics() -> list[tuple[str, str]]:
    """供一致性扫描（CON-07）使用的「主题 + 正则源码」形态。"""
    return [(topic, rx) for topic, rx, _vg, _og in NUM_TOPIC_DEFINITIONS]


def topic_object_group() -> dict[str, int]:
    """需要按对象分组比较的主题 → 对象捕获组下标。"""
    return {topic: og for topic, _rx, _vg, og in NUM_TOPIC_DEFINITIONS if og is not None}


# ---------------------------------------------------------------------------
# ✅ B-19（R176 · 2026-11-04）：长文本送 AI 的统一口径
# ---------------------------------------------------------------------------
# 本模块是「/check（内容检查）」「/consistency-audit（一致性审计）」
# 「consistency_scanner（预检扫描）」三处**唯一无依赖**的共用底座，故把长文本
# 阈值登记在此，避免三处各维护一套（R175 审计 B-19）。
#
# 取值依据（真实库只读统计，687 个章节 / 6 个方案）：
#   单章长度 p95=2486、p99=2963、max=4341 字；单方案正文总量 max=135330 字。
#   - 单章 6000 覆盖 100% 存量章节（存量不再产生「续片」）；
#   - 单次调用 6000 与 /check 既有装箱上限一致，作为一次 AI 调用的内容预算；
#   - 单次审核总量 240000 ≈ 存量最大方案的 1.8 倍，超出即触发预算闸门。
#
# 统一策略（禁止任何一处静默丢弃）：
#   - 单章 > LONG_TEXT_SECTION_CAP：切片并显式标注「（续 k/n）」或截断标记；
#   - 一次调用 > LONG_TEXT_CALL_CAP：按章装箱成多批（批次数由 batch_count 披露）；
#   - 一次审核 > LONG_TEXT_TOTAL_CAP：走预算闸门（/check 返回 partial + 未审清单，
#     一致性审计拒绝并提示，避免把不完整结论落库供总检消费）。
LONG_TEXT_SECTION_CAP = 6000
LONG_TEXT_CALL_CAP = 6000
LONG_TEXT_TOTAL_CAP = 240000
