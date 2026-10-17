# -*- coding: utf-8 -*-
"""遗留项收口护栏（R86 · 2026-10-16）

承接 `docs/目录生成模块深度探索与缺陷修复_R85_20261016.md` §7 的 L1–L7，
按「最优方案」分档处理：**能做成不变式的一律做成可执行断言**，确属缺陷的
按最小风险面修。每项锁定「修复点 + 反例 + 接线面」三层。

- **L3** `export_audit.py` 物理复制 `export.py` 的 14 个判据 → 委派单一事实源
- **L2** 展示态编号合法性判据分叉 → 与落库态共用 `numbering.stored_id_parts`
- **L5** `create_section` / `update_section` / `upload_outline.save_as_outline`
      三条写入口零占位闸门 → 统一走 `attach_generic_residual`
- **L6** 排序键三处不一致 +「同级 level 恒等」不变式零断言 → 做成可执行断言
- **L4** 修复轮残留「只报不拒」→ 登记为**有意设计**并加锁，防后人误改
- **L1** `generic_residual` 前端零消费 → 已接线（前端 vitest 覆盖）

⚠️ 判据纪律（AGENTS §5.14/§5.15）：
  · 静态锁一律 **AST 锁真实调用/真实结构**，不锁「源码含字符串」；
  · 每条行为锁都配**反例**，防「判据放宽成恒真」；
  · L6 的不变式断言必须**先证明前提**再断言结论（否则是空转）。
"""
import ast
import json
import re
import uuid

import pytest

from app.models import SectionCreate
from app.routers import schemes as _schemes
from app.routers.sections import _build_tree, create_section
from app.services.ai.heading_templates import (
    format_heading_by_id, outline_number_parts,
)
from app.services.numbering import (
    renumber_section_outline_ids, stored_id_parts,
    stored_id_to_display, stored_id_to_prefix,
)
from app.services.outline_quality import (
    attach_generic_residual, detect_generic_titles_for_write,
)

from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1] / "app"

#: R86 收敛的 14 个副本（L3）
DELEGATED = [
    "_build_chart_type_index", "_find_fallback_code", "_resolve_chart_code",
    "_count_unclosed_chart_fences", "_format_audit_log",
    "_count_child_namespace_subheadings", "_section_sort_key",
    "_detect_duplicate_sections", "_stored_outline_number",
    "_detect_section_number_mismatch", "_detect_body_subheading_namespace_conflict",
    "_detect_stale_cross_references", "_section_number_prefix", "_cn_numeral_to_int",
]

#: `export_audit` 独有的能力（**刻意不收敛**：收敛会丢功能）
AUDIT_ONLY = [
    "_detect_standard_pairing", "_detect_terminology_mix", "_detect_filler_phrases",
    "_detect_schedule_conflicts", "_detect_milestone_issues", "_norm_ref_text",
    "_parse_milestone_date",
]

#: 必须判为非法（会被旧松过滤「抢救」）的存储编号。
#: ⚠️ ``"3.5"``（**字符串**）**不在此列** —— 它整体匹配点分数字路径，
#: 按现行判据就是合法编号（D-8a 只拒 float ``3.5`` / UUID / 0 号段）。
#: 把字符串 "3.5" 当脏数据是本护栏首版的断言错误，已订正为 float 3.5。
GARBAGE_IDS = ["abc1.2", "1.0.2", "1..2", "0.3", "1.2.x", " 3 . 2 ", "", None, 3.5]


def _mod_ast(rel: str) -> ast.Module:
    return ast.parse((APP_DIR / rel).read_bytes().decode("utf-8"))


def _fn(tree: ast.Module, name: str) -> ast.FunctionDef:
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return n
    raise AssertionError(f"function {name} not found")


# ====================================================================
# A 组 · L3：export_audit 判据副本 → 委派单一事实源
# ====================================================================

