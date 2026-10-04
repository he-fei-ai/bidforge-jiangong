"""目录生成模块 · 2026-09-26 专项修复回归测试

覆盖本轮 4 处缺陷（每条断言都对应一处代码改动）：

1. **提示词依据错位（{scheme_basis} 静默失效）**
   - 模板自 2026-09-23「四项依据」改造起声明 `{scheme_basis}`（并登记进
     PROMPT_VARIABLE_CONTRACTS），但短方案 / 一级目录两条生成链路从未传值：
     `render_prompt` 对「整行独占占位符」按可选区块处理 → 整行被丢弃，
     模板 0.1 条款（工序/工艺/对象逐项落实）永不生效，且每次生成打
     "unresolved placeholders: ['scheme_basis']" WARNING。
   - 审核链路反而写死 `scheme_basis=construction_scope` → 同一段文字在提示词里
     以两个标签各注入一次（重复 token），且审核员按生成侧从未收到的信息判缺失。

2. **分步生成「停止」丢成果 / 结果缺失被静默当成功**（`_merge_unit_results`）
   - 单元内遇 stopped 直接 break，且 append 发生在 break 之后 →
     同批（OUTLINE_CHAPTER_BATCH_SIZE>1）已生成完的章节被整批丢弃；
   - `j >= len(per)` 静默 continue：章节以空 children 计入完成、失败数为 0。

3. **save-outline 缺 D4 重规范化**（编号顺移后落库正文子标题停在旧号）

4. **整表重建静默清除正文**（返回 cleared_content_sections 量化告知）
"""
import ast
import json

import pytest

import app.routers.sections as sec
import app.routers.sse_handlers as sh
from app.services.ai.prompts._registry import (  # noqa: E402  _is_false_positive 为模块内共享判据
    _is_false_positive, extract_user_variables, get_default_prompt, render,
)
from app.services.numbering import validate_scheme_numbering_consistency


# ---------------------------------------------------------------------------
# 公共脚手架
# ---------------------------------------------------------------------------
async def _seed_scheme(db, sid: str = "sc1", pid: str = "p1") -> None:
    await db.execute("INSERT OR IGNORE INTO projects(id,name) VALUES(?,?)", (pid, "测试项目"))
    await db.execute(
        "INSERT OR IGNORE INTO schemes(id,project_id,name,type,status,config_json)"
        " VALUES(?,?,?,?,?,?)",
        (sid, pid, "基坑支护及土方开挖专项施工方案", "深基坑", "目录已确认", "{}"))


