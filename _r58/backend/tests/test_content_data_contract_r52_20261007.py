"""一致性取值审计管线（content_data_contract / preflight_engine）缺陷回归。

本轮依据**生产实证**修复 6 个缺陷，每条护栏都要求**改动前必失败**：

- D1 **单位未归一**：「24 个月」与「24 月」被当成两个取值 → CON-01 误报。
  ``_UNIT_CANON`` 补 ``个月→月``，并公开 ``normalize_unit_text`` 别名供跨模块复用。
- D2 **桥接吃掉中文后缀**：``[^0-9]{0,N}`` 把「缺陷责任期**最长期限**」整段当桥接
  吃掉，导致三个语义完全不同的字段（24 月保修 / 7 天通知 / 28 天撤离）压成同一个
  key，真正该保留的取值被静默覆盖。现桥接只允许分隔符/空白与系词，汉字后缀一律
  不可跨。
- D3 **Markdown 泄漏**：生产事实形态是 ``- **工程名称**: 上海…``，不先剥 ``*``
  会把强调标记当取值抓进数据字典。
- D3b **停用词误杀合法数值**（本轮自引入又当场抓出）：停用词判据原被**无条件**
  应用到所有规则，而「总工期184天」本身含主题词「工期」→ 合法数值整条被丢。
  现停用词只对「自由文本取值」规则生效。
- D4 **对象维度不活动**：对象限定词必须在**匹配附近的文本窗口**里找（不能在压缩
  后的取值里找 —— 「保修期5年」结构上不可能含前置对象词「屋面及防水」）；且限定词
  已在取值里时不得再加前缀（否则「管理管理人员34人」）。
- D5 **主题覆盖度按词聚合**：一个规则键的替代式含多个同义词，按词统计会把 8 个
  规则算成 27 个「主题」，实测 missing 恒为 20 项、全是同义词噪声。现按**规则键**
  聚合，展示名带上替代式展开。
- D6 **``_row_source_ref`` 函数体为空**（本轮新发现）：函数体被 ``editor insert_line``
  错插到 ``audit_call_matrix`` 末尾成为**不可达死代码**，于是每个事实的
  ``source_ref``（原文溯源引用）都静默返回 ``None`` —— 零日志、零报错。

⚠️ **护栏自身的判据纪律**（本仓反复踩过的坑）：判据必须锚定**实现**而非**字符**
—— 用「正则字面量不含某子串」这类宽锚点会被注释/docstring 里的示例误伤，
导致护栏恒失败、后人被迫把它改松或删掉。本文件的断言一律锚定**函数输出**或
**AST 节点结构**。

✅ **接线（R52 同日收口）**：模块已进入生产路径（不再是孤儿）——
``sse_handlers._persist_section`` 的生成后自检消费
``cross_section_value_findings``（CON-01 逐章口径，与
``content_crosscheck_duplicate`` **互相独立**的开关
``content_crosscheck_values``）；``facts_builder.build_facts_text``
消费 ``build_global_data_dictionary`` + ``render_data_dictionary_block``
（数据字典注入，开关 ``content_data_dictionary``）。见 ``TestWiring``。
"""
import ast
import re
from pathlib import Path

import pytest

import app.services.preflight_engine as pf
from app.services import content_data_contract as c


def R(title: str, content: str, conf: float = 0.95,
      chapter: str = "", project_id: str = "proj") -> tuple:
    """构造事实行（5 元组）：``(project_id, title, content, confidence, chapter)``。

    ⚠️ 索引约定是硬契约：``_row_text`` 取 ``row[1]/row[2]``、``_row_confidence``
    取 ``row[3]``、``_row_chapter`` 取 ``row[4]``。写测试时若把 (title, content,
    conf) 当元组传，title 会落到 content 位置、confidence 恒为 None —— 抽取会
    「看起来正常」但溯源字段全是错的，护栏就失去了判据力。
    """
    return (project_id, title, content, conf, chapter)


