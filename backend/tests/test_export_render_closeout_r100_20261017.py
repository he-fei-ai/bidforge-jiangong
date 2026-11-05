"""导出渲染段收口护栏（R100 · 2026-10-17）

本轮在 R99「缓存命中快速路径」拆分的基础上，修复其**拆分继留缺陷**并锁死：
  F1（P0） `_prepare_export_render` 引用 `_settings` 但该名字只存在于原
       `_prepare_export` 函数体（拆分时未随行）→ 正文含远程图片 URL 的方案
       一导出即 NameError → 500。既有测试从未组合「渲染段 + 远程图」，基线全绿
       掩盖了它（R98 已移交、config docstring 亦登记在案）。
  F2（P1） `export_cache_fast_path=False` 回滚开关下，`_prepare_export` 内部
       已跑渲染段，而 `export_docx`/`export_pdf` 缓存未命中分支**无条件**再调
       一次 `_prepare_export_render`（R99 注释声称"此处 if 判定为假"，实际无
       if）→ 图表全量重渲染 / 位图重复下载 / AI 编排重跑，回滚开关不再是
       「逐字回退旧行为」。
  F3（P2） `_prepare_export` 拆分继留死码：`_settings` 悬空 import（F401）、
       `allow_pil_fallback` 死赋值（F841）、`render_stats` 死局部、
       `_get_cached_blocks` 死闭包。
  F4（P3） `_prepare_export_render` 尾部重复的不可达 `return plan`。
  F5（P4） config.py 注释锚点误写 `test_export_closeout_r94_*`（护栏实际在 r99）。

分组：
  A 组 · F1 功能实证（渲染段 + 远程位图下载真跑通）
  B 组 · F2 幂等哨兵功能实证（二次进入零成本 + 全链路 calls==1 + 缓存命中零渲染）
  C 组 · F1/F3/F4 静态锁（模块级 `_settings` 作用域扫描 / 死码不回流 / 不可达尾巴）
  D 组 · F5 注释锚点 + 快速路径接线不回流

判据纪律（AGENTS §5.14/§5.15）：静态锁一律锚 AST 节点或调用形态；
每个修复点均配「触发行为」的功能实证，防静态锁的结构性盲区（R98 M2 教训）。
"""

from __future__ import annotations

import ast
import inspect
import io
import uuid
from pathlib import Path

import app.config as cfg
import app.db as _appdb
import app.routers.export as export_mod
import pytest
from app.db import close_db, get_conn, init_db

APP_DIR = Path(export_mod.__file__).resolve().parents[1]


def _fn_ast(name: str):
    tree = ast.parse((APP_DIR / "routers" / "export.py").read_bytes().decode("utf-8"))
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    raise AssertionError("未找到函数 " + name)


def _imports_aliases(fn) -> set[str]:
    out = set()
    for n in ast.walk(fn):
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                out.add(a.asname or a.name.split(".")[0])
    return out


def _loads(fn) -> set[str]:
    return {n.id for n in ast.walk(fn) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}


def _png_bytes() -> bytes:
    """生成合法 1x1 PNG（渲染段会 PIL verify，桩下载必须回传真图片字节）。"""
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (1, 1), "white").save(buf, format="PNG")
    return buf.getvalue()


def _img_plan(content: str) -> dict:
    """满足 `_prepare_export_render` 直取键的最小 plan（含一个远程 image 块正文）。"""
    return {
        "config": {"ai_image_auto_generate": False},
        "sections": [{"id": "s1", "title": "T", "level": 1, "sort_order": 1, "content": content}],
        "chart_lookup": {},
        "blocks_cache": {},
        "fe_images": {},
        "rendered_bytes": {},
        "image_bytes": {},
    }


# ---------------------------------------------------------------------------
# A 组 · F1 功能实证：渲染段引用 `_settings` 的远程位图下载路径必须真跑通
# ---------------------------------------------------------------------------


