"""2026-10-05 · 导出模块主链路原子性/幂等性 定点回归。

覆盖本轮修复的两条 P1 缺陷：
- F-1（PDF 缓存原子替换）：``export_pdf`` 缓存写入旧实现直接
  ``out_path.write_bytes(pdf_bytes)``，非原子——并发同指纹导出、
  或写盘崩溃时会在 out_path 残留**半截 PDF**，下次同指纹命中缓存
  （size>0 守卫拦不住 >0 字节的半截文件）直接 FileResponse 交付坏档。
  修复：先写唯一 tmp → DB INSERT → ``os.replace`` 原子替换；
  失败一律清理 tmp。与 DOCX 分支已有的 ``os.replace`` 语义对齐。

- F-3（chart_pipeline INSERT 幽灵图）：``apply_inline_chart_plan``
  内的 ``INSERT INTO chart_predictions`` 未校验返回值/异常。
  aiosqlite 在事务冲突时 ``execute()`` 可能返回 None（同函数内
  SELECT 早已做 R13 判空），INSERT 却静默吞掉 →
  ``chart_predictions`` 缺一条但正文里那张图仍在，
  导出照渲、清单看不见（幽灵图）。
  修复：INSERT 显式 try/except + 判空，任一失败补进 skipped_types
  → 下一轮正文裁剪会精确定位并删除该块，落库与正文两侧一致。

测试策略与项目既有一致：AST/源码静态断言 + 纯逻辑，零 AI、零真实 DB。
"""
from __future__ import annotations

import ast
import io
import os
import re
import sys
import uuid
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
EX_PATH = os.path.join(BACKEND, "app", "routers", "export.py")
CP_PATH = os.path.join(BACKEND, "app", "routers", "_chart_pipeline.py")


def _src(path: str) -> str:
    with io.open(path, encoding="utf-8") as f:
        return f.read()


def _fn(path: str, name: str) -> str:
    src = _src(path)
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    raise AssertionError("未找到函数 " + name)


# =============================================================================
# F-1：PDF 缓存原子替换回归
# =============================================================================

def test_f1_pdf_cache_writes_via_atomic_replace():
    """PDF 缓存写入必须走 tmp + os.replace 原子替换（不得直接 write_bytes 到 out_path）。"""
    src = _fn(EX_PATH, "export_pdf")
    # 必须引入 os.replace（可能以 import 别名）
    assert "os.replace" in src or "_os.replace" in src, (
        "PDF 分支未做原子替换，与 DOCX 分支不对称（F-1 回退）")
    # 必须先生成唯一 tmp 文件路径
    assert ".tmp.pdf" in src, (
        "PDF 分支缺少 .tmp.pdf 临时文件名模式（F-1 回退）")
    assert "uuid.uuid4().hex" in src, (
        "PDF tmp 文件名必须含 uuid（否则并发同指纹会互相覆盖 tmp）")


def test_f1_pdf_cache_does_not_directly_write_out_path():
    """禁止 out_path.write_bytes(pdf_bytes) 直写——非原子是 F-1 的根因。

    注意：源码注释里可能引用旧实现的伪代码，因此这里用「去掉注释后」的
    实际可执行文本做匹配，避免注释文字误报。
    """
    src = _fn(EX_PATH, "export_pdf")
    # 去掉行注释（Python 里 '#' 起始到行尾），再匹配
    code_only = "\n".join(
        re.sub(r"#.*$", "", line, count=1) for line in src.splitlines())
    assert "out_path.write_bytes" not in code_only, (
        "PDF 缓存仍在直写 out_path（F-1 回退：并发/崩溃下残留半截 PDF）")


def test_f1_pdf_cache_cleans_tmp_on_failure():
    """异常路径必须清理 tmp，否则磁盘孤儿。"""
    src = _fn(EX_PATH, "export_pdf")
    assert "unlink(missing_ok=True)" in src, (
        "PDF 分支异常路径未清理 tmp（F-1 回退）")


def test_f1_pdf_replace_loop_retries_on_permission_error():
    """os.replace 遇 PermissionError 需 3 次退避重试（与 DOCX 分支同款）。"""
    src = _fn(EX_PATH, "export_pdf")
    assert "PermissionError" in src, (
        "PDF 分支未处理 PermissionError（Windows Word 占用场景）")
    assert "range(3)" in src, (
        "PDF 分支 os.replace 未做重试循环（F-1 回退）")


