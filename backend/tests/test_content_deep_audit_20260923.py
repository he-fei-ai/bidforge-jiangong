# -*- coding: utf-8 -*-
"""正文生成模块深度审计回归测试（2026-09-23）

覆盖本轮审计修复的缺陷（均有代码依据）：
- B1  早期失败二次崩溃：event_stream 终态收尾状态（total/done_ids/_failed_*/
      gen_runner/_content_ckpt_payload）必须在 try 之前预初始化，
      否则早期异常时 except/finally 再抛 NameError → 任务永久僵尸 running
- B2  continue 模式空正文只发 section_error 不登记失败 → stats.failed 与
      终态 failed_count 口径矛盾
- B4  word_budget_override 字符串/非法值入参炸整批任务 → 归一化纯函数
- B19 _section_partial 用整章 started_at 配阶段 expect → continue/persist
      阶段进度开局封顶（SSE 进度不准）
- 编号空缺：outline_json 无合法点分编号时空串渲染进系统提示词 → 兜底文案
- 未闭合围栏：AI 截断输出的半个 ``` 原样落库 → _persist_section 落库前补齐
- B31 手动保存正文（update_section）不同步 chart_predictions →
      幽灵图/僵尸登记（register_inline_charts 全量同步 + 同事务提交）

测试策略：
- 顺序/结构不变量用源码级断言（inspect，与 TestTerminalOrdering 同风格）；
- 纯函数行为直接调用；
- 手动保存图表同步用内存 sqlite（conftest.db_conn）直调路由函数。
"""
import inspect
import json
import re

import pytest
from app.models import SectionUpdate
from app.routers import sse_handlers
from app.routers.sections import update_section
from app.routers.sse_handlers import (
    _STAGE_FILL_MAX,
    _STAGE_MODEL,
    _section_number_for_prompt,
    _section_partial,
)
from app.services.content_utils import (
    normalize_word_budget_override,
    text_word_count,
)

# generate_content 全量源码（含嵌套的 event_stream / gen_one / _persist_section）
_GSRC = inspect.getsource(sse_handlers.generate_content)
# event_stream 起始之后的切片（本文件全部源码断言的作用域）
_ES = _GSRC[_GSRC.index("async def event_stream()"):]


# ============================================================
# B1：早期失败收尾状态预初始化（源码级结构断言）
# ============================================================
class TestEarlyFailureGuard:
    # event_stream 外层 try（8 空格缩进）：收尾状态必须在它之前就绪
    _OUTER_TRY = re.search(r"\n        try:\n", _ES)

    def test_counters_preinitialized_before_try(self):
        """total/done_ids/_failed_ids/_failed_reasons/gen_runner 在外层 try 前预初始化"""
        assert self._OUTER_TRY, "event_stream 外层 try 结构变化，需同步调整断言"
        t = self._OUTER_TRY.start()
        for marker in ("total = 0", "done_ids: set = set()",
                       "_failed_ids: set = set()",
                       "_failed_reasons: dict",
                       "gen_runner = None"):
            assert marker in _ES, f"缺少预初始化标记：{marker}"
            assert _ES.index(marker) < t, f"{marker} 必须在外层 try 之前"

    def test_ckpt_payload_defined_once_before_try(self):
        """_content_ckpt_payload 唯一定义点且上提到 try 前（早期 except 可用）"""
        assert _ES.count("def _content_ckpt_payload") == 1, \
            "拼装点必须唯一（防回退到手写字段拼装的分叉实现）"
        assert _ES.index("def _content_ckpt_payload") < self._OUTER_TRY.start()

    def test_finally_cancels_gen_runner_with_none_guard(self):
        """finally 兜底 cancel 前判 None（早期异常时协程尚未创建）"""
        assert "if gen_runner is not None:" in _ES

    def test_not_leaves_branch_saves_checkpoint(self):
        """空任务（没有待生成章节）分支也落 checkpoint，断线重挂可读到终态"""
        m = re.search(
            r"if not leaves:.*?_save_content_checkpoint\(\s*task_id,\s*"
            r"_content_ckpt_payload\(\"completed\",\s*\"没有待生成的章节\"\)\s*\)",
            _ES, re.S)
        assert m, "not-leaves 分支缺少 _save_content_checkpoint 收尾"