# ===========================================================================
# 1. {scheme_basis} 注入（生成侧补齐 + 审核侧去重）
# ===========================================================================
class TestSchemeBasisInjection:
    SCHEME = {"name": "基坑支护及土方开挖专项施工方案", "type": "深基坑"}

    def test_basis_block_is_labelled_and_carries_dimensions(self):
        from app.services.scheme_basis import parse_scheme_basis
        raw = parse_scheme_basis(self.SCHEME["name"]).prompt_text()
        text = sh._outline_scheme_basis(self.SCHEME)
        assert text, "可解析的方案名称必须产出【方案名称解析】区块"
        assert text.startswith("【方案名称解析"), "区块必须带标签（模板 0.1 条款按该标签引用）"
        assert raw and raw in text, "解析器产出的每一行维度都必须原样注入"
        assert "危大分类" in text, "危大判定是 0.1 条款的输入之一"

    def test_scope_is_content_items_only(self):
        """construction_scope 只放「主要施工内容」，不得混入其它维度。"""
        scope = sh._outline_construction_scope(self.SCHEME)
        assert scope == "基坑支护、土方开挖", scope
        for dim in ("施工工序", "施工工艺", "施工对象", "危大分类"):
            assert dim not in scope, f"{dim} 不属于 construction_scope，会与 scheme_basis 重复注入"

    def test_both_empty_when_switch_off(self, monkeypatch):
        monkeypatch.setattr(sh.settings, "outline_name_basis", False, raising=False)
        assert sh._outline_construction_scope(self.SCHEME) == ""
        assert sh._outline_scheme_basis(self.SCHEME) == ""

    def test_basis_empty_for_unparseable_name(self):
        """名称无任何可解析维度 → 空串（整段不注入，保持「不得编造」红线）。"""
        assert sh._outline_scheme_basis({"name": "", "type": ""}) == ""

    @pytest.mark.parametrize("key,extra", [
        ("outline_short_system", {"standards_text": "JGJ 120", "project_facts": "深度6.5m"}),
        ("outline_level1_system", {"project_brief": "摘要", "project_facts": "深度6.5m",
                                   "reference_outline": "无", "standards_text": "JGJ 120"}),
    ])
    def test_generation_prompts_receive_basis(self, key, extra, caplog):
        """两条生成链路的渲染结果必须含【方案名称解析】（缺陷修复的核心证明）。"""
        scheme = self.SCHEME
        with caplog.at_level("WARNING"):
            out = render(key, scheme_name=scheme["name"], scheme_type=scheme["type"],
                         construction_scope=sh._outline_construction_scope(scheme),
                         scheme_basis=sh._outline_scheme_basis(scheme), **extra)
        assert "【方案名称解析" in out, f"{key} 未注入【方案名称解析】"
        assert "危大分类" in out
        assert "{scheme_basis}" not in out
        assert "unresolved placeholders" not in caplog.text, (
            "修复后不应再出现未解析占位符告警：" + caplog.text)

    @pytest.mark.asyncio
    async def test_review_prompt_no_duplicate_injection(self, monkeypatch):
        """审核提示词：同一段解析文本只注入一次（不再以两个标签各来一遍）。"""
        marker = "施工工序：测量放线、土方开挖"
        basis = ("【方案名称解析（按方案名称字面确定性拆解，章节划分与三级标题须逐项落实，"
                 "不得虚构名称之外的内容）】：\n" + marker)
        seen: dict = {}

        async def fake(messages, validate_fn, **kwargs):
            seen["prompt"] = messages[0]["content"]
            return {"passed": True, "suggestions": []}, ""

        monkeypatch.setattr(sh, "collect_json_response", fake)
        await sh._review_and_fix_outline(
            [{"title": "工程概况", "children": []}], "深基坑", False, "简述",
            scheme_name="基坑支护及土方开挖专项施工方案", construction_scope="基坑支护、土方开挖",
            scheme_basis=basis)
        prompt = seen["prompt"]
        assert prompt.count(marker) == 1, "审核提示词重复注入方案名称解析"
        assert "【方案名称主要施工内容】：基坑支护、土方开挖" in prompt

    def test_review_default_falls_back_to_construction_scope(self):
        """未显式传 scheme_basis 的既有直调方行为不变（向后兼容）。"""
        import inspect
        src = inspect.getsource(sh._review_and_fix_outline)
        assert "if scheme_basis is None:" in src and "scheme_basis = construction_scope" in src



