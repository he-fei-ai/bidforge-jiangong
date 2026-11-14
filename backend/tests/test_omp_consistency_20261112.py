"""阶段 2 · omp 主 Agent「全文跨节一致性比对 + 统一修复」旁路护栏（2026-11-12）。

覆盖：
  - prompt 强制 read 分段跨节比对 / edit 统一修复、禁 bash、闸门 nudge 只进第 2 轮；
  - 工作区预写（facts / conflicts / plan / 分节只读索引）与分节 id 路径穿越防护；
  - 单进程多轮共享会话 + 回包读回（mock OmpRpcSession，不拉真实 omp）；
  - ``run_fullbook_repair(workspace_agent_fn=...)`` 接线：回调优先、chat 路径逐字不变、
    闸门拒绝时 nudge 透传，四道闸门 / 快照 / save_repair 全部复用；
  - 端点双闸 409 / 同型防重入源码锁 / services 不得 import routers。
"""

import re
import sys

import app.services.ai.omp_consistency_agent as oc
import app.services.consistency_fullbook_repair as fb
import pytest
from app.services.ai.omp_consistency_agent import (
    OmpConsistencyAgent,
    _write_workspace,
    build_consistency_round_prompts,
)

_PLAN = "<!-- §:s1 -->\n# 第一章\n本工程工期 400 天。\n<!-- §:s2 -->\n# 第二章\n本工程工期 500 天。\n"


# ===================== prompt 契约 =====================
def test_prompts_enforce_read_and_unified_edit():
    rounds = build_consistency_round_prompts(section_count=2)
    assert len(rounds) == 2
    first, second = rounds
    # 第 1 轮：READ 工具分段跨节比对，产出 FINDINGS.md，且明确不改整卷
    assert "read" in first and "分段读取" in first and "grep" in first
    assert "禁止把整卷正文塞进单次输出" in first
    assert "FINDINGS.md" in first and "不要修改" in first
    # 第 2 轮：edit 只改 technical-plan.md，且全篇统一（不只改一处）
    assert "edit" in second and "technical-plan.md" in second
    assert "同一主题必须全篇统一" in second
    assert "保留全部" in second and "只读索引，禁止修改" in second
    # 2026-11-13 · 放开命令执行：两轮提示词改为"bash 仅限隔离工作区内"而非"禁止 shell"
    for r in rounds:
        assert "bash" in r and "网络" in r and "工作区外路径" in r and "业务数据库" in r


def test_prompts_repair_nudge_only_in_second_round():
    rounds = build_consistency_round_prompts(section_count=1, repair_nudge="闸门 B：缺章")
    assert "闸门 B：缺章" not in rounds[0]
    assert "闸门 B：缺章" in rounds[1]
    assert "结构问题" in rounds[1]


# ===================== 工作区预写 =====================
def test_write_workspace_files_and_section_index(tmp_path):
    n = _write_workspace(
        str(tmp_path),
        workspace_text=_PLAN,
        facts="工期：450 天",
        conflicts_digest="[c1] 工期：权威值 450 天",
        section_count=2,
    )
    assert n == 2
    assert (tmp_path / "global-facts.md").read_text(encoding="utf-8") == "工期：450 天"
    assert (tmp_path / "conflicts.md").read_text(encoding="utf-8") == "[c1] 工期：权威值 450 天"
    assert (tmp_path / "technical-plan.md").read_text(encoding="utf-8") == _PLAN
    task = (tmp_path / "TASK.md").read_text(encoding="utf-8")
    assert "共 2 个末级章节" in task and "唯一允许修改" in task and "章节ID" in task
    s1 = (tmp_path / "sections" / "s1.md").read_text(encoding="utf-8")
    assert "<!-- §:s1 -->" in s1 and "400 天" in s1
    assert "500 天" not in s1  # 分节索引按 marker 正确切开
    assert (tmp_path / "sections" / "s2.md").exists()