# ============================================================
# B2：continue 空正文必须登记失败（源码级断言）
# ============================================================
class TestRequestAndTerminalContract:
    """正文入口脏 JSON 与终态 SSE 的稳定契约。"""

    def test_non_object_json_body_is_treated_as_empty_options(self):
        src = inspect.getsource(sse_handlers.generate_content)
        assert "if not isinstance(body, dict):" in src
        assert "body = {}" in src

    def test_boolean_options_use_coercion_not_python_truthiness(self):
        src = inspect.getsource(sse_handlers.generate_content)
        for field in ("force_rewrite", "auto_shrink_over",
                      "auto_consistency_repair", "force_full_repair"):
            assert re.search(
                rf'_coerce_bool\(\s*body\.get\("{field}"\)', src
            ), f"{field} 必须走字符串/数字安全的布尔归一"

    def test_completed_event_contains_final_progress_and_counts(self):
        src = inspect.getsource(sse_handlers.generate_content)
        block = src[src.index("completed_payload = {"):]
        block = block[:block.index("yield f\"data:")]
        for field in ("'progress': 1.0", "'done': total", "'total': total"):
            assert field in block


# ============================================================================
# B2：continue 空正文必须登记失败（源码级结构断言）
# ============================================================================
class TestContinueEmptyCountsFailure:
    def test_continue_empty_marks_failed(self):
        m = re.search(
            r"该章节暂无正文，无法续写.*?await _mark_section_failed\("
            r"leaf_id, title, _reason\)\s*\n\s*return",
            _ES, re.S)
        assert m, "continue 空正文分支未走 _mark_section_failed 失败计数"


# ============================================================
# 未闭合围栏：_persist_section 落库前自动补齐（源码级断言）
# ============================================================
class TestUnclosedFenceGuardInPersist:
    def test_persist_fixes_fences_before_write(self):
        assert "auto_fix_unclosed_fences(content)" in _ES, \
            "落库前缺少未闭合围栏修复调用"
        # 修复失败必须降级（不阻断落库）
        m = re.search(
            r"auto_fix_unclosed_fences\(content\).*?except Exception",
            _ES, re.S)
        assert m, "未闭合围栏修复缺少降级保护"


# ============================================================
# B19：_section_partial 用阶段起点（stage_started_at）填充
# ============================================================
class TestSectionPartialStageFill:
    NOW = 1000.0

    def _expect_fill(self, stage: str, elapsed: float) -> float:
        m = _STAGE_MODEL[stage]
        frac = min(elapsed / m["expect"], 1.0) * _STAGE_FILL_MAX
        return m["base"] + m["span"] * frac

    def test_uses_stage_started_at(self):
        """persist 阶段：填充按阶段起点耗时，而不是整章耗时"""
        rec = {"stage": "persist", "started_at": 100.0,
               "stage_started_at": self.NOW - 6.0}
        assert _section_partial(rec, self.NOW) == pytest.approx(
            self._expect_fill("persist", 6.0))

    def test_falls_back_to_started_at(self):
        """旧调用方只传 started_at → 回退兼容（既有单测不破坏）"""
        rec = {"stage": "persist", "started_at": self.NOW - 6.0}
        assert _section_partial(rec, self.NOW) == pytest.approx(
            self._expect_fill("persist", 6.0))

    def test_whole_chapter_elapsed_no_longer_caps_stage(self):
        """整章已跑很久但阶段刚起步 → 进度停在阶段基准，不得开局封顶"""
        rec = {"stage": "persist", "started_at": 0.0,
               "stage_started_at": self.NOW}
        assert _section_partial(rec, self.NOW) == pytest.approx(
            _STAGE_MODEL["persist"]["base"])

    def test_bad_override_falls_back_to_factory(self):
        """校准值脏（非数值）→ 回退出厂 expect，不抛异常"""
        rec = {"stage": "draft", "started_at": self.NOW - 45.0}
        p = _section_partial(rec, self.NOW, {"draft": "坏值"})
        assert p == pytest.approx(self._expect_fill("draft", 45.0))