# =========================================================================
# 2. render 调用点 ↔ 模板变量契约 的漂移护栏（系统性防复发）
# =========================================================================
def _resolve_star_expr(expr, tree, visiting: frozenset, depth: int = 0):
    """✅ R38 D7：静态解析 ``render(..., **expr)`` 展开的键集合。

    旧实现（R38 报告 D7）把所有 ``**`` 展开调用点**整体跳过**，造成 5 个
    真实调用点（检查点 kwargs helper / 条件字典字面量）零覆盖。本函数按
    三种在仓形态解析（均为**过近似**：宁可多认键不误报，不可漏报真漏传）：
      · Dict 字面量 / 三元表达式两分支 Dict
      · 同模块函数调用（收集函数体内全部 Dict 字面量键 + 下标赋值键，
        并递归展开其体内调用的已知函数，防环 visited，深度上限 4）
      · 局部变量（收集同文件内该变量的 ``var["k"] =`` 下标赋值键与
        Dict 初始值键 —— 文件级过近似，同名变量宁多勿少）
    无法解析（函数参数 Name / 未知来源）返回 None，由调用方计入盲区快照。
    """
    if depth > 4:
        return None
    if isinstance(expr, ast.Dict):
        return {k.value for k in expr.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)}
    if isinstance(expr, ast.IfExp):
        a = _resolve_star_expr(expr.body, tree, visiting, depth + 1)
        b = _resolve_star_expr(expr.orelse, tree, visiting, depth + 1)
        return None if (a is None or b is None) else (a | b)
    if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name):
        fname = expr.func.id
        fn = next((n for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and n.name == fname), None)
        if fn is None or fname in visiting:
            return set() if fname in visiting else None
        keys: set = set()
        for sub in ast.walk(fn):
            if isinstance(sub, ast.Dict):
                keys |= {k.value for k in sub.keys
                         if isinstance(k, ast.Constant) and isinstance(k.value, str)}
            elif isinstance(sub, ast.Assign):
                for t in sub.targets:
                    if (isinstance(t, ast.Subscript)
                            and isinstance(t.slice, ast.Constant)
                            and isinstance(t.slice.value, str)):
                        keys.add(t.slice.value)
        for sub in ast.walk(fn):
            if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
                    and sub.func.id not in visiting
                    and sub.func.id != fname):
                # 只展开**同模块有定义**的嵌套调用；getattr/bool 等内建或
                # 外部调用不产生 kwargs 键，直接跳过（否则 helper 内部任何
                # 普通函数调用都会把整条解析误判为盲区）
                if not any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                           and n.name == sub.func.id for n in ast.walk(tree)):
                    continue
                inner = _resolve_star_expr(sub, tree,
                                           visiting | {fname}, depth + 1)
                if inner is None:
                    return None
                keys |= inner
        return keys
    if isinstance(expr, ast.Name):
        var = expr.id
        keys = set()
        found = False
        for sub in ast.walk(tree):
            if isinstance(sub, ast.Assign):
                for t in sub.targets:
                    if (isinstance(t, ast.Subscript)
                            and isinstance(t.value, ast.Name)
                            and t.value.id == var
                            and isinstance(t.slice, ast.Constant)
                            and isinstance(t.slice.value, str)):
                        keys.add(t.slice.value)
                        found = True
            if isinstance(sub, ast.AnnAssign) and isinstance(sub.target, ast.Name) \
                    and sub.target.id == var and isinstance(sub.value, ast.Dict):
                keys |= {k.value for k in sub.value.keys
                         if isinstance(k, ast.Constant) and isinstance(k.value, str)}
                found = True
        return keys if found else None
    return None


def _scan_missing_render_kwargs(source: str, filename: str,
                                blindspots: list | None = None) -> list[str]:
    """AST 扫描：render("key", ...) 调用点是否漏传模板声明的用户变量。

    ✅ R38 D7：``**`` 展开不再整体跳过 —— 先走 :func:`_resolve_star_expr`
    静态解析合并进 supplied；真正解不了的才记入 blindspots（由快照护栏
    ``test_dynamic_blindspots_frozen`` 锁死，新增盲区即红）。
    """
    out: list[str] = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "render"):
            continue
        if not node.args or not isinstance(node.args[0], ast.Constant):
            if blindspots is not None:
                blindspots.append(f"{filename} render(<non-literal-key>)")
            continue
        key = node.args[0].value
        if not isinstance(key, str):
            continue
        supplied, dynamic = set(), False
        dynamic_keys: set = set()
        for kw in node.keywords:
            if kw.arg is None:
                resolved = _resolve_star_expr(kw.value, tree, frozenset())
                if resolved is None:
                    dynamic = True      # 确实无法静态判定 → 跳过并登记
                    if blindspots is not None:
                        blindspots.append(f"{filename} render({key}) **<unresolved>")
                else:
                    dynamic_keys |= resolved
            else:
                supplied.add(kw.arg)
        need = set(extract_user_variables(get_default_prompt(key)))
        # 与运行时告警同一判据：JSON 示例误报（{max: ...}）不算漏传
        tpl = get_default_prompt(key)
        need = {v for v in need if not _is_false_positive("", v, tpl)}
        if not dynamic and need - (supplied | dynamic_keys):
            out.append(f"{filename}:{node.lineno} render({key}) "
                       f"缺 {sorted(need - (supplied | dynamic_keys))}")
    return out


