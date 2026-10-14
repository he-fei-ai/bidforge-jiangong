"""R70（2026-10-12）字数口径「诊断专用读数」护栏。

背景：正文链体检发现 `text_word_count` 是「剔围栏后的**字符数**」，中文标点 /
Markdown 结构符 / URL / 空白换行全部计入；而预算数值（800/1200/2000/2500）是按
人的「字数」直觉定的。同批 33 章真实语料实测 mean 偏差 136.9%（字符口径） vs
106.5%（可读口径）。本轮**只加一个对照读数**接到 preflight DLV-16 明细，
不动摇任何判定 —— 所以本护栏的核心职责不是证明读数正确，而是**证明它没越界**。

覆盖：
A 纯函数行为：标点不计 / 围栏与图片 URL HTML 行内码剔除 / 西文按词 /
  结构符剔除 / LF 与 CRLF 同值 / None 与空串 → 0。
B 单一事实源：`_normalize_for_readable` 必须复用 `strip_fenced_code_blocks`
  （AST 锁），全模块不得再有 `word_like_count` 死代码。
C 既有结论逐字不变：`word_audit_summary` 键集合锁死；DLV-16 对纯 ASCII fixture
  的 evidence 串逐字不变（既有 85 例的断言依赖它）；中文 fixture 只**追加**
  对照读数，ratio / 判定结论与「超目标」文案一字不动。
D 接线：preflight import 两函数 + DLV-16 分支消费；sse_handlers 默认值表达式
  逐字未动（`_shrink_default = ...`），注释已订正为「136.9% 是字符口径」。
E **反向锁**（本轮最关键）：全仓 app/** 中 `readable_word_count` / `has_cjk` 的
  消费点**只允许**是 `preflight_engine.check_deliverability`；判定与落库路径
  （word_status_for / word_deviation_of / _should_auto_shrink / 任何写
  word_count 的语句）引用即红 —— 防「诊断读数」悄悄长成第二把执行尺子
  （易标报告 §10 薄弱点 1 与本仓 R55/R62 反复警告的同族反例）。
F 字节体检：触碰文件 CR==LF / 无 BOM / 无 U+FFFD / AST 无重复顶层名。
"""
from __future__ import annotations

import ast
import pathlib

from app.services.content_utils import (
    has_cjk,
    readable_word_count,
    text_word_count,
    word_audit_summary,
)
from app.services.preflight_engine import PreflightContext, check_deliverability

_BACKEND = pathlib.Path(__file__).resolve().parents[1]
_APP = _BACKEND / "app"
_SRC = {
    name: (_APP / rel).read_text(encoding="utf-8")
    for name, rel in {
        "content_utils": "services/content_utils.py",
        "preflight": "services/preflight_engine.py",
        "sse": "routers/sse_handlers.py",
        "sections": "routers/sections.py",
    }.items()
}

#: 诊断专用入口（唯一允许被反向锁枚举的名字）
DIAGNOSTICS = {"readable_word_count", "has_cjk"}
#: 唯一许可消费点：(app 下相对路径, 顶层函数名)
ALLOWED_CONSUMERS = {("services/preflight_engine.py", "check_deliverability")}


