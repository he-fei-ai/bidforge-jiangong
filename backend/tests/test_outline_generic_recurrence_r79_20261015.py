"""目录生成 · R79（2026-10-15）占位标题症状复发入口收口 + 四项同族缺陷护栏。

用户实测症状（复发链路）：TOC 三级标题大量占位式泛化名称
（「专项6」「总体实施策划（2）（3）」）+ 编号冲突后缀。
R73/R73b/R74 已收口**生成链**（`_review_and_fix_outline` 11 个检测点），
本轮补的是四条**从未被覆盖**的复发/旁路入口与一项统计失效：

  A 组 · C2（症状复发）外科式标题重写回传「（N）」编号冲突后缀的新标题
         → 第 6 条红线拒收（检测侧 conflict_suffix 要求「基名碰撞」才报，
         单个后缀样本复检恒过 ⇒ 症状经外科式重写原样落库）。
  B 组 · C3（第四条绕过出口）stopped/error/断线的**部分成果**与上传识别、
         目录库套用、AI 调整目录四类落库入口都不经占位闸门 →
         `_save_outline_to_db` 落库前检测 + 加法式回传 `generic_residual`。
  C 组 · C1 目录库引用计数 `bump_ref_count` 的 UPDATE 在 SSE 链路上
         **确定性回滚**（generate_outline 零 commit + get_db teardown 回滚）。
  D 组 · S1 `collect_titles` 位置路径把非 dict 项计入编号，与 renumber /
         `_outline_path_index` 口径分叉 ⇒ 「检出了却修不掉」。
  E 组 · C4 `outline_adjust_system` 无任何反泛化标题纪律。
  F 组 · S2 `checkpoint_schema` 把 failed_chapters 声明成 list[dict]（实为
         章标题字符串数组，声明失真传导到生成的 TS）。

运行：pytest tests/test_outline_generic_recurrence_r79_20261015.py -q
"""
from __future__ import annotations

import ast
import copy
import io
import json
import os

import pytest
import pytest_asyncio

from app.routers import sse_handlers as sh
from app.services import checkpoint_schema as cs
from app.services.outline_quality import (
    collect_titles,
    detect_generic_titles,
    has_trailing_counter_suffix,
)

_SSE_PATH = os.path.join(os.path.dirname(sh.__file__), "sse_handlers.py")
_SECTIONS_PATH = os.path.join(os.path.dirname(os.path.dirname(sh.__file__)),
                              "routers", "sections.py")
_TS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(_SECTIONS_PATH)))), "frontend", "src", "types",
    "checkpointSchema.ts")

PLACEHOLDER_TREE = [
    {"title": "工程概况", "children": []},
    {"title": "基坑支护施工", "children": [
        {"title": "灌注桩施工", "children": []},
        {"title": "专项6", "children": []},
    ]},
    {"title": "安全保证措施", "children": []},
]


def _read(path: str) -> str:
    raw = open(path, "rb").read()
    if raw[:3] == b"\xef\xbb\xbf":
        raise AssertionError("BOM 不应出现：%s" % path)
    text = raw.decode("utf-8")
    assert "\ufffd" not in text, "U+FFFD 不应出现：%s" % path
    return text


