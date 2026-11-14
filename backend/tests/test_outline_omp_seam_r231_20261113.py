"""R231 目录生成模块深度探索增强 · OMP 旁路接缝护栏（2026-11-13）。

OMP 目录生成旁路（generate-outline-omp 端点 + omp_outline_bridge + OmpTaskAgent）
是目录写路径家族的新成员，本轮 R231 亲验定罪两处缺陷并修复：

FIX-1（P2 · 崩溃面）  parse_outline_json 对 docstring 承诺的 dict-root 容器形态
        （{"title":..., "children":[...]}）以 level=0 起步产出根标题条目，
        fold_generated_sections_to_tree 的 (0, root) 哨兵被 ``0 >= 0`` 弹空
        => IndexError（端点 500）。修法：容器根 title 不是章节（与
        {"sections":[...]} 容器语义一致）不产出 + fold 对 level<=0 防御钳制。
FIX-2（P2 · 数据污染） _read_workspace 的「首个 .md/.json」回退不排除 NOTES.md
        —— sorted() 大写 N 排在小写 o 前，omp 只写 NOTES.md（未写 OUTLINE.md）
        时备忘散文被当章节标题解析入库（实测探针：NOTES.md + outline.json
        并存时 NOTES.md 胜出、「工作备忘/遗留问题」进 sections）。

护栏分四组：
  A 行为实证 —— 容器根形态 / 防御钳制 / 既有形态回归（parse + fold）
  B 行为实证 —— NOTES.md 排除（并存胜出次序 + 仅 NOTES 回到空产物主语义）
  C 源码棘轮 —— 三处修复判据在位（防回流；不锁字面量注释）
  D 字节体检 —— 本轮触碰的 omp 接缝文件行尾 / BOM / U+FFFD
"""

from __future__ import annotations

import inspect
import os
import shutil
import tempfile

from app.services.ai.omp_outline_bridge import fold_generated_sections_to_tree
from app.services.ai.omp_task_agent import (
    _NOTES_FILENAME,
    GeneratedSection,
    OmpTaskAgent,
    parse_outline_json,
)

# ===================== A · 行为实证 =====================


def test_a1_container_root_title_is_not_a_section():
    """dict-root 容器形态（title+children）：文档标题不再产出 level=0 条目。"""
    secs = parse_outline_json('{"title":"某桥梁专项施工方案目录","children":[{"title":"章1"}]}')
    assert [(s.level, s.title) for s in secs] == [(1, "章1")]
    # 全链不崩：fold 正常产出树
    tree = fold_generated_sections_to_tree(secs)
    assert [n["title"] for n in tree] == ["章1"]


def test_a2_sections_root_with_title_is_not_a_section():
    """{"sections":[...], "title":"T"} 混合形态：容器根 title 同样不产出。"""
    secs = parse_outline_json('{"sections":[{"title":"章1"}],"title":"文档标题"}')
    assert [(s.level, s.title) for s in secs] == [(1, "章1")]
    tree = fold_generated_sections_to_tree(secs)
    assert [n["title"] for n in tree] == ["章1"]


def test_a3_bare_dict_root_still_level1():
    """纯 dict（无 children/sections 键）走 else 分支 level=1，行为不变。"""
    secs = parse_outline_json('{"title":"章1"}')
    assert [(s.level, s.title, s.path) for s in secs] == [(1, "章1", "1")]


def test_a4_list_root_nested_unchanged():
    """list-root 嵌套形态逐字回归（既有契约保持）。"""
    secs = parse_outline_json(
        '[{"title":"章1","children":[{"title":"节1.1"},{"title":"节1.2",'
        '"children":[{"title":"子1.2.1"}]}]}]'
    )
    assert [(s.level, s.title, s.path) for s in secs] == [
        (1, "章1", "1"),
        (2, "节1.1", "1.1"),
        (2, "节1.2", "1.2"),
        (3, "子1.2.1", "1.2.1"),
    ]


def test_a5_fold_clamps_nonpositive_level():
    """防御钳制：level<=0 / None 不再 IndexError，按一级章处理。"""
    tree = fold_generated_sections_to_tree(
        [GeneratedSection(level=0, title="X"), GeneratedSection(level=2, title="X1")]
    )
    assert len(tree) == 1
    assert tree[0]["title"] == "X" and tree[0]["level"] == 1
    assert [c["title"] for c in tree[0]["children"]] == ["X1"]
    tree2 = fold_generated_sections_to_tree(
        [GeneratedSection(level=-1, title="Y"), GeneratedSection(level=1, title="Z")]
    )
    assert [n["title"] for n in tree2] == ["Y", "Z"]


