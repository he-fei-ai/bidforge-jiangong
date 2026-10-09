"""F-CONTENT-STANDARD · Task 2：生成标准纯函数服务测试

覆盖：
- 生效标准三级回落（任务 override / 章节 / 方案 / 默认）与非法值容错；
- 提示词文案两模式差异化注入；
- 代码块剥离、数值/型号提取、规范编号排除；
- 精准模式：模糊表述（概数/空泛/限值）、事实缺失、数值矛盾、限值事实豁免；
- 模糊模式：概数放行、不查覆盖率、±10% 容差、超差 value_conflict；
- 型号冲突（两模式）、占位符计数、降级不抛异常。
"""
import pytest
from app.services.content_standard import (
    FUZZY,
    PRECISE,
    build_continue_hint,
    build_system_block,
    build_user_block,
    extract_model_tokens,
    extract_number_tokens,
    normalize_standard,
    resolve_effective_standard,
    standard_report,
    strip_code_blocks,
)


def _fact(group: str, title: str, content: str):
    """构造 5 元组事实行（gt, title, content, confidence, chapter）。"""
    return (group, title, content, 0.9, "technique")


# ============================================================
# 生效值解析
# ============================================================
class TestResolve:

    def test_normalize(self):
        assert normalize_standard("precise") == "precise"
        assert normalize_standard("fuzzy") == "fuzzy"
        assert normalize_standard("strict") is None
        assert normalize_standard("") is None
        assert normalize_standard(None) is None

    def test_default_is_precise(self):
        assert resolve_effective_standard() == PRECISE

    def test_scheme_level(self):
        assert resolve_effective_standard(scheme_standard="fuzzy") == FUZZY
        # 非法方案值回落默认
        assert resolve_effective_standard(scheme_standard="x") == PRECISE

    def test_section_overrides_scheme(self):
        assert resolve_effective_standard(
            section_standard="precise", scheme_standard="fuzzy") == PRECISE
        # 空串=沿用方案
        assert resolve_effective_standard(
            section_standard="", scheme_standard="fuzzy") == FUZZY

    def test_task_override_semantics(self):
        # override=False：任务值被忽略（「按章节设置」）
        assert resolve_effective_standard(
            task_standard="fuzzy", override=False,
            section_standard="precise", scheme_standard="precise") == PRECISE
        assert resolve_effective_standard(
            task_standard="fuzzy", override=False,
            section_standard="", scheme_standard="precise") == PRECISE
        # override=True：任务值强制，压过章节
        assert resolve_effective_standard(
            task_standard="fuzzy", override=True,
            section_standard="precise", scheme_standard="precise") == FUZZY
        # override=True 但任务值非法 → 回落章节/方案
        assert resolve_effective_standard(
            task_standard="", override=True,
            section_standard="fuzzy", scheme_standard="precise") == FUZZY


# ============================================================
# 提示词文案
# ============================================================
class TestPromptBlocks:

    def test_precise_block_contains_strict_phrases(self):
        s = build_system_block(PRECISE)
        assert "精准内容" in s
        assert "原样引用" in s
        assert "约" in s and "满足要求的" in s
        # ✅ 2026-10-01（模糊生成改造）：精准模式缺失数据改为按模糊生成规则补齐，
        #    不再要求输出【待补充：参数名】—— 文案必须出现该规则且无占位指令。
        assert "模糊生成规则" in s
        assert "【待补充：参数名】" not in s

    def test_fuzzy_block_contains_tolerance_and_no_contradiction(self):
        s = build_system_block(FUZZY)
        assert "模糊内容" in s
        assert "±10%" in s
        assert "不得与全局事实矛盾" in s

    def test_blocks_differ_and_user_blocks_present(self):
        assert build_system_block(PRECISE) != build_system_block(FUZZY)
        assert "精准内容" in build_user_block(PRECISE)
        assert "模糊内容" in build_user_block(FUZZY)
        assert "精准内容" in build_continue_hint(PRECISE)
        assert "模糊内容" in build_continue_hint(FUZZY)

    def test_invalid_standard_falls_back_to_precise(self):
        assert build_system_block("???") == build_system_block(PRECISE)
        assert build_user_block(None) == build_user_block(PRECISE)


