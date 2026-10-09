# -*- coding: utf-8 -*-
"""F6 · 日志准确性（2026-10-05）护栏

背景（R41 目录生成模块增强 · 用户修复清单第 9 项「日志不准确」）：
本次实测发现三类「日志说不清楚 / 说错话」的形态，本文件对修复逐条锁定：

1. **级别口径错位**：`目录生成上下文装配失败（降级为空上下文继续）` —— 降级属于
   可恢复异常，却用 `logger.exception`（ERROR 级）落盘，与 AGENTS.md §3.1.6
   「ERROR=业务阻断、WARNING=可恢复异常」冲突，排障时被当成真故障。
2. **失败日志缺身份**：目录/正文/事实三条顶层 `logger.exception("XX生成失败")`
   与章节级失败日志一律不带 `scheme_id` / `task_id` / `section_id` ——
   多方案并发时无法把日志行关联回具体任务，只能靠时间窗猜。
3. **「失败 checkpoint 失败」双失败歧义**：checkpoint 写盘失败的告警原文形如
   `保存正文生成失败 checkpoint 失败（task=…）` —— 一句话里出现两次「失败」，
   分不清是**正文生成失败**还是**checkpoint 写盘失败**；且 13 处各写各的
   （`保存目录部分成果…` / `取消路径保存正文…` …），grep 不中。
   统一为 `检查点写入失败（task=… · <业务> · <场景>）`。
4. **成功路径零日志**：`finish_task` 是所有任务类型唯一的终态收口点，任务正常
   完成时 `logs/backend.log` 里查不到任何一行，无法区分「任务没跑」与
   「跑完了但没记录」。补一条 INFO（正常追踪），且**仅在终态成功落库后**记录，
   避免与「终态落库失败 ERROR」形成同一事件的矛盾双条。

约束：本轮**只改日志文本/级别，不改 SSE 载荷 message、不改任何控制流**。
"""
import inspect
import logging
import re

import app.routers.sse_handlers as sh
import app.services.ai.task_registry as tr
import pytest
from app.services.ai.task_registry import finish_task, register_task

SSE_SRC = inspect.getsource(sh)
TR_SRC = inspect.getsource(tr)
# 折叠空白/换行后做「同一语句」级断言（多行调用会被拆行，见 F6 各处排版）
SSE_FLAT = re.sub(r"\s+", " ", SSE_SRC)
TR_FLAT = re.sub(r"\s+", " ", TR_SRC)


# ============================================================
# 1. 级别口径：降级 ≠ 业务阻断
# ============================================================
class TestLevelCaliber:
    def test_context_assembly_downgrade_is_warning(self):
        """降级为「空上下文继续」= 可恢复异常 → 必须 WARNING（不得 exception）。"""
        assert 'logger.warning("目录生成上下文装配失败' in SSE_FLAT
        assert 'logger.exception("目录生成上下文装配失败' not in SSE_FLAT
        # 既然是失败仍须留堆栈（exc_info），但级别不能是 ERROR
        assert re.search(
            r'logger\.warning\("目录生成上下文装配失败[^)]*exc_info=True\)',
            SSE_FLAT), "降级告警丢失 exc_info=True"

    def test_downgrade_warning_carries_identity(self):
        """降级告警必须带 scheme_id + task_id，否则并发时无法定位。"""
        # ⚠️ 字符串里含 ASCII 引号时必须用单引号 Python 字面量，否则首个 `"`
        # 会被当成字符串定界符（本轮首版护栏即因此 4 例误报）。
        needle = ('目录生成上下文装配失败（降级为空上下文继续 · scheme_id=%s · task=%s）'
                  '", scheme_id, task_id')
        assert needle in SSE_FLAT, needle


