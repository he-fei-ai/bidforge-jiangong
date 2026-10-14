"""R76（2026-10-13）· 正文生成模块 · 围栏判据单一事实源护栏。

三组锁（全部由本轮深度审计取证到的真实分叉形态驱动，语料取自
``artifacts/_probe_r76_matrix.py`` 的实测矩阵，不是凭空构造）：

  A 静态接线：「这一行是不是闭合行 / 这块围栏到哪结束」这条判据在正文生成链路上
    只允许一处实现（``services/fence_common``）。历史上它有 **七份** 实现：
    content_utils（严格 + 宽松两份）、content_blocks（本地正则 + 自己的闭合判定）、
    _chart_pipeline（两份零引用悬空副本）、content_rewrite、content_checkpoint、
    content_polish、preflight_engine（在调用方重做分类）。副本回流 = 口径再次分叉。
  B 四口径 parity：同一份脏正文在「预检严格口径 / 渲染容错口径 / 字数剔除 /
    图表读取」四个出口上必须得出一致结论，或按 ``classify_fence_defects`` 明确分类。
  C 开关回退：``content_fence_normalize_close`` 置 False 时逐字回退旧行为。
"""
from __future__ import annotations

import ast
import pathlib
import re

import pytest

from app.config import settings
from app.services import content_blocks as cb
from app.services import content_utils as cu
from app.services import fence_common as fc

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
APP_ROOT = BACKEND_ROOT / "app"
FRONTEND_ROOT = BACKEND_ROOT.parent / "frontend"

BT = chr(96)          # 反引号：集中构造，避免源码里散落的 ``` 与注释/文档互相污染
FENCE = BT * 3
FENCE4 = BT * 4

# ── 实测语料（与探针 _probe_r76_matrix.py 逐字一致）────────────────────────
CORPUS_4_OPEN_3_CLOSE = "前置段落。\n" + FENCE4 + "mermaid\ngraph TD; A-->B\n" + FENCE + "\n后续正文。"
CORPUS_LABELED_CLOSE = (FENCE + "mermaid\ngraph TD\n" + FENCE + "python\n"
                        "console.log(1)\n" + FENCE + "\n后续正文。")
CORPUS_TILDE = "~~~mermaid\ngraph TD\n~~~\n尾段。"
CORPUS_WELL_FORMED = FENCE + "mermaid\ngraph TD; A-->B\n" + FENCE + "\n尾段。"
CORPUS_UNCLOSED_WITH_PROSE = FENCE + "mermaid\ngraph TD\n后续正文，结束。"


def _py(rel: str) -> str:
    return (APP_ROOT / rel).read_text(encoding="utf-8")


def _code_lines(rel: str) -> list[str]:
    """非注释行（R75b 教训：源码里的处置注释会逐字引用被禁形态，全文扫描自咬）。"""
    return [ln for ln in _py(rel).splitlines() if not ln.strip().startswith("#")]


def _top_level_tree(rel: str) -> ast.Module:
    return ast.parse(_py(rel))


