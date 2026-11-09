"""解析提取模块 R171 护栏（2026-11-03）：upload_outline 截断诊断 + _OUTLINE_PATTERNS 顺序。

背景：R171 对上传目录识别链路做深度审计，定稿两处真实缺陷：

  PARSE-3（P1）：截断诊断三态。
    - R181 起 raw_text 列（TEXT）容量足够 → **存全文**，不再裁到 5000 字，
      也不再回传 raw_text_truncated / 「截断至 5000 字」warning；
    - 解析器自身诊断（PDF 截页等）仍走 parse_truncated + parse_warnings；
    - 原文未超长时同样不回传截断键。
    根因：旧实现 parse_truncated / parse_warnings 从未随响应回传，
    用户看到「识别成功」却收不到「内容可能不完整」告警（与 global_facts 口径不一致）。

  PARSE-4（P2）：_OUTLINE_PATTERNS 顺序与 OCR 容错注释。
    - pattern 列表顺序固定（中文数字章 → 阿拉伯数字章 → 点分 → 单数字 →
      中文顿号 → 中文括号 → 阿拉伯括号 → 右括号），偏移即改变匹配优先级；
    - OCR 容错注释必须存在（说明「编号与分隔符之间允许 \\s*」的设计意图）。

本文件只覆盖上述两处（PARSE-1/2/5/6/7 滚递或登记不修）。
"""

from __future__ import annotations

import io
import re
import uuid
from pathlib import Path

import app.routers.upload_outline as uo
import app.services.file_parser as fp
import pytest
from app.db import get_conn, init_db
from starlette.datastructures import UploadFile

# ===========================================================================
# 工具
# ===========================================================================


def _upload(name: str = "outline.txt", text: str | bytes = "") -> UploadFile:
    if isinstance(text, str):
        text = text.encode("utf-8")
    return UploadFile(file=io.BytesIO(text), filename=name)