def test_f1_pdf_cache_hit_guards_size_gt_zero():
    """缓存命中守卫必须同时校验存在 + size>0（旧实现的 size>0 已拦 >0 半截文件——
    但 F-1 修好后不再产生半截文件，此守卫保留作纵深）。"""
    src = _fn(EX_PATH, "export_pdf")
    assert "Path(cached[0]).stat().st_size > 0" in src, (
        "PDF 缓存命中守卫缺 size>0 检查（纵深防御回退）")


# =============================================================================
# F-3：chart_pipeline INSERT 幽灵图回归
# =============================================================================

def test_f3_insert_result_is_checked_not_ignored():
    """INSERT 返回值必须被显式判定，禁止裸 ``await db.execute("INSERT ...")`` 无接收。"""
    src = _fn(CP_PATH, "apply_inline_chart_plan")
    # 修复后应有 `_res = await db.execute(...)` 形式的赋值
    assert "_res = await db.execute" in src, (
        "apply_inline_chart_plan 内 INSERT 未接收返回值（F-3 回退：幽灵图）")
    assert "if _res is None" in src, (
        "INSERT 返回值未判空（F-3 回退：aiosqlite 事务冲突时 None 静默吞）")
    assert "skipped_types.add(ct)" in src, (
        "INSERT 失败/返回 None 时未加入 skipped_types（正文不会同步裁剪）")


def test_f3_insert_wrapped_in_try_except():
    """INSERT 必须包裹 try/except，异常时同样降级为 skipped。"""
    src = _fn(CP_PATH, "apply_inline_chart_plan")
    # 找 INSERT 语句前后是否有 try/except
    i_insert = src.find('INSERT INTO chart_predictions')
    assert i_insert >= 0, "未找到 chart_predictions INSERT"
    # INSERT 前 500 字符内应出现 try:
    window = src[max(0, i_insert - 400):i_insert]
    assert "try:" in window, (
        "INSERT 未被 try/except 包裹（F-3 回退：异常时不降级 skipped）")
    # INSERT 后应出现 except 处理
    after = src[i_insert:i_insert + 500]
    assert "except Exception" in after, (
        "INSERT try 块无 except 处理（F-3 回退）")


def test_f3_skipped_types_feeds_into_content_edit():
    """skipped_types 必须驱动正文同步裁剪（保持落库/正文两侧一致）。"""
    src = _fn(CP_PATH, "apply_inline_chart_plan")
    # 关键代码形态：`if content is not None and skipped_types:`
    assert "if content is not None and skipped_types" in src, (
        "skipped_types 未驱动正文裁剪（幽灵图路径回退）")
    assert "_scan_chart_fences_full(content)" in src, (
        "正文裁剪未按围栏序号精确定位（可能误删其它类型的相同内容块）")


# =============================================================================
# F-3 行为级：INSERT 抛异常 / 返回 None 都要 skipped，不再写入
# =============================================================================

class _CountingDB:
    """最小 DB stub：记录 execute 调用，可按 SQL 前缀决定返回异常或 None。"""

    def __init__(self, insert_behavior: str = "ok"):
        # insert_behavior ∈ {"ok", "none", "raise"}
        self.insert_behavior = insert_behavior
        self.insert_calls: list[tuple] = []

    async def execute(self, sql: str, params=()):
        head = sql.lstrip().upper()
        if head.startswith("DELETE"):
            return None
        if head.startswith("SELECT"):
            # R13 已修的 SELECT 复核路径：返回空 fetchall
            class _C:
                async def fetchall(self):
                    return []
            return _C()
        if head.startswith("INSERT"):
            self.insert_calls.append(params)
            if self.insert_behavior == "none":
                return None
            if self.insert_behavior == "raise":
                raise RuntimeError("INSERT 事务冲突（模拟）")
            # 正常 aiosqlite INSERT 返回 Cursor（有 rowcount 属性），非 None
            class _FakeCur:
                rowcount = 1
                lastrowid = 1
            return _FakeCur()
        return None