# =========================================================================
# A 组 —— 判据单一事实源（静态接线锁）
# =========================================================================
class TestFencePredicateSingleSource:
    """围栏行/闭合行的**正则字面量**不得在正文生成链路上再出现第二份。"""

    #: 棘轮白名单：fence_common 是唯一事实源。
    #: ✅ R76b · G1：`services/bid_analysis_service.py` 已从白名单**摘除** ——
    #:    它那份 `_FENCE_LINE_RE` + 自写状态机不判信息串（块内 ```json 被当收尾，
    #:    保护区被拆两段、断点可落在围栏中间），现委托 `fence_common.fence_char_ranges`。
    #:    白名单自此**只剩唯一事实源本身**，任何新增副本即红。
    ALLOWED_FENCE_LINE_REGEX_FILES = {
        "services/fence_common.py",
    }

    def test_fence_line_regex_exists_only_in_whitelist(self):
        found: set[str] = set()
        for path in APP_ROOT.rglob("*.py"):
            rel = path.relative_to(APP_ROOT).as_posix()
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):  # pragma: no cover
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                if not (isinstance(fn, ast.Attribute) and fn.attr == "compile"):
                    continue
                if not (node.args and isinstance(node.args[0], ast.Constant)
                        and isinstance(node.args[0].value, str)):
                    continue
                pattern = node.args[0].value
                # 「行锚定围栏谓词」指纹：≤3 前导空白量词 + 反引号/波浪号重复类
                if "{0,3}" in pattern and ("`{3,}" in pattern or "```" in pattern
                                             or "~{3,}" in pattern or "~~~" in pattern):
                    found.add(rel)
        assert found <= self.ALLOWED_FENCE_LINE_REGEX_FILES, (
            f"围栏行判据正则副本回流：{sorted(found)} —— 请改为引用 fence_common"
        )
        assert "services/fence_common.py" in found, "唯一事实源自身的正则不得被删（护栏不能空转）"

    @pytest.mark.parametrize("rel", [
        "services/content_utils.py",
        "services/content_blocks.py",
        "services/content_rewrite.py",
        "services/content_checkpoint.py",
        "services/content_polish.py",
        "routers/_chart_pipeline.py",
        # ✅ R76b · G1/G3：投标分块与修复校验器也纳入同一约束（正则副本已删）。
        #    ⚠️ `content_standard` 刻意**不**列此处 —— 它的 `_FENCE_RE` 是合法的
        #    「保守剥离」用途（把 ``` 之间的内容整体剔掉、不让代码数字参与事实校验），
        #    由 R65 `test_content_standard_fence_regex_single_definition` 与
        #    本轮 G3 组分别锁住「只剩一处定义 + 行口径必须委托 fence_common」。
        "services/bid_analysis_service.py",
        "services/repair_validator.py",
    ])
    def test_consumers_define_no_local_fence_regex(self, rel):
        """消费模块顶层不得再有围栏正则/句读正则赋值（含零引用悬空副本）。"""
        banned = {"_FENCE_LINE_RE", "_FENCE_SPLIT_RE", "_CJK_TAIL_RE", "_FENCE_MARK",
                  "_PS_FENCE_LINE_RE", "_FENCE_RE"}
        assigned = {
            tgt.id
            for node in _top_level_tree(rel).body
            if isinstance(node, ast.Assign)
            for tgt in node.targets if isinstance(tgt, ast.Name)
        }
        assert not (assigned & banned), f"{rel} 重新定义了围栏正则：{sorted(assigned & banned)}"

    @pytest.mark.parametrize("rel", [
        "services/content_utils.py",
        "services/content_blocks.py",
        "services/content_rewrite.py",
        "services/content_checkpoint.py",
        "services/content_polish.py",
        # ✅ R76b · G1/G3：反向锁同样覆盖新收敛的三个消费点
        #    （投标分块 / 修复校验器 / 段落结构校验的块行口径）
        "services/bid_analysis_service.py",
        "services/repair_validator.py",
        "services/content_standard.py",
    ])
    def test_consumers_import_fence_common(self, rel):
        """反向锁：委托必须真的存在（删掉导入 = 口径回到本地实现）。"""
        src = _py(rel)
        assert "fence_common" in src, f"{rel} 不再引用 fence_common（判据分叉回流）"

    def test_read_fenced_block_closes_only_via_shared_predicate(self):
        """图表三侧共用扫描器的四处闭合判定必须全部走 ``is_fence_close``。"""
        fn = next(n for n in ast.walk(_top_level_tree("services/content_blocks.py"))
                  if isinstance(n, ast.FunctionDef) and n.name == "read_fenced_block")
        names = [
            (n.attr if isinstance(n, ast.Attribute) else n.id)
            for n in ast.walk(fn) if isinstance(n, (ast.Name, ast.Attribute))
        ]
        assert names.count("_fence_is_close") >= 4, (
            "read_fenced_block 的闭合判定数量减少 —— 说明有落点退回本地判据"
        )
        assert "FENCE_LINE_RE" not in names, "read_fenced_block 不得绕过谓词直接拿正则切分"

    def test_chart_pipeline_has_no_dangling_fence_regex_copies(self):
        """_chart_pipeline 那两份**零引用**悬空副本删了就不得回流（R76 死代码清理）。"""
        src = _py("routers/_chart_pipeline.py")
        assert "_CJK_TAIL_RE = " not in src
        assert "_FENCE_LINE_RE = " not in src

    def test_preflight_uses_classify_not_own_dispatch(self):
        """DLV-07 的分类分派必须在唯一事实源，不在调用方重做（分类也是判据）。"""
        src = _py("services/preflight_engine.py")
        assert "classify_fence_defects" in src
        code_only = "\n".join(_code_lines("services/preflight_engine.py"))
        assert "find_unclosed_fences(" not in code_only, (
            "预检又去自持严格口径扫描 —— 会重新出现「报警但修不了」的不闭环"
        )


