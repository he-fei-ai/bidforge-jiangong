"""全局事实模块 · R75b 后续加固护栏（2026-10-13）

承接 R75 §7 遗留项的三项收口，零行为变更（除 G7 不可达死分支改 fail-closed）：

G6 【跨文件契约硬化】唯一出口权威定义已下沉
     ``services/facts_extractor.fact_name_value``（routers 侧 ``_fact_name_value``
     为薄包装）。本护栏锁住下沉成果不被回流掏空：
     ① routers 顶层不得再持有 ``_RE_BOLD_FACT_LINE`` 正则副本；
     ② 跨模块只读桥接 ``load_resolved_facts_for_scope`` 必须消费
        ``fact_name_value``，不得直调底层原语自建第二份回解（R75 G-C 同族：
        非粗体段落行 name 回解成整段正文顶掉 title 列）；
     ③ 行为级双保险：包装与 services 本体对同一脏语料逐行同值（委托真实生效）。

G7 【batch_resolve 死分支 fail-closed】旧实现 `else: scope, params = "", fact_ids`
     因 :2305 作用域守卫不可达（R75 §2 核实），但结构上是「前置守卫被放松即
     静默跨域」的地雷。现改为显式 400，本护栏锁：
     ① 无作用域拼串形态不得回流（字符串 + AST 双锁 else 分支必 raise）；
     ② 可达入口行为锁：fact_ids 非空 + scheme_id 缺失 → 400。

G8 【盘点脚本只读锁】``artifacts/r75b_g8/inventory_g8.py`` 是历史脏行只读
     盘点工具，锁其 mode=ro 与零写语句形态，防未来被改成推断回填工具。
"""
from __future__ import annotations

import ast
import io
import os
import re
import sys

import pytest
from app.models import BatchResolveRequest
from app.routers.global_facts import batch_resolve
from fastapi import HTTPException

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(APP))
sys.path.insert(0, APP)

GF_PATH = os.path.join(APP, "app", "routers", "global_facts.py")
FE_PATH = os.path.join(APP, "app", "services", "facts_extractor.py")
INVENTORY_PATH = os.path.join(
    os.path.dirname(APP),  # APP=backend → 仓库根
    "artifacts", "r75b_g8", "inventory_g8.py")


def _read(path: str) -> str:
    with io.open(path, encoding="utf-8") as f:
        return f.read()


def _fn_def(src: str, name: str):
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == name:
            return node
    return None


def _called_names(node: ast.AST) -> set:
    out = set()
    for c in ast.walk(node):
        if isinstance(c, ast.Call):
            nm = getattr(c.func, "id", None) or getattr(c.func, "attr", None)
            if nm:
                out.add(nm)
    return out


#: 与 R75 护栏同款脏语料（行为 parity 对照用）
DIRTY_ROWS = [
    {"id": "d1", "title": "旧名称-开挖深度",
     "content": "- **基坑开挖深度**: 12.5m *(根据图纸)*"},
    {"id": "d2", "title": "基坑概况说明",
     "content": "本场区基坑深度约12.5米，采用桩锚支护体系。"},
    {"id": "d3", "title": "工期",
     "content": "- **合同工期**: 90天 ⚠️*(模拟值)*"},
    {"id": "d4", "title": "项目经理", "content": ""},
    {"id": "d5", "title": "总工", "content": None},
]


class TestSingleExitSinkedContract:
    """G6 · 下沉后的跨文件契约锁（防包装被掏空 / 副本回流）。"""

    def test_routers_holds_no_bold_regex_copy(self):
        """routers 顶层不得再 Assign `_RE_BOLD_FACT_LINE`（判据只有一份）。"""
        tree = ast.parse(_read(GF_PATH))
        top_assigns = set()
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        top_assigns.add(t.id)
        assert "_RE_BOLD_FACT_LINE" not in top_assigns, (
            "global_facts.py 顶层回流了粗体行正则副本 —— 同一判据两份实现复活，"
            "services 权威本体（fact_name_value）的修改将不再生效于该副本")

    def test_bridge_consumes_authority_not_bare_extract(self):
        """桥接 load_resolved_facts_for_scope 必须消费 fact_name_value，
        不得直调 extract_value_from_markdown_line 自建第二份回解。"""
        fn = _fn_def(_read(FE_PATH), "load_resolved_facts_for_scope")
        assert fn, "桥接函数不在 facts_extractor.py（护栏锚点漂移）"
        calls = _called_names(fn)
        assert "fact_name_value" in calls, (
            "桥接不再走读路径唯一出口 —— 审核/预检拿到的事实名与用户所见分叉"
            "（R75 G-C 同族缺陷复活）")
        assert "extract_value_from_markdown_line" not in calls, (
            "桥接直调底层原语回解库行 —— 跨模块第二份 name/value 切分回流")

    def test_wrapper_delegates_identical_values_to_services(self):
        """行为级双保险：routers 包装与 services 本体逐行同值（委托真实生效）。"""
        from app.routers import global_facts as gf
        from app.services.facts_extractor import fact_name_value
        for row in DIRTY_ROWS:
            assert gf._fact_name_value(dict(row)) == fact_name_value(dict(row)), (
                f"{row['id']}: routers 包装与 services 权威本体分值 —— 包装已非纯委托")