class TestL3AuditDelegatesToExport:
    """物理副本 = 本仓最高频反模式「同一判据两份副本」：任一侧改动即漂移，
    且漂移无任何测试能发现。"""

    @pytest.mark.parametrize("name", DELEGATED)
    def test_body_is_single_delegating_return(self, name):
        """结构锁：函数体必须**恰为 1 条 Return**，且其值是
        `return _ex.<同名>(...)` —— 副本物理体一旦回流即红。"""
        fn = _fn(_mod_ast("routers/export_audit.py"), name)
        stmts = [s for s in fn.body
                 if not (isinstance(s, ast.Expr)
                         and isinstance(s.value, ast.Constant)
                         and isinstance(s.value.value, str))]
        assert len(stmts) == 1 and isinstance(stmts[0], ast.Return), (
            f"{name} 不再是单行委派（副本回流或被改写）")
        call = stmts[0].value
        assert isinstance(call, ast.Call), f"{name} 未调用 _ex.{name}"
        target = call.func
        assert (isinstance(target, ast.Attribute)
                and target.attr == name
                and isinstance(target.value, ast.Name)
                and target.value.id == "_ex"), (
            f"{name} 的委派目标不是 _ex.{name}")

    def test_delegation_target_exists(self):
        """委派目标必须真实存在于 export（防改名字后静默 AttributeError）。"""
        exp = _mod_ast("routers/export.py")
        for name in DELEGATED:
            _fn(exp, name)

    def test_audit_content_is_not_delegated(self):
        """反向锁：`audit_content` 是本文件的**能力超集**（另加全文级体检），
        收敛会丢功能 ⇒ 必须仍是本文件实现，绝不可一并委派掉。"""
        fn = _fn(_mod_ast("routers/export_audit.py"), "audit_content")
        stmts = [s for s in fn.body
                 if not (isinstance(s, ast.Expr)
                         and isinstance(s.value, ast.Constant)
                         and isinstance(s.value.value, str))]
        assert not (len(stmts) == 1 and isinstance(stmts[0], ast.Return)
                    and isinstance(stmts[0].value, ast.Call)
                    and isinstance(stmts[0].value.func, ast.Attribute)), (
            "audit_content 被收敛成委派 —— 会丢掉本文件独有的全文级体检能力")

    @pytest.mark.parametrize("name", AUDIT_ONLY)
    def test_audit_only_capability_retained(self, name):
        """audit 独有的 7 项能力必须仍在（收敛时最容易顺手删掉的东西）。"""
        _fn(_mod_ast("routers/export_audit.py"), name)

    def test_module_imports_export(self):
        src = (APP_DIR / "routers" / "export_audit.py").read_bytes().decode("utf-8")
        assert "from app.routers import export as _ex" in src, (
            "export_audit 未导入 export（委派会 NameError）")

    def test_delegated_functions_behave_identically(self):
        """行为锁：委派是**薄包装**（非对象 rebinding），故断言**逐项输出相等**
        —— —— 「同一对象」断言会因包装层本身而恒假（首版即踩）。"""
        from app.routers import export, export_audit
        secs = [{"id": "s1", "title": "工程概况", "level": 1, "sort_order": 0,
                 "outline_json": '{"id":"1","level":1}',
                 "content": "# 工程概况\n## 1.1 周边\n无异常。"},
                {"id": "s2", "title": "工程概况", "level": 1, "sort_order": 0,
                 "outline_json": '{"id":"2","level":1}', "content": "# 工程概况"}]
        probes = {
            "_section_sort_key": [secs[0]],
            "_stored_outline_number": secs,
            "_detect_section_number_mismatch": secs,
            "_detect_duplicate_sections": secs,
            "_detect_body_subheading_namespace_conflict": secs,
            "_detect_stale_cross_references": secs,
            "_section_number_prefix": ["第二章 支护", ""],
            "_cn_numeral_to_int": ["二十一", "3", "十"],
            "_count_unclosed_chart_fences": ["```mermaid\nA-->B\n```", "```mermaid\nA-->B"],
            "_count_child_namespace_subheadings": [([{"lvl": 2, "text": "2.1 x"}], "2")],
            "_format_audit_log": [{"total": 1, "items": [], "counts": {"warn": 1}}],
            "_build_chart_type_index": [({("s1", "c1"): "flowchart"}, {"flowchart": 1})],
            "_find_fallback_code": [({"flowchart": [("s1", "c1")]}, "gantt", "s2",
                                     {"flowchart": 1})],
            "_resolve_chart_code": [({"type": "mermaid"}, "s1", "flowchart", {},
                                     {"flowchart": [("s1", "c1")]}, {"flowchart": 1})],
        }
        def _call(fn, args):
            try:
                return ("ok", repr(fn(args)))
            except Exception as e:  # noqa: BLE001 - 错误行为也是行为
                return ("exc", type(e).__name__)

        checked = 0
        for name, args_list in probes.items():
            for args in args_list:
                a = _call(getattr(export_audit, name), args)
                b = _call(getattr(export, name), args)
                assert a == b, f"{name}({args!r}): audit={a} export={b}"
                checked += 1
        assert checked >= len(probes), "探针未覆盖到全部函数"

    def test_audit_content_still_runs(self):
        from app.routers.export_audit import audit_content
        assert audit_content([])["total"] == 0


