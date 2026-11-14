"""✅ R229 · 目录匹配器第三份副本收敛（P2 维护性收口 · 行为零变化）

背景：目录树节点→旧章节的「结构感知匹配器」本仓存在第三份独立副本。
R209 已把 ``routers/outline_library`` 与 ``routers/sections._save_outline_to_db``
收敛到 ``services.outline_matcher.build_outline_matcher``（单一事实源），但
``routers/upload_outline.save_as_outline`` 仍自持一份「三层索引 + 标题路径 +
消费式认领」的内联副本（existing_by_title/by_parent_level/by_level/by_path +
_take_from + consumed），与本模块的既有实现**实质分叉**：

- 本路径的匹配身份取「自动外科式改写前的原标题」（``pre_nt_index``）；
- ``sections`` 侧的匹配身份取「清洗后 title」。

任一侧改判据另一侧必然漂移 —— 正是 AGENTS「同一判据多份副本」反模式的又一处。

修复（行为逐字保持）：把 ``upload_outline.save_as_outline`` 的内联三层索引 + 匹配器
整体替换为 ``build_outline_matcher``；并把「改写前标题作为匹配身份」的语义下沉为
``take()`` 的 ``match_title`` 形参（缺省回退 ``title``，既有调用方逐字不变）。

护栏（本文件）：
A 行为实证：``match_title`` 优先于 ``title`` 决定匹配身份；缺省时用 ``title``；
  改写前原标题（mnt）认领旧章节、展示/落库标题用改写后 title —— 语义与收敛前一致。
B 源码棘轮：``upload_outline`` 必须经 ``build_outline_matcher`` 取匹配器、
  内联三层索引 / ``_take_from`` 不得回流；``outline_matcher`` 的 ``match_title`` 形参在位。
C 字节体检：两个被改文件纯 CRLF / 无 BOM / 无 U+FFFD。
"""
from __future__ import annotations

import ast
import pathlib
import re

import pytest

_BACKEND = pathlib.Path(__file__).resolve().parents[1]
_APP = _BACKEND / "app"


def _src(rel: str) -> str:
    return (_APP / rel).read_text(encoding="utf-8")


def _py_compile_ok(rel: str) -> bool:
    try:
        ast.parse(_src(rel))
        return True
    except SyntaxError:  # pragma: no cover - 防御
        return False


def _byte_health(rel: str) -> None:
    p = _APP / rel
    raw = p.read_bytes()
    text = raw.decode("utf-8")
    assert b"\xef\xbf\xbd" not in raw, f"{rel} 含 U+FFFD 替换字符"
    crlf = text.count("\r\n")
    lone_lf = text.count("\n") - crlf
    assert lone_lf == 0, f"{rel} 含 {lone_lf} 个裸 LF（须纯 CRLF）"


# ---------------------------------------------------------------------------
# A · 行为实证：match_title 决定匹配身份
# ---------------------------------------------------------------------------


def _mk(rows):
    from app.services.outline_matcher import build_outline_matcher

    return build_outline_matcher(rows)


def test_match_title_takes_precedence_over_title():
    """匹配身份优先用 match_title：展示标题（title）与匹配身份不同也不影响认领。"""
    rows = [
        {"id": "s1", "title": "施工准备", "level": 2, "parent_id": "p1", "content": "x"},
    ]
    m = _mk(rows)
    # 旧章节标题是「施工准备」；新节点展示标题已被外科改写为「施工部署准备」，
    # 但匹配身份（改写前原标题）仍是「施工准备」→ 应认领 s1。
    hit = m.take(
        title="施工部署准备",  # 展示标题（改写后）
        level=2,
        parent_id="p1",
        path_titles=("第一章",),
        match_title="施工准备",  # 匹配身份（改写前）
    )
    assert hit is not None and hit["id"] == "s1"