class TestBatchResolveFailClosedDeadBranch:
    """G7 · 不可达死分支的 fail-closed 处置锁。"""

    def test_unscoped_scope_assembly_never_returns(self):
        """`scope, params = "", fact_ids`（无作用域拼串）形态不得回流。

        ⚠️ 只扫非注释行：源码里 G7 处置注释逐字引用了这个旧形态作反例说明，
        全文扫描会自咬（本轮实踩）；真正的回流必然以可执行代码形态出现，
        配合 B3 的 AST raise 锁形成互补。
        """
        src = _read(GF_PATH)
        fn = _fn_def(src, "batch_resolve")
        assert fn, "batch_resolve 不在 global_facts.py（护栏锚点漂移）"
        code = "\n".join(
            ln for ln in src.splitlines() if not ln.strip().startswith("#"))
        assert not re.search(r'scope\s*,\s*params\s*=\s*""\s*,\s*fact_ids', code), (
            "batch_resolve 回流出「fact_ids 非空但 scope 为空串」的无作用域查询 —— "
            "前置守卫一旦被放松即静默跨域批量确认（R75b G7 地雷复埋）")

    def test_no_scheme_id_branch_raises_not_silently_unscoped(self):
        """AST 锁：`if fact_ids:` 内层 `if scheme_id:` 的 else 必须 raise。"""
        fn = _fn_def(_read(GF_PATH), "batch_resolve")
        outer = next(
            (n for n in ast.walk(fn) if isinstance(n, ast.If)
             and isinstance(n.test, ast.Name) and n.test.id == "fact_ids"),
            None)
        assert outer, "batch_resolve 的 `if fact_ids:` 入口分支消失（护栏锚点漂移）"
        inner = next(
            (n for n in ast.walk(outer) if isinstance(n, ast.If)
             and isinstance(n.test, ast.Name) and n.test.id == "scheme_id"),
            None)
        assert inner, "`if scheme_id:` 作用域分支消失（护栏锚点漂移）"
        assert inner.orelse and any(
            isinstance(s, ast.Raise) for s in inner.orelse), (
            "scheme_id 缺失分支不再是显式 raise —— fail-closed 被改回静默放行")

    async def test_missing_scheme_id_rejected_400(self, db_conn):
        """可达入口行为锁：携带条目而缺作用域 → 400，绝不执行任何查询。"""
        with pytest.raises(HTTPException) as ei:
            await batch_resolve(
                BatchResolveRequest(fact_ids=["f1"], scheme_id=""), db_conn)
        assert ei.value.status_code == 400
        assert "scheme_id" in ei.value.detail