def test_write_workspace_skips_unsafe_section_id(tmp_path):
    # marker 内的 ../ 不能拼进文件名（路径穿越防护）
    text = "<!-- §:../evil -->\n坏数据\n<!-- §:s1 -->\n好数据\n"
    n = _write_workspace(str(tmp_path), workspace_text=text, facts="", conflicts_digest="", section_count=1)
    assert n == 1
    assert (tmp_path / "sections" / "s1.md").exists()
    assert not (tmp_path.parent / "evil.md").exists()
    assert not list((tmp_path / "sections").glob("*evil*"))


# ===================== 三道闸门 =====================
def test_check_ready_disabled_raises():
    agent = OmpConsistencyAgent(enabled=False)
    with pytest.raises(RuntimeError, match="未启用"):
        agent.check_ready()


def test_check_ready_missing_binary_raises():
    agent = OmpConsistencyAgent(enabled=True, binary="definitely_not_real_xyz", model="fake/model")
    with pytest.raises(RuntimeError, match="可执行文件不存在"):
        agent.check_ready()


def test_check_ready_missing_model_raises():
    agent = OmpConsistencyAgent(enabled=True, binary=sys.executable, model="")
    with pytest.raises(RuntimeError, match="未配置模型"):
        agent.check_ready()


# ===================== 单进程多轮 + 回包读回 =====================
async def test_run_consistency_check_shared_session_and_readback(tmp_path, monkeypatch):
    calls: list = []

    class _FakeSession:
        def __init__(self, *args, **kwargs):
            calls.append(("init", kwargs.get("workspace"), kwargs.get("tools")))

        async def start(self):
            calls.append("start")

        async def run_prompt(self, text):
            calls.append(("prompt", text[:24]))
            # 模拟第 2 轮 Agent 用 edit 把两章统一改成权威值
            if sum(1 for c in calls if isinstance(c, tuple) and c[0] == "prompt") == 2:
                (tmp_path / "technical-plan.md").write_text(
                    "<!-- §:s1 -->\n# 第一章\n本工程工期 450 天。\n"
                    "<!-- §:s2 -->\n# 第二章\n本工程工期 450 天。\n",
                    encoding="utf-8",
                )
                (tmp_path / "FINDINGS.md").write_text("- 工期：400/500 → 450 天", encoding="utf-8")
            return "done"

        async def stop(self, cleanup_workspace=True):
            calls.append(("stop", cleanup_workspace))

    monkeypatch.setattr(oc, "OmpRpcSession", _FakeSession)
    agent = OmpConsistencyAgent(
        enabled=True,
        binary=sys.executable,
        model="fake/model",
        workspace_root=str(tmp_path),
        keep_workspace=True,
    )
    res = await agent.run_consistency_check(
        workspace_text=_PLAN, facts="工期：450 天", conflicts_digest="c1", section_count=2
    )
    assert res.rounds == 2
    assert "450 天" in res.workspace_text and "400 天" not in res.workspace_text
    assert "工期" in res.findings
    assert calls.count("start") == 1  # 单进程共享，不是每轮拉起
    assert len([c for c in calls if isinstance(c, tuple) and c[0] == "prompt"]) == 2
    assert ("stop", False) in calls  # keep_workspace=True ⇒ 不清理


async def test_run_consistency_check_stops_session_on_failure(tmp_path, monkeypatch):
    """RPC 异常也必须回收子进程（防常驻 omp 进程随失败任务堆积）。"""
    stopped: list = []

    class _FailSession:
        def __init__(self, *a, **kw):
            pass

        async def start(self):
            pass

        async def run_prompt(self, text):
            raise RuntimeError("rpc boom")

        async def stop(self, cleanup_workspace=True):
            stopped.append(cleanup_workspace)

    monkeypatch.setattr(oc, "OmpRpcSession", _FailSession)
    agent = OmpConsistencyAgent(
        enabled=True, binary=sys.executable, model="fake/model", workspace_root=str(tmp_path)
    )
    with pytest.raises(RuntimeError, match="rpc boom"):
        await agent.run_consistency_check(workspace_text=_PLAN, section_count=2)
    assert stopped == [True]


