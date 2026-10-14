"""R76b（2026-10-14）· 正文生成模块遗留项收口护栏（G1 / G2 / G3 三组）。

R76 把围栏闭合判据收敛到 `services/fence_common`，并在报告 §九 留下三条遗留：
  G1 投标分析侧的第五份围栏正则副本（跨模块，R76 只做了棘轮钉住）
  G2 历史存量正文「不回填」缺一份有数据的盘点
  G3 若干「看着像剥离用途」的围栏实现 —— 逐一定性后发现其中两处其实是
     **闭合判据的又一套口径**（repair_validator 的奇偶计数、content_standard
     的行级翻转状态机），另四处确为剥离/提取用途。
本文件把三条结论全部做成可执行断言。语料与探针一一对应
（`artifacts/r76b/_probe_g1_bid.py` / `_probe_g3_repair_validator.py`），不是凭空构造。
"""
from __future__ import annotations

import ast
import pathlib
import re

import pytest

from app.services import bid_analysis_service as bas
from app.services import fence_common as fc
from app.services.repair_validator import structure_signature, validate_repair

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
APP_ROOT = BACKEND_ROOT / "app"
ARTIFACTS = BACKEND_ROOT / "artifacts" / "r76b"

BT = chr(96)
F3 = BT * 3
F4 = BT * 4


def _src(rel: str) -> str:
    return (APP_ROOT / rel).read_text(encoding="utf-8")


