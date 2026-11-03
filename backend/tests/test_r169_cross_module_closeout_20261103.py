"""R169（2026-11-03 · 六模块跨模块深度审计收口）护栏。

九项修复的回归锁（分组 A–E），全部判据取自本轮修复的真实缺陷形态：

A 提示词（FIX-1/FIX-2）
  A1 content_plan_user 模板 JSON 示例渲染后**不得含双花括号**——旧模板用
     ``{{...}}`` 转义写法，而渲染走正则替换（不处理 ``{{``→``{``），畸形 JSON
     示例原样下发，弱模型模仿输出即 json.loads 失败、每章白烧 1-2 轮修复
     （R169 审计实测渲染复现 has doubled braces = True）。
  A2 ``/admin/prompts/sync-from-code`` 的 inserted / force-overwritten 两分支
     必须写 ``prompt_audit_logs``（旧实现直写 DB 不留痕，force 覆盖的用户
     编辑不在任何审计快照里，回滚 UI 永远无法恢复）。

B 图表（FIX-4/FIX-7/FIX-8）
  B1 ``_json_fix_candidate_rejected`` 形态闸纯函数：JSON 型修复的 Mermaid
     候选必须拒绝（旧实现回落 Mermaid 校验放行 → validated=True 写库 →
     ```chart-json 围栏装 Mermaid 文本 → 导出解析跳过 / 渲染必然 None /
     清单 placed 翻 false 的三侧不可见图）。
  B2 fix_mermaid 循环体内必须调用该闸（AST）。
  B3 ``resync_inline_charts`` 兜底判据必须为 ``is None``——旧 ``or new_content``
     把 apply 的合法空串返回值回退成裁剪前正文（纯图块章节被限额裁空 →
     图块复活为幽灵图）。
  B4 ``_rewrite_code_block`` 对 eof 残片与 truncated 同口径**原样保留**——
     旧实现对 eof 命中 old_code 时补写闭合围栏（截断残片被洗白成合法图表，
     与登记/导出两侧「eof 不算图」口径分叉）。

C 解析提取（FIX-5/FIX-6）
  C1 Markdown 项「整体空结果重跑」的输入预算闸：超预算保留缺失哨兵不重跑
     （旧实现把 40 万字整卷塞单条 prompt，必超上下文且失败被写成 error）。
  C2 解析项重试循环接 ``is_non_retryable_error``（永久错误立即终止，不再
     白烧退避——与事实链 R111 P2-7 同判据单一出口）。

D 正文/事实（FIX-9/FIX-10/FIX-11）
  D1 ``_mark_section_failed`` 的 DB 写必须接 ``retry_db_op``（锁族漏面：
     locked 异常逃出 gen_one 造成同章双份 section_error）。
  D2 事实修复后的图表配对重建（``resync_inline_chart_rows``）必须透传
     ``dropped_out``（R48 删图可观测在该出口不完整）。
  D3 ``persist_extraction`` 两处作用域 SELECT 必须判 ``cur is None``
     （R166 FIX-3 服务层延伸：R13 命中裸 fetchall 即 AttributeError，
     整轮 AI 提取成果在落库前崩掉且无法定位）。

E 配置安全（FIX-13/FIX-14）
  E1 密钥文件排他创建：竞态失败方必须读取胜出方密钥（旧实现各写各的，
     后写者覆盖先写者 → 先写进程已加密落库的密文即 key_broken）。
  E2 ``pf.reset_reliability()`` 存在且清空 ``_provider_reliability`` 与
     ``_det_dead_streak``；conftest autouse 必须接线（R110 同族测试隔离）。
"""
from __future__ import annotations

import ast
from pathlib import Path

import app.routers._chart_pipeline as pl
import app.routers.bid_analysis as ba
import app.services.crypto as crypto
import pytest

