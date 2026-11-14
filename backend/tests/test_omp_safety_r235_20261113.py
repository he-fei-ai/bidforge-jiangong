"""R235 · OMP 沙箱铁律 fail-closed 与可观测闭环护栏（2026-11-13）。

覆盖《专项方案编制软件引入 oh-my-pi 底层 Agent 引擎.md》§8「安全与治理」与
§10 测试矩阵「安全」行（越界写 / 禁工具 / 禁网 / 密钥读取拒绝）：

* **A 工具白名单 fail-closed**：空白名单（= 不传 ``--tools`` = 放开 omp 全部内置
  工具，含 bash）与含命令执行 / 网络工具的白名单一律判不通过；
* **B 执行期闸门**：``OmpTaskAgent.check_ready`` 第四道闸门 + ``OmpRpcSession.start``
  子进程启动前兜底 —— 非法白名单在**拉起 omp 之前**就抛错；
* **C 路径越界**：工作区路径判据单一事实源（``omp_safety.resolve_workspace_path``），
  ``..`` / Windows 盘符 / 空路径一律拒绝，写读两侧同判据；
* **D 红线覆盖**：三角色 system prompt 必须含禁命令 / 禁网 / 工作区隔离 /
  禁业务库 / **禁读 .env 与密钥** 五条红线（R234 报告引文有、代码里缺 → 本轮补）；
* **E 静态扫仓**：``omp_*`` / ``ompi/*`` 服务模块零业务库访问、零网络 import、
  零 routers 反向依赖（分层约束）；
* **F 可观测闭环**：诊断 / 安全 / 指标三个端点的契约（只读、无副作用）。

全程不拉起真实 omp 二进制、不访问业务库。
"""

from __future__ import annotations

import ast
import asyncio
import os
from pathlib import Path

import pytest
from app.services.ai.omp_safety import (
    FORBIDDEN_TOOLS,
    NETWORK_TOOLS,
    SAFE_DEFAULT_TOOLS,
    assert_tool_allowlist,
    check_tool_allowlist,
    missing_redlines,
    resolve_workspace_path,
    safety_summary,
)

# ===================== A. 工具白名单 fail-closed =====================


def test_default_allowlist_is_safe():
    v = check_tool_allowlist(SAFE_DEFAULT_TOOLS)
    assert v["ok"] is True
    assert v["forbidden"] == [] and v["network"] == []


@pytest.mark.parametrize("tool", ["bash", "exec", "eval", "shell", "powershell", "cmd"])
def test_command_tools_allowed(tool):
    """2026-11-13 · 放开命令执行：bash/exec/eval 等命令工具不再被拦。"""
    v = check_tool_allowlist(f"read,{tool}")
    assert v["ok"] is True
    assert tool not in v["forbidden"]


@pytest.mark.parametrize("tool", ["web_search", "github", "browser", "curl"])
def test_network_tools_rejected(tool):
    v = check_tool_allowlist(f"read,{tool}")
    assert v["ok"] is False
    assert tool in v["network"]


def test_empty_allowlist_is_not_ok():
    """空白名单 = 不传 ``--tools`` = 放开 omp 全部内置工具（含 bash）⇒ 必须不通过。"""
    v = check_tool_allowlist("")
    assert v["ok"] is False
    assert v["problems"]


def test_assert_tool_allowlist_raises_with_reason():
    """放开命令执行后，改为验证网络工具仍被拒绝（eval 等命令工具已放开）。"""
    with pytest.raises(ValueError) as exc:
        assert_tool_allowlist("read,curl")
    assert "curl" in str(exc.value)


def test_assert_tool_allowlist_returns_allowed_list():
    assert assert_tool_allowlist("read,write") == ["read", "write"]


def test_tool_list_is_case_and_space_insensitive():
    v = check_tool_allowlist(" READ , Write ")
    assert v["ok"] is True and v["allowed"] == ["read", "write"]


# ===================== B. 执行期 fail-closed 闸门 =====================


def _agent_with(monkeypatch, **kwargs):
    import app.services.ai.omp_task_agent as agent_mod

    monkeypatch.setattr(agent_mod, "binary_exists", lambda path: True)
    return agent_mod.OmpTaskAgent(enabled=True, binary="omp", model="fake/model", **kwargs)


