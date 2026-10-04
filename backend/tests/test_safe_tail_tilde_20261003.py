"""续写上下文裁剪 _safe_tail 对波浪号（~~~）围栏的口径修复（2026-10-03）。

背景：_safe_tail 用于构造续写提示词的「前文结尾」上下文，旧实现只数 "```" 子串
判断切点是否落在代码块内 / 文末是否未闭合，对 ~~~mermaid 围栏完全失效：
- 切点在 ~~~ 代码块内时不跳过 → 把图表代码当散文喂给模型；
- 文末未闭合的 ~~~ 块不裁剪 → 模型收到半截代码块后臆造图表内容污染正文。

本文件锁定 _safe_tail 对两种围栏同口径处理（与正文生成 / 登记 / 导出一致）。
关键契约：
- 切点在代码块「内部」（切点前围栏数为奇数）→ 跳到闭合围栏之后，返回散文；
- 文末块「未闭合」（尾片段围栏数为奇数）→ 整块丢弃，宁缺勿滥；
- 切点落在代码块「之前」（围栏数为偶数）→ 块作为「前文结尾」保留，不得误删。
"""
import pytest
from app.routers.sse_handlers import _safe_tail

OPEN_BT = "```mermaid"
OPEN_TL = "~~~mermaid"
CLOSE_BT = "```"
CLOSE_TL = "~~~"


@pytest.mark.parametrize("open_marker,close_marker", [(OPEN_BT, CLOSE_BT), (OPEN_TL, CLOSE_TL)])
def test_safe_tail_skips_block_when_cut_inside(open_marker, close_marker):
    # 短前缀 + 块落在尾部 limit 窗口内、且在切点之前 ⇒ 切点落在块「内部」（奇偶=1）
    text = "X" * 10 + "\n" + open_marker + "\ngraph TD\n A-->B\n" + close_marker + "\n后续散文内容here"
    tail = _safe_tail(text, limit=30)
    assert "后续散文内容" in tail          # 闭合围栏之后的散文得到保留
    assert "graph TD" not in tail          # 块内图表代码被跳过
    assert open_marker not in tail         # 开围栏被跳过
    assert close_marker not in tail        # 闭围栏被跳过


@pytest.mark.parametrize("open_marker,close_marker", [(OPEN_BT, CLOSE_BT), (OPEN_TL, CLOSE_TL)])
def test_safe_tail_drops_unclosed_block_at_end(open_marker, close_marker):
    # 文末未闭合块（无闭围栏）⇒ 整块丢弃
    text = "正文内容" * 6 + "\n" + open_marker + "\ngraph TD\n A-->B"
    tail = _safe_tail(text, limit=200)
    assert "graph TD" not in tail
    assert open_marker not in tail


@pytest.mark.parametrize("open_marker,close_marker", [(OPEN_BT, CLOSE_BT), (OPEN_TL, CLOSE_TL)])
def test_safe_tail_keeps_block_when_cut_before(open_marker, close_marker):
    # 块位于切点「之前」（奇偶=0）：块作为前文结尾保留，不得误删
    text = "长前缀散文" * 4 + "\n" + open_marker + "\ngraph TD\n A-->B\n" + close_marker + "\n收尾散文。"
    tail = _safe_tail(text, limit=200)
    assert open_marker in tail               # 开围栏保留
    assert "graph TD" in tail                # 块内代码作为上下文保留
    assert "收尾散文" in tail


@pytest.mark.parametrize("open_marker,close_marker", [(OPEN_BT, CLOSE_BT), (OPEN_TL, CLOSE_TL)])
def test_safe_tail_parity_identical_for_both_fences(open_marker, close_marker):
    # 同一条正文，仅围栏类型不同，两种围栏必须得到逐字一致的结果
    bt_text = "P" * 10 + "\n" + OPEN_BT + "\ngraph TD\n A-->B\n" + CLOSE_BT + "\n后续散文内容here"
    tl_text = "P" * 10 + "\n" + OPEN_TL + "\ngraph TD\n A-->B\n" + CLOSE_TL + "\n后续散文内容here"
    assert _safe_tail(bt_text, limit=30) == _safe_tail(tl_text, limit=30)


@pytest.mark.parametrize("open_marker,close_marker", [("````mermaid", "````"), ("~~~~mermaid", "~~~~")])
def test_safe_tail_skips_full_marker_for_4plus_fences(open_marker, close_marker):
    # 4+ 字符围栏：必须跳过完整标记（旧实现只跳 3 字符，会残留 1 个标记字符）
    text = "P" * 10 + "\n" + open_marker + "\ngraph TD\n A-->B\n" + close_marker + "\n后续散文内容here"
    tail = _safe_tail(text, limit=30)
    assert "后续散文内容" in tail
    assert "graph TD" not in tail
    # 不得残留孤立的围栏标记字符（` 或 ~）
    assert "`" not in tail
    assert "~" not in tail