def _code_lines(rel: str) -> list[str]:
    """真正的**可执行**代码行：排除注释行与 docstring 行。

    R75b 教训（处置注释逐字引用被禁形态作反例，全文扫描自咬）在 docstring 上
    同样成立 —— 本轮 `structure_signature` 的 docstring 就写明了旧口径
    `body.count("```")`，全文扫描会把「修复记录」当成「缺陷回流」。
    """
    src = _src(rel)
    lines = src.splitlines()
    doc_lines: set[int] = set()
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, (ast.Module, ast.FunctionDef,
                                 ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if ast.get_docstring(node) is None or not getattr(node, "body", None):
            continue
        head = node.body[0]
        if not (isinstance(head, ast.Expr) and isinstance(head.value, ast.Constant)
                and isinstance(head.value.value, str)):
            continue
        doc_lines.update(range(head.lineno, head.end_lineno + 1))
    return [ln for idx, ln in enumerate(lines, start=1)
            if idx not in doc_lines and not ln.strip().startswith("#")]


def _tree(rel: str) -> ast.Module:
    return ast.parse(_src(rel))


def _func(rel: str, name: str) -> ast.FunctionDef:
    return next(n for n in ast.walk(_tree(rel))
                if isinstance(n, ast.FunctionDef) and n.name == name)


def _names_in(node: ast.AST) -> list[str]:
    return [(n.attr if isinstance(n, ast.Attribute) else n.id)
            for n in ast.walk(node) if isinstance(n, (ast.Name, ast.Attribute))]


# ── 与探针逐字一致的语料 ──────────────────────────────────────────────
C1_LABELED_INSIDE = "\n".join([
    "本章工期为 90 日历天，符合要求。",
    F3,
    "graph TD; A-->B;",
    F3 + "json",
    '{"note": "块内嵌套示例"}',
    F3,
    "权威值 90 日历天已写入。",
])
C2_TILDE_UNCLOSED = "\n".join([
    "本章工期为 90 日历天。",
    "~~~mermaid",
    "graph TD; A-->B;",
    "后续正文其实困在围栏里。",
])
C3_4OPEN_3CLOSE = "\n".join(["前段。", F4 + "mermaid", "graph TD; A-->B", F3, "尾段正文。"])


# =========================================================================
# G1 —— 投标分析侧副本收敛（跨出正文生成模块的第五份）
# =========================================================================
class TestBidAnalysisFenceDelegation:

    def test_bid_module_defines_no_fence_regex(self):
        """顶层不得重新出现 `_FENCE_LINE_RE`（棘轮白名单已在 R76 护栏里摘除该文件）。"""
        assigned = {
            tgt.id
            for node in _tree("services/bid_analysis_service.py").body
            if isinstance(node, ast.Assign)
            for tgt in node.targets if isinstance(tgt, ast.Name)
        }
        assert "_FENCE_LINE_RE" not in assigned, "投标分块重新定义了本地围栏正则"

    def test_collect_fence_ranges_is_pure_delegation(self):
        """`_collect_fence_ranges` 必须是**一行委托**，不得再自带状态机。

        锁形态而非只锁行为：旧实现是 26 行的 `opened_at / opened_marker` 扫描，
        一旦有人「顺手加个判断」就会重新分叉（F2/F5 同族的复发路径）。
        """
        fn = _func("services/bid_analysis_service.py", "_collect_fence_ranges")
        body = [n for n in fn.body if not (isinstance(n, ast.Expr)
                                           and isinstance(n.value, ast.Constant))]
        assert len(body) == 1 and isinstance(body[0], ast.Return), (
            "_collect_fence_ranges 必须只有一条 return（委托被掏空 = 口径回流）"
        )
        assert "fence_char_ranges" in _names_in(body[0]), (
            "未委托 fence_common.fence_char_ranges"
        )

    @pytest.mark.parametrize("corpus", [C1_LABELED_INSIDE, C2_TILDE_UNCLOSED, C3_4OPEN_3CLOSE])
    def test_bid_ranges_parity_with_single_source(self, corpus):
        """三语料上投标侧算出的保护区必须与唯一事实源**逐字相同**。"""
        assert bas._collect_fence_ranges(corpus) == fc.fence_char_ranges(corpus)

    def test_labeled_line_does_not_split_protection(self):
        """缺陷本体锁（探针 C1 实测 `[(1,4), (5,6)]` → 应为**一整块**）。

        块内 ```` ```json ```` 不是闭合行；旧副本会提前收尾，把 `{"k": 1}` 与其后的
        裸 ```` ```` 留在保护区**外**，分块断点因此可以合法落在围栏中间。
        """
        ranges = bas._collect_fence_ranges(C1_LABELED_INSIDE)
        assert len(ranges) == 1, f"保护区被带标签行拆成 {len(ranges)} 段（旧缺陷形态）"
        start, end = ranges[0]
        assert '{"note"' in C1_LABELED_INSIDE[start:end], "块内内容未纳入保护区"
        assert C1_LABELED_INSIDE[start:end].rstrip().endswith(F3), "保护区未覆盖到真正的闭合行"

    def test_split_never_leaves_unclosed_fence_in_a_chunk(self):
        """端到端：真实分块入口的每一段都不得自带未闭合围栏。"""
        prose = "这是一段足够长的中文说明文字，用于把总长度推到分段上限以上。" * 12
        big = "\n".join([
            prose, F3, "graph TD; A-->B;", F3 + "json",
            '{"项目": "某某隧道", "工期": 365}', F3,
            "紧跟围栏之后的正文同样属于代码块内容，不该被从中间切断。",
            prose,
        ])
        for even in (False, True):
            chunks = bas.split_for_analysis(big, chunk_size=600, overlap=0, even=even)
            bad = [i for i, c in enumerate(chunks)
                   if fc.classify_fence_defects(c)["kind"] != ""]
            assert not bad, f"even={even} 第 {bad} 段含未闭合围栏（保护区判定分叉）"


# =========================================================================
# G2 —— 历史存量只读盘点的「只读形态」锁（仿 R75b G8，防被改成回填工具）
# =========================================================================
class TestInventoryG2IsReadOnly:

    SCRIPT = ARTIFACTS / "inventory_g2.py"

    def test_inventory_script_exists(self):
        assert self.SCRIPT.is_file(), "G2 盘点脚本缺失（遗留项必须可复核，不能只写在报告里）"

    def test_inventory_opens_db_read_only_and_writes_nothing(self):
        src = self.SCRIPT.read_text(encoding="utf-8")
        assert "mode=ro" in src and "uri=True" in src, (
            "盘点脚本必须以 sqlite URI 只读模式打开连接"
        )
        tree = ast.parse(src)
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
        # 唯一的 connect 调用必须带 uri=True
        connects = [n for n in calls if isinstance(n.func, ast.Attribute)
                    and n.func.attr == "connect"]
        assert connects, "盘点脚本必须自行 connect（不得借道 app.db 的写连接）"
        for c in connects:
            kw = {k.arg for k in c.keywords}
            assert "uri" in kw, "connect 未使用 URI 形态（mode=ro 会被绕过）"
            # ⚠️ 必须在**连接参数里**出现 mode=ro，不能只全文扫描 —— 本脚本的
            #    只读纪律 docstring 就把 `mode=ro` 逐字引用了一遍，全文扫描对
            #    「摘掉 URI 参数」这种真实回归形态是抓不住的（A/B M4 定向验证）。
            arg_src = ast.get_source_segment(src, c.args[0]) if c.args else ""
            assert "mode=ro" in (arg_src or ""), (
                f"connect 的连接串未含 mode=ro：{arg_src!r}"
            )
        # 零写语句 / 零 DML / 零 commit / 不 import app.db
        # ⚠️ SQL 关键词按**大小写敏感**匹配：SQL 写语句在源码里必写作大写，
        #    而 `sys.path.insert` 这类 Python 内置同名方法是小写的，全文小写化
        #    扫描会把「把后端根目录塞进 sys.path」当成写库操作（本轮实踩）。
        for w in ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "TRUNCATE",
                  "REPLACE INTO", "commit(", "executemany", "executescript"):
            assert w not in src, f"盘点脚本出现写操作 {w} —— 它必须是纯只读"
        # 正向锁：每一条 execute() 的第一参数只能是 SELECT / PRAGMA
        for c in calls:
            if not (isinstance(c.func, ast.Attribute) and c.func.attr == "execute"):
                continue
            head = c.args[0] if c.args else None
            literal = head.value if isinstance(head, ast.Constant) else None
            start = literal if isinstance(literal, str) else ""
            if not start and isinstance(head, ast.JoinedStr):
                # f-string：取第一段字面量前缀
                first = next((v.value for v in head.values
                              if isinstance(v, ast.Constant) and isinstance(v.value, str)), "")
                start = first
            assert start.lstrip().upper().startswith(("SELECT", "PRAGMA")), (
                f"execute() 首参不是只读语句：{start[:40]!r}"
            )
        # 不 import 会打开写连接的路径 —— 按 AST 判，不按文本判：
        # 脚本 docstring 里就把 `app.db` / `get_conn` 作为**禁用反例**逐字引用了一遍
        # （本轮实踩：文本扫描自咬，与上方 SQL 关键词大小写同族）。
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("app.db"), (
                    f"盘点脚本 import 了写连接路径：{node.module}"
                )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("app.db"), (
                        f"盘点脚本 import 了写连接路径：{alias.name}"
                    )
        used_names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        assert "get_conn" not in used_names, "盘点脚本不得使用 app.db 的写连接"