class TestInventoryScriptReadOnly:
    """G8 · 盘点脚本的只读形态锁（防被改成推断回填工具）。"""

    def test_inventory_script_is_read_only(self):
        if not os.path.exists(INVENTORY_PATH):
            pytest.skip("G8 盘点脚本未随仓携带（artifacts 归档缺失）")
        src = _read(INVENTORY_PATH)
        assert "mode=ro" in src, "盘点脚本不再以只读 URI 打开运行库"
        for bad in ("UPDATE ", "INSERT ", "DELETE ", "executescript"):
            # 允许出现在注释行（本护栏说明文字里就有），逐行剔除注释后再判
            code = "\n".join(
                ln for ln in src.splitlines()
                if not ln.strip().startswith("#")
                and not ln.strip().startswith('"""'))
            assert bad not in code, f"盘点脚本出现写语句 {bad!r} —— 只读保证失守"

    def test_caliber1_checks_comment_mark_not_divergence_proxy(self):
        """口径 1 必须**真的调用**注释标记判定，不得用「两口径分叉」当代理。

        代理不等价（2026-10-14 实测）：多行分组行的 ``fact_name_value`` 只取
        首行（``_RE_BOLD_FACT_LINE`` 的取值组 ``(.*)`` 不跨换行），与剥注释
        口径同样分叉，却**根本没有注释可保护** —— 旧版报告据此把 1 行
        （`手动分组`，content 为两行 `**测试事实**` 列表）误述成「G-A 注释
        保护面」，而实际分叉原因是首行截断。两类行处置方向不同（1 无需
        处置 / 1b 属显示口径信息截断），混在一个计数里会让结论说谎。

        ⚠️ 本护栏用 AST 锁**调用与路由**而非**名字在场**：首版断言
        ``"_COMMENT_MARK_RE" in src`` / ``"cal1b" in src``，A/B **M4**（把
        三元路由改回 `cal1.append(r)`）后仍 18 例全绿 —— 定义与报告文字里
        的名字都在，判据却已失效。锚点选宽看似严格，实则放过最该拦的形态。
        """
        if not os.path.exists(INVENTORY_PATH):
            pytest.skip("G8 盘点脚本未随仓携带（artifacts 归档缺失）")
        src = _read(INVENTORY_PATH)
        fn = _fn_def(src, "main")
        assert fn, "main() 消失（护栏锚点漂移）"
        used = [
            c for c in ast.walk(fn)
            if isinstance(c, ast.Call)
            and isinstance(c.func, ast.Attribute)
            and c.func.attr == "search"
            and isinstance(c.func.value, ast.Name)
            and c.func.value.id == "_COMMENT_MARK_RE"
        ]
        assert used, (
            "口径 1 丢失 _COMMENT_MARK_RE.search 调用 —— 退回用「两口径分叉」"
            "当代理，把多行分组行的首行截断重新报成注释保留")
        appended = set()
        for c in ast.walk(fn):
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) \
                    and c.func.attr == "append":
                base = c.func.value
                if isinstance(base, ast.Name):
                    appended.add(base.id)
                elif isinstance(base, ast.IfExp):
                    for b in (base.body, base.orelse):
                        if isinstance(b, ast.Name):
                            appended.add(b.id)
        assert "cal1b" in appended, (
            "口径 1b 未参与分叉路由 —— 无注释分叉行会混进 G-A 保护面统计")


class TestMultiLineRowDisplaysFullContent:
    """R75c（2026-10-14）· 多行容器行显示口径不丢内容。

    生产库唯一命中 `129f36c9`（title=手动分组，content 两行）——
    旧实现 ``fact_name_value`` 用粗体行正则取值（`(.*)` 不跨换行），
    首行匹配即返回 ⇒ 第 2..N 行在界面/正文注入静默截断。修复后：
    多行 content → name 取 title 列、value 回退完整原文。
    """

    def _row(self) -> dict:
        return {
            "id": "129f36c9-3369-4f1d-8b8b-26003db244ee",
            "title": "手动分组",
            "content": "- **测试事实**: 值A\n- **测试事实2**: 值B",
        }

    def test_multi_line_row_value_keeps_all_lines(self):
        from app.services.facts_extractor import fact_name_value
        name, value = fact_name_value(self._row())
        assert name == "手动分组"          # 分组容器：name 用 title 列
        assert value == "- **测试事实**: 值A\n- **测试事实2**: 值B"
        assert "测试事实2" in value and "值B" in value   # 第 2 行不再丢

    def test_multi_line_row_does_not_truncate_to_first_line(self):
        """反向断言：不得退回只回解首行的旧形态。"""
        from app.services.facts_extractor import fact_name_value
        name, value = fact_name_value(self._row())
        assert value != "值A", "多行容器行被截断回首行值 —— 信息丢失回归"

    def test_single_bold_line_unaffected(self):
        """单行粗体行（128/129 行的常态）行为必须逐字不变。"""
        from app.services.facts_extractor import fact_name_value
        assert fact_name_value(
            {"title": "外行名", "content": "- **基坑开挖深度**: 12.5m"}
        ) == ("基坑开挖深度", "12.5m")
        assert fact_name_value(
            {"title": "备注", "content": "基坑深度：12.5m"}
        ) == ("备注", "12.5m")
        assert fact_name_value({"title": "空值", "content": ""}) == ("空值", "")

    def test_authority_fn_has_explicit_multiline_branch(self):
        """静态锁：权威本体必须显式存在多行分支（防实现回流为单行截断）。"""
        import inspect
        from app.services import facts_extractor as _fe
        src = inspect.getsource(_fe.fact_name_value)
        assert 'if "\\n" in stripped' in src, (
            "fact_name_value 丢失多行判定分支 —— 多行容器行退回首行截断")
