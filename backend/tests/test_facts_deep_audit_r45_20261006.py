"""全局事实模块 · 深度审计护栏（R45 · 2026-10-06）

本轮针对「全局事实」路由与分类链路的定向深审，锁定 4 个真实缺陷。
测试基建与既有护栏一致：内存 SQLite（conftest.db_conn）+ 源码/AST 静态锁。

G1  【P1】改值不重派生 chapter / fact_attr —— **同一判据两处各自实现**
     · ``_apply_item_updates``（条目级更新）判据只认 cat_changed / title_changed
     · ``update_fact`` 的 ``_carry_dimensions``（分组重建）判据只认 category / name
     而 ``classify_fact_attr`` 的输入正是 ``(name, value)`` —— 把定性描述改成
     具体参数（「按设计要求」→「8.5」）时 fact_attr 永远停在 qualitative；
     章节归属同理。下游按属性/章节统计与「按章精选」全部用错口径，
     而界面上毫无异常提示（改值本身返回 200）。
     ⚠️ 只修其中一份，另一份的缺陷会继续存活 —— 故两路径各配一条静态锁。

G3  【P2】``batch_resolve`` 的缓存失效作用域靠「推导」，与 ``batch_ack_stale`` 分叉
     · batch_resolve：``if prow:`` 按项目 / ``else:`` 按方案
     · batch_ack_stale：无条件 ``_invalidate_fact_scope_cache(db, "", real_pid)``
     ``schemes.project_id`` 为空串（NOT NULL 但允许空串）时 batch_resolve 退化为
     双空作用域 → 静默 no-op：已确认的事实改动一个缓存都不失效、
     ``schemes.facts_updated_at`` 不推进，「事实已变更」标记永久停在旧值。

G4  【P2】前端「AI 调整事实」是全仓唯一的 ``window.prompt``（阻塞式原生对话框）
     其余 57 处均走 antd ``modal.confirm``。

G2  【回归锁】``/adjust`` 的可操作 fact_id 集合必须与「读给 AI 的事实」同一来源
     （曾出现 valid_ids 只按 scheme 作用域填充、项目级调用时为空集 →
      update/delete 全部被当「幻觉 id」静默丢弃，而 summary 仍声称已修改 N 条）。
"""
from __future__ import annotations

import ast
import io
import os
import re
import sys

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(APP))
sys.path.insert(0, APP)

FRONTEND_SRC = os.path.join(os.path.dirname(APP), "frontend", "src")

NAME = "基坑开挖深度"          # 章节归属 overview（已实测）
CAT = "tech_param"             # CATEGORY_TITLES 合法键
OLD_Q = "按设计要求"            # classify_fact_attr → qualitative
NEW_Q = "8.5"                  # classify_fact_attr → quantitative


# ---------------------------------------------------------------- 测试基建 --
async def _insert_fact(db, *, fid="f1", pid="p1", sid="", gid="g1",
                       name=NAME, value=OLD_Q, category=CAT):
    """走模块自身的单一 INSERT 出口建行，避免列清单漂移。"""
    from app.routers import global_facts as gf
    await db.execute(
        gf.MANUAL_FACT_INSERT_SQL,
        gf._manual_fact_row(
            fid=fid, pid=pid, sid=sid, group_id=gid,
            group_title="技术参数", name=name,
            content=gf._build_fact_content(name, value, False),
            category=category, source_file="测试录入",
            is_simulated=False, confidence=1.0, is_resolved=True,
        ))
    await db.commit()
    return fid


async def _row(db, fid):
    cur = await db.execute(
        "SELECT title, content, category, fact_key, fact_type, "
        "chapter, fact_attr, source_kind FROM global_facts WHERE id=?", (fid,))
    r = await cur.fetchone()
    return dict(r) if r else None


def _gf():
    from app.routers import global_facts as gf
    return gf


def _src(name):
    """取 global_facts 模块内某函数的源码（静态锁统一入口）。"""
    import inspect
    return inspect.getsource(getattr(_gf(), name))


def inspect_module_source(module_name: str, func_name: str) -> str:
    """取任意模块内某函数的源码（跨模块静态锁用）。"""
    import importlib
    import inspect
    return inspect.getsource(getattr(importlib.import_module(module_name),
                                     func_name))


def _nested_src(src: str, fn_name: str) -> str:
    """取函数体内某个嵌套闭包的源码（AST 定位，避免正则切错边界）。"""
    lines = src.splitlines(keepends=True)
    tree = ast.parse("".join(lines))
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for n in ast.walk(fn):
            if (isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and n.name == fn_name):
                return "".join(lines[n.lineno - 1: n.end_lineno])
    return ""


