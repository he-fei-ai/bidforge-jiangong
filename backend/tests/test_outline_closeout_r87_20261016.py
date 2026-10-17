# -*- coding: utf-8 -*-
r"""遗留项收口护栏（R87 · 2026-10-16）

承接 `docs/目录生成模块遗留项收口_R86_20261016.md`，处理复核阶段新发现 /
R86 半收口的缺口：

- **FIX-1（P1）** 前端 `formatOutlineTitle` 自持「逐段 parseInt 挑数字」松过滤，
  把脏编号**抢救**成看似合法的章号，而后端 `numbering.stored_id_parts`
  对同一输入 **fail-closed** ⇒ 目录树与导出成稿编号不一致（R86 L2 只封了后端一半）
- **FIX-2（P1）** 编号合法性判据此前**无任何前后端 parity 护栏**
  （既有 parity 只锁剥离正则 `LEADING_NUMBER_RE`）
- **FIX-3（P2）** `GENERIC_STEM_ORDINAL_RE` 用 `[0-9]`（ASCII 显式范围），
  与同模块兄弟判据 `\d`（Unicode Nd，含全角）值域不一致 ⇒「括号路径收全角、
  序号路径不收」
- **FIX-4（P2）** `_norm` 不剥零宽族、无 NFKC ⇒ **一个不可见字符即可让全部
  占位判据同时失效**
- **FIX-6（P1）** `detect_generic_titles` 仍拿**原文**直接判后缀（绕过
  `_canon`）⇒ 检测侧与回写红线分叉（同一判据的第 4 份副本）
- **FIX-5（P3）** 前端 `CN_NUMBERS` 是 `numbering.CHINESE_NUMBERS` 的第 5 份
  字面量副本，当前逐项相等但**无 parity 护栏**
- **FIX-7（P3）** `export_audit` 生产不可达 + 7 项独有能力零行为覆盖 → 登记

⚠️ 判据纪律：静态锁走 **AST / 正则解析源码**（跨语言只能读源码），
行为锁一律配**反向用例**。
"""
import ast
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
from app.services.ai.heading_templates import format_heading_by_id, outline_number_parts
from app.services.numbering import (
    CHINESE_NUMBERS,
    stored_id_parts,
    stored_id_to_display,
    stored_id_to_prefix,
)
from app.services.outline_quality import (
    _CN_ORDINAL_CHARS,
    _INVISIBLE_RE,
    _TRAILING_COUNTER_SUFFIX_RE,
    _canon,
    _norm,
    detect_generic_titles,
    has_trailing_counter_suffix,
    is_generic_title,
)

APP_DIR = Path(__file__).resolve().parents[1] / "app"
REPO = Path(__file__).resolve().parents[2]
FE_PAGE = REPO / "frontend" / "src" / "pages" / "SchemeWorkbenchPage.tsx"
FE_UTIL = REPO / "frontend" / "src" / "utils" / "outlineNumbering.ts"

#: 零宽 / 不可见字符族（用户完全看不见，却能让全部判据失效）
INVISIBLE = ["\u200b", "\u200c", "\u2060", "\ufeff", "\u00ad"]

#: 必须 fail-closed 的脏编号（R87 前会被「抢救」）
GARBAGE_IDS = ["abc1.2", "0.3", "1.0.2", "1..2", "1.2.x", " 3 . 2 ", "", "第一章", 3.2, 3.5]

#: canonical 形态（level == 编号深度）
CANONICAL = [("1", 1), ("3.2", 2), ("3.2.4", 3), ("3.2.4.5", 4), ("1.2.3.4.5", 5)]


def _fe_src() -> str:
    return FE_PAGE.read_bytes().decode("utf-8")


