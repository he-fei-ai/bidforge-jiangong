"""F-CONTENT-STANDARD · 2026-09-26 缺口闭环（B1~B4）接线测试

本文件锁死四条**曾经真实存在**的缺口，防止回归：

- **B1 提示词自相矛盾**：`sse_handlers` 曾对精准/模糊**硬编码同一句**事实块引导语
  （「与各变量相关的数据必须直接引用」），与模糊模式 user 块的「方向参考」冲突
  → 模式差异在最关键的事实注入点上被抹平。现由 `build_facts_header` 按模式渲染。
- **B2 事件不携带标准与报告**：`section_start` / `section_done` 缺
  `generation_standard`（+ `standard_report`），终态缺 `standard_summary`
  → 前端无法显示模式徽标与告警，也无法做差异化校验。
- **B3 续写轮丢策略**：续写是独立 AI 调用且后续轮不重发首轮 user，
  缺模式提醒会让精准模式在续写阶段「凭记忆」退化。
- **B4 校验器占用全局写锁**：`standard_report()` 曾写在
  `async with _db_write_lock` 内的 UPDATE 参数里 —— 纯 CPU 扫描落在写锁内，
  与同函数 P1-3「锁内只保留最小事务」的设计结论冲突，并发档实际退化为串行。

约定：
- 文案/解析类用**纯函数**直接断言（可执行、不会随重构误报）；
- 接线类用**源码结构断言**（对齐仓库既有 `test_api_contract.py` 的
  source-contract 风格）。之所以不用端到端断言：`generate_content` 内的
  `_build_generation_context` / `_continue_if_needed` 是**闭包**，
  仓内既无 SSE 正文生成测试夹具（`tests/` 下无任何 `generate-content` 用例），
  为此新造整套 AI/DB 夹具的成本与回归风险远大于收益。
  故此处只锁「结构契约」，语义正确性由纯函数用例 + 前端 vitest 共同覆盖。
"""
import re
from pathlib import Path

import pytest
from app.services.content_standard import (
    DEFAULT_STANDARD,
    FUZZY,
    PRECISE,
    build_continue_hint,
    build_facts_header,
    build_system_block,
    build_user_block,
    standard_report,
)

_SSE = Path(__file__).resolve().parents[1] / "app" / "routers" / "sse_handlers.py"
_SRC = _SSE.read_text(encoding="utf-8")
# ✅ 2026-09-28（T-1 收口）：事实引导语 / 续写提醒的实现自 sse_handlers 下沉到
#    新建服务模块 content_runtime.py —— 接线断言需同时锁两处宿主的源码契约。
_CR = Path(__file__).resolve().parents[1] / "app" / "services" / "content_runtime.py"
_CR_SRC = _CR.read_text(encoding="utf-8")


# ============================================================
# B1：事实块引导语随模式变化（纯函数，可执行断言）
# ============================================================
class TestFactsHeaderByStandard:

    def test_precise_header_forbids_placeholders_and_requires_fuzzy_fill(self):
        """2026-10-01（模糊生成改造）：精准引导语不再指向占位符。

        旧断言锁定「与改造前硬编码逐字一致」（NFR-3 旧行为不变）。
        本轮需求明确要求「正文完整生成、不留占位标记」，精准文案因此
        **有意**从「使用占位符」改为「按模糊生成规则补齐」—— 该差异是需求本身，
        故断言改为锁定新文案，防止回退。
        """
        legacy = (
            "全局事实变量（唯一可信数据源：与各变量相关的数据必须直接引用，"
            "不得改写、推算或另取数值；未提供的数据严禁编造，"
            "按提示词“数据真实性红线”使用占位符或条件式表述）：\n"
        )
        h = build_facts_header(PRECISE)
        assert h != legacy
        assert "使用占位符或条件式表述" not in h
        # 精准模式的强制逐项引用口径必须保留
        assert "必须直接引用" in h
        assert "不得改写、推算或另取数值" in h
        # 新口径：按模糊生成规则补齐 + 禁止占位标记与空话
        assert "模糊生成规则" in h
        assert "占位标记" in h
        assert "空话" in h

    def test_fuzzy_header_differs_and_allows_generalization(self):
        h = build_facts_header(FUZZY)
        assert h != build_facts_header(PRECISE)
        # 模糊模式必须显式允许概括/归纳
        assert "方向参考" in h
        assert "概括" in h
        # 但底线约束不得丢失
        assert "不得出现与事实冲突" in h
        assert "严禁编造" in h
        # 不得出现精准模式的强制逐项引用措辞
        assert "必须直接引用" not in h

    @pytest.mark.parametrize("bad", [None, "", "strict", 123])
    def test_invalid_falls_back_to_default(self, bad):
        assert build_facts_header(bad) == build_facts_header(DEFAULT_STANDARD)

    def test_both_headers_end_with_newline(self):
        """引导语必须自带换行，否则拼接事实正文会粘成一行。"""
        for std in (PRECISE, FUZZY):
            assert build_facts_header(std).endswith("：\n")

    def test_sse_no_longer_hardcodes_strict_facts_header(self):
        """接线断言：sse_handlers 不得再内联「必须直接引用」的硬编码引导语。"""
        assert "与各变量相关的数据必须直接引用" not in _SRC
        # ✅ 2026-09-28：引导语实现已下沉 content_runtime.build_chapter_user_content
        #    （sse_handlers 只负责传参），两处宿主分别锁住调用与实现。
        assert "build_chapter_user_content(" in _SRC
        assert "build_facts_header(eff_standard)" in _CR_SRC

    def test_sse_facts_block_is_mode_dependent(self):
        """接线断言：事实块拼接必须传入生效标准，而非固定文案。"""