async def _rebuild_value(db, group_id: str, value: str = NEW_Q):
    """按 Markdown 列表重建一个分组（group_id 不存在于行 id，直接命中重建路径）。"""
    from app.models import FactGroupUpdate
    from app.routers import global_facts as gf
    await gf.update_fact(
        fact_id=group_id,
        data=FactGroupUpdate(id=group_id, title="技术参数",
                             content=f"- **{NAME}**: {value}",
                             category=CAT),
        db=db)


async def _col_by_group(db, group_id: str, col: str):
    """按分组读出重建后唯一行的某一列（重建会换新行 id，故不能按 id 查）。"""
    cur = await db.execute(
        f"SELECT {col} FROM global_facts WHERE group_id=?", (group_id,))
    rows = await cur.fetchall()
    assert rows, f"重建后库内无 group_id={group_id} 的事实行（结果丢失）"
    return dict(rows[0])[col]


async def _attr_by_group(db, gid):
    return await _col_by_group(db, gid, "fact_attr")


async def _chapter_by_group(db, gid):
    return await _col_by_group(db, gid, "chapter")


# ============================================================ G1 · 条目级更新
class TestValueChangeRerendersItemUpdate:
    """G1-a：条目级更新路径的「是否重派生」判据必须含 value_changed。"""

    async def test_value_change_reevaluates_dimensions(self, db_conn):
        from app.routers import global_facts as gf
        from app.services.facts_classification import (
            classify_chapter_from_text, classify_fact_attr)

        fid = await _insert_fact(db_conn)

        # ⚠️ 样本自校验：新旧值必须真被分类器区分开，否则本用例是空转。
        #    漏写这条，一旦换样本就会静默退化成一视同仁的空断言。
        assert classify_fact_attr(NAME, NEW_Q) != classify_fact_attr(NAME, OLD_Q)

        n, _ = await gf._apply_item_updates(
            db_conn, [{"fact_id": fid, "value": NEW_Q}])
        assert n == 1
        await db_conn.commit()

        after = await _row(db_conn, fid)
        assert NEW_Q in after["content"], "值本身没落库"
        assert after["fact_attr"] == classify_fact_attr(NAME, NEW_Q), (
            "G1 回归：改值后 fact_attr 仍停在旧值（只认分类/改名，不认改值）")
        assert after["chapter"] == classify_chapter_from_text(
            NAME, NEW_Q, after["category"], after["fact_type"],
            after["fact_key"]), "G1 回归：改值后 chapter 未重派生"

    async def test_unchanged_value_keeps_stored_dimensions(self, db_conn):
        """回归保护：值未变时不得把库里的四维标注重写一遍（尊重人工归类）。"""
        from app.routers import global_facts as gf

        fid = await _insert_fact(db_conn)
        await db_conn.execute(
            "UPDATE global_facts SET chapter='manual_marker', "
            "fact_attr='norm' WHERE id=?", (fid,))
        await db_conn.commit()

        # 只提交与库里完全相同的值 → 不属「改值」，四维必须原样保留
        n, _ = await gf._apply_item_updates(
            db_conn, [{"fact_id": fid, "value": OLD_Q}])
        assert n == 1
        await db_conn.commit()

        after = await _row(db_conn, fid)
        assert after["chapter"] == "manual_marker"
        assert after["fact_attr"] == "norm"

    # 静态锁已升级：旧的「本函数内必须出现 value_changed 门控」断言固化了
    # 「门控写在本地」这一错误形态。D1 收口后判据收敛为唯一事实源
    # facts_classification.derivation_inputs_changed，静态锁改由
    # TestDerivationGateSingleSource 统一负责（见本文件下方）。

