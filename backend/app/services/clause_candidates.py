"""文种 → 候选标准号收敛（R179 Phase F 检索重做，2026-11-07）。

本模块只做一件事：把「方案文种」收敛成一个**标准号基号**集合，供
:mod:`app.services.clause_store` 在候选范围内做条文检索。

数据源
------
* ``docs/条款库_17类核心规范清单_R179_20261106.csv`` 是「类别 → 标准号基号」
  的唯一事实源。运行时**不读 docs**（避免生产依赖仓库文档路径），而是把清单
  编译成 :data:`CATEGORY_STANDARD_BASES`；测试逐行比对 CSV 防两处漂移。
* 文种侧信号：``scheme_classification.build_genre_tag`` 的 ``category_ids`` +
  ``GenreTag.template_key``（``outline_templates.match_template``）。

收敛规则
--------
1. 大类 id 先映射到清单类键（``hoisting`` → ``lifting``）；
2. 细粒度模板键先归一化：同键直用 / 家族前缀（``scaffold_*``）/ 明确语义别名；
3. 17 类清单之外的文种（顶管、水下、暗挖、四新等）**不猜标准号** ——
   返回空集合，由调用方落 ``no_standard_code``，绝不退回全库模糊匹配。
"""

from __future__ import annotations

from app.services.standards_registry import strip_standard_year

#: 17 类核心清单（顺序与 CSV 一致；仅基号，不带年号）。
#: 由测试 ``test_clause_candidates_match_csv`` 逐行比对，禁止手工分叉。
CATEGORY_STANDARD_BASES: dict[str, tuple[str, ...]] = {
    "foundation_pit": ("GB 55003", "JGJ 120", "JGJ 311", "GB 50497", "GB 50202"),
    "formwork": ("GB 55023", "JGJ 162", "GB 50666", "GB 50204", "JGJ 300"),
    "scaffold": ("GB 55023", "JGJ 130", "JGJ/T 231", "JGJ 202", "JGJ 80"),
    "lifting": ("JGJ 276", "JGJ 196", "JGJ 215", "GB 6067.1", "GB 5144"),
    "temp_electricity": ("JGJ/T 46", "GB 50194", "GB 55034"),
    "work_at_height": ("JGJ 80", "GB 6095", "GB 2811", "JGJ 59"),
    "confined_space": ("GBZ/T 205", "GB 30871", "GB 8958", "JGJ 59"),
    "fire_water": ("GB 50720", "GB 55036", "GB 50140", "GB 50242", "GB 55037"),
    "prefabricated": ("GB/T 51231", "JGJ 1", "JGJ 355", "GB 50204", "GB 50205", "JGJ 276"),
    "waterproof": ("GB 55030", "GB 50108", "GB 50208", "GB 50345", "GB 50207"),
    "decoration": ("GB 50210", "GB 50209", "GB 50222", "JGJ 144", "GB 50411", "GB 50325"),
    "mep": ("GB 55036", "GB 50242", "GB 50243", "GB 50303", "GB 50140"),
    "quality": ("GB 50300", "GB 55032", "GB 50204", "GB/T 50375"),
    "safety_management": ("JGJ 59", "GB 55034", "GB 50870", "GB 50720", "JGJ 146"),
    "emergency": ("GB/T 29639", "AQ/T 9007", "GB 50870", "JGJ 59"),
    "seasonal": ("JGJ/T 104", "GB 50666", "GB 50496", "JGJ 59", "JGJ/T 46"),
    "green": ("GB/T 50905", "GB/T 50640", "JGJ 146", "GB 12523", "GB 55034"),
}

#: 危大六类 id → 清单类键（``other`` 不映射，交给 ``template_key``）。
_CATEGORY_ID_TO_KEY: dict[str, str] = {
    "foundation_pit": "foundation_pit",
    "formwork": "formwork",
    "hoisting": "lifting",
    "scaffold": "scaffold",
    "demolition": "safety_management",
}

#: 细粒度模板键 → 清单类键（未列出的键按「同键直用」处理）。
_TEMPLATE_ALIASES: dict[str, str] = {
    "formwork_tall": "formwork",
    "formwork_disc": "formwork",
    "tower_crane": "lifting",
    "construction_hoist": "lifting",
    "earthwork": "foundation_pit",
    "monitoring": "foundation_pit",
    "manual_dig_pile": "foundation_pit",
    "slope": "foundation_pit",
    "curtain_wall": "decoration",
    "steel_structure": "prefabricated",
    "grid_structure": "prefabricated",
    "prestress": "prefabricated",
    "demolition": "safety_management",
    "demolition_blast": "safety_management",
    "demolition_machine": "safety_management",
    "demolition_manual": "safety_management",
}

#: 模板键家族前缀（顺序无关；先精确别名，再家族前缀）。
_TEMPLATE_FAMILIES: tuple[tuple[str, str], ...] = (("scaffold_", "scaffold"),)


def resolve_category_keys(category_ids, template_key: str = "") -> tuple[str, ...]:
    """文种 → 清单类键（去重保序；空结果表示无法收敛）。"""
    keys: list[str] = []
    for cid in category_ids or ():
        key = _CATEGORY_ID_TO_KEY.get(str(cid or ""))
        if key and key not in keys:
            keys.append(key)
    for key in _template_keys(str(template_key or "")):
        if key not in keys:
            keys.append(key)
    return tuple(keys)


def _template_keys(template_key: str) -> tuple[str, ...]:
    if not template_key:
        return ()
    aliased = _TEMPLATE_ALIASES.get(template_key)
    if aliased:
        return (aliased,)
    for prefix, family in _TEMPLATE_FAMILIES:
        if template_key.startswith(prefix):
            return (family,)
    if template_key in CATEGORY_STANDARD_BASES:
        return (template_key,)
    return ()


def candidate_standard_bases(category_ids, template_key: str = "") -> tuple[str, ...]:
    """文种 → 标准号基号集合（归一化去重排序；空元组表示无可信候选）。"""
    keys = resolve_category_keys(category_ids, template_key)
    bases = {
        strip_standard_year(code)
        for key in keys
        for code in CATEGORY_STANDARD_BASES.get(key, ())
        if strip_standard_year(code)
    }
    return tuple(sorted(bases))