def test_a6_fold_markdown_parse_shape_parity():
    """markdown 主路径形态经 fold 不受钳制影响（level>=1 逐字保持）。"""
    from app.services.ai.omp_task_agent import parse_outline_markdown

    secs = parse_outline_markdown("# 章一\n正文段\n## 节一\n### 子节\n")
    tree = fold_generated_sections_to_tree(secs)
    assert [n["title"] for n in tree] == ["章一"]
    assert [n["level"] for n in tree] == [1]
    assert [c["title"] for c in tree[0]["children"]] == ["节一"]
    assert [g["title"] for g in tree[0]["children"][0]["children"]] == ["子节"]


# ===================== B · 行为实证（NOTES.md 排除） =====================


def _make_agent() -> OmpTaskAgent:
    return OmpTaskAgent(enabled=True, binary="nonexistent_xyz_for_probe", model="probe-model")


def test_b1_notes_file_loses_fallback_priority():
    """OUTLINE.md 缺失、NOTES.md + outline.json 并存：outline.json 胜出，notes 照常读回。"""
    ws = tempfile.mkdtemp(prefix="r231_notes_")
    try:
        with open(os.path.join(ws, "NOTES.md"), "w", encoding="utf-8") as f:
            f.write("# 工作备忘\n\n- 今天完成了大纲初稿\n## 遗留问题\n- 需要补充工程概况\n")
        with open(os.path.join(ws, "outline.json"), "w", encoding="utf-8") as f:
            f.write('[{"title":"章1","children":[{"title":"节1.1"}]}]')
        secs, src, notes = _make_agent()._read_workspace(ws)
        assert src == "outline.json"
        assert [(s.level, s.title) for s in secs] == [(1, "章1"), (2, "节1.1")]
        # 元文件信息不丢：notes 字段照常单独读回
        assert "工作备忘" in notes
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def test_b2_notes_only_yields_empty_sections():
    """仅 NOTES.md（无任何产物文件）：空产物回到 generate_outline 层 RuntimeError 主语义。"""
    ws = tempfile.mkdtemp(prefix="r231_notes_only_")
    try:
        with open(os.path.join(ws, "NOTES.md"), "w", encoding="utf-8") as f:
            f.write("# 工作备忘\n")
        secs, src, notes = _make_agent()._read_workspace(ws)
        assert secs == [] and src == ""
        assert "工作备忘" in notes
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def test_b3_markdown_fallback_still_works():
    """非 NOTES 命名的其它 .md 产物仍被回退解析（回退能力不因排除元文件受损）。"""
    ws = tempfile.mkdtemp(prefix="r231_md_fallback_")
    try:
        with open(os.path.join(ws, "大纲草稿.md"), "w", encoding="utf-8") as f:
            f.write("# 章一\n## 节一\n")
        secs, src, _notes = _make_agent()._read_workspace(ws)
        assert src == "大纲草稿.md"
        assert [s.title for s in secs] == ["章一", "节一"]
    finally:
        shutil.rmtree(ws, ignore_errors=True)


# ===================== C · 源码棘轮 =====================


def test_c1_parse_guard_ratchet():
    """容器根 title 判据（level>=1）在位——防回流成无条件产出。"""
    import app.services.ai.omp_task_agent as ta

    src = inspect.getsource(ta.parse_outline_json)
    assert "if title and level >= 1:" in src


def test_c2_notes_exclusion_ratchet():
    """回退循环的 NOTES.md 排除判据在位。"""
    import app.services.ai.omp_task_agent as ta

    src = inspect.getsource(ta.OmpTaskAgent._read_workspace)
    assert "if fn == _NOTES_FILENAME:" in src


def test_c3_fold_clamp_ratchet():
    """fold 防御钳制在位（isinstance+下界判据，缺省钳到 1）。"""
    import app.services.ai.omp_outline_bridge as bridge

    src = inspect.getsource(bridge.fold_generated_sections_to_tree)
    assert "isinstance(s.level, int) and s.level >= 1" in src
    assert "s_level = s.level if" in src


# ===================== D · 字节体检 =====================


class TestByteHealth:
    """本轮触碰的 omp 接缝生产源：行尾无混装 / 无 BOM / 无 U+FFFD。"""

    FILES = [
        os.path.join("app", "services", "ai", "omp_task_agent.py"),
        os.path.join("app", "services", "ai", "omp_outline_bridge.py"),
    ]

    def test_line_endings_not_mixed(self):
        for rel in self.FILES:
            with open(rel, "rb") as f:
                raw = f.read()
            crlf = raw.count(b"\r\n")
            lf = raw.count(b"\n")
            assert crlf == lf, f"{rel} 行尾混装：CRLF={crlf} 总LF={lf}"

    def test_no_bom_no_fffd(self):
        for rel in self.FILES:
            with open(rel, "rb") as f:
                raw = f.read()
            assert not raw.startswith(b"\xef\xbb\xbf"), f"{rel} 带 BOM"
            # ⚠️ 检索针用转义 "\ufffd" 而非字面量——字面量会让本文件自带
            #    U+FFFD（R212 教训③现场复现：护栏文件先被自家断言击中）。
            assert raw.decode("utf-8", errors="replace").count("\ufffd") == 0, f"{rel} 含 U+FFFD"
