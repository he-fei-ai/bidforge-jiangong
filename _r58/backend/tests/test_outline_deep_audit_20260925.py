"""目录生成模块 · 深度审查修复回归（2026-09-25 第五轮）

本文件锁定本轮深探查中定位并修复的三类缺陷，每条都附源码级依据：

| 编号 | 缺陷 | 依据 | 后果 |
|---|---|---|---|
| E1（P0） | 正文生成「没有待生成的章节」分支调用 `_update_progress_safe`，但该名字在同函数**更靠后**才赋值 | `ruff --select F821` 报 `sse_handlers.py:3889 F821`；`event_stream` 内 `assigns=[4057] uses=[3889,…]` | 该分支必然抛 `UnboundLocalError` → 被外层 except 吞成「正文生成失败」，而实际只是「本来就没有待生成章节」的正常空跑 |
| E2（P1） | `_build_partial_preview` 只裁剪**当前章**的子树，已完成章的深层节点原样下发 | 该函数仅对 `preview[-1]` 调 `clamp_outline_depth` | 分步生成过程中前端预览出现 4/5 级节点与未合并的 description，与收尾 `normalize_outline` 的三级结构不一致（用户看到目录树「生成完又变了一次」） |
| E3（P1） | 前端 `stripOutlineNumbering` 缺后端 `strip_outline_numbering` 的「整条标题即编号」护栏 | 后端 `_PURE_NUMBER_TITLE_RE.fullmatch` 提前返回原文；前端无此前置判断 | 标题本身是编号（"2.1"）时前端剥成 "1"、后端保持 "2.1"，同一节点前后端显示不一致 → 双重编号/编号漂移 |

E1 的回归护栏分两层：① 静态（F821 门禁，见 test_outline_workflow_logs.py）；
② 结构（此处断言「赋值行早于全部使用行」）。
"""
import ast
import copy
import inspect

import pytest
from app.routers import sse_handlers as sh
from app.services.outline_utils import MAX_OUTLINE_DEPTH, normalize_outline


def _chain(nodes: list, acc: list | None = None) -> list:
    """按前序遍历摊平 (id, title, description)，用于结构比对。"""
    acc = acc if acc is not None else []
    for n in nodes or []:
        if not isinstance(n, dict):
            continue
        acc.append((str(n.get("id") or ""), str(n.get("title") or ""),
                    str(n.get("description") or "")))
        _chain(n.get("children") or [], acc)
    return acc


def _depth(nodes: list, d: int = 1) -> int:
    """树深度（根为 1）。空列表返回 0；无子节点的节点即其所在层级 d。"""
    if not isinstance(nodes, list):
        return 0
    real = [n for n in nodes if isinstance(n, dict)]
    if not real:
        return 0
    deeper = max((_depth(n.get("children") or [], d + 1) for n in real), default=0)
    return deeper if deeper else d