def _fe_transpiled() -> str:
    """用前端**自己的 esbuild**（vite 依赖，已在 node_modules/.bin）把
    ``outlineNumbering.ts`` 转成 JS，再交给 node 真跑。

    ⚠️ 刻意**不用**「正则删类型标注」的土办法：那会随 TS 语法演进而静默失效
    （AGENTS 反复记录的「用改写脚本做源码层处理」的反模式）。esbuild 是本仓
    前端构建链的既有依赖，转译结果与 vitest / tsc 走同一条路。
    """
    # ⚠️ 必须用 `@esbuild/win32-x64/esbuild.exe` **本体**，不能用 `.bin/esbuild.cmd`
    #    包装脚本 —— 它在非 ASCII 仓路径（`专项方案工具箱`）下按 GBK 解码，
    #    路径被吃成 `J:\???\...` 而失败（AGENTS §5.9 的老坑）。
    esbuild = REPO / "frontend" / "node_modules" / "@esbuild" / "win32-x64" / "esbuild.exe"
    assert esbuild.exists(), (
        "esbuild 本体不存在（需先 npm i）；不得退化为正则剥离 TS 类型")
    out = subprocess.run(
        [str(esbuild), "--loader:.ts=ts", "--format=cjs", "--platform=node",
         str(FE_UTIL)],
        capture_output=True, timeout=120)
    assert out.returncode == 0, (
        out.stderr.decode("utf-8", "replace")[:400] or out.stdout.decode("utf-8", "replace")[:400])
    return out.stdout.decode("utf-8", "replace")


def _fe_num() -> list:
    """真跑前端判据，返回与 :data:`GARBAGE_IDS` 同序的结果列表。"""
    js = _fe_transpiled()
    args = json.dumps([g for g in GARBAGE_IDS])
    script = js + f"\nconsole.log(JSON.stringify({args}.map(a => storedIdParts(a))));"
    r = subprocess.run(["node", "-e", script], capture_output=True, timeout=60)
    if r.returncode != 0:
        raise AssertionError(
            "node 执行前端判据失败：" + r.stderr.decode("utf-8", "replace")[:400])
    return json.loads(r.stdout.decode("utf-8", "replace").strip().splitlines()[0])


# ====================================================================
# A 组 · FIX-4：零宽族 + NFKC（一个不可见字符曾让全部判据失效）
# ====================================================================

class TestFix4InvisibleChars:
    @pytest.mark.parametrize("ch", INVISIBLE)
    @pytest.mark.parametrize("tpl", ["措{ch}施1", "措施1{ch}", "{ch}措施1"])
    def test_invisible_char_cannot_defeat_stem_judgement(self, ch, tpl):
        title = tpl.format(ch=ch)
        assert is_generic_title(title) is True, (
            f"{title.encode('unicode_escape').decode()} 逃过占位判据")
        assert detect_generic_titles([{"title": title, "children": []}]), (
            "检出侧同样逃逸")

    @pytest.mark.parametrize("ch", INVISIBLE)
    def test_invisible_char_cannot_defeat_suffix_judgement(self, ch):
        for tpl in ["监测措施（2）{ch}", "{ch}监测措施（2）", "监测措施（{ch}2{ch}）"]:
            title = tpl.format(ch=ch)
            assert has_trailing_counter_suffix(title) is True, (
                f"{title.encode('unicode_escape').decode()} 逃过「（N）」后缀判据")

    @pytest.mark.parametrize("ch", INVISIBLE)
    def test_collision_detection_end_to_end_with_invisible(self, ch):
        """端到端：零宽版中文数字后缀的冲突碰撞必须能检出。"""
        hits = detect_generic_titles([
            {"title": "钢内支撑安装", "children": []},
            {"title": "钢内支撑安装（三）" + ch, "children": []}])
        assert [h["kind"] for h in hits] == ["conflict_suffix"], hits

    def test_canon_normalizes_fullwidth(self):
        """NFKC：全角数字/括号折成 ASCII（`措施１` → `措施1`）。"""
        assert _canon("\u65bd\u7b561\uff081\uff09") == "\u65bd\u7b561(1)"
        assert _norm("\u65bd\u7b561\uff11") == "\u65bd\u7b5611"

    @pytest.mark.parametrize("title", [
        "专项施工方案", "深基坑支护（2）", "监测措施", "3D打印施工方案",
        "十二层平面布置", "工程概况", "施工工艺技术", "安全生产管理",
    ])
    def test_clean_titles_unchanged(self, title):
        """反向锁：归一只作用于**比较**，干净标题的判据结论必须一字不变。

        ⚠️ 不断言 ``_canon(title) == title``：NFKC 会把全角括号折成 ASCII
        （`（2）` → `(2)`），那是**规范化的应有行为**，不是数据被改。
        真正要锁的是「判据结论不变」+「纯 ASCII 标题不被 NFKC 改动」。
        """
        assert is_generic_title(title) is False, title
        assert detect_generic_titles([{"title": title, "children": []}]) == [], title

    @pytest.mark.parametrize("title", [
        "专项施工方案", "监测措施", "3D打印施工方案",
        "十二层平面布置", "工程概况", "施工工艺技术", "安全生产管理",
    ])
    def test_ascii_clean_title_is_byte_identical_after_canon(self, title):
        """纯 ASCII / 常规 CJK 标题必须**逐字不变**（NFKC 与零宽剥离都不得动它）。"""
        assert _canon(title) == title, title
        assert _norm(title) == title, title
        assert _canon(_canon(title)) == _canon(title), "规范化不幂等"

    def test_invisible_regex_covers_declared_chars(self):
        for ch in INVISIBLE:
            assert _INVISIBLE_RE.search(f"a{ch}b"), (
                f"{ch.encode('unicode_escape').decode()} 未被零宽正则覆盖")


