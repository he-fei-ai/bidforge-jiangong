"""R71（2026-10-12）字数收尾透明警告 + F2 前端并列口径收口护栏。

背景：引入外部《易标文字生成数量控制规则与标准》R12「失败不硬裁 → 输出人工核对
透明警告」，并把易标报告 F2 的 5 处前端并列口径统一到「后端值优先」。用户明确决定
**不切换 `text_word_count` 权威判定口径**（那会推翻 R70 刚锁定的唯一口径 + 触发落库
迁移）。所以 R71 全是**加法式**：新增一个仅建议的收尾读数 `word_closeout_notice` +
导出/图表修复链回带权威字数 + 前端不再用 `.length` 伪造字数。本护栏的核心职责不是
证明读数正确，而是**证明它没越界**（不参与任何判定 / 落库，判定口径仍唯一走
text_word_count）。

覆盖：
A `word_closeout_notice` 纯函数：under/over/normal 三态 + 脏值不抛 + 预算兜底 +
  只依赖 text 口径入参（内部绝不引用 readable_word_count / has_cjk）。
B 接线：sse_handlers import 该函数 + 写 `report["word"]` + 重建 report_json。
C charts.py fix_mermaid 回带 word_count（源出 text_word_count，additive 键）。
D **反向锁**：word_closeout_notice 消费点只允许 sse_handlers 的报告接线；
  任何 `word_count=` 落库赋值与判定函数都不得引用它。
E 字节体检：三个触碰后端文件 CR==LF / 无 BOM / 无 U+FFFD / AST+compile OK。
"""
from __future__ import annotations

import ast
import pathlib

from app.services.content_utils import (
    DEFAULT_WORD_BUDGET,
    WORD_OVER_RATIO,
    WORD_UNDER_RATIO,
    word_closeout_notice,
)

_BACKEND = pathlib.Path(__file__).resolve().parents[1]
_APP = _BACKEND / "app"
_SRC = {
    name: (_APP / rel).read_text(encoding="utf-8")
    for name, rel in {
        "content_utils": "services/content_utils.py",
        "sse": "routers/sse_handlers.py",
        "charts": "routers/charts.py",
        "sections": "routers/sections.py",
    }.items()
}

_ADVISORY = {"word_closeout_notice"}
#: 唯一许可消费文件（仅报告接线，绝不落库 / 判定）
_ADVISORY_ALLOWED_CONSUMERS = {"routers/sse_handlers.py"}


def _advisory_consumer_files() -> set[str]:
    """扫全仓 app/**.py，返回调用 word_closeout_notice 的文件（相对 app 路径）。"""
    files: set[str] = set()
    for p in sorted(_APP.rglob("*.py")):
        tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
        for call in ast.walk(tree):
            if (isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Name)
                    and call.func.id in _ADVISORY):
                files.add(p.relative_to(_APP).as_posix())
    return files


# ===========================================================================
# A 纯函数行为
# ===========================================================================
class TestWordCloseoutNotice:
    def test_over_is_advisory_human_check(self):
        budget = 1200
        wc = int(budget * WORD_OVER_RATIO) + 500   # 明确超 1.3×
        rep = word_closeout_notice(wc, budget)
        assert rep["advisory"] is True
        assert rep["kind"] == "over"
        assert rep["word_status"] == "over"
        assert "人工核对" in rep["message"]
        assert "不可损坏正文" in rep["message"] or "未强行裁剪" in rep["message"]

    def test_under_is_advisory_human_check(self):
        budget = 1200
        wc = int(budget * WORD_UNDER_RATIO) - 400   # 明确低于 0.8×
        rep = word_closeout_notice(wc, budget)
        assert rep["advisory"] is True
        assert rep["kind"] == "under"
        assert "人工核对" in rep["message"]
        assert "补充" in rep["message"]

    def test_normal_is_silent(self):
        budget = 1200
        rep = word_closeout_notice(budget, budget)  # 恰好等于目标 → normal
        assert rep["advisory"] is False
        assert rep["kind"] is None
        assert rep["word_status"] == "normal"
        assert rep["message"] == ""

    def test_result_shape_locked(self):
        rep = word_closeout_notice(1000, 1200)
        assert set(rep) == {"advisory", "kind", "message", "word_status",
                            "ratio", "within_band", "severity"}
        assert rep["severity"] == "advisory"

    def test_dirty_inputs_never_raise(self):
        # 非数值 word_count / None / 预算 0 与 None 都不抛，按唯一口径兜底
        assert word_closeout_notice(None, 1200)["advisory"] is True   # 0 字 → under
        assert word_closeout_notice("abc", 1200)["advisory"] is True
        rep = word_closeout_notice(1200, 0)   # 预算 0 → 兜底 DEFAULT_WORD_BUDGET
        assert rep["word_status"] == "normal"
        assert word_closeout_notice(1200, None)["word_status"] == "normal"
        assert DEFAULT_WORD_BUDGET > 0

    def test_advisory_only_never_reads_readable_diagnostics(self):
        # 收尾读数只吃 text 口径入参：其函数体绝不引用 readable_word_count / has_cjk，
        # 否则就成了 R70 反向锁之外的第二把尺子。
        tree = ast.parse(_SRC["content_utils"])
        fn = next(n for n in tree.body
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "word_closeout_notice")
        called = {c.func.id for c in ast.walk(fn)
                  if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
        assert not (called & {"readable_word_count", "has_cjk"})
        # 复用唯一判定：内部走 word_status_for（同一 0.8×/1.3× 口径），不自造阈值
        assert "word_status_for" in called


# ===========================================================================
# B 接线（静态锁）
# ===========================================================================
class TestSseWiring:
    def test_sse_imports_closeout_notice(self):
        tree = ast.parse(_SRC["sse"])
        imported = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom) and n.module == "app.services.content_utils":
                imported |= {a.name for a in n.names}
        assert "word_closeout_notice" in imported

    def test_sse_writes_report_word_key(self):
        assert 'report["word"] = _word_rep' in _SRC["sse"]
        assert "_word_rep = word_closeout_notice(wc, word_budget)" in _SRC["sse"]
        # 加法式：写完后重建 report_json（与 structure 块同构），不改判定/落库
        assert _SRC["sse"].count('report["word"] = _word_rep') == 1


