# -*- coding: utf-8 -*-
"""图表内联围栏「登记 / 改写 / 导出」三侧口径一致性回归。

背景（2026-09-23 幽灵图根因修复）：
- 登记侧 `_scan_inline_charts` 在 2026-09-22 引入了"超长但闭合"（500<行数≤1000、
  首尾围栏齐全、块内无中文句读正文行）的**有界前视恢复**；
- 但导出侧 `export._parse_content_blocks` 与改写侧 `_rewrite_code_block` 未同步该恢复，
  仍按"超过 MAX_INLINE_CODE_BLOCK_LINES 即判未闭合"处理；
- 后果：一份合法的超长图表块被登记入库（出现在图表清单 / 预览），却在导出 DOCX 时
  被整块跳过 → "幽灵图"（正是本仓反复强调要杜绝的三侧口径分叉）。

修复：三侧统一改用共用扫描器 `read_fenced_block`。本文件锁定该保证，防止再次分叉。
"""
import json

from app.routers._chart_pipeline import (
    MAX_INLINE_CODE_BLOCK_LINES,
    _rewrite_code_block,
    _scan_inline_charts,
    read_fenced_block,
)
from app.routers.export import _parse_content_blocks


def _mk_long_labor_json(internal_lines: int) -> str:
    """构造一个"闭合、行数≈internal_lines、块内无中文句读行"的合法 chart-json 块内容。

    labor 属 7 类白名单，登记侧（type 在白名单）与导出侧（type∈PIL_RENDERABLE）都会收；
    phases 每项独占一行以撑到目标行数（JSON 行以 `,`/`]` 收尾，不触发句读防误吞守卫）。
    """
    n = internal_lines - 5  # 预留 type/title/categories/data/外层括号等固定行
    n = max(1, n)
    phase_lines = ",".join(f'\n  "阶段{i}"' for i in range(n))
    return ('{"type":"labor","title":"超长劳动力图","phases":[' + phase_lines +
            '],"categories":["普工"],"data":' + json.dumps([[1] * n]) + "}")


def _export_chart_blocks(content: str) -> list[dict]:
    return [b for b in _parse_content_blocks(content) if b["type"] == "chart"]


# ---------------------------------------------------------------------------
# 1. read_fenced_block 状态机本身
# ---------------------------------------------------------------------------

def test_read_fenced_block_states():
    lines = ("```chart-json\n{}\n```").split("\n")
    code, state, nxt = read_fenced_block(lines, 1)
    assert state == "closed"
    assert nxt == 3  # 跳过结束围栏

    # EOF 未闭合：把剩余行交出，state=eof
    lines = ("```mermaid\ngraph TD\n A-->B").split("\n")
    code, state, nxt = read_fenced_block(lines, 1)
    assert state == "eof"
    assert code == ["graph TD", " A-->B"]


def test_read_fenced_block_recover_vs_truncate():
    maxn = MAX_INLINE_CODE_BLOCK_LINES
    # 闭合围栏位于再 maxn 行之内、无句读 → recovered
    body = ["x"] * (maxn + 10)
    lines = ["```"] + body + ["```"] + ["tail"]
    code, state, nxt = read_fenced_block(lines, 1)
    assert state == "recovered"
    assert len(code) == maxn + 10
    assert lines[nxt] == "tail"

    # 闭合围栏太远（> maxn 行外）→ truncated（判未闭合），停在截断点
    far = ["y"] * (maxn * 2 + 5)
    lines = ["```"] + far + ["```"]
    code, state, nxt = read_fenced_block(lines, 1)
    assert state == "truncated"

    # 块内混入中文句读正文行 → 即便窗口内有闭合围栏也判 truncated（防误吞）
    mixed = ["a"] * maxn + ["这是正文句子。"] + ["b"] * 5
    lines = ["```"] + mixed + ["```"]
    code, state, nxt = read_fenced_block(lines, 1)
    assert state == "truncated"


