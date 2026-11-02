"""R138 · 目录生成模块遗留收口 · 编号算法单一源 + 空编号可观测性（2026-10-28）

承接 R137-O 报告 §八 遗留清单的两项「有代码依据 + 测试结果支撑」的收口：

A. **L-3 · 同一循环体三处各写一遍（P1）**
   ``numbering.renumber_section_body_subheadings``（落库规范化）、
   ``export._detect_duplicate_sections``（导出预检）、``export.write_section``
   （导出写稿）此前都各自写「遍历 blocks → 调 _compute_subheading → 写
   ``_fixed_text``」，差异只在于「谁传 has_children、谁写 _heading_style、
   谁在告警里带章节 ID」。任一处漏传 ``has_children`` 就会让
   「前端预览 = 落库正文 = 导出成稿」三处同源契约分叉。
   修法：新增 ``content_blocks.number_body_subheadings`` 作为唯一循环体，
   三处改为调用它；``renumber_section_body_subheadings`` 新增
   ``section_id``（默认空串 ⇒ 旧行为逐字不变），使落库路径的「层级不齐」
   告警能定位到具体章节（旧实现恒打 "?"）。

B. **L-1 · 空编号章节静默跳过（P2，判为「非缺陷但不可观测」）**
   ``load_scheme_section_index`` 跳过 ``outline_json.id`` 为空的章节。
   取证结论：跳过**不是**编号错误 —— 单章查库路径在同条件下也原样返回
   （``if not section_number: return content, False``），两条路径结果一致，
   此处只省掉一次查库。真问题是**静默**：方案里有多少章「永远不参与编号
   同步」无从观察。修法只补聚合计数 + 一条 WARNING，不改任何编号行为。

本文件是其护栏：A 静态栅栏（防三处重新分叉）+ B 行为等价（helper 与手写
循环逐字一致）+ C 告警定位 + D 聚合告警功能实证。
"""
import ast
import json
import logging
from pathlib import Path

import pytest
from app.services.content_blocks import (
    _compute_subheading,
    _strip_title_number,
    number_body_subheadings,
)
from app.services.numbering import (
    load_scheme_section_index,
    normalize_section_content_subheadings,
    renumber_section_body_subheadings,
)

_APP = Path(__file__).resolve().parents[1] / "app"


def _read(rel: str) -> str:
    return (_APP / rel).read_text(encoding="utf-8")


def _calls_to(src: str, name: str) -> list[str]:
    """返回源码中所有对 ``name`` 的调用点（Name 或 Attribute 形态）。"""
    tree = ast.parse(src)
    hits: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Name) and fn.id == name:
            hits.append(f"line {node.lineno}")
        elif isinstance(fn, ast.Attribute) and fn.attr == name:
            hits.append(f"line {node.lineno}")
    return hits


def _keyword_calls(src: str, name: str) -> list[dict]:
    """返回对 ``name`` 的调用及其关键字实参（值以 ``ast.unparse`` 取文本）。

    ⚠️ 不能用 ``inspect.getsource`` 定位写稿侧循环体：``export.write_section``
    是导出构建函数的**内部闭包**，不是模块属性，故只能走源码 AST。
    """
    out: list[dict] = []
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        fname = fn.id if isinstance(fn, ast.Name) else (
            fn.attr if isinstance(fn, ast.Attribute) else "")
        if fname != name:
            continue
        out.append({"line": node.lineno,
                    "kw": {k.arg: ast.unparse(k.value)
                           for k in node.keywords if k.arg}})
    return out