# ---------------------------------------------------------------------------
# D1 单位归一
# ---------------------------------------------------------------------------
class TestUnitCanon:
    def test_month_suffix_canonized(self):
        """「24 个月」与「24 月」必须同形（生产事实里两者并存）。"""
        assert c.normalize_unit_text("缺陷责任期24个月") == "缺陷责任期24月"
        assert c.normalize_unit_text("24个月") == "24月"

    def test_public_alias_is_the_private_impl(self):
        """别名必须与私有实现是**同一对象**，否则跨模块口径会分叉。"""
        assert pf.normalize_unit_text is pf._norm_unit
        assert c.normalize_unit_text is pf.normalize_unit_text

    def test_longest_match_priority_preserved(self):
        """补「个月」不得破坏「长串优先」替换序（cm/km/mm 不得被 m 吃掉）。"""
        assert c.normalize_unit_text("90日历天") == "90天"
        assert c.normalize_unit_text("6000mm") == "6000毫米"
        assert c.normalize_unit_text("5m") == "5米"
        assert c.normalize_unit_text("8h") == "8时"
        assert c.normalize_unit_text("3.5小时") == "3.5时"

    def test_con01_no_false_conflict_across_month_forms(self):
        """生产实证：同一方案里「24 个月」与「24 月」并存时不得误报 CON-01。"""
        secs = [
            {"id": "a", "title": "工程概况", "content": "缺陷责任期为24个月。"},
            {"id": "b", "title": "质量保修", "content": "缺陷责任期最长期限24月。"},
        ]
        assert pf.numeric_consistency_findings(secs, limit=0) == []

    def test_con01_still_catches_genuine_conflict(self):
        """归一化只能收敛**同义写法**，真实分歧仍必须报（不得放宽判据）。"""
        secs = [
            {"id": "a", "title": "工程基本情况", "content": "总工期约184天。"},
            {"id": "b", "title": "劳动力配置", "content": "总工期约195天。"},
        ]
        got = pf.numeric_consistency_findings(secs, limit=0)
        assert len(got) >= 1
        assert any(f.get("rule_id") == "CON-01" for f in got)


# ---------------------------------------------------------------------------
# D2 桥接
# ---------------------------------------------------------------------------
class TestBridge:
    def _rx(self, name_rx: str) -> re.Pattern:
        return re.compile(f"{name_rx}{c._bridge()}")

    def test_bridge_rejects_plain_cjk(self):
        """桥接不得跨过任意汉字（旧实现 ``[^0-9]{0,N}`` 会跨过）。"""
        rx = re.compile(r"(?P<name>缺陷责任期)" + c._bridge()
                        + r"(\d+)\s*(个?月|年|日历天|天)")
        assert rx.search("缺陷责任期届满通知期限28天") is None
        assert rx.search("缺陷责任期最长期限24月") is None

    def test_bridge_allows_copula(self):
        """系词（为/约/达/计/共）必须仍可作为桥接，否则大量真实写法抽不到。"""
        rx = re.compile(r"(?P<name>保修期)" + c._bridge() + r"(\d+)\s*(个?月|年)")
        for text in ("保修期为5年", "保修期约5年", "保修期达5年", "保修期计5年"):
            assert rx.search(text), f"系词桥接失效: {text}"

    def test_compound_titles_stay_distinct(self):
        """三个复合标题必须各自成键，不得互相吞并（生产实证场景）。"""
        rows = [
            R("缺陷责任期", "- **缺陷责任期**: 24 个月"),
            R("变更响应期限", "- **响应期限**: 7 天"),
            R("缺陷责任期届满通知期限", "- **通知期限**: 28 天"),
        ]
        keys = {h["value_text"] for h in c.iter_consistency_values(rows)}
        assert "缺陷责任期24月" in keys
        assert "响应期限7天" in keys
        # 「通知期限」不是任何规则的主题词 → 不得被误抓成「缺陷责任期28天」
        assert "缺陷责任期28天" not in keys


# ---------------------------------------------------------------------------
# D3 Markdown 剥离
# ---------------------------------------------------------------------------
class TestMarkdownPrep:
    def test_prep_strips_emphasis_marks(self):
        assert c._prep("- **工程名称**: 上海XX") == "- 工程名称: 上海XX"
        for mark in "*#`~":
            assert mark not in c._prep(f"值{mark}X{mark}")

    def test_prep_keeps_digits_units_and_cjk(self):
        """剥离只能动标记符，数字/单位/汉字/全角标点一律不动。"""
        s = c._prep("总工期184日历天，24个月，Q1（2026年）")
        assert "184" in s and "24" in s and "2026" in s
        assert "日历天" in s and "个月" in s

    def test_fact_row_value_has_no_markdown(self):
        """生产事实形态端到端：数据字典里不得出现强调标记。"""
        rows = [
            R("工程名称", "- **工程名称**: 上海市市级机关第二幼儿园整体维修工程"),
            R("管理人员数", "- **管理人员数**: 34 人"),
        ]
        for h in c.iter_consistency_values(rows):
            assert not any(ch in h["value_text"] for ch in "*#`~"), h["value_text"]