# ============================================================
# E1（P0）正文生成空章节分支的未定义名
# ============================================================
class TestE1NoLeavesBranchNoUnboundLocal:
    def _event_stream_fn(self):
        tree = ast.parse(inspect.getsource(sh.generate_content).lstrip())
        return next(n for n in ast.walk(tree)
                    if isinstance(n, ast.AsyncFunctionDef) and n.name == "event_stream")

    def test_update_progress_safe_assigned_before_first_use(self):
        """`_update_progress_safe` 的赋值行必须早于全部使用行。

        源码级护栏：正文生成 `event_stream` 内该名字是局部名（闭包内赋值），
        Python **编译期**就判定为局部变量，因此「先使用后赋值」不是「取到外层值」
        而是直接 `UnboundLocalError`。该分支需空目录方案才能走到，行为级单测
        成本高，静态顺序检查是唯一能在 CI 拦住它的护栏。
        """
        fn = self._event_stream_fn()
        assigns: list[int] = []
        uses: list[int] = []
        for n in ast.walk(fn):
            if isinstance(n, ast.Name) and n.id == "_update_progress_safe":
                (assigns if isinstance(n.ctx, ast.Store) else uses).append(n.lineno)
        assert assigns, "未找到 _update_progress_safe 的赋值点"
        assert uses, "未找到 _update_progress_safe 的使用点"
        assert max(assigns) <= min(uses), (
            f"_update_progress_safe 在第 {min(uses)} 行被使用，却到第 "
            f"{max(assigns)} 行才赋值 → 该分支必抛 UnboundLocalError"
        )

    def test_no_leaves_branch_precedes_updater_definition(self):
        """`if not leaves:` 早退分支必须位于进度更新器定义之前仍可安全调用。

        反例护栏：若把 `_update_progress_safe` 的定义放回 try 内靠后位置，
        本断言与上一条同时失败（防止「只改一处、另一处仍错」）。
        """
        fn = self._event_stream_fn()
        branch_lines = [n.lineno for n in ast.walk(fn)
                        if isinstance(n, ast.If)
                        and isinstance(n.test, ast.UnaryOp)
                        and isinstance(n.test.op, ast.Not)
                        and isinstance(n.test.operand, ast.Name)
                        and n.test.operand.id == "leaves"]
        assign_lines = [n.lineno for n in ast.walk(fn)
                        if isinstance(n, ast.Name)
                        and n.id == "_update_progress_safe"
                        and isinstance(n.ctx, ast.Store)]
        assert branch_lines, "未找到 `if not leaves:` 早退分支"
        assert assign_lines, "未找到 _update_progress_safe 赋值点"
        assert max(assign_lines) < min(branch_lines), (
            "进度更新器定义仍位于 `if not leaves:` 早退分支之后，"
            "该分支调用它会抛 UnboundLocalError"
        )

    def test_hoisted_updater_is_created_before_try(self):
        """写锁与更新器必须在 `try:` 之前建立（早退分支位于 try 内）。"""
        fn = self._event_stream_fn()
        try_line = next(n.lineno for n in fn.body if isinstance(n, ast.Try))
        hoist = [n.lineno for n in ast.walk(fn)
                 if isinstance(n, ast.Assign)
                 and any(getattr(t, "id", "") == "_update_progress_safe"
                         for t in n.targets)]
        assert hoist and min(hoist) < try_line, (
            "_update_progress_safe 必须在 try 之前赋值，否则 try 内任何早退分支"
            "（含 `if not leaves:`）都会 UnboundLocalError"
        )

    async def test_empty_leaves_branch_completes_instead_of_erroring(self, monkeypatch):
        """行为级回归：空章节方案点「生成正文」必须正常 completed，不抛异常。

        修复前该分支 100% 抛 `UnboundLocalError` → 被外层 except 捕获 →
        前端收到 error「正文生成失败」，而真实原因只是「本来就没有章节」。
        静态 AST 断言只能证明赋值顺序，本用例真正把 SSE 流跑一遍。
        """
        import json as _json

        finished: list[tuple] = []
        ckpt: list[dict] = []

        async def _register(*a, **k):
            return "task-empty"

        async def _progress(task_id, p, message="", event="progress", **k):
            return None

        async def _stats(*a, **k):
            return None

        async def _finish(task_id, status="", message="", **k):
            finished.append((status, message))

        async def _save_ckpt(task_id, payload):
            ckpt.append(payload)

        monkeypatch.setattr(sh, "register_task", _register)
        monkeypatch.setattr(sh, "update_progress", _progress)
        monkeypatch.setattr(sh, "update_task_stats", _stats)
        monkeypatch.setattr(sh, "finish_task", _finish)
        monkeypatch.setattr(sh, "_save_content_checkpoint", _save_ckpt)
        monkeypatch.setattr(sh, "settle_global_conn", _noop)
        monkeypatch.setattr(sh, "wait_resume", lambda *a, **k: _noop())
        monkeypatch.setattr(sh, "is_stopped", lambda *a, **k: False)
        monkeypatch.setattr(sh, "has_active_task", lambda *a, **k: False)

        resp = await sh.generate_content("scheme-x", _FakeRequest(), db=_EmptyDB())
        chunks: list[bytes] = []
        async for piece in resp.body_iterator:
            chunks.append(piece if isinstance(piece, bytes) else str(piece).encode())
        text = b"".join(chunks).decode("utf-8", "replace")
        names = [_json.loads(ln[6:]).get("event")
                 for ln in text.splitlines() if ln.startswith("data: ")]
        assert "error" not in names, (
            f"空章节方案不应走 error 分支（修复前 UnboundLocalError → error）：{text[:400]}"
        )
        assert "completed" in names, f"应收到 completed 事件，实际：{names}"
        assert any(s == "completed" for s, _ in finished), finished
        assert ckpt, "空章节分支也应写 checkpoint（断线重挂需可还原）"


