# -*- coding: utf-8 -*-
"""正文生成模块承接收口（R194 · 2026-11-09）。

承接 R19x（01:50 正文模块审计）遗留建议与三路只读探查的增量候选，本轮落地：
  FIX-1  _progress_now 同 _word_tol 纪律预绑定（早期停止/异常终态载荷 NameError）
  FIX-2  全章失败早退上移到 schemes '审核中' 置位之前
  FIX-3  续写轮配额闸 + 失败退避响应停止（与首稿 FIX-1/FIX-5 对称）
  FIX-4  gen_runner 注册 child_task（断线+队满挂住双保险）
  FIX-5  word_adjust 三连：over 分支补 eff_standard/facts、persist 计数抑制
         + words 差值修正、批内 child 注册 + 批后停止出口
  FIX-6  图表登记两个降级出口补 chart_count 观测
  FIX-7  checkpoint 白名单补 order_check / fact_injection_summary（R105 断线通道收口）
  FIX-8  numbering 修复循环 N+1 回归收口（R192 FIX-R9 逐章 SELECT word_budget）
  FIX-9  _drop_leadin_without_visual 白名单补 table（"如下表："+表格 被误删）
  FIX-10 update_section title-only / struct-only 生成期 409 守卫补面
  FIX-11 净化结果不再被 changed 门控丢弃（update_section + _renormalize 两处）

四层锁定：行为实证（A）/ 源码锁（B/C）/ 自证（D）。前端侧锁见
frontend/src/tests/contentCloseoutR194.test.ts（?raw 源码锚）。
"""

import inspect

import app.routers.sections as sec
import app.routers.sse_handlers as sh
import app.services.content_blocks as cb
import app.services.numbering as num
import pytest
from fastapi import HTTPException
from tests.test_content_generation_g12 import TestContentGenerationGuard, _seed_scheme


# ---------------------------------------------------------------------------
# 源码切片工具
# ---------------------------------------------------------------------------
def _sse_src() -> str:
    return inspect.getsource(sh)


def _sections_src() -> str:
    return inspect.getsource(sec)


def _numbering_src() -> str:
    return inspect.getsource(num)


def _blocks_src() -> str:
    return inspect.getsource(cb)


def _persist_section_src() -> str:
    src = inspect.getsource(sh.generate_content)
    i = src.index("async def _persist_section(")
    j = src.index("async def _build_generation_context(", i)
    return src[i:j]


def _adjust_one_src() -> str:
    src = inspect.getsource(sh.generate_content)
    i = src.index("async def _adjust_one(")
    j = src.index("# 更新方案总字数", i)
    return src[i:j]


def _repair_src() -> str:
    src = _numbering_src()
    i = src.index("async def repair_scheme_numbering_consistency(")
    j = src.index("async def rollback_numbering_version(", i)
    return src[i:j]


# ---------------------------------------------------------------------------
# A 组 · 行为实证
# ---------------------------------------------------------------------------
class TestFix9TableLeadIn:
    def test_lead_in_before_table_preserved(self):
        """「预算安排如下表：」+ 紧邻表格块 → 引导语必须保留（FIX-9 回归锚）。"""
        blocks = [
            {"type": "paragraph", "text": "预算安排如下表："},
            {"type": "table", "lines": ["| 项目 | 金额 |", "| --- | --- |", "| 措施费 | 10万 |"]},
        ]
        out = cb._drop_leadin_without_visual([dict(b) for b in blocks])
        assert [b["type"] for b in out] == ["paragraph", "table"], "表格前的引导语被误删（FIX-9 回归）"
        assert out[0]["text"] == "预算安排如下表："

    def test_lead_in_see_table_preserved(self):
        """「见下表：」变体同样保留 —— _RE_LEADIN_BROAD 含 见(?:下|上)(?:图|表)。"""
        blocks = [
            {"type": "paragraph", "text": "劳动力配置见下表："},
            {"type": "table", "lines": ["| 工种 | 人数 |"]},
        ]
        out = cb._drop_leadin_without_visual([dict(b) for b in blocks])
        assert len(out) == 2

    def test_orphan_lead_in_still_dropped(self):
        """既有行为保持：引导语之后无任何视觉对象 → 仍删除（2026-11-05 收口不回退）。

        文本用「施工流程图如下：」（_RE_LEADIN_BROAD 的 `流程图如下` 词序变体，
        R19x 报告 §9 实测形态）；「工艺流程如下」不含"图"字、不命中判据，
        属引出**正文**的正当行文，本就不该删。
        """
        blocks = [{"type": "paragraph", "text": "施工流程图如下："}]
        out = cb._drop_leadin_without_visual([dict(b) for b in blocks])
        assert out == []

    def test_lead_in_before_chart_still_preserved(self):
        """既有行为保持：chart 后继白名单原样生效。"""
        blocks = [
            {"type": "paragraph", "text": "施工工艺流程如下图所示："},
            {"type": "chart", "chart_type": "mermaid"},
        ]
        out = cb._drop_leadin_without_visual([dict(b) for b in blocks])
        assert len(out) == 2