# ---------------------------------------------------------------------------
# 公共：源码读取与 AST 助手
# ---------------------------------------------------------------------------
_APP = Path(__file__).resolve().parent.parent / "app"


def _src(rel: str) -> str:
    return (_APP / rel).read_text(encoding="utf-8")


def _func_src(source: str, name: str) -> str:
    """按 AST 抽取顶层（或任意层级首个同名）函数的源码片段。"""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"源码中找不到函数 {name}")


# ---------------------------------------------------------------------------
# A 提示词
# ---------------------------------------------------------------------------
class TestPromptDoubleBraceAndSyncAudit:
    async def test_a1_content_plan_user_render_has_no_doubled_braces(self):
        from app.services.ai.prompts._registry import render
        out = render(
            "content_plan_user",
            scheme_name="方案", scheme_type="基坑", section_number="3.1",
            section_title="支护", section_description="说明", word_budget=1500,
            parent_chain="上级", sibling_lines="同级", project_brief="概述",
            knowledge_names_text="- 基坑规范", fact_titles_text="- 深度",
        )
        assert "{{" not in out, "JSON 示例渲染后仍含双花括号（畸形 JSON 下发）"
        assert '"writing_focus"' in out
        # 既有 D1 护栏的弱判据保持成立（{writing_focus 无引号形态仍不存在）
        assert "{writing_focus" not in out

    def test_a1b_template_source_has_no_doubled_braces(self):
        from app.services.ai.prompts._registry import get_default_prompt
        assert '{{"writing_focus"' not in get_default_prompt("content_plan_user")

    async def test_a2_sync_from_code_writes_audit(self, db_conn, monkeypatch):
        from app.routers.prompts import sync_prompts_from_code

        async def _audit_count(action: str) -> int:
            cur = await db_conn.execute(
                "SELECT COUNT(*) FROM prompt_audit_logs WHERE action=?", (action,))
            return (await cur.fetchone())[0]

        res = await sync_prompts_from_code(force=False, db=db_conn)
        summary = res["summary"]
        assert summary["inserted"] > 0
        assert await _audit_count("sync_insert") == summary["inserted"]

        # 幂等二次：in_sync，审计不新增
        cur = await db_conn.execute("SELECT COUNT(*) FROM prompt_audit_logs")
        before = (await cur.fetchone())[0]
        summary2 = (await sync_prompts_from_code(force=False, db=db_conn))["summary"]
        assert summary2["inserted"] == 0
        cur = await db_conn.execute("SELECT COUNT(*) FROM prompt_audit_logs")
        assert (await cur.fetchone())[0] == before

        # drift + force：被覆盖的 DB 原文必须进审计快照（可回滚）
        key = "outline_short_system"
        await db_conn.execute(
            "UPDATE prompt_templates SET content = content || ? WHERE key=?",
            ("\n__用户编辑__", key))
        await db_conn.commit()
        summary3 = (await sync_prompts_from_code(force=False, db=db_conn))["summary"]
        assert summary3["drift"] >= 1
        summary4 = (await sync_prompts_from_code(force=True, db=db_conn))["summary"]
        assert summary4["overwritten"] >= 1
        cur = await db_conn.execute(
            "SELECT snapshot_json FROM prompt_audit_logs "
            "WHERE prompt_key=? AND action='sync_overwrite' "
            "ORDER BY created_at DESC LIMIT 1", (key,))
        row = await cur.fetchone()
        assert row is not None, "force 覆盖必须落 sync_overwrite 审计"
        assert "__用户编辑__" in (row[0] or ""), \
            "审计快照必须包含被覆盖的 DB 原文（否则回滚 UI 永远无法恢复）"

    def test_a2b_sync_audit_actions_are_literal_source_locked(self):
        src = _src("routers/prompts.py")
        assert '"sync_insert"' in src and '"sync_overwrite"' in src
        # 两个分支都必须经 record_prompt_audit（inserted 分支在 INSERT 之后）
        assert src.count("record_prompt_audit(") >= 3  # PATCH/reset/rollback + sync×2