# ============================================================
# 文本提取工具
# ============================================================
class TestExtractors:

    def test_strip_fence_and_inline(self):
        text = "正文12.5m。```mermaid\nA|13m\n```结尾`99m`"
        out = strip_code_blocks(text)
        assert "13m" not in out
        assert "99m" not in out
        assert "12.5m" in out

    def test_strip_tilde_fence(self):
        # ✅ 2026-09-28：~~~ 波浪线围栏内的数字也必须剔除（此前会进入事实校验）
        text = "说明12.5m。\n~~~chart-json\n{\"value\": 356}\n~~~\n结尾 20m"
        out = strip_code_blocks(text)
        assert "356" not in out
        assert "12.5m" in out
        assert "20m" in out

    def test_number_tokens_with_units_and_alias(self):
        toks = extract_number_tokens("基坑 12.5m，工期 450 日历天，混凝土 300方")
        pairs = {(t["value"], t["unit"]) for t in toks}
        assert (12.5, "m") in pairs
        assert (450.0, "day") in pairs
        # 无单位数字不提取（避免编号/序号噪声）
        assert extract_number_tokens("参见第 12 条，编号 8") == []
        # 米/m 等价
        assert extract_number_tokens("12.5米")[0]["unit"] == "m"

    def test_model_tokens_exclude_norm_codes(self):
        toks = extract_model_tokens("塔吊 QTZ80 与 QTZ63，混凝土 C30；依据 JGJ 120 与 GB50007")
        names = {t["token"] for t in toks}
        assert "QTZ80" in names and "QTZ63" in names and "C30" in names
        # 规范编号不识别为型号（JGJ 120 有空格不匹配；GB50007 前缀黑名单）
        assert "GB50007" not in names
        fam = {t["token"]: t["family"] for t in toks}
        assert fam["QTZ80"] == "QTZ" and fam["C30"] == "C"