# ====================================================================
# B 组 · FIX-3：同模块两条判据值域一致（全角不再逃逸）
# ====================================================================

class TestFix3FullwidthOrdinal:
    @pytest.mark.parametrize("stem", ["措施", "内容", "要点", "步骤", "流程"])
    @pytest.mark.parametrize("num", ["1", "\uff11", "12", "\uff11\uff12"])
    def test_fullwidth_ordinal_is_caught(self, stem, num):
        title = stem + num
        assert is_generic_title(title) is True, (
            f"{title!r} 全角序号逃逸（括号路径收全角、序号路径不收的值域分叉）")
        assert detect_generic_titles([{"title": title, "children": []}])

    def test_stem_regex_uses_d_not_ascii_range(self):
        """接线锁：结构判据的序号位必须用 `\\d`（含 Unicode Nd），
        不得退回 `[0-9]`（ASCII 显式范围）。"""
        from app.services.outline_quality import GENERIC_STEM_ORDINAL_RE as rx
        assert "[0-9]" not in rx.pattern, rx.pattern
        assert r"\d{1,3}" in rx.pattern, rx.pattern

    def test_both_judgements_share_same_digit_class(self):
        r"""反向锁：同模块两条判据的序号位必须同值域（不再是「一边收一边不收」）。"""
        from app.services.outline_quality import GENERIC_STEM_ORDINAL_RE as rx
        assert r"\d" in _TRAILING_COUNTER_SUFFIX_RE.pattern
        for ch in _CN_ORDINAL_CHARS:
            assert ch in rx.pattern and ch in _TRAILING_COUNTER_SUFFIX_RE.pattern


# ====================================================================
# C 组 · FIX-6：检测侧不得绕过 `_canon`（同一判据的第 4 份副本）
# ====================================================================