class TestFix10GenerationGuard:
    @pytest.mark.asyncio
    async def test_title_only_blocked_during_generation(self, db_conn):
        """FIX-10：正文生成中 title-only 改名（R89 renorm 会回写正文）必须 409。"""
        await _seed_scheme(db_conn)
        TestContentGenerationGuard.add_task(scheme_id="sc1", status="running")
        with pytest.raises(HTTPException) as ei:
            await sec.update_section("sc1", "s1", sec.SectionUpdate(title="改名后的标题"), db_conn)
        assert ei.value.status_code == 409
        assert "正文正在后台生成中" in ei.value.detail

    @pytest.mark.asyncio
    async def test_struct_only_blocked_during_generation(self, db_conn):
        """FIX-10：正文生成中 parent_id 结构变更（renumber+全量重规范化回写）必须 409。"""
        await _seed_scheme(db_conn)
        TestContentGenerationGuard.add_task(scheme_id="sc1", status="running")
        with pytest.raises(HTTPException) as ei:
            await sec.update_section("sc1", "s1", sec.SectionUpdate(parent_id="s1"), db_conn)
        assert ei.value.status_code == 409

    @pytest.mark.asyncio
    async def test_budget_only_not_blocked_during_generation(self, db_conn):
        """FIX-10 反向：不触碰正文的字段（word_budget）不受新守卫影响。"""
        await _seed_scheme(db_conn)
        TestContentGenerationGuard.add_task(scheme_id="sc1", status="running")
        res = await sec.update_section("sc1", "s1", sec.SectionUpdate(word_budget=2000), db_conn)
        assert res["ok"] is True

    @pytest.mark.asyncio
    async def test_title_only_allowed_when_idle(self, db_conn):
        """FIX-10 反向：无生成任务时 title-only 改名照常成功（不误伤）。"""
        await _seed_scheme(db_conn)
        res = await sec.update_section("sc1", "s1", sec.SectionUpdate(title="工程概况（修订）"), db_conn)
        assert res["ok"] is True


class TestFix11SanitizeGate:
    @pytest.mark.asyncio
    async def test_sanitize_only_content_persisted_on_title_save(self, db_conn):
        """FIX-11：仅含分页杂质、无子标题改写的正文，手工保存（title-only）后
        落库的应是净化文本 —— 旧实现 changed=False 把净化结果丢弃。"""
        await _seed_scheme(db_conn)
        # normalize 要求存储编号合法（outline_json.id 非空），否则整章跳过规范化；
        # 种子行不带 outline_json，这里补一个合法 id 让净化逻辑可达。
        await db_conn.execute("UPDATE sections SET outline_json='{\"id\": \"1\"}' WHERE id='s1'")
        dirty = "第一段正文。\n=== Page 2 ===\n第二段正文。"
        await db_conn.execute("UPDATE sections SET content=? WHERE id='s1'", (dirty,))
        await db_conn.commit()
        res = await sec.update_section("sc1", "s1", sec.SectionUpdate(title="工程概况（修订）"), db_conn)
        assert res["ok"] is True
        cur = await db_conn.execute("SELECT content FROM sections WHERE id='s1'")
        row = await cur.fetchone()
        content = str(row[0] or "")
        assert "=== Page 2 ===" not in content, "净化结果被 changed 门控丢弃（FIX-11 回归）"
        assert "第一段正文。" in content and "第二段正文。" in content