def _consumer_sites() -> set[tuple[str, str]]:
    """扫全仓 app/**.py，返回「在哪个文件的哪个函数里调用了诊断函数」。

    ⚠️ 名字匹配取 `ast.Name.id` —— 写成 `.name` 会永远匹配不到（本轮探针踩过）。
    """
    sites: set[tuple[str, str]] = set()
    for p in sorted(_APP.rglob("*.py")):
        tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
        for top in ast.walk(tree):
            if not isinstance(top, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for call in ast.walk(top):
                if (isinstance(call, ast.Call)
                        and isinstance(call.func, ast.Name)
                        and call.func.id in DIAGNOSTICS):
                    sites.add((p.relative_to(_APP).as_posix(), top.name))
    return sites


# ===========================================================================
# A 纯函数行为
# ===========================================================================
class TestReadableWordCount:
    def test_punctuation_not_counted(self):
        # 「，。」和「:」不计；12 = 基坑支护采用放坡(8) 坡比(2) + 1:1.2(1) + 共…
        assert text_word_count("基坑支护采用放坡，坡比1:1.2。") == 17
        assert readable_word_count("基坑支护采用放坡，坡比1:1.2。") == 12

    def test_fenced_block_stripped(self):
        fenced = ("流程如下：\n\n```mermaid\nflowchart TD\n"
                  "  A[开挖] --> B[支护]\n```\n")
        assert text_word_count(fenced) == 7
        assert readable_word_count(fenced) == 4

    def test_image_and_url_not_counted(self):
        assert readable_word_count("现场见下图 ![示意图](/uploads/a.png) 所示。") == 7
        assert readable_word_count(
            "依据 https://example.com/very/long/path/here 执行。") == 4

    def test_link_anchor_kept(self):
        # 链接只剔目标地址，锚文字是用户读到的内容 ⇒ 保留
        assert readable_word_count("详见《规范》[全文链接](https://a.b/c)") == 8

    def test_latin_grouped_by_word(self):
        # GB50007 / JGJ120-2012 各算 1 词（带连接符不拆），数字 3 单独算词
        assert readable_word_count("GB50007 与 JGJ120-2012 并行适用，共 3 条。") == 10

    def test_table_and_list_markers_dropped(self):
        assert readable_word_count(
            "| 工序 | 天数 |\n|---|---|\n| 开挖 | 5 |\n| 支护 | 3 |\n") == 10
        assert readable_word_count("### 3.1 施工部署\n\n- 第一项\n- 第二项\n") == 11

    def test_html_and_inline_code_dropped(self):
        assert readable_word_count("第一行<br>第二行 <!-- 内部备注 --> 结束。") == 8
        assert readable_word_count("采用 `C30 混凝土` 浇筑，强度等级 C30。") == 9

    def test_lf_crlf_same_value(self):
        lf = "流程如下：\n\n```mermaid\nflowchart TD\n  A[开挖] --> B[支护]\n```\n"
        assert readable_word_count(lf.replace("\n", "\r\n")) == readable_word_count(lf)

    def test_empty_and_none_are_zero(self):
        assert readable_word_count("") == 0
        assert readable_word_count(None) == 0
        assert has_cjk("") is False
        assert has_cjk(None) is False

    def test_has_cjk_only_cjk_ideographs(self):
        assert has_cjk("xxxxx") is False
        assert has_cjk("1:1.2") is False
        assert has_cjk("中文") is True
        assert has_cjk("あ") is True  # 假名同档（逐字计）

    def test_readable_never_exceeds_text_for_prose(self):
        # 包含关系：可读口径是字符口径的子集计数，散文场景不可能更大
        for body in ("基坑支护采用放坡，坡比1:1.2。",
                     "| 工序 | 天数 |\n|---|---|\n| 开挖 | 5 |\n",
                     "现场见下图 ![示意图](/uploads/a.png) 所示。"):
            assert readable_word_count(body) <= text_word_count(body), body


# ===========================================================================
# B 单一事实源 / 无死代码
# ===========================================================================
class TestSingleSourceAndNoDeadCode:
    def test_normalize_reuses_fence_source_of_truth(self):
        tree = ast.parse(_SRC["content_utils"])
        fn = next(n for n in tree.body
                  if isinstance(n, ast.FunctionDef) and n.name == "_normalize_for_readable")
        called = {c.func.id for c in ast.walk(fn)
                  if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
        assert "strip_fenced_code_blocks" in called, "围栏必须复用唯一事实源"

    def test_diagnostics_define_no_fence_pattern(self):
        # 「剔围栏」只有一个事实源；诊断常量不得自带围栏模式（否则是第二口径的雏形）
        tree = ast.parse(_SRC["content_utils"])
        for node in tree.body:
            if (isinstance(node, ast.Assign)
                    and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id.startswith("_RW_")):
                for const in ast.walk(node.value):
                    if isinstance(const, ast.Constant) and isinstance(const.value, str):
                        assert "```" not in const.value
                        assert "`{3," not in const.value and "~{3," not in const.value

    def test_word_like_count_removed_no_dead_code(self):
        assert "word_like_count" not in _SRC["content_utils"]
        assert "_RW_CJK_PUNCT_RE" not in _SRC["content_utils"]
        names = [n.name for n in ast.parse(_SRC["content_utils"]).body
                 if isinstance(n, ast.FunctionDef)]
        assert "readable_word_count" in names and "has_cjk" in names
        assert len(names) == len(set(names)), f"顶层重名：{names}"


# ===========================================================================
# C 既有结论逐字不变
# ===========================================================================
class TestExistingConclusionsUnchanged:
    def test_word_audit_summary_keys_locked(self):
        rep = word_audit_summary([
            {"section_id": "1", "title": "A", "level": 3, "word_budget": 1200,
             "word_count": 1200}])
        assert set(rep) == {"total", "in_band", "under_band", "over", "under",
                            "below_leaf_floor", "mean_ratio", "median_ratio",
                            "max_ratio", "items"}
        assert set(rep["items"][0]) == {"section_id", "title", "level",
                                        "word_budget", "word_count", "deviation",
                                        "ratio", "within_band", "word_status"}
        # 绝不含 readable_* —— rows 契约不带 content，汇总层拿不到正文
        assert not any(k.startswith("readable") for k in rep)
        assert not any(k.startswith("readable") for k in rep["items"][0])

    def _leaf(self, sec_id, title, wc, wb, content):
        return {"id": sec_id, "parent_id": "p", "title": title, "content": content,
                "word_count": wc, "level": 3, "word_budget": wb, "status": "done",
                "sort_order": int(sec_id)}

    def _ctx(self, secs):
        return PreflightContext(scheme_id="s", sections=secs)

    def _dlv16(self, secs):
        found = [f for f in check_deliverability(self._ctx(secs))
                 if f["rule_id"] == "DLV-16"]
        assert len(found) == 1
        return "\n".join(found[0]["evidence"])

    def test_ascii_fixture_evidence_byte_identical(self):
        # 既有 85 例依赖的形态：纯 ASCII fixture 不得出现任何对照读数
        ev = self._dlv16([self._leaf("1", "超纲A", 2100, 1200, "x" * 2100)])
        assert "超纲A" in ev and "超目标 175%" in ev
        assert "按可读字数" not in ev

    def test_cjk_fixture_only_appends_reading(self):
        body = "基坑支护采用放坡，坡比1:1.2。" * 200   # 字符口径 3400 / 可读 2400
        assert text_word_count(body) == 3400 and readable_word_count(body) == 2400
        ev = self._dlv16([self._leaf("1", "中文章", 3400, 1200, body)])
        assert "实际 3400/目标 1200 字" in ev          # 判定读数仍是字符口径
        assert "超目标 283%" in ev                     # 结论一字未动
        assert "按可读字数约 2400 字" in ev

    def test_reading_equals_count_then_omitted(self):
        # 两口径恰好相等时不追加（避免"按可读字数约 N 字"变成噪音）
        body = "基坑支护"          # 4 字且无标点 ⇒ text == readable == 4
        assert text_word_count(body) == readable_word_count(body) == 4
        ev = self._dlv16([self._leaf("1", "短章", 4, 1200, body)])
        assert "低于 300 字下限" in ev and "按可读字数" not in ev


# ===========================================================================
# D 接线（静态锁）
# ===========================================================================
class TestWiring:
    def test_preflight_imports_both_diagnostics(self):
        tree = ast.parse(_SRC["preflight"])
        imported = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom) and n.module == "app.services.content_utils":
                imported |= {a.name for a in n.names}
        assert {"has_cjk", "readable_word_count"} <= imported

    def test_dlv16_note_is_advisory_text_only(self):
        assert "_rw_note" in _SRC["preflight"]
        assert 'f"，按可读字数约 {_rw_txt} 字"' in _SRC["preflight"]
        # 追加点只在 evidence 串尾部，绝不进入 wc / _wc_dev / _wc_note
        assert "_wc_dev = word_deviation_of(wc, _s_wb" in _SRC["preflight"]
        # 追加点只有 evidence 串：_rw_note 定义 1 次 + 拼接 1 次，_rw_txt 4 次
        assert _SRC["preflight"].count("_rw_note") == 2
        assert _SRC["preflight"].count("_rw_txt") == 4

    def test_auto_shrink_default_untouched(self):
        assert ("_shrink_default = word_budget_tier is not None "
                "or word_budget_override is not None") in _SRC["sse"]
        # R66 旧论据已订正：注释不得再单独宣称"系统性超写（实测均值 136.9%）"
        assert "系统性超写（实测均值 136.9%）" not in _SRC["sse"]
        assert "含标点字符口径" in _SRC["sse"]
        assert "136.9% →" in _SRC["sse"] and "106.5%" in _SRC["sse"]


# ===========================================================================
# E 反向锁：诊断读数绝不进判定与落库
# ===========================================================================
class TestDiagnosticsNeverJudge:
    def test_consumer_set_is_exactly_the_one_advisory_site(self):
        assert _consumer_sites() == ALLOWED_CONSUMERS

    def test_no_judgment_function_references_diagnostics(self):
        forbidden = {"word_status_for", "word_deviation_of", "word_audit_summary",
                     "_should_auto_shrink", "should_continue_round", "_continue_if_needed"}
        for _file, fn in _consumer_sites():
            assert fn not in forbidden, f"诊断读数进入判定函数：{fn}"

    def test_word_count_is_always_written_with_char_caliber(self):
        # 落库侧（sections / sse）写 word_count 的语句只允许 text_word_count
        for key in ("sections", "sse"):
            src = _SRC[key]
            assert "text_word_count(" in src
            for name in DIAGNOSTICS:
                assert f'"{name}"' not in src
                assert f"{name}(" not in src, f"{key} 里出现了诊断读数调用"

    def test_assignment_to_word_count_never_uses_diagnostics(self):
        for p in sorted(_APP.rglob("*.py")):
            tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
            for node in ast.walk(tree):
                targets = []
                if isinstance(node, ast.Assign):
                    targets = [t for t in node.targets if isinstance(t, ast.Name)]
                elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                    targets = [node.target]
                for t in targets:
                    if t.id == "word_count" and node.value is not None:
                        ids = {c.func.id for c in ast.walk(node.value)
                               if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
                        assert not (ids & DIAGNOSTICS), f"{p}:{t.lineno} 用诊断读数落库"


# ===========================================================================
# F 字节体检
# ===========================================================================
class TestByteHealth:
    def test_touched_files_are_clean(self):
        for rel in ("services/content_utils.py", "services/preflight_engine.py",
                    "routers/sse_handlers.py"):
            raw = (_APP / rel).read_bytes()
            text = raw.decode("utf-8")
            assert raw[:3] != b"\xef\xbb\xbf", f"{rel} 不得有 BOM"
            assert text.count("\r") == text.count("\r\n"), f"{rel} 行尾非纯 CRLF"
            assert "\ufffd" not in text, f"{rel} 存在替换字符"
            ast.parse(text, filename=rel)
            compile(text, rel, "exec")