# ============================================================
# 2. 失败日志必须带身份标识
# ============================================================
class TestFailureIdentity:
    @pytest.mark.parametrize("msg", [
        "目录生成失败",
        "正文生成失败",
        "全局事实提取失败",
    ])
    def test_top_level_failure_carries_scheme_and_task(self, msg):
        """三条顶层终态失败必须带 scheme_id + task_id。"""
        assert f'logger.exception("{msg}（scheme_id=%s · task=%s）", scheme_id, task_id)' \
            in SSE_FLAT, f"{msg} 未带方案/任务标识"

    def test_section_failure_carries_section_title_scheme_task(self):
        """章节「最终失败」（单章重试耗尽）必须四标识齐全。"""
        needle = ('章节生成最终失败（section_id=%s · title=%s · scheme_id=%s · task=%s）: %s'
                  '", leaf_id, title, scheme_id, task_id, last_err')
        assert needle in SSE_FLAT, needle

    def test_section_task_exception_logs_traceback(self):
        """章节子任务异常（可恢复外的兜底）必须补堆栈 + 标识。"""
        needle = ('章节生成子任务异常（scheme_id=%s · task=%s）: %s'
                  '", scheme_id, task_id, e, exc_info=True')
        assert needle in SSE_FLAT, needle

    def test_unexpected_section_exception_keeps_identity_and_traceback(self):
        """guarded_gen 兜底分支的堆栈日志必须同时带上方案/任务标识。"""
        needle = ('章节生成未预期异常（section_id=%s · title=%s · scheme_id=%s · task=%s）: %s'
                  '", leaf_id, title, scheme_id, task_id, e, exc_info=True')
        assert needle in SSE_FLAT, needle


# ============================================================
# 3. checkpoint 写盘失败：统一前缀，消除「双失败」歧义
# ============================================================
class TestCheckpointLogUnification:
    CHECKPOINT_PREFIX = 'logger.warning("检查点写入失败'

    def test_unified_prefix_used_everywhere(self):
        """13 处 checkpoint 写盘失败告警必须全部走统一前缀（可一次 grep 命中）。"""
        assert SSE_FLAT.count(self.CHECKPOINT_PREFIX) == 13, (
            "checkpoint 告警应恰为 13 处：增删该类告警都要同步本断言")

    def test_no_double_failure_phrase(self):
        """禁止「…失败 checkpoint 失败」双失败歧义句式（原 8 处的真实原文）。"""
        assert "失败 checkpoint 失败" not in SSE_SRC
        for legacy in ("保存目录生成 checkpoint 失败",
                       "保存正文生成 checkpoint 失败",
                       "保存目录部分成果 checkpoint 失败",
                       "保存正文全章失败 checkpoint 失败",
                       "取消路径保存正文 checkpoint 失败"):
            assert legacy not in SSE_SRC, f"遗留告警原文未替换: {legacy}"

    def test_checkpoint_warnings_are_not_bare_exception(self):
        """checkpoint 写盘失败是可恢复异常 → 只能 WARNING，不得升级为 ERROR。"""
        bad = [ln for ln in SSE_SRC.splitlines()
               if "检查点写入失败" in ln and "logger.error" in ln]
        assert not bad, f"checkpoint 告警被升级为 ERROR: {bad}"

    def test_unified_messages_carry_business_and_scene(self):
        """统一前缀必须带业务域（目录生成/正文生成/事实提取）与场景标签。"""
        for biz in ("· 目录生成 ·", "· 正文生成 ·", "· 事实提取 ·"):
            assert biz in SSE_SRC, f"统一前缀缺少业务域: {biz}"
        for scene in ("部分成果", "终态成果", "阶段成果", "取消路径成果",
                      "异常终止明细", "成果已落库"):
            assert scene in SSE_SRC, f"统一前缀缺少场景标签: {scene}"


# ============================================================
# 4. 审核/修复链补方案身份（scheme=%s）
# ============================================================
class TestAuditChainSchemeIdentity:
    AUDIT_MSGS = [
        "外科式补齐超时（%ds），回退完整 AI 审核（scheme=%s）",
        "外科式补齐失败，回退完整 AI 审核（scheme=%s）",
        "目录审核超时（%ds），跳过审核直接完成（scheme=%s）",
        "目录审核失败（scheme=%s）",
        "审核轮合并修复结果不可用，回退独立修复调用（scheme=%s）",
        "目录自动修复超时（%ds），跳过修复保留原目录（scheme=%s）",
        "目录自动修复失败（scheme=%s）",
    ]

    @pytest.mark.parametrize("msg", AUDIT_MSGS)
    def test_audit_warning_carries_scheme(self, msg):
        """7 处目录审核/修复链告警必须带方案身份（多方案并发时的唯一线索）。"""
        assert msg in SSE_FLAT, f"审核链告警缺失或未带 scheme 标识: {msg}"
        i = SSE_FLAT.index(msg)
        window = SSE_FLAT[i:i + 200]
        assert "scheme_name" in window, f"告警未传 scheme_name: {msg}"