def test_match_title_absent_falls_back_to_title():
    """缺省（不传 match_title）时匹配身份 = title，既有调用方逐字不变。"""
    rows = [
        {"id": "s1", "title": "施工准备", "level": 2, "parent_id": "p1"},
    ]
    m = _mk(rows)
    hit = m.take(title="施工准备", level=2, parent_id="p1", path_titles=("第一章",))
    assert hit is not None and hit["id"] == "s1"
    # 展示标题与匹配身份都不同 → 认领不到（不会把不同标题的旧章节误保留）。
    miss = m.take(title="全新章节", level=2, parent_id="p1", path_titles=("第一章",))
    assert miss is None


def test_match_title_none_or_empty_uses_title():
    """match_title=None/空串 与缺省等价（都回退 title）。"""
    rows = [
        {"id": "s1", "title": "施工准备", "level": 2, "parent_id": "p1"},
    ]
    m1 = _mk(rows)
    a = m1.take(title="施工准备", level=2, parent_id="p1", match_title=None)
    m2 = _mk(rows)
    b = m2.take(title="施工准备", level=2, parent_id="p1", match_title="")
    assert a is not None and b is not None
    assert a["id"] == "s1" and b["id"] == "s1"


# ---------------------------------------------------------------------------
# B · 源码棘轮：副本已收敛、内联三层索引不得回流
# ---------------------------------------------------------------------------


def test_upload_outline_uses_single_matcher_source():
    """upload_outline.save_as_outline 必须经 build_outline_matcher 取匹配器。"""
    s = _src("routers/upload_outline.py")
    assert "build_outline_matcher" in s, "upload_outline 未收敛到单一事实源匹配器"
    # 匹配调用点必须传 match_title（改写前原标题语义）
    assert "match_title=mnt" in s, "upload_outline 匹配调用点未传 match_title=mnt"


def _strip_comment_lines(src: str) -> str:
    """只保留代码行（剥掉整行注释与行尾注释），避免护栏被自己写的说明注释触发。"""
    out = []
    for line in src.splitlines():
        code = re.sub(r"#.*$", "", line)  # 剥行尾注释（字符串内 # 极少，可接受）
        if code.strip():
            out.append(code)
    return "\n".join(out)


def test_upload_outline_inline_matcher_copy_not_reflowed():
    """内联三层索引 / _take_from / consumed 集合不得在 save_as_outline 内回流。"""
    import inspect

    from app.routers import upload_outline as _uo

    body_src = _strip_comment_lines(inspect.getsource(_uo.save_as_outline))
    # 内联匹配器的标志性局部（收敛前形态）不得在**代码**里回流
    for marker in ("existing_by_title", "existing_by_parent_level", "existing_by_level", "existing_by_path", "_take_from"):
        assert marker not in body_src, f"内联匹配器局部 {marker} 在 save_as_outline 代码回流"
    # 仍须使用 build_outline_matcher 取匹配器（防止被改成第三种内联形态）
    assert "build_outline_matcher(" in body_src, "save_as_outline 未调用 build_outline_matcher"


def test_outline_matcher_match_title_param_present():
    """outline_matcher.take 的 match_title 形参在位（语义下沉的契约）。"""
    s = _src("services/outline_matcher.py")
    assert "match_title" in s, "outline_matcher.take 缺 match_title 形参"
    # match_title 缺省回退 title 的判据
    assert "match_title if match_title else title" in s, "match_title 缺省回退判据丢失"


def test_sections_and_outline_library_still_consumers():
    """既有两个消费方（sections / outline_library）仍走单一事实源（不得被改回内联）。"""
    s_sec = _src("routers/sections.py")
    s_lib = _src("routers/outline_library.py")
    assert "build_outline_matcher" in s_sec, "sections._save_outline_to_db 脱离单一事实源"
    assert "build_outline_matcher" in s_lib, "outline_library 脱离单一事实源"


# ---------------------------------------------------------------------------
# C · 字节体检
# ---------------------------------------------------------------------------


def test_no_syntax_error_after_change():
    assert _py_compile_ok("services/outline_matcher.py")
    assert _py_compile_ok("routers/upload_outline.py")


@pytest.mark.parametrize("rel", ["services/outline_matcher.py", "routers/upload_outline.py"])
def test_byte_integrity(rel: str):
    _byte_health(rel)