# ============================================================
# A 组：静态栅栏 —— 编号循环体只允许存在于 helper 内
# ============================================================
class TestSingleSourceFence:
    @pytest.mark.parametrize("rel", ["routers/export.py", "services/numbering.py"])
    def test_no_direct_compute_subheading_call(self, rel):
        """export / numbering 不得再直接调 _compute_subheading。

        这两处的循环体已收口到 number_body_subheadings；出现直接调用即
        「三份副本重新分叉」的开端（历史上正是这种分叉让预检与渲染对
        同一正文给出不同编号）。导入本身允许（export 保留兼容再导出）。
        """
        hits = _calls_to(_read(rel), "_compute_subheading")
        assert hits == [], f"{rel} 仍在直接调用 _compute_subheading: {hits}"

    def test_helper_defined_exactly_once(self):
        """``number_body_subheadings`` 全 app 只有一处定义。"""
        owners = []
        for py in _APP.rglob("*.py"):
            try:
                tree = ast.parse(py.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):  # pragma: no cover
                continue
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                        and node.name == "number_body_subheadings":
                    owners.append(py.relative_to(_APP).as_posix())
        assert owners == ["services/content_blocks.py"], owners

    def test_helper_keeps_has_children_and_style_contract(self):
        """helper 必须把 has_children 透传给算法本体，并按需写 _heading_style。"""
        import inspect

        src = inspect.getsource(number_body_subheadings)
        assert "has_children=has_children" in src, src
        assert 'block["_fixed_text"] = text' in src, src
        assert 'block["_heading_style"] = style' in src, src

    def test_three_call_sites_use_helper(self):
        """三处调用点都必须经 helper，且各自关键字实参符合契约。"""
        numbering_calls = _keyword_calls(
            _read("services/numbering.py"), "number_body_subheadings")
        export_calls = _keyword_calls(
            _read("routers/export.py"), "number_body_subheadings")

        # 落库规范化：1 处，必须传 has_children 与 section_id
        assert len(numbering_calls) == 1, numbering_calls
        kw = numbering_calls[0]["kw"]
        assert kw.get("has_children") == "_demote", kw
        assert kw.get("sec_id") == "section_id", kw
        assert "with_style" not in kw, kw

        # 导出预检 + 导出写稿：各 1 处，都必须传 has_children 与 sec_id
        assert len(export_calls) == 2, export_calls
        for call in export_calls:
            assert call["kw"].get("has_children") == "has_children_demoted", call
            assert "sec_id" in call["kw"], call
        # 写稿侧独有：需要 _heading_style 供 Heading 样式深度计算
        styled = [c for c in export_calls
                  if c["kw"].get("with_style") == "True"]
        assert len(styled) == 1, export_calls

    def test_section_id_param_default_empty(self):
        """新增 ``section_id`` 必须默认空串（旧调用方零改动）。"""
        import inspect

        params = inspect.signature(
            renumber_section_body_subheadings).parameters
        assert "section_id" in params, list(params)
        assert params["section_id"].default == "", params["section_id"].default


# ============================================================
# B 组：行为等价 —— helper 与「手写 _compute_subheading 循环」逐字一致
# ============================================================
def _blocks():
    """每次返回全新块列表（helper 原地写入，不可共享样例）。"""
    return [
        {"type": "heading", "level": 2, "text": "总体安排", "src_line": 0},
        {"type": "para", "text": "正文一。", "src_line": 2},
        {"type": "heading", "level": 3, "text": "1.1 关键节点", "src_line": 4},
        {"type": "heading", "level": 4, "text": "细部构造", "src_line": 6},
        {"type": "heading", "level": 2, "text": "a、资源配置", "src_line": 8},
        {"type": "table", "text": "| a | b |", "src_line": 10},
    ]


def _reference(blocks, *, section_prefix, section_level, sec_id="",
               has_children=False, with_style=False):
    """R137 之前三处各自实现的那段循环（作为等价基准，不得再进生产代码）。"""
    sub_counters: dict = {}
    for block in blocks:
        if block.get("type") != "heading":
            continue
        text, style = _compute_subheading(
            section_prefix, section_level, block.get("level", 1), sub_counters,
            _strip_title_number(block.get("text", "")), sec_id,
            has_children=has_children)
        block["_fixed_text"] = text
        if with_style:
            block["_heading_style"] = style
    return sub_counters


class TestHelperBehaviorEquivalence:
    @pytest.mark.parametrize("has_children", [False, True])
    @pytest.mark.parametrize("with_style", [False, True])
    def test_matches_legacy_loop(self, has_children, with_style):
        got, ref = _blocks(), _blocks()
        got_counters = number_body_subheadings(
            got, section_prefix="2.1", section_level=2, sec_id="sec-x",
            has_children=has_children, with_style=with_style)
        ref_counters = _reference(
            ref, section_prefix="2.1", section_level=2, sec_id="sec-x",
            has_children=has_children, with_style=with_style)
        # 块内容（含 _fixed_text / _heading_style 存在性）逐字一致
        assert got == ref
        # 计数器状态一致（同一次调用即「本节」作用域）
        assert got_counters == ref_counters

    def test_non_heading_blocks_untouched(self):
        blocks = _blocks()
        number_body_subheadings(
            blocks, section_prefix="1", section_level=1)
        assert "para" not in [b["type"] for b in blocks if "_fixed_text" in b]
        for b in blocks:
            if b["type"] != "heading":
                assert "_fixed_text" not in b, b

    def test_style_key_absent_when_not_requested(self):
        """with_style=False 时不得写入 _heading_style（落库/预检不需要该键）。"""
        blocks = _blocks()
        number_body_subheadings(blocks, section_prefix="1", section_level=1)
        assert all("_heading_style" not in b for b in blocks)

    def test_fence_blocks_not_filtered_by_helper(self):
        """helper 不做围栏过滤 —— 取舍由调用方决定（numbering 传子集）。"""
        blocks = [{"type": "heading", "level": 2, "text": "围栏标题",
                   "_from_fence": True}]
        number_body_subheadings(blocks, section_prefix="1", section_level=1)
        assert blocks[0].get("_fixed_text"), blocks

    def test_counters_are_per_call(self):
        """两次调用互不影响（每次调用 = 本节作用域，与旧实现同）。"""
        one, two = _blocks(), _blocks()
        number_body_subheadings(one, section_prefix="1", section_level=1)
        number_body_subheadings(two, section_prefix="1", section_level=1)
        assert [b.get("_fixed_text") for b in one] == \
               [b.get("_fixed_text") for b in two]