# =========================================================================
# G3 —— 「剥离类」定性：两处判据副本修掉，四处剥离用途登记钉住
# =========================================================================
class TestRepairValidatorPredicateSingleSource:
    """BUG-F8 / F9：`count("```") % 2` 是第四套未闭合口径，两个方向都会错。"""

    def test_no_parity_count_left_in_source(self):
        for ln in _code_lines("services/repair_validator.py"):
            assert 'count("' + BT * 3 + '")' not in ln, f"围栏奇偶计数回流：{ln.strip()}"
            assert '"] % 2' not in ln, f"围栏奇偶判据回流：{ln.strip()}"

    def test_signature_and_strip_consume_single_source(self):
        sig_names = _names_in(_func("services/repair_validator.py", "structure_signature"))
        assert "iter_all_blocks" in sig_names and "find_unclosed" in sig_names, (
            "structure_signature 必须用唯一事实源给块数与未闭合数"
        )
        strip_names = _names_in(_func("services/repair_validator.py", "_strip_code_fences"))
        assert "fence_char_ranges" in strip_names, (
            "_strip_code_fences 必须按唯一事实源的区间剥离（旧非贪婪配对不认 ~~~ 与未闭合）"
        )

    def test_false_positive_fixed_labeled_line_inside_closed_block(self):
        """BUG-F8 假阳性：块内含 ```` ```json ```` 的**正常闭合**正文曾被判「未闭合」，
        导致一致性修复被判 failed、拒绝写库（探针实测 count=3）。"""
        sig = structure_signature(C1_LABELED_INSIDE)
        assert sig["unclosed_fences"] == 0, sig
        ok, problems = validate_repair(
            before="本章工期为 120 日历天。", after=C1_LABELED_INSIDE,
            wrong_values=["120 日历天"], authoritative_value="90 日历天")
        assert not any("围栏" in p for p in problems), f"仍误报未闭合：{problems}"
        assert ok is True, problems

    def test_false_negative_fixed_tilde_fence_blocks_write(self):
        """BUG-F8 假阴性：``~~~`` 真未闭合时旧计数=0 会**放行落库**（破损正文困住后续正文）。"""
        sig = structure_signature(C2_TILDE_UNCLOSED)
        assert sig["unclosed_fences"] == 1, sig
        ok, problems = validate_repair(
            before="本章工期为 120 日历天。", after=C2_TILDE_UNCLOSED,
            wrong_values=["120 日历天"], authoritative_value="90 日历天")
        assert any("代码围栏未闭合" in p for p in problems), problems
        assert ok is False, "真未闭合必须判失败（hard_keywords 含「代码围栏」）"

    def test_strip_covers_tilde_and_unclosed_blocks(self):
        """BUG-F9：剥离口径必须认 ~~~、且未闭合块剥到文末。"""
        from app.services.repair_validator import _strip_code_fences
        assert "graph TD" not in _strip_code_fences(C2_TILDE_UNCLOSED)
        assert "90 日历天" in _strip_code_fences(C2_TILDE_UNCLOSED)
        # 块内带标签行不得让闭合后的正文重新"可见"
        stripped = _strip_code_fences(C1_LABELED_INSIDE)
        assert '{"note"' not in stripped and "```" not in stripped
        assert "权威值" in stripped