# ============================================================
# E2（P1）分步生成预览必须与最终结构同口径（全部章节裁到三级）
# ============================================================
def _deep_outline() -> list:
    """构造含 5 级 / 4 级深度的两章目录（模拟弱模型越级输出）。"""
    return [
        {"title": "C1", "children": [
            {"title": "C1.1", "children": [
                {"title": "C1.1.1", "children": [
                    {"title": "C1.1.1.1", "children": [
                        {"title": "C1.1.1.1.1", "description": "L5"}]}]}]}]},
        {"title": "C2", "children": [
            {"title": "C2.1", "children": [
                {"title": "C2.1.1", "children": [
                    {"title": "C2.1.1.1", "description": "L4"}]}]}]},
    ]


# ------------------------------------------------------------
# E1 行为级用例的公共桩
# ------------------------------------------------------------
async def _noop(*a, **k):
    return None


class _FakeRequest:
    """最小 Request 替身（generate_content 只需 request.json()）。"""

    class _Client:
        host = "127.0.0.1"

    headers: dict = {}
    client = _Client()

    async def json(self):
        return {}


class _EmptyDB:
    """方案存在、但章节表为空（模拟「没有待生成的章节」的方案）。

    `generate_content` 先查 schemes 再查 sections，故按 SQL 关键字分流：
    schemes 返回一行方案，sections/chart_predictions 等返回空。
    """

    class _Cur:
        def __init__(self, row=None):
            self._row = row

        async def fetchone(self):
            return self._row

        async def fetchall(self):
            return []

    async def execute(self, sql, *a, **k):
        s = str(sql)
        if "FROM schemes" in s:
            return self._Cur({"id": "scheme-x", "project_id": "proj-x",
                              "name": "空目录方案", "type": "深基坑",
                              "word_budget": 30000, "config_json": "{}",
                              "auto_consistency_repair": 0})
        return self._Cur()

    async def executemany(self, *a, **k):
        return None

    async def commit(self):
        return None

    async def rollback(self):
        return None


class TestE2PartialPreviewMatchesFinal:
    def test_preview_never_exceeds_max_depth(self):
        """预览树深度不得超过 MAX_OUTLINE_DEPTH（修复前已完成章可达 5 级）。"""
        preview = sh._build_partial_preview(_deep_outline())
        assert _depth(preview) <= MAX_OUTLINE_DEPTH, (
            f"预览树深度 {_depth(preview)} 超过上限 {MAX_OUTLINE_DEPTH}"
        )

    def test_preview_structure_equals_final_normalized(self):
        """预览的 (id, title, description) 必须与收尾 normalize_outline 完全一致。

        本缺陷的核心断言：预览只是「提前看一眼」，不能与最终产物有结构差异
        （旧实现只裁当前章 —— 已完成章的深层标题既没并入 description，
        编号也停留在未重排状态）。
        """
        src = _deep_outline()
        preview = sh._build_partial_preview(copy.deepcopy(src))
        final = normalize_outline(copy.deepcopy(src))
        assert _chain(preview) == _chain(final)

    def test_deep_titles_are_merged_into_description(self):
        """被裁掉的深层标题必须并入父节点 description（内容线索不丢）。"""
        final = normalize_outline(copy.deepcopy(_deep_outline()))
        c1 = final[0]
        node = (c1.get("children") or [])[0]
        node = (node.get("children") or [])[0]
        assert "含：" in str(node.get("description") or ""), (
            "第四级标题未并入三级节点 description"
        )

    def test_preview_does_not_mutate_source(self):
        """预览构造必须深拷贝（不得就地裁剪污染 full_outline 主数据）。"""
        src = _deep_outline()
        before = _chain(copy.deepcopy(src))
        sh._build_partial_preview(src)
        assert _chain(src) == before

    def test_empty_and_malformed_inputs_are_safe(self):
        """空 / 非列表 / 畸形节点均不得抛异常（空数据护栏）。"""
        assert sh._build_partial_preview([]) == []
        assert sh._build_partial_preview("not-a-list") == []
        weird = [{"title": "A", "children": "x"}, {"title": "B"},
                 "junk", {"title": "C", "children": [None, {"title": "C1"}]}]
        out = sh._build_partial_preview(weird)
        assert isinstance(out, list) and out
        assert _depth(out) <= MAX_OUTLINE_DEPTH

    def test_single_chapter_outline_preview(self):
        """仅一章（当前章即末章）时行为不回退。"""
        one = [{"title": "A", "children": [
            {"title": "A1", "children": [{"title": "A1a", "description": "x"}]}]}]
        preview = sh._build_partial_preview(copy.deepcopy(one))
        assert _chain(preview) == _chain(normalize_outline(copy.deepcopy(one)))