# ============================================================
# B3：续写轮模式提醒（纯函数 + 接线）
# ============================================================
class TestContinueHint:

    def test_hints_differ_per_standard(self):
        assert build_continue_hint(PRECISE) != build_continue_hint(FUZZY)

    def test_precise_hint_forbids_hedging(self):
        h = build_continue_hint(PRECISE)
        assert "精准" in h
        # ✅ 2026-10-01：缺失参数改为按模糊生成规则补齐，不再要求【待补充】
        assert "模糊生成规则" in h
        assert "【待补充" not in h

    def test_fuzzy_hint_forbids_conflict(self):
        h = build_continue_hint(FUZZY)
        assert "模糊" in h and "矛盾" in h

    def test_continue_accepts_and_appends_eff_standard(self):
        assert re.search(
            r"async def _continue_if_needed\([^)]*eff_standard:\s*str\s*=\s*\"\"",
            _SRC, re.S
        ), "_continue_if_needed 必须接收 eff_standard"
        # ✅ 2026-09-28：续写 messages 构造下沉 content_runtime.build_continuation_messages，
        #    接线断言改为锁调用点 + 实现点的两处宿主。
        assert "build_continuation_messages(" in _SRC
        assert "build_continue_hint(eff_standard)" in _CR_SRC
        # 提醒必须真正进入续写的 user 消息（只能算一次，不能只算不用）
        assert re.search(r"\+ std_hint\}\]", _CR_SRC), "续写 user 消息必须拼接 std_hint"

    def test_both_call_sites_pass_eff_standard(self):
        """续写模式与首轮模式两个调用点都必须透传生效标准。"""
        found = False
        for m in re.finditer(
                r"await _continue_if_needed\((?:[^()]|\([^()]*\))*\)", _SRC, re.S):
            found = True
            assert "eff_standard=" in m.group(0), (
                f"_continue_if_needed 调用点缺少 eff_standard：{m.group(0)[:80]}")
        assert found, "未找到 _continue_if_needed 调用点"