class TestParagraphStructureUsesSingleSource:
    """G3：content_standard 的行级翻转状态机（第五套口径）已删。"""

    def test_no_local_flip_state_machine(self):
        src = _src("services/content_standard.py")
        code = "\n".join(_code_lines("services/content_standard.py"))
        assert "in_fence = not in_fence" not in code, "行级翻转判据回流"
        assigned = {tgt.id for node in ast.parse(src).body
                    if isinstance(node, ast.Assign)
                    for tgt in node.targets if isinstance(tgt, ast.Name)}
        assert "_PS_FENCE_LINE_RE" not in assigned, "本地围栏正则副本回流"

    # ⚠️ 本组刻意**只有判据锁、没有行为锁**，不是遗漏而是实测结论：
    #    `artifacts/r76b/_probe_g3_visible_diff.py` 用 7 份语料（含块内带标签行、
    #    4 开 3 闭、~~~ 未闭合、表行混排）对「可见行集合」做新旧差分，结果
    #    ANY_DIFF=False —— 上游 `strip_code_blocks`（保守非贪婪剥离）已先把围栏
    #    结构改写掉，翻转与严格两套口径到不了可观测差异。因此 A/B M3 由
    #    `test_report_consumes_iter_all_blocks`（AST 判据锁）定向抓住即为成立，
    #    补行为锁只会锁住一个恒等式（伪护栏）。

    def test_report_consumes_iter_all_blocks(self):
        names = _names_in(_func("services/content_standard.py", "paragraph_structure_report"))
        assert "iter_all_blocks" in names, (
            "段落结构校验必须用唯一事实源的块行区间（并 split('\\n') 与块行号对齐）"
        )

    def test_advisory_still_skips_unclosed_fence_tail(self):
        """行为等价锁：未闭合块之后的坏日期仍不得进 advisory（R65 既有意图不变）。"""
        from app.services.content_standard import paragraph_structure_report
        rep = paragraph_structure_report("正文正常。\n\n" + F3 + "mermaid\n2099/9/9 x")
        assert not [w for w in rep["warnings"] if w.get("type") == "bad_date"], rep


class TestStripClassRegistry:
    """「剥离/提取用途」四处登记：允许存在，但**不得演化成第二套闭合判据**。

    登记依据（G3 探针 `_probe_g3_census.py` + `_probe_g3_sites.txt` 实测）：
      · services/content_standard.py `_FENCE_RE` —— 保守剔除（含行内 ``` 片段），
        语义由 R65 与 test_content_standard_service.TestExtractors 钉住，刻意不改严格；
      · services/content_shrink.py —— `fence_spans()` 已走唯一事实源，另有一处
        剥 ```json 外壳；
      · services/ai/image_engine.py —— 剥 ```mermaid 外壳；
      · services/ai/json_response.py —— findall 提取块内 JSON；
      · routers/_chart_pipeline.py `_LEAD_IN_NOT_PARA_RE` —— 「这行像不像块起始」
        的段落识别排除项，不是闭合判定。
    """

    #: 只允许「识别/剥离/提取」，禁止出现的闭合判据名（出现 = 又长出一套口径）
    FORBIDDEN_CLOSE_PREDICATES = ("is_fence_close", "find_unclosed", "classify_fence_defects")

    @pytest.mark.parametrize("rel", [
        "services/ai/image_engine.py",
        "services/ai/json_response.py",
    ])
    def test_strip_only_modules_do_not_grow_close_predicate(self, rel):
        for ln in _code_lines(rel):
            for name in self.FORBIDDEN_CLOSE_PREDICATES:
                assert name not in ln, f"{rel} 把围栏判据扩权到闭合判定（{name}）：{ln.strip()}"

    def test_content_standard_fence_re_stays_single_definition(self):
        """`_FENCE_RE` 仍是且仅是一处「剥离」定义（同名遮蔽回归 + 不多头）。"""
        src = _src("services/content_standard.py")
        assert len(re.findall(r"^_FENCE_RE = ", src, re.M)) == 1
        # 且它只能被 strip_code_blocks 消费（防止有人拿它当闭合判据复用）
        users = [n.name for n in ast.walk(ast.parse(src)) if isinstance(n, ast.FunctionDef)
                 and any(isinstance(s, ast.Name) and s.id == "_FENCE_RE"
                         for s in ast.walk(n))]
        assert users == ["strip_code_blocks"], f"_FENCE_RE 消费方扩散：{users}"

    def test_content_shrink_protection_still_via_fence_spans(self):
        """压缩禁区必须继续走 `fence_spans`（它已委托唯一事实源），不得自写配对。"""
        for ln in _code_lines("services/content_shrink.py"):
            assert BT * 3 + "[\\s\\S]" not in ln, "content_shrink 长出非贪婪配对副本"
        assert "fence_spans" in _src("services/content_shrink.py")