# =================================================== G1 · 分组重建（同族第二处）
class TestValueChangeRerendersGroupRebuild:
    """G1-b：分组重建路径的 ``_carry_dimensions`` 必须把「值变化」纳入判据。

    ⚠️ 这是本仓反复出现的「同一判据两处各自实现」陷阱：只修 _apply_item_updates
    会让分组重建路径的同类缺陷继续存活（两条路径的注释都声称彼此同口径）。
    """

    async def test_group_rebuild_value_change_reevaluates_attr(self, db_conn):
        from app.services.facts_classification import classify_fact_attr

        await _insert_fact(db_conn, fid="r1", gid="grp1")
        old_attr = (await _row(db_conn, "r1"))["fact_attr"]

        await _rebuild_value(db_conn, "grp1")

        new_attr = await _attr_by_group(db_conn, "grp1")
        assert new_attr == classify_fact_attr(NAME, NEW_Q), (
            f"G1-b 回归：分组内改值后 fact_attr={new_attr!r}，"
            f"期望 classify_fact_attr({NAME!r}, {NEW_Q!r})")
        assert new_attr != old_attr, (
            "G1-b 回归：分组内改值后 fact_attr 停在旧值（判据漏 value）")

    async def test_group_rebuild_unchanged_keeps_stored_dimensions(self, db_conn):
        """回归保护：分组内三要素全未变 → 原样保留库里的四维标注。"""
        await _insert_fact(db_conn, fid="r2", gid="grp2")
        await db_conn.execute(
            "UPDATE global_facts SET chapter='manual_marker' WHERE id=?", ("r2",))
        await db_conn.commit()

        await _rebuild_value(db_conn, "grp2", value=OLD_Q)

        chapter = await _chapter_by_group(db_conn, "grp2")
        assert chapter == "manual_marker", (
            f"回归：三要素未变时不应重派生，chapter 被改写为 {chapter!r}")

    # 静态锁已升级（同 TestValueChangeRerendersItemUpdate）：判据收敛为唯一
    # 事实源后，「本函数内必须出现 old_val == 比较」这一断言本身固化了错误
    # 形态，故删除并由 TestDerivationGateSingleSource 统一负责。


# ============================================================ D1 · 单一事实源
class TestDerivationGateSingleSource:
    """D1 加固（R45）：派生输入变更判据必须是**唯一出口**，两条写路径共用。

    历史：同一个「是否重派生 chapter/fact_attr」的判据在三处各自实现 ——
      1. ``_apply_item_updates`` 的 ``cat_changed or title_changed or value_changed``
      2. ``update_fact._carry_dimensions`` 的三维字面量比较
      3. （2026-09-29 还漏了「改分类」，2026-10-01 还漏了「改名」）
    每次修复只改一份，另一份的缺陷继续存活。现收敛为
    ``facts_classification.derivation_inputs_changed``，本类锁定：

      ① 单一出口存在，且判据本身覆盖分类 / 名称 / 值**三维**；
      ② 两条写路径都必须调用它；
      ③ 两条写路径不得再写各自的字面量比较（防分叉回流）。
    """

    def test_single_source_exists(self):
        from app.services import facts_classification as fc
        assert hasattr(fc, "derivation_inputs_changed")
        src = inspect_module_source("app.services.facts_classification",
                                    "derivation_inputs_changed")
        assert src, "derivation_inputs_changed 源码取不到"
        for dim in ("old_category", "new_category", "old_name", "new_name",
                    "old_value", "new_value"):
            assert dim in src, f"判据未覆盖维度 {dim} —— G1 家族回归"

    def test_normalization_is_stable(self):
        """判据的归一化语义：strip + 分类空值兜底 other + None 安全。"""
        from app.services.facts_classification import (
            derivation_inputs_changed, _norm_fact_category, _norm_fact_text)
        assert _norm_fact_category("") == "other"
        assert _norm_fact_category(None) == "other"
        assert _norm_fact_category("  tech_param  ") == "tech_param"
        assert _norm_fact_text("  8.5  ") == "8.5"
        assert _norm_fact_text(None) == ""
        # 三维全等 → 未变化
        assert not derivation_inputs_changed(
            old_category="tech_param", new_category="tech_param",
            old_name=NAME, new_name=NAME, old_value="8.5", new_value="8.5")
        # 仅值变化 → 变化（G1）
        assert derivation_inputs_changed(
            old_category="tech_param", new_category="tech_param",
            old_name=NAME, new_name=NAME,
            old_value=OLD_Q, new_value=NEW_Q)
        # 仅名称变化 → 变化（2026-10-01）
        assert derivation_inputs_changed(
            old_category="tech_param", new_category="tech_param",
            old_name=NAME, new_name="混凝土浇筑工艺",
            old_value="C30", new_value="C30")
        # 仅分类变化 → 变化（2026-09-29）
        assert derivation_inputs_changed(
            old_category="tech_param", new_category="monitoring",
            old_name=NAME, new_name=NAME, old_value="C30", new_value="C30")
        # 空白差异不算变化
        assert not derivation_inputs_changed(
            old_category=" tech_param", new_category="tech_param",
            old_name=" 基坑开挖深度", new_name="基坑开挖深度",
            old_value=" 8.5 ", new_value="8.5")

    def _call_site_sources(self) -> dict:
        """取两条写路径的源码（_carry_dimensions 是嵌套闭包，需二次切）。"""
        item = _src("_apply_item_updates")
        group = _nested_src(_src("update_fact"), "_carry_dimensions")
        assert group, "未在 update_fact 内找到 _carry_dimensions（嵌套闭包）"
        return {"_apply_item_updates": item, "_carry_dimensions": group}

    def test_both_paths_call_single_source(self):
        # ✅ R57：两条写路径改为调用 _rederive_dimension_columns 单一出口，
        #    derivation_inputs_changed 的调用收敛到该函数内部。
        for fn_name, src in self._call_site_sources().items():
            assert "_rederive_dimension_columns(" in src, (
                f"{fn_name} 未调用单一出口 _rederive_dimension_columns —— D1 分叉回流")

    def test_no_local_literal_gate_remains(self):
        """两条路径不得再写各自的字面量门控（这是三次漏改的根因形态）。"""
        for fn_name, src in self._call_site_sources().items():
            assert "if cat_changed or title_changed" not in src, (
                f"{fn_name} 仍保留本地 `cat_changed or title_changed` 门控")
            assert re.search(r"if\s+old_cat\s*==", src) is None, (
                f"{fn_name} 仍保留本地 `old_cat ==` 字面量比较")
            assert re.search(r"old_val\s*==", src) is None, (
                f"{fn_name} 仍保留本地 `old_val ==` 字面量比较")

    def test_group_rebuild_still_reads_old_value_from_stored_content(self):
        """分组重建的旧值必须从库行 content 回解（不能拿新值自比）。"""
        body = self._call_site_sources()["_carry_dimensions"]
        assert re.search(
            r"extract_value_from_markdown_line\(\s*old\.get\(\"content\"\)",
            body), "分组重建未按旧行 content 回解出旧值"