# ===========================================================================
# C charts.py 回带 word_count（additive，源出 text_word_count）
# ===========================================================================
class TestChartsFixReturnsWordCount:
    def test_return_dict_has_word_count_key(self):
        assert '"word_count": section_word_count' in _SRC["charts"]

    def test_section_word_count_sourced_from_char_caliber(self):
        # 权威字数只能来自 text_word_count(patched)，且仅在正文重写时赋值
        assert "section_word_count = wc" in _SRC["charts"]
        assert "wc = text_word_count(patched)" in _SRC["charts"]
        # 初始化与失败回滚都置 None（未重写正文绝不谎报字数）
        assert _SRC["charts"].count("section_word_count = None") >= 2

    def test_no_local_char_fabrication_in_return(self):
        # 绝不用 len()/new_content.length 伪造：唯一来源是 text_word_count
        assert "word_count\": len(" not in _SRC["charts"]


# ===========================================================================
# D 反向锁：收尾读数绝不进判定与落库
# ===========================================================================
class TestAdvisoryNeverJudges:
    def test_only_consumer_is_sse_report(self):
        assert _advisory_consumer_files() == _ADVISORY_ALLOWED_CONSUMERS

    def test_sections_save_path_untouched_by_advisory(self):
        # 单章手工保存落库 word_count 只走 text_word_count，不引用收尾读数
        assert "word_closeout_notice" not in _SRC["sections"]
        assert "text_word_count(" in _SRC["sections"]

    def test_word_count_assignment_never_uses_advisory(self):
        # 全仓任何 `word_count = ...` 赋值的右值不得调用收尾/诊断读数
        forbidden = _ADVISORY | {"readable_word_count", "has_cjk"}
        for p in sorted(_APP.rglob("*.py")):
            tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
            for node in ast.walk(tree):
                targets: list[ast.Name] = []
                if isinstance(node, ast.Assign):
                    targets = [t for t in node.targets if isinstance(t, ast.Name)]
                elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                    targets = [node.target]
                for t in targets:
                    if t.id == "word_count" and node.value is not None:
                        ids = {c.func.id for c in ast.walk(node.value)
                               if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
                        assert not (ids & forbidden), f"{p}:{t.lineno} 用收尾/诊断读数落库"

    def test_closeout_notice_not_imported_by_sections_or_export(self):
        # 落库/导出侧不得引用收尾读数（只允许在 sse 报告接线）
        for rel in ("routers/sections.py", "routers/export.py"):
            src = (_APP / rel).read_text(encoding="utf-8")
            assert "word_closeout_notice" not in src, f"{rel} 引用了收尾读数"


# ===========================================================================
# E 字节体检
# ===========================================================================
class TestByteHealth:
    def test_touched_files_are_clean(self):
        for rel in ("services/content_utils.py", "routers/sse_handlers.py",
                    "routers/charts.py"):
            raw = (_APP / rel).read_bytes()
            text = raw.decode("utf-8")
            assert raw[:3] != b"\xef\xbb\xbf", f"{rel} 不得有 BOM"
            assert text.count("\r") == text.count("\r\n"), f"{rel} 行尾非纯 CRLF"
            assert "\ufffd" not in text, f"{rel} 存在替换字符"
            ast.parse(text, filename=rel)
            compile(text, rel, "exec")
