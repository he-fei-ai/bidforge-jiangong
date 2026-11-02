"""R155-O · 目录生成模块遗留收口护栏（2026-11-01）。

承 R149-O §八遗留清单 + R154 收口后剩余面（L-2/L-3/L-4/L-5/L-10/L-11/L-12
已由 R153/R154/并行会话收口）。本轮修 3 项：

- A 组：**L-1 Agent 分支统计接线**。旧实现只并 last_p ⇒ `_prog` 的 phase 恒
  "prepare"、nodes 恒 0，统计面板全程「准备中 · 0/0」。现 flow 侧经
  ``stage_ref`` 共享字典实时同步当前工作流阶段，stream 三构造器给 progress
  事件携带 ``workflow_stage``，SSE 消费侧 ``_enrich_agent_progress_event``
  映射进度阶段 + 实时计数节点（纯函数、可单测）。
- B 组：**L-9-D3 accept_agent_outline 剥标题内嵌编号**（剥空保留原文）——
  与 legacy 链「生成即 normalize」同口径；此前提示词明令但无程序侧强制，
  「第一章 工程概况」一路进 completed 事件/工作区/审核上下文（中间态双重
  编号、审核语义基于脏标题），落库才收敛。
- C 组：**L-3-tail upload save-as-outline 补终态 checkpoint 清理** —— R154
  已在 `_save_outline_to_db`（save-outline / apply-and-save 共同出口）接线，
  本接口是第三条整表重建路径（同族平行入口回扫，R149 FIX-J 同型）。
"""
import inspect
import json

import app.routers.sse_handlers as sh
import pytest
from app.routers.upload_outline import save_as_outline
from app.services.agent.outline_agent_flow import _OUTLINE_FLOW_STAGES
from app.services.agent.outline_identity import accept_agent_outline


# ===========================================================================
# A 组：L-1 Agent 分支统计接线
# ===========================================================================
class TestAgentStatsWiring:
    def test_stage_phase_mapping_covers_all_flow_stages(self):
        """映射必须双向完备：Agent 全部工作流阶段都有映射，值都在阶段模型内。"""
        assert set(sh._AGENT_STAGE_TO_PHASE) == set(_OUTLINE_FLOW_STAGES), (
            sh._AGENT_STAGE_TO_PHASE)
        assert set(sh._AGENT_STAGE_TO_PHASE.values()) <= set(sh._OUTLINE_PHASE_MODEL)

    def test_enrich_merges_last_p_monotonically(self):
        prog = {"last_p": 0.1, "started_at": 0.0}
        sh._enrich_agent_progress_event(
            {"event": "progress", "progress": 0.5}, prog, {})
        assert prog["last_p"] == 0.5
        # 单调不倒退：更小的 progress 不回退 last_p（修复前既有行为）
        sh._enrich_agent_progress_event(
            {"event": "progress", "progress": 0.3}, prog, {})
        assert prog["last_p"] == 0.5
        # progress=None（activity 事件）不动 last_p
        sh._enrich_agent_progress_event(
            {"event": "progress", "progress": None, "message": "x"}, prog, {})
        assert prog["last_p"] == 0.5

    def test_enrich_maps_stage_and_counts_nodes(self):
        prog = {"last_p": 0.1, "started_at": 0.0}
        holder = {"outline": [
            {"title": "甲", "children": [
                {"title": "乙", "children": []}]},
            {"title": "丙", "children": []},
        ]}
        out = sh._enrich_agent_progress_event(
            {"event": "progress", "progress": 0.5,
             "workflow_stage": "children_generation"}, prog, holder)
        assert prog["phase"] == "sublevels"
        assert prog["nodes"] == 3
        assert out["phase_label"] == "生成二三级目录"
        assert out["phase"] == "sublevels"
        # outline_review → review
        out2 = sh._enrich_agent_progress_event(
            {"event": "progress", "progress": 0.9,
             "workflow_stage": "outline_review"}, prog, holder)
        assert prog["phase"] == "review"
        assert out2["phase_label"] == "目录审核中"

    def test_enrich_unknown_stage_and_non_progress_passthrough(self):
        prog = {"last_p": 0.1, "phase": "draft", "started_at": 0.0}
        sh._enrich_agent_progress_event(
            {"event": "progress", "progress": 0.2,
             "workflow_stage": "zzz"}, prog, {})
        assert prog["phase"] == "draft", "未知阶段不得映射"
        stopped = {"event": "stopped", "outline": []}
        assert sh._enrich_agent_progress_event(stopped, prog, {}) is stopped

    def test_wiring_source_anchors(self):
        """静态锚：flow 两回调同步 stage_ref + stream 三构造器携带 + 分支接线。"""
        import app.services.agent.outline_agent_flow as flow
        impl_src = inspect.getsource(flow._run_outline_agent_impl)
        assert impl_src.count('stage_ref["stage"] = str(state.get("stage") or "")') == 2, (
            "impl 层 _emit_activity / _report 两处都必须同步 stage_ref")
        wrapper_src = inspect.getsource(flow.run_outline_agent)
        assert "stage_ref=stage_ref" in wrapper_src
        stream_src = inspect.getsource(flow.outline_agent_event_stream)
        assert stream_src.count('"workflow_stage": str(stage_ref.get("stage") or "")') == 3, (
            "stream 三个 progress 构造器都必须携带 workflow_stage")
        gen_src = inspect.getsource(sh.generate_outline)
        assert "_enrich_agent_progress_event(\n                            _agent_ev, _prog, _partial_holder)" in gen_src