# ============================================ G3 · batch_resolve 失效作用域
class TestBatchResolveInvalidationScope:
    """G3：失效作用域不得靠「scheme 是否存在」推导，须与 batch_ack_stale 同口径。"""

    def test_no_if_prow_branch_in_invalidation(self):
        src = _src("batch_resolve")
        assert "if prow:" not in src, (
            "G3 回归：失效作用域又改回「if prow → 按项目 / else → 按方案」推导 —— "
            "schemes.project_id 为空串时退化为双空作用域、静默 no-op")
        assert "real_pid" in src
        assert "invalidate_export_cache(db, scheme_id" in src, (
            "缺少项目作用域为空串时的退化分支（必须按方案失效，绝不静默跳过）")

    def test_scope_shape_parity_with_ack_stale(self):
        """两个批量端点的项目级失效调用形态必须一致（口径同源）。"""
        shape = '_invalidate_fact_scope_cache(db, "", '
        peer = "ack_fact_stale_batch"
        if peer not in dir(_gf()):
            # 批量解除过期与单条入口可能合并为同一函数，按真实名字取
            peer = "ack_fact_stale"
        assert shape in _src("batch_resolve"), (
            "batch_resolve 未走统一的项目级失效出口")
        assert shape in _src(peer), (
            f"{peer} 未走统一的项目级失效出口")

    def test_neither_endpoint_guards_on_scheme_row(self):
        """⚠️ 关键对称性：另一批量端点早就不判 prow，batch_resolve 必须跟上。"""
        assert "if prow:" not in _src("ack_fact_stale")
        assert "if prow:" not in _src("batch_resolve")


# ================================================== G2 · /adjust 作用域回归锁
class TestAdjustScopeRegression:
    """G2：可操作 fact_id 集合必须与「读给 AI 的事实」同一来源（已修，防回归）。"""

    def test_valid_ids_derived_from_same_rows(self):
        src = _src("adjust_facts")
        assert 'valid_ids = {r["id"] for r in rows}' in src, (
            "G2 回归：valid_ids 不再直接来自 rows —— 一旦重新引入独立的"
            "「按 scheme 作用域查一遍」判据，项目级调用就会得到空集，"
            "于是 update/delete 全部被当幻觉 id 静默丢弃，而 summary 仍声称已修改")
        assert "if scheme_scope:\n        valid_ids" not in src

    def test_project_scope_without_scope_is_rejected(self):
        """scheme_id / project_id 皆空必须显式拒绝，不得降级成空作用域。"""
        assert "需要 scheme_id 或 project_id" in _src("adjust_facts")


