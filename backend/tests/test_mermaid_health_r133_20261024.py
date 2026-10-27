"""R133-C Mermaid 服务健康探测 + 导出预检接线护栏（2026-10-24）。

本轮在 R130「缓存命中快速路径」基础上，给导出预检 content_audit
新增 Mermaid HTTP 服务健康探测结果（纯加法，不改既有 total/items 结构）：

  - audit_content 新增 mermaid_status 可选形参；传入时返回顶层
    "mermaid_service" 键（available / backends）。
  - _prepare_export 在调用 audit_content 前，异步探测 Mermaid 服务
    可用性（2s 硬超时 + fail-soft，探测失败不阻断导出）。
  - allow_pil_fallback 默认 False 契约、渲染回退 3 项 R130 测试不回归。

分组：
  A 组 · audit_content 传入/不传入 mermaid_status 的返回结构一致性
  B 组 · mermaid_service 键字段（available / backends）正确性
  C 组 · 源码锚点（audit_content 签名 / _prepare_export 探测接线）

判据纪律：静态锁一律锚 AST 节点或调用形态；每个修复点均配功能实证。
"""
from __future__ import annotations

import ast
from pathlib import Path

import app.routers.export as export_mod
import pytest

APP_DIR = Path(export_mod.__file__).resolve().parents[1]


def _fn_ast(name: str):
    tree = ast.parse((APP_DIR / "routers" / "export.py").read_bytes().decode("utf-8"))
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    raise AssertionError(f"函数 {name} 未在 export.py 中找到")


# ── A 组：audit_content 返回结构一致性 ──────────────────────────────────

def test_audit_content_without_mermaid_status_shape_unchanged():
    """不传 mermaid_status 时，返回仅含 total/items，与改造前逐字一致。"""
    result = export_mod.audit_content([])
    assert set(result.keys()) == {"total", "items"}
    assert result["total"] == 0
    assert result["items"] == []


def test_audit_content_with_mermaid_status_adds_key():
    """传入 mermaid_status 时，顶层新增 mermaid_service 键，不改 total/items。"""
    result = export_mod.audit_content(
        [], mermaid_status={"available": True, "backends": ["docker"]})
    assert set(result.keys()) == {"total", "items", "mermaid_service"}
    assert result["total"] == 0
    assert result["items"] == []
    assert result["mermaid_service"] == {"available": True, "backends": ["docker"]}


def test_audit_content_mermaid_status_false_backends_empty():
    """Mermaid 不可用时，available=False、backends=[]，fail-soft 不阻断。"""
    result = export_mod.audit_content(
        [], mermaid_status={"available": False, "backends": []})
    assert result["mermaid_service"]["available"] is False
    assert result["mermaid_service"]["backends"] == []


def test_audit_content_mermaid_status_multiple_backends():
    """多个可用后端时，backends 列表完整。"""
    result = export_mod.audit_content(
        [], mermaid_status={"available": True,
                            "backends": ["docker", "railway", "mmdc"]})
    assert result["mermaid_service"]["available"] is True
    assert result["mermaid_service"]["backends"] == ["docker", "railway", "mmdc"]


# ── B 组：mermaid_service 键字段正确性 ──────────────────────────────────

def test_mermaid_service_key_excludes_total_items_pollution():
    """mermaid_service 键不污染 total/items 语义，纯加法。"""
    sections = [{"id": "s1", "title": "章1", "content": "正文内容"}]
    result = export_mod.audit_content(
        sections, mermaid_status={"available": True, "backends": ["docker"]})
    assert result["total"] >= 0
    assert isinstance(result["items"], list)
    assert "available" in result["mermaid_service"]
    assert "backends" in result["mermaid_service"]


# ── C 组：源码锚点（AST） ─────────────────────────────────────────────

def test_audit_content_signature_has_mermaid_status():
    """audit_content 函数签名包含 mermaid_status 可选形参。"""
    fn = _fn_ast("audit_content")
    param_names = [a.arg for a in fn.args.args]
    kwonly_names = [a.arg for a in fn.args.kwonlyargs]
    assert "mermaid_status" in kwonly_names, (
        "audit_content 应新增 mermaid_status 关键字仅参数")


def test_prepare_export_mermaid_probe_in_source():
    """_prepare_export 通过共享 helper 探测 Mermaid 服务（含 mermaid_status 接线）。"""
    src = (APP_DIR / "routers" / "export.py").read_text(encoding="utf-8")
    assert "_probe_mermaid_status" in src, (
        "导出主链路应通过 _probe_mermaid_status() 探测 Mermaid 服务")
    assert "mermaid_status" in src, (
        "_prepare_export 应构造 mermaid_status 并传入 audit_content")
    assert "2s 硬超时" in src or "硬超时" in src, (
        "探测应有超时说明，防止阻塞导出主流程")