async def test_revise_workspace_is_adapter(tmp_path, monkeypatch):
    class _FakeSession:
        def __init__(self, *a, **kw):
            pass

        async def start(self):
            pass

        async def run_prompt(self, text):
            return "ok"

        async def stop(self, cleanup_workspace=True):
            pass

    monkeypatch.setattr(oc, "OmpRpcSession", _FakeSession)
    agent = OmpConsistencyAgent(
        enabled=True, binary=sys.executable, model="fake/model", workspace_root=str(tmp_path)
    )
    out = await agent.revise_workspace(workspace=_PLAN, facts="f", section_count=2)
    assert out == _PLAN


# ===================== run_fullbook_repair 回调接线（行为实证） =====================
class _FakeDB:
    """整卷链末尾会缓冲「冲突状态回写」SQL；此处只记录不落库。"""

    def __init__(self):
        self.sql: list[str] = []

    async def execute(self, sql, params=()):
        self.sql.append(sql)
        return None

    async def commit(self):
        return None


def _install_fullbook_mocks(monkeypatch, *, chat_calls: list, save_calls: list, agent_calls: list):
    """把整卷链路的外部依赖替换为内存桩，只保留真实的闸门 / 解析 / 装配逻辑。"""
    conflicts = [
        {
            "id": "c1",
            "topic": "工期",
            "conflict_type": "事实冲突",
            "authoritative_value": "450 天",
            "repair_instruction": "统一为 450 天",
            "occurrences": [{"section_id": "s1", "value": "400 天"}],
        }
    ]

    monkeypatch.setattr(
        "app.services.repair_agent.filter_conflicts", lambda cs, **kw: cs
    )

    async def _load_leaf(db, sid):
        return [{"id": "s1", "title": "第一章", "content": "本工程工期 400 天。", "word_budget": 1000}]

    monkeypatch.setattr("app.services.consistency_scanner.load_leaf_sections", _load_leaf)

    async def _snap(db, sid, sections):
        return "snap-1"

    async def _save(db, **kw):
        save_calls.append(kw)
        # run_fullbook_repair 末尾会记日志读取这些统计键
        return {
            "repair_id": "r1", "snapshot_id": "snap-1",
            "items": kw.get("items") or [],
            "repaired": 0, "failed": 1, "skipped": 0, "total_conflicts": 1,
        }

    monkeypatch.setattr(fb.repair_record, "create_snapshot", _snap)
    monkeypatch.setattr(fb.repair_record, "save_repair", _save)
    monkeypatch.setattr(fb, "render", lambda *a, **kw: "")

    async def _chat(messages, **kw):
        chat_calls.append(kw)
        return fb.build_fullbook_workspace(
            facts="工期：450 天", conflicts_digest="c1", sections=[
                {"id": "s1", "title": "第一章", "content": "本工程工期 400 天。"}
            ]
        )

    monkeypatch.setattr(fb, "chat_with_fallback", _chat)
    return conflicts


async def test_fullbook_workspace_agent_fn_used_and_chat_untouched(monkeypatch):
    chat_calls: list = []
    save_calls: list = []
    agent_calls: list = []
    conflicts = _install_fullbook_mocks(monkeypatch, chat_calls=chat_calls, save_calls=save_calls, agent_calls=agent_calls)

    async def _agent_fn(*, workspace, facts, conflicts_digest, section_count, repair_nudge):
        agent_calls.append(
            {"workspace": workspace, "facts": facts, "digest": conflicts_digest,
             "count": section_count, "nudge": repair_nudge}
        )
        return workspace  # 原样回包 → 各章无变化，走 failed 分支（不回写）

    result = await fb.run_fullbook_repair(
        _FakeDB(), scheme_id="sc1", scan_id="scan1", conflicts=conflicts, facts="工期：450 天",
        workspace_agent_fn=_agent_fn,
    )
    assert result["repair_id"] == "r1"
    assert len(agent_calls) == 1, "回调应只调 1 次（首轮即通过闸门 A/B）"
    assert agent_calls[0]["count"] == 1
    assert "450 天" in agent_calls[0]["digest"]
    assert "<!-- §:s1 -->" in agent_calls[0]["workspace"]
    assert chat_calls == [], "注入回调时不得再调 chat_with_fallback"
    assert save_calls and save_calls[0]["mode"] == "fullbook"  # 落库与回滚链复用


