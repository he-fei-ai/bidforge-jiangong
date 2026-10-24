"""R118 · 六模块跨模块收口（2026-10-21）· 第一组：围栏递归 src_line 回归。

【缺陷来源】`-k outline` 全量基线跑出 1 项稳定失败：
``tests/test_outline_generation_gaps_20261008.py::TestEncodingAndLargePayload
::test_heading_inside_code_fence_with_unicode_is_untouched``

【根因链（已取证）】R117 给 ``_parse_content_blocks`` 加了「`` ```markdown `` 家族围栏
递归解析」能力（修复成稿里整段 Markdown 退化成一个巨型段落），但递归调用传的是
**围栏内部文本**，于是两个字块缺陷：

1. **src_line 变成围栏内相对行号**（本应是全文绝对行号）。``src_line`` 的唯一消费方
   ``numbering.renumber_section_body_subheadings`` 把它当 ``content.split("\n")``
   的索引去**直接覆盖源行** ⇒ 实测输入
   `` ```markdown␊＃＃ 围栏内全角井号␊## 围栏内标题围栏内标题␊```␊␊## 真标题 ``
   的内层块 src_line=1，而外层 ``lines[1]`` 恰好是全角井号行 ⇒
   **该行被整行覆盖（原文永久丢失）**。
2. **围栏内标题吃掉本节子标题序号** ⇒ 真标题从 2.1 被顺延成 2.2（编号虚跳）；
   而导出侧 ``_collapse_repeated_blocks`` 还会对「围栏内一份 + 围栏外重述一份」
   去重渲染 ⇒ 落库阶段白白消耗的序号在成稿里表现为「有 2.1、无 2.2」的空洞。

【修法】① 递归时换算 ``_line_offset``（内容首行的全文绝对行号）；
② 递归产出的块打 ``_from_fence`` 标记；③ 编号侧两遍统一走 ``_renumberable``
（不含围栏派生块）—— 保住 D-8g 既有契约「围栏内内容一概不动」。

本文件分五组：A 解析层行号 / B 编号层不占号 / C 数据丢失回归 / D 嵌套与幂等 /
E 子进程输出解码容错。
"""
import inspect
import locale
import subprocess

import pytest
from app.services.content_blocks import _parse_content_blocks
from app.services.numbering import renumber_section_body_subheadings
from app.utils import process_cleanup as _pc

MD_FENCE_SRC = "```markdown\n＃＃ 围栏内全角井号\n## 围栏内标题\n```\n\n## 真标题\n"


def _headings(content: str) -> list:
    return [b for b in _parse_content_blocks(content)
            if b.get("type") == "heading"]


# ============================================================
# A 组：src_line 必须是**全文绝对行号**（解析层契约）
# ============================================================
class TestSrcLineIsAbsolute:
    """src_line 是「0 基、相对最外层原文」的行号，递归层不得退化为相对号。"""

    def test_top_level_heading_src_line_is_absolute(self):
        """基线：无围栏时行号与肉眼一致（防止把absolue改坏都不知道）。"""
        content = "正文段落。\n\n## 甲标题\n\n tail\n"
        hs = _headings(content)
        assert len(hs) == 1
        assert hs[0]["src_line"] == 2, hs

    def test_fence_inner_heading_src_line_is_absolute(self):
        """A-2（核心）：围栏内标题的 src_line 必须落到它真正的行上。

        变形前内层块的 src_line=1（围栏内相对号），外层第 1 行是全角井号行。
        """
        hs = _headings(MD_FENCE_SRC)
        # 围栏内 "## 围栏内标题" 位于第 2 行（0 基）；"## 真标题" 位于第 5 行
        assert sorted(h["src_line"] for h in hs) == [2, 5], hs
        assert hs[0]["_from_fence"] is True, "围栏派生块必须打标"
        assert hs[1].get("_from_fence") is None, "围栏外标题不得被误标"

    def test_src_line_points_at_the_heading_itself(self):
        """A-3：src_line 必须能真的取回该标题行（不是"看着像行号"）。"""
        lines = MD_FENCE_SRC.split("\n")
        for h in _headings(MD_FENCE_SRC):
            src = h["src_line"]
            assert isinstance(src, int) and 0 <= src < len(lines)
            assert h["text"] in lines[src], (
                f"src_line={src} 没有指到标题自身: {lines[src]!r}")

    def test_src_line_absolute_after_leading_prose(self):
        """A-4：围栏前面有内容时偏移必须累加（验证 _line_offset 真的被用上）。"""
        content = "引言段落一。\n\n引言段落二。\n\n" + MD_FENCE_SRC
        hs = _headings(content)
        assert sorted(h["src_line"] for h in hs) == [6, 9], hs