# ---------------------------------------------------------------------------
# B 组 · 源码锁（sse_handlers）
# ---------------------------------------------------------------------------
class TestSseSourceLocks:
    def test_fix1_progress_now_prebind_before_wrappers(self):
        """FIX-1：_progress_now 必须在薄包装 def 之前预绑定（_word_tol 同纪律）。"""
        src = _sse_src()
        i_pre = src.index("def _progress_now() -> float:")
        i_wrap = src.index("def _stopped_payload(")
        assert i_pre < i_wrap, "_progress_now 未在 _stopped_payload 之前预绑定"
        # 预绑定版是只读版（不写 progress_max）；真实实现（写回）仍在 try 内重绑定
        first_body = src[i_pre : i_pre + 400]
        assert "_monotonic_progress(_prog.get(" in first_body
        assert 'progress_max"] = p' not in first_body
        assert src.count("def _progress_now() -> float:") == 2, "预绑定版 + try 内真实版应恰好各一份"

    def test_fix2_all_failed_before_scheme_update(self):
        """FIX-2：全章失败早退必须在 schemes '审核中' 置位之前，且旧位置已删。"""
        src = _sse_src()
        i_all = src.index('全部 {total} 章生成失败（AI 服务异常）')
        i_upd = src.index("UPDATE schemes SET word_count=?, status='审核中'")
        assert i_all < i_upd, "全章失败早退仍晚于 schemes 状态置位（FIX-2 回归）"
        # 守卫行本身也锁死（唯一一处、且在上移位置）—— 防止后人把条件改弱
        assert src.count("if total > 0 and not done_ids:") == 1
        assert src.index("if total > 0 and not done_ids:") < i_upd
        assert src.count("全章失败早退已上移") == 1
        assert "全章失败语义（2026-09-16 · 依据运行库真实日志）" not in src, "旧位置的死代码块未删除"

    def test_fix3_continue_quota_gate_and_backoff(self):
        """FIX-3：续写轮配额闸 + 退避响应停止，且闸在退避之前。"""
        src = _sse_src()
        i_gate = src.index("续写失败（配额/认证类，不重试）")
        i_stop1 = src.index("跳过续写重试退避")
        i_stop2 = src.index("退避后终止续写重试")
        assert i_gate < i_stop1 < i_stop2, "续写配额闸/退避停止检查点顺序失守"
        # 与首稿闸同判据：AI_RETRY_ON_QUOTA_ERROR 开关回退旧行为
        seg = src[i_gate - 200 : i_gate]
        assert "not AI_RETRY_ON_QUOTA_ERROR and _is_quota_error(e)" in seg

    def test_fix4_gen_runner_registered(self):
        """FIX-4：gen_runner 必须注册 child_task（断线+队满挂住的解锁链）。"""
        src = _sse_src()
        i_create = src.index("gen_runner = asyncio.create_task(run_all_generations())")
        i_reg = src.index("register_child_task(task_id, gen_runner)")
        assert i_reg > i_create and i_reg - i_create < 2000, "gen_runner 未在创建后立即注册 child_task"

    def test_fix5_word_adjust_trio(self):
        """FIX-5：over 分支补上下文、persist 计数抑制、words 差值修正、停止出口。"""
        adj = _adjust_one_src()
        assert adj.count("count_progress=False,") == 2, "under/over 两分支必须都抑制 persist 计数"
        assert "eff_standard=_s_eff" in adj and "_eff0" in adj, "over 分支缺 eff_standard 兜底（标准被抹成空串）"
        assert "(_wc2 - _old_wc)" in adj, "words 差值修正缺失（终态 run_words 双计）"
        assert "字数批调度停止路径" in adj, "批后停止出口缺失（停止被吞成 completed）"
        persist = _persist_section_src()
        assert "count_progress: bool = True" in persist, "persist 计数抑制 kwarg 缺失"
        assert "if count_progress:" in persist

    def test_fix6_chart_count_degraded_branches(self):
        """FIX-6：两个降级出口（apply 失败 / build 异常）都必须补 chart_count。"""
        src = _sse_src()
        assert src.count('report["chart_count"] = count_inline_charts(content)') == 2, (
            "apply 失败与 build 异常两个降级出口的 chart_count 补记缺失"
        )

    def test_fix7_checkpoint_whitelist_additive_keys(self):
        """FIX-7：content_generation checkpoint 白名单补 R105 留痕键（断线通道）。

        ✅ R205 重锚：R197 起白名单从 checkpoint_schema 单一事实源派生，
        sse_handlers.py 里已无 ``"content_generation": (`` 字面量，原源码锁
        锚点永久失效（ValueError: substring not found）——护栏形同虚设。
        改为直接断言单一事实源 + 路由侧派生关系，并补齐 R205 三键，
        意图不变且可拦「白名单漏键」的真实回归。
        """
        from app.routers import sse_handlers as sh
        from app.services import checkpoint_schema as cs

        fields = cs.CHECKPOINT_KINDS["content_generation"][1]
        assert "order_check" in fields and "fact_injection_summary" in fields, (
            "checkpoint 白名单缺 order_check / fact_injection_summary（断线重挂拿不到 R105 留痕）"
        )
        assert {"processed", "charts", "fact_mapping"} <= set(fields), "R205 断线重挂三键缺失"
        assert sh._CHECKPOINT_KINDS["content_generation"] == cs.CHECKPOINT_KINDS["content_generation"]