# ---------------------------------------------------------------------------
# A 组：C2 — 外科式标题重写拒收「（N）」编号冲突后缀
# ---------------------------------------------------------------------------
class TestTitleFixRejectsCounterSuffix:
    def test_suffix_proposal_is_rejected(self):
        """修复前：`_apply_title_updates` 无此红线 ⇒ applied=1、复检恒过。"""
        tree, applied, rejected = sh._apply_title_updates(
            copy.deepcopy(PLACEHOLDER_TREE),
            [{"path": "2.2", "title": "钢内支撑安装（2）"}])
        assert applied == 0, "带（N）编号冲突后缀的新标题必须被拒收（C2）"
        assert rejected and "编号冲突后缀" in rejected[0]["reason"]
        assert tree[1]["children"][1]["title"] == "专项6", \
            "拒收时节点标题必须保持原样（纯函数：不得写坏）"

    @pytest.mark.parametrize("title", [
        "钢内支撑安装（2）",
        "钢内支撑安装(3)",
        "钢内支撑安装（ 4 ）",
        "钢内支撑安装（12）",
    ])
    def test_all_suffix_shapes_rejected(self, title):
        """半角/全角/带空格/多位数字形态一律拒收（同一判据，不另抄正则）。"""
        _tree, applied, rejected = sh._apply_title_updates(
            copy.deepcopy(PLACEHOLDER_TREE),
            [{"path": "2.2", "title": title}])
        assert applied == 0 and rejected, title

    def test_specific_title_still_accepted(self):
        """回归：具体标题照常落地（红线不得误伤正常改写）。"""
        tree, applied, rejected = sh._apply_title_updates(
            copy.deepcopy(PLACEHOLDER_TREE),
            [{"path": "2.2", "title": "钢内支撑安装与拆除"}])
        assert applied == 1 and rejected == []
        assert tree[1]["children"][1]["title"] == "钢内支撑安装与拆除"

    def test_predicate_is_single_source(self):
        """回写侧判据必须复用 outline_quality 唯一事实源（不自造正则）。"""
        src = _read(_SSE_PATH)
        assert "has_trailing_counter_suffix" in src, \
            "sse_handlers 必须 import 唯一事实源判据"
        fn_src = _read(os.path.join(os.path.dirname(sh.__file__), "..",
                                    "services", "outline_quality.py"))
        tree = ast.parse(fn_src)
        names = [n.name for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef)]
        assert "has_trailing_counter_suffix" in names

    def test_predicate_values(self):
        """判据值域：只看形态，不看是否碰撞（与检测侧刻意不同）。"""
        assert has_trailing_counter_suffix("总体实施策划（2）") is True
        assert has_trailing_counter_suffix("灌注桩施工") is False
        assert has_trailing_counter_suffix("") is False

    async def test_chain_reports_residual_when_ai_keeps_suffix(self, monkeypatch):
        """端到端：AI 坚持回传带后缀的新标题 ⇒ 出口如实报残留（passed=False）。

        这是 C2 的**行为锚点**：单靠静态红线断言无法证明「复检不会放行」。
        """
        seen: list[str] = []
        title_fix_vf = getattr(sh, "_title_fix_validate_fn", None)
        if title_fix_vf is None:  # pragma: no cover - 形态回归即失败
            raise AssertionError("_title_fix_validate_fn 不存在")

        suffix_proposal = {
            "updates": [{"path": "2.2", "title": "钢内支撑安装（2）"}]}

        async def fake_collect(messages, validate_fn=None, **kwargs):
            seen.append(kwargs.get("scene") or "?")
            if validate_fn is title_fix_vf:
                return suffix_proposal, json.dumps(suffix_proposal,
                                                   ensure_ascii=False)
            return ({"passed": False, "suggestions": ["仍有占位标题"]},
                    json.dumps({"passed": False, "suggestions": ["仍有占位标题"]}))

        monkeypatch.setattr(sh, "collect_json_response", fake_collect)
        monkeypatch.setattr(sh, "render", lambda *a, **k: "PROMPT")
        monkeypatch.setattr(sh.settings, "outline_checkpoint_check", False,
                            raising=False)

        outline, review = await sh._review_and_fix_outline(
            copy.deepcopy(PLACEHOLDER_TREE), "基坑", False, "摘要")

        assert detect_generic_titles(outline), "夹具前提：仍有占位标题"
        assert review.get("passed") is False, \
            "占位残留必须如实报未通过（不得谎报已修复）"
        assert int(review.get("generic_residual") or 0) >= 1
        assert outline[1]["children"][1]["title"] == "专项6", \
            "拒收后标题必须保持占位原样，由人工/后续链路处置"