# ---------------------------------------------------------------------------
# D3b 停用词作用域（本轮自引入又当场抓出的回归）
# ---------------------------------------------------------------------------
class TestStopwordScope:
    def test_stopwords_only_for_free_text_keys(self):
        """停用词判据只对自由文本取值规则生效 —— 数值规则必须拿到空集。"""
        assert c._stopwords_for(c._PROJECT_NAME_KEY) == c._NAME_VALUE_STOPWORDS
        for key, _p, _r, _o in c.CONSISTENCY_KEY_RULES:
            if key != c._PROJECT_NAME_KEY:
                assert c._stopwords_for(key) == frozenset(), key
        assert c._stopwords_for("不存在的键") == frozenset()

    def test_numeric_value_containing_topic_word_survives(self):
        """⚠️ 关键回归护栏：「总工期184天」本身含主题词「工期」，不得被误杀。

        旧实现把停用词无条件套到所有规则上，「工期」在停用词表里 → 合法数值
        整条被丢，数据字典永远缺工期。
        """
        rows = [R("工期计算起算依据", "- **工期**: 184 日历天")]
        keys = [h["value_text"] for h in c.iter_consistency_values(rows)]
        assert keys == ["工期184天"], keys

    def test_prose_after_project_name_rejected(self):
        """工程名后的散文说明必须被丢掉（「工程名称划分依据及数量说明」）。"""
        rows = [R("工程名称", "工程名称划分依据及数量说明详见附表")]
        assert list(c.iter_consistency_values(rows)) == []

    def test_real_project_name_still_captured(self):
        """停用词不得误伤真正的工程名（不得为了消噪而放宽到漏抽）。"""
        rows = [R("工程名称", "- **工程名称**: 上海市某某大厦装修工程")]
        keys = [h["value_text"] for h in c.iter_consistency_values(rows)]
        assert keys == ["工程名称上海市某某大厦装修工程"], keys