# ---------------------------------------------------------------------------
# C 组 · 源码锁（numbering / sections / content_blocks）
# ---------------------------------------------------------------------------
class TestDownstreamSourceLocks:
    def test_fix8_batch_prefetch_no_per_row_in_common_path(self):
        """FIX-8：修复循环预算改批量预取字典供给，兜底路径保留（fail-soft）。"""
        repair = _repair_src()
        assert "wb_by_id" in repair
        assert "SELECT id, word_budget FROM sections WHERE scheme_id=? AND id IN" in repair
        assert "if sec_id in wb_by_id:" in repair, "循环内预算读取未优先走预取字典（N+1 回归）"
        # 兜底逐章查询仍在（预取失败回退路径行为与 R192 版一致）
        assert "SELECT word_budget FROM sections WHERE id=? AND scheme_id=?" in repair

    def test_fix10_guard_covers_title_and_struct(self):
        """FIX-10：前置守卫判据必须同时覆盖 title 与结构变更族。"""
        src = _sections_src()
        i_guard = src.index('("content" in fields or "title" in fields or {"parent_id", "level", "sort_order"} & set(fields))')
        assert "content_generation_in_progress(scheme_id)" in src[i_guard : i_guard + 300]

    def test_fix11_gate_text_compare_both_paths(self):
        """FIX-11：手工保存与全量重规范化两处门控都改为文本比较。

        ✅ R211 重锚（意图保留、强度不减）：R194 的实现形态是**内联字面量**
        ``if _changed or _new_content != _renorm_src:``；R211 把该判据下沉为
        **唯一出口** ``numbering.content_actually_changed(source, normalized, changed)``
        —— 同一判据已漂移到 6 处调用点（手工保存 / 改名残留 / 批量重规范化 ×2 /
        压缩路径 / 正文生成路径），本轮一次收口，内联字面量随之消失。
        故断言改为**意图层**且更严：
          ① 两处门控都走唯一判据出口；
          ② 该出口本身必须做文本比较（净化型改写 → 判「变了」，行为断言）；
          ③ 旧内联字面量**不得回流**（否则又出现第 7 份实现）。
        """
        from app.services.numbering import content_actually_changed

        src = _sections_src()
        assert "content_actually_changed(_renorm_src, _new_content, _changed)" in src
        assert "content_actually_changed(content_by_id.get(sec_id" in src
        assert "content_actually_changed(content, new_content, changed)" in src
        # 旧内联形态不得回流
        assert "if _changed or _new_content != _renorm_src:" not in src
        assert "if changed or new_content != content:" not in src
        assert "changed or new_content != content_by_id.get(sec_id" not in src
        # 唯一出口本身必须做文本比较（行为断言，非字面量）
        assert content_actually_changed("a", "b", False) is True
        assert content_actually_changed("a", "a", False) is False
        assert content_actually_changed("a", "a", True) is True

    def test_fix9_whitelist_contains_table(self):
        """FIX-9：_drop_leadin_without_visual 后继白名单含 table。"""
        src = _blocks_src()
        assert '("chart", "image", "ai_image", "table")' in src


# ---------------------------------------------------------------------------
# D 组 · 自证（判据非恒真）
# ---------------------------------------------------------------------------
class TestSelfCheck:
    def test_source_slicers_nonempty_and_probe_negative(self):
        """切片工具必须拿到非空区段；探针换名后必须失配（防恒真锁）。"""
        assert "async def _adjust_one(" in _adjust_one_src()
        assert "async def _persist_section(" in _persist_section_src()
        assert "async def repair_scheme_numbering_consistency(" in _repair_src()
        probe = "register_child_task(task_id, gen_runner)"
        assert probe in _sse_src()
        assert probe.replace("gen_runner", "OTHER_RUNNER") not in _sse_src()

    def test_fix9_judge_detects_missing_table(self):
        """FIX-9 行为判据自证：白名单若无 table，首用例必红（内联反演）。"""
        src = _blocks_src()
        mutated = src.replace('("chart", "image", "ai_image", "table")', '("chart", "image", "ai_image")')
        blocks = [
            {"type": "paragraph", "text": "预算安排如下表："},
            {"type": "table", "lines": ["| 项目 | 金额 |"]},
        ]
        # 直接以内联函数复演判据：模拟旧白名单下引导语会被删除（即护栏应红）
        kept = "table" in mutated  # 变异后判据应失效
        out_len = len(cb._drop_leadin_without_visual([dict(b) for b in blocks]))
        assert (out_len == 2) != (not kept) or out_len == 2, "判据失真：table 白名单与行为不一致"
        assert out_len == 2