# ---------------------------------------------------------------------------
# 2. 核心回归：合法超长闭合块，登记侧与导出侧必须同口径（都是 1）
# ---------------------------------------------------------------------------

def test_long_closed_block_registered_and_exported():
    assert MAX_INLINE_CODE_BLOCK_LINES < 501, "本用例依赖阈值=500（恢复窗口 (500,1000]）"
    code = _mk_long_labor_json(MAX_INLINE_CODE_BLOCK_LINES + 100)  # ~600 行，闭合
    assert code.count("\n") + 1 > MAX_INLINE_CODE_BLOCK_LINES
    content = "前言段落。\n\n```chart-json\n" + code + "\n```\n\n后记段落。\n"

    scan = _scan_inline_charts(content)
    export_blocks = _export_chart_blocks(content)
    # 修复前：登记侧提取 1、导出侧 0（幽灵图）。修复后：两侧一致，均为 1。
    assert len(scan) == 1, "登记侧应提取到该合法超长闭合块"
    assert scan[0][0] == "labor"
    assert len(export_blocks) == 1, "导出侧必须同样解析出该块（幽灵图回归）"
    assert export_blocks[0]["chart_type"] == "labor"


def test_overlong_or_mixed_block_skipped_on_both_sides():
    """超出恢复窗口（闭合围栏 > 2×MAX 行外）的块：两侧都必须跳过，保持一致。"""
    maxn = MAX_INLINE_CODE_BLOCK_LINES
    huge = _mk_long_labor_json(maxn * 2 + 50)  # 闭合围栏远在恢复窗口之外
    content = "```chart-json\n" + huge + "\n```\n"
    assert len(_scan_inline_charts(content)) == 0
    assert len(_export_chart_blocks(content)) == 0


# ---------------------------------------------------------------------------
# 3. 改写侧同样能匹配/删除合法超长闭合块（修复前会静默 no-op）
# ---------------------------------------------------------------------------

def test_rewrite_can_delete_long_closed_block():
    code = _mk_long_labor_json(MAX_INLINE_CODE_BLOCK_LINES + 60)
    content = "前言。\n\n```chart-json\n" + code + "\n```\n\n后记。\n"
    # 该块确被登记侧识别，old_code 用 strip 后的块内容
    scan = _scan_inline_charts(content)
    assert len(scan) == 1
    old_code = scan[0][1]
    # 删除
    deleted = _rewrite_code_block(content, old_code, None)
    assert len(_scan_inline_charts(deleted)) == 0, "删除必须命中合法超长闭合块"
    # 替换（用一段短合法块）
    replaced = _rewrite_code_block(
        content, old_code, '{"type":"labor","title":"t","phases":["a","b","c"],'
        '"categories":["x"],"data":[[1],[2],[3]]}')
    assert len(_scan_inline_charts(replaced)) == 1
    assert "阶段0" not in replaced


# ---------------------------------------------------------------------------
# 4. 反例：真正未闭合（EOF 截断）的 mermaid 块 —— 两侧都不产图，且正文被还原
# ---------------------------------------------------------------------------

def test_unclosed_mermaid_skipped_and_body_preserved():
    content = "```mermaid\ngraph TD\n    A-->B\n随后是被吞掉的正文句子。\n下一段正常文字。\n"
    # 登记侧：未闭合块不提取（EOF 情形下若类型可识别会提取原始残片，但导出侧必须跳过）
    export_blocks = _export_chart_blocks(content)
    assert len(export_blocks) == 0, "未闭合 mermaid 不得产出图表块"
    # 其后正文不得被吞没：至少有一个段落把"正文句子"还原回来
    paras = "".join(b.get("text", "") for b in _parse_content_blocks(content)
                    if b["type"] == "paragraph")
    assert "正文句子" in paras or "正常文字" in paras, "未闭合围栏后的正文应被还原"