class TestRenderSettingsWiredF1:
    async def test_remote_image_download_path_no_nameerror(self, monkeypatch):
        """摘掉 `_prepare_export_render` 内的 `_settings` import 即 NameError → 本例恒红。"""
        url = "https://example.com/a.png"
        plan = _img_plan(f"![配图]({url})\n")
        downloaded: list[str] = []

        async def fake_dl(u: str) -> bytes:
            downloaded.append(u)
            return _png_bytes()

        monkeypatch.setattr(export_mod, "_download_remote_image", fake_dl)
        await export_mod._prepare_export_render(plan)  # 修复前：NameError 直冲
        assert downloaded == [url], "远程 URL 应进入下载出口"
        assert url in plan["image_bytes"], "合法 PNG 应落入 image_bytes"

    async def test_image_max_concurrency_actually_read(self, monkeypatch):
        """`_settings.image_max_concurrency` 必须被渲染段真实读取（getattr 探针）。"""
        seen: list[object] = []

        class _Probe:
            def __getattr__(self, name):
                if name == "image_max_concurrency":
                    seen.append(name)
                    return 3
                return getattr(cfg.settings, name)

        monkeypatch.setattr(cfg, "settings", _Probe())
        # 渲染段内的 `from app.config import settings as _settings` 在 import 时刻
        # 绑定的是补丁后的对象（monkeypatch 于 app.config 模块属性层生效）
        plan = _img_plan("![图](https://example.com/b.png)\n")

        async def fake_dl(u: str) -> bytes:
            return _png_bytes()

        monkeypatch.setattr(export_mod, "_download_remote_image", fake_dl)
        await export_mod._prepare_export_render(plan)
        assert "image_max_concurrency" in seen, "并发上限必须来自 settings 单一事实源"


# ---------------------------------------------------------------------------
# B 组 · F2 功能实证：`_render_done` 幂等哨兵 + 回滚开关全链路
# ---------------------------------------------------------------------------


@pytest.fixture
async def db_ctx(tmp_path, monkeypatch):
    """真实库 + 真实 _prepare_export；桩掉 DOCX 构建与 EXPORTS_DIR（零真实渲染对象）。"""
    _appdb.DB_PATH = tmp_path / "export_r100.sqlite"
    await init_db()
    db = await get_conn()
    pid, sid = uuid.uuid4().hex, uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.execute("INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)", (sid, pid, "s"))
    await db.execute(
        "INSERT INTO sections(id,scheme_id,level,title,sort_order,content)" " VALUES(?,?,1,?,1,?)",
        (uuid.uuid4().hex, sid, "Overview", "正文一段。"),
    )
    await db.commit()

    def fake_build(*args, **kwargs):
        # ✅ R175 · BUG-3：可用判据含 ZIP 完整性校验，桩须产出真实合法 DOCX
        #    （旧截断包会被判「损坏」→ 缓存永远 miss）
        from docx import Document

        Document().save(str(args[0]))
        return {"sections": 1, "tables": 0, "figures": 0}

    monkeypatch.setattr(export_mod, "_build_docx_sync", fake_build)
    monkeypatch.setattr(export_mod, "_build_docx_task", lambda out, prep: (out,))
    monkeypatch.setattr(export_mod, "EXPORTS_DIR", tmp_path)
    try:
        yield db, sid
    finally:
        await close_db()


def _spy_render(monkeypatch):
    """计「渲染段主体真实执行」的次数，**不是进入次数**。

    判据教训（AGENTS §5.15 静态锁盲区同款）：哨兵早退发生在函数体内，
    包裹 `_prepare_export_render` 计次会把「被哨兵吸收的第二次进入」也计上
    （进入 ≠ 花钱）。改锚渲染段**无条件经过且唯一由渲染段调用**的成本点
    `_auto_generate_ai_image_blocks`（`_prepare_export` 取数段不调它）——
    哨兵摘除后二次进入会执行到该点 → 计数 2 → B 组恒红。
    """
    real = export_mod._auto_generate_ai_image_blocks
    calls: list[int] = []

    async def spy(sections, blocks_cache, config, **kw):
        # ✅ R106 适配：本函数新增可选形参 cancel_event（取消登记下传）。计次哨兵
        #    只锚「成本点是否执行到」，与取消无关 —— 原样透传，避免真实调用形态
        #    `cancel_event=_cancel_ev` 落到桩上直接 TypeError。
        calls.append(len(sections))
        return await real(sections, blocks_cache, config, **kw)

    monkeypatch.setattr(export_mod, "_auto_generate_ai_image_blocks", spy)
    return calls