# ============================================================
# C 组：section_id 使「层级不齐」告警可定位（L-3 附带修「日志不准确」）
# ============================================================
_LEVEL_SKEW = "## 总体安排\n\n正文。\n\n# 回跳标题\n\n更多。\n"


class TestSectionIdInWarning:
    def test_warning_carries_real_section_id(self, caplog):
        with caplog.at_level(logging.WARNING, logger="content_blocks"):
            renumber_section_body_subheadings(
                _LEVEL_SKEW, "2.1", 2, "进度计划", section_id="sec-abcdef")

        msg = [r.getMessage() for r in caplog.records
               if "层级不齐" in r.getMessage()]
        assert msg, "未触发层级不齐告警（样例内容失效）"
        assert "sec-abcdef" in msg[0], msg[0]

    def test_default_section_id_keeps_old_label(self, caplog):
        """不传 section_id ⇒ 标签仍是 "?"（默认值不改变旧行为）。"""
        with caplog.at_level(logging.WARNING, logger="content_blocks"):
            renumber_section_body_subheadings(_LEVEL_SKEW, "2.1", 2, "进度计划")

        msg = [r.getMessage() for r in caplog.records
               if "层级不齐" in r.getMessage()]
        assert msg and "section=?" in msg[0], msg[:1]


# ============================================================
# D 组：空编号章节聚合告警（L-1 可观测性，行为零变更）
# ============================================================
async def _seed(db, pid="p1", sid="s1"):
    await db.execute("INSERT OR IGNORE INTO projects (id, name) VALUES (?,?)",
                     (pid, "测试项目"))
    await db.execute("INSERT INTO schemes (id, project_id, name) VALUES (?,?,?)",
                     (sid, pid, "测试方案"))
    await db.commit()


async def _insert_section(db, sid, sec_id, title, level=1, outline_id="1"):
    await db.execute(
        "INSERT INTO sections (id, scheme_id, project_id, title, parent_id, "
        "sort_order, level, outline_json) VALUES (?,?,?,?,?,?,?,?)",
        (sec_id, sid, "p1", title, "", 0, level,
         json.dumps({"id": outline_id, "level": level}, ensure_ascii=False)))
    await db.commit()


class TestSkippedNoNumberObservable:
    async def test_single_aggregated_warning_with_count(self, db_conn, caplog):
        await _seed(db_conn)
        await _insert_section(db_conn, "s1", "sec1", "甲", outline_id="1")
        await _insert_section(db_conn, "s1", "sec2", "乙", outline_id="")
        await _insert_section(db_conn, "s1", "sec3", "丙", outline_id="")

        with caplog.at_level(logging.WARNING,
                             logger="app.services.numbering"):
            idx = await load_scheme_section_index(db_conn, "s1")

        # 行为零变更：空编号章节仍不进 meta
        assert set(idx["meta"]) == {"sec1"}, idx["meta"]
        # 聚合为一条，且带计数（不按章刷屏）
        warns = [r.getMessage() for r in caplog.records
                 if "缺存储编号" in r.getMessage()]
        assert len(warns) == 1, warns
        assert "2 个章节缺存储编号" in warns[0], warns[0]

    async def test_no_warning_when_all_numbered(self, db_conn, caplog):
        await _seed(db_conn)
        await _insert_section(db_conn, "s1", "sec1", "甲", outline_id="1")
        await _insert_section(db_conn, "s1", "sec2", "乙", outline_id="1.1")

        with caplog.at_level(logging.WARNING,
                             logger="app.services.numbering"):
            idx = await load_scheme_section_index(db_conn, "s1")

        assert len(idx["meta"]) == 2
        assert not [r for r in caplog.records
                    if "缺存储编号" in r.getMessage()]

    async def test_skipped_section_content_still_unchanged(self, db_conn):
        """索引未命中的章节走单章查库回退，同条件下仍原样返回（结果一致）。"""
        await _seed(db_conn)
        await _insert_section(db_conn, "s1", "sec1", "甲", outline_id="1")
        await _insert_section(db_conn, "s1", "sec2", "乙", outline_id="")

        idx = await load_scheme_section_index(db_conn, "s1")
        content = "## 子标题\n\n正文。\n"
        new, changed = await normalize_section_content_subheadings(
            db_conn, "s1", "sec2", content, index=idx)
        assert changed is False and new == content, new

    async def test_index_meta_carries_section_id(self, db_conn):
        """index 的 meta 必须带 section_id（否则预取路径告警无法定位）。"""
        await _seed(db_conn)
        await _insert_section(db_conn, "s1", "sec1", "甲", level=2,
                             outline_id="1.2")
        idx = await load_scheme_section_index(db_conn, "s1")
        assert idx["meta"]["sec1"]["section_id"] == "sec1", idx["meta"]