# =========================================================================
# A2 组 —— 前端侧接线锁（跨语言 parity，glob 静态读，同 R68/R75 惯例）
# =========================================================================
class TestFrontendFenceWiring:
    MD_RENDERER = FRONTEND_ROOT / "src" / "components" / "MarkdownRenderer.tsx"
    UTIL = FRONTEND_ROOT / "src" / "utils" / "fencePreprocess.ts"

    def _code(self, p: pathlib.Path) -> str:
        return "\n".join(
            ln for ln in p.read_text(encoding="utf-8").splitlines()
            if not ln.strip().startswith(("//", "* ", "/*"))
        )

    def test_renderer_imports_shared_utils(self):
        src = self._code(self.MD_RENDERER)
        assert "utils/fencePreprocess" in src
        for name in ("normalizeFenceCloses", "replaceChartFences", "scanFenceBlocks"):
            assert name in src, f"MarkdownRenderer 未接线 {name}（预览口径分叉回流）"

    def test_old_inline_fence_predicates_are_gone(self):
        src = self._code(self.MD_RENDERER)
        assert 'startsWith("```mermaid")' not in src
        assert not re.search(r"/```\(?mermaid", src), "旧的单条全局围栏正则回流"

    def test_utils_predicate_requires_empty_info_string(self):
        src = self._code(self.UTIL)
        assert re.search(r"pf\.info\s*===\s*\"\"", src), (
            "前端闭合判据必须排除带信息串的行（BUG-F2 前端侧）"
        )