# ---------------------------------------------------------------------------
# D4 对象维度
# ---------------------------------------------------------------------------
class TestObjectQualify:
    def test_hint_prefixed_from_context(self):
        """对象词在**主题词之前**的上下文里时，必须作为前缀加到取值上。

        取值本身（「保修期5年」）结构上不可能含前置对象词，所以在压缩后的
        取值里找限定词必然找不到 —— 判据必须落在匹配附近的文本窗口上。
        """
        assert c._qualify_key("保修期5年", ("屋面及防水",),
                              "屋面及防水工程保修期5年") == "屋面及防水保修期5年"

    def test_hint_already_in_value_not_doubled(self):
        """⚠️ 限定词已在取值里时不得再加前缀（否则「管理管理人员34人」）。"""
        assert c._qualify_key("管理人员34人", ("管理",), "管理人员数量34人") == \
            "管理人员34人"

    def test_hint_far_from_match_not_applied(self):
        """对象词离取值太远时不得误当前缀 —— 窗口约束在调用点生效。

        ⚠️ 窗口逻辑在**调用点**（不是 ``_qualify_key`` 内部），所以本用例走
        端到端路径：一条长事实里对象词出现在与取值相距 60+ 字的另一段。
        """
        prose = "本方案由屋面及防水承包队伍施工，另行委托第三方检测单位进场。"
        rows = [R("其他说明", prose + " " * 60 + "- **保修期**: 2 年")]
        keys = [h["key"] for h in c.iter_consistency_values(rows)]
        assert keys == ["保修期2年"], keys

    def test_call_site_must_pass_a_window_not_the_whole_text(self):
        """AST 静态护栏：取键窗口必须由匹配位置切片，不得整行文本直接传入。

        ⚠️ 本轮 D4 的窗口逻辑写在**调用点**。首版 ``iter_consistency_values``
        把整行 ``text`` 直接传给 ``_qualify_key``，等于绕过窗口 —— 一条长事实里
        对象词出现在无关段落也会被当成取值前缀。该回退形态语义上完全合法，
        只有静态锚点能拦住。
        """
        src = Path(c.__file__).read_text(encoding="utf-8")
        idx = src.find('_qualify_key(value_text, obj_hints, ')
        assert idx > 0, "取键调用点丢失"
        # 取第三个位置参数：必须是按匹配位置切片的窗口名，不得是整行 text
        args = src[idx + len('_qualify_key(value_text, obj_hints, '):]
        assert args.startswith('win)'), args[:30]
        assert not args.startswith('text'), args[:30]
        # 窗口必须是「按匹配起点切片」的表达式
        region = src[max(0, idx - 400):idx]
        assert "m.start()" in region, "窗口未以匹配起点为锚点"

    def test_no_object_hints_passthrough(self):
        assert c._qualify_key("总工期184天", None, "总工期184天") == "总工期184天"
        assert c._qualify_key("总工期184天", (), "总工期184天") == "总工期184天"

    def test_five_warranty_facts_stay_distinct(self):
        """生产实证：多条保修期事实必须各自成键（合法差异不得被判成冲突）。"""
        rows = [
            R("屋面及防水工程保修期", "- **保修期**: 5 年"),
            R("装修工程保修期", "- **保修期**: 2 年"),
            R("电气给排水设备安装工程保修期", "- **保修期**: 2 年"),
            R("缺陷责任期", "- **缺陷责任期**: 24 个月"),
            R("缺陷责任期最长期限", "- **缺陷责任期**: 24 月"),
        ]
        keys = [h["key"] for h in c.iter_consistency_values(rows)]
        assert "屋面及防水保修期5年" in keys
        assert "装修保修期2年" in keys
        assert "缺陷责任期24月" in keys
        # 对象前缀不得重复（D4 的核心不变式）
        assert not any(k.count("保修期") > 1 for k in keys), keys


# ---------------------------------------------------------------------------
# 自指匹配重扫
# ---------------------------------------------------------------------------
class TestSelfRefRescan:
    def test_self_ref_detected(self):
        rx = re.compile(r"(?P<name>工程名称)" + c._bridge()
                        + r"([\u4e00-\u9fffA-Za-z0-9（）()、\-—]{2,60})")
        m = rx.search("工程名称工程名称")
        assert m is not None
        assert c._is_self_ref(m, c._value_text(m)) is True

    def test_self_ref_rescans_next_occurrence(self):
        """⚠️ 关键不变式：自指命中后必须从 ``start()+1`` 重扫。

        旧实现用 ``finditer`` + continue：自指匹配已经**消费掉了第二个同名主题词**，
        ``finditer`` 从匹配末尾继续，于是「工程名称 - **工程名称**: 上海XX」里真正
        带取值的那一处再也没机会被扫到，工程名整条丢失。
        """
        rows = [("工程名称", "- 工程名称: 上海市某某大厦装修工程", 1.0)]
        keys = [h["value_text"] for h in c.iter_consistency_values(rows)]
        assert any(k.startswith("工程名称上海") for k in keys), keys
        assert "工程名称工程名称" not in keys

    def test_bare_topic_only_not_yielded(self):
        """只取到主题名、没取到取值时不得产出空值条目。"""
        rx = re.compile(r"(?P<name>总工期)" + c._bridge() + r"(\d+)\s*(日历天|天)")
        assert list(c._iter_value_matches("工期要求见规范", rx)) == []