def _install_fake_scanner(monkeypatch, fences: list[tuple[str, str, int]]):
    """把 `_scan_chart_fences_full` 打桩成直接返回固定结果。

    `_apply_chart_fence_edits` 会按 fence 顺序回写原始行，但**当 fence_ord
    遇到第一个真实 fence 时才匹配 edits**——为了让 `_apply_chart_fence_edits`
    真的能把测试正文里那些 ```code``` 段当作图表围栏处理并删除，最简单
    的做法是让测试正文里的 ```` ```pie ```` 就是真正的图表围栏——但
    `INLINE_CHART_FENCE_LANGS` 只认 mermaid/chart-json/ai_image。

    这里 monkeypatch `_scan_chart_fences_full` 让其返回预设的
    [(chart_type, code, ordinal)]，让「正文里有几块、哪些类型」完全由
    测试决定；同时 patch `_apply_chart_fence_edits` 让删除直接以
    正则去掉对应 code 块（等价语义，避免依赖真实围栏解析器）。
    """
    import app.routers._chart_pipeline as cp

    monkeypatch.setattr(cp, "_scan_chart_fences_full",
                        lambda content: fences)

    def _fake_apply(content, edits):
        # edits 形如 {ordinal: None}，None 表示删除该 fence 块。
        # 与真实实现等价的语义：按 code 精确删除该块及其围栏。
        for _ord, new_code in edits.items():
            if new_code is None:
                # 找到对应的 code，用正则去掉其围栏
                code = fences[_ord][1]
                pattern = r"```\s*\n" + re.escape(code) + r"\s*\n```\s*"
                content = re.sub(pattern, "", content, count=1)
            # 非 None 的替换场景本测试用不到
        return content

    monkeypatch.setattr(cp, "_apply_chart_fence_edits", _fake_apply)


@pytest.mark.asyncio
async def test_f3_behavior_insert_returns_none_marks_skipped(monkeypatch):
    """INSERT 返回 None 时不应计入 running（不产生幽灵图口径），
    且 skipped_types 应包含该类型 → 正文中该块被裁剪。"""
    import app.routers._chart_pipeline as cp
    from app.routers._chart_pipeline import apply_inline_chart_plan

    # 让扫描器认为正文里有一块 chart_type=pie 的图（用围栏 code 作锚点）
    fence_code = "pie-chart-payload"
    _install_fake_scanner(monkeypatch, [("pie", fence_code, 0)])

    section_id = "sec-1"
    scheme_id = "scheme-1"
    row = (str(uuid.uuid4()), section_id, scheme_id, "pie",
           "标题", "目的", "generated", "{}")
    db = _CountingDB(insert_behavior="none")
    content = f"前言\n```\n{fence_code}\n```\n后记"
    result = await apply_inline_chart_plan(db, section_id, [row], content)
    # INSERT 尝试发生了
    assert len(db.insert_calls) == 1
    # 正文中该图块应被裁剪（skipped_types 驱动）
    assert result is not None
    assert fence_code not in result, (
        f"INSERT 返回 None 后正文未裁剪 → 幽灵图（实际={result!r}）")
    # 未图外的正文应保留
    assert "前言" in result and "后记" in result


@pytest.mark.asyncio
async def test_f3_behavior_insert_raises_marks_skipped(monkeypatch):
    """INSERT 抛异常时同样应把该类型标记为 skipped（正文同步裁剪）。"""
    import app.routers._chart_pipeline as cp
    from app.routers._chart_pipeline import apply_inline_chart_plan

    fence_code = "bar-chart-payload"
    _install_fake_scanner(monkeypatch, [("bar", fence_code, 0)])

    section_id = "sec-1"
    scheme_id = "scheme-1"
    row = (str(uuid.uuid4()), section_id, scheme_id, "bar",
           "标题", "目的", "generated", "{}")
    db = _CountingDB(insert_behavior="raise")
    content = f"前言\n```\n{fence_code}\n```\n后记"
    result = await apply_inline_chart_plan(db, section_id, [row], content)
    # 异常路径不抛出，而是静默降级
    assert result is not None
    assert fence_code not in result, (
        "INSERT 抛异常后正文未裁剪 → 幽灵图（异常被吞掉但没降级）")


@pytest.mark.asyncio
async def test_f3_behavior_insert_ok_keeps_content(monkeypatch):
    """INSERT 正常成功时，正文保持不动（不裁剪）。"""
    import app.routers._chart_pipeline as cp
    from app.routers._chart_pipeline import apply_inline_chart_plan

    fence_code = "pie-chart-payload"
    _install_fake_scanner(monkeypatch, [("pie", fence_code, 0)])

    section_id = "sec-1"
    scheme_id = "scheme-1"
    row = (str(uuid.uuid4()), section_id, scheme_id, "pie",
           "标题", "目的", "generated", "{}")
    db = _CountingDB(insert_behavior="ok")
    content = f"前言\n```\n{fence_code}\n```\n后记"
    result = await apply_inline_chart_plan(db, section_id, [row], content)
    assert result == content, "INSERT 成功时不应改动正文"