# ---------------------------------------------------------------------------
# B 组：C3 — 落库入口的占位标题检测（部分成果 / 上传 / 套库 / AI 调整）
# ---------------------------------------------------------------------------
class TestSaveOutlineGenericDetection:
    @pytest_asyncio.fixture
    async def ctx(self, tmp_path, monkeypatch):
        import app.db as appdb
        appdb.DB_PATH = tmp_path / "save-outline.sqlite"
        await appdb.init_db()
        db = await appdb.get_conn()
        pid, sid = __import__("uuid").uuid4().hex, __import__("uuid").uuid4().hex
        await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
        await db.execute(
            "INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)",
            (sid, pid, "s"))
        await db.commit()
        yield db, pid, sid
        await appdb.close_db()

    async def test_save_reports_generic_residual(self, ctx):
        """修复前：占位标题直接落库，响应里没有任何痕迹（用户无从察觉）。"""
        from app.routers.sections import _save_outline_to_db
        db, _pid, sid = ctx
        res = await _save_outline_to_db(
            db, sid, copy.deepcopy(PLACEHOLDER_TREE), source="partial")
        hits = res.get("generic_residual")
        assert isinstance(hits, list) and hits, "落库检测必须回传明细"
        assert any(h.get("title") == "专项6" for h in hits)
        assert res.get("ok") is True and res.get("count") >= 3, \
            "加法式：保存本身必须照常成功（只提示、不阻断）"

    async def test_clean_save_has_no_residual_key(self, ctx):
        """干净目录不得凭空多出该键（加法式向后兼容红线）。"""
        from app.routers.sections import _save_outline_to_db
        db, _pid, sid = ctx
        clean = [{"title": "工程概况", "children": []},
                 {"title": "灌注桩支护施工", "children": []}]
        res = await _save_outline_to_db(db, sid, clean, source="partial")
        assert "generic_residual" not in res

    async def test_switch_off_disables_detection(self, ctx, monkeypatch):
        """`outline_check_generic_titles=False` ⇒ 逐字回到引入前（不检测）。"""
        from app.routers.sections import _save_outline_to_db
        from app.config import settings
        db, _pid, sid = ctx
        orig = settings.outline_check_generic_titles
        monkeypatch.setattr(settings, "outline_check_generic_titles", False,
                            raising=False)
        try:
            res = await _save_outline_to_db(
                db, sid, copy.deepcopy(PLACEHOLDER_TREE), source="partial")
            assert "generic_residual" not in res
        finally:
            monkeypatch.setattr(settings, "outline_check_generic_titles", orig,
                                raising=False)


# ---------------------------------------------------------------------------
# C 组：C1 — bump_ref_count 必须 commit
# ---------------------------------------------------------------------------
class TestBumpRefCountCommits:
    def test_commit_follows_bump_in_generate_outline(self):
        """静态锁：`bump_ref_count` 之后同一 if 块内必须 `db.commit()`。

        修复前 generate_outline 函数体内零 commit，而 get_db teardown 会
        rollback ⇒ ref_count 递增确定性丢失（统计承诺从未生效）。
        """
        src = _read(_SSE_PATH)
        tree = ast.parse(src)
        target = None
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and node.name == "generate_outline":
                target = node
        assert target is not None
        seg = ast.get_source_segment(src, target) or ""
        assert "await bump_ref_count(db, _bump_ids)" in seg
        # commit 必须紧跟 bump 调用之后（同一块内）
        idx = seg.find("await bump_ref_count(db, _bump_ids)")
        assert seg.find("await db.commit()", idx) > idx, (
            "bump_ref_count 后必须 commit，否则 UPDATE 会被 get_db teardown 回滚")
        # 且 commit 出现在同一 if 块内（缩进与 bump 一致）
        bump_indent = len(seg[idx:].split("\n")[0]) - len(
            seg[idx:].split("\n")[0].lstrip())
        commit_idx = seg.find("await db.commit()", idx)
        commit_line = seg[commit_idx:].split("\n")[0]
        commit_indent = len(commit_line) - len(commit_line.lstrip())
        assert commit_indent == bump_indent, "commit 必须与 bump 同级（同一 if 块）"