# ---------------------------------------------------------------------------
# B 图表
# ---------------------------------------------------------------------------
class TestChartFixShapeGateAndPipeline:
    def test_b1_json_fix_candidate_rejected(self):
        from app.routers.charts import _json_fix_candidate_rejected as rej
        assert rej(True, "graph TD\nA-->B\nB-->C") is True
        assert rej(True, "```mermaid\ngraph TD\nA-->B\n```") is True
        assert rej(True, '{"type":"labor"}') is False
        assert rej(True, '[{"x":1}]') is False
        assert rej(False, "graph TD\nA-->B\nB-->C") is False
        assert rej(True, "") is True
        assert rej(True, None) is True  # type: ignore[arg-type]

    def test_b2_fix_mermaid_calls_gate(self):
        src = _func_src(_src("routers/charts.py"), "fix_mermaid")
        assert "_json_fix_candidate_rejected(" in src
        tree = ast.parse(src)
        called = any(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "_json_fix_candidate_rejected"
            for n in ast.walk(tree))
        assert called, "fix_mermaid 必须调用形态闸（防内联条件被摘除后函数成死码）"

    async def test_b3_resync_none_sentinel_not_or_fallback(self, monkeypatch):
        async def fake_apply_empty(db, sid, rows, content, **kw):
            return ""

        monkeypatch.setattr(pl, "apply_inline_chart_plan", fake_apply_empty)
        n, out = await pl.resync_inline_charts(
            None, "scheme-1", "sec-1", "正文内容", enforce_limits=False)
        assert n == 0
        assert out == "", "apply 合法空串（限额裁空）不得回退成裁剪前正文（幽灵图）"

        async def fake_apply_none(db, sid, rows, content, **kw):
            return None

        monkeypatch.setattr(pl, "apply_inline_chart_plan", fake_apply_none)
        _n, out2 = await pl.resync_inline_charts(
            None, "scheme-1", "sec-1", "正文内容", enforce_limits=False)
        assert out2 == "正文内容", "None（未传 content）语义回退保持不变"

    def test_b4_rewrite_preserves_eof_fragment(self):
        frag = '前文\n```chart-json\n{"type":"labor"}\n'
        out = pl._rewrite_code_block(frag, '{"type":"labor"}',
                                     '{"type":"labor","rows":[]}')
        assert out == frag, "eof 残片必须原样保留，绝不补写闭合围栏（洗白）"
        assert out.rstrip().endswith('{"type":"labor"}')

        closed = '前文\n```chart-json\n{"type":"labor"}\n```\n后文\n'
        out2 = pl._rewrite_code_block(closed, '{"type":"labor"}',
                                      '{"type":"labor","rows":[]}')
        assert out2 != closed and '{"type":"labor","rows":[]}' in out2
        assert "```\n后文" in out2, "闭合块改写行为保持不变（补写闭合围栏原语义）"