# ---------------------------------------------------------------------------
# D5 主题词 / 替代式
# ---------------------------------------------------------------------------
class TestTopicWords:
    def test_topic_words_expanded_no_pipe(self):
        """主题词必须按 ``|`` 展开成单词，不得残留替代式。"""
        words = c._topic_words()
        assert words, "主题词集合不得为空"
        assert not any("|" in w for w in words), [w for w in words if "|" in w]

    def test_rule_aliases_complete(self):
        aliases = c._RULE_ALIASES
        assert set(aliases) == {k for k, _p, _r, _o in c.CONSISTENCY_KEY_RULES}

    def test_topic_words_covers_every_alias(self):
        """主题词集合必须等于全部替代式的并集（防替代式漂移静默漏报）。"""
        expected = set()
        for _k, pat, _r, _o in c.CONSISTENCY_KEY_RULES:
            expected |= {w for w in str(pat).split("|") if w}
        assert c._topic_words() == expected

    def test_extracted_names_all_registered(self):
        """抓到的主题词必须都在规则表里 —— 否则就是某处 name 组写错了词。"""
        rows = [
            R("工程名称", "- **工程名称**: 上海XX"),
            R("缺陷责任期", "- **缺陷责任期**: 24 个月"),
            R("管理人员数", "- **管理人员数**: 34 人"),
            R("计划开工日期", "- **计划开工日期**: 2026年05月08日"),
        ]
        names = {h["name"] for h in c.iter_consistency_values(rows)}
        assert names, "样本必须能抓到主题词"
        assert names <= c._topic_words(), names - c._topic_words()

    def test_must_keep_excludes_project_name_only(self):
        """排除只针对「项目全称」这一条规则，不得误伤其它规则的主题词。"""
        keep = c.MUST_KEEP_TOPIC_NAMES
        for w in ("缺陷责任期", "总工期", "人员数量", "响应期限"):
            assert w in keep, w
        for w in ("工程名称", "项目名称", "项目全称", "工程全称"):
            assert w not in keep, w


# ---------------------------------------------------------------------------
# D5 审计覆盖度
# ---------------------------------------------------------------------------
class TestAuditCoverage:
    @pytest.fixture()
    def facts(self):
        return [
            ("工程名称", "- **工程名称**: 上海市某某大厦装修工程", 1.0, 0.9, "overview"),
            ("计划开工日期", "- **计划开工日期**: 2026年05月08日", 0.95, 0.9, "plan"),
            ("计划竣工日期", "- **计划竣工日期**: 2026年11月18日", 0.95, 0.9, "plan"),
            ("缺陷责任期", "- **缺陷责任期**: 24 个月", 0.95, 0.9, "safety"),
            ("变更响应期限", "- **响应期限**: 7 天", 0.85, 0.85, "emergency"),
            ("管理人员数", "- **管理人员数**: 34 人", 0.9, 0.9, "personnel"),
        ]

    def test_coverage_grouped_by_rule_key(self, facts):
        """⚠️ 关键不变式：按**规则键**聚合，不按单个主题词。"""
        f = c.audit_call_matrix(extraction_items=[], facts_rows=facts,
                                sections=[])["facts_inventory"]
        assert len(f["consistency_topics_covered"]) == 6
        for item in f["consistency_topics_covered"]:
            assert "|" not in item.split("（")[0], item

    def test_missing_shrinks_to_real_gaps(self, facts):
        """缺失清单只含**真正的空档**，不再被同义词撑到恒假红。"""
        f = c.audit_call_matrix(extraction_items=[], facts_rows=facts,
                                sections=[])["facts_inventory"]
        missing = f["consistency_topics_missing"]
        assert set(missing) == {"总工期（总工期|工期）",
                                "设备型号（设备型号|机械型号|起重机型号|升降机型号）"}

    def test_display_includes_alias_expansion(self, facts):
        """展示名必须带上替代式展开，让用户看清一个主题含哪些同义词。"""
        f = c.audit_call_matrix(extraction_items=[], facts_rows=facts,
                                sections=[])["facts_inventory"]
        for item in f["consistency_topics_missing"]:
            assert "（" in item and "|" in item, item

    def test_empty_input_all_missing_and_no_crash(self):
        inv = c.audit_call_matrix(extraction_items=[], facts_rows=[], sections=[])
        f = inv["facts_inventory"]
        assert f["total"] == 0
        assert f["consistency_topics_covered"] == []
        assert len(f["consistency_topics_missing"]) == len(c.CONSISTENCY_KEY_RULES)
        assert inv["extraction_inventory"] == []
        assert inv["call_matrix"] == []
        assert isinstance(inv["generated_report"], dict)

    def test_gaps_sorted_high_first(self):
        gaps = c.audit_call_matrix(extraction_items=[], facts_rows=[],
                                   sections=[])["gaps"]
        assert gaps, "空事实库必然产出缺失清单"
        order = [0 if g["severity"] == "high" else 1 for g in gaps]
        assert order == sorted(order)

    def test_gaps_carry_impact_text(self):
        gaps = c.audit_call_matrix(extraction_items=[], facts_rows=[],
                                   sections=[])["gaps"]
        for g in gaps:
            assert g["kind"], g
            assert g["severity"] in ("high", "medium", "low"), g
            assert g["impact"], g