# ============================================================
# 5. finish_task 成功路径必须留痕（INFO = 正常追踪）
# ============================================================
class TestFinishTaskTerminalLog:
    def test_completed_logged_at_info(self):
        """任务完成必须记 INFO（AGENTS.md §3.1.6：正常追踪用 INFO）。"""
        assert 'logger.info("任务完成（type=%s · scheme=%s · task=%s）"' in TR_FLAT

    def test_non_completed_logged_with_status_and_message(self):
        """非完成终态（failed/stopped）必须记状态 + 截断后的失败消息。"""
        assert 'logger.info("任务终止 %s（type=%s · scheme=%s · task=%s）: %s"' in TR_FLAT
        assert "(message or \"\")[:120]" in TR_FLAT, "终态消息未截断（可能刷爆日志）"

    def test_info_is_guarded_by_terminal_db_ok(self):
        """INFO 必须以 `_terminal_db_ok` 为门 —— 落库失败时上一行已按 ERROR
        落盘，再记一条「任务完成」会构成同一事件的矛盾日志。

        只在 ``finish_task`` 函数体内做时序判定（模块内其它函数也会出现
        ``retry_db_op`` 等同名 token，全文匹配会错位）。
        """
        fin = re.sub(r"\s+", " ", inspect.getsource(tr.finish_task))
        order = [
            "state = _tasks.get(task_id)",
            "_terminal_db_ok = False",
            "await retry_db_op(",
            "_terminal_db_ok = True",
            'logger.error("finish_task 终态落库失败',
            "if _terminal_db_ok:",
            'logger.info("任务完成',
        ]
        pos = [fin.find(tok) for tok in order]
        assert all(p >= 0 for p in pos), dict(zip(order, pos))
        assert pos == sorted(pos), (
            "终态日志门控时序被破坏（应先落库、失败记 ERROR、"
            "仅成功后才记 INFO）: " + str(dict(zip(order, pos))))


# ============================================================
# 6. 行为级实证（caplog）
# ============================================================
class TestFinishTaskLogBehavior:
    async def test_completed_emits_info_with_identity(self, db_conn, caplog):
        caplog.set_level(logging.INFO)
        tid = await register_task("generate_outline",
                                  project_id="p1", scheme_id="scheme-42")
        await finish_task(tid, "completed", "目录生成完成")
        msgs = [r.getMessage() for r in caplog.records
                if r.levelno == logging.INFO and "任务完成（" in r.getMessage()]
        assert len(msgs) == 1, msgs
        m = msgs[0]
        assert "type=generate_outline" in m
        assert "scheme=scheme-42" in m
        assert f"task={tid}" in m

    async def test_failed_status_emits_termination_info(self, db_conn, caplog):
        caplog.set_level(logging.INFO)
        tid = await register_task("generate_content", scheme_id="scheme-7")
        await finish_task(tid, "failed", "全部 38 章生成失败")
        msgs = [r.getMessage() for r in caplog.records
                if r.levelno == logging.INFO and "任务终止" in r.getMessage()]
        assert len(msgs) == 1, msgs
        assert "status=failed" not in msgs[0] and "终止 failed" in msgs[0]
        assert "scheme-7" in msgs[0] and "全部 38 章生成失败" in msgs[0]

    async def test_db_failure_logs_error_without_contradictory_info(
            self, db_conn, monkeypatch, caplog):
        """终态落库失败 → 只有 ERROR，不得再补一条「任务完成」INFO。"""
        caplog.set_level(logging.INFO)

        async def _boom(_fn):
            raise RuntimeError("database is locked")

        # 先注册再打桩（register_task 本身也走 retry_db_op）
        tid = await register_task("generate_outline", scheme_id="scheme-9")
        monkeypatch.setattr(tr, "retry_db_op", _boom)
        await finish_task(tid, "completed", "目录生成完成")
        texts = [r.getMessage() for r in caplog.records]
        assert any("finish_task 终态落库失败" in t for t in texts), texts
        assert not any("任务完成（" in t for t in texts), (
            "落库失败仍记了「任务完成」，与 ERROR 构成矛盾日志")