# ---------------------------------------------------------------------------
# C 解析提取
# ---------------------------------------------------------------------------
class TestBidAnalysisRetryAndRerunBudget:
    async def test_c1_markdown_rerun_budget_gate(self, monkeypatch):
        from app.services.bid_analysis_service import (
            MARKDOWN_MISSING_RESULT,
            is_missing_result,
        )

        calls: list[str] = []

        async def fake_single_call(item, text, output_type, **kw):
            calls.append(text)
            return MARKDOWN_MISSING_RESULT

        monkeypatch.setattr(ba, "_run_single_call", fake_single_call)
        monkeypatch.setattr(ba, "_MARKDOWN_RERUN_MAX_CHARS", 10)
        item = {"item_id": "it1", "label": "测试项", "output_type": "markdown"}

        res = await ba._run_single_item(
            None, "p1", "s1", item, "x" * 50, ["seg"])
        assert len(calls) == 1, "超预算不得整卷重跑"
        assert res["status"] == "success"
        assert is_missing_result(res["content"], "markdown"), \
            "超预算必须保留缺失哨兵（写成 error 会令 missing_required 误报）"

        res2 = await ba._run_single_item(
            None, "p1", "s1", item, "短文本", ["seg"])
        # 第二项：首次调用 + 空结果重跑 = 累计 3 次（首项 1 次未重跑）
        assert calls[1:] == ["短文本", "短文本"], "预算内保留原重跑语义（首次+重跑各一次）"
        assert res2["content"] == MARKDOWN_MISSING_RESULT

    def test_c2_permanent_error_breaks_retry_loop(self):
        src = _src("routers/bid_analysis.py")
        assert "if is_non_retryable_error(e):" in src
        pos_gate = src.index("if is_non_retryable_error(e):")
        pos_backoff = src.index("wait_s = 5 * (attempt + 1)")
        assert pos_gate < pos_backoff, \
            "永久错误判定必须在退避等待之前（立即终止，不再白烧 sleep+重试）"

    def test_c3_rerun_budget_constant_tied_to_chunk_size(self):
        from app.services.bid_analysis_service import DEFAULT_CHUNK_SIZE
        assert ba._MARKDOWN_RERUN_MAX_CHARS == DEFAULT_CHUNK_SIZE * 4
        assert ba._MARKDOWN_RERUN_MAX_CHARS > 0


# ---------------------------------------------------------------------------
# D 正文 / 事实
# ---------------------------------------------------------------------------
class TestContentChainLockRetryAndObservability:
    def test_d1_mark_section_failed_wraps_retry_db_op(self):
        gen_src = _func_src(_src("routers/sse_handlers.py"), "generate_content")
        mark_src = _func_src(gen_src, "_mark_section_failed")
        assert "_mark_failed_once" in mark_src
        assert "retry_db_op(_mark_failed_once" in mark_src
        tree = ast.parse(mark_src)
        # _mark_failed_once 是 _mark_section_failed 内的嵌套函数
        nested = any(
            isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name == "_mark_failed_once" for n in ast.walk(tree))
        assert nested, "UPDATE+commit 必须收敛为可重试闭包"

    def test_d2_facts_repair_resync_passes_dropped_out(self):
        src = _src("routers/sse_handlers.py")
        pos = src.index("resync_inline_chart_rows(")
        window = src[pos:pos + 400]
        assert "dropped_out=_chart_dropped" in window, \
            "事实修复后重建配对必须透传 dropped_out（R48 删图可观测）"
        # 首建与重建两个入口都透传（防其中一处回退）
        assert src.count("dropped_out=_chart_dropped") >= 2

    def test_d3_persist_extraction_cursor_guards(self):
        persist_src = _func_src(
            _src("services/facts_extractor.py"), "persist_extraction")
        assert "R13：db.execute 返回 None（persist_extraction 作用域事实加载）" \
            in persist_src
        assert "R13：db.execute 返回 None（persist_extraction 共享保护行回查）" \
            in persist_src
        assert persist_src.count("if cur is None") >= 2


# ---------------------------------------------------------------------------
# E 配置安全
# ---------------------------------------------------------------------------
class _FakeRacingKeyFile:
    """模拟「两进程同时通过 exists 检查、另一进程抢先建盘」的密钥文件。

    exists() 首查谎报不存在（两进程竞态窗口），其后如实；
    open("x") 因磁盘已有文件（对方已建）而 FileExistsError；
    read_text 前段返回空串（对方尚未写完），之后交出完整密钥。
    """

    parent = type("P", (), {"mkdir": staticmethod(lambda **kw: None)})()

    def __init__(self, disk_content: str, empty_reads: int = 0):
        self.disk = disk_content
        self._exists_calls = 0
        self._reads = 0
        self._empty_reads = empty_reads

    def exists(self) -> bool:
        self._exists_calls += 1
        return self._exists_calls != 1

    def read_text(self, encoding: str = "utf-8") -> str:
        self._reads += 1
        if self._reads <= self._empty_reads:
            return ""
        return self.disk

    def open(self, mode: str = "r", encoding: str | None = None):
        if "x" in mode:
            raise FileExistsError
        raise AssertionError("竞态失败方不应走写入路径")