# ============================================================
# B 组：编号侧 —— 围栏派生标题不占号、不被重写
# ============================================================
class TestRenumberSkipsFenceDerivedHeadings:
    """既保住 D-8g 契约，也避免"编号虚跳"。"""

    def test_fence_content_untouched(self):
        """B-1：围栏内容一个字节都不能变。"""
        new, changes = renumber_section_body_subheadings(
            MD_FENCE_SRC, "3.2", 2, "进度计划")
        assert "＃＃ 围栏内全角井号" in new
        assert "## 围栏内标题" in new
        assert len(changes) == 1, changes

    def test_real_heading_gets_first_number(self):
        """B-2（编号虚跳回归）：围栏内标题不得吃掉 2.1。"""
        new, changes = renumber_section_body_subheadings(
            MD_FENCE_SRC, "3.2", 2, "进度计划")
        assert "## 2.1 真标题" in new, new
        assert changes[0]["new"] == "2.1 真标题", changes

    def test_text_fence_still_untouched(self):
        """B-3（回归锚）：普通代码围栏语义不受本次改动影响。"""
        content = "```text\n## 不是标题\n```\n\n## 甲\n\n## 乙\n"
        new, changes = renumber_section_body_subheadings(
            content, "3.2", 2, "进度计划")
        assert "## 不是标题" in new
        assert [c["new"] for c in changes] == ["2.1 甲", "2.2 乙"], changes


# ============================================================
# C 组：数据丢失回归（本轮最严重的一项）
# ============================================================
class TestNoDataLoss:
    """src_line 错位会把无关行整行覆盖 —— 这是静默的原文销毁。"""

    def test_fullwidth_line_survives(self):
        """C-1：全角井号行必须原样存活（D-8g 的失败点）。"""
        new, _ = renumber_section_body_subheadings(
            MD_FENCE_SRC, "3.2", 2, "进度计划")
        assert new.count("＃＃") == 1, "全角井号行被改写"
        assert "围栏内全角井号" in new, "全角井号行内容丢失"

    def test_no_unrelated_line_is_overwritten(self):
        """C-2：围栏前后的所有非标题行必须逐字保留。"""
        content = ("前言第一行。\n\n" + MD_FENCE_SRC + "\n尾行。\n")
        new, _ = renumber_section_body_subheadings(
            content, "3.2", 2, "进度计划")
        for keep in ("前言第一行。", "尾行。", "＃＃ 围栏内全角井号",
                     "## 围栏内标题", "```markdown"):
            assert keep in new, f"原文片段被销毁: {keep!r}"

    def test_line_count_is_stable(self):
        """C-3：规范化不得增删行数（{标题}:=?的行一定漂移了）。

        renumber 只做"行内重写"，行数守恒是比内容比对更强的结构性判据。
        """
        for content in (MD_FENCE_SRC,
                        "引言。\n\n" + MD_FENCE_SRC,
                        MD_FENCE_SRC + "\n## 后标题\n"):
            new, _ = renumber_section_body_subheadings(
                content, "3.2", 2, "进度计划")
            assert len(new.split("\n")) == len(content.split("\n")), (
                f"行数被改写: {len(content.split(chr(10)))} → "
                f"{len(new.split(chr(10)))}")


# ============================================================
# D 组：嵌套 / 幂等 / fail-soft
# ============================================================
class TestNestedAndIdempotent:

    def test_nested_fence_offset_accumulates(self):
        """D-1：两层嵌套时行号必须累加到最外层绝对坐标。

        外层刻意用 **4 反引号**、内层用 **波浪号**：闭合判定是 ``tolerant_length=True``
        （`read_fenced_block`，短围栏也可闭合长围栏 —— 为兼容 AI 写错围栏长度的有意
        取舍，**不是缺陷，不要改**），所以同字符围栏根本测不到嵌套；换不同围栏字符
        才能真实构造两层嵌套。
        行号预算（0 基）：0 前言 / 2 开外层 / 3 前导 / 5 开内层 /
        6 内层标题 / 7 闭内层 / 9 闭外层 / 11 外层标题。
        """
        content = (
            "前言。\n\n"
            "````markdown\n"
            "前导。\n\n"
            "~~~markdown\n"
            "## 内层标题\n"
            "~~~\n\n"
            "````\n\n"
            "## 外层标题\n"
        )
        hs = _headings(content)
        texts = {h["text"]: h["src_line"] for h in hs}
        assert texts.get("内层标题") == 6, texts
        assert texts.get("外层标题") == 11, texts
        assert hs[0].get("_from_fence") is True, "嵌套派生块同样要打标"

    def test_renumber_is_idempotent_over_markdown_fence(self):
        """D-2：二次运行必须零改动（否则落库会反复写入不同版本）。"""
        new, changes = renumber_section_body_subheadings(
            MD_FENCE_SRC, "3.2", 2, "进度计划")
        assert changes, "首次运行应有改动"
        twice, changes2 = renumber_section_body_subheadings(
            new, "3.2", 2, "进度计划")
        assert twice == new, "围栏用例下规范化不幂等"
        assert changes2 == [], changes2

    def test_unclosed_markdown_fence_does_not_crash(self):
        """D-3：未闭合 markdown 围栏（max_tokens 截断常见）必须 fail-soft。"""
        content = "```markdown\n## 标题A\n\n正文。\n"
        new, changes = renumber_section_body_subheadings(
            content, "3.2", 2, "进度计划")
        assert "## 标题A" in new
        assert changes == [], changes