class TestRenderCallSiteContract:
    def test_guard_detects_missing_variable(self):
        """护栏自检：能发现漏传（防止护栏自身写成空断言）。"""
        src = 'def f():\n    return render("outline_short_system", scheme_name="x")\n'
        missing = _scan_missing_render_kwargs(src, "snippet.py")
        assert missing and "scheme_basis" in missing[0]

    def test_guard_ignores_dynamic_kwargs(self):
        src = 'def f(kw):\n    return render("outline_short_system", **kw)\n'
        assert _scan_missing_render_kwargs(src, "snippet.py") == []

    def test_guard_resolves_helper_kwargs_and_detects_miss(self):
        """✅ R38 D7：helper 展开的调用点不再被整体跳过 —— 漏传能被发现。"""
        src = (
            'def _mk():\n    return {"a_var": "x"}\n'
            'def f():\n    return render("outline_short_system", '
            'scheme_name="n", scheme_type="t", construction_scope="c", '
            'scheme_basis="b", standards_text="s", project_facts="p", **_mk())\n'
        )
        # 模板需要的真实变量没给全 → 必须报（旧实现遇 ** 展开直接跳过，零覆盖）
        assert _scan_missing_render_kwargs(src, "snippet.py")


class TestRenderDynamicBlindspots:
    """✅ R38 D7：动态展开盲区快照锁 —— 存量无法静态判定的 render 调用点
    必须与登记名单完全一致；新增盲区（任意新 ``**`` 展开/动态 key）即红，
    迫使作者把新调用点写成可静态解析的形态或显式登记豁免理由。"""

    #: 存量盲区（rel → 条数）。均为「键本身动态」而非「kwargs 动态」：
    #: · charts.py —— render(_fix_key, ...) 图表修复变体族选择（已有
    #:   test_json_repair_key_routing / 契约表逐 variant 登记双重兑付）；
    #: · json_response.py —— render(repair_key, ...) 默认/目录族参数，
    #:   取值范围由路由护栏锁死。
    SNAPSHOT: dict = {
        "routers/charts.py render(<non-literal-key>)": 2,
        "services/ai/json_response.py render(<non-literal-key>)": 1,
    }

    def _collect(self):
        import pathlib
        import app as app_pkg
        root = pathlib.Path(app_pkg.__file__).parent
        blind: list = []
        for p in sorted(root.rglob("*.py")):
            rel = str(p.relative_to(root)).replace("\\", "/")
            try:
                src = p.read_text(encoding="utf-8")
            except (FileNotFoundError, OSError):
                continue
            try:
                _scan_missing_render_kwargs(src, rel, blindspots=blind)
            except SyntaxError:
                continue
        return blind

    def test_dynamic_blindspots_frozen(self):
        from collections import Counter
        got = Counter(self._collect())
        assert dict(got) == self.SNAPSHOT, (
            "render 动态盲区与登记快照不一致（新增盲区需改造为可静态解析"
            "形态，或同步登记并说明理由）：\n"
            + "\n".join(f"{k} ×{v}" for k, v in sorted(got.items())))

    def test_five_checkpoint_render_sites_no_longer_blind(self):
        """D7 点名的 5 个零覆盖调用点现已全部被静态解析覆盖。"""
        import pathlib
        import app as app_pkg
        src = (pathlib.Path(app_pkg.__file__).parent
               / "routers" / "sse_handlers.py").read_text(encoding="utf-8")
        blind: list = []
        _scan_missing_render_kwargs(src, "routers/sse_handlers.py",
                                    blindspots=blind)
        assert blind == [], (
            "sse_handlers 的 5 个检查点 render 调用点不应再有盲区：" + str(blind))

    def test_no_render_call_site_misses_variables(self):
        import pathlib
        import time as _time
        import app as app_pkg
        root = pathlib.Path(app_pkg.__file__).parent
        # 关键模块必须真的被扫描到 —— 防止"读不到就跳过"把护栏退化成空断言
        required = {
            "routers/sse_handlers.py", "routers/sections.py",
            "routers/outline_library.py", "routers/upload_outline.py",
            "services/ai/prompts/outline.py", "services/ai/prompts/_registry.py",
            "services/ai/prompts/content.py",
        }
        bad: list[str] = []
        scanned: set = set()
        for p in sorted(root.rglob("*.py")):
            rel = str(p.relative_to(root)).replace("\\", "/")
            src = None
            # ⚠️ 全量套件里偶发 OSError(22)：J 盘写完立即读会被索引器/杀软短暂独占，
            #    其它用例也会在 app/ 树内增删临时文件（rglob 刚列出的路径可能已消失）。
            #    先重试到约 1s；"已消失"属扫描竞态可跳过，其余仍未读到则本文件不算已扫。
            for _attempt in range(10):
                try:
                    src = p.read_text(encoding="utf-8")
                    break
                except FileNotFoundError:
                    break
                except OSError:
                    _time.sleep(0.1)
            if src is None:
                continue
            scanned.add(rel)
            try:
                bad += _scan_missing_render_kwargs(src, rel)
            except SyntaxError:
                continue
        assert not (required - scanned), (
            "护栏未能扫描到关键模块（会退化为空断言）：" + ", ".join(sorted(required - scanned)))
        assert not bad, "提示词变量漏传（会被整行丢弃或残留字面量）：\n" + "\n".join(bad)