# ============================================================
# B4：word_budget_override 归一化
# ============================================================
class TestWordBudgetNormalize:
    @pytest.mark.parametrize("raw,expected", [
        (None, None),                       # 缺省 → 不覆盖（旧行为）
        ("", None), ("   ", None),          # 空串
        (2000, 2000),                       # int 原样
        (1500.0, 1500),                     # float 取整
        ("2000", 2000), ("2000.9", 2000),   # 数字字符串（旧实现炸点）
        (0, None), (-5, None), ("-3", None),  # 非正数无效
        (True, None), (False, None),        # bool 不是字数
        ("abc", None), ({}, None), ([], None),  # 非法类型不抛异常
    ])
    def test_normalize(self, raw, expected):
        assert normalize_word_budget_override(raw) == expected


# ============================================================
# 编号空缺：提示词「当前章节编号」兜底
# ============================================================
class TestSectionNumberForPrompt:
    def test_valid_dotted_number(self):
        sec = {"outline_json": json.dumps({"id": "2.1"})}
        assert _section_number_for_prompt(sec) == "2.1"

    def test_missing_number_returns_explicit_placeholder(self):
        """编号缺失 → 显式兜底文案（不得渲染空串诱导模型编造章号）"""
        for bad in ({}, {"outline_json": ""}, {"outline_json": "{}"},
                    {"outline_json": json.dumps({"id": "不是编号"})}):
            out = _section_number_for_prompt(bad)
            assert "未提供" in out and out != ""

    def test_uuid_id_not_leaked(self):
        """UUID 主键不得作为章节编号注入提示词（回归锁定）"""
        sec = {"outline_json": json.dumps(
            {"id": "550e8400-e29b-41d4-a716-446655440000"})}
        assert "550e8400" not in _section_number_for_prompt(sec)


# ============================================================
# B31：手动保存正文 → chart_predictions 全量同步
# ============================================================
VALID_FLOWCHART = "flowchart TD\n    A --> B\n    B --> C"