class TestRenderIdempotentF2:
    async def test_second_entry_is_noop(self, monkeypatch):
        """同一 plan 二次直调真实渲染段：第二次必须早退（哨兵生效）。"""
        plan = _img_plan("![图](https://example.com/c.png)\n")
        dl: list[str] = []

        async def fake_dl(u: str) -> bytes:
            dl.append(u)
            return _png_bytes()

        monkeypatch.setattr(export_mod, "_download_remote_image", fake_dl)
        await export_mod._prepare_export_render(plan)
        assert plan.get("_render_done") is True
        await export_mod._prepare_export_render(plan)
        assert dl == ["https://example.com/c.png"], "二次进入不得重复下载"

    async def test_fast_path_disabled_single_render(self, db_ctx, monkeypatch):
        """回滚开关 False：渲染段**主体真实执行**次数必须 == 1（成本计数非进入计数）。"""
        db, sid = db_ctx
        monkeypatch.setattr(cfg.settings, "export_cache_fast_path", False)
        calls = _spy_render(monkeypatch)
        resp = await export_mod.export_docx(sid, {"config": {}}, db)
        assert resp is not None
        assert len(calls) == 1, (
            "禁用快速路径时 _prepare_export 内部已渲染，"
            "导出链路第二次进入必须被幂等哨兵吸收、零二次成本（修复前为 2）"
        )

    async def test_fast_path_enabled_render_only_on_miss(self, db_ctx, monkeypatch):
        """默认开启：未命中渲染 1 次；再导出命中 → 渲染次数不再增加（零渲染命中）。"""
        db, sid = db_ctx
        monkeypatch.setattr(cfg.settings, "export_cache_fast_path", True)
        calls = _spy_render(monkeypatch)
        r1 = await export_mod.export_docx(sid, {"config": {}}, db)
        assert len(calls) == 1
        assert r1.headers.get("X-Cache-Status") == "miss"
        r2 = await export_mod.export_docx(sid, {"config": {}}, db)
        assert r2.headers.get("X-Cache-Status") == "hit"
        assert len(calls) == 1, "缓存命中路径不得触碰渲染段"

    async def test_fetch_segment_does_not_preset_sentinel(self, db_ctx, monkeypatch):
        """开启快速路径时 `_prepare_export` 只取数：返回的 plan 不得预置哨兵，
        否则导出链路的渲染段会被误吸收成永不渲染的假绿。"""
        db, sid = db_ctx
        monkeypatch.setattr(cfg.settings, "export_cache_fast_path", True)
        prep = await export_mod._prepare_export(sid, {"config": {}}, db)
        assert not prep.get("_render_done")


# ---------------------------------------------------------------------------
# C 组 · 静态锁：作用域扫描 + 拆分继留死码不回流
# ---------------------------------------------------------------------------