# =========================================================================
# B 组 —— 四口径 parity（行为锁）
# =========================================================================
class TestFourWayParity:
    def test_4open_3close_is_length_mismatch_and_one_step_fixable(self):
        src = CORPUS_4_OPEN_3_CLOSE
        assert cu.find_unclosed_fences(src), "严格口径应报缺陷"
        defect = fc.classify_fence_defects(src)
        assert defect["kind"] == "length_mismatch"
        assert len(defect["length_mismatch"]) == 1 and defect["unclosed"] == []
        fixed, log = cu.auto_fix_unclosed_fences(src)
        assert [x["action"] for x in log] == ["normalize_close_length"], (
            "长度错配不该走文末追加（追加会把「正文被吞」固化成检查通过）"
        )
        assert len(fixed.split("\n")) == len(src.split("\n")), "归一不得增删行"
        assert cu.find_unclosed_fences(fixed) == []
        assert fc.find_unclosed(fixed, tolerant_length=True) == []
        # 归一后严格口径的字数剔除恢复正常：被吞的「后续正文。」回到正文字数
        assert cu.text_word_count(fixed) > cu.text_word_count(src)
        assert cu.fence_spans(fixed)

    def test_labeled_line_is_content_not_close(self):
        """BUG-F2 本体：块内带信息串的围栏行不是闭合行，其后正文不得被吞。"""
        src = CORPUS_LABELED_CLOSE
        assert fc.is_fence_close(FENCE + "python", "`", 3) is False
        assert fc.is_fence_close(FENCE, "`", 3) is True
        lines = src.split("\n")
        code, state, nxt = cb.read_fenced_block(lines, 1, open_char="`", open_len=3)
        assert state == "closed"
        assert code == ["graph TD", FENCE + "python", "console.log(1)"]
        assert lines[nxt] == "后续正文。", "闭合点必须落在真正的裸标记行"
        assert cu.find_unclosed_fences(src) == []

    def test_char_kind_never_mixes(self):
        assert fc.is_fence_close(FENCE, "~", 3) is False
        assert fc.is_fence_close("~~~", "`", 3) is False
        code, state, _ = cb.read_fenced_block(CORPUS_TILDE.split("\n"), 1,
                                              open_char="~", open_len=3)
        assert state == "closed" and code == ["graph TD"]

    def test_true_unclosed_appends_and_reports_swallowed_prose(self):
        src = CORPUS_UNCLOSED_WITH_PROSE
        defect = fc.classify_fence_defects(src)
        assert defect["kind"] == "unclosed"
        fixed, log = cu.auto_fix_unclosed_fences(src)
        assert log[0]["action"] == "append_close"
        assert log[0]["swallowed_prose"] >= 1, (
            "补齐动作必须显式上报「疑似正文困在块内」，不得静默当成已修复"
        )
        assert fc.find_unclosed(fixed, tolerant_length=True) == []
        # 诚实性：追加闭合救不回被吞正文，字数不应回升
        assert cu.text_word_count(fixed) == cu.text_word_count(src)

    def test_well_formed_corpus_untouched_and_idempotent(self):
        src = CORPUS_WELL_FORMED
        assert fc.classify_fence_defects(src)["kind"] == ""
        fixed, log = cu.auto_fix_unclosed_fences(src)
        assert log == [] and fixed == src
        again, log2 = cu.auto_fix_unclosed_fences(fixed)
        assert again == fixed and log2 == []

    @pytest.mark.parametrize("src", [
        CORPUS_4_OPEN_3_CLOSE, CORPUS_LABELED_CLOSE, CORPUS_TILDE,
        CORPUS_WELL_FORMED, CORPUS_UNCLOSED_WITH_PROSE,
    ])
    def test_two_predicates_converge_after_normalize(self, src):
        """核心不变式：归一后严格口径与容错口径对同一份正文结论恒等。"""
        normalized, _log = fc.normalize_fence_closes(src)
        fixed, _ = cu.auto_fix_unclosed_fences(normalized)
        strict = fc.find_unclosed(fixed, tolerant_length=False)
        lenient = fc.find_unclosed(fixed, tolerant_length=True)
        assert strict == lenient == [], (lenient, strict)

    @pytest.mark.parametrize("src", [
        CORPUS_4_OPEN_3_CLOSE, CORPUS_LABELED_CLOSE, CORPUS_TILDE,
        CORPUS_UNCLOSED_WITH_PROSE,
    ])
    def test_char_ranges_agree_with_line_blocks(self, src):
        """保护区间出口与行级块出口必须是同一次遍历的结果（不重叠、有序、闭合一致）。"""
        ranges = fc.fence_char_ranges(src)
        blocks = fc.iter_all_blocks(src)
        assert len(ranges) == len(blocks)
        lines = src.split("\n")
        starts = [0]
        for ln in lines[:-1]:
            starts.append(starts[-1] + len(ln) + 1)
        for idx, ((s, e), b) in enumerate(zip(ranges, blocks)):
            assert s == starts[b["open_line"]]
            if idx:
                assert ranges[idx - 1][1] <= s, "保护区间不得重叠或乱序"
            if b["close_line"] < 0:
                assert e == len(src)
            else:
                assert e == starts[b["close_line"]] + len(lines[b["close_line"]])