# ============================================================
# 精准模式报告
# ============================================================
class TestPreciseReport:

    def test_clean_precise_passes(self):
        rows = [_fact("参数", "基坑深度", "本工程基坑深度为 12.5m")]
        body = "本工程基坑深度为 12.5m，分层开挖，及时支护。"
        rep = standard_report(body, PRECISE, rows)
        assert rep["passed"] is True
        assert rep["error_count"] == 0 and rep["warning_count"] == 0
        assert rep["stats"]["checked_facts"] == 1

    def test_unit_alias_quote_passes(self):
        rows = [_fact("参数", "基坑深度", "基坑深度 12.5 米")]
        body = "基坑深度控制为 12.5m。"
        assert standard_report(body, PRECISE, rows)["passed"] is True

    def test_hedge_prefix_and_suffix_flagged(self):
        rows = [_fact("参数", "基坑深度", "基坑深度 12.5m")]
        rep1 = standard_report("基坑深度约 13m。", PRECISE, rows)
        assert any(i["type"] == "fuzzy_expression" for i in rep1["issues"])
        rep2 = standard_report("基坑深度 12.5 米左右。", PRECISE, rows)
        assert any("左右" in i["message"] for i in rep2["issues"])
        # “合约12”不得误报
        rep3 = standard_report("分包合约 12 份已归档，基坑深度 12.5m。",
                               PRECISE, rows)
        assert not any(i["type"] == "fuzzy_expression" for i in rep3["issues"])

    def test_bare_and_limit_phrases_flagged(self):
        rows = [_fact("工期", "总工期", "总工期 450 日历天")]
        rep = standard_report("设备按合同要求进场，总工期 450 日历天。", PRECISE, rows)
        msgs = " ".join(i["message"] for i in rep["issues"])
        assert "按合同要求" in msgs

        rows2 = [_fact("工期", "总工期", "总工期 450 日历天")]
        rep2 = standard_report("总工期不少于 400 日历天。", PRECISE, rows2)
        assert any("不少于" in i["message"] for i in rep2["issues"])

    def test_limit_phrase_exempt_when_fact_uses_it(self):
        """事实本身即限值「不低于 C30」时，正文照抄同措辞合法。"""
        rows = [_fact("材料", "混凝土强度等级", "混凝土强度等级不低于 C30")]
        body = "基础混凝土强度不低于 C30，连续浇筑。"
        rep = standard_report(body, PRECISE, rows)
        assert not any(i["type"] == "fuzzy_expression" for i in rep["issues"])

    def test_fact_hedge_exempt_when_fact_uses_it(self):
        rows = [_fact("参数", "基坑深度", "基坑深度约 12m")]
        rep = standard_report("基坑深度约 12m。", PRECISE, rows)
        assert not any(i["type"] == "fuzzy_expression" for i in rep["issues"])

    def test_missing_fact_value_warns(self):
        rows = [_fact("参数", "基坑深度", "基坑深度 12.5m")]
        rep = standard_report("本章介绍施工顺序与安全管理要求。", PRECISE, rows)
        miss = [i for i in rep["issues"] if i["type"] == "fact_value_missing"]
        assert len(miss) == 1
        assert "基坑深度" in miss[0]["message"] and "12.5" in miss[0]["message"]

    def test_value_mismatch_error(self):
        rows = [_fact("参数", "基坑深度", "基坑深度 12.5m")]
        rep = standard_report("基坑深度按 13m 控制开挖。", PRECISE, rows)
        errs = [i for i in rep["issues"] if i["type"] == "value_mismatch"]
        assert len(errs) == 1 and errs[0]["severity"] == "error"
        assert "12.5" in errs[0]["message"] and "13" in errs[0]["message"]
        assert rep["passed"] is False

    def test_same_value_different_unit_not_conflict(self):
        """12.5m 与 12.5天 单位不同，不构成数值矛盾。"""
        rows = [_fact("参数", "基坑深度", "基坑深度 12.5m")]
        body = "基坑深度 12.5m；前期准备 12.5 天。"
        rep = standard_report(body, PRECISE, rows)
        assert not [i for i in rep["issues"] if i["type"] == "value_mismatch"]

    def test_unrelated_number_outside_window_not_flagged(self):
        """远离事实标题语境（>±30 字符）的同单位异值不算矛盾。"""
        rows = [_fact("参数", "基坑深度", "基坑深度 12.5m")]
        body = ("排水沟宽度 13m。"
                "现场材料堆场与运输通道布置在场地东侧围挡内侧，"
                "本工程基坑深度为 12.5m，按图施工。")
        rep = standard_report(body, PRECISE, rows)
        assert not [i for i in rep["issues"] if i["type"] == "value_mismatch"]

    def test_numbers_inside_code_block_ignored(self):
        rows = [_fact("参数", "基坑深度", "基坑深度 12.5m")]
        body = ("基坑深度 12.5m。\n```mermaid\nA-->B|深度 13m|\n```\n"
                "行内 `14m` 也不参与。")
        rep = standard_report(body, PRECISE, rows)
        assert rep["passed"] is True

    def test_placeholder_counted_and_flagged_as_error(self):
        """2026-10-01：占位标记由「合规产物」升级为 error（正文必须完整）。

        旧的 test_placeholder_counted_not_flagged 断言 passed=True，
        与「不留占位标记、不留空」的需求直接冲突，已随语义一并更新。
        """
        rows = [_fact("参数", "基坑深度", "基坑深度 12.5m")]
        body = "基坑深度 12.5m；地下水位见【待补充：地勘报告】。"
        rep = standard_report(body, PRECISE, rows)
        # 既有字段保留（只增不减，不破坏老消费方）
        assert rep["stats"]["placeholders"] == 1
        assert rep["stats"]["placeholder_marks"] == 1
        marked = [i for i in rep["issues"] if i["type"] == "placeholder_mark"]
        assert len(marked) == 1
        assert marked[0]["severity"] == "error"
        assert "待补充" in marked[0]["message"]
        assert marked[0]["excerpt"]
        assert rep["passed"] is False
        assert rep["error_count"] == 1