# ---------------------------------------------------------------------------
# D6 source_ref 溯源引用（本轮新发现：函数体为空 + 不可达死代码）
# ---------------------------------------------------------------------------
class TestSourceRef:
    def test_source_ref_from_dict(self):
        assert c._row_source_ref({"source_ref": "docA#page:3#section:概况"}) == \
            "docA#page:3#section:概况"

    def test_source_ref_empty_for_tuple(self):
        """元组行没有 source_ref 列 → 空串（不是 None）。"""
        assert c._row_source_ref(("proj", "缺陷责任期", "24 个月", 0.95)) == ""
        assert c._row_source_ref(("a", "b", "c", 0.9, "ch")) == ""

    def test_source_ref_fail_soft(self):
        """⚠️ 修复前该函数**无任何 return**，一律返回 None。"""
        for bad in (None, object(), (), [], {"source_ref": None}):
            assert c._row_source_ref(bad) == "", bad

    def test_source_ref_flows_into_data_dictionary(self):
        """溯源引用必须一路传到数据字典条目（修复前该字段恒为 None）。"""
        rows = [("proj", "工程名称", "- **工程名称**: 上海XX", 1.0, "overview"),
                {"title": "缺陷责任期", "content": "- **缺陷责任期**: 24 个月",
                 "confidence": 0.95, "source_ref": "docB#page:9"}]
        d = c.build_global_data_dictionary(rows)
        refs = [v["source_ref"] for v in d.values()]
        assert "docB#page:9" in refs, refs
        assert all(r is not None for r in refs), refs

    def test_rule_key_flows_into_data_dictionary(self):
        """rule_key 是审计覆盖度按规则键聚合的前提，必须一路透传。"""
        rows = [("proj", "缺陷责任期", "- **缺陷责任期**: 24 个月", 0.95, "safety")]
        d = c.build_global_data_dictionary(rows)
        assert all(v.get("rule_key") == "保修期与缺陷责任期" for v in d.values())

    def test_no_statement_after_return_in_any_function(self):
        """AST 护栏：任何函数体里最后一个顶层 Return 之后不得再有语句。

        这正是 D6 的根因形态 —— 一段 ``return`` 被错插到别的函数末尾，
        成为**不可达死代码**，而原函数只剩 docstring 静默返回 None（零日志）。
        """
        tree = ast.parse(Path(c.__file__).read_text(encoding="utf-8"))
        unreachable = []
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            seen_return = False
            for stmt in fn.body:
                if isinstance(stmt, ast.Return):
                    seen_return = True
                elif seen_return:
                    unreachable.append(fn.name)
                    break
        assert unreachable == [], f"存在 return 之后的不可达语句: {unreachable}"

    def test_no_return_outside_function(self):
        tree = ast.parse(Path(c.__file__).read_text(encoding="utf-8"))
        assert not [n for n in tree.body if isinstance(n, ast.Return)]
        assert all(n.col_offset >= 4
                   for n in ast.walk(tree) if isinstance(n, ast.Return))