# =========================================================================
# B2 组 —— 保护区间消费点（重写 / 清洗 / 检查点）
# =========================================================================
class TestProtectionRangeConsumers:
    def test_rewrite_split_keeps_prose_outside_after_labeled_line(self):
        from app.services.content_rewrite import _split_fences

        segs = _split_fences(CORPUS_LABELED_CLOSE)
        assert [s for ok, s in segs if not ok][-1] == "后续正文。", (
            "带标签行被当闭合时，正文会滑进围栏侧被改写（R76 同族 BUG）"
        )
        fence_side = [s for ok, s in segs if ok]
        assert fence_side == [FENCE + "mermaid\ngraph TD\n" + FENCE + "python\n"
                              "console.log(1)\n" + FENCE]

    def test_rewrite_roundtrip_preserves_text_when_no_defect(self):
        from app.services.content_rewrite import _split_fences

        for src in (CORPUS_WELL_FORMED, CORPUS_TILDE, CORPUS_UNCLOSED_WITH_PROSE,
                    "纯正文，无围栏。", ""):
            segs = _split_fences(src)
            assert "\n".join(seg for _ok, seg in segs) == src, src

    def test_polish_protects_tail_after_unclosed_fence(self):
        from app.services import content_polish as cp

        ranges = cp._protected_ranges(CORPUS_UNCLOSED_WITH_PROSE)
        assert ranges and ranges[0][1] == len(CORPUS_UNCLOSED_WITH_PROSE), (
            "旧非贪婪正则对未闭合围栏完全不匹配 → 其后正文失去清洗保护"
        )
        stripped = cp._strip_fences(CORPUS_UNCLOSED_WITH_PROSE)
        assert "graph TD" not in stripped

    def test_checkpoint_fence_ranges_delegate(self):
        from app.services import content_checkpoint as ckpt

        assert ckpt._fence_ranges(CORPUS_LABELED_CLOSE) == fc.fence_char_ranges(
            CORPUS_LABELED_CLOSE
        )
        assert ckpt._fence_ranges("无围栏正文。") == []


# =========================================================================
# C 组 —— 开关回退（默认开、置 False 逐字回退旧行为）
# =========================================================================
class TestNormalizeSwitch:
    def test_config_default_is_on(self):
        assert settings.content_fence_normalize_close is True

    def test_switch_off_falls_back_to_append_only(self, monkeypatch):
        monkeypatch.setattr(fc, "normalize_close_enabled", lambda: False)
        src = CORPUS_4_OPEN_3_CLOSE
        fixed, log = cu.auto_fix_unclosed_fences(src)
        assert log == [] and fixed == src, "开关关闭时长度错配不得被改写（旧行为）"

    def test_switch_on_normalizes(self, monkeypatch):
        monkeypatch.setattr(fc, "normalize_close_enabled", lambda: True)
        fixed, log = cu.auto_fix_unclosed_fences(CORPUS_4_OPEN_3_CLOSE)
        assert [x["action"] for x in log] == ["normalize_close_length"]
        assert FENCE4 == fixed.split("\n")[3].strip(), "闭合行应被补齐到开围栏长度"

    def test_switch_off_still_appends_true_unclosed(self, monkeypatch):
        """开关只管「长度归一」，不管文末追加（两件独立的事，不得捆绑回退）。"""
        monkeypatch.setattr(fc, "normalize_close_enabled", lambda: False)
        _fixed, log = cu.auto_fix_unclosed_fences(CORPUS_UNCLOSED_WITH_PROSE)
        assert [x["action"] for x in log] == ["append_close"]

    def test_authority_reads_settings_via_module_attribute(self):
        """monkeypatch 表面锁：开关必须经 fence_common 模块属性生效（R75b 同族坑）。"""
        src = "\n".join(_code_lines("services/content_utils.py"))
        assert "_fence_common.normalize_close_enabled()" in src
        assert "from app.services.fence_common import" in src
        assert "normalize_close_enabled" not in src.split(
            "from app.services.fence_common import"
        )[1].split("def ")[0], "content_utils 不得把开关按名字导入（patch 表面会迁移）"