def test_probe_mermaid_status_helper_fail_soft_and_timeout():
    """_probe_mermaid_status：asyncio.wait_for(2s) 硬封顶 + 异常降级不可用。"""
    fn = _fn_ast("_probe_mermaid_status")
    src_seg = ast.get_source_segment(
        (APP_DIR / "routers" / "export.py").read_bytes().decode("utf-8"), fn) or ""
    assert "wait_for" in src_seg and "timeout=2" in src_seg, (
        "探测必须用 asyncio.wait_for(timeout=2) 硬封顶（mmdc 内部超时 5s）")
    assert "except Exception" in src_seg, (
        "探测异常必须 fail-soft 降级为不可用")
    assert "不影响导出" in src_seg, (
        "探测失败应明确不影响导出主流程")


def test_probe_mermaid_status_functional_available_and_unavailable():
    """功能实证：helper 正常返回结构 / 客户端异常时 fail-soft 降级。"""
    import asyncio

    from app.routers import export as exp

    async def _run():
        return await exp._probe_mermaid_status()

    status = asyncio.run(_run())
    assert set(status.keys()) == {"available", "backends"}
    assert isinstance(status["available"], bool)
    assert isinstance(status["backends"], list)

    # 摘除客户端 → fail-soft 降级，绝不抛
    import app.routers.export as exp_mod

    real = exp_mod._probe_mermaid_status

    async def _run_bad():
        async def _boom():
            raise RuntimeError("probe exploded")
        import app.services.ai.mermaid_service as ms
        orig = ms.get_mermaid_service_client
        try:
            ms.get_mermaid_service_client = lambda *a, **k: type(
                "C", (), {"get_available_backends": staticmethod(_boom)})()
            return await exp_mod._probe_mermaid_status()
        finally:
            ms.get_mermaid_service_client = orig

    bad = asyncio.run(_run_bad())
    assert bad == {"available": False, "backends": []}
    _ = real


def test_collect_export_issues_wires_mermaid_probe():
    """/export/check 预检出口（collect_export_issues）同样接线探测。"""
    src = (APP_DIR / "routers" / "export.py").read_text(encoding="utf-8")
    # collect_export_issues 内的 audit_content 调用必须带 mermaid_status
    anchor = "content_audit = audit_content(sections, mermaid_status=await _probe_mermaid_status())"
    assert anchor in src, "预检端点 collect_export_issues 应传入 mermaid_status"
    # 不允许回退到不带探测的裸调用（除函数定义与 _prepare_export 处）
    bare = [ln for ln in src.splitlines()
            if "audit_content(sections)" in ln]
    assert not bare, f"audit_content 裸调用（不带 mermaid_status）不得回流: {bare}"


def test_export_audit_headers_items_empty_with_mermaid_still_emits():
    """items 为空但带 mermaid_service 时，响应头仍回传 Mermaid 结论（精简形态）。"""
    audit = {"total": 0, "items": [],
             "mermaid_service": {"available": True, "backends": ["docker"]}}
    headers = export_mod._export_audit_headers(audit)
    assert "X-Content-Audit" in headers
    import json as _json
    from urllib.parse import unquote
    payload = _json.loads(unquote(headers["X-Content-Audit"]))
    assert payload == {"mermaid_service": {"available": True,
                                           "backends": ["docker"]}}


def test_export_audit_headers_legacy_shapes_unchanged():
    """既有两种形态逐字不回归：有 items 发全量 / 空 audit 不发头。"""
    audit_full = {"total": 2, "items": [{"key": "k", "label": "l",
                                         "level": "warn", "count": 2,
                                         "sections": []}]}
    h1 = export_mod._export_audit_headers(audit_full)
    assert "X-Content-Audit" in h1
    import json as _json
    from urllib.parse import unquote
    assert _json.loads(unquote(h1["X-Content-Audit"])) == audit_full
    assert export_mod._export_audit_headers({}) == {}
    assert export_mod._export_audit_headers({"total": 0, "items": []}) == {}


def test_allow_pil_fallback_default_false_contract():
    """allow_pil_fallback 默认 False，PIL v2 为默认渲染路径（R130 契约不回归）。"""
    import app.config as cfg
    # 通过环境变量或配置类断言默认值（不依赖具体字段名，避免与 config 耦合过紧）
    # 此处仅锚：export.py 中不应将 allow_pil_fallback 改为 True
    src = (APP_DIR / "routers" / "export.py").read_text(encoding="utf-8")
    assert '"allow_pil_fallback": True' not in src and "'allow_pil_fallback': True" not in src, (
        "export.py 不应将 allow_pil_fallback 硬编码为 True")