def test_task_agent_allows_command_tools(monkeypatch):
    """2026-11-13 · 放开命令执行：bash 白名单不再被 Agent 闸门拦截。"""
    agent = _agent_with(monkeypatch, tools="read,bash")
    agent.check_ready()  # 不抛


def test_task_agent_rejects_empty_allowlist(monkeypatch):
    agent = _agent_with(monkeypatch, tools=" ")
    with pytest.raises(RuntimeError, match="白名单"):
        agent.check_ready()


def test_task_agent_rejects_network_tools(monkeypatch):
    agent = _agent_with(monkeypatch, tools="read,web_search")
    with pytest.raises(RuntimeError, match="白名单"):
        agent.check_ready()


def test_task_agent_accepts_safe_allowlist(monkeypatch):
    agent = _agent_with(monkeypatch, tools="write,read,edit,grep,glob")
    agent.check_ready()  # 不抛


def test_task_agent_gate_is_wired_in_source():
    import inspect

    from app.services.ai.omp_task_agent import OmpTaskAgent

    src = inspect.getsource(OmpTaskAgent._assert_ready)
    assert "assert_tool_allowlist" in src


def test_rpc_start_allows_command_tools_before_spawn(monkeypatch):
    """2026-11-13 · 放开命令执行：bash 白名单不再被 RPC 启动前拦截。

    只验证「走到拉起子进程那一步」——子进程本身用哨兵异常拦截，绝不真拉 omp。
    """
    import app.services.ai.omp_rpc as rpc_mod

    class _Sentinel(Exception):
        pass

    async def _boom(*args, **kwargs):
        raise _Sentinel("reached-spawn")

    monkeypatch.setattr(rpc_mod.asyncio, "create_subprocess_exec", _boom)
    session = rpc_mod.OmpRpcSession("omp", "fake/model", "", workspace=os.getcwd(), tools="read,bash")
    with pytest.raises(_Sentinel):
        asyncio.run(session.start())


def test_rpc_start_allows_empty_tools_for_provider_role(monkeypatch):
    """Provider 角色刻意不挂工具（``tools=""`` ⇒ 不传 ``--tools``）⇒ 不应被拦。

    只验证「走到拉起子进程那一步」——子进程本身用哨兵异常拦截，绝不真拉 omp。
    """
    import app.services.ai.omp_rpc as rpc_mod

    class _Sentinel(Exception):
        pass

    async def _boom(*args, **kwargs):
        raise _Sentinel("reached-spawn")

    monkeypatch.setattr(rpc_mod.asyncio, "create_subprocess_exec", _boom)
    session = rpc_mod.OmpRpcSession("omp", "fake/model", "", workspace=os.getcwd(), tools="")
    with pytest.raises(_Sentinel):
        asyncio.run(session.start())


def test_preflight_uses_single_source_allowlist():
    """诊断侧与执行期必须共用同一份禁用清单（禁止两份副本漂移）。

    2026-11-13 · 放开命令执行后，改用 curl（网络工具）验证禁用清单仍生效。
    """
    from app.services.ai.omp_diagnostics import FORBIDDEN_TOOLS as DIAG_LIST
    from app.services.ai.omp_diagnostics import preflight

    assert DIAG_LIST is FORBIDDEN_TOOLS
    # 命令工具已放开 — bash 是安全的
    rep_ok = preflight(enabled=False, binary="x", model="m", tools="read,bash")
    assert rep_ok.checks.get("tools_safe") is True
    # 网络工具仍被拦截
    rep_bad = preflight(enabled=False, binary="x", model="m", tools="read,curl")
    assert rep_bad.checks.get("tools_safe") is False


# ===================== C. 路径越界（单一事实源） =====================


def test_resolve_workspace_path_inside(tmp_path):
    target = resolve_workspace_path(str(tmp_path), "sections/a.md")
    assert os.path.commonpath([str(tmp_path), target]) == str(tmp_path)


@pytest.mark.parametrize("bad", ["../evil.md", "a/../../evil.md", "..\\evil.md"])
def test_resolve_rejects_parent_escape(tmp_path, bad):
    with pytest.raises(ValueError):
        resolve_workspace_path(str(tmp_path), bad)


def test_resolve_rejects_windows_absolute(tmp_path):
    with pytest.raises(ValueError):
        resolve_workspace_path(str(tmp_path), "C:\\Windows\\evil")


def test_resolve_rejects_empty(tmp_path):
    with pytest.raises(ValueError):
        resolve_workspace_path(str(tmp_path), "")