# ====================================================================
# B 组 · L2：编号合法性判据统一（展示态 ⇔ 落库态）
# ====================================================================

class TestL2NumberingLegalityUnified:
    """旧实现两套判据：落库态 fail-closed，展示态把脏编号**抢救**成
    看似合法的数字 ⇒ 同一份脏数据下「落库正文不套号、导出却渲染出编号」。"""

    @pytest.mark.parametrize("depth", [1, 2, 3, 4, 5])
    def test_canonical_ids_unchanged(self, depth):
        """正向锁：canonical 形态（``level == 编号深度``）两侧输出**逐项恒等**
        —— 本项修复对正常数据零行为变化。"""
        sid = ".".join(str(i) for i in range(1, depth + 1))
        assert outline_number_parts(sid) == [int(p) for p in sid.split(".")], sid
        # 展示态前缀与落库态前缀必须一致（depth>=2 时均为 drop-first 结果）
        if depth >= 2:
            assert stored_id_to_prefix(sid) == ".".join(
                str(i) for i in range(2, depth + 1)), sid

    @pytest.mark.parametrize("gid", GARBAGE_IDS)
    def test_garbage_ids_fail_closed_on_both_sides(self, gid):
        """核心锁：脏编号两侧统一 fail-closed（**不再抢救**）。"""
        assert outline_number_parts(gid) == [], repr(gid)
        assert stored_id_to_prefix(gid) == "", repr(gid)
        assert stored_id_to_display(gid) == "", repr(gid)

    @pytest.mark.parametrize("gid", ["abc1.2", "1.0.2", "0.3", "1.2.x"])
    def test_heading_does_not_fabricate_number(self, gid):
        """端到端：``format_heading_by_id`` 不得为脏编号**编造**章节号，
        只能退回裸标题（与落库态「不套号」同口径）。"""
        out = format_heading_by_id(gid, 2, "T")
        assert out == "T", f"{gid!r} 被渲染成带编号的标题：{out!r}"

    def test_outline_number_parts_consumes_single_source(self):
        """接线锁（AST）：``outline_number_parts`` 必须消费
        ``numbering.stored_id_parts``（防有人改回自持松过滤）。"""
        fn = _fn(_mod_ast("services/ai/heading_templates.py"), "outline_number_parts")
        called = {c.func.id for c in ast.walk(fn)
                  if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
        assert "stored_id_parts" in called, (
            "outline_number_parts 不再消费 numbering.stored_id_parts "
            "⇒ 脏编号重新被「抢救」")

    def test_public_alias_is_the_same_object(self):
        from app.services import numbering
        assert numbering.stored_id_parts is numbering._stored_id_parts

    def test_no_loose_copy_elsewhere_in_backend(self):
        """反向锁：全仓不得再有「逐段 isdigit 挑数字」的自持副本
        （那是本次被消灭的分叉形态）。"""
        hits = []
        for p in (APP_DIR).rglob("*.py"):
            try:
                text = p.read_bytes().decode("utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            tree = ast.parse(text)
            for n in ast.walk(tree):
                if (isinstance(n, ast.ListComp) and n.elt
                        and isinstance(n.elt, ast.Call)
                        and isinstance(n.elt.func, ast.Attribute)
                        and n.elt.func.attr == "isdigit"):
                    hits.append(str(p.relative_to(APP_DIR)))
        assert hits == [], f"仍有松过滤副本：{hits}"


# ====================================================================
# C 组 · L5：四条写入口统一占位闸门（加法式）
# ====================================================================

class TestL5WritePathPlaceholderGate:
    """四条写入口必须共用同一出口；且**只报不拒**（绝不阻断保存）。"""

    @pytest.mark.parametrize("rel", [
        "routers/sections.py", "routers/upload_outline.py",
    ])
    def test_module_imports_gate(self, rel):
        src = (APP_DIR / rel).read_bytes().decode("utf-8")
        assert "attach_generic_residual" in src, f"{rel} 未接入占位闸门"

    @pytest.mark.parametrize("fn_name,rel", [
        ("create_section", "routers/sections.py"),
        ("update_section", "routers/sections.py"),
        ("save_as_outline", "routers/upload_outline.py"),
    ])
    def test_write_entry_calls_gate(self, fn_name, rel):
        """接线锁（AST）：三条此前零闸门的入口必须**真实调用**闸门。"""
        tree = _mod_ast(rel)
        fn = next((n for n in ast.walk(tree)
                   if isinstance(n, ast.AsyncFunctionDef) and n.name == fn_name), None)
        assert fn is not None, f"{fn_name} not found in {rel}"
        calls = [c for c in ast.walk(fn)
                 if isinstance(c, ast.Call)
                 and isinstance(c.func, ast.Name)
                 and c.func.id == "attach_generic_residual"]
        assert calls, f"{fn_name} 未调用 attach_generic_residual（L5 复发）"

    def test_save_outline_uses_single_exit(self):
        """`_save_outline_to_db` 的回传也改由单一出口统一挂载。"""
        src = (APP_DIR / "routers" / "sections.py").read_bytes().decode("utf-8")
        assert 'result["generic_residual"] = generic_hits' not in src, (
            "_save_outline_to_db 回传未收敛到单一出口")

    def test_gate_is_additive_not_rejecting(self):
        """反向锁：闸门**只加键、不改结果**（绝不阻断保存、绝不静默改写）。"""
        base = {"ok": True, "id": "x"}
        out = attach_generic_residual(dict(base),
                                      [{"title": "专项6", "children": []}],
                                      scheme_id="ab" * 4)
        assert out["ok"] is True and out["id"] == "x", out
        assert isinstance(out["generic_residual"], list) and out["generic_residual"]

    @pytest.mark.parametrize("outline", [
        [{"title": "专项施工方案", "children": []}],
        [{"title": "深基坑支护（2）", "children": []}],   # 单样本后缀不报（R79 分工）
        [],
    ])
    def test_clean_outline_adds_no_key(self, outline):
        out = attach_generic_residual({"ok": True}, outline)
        assert "generic_residual" not in out, out

    def test_gate_respects_switch(self, monkeypatch):
        """开关关闭即逐字回到引入前：不检测、不回传该键（零新增配置项）。"""
        from app.config import settings
        monkeypatch.setattr(settings, "outline_check_generic_titles", False,
                            raising=False)
        assert detect_generic_titles_for_write([{"title": "专项6", "children": []}]) == []
        out = attach_generic_residual({"ok": True},
                                      [{"title": "专项6", "children": []}])
        assert "generic_residual" not in out

    def test_gate_never_raises(self, monkeypatch):
        """检测自身异常必须 fail-soft —— **绝不阻断保存**。"""
        import app.services.outline_quality as oq
        monkeypatch.setattr(oq, "detect_generic_titles",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        assert detect_generic_titles_for_write([{"title": "x"}]) == []

    @pytest.mark.asyncio
    async def test_create_section_end_to_end(self, db_conn):
        db = db_conn
        pid = str(uuid.uuid4())
        await db.execute("INSERT INTO projects (id,name) VALUES (?,?)", (pid, "P"))
        sid = str(uuid.uuid4())
        await db.execute(
            "INSERT INTO schemes (id,project_id,name,type) VALUES (?,?,?,?)",
            (sid, pid, "S", "基坑支护与降水"))
        bad = await create_section(scheme_id=sid,
                                   data=SectionCreate(title="专项6"), db=db)
        assert bad["id"], "创建必须成功（闸门只报不拒）"
        assert [h["title"] for h in bad["generic_residual"]] == ["专项6"], bad

        good = await create_section(scheme_id=sid,
                                    data=SectionCreate(title="专项施工方案"), db=db)
        assert "generic_residual" not in good, good


# ====================================================================
# D 组 · L6：排序键一致性与「同级 level 恒等」不变式
# ====================================================================

class TestL6SortKeyInvariants:
    """写侧 ``(sort_order, id)`` 与导出侧 ``(sort_order, level, id)`` 的等价性
    依赖「同级 level 恒等」，而该不变式此前**无任何断言**。"""

    @pytest.mark.asyncio
    async def test_siblings_have_equal_level_after_renumber(self, db_conn):
        """不变式本体：任何树经 renumber 后，**同一父节点下的子节点 level 必相等**
        （renumber 把 level 刷成位置深度 ⇒ 该不变式自愈）。"""
        db = db_conn
        pid = str(uuid.uuid4())
        await db.execute("INSERT INTO projects (id,name) VALUES (?,?)", (pid, "P"))
        sid = str(uuid.uuid4())
        await db.execute(
            "INSERT INTO schemes (id,project_id,name,type) VALUES (?,?,?,?)",
            (sid, pid, "S", "基坑支护与降水"))
        # 故意造「同级 level 不等 + sort_order 并列」的脏数据
        rows = [("c1", "", "A", 5, 1), ("c2", "", "B", 5, 3),
                ("c3", "c1", "A1", 0, 2), ("c4", "c1", "A2", 0, 9)]
        for nid, parent, title, so, lv in rows:
            await db.execute(
                "INSERT INTO sections (id, scheme_id, project_id, parent_id, title,"
                " level, sort_order, status, outline_json, word_budget)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (nid, sid, pid, parent, title, lv, so, "empty",
                 json.dumps({"id": "9.9", "level": lv}), 1500))
        await db.commit()

        from app.routers.sections import renumber_sections_after_reorder
        await renumber_sections_after_reorder(db, sid)

        def _check(nodes):
            if not nodes:
                return          # 空列表无「同级」可言（首版漏判，恒假失败）
            lv = {n["level"] for n in nodes}
            assert len(lv) == 1, f"同级 level 不等（导出/写侧排序键将分叉）：{lv}"
            for n in nodes:
                _check(n.get("children") or [])

        _check(await _build_tree(db, sid))

    @pytest.mark.asyncio
    async def test_write_and_export_sort_keys_agree_on_canonical_tree(self, db_conn):
        """正向锁：canonical 树下写侧与导出侧排序结果**逐项一致**
        （两条排序键在「同级 level 恒等」下等价 —— 这正是本断言的前提）。"""
        from app.routers.export import _section_sort_key
        db = db_conn
        pid = str(uuid.uuid4())
        await db.execute("INSERT INTO projects (id,name) VALUES (?,?)", (pid, "P"))
        sid = str(uuid.uuid4())
        await db.execute(
            "INSERT INTO schemes (id,project_id,name,type) VALUES (?,?,?,?)",
            (sid, pid, "S", "基坑支护与降水"))
        for i, (title, so) in enumerate([("A", 2), ("B", 2), ("C", 1)]):
            await db.execute(
                "INSERT INTO sections (id, scheme_id, project_id, parent_id, title,"
                " level, sort_order, status, outline_json, word_budget)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), sid, pid, "", title, 1, so, "empty",
                 json.dumps({"id": str(i + 1), "level": 1}), 1500))
        await db.commit()
        tree = await _build_tree(db, sid)
        write_order = [n["title"] for n in tree]
        flat = [{**n, "children": []} for n in tree]
        export_order = [s["title"] for s in sorted(flat, key=_section_sort_key)]
        assert write_order == export_order, (write_order, export_order)

    def test_renumber_writes_level_as_depth(self):
        """不变式的**来源**：renumber 必须把 level 刷成位置深度
        （否则该不变式不成立，两侧排序键就真的可能分叉）。"""
        tree = [{"id": "77", "level": 5, "title": "A",
                 "children": [{"id": "88", "level": 9, "title": "A1", "children": []}]}]
        ups = renumber_section_outline_ids(tree)
        assert sorted(u[1] for u in ups) == [1, 2], ups


# ====================================================================
# E 组 · L4：修复轮残留「只报不拒」登记为有意设计
# ====================================================================

class TestL4ResidualIsReportedNotRejected:
    """⚠️ 有意设计登记（不是缺陷）：修复轮采用 AI 的树后若仍有占位标题，
    本仓选择**如实报告**（``passed=False`` + ``generic_residual``）而非
    静默拒收 —— 拒收会把「AI 修了 3 处占位、还剩 1 处」的好成果一并丢弃。
    本组锁死这个契约，防后人「顺手改成拒收」而悄悄改变语义。"""

    SRC = APP_DIR / "routers" / "sse_handlers.py"

    def _src(self) -> str:
        return self.SRC.read_bytes().decode("utf-8")

    def test_both_repair_paths_recheck_residual(self):
        """合并修复轮与独立修复轮**都必须复检**（R73 根因 B 的锁定）。"""
        src = self._src()
        n = len(re.findall(r"_detect_generic_titles\(outline\)", src))
        assert n >= 2, (
            f"复检点只剩 {n} 处（合并轮 + 独立轮各一处，缺一即回归）")

    def test_residual_sets_passed_false_and_generic_residual(self):
        """残留时必须**同时**置 ``passed=False`` 与 ``generic_residual``，
        绝不谎报「已自动修复」。"""
        src = self._src()
        assert src.count('review_obj["passed"] = not _residual') >= 1
        assert src.count('review_obj["generic_residual"] = len(_residual)') >= 1

    def test_no_silent_rejection_in_repair_paths(self):
        """反向锁：两条修复路径里**不得**出现「有残留就丢弃整棵树」的分支
        （那是行为变更，需单独立项 + 显式决策，不得顺手引入）。"""
        src = self._src()
        for marker in ("if _residual:\n                outline = ",
                       "if _residual2:\n                outline = "):
            assert marker not in src, "修复轮出现了未登记的静默拒收分支"


# ====================================================================
# F 组 · 静态卫生与字节体检
# ====================================================================

class TestStaticHygiene:
    TOUCHED = [
        "routers/export_audit.py", "routers/sections.py", "routers/upload_outline.py",
        "services/outline_quality.py", "services/numbering.py",
        "services/ai/heading_templates.py",
    ]

    @pytest.mark.parametrize("rel", TOUCHED)
    def test_byte_health(self, rel):
        raw = (APP_DIR / rel).read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf"), f"{rel} 含 BOM"
        assert raw.count(b"\xef\xbf\xbd") == 0, f"{rel} 含 U+FFFD"
        crlf = raw.count(b"\r\n")
        assert raw.count(b"\r") == crlf, f"{rel} 存在孤立 CR（行尾损坏）"
        ast.parse(raw.decode("utf-8"))

    @pytest.mark.parametrize("rel", TOUCHED)
    def test_no_duplicate_module_level_names(self, rel):
        tree = ast.parse((APP_DIR / rel).read_bytes().decode("utf-8"))
        tops = [n.name for n in tree.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
        assert len(tops) == len(set(tops)), (
            f"{rel} 顶层重名：{[n for n in tops if tops.count(n) > 1]}")

    def test_export_audit_has_no_dead_imports(self):
        """L3 收敛后不得留下死导入（副本函数体是它们此前的唯一使用者）。

        ⚠️ 判定走 **AST 导入节点**，不是源码子串 —— 文件里有一句注释
        明确点名了这些被删的名字（说明「为什么删」），纯文本匹配会自咬
        （首版即踩：AGENTS §5.14「源码含字符串是假判据」同族）。
        """
        tree = _mod_ast("routers/export_audit.py")
        imported: set[str] = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                imported |= {a.asname or a.name.split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom):
                imported |= {a.asname or a.name for a in n.names}
        for dead in ("json", "parse_fence_line", "_cn_pure_to_int",
                     "_compute_subheading", "_parse_content_blocks",
                     "_strip_duplicate_leading_title", "_strip_title_number"):
            assert dead not in imported, f"死导入未清理：{dead}"

        import app.routers.export_audit as ea
        assert ea.audit_content is not None
        assert ea.logger is not None

    @pytest.mark.asyncio
    async def test_duplicate_scheme_still_renumbers(self, db_conn):
        """回归锁：R85 的 BUG-C 修复不得被 R86 的改动破坏。"""
        db = db_conn
        pid = str(uuid.uuid4())
        await db.execute("INSERT INTO projects (id,name) VALUES (?,?)", (pid, "P"))
        sid = str(uuid.uuid4())
        await db.execute(
            "INSERT INTO schemes (id,project_id,name,type) VALUES (?,?,?,?)",
            (sid, pid, "源", "基坑支护与降水"))
        for i, t in enumerate(("A 章", "B 章")):
            await db.execute(
                "INSERT INTO sections (id, scheme_id, project_id, parent_id, title,"
                " level, sort_order, status, outline_json, word_budget)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), sid, pid, "", t, 1, 0, "empty",
                 json.dumps({"id": str(i + 1), "level": 1}), 1500))
        await db.commit()
        new_id = (await _schemes.duplicate_scheme(
            db=db, project_id=pid, scheme_id=sid))["id"]
        tree = await _build_tree(db, new_id)
        assert [json.loads(n.get("outline_json") or "{}").get("id")
                for n in tree] == ["1", "2"]

    def test_stored_id_parts_public_alias_used_by_heading_templates(self):
        """跨模块接线：heading_templates 的导入名必须是公开别名。"""
        src = (APP_DIR / "services" / "ai" / "heading_templates.py")\
            .read_bytes().decode("utf-8")
        assert "stored_id_parts" in src
        assert "ALPHABET" in src and "CHINESE_NUMBERS" in src, (
            "既有导入被误删（heading_templates 的数字字表）")
        assert stored_id_parts("3.2.4") == ["3", "2", "4"]