# ---------------------------------------------------------------------------
# D 组：S1 — collect_titles 位置路径口径对齐
# ---------------------------------------------------------------------------
class TestCollectTitlesPathAlignment:
    def test_dirty_array_paths_match_path_index(self):
        """脏数组里非 dict 项不得占用位置编号（与 renumber/_outline_path_index
        同口径）；修复前 collect_titles 报 '3' 而修复侧只认 '1'/'2'。"""
        dirty = [{"title": "甲", "children": []}, "STRING-ITEM", 123,
                 {"title": "专项9", "children": []}]
        paths = [e["path"] for e in collect_titles(dirty)]
        assert paths == ["1", "2"], paths
        idx = sh._outline_path_index(dirty)
        assert sorted(idx.keys()) == ["1", "2"], sorted(idx.keys())

    def test_detect_paths_are_addressable(self):
        """检出报出的每个 path 都必须能在重写索引里定位（否则修不掉）。"""
        dirty = [{"title": "甲", "children": []}, "STRING-ITEM",
                 {"title": "专项9", "children": []}]
        hits = detect_generic_titles(dirty)
        assert hits and all(h["path"] == "2" for h in hits), hits
        idx = sh._outline_path_index(dirty)
        assert all(h["path"] in idx for h in hits), \
            "检出的 path 必须可被外科式重写定位（S1）"

    def test_normal_tree_paths_unchanged(self):
        """回归：合法树的路径与旧实现逐字一致（1 基、点分、深度优先）。"""
        tree = [{"title": "A", "children": [
            {"title": "A1", "children": []},
            {"title": "A2", "children": [{"title": "A2a", "children": []}]}]},
            {"title": "B", "children": []}]
        paths = [e["path"] for e in collect_titles(tree)]
        assert paths == ["1", "1.1", "1.2", "1.2.1", "2"], paths


# ---------------------------------------------------------------------------
# E 组：C4 — outline_adjust_system 反泛化纪律
# ---------------------------------------------------------------------------
class TestAdjustPromptAntiGeneric:
    def test_adjust_prompt_forbids_generic_and_suffix(self):
        """AI 调整目录入口此前 7 条纪律里没有任何反泛化条款。"""
        from app.services.ai.prompts import get_prompt
        text = get_prompt("outline_adjust_system", scheme_name="s",
                          scheme_type="t", current_outline="{}",
                          instruction="加两章")
        assert "严禁泛化/占位标题" in text, "必须显式禁止泛化/占位标题"
        assert "编号冲突后缀" in text, "必须显式禁止（N）编号冲突后缀"
        assert "绝不用序号消歧" in text, "必须给出替代方案（差异化命名）"


# ---------------------------------------------------------------------------
# F 组：S2 — checkpoint_schema failed_chapters 类型订正
# ---------------------------------------------------------------------------
class TestCheckpointSchemaTypes:
    def test_python_declares_list_str(self):
        assert cs.CHECKPOINT_FIELD_TYPES["outline_result"]["failed_chapters"] \
            == "list[str]", "实收值是章标题字符串数组，声明不得写成 list[dict]"

    def test_ts_matches_python(self):
        """生成的 TS 必须与 Python 单一事实源同口径（防手改 TS 漂移）。

        比对前归一行尾：生成器在 Windows 落盘 CRLF，内存串是 LF，
        行尾不是本护栏的消费判据（字节体检另有 loneCR 锁）。
        """
        from app.services.checkpoint_schema import generate_typescript_schema
        generated = generate_typescript_schema()
        on_disk = _read(_TS_PATH).replace("\r\n", "\n")
        assert generated.strip() == on_disk.strip(), (
            "types/checkpointSchema.ts 与生成结果不一致 —— 该文件 AUTO-GENERATED，"
            "须用 backend/tools/generate_checkpoint_schema.py 重新生成")
        assert "failed_chapters?: string[];" in on_disk

    def test_runtime_values_are_strings(self):
        """生产写入侧实参形态：章标题字符串（与声明互证）。"""
        src = _read(_SSE_PATH)
        assert 'failed_chapters.append(str(ch.get("title")' in src, (
            "写入侧必须是章标题字符串；若改成 dict，本组类型断言需同步修订")