# ===========================================================================
# B 组：L-9-D3 accept_agent_outline 剥标题内嵌编号
# ===========================================================================
class TestAcceptAgentOutlineStripsNumbering:
    def test_strips_embedded_numbering_recursively(self):
        """「第一章 工程概况」→「工程概况」；多层前缀树档剥净；子节点递归。"""
        tree = accept_agent_outline([
            {"id": None, "title": "第一章 工程概况", "children": [
                {"id": None, "title": "1.2 施工条件", "children": []}]},
            {"id": None, "title": "第1章 1.1 依据", "children": []},
        ])
        assert [n["title"] for n in tree] == ["工程概况", "依据"]
        assert tree[0]["children"][0]["title"] == "施工条件"
        # 身份分配不受影响（新节点获得 UUID）
        import re as _re
        for n in tree:
            assert _re.match(r"^[0-9a-f-]{36}$", n["id"])
        # 编号字段照常
        assert tree[0]["number"] == "1"
        assert tree[0]["children"][0]["number"] == "1.1"

    def test_pure_number_title_kept_verbatim(self):
        """标题本身即编号（「第一章」）剥空保留原文（R85 BUG-D 同口径）。"""
        tree = accept_agent_outline([
            {"id": None, "title": "第一章", "children": []}])
        assert tree[0]["title"] == "第一章"

    def test_plain_title_unchanged(self):
        tree = accept_agent_outline([
            {"id": None, "title": "施工准备", "children": []}])
        assert tree[0]["title"] == "施工准备"

    def test_identity_contract_not_broken(self):
        """反向锚：未知 ID 拒收 / 重复 ID 拒收 / 已知 ID 沿用（身份红线）。"""
        tree = accept_agent_outline(
            [{"id": "known-1", "title": "第一章 甲", "children": []}],
            known_ids={"known-1"})
        assert tree[0]["id"] == "known-1"
        assert tree[0]["title"] == "甲"
        with pytest.raises(ValueError, match="未知 ID"):
            accept_agent_outline([{"id": "alien", "title": "x", "children": []}])
        # 重复 ID：两个已知 id 同树出现（known_ids 预登记，避开「未知 ID」前置闸）
        with pytest.raises(ValueError, match="ID 重复"):
            accept_agent_outline([
                {"id": "dup", "title": "x", "children": []},
                {"id": "dup", "title": "y", "children": []}],
                known_ids={"dup"})