# ============================================================
# 模糊模式报告
# ============================================================
class TestFuzzyReport:

    def test_hedges_allowed_and_no_coverage_check(self):
        rows = [_fact("参数", "基坑深度", "基坑深度 12.5m")]
        rep = standard_report(
            "基坑深度约 12m，具体按设计确定，设备按合同要求进场。", FUZZY, rows)
        assert not [i for i in rep["issues"]
                    if i["type"] in ("fuzzy_expression", "fact_value_missing")]

    def test_within_tolerance_passes(self):
        rows = [_fact("参数", "基坑深度", "基坑深度 12.5m")]
        # 13m 偏差 4%，在 ±10% 容差内
        rep = standard_report("基坑深度约 13m。", FUZZY, rows)
        assert rep["passed"] is True

    def test_beyond_tolerance_conflict(self):
        rows = [_fact("参数", "基坑深度", "基坑深度 12.5m")]
        rep = standard_report("基坑深度按 20m 组织开挖。", FUZZY, rows)
        errs = [i for i in rep["issues"] if i["type"] == "value_conflict"]
        assert len(errs) == 1
        assert "±10%" in errs[0]["message"]

    def test_model_conflict_in_fuzzy(self):
        rows = [_fact("设备", "塔吊型号", "本工程塔吊型号为 QTZ80")]
        rep = standard_report("选用满足要求的塔吊（QTZ63）进行吊装。", FUZZY, rows)
        errs = [i for i in rep["issues"] if i["type"] == "model_conflict"]
        assert len(errs) == 1 and "QTZ80" in errs[0]["message"] and "QTZ63" in errs[0]["message"]

    def test_model_consistent_passes(self):
        rows = [_fact("设备", "塔吊型号", "塔吊型号 QTZ80")]
        assert standard_report("采用 QTZ80 塔吊一台。", FUZZY, rows)["passed"] is True


class TestModelConflictPrecise:
    """精准模式同样查型号冲突（设计说明 3.1 型号必须具体且一致）。"""

    def test_model_conflict_in_precise(self):
        rows = [_fact("设备", "塔吊型号", "塔吊型号 QTZ80")]
        rep = standard_report("现场安装 QTZ63 塔吊。", PRECISE, rows)
        assert any(i["type"] == "model_conflict" for i in rep["issues"])

    def test_norm_code_never_model_conflict(self):
        rows = [_fact("依据", "规范编号", "执行 JGJ 120 及 GB50007 相关规定")]
        body = "依据《建筑基坑支护技术规程》JGJ 120 与 GB50007 执行。"
        rep = standard_report(body, PRECISE, rows)
        assert not [i for i in rep["issues"] if i["type"] == "model_conflict"]


# ============================================================
# 降级与健壮性
# ============================================================
class TestRobustness:

    def test_none_and_empty_inputs(self):
        rep = standard_report(None, PRECISE, None)
        assert rep["passed"] is True and rep["issues"] == []
        rep2 = standard_report("", "非法值", [])
        assert rep2["standard"] == PRECISE

    def test_dict_rows_tolerated(self):
        rows = [{"title": "基坑深度", "content": "基坑深度 12.5m"}]
        rep = standard_report("基坑深度 12.5m。", PRECISE, rows)
        assert rep["stats"]["checked_facts"] == 1