# ---------------------------------------------------------------------------
# fail-soft
# ---------------------------------------------------------------------------
class TestFailSoft:
    def test_digest_empty_when_no_values(self):
        assert c.build_cross_section_digest({}) == ""
        assert c.build_cross_section_digest({"s": ""}) == ""
        assert c.build_cross_section_digest({"s": "没有任何数值主题的散文。"}) == ""

    def test_digest_cap_and_item_limit_respected(self):
        secs = {f"s{i}": f"总工期约{i}天。" for i in range(1, 40)}
        out = c.build_cross_section_digest(secs, max_items=5)
        assert out, "样本必然能抽出取值"
        assert len(out) <= c.DIGEST_MAX_CHARS
        assert out.count("\n- 【") <= 5

    def test_digest_accepts_dict_values(self):
        out = c.build_cross_section_digest(
            {"s1": {"title": "工程基本情况", "content": "总工期约184天。"}})
        assert "工程基本情况" in out
        assert "总工期184天" in out

    def test_cross_section_findings_fail_soft_on_garbage(self):
        for bad in (None, [], "x", 42, [None, 42, "s"], [{"content": None}]):
            assert c.cross_section_value_findings(bad, new_section_id="s1") == []

    def test_cross_section_findings_requires_new_title(self):
        """new_section_id 对不上任何章节 → 无归属可判，返回空。"""
        secs = [{"id": "a", "title": "工程基本情况", "content": "总工期约184天。"}]
        assert c.cross_section_value_findings(secs, new_section_id="不存在") == []

    def test_cross_section_findings_returns_only_own_chapter(self):
        """判据同源 + 只报本章：必须复用预检 CON-01 的唯一实现并按本章过滤。

        证据里章节名嵌在每条取值文本尾部（``总工期…184（工程基本情况）``），
        不是独立的列表项 —— 故按子串判定。
        """
        secs = [
            {"id": "a", "title": "工程基本情况", "content": "总工期约184天。"},
            {"id": "b", "title": "劳动力配置", "content": "总工期约195天。"},
        ]
        # limit=0 → 不截断，生成侧自检可拿到全部主题
        got = c.cross_section_value_findings(secs, new_section_id="a", limit=0)
        assert got, "两章取值确实不同，必须报出冲突"
        for f in got:
            assert f["rule_id"] == "CON-01"
            assert f["source"] == "selfcheck"
            assert any("工程基本情况" in e for e in f["evidence"]), f["evidence"]
            assert "工程基本情况" in f["detail"]


# ---------------------------------------------------------------------------
# 文档化缺口
# ---------------------------------------------------------------------------
class TestWiring:
    """接线锁（R52 同日收口）：本模块的能力必须真的被生产路径消费。

    ⚠️ 判据纪律：AST 扫真实调用节点，不匹配「源码含字符串」——
    后者会被注释里的函数名误伤（本仓 §5.14 同族教训）。
    """

    @staticmethod
    def _call_names(tree, pred):
        out = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and pred(node):
                out.append(node)
        return out

    @staticmethod
    def _name_of(call):
        f = call.func
        if isinstance(f, ast.Name):
            return f.id
        if isinstance(f, ast.Attribute):
            return f.attr
        return ""

    def _app_tree(self, rel):
        p = Path(c.__file__).resolve().parent.parent / rel
        return ast.parse(p.read_text(encoding="utf-8")), p

    def test_module_is_imported_by_production_code(self):
        """生产代码至少一处真实 import（接线最低门槛）。"""
        app_dir = Path(c.__file__).resolve().parent.parent
        self_name = Path(c.__file__).name
        for py in app_dir.rglob("*.py"):
            if py.name == self_name:
                continue
            if "content_data_contract" in py.read_text(encoding="utf-8",
                                                        errors="replace"):
                return
        raise AssertionError("全仓 app/ 下无任何文件引用 content_data_contract")

    def test_sse_handlers_calls_value_findings_with_new_section_id(self):
        """CON-01 逐章自检必须传 new_section_id —— 不传则 new_title 恒空、
        函数恒返回 []（自检静默空转，等于没接线）。"""
        tree, _ = self._app_tree(Path("routers") / "sse_handlers.py")
        calls = self._call_names(
            tree, lambda n: self._name_of(n) == "cross_section_value_findings")
        assert calls, "sse_handlers 未调用 cross_section_value_findings"
        ok = any(
            any(k.arg == "new_section_id" for k in call.keywords)
            for call in calls)
        assert ok, "cross_section_value_findings 调用缺 new_section_id 实参"

    def test_value_check_switch_is_independent_of_duplicate_switch(self):
        """两个开关必须互相独立：CON-01 自检不得嵌在
        `if _crosscheck_dup_on:` 块内（否则关掉搬运检测连带关掉数值自检）。
        AST 判定：value-findings 调用节点的**祖先 If 链**上不得出现
        `_crosscheck_dup_on` 单独作门控的节点。"""
        tree, _ = self._app_tree(Path("routers") / "sse_handlers.py")
        # 收集每个 Call 所在的最内层 If 测试源码链
        calls = self._call_names(
            tree, lambda n: self._name_of(n) == "cross_section_value_findings")
        assert calls
        parent = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parent[child] = node
        for call in calls:
            gates = []
            node = call
            while node in parent:
                node = parent[node]
                if isinstance(node, ast.If):
                    gates.append(ast.unparse(node.test))
            dup_only = [g for g in gates
                        if "_crosscheck_dup_on" in g
                        and "_crosscheck_values_on" not in g]
            assert not dup_only, (
                f"CON-01 调用被搬运开关单独门控: {dup_only}")

    def test_facts_builder_injects_data_dictionary(self):
        """数据字典必须真的注入 facts 文本，且受开关门控。

        ⚠️ 只断言「调用存在」会被 `if False:` 这类死分支变异逃逸
        （A/B 实证）—— 必须同时断言 render 调用位于引用
        ``content_data_dictionary`` 的 If 门控之下。
        """
        tree, _ = self._app_tree(Path("services") / "facts_builder.py")
        for fn in ("build_global_data_dictionary", "render_data_dictionary_block"):
            calls = self._call_names(tree, lambda n, fn=fn: self._name_of(n) == fn)
            assert calls, f"facts_builder 未调用 {fn}"
        parent = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parent[child] = node
        render_calls = self._call_names(
            tree, lambda n: self._name_of(n) == "render_data_dictionary_block")
        gated = False
        for call in render_calls:
            node = call
            while node in parent:
                node = parent[node]
                if isinstance(node, ast.If) and \
                        "content_data_dictionary" in ast.unparse(node.test):
                    gated = True
        assert gated, "数据字典注入必须受 content_data_dictionary 开关门控"

    def test_config_flags_exist_and_default_true(self):
        """两个新开关必须在 config 中登记且默认 True（向后兼容 = 默认生效）。"""
        import app.config as cfg
        assert getattr(cfg.settings, "content_crosscheck_values") is True
        assert getattr(cfg.settings, "content_data_dictionary") is True