# ===========================================================================
# C 组：L-3-tail upload save-as-outline 补终态 checkpoint 清理
# ===========================================================================
async def _seed_l3(db_conn):
    await db_conn.execute(
        "INSERT OR IGNORE INTO projects (id, name) VALUES ('p155','测试')")
    await db_conn.execute(
        "INSERT OR IGNORE INTO schemes (id, project_id, name) VALUES"
        " ('s155','p155','方案R155')")
    await db_conn.execute(
        "INSERT INTO uploaded_outlines (id, project_id, scheme_id, file_name,"
        " status) VALUES ('up155','p155','s155','x.md','parsed')")
    await db_conn.execute(
        "INSERT INTO sections (id, scheme_id, project_id, title, parent_id,"
        " sort_order, level, status, outline_json, content) VALUES"
        " ('sec155','s155','p155','总体实施策划','',0,1,'empty',?,'正文。')",
        (json.dumps({"id": "1", "level": 1}, ensure_ascii=False),))
    # 终态任务：completed + outline_result checkpoint（应被清理）
    await db_conn.execute(
        "INSERT INTO task_registry (id, task_type, project_id, scheme_id,"
        " status, checkpoint_json) VALUES ('t-done','outline_generation',"
        " 'p155','s155','completed',?)",
        (json.dumps({"kind": "outline_result", "outline": [{"title": "旧树"}]},
                    ensure_ascii=False),))
    # 运行中任务：partial checkpoint（绝不清——断线重挂唯一恢复源）
    await db_conn.execute(
        "INSERT INTO task_registry (id, task_type, project_id, scheme_id,"
        " status, checkpoint_json) VALUES ('t-run','outline_generation',"
        " 'p155','s155','running',?)",
        (json.dumps({"kind": "outline_partial", "outline": [{"title": "半成品"}]},
                    ensure_ascii=False),))
    # 终态但非目录成果（content_generation 等，不清）
    await db_conn.execute(
        "INSERT INTO task_registry (id, task_type, project_id, scheme_id,"
        " status, checkpoint_json) VALUES ('t-other','content_generation',"
        " 'p155','s155','completed',?)",
        (json.dumps({"kind": "content_result"}, ensure_ascii=False),))
    await db_conn.commit()


class TestUploadSaveClearsCheckpoints:
    async def test_terminal_outline_checkpoint_cleared_running_kept(
            self, db_conn):
        """整表重建后：终态 outline_result 清空、running partial 保留、非目录
        成果不动；响应加法式回传 cleared_checkpoints。"""
        await _seed_l3(db_conn)
        result = await save_as_outline(
            "up155", {"scheme_id": "s155", "outline": [
                {"title": "总体实施策划", "children": []}]}, db=db_conn)
        assert result.get("ok"), result
        assert result.get("cleared_checkpoints") == 1, result.get(
            "cleared_checkpoints")
        cur = await db_conn.execute(
            "SELECT checkpoint_json FROM task_registry WHERE id='t-done'")
        row = await cur.fetchone()
        cleared = json.loads(row["checkpoint_json"] or "{}")
        assert not cleared.get("outline"), "终态目录成果必须被清掉"
        cur = await db_conn.execute(
            "SELECT checkpoint_json FROM task_registry WHERE id='t-run'")
        kept = json.loads((await cur.fetchone())["checkpoint_json"] or "{}")
        assert kept.get("kind") == "outline_partial", "运行中 partial 绝不清"
        cur = await db_conn.execute(
            "SELECT checkpoint_json FROM task_registry WHERE id='t-other'")
        other = json.loads((await cur.fetchone())["checkpoint_json"] or "{}")
        assert other.get("kind") == "content_result", "非目录成果不动"

    def test_wiring_source_anchor(self):
        """静态锚：upload 入口必须接线清理函数（防平行入口再次漂移）。"""
        import inspect

        import app.routers.upload_outline as upl
        src = inspect.getsource(upl.save_as_outline)
        assert "_clear_outline_checkpoints_for_scheme" in src
        assert 'result["cleared_checkpoints"] = _cleared_checkpoints' in src