def test_workflow_write_and_read_share_same_guard(tmp_path):
    from app.services.ai.omp_workflow import _read_output, _write_file

    fp = _write_file(str(tmp_path), "sections/a.md", "内容")
    assert os.path.isfile(fp)
    assert _read_output(str(tmp_path), "sections/a.md") == "内容"
    with pytest.raises(ValueError):
        _write_file(str(tmp_path), "../evil.md", "x")
    with pytest.raises(ValueError):
        _read_output(str(tmp_path), "../evil.md")
    assert not (tmp_path.parent / "evil.md").exists()


# ===================== D. 红线覆盖（提示词层） =====================


def test_role_prompts_cover_all_redlines():
    from app.services.ai.omp_agent_roles import (
        AUDIT_ROLE,
        CONTENT_ROLE,
        OUTLINE_ROLE,
        SYSTEM_PREAMBLE,
    )

    assert missing_redlines(SYSTEM_PREAMBLE) == []
    for role in (OUTLINE_ROLE, CONTENT_ROLE, AUDIT_ROLE):
        assert missing_redlines(role.system_prompt) == []


def test_secret_redline_present_in_preamble():
    """R234 报告引文含「不读 .env / 密钥」，代码里此前**缺** ⇒ 本轮补齐。"""
    from app.services.ai.omp_agent_roles import SYSTEM_PREAMBLE

    assert ".env" in SYSTEM_PREAMBLE
    assert "密钥" in SYSTEM_PREAMBLE


def test_missing_redlines_detects_absent_marker():
    """2026-11-13 · 放开命令执行后，no_command 已从红线清单移除。

    仅验证仍在红线清单中的四项（禁网 / 工作区隔离 / 禁业务库 / 禁密钥读取）。
    """
    missing = missing_redlines("禁止访问网络；只在本工作区内工作；不得访问业务数据库；")
    assert "no_secret_read" in missing
    assert "no_command" not in missing  # 已移除，不再是强制红线


def test_safety_summary_reports_command_policy():
    """用户裁决（2026-11-13）：放开命令运行约束 ⇒ 运维面必须能查到策略与放开清单。"""
    from app.services.ai.omp_safety import COMMAND_EXECUTION_POLICY, COMMAND_TOOLS

    summary = safety_summary()
    assert "bash" in summary["command_tools_allowed"]
    assert summary["command_tools_allowed"] == list(COMMAND_TOOLS)
    assert summary["command_execution_policy"] == COMMAND_EXECUTION_POLICY
    assert "allowed" in summary["command_execution_policy"]


def test_command_scope_constraint_stays_in_role_prompt():
    """命令已放开 ⇒「仅限隔离工作区」是唯一作用域软约束，必须在角色提示词里保持在场。"""
    from app.services.ai.omp_agent_roles import (
        AUDIT_ROLE,
        CONTENT_ROLE,
        OUTLINE_ROLE,
        SYSTEM_PREAMBLE,
    )

    assert missing_redlines(SYSTEM_PREAMBLE) == []
    assert "command_scope" not in missing_redlines(SYSTEM_PREAMBLE)
    for role in (OUTLINE_ROLE, CONTENT_ROLE, AUDIT_ROLE):
        assert "command_scope" not in missing_redlines(role.system_prompt)


def test_missing_redlines_detects_absent_command_scope():
    missing = missing_redlines("禁止访问网络；工作区；数据库；.env；")
    assert "command_scope" in missing


def test_safety_summary_reports_redline_coverage():
    summary = safety_summary()
    assert summary["redlines_ok"] is True
    assert summary["forbidden_tools"] == list(FORBIDDEN_TOOLS)
    assert summary["network_tools"] == list(NETWORK_TOOLS)