# ===========================================================================
# 3. 分步生成结果归位（停止不丢成果 / 缺结果不静默）
# ===========================================================================
class TestMergeUnitResults:
    def _level1(self, n: int) -> list:
        return [{"title": f"第{i}章", "children": []} for i in range(1, n + 1)]

    def test_stopped_mid_unit_keeps_completed_chapters(self):
        """单元内第 2 章被停止 → 第 1 章已完成成果必须保留（缺陷核心）。"""
        level1 = self._level1(2)
        full, prior, failed = [], [], []
        stopped, nodes = sh._merge_unit_results(
            level1, [0, 1],
            [("ok", [{"title": "1.1 概述", "children": []}]), ("stopped", [])],
            full, prior, failed, unit_status="stopped")
        assert stopped is True
        assert [c["title"] for c in full] == ["第1章"], "已完成章节被丢弃 = 数据丢失"
        assert full[0]["children"][0]["title"] == "1.1 概述"
        assert nodes == 1 and prior == ["第1章 / 1.1 概述"] and failed == []

    def test_unit_stopped_with_empty_results_yields_nothing(self):
        """单元整体在发起前被停止（per 为空）：不得把空 children 章当作成功追加。"""
        level1 = self._level1(2)
        full, prior, failed = [], [], []
        stopped, nodes = sh._merge_unit_results(
            level1, [0, 1], [], full, prior, failed, unit_status="stopped")
        assert stopped is True and full == [] and nodes == 0

    def test_missing_results_counted_as_failed(self):
        """模型少回一章 → 按失败记账（此前静默跳过，整批空目录被当成成功）。"""
        level1 = self._level1(2)
        full, prior, failed = [], [], []
        stopped, _ = sh._merge_unit_results(
            level1, [0, 1],
            [("ok", [{"title": "1.1 概述", "children": []}])],  # 第 2 章结果缺失
            full, prior, failed, unit_status="ok")
        assert stopped is False
        assert failed == ["第2章"], "缺失结果必须计入失败章节，前端据此高亮提示"
        assert len(full) == 2, "章标题仍要保留（保留一级骨架便于用户重试）"

    def test_failed_chapter_keeps_empty_children_and_order(self):
        level1 = self._level1(3)
        full, prior, failed = [], [], []
        stopped, nodes = sh._merge_unit_results(
            level1, [0, 1, 2],
            [("ok", [{"title": "1.1", "children": []}]),
             ("failed", []),
             ("ok", [{"title": "3.1", "children": []}])],
            full, prior, failed, unit_status="ok")
        assert stopped is False
        assert [c["title"] for c in full] == ["第1章", "第2章", "第3章"]
        assert full[1]["children"] == []
        assert failed == ["第2章"] and nodes == 2

    def test_malformed_children_coerced_to_list(self):
        level1 = self._level1(1)
        full, prior, failed = [], [], []
        sh._merge_unit_results(level1, [0], [("ok", None)], full, prior, failed)
        assert full[0]["children"] == []