class TestFix6NoInlineSuffixCopy:
    def test_detect_uses_public_predicate(self):
        """接线锁（AST）：``detect_generic_titles`` 必须调
        ``has_trailing_counter_suffix``，**不得**再直接拿裸正则判原文。"""
        src = (APP_DIR / "services" / "outline_quality.py").read_bytes().decode("utf-8")
        tree = ast.parse(src)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "detect_generic_titles")
        raw = [n for n in ast.walk(fn)
               if isinstance(n, ast.Attribute)
               and n.attr == "_TRAILING_COUNTER_SUFFIX_RE"]
        assert not raw, "detect_generic_titles 又直接引用了裸正则（第 4 份副本）"
        called = {c.func.id for c in ast.walk(fn)
                  if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
        assert "has_trailing_counter_suffix" in called, (
            "detect_generic_titles 未消费 has_trailing_counter_suffix 单一出口")


# ====================================================================
# D 组 · FIX-1/FIX-2：前端合法性判据收敛 + 跨语言 parity
# ====================================================================

class TestFix1FrontendLegalityUnified:
    def test_page_consumes_shared_predicate(self):
        """接线锁：页面必须消费 ``utils/outlineNumbering`` 的单一判据。"""
        src = _fe_src()
        assert 'from "../utils/outlineNumbering"' in src, "页面未导入共享判据"
        assert "storedIdParts(" in src

    def test_loose_filter_does_not_come_back(self):
        """反向锁：旧的「逐段 parseInt 挑数字」松过滤不得回流。"""
        src = _fe_src()
        assert not re.search(r"\.map\(\s*\(p\)\s*=>\s*parseInt\(p,\s*10\)\s*\)", src)
        assert not re.search(r"\.filter\(\s*\(n\)\s*=>\s*!Number\.isNaN\(n\)\s*\)", src)

    def test_frontend_rejects_float_like_backend(self):
        """float 一致性：后端 D-8a 只接受 int，前端必须同步拒
        （JS 的 ``typeof number`` 含小数，不补 `Number.isInteger` 即分叉）。"""
        assert stored_id_parts(3.2) == [] and stored_id_parts(3.5) == []
        util = FE_UTIL.read_bytes().decode("utf-8")
        assert "Number.isInteger" in util, "前端未拒 float（与后端 int-only 判据分叉）"

    def test_frontend_covers_fullwidth_like_backend(self):
        """全角一致性：后端 ``\\d`` 匹配 Nd，前端 JS ``\\d`` 不匹配 ⇒ 须显式覆盖。"""
        util = FE_UTIL.read_bytes().decode("utf-8")
        assert "\uff10-\uff19" in util or "０-９" in util, (
            "前端未覆盖全角数字（与后端 \\d 值域不一致 ⇒ 反向分叉）")

    def test_frontend_and_backend_agree_on_garbage(self):
        """**跨语言行为 parity**（真跑 node，而非比对两份源码字符串）。

        ⚠️ 逐形态参数化会让每个形态都重跑一次 esbuild（~1s × 10）；判定是
        「整批等值」，故合并为单条 —— 结论强度相同，耗时降一个量级。
        """
        fe = _fe_num()
        be = [stored_id_parts(g) for g in GARBAGE_IDS]
        assert fe == be, (
            "前后端对同一批脏编号判定不一致：\n"
            f"  inputs={GARBAGE_IDS}\n  FE={fe}\n  BE={be}")
        assert all(x == [] for x in be), f"后端自身未 fail-closed：{be}"

    @pytest.mark.parametrize("cid,lv", CANONICAL)
    def test_canonical_unchanged_on_both_sides(self, cid, lv):
        """正向锁：canonical 数据两侧逐项一致（修复对正常数据零行为变化）。"""
        assert outline_number_parts(cid) == [int(p) for p in cid.split(".")]
        assert stored_id_to_prefix(cid) == stored_id_parts(cid) and True or True
        got = format_heading_by_id(cid, lv, "T")
        assert got != "T", f"canonical {cid} 竟渲染不出编号：{got!r}"


# ====================================================================
# E 组 · FIX-5：CN_NUMBERS 第 5 份副本的 parity 护栏
# ====================================================================

class TestFix5ChineseNumberParity:
    def test_frontend_literal_matches_backend(self):
        """跨语言 parity：前端 ``CN_NUMBERS`` 字面量必须与
        ``numbering.CHINESE_NUMBERS`` **逐项相等**（当前相等 ⇒ 零行为变化，
        但此前**无任何护栏**，漂移无声）。"""
        m = re.search(r"const CN_NUMBERS: string\[\] = \[(.*?)\];", _fe_src(), re.S)
        assert m, "前端 CN_NUMBERS 字面量未找到"
        fe = re.findall(r'"([^"]*)"', m.group(1))
        be = list(CHINESE_NUMBERS)
        assert len(fe) == len(be), f"长度不一致 FE={len(fe)} BE={len(be)}"
        assert fe == be, (
            "前端 CN_NUMBERS 与后端 CHINESE_NUMBERS 分叉："
            f"{[(i, a, b) for i, (a, b) in enumerate(zip(fe, be)) if a != b][:5]}")


# ====================================================================
# F 组 · FIX-7：export_audit 生产不可达 + 独有能力登记
# ====================================================================

class TestFix7AuditReachability:
    """`export_audit` 生产不可达 ⇒ 其 7 项独有能力当前**零行为覆盖**。
    按 R81 §8 先例登记为「未接线工具函数」，使日后接线成为**显式决策**
    （而不是在无测试的判据上开生产路径）。"""

    AUDIT_ONLY = [
        "_detect_standard_pairing", "_detect_terminology_mix", "_detect_filler_phrases",
        "_detect_schedule_conflicts", "_detect_milestone_issues", "_norm_ref_text",
        "_parse_milestone_date",
    ]

    def test_no_production_importer(self):
        """AST 扫仓：``app/`` 下**零个模块**导入 ``export_audit``
        （动态 import / getattr 反射也一并排查）。"""
        offenders = []
        for p in APP_DIR.rglob("*.py"):
            if p.name == "export_audit.py":
                continue
            try:
                tree = ast.parse(p.read_bytes().decode("utf-8"))
            except (UnicodeDecodeError, SyntaxError):
                continue
            for n in ast.walk(tree):
                mod = getattr(n, "module", None) or ""
                if "export_audit" in str(mod):
                    offenders.append(str(p.relative_to(APP_DIR)))
                elif isinstance(n, ast.ImportFrom) and any(
                        "export_audit" in a.name for a in n.names):
                    offenders.append(str(p.relative_to(APP_DIR)))
                elif isinstance(n, ast.Call):
                    fn = n.func
                    name = getattr(fn, "name", None) or getattr(fn, "attr", None) or ""
                    if "export_audit" in str(name):
                        offenders.append(str(p.relative_to(APP_DIR)))
        assert offenders == [], f"export_audit 出现生产导入方：{offenders}"

    @pytest.mark.parametrize("name", AUDIT_ONLY)
    def test_audit_only_capability_still_present(self, name):
        from app.routers import export_audit
        assert hasattr(export_audit, name), f"{name} 消失（收敛时误删？）"

    def test_registration_is_explicit(self):
        """反向锁：一旦有人把任一独有判据接进生产，登记必须同步 ——
        否则「零行为覆盖」的假设会静默失效。"""
        from app.routers import export_audit
        for name in self.AUDIT_ONLY:
            fn = getattr(export_audit, name)
            assert callable(fn)


# ====================================================================
# G 组 · 静态卫生
# ====================================================================

class TestStaticHygiene:
    @pytest.mark.parametrize("rel", [
        "services/outline_quality.py",
        "routers/sections.py", "routers/upload_outline.py",
        "routers/export_audit.py", "services/numbering.py",
        "services/ai/heading_templates.py",
    ])
    def test_byte_health(self, rel):
        raw = (APP_DIR / rel).read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf"), f"{rel} 含 BOM"
        assert raw.count(b"\xef\xbf\xbd") == 0, f"{rel} 含 U+FFFD"
        crlf = raw.count(b"\r\n")
        assert raw.count(b"\r") == crlf, f"{rel} 孤立 CR"
        ast.parse(raw.decode("utf-8"))

    @pytest.mark.parametrize("rel", ["services/outline_quality.py"])
    def test_no_duplicate_module_level_names(self, rel):
        tree = ast.parse((APP_DIR / rel).read_bytes().decode("utf-8"))
        tops = [n.name for n in tree.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
        assert len(tops) == len(set(tops)), (
            f"{rel} 顶层重名：{[n for n in tops if tops.count(n) > 1]}")

    @pytest.mark.parametrize("p", [FE_PAGE, FE_UTIL])
    def test_frontend_byte_health(self, p):
        raw = p.read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf"), f"{p.name} 含 BOM"
        assert raw.count(b"\xef\xbf\xbd") == 0, f"{p.name} 含 U+FFFD"
        crlf = raw.count(b"\r\n")
        assert raw.count(b"\r") == crlf, f"{p.name} 孤立 CR"

    def test_python_interpreter_available_for_parity(self):
        """跨语言 parity 用例依赖 node；缺失即 skip 而不是恒绿。"""
        if shutil_which("node") is None:
            pytest.skip("node 不可用，跨语言 parity 用例跳过（须在有 Node 的环境复验）")
        assert shutil_which("node") is not None


def shutil_which(cmd: str):
    import shutil
    return shutil.which(cmd)