class TestStaticLocks:
    def test_no_dangling_settings_reference_anywhere(self):
        """模块级扫描：任何函数体内 Load `_settings` 必须自身（或外层闭包可解析地）
        在**同一函数 AST** 内 import —— 抓 F1 同族复发（拆分搬移漏随行 import）。"""
        tree = ast.parse((APP_DIR / "routers" / "export.py").read_bytes().decode("utf-8"))

        # 收集顶层函数；闭包（如 _auto_generate_ai_image_blocks 内的 _gen）按
        # 「其最近的顶层祖先函数有 import」判可达。
        def top_functions(node):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    yield child
                elif isinstance(child, (ast.ClassDef,)):
                    yield from top_functions(child)

        offenders = []
        for fn in top_functions(tree):
            loads = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
            if "_settings" in loads and "_settings" not in _imports_aliases(fn):
                offenders.append(fn.name)
        assert not offenders, f"`_settings` 悬空引用的函数：{offenders}"

    def test_prepare_export_dead_code_not_returning(self):
        """F3：取数段不得回流四类拆分继留死码（import 悬空 / 死赋值 / 死闭包）。"""
        fn = _fn_ast("_prepare_export")
        assert "_settings" not in _imports_aliases(fn), "悬空 import 不得回流"
        assigned = {
            t.id for n in ast.walk(fn) if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name)
        }
        loads = _loads(fn)
        for dead in ("allow_pil_fallback", "render_stats"):
            if dead in assigned:
                assert dead in loads, f"`_prepare_export` 中 {dead} 为死赋值，不得回流"
        nested = {
            n.name for n in ast.walk(fn) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n is not fn
        }
        assert "_get_cached_blocks" not in nested, "死闭包不得回流取数段"

    def test_render_segment_has_own_settings_import(self):
        """F1 修复本体必须在渲染段 AST 内（import 行 + 调用点实参锁不住委派本体）。"""
        fn = _fn_ast("_prepare_export_render")
        assert "_settings" in _imports_aliases(fn)
        assert "_settings" in _loads(fn)

    def test_render_segment_tail_has_no_unreachable_stmt(self):
        """F4：顶层函数体内 Return 之后不得再挂任何兄弟语句（不可达尾巴不回流）。"""
        for name in ("_prepare_export_render", "_prepare_export"):
            fn = _fn_ast(name)
            body = fn.body
            # 允许 docstring 形态；从末尾往前找最后一个 Return，其后的语句必须仅为
            # 空（Expression 裸字符串 docstring 除外）。
            last_return = max((i for i, st in enumerate(body) if isinstance(st, ast.Return)), default=None)
            if last_return is None:
                continue
            trailing = [
                st
                for st in body[last_return + 1 :]
                if not (
                    isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant) and isinstance(st.value.value, str)
                )
            ]
            assert not trailing, f"{name} Return 之后存在不可达语句"

    def test_ruff_clean_on_touched_files(self):
        """ruff F821/F401/F841 对触碰文件必须零告警（与本轮验收同判据）。"""
        import subprocess
        import sys

        root = APP_DIR.parent
        rc = subprocess.run(
            [
                sys.executable,
                "-m",
                "ruff",
                "check",
                str(root / "app" / "routers" / "export.py"),
                str(root / "app" / "config.py"),
                "--select",
                "F821,F401,F841,F811,E9",
            ],
            cwd=str(root),
            capture_output=True,
            text=True,
        )
        assert rc.returncode == 0, rc.stdout + rc.stderr


# ---------------------------------------------------------------------------
# D 组 · 注释锚点与快速路径接线
# ---------------------------------------------------------------------------


class TestWiringAndAnchors:
    def test_config_anchor_points_to_r99_guard(self):
        src = (APP_DIR / "config.py").read_bytes().decode("utf-8")
        assert "test_export_closeout_r99_20261017.py" in src
        assert "test_export_closeout_r94_*" not in src, "错误锚点不得回流"

    def test_export_chains_call_render_after_cache_query(self):
        """F2 接线前提：两条链路的 `_prepare_export_render` 调用必须发生在
        `X-Cache-Status": "hit`` 返回**之后**（命中即零渲染的顺序锁）。"""
        for name in ("export_docx", "export_pdf"):
            src = inspect.getsource(getattr(export_mod, name))
            hit_idx = src.find('"hit"')
            render_idx = src.find("_prepare_export_render(")
            assert hit_idx != -1, f"{name} 缺少缓存命中分支"
            assert render_idx != -1, f"{name} 渲染段调用不得被摘除"
            assert hit_idx < render_idx, f"{name}：渲染段必须先于缓存命中返回（R99 快速路径顺序）"

    def test_sentinel_keys_are_explicit(self):
        """哨兵键名锁定：`_render_done` 的读写点缺一不可（防改名半边失配）。"""
        src = (APP_DIR / "routers" / "export.py").read_bytes().decode("utf-8")
        assert 'plan.get("_render_done")' in src
        assert 'plan["_render_done"] = True' in src
        assert src.count('"_render_done"') >= 2