# ===========================================================================
# 4. save-outline：编号顺移后正文子标题同步 + 正文清除量化
# ===========================================================================
async def _seed_sections(db, sid: str, with_content: bool = True) -> None:
    await db.execute(
        "INSERT INTO sections (id, scheme_id, parent_id, title, level, status,"
        " sort_order, outline_json, word_budget)"
        " VALUES (?,?,'','工程概况',1,'empty',0,'',1500)", (f"{sid}-R1", sid))
    await db.execute(
        "INSERT INTO sections (id, scheme_id, parent_id, title, level, status,"
        " sort_order, outline_json, word_budget)"
        " VALUES (?,?,'','施工工艺',1,'empty',1,'',1500)", (f"{sid}-R2", sid))
    if with_content:
        await db.execute(
            "UPDATE sections SET content=?, word_count=100 WHERE id=?",
            ("## 2.1 材料要求\n\n正文A\n", f"{sid}-R2"))
    await sec.renumber_sections_after_reorder(db, sid)
    await db.commit()


class TestSaveOutlineNumberingParity:
    @pytest.mark.asyncio
    async def test_inserted_chapter_renumbers_body_subheadings(self, db_conn):
        """在首部插入一章 → R2 由第 2 章变第 3 章，正文子标题必须同步（3.1）。

        修复前：create/delete/reorder 都调了 _renormalize_all_section_contents，
        唯独 save-outline 整表重建没有 —— 落库正文停在 "## 2.1"，
        只有导出时才重算，导致「前端预览 ≠ 落库正文」且
        /numbering-consistency 长期报漂移。
        """
        await _seed_scheme(db_conn)
        await _seed_sections(db_conn, "sc1")

        before = await validate_scheme_numbering_consistency(db_conn, "sc1")
        assert before["consistent"] is True, "前置条件：初始状态无漂移"

        await sec.save_outline("sc1", {"outline": [
            {"title": "编制依据", "children": []},
            {"title": "工程概况", "__original_id": "sc1-R1", "children": []},
            {"title": "施工工艺", "__original_id": "sc1-R2", "children": []},
        ]}, db_conn)

        cur = await db_conn.execute(
            "SELECT content, outline_json FROM sections WHERE id='sc1-R2'")
        row = await cur.fetchone()
        assert "## 3.1" in (row["content"] or ""), row["content"]
        assert "## 2.1" not in (row["content"] or "")
        assert json.loads(row["outline_json"])["id"] == "3"
        after = await validate_scheme_numbering_consistency(db_conn, "sc1")
        assert after["consistent"] is True, after

    @pytest.mark.asyncio
    async def test_second_save_is_idempotent(self, db_conn):
        """重复保存同一棵树：正文子标题不再被改写（幂等，无副作用）。"""
        await _seed_scheme(db_conn)
        await _seed_sections(db_conn, "sc1")
        outline = [
            {"title": "编制依据", "children": []},
            {"title": "工程概况", "__original_id": "sc1-R1", "children": []},
            {"title": "施工工艺", "__original_id": "sc1-R2", "children": []},
        ]
        await sec.save_outline("sc1", {"outline": json.loads(json.dumps(outline))}, db_conn)
        cur = await db_conn.execute("SELECT content FROM sections WHERE id='sc1-R2'")
        first = (await cur.fetchone())["content"]
        await sec.save_outline("sc1", {"outline": json.loads(json.dumps(outline))}, db_conn)
        cur = await db_conn.execute("SELECT content FROM sections WHERE id='sc1-R2'")
        assert (await cur.fetchone())["content"] == first