def test_safety_summary_reports_workspace_secret_files(tmp_path, monkeypatch):
    """密钥泄露前置信号：工作区根目录里躺着 ``.env`` / 密钥类文件要点名。"""
    import app.config as cfg

    (tmp_path / ".env").write_text("SECRET=1", encoding="utf-8")
    (tmp_path / "secret_key.key").write_text("k", encoding="utf-8")
    (tmp_path / "OUTLINE.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(cfg.settings, "omp_workspace_dir", str(tmp_path))
    summary = safety_summary()
    names = summary["secret_like_files"]
    assert any(".env" in n for n in names)
    assert any("secret_key.key" in n for n in names)
    assert not any("OUTLINE.json" in n for n in names)
    assert summary["workspace_root"] == str(tmp_path)


def test_secret_like_paths_ignores_ordinary_files():
    from app.services.ai.omp_safety import secret_like_paths

    assert secret_like_paths(["OUTLINE.json", "sections/a.md"]) == []
    assert secret_like_paths([]) == []


# ===================== E. 静态扫仓（omp 模块零越权） =====================

_FORBIDDEN_IMPORT_ROOTS = {
    "sqlite3",
    "httpx",
    "requests",
    "aiohttp",
    "socket",
    "urllib",
}


def _omp_service_files() -> list[Path]:
    import app.services.ai as ai_pkg

    base = Path(ai_pkg.__file__).parent
    files = sorted(base.glob("omp_*.py"))
    ompi = base / "ompi"
    if ompi.is_dir():
        files += sorted(p for p in ompi.glob("*.py") if p.name != "__init__.py")
    return files


def test_omp_service_modules_exist():
    assert len(_omp_service_files()) >= 10


def test_omp_modules_never_import_db_or_network():
    for path in _omp_service_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            mods: list[str] = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                mods = [node.module or ""]
            for mod in mods:
                root = mod.split(".")[0]
                assert root not in _FORBIDDEN_IMPORT_ROOTS, f"{path.name} 禁止导入 {mod}"
                assert not mod.startswith("app.db"), f"{path.name} 禁止依赖业务库 {mod}"


def test_omp_modules_never_import_routers():
    for path in _omp_service_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            mods: list[str] = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                mods = [node.module or ""]
            for mod in mods:
                assert not mod.startswith("app.routers"), f"{path.name} 禁止反向依赖路由层 {mod}"


def test_omp_modules_never_call_db_execute():
    for path in _omp_service_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in ("execute", "executemany"):
                base = func.value
                name = base.id if isinstance(base, ast.Name) else ""
                assert name not in ("db", "conn", "cur"), (
                    f"{path.name} 禁止直接写业务库（Agent 不写库，平台回写）"
                )


# ===================== F. 可观测闭环（端点契约） =====================


def test_router_paths_registered():
    from app.routers import omp_ops

    paths = {r.path for r in omp_ops.router.routes}
    assert {"/api/v1/omp/diagnostics", "/api/v1/omp/safety", "/api/v1/omp/metrics"} <= paths


@pytest.mark.asyncio
async def test_diagnostics_endpoint_contract():
    from app.routers import omp_ops

    out = await omp_ops.omp_diagnostics()
    assert {"enabled", "ok", "checks", "problems", "snapshot", "safety"} <= set(out.keys())
    assert isinstance(out["enabled"], bool)
    assert out["safety"]["redlines_ok"] is True


@pytest.mark.asyncio
async def test_safety_endpoint_contract():
    from app.routers import omp_ops

    out = await omp_ops.omp_safety()
    assert {"tools", "forbidden_tools", "network_tools", "redlines_missing"} <= set(out.keys())
    assert out["tools"]["ok"] is True


@pytest.mark.asyncio
async def test_metrics_endpoint_cost_and_alarm():
    from app.routers import omp_ops

    out = await omp_ops.omp_metrics(
        input_chars=1000, output_chars=500, input_per_1k=1.0, output_per_1k=2.0, threshold=1.0
    )
    assert out["rate_known"] is True
    assert out["estimated_cost"] == pytest.approx(2.0)
    assert out["alarm"] is True and out["alarm_message"]


@pytest.mark.asyncio
async def test_metrics_endpoint_zero_rate_is_unknown():
    from app.routers import omp_ops

    out = await omp_ops.omp_metrics(input_chars=1000, output_chars=500)
    assert out["rate_known"] is False
    assert out["alarm"] is False


@pytest.mark.asyncio
async def test_metrics_post_aggregates_task_results():
    from app.routers import omp_ops

    out = await omp_ops.omp_metrics_post(
        body={
            "results": [
                {"status": "completed", "attempts": 1, "fix_rounds": 0, "payload": "ab"},
                {"status": "paused", "attempts": 3, "fix_rounds": 2, "payload": ""},
            ]
        }
    )
    assert out["metrics"]["tasks"] == 2
    assert out["metrics"]["completed"] == 1
    assert out["metrics"]["paused"] == 1
    assert out["metrics"]["output_chars"] == 2
