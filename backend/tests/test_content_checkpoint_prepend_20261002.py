"""护栏：审核检查点前置到正文生成（2026-10-02 · 第二十三轮）。

覆盖五层：
A. 判据同源 —— 章节必含要求锚定的 rule_id 必须真实存在于 audit_rules 注册表，
   且九大章节 key 与 scheme_classification.NINE_CHAPTERS 同一词表；
B. 提示词注入 —— 契约登记、开启/关闭态渲染、可选区块不刷 WARNING；
C. 生成后自检 —— 占位符/缺要素/裸编号/危大法规判定与误报边界、fail-soft；
D. 配置默认值 —— prepend 默认开（本轮核心交付）、自检默认开
   （2026-10-02 第二十六轮校准：需求目标一「生成即完整」硬性要求）；
E. 装配接线（源码静态锁）—— sse_handlers 必须「只在非空时传入」两个检查点
   变量，杜绝「首轮有、续写无」与「传空串留白行」两类分叉回归。

⚠️ 判据指向：本文件对 sse_handlers 的断言走**源码扫描**（生成链路是巨型
async 生成器，无法在不可mock的体量下做行为级单测），与 AGENTS.md
「域门禁 AST 扫描」同款手法；行为级正确性由 B/C 层的纯函数用例锁定。
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest

from app.config import Settings
from app.services.audit_rules import RULE_MAP
from app.services.content_checkpoint import (
    CHAPTER_CHECKPOINT_REQUIREMENTS,
    build_chapter_checkpoint_block,
    build_content_system_checkpoint_block,
    checkpoint_selfcheck,
    is_hazardous_scheme,
    validate_rule_anchoring,
)
from app.services.scheme_classification import NINE_CHAPTERS

# 触发正文模板注册（_reg 在 import 期执行）
import app.services.ai.prompts.content  # noqa: F401
from app.services.ai.prompts._registry import (
    PROMPT_VARIABLE_CONTRACTS, get_prompt, render,
)


# ---------------------------------------------------------------------------
# A. 判据同源
# ---------------------------------------------------------------------------

class TestRuleAnchoring:
    def test_all_anchored_rules_exist(self):
        """每条必含要求引用的 rule_id 都在审核注册表（悬空即判据分叉前兆）。"""
        assert validate_rule_anchoring() == []

    def test_chapter_keys_parity_with_nine_chapters(self):
        """章节 key 与 NINE_CHAPTERS 同一词表，且九章全覆盖（零遗漏）。"""
        assert set(CHAPTER_CHECKPOINT_REQUIREMENTS) == {
            ch["key"] for ch in NINE_CHAPTERS}

    def test_titles_parity(self):
        for ch in NINE_CHAPTERS:
            req = CHAPTER_CHECKPOINT_REQUIREMENTS[ch["key"]]
            assert req["title"] == ch["title"], ch["key"]

    def test_production_evidence_anchored_topics(self):
        """生产库实证的高频缺陷主题词必须出现在对应章节（第22轮遗留清单）。"""
        safety = CHAPTER_CHECKPOINT_REQUIREMENTS["safety"]["requirements"]
        kws = {k for r in safety for k in r["must_include"]}
        assert {"高处作业", "临时用电", "消防", "机械"} <= kws  # SAF-02
        personnel = CHAPTER_CHECKPOINT_REQUIREMENTS["personnel"]["requirements"]
        kws = {k for r in personnel for k in r["must_include"]}
        assert {"特种作业", "持证"} <= kws  # SAF-07
        basis = CHAPTER_CHECKPOINT_REQUIREMENTS["basis"]["requirements"]
        kws = {k for r in basis for k in r["must_include"]}
        assert "37号" in kws  # STD-04

    def test_selfcheck_findings_rule_ids_in_registry(self):
        """自检 findings 的 rule_id 与预检同词表：任一脏输入都不许报悬空规则。"""
        out = checkpoint_selfcheck(
            "深度【待补充】，依据 GB 50202 执行。", chapter_key="basis",
            is_hazardous_basis=True)
        assert out
        for f in out:
            assert f["rule_id"] in RULE_MAP, f


# ---------------------------------------------------------------------------
# B. 提示词注入
# ---------------------------------------------------------------------------

class TestPromptInjection:
    _BASE_KW = dict(section_number="1", standards_text="",
                    scheme_name="N", scheme_type="T", subheading_rule="R")

    def test_contracts_declare_checkpoint_vars(self):
        assert "content_checkpoint_block" in \
            PROMPT_VARIABLE_CONTRACTS["content_generation_system"]
        assert "chapter_checkpoint_block" in \
            PROMPT_VARIABLE_CONTRACTS["content_generation_system"]
        assert "content_checkpoint_block" in \
            PROMPT_VARIABLE_CONTRACTS["content_continue_system"]

    def test_off_state_no_trace_no_residue(self):
        """关闭态（不传变量）：既无检查点文字，也无占位符残留（整行丢弃）。"""
        text = render("content_generation_system", **self._BASE_KW)
        assert "审核检查点前置要求" not in text
        assert "本章审核检查点要求" not in text
        assert "{content_checkpoint_block}" not in text
        assert "{chapter_checkpoint_block}" not in text

    def test_on_state_injected(self):
        blk = build_content_system_checkpoint_block("深基坑支护工程", "")
        ck = build_chapter_checkpoint_block("overview")
        text = render("content_generation_system",
                      content_checkpoint_block=blk,
                      chapter_checkpoint_block=ck, **self._BASE_KW)
        assert "审核检查点前置要求" in text
        assert "本章审核检查点要求" in text

    def test_continue_template_on_off(self):
        off = render("content_continue_system", scheme_name="N",
                     scheme_type="T", standards_text="")
        assert "审核检查点前置要求" not in off
        assert "{content_checkpoint_block}" not in off
        blk = build_content_system_checkpoint_block("基坑降水", "")
        on = render("content_continue_system", scheme_name="N",
                    scheme_type="T", standards_text="",
                    content_checkpoint_block=blk)
        assert "审核检查点前置要求" in on

    def test_optional_block_does_not_warn(self, caplog):
        """未传的独占行可选区块不得刷 unresolved WARNING（开关型注入点）。"""
        with caplog.at_level(logging.WARNING):
            render("content_generation_system", **self._BASE_KW)
        joined = " ".join(r.getMessage() for r in caplog.records)
        assert "unresolved" not in joined
        assert "checkpoint_block" not in joined

    def test_genuine_missing_var_still_warns(self, caplog):
        """豁免不能过头：真实漏传（scheme_name）仍必须报。"""
        kw = dict(self._BASE_KW)
        kw.pop("scheme_name")
        with caplog.at_level(logging.WARNING):
            render("content_generation_system", **kw)
        assert any("scheme_name" in r.getMessage() for r in caplog.records)

    def test_hazard_block_only_for_hazardous(self):
        up = build_content_system_checkpoint_block("落地式钢管脚手架工程", "脚手架")
        assert "危大工程要求" in up
        plain = build_content_system_checkpoint_block("办公楼室内装修工程", "装修")
        assert "危大工程要求" not in plain
        # 通用约束恒注入（永不为空）
        assert "法定内容齐全" in plain and "法定内容齐全" in up
        # 显式覆盖判定
        assert "危大工程要求" in build_content_system_checkpoint_block(
            "随便什么", "", is_hazardous=True)

    def test_chapter_block_unknown_key_empty(self):
        assert build_chapter_checkpoint_block("不存在的章") == ""
        assert build_chapter_checkpoint_block("") == ""
        blk = build_chapter_checkpoint_block("safety")
        assert "CMP-05" in blk and "高处作业" in blk

    def test_hazard_only_requirement_gated(self):
        """非危大方案不被要求引 37 号令；危大方案必须被要求。"""
        plain = build_chapter_checkpoint_block("basis", is_hazardous=False)
        assert "危大工程法定依据" not in plain
        assert "四层依据齐备" in plain  # 其余要求不受影响
        haz = build_chapter_checkpoint_block("basis", is_hazardous=True)
        assert "危大工程法定依据" in haz and "37号" in haz


# ---------------------------------------------------------------------------
# C. 生成后自检
# ---------------------------------------------------------------------------

class TestSelfcheck:
    @pytest.mark.parametrize("bad", [
        "开挖深度【待补充】m。",
        "参建单位：[待补充：监理单位]。",
        "警戒荷载为×× kPa。",
        "方案状态：【待定】。",
    ])
    def test_placeholder_detected(self, bad):
        out = checkpoint_selfcheck(bad, chapter_key="technique")
        con = [f for f in out if f["rule_id"] == "CON-04"]
        assert con and con[0]["severity"] == "error" and con[0]["fixable"]

    def test_multiplication_sign_not_flagged(self):
        """乘号 × 单个出现不是占位符（误伤防线）。"""
        out = checkpoint_selfcheck("截面 2×3 mm 角钢，间距 300mm，工艺参数见计算书，检查要求明确。",
                                   chapter_key="technique")
        assert not [f for f in out if f["rule_id"] == "CON-04"]

    def test_missing_topic_detected(self):
        out = checkpoint_selfcheck("本章概述了安全管理总体思路。",
                                   chapter_key="safety")
        rids = {f["rule_id"] for f in out}
        # 专项安全技术措施（CMP-05/SAF-02 共用首 id CMP-05）与监测（SAF-06）均缺
        assert {"CMP-05", "SAF-06"} <= rids
        assert all(f["severity"] == "medium" for f in out)

    def test_topic_alt_include_accepted(self):
        text = ("落实高处作业与临时用电防护，消防与机械设备管理到位；"
                "对支撑体系实施监控量测并设定阈值。")
        out = checkpoint_selfcheck(text, chapter_key="safety")
        assert not [f for f in out if f["rule_id"] in ("CMP-05", "SAF-02", "SAF-06")]

    def test_safety_topic_requires_all_items(self):
        """must_include 语义为「全部」：只写一项专项措施仍报缺（防蒙混）。"""
        partial = checkpoint_selfcheck(
            "落实高处作业防护。监测预警值另定。", chapter_key="safety")
        assert any(f["rule_id"] == "CMP-05" for f in partial)

    def test_std04_only_when_hazardous_basis(self):
        text = "本节列出以下依据：GB 50202-2018、施工图纸。"
        assert [f for f in checkpoint_selfcheck(text, chapter_key="basis",
                                                is_hazardous_basis=True)
                if f["rule_id"] == "STD-04"]
        assert not [f for f in checkpoint_selfcheck(text, chapter_key="basis",
                                                    is_hazardous_basis=False)
                    if f["rule_id"] == "STD-04"]
        # 已引 31 号文亦视为满足
        text2 = text + "并执行建办质〔2018〕31号文。"
        assert not [f for f in checkpoint_selfcheck(text2, chapter_key="basis",
                                                    is_hazardous_basis=True)
                    if f["rule_id"] == "STD-04"]

    def test_std03_bare_code_vs_year(self):
        bare = checkpoint_selfcheck("参照 GB 50202 及 JGJ 130 执行相关验收。",
                                    chapter_key="basis")
        std3 = [f for f in bare if f["rule_id"] == "STD-03"]
        assert std3 and std3[0]["severity"] == "warning"
        full = checkpoint_selfcheck("参照 GB 50202-2018 及 JGJ 130-2011 执行。",
                                    chapter_key="basis")
        assert not [f for f in full if f["rule_id"] == "STD-03"]

    @pytest.mark.parametrize("dirty", ["", None, "   ", "\n\n"])
    def test_failsoft_dirty_input(self, dirty):
        assert checkpoint_selfcheck(dirty, chapter_key="basis") == []

    def test_failsoft_unknown_chapter(self):
        out = checkpoint_selfcheck("正常正文内容。", chapter_key="幽灵章节")
        assert isinstance(out, list)

    def test_is_hazardous_scheme_deterministic(self):
        assert is_hazardous_scheme("塔式起重机安装拆卸", "")
        assert not is_hazardous_scheme("", "")
        assert not is_hazardous_scheme("办公楼室内装修", "")


# ---------------------------------------------------------------------------
# D. 配置默认值
# ---------------------------------------------------------------------------

class TestConfigDefaults:
    def test_defaults(self):
        s = Settings()
        assert s.content_checkpoint_prepend is True   # 本轮核心交付：默认生效
        # 2026-10-02 第二十六轮：自检与确定性自动修复由观察期默认关
        # 改为默认开（需求目标一「生成即完整、不残留占位标记」）
        assert s.content_selfcheck is True
        assert s.content_selfcheck_autofix is True


# ---------------------------------------------------------------------------
# E. 装配接线（源码静态锁，防「传空串留白行 / 首轮有续写无」回归）
# ---------------------------------------------------------------------------

_SSE_SRC = (Path(__file__).resolve().parents[1]
            / "app" / "routers" / "sse_handlers.py").read_text(encoding="utf-8")


class TestWiringLocked:
    def test_prepend_and_selfcheck_gates_read_config(self):
        # 2026-10-02 第二十六轮：自检/自动修复兜底默认由 False 改为 True
        # （与 config.py 默认一致，需求目标一「生成即完整」）
        assert 'getattr(settings, "content_checkpoint_prepend", True)' in _SSE_SRC
        assert 'getattr(settings, "content_selfcheck", True)' in _SSE_SRC
        assert 'getattr(settings, "content_selfcheck_autofix", True)' in _SSE_SRC

    def test_checkpoint_vars_passed_only_when_nonempty(self):
        """禁止回归为无条件传值（传空串会留白行；关闭态须逐字回退）。"""
        assert "content_checkpoint_block=_checkpoint_block," not in _SSE_SRC
        assert "chapter_checkpoint_block=build_chapter_checkpoint_block(" not in _SSE_SRC
        assert 'if _checkpoint_block:' in _SSE_SRC
        assert 'if _chapter_ck:' in _SSE_SRC
        # 续写轮同源条件展开（避免首轮有、续写无）
        assert '**({"content_checkpoint_block": _checkpoint_block}' in _SSE_SRC

    def test_selfcheck_wired_into_persist_section(self):
        assert "checkpoint_selfcheck(" in _SSE_SRC
        assert 'report["checkpoint_findings"] = _ck_findings' in _SSE_SRC
        # 自检在锁外（「锁内：最小事务」注释之前出现）
        i_sc = _SSE_SRC.index("if _selfcheck_on:")
        i_lock = _SSE_SRC.index("---------- 锁内：最小事务")
        assert i_sc < i_lock

    def test_hazard_flag_computed_once_reused(self):
        m = re.search(r"_ck_hazardous = is_hazardous_scheme\(", _SSE_SRC)
        assert m, "危大判定应整案一次、逐章复用"
        # 注入与自检共用同一结果（不得各自再算一份）
        assert "is_hazardous=_ck_hazardous" in _SSE_SRC
        assert "_ck_hazardous) if _ck_prepend" in _SSE_SRC
        assert "is_hazardous_basis=_ck_hazardous" in _SSE_SRC
        assert "_selfcheck_hazardous" not in _SSE_SRC


# ---------------------------------------------------------------------------
# F. STD-03 误报收口（生产库证据驱动：正文裸写无年号在库编号被报「未收录」）
# ---------------------------------------------------------------------------

class TestStd03Closure:
    def test_bare_in_library_codes_accepted(self):
        from app.services.standards_registry import is_known_base_number
        # 生产证据：GB 55032 / GB 55034 / GB 50210 裸写（库内为带年号形态）
        assert is_known_base_number("GB 55032")
        assert is_known_base_number("GB55034")
        assert is_known_base_number("GB 50210")
        # 带年号的库内标准同样命中（基号池由全库剥年号生成）
        assert is_known_base_number("GB 55032-2022")

    def test_fabricated_and_abolished_bare_not_exempt(self):
        from app.services.standards_registry import is_known_base_number
        assert not is_known_base_number("GB 99999")      # 编造编号
        assert not is_known_base_number("GB 18581")      # 库内仅剩废止版（无现行版），不豁免
        assert not is_known_base_number("")              # 脏输入

    def test_preflight_std03_no_false_positive_on_bare_codes(self):
        """复现生产误报：正文裸写 6 个无年号编号，旧判据整批误报 STD-03。"""
        from app.services.preflight_engine import PreflightContext, check_standards
        body = ("依据 GB 55034、GB 55032、GB 50210、GB 50325、JGJ 59、JGJ 120 "
                "组织施工并控制扬尘噪声。")
        ctx = PreflightContext(scheme_name="装饰装修方案", sections=[
            {"id": "1", "title": "编制依据", "content": body,
             "word_count": 0, "parent_id": ""}])
        std03 = [f for f in check_standards(ctx) if f["rule_id"] == "STD-03"]
        assert std03 == []

    def test_preflight_std03_still_fires_on_fabricated_codes(self):
        """豁免不得架空判据：≥3 个库外编造编号仍须报出。"""
        from app.services.preflight_engine import PreflightContext, check_standards
        body = "参照 GB 99999、JGJ 88888、CECS 77777、DBJ 66666 执行。"
        ctx = PreflightContext(scheme_name="测试方案", sections=[
            {"id": "1", "title": "编制依据", "content": body,
             "word_count": 0, "parent_id": ""}])
        std03 = [f for f in check_standards(ctx) if f["rule_id"] == "STD-03"]
        assert len(std03) == 1
        assert "GB 99999" in std03[0]["evidence"]

    def test_newly_verified_abolished_entries(self):
        """2026-10-02 核实的两条废止登记（国标委/公告依据见登记说明）。"""
        from app.services.standards_registry import ABOLISHED_STANDARDS
        assert "GB 12523-2011" in ABOLISHED_STANDARDS
        assert "GB 18581-2020" in ABOLISHED_STANDARDS
        from app.services.standards_registry import find_abolished_codes
        assert "GB 12523-2011" in find_abolished_codes("依据 GB12523-2011 控制噪声")


# ---------------------------------------------------------------------------
# G. STD-04 危大门控（非危大方案不得被要求引 37 号令）+ 单一事实源
# ---------------------------------------------------------------------------

class TestStd04HazardGate:
    def test_single_source_of_truth(self):
        """生成侧 is_hazardous_scheme 与预检侧必须同一判据（避免分叉）。"""
        from app.services.content_checkpoint import is_hazardous_scheme
        from app.services.scheme_classification import is_hazardous_by_keywords
        for name in ("落地式钢管脚手架", "基坑支护", "人工挖孔桩", "塔式起重机"):
            assert is_hazardous_scheme(name) is is_hazardous_by_keywords(name)
        # 非危大方案两侧均降为 False
        for name in ("临时用电", "装饰装修", "项目管理大纲"):
            assert is_hazardous_scheme(name) is False
            assert is_hazardous_by_keywords(name) is False

    def test_preflight_std04_fires_on_hazardous_missing_reg(self):
        from app.services.preflight_engine import PreflightContext, check_standards
        body = "依据 GB 55034-2022、JGJ 130-2011 组织脚手架搭设。"
        ctx = PreflightContext(scheme_name="落地式钢管脚手架专项施工方案",
                              scheme_type="脚手架", sections=[
            {"id": "1", "title": "编制依据", "content": body,
             "word_count": 0, "parent_id": ""}])
        assert any(f["rule_id"] == "STD-04" for f in check_standards(ctx))

    def test_preflight_std04_not_fired_on_non_hazardous(self):
        """非危大方案（纯临电）未引 37 号令不应报 STD-04（误报收口）。"""
        from app.services.preflight_engine import PreflightContext, check_standards
        body = "依据 JGJ/T 46-2024、GB 55034-2022 布置现场临时用电。"
        ctx = PreflightContext(scheme_name="现场临时用电专项方案",
                              scheme_type="临时用电", sections=[
            {"id": "1", "title": "编制依据", "content": body,
             "word_count": 0, "parent_id": ""}])
        assert not any(f["rule_id"] == "STD-04" for f in check_standards(ctx))


# ---------------------------------------------------------------------------
# H. 一致性扫描「可选材料并列 / 不同部位选材」消歧（CON-SCAN design 误报）
#    生产实证：「18mm 多层板或 12mm 竹胶板」（同一部位二选一）被误判矛盾。
#    仅能锁定「消歧文案已下发给模型」，AI 行为本身不可确定性断言。
# ---------------------------------------------------------------------------

class TestConsistencyScanDisambiguation:
    @pytest.mark.parametrize("key", [
        "consistency_scan_system", "consistency_scan_batch_system"])
    def test_both_scan_prompts_have_disambiguation(self, key):
        text = get_prompt(key)
        assert "不得判为冲突" in text
        assert "可选材料" in text or "可选" in text
        assert "同一部位" in text