class TestClearedContentReporting:
    @pytest.mark.asyncio
    async def test_rebuild_reports_cleared_content(self, db_conn):
        """AI 生成结果（id 为展示编号）整表重建 → 明确回报清除了几章正文。"""
        await _seed_scheme(db_conn)
        await _seed_sections(db_conn, "sc1", with_content=True)
        res = await sec.save_outline("sc1", {"outline": [
            {"id": "1", "title": "工程概况", "children": []},
            {"id": "2", "title": "施工工艺", "children": []},
        ], "source": "ai"}, db_conn)
        assert res.get("cleared_content_sections") == 1, res
        cur = await db_conn.execute(
            "SELECT COUNT(*) FROM sections WHERE scheme_id='sc1'"
            " AND COALESCE(content,'')!=''")
        assert (await cur.fetchone())[0] == 0

    @pytest.mark.asyncio
    async def test_no_report_when_content_preserved(self, db_conn):
        """按主键匹配的正常保存不得误报（纯提示性字段，无噪声）。"""
        await _seed_scheme(db_conn)
        await _seed_sections(db_conn, "sc1", with_content=True)
        res = await sec.save_outline("sc1", {"outline": [
            {"title": "工程概况", "__original_id": "sc1-R1", "children": []},
            {"title": "施工工艺", "__original_id": "sc1-R2", "children": []},
        ]}, db_conn)
        assert "cleared_content_sections" not in res


# ===========================================================================
# 5. upload-outline 的第三个整表重建入口（D4 同步 + 清除量化）
# ===========================================================================
class TestUploadSaveAsOutlineParity:
    async def _seed(self, db) -> str:
        await _seed_scheme(db)
        await db.execute(
            "INSERT INTO uploaded_outlines(id, project_id, file_name, parsed_json, status)"
            " VALUES('up1', 'p1', 'x.docx', '[]', 'parsed')")
        await _seed_sections(db, "sc1", with_content=True)   # R2 带正文（## 2.1）
        await db.commit()
        return "up1"

    @pytest.mark.asyncio
    async def test_title_matched_save_renumbers_body_subheadings(self, db_conn):
        """上传识别结果按标题命中保留正文时，编号顺移同样要同步子标题。

        修复前 save_as_outline 与 create/delete/reorder/save-outline 一样
        漏掉了 _renormalize_all_section_contents：把「施工工艺」从第 2 章挪到
        第 1 位后，落库正文仍写 "## 2.1"（导出才重算）→ 预览/成稿不一致。
        """
        import app.routers.upload_outline as uo
        await self._seed(db_conn)
        res = await uo.save_as_outline("up1", {"scheme_id": "sc1", "outline": [
            {"title": "施工工艺", "children": []},
            {"title": "工程概况", "children": []},
        ]}, db_conn)
        assert res["ok"] is True
        assert res.get("preserved_content") == 2, "标题匹配应保留两个章节的正文"
        assert res.get("cleared_content_sections") is None, "全命中时不得报清除"
        cur = await db_conn.execute(
            "SELECT content FROM sections WHERE title='施工工艺'")
        content = (await cur.fetchone())["content"]
        assert "## 1.1" in content and "## 2.1" not in content, content

    @pytest.mark.asyncio
    async def test_unmatched_section_clears_content_reported(self, db_conn):
        """未匹配上的旧章节若带正文，必须量化告知（不可静默丢失）。"""
        import app.routers.upload_outline as uo
        await self._seed(db_conn)
        res = await uo.save_as_outline("up1", {"scheme_id": "sc1", "outline": [
            {"title": "编制依据", "children": []},
        ]}, db_conn)
        assert res.get("cleared_content_sections") == 1, res
        assert "1 个未匹配章节" in (res.get("note") or ""), res

        """按主键匹配的正常保存不得误报（纯提示性字段，无噪声）。"""
        await _seed_scheme(db_conn)
        await _seed_sections(db_conn, "sc1", with_content=True)
        res = await sec.save_outline("sc1", {"outline": [
            {"title": "工程概况", "__original_id": "sc1-R1", "children": []},
            {"title": "施工工艺", "__original_id": "sc1-R2", "children": []},
        ]}, db_conn)
        assert "cleared_content_sections" not in res

