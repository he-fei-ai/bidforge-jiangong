"""四套专项方案分类体系的**一致性护栏**（2026-10-01，缺口 G11）。

为什么需要
----------
本仓同时存在四套**语义不同但互相引用**的分类体系：

  (a) ``scheme_classification.HAZARD_CATEGORIES``  危大六大类 + 20 子类
      （法规分类，``id`` = ``foundation_pit`` 等）
  (b) ``scheme_classification.NINE_CHAPTERS[].category_fields``  九大章节
      下的**危大专属字段**（键名用的是 (a) 的 ``category_id``）
  (c) ``sse_handlers._DANGEROUS_REQUIRED_KEYWORDS``  危大 10 章必备关键词
      （住建部 37 号令 9 章 + 本仓额外加的「监测方案」）
  (d) ``scheme_classification.HAZARD_THRESHOLDS``  14 个阈值表的键
      （用的是 (a) 的**子类 id**，即 ``fp_``/``fw_``/``ho_``/``sc_``/``ot_`` 前缀）

四套并存且**互相引用**（b 引用 a 的 id、d 引用 a 的子类 id、c 与
NINE_CHAPTERS 的标题重叠），但**没有任何断言**保证它们不漂移 —— 而本仓
历史已反复证明「同一判据在多处各自实现 ⇒ 必然分叉」（AGENTS.md §4.3/§4.7/
§4.13/§4.14 均是同构教训）。

本护栏把「四套必须对齐」变成**可自动检测的契约**：
  - (a) 六大类 id ↔ (b) category_fields 键，必须**完全一致**（不多不少）；
  - (d) 阈值键必须是 (a) 某子类的 id（不得出现游离于分类之外的阈值）；
  - (a) 六大类必须覆盖建办质〔2018〕31 号附件一的六大类；
  - (d) 阈值**必须**是闭区间（``>=``）—— 部文措辞是「及以上」，
    写成 ``>`` 会让恰好等于阈值的工程漏判（脚手架 24m 已是前车之鉴）；
  - (c) 10 章与 NINE_CHAPTERS 的标题必须对齐（监测方案为额外章）；
  - 超规模阈值不得低于危大阈值（否则出现「超规模反而不超规模」）。

⚠️ 本护栏**只断言已成立的不变式**，不新增任何业务数据 ——
新增分类时它会定向失败，提醒同步下游（这正是它存在的意义）。

测试策略：纯断言，零 AI、零 DB。
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.routers.sse_handlers import _DANGEROUS_REQUIRED_KEYWORDS  # noqa: E402
from app.services.scheme_classification import (  # noqa: E402
    HAZARD_CATEGORIES,
    HAZARD_THRESHOLDS,
    NINE_CHAPTERS,
)

# ---------------------------------------------------------------------------
# 建办质〔2018〕31 号附件一：危大工程分部分项工程（六大类）
# 护栏的「期望值」写死在此处，而不是从代码里再推导一遍（否则仍是单一来源）
# ---------------------------------------------------------------------------
#: 六大类 category_id（代码侧标识）
EXPECTED_HAZARD_CATEGORY_IDS: frozenset[str] = frozenset({
    "foundation_pit",   # 基坑工程
    "formwork",         # 模板工程及支撑体系
    "hoisting",         # 起重机械安装拆卸工程
    "scaffold",         # 脚手架工程
    "demolition",       # 拆除工程
    "other",            # 其他（幕墙 / 钢结构 / 预应力 / 暗挖 / 有限空间等）
})

#: 九大章节 key（住建部令 37 号第十七条）
EXPECTED_NINE_CHAPTER_KEYS: tuple[str, ...] = (
    "overview", "basis", "plan", "technique", "safety",
    "personnel", "acceptance", "emergency", "calc_drawings",
)

#: 危大 10 章 = 九大章节 + 本仓额外加的「监测方案」（不在 31 号文九章内）
EXTRA_DANGEROUS_CHAPTER = "监测方案"

#: 前 9 章标签与九大章节的**对应关系**（标签是法定全称的简写，顺序一一对应）。
#: 写成显式映射而非「子串」断言：「人员分工」并不是「施工管理及作业人员配备和分工」
#: 的子串（中间隔着「配备和」），「计算书及相关图纸」也不是「计算书及相关施工图纸」
#: 的子串 —— 用子串判定会把**正确**的简称判成缺陷，后人只会把护栏改松。
#: 顺序仍由下标锁定：调换顺序会让预检把「安全」错配到「施工工艺」。
EXPECTED_DANGEROUS_CHAPTER_LABELS: tuple[str, ...] = (
    "工程概况", "编制依据", "施工计划", "施工工艺技术", "安全保证措施",
    "人员分工", "验收要求", "应急处置措施", "计算书及相关图纸",
)


def _cat_ids() -> set:
    return {c["id"] for c in HAZARD_CATEGORIES}


def _sub_ids() -> set:
    return {s["id"] for c in HAZARD_CATEGORIES for s in c.get("subs") or []}


# ---------------------------------------------------------------------------
# 1. 危大六大类：分类本身（a）
# ---------------------------------------------------------------------------

def test_hazard_categories_are_exactly_six():
    assert len(HAZARD_CATEGORIES) == 6
    assert _cat_ids() == set(EXPECTED_HAZARD_CATEGORY_IDS), (
        f"危大六大类 id 漂移：{sorted(_cat_ids())}")


def test_hazard_category_ids_unique():
    ids = [c["id"] for c in HAZARD_CATEGORIES]
    assert len(ids) == len(set(ids)), "六大类 id 重复"


def test_hazard_subcategory_ids_globally_unique():
    """子类 id 是 ``HAZARD_THRESHOLDS`` 的外键，重复会让阈值表静默覆盖。"""
    subs = [s["id"] for c in HAZARD_CATEGORIES for s in c.get("subs") or []]
    assert len(subs) == len(set(subs)), f"子类 id 重复：{subs}"


def test_hazard_subcategory_prefix_matches_parent():
    """子类 id 前缀必须能反推所属大类（``fp_`` → ``foundation_pit``）。"""
    prefix_map = {
        "fp": "foundation_pit", "fw": "formwork", "ho": "hoisting",
        "sc": "scaffold", "dm": "demolition", "ot": "other",
    }
    for c in HAZARD_CATEGORIES:
        for s in c.get("subs") or []:
            prefix = str(s["id"]).split("_", 1)[0]
            assert prefix_map.get(prefix) == c["id"], (
                f"子类 {s['id']!r} 的前缀 {prefix!r} 与所属大类 "
                f"{c['id']!r} 不符")


def test_hazard_categories_have_name_standards_and_subs():
    """每个大类必须有中文名、子类、standards 字段（**允许为空列表**）。

    ⚠️ ``standards`` 不强制非空：拆除 / 爆破类在本仓**确实**没有独立标准清单
    （其依据由 ``standards_registry`` 的法规清单承载）。断言非空会把
    「本就不该有标准清单」误报为缺陷，反而让护栏被后人放宽。
    这里只锁**结构完整**（字段存在且类型正确），不锁业务内容。
    """
    for c in HAZARD_CATEGORIES:
        assert c.get("name"), f"六大类 {c['id']} 缺中文名（前端展示用）"
        assert isinstance(c.get("standards"), list), \
            f"六大类 {c['id']} 的 standards 字段类型错误（应为 list）"
        assert c.get("subs"), f"六大类 {c['id']} 缺子类（无法做阈值判定）"
        for s in c["subs"]:
            assert s.get("name"), f"子类 {s['id']} 缺中文名"
            assert s.get("keywords"), f"子类 {s['id']} 缺识别关键词"


# ---------------------------------------------------------------------------
# 2. 六类 ↔ 九章 category_fields（a ↔ b）
# ---------------------------------------------------------------------------

def test_nine_chapters_exactly_nine_and_ordered():
    assert len(NINE_CHAPTERS) == 9
    keys = tuple(c["key"] for c in NINE_CHAPTERS)
    assert keys == EXPECTED_NINE_CHAPTER_KEYS, f"九大章节 key 漂移：{keys}"
    assert [c["chapter"] for c in NINE_CHAPTERS] == list(range(1, 10)), \
        "九大章节编号必须连续 1..9"


def test_chapter_titles_unique_and_non_empty():
    titles = [c["title"] for c in NINE_CHAPTERS]
    assert all(titles)
    assert len(titles) == len(set(titles))


@pytest.mark.parametrize("chapter_index", [0, 1])
def test_category_fields_keys_equal_six_categories(chapter_index):
    """b 的键必须**恰好**是 a 的六大类 id（多一个少一个都是分叉）。"""
    fields = NINE_CHAPTERS[chapter_index].get("category_fields") or {}
    assert set(fields) == set(EXPECTED_HAZARD_CATEGORY_IDS), (
        f"第 {NINE_CHAPTERS[chapter_index]['chapter']} 章 category_fields "
        f"键与危大六大类不一致：多 {sorted(set(fields) - _cat_ids())}、"
        f"少 {sorted(_cat_ids() - set(fields))}")


def test_every_category_has_fields_in_overview_and_basis():
    """每一类危大都必须在「工程概况」和「编制依据」里有字段落点。"""
    for idx in (0, 1):
        fields = NINE_CHAPTERS[idx].get("category_fields") or {}
        for cat_id in EXPECTED_HAZARD_CATEGORY_IDS:
            assert fields.get(cat_id), (
                f"第 {NINE_CHAPTERS[idx]['chapter']} 章缺 {cat_id} 的字段清单")


# ---------------------------------------------------------------------------
# 3. 阈值表（d）必须挂在已声明子类（a）之下
# ---------------------------------------------------------------------------

def test_threshold_keys_are_declared_subcategories():
    orphans = sorted(set(HAZARD_THRESHOLDS) - _sub_ids())
    assert not orphans, (
        f"阈值表存在游离于六大类之外的键（改了阈值却查不到所属分类）：{orphans}")


def test_every_threshold_can_reach_hazard_and_oversize():
    """每条阈值都必须能判出危大、且能判出超规模。

    「判得出危大」有三种合法表达（缺一即永远是死规则）：
      1. ``hazard_when`` 非空 —— 按参数条件判定（如基坑深度 >= 3m）；
      2. ``hazard_always=True`` —— 无条件即危大（起重机械安装拆卸工程本身
         即危大，只有「起重量 >= 300kN / 起升高度 >= 200m」才超规模）；
      3. **空规则**（``params`` / ``hazard_when`` / ``oversize_when`` 全空）——
         非参数型危大（附着式升降脚手架、悬挑式、门型架、预应力张拉等），
         部文本就无量化条件，按「出现即危大、同属超规模」保守判定；
         `evaluate_hazard_level` 对这三种均有显式分支。

    真正要拦的是**半吊子规则**：声明了条件却两个字段都空
    （有参数却什么都不判）。
    """
    for key, rule in HAZARD_THRESHOLDS.items():
        is_empty_rule = (not rule.get("params")
                         and not rule.get("hazard_when")
                         and not rule.get("oversize_when"))
        if is_empty_rule:
            continue   # 非参数型危大，由 evaluate_hazard_level 显式分支处理
        assert rule.get("params"), f"{key} 声明了条件却未声明 params"
        assert rule.get("hazard_when") or rule.get("hazard_always"), \
            f"{key} 有 params 却既无 hazard_when 也无 hazard_always"
        assert rule.get("oversize_when"), f"{key} 缺 oversize_when（判不出超规模）"


def test_oversize_threshold_never_looser_than_hazard():
    """超规模阈值必须**不低于**危大阈值，否则出现「超规模反而不超规模」。"""
    for key, rule in HAZARD_THRESHOLDS.items():
        for hz in rule["hazard_when"]:
            for os_ in rule["oversize_when"]:
                if hz[0] != os_[0]:
                    continue      # 不同参数，逐参数不交叉比较
                assert os_[2] >= hz[2], (
                    f"{key}.{hz[0]} 超规模阈值 {os_[2]} < 危大阈值 {hz[2]}")


def test_all_threshold_conditions_use_closed_interval():
    """⚠️ 部文措辞是「**及以上**」，必须用 ``>=``。

    写成 ``>`` 会让**恰好等于阈值**的工程漏判 —— 脚手架 24m 曾因此
    被判为「非危大」，是本仓已发生过的真实事故（AGENTS.md §4.16.3）。
    """
    for key, rule in HAZARD_THRESHOLDS.items():
        for field in ("hazard_when", "oversize_when"):
            for clause in rule[field]:
                assert clause[1] == ">=", (
                    f"{key}.{field} 使用了开区间 {clause[1]!r}："
                    f"部文为「及以上」，恰好等于阈值会漏判")


def test_threshold_params_all_declared():
    """条件里引用的参数名必须出现在 ``params`` 中。"""
    for key, rule in HAZARD_THRESHOLDS.items():
        declared = set(rule["params"])
        for field in ("hazard_when", "oversize_when"):
            for clause in rule[field]:
                assert clause[0] in declared, (
                    f"{key}.{field} 引用了未声明参数 {clause[0]!r}")


# ---------------------------------------------------------------------------
# 4. 危大 10 章（c）↔ 九大章节
# ---------------------------------------------------------------------------

def test_dangerous_required_keywords_has_ten_chapters():
    assert len(_DANGEROUS_REQUIRED_KEYWORDS) == 10


def test_dangerous_chapters_align_with_nine_chapters_plus_one():
    """10 章 = 九大章节（**简称**）+ 额外「监测方案」，顺序与全称一一对应。

    见 :data:`EXPECTED_DANGEROUS_CHAPTER_LABELS` 的注释：标签是简写，不能用
    「子串 / 逐字相等」判定，故显式列出期望序列按序比对 ——
    这样既能拦「新增/删除一章」，也能拦「调换顺序」这种最危险的漂移
    （预检会把「安全」错配到「施工工艺」，用户被要求补一个根本不需要的章节）。
    """
    nine_titles = [c["title"] for c in NINE_CHAPTERS]
    labels = [label for label, _kw in _DANGEROUS_REQUIRED_KEYWORDS]
    assert labels[-1] == EXTRA_DANGEROUS_CHAPTER, (
        f"第 10 章应为 {EXTRA_DANGEROUS_CHAPTER}（本仓额外章），实际 {labels[-1]}")
    assert tuple(labels[:9]) == EXPECTED_DANGEROUS_CHAPTER_LABELS, (
        f"危大 10 章标签序列已漂移。\n实际：{labels[:9]}\n"
        f"期望：{list(EXPECTED_DANGEROUS_CHAPTER_LABELS)}")
    assert len(nine_titles) == 9


def test_dangerous_chapter_keywords_never_empty():
    for label, keywords in _DANGEROUS_REQUIRED_KEYWORDS:
        assert keywords, f"「{label}」缺关键词 → 预检永远判为缺失"
        assert all(str(k).strip() for k in keywords), \
            f"「{label}」存在空关键词"