def _long_text(n_chars: int = 6000) -> str:
    """构造超长原文（>5000 字），用于验证 TEXT 列存全文（R181）。"""
    return ("第一章 工程概况\n" + "内容填充行用于构造超长原文。\n") * (n_chars // 20 + 1)


# ===========================================================================
# PARSE-3 · 截断诊断三态（R181：raw_text 落 TEXT 列 → 存全文，不再 5000 硬切）
# ===========================================================================


class TestParseTruncatedDiagnosis:
    """截断诊断三态：长原文存全文 / 不回传 raw_text_truncated / warning 形态。"""

    async def test_long_raw_text_stored_full(self, db_conn):
        """原文 >5000 字：raw_text 列 TEXT 容量足够 → 落库全文，响应无截断标记。"""
        text = _long_text(6000)
        assert len(text) > 5000
        res = await uo.parse_outline(file=_upload("long.txt", text), db=db_conn, scheme_name="", reorganize=False)
        assert "raw_text_truncated" not in res
        cur = await db_conn.execute("SELECT raw_text FROM uploaded_outlines WHERE id=?", (res["id"],))
        row = await cur.fetchone()
        assert row is not None
        assert row["raw_text"] == text, f"落库 raw_text 应存全文，实际 {len(row['raw_text'])} 字"

    async def test_long_raw_text_warning_not_5000(self, db_conn):
        """原文 >5000 字：不得再出现「保存记录时截断至 5000 字」的旧 warning。"""
        text = _long_text(6000)
        res = await uo.parse_outline(file=_upload("long.txt", text), db=db_conn, scheme_name="", reorganize=False)
        assert "5000 字" not in (res.get("warning") or "")
        cur = await db_conn.execute("SELECT raw_text FROM uploaded_outlines WHERE id=?", (res["id"],))
        row = await cur.fetchone()
        assert row is not None and row["raw_text"] == text

    async def test_non_truncated_no_parse_truncated_key(self, db_conn):
        """原文 ≤5000 字：响应不得出现 raw_text_truncated 键。"""
        text = "第一章 工程概况\n第二章 施工部署\n"
        assert len(text) <= 5000
        res = await uo.parse_outline(file=_upload("small.txt", text), db=db_conn, scheme_name="", reorganize=False)
        assert "raw_text_truncated" not in res
        assert res.get("raw_text_truncated") is None

    async def test_parse_truncated_flag_only_when_diag_truncated(self, db_conn, monkeypatch):
        """diag['truncated']=True 时回传 parse_truncated=True；否则不回传。"""

        # 用 monkeypatch 伪造 diag.truncated=True
        def _fake_parse(content, fname):
            return ("第一章 工程概况\n", {"truncated": True, "warnings": ["PDF 截页"]})

        monkeypatch.setattr(uo, "parse_file_content_ex", _fake_parse)
        text = b"fake content"
        res = await uo.parse_outline(file=_upload("doc.txt", text), db=db_conn, scheme_name="", reorganize=False)
        assert res.get("parse_truncated") is True
        assert "parse_truncated" in res
        assert res.get("parse_warnings") == ["PDF 截页"]

    async def test_parse_truncated_not_set_when_diag_not_truncated(self, db_conn, monkeypatch):
        """diag['truncated'] 非 True 时不得回传 parse_truncated 键。"""

        def _fake_parse(content, fname):
            return ("第一章 工程概况\n", {"truncated": False, "warnings": []})

        monkeypatch.setattr(uo, "parse_file_content_ex", _fake_parse)
        text = b"fake content"
        res = await uo.parse_outline(file=_upload("doc.txt", text), db=db_conn, scheme_name="", reorganize=False)
        assert "parse_truncated" not in res
        assert res.get("parse_truncated") is None

    async def test_empty_parse_warnings_not_returned(self, db_conn, monkeypatch):
        """parse_warnings 为空列表时响应不得带该键。"""

        def _fake_parse(content, fname):
            return ("第一章 工程概况\n", {"truncated": False, "warnings": []})

        monkeypatch.setattr(uo, "parse_file_content_ex", _fake_parse)
        text = b"fake content"
        res = await uo.parse_outline(file=_upload("doc.txt", text), db=db_conn, scheme_name="", reorganize=False)
        assert "parse_warnings" not in res
        assert res.get("parse_warnings") is None

    async def test_nonempty_parse_warnings_returned(self, db_conn, monkeypatch):
        """parse_warnings 非空时必须随响应回传。"""

        def _fake_parse(content, fname):
            return ("第一章 工程概况\n", {"truncated": False, "warnings": ["OCR 兜底"]})

        monkeypatch.setattr(uo, "parse_file_content_ex", _fake_parse)
        text = b"fake content"
        res = await uo.parse_outline(file=_upload("doc.txt", text), db=db_conn, scheme_name="", reorganize=False)
        assert res.get("parse_warnings") == ["OCR 兜底"]
        assert "parse_warnings" in res

    async def test_long_raw_text_no_legacy_5000_warning(self, db_conn):
        """R181：原文 >5000 字存全文，不得再出现「截断至 5000 字」的旧 warning。"""
        text = _long_text(6000)
        res = await uo.parse_outline(file=_upload("long.txt", text), db=db_conn, scheme_name="", reorganize=False)
        assert "5000" not in (res.get("warning") or "")
        cur = await db_conn.execute("SELECT raw_text FROM uploaded_outlines WHERE id=?", (res["id"],))
        row = await cur.fetchone()
        assert row is not None and row["raw_text"] == text


# ===========================================================================
# PARSE-4 · _OUTLINE_PATTERNS 顺序与 OCR 容错注释
# ===========================================================================


class TestOutlinePatternsOrder:
    """_OUTLINE_PATTERNS 顺序锁 + OCR 容错注释存在性。"""

    def test_patterns_order_fixed(self):
        """pattern 列表顺序固定：中文数字章 → 阿拉伯数字章 → 点分（无分隔符·R191）→ 点分 → 单数字 → 中文顿号 → 中文括号 → 阿拉伯括号 → 右括号。

        R191 重锚（意图不放宽）：新增「点分无分隔符直接跟汉字」条目（8→9 条），
        修复 "1.1工程概况" 被（有分隔符）点分条回溯命中错撕为 "1工程概况"。
        ⚠️ 新条必须排在「点分（要求分隔符）」之前：后者对 "1.1.1施工准备"
        会回溯命中（raw_id="1.1"+吃掉分隔点），更具体的无分隔符形态永远轮不到。
        """
        patterns = fp._OUTLINE_PATTERNS
        # 共 9 条
        assert len(patterns) == 9
        # 第 0 条：中文数字章（level=1，含 章/节/篇）
        assert patterns[0][0] == 1
        assert "部分|章|节|篇" in patterns[0][1].pattern
        # 第 1 条：阿拉伯数字章
        assert patterns[1][0] == 1
        assert r"\d+" in patterns[1][1].pattern and "部分|章|节|篇" in patterns[1][1].pattern
        # 第 2 条（R191 新增）：点分编号无分隔符直接跟汉字（level=None）
        assert patterns[2][0] is None
        assert "." in patterns[2][1].pattern
        # 汉字前瞻判据（用 "4e00" 子串断言，避开转义层级歧义）
        assert "4e00" in patterns[2][1].pattern, patterns[2][1].pattern
        # 第 3 条：点分编号（level=None，要求分隔符）
        assert patterns[3][0] is None
        assert "." in patterns[3][1].pattern
        # 第 4 条：单数字编号
        assert patterns[4][0] == 1
        assert r"\d+" in patterns[4][1].pattern
        # 第 5 条：中文数字顿号
        assert patterns[5][0] == 2
        assert "、" in patterns[5][1].pattern
        # 第 6 条：中文数字括号
        assert patterns[6][0] is None
        assert "（" in patterns[6][1].pattern or "(" in patterns[6][1].pattern
        # 第 7 条：阿拉伯数字括号
        assert patterns[7][0] == 3
        assert r"\d+" in patterns[7][1].pattern
        # 第 8 条：右括号编号（兜底）
        assert patterns[8][0] == 3
        assert ")" in patterns[8][1].pattern or "）" in patterns[8][1].pattern

    def test_ocr_robustness_comment_present(self):
        """OCR 容错注释必须存在（说明「编号与分隔符之间允许 \\s*」的设计意图）。"""
        src = Path(fp.__file__).read_text(encoding="utf-8")
        # 注释必须提及「容忍空格」或「\\s*」
        assert "容忍" in src or r"\s*" in src, "OCR 容错注释缺失"
        # 注释必须提及「编号」或「章节」
        assert "编号" in src or "章" in src, "OCR 容错注释未提及编号语义"

    def test_pattern_sequence_cannot_be_reordered(self):
        """模式顺序不得调换：首条必须是中文数字章，末条必须是右括号兜底。"""
        patterns = fp._OUTLINE_PATTERNS
        # 首条：中文数字章（含「章/节/篇」）
        assert "章" in patterns[0][1].pattern
        # 末条：右括号兜底
        assert patterns[-1][0] == 3


# ===========================================================================
# 反向验证 · A/B 锚点
# ===========================================================================


class TestAbortPoints:
    """A/B 反向验证锚点（变异后必红，还原后必绿）。"""

    def test_raw_text_slice_at_5000_removed(self):
        """反向锚点：R181 起 raw_text[:5000] 裸切片必须不存在。"""
        src = Path(uo.__file__).read_text(encoding="utf-8")
        norm = src.replace("\r\n", "\n")
        assert norm.count("raw_text[:5000]") == 0, "raw_text[:5000] 裸切片回流"

    def test_raw_truncated_guard_removed(self):
        """反向锚点：R181 起 raw_truncated = len(raw_text) > 5000 必须不存在。"""
        src = Path(uo.__file__).read_text(encoding="utf-8")
        assert "raw_truncated = len(raw_text) > 5000" not in src

    def test_parse_truncated_key_assignment(self):
        """锚点：parse_truncated 键赋值必须存在（if diag.get("truncated"): result["parse_truncated"] = True）。"""
        src = Path(uo.__file__).read_text(encoding="utf-8")
        assert 'result["parse_truncated"] = True' in src

    def test_parse_warnings_key_assignment(self):
        """锚点：parse_warnings 键赋值必须存在（if parse_warnings: result["parse_warnings"] = parse_warnings）。"""
        src = Path(uo.__file__).read_text(encoding="utf-8")
        assert 'result["parse_warnings"] = parse_warnings' in src


# ===========================================================================
# 回归锁 · 既有用例不回归
# ===========================================================================


class TestRegression:
    """回归锁：既有用例形态不得破坏。"""

    async def test_empty_text_returns_empty_text_flag(self, db_conn):
        """空白原文：响应回传 empty_text=True + warning（非 400）。"""
        res = await uo.parse_outline(file=_upload("ws.txt", b"   \n  \n"), db=db_conn, scheme_name="", reorganize=False)
        assert res.get("empty_text") is True
        assert "warning" in res

    async def test_normal_text_no_warning(self, db_conn):
        """正常原文（≤5000 字且非空白）：无 raw_text_truncated / empty_text / warning。"""
        text = "第一章 工程概况\n第二章 施工部署\n"
        res = await uo.parse_outline(
            file=_upload("normal.txt", text.encode("utf-8")), db=db_conn, scheme_name="", reorganize=False
        )
        assert "raw_text_truncated" not in res
        assert "empty_text" not in res
        assert "warning" not in res
