# -*- coding: utf-8 -*-
"""正文续写的「前文尾部上下文」与「重复剥离」纯函数测试。

覆盖 2026-09-16 修复的两处缺陷（此前无任何测试锁定，属纯函数、无 IO）：

1. `_safe_tail` —— 送入续写提示词的「前文结尾」片段。
   旧实现只看**尾部片段内** ``` 的奇偶、并从「片段中第一个围栏之后」截取，
   存在两类错误：
     a) 切点落在代码块内部时，片段以代码内容开头 —— 跳过第一个围栏**并不能**
        去掉泄漏，模型会把 Mermaid/chart-json 代码当散文继续复述；
     b) 片段以未闭合围栏结尾时（AI 输出被截断），旧实现不再处理，模型收到
        半个代码块后会"热心"补一个闭合围栏并臆造图表内容。
   新实现：用**整篇正文**判断切点是否落在围栏内（奇偶）→ 前移跳过该块；
   再对尾部做"文末未闭合块"裁剪。不变量：产出的尾部围栏数为偶数，
   且不以代码围栏内容开头。

2. `_dedup_continuation` —— 续写与前文的重复剥离。
   字符级重叠原实现以**步长 20** 试探前缀是否出现在前文尾部，只有重叠长度
   恰好落在 len(b)-20k 的离散点上才命中，绝大多数真实重复被漏检
   （正文出现成段复述）。新实现精确求最长重叠（先接缝对齐，再兜底全尾搜索）。
"""
import pytest

from app.routers.sse_handlers import _dedup_continuation, _safe_tail


# ---------------------------------------------------------------- _safe_tail

def test_short_text_returned_as_is():
    text = "混凝土浇筑前应检查模板与钢筋。"
    assert _safe_tail(text, 2000) == text


def test_empty_input():
    assert _safe_tail("", 100) == ""
    assert _safe_tail(None, 100) == ""


def test_plain_text_backs_off_to_newline():
    """非围栏场景：首行距切点 < 200 字时回退到换行边界，避免半行开头。"""
    text = "甲" * 1000 + "\n" + "乙" * 50
    assert _safe_tail(text, 200) == "乙" * 50


def test_plain_text_single_long_line_kept():
    """整篇无换行时不强行截短（否则上下文可能只剩几十字）。"""
    text = "甲" * 500
    assert len(_safe_tail(text, 200)) == 200


def test_cut_inside_code_block_skips_to_prose():
    """✅ 修复 a)：切点落在代码块内部 → 前移到该块结束之后，只留散文。"""
    prose_head = "甲" * 200
    block = "```mermaid\nflowchart TD\n  A --> B\n```"
    prose_tail = "乙" * 80
    text = prose_head + block + "\n" + prose_tail
    cut = len(prose_head) + 5          # 落在 block 内部
    tail = _safe_tail(text, len(text) - cut)
    assert tail == prose_tail
    assert "flowchart" not in tail and "```" not in tail


def test_old_behaviour_leak_regression():
    """✅ 修复 a) 的关键回归：旧实现在本场景会把代码当散文送出。

    构造：切点落在闭合块 A 内部 → 尾部片段以 A 的代码开头（1 个闭合围栏），
    之后是散文，最后是**未闭合**的块 B（AI 截断）。旧实现只切一次：
    片段内围栏数为偶数（A 的闭合 + B 的开启）→ 完全不处理，
    代码泄漏 + 半截 JSON 一起进入续写提示词。
    """
    prose_head = "甲" * 120
    block_a = "```mermaid\nflowchart TD\n  A --> B\n```"
    prose_mid = "乙" * 60
    block_b_unclosed = "```chart-json\n{\"type\":\"labor\",\"rows\":[{\"name\":\"钢筋工\""
    text = prose_head + block_a + prose_mid + block_b_unclosed
    cut = len(prose_head) + 10
    tail = _safe_tail(text, len(text) - cut)
    assert tail == prose_mid
    for leak in ("mermaid", "flowchart", "chart-json", "```"):
        assert leak not in tail


def test_trailing_unclosed_block_dropped():
    """✅ 修复 b)：正文以未闭合块结尾 → 整块丢弃（宁缺勿滥）。"""
    prose = "丙" * 150
    text = prose + "```chart-json\n{\"type\":\"labor\",\"rows\":[1,2"
    assert _safe_tail(text, len(text)) == prose
    # 切点落在散文区内、未闭合块在尾部时同样要裁掉
    assert _safe_tail(text, 200) == prose[-200:]


def test_unclosed_block_only_text_yields_empty():
    """整段只有未闭合围栏（无可用散文）→ 返回空串，而不是半截代码。"""
    assert _safe_tail("```mermaid\nflowchart TD\n A-->B", 1000) == ""