# ============================================================
# E 组：子进程输出解码容错（_process_cleanup._pid_alive）
# ------------------------------------------------------------
# 取证：后端全量回归日志（logs/full_r118_after_fix1.txt）里出现了真实的
#   UnicodeDecodeError: 'gbk' codec can't decode byte 0xa1 in position 23
#   File "...\subprocess.py", line 1614, in _readerthread
# 说明仍有命名空间外的同族调用点未被 mermaid_service.py:353-372 的修复覆盖。
# ============================================================
# GBK / UTF-8 都解不出来的字节（0xff 既非 GBK 合法首字节、也非 UTF-8 合法字节）
_BAD = b"\xff\xfe tasklist output line\n"


def _fake_run(monkeypatch, raw: bytes):
    """忠实模拟 ``subprocess.run`` 的 text 模式解码行为。

    真实现是在后台 ``_readerthread`` 里解码，编码取 locale、严格模式不解
    ``errors="replace"`` 时**抛 UnicodeDecodeError**；这里按传入 kwargs 复刻，
    因此「删掉 errors="replace"」这条变异会被 E-2/E-3 真正打到。
    """
    def _run(cmd, **kw):
        if kw.get("encoding") or kw.get("text"):
            enc = kw.get("encoding") or locale.getpreferredencoding(False)
            if (kw.get("errors") or "strict") == "strict":
                raw.decode(enc)  # 与 _readerthread 同款：严格模式直接抛
            out = raw.decode(enc, errors=kw.get("errors") or "strict")
        else:
            out = raw
        return subprocess.CompletedProcess(list(cmd), 0, stdout=out, stderr="")
    monkeypatch.setattr(_pc.subprocess, "run", _run)


class TestSubprocessOutputDecoding:

    def test_pid_alive_declares_replace_errors(self):
        """E-1：源码级契约 —— 与已修 Rhe 兄弟 mermaid_service 同口径。"""
        src = inspect.getsource(_pc._pid_alive)
        assert "text=True" in src
        assert 'errors="replace"' in src, (
            "Windows 子进程输出含 locale 无法解码的字节时会在 _readerthread 抛 "
            "UnicodeDecodeError（全量回归已取证），必须 errors=replace")

    @pytest.mark.skipif(not _pc.IS_WIN, reason="该分支仅 Windows 生效")
    def test_absent_pid_is_reported_dead_even_with_bad_bytes(self, monkeypatch):
        """E-2（核心）：输出里有坏字节时，仍要正确判定「进程已不存在」。

        修好的情况应返回 False；一旦删掉 errors="replace"，内部抛异常被
        ``except Exception: return True`` 吞掉 ⇒ 变成「保守判活」= **误判**。
        """
        _fake_run(monkeypatch, _BAD)
        assert _pc._pid_alive(4242) is False

    @pytest.mark.skipif(not _pc.IS_WIN, reason="该分支仅 Windows 生效")
    def test_present_pid_is_reported_alive(self, monkeypatch):
        """E-3（回归锚）：PID 确实出现在输出里时必须判活。"""
        _fake_run(monkeypatch, _BAD.replace(b"line", b"Console  4242"))
        assert _pc._pid_alive(4242) is True

    def test_run_child_defaults_are_lenient(self):
        """E-4：子进程执行器默认止血（不得退化成 strict）。"""
        sig = inspect.signature(_pc.run_child)
        assert sig.parameters["errors"].default == "replace", sig
        assert sig.parameters["encoding"].default == "utf-8", sig