async def _seed_section(db, sid="s1", pid="p1", sec_id="c1"):
    await db.execute("INSERT OR IGNORE INTO projects (id, name) VALUES (?,?)",
                     (pid, "项目"))
    await db.execute(
        "INSERT OR IGNORE INTO schemes (id, project_id, name) VALUES (?,?,?)",
        (sid, pid, "方案"))
    await db.execute(
        "INSERT INTO sections (id, scheme_id, project_id, parent_id, title,"
        " level, sort_order, status, word_budget)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (sec_id, sid, pid, "", "第一章", 1, 0, "empty", 1500))
    await db.commit()


async def _chart_rows(db, sec_id="c1"):
    cur = await db.execute(
        "SELECT chart_type FROM chart_predictions WHERE section_id=?", (sec_id,))
    return [r["chart_type"] for r in await cur.fetchall()]


async def _section_row(db, sec_id="c1"):
    cur = await db.execute(
        "SELECT content, word_count, status FROM sections WHERE id=?", (sec_id,))
    return await cur.fetchone()


class TestManualSaveChartSync:
    async def test_save_with_chart_registers(self, db_conn):
        """手动保存含合法 mermaid 的正文 → 同步登记 1 个图表，正文原样入库"""
        await _seed_section(db_conn)
        content = f"总体流程如下：\n```mermaid\n{VALID_FLOWCHART}\n```\n后续说明。"
        r = await update_section("s1", "c1", SectionUpdate(content=content),
                                 db=db_conn)
        assert r["ok"] is True
        assert await _chart_rows(db_conn) == ["flowchart"]
        row = await _section_row(db_conn)
        assert "```mermaid" in row["content"]
        # 字数口径与生成链路统一：剔除围栏代码
        assert row["word_count"] == text_word_count(content)
        assert row["status"] == "generated"

    async def test_save_without_chart_clears_zombies(self, db_conn):
        """再次保存删掉图表的正文 → 历史登记同步清空（不留幽灵图）"""
        await _seed_section(db_conn)
        content = f"流程：\n```mermaid\n{VALID_FLOWCHART}\n```"
        await update_section("s1", "c1", SectionUpdate(content=content),
                             db=db_conn)
        assert len(await _chart_rows(db_conn)) == 1
        await update_section("s1", "c1", SectionUpdate(content="纯文字，无图表。"),
                             db=db_conn)
        assert await _chart_rows(db_conn) == []

    async def test_save_invalid_chart_removes_block(self, db_conn):
        """修复失败的坏图：块从正文删除、不登记，但保存本身不失败"""
        await _seed_section(db_conn)
        content = "前文\n```mermaid\ngraph TD\nA-[坏\n```\n后文"
        r = await update_section("s1", "c1", SectionUpdate(content=content),
                                 db=db_conn)
        assert r["ok"] is True
        assert await _chart_rows(db_conn) == []
        row = await _section_row(db_conn)
        assert "```mermaid" not in row["content"]
        assert "前文" in row["content"] and "后文" in row["content"]

    async def test_pure_text_save_noop_chart_table(self, db_conn):
        """无图表正文保存：不产生任何登记，行为与旧版一致（向后兼容）"""
        await _seed_section(db_conn)
        await update_section("s1", "c1", SectionUpdate(content="普通正文。"),
                             db=db_conn)
        assert await _chart_rows(db_conn) == []
        row = await _section_row(db_conn)
        assert row["content"] == "普通正文。"

    # ---------------- R38 遗留收口：手动保存接入未闭合围栏补齐 ----------------

    async def test_save_unclosed_fence_completed_before_persist(self, db_conn):
        """未闭合围栏（粘贴截断）→ 落库前补齐闭合，与生成链路同函数同口径。

        旧行为：手动保存不做 auto_fix，未闭合 ```mermaid 原样落库，
        导出/预览时其后正文被整段吞进代码块。新行为：保存前用
        content_utils.auto_fix_unclosed_fences（与 _persist_section 同一实现）
        幂等追加收尾围栏，补齐后成为合法图表 → 正常登记（而非删块）。
        """
        await _seed_section(db_conn)
        content = f"总体流程如下：\n```mermaid\n{VALID_FLOWCHART}"
        r = await update_section("s1", "c1", SectionUpdate(content=content),
                                 db=db_conn)
        assert r["ok"] is True
        row = await _section_row(db_conn)
        assert row["content"].rstrip().endswith("```")
        # 补齐后是闭合合法图 → 按登记侧同一判据正常入清单
        assert await _chart_rows(db_conn) == ["flowchart"]
        # 字数口径基于补齐后正文（闭合块内文字同样被剔除）
        assert row["word_count"] == text_word_count(row["content"])

    async def test_save_closed_content_untouched_by_fence_fix(self, db_conn):
        """已闭合正文一字不动：补齐仅针对未闭合围栏，幂等无损（护栏）。

        A/B 承重反向：若接线退化为无条件重写/重复追加，本用例即失败。
        """
        await _seed_section(db_conn)
        content = (f"总体流程如下：\n```mermaid\n{VALID_FLOWCHART}\n```\n"
                   "后续说明不得被吞进代码块。")
        await update_section("s1", "c1", SectionUpdate(content=content),
                             db=db_conn)
        row = await _section_row(db_conn)
        assert row["content"] == content
        assert await _chart_rows(db_conn) == ["flowchart"]