class TestKeyRulesChapterAlignment:
    """一致性主题键 ⇔ 九大章节必含要素 对齐（R52 收口）。

    「章节 → 所需字段」映射与提示词要素清单的唯一事实源是
    ``NINE_CHAPTERS.base_fields``；若 ``CONSISTENCY_KEY_RULES`` 的数据
    字典权威键在九大章节无落点，会出现「数据字典有值、要素清单不要求、
    提示词不提示」的断链。本护栏锁定逐键落点（语义别名在测试内显式声明，
    后人改名/移除任一侧即红）。
    """

    def _fields(self, key):
        from app.services.scheme_classification import required_fields_for_chapter
        return set(required_fields_for_chapter(key))

    def test_exact_keys_have_chapter_home(self):
        assert "总工期" in self._fields("overview")
        assert "计划开工日期" in self._fields("plan")
        assert "计划竣工日期" in self._fields("plan")
        assert "保修期与缺陷责任期" in self._fields("acceptance")
        assert "响应时限" in self._fields("emergency")

    def test_aliased_keys_have_semantic_home(self):
        """无逐字同名字段的键，其语义落点字段必须存在（显式别名表）。"""
        aliases = {
            "项目全称": ("overview", ("工程名称",)),
            "人员数量": ("plan", ("劳动力配置表", "作业人员配置")),
            "设备型号": ("plan", ("设备配置清单",)),
        }
        for rule_key, (chapter, names) in aliases.items():
            fields = self._fields(chapter)
            hit = [n for n in names if n in fields]
            assert hit, f"一致性键「{rule_key}」在 {chapter} 章无语义落点 {names}"

    def test_appended_fields_do_not_reorder_existing_fields(self):
        """R52 追加必须在尾部 —— 不得改动既有要素顺序（历史契约）。"""
        from app.services.scheme_classification import NINE_CHAPTERS
        for ch in NINE_CHAPTERS:
            base = ch["base_fields"]
            for tail, anchor in (("总工期", "气候特征"),
                                 ("保修期与缺陷责任期", "验收人员组成"),
                                 ("响应时限", "附近医院信息")):
                if tail in base:
                    assert base.index(tail) > base.index(anchor), (
                        f"{ch['key']}: {tail} 必须在 {anchor} 之后")