# ===================================================== G4 · 前端对话框口径
class TestFrontendDialogConvention:
    """G4：禁止阻塞式原生对话框（全仓 57 处 modal.confirm，不得回退 window.prompt）。"""

    _FILES = None

    @classmethod
    def _collect(cls):
        if cls._FILES is not None:
            return cls._FILES
        out = []
        if os.path.isdir(FRONTEND_SRC):
            for dirpath, _d, fnames in os.walk(FRONTEND_SRC):
                for fn in fnames:
                    if fn.endswith((".ts", ".tsx")):
                        out.append(os.path.join(dirpath, fn))
        cls._FILES = out
        return out

    def test_frontend_sources_exist(self):
        assert self._collect(), "未找到前端源码目录，护栏空转（需确认仓库结构）"

    def test_no_window_prompt_anywhere(self):
        pat = re.compile(r"window\.prompt\s*\(")
        hits = []
        for p in self._collect():
            for i, line in enumerate(
                    io.open(p, encoding="utf-8").read().splitlines(), 1):
                if pat.search(line) and not line.strip().startswith(("//", "*")):
                    hits.append(f"{os.path.relpath(p, FRONTEND_SRC)}:{i}")
        assert hits == [], (
            "G4 回归：出现阻塞式 window.prompt。它会让整个渲染线程卡死、"
            "在部分容器内被禁用、且无法做二次确认排版；"
            "请用 antd Modal + 受控 Input（参考 modal.confirm 的口径）")

    def test_facts_adjust_entry_is_component_based(self):
        """「AI 调整事实」入口必须是独立的受控状态机组件，而非同步原生对话框。

        R45-G4 最初锚定的是页面里的 `handlePreviewFactsAdjust`。该流程已在 R45-D5
        抽成 `FactsAdjustPanel`（弹层开关态 + 输入文案态在组件内闭环），页面只剩
        一处接线。护栏随之改锚组件本身 —— 判据仍是同一风险：不得回到
        window.prompt，且必须存在「打开弹层 + 输入文案」两个受控状态。

        取区间而不是抓函数体：`export function x({...})` 的参数列表本身就以
        列首的 `}` 结束，按「行首 `}`」截断会停在函数签名那一行（护栏自身
        判据失真的又一例，见 AGENTS.md §5.14）。
        """
        p = os.path.join(FRONTEND_SRC, "pages", "SchemeWorkbenchPage.tsx")
        assert os.path.isfile(p), "未找到 SchemeWorkbenchPage.tsx"
        src = io.open(p, encoding="utf-8").read()
        # 区块以组件上方的说明块起始；两者在文件内各出现一次，取区间不会误切
        marker = "全局事实 · AI 调整面板"
        assert src.count(marker) == 1, (
            f"事实调整区块标记应唯一，实际 {src.count(marker)} 次（区间锚点必须唯一）")
        start = src.index(marker)
        end = src.index("export default function SchemeWorkbenchPage", start)
        body = src[start:end]
        # 组件必须是导出的具名函数：可被单测单独渲染（这是本次抽出换来的价值）
        assert "export function FactsAdjustPanel" in body, (
            "AI 调整事实入口必须是 export function FactsAdjustPanel"
            "（可被单测单独渲染的独立组件）")
        # window.prompt 只查代码行：组件说明块里在讲述「原先用过 window.prompt」
        # 这段历史，按整段文本查会误伤注释（整仓代码行的判据见上一条测试）
        code_lines = [
            ln for ln in body.splitlines()
            if not ln.strip().startswith(("//", "*", "/*"))
        ]
        assert not any("window.prompt" in ln for ln in code_lines), (
            "AI 调整事实入口仍在用 window.prompt")
        # 两个受控状态：打开弹层 + 输入文案（缺一即退化为同步对话框形态）
        assert re.search(r"setAsk\s*\(", body), "缺少「打开弹层」受控状态"
        assert re.search(r"setText\s*\(", body), "缺少「输入文案」受控状态"
        # 页面只能有一处接线：保证状态作用域唯一，切换方案不会串台。
        # 只数代码行 —— 组件上方的说明行里也在提「<FactsAdjustPanel /> 处的
        # preview/apply」，整段文本计数会把它数成第二处接线。
        wires = [
            ln for ln in src.splitlines()
            if "<FactsAdjustPanel" in ln
            and not ln.strip().startswith(("//", "*", "/*", "{/*"))
        ]
        assert len(wires) == 1, (
            f"FactsAdjustPanel 应恰好一处接线，实际 {len(wires)} 处")