class TestCryptoExclusiveCreateAndReliabilityReset:
    def test_e1_winner_path_creates_valid_key(self, tmp_path, monkeypatch):
        key_file = tmp_path / "secret_key.key"
        monkeypatch.setattr(crypto, "_KEY_FILE", key_file)
        k1 = crypto._load_or_create_persistent_key()
        crypto._is_valid_fernet_key(k1) or pytest.fail("首启生成的必须是合法 Fernet 密钥")
        assert key_file.read_text(encoding="utf-8").strip() == k1
        assert crypto._load_or_create_persistent_key() == k1, "二次加载必须幂等"

    def test_e1b_existing_valid_key_returned_untouched(self, tmp_path, monkeypatch):
        from cryptography.fernet import Fernet
        key_file = tmp_path / "secret_key.key"
        existing = Fernet.generate_key().decode()
        key_file.write_text(existing, encoding="utf-8")
        monkeypatch.setattr(crypto, "_KEY_FILE", key_file)
        assert crypto._load_or_create_persistent_key() == existing
        assert key_file.read_text(encoding="utf-8").strip() == existing

    def test_e1c_race_loser_reads_winner_key(self, monkeypatch):
        import builtins

        from cryptography.fernet import Fernet
        winner = Fernet.generate_key().decode()
        fake = _FakeRacingKeyFile(winner, empty_reads=2)
        monkeypatch.setattr(crypto, "_KEY_FILE", fake)
        # 生产代码经内建 open(path, "x") 排他创建——拦截内建 open 使竞态下
        # "x" 创建抛 FileExistsError（磁盘已被另一进程占用），其余调用原样透传
        # （pathlib 的 read_text 走 io.open，不受影响）。
        real_open = builtins.open

        def _racing_open(file, mode="r", *args, **kwargs):
            if file is fake and "x" in mode:
                raise FileExistsError
            return real_open(file, mode, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", _racing_open)
        got = crypto._load_or_create_persistent_key()
        assert got == winner, "竞态失败方必须采用胜出方已落盘密钥（等待写完再读）"

    def test_e1d_invalid_fernet_key_rejected_by_validator(self):
        assert crypto._is_valid_fernet_key("") is False
        assert crypto._is_valid_fernet_key("半截密钥") is False
        from cryptography.fernet import Fernet
        assert crypto._is_valid_fernet_key(Fernet.generate_key().decode()) is True

    def test_e2_reset_reliability_clears_module_state(self):
        import app.services.ai.provider_factory as pf
        assert hasattr(pf, "reset_reliability")
        with pf._provider_reliability_lock:
            pf._provider_reliability["prov|x|url"] = {"ok": 0, "fail": 9}
            pf._det_dead_streak["prov|x|url"] = 3
        pf.reset_reliability()
        assert pf._provider_reliability == {}
        assert pf._det_dead_streak == {}

    def test_e2b_conftest_autouse_wires_reset(self):
        conftest = (Path(__file__).resolve().parent / "conftest.py").read_text(
            encoding="utf-8")
        assert "pf.reset_reliability()" in conftest


# ---------------------------------------------------------------------------
# 导入自证（防上方 noqa 导入被误删后断言失真）
# ---------------------------------------------------------------------------
def test_import_surface_intact():
    import app.services.ai.provider_factory as pf
    assert callable(pf.is_non_retryable_error)
    assert callable(pf.reset_reliability)
    assert callable(pl.resync_inline_chart_rows)
    assert callable(ba._run_single_item)