# ============================================================
# E3（P1）前端编号剥离必须与后端同源
# ============================================================
class TestE3StripNumberingParity:
    """前端 `stripOutlineNumbering` 与后端 `strip_outline_numbering` 同源护栏。

    后端在剥离前有「整条标题即纯编号路径」的短路（`_PURE_NUMBER_TITLE_RE`），
    前端缺失该判断 → 标题为 "2.1" 这类纯编号时两侧结论不同。
    """

    CASES = [
        "2.1", "1.2.3", "1.2.3.4.5.6.7.8", "第一章", "1.1 编制依据",
        "3D打印", "2023年规范", "1.2、3.4 混合", "第一章 第一章 工程概况",
        "00 零", "1..2 异常", "1.0 零级", "12.34 超长编号",
    ]

    def test_backend_contract_table_is_stable(self):
        """锁定后端语义（前端对齐基准），防止两侧一起漂移。"""
        from app.services.numbering import strip_outline_numbering as be
        got = {c: be(c) for c in self.CASES}
        assert got["2.1"] == "2.1", "整条标题即编号时后端应原样返回"
        assert got["1.2.3"] == "1.2.3"
        assert got["1.1 编制依据"] == "编制依据"
        assert got["3D打印"] == "3D打印", "无分隔符的数字开头标题不应被剥离"
        assert got["2023年规范"] == "2023年规范"
        assert got["第一章 第一章 工程概况"] == "第一章 工程概况", "每次只剥一层"

    def test_frontend_has_pure_number_guard(self):
        """前端实现必须含「整条标题即编号」的短路护栏。

        以源码级断言锁定（后端 pytest 无法直接 import 前端 TS 实现），
        命中任一等价写法（PURE_NUMBER 常量 / fullMatch 调用）即通过。
        """
        from pathlib import Path
        page = (Path(__file__).resolve().parents[1].parent
                / "frontend" / "src" / "pages" / "SchemeWorkbenchPage.tsx")
        if not page.exists():                      # pragma: no cover - 部署形态差异
            pytest.skip("前端源码不在后端仓库路径下，跳过")
        src = page.read_text(encoding="utf-8")
        parts = src.split("export function stripOutlineNumbering", 1)
        assert len(parts) == 2, "未找到前端 stripOutlineNumbering 定义"
        body = parts[1].split("\n}", 1)[0]
        assert "PURE_NUMBER" in body or "fullMatch" in body, (
            "前端 stripOutlineNumbering 缺少「整条标题即纯编号」护栏，"
            "与后端 strip_outline_numbering 不一致"
        )

    def test_frontend_pure_number_constant_matches_backend_regex(self):
        """前端的纯编号正则必须与后端 `_PURE_NUMBER_TITLE_RE` 等价。

        两侧都接受全角点 `．`；前端用 ``\\d``（等价 Python ``[0-9]``）不算漂移。
        """
        import re as _re
        from pathlib import Path

        from app.services.numbering import _PURE_NUMBER_TITLE_RE
        page = (Path(__file__).resolve().parents[1].parent
                / "frontend" / "src" / "pages" / "SchemeWorkbenchPage.tsx")
        if not page.exists():                      # pragma: no cover
            pytest.skip("前端源码不在后端仓库路径下，跳过")
        src = page.read_text(encoding="utf-8")
        m = _re.search(r"const\s+PURE_NUMBER_TITLE_RE\s*=\s*/(.+?)/[a-z]*\s*;",
                       src, _re.S)
        if not m:
            m = _re.search(r"PURE_NUMBER[^=]*=\s*/(.+?)/[a-z]*\s*;", src, _re.S)
        assert m, "未找到前端纯编号正则定义"
        fe = _re.compile(m.group(1))
        for c in self.CASES:
            assert bool(fe.fullmatch(c)) == bool(_PURE_NUMBER_TITLE_RE.fullmatch(c)), (
                f"纯编号判定不一致：{c!r}"
            )