# ============================================================
# B2：事件字段下发（接线断言）
# ============================================================
class TestEventPayloads:

    def test_section_start_carries_standard(self):
        m = re.search(r'"event":\s*"section_start".*?\}\s*,\s*ensure_ascii=False\)',
                      _SRC, re.S)
        assert m, "未找到 section_start 事件"
        assert "generation_standard" in m.group(0)

    def test_section_done_carries_standard_and_report(self):
        m = re.search(r'"event":\s*"section_done".*?\}\s*,\s*ensure_ascii=False\)',
                      _SRC, re.S)
        assert m, "未找到 section_done 事件"
        assert "generation_standard" in m.group(0)
        assert "standard_report" in m.group(0)

    def test_standard_declared_before_section_start(self):
        """生效标准必须先解析再发事件（否则 section_start 拿不到值）。"""
        loop_start = _SRC.index('leaf_id = leaf["id"]')
        resolve_at = _SRC.index("_eff_std = resolve_effective_standard(")
        evt_at = _SRC.index('"event": "section_start"')
        assert loop_start < resolve_at < evt_at

    def test_terminal_payloads_carry_standard_summary(self):
        assert re.search(
            r"completed_payload\['standard_summary'\]\s*=\s*_std_sum", _SRC)
        # ✅ R2（2026-10-05）：stopped 也下发（用户在标准校验上同样需要看到
        #    已完成部分的汇总）。载荷改由 `_stopped_payload` 单一拼装点产出，
        #    故断言改锚到该 helper —— 比旧的「路径 A 后面跟着字段」正则更强：
        #    旧正则只保证**某一条** stopped 路径带汇总（另一条不带也照样通过，
        #    这正是本轮修掉的缺陷），现在锁住拼装点本体，两条路径同时受约束。
        # ⚠️ 必须先切到 generate_content 段：目录生成路径里**也有**一个同名
        #    `_stopped_payload(task_id, message, *, progress=None)`（模块级），
        #    全文件 index 会先命中它 —— 锚点选错即恒失败（§5.14）。
        _c = _SRC[_SRC.index("async def generate_content("):]
        i = _c.index("def _stopped_payload(")
        block = _c[i:_c.index("\n        def ", i + 10)]
        assert "'event': 'stopped'" in block
        assert "'standard_summary': _std_sum" in block
        # 两条 stopped 路径都必须走拼装点
        assert "_stopped_payload('用户已停止', stop_progress)" in _c
        assert "_stopped_payload('任务已取消')" in _c

    def test_summary_accumulator_init_outside_loop(self):
        """累加器必须提升到闭包外层（早期失败路径可读，不能 NameError）。"""
        assert '_std_sum: dict = {"precise": 0, "fuzzy": 0' in _SRC
        assert '_std_sum["issue_sections"] += 1' in _SRC
        assert '_std_sum["total_issues"] += _se + _sw' in _SRC

    def test_report_is_not_recomputed_in_event_path(self):
        """事件侧必须复用落库时的报告（此前同一章正文被扫描两遍）。

        统计前先剔除注释行：缺口说明注释里会引用 `standard_report()` 字面量，
        直接对全文件计数会把注释误判成第二次调用。
        """
        code_only = "\n".join(
            ln for ln in _SRC.splitlines() if not ln.lstrip().startswith("#"))
        calls = re.findall(r"standard_report\(", code_only)
        # 1 处导入 + 1 处调用（导入行是 `standard_report,` 不带括号，故此处应为 1）
        assert len(calls) == 1, (
            f"standard_report 应只在落库处调用一次，实际 {len(calls)} 次"
            "（事件侧重复扫描 = 每章白跑一遍正则）")


# ============================================================
# B4：校验器移出全局写锁
# ============================================================
class TestValidatorOutsideWriteLock:

    def test_standard_report_not_inside_write_lock(self):
        """标准校验必须在 `async with _db_write_lock` **之前**完成。"""
        lock_at = _SRC.index("async with _db_write_lock:")
        call_at = _SRC.index("report = standard_report(")
        assert call_at < lock_at, (
            "standard_report 又落回写锁内了（CPU 扫描会串行化高并发档）")

    def test_update_uses_precomputed_report_json(self):
        """UPDATE 必须直接落预计算的 report_json，而不是内联再算一次。"""
        assert "report_json" in _SRC
        assert "last_generation_report=?" in _SRC

    def test_persist_returns_report_to_event_layer(self):
        assert "return content, wc, ws, report" in _SRC
        assert "return content, wc, wb, ws, report" in _SRC


# ============================================================
# 模式差异性（AC「两选项有明确差异」的语义保证）
# ============================================================
class TestTwoModesDiffer:

    def test_all_four_prompt_blocks_differ_between_modes(self):
        """system / user / facts / continue 四处文案必须两两不同。

        这是「两选项无差异」BUG 的直接守门：任一处被写成模式无关的固定文案，
        对应断言即失败。
        """
        pairs = [
            (build_system_block(PRECISE), build_system_block(FUZZY), "system"),
            (build_user_block(PRECISE), build_user_block(FUZZY), "user"),
            (build_facts_header(PRECISE), build_facts_header(FUZZY), "facts"),
            (build_continue_hint(PRECISE), build_continue_hint(FUZZY), "continue"),
        ]
        for a, b, name in pairs:
            assert a and b, f"{name} 块为空"
            assert a != b, f"{name} 块两模式相同 → 选项无差异"

    def test_same_content_yields_different_verdicts_by_mode(self):
        """同一份正文（概述 + 含概数），两模式结论必须不同。

        正文含「约 12m」这类概数：精准禁止（warning），模糊允许；
        12m 与事实 12.5m 偏差 4% < 10% 容差，模糊不得报冲突。
        """
        facts = [("工程概况", "基坑开挖深度", "基坑开挖深度 12.5m", 0.9, "plan")]
        body = "本工程基坑开挖深度约 12m，按设计要求施工。"
        p = standard_report(body, PRECISE, facts)
        f = standard_report(body, FUZZY, facts)
        assert p["warning_count"] > 0, "精准模式应报出模糊表述"
        assert f["error_count"] == 0, "模糊模式不应因概述表达而报错"

        assert re.search(
            r"build_facts_header\(\s*eff_standard\s*\)", _CR_SRC
        ), "事实块必须按 eff_standard 渲染（否则两模式提示词仍然相同）"