@pytest.mark.parametrize("text", [
    "散文段落。" * 20,
    "甲" * 30 + "```mermaid\nflowchart TD\n A-->B\n```" + "乙" * 200,
    "甲" * 200 + "```mermaid\nflowchart TD\n A-->B\n```" + "乙" * 40 + "```chart-json\n{\"type\":\"labor\"",
    "```mermaid\nflowchart TD\n A-->B\n```" + "丙" * 300,
    "甲" * 50 + "```\ncode```\n" + "乙" * 50 + "```gantt\n{}",
    ("段落" * 100) + "\n\n" + "```chart-json\n{\"type\":\"timeline\",\"items\":[]}\n```" + "尾声" * 50,
])
@pytest.mark.parametrize("limit", [40, 120, 300, 2000])
def test_safe_tail_never_leaves_unbalanced_fence(text, limit):
    """不变量：任何切点/长度下，产出的尾部围栏数都是偶数（模型不会看到半截代码）。

    注：尾部**以完整代码块开头**是允许的（成对围栏，模型能正确识别），
    不允许的是"半截块"——故只断言奇偶，不断言首字符。
    """
    tail = _safe_tail(text, limit)
    assert tail.count("```") % 2 == 0, f"未闭合围栏: {tail[:80]!r}"


# ------------------------------------------------------- _dedup_continuation

def test_dedup_strips_duplicated_paragraph():
    """整段复述（段落级）：段落已在前文出现 → 整段剥离。"""
    dup = ("振捣棒应快插慢拔，插点均匀布置，不得漏振或过振；"
           "以混凝土表面泛浆、不再显著下沉且无气泡逸出为振捣密实标准；"
           "分层浇筑时每层厚度不应超过振动器作用部分长度的 1.25 倍。")
    assert len(dup) >= 40
    prev = "甲" * 200 + "\n" + dup
    cont = dup + "\n养护措施：浇筑完毕后 12h 内覆盖并保湿。"
    out = _dedup_continuation(prev, cont)
    assert dup not in out
    assert out.startswith("养护措施")


def test_dedup_strips_seam_overlap_exact():
    """✅ 修复：接缝重叠（续写重抄了前文最后一句再往下写）必须精确剥离。

    旧实现按步长 20 试探，只有重叠长度恰好落在离散点上才命中：
    本用例的重叠长度 = len(shared)，旧实现会整段漏检。
    """
    shared = "立杆间距、水平杆步距、扫地杆设置与剪刀撑布置应符合方案要求，并逐根检查扣件拧紧力矩"
    tail_text = "验收合格后方可进入下道工序。"
    prev = "甲" * 200 + shared
    cont = shared + tail_text
    out = _dedup_continuation(prev, cont)
    assert out == tail_text


def test_dedup_seam_overlap_len_independent():
    """重叠长度任意（不止 len(b)-20k）时都要命中 —— 参数化锁定修复效果。"""
    base_shared = "模板支撑体系搭设完成后应组织验收，验收内容包括立杆垂直度、水平杆步距与剪刀撑设置"
    for extra in (1, 7, 20, 33):
        shared = base_shared + "，另加说明" + "。" * extra
        prev = "甲" * 100 + shared
        cont = shared + "随后进行混凝土浇筑。"
        out = _dedup_continuation(prev, cont)
        assert out == "随后进行混凝土浇筑。", f"extra={extra} 漏检重叠: {out[:40]!r}"


def test_dedup_fallback_finds_prefix_anywhere_in_tail():
    """兜底分支：续写前缀在前文尾部**任意位置**原文出现（弱模型跨段复述）。

    ⚠️ 前缀长度必须 ≥ min_overlap(40)：短于阈值的重叠按设计不剥离
    （避免误删正常短句）。
    """
    shared = ("安全技术交底应由项目技术负责人组织，参加人员须签字确认，"
              "交底记录随施工资料一并归档保存")
    assert len(shared) >= 40
    prev = shared + "。" + "乙" * 300      # shared 不在尾部最末，但在 tail 窗口内
    cont = shared + "，交底内容变更时应重新交底。"
    out = _dedup_continuation(prev, cont)
    assert shared not in out
    assert out.startswith("交底内容变更")


def test_dedup_no_overlap_returns_original():
    prev = "甲" * 100
    cont = "全新的补充内容：本节说明冬季施工的测温频次与保温措施。"
    assert _dedup_continuation(prev, cont) == cont


def test_dedup_empty_and_short_inputs():
    assert _dedup_continuation("甲" * 100, "") == ""
    assert _dedup_continuation("", "短续写") == "短续写"
    assert _dedup_continuation(None, "短续写") == "短续写"
    # 短于 min_overlap 的前缀不做字符级剥离（避免误删正常短句）
    assert _dedup_continuation("甲" * 100, "短句。") == "短句。"


def test_dedup_multiple_paragraphs_stripped():
    """多段复述：循环须剥离连续的重复段落（上限 8 段）。"""
    p1 = "第一段：基坑开挖应分层分段进行，每层开挖深度不宜大于 2m，严禁超挖。"
    p2 = "第二段：开挖至基底后应及时验槽，验槽合格后方可进行垫层施工，避免基底浸泡。"
    prev = "甲" * 200 + "\n" + p1 + "\n" + p2
    cont = p1 + "\n" + p2 + "\n" + "第三段：雨季施工应设置排水沟与集水井。"
    out = _dedup_continuation(prev, cont)
    assert p1 not in out and p2 not in out
    assert out.startswith("第三段")