async def test_fullbook_default_path_still_uses_chat(monkeypatch):
    chat_calls: list = []
    save_calls: list = []
    agent_calls: list = []
    conflicts = _install_fullbook_mocks(monkeypatch, chat_calls=chat_calls, save_calls=save_calls, agent_calls=agent_calls)
    result = await fb.run_fullbook_repair(
        _FakeDB(), scheme_id="sc1", scan_id="scan1", conflicts=conflicts, facts="工期：450 天"
    )
    assert result["repair_id"] == "r1"
    assert len(chat_calls) == 1, "默认路径（workspace_agent_fn=None）行为逐字不变"
    assert agent_calls == []
    assert save_calls and save_calls[0]["mode"] == "fullbook"


async def test_fullbook_gate_reject_passes_nudge_to_agent(monkeypatch):
    chat_calls: list = []
    save_calls: list = []
    agent_calls: list = []
    conflicts = _install_fullbook_mocks(monkeypatch, chat_calls=chat_calls, save_calls=save_calls, agent_calls=agent_calls)
    n = {"i": 0}

    async def _agent_fn(*, workspace, facts, conflicts_digest, section_count, repair_nudge):
        n["i"] += 1
        agent_calls.append(repair_nudge)
        if n["i"] == 1:
            # 闸门 A：含未知小节 id → 整包拒绝，应触发重问并把原因透传
            return "<!-- §:UNKNOWN -->\n# 幽灵章\n坏回包\n"
        return workspace

    await fb.run_fullbook_repair(
        _FakeDB(), scheme_id="sc1", scan_id="scan1", conflicts=conflicts, facts="工期：450 天",
        workspace_agent_fn=_agent_fn,
    )
    assert n["i"] == 2
    assert agent_calls[0] == ""
    assert "未知或重复小节标记" in agent_calls[1]
    assert chat_calls == []


# ===================== 端点门控 / 源码锁 =====================
async def test_endpoint_disabled_returns_409():
    from app.routers import consistency_repair as cr

    with pytest.raises(Exception) as ei:
        await cr.omp_agent_check("sc1", {}, None)
    assert ei.value.status_code == 409
    assert "未启用" in str(ei.value.detail)


def test_endpoint_and_service_source_locks():
    import inspect

    from app.routers import consistency_repair as cr

    endpoint_src = inspect.getsource(cr.omp_agent_check)
    assert "omp_task_enabled" in endpoint_src and "omp_consistency_enabled" in endpoint_src
    assert "ensure_no_generation_write" in endpoint_src
    assert "workspace_agent_fn=agent.revise_workspace" in endpoint_src
    assert "finally:" in endpoint_src and "_OMP_CONSISTENCY_INFLIGHT.discard" in endpoint_src
    # 端点 docstring 会说明「不触碰 sse_handlers」，逐字 not-in 断言会自咬；
    # 只锁真正的 import 行（AST 判据的代码行等价物）。
    assert not re.search(r"^\s*(?:from|import)\s+\S*sse_handlers", endpoint_src, flags=re.M)

    # services 层不得 import routers（AST 契约同族）
    service_src = inspect.getsource(oc)
    assert "from app.routers" not in service_src
    assert "import app.routers" not in service_src
    # 2026-11-13 · 放开命令执行：模块注释改为"放开命令执行，仅限隔离工作区内"
    assert 'oc._PLAN_FILE' in service_src or "technical-plan.md" in service_src
    assert "放开命令执行" in service_src or "仅限隔离工作区内" in service_src


def test_fullbook_signature_is_additive():
    """回调必须是加法式可选形参，既有调用方零改动。"""
    import inspect

    sig = inspect.signature(fb.run_fullbook_repair)
    assert sig.parameters["workspace_agent_fn"].default is None
