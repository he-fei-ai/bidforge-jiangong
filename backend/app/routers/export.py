"""DOCX 导出路由（增强版 — 图表渲染 + 封面 + 目录 + 标题编号）"""

import asyncio
import base64
import hashlib
import ipaddress
import json
import logging
import os
import re
import socket
import sqlite3
import threading
import time
import uuid
from datetime import datetime
from io import BytesIO
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, JSONResponse

from app.config import EXPORTS_DIR
from app.db import get_db, read_db, retry_db_op

# ✅ 围栏保护阈值与图表围栏家族统一取自登记侧（_chart_pipeline），
#    两端必须同口径：旧实现登记侧按「500 行」判未闭合、导出侧按「8000 字符」判，
#    单位与阈值都不同，导致一份"登记侧判未闭合跳过、导出侧照常渲染"的幽灵图。
# ✅ 围栏读取本身（含"超长但闭合"的有界前视恢复）统一取自 read_fenced_block，
#    登记/改写/导出三侧共用同一扫描器，杜绝"合法超长块被登记却不被导出渲染"的幽灵图。
from app.routers._chart_pipeline import (  # noqa: F401 — 拆分后保留绑定（_export_issue_scan 经 _sh 引用）
    INLINE_CHART_FENCE_LANGS,
    parse_fence_line,
)
from app.services import docx_math  # noqa: F401 — 拆分后保留绑定（_export_docx_parts 经 _sh 引用）
from app.services.ai.json_response import strip_outline_numbering

# ✅ 图表关键字→内部类型映射统一取自 chart_validators（唯一事实来源），
#    避免本模块与 _chart_pipeline 的映射继续分叉（曾三份互不一致）。
from app.services.chart_validators import (
    MERMAID_KEYWORD_TO_CHART_TYPE,
)

# ✅ R102（G2 · 排序键单一出口）：导出序与展示序/生成序共用同一归一键。
from app.services.content_utils import (  # noqa: F401 — 拆分后保留绑定（_export_issue_scan 经 _sh 引用）
    section_sort_order_key,
)

# ✅ 《待补充清单》（2026-09-24，治 F 层人工补录兜底）：逐条扫描章节正文中的
#    占位符（规范字段占位 / 裸标记 / 模糊 ××），按字段与章节聚合，支持前端跳转定位。
from app.services.facts_extractor import FACT_SCOPE_PREDICATE, resolve_scheme_project_id
from app.services.placeholder_inventory import (
    DEFAULT_OCCURRENCE_CAP,
    build_placeholder_report,
    build_report_for_scheme,
    build_rerun_plan,
)
from app.services.placeholder_sections import is_placeholder_section

logger = logging.getLogger("export")
router = APIRouter(prefix="/api/v1/schemes/{scheme_id}/export", tags=["export"])


# ---------------------------------------------------------------------------
# ✅ R13 判空单一出口（2026-10-06）
# ---------------------------------------------------------------------------
# 背景（AGENTS.md §5.5 R13）：全局单连接 + aiosqlite 下 ``db.execute()`` 可能
# 返回 ``None``（连接/事务异常），此时 ``cur.fetchone()`` / ``cur.fetchall()``
# 抛 ``AttributeError``。
# 本模块此前**只有 2 处**做了判空（``cache_status`` 与 ``_prune_export_cache``），
# 其余 20+ 处全部裸调 —— AST 扫仓实测「后续 8 行内无 is-None 守卫」的
# ``db.execute`` 点位共 31 个，其中包含两条**导出主干**：
#   · ``export_docx`` 的缓存查询 → 缓存查询失败 ⇒ **整次导出 500**，
#     用户连"重新生成一次"的机会都没有；
#   · ``export_pdf`` 的缓存查询（:5023）→ 同上；
#   · ``_prepare_export`` 的 schemes / sections / chart_predictions 三次查询
#     → 同样 500。
# 本仓历史上已两次因 R13 漏改点被逐个排查（§4.11.6 / §4.17.3），本次直接
# 收敛为**三个 helper 单一出口**，杜绝"改一处漏一处"。
#
# 语义选择（全 fail-soft，理由同仓库既有口径：诊断/旁路不得阻断交付）：
#   · 读：无行 / 异常 → 空结果集并记 WARNING（调用方按"查不到"继续）；
#     方案不存在仍由 ``_prepare_export`` 显式 404，不靠这里判别。
#   · 写：返回 False 并记 WARNING（**绝不静默**），调用方据此决定是否降级。
class DBReadError(Exception):
    """严格读（``strict=True``）下读取失败的信号。

    绝大多数读点都是**旁路/诊断**性质，读不到就按「无数据」继续是正确的。
    但**破坏性**调用方不同：若它把「读失败」当成「确实无行」，后续判据就会
    从「无缓存行引用」变成真话 → 现存产物被误判为孤儿并被删除。
    这类调用方必须显式声明 ``strict=True``，读失败一律抛本异常 → 保守中止。
    """


async def _db_fetch_all(cur, *, what: str, strict: bool = False) -> list:
    """R13 + 容错读：``fetchall`` 的唯一出口。

    默认（``strict=False``）fail-soft：cur 为 None / 读取异常 → ``[]``。
    ``strict=True`` 时改为抛 :class:`DBReadError`，供破坏性调用方区分
    「查询失败」与「确实无行」—— 这是两条完全不同的语义，
    混在一起等于把磁盘上的成稿判成孤儿（见 ``_gc_orphan_exports``）。
    """
    if cur is None:
        msg = f"db.execute 返回 None（{what}）"
        if strict:
            logger.warning("导出：%s，严格读模式 → 抛 DBReadError", msg)
            raise DBReadError(msg)
        logger.warning("导出：%s，降级为空结果集", msg)
        return []
    try:
        return list(await cur.fetchall())
    except Exception as e:  # pragma: no cover - 连接层异常
        if strict:
            logger.warning("导出：读取 %s 失败（严格读模式 → 抛 DBReadError）: %s", what, e)
            raise DBReadError(str(e)) from e
        logger.warning("导出：读取 %s 失败（降级为空结果集）: %s", what, e)
        return []


async def _db_fetch_one(cur, *, what: str, strict: bool = False):
    """R13 + 容错读：``fetchone`` 的唯一出口。cur 为 None / 读取异常 → None。

    ✅ 2026-10-17：补 ``strict``（与 :func:`_db_fetch_all` 同语义），供
    ``collect_export_issues(strict_read=True)`` 把「查询失败」与「确实无行」
    区分开。缺省 ``False`` 时行为与引入前**逐字一致**。
    """
    if cur is None:
        if strict:
            msg = f"db.execute 返回 None（{what}）"
            logger.warning("导出：%s，严格读模式 → 抛 DBReadError", msg)
            raise DBReadError(msg)
        logger.warning("导出：%s，降级为无行", what)
        return None
    try:
        return await cur.fetchone()
    except Exception as e:  # pragma: no cover - 连接层异常
        if strict:
            logger.warning("导出：读取 %s 失败（严格读模式 → 抛 DBReadError）: %s", what, e)
            raise DBReadError(str(e)) from e
        logger.warning("导出：读取 %s 失败（降级为无行）: %s", what, e)
        return None


def _merge_borrowed_stats(render_stats: dict, fix_stats: dict | None) -> dict:
    """把构建期统计的「跨章借图」并入渲染统计（单一出口，DOCX / PDF 共用）。

    ✅ 加固（2026-10-06 · D1）：跨章借图的真实发生地是 `_build_docx_sync`
    （渲染期才知道借给了谁），而响应头 `X-Chart-Render-Stats` 来自
    `_prepare_export` 的 ``render_stats``。此 helper 是两者之间**唯一的桥**，
    避免"两条导出链路各写一遍合并逻辑"的分叉。
    """
    if not isinstance(render_stats, dict):
        return {}
    n = int((fix_stats or {}).get("chart_fallback_borrowed") or 0)
    render_stats["fallback_borrowed"] = n
    details = (fix_stats or {}).get("chart_fallback_borrowed_details")
    if details:
        render_stats["fallback_borrowed_details"] = details
    return render_stats


async def _db_exec(db, sql: str, params=(), *, what: str) -> bool:
    """R13 + 写路径可观测：``execute`` 的唯一出口。返回 False = 本次写入未生效。

    ✅ R108（2026-10-18 · 日志审计 P0-3）：既有 docstring 自称「写路径唯一出口」，
    但锁瞬态（database is locked）此前**直通上抛**——批量导出撞 WAL 写锁时缓存行
    写丢 + 500 直通。现接 ``retry_db_op`` 单一出口重试（只重试锁/i/o 瞬态，
    语义错误原样上抛）；SQLite 的 locked 在语句获取写锁**前**抛出、首次尝试
    必未落库，INSERT（uuid 主键）/UPDATE/DELETE 重放均无重复副作用。
    """
    cur = await retry_db_op(lambda: db.execute(sql, params), max_retries=4, base_delay=0.1, jitter=0.05)
    if cur is None:
        logger.warning("导出：写操作 %s 未生效（db.execute 返回 None）", what)
        return False
    return True


def _norm_code(code: str) -> str:
    """规范化 mermaid 代码用于前后端匹配（折叠空白差异）"""
    return " ".join(str(code).split())


def _compute_doc_order_rank(roots: list, children_map: dict) -> dict[str, int]:
    """按**文档实际渲染顺序**（同级同键排序 + 深度优先）计算章节 rank。

    ✅ RISK-175-01（R175）：预渲染段 ``_prepare_export_render`` 此前用
    ``enumerate(sections)``（SQL 扁平序 ``ORDER BY sort_order, level, id``），
    而写侧 ``_build_docx_sync._rank_order`` 用 DFS 序。父子章节存在时两种
    口径不同 → 跨章「兜底借图」按错误 rank 选来源，图表被插到错误章节的
    位置（看起来像"图跑到了下一节"）。现两处共用本函数，口径唯一。

    对**全为根章节**的方案（绝大多数），扁平序与 DFS 序相同 → 产物不变；
    仅含父子层级时，借图来源可能从"SQL 序前一章"改为"文档序前一章"，
    即修正为正确的文档顺序。
    """
    rank: dict[str, int] = {}

    def _walk(secs):
        for s in sorted(secs, key=_section_sort_key):
            if s["id"] not in rank:
                rank[s["id"]] = len(rank)
            _walk(children_map.get(s["id"], []))

    _walk(roots)
    return rank


def _build_chart_type_index(
    chart_lookup: dict[tuple[str, str], str],
    order_rank: dict[str, int] | None = None,
) -> dict[str, list[tuple[str, str]]]:
    """构建 chart_type → [(section_id, code)] 倒排索引（P0-3：O(N²) → O(1) 命中）。

    语义与旧实现"遍历 chart_lookup 取第一个同类型非空 code"一致，但**补齐了
    来源章节**并在提供 ``order_rank`` 时按**文档顺序**排序。

    ⚠️ 加固（2026-10-06 · D1）：旧实现的候选顺序 = ``chart_predictions`` 的
    **行插入顺序**（同 scan 逻辑下不可控），于是"借用谁的图"随数据装载顺序
    漂移 —— 同一份方案在两次导出/两台机器上可能借到不同章节的图。现按渲染
    文档顺序（``order_rank``）排序：借**最近的、在自己之前**的那张同类图。
    ``order_rank`` 缺省时保持原插入顺序（向后兼容，旧调用方零影响）。

    注意：这里的 chart_type → code 映射是全局的（不带 section_id），因为
    fallback 的既定契约是"当前章节标记 [CHART_TYPE: X] 但没有独立登记时，
    借用同类型图表代码"——这是 AI 单张覆盖多章正文的合理场景（例如三章
    共用一张 labor 图），而不是缺陷。参见 test_export_fallback_dedup.py。
    跨章借用的**可观测性**（计数 / 响应头 / 前端提示）由 ``_resolve_chart_code``
    统一产出，见其 docstring。
    """
    index: dict[str, list[tuple[str, str]]] = {}
    items = list(chart_lookup.items())
    if order_rank:
        # 稳定排序：rank 相同的（理论上不会出现）保持原插入顺序
        items.sort(key=lambda kv: order_rank.get(kv[0][0], 1 << 30))
    for (sid, ct), code in items:
        if not code:
            continue
        bucket = index.setdefault(ct, [])
        if not any(s == sid and c == code for s, c in bucket):
            bucket.append((sid, code))
    return index


def _find_fallback_code(
    chart_type_index: dict[str, list[tuple[str, str]]],
    chart_type: str,
    current_section_id: str = "",
    order_rank: dict[str, int] | None = None,
) -> tuple[str, str]:
    """图表兜底查找：返回 ``(code, borrowed_from_section_id)``。

    仅用于"章节正文标记了 [CHART_TYPE: X] 但 chart_predictions 里没有登记
    对应代码"的场景——此时同类型的其他章节 code 可以作为兜底。这是
    fallback 的既定契约，与 test_export_fallback_dedup 一致，不是缺陷。

    ⚠️ 曾在 2026-10-05 R-3 尝试加入 current_section_id 过滤禁止跨章节借用，
    但那会打破三章共用一张 labor 图的合理场景，故已回退。跨章节借用是
    fallback 契约的一部分，test_missing_chart_does_not_borrow_other_chapter_code
    保留 strict xfail 记录该语义分歧。

    ⚠️ 加固（2026-10-06 · D1）：**不改变借用语义**（仍会借），但改为**确定
    性地借"文档顺序上最近的前一张同类图"**（旧实现依赖 DB 行插入顺序，不可控），
    并**回传来源章节**让调用方能计数与上报 —— 此前跨章借图是**零信号**的
    （成稿里 B 章可能出现 A 章的流程图，图文不符却无任何提示）。
    """
    bucket = chart_type_index.get(chart_type) or []
    if not bucket:
        return "", ""
    if current_section_id:
        for sid, code in bucket:
            if sid == current_section_id:
                return code, sid
        if order_rank:
            me = order_rank.get(current_section_id)
            if me is not None:
                # 优先取「排在自己之前且距离最近」的同类图；都排在自己之后时
                # 取全局第一张（保持"有图总比没图好"的兜底语义）
                before = [
                    (abs(me - order_rank.get(sid, me)), i, code)
                    for i, (sid, code) in enumerate(bucket)
                    if order_rank.get(sid) is not None and order_rank[sid] < me
                ]
                if before:
                    before.sort()
                    return before[0][2], bucket[before[0][1]][0]
    return bucket[0][1], bucket[0][0]


def _resolve_chart_code(
    block: dict,
    sec_id: str,
    chart_type: str,
    chart_lookup: dict[tuple[str, str], str],
    chart_type_index: dict[str, list[tuple[str, str]]],
    order_rank: dict[str, int] | None = None,
) -> tuple[str, str]:
    """图表取码的**唯一出口**：``(code, borrowed_from_section_id)``。

    优先级：块自带内联码 > 本章节 chart_predictions 登记 > 同类型兜底借用。

    ⚠️ 加固（2026-10-06 · D1）：`_prepare_export`（预渲染收集）与
    `write_section`（落图渲染）此前**各自实现了一遍同样的三级取码逻辑**，
    既重复又没有任何一方能观测到"这张图是借来的"。现收敛为单一出口，
    兜底命中的来源章节由返回值给出，供渲染统计（X-Chart-Render-Stats）
    与导出日志如实上报。
    """
    code = block.get("code") or chart_lookup.get((sec_id, chart_type), "")
    if code:
        return code, ""
    return _find_fallback_code(chart_type_index, chart_type, sec_id, order_rank)


# ✅ 导出器逻辑版本：纳入内容指纹，使渲染/排版逻辑修复后旧缓存放缓。
#    否则"仅代码修复、正文与配置未变"时会命中旧缓存，导出仍是修复前的文档。
#    每次修改导出排版逻辑（标题、编号、缓存、样式等）后 +1。
#    v6：章节自动分页 / 正文行距 / 代码块底纹 / 列表悬挂缩进 / 图题规范化为
#        「图 X-Y 图名」/ 封面与目录后分页 / 页眉页脚首页独立 /
#        settings.xml 声明域自动刷新 / 表格表头跨页重复 / 引用块与分隔线 /
#        PDF 与 DOCX 共用准备逻辑（修复 PDF 丢失 JSON 数据型图表、
#        以及中文方案名导致 PDF 导出 500）。
#    v7：表格列宽按内容自适应（不再等宽挤压长文本）/ 图片按原始比例自适应且窄图不放大 /
#        有序列表编号自动连续（修复 AI 跳号、重复编号）/ 封面支持项目信息表 /
#        页边距可配置。
#    v8：中文有序列表 `1、xxx` / `1）xxx`（AI 最常用写法，顿号/右括号后常无空格）
#        识别为列表项 / 图题字体与字号跟随导出配置 / 导出修复统计改为线程局部
#        （消除并发导出时的统计串号）。
#    v9：有序列表标记样式识别与还原（（一）/（1）/ 1、/ 1）不再被拍平成 "1. "）/
#        有序列表被无序子项打断后序号继续递增（不再从 1 重排）/
#        表格题注「表 {章号}-{序号} 表名」（自动吸收表上方表名行，表题在表格上方）。
#    v10：未生成的 AI 配图（```ai_image 占位块）不再作为代码块印进成稿
#        （旧行为把绘图提示词 JSON 原样输出），改为整块跳过并告警、不占用图号。
#    v11：图题改为「载荷 title > Mermaid title 指令 > 引导语 > 类型通用名」
#        （修复「图 4-1 劳动力配置计划」挂在流程图正文之下）；
#        chart-json 缺/非法 type 时按载荷结构推断（不再静默兜底 labor 导致红字占位）；
#        未闭合图表围栏（生成截断）整块跳过，不再渲染残片或把源码印进成稿；
#        新增导出前内容体检（占位符/HTML 残留/LaTeX 空参数/投标用语/未闭合围栏）。
#    v12：图表渲染失败默认跳过且不占图号（交付文档不再出现红字「渲染失败」段），
#        新增配置 chart_fail_placeholder=true 恢复 V7.0 占位形态；
#        "不静默"承诺转移至 X-Chart-Render-Stats / X-Content-Audit / 预检 / 日志。
#    v13：未闭合围栏的正文还原对**所有**围栏语言生效（修复未闭合 ai_image 围栏
#        静默吞掉其后正文、未闭合 python/json 围栏把正文当代码块印进成稿）；
#        单代码块行数上限与登记侧统一为 MAX_INLINE_CODE_BLOCK_LINES（修复 500 行
#        vs 8000 字符口径分叉导致的幽灵图）；导出前体检改为统计全部未闭合围栏。
#    v14：剪贴板表格残留（Excel/Word 粘贴带入的 <fcel>/<lcel>/<ucel>/<xc>/<nl>
#        占位标记与 <table>/<tr>/<td>/<br> 等 HTML 表格标签）导出时降级为可读纯
#        文本（修复交付文档出现「<br><br><table>&lt;fcel&gt;&lt;nl&gt;</table>」式乱码），
#        并新增 clipboard_table 体检项供用户回改源文。
#    v15：新增可选「内容自动改写」开关 auto_rewrite_content（默认关闭，向后兼容）：
#        在代码围栏外把 HTML 表格转原生 Word 表格、修正 AI 英文串写、合并相邻
#        完全重复块。仅做无损/低风险的格式与词汇修复，绝不臆造【待补充】/空章节
#        /截断句等数据（安全敏感专项方案的底线）。
#    v16：Mermaid 块图题优先级对齐图表需求规格（Mermaid title 指令 > 引导语 >
#        类型通用名；chart-json 块仍为 载荷 title > 引导语），修复 v11 规格注释
#        与 2026-09-19 实现反转的规格-实现漂移。
#    v17：AI 配图（```ai_image 占位块）导出时自动生成真实图片并嵌入（新增开关
#        ai_image_auto_generate，默认开启；失败占位块仍整块跳过不占图号），
#        移除对前端「生成配图」人工按钮的依赖（图表全自动生成，无人工生图入口）。
#    v18：图表围栏读取（_parse_content_blocks）改用与登记侧/改写侧同源的共用扫描器
#        read_fenced_block，补齐"超长但闭合"（500<行数≤1000、首尾围栏齐全）的有界前视恢复——
#        修复此类合法图表块被登记入库、却在导出 DOCX 里凭空消失的"幽灵图"（三侧口径分叉）。
#    v19：编号统一（2026-09-25）：① 附录组标题改用自定义样式（大纲级别 9），
#        不再进入 TOC 域（修复目录页「有目录项、无编号」孤条目）；② heading 块
#        新增 src_line 增量字段（配合正文落库前子标题编号规范化，预览=落库=导出）。
#        版式变化烤进导出缓存，必须整体失效。
#    v20：图表版式修复（2026-09-25）：① 图片插入尺寸改为「PNG DPI 换算真实物理
#        尺寸 + 栏宽 16cm / 高度 22cm 双上限等比缩放」（修复纵向长图被撑到
#        16×93cm 溢出页面、AI 竖版配图同样溢出）；② 图表被跳过（渲染失败 / 去重 /
#        无代码 / AI 配图未生成）时同步移除其孤儿引导语（修复"见下图"却无图）。
#        版式变化烤进导出缓存，必须整体失效。
#    v21：未闭合围栏三侧口径收口（2026-10-03 · 未闭合=不是图）：chart-json / ai_image
#        解析分支补齐闭合检查（旧实现只看 JSON 可解析+类型白名单，EOF 截断残片
#        照样渲染占号）；登记侧 _scan_chart_fences_full 同步跳 eof（消除幽灵登记/
#        绕过配图上限）；解析期跳过的图表围栏同步回收孤儿引导语（与 v20 渲染期
#        回收同口径）。版式变化烤进导出缓存，必须整体失效。
#    v22：高风险回归修复（2026-10-05 · 两项 strict xfail 落地）：
#        ① R-1 前端 PNG 像素指纹（fe_render_pixels）—— 同 code 不同 PNG 字节
#           不再命中旧 DOCX 缓存；② R-2 ``prep["image_config"]`` 覆盖
#           ``_image_generation_signature()`` —— 前端 per-request 图像模型/
#           尺寸变化能触发缓存失效。
#        修改影响 content_fingerprint 输出，旧缓存（v21）必须整体失效。
#        ⚠️ R-3（章节隔离借用）曾尝试修复但已回退：test_export_fallback_dedup
#           与 test_missing_chart_does_not_borrow_other_chapter_code 存在语义
#           分歧（fallback 契约 vs 章节隔离契约互斥），R-3 保持 strict xfail
#           记录该分歧，业务侧统一契约后再落地。
#    v23：R109（2026-10-19 · P0）WPS 打不开修复 —— ``w:tcPr`` 等 CT_* 严格序列
#        归一（见 ``_normalize_docx_strict_sequence``）。**必须整体失效旧缓存**：
#        修复前落盘的成稿普遍带有 tcW→vAlign→shd 的非法序列，若沿用旧缓存行，
#        用户内容与配置未变 → 每次命中都拿回打不开的坏档，修复永不可见。
#        版式/产物字节变化烤进导出缓存，故沿用既有版本号机制整体失效。
_EXPORTER_VERSION = "23"


_CHART_TYPE_MAP = {
    "gantt": ("gantt_json", "施工进度计划"),
    "flowchart": ("flowchart_json", "施工流程图"),
    "architecture": ("architecture_json", "组织架构图"),
    "labor": ("labor_json", "劳动力配置计划"),
    "comparison": ("comparison_json", "对比图"),
    "layout": ("layout_json", "总平面布置图"),
    "timeline": ("timeline_json", "关键里程碑时间线"),
}


# ---------------------------------------------------------------------------
# ✅ 导出前内容体检（2026-09-19 新增）：把"生成的文档到底能不能直接交付"这一判断
#    从"人工翻页发现"前移为"机器可枚举清单"。
#    设计原则：**只体检、不篡改**——占位符/投标用语/HTML 标签一律不改写正文
#    （改写等于替用户决定内容），只给出计数与定位，供前端提示与用户回改。
# ---------------------------------------------------------------------------
from app.routers._export_issue_scan import (  # noqa: F401,E402,I001 — 拆分兼容转发（原名原对象，monkeypatch 语义不变）
    _AUDIT_RULES,
    _CHART_FENCE_LANGS,
    _STALE_FIG_REF_RE,
    _STALE_SEC_REF_RE,
    _STALE_TBL_REF_RE,
    _count_child_namespace_subheadings,
    _count_unclosed_chart_fences,
    _detect_body_subheading_namespace_conflict,
    _detect_duplicate_sections,
    _detect_section_number_mismatch,
    _detect_stale_cross_references,
    _format_audit_log,
    _section_sort_key,
    _stored_outline_number,
    audit_content,
    audit_rules_fingerprint,
    collect_figure_table_number_superset,
)


@router.post("/check")
async def export_check(scheme_id: str, db=Depends(get_db)):
    """导出预检：空章节、孤立节点、图表未生成、字数不足等。

    ✅ 增强：旧实现仅检查空章节和 status=empty，现增加：
    - 孤立节点（parent_id 指向不存在的父节点）
    - 图表未生成（chart_predictions 中 status != done）
    - 字数统计（总字数、已生成章节数、空章节占比）
    - 低字数章节（叶子节点 word_count < 100，可能生成不完整）

    ✅ G1（2026-09-21）：附加 ``preflight_summary`` —— 就绪度总检（一键总检）的
    最新结论及其是否已过期。此前导出预检与就绪度总检是**两套互不共享的体系**：
    本接口算出的问题既不进 preflight_runs 也不进 readiness_overview 的 findings，
    用户在总检页看到 B 级，去导出页却被一堆问题拦住，两份报告对不上，用户不知道该信哪个。
    现在两处共用同一份判定逻辑与同一套规则词表（见 ``export_issues_to_findings``），
    导出页能直接看到总检结论，总检也能算进导出预检的问题。
    """
    # ✅ B1-1（R176 · 2026-11-04）：预检不得再用「空结果集」冒充「0 问题」。
    #    旧实现调 ``collect_export_issues()`` 未传 ``strict_read``，DB 读失败被
    #    ``_db_fetch_all(strict=False)`` 降级为空集 → 页面渲染「预检通过」。
    #    现改 fail-fast：读失败 → 503 + 机读字段 ``read_degraded``，使「确实无问题」
    #    （200 且 ``read_degraded=false``）与「没读到数据」（503 且 ``read_degraded=true``）
    #    在响应体层面可区分；``Retry-After`` 给出可行动的重试建议。
    try:
        result = await collect_export_issues(scheme_id, db, strict_read=True)
    except DBReadError as e:
        logger.warning("导出预检读取失败（fail-fast，不返回假通过）: scheme=%s err=%s", scheme_id[:8], e)
        return JSONResponse(
            status_code=503,
            content={
                "detail": ("导出预检未能读取方案数据（数据库连接异常），" "本次结果不代表「无问题」，请稍后重试"),
                "error": "export_check_read_failed",
                "read_degraded": True,
                "retry_after": 5,
            },
            headers={"Retry-After": "5"},
        )
    # ✅ B1-1：成功路径显式回写「数据完整」——前端据此区分两种语义，
    #    缺省 False 对既有消费方零影响（加法字段）。
    result["read_degraded"] = False
    result["preflight_summary"] = await _readiness_preflight_summary(scheme_id, db)
    # ✅ 新增（2026-09-24）：占位符基线落库（监控旁路，失败不阻断预检返回）。
    #    每次预检落一行快照，GET /export/placeholder-history 供趋势对比。
    await _record_placeholder_baseline(scheme_id, result.get("placeholder_report") or {}, db)
    return result


@router.get("/placeholder-report")
async def placeholder_report(scheme_id: str, db=Depends(get_db)):
    """《待补充清单》：逐条扫描各章正文中的占位符并聚合（2026-09-24 新增）。

    与 POST /export/check 的 ``placeholder_report`` 键同源同口径，区别在于：
    - 本端点额外返回 ``occurrences``（逐条出现记录，含章节 ID / 字段名 /
      上下文片段，截断至 ``occurrence_cap``），供前端「待补充清单」面板
      按字段 / 按章节展示与一键跳转定位；
    - /check 只带聚合部分，控制响应体。

    三类占位口径见 ``services/placeholder_inventory.py`` 模块注释。
    只扫描、不篡改正文 —— 补录由用户完成，属「人工补录兜底」层。
    """
    return await build_report_for_scheme(scheme_id, db, occurrence_cap=DEFAULT_OCCURRENCE_CAP)


@router.get("/placeholder-rerun-plan")
async def placeholder_rerun_plan(scheme_id: str, db=Depends(get_db)):
    """重跑计划（2026-09-24，治 F 层第 3 条：补录后只重跑受影响章节）。

    与 /placeholder-report 同口径扫描，并对照**当前**可注入语料
    （全局事实 is_resolved=1 且 has_conflict=0 + 解析提取 success 成果）
    逐字段判定「可补齐」（字段名子串命中语料 → 说明数据源里已有该参数）。
    章节「可重跑」= 叶子章节 且 至少一个占位字段可补齐——前端据此提供
    「重跑受影响章节」（mode=section + force_rewrite 逐章重跑，不整篇重生成）。

    查询失败一律降级为空计划，绝不阻断。
    """
    return await build_rerun_plan(scheme_id, db)


# 《待补充清单》基线保留行数（按 scheme 裁剪，防长周期膨胀）
_PLACEHOLDER_BASELINE_KEEP = 50


async def _record_placeholder_baseline(scheme_id: str, report: dict, db) -> None:
    """把本次预检的占位符统计落基线表（监控旁路：失败只记日志不阻断）。

    与 preflight_runs 的写入策略同构：INSERT 失败 / 事务残留时尝试 rollback，
    绝不让监控写入影响预检主流程。

    ✅ R99（2026-10-17 · R13 写路径收口）：此前本函数的两条写入是**全模块唯一**
    绕过 ``_db_exec`` 单一出口的写路径 —— 裸 ``await db.execute(...)`` 后**丢弃游标、
    既不判空也不看 rowcount**，随后照常 ``commit``。全局单连接 + aiosqlite 下
    ``execute()`` 可能返回 None（R13），此时写操作**静默丢失**（无异常、无日志、
    连 rollback 都不会触发），而监控旁路恰恰最需要「至少留痕」。
    现两条写入统一走 ``_db_exec``（内部判空 + WARNING + 返回生效布尔），
    写未生效即跳过裁剪并降级为一条 WARNING；行为对正常路径**逐字不变**。
    """
    if not isinstance(report, dict):
        return
    try:
        inserted = await _db_exec(
            db,
            "INSERT INTO placeholder_baselines (id, scheme_id, total,"
            " formatted_total, bare_total, fuzzy_total, field_count, section_count)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (
                str(uuid.uuid4()),
                scheme_id,
                int(report.get("total") or 0),
                int(report.get("formatted_total") or 0),
                int(report.get("bare_total") or 0),
                int(report.get("fuzzy_total") or 0),
                int(report.get("field_count") or 0),
                int(report.get("section_count") or 0),
            ),
            what="placeholder_baselines 写入",
        )
        # 写入端裁剪：只保留最近 N 行（created_at 秒级精度不足，按 rowid 兜底）
        # ⚠️ 仅在 INSERT 生效后才裁剪：否则「写失败 + 裁剪成功」会把历史基线删掉
        # 而本次快照根本没进去，历史趋势被单次失败污染。
        if inserted:
            await _db_exec(
                db,
                "DELETE FROM placeholder_baselines WHERE scheme_id=? AND id NOT IN ("
                " SELECT id FROM placeholder_baselines WHERE scheme_id=?"
                " ORDER BY created_at DESC, rowid DESC LIMIT ?)",
                (scheme_id, scheme_id, _PLACEHOLDER_BASELINE_KEEP),
                what="placeholder_baselines 裁剪",
            )
        await db.commit()
    except Exception as e:
        logger.warning("placeholder_baselines 写入失败（不影响预检返回）: scheme=%s err=%s", scheme_id, e)
        try:
            await db.rollback()
        except Exception as _re:
            logger.warning("placeholder_baselines rollback 也失败: %s", _re)


@router.get("/placeholder-history")
async def placeholder_history(scheme_id: str, limit: int = 20, db=Depends(get_db)):
    """《待补充清单》历史基线（分数趋势，六层方案第 6 层：监控与回归）。

    返回按时间倒序的占位符统计快照（每次导出预检落一行），
    前端据此展示「较上次预检 total 变化」，验证逐次优化是否真实下降。
    """
    limit = max(1, min(int(limit or 20), 100))
    cur = await db.execute(
        "SELECT total, formatted_total, bare_total, fuzzy_total,"
        " field_count, section_count, created_at FROM placeholder_baselines"
        " WHERE scheme_id=? ORDER BY created_at DESC, rowid DESC LIMIT ?",
        (scheme_id, limit),
    )
    # ✅ R13 守卫（2026-09-30 引入，2026-10-06 收敛到 _db_fetch_all 单一出口）：
    #    全局单连接下 db.execute 可能返回 None（连接/事务异常）。本端点是只读
    #    监控旁路，降级为空历史即可，不应 500。
    rows = [dict(r) for r in await _db_fetch_all(cur, what="placeholder_history")]
    return {"scheme_id": scheme_id, "history": rows, "keep": _PLACEHOLDER_BASELINE_KEEP}


async def collect_export_issues(
    scheme_id: str, db, *, strict_read: bool = False, include_content_audit: bool = True
) -> dict:
    """导出预检的核心计算（**不写库**，可被多处复用）。

    ✅ G1：此前本计算内联在 ``export_check`` 路由里，就绪度总检无法复用，
    只能各自实现一份判定 → 口径分叉。现抽成独立函数：
      · 导出页 → 展示问题清单 + ``preflight_summary``；
      · readiness_overview → 经 ``export_issues_to_findings`` 并入六维评分。

    Args:
        strict_read: **加法式**开关，缺省 ``False`` 时全部查询沿用 fail-soft
            （读失败降级为空结果集）。
            总检侧传 ``True``：把「查询失败」与「确实无行」区分开，抛
            ``DBReadError`` 让 ``compliance._readiness_overview_compute``
            的 ``except`` 真正生效。
            ✅ B1-1（R176 · 2026-11-04）：导出页 ``export_check`` 也已改为传
            ``True`` 并捕获 ``DBReadError`` 返回 503（``read_degraded=true``）——
            旧行为「导出页逐字不变」会把读失败渲染成「预检通过」，属假绿。
        include_content_audit: **加法式**开关，缺省 ``True``（既有调用方逐字不变）。
            导出闸门（``_export_high_issue_gate``）传 ``False``：闸门只关心 high 级
            阻断项，内容体检项固定 info 且不在规则表，跳过可避免每次导出都多跑一遍
            全文正则扫描（导出路径自身还会再算一次）。

    ✅ B-17（R182 · 2026-11-06）：返回体新增 ``crossref_check``（``undetermined``
    + ``reason``）。方案无任何图表时图/表分支无从比对 → 必须如实标注「无法判定」，
    不得让前端把「跳过」显示成「通过」。不产生 finding、不扣分。

    ✅ BUG 修复（2026-10-17 · 正文/审核联合审计）：四路查询此前一律
    ``_db_fetch_all(strict=False)`` —— DB 瞬时故障时**静默返回空结果集**，
    于是总检侧 ``export_check`` 这一路 findings 整体消失、
    ``failed_sources`` **也不会**出现该来源（异常根本没抛出来），
    用户看到「六路来源正常」而分数**虚高**。总检消费点
    (``compliance.py`` export_check 段) 的注释本就写着「读取失败必须让用户
    可见（否则该来源静默不参与评分、分数虚高）」，但那条守卫对 DB 读失败
    这一形态是**结构性不可达**的 —— 由本参数让它真正可达。
    """
    # ✅ P1-7 性能优化：export_check 仅需校验字段，裁剪 content 外的大字段列
    # ✅ BUG 修复（2026-09-20，第 8 轮交付文档取证）：旧列清单**缺 level**（也缺
    # sort_order）。_detect_duplicate_sections 依赖 level 判定「章作用域」，缺列时
    # 每个节点都被当成 level=1 → 每节自成一章、作用域互不相同 → 跨节重复编号
    # 全部漏报（实测脚手架专项方案：成稿 81 处重复编号，预检只报 0~4 处）。
    # 这是「预检说没问题、导出却满页撞号」的直接原因，必须与渲染端读同一批列。
    # ✅ 编号统一（2026-09-25）：补读 outline_json —— 编号一致性预检
    #    （_detect_section_number_mismatch）需对照存储编号与结构重算编号。
    cur = await db.execute(
        "SELECT id, parent_id, level, sort_order, title, status, review_status, "
        "word_count, content, outline_json FROM sections"
        " WHERE scheme_id=? ORDER BY sort_order, level, id",
        (scheme_id,),
    )
    sections = [dict(r) for r in await _db_fetch_all(cur, what="collect_export_issues:sections", strict=strict_read)]
    section_ids = {s["id"] for s in sections}
    parent_ids = {s["parent_id"] for s in sections if s.get("parent_id")}
    issues = []
    total_words = 0
    # ✅ R98（R90 遗留⑤收口 · DLV 口径分叉根治）：generated_count / empty_ratio
    #    不再本地自算（旧口径 = 自身 content truthy + **全章节分母**，与总检看板
    #    preflight_stats 的「有效正文（含子孙）+ 叶子分母」分叉，同一方案两处
    #    显示两个数字，且导出侧正是 2026-10-02 生产实证过的父节点恒空假象形态）。
    #    现与 preflight 共用 services.preflight_engine.content_coverage_stats
    #    唯一实现（懒导入规避模块级依赖，与本函数既有 services 用法同手法）。
    from app.services.preflight_engine import content_coverage_stats

    _cov = content_coverage_stats(sections)
    for s in sections:
        has_children = s["id"] in parent_ids
        wc = s.get("word_count") or 0
        total_words += wc
        # ✅ 2026-11-05（目录占位章节）：status=placeholder 的章节按规则只登记
        #    「另行补充内容」，word_count 已置 0；不属于漏生成/低字数/审核缺失，
        #    也不参与导出预检告警（否则会把规则性跳过误报成内容缺失）。
        if is_placeholder_section(s):
            continue
        # 孤立节点：parent_id 非空但指向不存在的父节点
        pid = s.get("parent_id", "")
        if pid and pid not in section_ids:
            issues.append({"type": "orphan_node", "section_id": s["id"], "title": s["title"], "parent_id": pid})
        # 空叶子章节（无内容且无子女；✅ R98 判据补 strip，与共用口径对齐
        #    「content 只有空白也算空」）
        if not (s.get("content") or "").strip() and not has_children:
            issues.append({"type": "empty_section", "section_id": s["id"], "title": s["title"]})
        # 状态为 empty 但实际有内容（数据不一致）
        if s.get("status") == "empty" and (s.get("content") or wc > 0):
            issues.append(
                {
                    "type": "status_inconsistent",
                    "section_id": s["id"],
                    "title": s["title"],
                    "detail": "status=empty 但有内容",
                }
            )
        # 低字数叶子章节（可能生成不完整）
        if not has_children and 0 < wc < 100:
            issues.append({"type": "low_word_count", "section_id": s["id"], "title": s["title"], "word_count": wc})
    # 图表未生成检查
    cur = await db.execute("SELECT chart_type, status FROM chart_predictions WHERE scheme_id=?", (scheme_id,))
    chart_rows = await _db_fetch_all(cur, what="collect_export_issues:chart_predictions", strict=strict_read)
    chart_total = len(chart_rows)
    # ✅ 状态口径兼容："generated" 是正文同步图表与 fix-mermaid 的历史写法，
    #    语义上同样是"代码已生成完毕"，必须与 "done" 一并视为已完成，
    #    否则正文里已内嵌的图表会被预检误报为「图表未生成」。
    _DONE_STATUSES = ("done", "generated")
    chart_done = sum(1 for r in chart_rows if r["status"] in _DONE_STATUSES)
    chart_failed = [dict(r) for r in chart_rows if r["status"] not in _DONE_STATUSES and r["status"] != "pending"]
    # ✅ P0 收尾（对齐 OpenBidKit 图表未生成告警）：正文同步图表登记即 done，
    #    仅 ai_image 在生成前为 pending（占位）。pending 不计入已完成，也不计入
    #    failed（非生成失败），但导出前应提醒用户「有配图尚未生成」，避免导出后
    #    正文缺少图片。这里统一把 pending 记为 chart_ungenerated 告警。
    chart_pending = [dict(r) for r in chart_rows if r["status"] == "pending"]
    for cf in chart_failed:
        issues.append({"type": "chart_failed", "chart_type": cf["chart_type"], "status": cf["status"]})
    for cp in chart_pending:
        issues.append(
            {
                "type": "chart_ungenerated",
                "chart_type": cp["chart_type"],
                "status": "pending",
                "detail": ("该图表（AI 配图）正文仅占位；导出时将自动生成真实图片" "（失败则该图跳过、不占图号）"),
            }
        )

    # ✅ P0-1：章节审核状态检查（generate_content 后自动置 pending）
    #    旧实现导出前无法感知"正文写好但尚未过审"，导致未审核章节混进交付文档。
    #    这里把 review_status!=approved 的已生成章节统计出来作为 warning。
    review_pending = 0
    review_rejected = 0
    review_missing = 0
    for s in sections:
        # 未生成正文 / 按规则登记为「另行补充内容」的章节不参与审核检查
        if not s.get("content") or is_placeholder_section(s):
            continue
        rs = s.get("review_status") or ""
        if rs == "approved":
            continue
        if rs == "rejected":
            review_rejected += 1
        elif rs == "pending" or rs == "reviewing":
            review_pending += 1
        else:
            review_missing += 1
    review_unapproved = review_pending + review_rejected + review_missing
    if review_pending:
        issues.append(
            {
                "type": "review_pending",
                "count": review_pending,
                "detail": f"{review_pending} 章正文已生成但审核状态为 pending",
            }
        )
    if review_rejected:
        issues.append(
            {"type": "review_rejected", "count": review_rejected, "detail": f"{review_rejected} 章正文审核未通过"}
        )
    if review_missing:
        issues.append(
            {"type": "review_missing", "count": review_missing, "detail": f"{review_missing} 章正文已生成但无审核记录"}
        )

    # ✅ P0-2：bid_analysis_items 必选项完整性检查
    #    目录生成 / 正文生成都消费 bid_analysis 结构化提取结果作为 project_brief，
    #    如果 17 个必选项中有大量失败，导出的文档可能信息不全。
    # ✅ 收敛：委托 facts_extractor.resolve_scheme_project_id（服务层单一出口），
    #    不再内联 SQL（R54/R68 跨模块 scheme→project 反查收口）。
    ba_pid = await resolve_scheme_project_id(db, scheme_id)
    _ba_summary = None
    if ba_pid:
        cur2 = await db.execute(
            "SELECT item_id, label, status, required FROM bid_analysis_items "
            "WHERE project_id=? AND COALESCE(required,0)=1",
            (ba_pid,),
        )
        ba_required = [
            dict(r)
            for r in await _db_fetch_all(cur2, what="collect_export_issues:bid_analysis_items", strict=strict_read)
        ]
        ba_total = len(ba_required)
        ba_success = sum(1 for r in ba_required if r["status"] == "success")
        ba_failed = [r for r in ba_required if r["status"] not in ("success", "idle")]
        if ba_total > 0 and ba_success < ba_total:
            _ba_summary = {
                "required_total": ba_total,
                "success": ba_success,
                "failed_count": len(ba_failed),
                "failed_items": [
                    {"item_id": r["item_id"], "label": r["label"], "status": r["status"]} for r in ba_failed[:10]
                ],
            }
            issues.append(
                {
                    "type": "bid_analysis_incomplete",
                    "required_total": ba_total,
                    "success": ba_success,
                    "failed_count": len(ba_failed),
                    "detail": f"必选项 {ba_total - ba_success}/{ba_total} 未成功提取",
                }
            )

    # ✅ 全局事实门控：统计当前方案可见事实（方案私有 + 项目共享）。
    # 未确认/模拟/冲突/来源过期项均已被正文与导出查询排除，交付前必须显式提示。
    # ✅ P1 修复（2026-09-27 · 口径分叉）：is_resolved 的 COALESCE 默认值由 1 改为 0。
    #    旧口径把 is_resolved IS NULL 当作「已确认」（COALESCE(NULL,1)=1 → 不计 blocked），
    #    但注入侧 facts_extractor.FACTS_INJECT_WHERE_FALLBACK 用的是严格相等 `is_resolved=1`，
    #    NULL=1 为 false → 该事实**不会出现在产物里**。两者结合 = 预检放行、产物缺该事实，
    #    用户在预检报告里无法发现问题。改为 fail-closed（NULL 计为未确认）后，
    #    门控与注入同口径：注入端排除的事实，预检一定告诫用户。
    #    影响面：仅 is_resolved IS NULL 的行从「不阻断」变为「阻断并提示」，
    #    属于修正漏报（以前漏报更危害），不涉及任何正常事实。
    _facts_summary = {"total": 0, "unresolved": 0, "simulated": 0, "conflicted": 0, "stale": 0, "blocked": 0}
    _facts_status = {"ok": True, "code": "not_checked"}
    try:
        # ✅ BUG 修复（2026-10-11 · 跨模块数据链 R70 · 全局事实交付门控 100% 空转）：
        #    此处原引用 ``scheme_row`` —— 该名字在本文件/本函数内**从未赋值**
        #    （ruff F821 已报 ``export.py:1148/1159 Undefined name 'scheme_row'``），
        #    运行期必然抛 ``NameError``，随即被本 try 的 except 吞成一条 WARNING，
        #    ``_facts_summary`` 保持全 0、`_facts_status`` 保持 ``not_checked``。
        #    后果（实测 tests/test_global_facts_safety_scope.py 复现）：
        #      · 导出预检**永远**报「全局事实 0 项 / blocked 0」→ 未确认、模拟值、
        #        冲突、过期四类事实**一项都拦不住**，带着编造数值的稿子照样导出；
        #      · readiness_overview 经 export_issues_to_findings 并入六维评分时
        #        同样缺这一路，总分虚高；
        #      · 界面上 ``not_checked`` 与「检查通过、0 项待处理」 indistinguishable。
        #    修法：项目 ID 本函数已在 :1108 用服务层单一出口
        #    ``resolve_scheme_project_id`` 取到并存为 ``ba_pid``，直接复用，
        #    不再引入第二份反查（避免 R54/R68 反复记录的「反查多副本」）。
        if ba_pid:
            cur = await db.execute(
                "SELECT COUNT(*) AS total, "
                "SUM(CASE WHEN COALESCE(is_resolved,0)=0 THEN 1 ELSE 0 END) AS unresolved, "
                "SUM(CASE WHEN COALESCE(is_simulated,0)=1 THEN 1 ELSE 0 END) AS simulated, "
                "SUM(CASE WHEN COALESCE(has_conflict,0)=1 THEN 1 ELSE 0 END) AS conflicted, "
                "SUM(CASE WHEN COALESCE(is_stale,0)=1 THEN 1 ELSE 0 END) AS stale, "
                "SUM(CASE WHEN COALESCE(is_resolved,0)=0 OR COALESCE(is_simulated,0)=1 "
                "OR COALESCE(has_conflict,0)=1 OR COALESCE(is_stale,0)=1 THEN 1 ELSE 0 END) "
                "AS blocked "
                "FROM global_facts WHERE " + FACT_SCOPE_PREDICATE,
                (scheme_id, ba_pid),
            )
            frow = await _db_fetch_one(cur, what="collect_export_issues:global_facts 统计", strict=strict_read)
            if frow:
                _facts_summary = {k: int(frow[k] or 0) for k in _facts_summary}
                _facts_status = {"ok": True, "code": "ok"}
        blocked_fact_count = int((_facts_summary.get("blocked") or 0))
        if blocked_fact_count > 0:
            issues.append(
                {
                    "type": "global_facts_blocked",
                    "count": blocked_fact_count,
                    "detail": (
                        f"全局事实待处理 {blocked_fact_count} 项："
                        f"未确认 {_facts_summary['unresolved']}、"
                        f"模拟 {_facts_summary['simulated']}、"
                        f"冲突 {_facts_summary['conflicted']}、"
                        f"来源过期 {_facts_summary['stale']}"
                    ),
                }
            )
    except Exception:
        _facts_status = {"ok": False, "code": "query_failed"}
        # ✅ B1-1（R176）：严格读模式下不得把「读失败」吞成 not_checked/0 项 ——
        #    丢给调用方（/check → 503；总检 → failed_sources），否则该路来源静默缺席。
        if strict_read:
            raise
        logger.exception("全局事实交付门控检查失败（不阻断其它预检）")

    # ✅ 同一级重复编号 / 重复标题检测（长方案分步生成易重复挂接子节点，
    #    导致导出文档出现「1.1 … 1.1 …」式重复章节，破坏目录与编号同步性）。
    #    仅追加 issue，不改变既有校验语义；前端按类型展示并提示用户先清理重复节点。
    issues.extend(_detect_duplicate_sections(sections))

    # ✅ 编号统一（2026-09-25）：目录存储编号 vs 结构重算编号 一致性预检
    #    （needs outline_json 列 —— 缺列时 stored 为空全部跳过，不误报）
    issues.extend(_detect_section_number_mismatch(sections))

    # ✅ E6 Tier 1（2026-09-25 · DLV-14）：失效交叉引用预检（图号/表号/节号漂移）。
    #    默认开启（settings.crossref_stale_detect_enabled=True），关闭即回退旧行为。
    # ✅ R99 Tier 2（2026-10-17）：图号 / 表号漂移**检测接线**。
    #    此前本行调 `_detect_stale_cross_references(sections)` 从不传 real_nums，
    #    而该函数把 None 归一为 `{"fig": set(), "tbl": set(), "sec": set()}` →
    #    图号 / 表号两个分支**结构性不可达**，DLV-14 实际只能检出节号漂移
    #    （实测：正文引用「图9-9/表9-9」，真实只有「图 1-1」，产出 0 条告警）。
    #    现由 `collect_figure_table_number_superset` 供给「最宽松上界」集合：
    #    渲染侧所有减法路径（同码去重 / 渲染失败 / 位图缺失 / 块级回滚）只会让
    #    真实分配更小，故本集合是真实分配的**超集** —— 检测为纯增量、零新增误报。
    #    节号集合仍留空，由被调函数内部照旧从 outline_json 自行推导。
    #    目录树异常时该函数返回空集合 = 逐字回退修复前行为。
    # ✅ B-17（R182 · 2026-11-06）：方案没有任何图表时，real_figs/real_tbls 是空集，
    #    交叉引用检查会「跳过」→ 引用了「图 9-9」也报 0 问题并被当成「通过」。
    #    现新增「无法判定」态（undetermined + reason=no_chart_set）：不产生 finding、
    #    不扣分，但必须回传前端明确展示，禁止伪装成通过。
    crossref_check: dict = {"undetermined": False, "reason": "", "fig_count": 0, "tbl_count": 0}
    try:
        from app.config import settings as _s

        if _s.crossref_stale_detect_enabled:
            _real_nums = collect_figure_table_number_superset(sections)
            _fig_n = len(_real_nums.get("fig") or ())
            _tbl_n = len(_real_nums.get("tbl") or ())
            crossref_check.update({"fig_count": _fig_n, "tbl_count": _tbl_n})
            if _fig_n == 0 and _tbl_n == 0:
                crossref_check.update({"undetermined": True, "reason": "no_chart_set"})
            issues.extend(_detect_stale_cross_references(sections, real_nums=_real_nums))
    except Exception:
        # 配置缺失 / 函数签名变更等异常 → 静默降级（不阻断预检主流程）
        logger.debug("失效交叉引用预检降级跳过（配置读取异常，不阻断预检）", exc_info=True)

    # ✅ E3（2026-09-25 · DLV-13 high）：正文子标题与 DB 子章节命名空间冲突。
    #    配置开启降级（默认）、但正文仍残留 AI 写的 X.X 点分子标题 → 规范化可能被
    #    跳过，导出成稿真撞号。本预检阻断导出（high 级），要求先修复再交付。
    try:
        issues.extend(_detect_body_subheading_namespace_conflict(sections))
    except Exception:
        logger.debug("正文子标题命名空间冲突预检失败（跳过，不阻断预检）", exc_info=True)

    empty_ratio = _cov["empty_ratio"]  # ✅ R98：与总检共用口径（见上方 _cov）
    # ✅ 新增（2026-09-19）：内容体检（占位符 / HTML 残留 / LaTeX 空参数 /
    #    投标用语 / 未闭合图表围栏）—— 仍是独立字段 ``content_audit``（供导出前
    #    弹窗与"待补充清单"定位）；✅ B-16（R182）起**同时**并入 issues 供前端可见。
    if include_content_audit:
        content_audit = audit_content(sections)
        # ✅ B-16（R182 · 2026-11-06）：内容体检结果**并入 issues**（可见性），
        #    但评分与阻断口径保持不变 —— 这些 type 不在 _EXPORT_ISSUE_RULE_MAP：
        #    export_issues_to_findings 与 high 级闸门都会跳过；severity 固定 info
        #    以免改变前端 deriveExportGate 的 highIssueCount 统计。
        for _item in content_audit.get("items") or []:
            _cnt = int(_item.get("count") or 0)
            _secs = [str(t) for t in (_item.get("sections") or []) if t]
            issues.append(
                {
                    "type": str(_item.get("key") or ""),
                    "source": "content_audit",
                    "severity": "info",
                    "audit_level": str(_item.get("level") or ""),
                    "title": str(_item.get("label") or ""),
                    "count": _cnt,
                    "sections": _secs,
                    "detail": (f"体检命中 {_cnt} 处" + (f"；涉及章节：{'、'.join(_secs[:3])}" if _secs else "")),
                }
            )
    else:
        # 导出闸门路径：不跑全文体检（见 docstring），返回与体检空态同形状。
        content_audit = {"total": 0, "items": []}
    # ✅ 新增（2026-09-24）：《待补充清单》精简版（additive 键，不改变既有字段）。
    #    只有聚合计数与按字段/章节聚合，不带逐条 occurrences（响应体控制），
    #    逐条清单走 GET /export/placeholder-report 专用端点。
    try:
        _ph_full = build_placeholder_report(sections)
        placeholder_report = {
            k: _ph_full[k]
            for k in (
                "total",
                "formatted_total",
                "bare_total",
                "fuzzy_total",
                "bracket_total",
                "field_count",
                "section_count",
                "by_field",
                "by_section",
            )
        }
    except Exception:  # 清单属旁路审计：失败不阻断预检主流程
        placeholder_report = build_placeholder_report([])
    return {
        "issues": issues,
        "section_count": len(sections),
        "generated_count": _cov["generated_count"],
        "total_words": total_words,
        "empty_ratio": empty_ratio,
        "chart_total": chart_total,
        "chart_done": chart_done,
        "chart_pending": len(chart_pending),
        "content_audit": content_audit,
        "crossref_check": crossref_check,
        "placeholder_report": placeholder_report,
        "review_summary": {
            "pending": review_pending,
            "rejected": review_rejected,
            "missing": review_missing,
            "unapproved": review_unapproved,
        },
        "bid_analysis_summary": _ba_summary,
        "global_facts_summary": _facts_summary,
        "global_facts_status": _facts_status,
    }


# ---------------------------------------------------------------------------
# ✅ G1（2026-09-21）：导出预检 ↔ 就绪度总检 共用同一份数据
# ---------------------------------------------------------------------------
#: 导出预检 issue 类型 → (rule_id, severity)。
#: rule_id 统一取自 services/audit_rules.py（唯一事实源），与程序化预检
#: 共用同一套词表与维度权重，使两处结论可直接比较、不会口径漂移。
_EXPORT_ISSUE_RULE_MAP: dict[str, tuple[str, str]] = {
    "orphan_node": ("DLV-03", "high"),
    "empty_section": ("DLV-01", "high"),
    "low_word_count": ("DLV-02", "medium"),
    "chart_failed": ("DLV-04", "medium"),
    "chart_ungenerated": ("DLV-04", "low"),
    "status_inconsistent": ("DLV-10", "medium"),
    "bid_analysis_incomplete": ("DLV-11", "medium"),
    "review_pending": ("DLV-09", "high"),
    "review_rejected": ("DLV-09", "high"),
    "review_missing": ("DLV-09", "high"),
    "duplicate_section_number": ("DLV-12", "medium"),
    "section_number_mismatch": ("DLV-12", "medium"),  # ✅ 编号统一：存储编号 vs 结构重算编号漂移
    "global_facts_blocked": ("DLV-15", "high"),
    # ✅ E6 Tier 1（2026-09-25 · DLV-14）：失效交叉引用（图号/表号/节号漂移）。
    #    medium 级不阻断导出；用户确认有效可忽略，无效则需修复正文引用。
    "stale_cross_reference": ("DLV-14", "medium"),
    # ✅ E3（2026-09-25 · DLV-13 high）：正文子标题与 DB 子章节命名空间冲突。
    #    配置开启降级但正文仍残留 AI 写的 X.X 点分格式 → 导出成稿真撞号。
    #    high 级阻断导出（要求先修复再交付）。
    "body_subheading_namespace_conflict": ("DLV-13", "high"),
}
#: 单个 finding 最多回带的章节 ID（避免响应体膨胀）
_EXPORT_FINDING_SECTION_CAP = 20


# ---------------------------------------------------------------------------
# ✅ B-15（R182 · 2026-11-06）：high 级问题「阻断导出」承诺兑现（可显式覆写）
# ---------------------------------------------------------------------------
#: 覆写请求体键：前端「已知悉并继续」显式携带；缺省（含旧前端）一律按阻断处理。
_EXPORT_OVERRIDE_BODY_KEY = "override_high_issues"
#: 覆写响应头：只在本轮**确实带着覆写继续导出**时出现。
#: 不复用 X-Export-Degraded —— 后者语义是「后端降级」，覆写是用户显式选择，不得混用。
_EXPORT_OVERRIDE_HEADER = "X-Export-Override"


def _export_override_requested(body: dict | None) -> bool:
    """仅接受显式布尔 True；缺省 / 旧前端 / 字符串一律视为未覆写。"""
    return (body or {}).get(_EXPORT_OVERRIDE_BODY_KEY) is True


def _export_high_issue_blockers(issues: list) -> list[dict]:
    """筛出 high 级阻断项并按 issue 类型聚合。

    只认 :data:`_EXPORT_ISSUE_RULE_MAP` 中的类型 —— B-16 并入的内容体检项
    （``source=content_audit``）不在规则表，天然不参与阻断，口径保持不变。
    """
    from app.services.audit_rules import get_rule

    blocked: dict[str, dict] = {}
    for it in issues or []:
        if not isinstance(it, dict):
            continue
        itype = str(it.get("type") or "")
        meta = _EXPORT_ISSUE_RULE_MAP.get(itype)
        if not meta or meta[1] != "high":
            continue
        entry = blocked.setdefault(
            itype,
            {"type": itype, "rule_id": meta[0], "severity": "high", "count": 0, "title": ""},
        )
        cnt = it.get("count")
        entry["count"] += int(cnt) if isinstance(cnt, int) and cnt > 0 else 1
        if not entry["title"]:
            rule = get_rule(meta[0])
            entry["title"] = str(getattr(rule, "title", "") or "") if rule is not None else ""
    return list(blocked.values())


async def _export_high_issue_gate(scheme_id: str, db, body: dict) -> tuple[list[dict], dict]:
    """导出前 high 级问题闸门。

    Returns:
        ``(blockers, meta)``。``meta["override"]`` 为 True 表示本轮带覆写放行；
        ``meta["checked"]`` 为 False 表示预检读取失败 —— **fail-open 不阻断**：
        「无法判定」不得被伪装成阻断结论（与 B-17 同一口径）。
    """
    try:
        result = await collect_export_issues(scheme_id, db, strict_read=False, include_content_audit=False)
    except Exception as e:  # noqa: BLE001 —— 预检不可用时不阻断导出
        logger.warning("导出前高风险问题检查失败（跳过阻断，允许继续导出）: %s", e)
        return [], {"checked": False, "override": False}
    blockers = _export_high_issue_blockers(result.get("issues") or [])
    if not blockers:
        return [], {"checked": True, "override": False}
    if not _export_override_requested(body):
        return blockers, {"checked": True, "override": False}
    total = sum(int(b["count"]) for b in blockers)
    logger.warning(
        "导出覆写：用户在已知悉 %d 类高风险问题（共 %d 处）后继续导出（scheme=%s）",
        len(blockers),
        total,
        str(scheme_id)[:8],
    )
    return blockers, {"checked": True, "override": True, "total": total}


def _export_blocked_response(blockers: list[dict]) -> JSONResponse:
    """409 + 可执行 detail：明确告诉用户拦了什么、怎么继续。"""
    total = sum(int(b["count"]) for b in blockers)
    names = "、".join(f"{b['title'] or b['type']}（{b['rule_id']}）{b['count']} 处" for b in blockers)
    detail = (
        f"导出已被阻断：存在 {len(blockers)} 类高风险问题（共 {total} 处）——{names}。"
        "请先修复后重试；如确认已知悉，可勾选「已知悉并继续」再次导出（本次覆写会写入导出留痕）。"
    )
    return JSONResponse(
        status_code=409,
        content={
            "detail": detail,
            "error": "export_blocked_high_issues",
            "blocked": True,
            "high_issue_count": len(blockers),
            "high_issue_total": total,
            "high_issues": blockers,
            "override_hint": "如确认已知悉，请在请求体传入 override_high_issues=true（会写入导出留痕）",
        },
    )


def _export_override_headers(meta: dict) -> dict:
    return {_EXPORT_OVERRIDE_HEADER: "high-issues"} if meta.get("override") else {}


def _export_override_note(blockers: list[dict], meta: dict) -> str | None:
    """覆写留痕文案；未覆写返回 None（留痕 comment 逐字保持旧行为）。"""
    if not meta.get("override"):
        return None

    total = meta.get("total", sum(int(b["count"]) for b in blockers))
    return f"已知悉并覆写导出：high 级问题 {len(blockers)} 类 / {total} 处未修复"


def export_issues_to_findings(issues: list) -> list:
    """把导出预检的 issue 列表映射为就绪度评分用的 finding 列表（G1 桥接）。

    同一 rule_id 的问题**聚合为一条 finding**（detail 带数量与样例章节），
    两个原因：
      1. 20 个空章节若各自成 finding，维度扣分会被打到 0 分，远超单条规则的
         语义（"存在空章节"本身只是一条 high）；
      2. merge_findings 本就按 rule_id 去重，产出方就应保证 rule_id 唯一，
         聚合后与程序化预检（DLV-01/03 等）自然按严重度取胜，不会重复扣分。

    Returns:
        统一 finding 结构（与 preflight_engine 输出契约一致，可直接进 score_findings）。
    """
    from app.services.audit_rules import SEVERITY_ORDER, get_rule

    buckets: dict[str, list[dict]] = {}
    for it in issues or []:
        if not isinstance(it, dict):
            continue
        issue_type = str(it.get("type") or "")
        if issue_type not in _EXPORT_ISSUE_RULE_MAP:
            continue
        buckets.setdefault(_EXPORT_ISSUE_RULE_MAP[issue_type][0], []).append(it)

    findings: list[dict] = []
    for rid, items in buckets.items():
        rule = get_rule(rid)
        # ✅ 防漂移守卫（2026-09-27）：本函数产出的 rule_id 必须全部登记在
        #    services/audit_rules.py（唯一事实源）。此前 DLV-13/DLV-14 两条已在此
        #    映射表中使用却从未注册，导致 high 级问题落到下方兜底分支：
        #    title 退化为「导出预检问题」、basis 与 suggestion 全空 —— 用户看到的是
        #    一条没有标题、没有依据、没有修复建议的阻断项。注册表一旦漏登即打 WARNING。
        if rule is None:
            logger.warning(
                "导出预检 issue 类型 %s 映射到未注册规则 %s，请同步 services/audit_rules.py",
                "/".join(sorted({str(i.get("type")) for i in items})),
                rid,
            )
        # 同一 rule 可能由多个 issue 类型映射而来（review_pending / rejected / missing
        # 都映射 DLV-09），取其中最严重的一档，避免轻微类型把严重问题拉低。
        sevs = [_EXPORT_ISSUE_RULE_MAP[str(i.get("type"))][1] for i in items]
        severity = max(sevs, key=lambda s: SEVERITY_ORDER.get(s, 0))

        sections_hit = [str(i.get("section_id") or "") for i in items if i.get("section_id")]
        sample_titles = [str(i.get("title") or "") for i in items if i.get("title")][:3]
        counts: dict[str, int] = {}
        for i in items:
            cnt = i.get("count")
            if isinstance(cnt, int):
                counts[str(i.get("type"))] = counts.get(str(i.get("type")), 0) + cnt

        parts: list[str] = []
        if counts:
            parts.append("、".join(f"{k} {v} 项" for k, v in counts.items()))
        if len(items) > 1:
            parts.append(f"共 {len(items)} 处")
        if sample_titles:
            parts.append("样例：" + "、".join(f"「{t}」" for t in sample_titles))
        findings.append(
            {
                "rule_id": rid,
                "dimension": (rule.dimension if rule else "deliverability"),
                "severity": severity,
                "title": (rule.title if rule else "导出预检问题"),
                "detail": "；".join(parts) or "导出预检发现问题",
                "suggestion": (rule.detail if rule else ""),
                "basis": (rule.basis if rule else ""),
                "section_id": sections_hit[0] if sections_hit else "",
                "section_ids": sections_hit[:_EXPORT_FINDING_SECTION_CAP],
                "section_title": "、".join(sample_titles),
                "mode": "program",
                "source": "export_check",
                "count": len(items),
            }
        )
    return findings


async def _readiness_preflight_summary(scheme_id: str, db) -> dict:
    """就绪度总检（一键总检）最新结论摘要，供导出页直接展示（G1 反向打通）。

    无历史运行时返回 ``{"has_run": False}``，绝不阻塞导出。
    ``stale=True`` 表示正文已变更、该结论已过期（G3 指纹判定），
    导出页据此提示用户"结论可能已过期，建议先重跑一键总检"。
    """
    from app.routers.compliance import _run_is_stale

    try:
        cur = await db.execute(
            "SELECT total, grade, verdict, released, blocked, rule_version,"
            " content_fingerprint, created_at"
            " FROM preflight_runs WHERE scheme_id=?"
            " ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (scheme_id,),
        )
        row = await _db_fetch_one(cur, what="_readiness_preflight_summary")
        if not row:
            return {"has_run": False}
        return {
            "has_run": True,
            "total": row["total"],
            "grade": row["grade"] or "",
            "verdict": row["verdict"] or "",
            "released": bool(row["released"]),
            "blocked": bool(row["blocked"]),
            "rule_version": row["rule_version"] or "",
            "created_at": row["created_at"] or "",
            "stale": await _run_is_stale(db, scheme_id, row),
        }
    except Exception as e:
        logger.warning("导出页读取就绪度总检摘要失败（降级为无结论）: %s", e)
        return {"has_run": False}


async def _record_export_review_trace(
    db, scheme_id: str, fmt: str, round_no: int, filename: str, *, note: str | None = None, commit: bool = True
) -> None:
    """✅ G12：导出成稿写回审核留痕（工程可追溯性硬要求）。

    背景：导出是整条链路里唯一"内容离开系统"的动作，但此前 export.py 全文件
    没有任何 review_records 写入 —— 事后**无法回答"这份成稿是不是评审通过后的
    版本"**。现在每次成功导出落一条**方案级**评审记录：

    - ``from_status = to_status = 方案当前审核状态``（只留痕，不改变状态机）；
    - ``comment`` 含格式 / 导出轮次 / 文件名，可回溯到具体那份文档。

    写入失败只记日志并 rollback，**绝不阻塞导出** —— 文件已是用户此刻要的东西。

    ✅ R176 · D2-3（2026-11-04）：写入主体抽到
    :func:`_write_export_review_trace`（不提交、异常上抛），本函数保留原
    「失败只记日志+rollback、绝不阻塞导出」的包装语义；命中路径由
    :func:`_bump_export_round_and_trace` 复用同一写入主体做单事务。
    """
    try:
        await _write_export_review_trace(db, scheme_id, fmt, round_no, filename, note=note)
        if commit:
            await db.commit()
    except Exception as e:
        logger.warning("导出留痕写入失败（不影响导出结果）: %s", e)
        try:
            await db.rollback()
        except Exception:
            logger.debug("导出留痕写入失败后的回滚也失败（放弃清理）", exc_info=True)


async def _write_export_review_trace(
    db, scheme_id: str, fmt: str, round_no: int, filename: str, *, note: str | None = None
) -> None:
    """写入**未提交**的评审留痕记录（异常一律上抛，commit/rollback 由调用方决定）。

    ✅ R176 · D2-3：从 :func:`_record_export_review_trace` 抽出的纯写入段 ——
    命中路径需要把「轮次自增」与「留痕」放进同一事务，任何一步失败都必须
    整体回滚，故此处**不得**吞异常（吞掉会让调用方误以为写成功而照常 commit）。
    """
    from app.routers.review import _scheme_project_id, _write_record

    cur = await db.execute("SELECT name, review_status FROM schemes WHERE id=?", (scheme_id,))
    row = await _db_fetch_one(cur, what="_record_export_review_trace:schemes")
    status = (row["review_status"] or "") if row else ""
    name = (row["name"] or "") if row else scheme_id
    comment = f"已导出 {fmt.upper()}（第 {round_no} 轮）：{filename}"
    # ✅ B-15（R182 · 2026-11-06）：覆写导出必须在留痕里可追溯（note 缺省时逐字不变）。
    if note:
        comment = f"{comment}；{note}"
    await _write_record(
        db,
        scheme_id,
        "",
        name,
        status,
        status,
        "系统",
        comment,
        await _scheme_project_id(db, scheme_id),
    )


# ===================== 导出格式预设库（对标 OpenBidKit export_templates） =====================
async def _preset_project_id(db, scheme_id: str) -> str:
    """预设按项目维度共享：从方案反查其 project_id。

    ✅ 收敛：委托 facts_extractor.resolve_scheme_project_id（服务层单一出口），
       不再内联 SQL。行为等价（fail-soft 返回空串），且失败时有 WARNING 日志。
    """
    return await resolve_scheme_project_id(db, scheme_id)


@router.get("/presets")
async def list_export_presets(scheme_id: str, db=Depends(get_db)):
    """列出当前方案所属项目的导出格式预设（默认置顶）。"""
    pid = await _preset_project_id(db, scheme_id)
    cur = await db.execute(
        "SELECT id, name, config_json, is_default, updated_at FROM export_presets "
        "WHERE project_id=? ORDER BY is_default DESC, updated_at DESC",
        (pid,),
    )
    rows = await _db_fetch_all(cur, what="list_export_presets")
    return {
        "presets": [
            {
                "id": r["id"],
                "name": r["name"],
                "config": json.loads(r["config_json"] or "{}"),
                "is_default": bool(r["is_default"]),
                "updated_at": r["updated_at"],
            }
            for r in rows
        ]
    }


@router.post("/presets")
async def create_export_preset(scheme_id: str, body: dict, db=Depends(get_db)):
    """保存当前导出配置为命名预设（按项目共享，配置按白名单规范化）。"""
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "预设名称不能为空")
    config = _normalize_config(body.get("config") or {})
    pid = await _preset_project_id(db, scheme_id)
    preset_id = str(uuid.uuid4())
    now = datetime.now().isoformat()
    await _db_exec(
        db,
        "INSERT INTO export_presets (id, project_id, name, config_json, is_default, created_at, updated_at) "
        "VALUES (?,?,?,?,0,?,?)",
        (preset_id, pid, name, json.dumps(config, ensure_ascii=False), now, now),
        what="create_export_preset",
    )
    await db.commit()
    return {"id": preset_id, "name": name, "config": config}


@router.put("/presets/{preset_id}")
async def update_export_preset(scheme_id: str, preset_id: str, body: dict, db=Depends(get_db)):
    """更新预设（名称 / 配置）。"""
    pid = await _preset_project_id(db, scheme_id)
    cur = await db.execute("SELECT id FROM export_presets WHERE id=? AND project_id=?", (preset_id, pid))
    if not await _db_fetch_one(cur, what="update_export_preset 存在性"):
        raise HTTPException(404, "预设不存在")
    name = (body.get("name") or "").strip()
    sets, params = [], []
    if name:
        sets.append("name=?")
        params.append(name)
    if "config" in body:
        sets.append("config_json=?")
        params.append(json.dumps(_normalize_config(body["config"]), ensure_ascii=False))
    sets.append("updated_at=?")
    params.append(datetime.now().isoformat())
    params += [preset_id, pid]
    await _db_exec(
        db,
        f"UPDATE export_presets SET {','.join(sets)} WHERE id=? AND project_id=?",
        params,
        what="update_export_preset",
    )
    await db.commit()
    return {"ok": True}


@router.post("/presets/{preset_id}/default")
async def set_default_export_preset(scheme_id: str, preset_id: str, db=Depends(get_db)):
    """将某预设设为项目默认（同项目其它预设取消默认）。"""
    pid = await _preset_project_id(db, scheme_id)
    cur = await db.execute("SELECT id FROM export_presets WHERE id=? AND project_id=?", (preset_id, pid))
    if not await _db_fetch_one(cur, what="set_default_export_preset 存在性"):
        raise HTTPException(404, "预设不存在")
    await _db_exec(
        db, "UPDATE export_presets SET is_default=0 WHERE project_id=?", (pid,), what="set_default_export_preset 清默认"
    )
    await _db_exec(
        db,
        "UPDATE export_presets SET is_default=1 WHERE id=? AND project_id=?",
        (preset_id, pid),
        what="set_default_export_preset 设默认",
    )
    await db.commit()
    return {"ok": True}


@router.delete("/presets/{preset_id}")
async def delete_export_preset(scheme_id: str, preset_id: str, db=Depends(get_db)):
    """删除预设。"""
    pid = await _preset_project_id(db, scheme_id)
    await _db_exec(
        db, "DELETE FROM export_presets WHERE id=? AND project_id=?", (preset_id, pid), what="delete_export_preset"
    )
    await db.commit()
    return {"ok": True}


# ✅ 修复：本模块原自带一份 20 条的关键字映射，与 _chart_pipeline（8 条）分叉。
# 现已统一到 chart_validators 的唯一映射表，保留同名别名以兼容既有引用点与诊断脚本。
# 注：原先 pie / xychart 归一为 comparison（而非 "pie"/"xychart"）是有意为之——
# 渲染器只认识 comparison，产出渲染器不认识的类型会让 PIL 兜底必然失败，
# 且因缺少 _CHART_TYPE_MAP 条目而拿不到「对比图」默认图题（该语义在统一表中保持）。
_MERMAID_TYPE_MAP = MERMAID_KEYWORD_TO_CHART_TYPE


from app.services.content_blocks import (  # noqa: F401 — 拆分后保留绑定（_export_docx_parts 经 _sh 引用）
    _LEAD_IN_HINT_RE,  # ✅ 2026-10-05（D4）：引导语识别正则收敛到唯一实现，删除本地副本
    LEAD_IN_MAX_CHARS,  # ✅ R48（2026-10-06）：引导语长度上限收敛到唯一实现，
    _cn_pure_to_int,
    _compute_subheading,
    _detect_plain_heading,  # noqa: F401  兼容再导出：测试与诊断脚本经 app.routers.export 引用
    _is_orphan_lead_in_text,
    #                          消除本模块此前的裸 `60` 字面量（同一判据第 3 份副本）。
    _norm_heading_text,
    _parse_content_blocks,
    _strip_duplicate_leading_title,
    _strip_title_number,
    strip_subitem_ordinal_prefix,
)

# ---------------------------------------------------------------------------
# ✅ 优化：有序列表标记样式**渲染**（保留作者枚举符外观 + 导出时自动连续编号）
# ---------------------------------------------------------------------------
# 工程文档常见的多级枚举：一、→（一）→ 1. →（1）。**识别**（含原标记样式记录）
# 已随 `_parse_content_blocks` 下沉到 services/content_blocks.py 的
# `_ORDERED_MARKER_RES`，解析结果带出 marker 风格；本模块只保留**渲染**侧
# （marker → 连续序号前缀，见下方 _ordered_prefix）。
# ✅ 2026-10-05（D4 · 死副本清理）：此处原先残留一份 `_ORDERED_MARKER_RES` 副本，
#    T-2 下沉后已无任何引用（导出侧一律消费 content_blocks 解析出的 marker），删除。
_CN_ORDINAL_DIGITS = "零一二三四五六七八九"


def _cn_ordinal(n: int) -> str:
    """阿拉伯数字 → 中文序数（1→一、11→十一、21→二十一；超出 99 回退阿拉伯数字）。"""
    if n <= 0:
        return str(n)
    if n < 10:
        return _CN_ORDINAL_DIGITS[n]
    if n == 10:
        return "十"
    if n < 20:
        return "十" + _CN_ORDINAL_DIGITS[n % 10]
    if n < 100:
        return _CN_ORDINAL_DIGITS[n // 10] + "十" + (_CN_ORDINAL_DIGITS[n % 10] if n % 10 else "")
    return str(n)


def _ordered_prefix(seq: int, marker: str) -> str:
    """按原标记样式渲染连续序号前缀（seq 为导出器重新计数后的序号）。"""
    if marker == "num_dun":
        return f"{seq}、"
    if marker == "num_paren_r":
        return f"{seq}）"
    if marker == "num_paren_lr":
        return f"（{seq}）"
    if marker == "paren_ascii":
        return f"{seq}) "
    if marker == "cn_num_paren":
        return f"（{_cn_ordinal(seq)}）"
    return f"{seq}. "  # ascii：与既有版式保持一致（"1. "）


def _estimate_prefix_width(prefix: str) -> float:
    """估算前缀文本宽度（cm），用于悬挂缩进计算。

    10.5pt 字体下：ASCII 字符 ≈ 0.20 cm/字，中文/全角 ≈ 0.37 cm/字。
    取整到 0.05 cm 精度，避免 Word 渲染时出现亚像素错位。
    """
    w = 0.0
    for ch in prefix:
        if ord(ch) > 0x7F:
            w += 0.37  # 中文/全角字符
        else:
            w += 0.20  # ASCII 字符
    # 至少 0.5 cm，确保悬挂缩进可见
    return max(0.5, round(w, 2))


def _ensure_list_paragraph_style(doc) -> str:
    """获取或创建 "List Paragraph" 段落样式，返回列表段落样式名。

    "List Paragraph" 是 Word 内置的列表段落样式，使段落被识别为列表项
    （带缩进、可被 Word 列表功能操作）。若文档未包含该样式则自动创建。
    """
    from docx.enum.style import WD_STYLE_TYPE
    from docx.oxml.ns import qn

    name = "List Paragraph"
    for st in doc.styles:
        if st.type == WD_STYLE_TYPE.PARAGRAPH and st.name == name:
            return name
    # 创建新样式（基于 Normal，添加基础缩进设置）
    style = doc.styles.add_style(name, WD_STYLE_TYPE.PARAGRAPH)
    style.base_style = doc.styles["Normal"]
    ppr = style.element.get_or_add_pPr()
    # 清除继承的大纲级别（列表段落不属于大纲层级）
    for ol in ppr.findall(qn("w:outlineLvl")):
        ppr.remove(ol)
    return name


# ✅ 2026-10-05（D4 · 死副本清理）：`_TABLE_CAPTION_RE` / `_IMAGE_LINE_RE` /
# `_MERMAID_TITLE_RES` / `_LEAD_IN_TAIL_RE` 在 T-2 下沉后本模块内**已无任何引用**，
# 唯一实现均在 services/content_blocks.py（导出侧只消费其解析结果）。
# 为杜绝「改一处、另一处漂移」，此处本地副本全部删除。


# ✅ AI 配图：正文内嵌的 Markdown 图片行识别（`_IMAGE_LINE_RE`）已随
# _parse_content_blocks 下沉到 services/content_blocks.py（导出前会下载并作为真实位图插入）。


# ---------------------------------------------------------------------------
# ✅ 图题抽取（2026-09-19）：导出图题此前一律取「图表类型通用名」
# ---------------------------------------------------------------------------
# 实测交付文档取证：正文写「脚手架分段搭设与转换顺序如下图所示：」，紧跟的图题
# 却是「图 4-1 劳动力配置计划」—— 类型通用名与本章语义完全错配。
# 提示词已明确要求 chart-json 载荷自带 title（fig_no 留空、图号由导出器编号），
# 故图题优先级为：
#   ① chart-json 载荷 title（AI 按语义写的业务标题，最可信）
#   ② Mermaid 的 `title` 指令（仅 gantt/pie/timeline 有）
#   ③ 图表块上方引导语（「…如下图所示：」剥尾后的名词短语）
#   ④ 类型通用名（_CHART_TYPE_MAP，最终兜底）
# ✅ 2026-10-05（D4）：`_MERMAID_TITLE_RES` / `_LEAD_IN_TAIL_RE` / `_LEAD_IN_HINT_RE`
#    的唯一实现均在 services/content_blocks.py；本模块已删除同名本地副本
#    （`_LEAD_IN_HINT_RE` 改为从 content_blocks 导入）。


# ---------------------------------------------------------------------------
# ✅ BUG 修复：正文开头重复章节标题（导出后出现 "1 XXX" + "XXX" 两行）
# ---------------------------------------------------------------------------
# 标题编号前缀剥离（Markdown 井号 / 第X章 / （一） / 1.1.1 / 1） / a ）：
# ✅ 2026-10-05（D4）：唯一实现为 services/content_blocks.py 的
#    `_HEADING_NUM_PREFIX_RES`（`_strip_duplicate_leading_title` 消费）；
#    本模块的同名本地副本已无引用，删除。


# Markdown 行内标记：
#   ***粗斜*** / **粗** / *斜* / ~~删除线~~ / `行内代码` / [文本](链接) / ![图片说明](url)
# ✅ 注意分组顺序：三连星必须先于双星、双星先于单星，否则 ***x*** 会被拆成
#    "*" + "*x*" + "*" 三个碎块（历史行为，已修正）。
from app.routers._export_docx_parts import (  # noqa: F401,E402,I001 — 拆分兼容转发（原名原对象，monkeypatch 语义不变）
    LAYOUT_PRESETS,
    _APPENDIX_LIST_LEAD_RE,
    _CHART_DPI_FALLBACK,
    _CHART_MAX_HEIGHT_CM,
    _CHART_MAX_WIDTH_CM,
    _DOCX_SAFE_IMAGE_FORMATS,
    _DocxBodyRollback,
    _FIX_STATS_KEYS,
    _FIX_STATS_LOCAL,
    _INLINE_RE,
    _LEADING_TABLE_NUM_RE,
    _TABLE_MAX_CELLS,
    _TABLE_MAX_COLS,
    _TABLE_MAX_ROWS,
    _accumulate_fix_stats,
    _add_compilation_note_page,
    _add_cover_info_table,
    _add_drawing_appendix_page,
    _add_expert_review_form,
    _add_form_table,
    _add_illustration_from_bytes,
    _add_inline_chart_from_bytes,
    _add_markdown_runs,
    _add_omml_formula,
    _add_prelim_page_title,
    _add_runs_with_inline_format,
    _add_scheme_approval_table,
    _add_table_caption,
    _add_table_from_markup,
    _appendix_cell_paragraph,
    _assign_table_column_widths,
    _block_plain_text,
    _chart_ok,
    _collapse_redundant_spaces,
    _degrade_block_to_text,
    _figure_chapter_num,
    _fit_image_cm,
    _fix_stats,
    _image_format_supported,
    _insert_image_with_caption,
    _log_fix_stats,
    _normalize_delivery_text,
    _pop_orphan_lead_in,
    _read_image_dpi,
    _resolve_layout_preset,
    _safe_add_run,
    _scheme_form_header,
    _set_margins,
    _set_run_font,
    _strip_leading_table_number,
    _unstack_or_trim_table_rows,
    _write_chart_placeholder_line,
    _xml_safe_text,
)


def _add_word_field(paragraph, instr: str, placeholder: str = "1"):
    """在段落中插入一个简单 Word 域并返回该 run。

    XML 结构：fldChar(begin) + instrText + fldChar(separate) + w:t + fldChar(end)。
    ✅ BUG 修复：旧实现缺少 `separate` 与占位结果，Word 在域未刷新时该处渲染为
    空白（页码"消失"），而带 separate 的域即使未刷新也会显示合理占位值。
    """
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    run = paragraph.add_run()
    fld_begin = OxmlElement("w:fldChar")
    fld_begin.set(qn("w:fldCharType"), "begin")
    instr_el = OxmlElement("w:instrText")
    instr_el.set(qn("xml:space"), "preserve")
    instr_el.text = instr
    fld_sep = OxmlElement("w:fldChar")
    fld_sep.set(qn("w:fldCharType"), "separate")
    txt = OxmlElement("w:t")
    txt.text = placeholder
    fld_end = OxmlElement("w:fldChar")
    fld_end.set(qn("w:fldCharType"), "end")
    for el in (fld_begin, instr_el, fld_sep, txt, fld_end):
        run._r.append(el)
    return run


def _add_page_field(paragraph):
    """在段落中插入 Word PAGE 域（当前页码），用于 show_page_number 开关"""
    return _add_word_field(paragraph, " PAGE ", "1")


def _add_numpages_field(paragraph):
    """在段落中插入 Word NUMPAGES 域（文档总页数）"""
    return _add_word_field(paragraph, " NUMPAGES ", "1")


def _add_toc_field(paragraph, depth: int = 3):
    """在段落中插入 Word TOC 域（自动目录生成）

    域代码：{ TOC \\o "1-3" \\h \\z \\u \\* MERGEFORMAT }
    Word 打开后自动扫描 Heading 1/2/3 样式生成目录。
    用户可按 F9 或右键"更新域"刷新目录。
    """
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    run = paragraph.add_run()
    fld_begin = OxmlElement("w:fldChar")
    fld_begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    try:
        depth = max(1, min(int(depth or 3), 7))
    except (TypeError, ValueError):
        depth = 3
    instr.text = rf' TOC \o "1-{depth}" \h \z \u \* MERGEFORMAT '
    fld_sep = OxmlElement("w:fldChar")
    fld_sep.set(qn("w:fldCharType"), "separate")
    # 初始占位文本（Word 刷新后会被替换成真实目录）
    placeholder = OxmlElement("w:t")
    placeholder.text = "（Word 打开后自动生成目录，请按 F9 更新域）"
    fld_end = OxmlElement("w:fldChar")
    fld_end.set(qn("w:fldCharType"), "end")
    run._r.append(fld_begin)
    run._r.append(instr)
    run._r.append(fld_sep)
    run._r.append(placeholder)
    run._r.append(fld_end)


# ---------------------------------------------------------------------------
# ✅ 版式增强：XML 级排版能力（底纹 / 边框 / 代码块 / 域自动刷新 / 文件名）
# ---------------------------------------------------------------------------
_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_PDF_MIME = "application/pdf"

# CT_PPr 为严格序列，w:pBdr / w:shd 必须排在下列元素之前
_PPR_AFTER_PBDR = (
    "w:shd",
    "w:tabs",
    "w:suppressAutoHyphens",
    "w:kinsoku",
    "w:wordWrap",
    "w:overflowPunct",
    "w:topLinePunct",
    "w:autoSpaceDE",
    "w:autoSpaceDN",
    "w:bidi",
    "w:adjustRightInd",
    "w:snapToGrid",
    "w:spacing",
    "w:ind",
    "w:contextualSpacing",
    "w:mirrorIndents",
    "w:suppressOverlap",
    "w:jc",
    "w:textDirection",
    "w:textAlignment",
    "w:textboxTightWrap",
    "w:outlineLvl",
    "w:divId",
    "w:cnfStyle",
    "w:rPr",
    "w:sectPr",
    "w:pPrChange",
)


def _set_paragraph_shading(paragraph, fill: str):
    """给段落加底纹（代码块 / 引用块共用）。"""
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    ppr = paragraph._p.get_or_add_pPr()
    if ppr.find(qn("w:shd")) is not None:
        return
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill)
    ppr.insert_element_before(shd, *_PPR_AFTER_PBDR[1:])


def _set_paragraph_borders(paragraph, **sides):
    """给段落加边框，用法：``bottom=("single", 6, "BFBFBF")``。

    参数为 (线型, 线宽/八分之一磅, 颜色)。CT_PPr 序列要求 w:pBdr 在 w:shd 之前，
    w:pBdr 自身的子元素序列为 top → left → bottom → right → between → bar。
    """
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    ppr = paragraph._p.get_or_add_pPr()
    pbd = ppr.find(qn("w:pBdr"))
    if pbd is None:
        pbd = OxmlElement("w:pBdr")
        ppr.insert_element_before(pbd, *_PPR_AFTER_PBDR)
    for side in ("top", "left", "bottom", "right", "between", "bar"):
        spec = sides.get(side)
        if not spec or pbd.find(qn(f"w:{side}")) is not None:
            continue
        style, sz, color = spec
        el = OxmlElement(f"w:{side}")
        el.set(qn("w:val"), style)
        el.set(qn("w:sz"), str(sz))
        el.set(qn("w:space"), "4")
        el.set(qn("w:color"), color)
        pbd.append(el)


# ---------------------------------------------------------------------------
# ✅ R109（2026-10-19 · P0）：OOXML 严格序列归一 —— 根治「WPS 打不开」
# ---------------------------------------------------------------------------
# 实证取证（桌面《文档损坏诊断与修复报告》对应成稿，逐 ZIP 条目 + lxml 体检）：
#   落地脚手架第1轮 162 处 / 第2轮 166 处 / 模板工程第4轮 256 处
#   <w:tcPr> 子元素顺序违例（实测 tcW → vAlign → shd，而 ECMA-376 CT_TcPr
#   要求 shd 排在 vAlign 之前）；同一份文档「能打开」的修复版违例数为 **0**。
#   其余部件（styles / numbering / footer / settings）与结构（tbl 归属、
#   sectPr 位置、非法 XML 字符）两侧均无差异 —— 这是唯一差异点。
#   Word 对 CT_* 序列违例宽容，WPS 严格校验直接拒绝打开，于是出现
#   「python-docx 能读、WPS 打不开」的怪象。
# 根因：旧实现 ``get_or_add_tcPr().append(shd)`` 把 w:shd 追加到末尾，
#   而该格此前已被 ``cell.vertical_alignment = CENTER`` 写入 w:vAlign，
#   于是必然产出 tcW → vAlign → shd 的非法序列。
# CT_TcPr 为严格序列，w:shd 必须排在下列元素之前
_TCPR_AFTER_SHD = (
    "w:noWrap",
    "w:tcMar",
    "w:textDirection",
    "w:tcFitText",
    "w:vAlign",
    "w:hideMark",
)


def _set_cell_shading(cell, fill: str) -> None:
    """给单元格加底纹（表头 / 封面信息表 / 签字表共用）。

    ✅ R109：旧实现 ``tcPr.append(shd)`` 有两处缺陷 ——
      ① 把 w:shd 追加到末尾，若该格已有 w:vAlign 就产出**非法序列**
         （Word 容忍、WPS 拒绝打开）；
      ② 重复调用会堆出多个 w:shd，而 CT_TcPr 中 w:shd 只允许 0~1 个。
    现改为按 CT_TcPr 序列定位插入，并做幂等去重。
    """
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    tc_pr = cell._tc.get_or_add_tcPr()
    if tc_pr.find(qn("w:shd")) is not None:
        return
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill)
    tc_pr.insert_element_before(shd, *_TCPR_AFTER_SHD)


# ECMA-376 中具有**严格子元素序列**、且导出侧会手写的属性容器。
# 顺序表取自 CT_PPr / CT_RPr / CT_TblPr / CT_TcPr / CT_TrPr / CT_SectPr。
_STRICT_CT_SEQUENCE: dict[str, tuple[str, ...]] = {
    "pPr": (
        "pStyle",
        "keepNext",
        "keepLines",
        "pageBreakBefore",
        "framePr",
        "widowControl",
        "numPr",
        "suppressLineNumbers",
        "pBdr",
        "shd",
        "tabs",
        "suppressAutoHyphens",
        "kinsoku",
        "wordWrap",
        "overflowPunct",
        "topLinePunct",
        "autoSpaceDE",
        "autoSpaceDN",
        "bidi",
        "adjustRightInd",
        "snapToGrid",
        "spacing",
        "ind",
        "contextualSpacing",
        "mirrorIndents",
        "suppressOverlap",
        "jc",
        "textDirection",
        "textAlignment",
        "textboxTightWrap",
        "outlineLvl",
        "divId",
        "cnfStyle",
        "rPr",
        "sectPr",
        "pPrChange",
    ),
    "rPr": (
        "rStyle",
        "rFonts",
        "b",
        "bCs",
        "i",
        "iCs",
        "caps",
        "smallCaps",
        "strike",
        "dstrike",
        "outline",
        "shadow",
        "emboss",
        "imprint",
        "noProof",
        "snapToGrid",
        "vanish",
        "webHidden",
        "color",
        "spacing",
        "w",
        "kern",
        "position",
        "sz",
        "szCs",
        "highlight",
        "u",
        "effect",
        "bdr",
        "shd",
        "fitText",
        "vertAlign",
        "rtl",
        "cs",
        "em",
        "lang",
        "eastAsianLayout",
        "specVanish",
        "oMath",
    ),
    "tblPr": (
        "tblStyle",
        "tblpPr",
        "tblOverlap",
        "bidiVisual",
        "tblStyleRowBandSize",
        "tblStyleColBandSize",
        "tblW",
        "jc",
        "tblCellSpacing",
        "tblInd",
        "tblBorders",
        "shd",
        "tblLayout",
        "tblCellMar",
        "tblLook",
        "tblCaption",
        "tblDescription",
    ),
    "tcPr": (
        "cnfStyle",
        "tcW",
        "gridSpan",
        "hMerge",
        "vMerge",
        "tcBorders",
        "shd",
        "noWrap",
        "tcMar",
        "textDirection",
        "tcFitText",
        "vAlign",
        "hideMark",
    ),
    "trPr": (
        "cnfStyle",
        "divId",
        "gridBefore",
        "gridAfter",
        "wBefore",
        "wAfter",
        "cantSplit",
        "trHeight",
        "tblHeader",
        "tblCellSpacing",
        "jc",
        "hidden",
    ),
    "sectPr": (
        "footnotePr",
        "endnotePr",
        "type",
        "pgSz",
        "pgMar",
        "paperSrc",
        "pgBorders",
        "lnNumType",
        "pgNumType",
        "cols",
        "formProt",
        "vAlign",
        "noEndnote",
        "titlePg",
        "textDirection",
        "bidirectional",
        "rtlGutter",
        "docGrid",
        "printerSettings",
        "sectPrChange",
    ),
}


def _export_content_residue(fix_stats: dict | None) -> int:
    """终稿无法自动消除的格式残留总数（用于缓存闸门与 ``X-Export-Degraded``）。

    计数来源：``_docx_final_lint`` 的第三遍只读扫描；两轮规范化已清零时恒为 0，
    旧调用方与旧产物零回归（新增键只在 ``_docx_final_lint`` 内写入）。
    """
    try:
        return int((fix_stats or {}).get("lint_residual_total", 0) or 0)
    except (TypeError, ValueError):  # pragma: no cover - 防御异常统计值
        return 0


def _iter_doc_paragraphs(doc):
    """遍历正文 + 表格单元格 + 页眉/页脚段落。

    ✅ R179 修复覆盖盲区：旧版只遍历 ``doc.paragraphs``（仅正文顶层），
    表格单元格与页眉页脚里的 ``===== Page`` / HTML / 双重实体 / LaTeX
    完全漏检 —— 而第1轮报告的残留恰恰大量出现在表格单元格内。
    """
    for para in doc.paragraphs:
        yield para
    try:
        seen = set()
        for table in doc.tables:
            for row in table.rows:
                for cell in row.cells:
                    key = id(cell._tc)
                    if key in seen:
                        continue
                    seen.add(key)
                    for para in cell.paragraphs:
                        yield para
    except Exception:  # noqa: BLE001 - python-docx 结构异常（合并/损坏单元格）时停止遍历表格，不阻断导出
        pass
    try:
        for sec in doc.sections:
            for hf in (sec.header, sec.footer):
                try:
                    if hf.is_linked_to_previous:
                        continue
                    for para in hf.paragraphs:
                        yield para
                except Exception:  # pragma: no cover
                    continue
    except Exception:  # noqa: BLE001 - 页眉页脚结构异常时停止遍历，不阻断导出
        pass


def _docx_final_lint(doc) -> dict:
    """落盘前成稿 lint 守卫（2026-11-05 · 防回归最后一道闸）。

    背景：《机电安装施工组织设计_20261104_第1轮》格式问题报告证明，仅靠
    源文解析层修复不够 —— 字面 ``\\n``、PDF 分页标记、HTML 标签、Markdown
    残留可能从**任何旁路**（封面、事实附录、降级块、题注）渗入成稿。
    本函数在 ``doc.save()`` 之前对全文档段落做一次扫仓：

      自动修复（只做确定性、无格式风险的替换）：
        · run 文本中的字面 ``\\n`` → 软换行（保留换行意图）；
        · ``===== Page N =====`` / 「第X页共Y页」残留 → 删除；
        · 行内 ``<table>`` 等裸 HTML 标签 → 删除（内容保留）；
        · 非代码段行首 Markdown ``##`` 井号残留 → 删除。

      只告警不修改（无安全的自动修复）：
        · 非 code 段落含软换行（``<w:br/>``）—— 提示正文可能被压进一段；
        · 普通段落含 ≥2 个管道符 —— 提示可能的未转换表格文本。

    返回修复/检出计数 dict（并入本次导出的 X-Fix-Stats）。fail-soft：
    lint 自身异常绝不阻断导出。
    """
    stats = {
        "lint_literal_newline": 0,
        "lint_page_mark": 0,
        "lint_html_tag": 0,
        "lint_md_residue": 0,
        "lint_soft_break": 0,
        "lint_pipe_text": 0,
        # ✅ R179 新增（加法式扩展，既有键语义/取值不变）：
        #   双重实体 / 残留 LaTeX / 公式分隔符的**自动还原**计数；
        #   residual_* 为两轮规范化后仍无法消除的残留（非零 → 可见降级）。
        "lint_entity_residue": 0,
        "lint_latex_residue": 0,
        "lint_residual_page": 0,
        "lint_residual_md": 0,
        "lint_residual_html": 0,
        "lint_residual_entity": 0,
        "lint_residual_latex": 0,
        "lint_residual_pipe": 0,
        "lint_duplicate_table_no": 0,
        "lint_residual_total": 0,
    }
    try:
        from docx.oxml.ns import qn

        # run 内含媒体/域代码时禁止重写 run.text（会摧毁图片/域结构）
        _RUN_UNSAFE_CHILDREN = (
            qn("w:drawing"),
            qn("w:pict"),
            qn("w:object"),
            qn("w:fldChar"),
            qn("w:instrText"),
            qn("w:footerReference"),
        )
        _RE_LIT_NL = re.compile(r"(?<!\\)\\n(?!\\)")
        _RE_PAGE_RESIDUE = re.compile(r"\s*=+\s*Page\s*\d+\s*=+\s*|\s*第\s*\d+\s*页\s*共\s*\d+\s*页\s*", re.I)
        _RE_HTML_TAG = re.compile(r"</?(?:table|tr|td|th|tbody|thead|span|div|p)\b[^>]*>", re.I)
        _RE_MD_HASH = re.compile(r"^\s*#{1,6}\s+")
        #: `&amp;gt;` / `&amp;lt;` / 更深的 `&amp;amp;gt;` —— 转换链每多一层就多一个 `amp;`。
        _RE_ENTITY = re.compile(r"&(?:amp;)*(?:gt|lt|amp|nbsp);", re.I)
        #: 残留 LaTeX：命令名、行内/行间公式分隔符（成稿中不应出现任何 `\命令`）。
        _RE_LATEX = re.compile(r"\\[A-Za-z]+|\\\(|\\\[")
        #: 表号重复检测（`表4-1` / `表 4-1` 统一成无空格形态后比较）。
        _RE_TABLE_NO = re.compile(r"表\s*\d+\s*-\s*\d+")

        # 两轮规范化：第一轮通常已清零；第二轮兜住「替换后再次暴露」的嵌套形态
        # （如 `&amp;amp;gt;` 需逐层解码、`^{\circ}\mathrm{C}` 需二次命令展开）。
        for _pass in range(2):
            for para in _iter_doc_paragraphs(doc):
                try:
                    style_name = para.style.name if para.style is not None else ""
                except Exception:
                    style_name = ""
                is_code = "Consolas" in style_name or "code" in style_name.lower()

                p_text = para.text or ""
                # 只统计一次（两轮规范化会让同一段落在第二遍重复计数）
                if _pass == 0:
                    if "\n" in p_text and not is_code:
                        stats["lint_soft_break"] += 1
                    if not is_code and p_text.count("|") >= 2:
                        stats["lint_pipe_text"] += 1

                for run in para.runs:
                    if any(run._r.find(tag) is not None for tag in _RUN_UNSAFE_CHILDREN):
                        continue
                    t = run.text or ""
                    if not t:
                        continue
                    new = t
                    if _RE_LIT_NL.search(new):
                        new = _RE_LIT_NL.sub("\n", new)
                        stats["lint_literal_newline"] += 1
                    if _RE_PAGE_RESIDUE.search(new):
                        new = _RE_PAGE_RESIDUE.sub(" ", new)
                        stats["lint_page_mark"] += 1
                    if _RE_HTML_TAG.search(new):
                        new = _RE_HTML_TAG.sub("", new)
                        stats["lint_html_tag"] += 1
                    if not is_code and _RE_MD_HASH.match(new):
                        new = _RE_MD_HASH.sub("", new, count=1)
                        stats["lint_md_residue"] += 1
                    if _RE_ENTITY.search(new):
                        new = _normalize_delivery_text(new, residual=True)
                        stats["lint_entity_residue"] += 1
                    if _RE_LATEX.search(new):
                        new = _normalize_delivery_text(new, residual=True)
                        stats["lint_latex_residue"] += 1
                    if new != t:
                        # run.text setter 会把 \n 转 <w:br/>、\t 转 <w:tab/>，
                        # run 级 rPr 格式保留（此处 run 已排除媒体/域）。
                        run.text = new

        # 第三遍只扫描不修改：两轮规范化后仍然命中的，即「无法自动消除的残留」，
        # 必须计数并触发可见降级（见 _build_docx_sync 的 X-Export-Degraded）。
        table_nos: dict[str, int] = {}
        for para in _iter_doc_paragraphs(doc):
            try:
                style_name = para.style.name if para.style is not None else ""
            except Exception:
                style_name = ""
            is_code = "Consolas" in style_name or "code" in style_name.lower()
            p_text = para.text or ""
            for run in para.runs:
                t = run.text or ""
                if not t:
                    continue
                if _RE_PAGE_RESIDUE.search(t):
                    stats["lint_residual_page"] += 1
                if not is_code and _RE_MD_HASH.match(t):
                    stats["lint_residual_md"] += 1
                if _RE_HTML_TAG.search(t):
                    stats["lint_residual_html"] += 1
                if _RE_ENTITY.search(t):
                    stats["lint_residual_entity"] += 1
                if _RE_LATEX.search(t):
                    stats["lint_residual_latex"] += 1
            if not is_code and p_text.count("|") >= 2:
                stats["lint_residual_pipe"] += 1
            # 只把**居中的表题段**计入表号重复判定；正文引用（「见表 4-1」）
            # 重复出现是正常写法，计入会造成每次导出都误判降级。
            try:
                _centered = para.alignment is not None and int(para.alignment) == 1
            except Exception:
                _centered = False
            if _centered and p_text.strip().startswith("表"):
                for m in _RE_TABLE_NO.finditer(p_text):
                    key = re.sub(r"\s+", "", m.group(0))
                    table_nos[key] = table_nos.get(key, 0) + 1

        stats["lint_duplicate_table_no"] = sum(1 for v in table_nos.values() if v > 1)
        stats["lint_residual_total"] = sum(
            stats[k]
            for k in (
                "lint_residual_page",
                "lint_residual_md",
                "lint_residual_html",
                "lint_residual_entity",
                "lint_residual_latex",
                "lint_residual_pipe",
                "lint_duplicate_table_no",
            )
        )

        _hit = {k: v for k, v in stats.items() if v}
        if _hit:
            logger.warning("导出：落盘前 lint 守卫命中 %s（详见 X-Fix-Stats）", _hit)
        if stats["lint_residual_total"]:
            logger.warning(
                "导出：终稿仍有 %d 处无法自动消除的格式残留（成稿已标记降级，" "frontend 将提示人工核对）",
                stats["lint_residual_total"],
            )
    except Exception:  # pragma: no cover - lint 失败不阻断导出
        logger.warning("导出：落盘前 lint 守卫执行失败（跳过，不影响成稿）", exc_info=True)
    return stats


def _normalize_ct_sequence(root) -> int:
    """把一棵 XML 树内所有严格序列容器的子元素重排到 ECMA-376 规定顺序。

    返回被修正的容器数。**fail-safe 铁律**：
      · 容器内出现序列表以外的子元素（含注释 / PI / 未知扩展）→ 整容器不动；
      · 已经有序的容器 → 一个字节都不动（稳定排序，既有产物零漂移）。
    """
    from docx.oxml.ns import qn

    fixed = 0
    for tag, seq in _STRICT_CT_SEQUENCE.items():
        rank = {qn(f"w:{n}"): i for i, n in enumerate(seq)}
        for el in root.iter(qn(f"w:{tag}")):
            children = list(el)
            if len(children) < 2:
                continue
            ranks = [rank.get(c.tag) for c in children]
            if any(r is None for r in ranks) or ranks == sorted(ranks):
                continue
            for c in sorted(children, key=lambda c: rank[c.tag]):
                el.append(c)  # lxml：对已在树中的节点 append 即"移动"
            fixed += 1
    return fixed


def _normalize_docx_strict_sequence(doc) -> int:
    """落盘前对包内**全部** XML 部件做一次严格序列归一。

    这是最后一道理，不替代正确的插入位置（见 :func:`_set_cell_shading`）：
    任何一处遗漏的 ``append``（以及未来新增的手写 XML）都在此被兜住，
    WPS 不会再因 CT_* 序列违例拒绝打开成稿。
    """
    total = 0
    try:
        parts = list(doc.part.package.iter_parts())
    except Exception as e:  # pragma: no cover - 拿不到包就只整主文档
        logger.warning("导出：OOXML 序列归一无法枚举包内部件（仅处理主文档）: %s", e)
        parts = [(None, doc.part)]
    for item in parts:
        part = item[1] if isinstance(item, tuple) else item
        try:
            el = getattr(part, "element", None)
        except Exception as e:  # pragma: no cover - 部件属性本身抛错同样跳过
            logger.warning("导出：OOXML 序列归一跳过一个部件: %s", e)
            continue
        if el is None:
            continue
        try:
            total += _normalize_ct_sequence(el)
        except Exception as e:  # pragma: no cover - 归一失败绝不阻断落盘
            logger.warning("导出：OOXML 序列归一跳过一个部件: %s", e)
    if total:
        logger.warning(
            "导出：已修正 %d 处 OOXML 严格序列违例（子元素顺序不符 ECMA-376，"
            "WPS 会拒绝打开）—— 成稿内容一字不变，仅调整属性元素书写顺序",
            total,
        )
    return total


def _add_code_block(doc, lines: list[str]):
    """插入代码 / 命令行块：浅灰底纹 + 四边细边框 + 等宽字体，并保留原始缩进。

    ✅ 增强：旧实现把代码块每一行当作独立正文段落输出（无底纹、无边框、
    行首缩进被 Word 折叠、还带 0.74cm 首行缩进），命令行示例会被排成散文。
    """
    from docx.shared import Cm, Pt

    p = doc.add_paragraph()
    pf = p.paragraph_format
    pf.left_indent = Cm(0.5)
    pf.right_indent = Cm(0.5)
    pf.first_line_indent = Cm(0)
    pf.space_before = Pt(4)
    pf.space_after = Pt(6)
    pf.line_spacing = 1.0  # 代码块不继承正文行距，保持紧凑
    run = p.add_run()
    _set_run_font(run, "Consolas", 9.5)
    for idx, raw_line in enumerate(lines or []):
        if idx:
            run.add_break()
        line = raw_line.rstrip()
        lead = len(line) - len(line.lstrip(" \t"))
        prefix = "".join("\u00a0\u00a0\u00a0\u00a0" if ch == "\t" else "\u00a0" for ch in line[:lead])
        run.add_text(prefix + line[lead:])
    _set_paragraph_shading(p, "F2F2F2")
    _set_paragraph_borders(
        p,
        top=("single", 4, "D9D9D9"),
        left=("single", 4, "D9D9D9"),
        bottom=("single", 4, "D9D9D9"),
        right=("single", 4, "D9D9D9"),
    )
    return p


def _enable_update_fields_on_open(doc) -> None:
    """在 settings.xml 中声明 ``<w:updateFields w:val="true"/>``。

    作用：Word 打开文档时自动刷新目录域与页码域，用户无需手动按 F9
    （旧实现只写域代码、未声明本开关，目录长期停留在"请按 F9 更新域"占位文本）。

    ⚠️ 插入位置遵循 ECMA-376 `CT_Settings` 严格序列：`w:updateFields` 必须位于
    `w:hdrShapeDefaults` / `w:compat` / `w:rsids` 等元素之前，否则 Word 打开时会
    报「文件内容有问题，是否恢复」。
    """
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    try:
        settings_el = doc.settings.element
    except Exception:  # pragma: no cover - 极旧版本 python-docx 无 settings
        return
    if settings_el.find(qn("w:updateFields")) is not None:
        return
    el = OxmlElement("w:updateFields")
    el.set(qn("w:val"), "true")
    anchor = None
    for tag in ("w:hdrShapeDefaults", "w:compat", "w:rsids", "w:shapeDefaults", "w:decimalSymbol", "w:listSeparator"):
        anchor = settings_el.find(qn(tag))
        if anchor is not None:
            break
    if anchor is not None:
        anchor.addprevious(el)
    else:
        settings_el.append(el)


_INVALID_FILENAME_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def _safe_filename(name: str, ext: str = "") -> str:
    """清洗下载文件名：剔除路径分隔符与 Windows 非法字符，并限制长度。

    ✅ BUG 修复：旧实现先把扩展名拼上再整体截断到 120 字符 —— 方案名较长时
    （专项方案标题常超 115 字）扩展名会被从尾部截掉（".docx" 变成 ".do"），
    浏览器据此无法识别类型、保存出来是无扩展名文件。现改为**先截断主名、
    再拼扩展名**，保证扩展名永远完整。
    """
    s = _INVALID_FILENAME_RE.sub("_", str(name or "").strip()).strip(" .")
    if not s:
        s = "导出文档"
    suffix = ""
    if ext:
        suffix = f".{ext}"
        # 名称已自带同扩展名：先从主名中剥掉，统一在末尾重新拼接 ——
        # 否则超长自带扩展名会被连同扩展名一起截断。
        if s.lower().endswith(suffix.lower()):
            s = s[: -len(suffix)].rstrip(" .")
    max_base = 120 - len(suffix)
    if len(s) > max_base:
        # 截断后再剥离尾部空白与点，避免出现 "xxx .docx" 之类的怪异名
        s = s[:max_base].rstrip(" .") or "导出文档"
    return s + suffix


def _attachment_disposition(filename: str) -> str:
    """构造健壮的 Content-Disposition（ASCII 回退名 + RFC 5987 UTF-8 名）。

    ✅ BUG 修复（严重）：旧实现把中文文件名直接塞进 HTTP 响应头
    （``filename="{方案名}.pdf"``），而 HTTP 头只能承载 latin-1 —— 中文方案名
    会在编码响应头时抛 UnicodeEncodeError，导致 **PDF 导出整体 500**
    （DOCX 走 FileResponse 由 Starlette 自行编码，所以该缺陷长期只暴露在 PDF 链路）。
    """
    from urllib.parse import quote

    safe = _safe_filename(filename)
    ascii_fallback = re.sub(r"[^\x20-\x7e]", "_", safe) or "export"
    return f'attachment; filename="{ascii_fallback}"; ' f"filename*=UTF-8''{quote(safe, safe='')}"


# ---------------------------------------------------------------------------
# 导出文件名规则：专项方案名称 + 导出日期 + 导出轮次
#   例：某工程基坑支护专项方案_20260916_第3轮.docx
# ---------------------------------------------------------------------------
def _build_export_filename(scheme_name: str, ext: str, round_no: int, when: "datetime | None" = None) -> str:
    """按「专项方案名称 + 日期 + 导出轮次」生成导出文件名（含扩展名）。

    格式：``{方案名称}_{YYYYMMDD}_第{轮次}轮.{ext}``
    日期取导出当天的本地日期；轮次由 `_bump_export_round` 持久化维护。
    名称中的非法字符/超长由 `_safe_filename` 统一清洗。
    """
    day = (when or datetime.now()).strftime("%Y%m%d")
    try:
        n = max(1, int(round_no))
    except (TypeError, ValueError):
        n = 1
    tail = f"_{day}_第{n}轮"
    # ✅ 先按剩余额度截断方案名，再拼「日期+轮次」后缀 —— 否则超长方案名会在
    #    120 字符总长限制下把"第N轮"截掉，轮次信息丢失、同日多次导出重名。
    name = _INVALID_FILENAME_RE.sub("_", str(scheme_name or "").strip()).strip(" .")
    # 方案名自带扩展名时先剥掉，避免出现 "方案.docx_20260916_第2轮.docx"
    for known_ext in (".docx", ".doc", ".pdf"):
        if name.lower().endswith(known_ext):
            name = name[: -len(known_ext)].rstrip(" .")
            break
    max_base = 120 - len(f".{ext}") - len(tail)
    if len(name) > max_base:
        name = name[:max_base].rstrip(" .")
    return _safe_filename(f"{name or '导出文档'}{tail}", ext)


# ---------------------------------------------------------------------------
# ✅ 2026-10-11（文档质量报告 P0-B · 修复1/17）：页眉/封面标题的**模板残留清洗**
#    + 方案名为空或为残留时的**项目名兜底**。
#
# 现象（实证形态）：上游模板 / 定位切换前的占位串原样印进交付件第一页 ——
#   「招投标项目 2026/10/7 19:56:43」（「招投标」是招投标定位时代的项目名，
#   后半段是生成工具的时间戳）一旦进入方案名或页眉配置，**每一页页眉**与
#   **封面大字标题**都会印着它；反之方案名为空时封面是一张空白标题。
#   两者此前全链路零拦截：导出前体检 `audit_content` 只扫正文，
#   页眉 / 封面 / 前置表单抬头 / 文件名完全不在其视野内。
#
# 取舍（刻意保守 —— 避免误伤合法工程名）：
#   · 只在**整串都是残留**时判空，绝不逐片段裁剪真实标题。
#     「XX大桥加固工程专项施工方案」里的「XX」在工程名中是合法编号前缀，
#     逐片段裁剪会把它吃掉 → 无声的内容损坏，比留一条脏页眉更糟。
#   · 项目名兜底只取全局事实里**明确**写着「项目名称 / 工程名称」的条目，
#     绝不猜；取到的值同样过一遍残留清洗（事实库里的项目名本身也可能是占位串）。
#   · 三个函数均纯函数、fail-soft、不写库、不阻断导出；判不出来一律**原样返回**。
# ---------------------------------------------------------------------------

#: 上游模板 / 历史定位遗留的项目名占位关键词（替换时按长度降序，最长优先）
_TEMPLATE_RESIDUE_TOKENS: tuple[str, ...] = (
    "招投标项目",
    "投标项目",
    "投标标书",
    "招标文件",
    "标书编制",
    "项目名称",
    "工程名称",
    "项目名",
    "工程名",
    "未命名项目",
    "新建项目",
    "测试项目",
    "示例项目",
    "模板项目",
    "未命名",
    "无标题",
    "未填写",
    "待填写",
    "请填写",
    "占位",
    "待定",
    "暂定",
    # ⚠️ 刻意不含 `示例`/`模板`/`项目`/`工程` 这四个泛词：只由它们拼成的名字
    #    （如「示例工程」）可能是真实落库工程名，整串置空会静默吃掉用户数据 ——
    #    残留串要被判掉，靠「招投标/投标/招标」前缀 + 日期时间片段即可。
)

#: 「没有任何语义」的片段：日期 / 时间 / 年份 / 括号提示 / 占位下划线。
#: 残留串通常 = 占位关键词 + 这些片段（如「招投标项目 2026/10/7 19:56:43」）。
_TEMPLATE_RESIDUE_FRAG_RE = re.compile(
    r"\d{4}\s*[-/年.]\s*\d{1,2}\s*[-/月.]\s*\d{1,2}\s*日?"  # 2026/10/7、2026-10-7
    r"|\d{4}\s*[-/年.]\s*\d{1,2}\s*[-/月.]"  # 2026/10
    r"|\d{1,2}:\d{2}(?::\d{2})?"  # 19:56:43
    r"|\d{4}"  # 单年份
    r"|【[^】]{0,16}】|\[[^\]]{0,16}\]|（[^）]{0,16}）|\([^)]{0,16}\)"
    r"|[_＿]{2,}|X{2,}|x{2,}|×{2,}|＊{2,}"  # ___、XX、××
    r'|["\s\u3000\u00a0]+'
)

#: 残留判定的"实质内容"出口：剥离全部残留片段后仍有任何汉字即视为真实标题
_TEMPLATE_RESIDUE_CJK_RE = re.compile(r"[\u4e00-\u9fff]")

#: 全局事实里可作为项目名兜底的条目标题（只认明确命名，不按语义猜）
_PROJECT_NAME_FACT_RE = re.compile(r"项目名称|工程名称|项目名|工程名")


def _is_template_residue(text: str) -> bool:
    """判断整串是否只是上游模板的占位残留（剥离后无任何实质内容）。

    反复剥离占位关键词与日期/时间/括号/下划线等无语义片段，
    **一个汉字都不剩才算残留** —— 残留任意一处汉字就说明这是真实工程名。
    """
    cur = str(text or "").strip()
    if not cur:
        return True
    tokens = sorted(_TEMPLATE_RESIDUE_TOKENS, key=len, reverse=True)
    while True:
        nxt = cur
        for tok in tokens:
            if tok in nxt:
                nxt = nxt.replace(tok, " ")
        nxt = _TEMPLATE_RESIDUE_FRAG_RE.sub(" ", nxt)
        if nxt == cur:
            break
        cur = nxt
    return not _TEMPLATE_RESIDUE_CJK_RE.search(cur)


def _sanitize_template_residue_title(title: str) -> str:
    """清洗页眉 / 封面 / 抬头的模板残留：**整串是残留才置空，否则逐字原样返回**。

    保守判空是唯一安全方向：留一条脏页眉用户看得见、会去改导出配置；
    而把真实工程名剪掉一截属于无声的内容损坏，交付件里无从察觉。
    因此本函数只可能「整串判空」，绝不返回被裁剪过的半截标题。
    """
    raw = str(title or "")
    if not raw.strip():
        return ""
    if _is_template_residue(raw):
        logger.warning("导出：标题/页眉为模板残留，已置空: %r", raw[:60])
        return ""
    return raw


def _project_name_from_facts(facts: list | None) -> str:
    """从全局事实取项目名，作为方案名为空 / 为残留时的兜底。

    只认标题明确写着「项目名称 / 工程名称」的条目；取到的值同样过一遍
    残留清洗（事实库里的项目名本身也可能是占位串）。零命中或无事实返回空串 ——
    宁可封面留空，也不猜一个项目名（与前置表单「签字栏不预填」同一红线）。
    """
    for item in facts or []:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or item.get("name") or "")
        if not _PROJECT_NAME_FACT_RE.search(title):
            continue
        val = _sanitize_template_residue_title(item.get("content") or item.get("value") or "")
        if val:
            return val
    return ""


def _display_scheme_name(scheme: dict | None, global_facts: list | None = None) -> str:
    """封面标题 / 下载文件名共用的方案名（清洗残留 + 项目名兜底，单一出口）。

    封面大字与下载文件名是同一份"交付件署名"，必须同一口径 —— 此前
    封面用 `scheme["name"]` 原样、文件名再各自清洗一遍，一旦方案名为残留串，
    封面印着「招投标项目 2026/10/7 19:56:43」而文件名却是「导出文档_…」，
    两份署名对不上。此处收敛为唯一出口；无事实可用时保持旧行为（空串）。
    """
    clean = _sanitize_template_residue_title((scheme or {}).get("name", ""))
    if clean:
        return clean
    return _project_name_from_facts(global_facts)


async def _bump_export_round(db, scheme_id: str) -> int:
    """累加并返回该方案的导出轮次（持久化在 schemes.export_round）。

    语义：**每次导出请求计一轮**（DOCX / PDF 共用同一计数器，PDF 内部生成的
    中间 DOCX 不额外计数）—— 保证同一天多次导出的文件名互不重复，不会出现
    浏览器/系统自动追加的 "xxx(1).docx" 之类的重名副本。
    失败时回退为第 1 轮，绝不阻塞导出主流程。
    """
    try:
        await _db_exec(
            db,
            "UPDATE schemes SET export_round = COALESCE(export_round, 0) + 1 WHERE id=?",
            (scheme_id,),
            what="_bump_export_round",
        )
        cur = await db.execute("SELECT export_round FROM schemes WHERE id=?", (scheme_id,))
        row = await _db_fetch_one(cur, what="_bump_export_round 回读")
        await db.commit()
        if row and row[0]:
            return int(row[0])
    except Exception as e:  # noqa: BLE001 —— 轮次只影响文件名，失败不阻断导出
        logger.warning("导出轮次累加失败（回退为第 1 轮）: %s", e)
        try:
            await db.rollback()
        except Exception:
            logger.debug("导出轮次累加失败后的回滚也失败（放弃清理）", exc_info=True)
    return 1


async def _bump_export_round_and_trace(
    db, scheme_id: str, fmt: str, display_name: str, *, note: str | None = None
) -> tuple[int, str] | None:
    """缓存命中路径：轮次自增 + 审核留痕 **合并为一个事务**（R176 · D2-3）。

    旧实现是两次独立 commit：先 ``_bump_export_round`` 提交轮次，命中分支再
    ``_record_export_review_trace`` 提交留痕。中途失败会出现「轮次涨了但留痕
    丢了」，而 ``export_round`` 是文件名唯一序号 —— 事后无法把某份成稿对应回
    「第几轮已交付」的审计轨迹，链路断裂且不可自愈。

    现顺序：UPDATE+回读轮次 → INSERT 留痕 → **单次 commit**；任一步异常整体
    ``rollback`` 并返回 ``None``（调用方据此放弃本次命中、转 503）。

    为什么失败时不降级交付：留痕是 G12 契约（"这份成稿是不是评审后版本"必须
    可回答），缓存命中只是纯优化；写不进去仍返回 200 等于假报一次未留痕的交付。
    为什么不转"未命中重建"：重建尾部同样要写缓存行与留痕（还要多付一次全量
    渲染），DB 写入不可用时只会放大成本、仍可能丢留痕 —— 一次失败不连锁写库。

    轮次与文件名在同一事务内产出：只有 commit 成功，``round_no`` 才是对外承诺
    的轮次，回滚后不会留下"涨了却没交付"的空号。
    """
    try:
        await _db_exec(
            db,
            "UPDATE schemes SET export_round = COALESCE(export_round, 0) + 1 WHERE id=?",
            (scheme_id,),
            what="_bump_export_round_and_trace",
        )
        cur = await db.execute("SELECT export_round FROM schemes WHERE id=?", (scheme_id,))
        row = await _db_fetch_one(cur, what="_bump_export_round_and_trace 回读")
        round_no = int(row[0]) if (row and row[0]) else 1
        export_filename = _build_export_filename(display_name, fmt, round_no)
        await _write_export_review_trace(db, scheme_id, fmt, round_no, export_filename, note=note)
        await db.commit()
        return round_no, export_filename
    except Exception as e:  # noqa: BLE001 —— 事务失败统一回滚，语义见 docstring
        logger.warning("缓存命中路径「轮次+留痕」事务失败（整体回滚，本次不交付缓存产物）: %s", e)
        try:
            await db.rollback()
        except Exception:
            logger.debug("「轮次+留痕」事务回滚也失败（放弃清理）", exc_info=True)
        return None


def _export_name_headers(filename: str, round_no: int) -> dict:
    """把最终导出文件名透传给前端（下载名唯一事实源）。

    前端 blob 下载时 `a.download` 会覆盖响应头里的 Content-Disposition，
    故必须显式回传；Value 用百分号编码，避免非 ASCII 文件名写 HTTP 头时
    触发 latin-1 编码异常（与 `_attachment_disposition` 同理）。
    """
    from urllib.parse import quote

    return {
        "X-Export-Filename": quote(filename, safe=""),
        "X-Export-Round": str(round_no),
    }


def _global_facts_status_headers(status: dict | None) -> dict:
    """将事实读取状态以 latin-1 安全编码回传，便于客户端观测降级原因。"""
    if not status:
        return {}
    from urllib.parse import quote

    return {
        "X-Global-Facts-Status": quote(json.dumps(status, ensure_ascii=False, separators=(",", ":")), safe=""),
    }


def _export_audit_headers(audit: dict) -> dict:
    """把导出前内容体检结果回传前端（占位符 / HTML 残留 / 投标用语 / 未闭合围栏）。

    响应头 Value 必须是 latin-1 安全字节，JSON 里含中文会直接抛
    ``UnicodeEncodeError``（与 X-Export-Filename 同一原因）→ 百分号编码。
    无风险时不发头，避免前端统计条出现无意义的空提示。
    """
    if not audit or not audit.get("items"):
        return {}
    from urllib.parse import quote

    return {"X-Content-Audit": quote(json.dumps(audit, ensure_ascii=False), safe="")}


# ---------------------------------------------------------------------------
# ✅ R176 · D2-2（2026-11-04）：内容体检结果旁车（``<产物>.audit.json``）
# ---------------------------------------------------------------------------
# 动机：`audit_content` 在命中路径上是纯浪费（M 档 26.5 ms / L 档 65.7 ms
# median，安静窗口 §12.2）。旁车把"这一版内容已经体检过"的结论存到产物旁，
# 命中时直接读回，省掉整轮正则扫描；但**不能**因此不发 X-Content-Audit 头
# （契约不变，见 `_export_audit_headers`）。
#
# 设计约束（缺一不可，改动前先读完）：
#   1) 同生共死：旁车只与"被缓存行引用的产物"配套。读侧只在
#      `_cache_row_viable` 通过后才调用；`_prune_export_cache` /
#      `_gc_orphan_exports` 删产物时连带删旁车（见 `_remove_export_artifact`）。
#      反向（旁车在、缓存行不在）永不生效 —— 没有行就永远不会走到读旁车。
#   2) 崩溃安全：旁车自带 `content_hash` + `fences_fixed` + 章节数三重校验。
#      半截/截断 JSON 解析失败、字段缺失、值不匹配 → 一律**静默回退重算**，
#      绝不抛错、也绝不把坏数据当结论。
#   3) 原子性：临时文件写完再 `os.replace`，不会出现半截旁车。
#   4) 失效：两条**独立**的版本轴，各自只对自己负责——
#      · `_AUDIT_SIDECAR_VERSION` = **载荷格式版本**：只在旁车结构（字段增删/改名、
#        `audit` 语义）变更时才 +1；格式没变就别动。
#      · `audit_rules_fingerprint()` = **审计规则指纹**：规则表（key/标签/级别/
#        正则与 flags）与三个参与判定的函数（`audit_content` /
#        `_count_unclosed_chart_fences` / `is_fence_close`）的 AST 摘要。
#        改规则/改判定代码 → 指纹自动变 → 旧旁车读侧不匹配 → 回退重算。
#        因此**不再需要**任何人记得手动 bump 版本号（R177 收口）。
_AUDIT_SIDECAR_VERSION = 1
_AUDIT_SIDECAR_SUFFIX = ".audit.json"


def _audit_sidecar_path(result_path) -> Path:
    return Path(f"{result_path}{_AUDIT_SIDECAR_SUFFIX}")


def _remove_export_artifact(result_path) -> None:
    """删除产物并连带删除旁车（保留策略 / 孤儿回收的唯一删除出口）。"""
    try:
        Path(result_path).unlink(missing_ok=True)  # R177-DELETE-EXEMPT: 唯一删除出口（产物本体）
    except OSError:
        pass
    try:
        _audit_sidecar_path(result_path).unlink(missing_ok=True)  # R177-DELETE-EXEMPT: 唯一删除出口（配套旁车）
    except OSError:
        pass


async def _read_audit_sidecar(result_path, prep: dict, content_hash: str):
    """读旁车；任何不可用情形（缺失/坏 JSON/版本或校验不符）→ ``None``。"""
    sidecar = _audit_sidecar_path(result_path)

    def _read():
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return None
            if data.get("v") != _AUDIT_SIDECAR_VERSION:
                return None
            if data.get("audit_fp") != audit_rules_fingerprint():
                return None
            if data.get("content_hash") != content_hash:
                return None
            if int(data.get("fences_fixed") or 0) != int(prep.get("fences_fixed") or 0):
                return None
            if int(data.get("sections") or 0) != len(prep.get("sections") or []):
                return None
            audit = data.get("audit")
            if not isinstance(audit, dict) or not isinstance(audit.get("items"), list):
                return None
            return audit
        except Exception as e:
            # 缺失 / 半截 JSON / 编码损坏 / 字段类型畸形（如 "abc" 转 int 失败）
            # —— 全部回退重算，绝不把坏数据当结论、也绝不抛给业务
            logger.warning("审计旁车不可用（回退实时体检）: path=%s err=%s", sidecar.name, e)
            return None

    # 小文件（KB 级）但落在外置盘（J:）上，偶发抖动可达数十 ms；
    # 命中路径每请求都会读，故与其它文件 IO 同口径下沉线程池，绝不压事件循环。
    return await asyncio.to_thread(_read)


async def _write_audit_sidecar(result_path, prep: dict, content_hash: str) -> None:
    """原子写旁车（临时文件 + os.replace）；失败只告警，不影响导出交付。"""
    audit = prep.get("content_audit")
    if not isinstance(audit, dict):
        return
    sidecar = _audit_sidecar_path(result_path)
    payload = {
        "v": _AUDIT_SIDECAR_VERSION,
        "audit_fp": audit_rules_fingerprint(),
        "content_hash": content_hash,
        "fences_fixed": int(prep.get("fences_fixed") or 0),
        "sections": len(prep.get("sections") or []),
        "audit": audit,
    }

    def _write():
        tmp = sidecar.with_name(f"{sidecar.name}.{uuid.uuid4().hex}.tmp")
        try:
            sidecar.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
            os.replace(tmp, sidecar)
        finally:
            try:
                tmp.unlink(missing_ok=True)  # R177-DELETE-EXEMPT: 仅清理本次临时文件
            except OSError:
                pass

    try:
        await asyncio.to_thread(_write)
    except Exception as e:
        logger.warning("审计旁车写入失败（不影响导出，下次命中将回退重算）: %s", e)


async def _resolve_content_audit(prep: dict, *, content_hash: str, cached_path=None) -> dict:
    """内容体检的唯一解析出口：优先命中旁车，否则实时重算。

    调用时机：``_prepare_export`` 不再无条件体检；DOCX / PDF 两条链路在
    **缓存判定之后**各调用一次 —— 命中传 ``cached_path``（可读旁车），
    未命中不传（必然重算）。命中路径的旁车若不可用，行为与未命中完全相同
    （重算），因此不存在"命中但少了审计提示"的退化路径。
    """
    audit = None
    if cached_path:
        audit = await _read_audit_sidecar(cached_path, prep, content_hash)
    if audit is None:
        audit = await asyncio.to_thread(
            audit_content, prep["sections"], fences_fixed=int(prep.get("fences_fixed") or 0)
        )
    logger.info("%s", _format_audit_log(audit))
    return audit


# ---------------------------------------------------------------------------
# ✅ BUG 修复：从章节标题中剥离已有的编号前缀（防止与 heading_gen 双重编号）
# ---------------------------------------------------------------------------
# ✅ 2026-10-05（D4 · 死副本清理）：唯一实现为 services/content_blocks.py 的
#    `_strip_title_number`（已从 content_blocks 导入），其内部委托 numbering 的
#    `strip_outline_numbering`；本模块此前的 `_TITLE_NUM_STRIP_RES` 副本无任何引用，删除。


def _section_number_prefix(full_title: str) -> str:
    """从已格式化的章节标题中提取编号前缀，用于正文内子标题的嵌套编号。

    例：
        '第一章 施工组织设计' -> '1'
        '1.2 进度计划'       -> '1.2'
        '1.1.1 立面设计'     -> '1.1.1'
        '（一） 设计标准'     -> ''（中文序号前缀无法嵌套，子标题直接用自身序号）
    无编号标题返回 ''。

    ✅ BUG 修复：中文数字解析此前用 str.replace 逐项替换，顺序敏感导致
    "二十一"→"2十一"→"210一"→"2101"（错误 2101）；"二十三"→"2三"→"23"（碰巧正确）。
    现改为按位置解析的正规实现，覆盖"零"到"九十九九"等常见组合。
    """
    s = (full_title or "").strip()
    if not s:
        return ""
    m = re.match(r"^第([一二三四五六七八九十百千零\d]+)[章节]", s)
    if m:
        cn = m.group(1)
        return _cn_numeral_to_int(cn)
    m = re.match(r"^(\d+(?:\.\d+)*)", s)
    if m:
        return m.group(1)
    return ""


def _cn_numeral_to_int(cn: str) -> str:
    """中文数字串转整数（阿拉伯数字）。

    支持：
      - 纯数字 "一二三四五六七八九"  → 1~9
      - 十位组合 "十/十一/二十/二十一/九十九" → 10~99
      - 百位组合 "一百/一百二十/三百五十八" → 100~999
      - 千位组合 "一千/一千零二十"  → 1000~9999
      - 混入阿拉伯数字 "20" / "12"  直接 int()
    解析失败时返回原字符串（不做静默吞错）。
    """
    if not cn:
        return ""
    # 纯阿拉伯数字
    if cn.isdigit():
        return cn
    # 若串中同时出现阿拉伯数字，用正则切分后逐段解析再拼接
    if re.search(r"\d", cn):
        parts = re.split(r"([0-9]+)", cn)
        out = ""
        for p in parts:
            if p == "":
                continue
            if p.isdigit():
                out += p
            else:
                # 中文部分按位解析
                val = _cn_pure_to_int(p)
                if val is not None:
                    out += str(val)
        return out if out else cn

    val = _cn_pure_to_int(cn)
    return str(val) if val is not None else cn


def _finalize_heading_runs(paragraph) -> None:
    """✅ 编号统一（2026-09-26）：强制标题段落所有 run 不倾斜。

    样式层已置 italic=False（_build_docx_sync），但标题文本内嵌的 Markdown 行内标记
    （如 *斜体*）会在 run 级把局部设成倾斜，与「所有标题不得倾斜」要求冲突。
    此处兜底，保证 L1~L7 标题（含正文子标题）任何 run 都不会倾斜。
    """
    for _r in paragraph.runs:
        _r.italic = False


def _get_or_add_appendix_heading_style(doc):
    """获取/创建「附录组标题」自定义样式：外观继承 Heading 2，但不进 TOC 域。

    ✅ 编号统一（2026-09-25 · E4 修复）：附录组标题此前用 ``doc.add_heading(level=2)``，
    自带 Heading 2 的大纲级别（outlineLvl=1）→ 被 ``TOC \\o "1-N"`` 域收录，
    且该标题无章节编号，目录页出现「有目录项、无编号」的孤条目。
    本样式基于 Heading 2（继承字体/字号/加粗外观），但显式把大纲级别覆盖为
    9（Word 的「正文文本」，不参与 TOC 收录）。同一文档内幂等（重复导出/重试
    时复用已存在的样式，不重复创建）。
    """
    from docx.enum.style import WD_STYLE_TYPE
    from docx.oxml.ns import qn

    name = "Appendix Group Heading"
    # 遍历按名称匹配：python-docx 每次按名查找（doc.styles[name]）都会新建包装
    # 对象（StyleFactory），不能依赖对象同一性；遍历同时天然规避 KeyError 流程。
    for st in doc.styles:
        if st.type == WD_STYLE_TYPE.PARAGRAPH and st.name == name:
            return st
    style = doc.styles.add_style(name, WD_STYLE_TYPE.PARAGRAPH)
    style.base_style = doc.styles["Heading 2"]
    # 显式覆盖继承的大纲级别为 9（正文文本）—— TOC \o "1-N" 只收 1~N 级
    ppr = style.element.get_or_add_pPr()
    for ol in ppr.findall(qn("w:outlineLvl")):
        ppr.remove(ol)
    ol = ppr.makeelement(qn("w:outlineLvl"), {qn("w:val"): "9"})
    ppr.append(ol)
    return style


# ---------------------------------------------------------------------------
# ✅ R179（2026-11-05）：渲染前内容块结构收口（不改解析层，纯渲染期视图）
# ---------------------------------------------------------------------------
# 第1轮报告取证的三类结构缺陷都无法在解析层安全修复（解析层归正文生成会话）：
#   · 标题与正文粘连成一段（"1）国家法律国家法律是…"）；
#   · 标题段落吞掉整句正文（标题里出现"。"）；
#   · Markdown 管道符表格带着表题前缀以**普通段落**漏进成稿（首字符不是 ``|``
#     时解析层不认表格）。
# 本函数只在渲染前把这类块重组为可渲染结构，**不修改源内容、不写回缓存**。
_RE_PIPE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_RE_PIPE_WITH_CAPTION = re.compile(r"^(?P<cap>[^|\n]{1,80}?)\s*\|(?P<rest>.+)\|\s*$")
_RE_TABLE_CAPTION_LINE = re.compile(r"^\s*表\s*(?:[0-9０-９一二三四五六七八九十]+[0-9.\-–—]*|[：:])")
_RE_ADHERED_TITLE = re.compile(r"^([\u4e00-\u9fffA-Za-z0-9]{2,14})\1")
#: ✅ R181（2026-11-06）：列表项字母序前缀的唯一实现在
#: ``content_blocks.strip_subitem_ordinal_prefix``，本模块已删除本地副本，
#: 避免落库侧与导出侧两套正则漂移。


def _split_pipe_cells(raw: str) -> list[str]:
    """按 GFM 竖线切分单元格（先保护转义竖线 ``\\|``）."""
    safe = raw.replace("\\|", "\x00").strip()
    if safe.startswith("|"):
        safe = safe[1:]
    if safe.endswith("|"):
        safe = safe[:-1]
    return [c.replace("\x00", "|").strip() for c in safe.split("|")]


def _make_table_block(cells: list[str], caption: str = "", extra_rows: list[list[str]] | None = None) -> dict:
    """由单元格行合成解析层同构的 ``table`` 块（表头 + 对齐行 + 数据行）。"""
    n = len(cells)
    sep = "| " + " | ".join(["---"] * n) + " |"
    lines = ["| " + " | ".join(cells) + " |", sep]
    for row in extra_rows or []:
        lines.append("| " + " | ".join(row) + " |")
    block = {"type": "table", "lines": lines}
    if caption:
        block["caption"] = caption
    return block


def _looks_like_table_caption(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    return bool(_RE_TABLE_CAPTION_LINE.match(t)) or (t.endswith("表") and len(t) <= 40)


def _normalize_render_blocks(blocks: list[dict]) -> list[dict]:
    """把粘连标题 / 管道符段落重排为规范块；无坏形态时返回内容等价的新列表。"""
    out: list[dict] = []
    i = 0
    total = len(blocks)
    while i < total:
        b = blocks[i]
        if not isinstance(b, dict):
            out.append(b)
            i += 1
            continue
        bt = b.get("type")

        # ① 列表项「标题正文粘连」：重复前缀（2~14 字）后还有 ≥8 字正文。
        if bt == "list_item":
            txt = str(b.get("text") or "").strip()
            # ⓪ `a、a、正文` 双写：循环剥离字母序前缀（渲染层会重新编号）。
            #    ✅ R181（2026-11-06）：下沉到 content_blocks 的唯一实现，
            #    本模块不再保留第二套正则/循环（两侧同语料输出一致）。
            stripped = strip_subitem_ordinal_prefix(txt)
            if stripped != txt:
                b = dict(b, text=stripped)
                txt = stripped
            m = _RE_ADHERED_TITLE.match(txt)
            if m:
                rest = txt[m.end() :]
                if len(rest) >= 8 and rest[0] not in "，。；：、）)":
                    out.append(dict(b, text=m.group(1)))
                    out.append({"type": "paragraph", "text": rest})
                    i += 1
                    continue

        # ② 标题段落吞正文：标题含句号 → 句号前为标题、其后 ≥8 字降为正文。
        if bt == "heading":
            txt = str(b.get("text") or "")
            if "。" in txt:
                head, _, rest = txt.partition("。")
                if head.strip() and len(rest.strip()) >= 8:
                    out.append(dict(b, text=head.strip()))
                    out.append({"type": "paragraph", "text": rest.strip()})
                    i += 1
                    continue

        if bt == "paragraph":
            txt = str(b.get("text") or "").strip()

            # ③ 连续纯管道行（首字符为 ``|``）→ 原生表格；吸收紧邻上文表题。
            if _RE_PIPE_ROW.match(txt):
                rows: list[list[str]] = []
                j = i
                while j < total:
                    nb = blocks[j]
                    if not isinstance(nb, dict) or nb.get("type") != "paragraph":
                        break
                    ntxt = str(nb.get("text") or "").strip()
                    if not _RE_PIPE_ROW.match(ntxt):
                        break
                    cells = _split_pipe_cells(ntxt)
                    if len(cells) < 2:
                        rows = []
                        break
                    rows.append(cells)
                    j += 1
                if rows and all(len(r) == len(rows[0]) for r in rows):
                    cap = ""
                    if (
                        out
                        and out[-1].get("type") == "paragraph"
                        and _looks_like_table_caption(str(out[-1].get("text") or ""))
                    ):
                        cap = str(out[-1].get("text") or "").strip()
                        out.pop()
                    out.append(_make_table_block(rows[0], cap, rows[1:]))
                    i = j
                    continue

            # ④ 「表题前缀 | 表头 | …」段落：后随表格则并入，否则单独成表。
            mcapb = _RE_PIPE_WITH_CAPTION.match(txt) if "|" in txt else None
            if mcapb:
                cells = _split_pipe_cells(mcapb.group("rest"))
                cap = mcapb.group("cap").strip()
                if len(cells) >= 2:
                    nxt = blocks[i + 1] if i + 1 < total else None
                    if isinstance(nxt, dict) and nxt.get("type") == "table":
                        nxt_lines = nxt.get("lines") or []
                        if len(nxt_lines) >= 2:
                            old_hdr = _split_pipe_cells(nxt_lines[0])
                            if len(old_hdr) == len(cells):
                                merged = dict(nxt)
                                sep = "| " + " | ".join(["---"] * len(cells)) + " |"
                                merged["lines"] = [
                                    "| " + " | ".join(cells) + " |",
                                    sep,
                                    nxt_lines[0],
                                    *nxt_lines[2:],
                                ]
                                merged["caption"] = cap or nxt.get("caption", "")
                                out.append(merged)
                                i += 2
                                continue
                    out.append(_make_table_block(cells, cap))
                    i += 1
                    continue

        out.append(b)
        i += 1
    return out


def _build_docx_sync(
    out_path: str,
    scheme: dict,
    roots: list[dict],
    children_map: dict[str, list],
    chart_lookup: dict[tuple[str, str], str],
    rendered_bytes: dict[tuple[str, str], object],
    blocks_cache: dict[str, list[dict]],
    font_name: str,
    font_size: int,
    page_header: str,
    page_footer: str,
    show_page_number: bool,
    show_title_page: bool,
    show_toc: bool,
    bidder_name: str,
    heading_styles: dict,
    page_break_before_chapter: bool = True,
    line_spacing: float = 1.15,
    # ✅ 追加参数统一放末尾（历史调用方多为位置参数，插入中间会错位）
    page_number_style: str = "simple",
    toc_depth: int = 3,
    margins: dict | None = None,
    cover_info: dict | None = None,
    image_bytes: dict | None = None,
    global_facts: list | None = None,
    # ✅ 追加参数统一放末尾（历史调用方多为位置参数，插入中间会错位）
    chart_fail_placeholder: bool = False,
    # ✅ 2026-09-22 引入（对齐 OpenBidKit 章框 heading_border 能力）：
    #    一级章节标题加底部边框，增强版式层次感。默认 False（旧导出无边框，向后兼容）。
    heading_border: bool = False,
    # ✅ D-3（2026-10-01）：附录补充数据源（知识库条目 / 解析提取成果）。
    #    默认 None → 不渲染任何补充附录（向后兼容，与旧产物逐字一致）。
    #    数据来源由 `export_appendix_sources` 开关控制（默认 False）。
    appendix_sources: list | None = None,
    # ✅ 2026-10-02（第二十三轮）：专项施工方案法定前置表单。
    #    dict 承载 4 个独立开关（编制说明 / 审批表 / 专家论证报告 / 图纸附件清单），
    #    合并为单参数而非 4 个位置参数：保持签名尾部稳定、便于同批扩展。
    #    缺省 None 或全 False → 不渲染任何前置表单，产物与旧版逐字一致。
    scheme_forms: dict | None = None,
    # ✅ R99（2026-10-17）：前置「自动补齐未闭合围栏」修好的围栏数。
    #    缺省 0 = 不传时行为与改造前**逐字一致**（既有位置参数调用方零影响）。
    #    在构建线程内累加进线程局部 fix_stats，随返回值进 X-Fix-Stats 响应头。
    fences_fixed: int = 0,
    # ✅ R106 FIX-1（2026-10-18）：本次导出的取消事件（threading.Event，
    #    由 export_docx/export_pdf 登记并经 _build_docx_task 末尾透传）。
    #    缺省 None = 无取消登记（离线/测试直调），行为与改造前逐字一致。
    #    命中在「章节写入循环段边界」抛 ExportCancelled 提前退出（不 save）。
    cancel_event: threading.Event | None = None,
) -> None:
    """同步构建 DOCX 并保存（在 asyncio.to_thread 中调用，避免阻塞事件循环）。

    ✅ 性能优化：旧实现 Document 创建、样式设置、write_section 递归渲染全部在
    事件循环线程同步执行。大文档（50+章）write_section 是 CPU 密集操作，
    会阻塞事件循环 3-10 秒，期间其他请求无法响应。现整体移到线程池。
    """
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml.ns import qn
    from docx.shared import Cm, Pt, RGBColor

    from app.services.ai.heading_standard import HeadingNumberingGeneratorV2

    # ✅ R106 FIX-1：入口检查点 —— 排队期间已被取消则一个块都不写。
    _check_export_cancelled(cancel_event)

    # ✅ R99（2026-10-17）：把前置围栏补齐数并入本次构建的修复统计。
    #    必须在**本构建线程内**累加（_FIX_STATS_LOCAL 是 threading.local，
    #    _prepare_export 在事件循环线程写入对它不可见），随返回值进 X-Fix-Stats。
    if fences_fixed:
        _fix_stats()["fences_fixed"] += int(fences_fixed)

    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = font_name
    style.font.size = Pt(font_size)
    style.element.rPr.rFonts.set(qn("w:eastAsia"), font_name)
    # ✅ P0 修复（2026-09-27）：同标题样式，删除 Normal 上残留的主题字体属性，
    #    否则 w:asciiTheme 会反向覆盖上面 style.font.name 写入的显式字体。
    for _theme_attr in ("w:asciiTheme", "w:hAnsiTheme", "w:eastAsiaTheme", "w:cstheme"):
        if style.element.rPr.rFonts.get(qn(_theme_attr)) is not None:
            del style.element.rPr.rFonts.attrib[qn(_theme_attr)]
    # ✅ 增强：正文行距与段后间距（旧实现完全未设置，长文档通读体验差）
    style.paragraph_format.line_spacing = line_spacing
    style.paragraph_format.space_after = Pt(0)

    for lvl in range(1, 8):
        hs = heading_styles[lvl]
        h_style = doc.styles[f"Heading {lvl}"]
        h_style.font.name = hs["font_name"]
        h_style.font.size = Pt(hs["font_size"])
        h_style.font.bold = hs["bold"]
        h_style.font.italic = False
        # ⚠️ 需求「标题字体不倾斜」：这里显式写 italic=False（覆盖 python-docx
        #    内置 Heading 样式可能继承的斜体），是防倾斜的第一道闸。
        h_style.element.rPr.rFonts.set(qn("w:eastAsia"), hs["font_name"])
        # ✅ P0 修复（2026-09-27 · 主题字体反覆盖显式字体）：
        #    python-docx 内置 Heading N 样式带 w:asciiTheme / w:hAnsiTheme /
        #    w:eastAsiaTheme / w:cstheme 四个**主题字体属性**。按 OOXML 规则，
        #    同名显式属性（w:ascii / w:hAnsi / w:eastAsia）与 *Theme 属性共存时
        #    **Theme 优先**——上面 h_style.font.name = ... 写入的 w:ascii 会被
        #    w:asciiTheme="majorHAnsi" 覆盖，用户在导出配置里选的标题字体
        #    （黑体/宋体）在 Word 中**静默失效**。
        #    这里显式删除四个主题属性，使显式字体真正生效（删除是安全的：
        #    没有主题属性时 Word 直接使用 w:ascii/w:hAnsi/w:eastAsia）。
        _rfonts = h_style.element.rPr.rFonts
        for _theme_attr in ("w:asciiTheme", "w:hAnsiTheme", "w:eastAsiaTheme", "w:cstheme"):
            if _rfonts.get(qn(_theme_attr)) is not None:
                del _rfonts.attrib[qn(_theme_attr)]
        # ✅ 增强：标题与后文同页（避免标题孤行落在页尾），并留段前段后间距
        hpf = h_style.paragraph_format
        hpf.keep_with_next = True
        hpf.space_before = Pt(12 if lvl <= 2 else 8)
        hpf.space_after = Pt(6)
        # ✅ 默认：所有标题行距与正文一致（1.15 倍）
        hpf.line_spacing = line_spacing

    # ✅ 增强：声明"打开时更新域"，Word 自动生成目录并计算页码；
    #    否则用户不按 F9 就只能看到"请按 F9 更新域"的占位文本。
    if show_toc or show_page_number:
        _enable_update_fields_on_open(doc)

    # 封面页
    if show_title_page:
        for _ in range(6):
            doc.add_paragraph()
        title_p = doc.add_paragraph()
        title_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        # ✅ 2026-10-11（文档质量报告 P0-B）：封面标题走「清洗残留 + 项目名兜底」
        #    的单一出口，避免残留串上封面或标题空白。
        # ✅ BUG 修复（2026-11-03 · R175 · BUG-2）：封面标题/投标人改用
        #    `_safe_add_run`（先剥 XML 非法控制字符再建 run）——用户表单里
        #    混入 \x00-\x08 等字符时，旧实现 add_run 抛 ValueError → 整单 500。
        _set_run_font(_safe_add_run(title_p, _display_scheme_name(scheme, global_facts)), font_name, 28, bold=True)
        if bidder_name:
            bidder_p = doc.add_paragraph()
            bidder_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            _set_run_font(_safe_add_run(bidder_p, bidder_name), font_name, 16)
        date_p = doc.add_paragraph()
        date_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        _set_run_font(date_p.add_run(datetime.now().strftime("%Y年%m月%d日")), font_name, 14)
        # ✅ 增强 v7：封面项目信息表（工程名称 / 编制单位 / 方案编号 / 版本 等）
        if cover_info:
            _add_cover_info_table(doc, cover_info, font_name)
        # ✅ BUG 修复：旧实现封面后不分页，正文第一章直接接排在封面页上
        doc.add_page_break()

    # 目录页
    if show_toc:
        toc_title = doc.add_paragraph()
        toc_title.alignment = WD_ALIGN_PARAGRAPH.CENTER
        _set_run_font(toc_title.add_run("目录"), font_name, 16, bold=True)
        toc_p = doc.add_paragraph()
        _add_toc_field(toc_p, toc_depth)
        # ✅ BUG 修复：旧实现目录后不分页（正文接排在目录页），
        #    而在没有封面时会先 add_page_break —— 首页是一张空白页。
        doc.add_page_break()

    # ✅ 2026-10-02（第二十三轮）：专项施工方案法定前置表单。
    #    顺序放在封面 / 目录之后、正文之前 —— 与实际报审件形态一致
    #    （封面 → 目录 → 编制说明 → 审批表 → 专家论证报告 → 图纸清单 → 正文）。
    #    全部默认关闭；每张表独立分页，关闭任一开关不影响其他。
    _forms = scheme_forms if isinstance(scheme_forms, dict) else {}
    if _forms:
        for _key, _fn in (
            ("compilation_note", _add_compilation_note_page),
            ("approval", _add_scheme_approval_table),
            ("expert_review", _add_expert_review_form),
        ):
            if not _forms.get(_key):
                continue
            try:
                if _key == "compilation_note":
                    _add_prelim_page_title(doc, "编制说明", font_name)
                    _add_compilation_note_page(doc, scheme, cover_info, font_name)
                else:
                    _fn(doc, scheme, cover_info, font_name)
            except Exception:  # fail-soft：前置表单失败不阻断导出正文
                logger.warning("导出前置表单失败（已跳过）: %s", _key, exc_info=True)
            doc.add_page_break()
        if _forms.get("drawing_appendix"):
            try:
                _add_drawing_appendix_page(doc, scheme, font_name)
            except Exception:
                logger.warning("导出图纸附件清单失败（已跳过）", exc_info=True)
            doc.add_page_break()

    sec = doc.sections[0]
    # ✅ 增强 v7：页边距可配置（单位 cm，缺省 2.5）
    if margins:
        _set_margins(sec, margins)
    # ✅ 增强：有封面时首页不显示页眉页脚（封面顶着页眉很不专业）
    if show_title_page:
        sec.different_first_page_header_footer = True

    # 页眉
    # ✅ 2026-10-11（文档质量报告 P0-B）：页眉过一遍模板残留清洗。
    #    残留串置空后等价于「未配置页眉」，不编造替代内容（不预填红线）。
    page_header = _sanitize_template_residue_title(page_header)
    if page_header:
        hp = sec.header.paragraphs[0]
        hp.alignment = WD_ALIGN_PARAGRAPH.CENTER
        _set_run_font(_safe_add_run(hp, page_header), font_name, 9)
        _set_paragraph_borders(hp, bottom=("single", 6, "AAAAAA"))

    # 页脚
    if page_footer or show_page_number:
        fp = sec.footer.paragraphs[0]
        fp.alignment = WD_ALIGN_PARAGRAPH.CENTER
        if page_footer:
            _set_run_font(_safe_add_run(fp, page_footer), font_name, 9)
            fp.add_run("   ")
        if show_page_number:
            if page_number_style == "page_of_total":
                _set_run_font(fp.add_run("第 "), font_name, 9)
                _set_run_font(_add_page_field(fp), font_name, 9)
                _set_run_font(fp.add_run(" 页 / 共 "), font_name, 9)
                _set_run_font(_add_numpages_field(fp), font_name, 9)
                _set_run_font(fp.add_run(" 页"), font_name, 9)
            else:
                _set_run_font(fp.add_run("- "), font_name, 9)
                _set_run_font(_add_page_field(fp), font_name, 9)
                _set_run_font(fp.add_run(" -"), font_name, 9)

    heading_gen = HeadingNumberingGeneratorV2()
    figure_counters: dict[str, int] = {}
    table_counters: dict[str, int] = {}
    rendered_charts: set[tuple] = set()
    # ✅ 加固（2026-10-06 · D1）：按**实际渲染顺序**给每个章节编号（roots 排序 +
    # 深度优先），用于「兜底借图」时确定性地选**文档顺序上最近的前一张同类图**
    # （旧实现依赖 chart_predictions 行插入顺序，借谁不可控）。
    # ✅ RISK-175-01（R175）：与预渲染段共用同一排序口径（DFS 文档序）。
    order_rank: dict[str, int] = _compute_doc_order_rank(roots, children_map)
    # ✅ 加固（D1）：本次构建期间「跨章借图」的 (章节, 类型, 借自) 去重集合
    _BORROWED_CHARTS: set[tuple] = set()
    # P0-3 性能优化：倒排索引一次构建，write_section 兜底查找从 O(N) 降为 O(1)
    chart_type_index = _build_chart_type_index(chart_lookup, order_rank)
    # 正文首个标题之前不允许分页，否则封面/目录之后会多出一张空白页
    body_state = {"started": False}
    # 中文正文首行缩进 2 字符（随正文字号缩放，旧实现固定 0.74cm）
    body_first_line_indent = Cm(round(font_size * 2 * 0.0352778, 3))

    def write_section(sec: dict, parent_id: str = ""):
        # ✅ 健壮性修复（2026-09-20）：level 来自 DB（TEXT 列），脏数据可能为
        # 字符串或空值；下游 min(level, 7)、heading_gen.format_heading(level, ...)
        # 均按 int 使用，非 int 会直接抛异常导致整份文档导出失败。
        try:
            level = int(sec.get("level") or 1)
        except (TypeError, ValueError):
            level = 1
        level = max(1, min(level, 8))
        title = strip_outline_numbering(sec.get("title", ""))
        content = sec.get("content", "")
        sec_id = sec["id"]
        pure_title = _strip_title_number(title)
        full_title = heading_gen.format_heading(level, pure_title, parent_id)
        section_prefix = _section_number_prefix(full_title)

        h = doc.add_paragraph(style=f"Heading {min(level, 7)}")
        _add_runs_with_inline_format(h, full_title)
        _finalize_heading_runs(h)
        # ✅ 增强：一级章节另起一页（专项方案通用版式）
        if page_break_before_chapter and level <= 1 and body_state["started"]:
            h.paragraph_format.page_break_before = True
        # ✅ 2026-09-22 引入（对齐 OpenBidKit 章框 heading_border）：一级章节标题加
        #    底部边框增强层次感；默认关闭，旧导出无边框、行为完全不变。
        if heading_border and level <= 1:
            _set_paragraph_borders(h, bottom=("single", 12, "1F4E79"))
        body_state["started"] = True

        if content:
            blocks = blocks_cache.get(sec_id) or _parse_content_blocks(content)
            blocks = _strip_duplicate_leading_title(blocks, title, full_title)
            # ✅ 2026-11-05（第1轮报告 §12「正文重复/标题重复」根因收口）：
            #    E3 降级后正文子标题与 DB 章节标题同文本（如章节"3.4 通风空调
            #    系统施工工艺"的正文里又有内容标题"1）、通风空调系统施工工艺"）
            #    → 成稿同页出现两个同义标题。既有 _strip_duplicate_leading_title
            #    只剥【开头连续】重复；此处把**任意位置**与章节标题同文本的
            #    内容子标题一并剔除（编号由程序统一分配，重复标题无信息量）。
            _sec_title_norms = {_norm_heading_text(t) for t in (title, full_title, pure_title)}
            _sec_title_norms.discard("")
            blocks = [
                b
                for b in blocks
                if not (b.get("type") == "heading" and _norm_heading_text(b.get("text", "")) in _sec_title_norms)
            ]
            # ✅ R179：渲染前结构收口（粘连标题拆分 / 管道符段落转原生表格）；
            #    无坏形态时内容与顺序完全不变（解析层与缓存均不受影响）。
            blocks = _normalize_render_blocks(blocks)
            sub_counters: dict[int, int] = {}
            # ✅ E3（2026-09-25）：有 DB 子章节 + 配置开启 → 正文子标题降级为节内
            # body 命名空间（1）/ a、…），与 DB 子章节彻底隔离。无子女或配置关
            # 闭 → 保持旧点分编号，由后续计数器前移（L2991+）处理撞号。
            try:
                from app.config import settings as _s

                _demote = _s.body_subheading_demote_with_children
            except Exception:
                _demote = True
            has_children_demoted = bool(children_map.get(sec_id)) and _demote
            for block in blocks:
                if block["type"] == "heading":
                    md_lv = block.get("level", 1)
                    # ✅ 2026-11-05（"a、a、"双重前缀根因）：源文子标题自带的
                    #    字母序前缀在重编号前剥离（strip_outline_numbering 不认
                    #    字母序），否则 E3 降级重新分配的 "a、" 与源前缀叠加。
                    pure = strip_subitem_ordinal_prefix(_strip_title_number(block.get("text", "")))
                    text, style_lv = _compute_subheading(
                        section_prefix, level, md_lv, sub_counters, pure, sec_id, has_children=has_children_demoted
                    )
                    block["_fixed_text"] = text
                    block["_heading_style"] = style_lv

            # ✅ 增强 v7：有序列表自动连续编号（修复 AI 输出跳号/重复编号，
            #    如 "1. 2. 1. 2." 或断号 "1. 3. 5."）。遇到非列表块即重置序列。
            # ✅ 优化 v9：① 有序序号与标记样式绑定 —— 样式变化（（一）→ 1、）视为
            #    新序列从 1 重新计数；② 无序子项不再清零序号 ——
            #    "1. 顶层一 / - 子项 / 2. 顶层二" 曾把第二个顶层项重排成 "1."。
            # ✅ 修复（2026-10-03 · 列表样式被拍平）：有序列表序号改为**按缩进层级
            #    分层独立计数**（`ordered_stack`，每层 [marker, seq]）。旧实现用单一全局
            #    计数器，导致嵌套有序列表子项被拍平成一条贯穿所有层级的连续序列
            #    （"1. 顶层 / 2. 子项 / 3. 子项 / 4. 顶层二"），子项未在父级内重新从 1。
            #    契约全部沿用：① 遇非列表块整栈清空（重置序列）；② 标记样式变化该层从
            #    1 重计；③ 无序子项不碰栈（父级序号续接）；④ 重复/跳号自动连续编号；
            #    ⑤ 中文枚举符外观保留。零新增依赖 / 零数据迁移 / 零配置项。
            ordered_stack: list[list] = []  # [[marker, seq], ...]，下标 = indent_lvl
            for block in blocks:
                if block["type"] != "list_item":
                    # ✅ 遇非列表块 → 重置整条有序序列（沿用既有契约）
                    ordered_stack = []
                # ✅ P0 逐块 fail-soft（2026-10-06）：把「渲染一个内容块」做成事务。
                #    快照 = 文档 body 元素集合 + 图号/表号/去重集合；块渲染抛异常时
                #    全部回滚（不留半截表格、不留已占图号），再降级为纯文本。
                _blk_rb = _DocxBodyRollback(doc)
                _fig_snap = dict(figure_counters)
                _tbl_snap = dict(table_counters)
                _dedup_snap = set(rendered_charts)
                try:
                    if block["type"] == "table":
                        # ✅ 新增：表格题注「表 {章号}-{序号} 表名」（表题在表格上方）
                        caption = (block.get("caption") or "").strip()
                        chapter_num = _figure_chapter_num(heading_gen)
                        t_key = f"ch{chapter_num}"
                        table_counters[t_key] = table_counters.get(t_key, 0) + 1
                        table_num = f"{chapter_num}-{table_counters[t_key]}"
                        if caption:
                            # ✅ 2026-10-17（表号叠写根因 B）：剥离源文自带「表 X-Y」前缀
                            _add_table_caption(
                                doc, f"表 {table_num} {_strip_leading_table_number(caption)}", font_name, font_size
                            )
                        else:
                            # ✅ R51（2026-10-07）：表格无表名行 → 成稿表格裸奔无表题。
                            #    R50 仅补 WARNING 供排障，但用户成稿里 38 张表零编号零表题。
                            #    现从表格首行（GFM header）自动生成表题：取前 3 列名拼接，
                            #    确保每张表都有编号与标题。计数器始终递增（修复编号断裂）。
                            _tbl_lines = block.get("lines") or []
                            _first = (_tbl_lines[0] if _tbl_lines else "").strip()
                            _auto_caption = ""
                            if _first and "|" in _first and not re.match(r"^[\|\s\-:]+$", _first):
                                cells = [c.strip() for c in _first.split("|") if c.strip()]
                                if len(cells) >= 2:
                                    _auto_caption = "、".join(cells[:3])
                                elif cells:
                                    _auto_caption = cells[0]
                            if not _auto_caption:
                                _auto_caption = "相关数据"
                            # ✅ 2026-10-17（表号叠写根因 B）：AI 把表题连同编号写进首格
                            #    （首格「表 6-1 …」）→ 拼出的自动表题带编号 → 与导出编号叠写。
                            #    此处剥离首格自带的「表 X-Y」，只保留统一编号。
                            _auto_caption = _strip_leading_table_number(_auto_caption)
                            _add_table_caption(doc, f"表 {table_num} {_auto_caption}", font_name, font_size)
                            logger.warning(
                                "导出：章节 %s 表格无表名行，自动生成表题: 表 %s %s（首行: %s）",
                                str(sec_id)[:8],
                                table_num,
                                _auto_caption,
                                _first[:80],
                            )
                        _add_table_from_markup(doc, block["lines"], font_name)
                    elif block["type"] == "chart":
                        chart_type = block["chart_type"]
                        # ✅ 加固（2026-10-06 · D1）：取码收敛到唯一出口
                        #    _resolve_chart_code，并回传「是否跨章借图 + 借自谁」。
                        mermaid_code, borrowed_from = _resolve_chart_code(
                            block, sec_id, chart_type, chart_lookup, chart_type_index, order_rank
                        )
                        if mermaid_code:
                            # ✅ BUG 修复：去重键必须包含代码本体 —— 旧实现按 (章节, 类型) 去重，
                            #    同一章节内两张**不同**的同类图表（如两张不同的流程图）会被静默吞掉一张。
                            render_key = (sec_id, chart_type, _norm_code(mermaid_code))
                        else:
                            # ✅ 修复（2026-09-20 深度审查）：兜底渲染去重键必须含章节维度与代码本体。
                            #    旧键 ("fallback", chart_type, "") 与具体章节无关 —— 当两个及以上章节
                            #    的同类型图表都无注册代码时，只有第一张被渲染，其余被 rendered_charts
                            #    静默丢弃（图号直接缺失、无任何系统侧信号）。加 sec_id 后每章各自渲染；
                            #    加代码本体避免同章节两张不同代码的兜底图互相吞并。
                            render_key = (sec_id, "fallback", chart_type, _norm_code(mermaid_code))
                        if borrowed_from:
                            # 跨章借图此前**零信号**（成稿里 B 章可能出现 A 章的流程图）。
                            # 此处按「每 (章节,类型) 只记一次」累加，供
                            # X-Chart-Render-Stats / 前端提示 / 导出日志如实上报。
                            _BORROWED_CHARTS.add((sec_id, chart_type, borrowed_from))
                            logger.info(
                                "导出：章节 %s 的 %s 图无本章节登记，兜底借用第 %s 章的同类图"
                                "（跨章借图会在 X-Chart-Render-Stats 中计入 fallback_borrowed）",
                                sec_id[:8],
                                chart_type,
                                borrowed_from[:8],
                            )
                        if not mermaid_code:
                            # ✅ 2026-09-25：图表被跳过时同步回收其孤儿引导语
                            #    （"如下图所示："留在成稿里却无图 = 图文不连贯）。
                            _pop_orphan_lead_in(doc)
                            continue
                        if render_key in rendered_charts:
                            # ✅ 2026-09-25：同代码第二次出现（去重跳过）→ 该处引导语
                            #    同样指向一张不会出现的图，一并回收。
                            _pop_orphan_lead_in(doc)
                            continue
                        rendered_charts.add(render_key)
                        _, default_title = _CHART_TYPE_MAP.get(chart_type, (None, chart_type))
                        # ✅ 图题优先用块自带业务标题（载荷 title / 引导语 / Mermaid title 指令），
                        #    仅在其缺失时退回类型通用名 —— 旧实现一律用通用名，导致
                        #    「图 4-1 劳动力配置计划」挂在本章实为流程图的正文之下。
                        fig_title = (block.get("title") or "").strip() or default_title
                        # ✅ 契约变更（2026-09-19，配置开关 chart_fail_placeholder）：
                        #    默认**跳过渲染失败的图表**且不占用图号 —— 交付文档里出现
                        #    「[图 X-Y … — 渲染失败]」红字报错比少一张图更糟（实测取证：
                        #    第 4 章 4 处占位，评审观感极差）。"不静默"承诺并不因此失效，
                        #    而是转移到系统侧信号：X-Chart-Render-Stats / X-Content-Audit
                        #    响应头、导出日志、/export/check 预检与坏缓存守卫全部保留。
                        #    传 chart_fail_placeholder=True 可恢复 V7.0 的红字占位形态
                        #    （排查"到底是哪张图没出来"时使用）。
                        _chart_bytes = rendered_bytes.get((chart_type, mermaid_code))
                        if not _chart_ok(_chart_bytes) and not chart_fail_placeholder:
                            logger.warning(
                                "章节 %s 图表渲染失败（type=%s），已跳过且不占用图号"
                                "（交付文档不写红字占位；传 chart_fail_placeholder=true 可恢复占位）",
                                sec_id[:8],
                                chart_type,
                            )
                            # ✅ 2026-09-25：跳过该图 → 回收其孤儿引导语（图文连贯）
                            _pop_orphan_lead_in(doc)
                            continue
                        chapter_num = _figure_chapter_num(heading_gen)
                        fig_key = f"ch{chapter_num}"
                        figure_counters[fig_key] = figure_counters.get(fig_key, 0) + 1
                        fig_num = f"{chapter_num}-{figure_counters[fig_key]}"
                        # ✅ 图号虚跳收口（2026-10-03）：插入成功才保留图号；
                        #    插入失败（格式合法但文件损坏等）→ 回退图号 + 回收孤儿引导语，
                        #    避免「占号却无图」的错号（placeholder 模式保留红字、图号照占）。
                        _ok = _add_inline_chart_from_bytes(
                            doc,
                            chart_type,
                            _chart_bytes,
                            fig_num,
                            fig_title,
                            font_name,
                            font_size,
                            placeholder=chart_fail_placeholder,
                        )
                        if not _ok and not chart_fail_placeholder:
                            figure_counters[fig_key] = figure_counters.get(fig_key, 0) - 1
                            _pop_orphan_lead_in(doc)
                    elif block["type"] == "ai_image":
                        # ✅ AI 配图（文生图）**未生成**占位：导出时整块跳过。
                        #    v17 起占位块已先经 _auto_generate_ai_image_blocks 自动生成并
                        #    改写为 image 块 —— 走到本分支说明自动生成失败（或开关关闭）。
                        #    设计依据（对齐「宁缺毋滥」与交付质量）：
                        #    · 绝不写裸 JSON（旧实现把它当 code 块 → 提示词泄漏进成稿）；
                        #    · 不做红字占位（AI 配图失败/缺失时只保留图题的既有语义）；
                        #    · 不占用图号（否则出现「图 1-2」却无图，编号虚跳）。
                        logger.warning(
                            "章节 %s 存在未生成的 AI 配图占位（%s），本次导出已跳过且不占图号；"
                            "请检查图像模型配置（导出期自动生成失败）",
                            sec_id[:8],
                            block.get("title") or "未命名",
                        )
                        # ✅ 2026-09-25：AI 配图未生成 → 同样回收孤儿引导语
                        _pop_orphan_lead_in(doc)
                    elif block["type"] == "image":
                        # ✅ AI 配图（文生图）：正文里的 ![说明](url)，导出时插入真实位图
                        #
                        # ✅ BUG 修复（2026-09-26 · 图号虚跳）：旧实现**先占图号、
                        #    后插图**，与同文件 `_chart_ok` docstring 写明的原则
                        #    （"跳过 vs 占位的决策必须发生在**占用图号之前**"）
                        #    自相矛盾 —— 配图字节缺失（``image_bytes`` 未命中 url，
                        #    例如导出期自动生成失败 / 缓存失效）时，
                        #    ``_add_illustration_from_bytes`` 只写图题、不写位图，
                        #    但计数器已 +1 → 交付文档出现「图 1-1」缺失、编号却从
                        #    「图 1-2」起跳的**图号虚跳**。
                        #    现与图表分支同口径：先判可用性；不可用则不占图号、
                        #    不留孤立图题，并回收孤儿引导语（图文连贯）。
                        _img_bytes = (image_bytes or {}).get((block.get("url") or "").strip())
                        if not _chart_ok(_img_bytes):
                            logger.warning(
                                "章节 %s 存在无位图的 AI 配图引用（%s），本次导出已跳过" "且不占用图号（避免图号虚跳）",
                                sec_id[:8],
                                (block.get("alt") or "").strip() or "未命名",
                            )
                            _pop_orphan_lead_in(doc)
                            continue
                        chapter_num = _figure_chapter_num(heading_gen)
                        fig_key = f"ch{chapter_num}"
                        figure_counters[fig_key] = figure_counters.get(fig_key, 0) + 1
                        # ✅ 图号虚跳收口（2026-10-03）：插入成功才保留图号；
                        #    插入失败（格式合法但文件损坏）→ 回退图号 + 回收孤儿引导语。
                        _ok = _add_illustration_from_bytes(
                            doc,
                            _img_bytes,
                            f"{chapter_num}-{figure_counters[fig_key]}",
                            (block.get("alt") or "").strip(),
                            font_name,
                            font_size,
                        )
                        if not _ok:
                            figure_counters[fig_key] = figure_counters.get(fig_key, 0) - 1
                            _pop_orphan_lead_in(doc)
                    elif block["type"] == "heading":
                        h_lv = min(block.get("_heading_style", min(level + block.get("level", 1), 7)), 7)
                        h = doc.add_paragraph(style=f"Heading {h_lv}")
                        _add_runs_with_inline_format(h, block.get("_fixed_text", block.get("text", "")))
                        _finalize_heading_runs(h)
                    elif block["type"] == "list_item":
                        # ✅ P1-1 修复（2026-10-07 · 列表样式拍平）：
                        #    旧实现将列表项渲染为普通段落（Normal 样式），无缩进、
                        #    无悬挂缩进，Word 不识别为列表项。现改为：
                        #    ① 使用 "List Paragraph" 样式（Word 识别为列表项）
                        #    ② 动态计算悬挂缩进（基于前缀宽度，不再硬编码 -0.6cm）
                        #    ③ indent_lvl 口径统一（ordered_stack 与 left_indent 共用）
                        #    向后兼容：不传 indent 参数时默认 0 级缩进（与旧行为一致）。
                        indent_lvl = block.get("indent", 0) // 2
                        if block.get("ordered"):
                            marker = block.get("marker", "ascii")
                            # ✅ 分层栈维护：裁剪到当前层、补齐缺失层级（容忍跳级）；
                            #    当前层标记样式变化 → 该层 seq 归 0（视为新序列从 1）
                            if indent_lvl < len(ordered_stack):
                                ordered_stack = ordered_stack[: indent_lvl + 1]
                            while len(ordered_stack) <= indent_lvl:
                                ordered_stack.append([marker, 0])
                            entry = ordered_stack[indent_lvl]
                            if entry[0] != marker:
                                entry[0] = marker
                                entry[1] = 0
                            entry[1] += 1
                            # ✅ 按原标记样式渲染（（一）/（1）/ 1、不再被拍平成 "1. "）
                            prefix = _ordered_prefix(entry[1], marker)
                        else:
                            prefix = "• "
                        # ✅ 使用 "List Paragraph" 样式（Word 识别为列表项）
                        _lp_style = _ensure_list_paragraph_style(doc)
                        # ✅ 2026-11-05（第1轮报告 §4「1）国家法律」×2 / "a、a、"根因收口）：
                        #    导出按序重编号（契约④），源文残留的同类序号前缀必须剥离，
                        #    否则渲染成 "1）1）国家法律" / "1. a、吊顶" 叠加。
                        #    只剥两类：① 与本次分配前缀**逐字相同**的数字前缀（必然是
                        #    AI 双写）；② 字母序前缀（重编号路径不保留源序号）。数字前缀
                        #    数值不同时不剥 —— 防止误伤 "3、5号楼立面" 这类数字开头的实质内容。
                        _item_text = str(block.get("text") or "")
                        if block.get("ordered"):
                            if _item_text.startswith(prefix):
                                _item_text = _item_text[len(prefix) :].lstrip()
                            _item_text = strip_subitem_ordinal_prefix(_item_text)
                        p = doc.add_paragraph(style=_lp_style)
                        _add_runs_with_inline_format(p, prefix + _item_text)
                        lpf = p.paragraph_format
                        # ✅ 左缩进：每级 0.74 cm（基础缩进 + 嵌套层级）
                        lpf.left_indent = Cm(0.74 * (indent_lvl + 1))
                        # ✅ 动态悬挂缩进：基于前缀实际宽度（不再硬编码 -0.6cm）
                        _hang_w = _estimate_prefix_width(prefix)
                        lpf.first_line_indent = Cm(-_hang_w)
                        lpf.space_after = Pt(2)
                    elif block["type"] == "code":
                        _add_code_block(doc, block.get("lines", []))
                    elif block["type"] == "quote":
                        for qline in (block.get("text") or "").split("\n"):
                            qp = doc.add_paragraph()
                            _add_runs_with_inline_format(qp, qline)
                            qpf = qp.paragraph_format
                            qpf.left_indent = Cm(0.74)
                            qpf.right_indent = Cm(0.5)
                            for r in qp.runs:
                                if not r.font.name:
                                    _set_run_font(r, "楷体", 10.5)
                                r.font.color.rgb = RGBColor(0x44, 0x44, 0x44)
                            _set_paragraph_shading(qp, "F7F7F7")
                            _set_paragraph_borders(qp, left=("single", 12, "A6A6A6"))
                    elif block["type"] == "hr":
                        # ✅ 修复（2026-09-20）：AI 生成正文常以 --- 作为章节分隔符，
                        # 旧实现将其画成一条下边框横线，正式交付文档中观感突兀。
                        # 工程方案文档不应含装饰性分隔线 → 直接跳过（解析仍识别 hr，
                        # 仅渲染侧丢弃；前端预览 <hr> 不受影响）。
                        continue
                    else:
                        p = doc.add_paragraph()
                        _add_runs_with_inline_format(p, block.get("text", ""))
                        p.paragraph_format.first_line_indent = body_first_line_indent
                        p.paragraph_format.space_after = Pt(3)
                except Exception as _blk_err:
                    # ⚠️ P0 逐块 fail-soft（2026-10-06）：块渲染器抛异常时
                    #    图号/表号/去重集合全部回滚（不留半截表格、不留已占图号），
                    #    文档元素回滚后降级为纯文本，整份导出继续。
                    _blk_rb.rollback()
                    figure_counters.clear()
                    figure_counters.update(_fig_snap)
                    table_counters.clear()
                    table_counters.update(_tbl_snap)
                    rendered_charts.difference_update(rendered_charts - _dedup_snap)
                    _degrade_block_to_text(doc, block, font_name, font_size)

            # ✅ 修复（2026-09-20，第 8 轮交付文档取证）：内容子标题与 DB 子章节编号碰撞。
            # 现象：本节**内容**含 "## 项目概况 / ## 建筑概况" → 渲染为
            # "1.1 项目概况 / 1.2 建筑概况"；随后本节的 **DB 子章节**又从 1.1 起算
            # → "1.1 工程规模与结构形式"。同一文档出现成对的 "1.1/1.1"、"2.1~2.4/2.1~2.3"
            # （实测第 1/2/9/10 章均有多处）。
            # 根因：内容子标题（_compute_subheading，{本节编号}.N）与 DB 子章节
            # （heading_gen，同层级同样从 .1 起算）在同一 "X.N" 命名空间各用一套
            # 计数器，互不感知。
            # 修复：内容渲染完后，若本节还有 DB 子章节，则把编号引擎在子章节层级
            # 的计数器前移 N（N = 内容中落在子章节命名空间的子标题数），子章节从
            # N+1 续排；其后代（含各自正文内子标题前缀）随之整体右移，编号恢复唯一。
            # 注意：仅当 level>=2 时前移有意义——正文子标题的命名空间是「{本节编号}.N」
            # （如 "2.1"），恰与 DB 子章节同级（L3）；level=1 章节的正文子标题 "1.1"
            # 属于更深的命名空间，与其 L2 子章节（编号 "1" / "2"）并不冲突，前移反而
            # 会把子章节错误改成 "18 / 19"。
            # ✅ E3（2026-09-25）：降级激活时正文子标题已走 body 命名空间（1）/ a、…），
            # 不再占用 X.X 命名空间，此计数器前移变为多余——显式门控避免无意义操作。
            if level >= 2 and children_map.get(sec_id) and not has_children_demoted:
                n_sub = _count_child_namespace_subheadings(blocks, section_prefix)
                if n_sub:
                    _idx = min(level, 7)  # 子章节层级 = level + 1（0 基索引恰为 level）
                    heading_gen.parent_ids[_idx] = sec_id
                    heading_gen.counters[_idx] = n_sub
                    for _i in range(_idx + 1, 8):
                        heading_gen.counters[_i] = 0
                        heading_gen.parent_ids[_i] = None

        children = sorted(children_map.get(sec_id, []), key=_section_sort_key)
        for child in children:
            _write_section_safe(child, sec_id)

    def _write_section_safe(sec: dict, parent_id: str = ""):
        """章节级 fail-soft（块级之外的第二道防线）。

        ✅ 2026-10-06：块级 fail-soft 只覆盖**内容块**；章节标题渲染与子章节
        递归仍在块循环之外。此处再包一层，使「一章渲染失败」只丢该章，
        其余章节、正文后续内容与两个附录照常产出（此前任一异常即整份导出 500，
        用户拿不到任何产物）。
        """
        try:
            return write_section(sec, parent_id)
        except Exception as e:
            st = _fix_stats()
            st["block_render_failed"] = st.get("block_render_failed", 0) + 1
            logger.error(
                "导出：章节「%s」渲染失败（已跳过该章，其余内容不受影响）: %s",
                str(sec.get("title") or "")[:40],
                e,
                exc_info=True,
            )
            return None

    for root in sorted(roots, key=_section_sort_key):
        # ✅ R106 FIX-1：章节写入循环段边界取消检查点 —— 命中即抛
        #    ExportCancelled（在 doc.save 之前退出，临时产物不落地）。
        #    必须放在循环体而非 _write_section_safe 内：后者 fail-soft
        #    会吞掉任何 Exception（含 ExportCancelled），取消信号将失效。
        _check_export_cancelled(cancel_event)
        _write_section_safe(root)

    # ✅ 修复（2026-09-17）：导出「项目关键事实」附录（全局事实维度）。
    #    此前交付文档完全不含事实维度（工程量/材料设备/规范依据/模拟值待确认项等），
    #    与「专项方案必须呈现关键事实」的要求相悖。按 group_title 分组渲染为表格。
    if global_facts:
        try:
            doc.add_page_break()
            ap = doc.add_paragraph()
            ap.alignment = WD_ALIGN_PARAGRAPH.CENTER
            _set_run_font(ap.add_run("附录：项目关键事实"), font_name, 16, bold=True)
            grouped: dict[str, list] = {}
            for f in global_facts:
                grouped.setdefault(f.get("gt") or "其他事实", []).append(f)
            apx_style = _get_or_add_appendix_heading_style(doc)
            for gt, items in grouped.items():
                # ✅ 编号统一（2026-09-25 · E4 修复）：附录组标题改用自定义样式 ——
                #    旧实现 doc.add_heading(level=2) 使分组标题带 Heading 2 大纲级别，
                #    会进入 TOC 域（TOC \o "1-N" 收录大纲级别 1~N）且不带任何编号，
                #    目录页出现「有目录项、无编号」的孤条目。自定义样式继承 Heading 2
                #    外观，但大纲级别显式压为 9（正文文本），不再进目录。
                # ✅ BUG-2（R175）：组标题 gt 来自事实数据，可能含 XML 非法控制字符
                gh = doc.add_paragraph(style=apx_style)
                _set_run_font(_safe_add_run(gh, gt), font_name, heading_styles[2]["font_size"], bold=True)
                tbl = doc.add_table(rows=1, cols=2)
                try:
                    tbl.style = "Table Grid"
                except Exception:
                    logger.debug("附录表格套用 Table Grid 样式失败（保留默认样式）", exc_info=True)
                hc = tbl.rows[0].cells
                _set_run_font(hc[0].paragraphs[0].add_run("名称"), font_name, 10.5, bold=True)
                _set_run_font(hc[1].paragraphs[0].add_run("取值 / 内容"), font_name, 10.5, bold=True)
                for f in items:
                    rc = tbl.add_row().cells
                    _set_run_font(_safe_add_run(rc[0].paragraphs[0], str(f.get("title") or "")), font_name, 10.5)
                    _appendix_cell_paragraph(
                        rc[1],
                        str(f.get("content") or ""),
                        font_name,
                        first=True,
                        # ✅ R179：第二列剥离与第一列重复的事实名称前缀（只留取值）
                        strip_name=str(f.get("title") or ""),
                    )
        except TypeError as e:
            # ✅ P0 修复（2026-09-27）：TypeError 是**编程错误**（如把 bold 当位置参数
            #    传给 keyword-only 形参），不是"附录渲染不出来"的可恢复异常。
            #    旧实现把它和真正的运行期波动一起降级为 WARNING，于是整张
            #    「项目关键事实」附录静默消失、导出却报"成功"——最隐蔽的一类缺陷。
            #    现按 ERROR 上报，保留原降级（不阻断正文导出）。
            logger.error("导出：渲染全局事实附录遇到编程错误（附录将缺失，" "正文不受影响）: %s", e, exc_info=True)
        except Exception as e:
            logger.warning("导出：渲染全局事实附录失败（降级跳过，不影响正文）: %s", e)

    # ✅ D-3（2026-10-01）：附录补充数据源（知识库条目 / 解析提取成果）。
    #    旧实现这两类上游成果**从未进入交付文档**（全仓唯一消费点分别是
    #    正文生成注入 sse_handlers:1515 与 doc_pipeline:132），用户维护的知识库
    #    与解析提取成果在导出产物里完全不可见 —— 属于数据链断点。
    #    默认关闭（export_appendix_sources=False）：appendix_sources 为空 →
    #    不渲染、产物与旧版逐字一致（向后兼容）。
    if appendix_sources:
        try:
            doc.add_page_break()
            ap = doc.add_paragraph()
            ap.alignment = WD_ALIGN_PARAGRAPH.CENTER
            _set_run_font(ap.add_run("附录：参考资料与提取成果"), font_name, 16, bold=True)
            apx_style = _get_or_add_appendix_heading_style(doc)
            for group in appendix_sources:
                gtitle = str(group.get("title") or "")
                items = group.get("items") or []
                if not items:
                    continue
                gh = doc.add_paragraph(style=apx_style)
                _set_run_font(_safe_add_run(gh, gtitle), font_name, heading_styles[2]["font_size"], bold=True)
                tbl = doc.add_table(rows=1, cols=2)
                try:
                    tbl.style = "Table Grid"
                except Exception:
                    logger.debug("附录表格套用 Table Grid 样式失败（保留默认样式）", exc_info=True)
                hc = tbl.rows[0].cells
                _set_run_font(hc[0].paragraphs[0].add_run("名称"), font_name, 10.5, bold=True)
                _set_run_font(hc[1].paragraphs[0].add_run("内容 / 要点"), font_name, 10.5, bold=True)
                for it in items:
                    rc = tbl.add_row().cells
                    _set_run_font(_safe_add_run(rc[0].paragraphs[0], str(it.get("name") or "")), font_name, 10.5)
                    _appendix_cell_paragraph(
                        rc[1],
                        str(it.get("value") or ""),
                        font_name,
                        first=True,
                        # ✅ R179：同一重复名称口径，名称列已展示则内容列不再重复
                        strip_name=str(it.get("name") or ""),
                    )
        except TypeError as e:
            # 与事实附录同口径：TypeError 是编程错误，按 ERROR 上报（否则附录
            # 静默消失而导出报成功，是最隐蔽的一类缺陷）。
            logger.error("导出：渲染补充附录遇到编程错误（附录将缺失，正文不受影响）: %s", e, exc_info=True)
        except Exception as e:
            logger.warning("导出：渲染补充附录失败（降级跳过，不影响正文）: %s", e)

    # 注意：局部变量勿命名为 `_fix_stats`，否则会遮蔽同名模块级函数
    # ✅ 2026-11-05：落盘前 lint 守卫（修复/检出计数并入本次统计）
    fix_stats = _log_fix_stats()
    fix_stats.update(_docx_final_lint(doc))
    # ✅ 加固（D1）：把「本次构建跨章借图」的明细回传给调用方，写进
    #    X-Chart-Render-Stats（前端提示 + 审计）。加法式：dict 里多一个键，
    #    既有渲染统计键语义一字未动。
    borrowed = [
        {"section_id": sid, "chart_type": ct, "borrowed_from": src} for (sid, ct, src) in sorted(_BORROWED_CHARTS)
    ]
    if borrowed:
        logger.warning(
            "导出：本次有 %d 处图表使用了**跨章借图**（该章节没有自己的图表登记，"
            "借用了同类图）—— 成稿图文可能与章节内容不符，请回查对应章节的图表登记",
            len(borrowed),
        )
    # ✅ R109（2026-10-19 · P0）：落盘前统一归一 OOXML 严格序列。
    #    任何手写 XML 的插入位置疏漏（如 w:shd 追加到 w:vAlign 之后）都在此兜住，
    #    否则 WPS 会因 CT_* 序列违例直接拒绝打开整份成稿。
    _normalize_docx_strict_sequence(doc)
    doc.save(out_path)
    # ✅ P2-1 修复（2026-10-07）：导出落盘日志缺失。旧实现 doc.save() 后无任何
    #    日志记录，用户从日志中无法追溯成稿何时落盘、落在何处。现补 INFO 日志，
    #    含文件路径、大小与章节数，供运维与排障使用。
    try:
        _fsize = out_path.stat().st_size if out_path.exists() else 0
        logger.info(
            "导出：DOCX 已落盘（路径=%s · 大小=%d bytes · 章节数=%d · 表=%d · 图=%d）",
            out_path,
            _fsize,
            fix_stats.get("sections", 0) if isinstance(fix_stats, dict) else 0,
            fix_stats.get("tables", 0) if isinstance(fix_stats, dict) else 0,
            fix_stats.get("figures", 0) if isinstance(fix_stats, dict) else 0,
        )
    except Exception:
        logger.info("导出：DOCX 已落盘（路径=%s）", out_path)
    if borrowed:
        fix_stats = dict(fix_stats or {})
        fix_stats["chart_fallback_borrowed"] = len(borrowed)
        fix_stats["chart_fallback_borrowed_details"] = borrowed[:20]
    return fix_stats


# ---------------------------------------------------------------------------
# ✅ 重构：DOCX / PDF 两条导出链路共用同一套"准备逻辑"
# ---------------------------------------------------------------------------
# 旧实现中 export_docx 与 export_pdf 各自复制了一份"config 解析 + 章节树构建 +
# 图表取码 + 预渲染"（约 140 行近乎逐行重复）。两份实现的漂移已经造成真实缺陷：
# export_docx 已改用 chart_payload.extract_chart_payload 统一解析，
# 而 export_pdf 仍停留在 json.loads(...).get("mermaid_code")，于是
# "信封 + data" 形态的 JSON 数据型图表（组织架构 / 劳动力 / 总平面 / 时间线）
# 在 PDF 中全部静默消失。此处收敛为单一实现，杜绝再次漂移。
_DEFAULT_HEADING_STYLES = {
    1: {"font_name": "宋体", "font_size": 14, "bold": True},
    2: {"font_name": "宋体", "font_size": 14, "bold": True},
    3: {"font_name": "宋体", "font_size": 14, "bold": True},
    4: {"font_name": "宋体", "font_size": 14, "bold": True},
    5: {"font_name": "宋体", "font_size": 12, "bold": False},
    6: {"font_name": "宋体", "font_size": 12, "bold": False},
    7: {"font_name": "宋体", "font_size": 12, "bold": False},
}


def _clamp_font_size(v, default: float) -> float:
    """字号钳制到 [5, 72] Pt，非法/越界值回退默认，防止脏预设导致导出 500。"""
    try:
        n = float(v)
    except (TypeError, ValueError):
        return default
    if n != n or n < 5 or n > 72:
        return default
    return n


def _load_heading_styles(config: dict) -> dict:
    """合并用户配置与默认值，返回 1~7 级标题样式（每级 font_name/font_size/bold）。

    ✅ 防御：heading_styles 子项非 dict、font_size 非数字/越界时静默回退默认，
    避免 merged.update/ Pt() 抛异常导致整个导出 500（脏预设可反复触发）。
    """
    raw_user = config.get("heading_styles", {})
    user = raw_user if isinstance(raw_user, dict) else {}
    styles: dict[int, dict] = {}
    for lvl in range(1, 8):
        merged = dict(_DEFAULT_HEADING_STYLES[lvl])
        user_lvl = user.get(str(lvl))
        if isinstance(user_lvl, dict):
            merged.update(user_lvl)
        merged["font_size"] = _clamp_font_size(merged.get("font_size"), _DEFAULT_HEADING_STYLES[lvl]["font_size"])
        styles[lvl] = merged
    return styles


def _parse_frontend_images(body: dict) -> tuple[dict[str, BytesIO], list[str]]:
    """解析前端提交的 mermaid.js 渲染结果（dataURL → BytesIO）。"""
    fe_images: dict[str, BytesIO] = {}
    fe_codes: list[str] = []
    for item in body.get("chart_images") or []:
        try:
            code = str(item.get("mermaid_code", "")).strip()
            png = str(item.get("png", ""))
            if not code or not png.startswith("data:image/png;base64,"):
                continue
            fe_images[_norm_code(code)] = BytesIO(base64.b64decode(png.split(",", 1)[1]))
            fe_codes.append(code)
        except Exception:
            continue
    return fe_images, fe_codes


def _validate_remote_image_url(url: str) -> str:
    """校验远程图片 URL，阻断 SSRF 目标并保留 localhost 兼容开关。"""
    from app.config import settings as _settings

    try:
        parsed = urlsplit(str(url or "").strip())
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise ValueError("图片 URL 格式无效") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("图片 URL 仅允许 HTTP(S)")
    if parsed.username or parsed.password:
        raise ValueError("图片 URL 不允许携带认证信息")

    hostname = parsed.hostname.rstrip(".").lower()
    try:
        addrinfos = socket.getaddrinfo(
            hostname,
            port or (443 if parsed.scheme.lower() == "https" else 80),
            type=socket.SOCK_STREAM,
        )
    except OSError as exc:
        raise ValueError("图片 URL 主机无法解析") from exc
    if not addrinfos:
        raise ValueError("图片 URL 主机无法解析")

    resolved: list[ipaddress._BaseAddress] = []
    for item in addrinfos:
        try:
            resolved.append(ipaddress.ip_address(item[4][0].split("%", 1)[0]))
        except ValueError as exc:
            raise ValueError("图片 URL 解析结果无效") from exc
    allow_localhost = bool(_settings.image_download_allow_localhost)
    for address in resolved:
        is_local = address.is_loopback
        if is_local and allow_localhost:
            continue
        if not address.is_global:
            raise ValueError("图片 URL 指向内网、链路本地或保留地址")
    return str(url)


async def _download_remote_image(
    url: str,
    *,
    max_bytes: int | None = None,
    timeout: float | None = None,
    max_redirects: int | None = None,
) -> bytes:
    """按有界策略下载远程图片；每一跳重定向都重新执行 URL 安全校验。"""
    from app.config import settings as _settings

    limit = int(max_bytes if max_bytes is not None else _settings.image_download_max_bytes)
    limit = max(1, limit)
    redirects_left = int(max_redirects if max_redirects is not None else _settings.image_download_max_redirects)
    redirects_left = max(0, min(redirects_left, 10))
    request_timeout = float(timeout if timeout is not None else _settings.image_download_timeout)
    current_url = _validate_remote_image_url(url)
    async with httpx.AsyncClient(timeout=request_timeout, follow_redirects=False, trust_env=False) as client:
        for _ in range(redirects_left + 1):
            async with client.stream("GET", current_url) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location", "")
                    if not location or _ == redirects_left:
                        raise ValueError("图片 URL 重定向次数超限")
                    current_url = _validate_remote_image_url(urljoin(current_url, location))
                    continue
                if response.status_code != 200:
                    raise ValueError(f"图片下载失败（HTTP {response.status_code}）")
                content_length = response.headers.get("content-length", "")
                if content_length:
                    try:
                        if int(content_length) > limit:
                            raise ValueError("图片响应体大小超过限制")
                    except ValueError as exc:
                        if "大小超过限制" in str(exc):
                            raise
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > limit:
                        raise ValueError("图片响应体大小超过限制")
                if not body:
                    raise ValueError("图片响应体为空")
                return bytes(body)
    raise ValueError("图片 URL 重定向次数超限")


async def _auto_generate_ai_image_blocks(
    sections: list[dict],
    blocks_cache: dict[str, list[dict]],
    config: dict,
    # ✅ R106 FIX-1：本次导出的取消事件（缺省 None = 无取消登记，行为逐字不变）。
    #    逐占位码检查：已取消 → 不再发起新的生图调用（在飞的一次跑完即止，
    #    协作式停止口径），由调用方在 gather 后统一抛 ExportCancelled。
    cancel_event: threading.Event | None = None,
) -> int:
    """AI 配图全自动化：正文内嵌的 ```` ```ai_image ```` 占位块在导出准备阶段**自动**
    生成真实图片并就地改写为 ``{"type":"image"}`` 块（走既有位图下载/嵌入/图号路径）。

    ✅ 产品约束（2026-09-22）：图表全部自动生成，**不设置人工配图/生图入口** ——
    旧流程需要用户在前端点「生成配图」按钮触发 /charts/generate-ai-image，
    现改为导出时按需自动生成（与需求「按需渲染：导出 DOCX 时实时调用渲染引擎」
    同一哲学）。/charts/generate-ai-image 端点保留（向后兼容），前端按钮已移除。

    行为细节：
    - 开关 ``ai_image_auto_generate``（导出 config，默认 **True**）；False 恢复旧行为
      （占位块在 write_section 整块跳过、不占图号）。
    - 生成复用 ``image_engine.generate_illustration_image``：MD5 成功缓存 + Provider
      降级链 + 失败短 TTL 缓存 —— 同一占位码重复导出命中缓存，不重复计费。
      导出路径**不做**提示词 AI 扩写（非确定性会导致缓存永不命中）。
    - 生成成功 → 块就地改写为 image 块（含 alt=图题）；生成失败/无 prompt →
      块保持 ai_image 占位，write_section 按既有语义整块跳过（不占图号、无红字）。

    Returns:
        成功改写为 image 块的数量。
    """
    if not bool(config.get("ai_image_auto_generate", True)):
        return 0
    from app.config import settings as _settings
    from app.services.ai.image_engine import generate_illustration_image

    _img_cfg = {
        "image_enabled": _settings.image_enabled,
        "image_api_key_enc": _settings.image_api_key,
        "image_base_url": _settings.image_base_url,
        "image_model": _settings.image_model,
        "image_default_size": _settings.image_default_size,
        "image_price_per_image": _settings.image_price_per_image,
    }
    # 按占位码分组（同码多块共享一次生成，全部改写）；每组只生成一次
    groups: dict[str, list[dict]] = {}
    for sec in sections:
        for block in blocks_cache.get(sec["id"]) or []:
            if block["type"] != "ai_image":
                continue
            code = str(block.get("code") or "")
            if not code:
                continue
            groups.setdefault(code, []).append(block)
    if not groups:
        return 0

    # G2（2026-09-30）：AI 配图全局预算分段择优。
    # max_ai_images<=0 关闭（默认），行为逐字不变；>0 时按文档位置把候选分段、
    # 段内择优，全文累计生图数不超过该值，避免前面章节把额度全部用完。
    #   ⚠️ 分段依据的是「占位码首次出现的章节下标」（sections 已是文档顺序），
    #      这样 20 个候选、限 6 张时，6 张会均匀分布在全文各段，而不是前几章独占。
    _max_ai = int(getattr(_settings, "max_ai_images", 0) or 0)
    if _max_ai > 0:
        from app.services.ai.image_engine import select_ai_image_codes

        def _order_of(code: str) -> int:
            # 该占位码首次出现的章节下标（保证按文档顺序分段）
            for i, sec in enumerate(sections):
                for blk in blocks_cache.get(sec["id"]) or []:
                    if blk.get("type") == "ai_image" and str(blk.get("code") or "") == code:
                        return i
            return len(sections)

        _keep = select_ai_image_codes(groups, _order_of, _max_ai)
        if len(groups) > _max_ai:
            logger.info(
                "AI 配图全局预算生效: 候选 %d 张 → 按文档分段择优保留 %d 张（max_ai_images=%d）",
                len(groups),
                len(_keep),
                _max_ai,
            )
        groups = {c: bs for c, bs in groups.items() if c in _keep}
        if not groups:
            return 0

    # 并发上限由统一配置控制；默认值为 2，非法值收敛到全局 AI 并发安全范围。
    try:
        _image_concurrency = int(_settings.image_max_concurrency)
    except (TypeError, ValueError):
        _image_concurrency = 2
    sem = asyncio.Semaphore(max(1, min(_image_concurrency, 5)))

    async def _gen(code: str, blocks: list[dict]) -> None:
        # ✅ R106 FIX-1：取消已请求 → 不再发起新的生图调用（计费边界检查点）。
        #    不在这里 raise：gather 的兄弟任务 raise 会引发未取回异常噪声，
        #    由调用方在全部任务早退后统一抛 ExportCancelled。
        if cancel_event is not None and cancel_event.is_set():
            return
        try:
            obj = json.loads(code)
        except (json.JSONDecodeError, TypeError, ValueError):
            obj = {}
        if not isinstance(obj, dict):
            obj = {}
        prompt = str(obj.get("prompt") or "").strip()
        if not prompt:
            logger.warning("AI 配图占位块缺少 prompt，导出时跳过")
            return
        style = str(obj.get("style") or "engineering_diagram")
        title = str(obj.get("title") or blocks[0].get("title") or "AI 配图")
        async with sem:
            try:
                url = await generate_illustration_image(None, prompt, style, "16:9", _settings.image_provider, _img_cfg)
            except Exception as e:
                logger.warning("AI 配图自动生成异常（导出降级跳过）: %s", e)
                return
        if not url:
            logger.warning("AI 配图自动生成未产出 URL（导出按占位跳过）")
            return
        for block in blocks:
            block["type"] = "image"
            block["url"] = url
            block["alt"] = title

    await asyncio.gather(*[_gen(c, bs) for c, bs in groups.items()])
    converted = sum(1 for bs in groups.values() for b in bs if b["type"] == "image")
    logger.info(
        "AI 配图自动生成: %d/%d 块改写成功，共 %d 个唯一占位码" "（失败占位块导出时跳过、不占图号）",
        converted,
        sum(len(bs) for bs in groups.values()),
        len(groups),
    )
    return converted


async def _query_global_facts(
    db,
    scheme_id: str,
    status: dict | None = None,
) -> list:
    """读取方案下「可注入正文」的全局事实（与正文/目录生成口径一致：
    has_conflict=0 且已审核确认 is_resolved=1），供导出渲染「项目关键事实」附录。

    ✅ 修复（2026-09-17）：导出此前完全不读取 global_facts，交付文档缺失事实维度；
    而 global_facts 变更会主动 invalidate_export_cache，说明设计意图是事实应进入产物。
    现补齐读取，并把事实纳入内容指纹，使事实变更真正触发缓存更新。

    ✅ 收敛去重（数据流审计 2026-09-23）：查询口径不再自维护，改用
    facts_extractor.build_injectable_facts_query，与 sse_handlers._load_facts_rows 共用同一段 SQL。
    """
    try:
        from app.services.facts_extractor import (
            FACTS_GT_COLUMN,
            build_injectable_facts_query,
            resolve_scheme_project_id,
        )

        project_id = await resolve_scheme_project_id(db, scheme_id)
        columns = f"{FACTS_GT_COLUMN}, title, content"
        sql, params = build_injectable_facts_query(scheme_id, project_id, columns)
        cur = await db.execute(sql, params)
        facts = [dict(r) for r in await _db_fetch_all(cur, what="_query_global_facts")]
        if status is not None:
            status.update({"ok": True, "code": "ok" if facts else "empty", "count": len(facts)})
        return facts
    except Exception as e:
        if status is not None:
            status.update({"ok": False, "code": "query_failed", "count": 0})
        logger.warning("导出：读取全局事实失败（降级跳过）: %s", e)
        return []


def _merge_chart_json_skip_stats(render_stats: dict, parse_diag: dict) -> None:
    """把「解析期被跳过的 chart-json 块」并入 ``X-Chart-Render-Stats``。

    ✅ 2026-11-05（第二轮日志驱动分析 P1）：旧实现里这些块只写一行 INFO，
    响应头 failed 仍为 0 —— 生成出来的 DOCX 确实缺图，统计却报「失败 0」，
    用户与排障者都看不到。口径：
      · ``chart_json_skipped`` 计入 ``failed``（缺图就是失败，与渲染失败同权重）；
      · 按原因拆分（未闭合 / 不可渲染）便于定位是生成截断还是模型乱写 type；
      · 沿用既有坏缓存守卫：failed > 0 → 本次产物不写缓存（否则指纹不变时
        会一直命中这份缺图产物）。
    无跳过时**不新增任何键**，旧响应头形状逐字节不变（向后兼容）。
    """
    skipped = int(parse_diag.get("chart_json_skipped") or 0)
    if not skipped:
        return
    render_stats["chart_json_skipped"] = skipped
    render_stats["failed"] = int(render_stats.get("failed") or 0) + skipped
    unclosed = int(parse_diag.get("chart_json_skipped_unclosed") or 0)
    unrenderable = int(parse_diag.get("chart_json_skipped_unrenderable") or 0)
    if unclosed:
        render_stats["chart_json_skipped_unclosed"] = unclosed
    if unrenderable:
        render_stats["chart_json_skipped_unrenderable"] = unrenderable
    logger.warning(
        "导出：%d 处 chart-json 块在解析期被跳过（未闭合 %d / 不可渲染 %d），" "已计入图表失败数（成稿缺图且不写缓存）",
        skipped,
        unclosed,
        unrenderable,
    )


def _count_undownloaded_image_blocks(
    sections: list[dict], blocks_cache: dict[str, list[dict]], image_bytes: dict[str, object]
) -> int:
    """统计「已改写为 image 块、但位图未取回」的配图数量（坏缓存守卫用）。

    ✅ P2 修复（2026-09-30 · 残缺文档被写入缓存 → 永久缺图的第二缺口）：
    ``_ai_pending`` 只统计「自动生图失败后残留的 ``ai_image`` 占位块」——
    若生图**成功**（占位块已改写为 ``image`` 块）、但随后按 URL 取回位图时
    下载失败 / 厂商返回非图片（见 ``_prepare_export`` 的 ``_fetch_image``），
    该图在 ``write_section`` 的 image 分支同样会被跳过（不占图号、无红字）。
    旧实现这段失败**不计入**坏缓存守卫 → 成稿缺图却被 INSERT 进 export_cache，
    服务恢复后因指纹不变而永远命中这份缺图缓存（与 2026-09-27 P1 修的
    "生图失败残留占位"是同一类缺陷，只差一步）。

    判据与 write_section 的 image 分支严格同口径：块 type=image 且 url 非空、
    且 url 不在已下载集合 image_bytes 中。返回计数（0 = 全部取回）。

    ⚠️ 不能因 image_bytes 为空就提前返回 0：下载**全部失败**时 image_bytes 恰为
    空 dict，而正文里存在待插图引用 —— 那正是守卫要拦的场景（空 = 一张都没取回，
    每张都会被 write_section 跳过）。

    纯函数：不依赖 DB / 网络，便于单测直接断言。
    """
    downloaded = set(image_bytes or {})
    n = 0
    for sec in sections or []:
        for blk in (blocks_cache or {}).get(sec.get("id")) or []:
            if blk.get("type") != "image":
                continue
            url = str(blk.get("url") or "").strip()
            if url and url not in downloaded:
                n += 1
    return n


# ---------------------------------------------------------------------------
# ✅ R176 · P1-3（2026-11-04）：事件循环上的**阻塞调用**统一下沉线程池。
# 参照仓库既有正确模式（``upload_outline.py:243`` 的 ``asyncio.to_thread``），
# 只包「一次同步调用」，不重构周边逻辑。下列 helper 全部**每次调用新建句柄**
# （BytesIO / open），因此不存在同一文件句柄或同一 PIL 对象跨线程复用：
#   · ``_verify_image_bytes``：PIL ``verify()`` 需整图解码校验（CPU）；
#   · ``_read_path_bytes``：PDF 整文件读；
#   · PDF 整文件写直接用 ``asyncio.to_thread(Path.write_bytes, ...)``。
# ---------------------------------------------------------------------------
def _verify_image_bytes(data) -> None:
    """PIL 完整性校验（阻塞；调用方必须放在 ``asyncio.to_thread`` 中）。

    ``data`` 是**本次下载独占**的 BytesIO，线程内只读校验、不 seek；
    返回后由事件循环侧 ``seek(0)`` 交给后续渲染 —— 跨线程不共享读写位置。
    非法图片抛异常（调用方按「不是合法图片」跳过）。
    """
    from PIL import Image as PILImage

    PILImage.open(data).verify()


def _read_path_bytes(path) -> bytes:
    """整文件读（阻塞；调用方必须放在 ``asyncio.to_thread`` 中）。

    ✅ 每次调用在**工作线程内**新建文件句柄，无跨线程句柄复用。
    """
    with open(path, "rb") as f:
        return f.read()


async def _prepare_export(
    scheme_id: str, body: dict, db, cancel_event: threading.Event | None = None, defer_audit: bool = False
) -> dict:
    """导出前的公共准备：解析配置 → 读取章节与图表 → 预渲染图表图片。

    DOCX 与 PDF 两条链路共用，避免"改了一处漏一处"（历史上正是如此丢失了
    PDF 里的 JSON 数据型图表）。

    ✅ R106 FIX-1：``cancel_event`` 存入 plan（``_cancel_event``），随
    「取数段 → 渲染段 → 构建线程」逐层透传；缺省 None = 无取消登记，
    各检查点自动短路（离线调用方/既有测试零影响）。

    ✅ R100（2026-10-17）：R99 把渲染段拆出 `_prepare_export_render` 后，本函数体内
    遗留四处**拆分继留死码**（`_settings` 悬空 import、`allow_pil_fallback`、
    `render_stats` 局部、`_get_cached_blocks` 闭包——渲染统计与块缓存闭包已由渲染段
    自建，本段只把空 `_blocks_cache` 装进 plan）。全部摘除，行为逐字不变。
    """
    config = body.get("config", {}) or {}

    def _float(key: str, default: float) -> float:
        try:
            return float(config.get(key, default))
        except (TypeError, ValueError):
            return default

    def _int(key: str, default: int) -> int:
        try:
            return int(config.get(key, default))
        except (TypeError, ValueError):
            return default

    # 统一渲染轨：接收前端 mermaid.js（与预览一致）渲染的 PNG。
    # 命中的图表直接嵌入（所见即所得）；未命中/失败的图表回退后端渲染轨。
    # ✅ R100：渲染策略注释与 allow_pil_fallback / render_stats 的构造已随
    #   渲染段迁至 `_prepare_export_render`（本段产出的 plan 只带空 render_stats 壳）。
    fe_images, fe_codes = _parse_frontend_images(body)

    cur = await db.execute("SELECT * FROM schemes WHERE id=?", (scheme_id,))
    # ✅ R168：方案存在性读取改严格读 —— 旧实现 fail-soft 把「数据库读失败」降级为
    #    无行 → 404「方案不存在」，运维难辨「真不存在」还是「库故障」（R102 日志审计
    #    同族误诊；连接故障在日志里只有一条 WARNING）。现 DBReadError → 503 可行动文案。
    try:
        scheme = await _db_fetch_one(cur, what="_prepare_export:schemes", strict=True)
    except DBReadError as e:
        raise HTTPException(503, "方案数据读取失败（数据库连接异常），请稍后重试") from e
    if not scheme:
        raise HTTPException(404, "方案不存在")
    scheme = dict(scheme)

    cur = await db.execute(
        # ✅ BUG 修复（2026-09-20）：sort_order 在不同写入路径下语义不一致
        # （落库为同级序号 / 拖拽重排为全局序号），同级重复序号时仅按 sort_order
        # 排序的行次不确定 → 章节顺序在两次导出间漂移，且内容指纹随之抖动
        # （缓存永远 miss、重复渲染）。统一加 level、id 作确定性兜底。
        #
        # ✅ R176 · D2-1（2026-11-04）：由 `SELECT *` 收窄为**命中路径必需列**。
        #    逐列核对结果（消费方 → 列）：
        #      · `_export_cache._content_fingerprint` 的 sections 维度：id / parent_id /
        #        sort_order / level / title / content（六列，缺一即指纹变）；
        #      · `_prepare_export` 自身：id（nodes / blocks_cache 键）、parent_id
        #        （建树）、sort_order + level（排序键）、title + content（空章判定、
        #        自动改写/围栏补齐、audit_content）；
        #      · `_prepare_export_render` / `_auto_generate_ai_image_blocks`：
        #        id（blocks_cache 键）+ content（块解析）；
        #      · `_build_docx_sync.write_section`：id / level / title / content。
        #    明确**不需要**的四列：status、review_status、word_count、outline_json
        #    —— 它们只被 `collect_export_issues`（预检 /check）消费，而那里有
        #    **自己的显式列查询**（见本模块 `SELECT id, parent_id, level, ...`），
        #    与导出计划无关。故收窄不改变任何消费方语义，指纹逐字节不变
        #    （回归护栏：tests/test_export_r176_b1_fixes_20261104.py）。
        "SELECT id, parent_id, sort_order, level, title, content"
        " FROM sections WHERE scheme_id=? ORDER BY sort_order, level, id",
        (scheme_id,),
    )
    # ✅ BUG 修复（2026-11-03 · R175 · BUG-5）：章节读取改**严格读** + 空章留痕。
    #    旧实现 fail-soft：数据库读失败 → 空章节集 → 仍照常构建并写入缓存，
    #    产出一份「只有封面/目录、无正文」的残缺文档且**命中缓存后永久复现**，
    #    用户侧看到的是「导出成功但文档打不开/没内容」。现：
    #      · 读失败 → DBReadError → 503（可行动文案，绝不静默落缓存）；
    #      · 零章节 / 全部为空章 → **明确降级并留痕**：本次仍交付（封面/目录
    #        本身是可打开的合法文档），但响应头 `X-Export-Degraded:
    #        empty-sections` + WARNING 日志，且 **不写入缓存**（见 export_docx
    #        的降级分支）——残缺产物永不被后续命中复用。
    try:
        sections = [dict(r) for r in await _db_fetch_all(cur, what="_prepare_export:sections", strict=True)]
    except DBReadError as e:
        raise HTTPException(503, "章节数据读取失败（数据库连接异常），请稍后重试") from e
    _has_content = any((s.get("title") or "").strip() or (s.get("content") or "").strip() for s in sections)
    if not _has_content:
        logger.warning(
            "导出：方案 %s 章节为空或全部为空章 → 本次降级交付（封面/目录），" "不写入导出缓存", str(scheme_id)[:8]
        )

    # ✅ v15 内容自动改写（默认关闭，需显式 config.auto_rewrite_content=true）：
    #    把「只体检不改写」升级为「可选自动改写」——仅做无损/低风险的格式与词汇修复：
    #    HTML/剪贴板表格→原生 GFM 表格、AI 英文串写→中文、相邻完全重复块去重；
    #    改写只在代码围栏外进行，不破坏 mermaid/chart-json。绝不臆造数据（【待补充】/
    #    空章节/截断句仍只体检），故对安全类专项方案无误导风险。
    #    在体检与 blocks 缓存之前执行，确保审计/编号检测/渲染看到同一份正文。
    if bool(config.get("auto_rewrite_content", False)):
        from app.services.content_rewrite import normalize_section_content

        _rw = {"tables": 0, "english": 0, "dupes": 0}
        for sec in sections:
            raw = sec.get("content") or ""
            if not raw:
                continue
            new, st = normalize_section_content(raw)
            if new != raw:
                sec["content"] = new
                for _k in _rw:
                    _rw[_k] += st.get(_k, 0)
        if any(_rw.values()):
            logger.info(
                "内容自动改写：HTML 表格转原生 %d 处；英文串写修正 %d 处；" "相邻重复块合并 %d 处",
                _rw["tables"],
                _rw["english"],
                _rw["dupes"],
            )

    # ✅ 修复（2026-09-22，未闭合围栏吞噬正文）：系统已具备纯函数
    #    auto_fix_unclosed_fences（CommonMark 口径、幂等、无损），此前仅被单测覆盖、
    #    从未接入导出链路，导致预检报出 DLV-07 后只能靠用户手工补全。
    #    现提供可选开关 auto_fix_unclosed_fences（默认关闭，需显式
    #    config.auto_fix_unclosed_fences=true）：在体检与 blocks 缓存之前，对每章正文
    #    补齐未闭合的 ``` / ~~~ 围栏（追加闭合标记，不改动已闭合块与正文），
    #    与 auto_rewrite_content 同口径——审计/编号/渲染看到同一份已修复正文。
    #    默认关闭确保向后兼容：未开启时导出行为与上版完全一致。
    if bool(config.get("auto_fix_unclosed_fences", False)):
        from app.services.content_utils import auto_fix_unclosed_fences

        _fix_total = 0
        for sec in sections:
            raw = sec.get("content") or ""
            if not raw:
                continue
            new, log = auto_fix_unclosed_fences(raw)
            if log:
                sec["content"] = new
                _fix_total += len(log)
        if _fix_total:
            logger.info("导出前置自动修复未闭合围栏：共 %d 处", _fix_total)
    else:
        _fix_total = 0
    # ✅ R99：围栏补齐会抹掉 DLV-07 的 error 级提示（体检看到的是修好后的正文），
    #   而围栏之后的中文正文会被并入图表源码整体跳过 —— 计数必须随 prep 传下去，
    #   一路进 audit_content 的 fence_auto_fixed 提示与 X-Fix-Stats 的 fences_fixed。

    # ✅ 修复（2026-09-17）：补齐交付文档的事实维度（此前漏渲染，见 _query_global_facts）
    global_facts_status: dict = {}
    global_facts = await _query_global_facts(db, scheme_id, status=global_facts_status)

    # ✅ 修复：图表数据纳入内容指纹（旧实现指纹只含正文与配置，
    # 重新编排/删除图表/修复代码后正文未变 → 导出仍命中旧缓存，返回旧图）
    cur = await db.execute(
        "SELECT section_id, chart_type, data_json FROM chart_predictions WHERE scheme_id=?", (scheme_id,)
    )
    # ✅ BUG-5（R175）：图表登记同样严格读 —— 读失败降级为空集会让图表从产物
    #    静默消失（却仍按「成功」入缓存），改为 503 可行动错误。
    try:
        chart_fp = [
            (r["section_id"], r["chart_type"], r["data_json"] or "")
            for r in await _db_fetch_all(cur, what="_prepare_export:chart_predictions", strict=True)
        ]
    except DBReadError as e:
        raise HTTPException(503, "图表登记数据读取失败（数据库连接异常），请稍后重试") from e

    # ✅ BUG 修复：读取侧统一走 chart_payload.extract_chart_payload（唯一规范解析器）。
    #    只认 mermaid_code 键的旧写法会让 JSON 数据型图表（architecture/labor/
    #    layout/timeline，落库形态为 {"mermaid_code": "", "data": {...}}）取到空串，
    #    该图既进不了 chart_lookup、也拿不到兜底 code，便从文档中静默消失
    #    （与 /charts/list 清单能显示自相矛盾 —— 清单里读的是 data 键）。
    from app.services.chart_payload import extract_chart_payload

    chart_lookup: dict[tuple[str, str], str] = {}
    for sid, ct, data_json in chart_fp:
        if not data_json:
            continue
        code = extract_chart_payload(data_json)
        if not code:
            continue
        # ✅ BUG 修复：extract_chart_payload 对 JSON 数据型图表返回的是 dict/list，
        #    而下游渲染管线（render_mermaid_to_bytes / 缓存 key / _norm_code）均按字符串处理。
        #    旧版 [CHART_TYPE: xxx] 标记块没有 inline code 时，会直接把这个 dict 传入渲染器，
        #    导致 json.loads 解析 Python repr 失败、该图导出为「渲染失败」。
        #    在读取侧统一序列化为 JSON 字符串，保证 chart_lookup 值恒为 str。
        if not isinstance(code, str):
            try:
                code = json.dumps(code, ensure_ascii=False)
            except (TypeError, ValueError):
                code = str(code)
        chart_lookup[(sid, ct)] = code

    # ✅ 性能优化：缓存每章 _parse_content_blocks 结果
    # 旧实现预收集图表代码时解析一次、write_section 中又解析一次，
    # 大文档（50+章）重复解析开销可观。此处统一缓存，两处复用。
    # ✅ R100：`_get_cached_blocks` 闭包已随渲染段迁入 `_prepare_export_render`，
    #   本段只建空缓存 dict 装进 plan（闭包在此定义后零调用，属拆分继留死码）。
    _blocks_cache: dict[str, list[dict]] = {}

    # 构建章节树
    nodes = {s["id"]: s for s in sections}
    children_map: dict[str, list] = {}
    for s in sections:
        children_map.setdefault(s.get("parent_id", ""), []).append(s)
    # ✅ 修复 P1：孤儿章节（parent_id 指向已删节点）不应被静默丢弃，挂为根节点导出
    roots = list(children_map.get("", []))
    for s in sections:
        pid = s.get("parent_id", "")
        if pid and pid not in nodes and s not in roots:
            roots.append(s)

    # ✅ R99（2026-10-17 · 缓存命中快速路径）：先把「取数段」的产物固化到 plan，
    #    再按调用方意图决定是否执行渲染段。指纹所需字段全部在 plan 内，
    #    故 export_docx / export_pdf 可在两者之间插入缓存查询。
    plan = {
        "scheme": scheme,
        "sections": sections,
        "global_facts": global_facts,
        "global_facts_status": global_facts_status,
        "chart_fp": chart_fp,
        "chart_lookup": chart_lookup,
        "blocks_cache": _blocks_cache,
        "roots": roots,
        "children_map": children_map,
        "fe_codes": fe_codes,
        "fe_images": fe_images,
        "render_stats": {"fe": 0, "backend_ok": 0, "failed": 0, "fallback_borrowed": 0},
        # ✅ R99：由 _prepare_export_render **就地填充**（同一 dict 对象）
        "rendered_bytes": {},
        "image_bytes": {},
        "ai_image_pending": 0,
        "ai_image_download_pending": 0,
        "fences_fixed": _fix_total,
        # ✅ BUG-5（R175）：零章节/全空章标记 —— 交付时降级且**不写缓存**。
        "empty_sections": not _has_content,
    }

    # ✅ 导出前内容体检（只读、不改正文）：与预检 /check 同口径，
    #    通过响应头 X-Content-Audit 回传前端，并在日志中留痕。
    #    ✅ R99：fences_fixed 只在「本次确实补齐过围栏」时产出提示项，
    #    缺省 0 时与改造前逐字一致。
    # ✅ R176 · P1-3（2026-11-04）：``audit_content`` 是**纯 CPU 正则扫描**全章节
    #    正文（安静窗口实测 M 档 26.5 ms / L 档 65.7 ms median），在事件循环上同步
    #    执行会直接表现为「导出期间其它请求卡住」；现一律下沉线程池执行。
    # ✅ R176 · D2-2（2026-11-04）：调用时机从「无条件执行」改为「按命中/未命中
    #    解析」—— 命中路径优先读产物旁的 ``*.audit.json`` 旁车（省掉整轮正则扫描），
    #    旁车缺失/过期/损坏则回退实时重算。体检结果仍是**必有**的：X-Content-Audit
    #    响应头是既有契约，命中路径不得静默不发头（见 §10.6 的"不能直接省略"）。
    #    解析入口统一为 ``_resolve_content_audit``（单一出口），此处只记录输入。
    #    ``defer_audit`` 保留给"只做前置准备、不出产物"的调用方（测试/诊断）：
    #    缺省 False = 与改造前逐字一致（立即体检并留痕），两条导出链路显式传
    #    True 才会延后到缓存判定之后。
    plan["content_audit"] = None
    plan["fences_fixed"] = _fix_total
    if not defer_audit:
        plan["content_audit"] = await asyncio.to_thread(audit_content, sections, fences_fixed=_fix_total)
        logger.info("%s", _format_audit_log(plan["content_audit"]))

    # ✅ D-3（2026-10-01）：补充附录数据源。默认关闭 → 空列表 → 不渲染。
    # ⚠️ project_id 取自 scheme（本函数作用域内没有名为 project_id 的局部变量，
    #    直接引用会 NameError 并让**整条导出链路 500**）。
    plan["appendix_sources"] = await _load_appendix_sources(
        db, scheme_id, str((scheme or {}).get("project_id", "") or "")
    )
    plan["config"] = config
    plan["heading_styles"] = _load_heading_styles(config)
    plan["docx_options"] = _build_docx_options(config, _float, _int)
    # ✅ R106 FIX-1：取消事件入 plan —— 渲染段（含本函数内的慢路径直调）与
    #    构建线程（经 _build_docx_task 末尾透传）共用同一份登记。
    plan["_cancel_event"] = cancel_event
    if not _export_cache_fast_path_enabled():
        await _prepare_export_render(plan)
    return plan


async def _prepare_export_render(plan: dict) -> dict:
    """导出的**渲染段**：图表预渲染 + AI 配图生成 + 远程位图下载（会花钱/花时间）。

    ✅ R99（2026-10-17 · 缓存命中快速路径）：本函数由 ``_prepare_export`` 拆出，
    使「取数段」（DB 读 + 块解析 + 指纹）与「渲染段」可被缓存查询**夹在中间**。
    原实现缓存查询在 ``_prepare_export`` 之后（模块内自认的未收口项），实测
    「缓存命中」仍会跑一遍完整渲染：图表全部重新渲染、AI 配图全部重新编排、
    远程位图全部重新下载（artifacts/exp94/probe_confirm.py 实测 cache=hit 时
    ``_chart_cache.get_or_render`` 仍被调用 2 次）。用户点「复用上次成果」
    拿到的秒级响应，实际付出的是一次全量渲染成本。

    拆分口径（**关键不变量**）：``_content_fingerprint`` 所需的全部材料
    （scheme / sections / chart_fp / global_facts / appendix_sources /
    fe_codes / fe_images / config + 服务端 settings 签名）**全部**在取数段产出，
    本段只产出渲染结果（rendered_bytes / image_bytes / render_stats /
    ai_image_pending / ai_image_download_pending），**不改变任何被指纹覆盖的字段**
    ⇒ 拆分段与合并路径算出的 (config_hash, content_fingerprint) 逐字相同
    ⇒ 同一份内容无论走「命中」还是「未命中」，缓存键完全一致（护栏锁定）。

    本段只被 ``_prepare_export``（DOCX/PDF 两条链路的共用入口）与
    ``export_docx`` / ``export_pdf`` 的缓存未命中分支调用；禁用快速路径时
    两条链路仍走 ``_prepare_export`` = 取数段 + 本段，与改造前逐字一致。

    ✅ R100（2026-10-17）两处修复：
    ① **`_settings` 悬空引用（R98 移交、R99 拆分继留）**：本段远程位图下载
      （`img_sem` 取 `image_max_concurrency`）引用的 `_settings` 原 import 留在
      取数段函数体内，拆分后本作用域根本没有该名字 —— 正文含远程图片 URL 的
      方案一导出即 **NameError → 500**（既有测试从未覆盖「渲染段 + 远程图」组合，
      故基线全绿掩盖了它）。现于本段内显式 import。
    ② **禁用快速路径时的二次渲染回归**：`export_cache_fast_path=False` 时
      `_prepare_export` 内部已调本段一次，而 `export_docx`/`export_pdf` 的调用点
      无条件再调一次（R99 注释声称"此处 if 判定为假"，实际无 if）→ 图表全量重渲染、
      位图重复下载、AI 编排重跑，回滚开关不再是逐字回退。现以 plan 上的
      `_render_done` 哨兵做幂等：二次进入直接返回，不重复任何成本。
    """
    if plan.get("_render_done"):
        return plan
    from app.config import settings as _settings

    # ✅ R106 FIX-1：本次导出的取消事件（由 _prepare_export 存入 plan；
    #    缺省 None = 无取消登记，各检查点自动短路，行为逐字不变）。
    _cancel_ev = plan.get("_cancel_event")
    _check_export_cancelled(_cancel_ev)
    config = plan.get("config") or {}
    sections = plan["sections"]
    chart_lookup = plan["chart_lookup"]
    _blocks_cache = plan["blocks_cache"]
    # ✅ 2026-11-05（第二轮日志驱动分析 P1）：解析期被跳过的 chart-json 块
    #    必须计入统计（旧实现只写 INFO，响应头 failed 仍为 0 → 成稿缺图却报「失败 0」）。
    _parse_diag: dict = plan.setdefault("_parse_diagnostics", {})
    fe_images = plan.get("fe_images") or {}
    allow_pil_fallback = bool(config.get("allow_pil_fallback", False))
    render_stats: dict = {
        "fe": 0,
        "backend_ok": 0,
        "failed": 0,
        # ✅ 加固（D1）：跨章借图计数（由 _build_docx_sync 的
        #   fix_stats 合流后写入），缺省键保持既有形状。
        "fallback_borrowed": 0,
    }

    def _get_cached_blocks(sec_id: str, content: str) -> list[dict]:
        if sec_id not in _blocks_cache:
            _blocks_cache[sec_id] = _parse_content_blocks(content or "", diagnostics=_parse_diag)
        return _blocks_cache[sec_id]

    # 预收集需要渲染的图表代码（去重）
    # ✅ 图表同步生成：内联块自带代码优先；无内联码的标记块仍走 chart_predictions
    # P0-3 性能优化：倒排索引一次构建，避免每块 O(N) 兜底扫描（O(N²) → O(N)）
    # ✅ 加固（D1）：传入文档顺序 rank，使跨章借图的选择确定化
    # ✅ RISK-175-01（R175）：改用与写侧同一的 DFS 文档序（旧实现 enumerate(sections)
    #    是 SQL 扁平序，含父子层级时与写侧不一致 → 跨章借图选源错位）。
    order_rank = _compute_doc_order_rank(plan.get("roots") or [], plan.get("children_map") or {})
    chart_type_index = _build_chart_type_index(chart_lookup, order_rank)
    seen_codes: set[tuple[str, str]] = set()
    unique_codes: list[tuple[str, str]] = []
    # ✅ R180（2026-11-05 · 可观测性）：把「图型+代码」映射回章节，供失败明细
    #    回答「哪一章的哪种图失败了」。旧实现只累加 failed 总数。
    code_meta: dict[tuple[str, str], dict] = {}
    for sec in sections:
        for block in _get_cached_blocks(sec["id"], sec.get("content") or ""):
            if block["type"] != "chart":
                continue
            ct = block["chart_type"]
            # ✅ 加固（D1）：取码收敛到唯一出口 `_resolve_chart_code`，与
            #    write_section 的落图路径**逐字同口径**（旧实现两处各写一遍
            #    三级取码逻辑，既重复又无法观测跨章借图）。
            #    章节顺序即 SQL 的 `ORDER BY sort_order, level, id`，
            #    与渲染端的 `_rank_order`（同键排序 + 深度优先）一致。
            code, _borrowed = _resolve_chart_code(block, sec["id"], ct, chart_lookup, chart_type_index, order_rank)
            if code and (ct, code) not in seen_codes:
                seen_codes.add((ct, code))
                unique_codes.append((ct, code))
                code_meta[(ct, code)] = {
                    "section_id": sec["id"],
                    "section_title": sec.get("title") or "",
                }

    # 预渲染（to_thread 线程池，避免阻塞事件循环）
    # ✅ R99：直接复用 plan 里的同一个 dict（就地填充），
    #    不得新建局部 dict —— 否则渲染结果不会回传到 plan。
    rendered_bytes = plan["rendered_bytes"]

    def _note_no_pil_fallback(ct: str) -> None:
        """✅ R179（G-06）：记录「无本地渲染兜底」类型的渲染失败，避免静默缺图。

        旧实现只累加 ``failed`` —— 用户在响应头/日志里无法区分「语法坏了」
        与「本地 PIL 根本不会画、必须依赖 HTTP 引擎」两类失败。新增计数与类型
        明细为**加法式字段**，默认不出现（0 时不挂键），旧消费端零回归。
        """
        try:
            from app.services.chart_validators import PIL_RENDERABLE_CHART_TYPES
        except Exception:  # pragma: no cover - 导入失败不应影响导出主流程
            return
        if ct in PIL_RENDERABLE_CHART_TYPES:
            return
        render_stats["no_pil_fallback"] = render_stats.get("no_pil_fallback", 0) + 1
        _types = render_stats.setdefault("no_pil_fallback_types", [])
        if ct not in _types:
            _types.append(ct)
            logger.warning(
                "图表 %s 无本地渲染兜底（PIL 仅支持 %s），HTTP Service 不可用时该图会缺失",
                ct,
                sorted(PIL_RENDERABLE_CHART_TYPES),
            )

    def _note_no_http_fallback(ct: str, code: str) -> None:
        """✅ R181（2026-11-06）：记录「仅本地 PIL、无 HTTP 出口」的甘特 JSON 失败。

        甘特 chart-json 与 mermaid 甘特轨出口不同：JSON 载荷只在本地 PIL 渲染，
        不经过 Mermaid HTTP 服务。旧实现失败时只累加 ``failed``，前端无法区分
        「HTTP 服务/语法问题」与「该载荷形态本来就没有 HTTP 兜底」。新增计数与
        类型明细为**加法式字段**（0 时不挂键），复用 no_pil_fallback 同款机制，
        旧消费端零回归。
        """
        if ct != "gantt" or not str(code or "").lstrip().startswith("{"):
            return
        render_stats["no_http_fallback"] = render_stats.get("no_http_fallback", 0) + 1
        _types = render_stats.setdefault("no_http_fallback_types", [])
        if ct not in _types:
            _types.append(ct)
            logger.warning(
                "图表 %s 的 chart-json 载荷仅支持本地 PIL 渲染（无 HTTP 出口），本次渲染失败且无云端兜底",
                ct,
            )

    def _note_render_failure(ct: str, code: str, reason: str) -> None:
        """✅ R180（2026-11-05 · 可观测性）：结构化记录单张图渲染失败。

        旧实现只 ``render_stats["failed"] += 1`` + 一行无图型的 warning，
        日志里无法回答「哪一章的哪种图、为什么失败、是否占用了图号」。
        现追加 ``failed_details``（最多 20 条，随 X-Chart-Render-Stats 返回，
        加法式字段、0 失败时不出现），并打一条**可聚合**的结构化 WARNING：
        ``chart_render_failure chart_type=... section_id=... code_hash=... reason=... occupies_number=...``。
        ``occupies_number`` 取 ``chart_fail_placeholder``：True 时插图号占位，
        False 时整块跳过（不占号、无红字），两者用户可见后果不同。
        """
        _meta = code_meta.get((ct, code)) or {}
        _item = {
            "chart_type": ct,
            "section_id": _meta.get("section_id", ""),
            "section_title": _meta.get("section_title", ""),
            "code_hash": hashlib.sha1((code or "").encode("utf-8")).hexdigest()[:8],
            "reason": reason,
            "occupies_number": bool(config.get("chart_fail_placeholder", False)),
        }
        _details = render_stats.setdefault("failed_details", [])
        if len(_details) < 20 and _item not in _details:
            _details.append(_item)
        logger.warning(
            "chart_render_failure chart_type=%s section_id=%s section_title=%s "
            "code_hash=%s reason=%s occupies_number=%s",
            _item["chart_type"],
            _item["section_id"],
            _item["section_title"],
            _item["code_hash"],
            _item["reason"],
            _item["occupies_number"],
        )

    async def _render_one(ct: str, code: str):
        # 统一渲染轨：优先采用前端 mermaid.js 渲染的 PNG（与预览所见一致）
        fe = fe_images.get(_norm_code(code))
        if fe is not None:
            rendered_bytes[(ct, code)] = fe
            render_stats["fe"] += 1
            return
        try:
            from app.services.ai.mermaid_renderer import _chart_cache

            # ✅ 不降级开关透传：allow_pil=False 时 Mermaid 语法载荷在 HTTP Service 失败后
            # 不回退 PIL 自绘（JSON 数据载荷由渲染器内部豁免——PIL 是其唯一路径）
            rendered_bytes[(ct, code)] = await asyncio.to_thread(
                _chart_cache.get_or_render, code, ct, "png", False, allow_pil_fallback
            )
            if rendered_bytes[(ct, code)] is not None:
                render_stats["backend_ok"] += 1
                # ✅ D-23（R176 · 2026-11-04）：甘特图 >30 任务只渲染前 30 项，
                #    旧实现仅留日志 —— 用户看到「完整」的横道图而少了一批任务。
                #    现把截断计数并入 X-Chart-Render-Stats（0 时不挂键）。
                if ct == "gantt":
                    from app.services.ai.mermaid_gantt import gantt_truncated_task_count

                    _gt = gantt_truncated_task_count(code)
                    if _gt:
                        render_stats["gantt_truncated"] = render_stats.get("gantt_truncated", 0) + _gt
            else:
                render_stats["failed"] += 1
                _note_render_failure(ct, code, "render_returned_none")
                _note_no_pil_fallback(ct)
                _note_no_http_fallback(ct, code)
        except Exception as e:
            logger.warning("图表预渲染失败 (%s): %s", ct, e)
            rendered_bytes[(ct, code)] = None
            render_stats["failed"] += 1
            _note_render_failure(ct, code, f"exception:{type(e).__name__}")
            _note_no_pil_fallback(ct)
            _note_no_http_fallback(ct, code)

    # ✅ 2026-11-05（第二轮日志驱动分析 · 图表缓存可观测）：后端渲染轨的
    #    进程内/磁盘缓存命中计数此前只在 get_stats() 里自用、没有任何出口。
    #    这里取「渲染前后计数差」，暴露到 X-Chart-Render-Stats：
    #    cache_hits / cache_misses（本次导出口径，非进程累计）。
    _cache_counters_before: dict | None = None
    if unique_codes:
        try:
            from app.services.ai.mermaid_renderer import _chart_cache

            _cache_counters_before = _chart_cache.get_counters()
        except Exception:  # noqa: BLE001 — 统计失败绝不影响导出
            _cache_counters_before = None

    if unique_codes:
        # ✅ 性能优化：并发上限 4（旧实现无上限 gather，图多时线程池打满 + 内存尖峰）
        render_sem = asyncio.Semaphore(4)

        async def _render_one_limited(ct: str, code: str):
            async with render_sem:
                # ✅ R106 FIX-1：取消已请求 → 跳过剩余图表渲染（不 raise，
                #    避免 gather 兄弟任务未取回异常噪声；段后统一判定）。
                if _cancel_ev is not None and _cancel_ev.is_set():
                    return
                await _render_one(ct, code)

        await asyncio.gather(*[_render_one_limited(ct, code) for ct, code in unique_codes])
        logger.info(
            "渲染轨统计: 前端图 %d, 后端 %d, 失败 %d（总计 %d, allow_pil=%s）",
            render_stats["fe"],
            render_stats["backend_ok"],
            render_stats["failed"],
            len(unique_codes),
            allow_pil_fallback,
        )
        if _cache_counters_before is not None:
            try:
                from app.services.ai.mermaid_renderer import _chart_cache

                _after = _chart_cache.get_counters()
                render_stats["cache_hits"] = max(0, _after["hits"] - _cache_counters_before["hits"])
                render_stats["cache_misses"] = max(0, _after["misses"] - _cache_counters_before["misses"])
            except Exception as e:  # noqa: BLE001 — 统计失败不影响导出
                logger.debug("图表缓存计数读取失败（忽略）: %s", e)

    # ✅ R106 FIX-1：图表段与 AI 配图段之间的取消检查点（命中即抛，
    #    后续「计费生图 + 位图下载 + DOCX 构建」全部不再发生）。
    _check_export_cancelled(_cancel_ev)

    # ✅ AI 配图全自动化（v17）：```ai_image 占位块在导出时自动生成真实图片并
    #    就地改写为 image 块 —— 随后下面的 URL 收集/下载循环会一并取回位图，
    #    write_section 走既有 image 分支插图与编号。生成失败的块保持占位、
    #    整块跳过（不占图号、无红字）。
    _ai_converted = await _auto_generate_ai_image_blocks(sections, _blocks_cache, config, cancel_event=_cancel_ev)
    # ✅ R106 FIX-1：AI 配图段后的取消检查点（在飞生图跑完即止，不再下载/构建）。
    _check_export_cancelled(_cancel_ev)
    # ✅ P1 修复（2026-09-27 · 残缺文档被写入缓存 → 永久缺图）：
    #   旧实现把返回值赋给 `_ai_converted` 之后再无任何引用，等于「AI 配图全部
    #   失败」这一信号被丢弃；而下方坏缓存守卫只看 render_stats["failed"]
    #   （仅统计 Mermaid 图表轨），于是：生图服务不可用 → 本次产物缺图 →
    #   守卫判定通过 → 残缺文档被 INSERT 进 export_cache → 服务恢复后因
    #   content_fingerprint 不变而永远命中这份缺图缓存（与代码注释承诺的
    #   「AI 渲染服务恢复后可重试」直接矛盾）。
    #   修复：以「仍残留 ai_image 占位块」为判据（生成失败的块保持占位），
    #   计入同一个守卫，使缺图产物与缺图渲染一样不入缓存。
    _ai_pending = 0
    if bool(config.get("ai_image_auto_generate", True)):
        # 仅在「本应自动生图」时才把残留占位视为异常：用户显式关闭
        # ai_image_auto_generate 时占位块整块跳过是**既定行为**（旧行为），
        # 此时若也判 degraded 会让该方案的导出缓存永久失效 —— 那是回归。
        for _sec in sections:
            for _blk in _get_cached_blocks(_sec["id"], _sec.get("content") or ""):
                if _blk["type"] == "ai_image":
                    _ai_pending += 1
    if _ai_pending:
        logger.warning(
            "导出：%d 处 AI 配图未生成（占位块残留，成稿将缺图且不写缓存）" "；成功改写 %d 处",
            _ai_pending,
            _ai_converted,
        )

    #    下载采用有界安全策略：仅 HTTP(S)、拒绝内网目标、限制超时/大小/重定向。
    # ✅ R99：同上，直接复用 plan["image_bytes"]。
    image_bytes = plan["image_bytes"]
    unique_image_urls: list[str] = []
    _seen_urls: set[str] = set()
    for sec in sections:
        for block in _get_cached_blocks(sec["id"], sec.get("content") or ""):
            if block["type"] != "image":
                continue
            url = (block.get("url") or "").strip()
            if url and url not in _seen_urls:
                _seen_urls.add(url)
                unique_image_urls.append(url)

    if unique_image_urls:
        img_sem = asyncio.Semaphore(max(1, min(int(_settings.image_max_concurrency), 5)))

        async def _fetch_image(url: str):
            async with img_sem:
                # ✅ R106 FIX-1：取消已请求 → 跳过剩余位图下载（网络成本边界）。
                if _cancel_ev is not None and _cancel_ev.is_set():
                    return
                try:
                    raw = await _download_remote_image(url)
                    data = BytesIO(raw)
                    # 校验确为图片：厂商偶发返回 HTML 错误页，直接插入会破坏 DOCX
                    try:
                        # ✅ R176 · P1-3：PIL verify() 是整图解码校验（CPU），
                        #    下沉线程池；data 为本次下载独占，线程内只读。
                        await asyncio.to_thread(_verify_image_bytes, data)
                    except Exception:
                        logger.warning("AI 配图不是合法图片，已跳过: %s", url[:120])
                        return
                    data.seek(0)
                    image_bytes[url] = data
                except Exception as e:
                    logger.warning("AI 配图下载异常（%s）: %s", url[:120], e)

        await asyncio.gather(*[_fetch_image(u) for u in unique_image_urls])
        logger.info("AI 配图下载: %d/%d 成功", len(image_bytes), len(unique_image_urls))

    # ✅ R106 FIX-1：位图下载段后的取消检查点 —— 渲染段就此收口，
    #    不再进入（或继续）昂贵的 DOCX 构建段。
    _check_export_cancelled(_cancel_ev)

    # ✅ P2 修复（2026-09-30）：生图成功但**位图下载失败**的配图同样造成成稿缺图，
    #    必须计入坏缓存守卫（判据与 write_section 的 image 分支同口径，见
    #    _count_undownloaded_image_blocks）。与 _ai_pending 一样只在「本应自动生图」
    #    时统计 —— 用户显式关闭 ai_image_auto_generate 后正文里的 image 引用下载
    #    失败属旧行为（跳过不占图号），不使缓存永久失效。
    _ai_download_pending = 0
    if bool(config.get("ai_image_auto_generate", True)):
        _ai_download_pending = _count_undownloaded_image_blocks(sections, _blocks_cache, image_bytes)
    if _ai_download_pending:
        render_stats["ai_image_download_pending"] = _ai_download_pending
        logger.warning(
            "导出：%d 处 AI 配图位图下载失败（成稿将缺图且不写缓存），" "已下载 %d/%d 个唯一 URL",
            _ai_download_pending,
            len(image_bytes),
            len(unique_image_urls),
        )

    _merge_chart_json_skip_stats(render_stats, _parse_diag)

    plan["render_stats"] = render_stats
    plan["ai_image_pending"] = _ai_pending
    plan["ai_image_download_pending"] = _ai_download_pending
    # ✅ R100：置幂等哨兵（禁用快速路径时 export_docx/export_pdf 的第二次进入
    #   直接返回，见本函数 docstring ②）。
    plan["_render_done"] = True
    return plan


def _build_docx_options(config: dict, _float, _int) -> dict:
    """导出版式选项（``_prepare_export`` 与缓存命中路径共用的唯一构造出口）。"""
    return {
        "font_name": config.get("font_name", "宋体"),
        "font_size": _clamp_font_size(config.get("font_size", 12), 12),
        "page_header": config.get("page_header", ""),
        "page_footer": config.get("page_footer", ""),
        "show_page_number": config.get("show_page_number", True),
        "show_title_page": config.get("show_title_page", False),
        "show_toc": config.get("show_toc", False),
        "bidder_name": config.get("bidder_name", ""),
        # ✅ 新增排版开关（老前端不传时取默认值，行为等同"标准专项方案版式"）
        "page_break_before_chapter": bool(config.get("chapter_page_break", True)),
        "line_spacing": max(1.0, min(_float("line_spacing", 1.15), 3.0)),
        "page_number_style": str(config.get("page_number_style", "simple") or "simple"),
        "toc_depth": max(1, min(_int("toc_depth", 3), 7)),
        # ✅ 增强 v7：封面项目信息表 + 页边距（缺省 None → 走默认行为，向后兼容）
        # ✅ 类型防御：脏预设/直调 API 传入非 dict 时静默忽略，避免构建期 500
        "cover_info": config.get("cover_info") if isinstance(config.get("cover_info"), dict) else None,
        "margins": config.get("margins") if isinstance(config.get("margins"), dict) else None,
        # ✅ 新增（2026-09-19）：图表渲染失败时的产物形态开关。
        "chart_fail_placeholder": bool(config.get("chart_fail_placeholder", False)),
        # ✅ 2026-09-22 引入：一级章节标题底部边框（章框），默认关闭向后兼容
        "heading_border": bool(config.get("heading_border", False)),
        # ✅ 2026-10-02（第二十三轮）：专项施工方案法定前置表单四开关。
        "scheme_forms": {
            k: True
            for k in ("compilation_note", "approval", "expert_review", "drawing_appendix")
            if bool(
                (config.get("scheme_forms") or {}).get(k) if isinstance(config.get("scheme_forms"), dict) else False
            )
        },
        # ✅ 2026-10-11（《文档生成规则》4.2 导出标准）：版式预设名在此**读取**，
        #    实际覆盖发生在 _build_docx_task → _resolve_layout_preset。
        "layout_preset": str(config.get("layout_preset", "") or "").strip().lower(),
    }


def _export_cache_fast_path_enabled() -> bool:
    """缓存命中快速路径开关（默认开启）；配置不可用时按默认处理，绝不阻断导出。"""
    try:
        from app.config import settings as _fp

        return bool(getattr(_fp, "export_cache_fast_path", True))
    except Exception:  # pragma: no cover - 配置不可用时保持默认行为
        return True


async def _load_appendix_sources(db, scheme_id: str, project_id: str) -> list[dict]:
    """D-3：加载导出补充附录的数据源（知识库条目 / 解析提取成果）。

    ⚠️ 默认关闭（``export_appendix_sources=False``）→ 返回空列表，不渲染任何
    补充附录，产物与旧版**逐字一致**（向后兼容）。

    修复的数据链断点：这两类上游成果此前**从未被导出读取**——
      · knowledge_base 唯一消费点是正文生成注入（sse_handlers:1515）
      · doc_extractions 唯一消费点是完整性报告（doc_pipeline:132）
    于是用户维护的知识库与解析提取成果在交付文档中完全不可见。

    全部 fail-soft：任一表缺失 / 查询失败只记 WARNING，返回已成功加载的部分
    （绝不让附录数据源问题阻断导出）。

    Returns:
        [{"title": 分组标题, "items": [{"name":..., "value":...}, ...]}, ...]
    """
    from app.config import settings as _cfg

    if not bool(getattr(_cfg, "export_appendix_sources", False)):
        return []

    groups: list[dict] = []

    # ① 知识库条目
    # ✅ R179 Phase D（2026-11-06）：列名修正。旧实现查 `title, usage_text`，
    #    而 knowledge_base 的真实列是 `name / usage_hint` —— SQL 必然抛
    #    OperationalError，被下方 `except Exception` 吞成 WARNING，附录**静默为空**
    #    （用户看到「没有知识库条目」，实际库里有数据）。异常捕获同步收窄为
    #    sqlite3.Error（可接受的失败 = 表/列缺失、连接层错误），编程错误不再被吞。
    try:
        cur = await db.execute(
            "SELECT name, usage_hint, content FROM knowledge_base " "WHERE project_id=? ORDER BY id LIMIT 200",
            (project_id,),
        )
        items = []
        for r in await _db_fetch_all(cur, what="_load_appendix_sources:knowledge_base"):
            d = dict(r)
            val = str(d.get("content") or d.get("usage_hint") or "")
            items.append({"name": str(d.get("name") or ""), "value": val[:500]})
        if items:
            groups.append({"title": "知识库条目", "items": items})
    except sqlite3.Error as e:
        logger.warning("导出：加载知识库附录数据源失败（降级跳过）: %s", e)
    except DBReadError as e:
        logger.warning("导出：加载知识库附录数据源读取失败（降级跳过）: %s", e)

    # ② 解析提取成果（四层存储的提取层）
    try:
        cur = await db.execute(
            "SELECT extract_type, extract_data FROM doc_extractions "
            "WHERE project_id=? AND status != 'stale' ORDER BY extraction_id LIMIT 200",
            (project_id,),
        )
        items = []
        for r in await _db_fetch_all(cur, what="_load_appendix_sources:doc_extractions"):
            d = dict(r)
            items.append({"name": str(d.get("extract_type") or ""), "value": str(d.get("extract_data") or "")[:500]})
        if items:
            groups.append({"title": "解析提取成果", "items": items})
    except sqlite3.Error as e:
        logger.warning("导出：加载解析提取成果附录数据源失败（降级跳过）: %s", e)
    except DBReadError as e:
        logger.warning("导出：加载解析提取成果附录数据源读取失败（降级跳过）: %s", e)

    return groups


def _build_docx_task(out_path: str, prep: dict) -> tuple:
    """组装 `_build_docx_sync` 的调用参数（DOCX / PDF 两条链路共用）。

    ✅ 2026-10-11：本函数是两条链路唯一收敛点，版式预设（layout_preset，
    引入自《文档生成规则分析与软件标准建立》4.2 导出标准）在这里套用一次
    即两侧同时生效；未配置预设时 `_resolve_layout_preset` 原样返回，
    产物与改造前逐字一致。
    """
    o = _resolve_layout_preset(prep.get("config") or {}, prep["docx_options"])
    return (
        str(out_path),
        prep["scheme"],
        prep["roots"],
        prep["children_map"],
        prep["chart_lookup"],
        prep["rendered_bytes"],
        prep["blocks_cache"],
        o["font_name"],
        o["font_size"],
        o["page_header"],
        o["page_footer"],
        o["show_page_number"],
        o["show_title_page"],
        o["show_toc"],
        o["bidder_name"],
        prep["heading_styles"],
        o["page_break_before_chapter"],
        o["line_spacing"],
        o["page_number_style"],
        o["toc_depth"],
        o.get("margins"),
        o.get("cover_info"),
        prep.get("image_bytes") or {},
        prep.get("global_facts") or [],
        o.get("chart_fail_placeholder", False),
        o.get("heading_border", False),
        # ✅ D-3：末尾追加，既有位置参数顺序不变（与 _build_docx_sync 签名同序）
        prep.get("appendix_sources") or [],
        # ✅ 2026-10-02：专项施工方案法定前置表单开关（默认全关 → 产物逐字不变）
        prep["docx_options"].get("scheme_forms") or {},
        # ✅ R99（2026-10-17）：末尾追加，既有位置参数顺序不变（与签名同序）
        int(prep.get("fences_fixed") or 0),
        # ✅ R106 FIX-1（2026-10-18）：末尾追加，既有位置参数顺序不变（与签名同序）。
        #    本次导出的取消事件（缺省 None → _build_docx_sync 检查点自动短路）。
        prep.get("_cancel_event"),
    )


# 影响 DOCX/PDF 产物内容的全部配置键白名单（与 _prepare_export 读取保持同步）
from app.routers._export_cache import (  # noqa: F401,E402,I001 — 拆分兼容转发（原名原对象，monkeypatch 语义不变）
    _EXPORT_CONFIG_KEYS,
    _ORPHAN_TMP_TTL_SECONDS,
    _cache_row_viable,
    _content_fingerprint,
    _gc_orphan_exports,
    _guard_numbering_consistency,
    _image_generation_signature,
    _normalize_config,
    _prune_export_cache,
    _summarize_numbering_consistency,
)


# ✅ P2 收口（2026-10-13）：同「方案 + 格式 + 配置」并发导出的重复成本。
#
# 缺陷：缓存查询发生在 `_prepare_export` **之后** ⇒ 第二个同键请求（双击 /
# 多标签页）到达时先行请求尚未落盘，必然 miss，于是两者**各自**付一次全量成本：
#   · `_prepare_export` 内的图表预渲染（有下游缓存，代价较小）；
#   · `_auto_generate_ai_image_blocks` —— **真实计费**。image_engine 的 MD5 成功
#     缓存是**运行期**缓存：并发时两个请求同时错过、各自调用一次 provider；
#   · `_build_docx_sync`（大文档数分钟）；PDF 链再加一次 Word COM 转换。
# 结果本身正确（uuid temp + 原子 os.replace + INSERT OR IGNORE 唯一约束），
# 但成本翻倍，且两个重活同时抢线程池与 AI 配额（§4.1 的配额压力来源）。
#
# 修法：**进程内** asyncio.Lock，键 = (格式, 方案, 配置指纹)。先行请求独占构建并
# **提交**缓存行后才释放；等待者拿到锁后重查缓存即命中、直接返回 —— 不重复
# 构建、不重复计费。配置指纹只取请求体（见 _export_config_hash），可在昂贵的
# `_prepare_export` 之前算出，故锁能覆盖整个准备阶段而无需先知道内容指纹。
# 锁键与缓存判据**同源**：config_hash ≡ _content_fingerprint(prep)[0]（有测试锁定），
# 所以「被串行的是同键请求」与「能命中同一缓存行的请求」永远一致 —— 只会串行
# 化彼此可复用缓存的请求，不会误串行不同配置的导出。
# 开关 export_dedup_concurrent（默认 True）：设 False 逐字回退旧行为。
#
# 仍未收口（建议独立一轮）：缓存查询在 `_prepare_export` 之后，故一次「缓存
# 命中」也要先跑一遍准备阶段（其下游缓存使代价很小）。彻底省掉需把
# `_prepare_export` 拆成「取数 / 渲染」两段并把缓存查询夹在中间，属结构改造。
_EXPORT_BUILD_LOCKS: dict[tuple, asyncio.Lock] = {}
_EXPORT_BUILD_LOCKS_GUARD = threading.Lock()
_EXPORT_BUILD_LOCKS_MAX = 512

# ---------------------------------------------------------------------------
# ✅ BUG 修复（2026-10-14 · P0 跨配置并发误删最终产物）：
#
# 去重锁的键含 config_hash（见 _export_build_lock_for），因此**不同配置指纹**的
# 两个导出请求持有不同的锁、可以完全并发地执行
# 「构建 → os.replace → INSERT → commit → _gc_orphan_exports → FileResponse」。
# 而 _gc_orphan_exports 判孤儿的唯一依据是「DB 里没有缓存行指向它」：
#
#   请求 A（config=X）：os.replace 成功（产物已落在最终路径、无 .tmp. 后缀）
#   请求 B（config=Y）：_gc_orphan_exports 此刻读到 DB，A 的 INSERT 尚未提交
#                       → A 的产物既不在 live、也不在 B 的 protect、也不是 .tmp.
#                       → 被 B 当成孤儿 unlink。
#
# 后果：
#   · 若发生在 A 的 FileResponse 之前 → 用户本次导出 500/404（构建成功却拿不到文档）；
#   · 若发生在 A 的 FileResponse 之后 → export_cache 行永久指向已删除文件
#     （命中判据 Path.exists() 失败 → 每次必然 miss，且该行占据 5 份保留配额）。
#
# 修法：维护进程内「在飞产物路径」集合，覆盖 replace → INSERT → commit → GC 整段
# 窗口；_gc_orphan_exports 一律跳过在飞路径。集合只在同进程内有效，而 GC 也只在
# 本进程被调用，语义自洽；进程崩溃时集合随进程消失，残留产物转为普通孤儿，
# 由既有「无缓存行引用 + .tmp. 宽限期」判定兜底，行为与修复前一致。
# ---------------------------------------------------------------------------
_EXPORT_INFLIGHT_PATHS: set[str] = set()
_EXPORT_INFLIGHT_GUARD = threading.Lock()


def _export_inflight_key(path: Path) -> str:
    """在飞集合的规范化键（Windows 大小写不敏感，需 normcase 后比较）。"""
    return os.path.normcase(str(path))


def _export_inflight_register(path: Path) -> None:
    """注册本次导出的最终产物路径（fail-soft，绝不阻断导出）。"""
    try:
        with _EXPORT_INFLIGHT_GUARD:
            _EXPORT_INFLIGHT_PATHS.add(_export_inflight_key(path))
    except Exception:  # noqa: BLE001 - pragma: no cover - 注册失败只失去保护，不影响正确性
        pass


def _export_inflight_unregister(path: Path | None) -> None:
    """注销在飞产物路径（幂等；None 表示本次降级未落盘、无需注销）。"""
    try:
        with _EXPORT_INFLIGHT_GUARD:
            _EXPORT_INFLIGHT_PATHS.discard(_export_inflight_key(path or Path()))
    except Exception:  # noqa: BLE001 - pragma: no cover - 注销失败无副作用可观测
        pass


def _export_inflight_snapshot() -> set[str]:
    """在飞路径快照（供 _gc_orphan_exports 判定），避免持锁期间做 IO。"""
    with _EXPORT_INFLIGHT_GUARD:
        return set(_EXPORT_INFLIGHT_PATHS)


# ---------------------------------------------------------------------------
# ✅ BUG 修复（2026-11-03 · R175 · BUG-6）：「正在读取」租约（读保护）
# ---------------------------------------------------------------------------
# FileResponse 是**懒打开**的：handler 返回之后才开始流式读文件。缓存命中的
# 产物在 DB 里虽有行，但同一方案的新导出会触发 `_prune_export_cache`
# （保留最近 5 份）—— 若本次命中的是第 6 份以内的旧行、或同一秒插入次序抖动，
# 该文件可能在流式读取途中被 unlink，用户侧表现为下载中断/0 字节/校验失败。
# 在飞集合覆盖不到「读」这一段，故补一条**短租约**：命中/交付前登记
# （path → 过期时间），prune 与孤儿 GC 在租约内一律跳过该路径的删除。
# 进程内、TTL 到期自动失效；即使登记失败也只会退回旧行为（不阻断交付）。
_EXPORT_READ_LEASES: dict[str, float] = {}
_EXPORT_READ_LEASE_TTL_SECONDS = 600  # 与 _ORPHAN_TMP_TTL_SECONDS 同量级


def _export_read_lease_register(path) -> None:
    """登记「本次响应将读取该产物」的短租约（fail-soft）。"""
    try:
        key = _export_inflight_key(path)
        now = time.time()
        with _EXPORT_INFLIGHT_GUARD:
            _EXPORT_READ_LEASES[key] = now + _EXPORT_READ_LEASE_TTL_SECONDS
            # 顺带清过期条目，避免集合随命中次数无限增长
            for _k in [k for k, exp in _EXPORT_READ_LEASES.items() if exp < now]:
                _EXPORT_READ_LEASES.pop(_k, None)
    except Exception:  # noqa: BLE001 - pragma: no cover - 登记失败仅失去保护
        pass


def _export_read_lease_snapshot() -> set[str]:
    """有效租约路径快照（供 prune / 孤儿 GC 判定），过期条目在此清理。"""
    try:
        now = time.time()
        with _EXPORT_INFLIGHT_GUARD:
            for _k in [k for k, exp in _EXPORT_READ_LEASES.items() if exp < now]:
                _EXPORT_READ_LEASES.pop(_k, None)
            return set(_EXPORT_READ_LEASES)
    except Exception:  # noqa: BLE001 - pragma: no cover
        return set()


# ---------------------------------------------------------------------------
# ✅ 导出终局序列收口（2026-10-14 · 遗留建议执行）：
#    DOCX / PDF 两分支的「写缓存行 → 裁剪 → 提交」与「留痕 → 回收 → 注销」
#    序列曾各自手写两份，历史缺陷全部源于双写分叉：
#      · PDF 长期只 INSERT 不裁剪（T1，行数无限增长）；
#      · DOCX 的 INSERT/prune 长期无显式 commit（P1，靠旁路 helper 副作用）；
#      · PDF 的提交顺序修复（2026-10-13）只落在 PDF 侧，DOCX 侧口径不同。
#    现把两段序列各自收敛为单一 helper，两分支只允许传参不同。
# ---------------------------------------------------------------------------
async def _persist_export_cache_row(
    db,
    *,
    scheme: dict,
    scheme_id: str,
    config_hash: str,
    content_fingerprint: str,
    cache_key: str,
    out_path: Path,
    what: str,
) -> None:
    """写入导出缓存行并执行保留裁剪（INSERT OR IGNORE → commit → prune → commit）。

    提交顺序采用 PDF 分支 2026-10-13 修复确立的**双提交**口径：
    新行先落盘，再做保留裁剪 —— 即使 prune 失败/回滚，本次产物下次仍可命中；
    DOCX 旧的「INSERT → prune → 单次 commit」同事务口径在 commit 抛异常时会把
    新行与裁剪一并回滚（缓存永不落盘），统一为双提交后该窗口闭合。
    INSERT OR IGNORE + (scheme_id, content_fingerprint) 唯一约束保证同指纹并发
    只持久化一行（B5，2026-09-23）。

    ✅ R108（2026-10-18 · 日志审计 P0-3）：缓存行是**辅助产物**——DOCX 已生成、
    用户即将拿到文件时，绝不允许「写缓存撞锁」把整次导出炸成 500（生产实证：
    批量导出与正文生成并发时 commit 抛 locked 直通）。现 INSERT+commit 与
    prune+commit 各自成段接 ``retry_db_op``（commit 与 execute 同段重放：locked
    在 WAL 下只在获取写锁的语句抛出，重放整段无重复副作用；INSERT OR IGNORE
    天然幂等）；任一段重试耗尽 → rollback + WARNING 点名，**不阻断交付**。
    """

    async def _insert_and_commit() -> bool:
        ok = await _db_exec(
            db,
            "INSERT OR IGNORE INTO export_cache (id, project_id, scheme_id, config_hash,"
            " content_fingerprint, cache_key, result_path) VALUES (?,?,?,?,?,?,?)",
            (
                str(uuid.uuid4()),
                scheme.get("project_id", ""),
                scheme_id,
                config_hash,
                content_fingerprint,
                cache_key,
                str(out_path),
            ),
            what=what,
        )
        await db.commit()
        return ok

    async def _prune_and_commit() -> None:
        await _prune_export_cache(db, scheme_id, protect_path=out_path)
        await db.commit()

    try:
        await retry_db_op(_insert_and_commit, max_retries=4, base_delay=0.1, jitter=0.05)
    except Exception:
        try:
            await db.rollback()
        except Exception:
            # rollback 本身再失败也无可补救，仅放弃缓存行
            logger.debug("导出缓存行写入失败后的回滚也失败（%s，放弃缓存行）", what, exc_info=True)
        logger.warning(
            "导出缓存行持久化失败（%s），本次产物不落缓存、下次将重新生成；" "交付不受影响", what, exc_info=True
        )
        return
    try:
        await retry_db_op(_prune_and_commit, max_retries=4, base_delay=0.1, jitter=0.05)
    except Exception:
        try:
            await db.rollback()
        except Exception:
            logger.debug("导出缓存裁剪失败后的回滚也失败（%s）", what, exc_info=True)
        # 裁剪失败仅影响保留策略（旧行多留一轮），下一轮导出的裁剪自然自愈
        logger.warning("导出缓存裁剪失败（%s），旧缓存行将由下次导出自愈", what, exc_info=True)


async def _finalize_export_delivery(
    db,
    *,
    scheme_id: str,
    kind: str,
    round_no: int,
    export_filename: str,
    gc_protect: set[Path] | None = None,
    inflight_path: Path | None = None,
    note: str | None = None,
) -> None:
    """导出交付前的统一收尾：审核留痕 → 孤儿回收 → 解除在飞保护。

    三步的顺序是安全契约，不得调换：
      1. 留痕先行（其内部 commit 顺带把此前未提交的写入落盘）；
      2. GC 在留痕之后（GC 判定依赖已提交的缓存行）；
      3. 注销在飞保护必须在 GC 之后（GC 期间本产物仍需受保护）。
    inflight_path=None 表示本次降级未落盘最终产物、无注册可注销
    （PDF degraded 分支的 out_path 未绑定场景，旧实现靠 `if not degraded:`
    守卫，现由 None 判定承担）。
    """
    await _record_export_review_trace(db, scheme_id, kind, round_no, export_filename, note=note)
    await _gc_orphan_exports(db, scheme_id, protect=gc_protect)
    if inflight_path is not None:
        _export_inflight_unregister(inflight_path)


def _export_config_hash(body: dict) -> str:
    """仅由请求体配置算出的配置指纹。

    与 ``_content_fingerprint(prep)[0]`` **同值**（后者同为
    ``md5(json.dumps(_normalize_config(body["config"]), sort_keys=True))``），
    但可在昂贵的 ``_prepare_export`` **之前**算出 ⇒ 能用作并发去重锁的键。
    """
    return hashlib.md5(
        json.dumps(_normalize_config((body or {}).get("config", {}) or {}), sort_keys=True).encode()
    ).hexdigest()


def _export_dedup_enabled() -> bool:
    """并发去重开关；配置不可用时按默认（开启）处理，绝不阻断导出。"""
    try:
        from app.config import settings as _dedup_settings

        return bool(getattr(_dedup_settings, "export_dedup_concurrent", True))
    except Exception:  # pragma: no cover - 配置不可用时保持默认行为
        return True


def _export_build_lock_for(key: tuple) -> asyncio.Lock:
    """按 (去重键, 事件循环) 取进程内构建锁；懒创建 + 有界整体淘汰。

    键里带事件循环 id：pytest 每个用例新建 loop，复用已绑定的旧锁会让测试
    互相卡住。锁只增不删，下标数到上限时**整体**清空 —— 最坏只让两个并发
    导出暂时失去去重（各自构建），**不影响正确性**（唯一约束仍在）。
    """
    _lk_key = (key, id(asyncio.get_running_loop()))
    with _EXPORT_BUILD_LOCKS_GUARD:
        _lk = _EXPORT_BUILD_LOCKS.get(_lk_key)
        if _lk is None:
            if len(_EXPORT_BUILD_LOCKS) >= _EXPORT_BUILD_LOCKS_MAX:
                _EXPORT_BUILD_LOCKS.clear()
            _lk = asyncio.Lock()
            _EXPORT_BUILD_LOCKS[_lk_key] = _lk
        return _lk


async def _export_dedup_acquire(key: tuple) -> asyncio.Lock | None:
    """获取同键并发去重锁；关闭时返回 None（调用方无需释放）。

    返回的锁**必须**在缓存行提交之后才释放：释放早于提交会让等待者重查
    缓存漏命中、退化为各自构建（去重形同虚设）。
    """
    if not _export_dedup_enabled():
        return None
    _lk = _export_build_lock_for(key)
    _had_peer = _lk.locked()
    await _lk.acquire()
    if _had_peer:
        logger.info(
            "导出并发去重：format=%s scheme=%s 检测到同键导出在飞，" "等待其提交缓存后再判定", key[0], str(key[1])[:8]
        )
    return _lk


def _export_dedup_release(_lk: asyncio.Lock | None) -> None:
    """释放去重锁（幂等：None 表示本次未加锁）。"""
    if _lk is not None:
        _lk.release()


# ---------------------------------------------------------------------------
# ✅ R106 FIX-1（2026-10-18）：导出取消真停止。
#    旧「取消导出」只 abort 前端 fetch：``asyncio.to_thread(_build_docx_sync)``
#    的 await 侧收到 CancelledError，但构建线程（CPU 密集）与延后的位图下载
#    继续跑完，AI 配图/图表兜底渲染白烧。现按 scheme_id 维护取消登记
#    （内存事件，**不**纳入 task_registry —— 导出是 await-REST 交付而非后台
#    任务，强行注册反而破坏其轮询/接管契约），由 /cancel 端点置位事件，
#    构建线程与渲染段在成本边界协作式退出。
#    语义契约：
#      · 协作式取消：命中在「下一段边界」生效（图表逐段之间 / AI 配图逐占位码
#        / 位图下载逐 URL / 章节写入逐章之间），非硬杀线程；
#      · 幂等：重复 /cancel 与无在飞导出均返回 ok；
#      · 断连即停：export 路由被客户端断连 cancel 时同样置位事件，
#        使失去 awaiter 的孤儿线程在下一检查点退出；
#      · 取消后不写缓存行、不 os.replace（临时产物清理后 499 退出）。
# ---------------------------------------------------------------------------
class ExportCancelled(Exception):
    """导出在成本边界被 R106 取消登记协作式命中（不落地产物、不写缓存）。"""


_EXPORT_CANCEL_EVENTS: dict[str, list[threading.Event]] = {}
_EXPORT_CANCEL_GUARD = threading.Lock()


def _export_cancel_register(scheme_id: str) -> threading.Event:
    """登记本次导出的取消事件。

    每次调用持**独立** Event（同 scheme 并发导出互不影响；旧导出被取消后
    其孤儿线程只读自己那份事件，不会误读后续新导出登记的事件）。
    """
    ev = threading.Event()
    with _EXPORT_CANCEL_GUARD:
        _EXPORT_CANCEL_EVENTS.setdefault(scheme_id, []).append(ev)
    return ev


def _export_cancel_deregister(scheme_id: str, ev: threading.Event) -> None:
    """注销登记（幂等；只摘本次事件，不动同 scheme 的并发对等导出）。"""
    try:
        with _EXPORT_CANCEL_GUARD:
            lst = _EXPORT_CANCEL_EVENTS.get(scheme_id)
            if not lst:
                return
            try:
                lst.remove(ev)
            except ValueError:
                pass
            if not lst:
                _EXPORT_CANCEL_EVENTS.pop(scheme_id, None)
    except Exception:  # noqa: BLE001 - pragma: no cover - 注销失败只失登记表，不损失正确性
        pass


def _export_cancel_request(scheme_id: str) -> bool:
    """置位该 scheme 全部在飞导出的取消事件，返回是否存在在飞导出。"""
    with _EXPORT_CANCEL_GUARD:
        events = list(_EXPORT_CANCEL_EVENTS.get(scheme_id) or [])
    for ev in events:
        ev.set()
    return bool(events)


def _check_export_cancelled(ev: threading.Event | None) -> None:
    """成本边界检查点：取消事件已置位即抛出提前退出（None = 未启用取消）。"""
    if ev is not None and ev.is_set():
        raise ExportCancelled("导出已被用户停止")


@router.post("/cancel")
async def export_cancel(scheme_id: str):
    """✅ R106 FIX-1：导出停止指令（后端真停止，非仅前端断流）。

    置位该 scheme 在飞导出的取消事件：构建线程/渲染段在下一段边界退出，
    不落地产物、不写缓存。幂等：重复调用与无在飞导出均返回 ok
    （in_progress=false），前端据此可安全地「先 cancel 再 abort」。
    """
    in_progress = _export_cancel_request(scheme_id)
    logger.info("导出取消指令：scheme=%s 在飞导出=%s", scheme_id[:8], in_progress)
    return {"ok": True, "in_progress": in_progress}


@router.post("/docx")
async def export_docx(scheme_id: str, body: dict, db=Depends(get_db)):
    """导出 DOCX（封面 / 目录 / 标题编号 / 图表 / 页眉页脚页码）。

    ✅ 编号统一（2026-09-26 · 显式跨校验器）：导出前编号一致性守卫
    （DOCX/PDF 共用实现，见 _guard_numbering_consistency）。

    ✅ D-1（2026-10-01）：守卫**前置**到 _prepare_export 之前。
    旧顺序是「先 prepare、后守卫」，而 _prepare_export 内部会执行图表渲染
    与 AI 生图（**真实计费**）；严格模式（numbering_consistency_strict=True）
    下守卫抛 409 时，这些成本已经付出且产物全部作废，用户只看到一次失败。
    PDF 链（export_pdf）本来就是「守卫 → prepare」，此处对齐为同一顺序。
    守卫只依赖 (db, scheme_id)，不依赖 prep，前置无副作用。

    ✅ P2（2026-10-13）：同键并发去重。**守卫在锁外** —— 它只读 (db, scheme_id)，
    放锁内会让注定 409 的请求白等一整个构建周期；而锁本身必须覆盖
    _prepare_export **之前**的临界区（含计费的 AI 生图），否则并发会双计费。
    键与判据见 _EXPORT_BUILD_LOCKS 前的模块注释。
    """
    await _guard_numbering_consistency(db, scheme_id)
    # ✅ R106 FIX-1：取消事件登记在去重锁**之前** —— /cancel 在全窗口
    #    （等锁 / 取数段 / 渲染段 / 构建线程）均可生效；本路由 finally 注销。
    cancel_ev = _export_cancel_register(scheme_id)
    _lk = None
    try:
        _lk = await _export_dedup_acquire(("docx", scheme_id, _export_config_hash(body)))
        # ✅ B-15（R182 · 2026-11-06）：DLV-13「high 级问题阻断导出」承诺落地。
        #    默认阻断并返回可执行 409；前端「已知悉并继续」显式携带 override 放行，
        #    放行时写 X-Export-Override 响应头 + 审核留痕（见 _export_override_note）。
        #    位置：守卫/去重锁之后、_prepare_export（计费渲染）之前 —— 既不白烧成本，
        #    又保持「守卫在锁外 → 去重锁 → prepare 在 try 内」的既有并发结构。
        _blockers, _gate = await _export_high_issue_gate(scheme_id, db, body)
        if _blockers and not _gate["override"]:
            return _export_blocked_response(_blockers)
        _override_note = _export_override_note(_blockers, _gate)
        _override_headers = _export_override_headers(_gate)
        # ✅ R99（2026-10-17 · 缓存命中快速路径）：取数段与渲染段之间插入缓存查询。
        #    `_prepare_export` 在快速路径开启时**只做取数段**（DB 读 + 块解析 +
        #    指纹材料），渲染段延后到「确认未命中」之后才执行 —— 命中即零渲染。
        #    关闭 `export_cache_fast_path` 时 `_prepare_export` 内部已跑完渲染段，
        #    此处 `if` 判定为假 → 与改造前逐字一致。
        prep = await _prepare_export(scheme_id, body, db, cancel_event=cancel_ev, defer_audit=True)
        scheme = prep["scheme"]
        config_hash, content_hash = _content_fingerprint(prep)

        # ✅ 2026-10-11（文档质量报告 P0-B）：下载名与封面标题同一出口，避免
        #    「封面印着模板残留串、下载名却是导出文档」两份署名对不上。
        display_name = _display_scheme_name(scheme, prep.get("global_facts"))

        # 缓存检查：内容与配置未变时直接返回历史产物（秒级响应）
        cur = await db.execute(
            "SELECT result_path FROM export_cache WHERE scheme_id=? AND config_hash=? AND content_fingerprint=?",
            (scheme_id, config_hash, content_hash),
        )
        cached = await _db_fetch_one(cur, what="export_docx 缓存查询")
        # ✅ 修复（2026-10-14 · P2）：可用判据收敛到 _cache_row_viable，与
        #    _prune_export_cache 的保留判据同一口径，避免「命中判据/保留判据」分叉。
        # ✅ R176 补漏（2026-11-04）：完整性校验（.docx 走 zipfile.testzip() 整包 CRC）
        #    是同步阻塞，命中分支改由 asyncio.to_thread 执行，不得压事件循环。
        #    判据与记忆化逻辑一字未改（_cache_row_viable 本体保持原样），
        #    「坏产物绝不复用」不变；仅首次命中付一次线程切换成本。
        if cached and cached[0] and await asyncio.to_thread(_cache_row_viable, cached[0]):
            # ✅ R111（P2-5）：缓存命中日志埋点（纯可观测；此前命中态只回
            #    X-Cache-Status 响应头，运维无法从 backend.log 统计命中率）。
            logger.info("导出缓存命中（scheme=%s · docx · 秒级返回历史产物）", scheme_id[:8])
            # ✅ R176 · D2-2（2026-11-04）：命中路径优先读旁车，省掉整轮
            #    `audit_content`；旁车缺失/过期/损坏则无差别回退重算。
            prep["content_audit"] = await _resolve_content_audit(prep, content_hash=content_hash, cached_path=cached[0])
            # ✅ R176 · D2-3（2026-11-04）：命中路径只有**一次写库事务** ——
            #    「轮次自增 + 审核留痕」同一 commit；失败整体回滚且不交付缓存
            #    产物（转 503），杜绝"round 涨了 trace 丢了"的审计断裂。
            committed = await _bump_export_round_and_trace(db, scheme_id, "docx", display_name, note=_override_note)
            if committed is None:
                raise HTTPException(503, "导出审计留痕写入失败：本次缓存命中已整体回滚、未交付文件，" "请稍后重试")
            round_no, export_filename = committed
            name_headers = {
                **_export_name_headers(export_filename, round_no),
                **_export_audit_headers(prep.get("content_audit") or {}),
                **_global_facts_status_headers(prep.get("global_facts_status")),
                **_override_headers,
            }
            # ✅ BUG-6（R175）：登记读租约 —— FileResponse 懒打开，文件在本响应
            #    流式读取期间不得被并发 prune 删除。
            _export_read_lease_register(cached[0])
            # ✅ 契约完整：缓存命中也回传统计/缓存标识响应头，前端统计条行为一致
            return FileResponse(
                cached[0],
                filename=export_filename,
                headers={"X-Chart-Render-Stats": '{"cached":true}', "X-Cache-Status": "hit", **name_headers},
                media_type=_DOCX_MIME,
            )

        # 未命中：本次导出即一轮（先自增再命名）
        round_no = await _bump_export_round(db, scheme_id)
        export_filename = _build_export_filename(display_name, "docx", round_no)
        # ✅ R176 · D2-2：未命中必然重算体检（无旁车可读）。
        prep["content_audit"] = await _resolve_content_audit(prep, content_hash=content_hash)
        name_headers = {
            **_export_name_headers(export_filename, round_no),
            **_export_audit_headers(prep.get("content_audit") or {}),
            **_global_facts_status_headers(prep.get("global_facts_status")),
            **_override_headers,
        }

        # ✅ R99：未命中 → 此时才执行渲染段（图表预渲染 / AI 配图 / 位图下载）。
        await _prepare_export_render(prep)
        render_stats = prep["render_stats"]

        # ✅ 性能优化：out_path 提前计算（仅依赖 scheme_id/hash，与 DOCX 内容无关）
        out_path = EXPORTS_DIR / f"{scheme_id}_{config_hash[:8]}_{content_hash[:8]}.docx"
        # ✅ BUG 修复（2026-10-14 · P0）：注册为「在飞产物」，覆盖 replace → INSERT →
        #    commit → 孤儿回收 整段窗口。不同配置指纹的并发导出持不同的去重锁，
        #    可能在此窗口内触发孤儿回收，把本产物（已落盘、但缓存行未提交）误判为孤儿删除。
        #    ⚠️ 另存一份 `_inflight_final`：下方降级分支会把 out_path 重绑定为
        #    tmp_out_path（本次不写缓存），若注销时读 out_path 就会拿错路径，
        #    导致本条注册永久残留在集合里（虽不致误删，但集合会随失败次数缓慢膨胀）。
        _inflight_final = out_path
        _export_inflight_register(out_path)

        # ✅ 并发修复：相同内容指纹的并发导出（双击/多标签页）会同时对同一个
        # out_path 各自 doc.save()，可能产出损坏的 DOCX 并被写入 export_cache，
        # 之后每次缓存命中都返回坏文件。现先写唯一临时文件，完成后原子替换。
        tmp_out_path = EXPORTS_DIR / f"{scheme_id}_{config_hash[:8]}_{content_hash[:8]}.{uuid.uuid4().hex}.tmp.docx"

        # ✅ 性能优化：DOCX 构建（CPU 密集）整体移到线程池，避免阻塞事件循环
        # ✅ 资源守卫：构建异常时必须清理临时文件，否则数十 MB 的 tmp.docx 永久残留。
        try:
            fix_stats = await asyncio.to_thread(_build_docx_sync, *_build_docx_task(tmp_out_path, prep))
        except ExportCancelled:
            # ✅ R106 FIX-1：构建线程在段边界命中取消 —— 清理本次临时产物、
            #    注销在飞保护（out_path 从未 replace 出来，不存在成品），
            #    上抛由路由出口转 499。**不写缓存行**（取消产物指纹无效）。
            tmp_out_path.unlink(missing_ok=True)  # R177-DELETE-EXEMPT: 取消时仅清理本次临时产物
            _export_inflight_unregister(_inflight_final)
            raise
        except Exception:
            tmp_out_path.unlink(missing_ok=True)  # R177-DELETE-EXEMPT: 异常时仅清理本次临时产物
            # ✅ R168（2026-11-03 · 资源泄漏收口）：普通异常分支此前只清临时文件、
            #    漏了注销在飞登记 —— 上方注释保存 _inflight_final 正是防「注册键
            #    残留」的，ExportCancelled 分支有注销而本分支没有。失败一次即残留
            #    一条永不存在的路径，孤儿回收（_gc_orphan_exports）从此跳过该路径
            #    （集合随失败次数缓慢膨胀 + GC 盲区）。
            _export_inflight_unregister(_inflight_final)
            raise
        import os

        # ✅ Windows 竞争窗口：并发同指纹导出时 FileResponse 懒打开目标文件，
        # os.replace 可能抛 WinError 32；重试 3 次后退化为直接使用本次临时产物。
        replaced = False
        cacheable = True
        for _attempt in range(3):
            try:
                os.replace(tmp_out_path, out_path)
                replaced = True
                break
            except PermissionError:
                await asyncio.sleep(0.3 * (_attempt + 1))
        if not replaced:
            # ✅ BUG 修复（2026-10-06 · P1 交付物/缓存路径治理）：旧实现把
            #    ``out_path = tmp_out_path`` 之后**继续写缓存**，于是 export_cache
            #    的 result_path 永久指向一个 ``.tmp.docx`` 文件：文件名带 uuid 与
            #    .tmp 后缀（运维按名字辨识产物时极具误导），且该行命中即返回，
            #    后续导出即便成功 replace 到规范路径也不会更新该行
            #    （INSERT OR IGNORE 不覆盖已存在行）→ 磁盘与 DB 双双留下孤儿。
            #    现降级为「本次不写缓存」：本次响应体仍是完整产物（tmp 文件
            #    保留给 FileResponse 读取），下次导出重走全链路并落到规范路径。
            out_path = tmp_out_path
            cacheable = False
            logger.warning(
                "DOCX 缓存原子替换 3 次重试均失败（scheme=%s, out=%s），" "本次不写入导出缓存（响应体仍为完整产物）",
                scheme_id[:8],
                EXPORTS_DIR.name,
            )

        # ✅ 坏缓存守卫：存在图表渲染失败 / AI 配图未生成 / 配图位图下载失败时产物含缺图，
        #    不写入缓存——否则 AI 渲染服务恢复后，因内容指纹不变永远命中残缺文档。
        # ✅ BUG-5（R175）：空章/全空章同样走降级分支 —— 不写缓存 + 响应头留痕。
        _empty_sections = bool(prep.get("empty_sections"))
        # ✅ B1-2（R176 · 2026-11-04）：全局事实查询失败 → 产物缺「项目关键事实」，
        #    同样不得写缓存（指纹已含 status，但写缓存会让「失败态」长期固化）。
        _facts_query_failed = (prep.get("global_facts_status") or {}).get("code") == "query_failed"
        # ✅ R179（2026-11-05）：终稿仍有无法自动消除的格式残留（lint_residual_total）
        #    → 与空章/缺图同一降级口径：不写缓存 + X-Export-Degraded 留痕，
        #    明细（各类残留计数）随 X-Fix-Stats 下发。**禁止检测到就静默放过。**
        _content_residue = _export_content_residue(fix_stats)
        if (
            render_stats.get("failed", 0) > 0
            or prep.get("ai_image_pending", 0) > 0
            or prep.get("ai_image_download_pending", 0) > 0
            or _empty_sections
            or _facts_query_failed
            or _content_residue > 0
        ):
            _merge_borrowed_stats(render_stats, fix_stats)
            headers = {
                # ✅ R180 修复（2026-11-05）：响应头必须 latin-1 可编码（Starlette
                #    `init_headers` 用 latin-1 编码 header 值）。新增的 fail 明细含
                #    中文 `section_title`，若 `ensure_ascii=False` 会把中文原样写进
                #    header → `UnicodeEncodeError` 直接打断 DOCX/PDF 交付。
                #    `ensure_ascii=True` 只把非 ASCII 转成 `\uXXXX` 转义，
                #    JSON.parse 后仍是原文，前端消费零影响。
                "X-Chart-Render-Stats": json.dumps(
                    {
                        **render_stats,
                        "ai_image_pending": prep.get("ai_image_pending", 0),
                        "ai_image_download_pending": prep.get("ai_image_download_pending", 0),
                    },
                    ensure_ascii=True,
                ),
                "X-Cache-Status": "degraded",
                **name_headers,
            }
            if _empty_sections:
                headers["X-Export-Degraded"] = "empty-sections"
            if _facts_query_failed:
                _sig = headers.get("X-Export-Degraded")
                headers["X-Export-Degraded"] = (
                    f"{_sig},global-facts-query-failed" if _sig else "global-facts-query-failed"
                )
            if _content_residue > 0:
                _sig = headers.get("X-Export-Degraded")
                headers["X-Export-Degraded"] = f"{_sig},content-residue" if _sig else "content-residue"
            if fix_stats:
                headers["X-Fix-Stats"] = json.dumps(fix_stats, ensure_ascii=False)
            # ✅ 收口（2026-10-14）：留痕 → 回收 → 注销 统一走 _finalize_export_delivery
            #    （与 PDF 分支同一实现；GC protect 传 out_path——该文件即将被流式返回，
            #    不得在本次回收；cacheable=False 时本次无缓存行，只能靠这里回收孤儿）。
            await _finalize_export_delivery(
                db,
                scheme_id=scheme_id,
                kind="docx",
                round_no=round_no,
                export_filename=export_filename,
                gc_protect={out_path},
                inflight_path=_inflight_final,
                note=_override_note,
            )
            # ✅ BUG-6（R175）：交付前登记读租约（防并发 prune 删除正在下载的文件）
            _export_read_lease_register(out_path)
            return FileResponse(str(out_path), filename=export_filename, headers=headers, media_type=_DOCX_MIME)

        # 缓存记录
        # ✅ B5（2026-09-23）：INSERT OR IGNORE + (scheme_id, content_fingerprint) 唯一约束，
        #    使同指纹并发导出只持久化一行（另一并发请求虽仍各自构建 docx，但 os.replace 保证
        #    out_path 不坏，且缓存行不重复），实现严格原子。
        # ✅ 2026-10-06：原子替换失败（``cacheable=False``）时**跳过**缓存写入，
        #    避免 result_path 永久指向 ``.tmp.docx`` 孤儿文件（见上方 warning）。
        # ✅ 收口（2026-10-14）：INSERT → commit → prune → commit 收敛为
        #    _persist_export_cache_row（与 PDF 分支同一实现；提交顺序采用 PDF 侧
        #    2026-10-13 确立的双提交口径，详见 helper docstring）。
        # ✅ B1-3（R176 · 2026-11-04）：写入前用**与命中同一判据**复核产物 ——
        #    截断/损坏（size>0 但结构不完整）绝不落缓存行，避免坏缓存被长期命中。
        if cacheable and not _cache_row_viable(str(out_path)):
            cacheable = False
            logger.warning("DOCX 产物完整性判据未通过（scheme=%s），本次不写缓存: %s", scheme_id[:8], out_path.name)
        if cacheable:
            await _persist_export_cache_row(
                db,
                scheme=scheme,
                scheme_id=scheme_id,
                config_hash=config_hash,
                content_fingerprint=content_hash,
                cache_key=f"{scheme_id}_{config_hash[:8]}",
                out_path=out_path,
                what="export_cache 写入(DOCX)",
            )
            # ✅ R176 · D2-2：只在「可写缓存」路径写旁车 —— 产物既然不落缓存
            #    行，旁车也永远不会被读到，写了只会变成孤儿文件。
            await _write_audit_sidecar(out_path, prep, content_hash)

        # ✅ 加固（D1）：把构建期统计到的「跨章借图」并入渲染统计，一并回传
        #    X-Chart-Render-Stats（前端展示 + 用户知情）。加法式合并，既有键不变。
        _merge_borrowed_stats(render_stats, fix_stats)
        # ✅ R180 修复：header 值须 latin-1 可编码（ensure_ascii=True 让中文标题
        #    以 \uXXXX 转义出现，JSON.parse 后语义不变）。
        headers = {
            "X-Chart-Render-Stats": json.dumps(render_stats, ensure_ascii=True),
            "X-Cache-Status": "miss",
            **name_headers,
        }
        # ✅ R111（P2-5）：缓存未命中日志埋点（纯可观测）。
        logger.info("导出缓存未命中（scheme=%s · docx · 本轮全量渲染，产物已写缓存）", scheme_id[:8])
        if fix_stats:
            headers["X-Fix-Stats"] = json.dumps(fix_stats, ensure_ascii=False)
        # ✅ G12：导出成稿落一条审核留痕（from_status = to_status，只留痕不改状态机）
        # ✅ 收口（2026-10-14）：留痕 → 回收 → 注销 统一走 _finalize_export_delivery
        #    （protect 传本次产物，避免被当成孤儿删掉）。
        await _finalize_export_delivery(
            db,
            scheme_id=scheme_id,
            kind="docx",
            round_no=round_no,
            export_filename=export_filename,
            gc_protect={out_path},
            inflight_path=_inflight_final,
            note=_override_note,
        )
        # ✅ BUG-6（R175）：交付前登记读租约（防并发 prune 删除正在下载的文件）
        _export_read_lease_register(out_path)
        return FileResponse(str(out_path), filename=export_filename, headers=headers, media_type=_DOCX_MIME)
    except asyncio.CancelledError:
        # ✅ R106 FIX-1：客户端断连（关页/abort/切方案）= 停止 —— 置位取消事件，
        #    使失去 awaiter 的构建线程/渲染段在下一检查点退出，不再白烧成本。
        #    断连取消下本协程已无人消费返回值，499 分支不会走到，此处只负责置位。
        cancel_ev.set()
        raise
    except ExportCancelled:
        # ✅ R106 FIX-1：/cancel 已置位且本协程仍在 await（如第二标签页点停止、
        #    或前端 cancel 与 abort 之间的窗口）→ 协作式退出，回 499（非常规但
        #    语义准确的 "client closed request"），不落地产物、不写缓存。
        logger.info("导出已取消（docx, scheme=%s）", scheme_id[:8])
        raise HTTPException(499, "导出已被用户停止")
    finally:
        _export_dedup_release(_lk)
        _export_cancel_deregister(scheme_id, cancel_ev)


# ✅ R178（B-9 · 2026-11-05）：PDF 转换的**进程级串行锁**。
#   Word COM（``win32com.client.Dispatch("Word.Application")``）是**单例**且
#   **不可重入**：两个并发 PDF 导出会各自 Dispatch 一个 Word 实例争抢同一 COM
#   服务器，实测表现为间歇性 0x800706BE（RPC 失败 —— 下方注释已记录「4 次中
#   3 次失败」）与文档损坏。本函数经 ``asyncio.to_thread`` 在**工作线程**中执行
#   ⇒ 必须用 ``threading.Lock``（跨线程串行），不能用 asyncio.Lock。
#   PDF 导出本身是低频操作（生产日志 7 天内 0 次），串行化的代价可忽略。
_PDF_CONVERT_LOCK = threading.Lock()


def _convert_docx_to_pdf(docx_path: str, pdf_path: str) -> str:
    """PDF 转换入口：**进程级串行**（Word COM 单例保护），实际逻辑见内部实现。"""
    with _PDF_CONVERT_LOCK:
        return _convert_docx_to_pdf_unlocked(docx_path, pdf_path)


def _convert_docx_to_pdf_unlocked(docx_path: str, pdf_path: str) -> str:
    """将 DOCX 转换为 PDF，尝试多种转换策略。

    策略优先级：
    1. win32com（Microsoft Word COM，Windows + Word 最可靠）
    2. docx2pdf（封装 Word COM，需 pip install docx2pdf）
    3. libreoffice / soffice 命令行（跨平台，需安装 LibreOffice）

    Returns:
        生成的 PDF 文件路径

    Raises:
        RuntimeError: 所有转换策略均不可用或失败
    """
    import os
    import shutil
    import subprocess

    docx_abs = os.path.abspath(docx_path)
    pdf_abs = os.path.abspath(pdf_path)

    # 策略1: win32com（Microsoft Word COM）
    # ✅ BUG 修复（严重）：本函数经 `asyncio.to_thread` 在**工作线程**中执行，
    #    而 COM 要求调用线程先 CoInitialize —— 旧实现未初始化，win32com 在
    #    线程池里必然抛 "CoInitialize has not been called"，于是"装了 Word 也永远
    #    走不进策略1"，Windows 上 PDF 导出只能靠 LibreOffice 兜底（多数机器未装）
    #    → 最终 501。这里补齐 CoInitialize/CoUninitialize。
    # ✅ 修复（2026-09-20 深度审查、实测证据）：
    #    ① TOC/页码域在 SaveAs(PDF) 前不更新 → PDF 里目录位置出现「（Word 打开后
    #       自动生成目录，请按 F9 更新域）」占位文本（Word 2007 与 LibreOffice
    #       headless 均不尊重 updateFields）。转换前显式更新全部域与目录。
    #    ② 连续调用时 Word COM 间歇性抛 0x800706BE（RPC 失败，本机实测 4 次中
    #       3 次失败）。对照实验定位根因：CoUninitialize 执行时 COM 对象仍被
    #       Python 引用持有（word/doc 未释放），下一次 Dispatch 与垂死的 RPC
    #       服务器竞态。正确顺序必须是「Quit → del 引用 → gc.collect() →
    #       CoUninitialize」；配合 3 次重试兜底，把瞬态失败转为重试成功。
    import gc as _gc
    import time as _time

    try:
        import pythoncom
        import win32com.client
    except ImportError:
        pythoncom = None
        win32com = None
    _last_err: Exception | None = None
    for _attempt in range(3):
        _word = None
        _doc = None
        if pythoncom is None:
            _last_err = RuntimeError("缺少 pywin32（pythoncom）")
            break
        pythoncom.CoInitialize()
        try:
            _word = win32com.client.Dispatch("Word.Application")
            _word.Visible = False
            try:
                _word.DisplayAlerts = 0
            except Exception:
                logger.debug("Word COM 设置 DisplayAlerts 失败（第 %d 次尝试）", _attempt, exc_info=True)
            _doc = _word.Documents.Open(docx_abs, ReadOnly=True)
            try:
                _doc.Fields.Update()
            except Exception:
                logger.warning(
                    "Word COM 更新域失败（PDF 目录/页码可能保留占位文本，" "第 %d 次尝试 · %s）",
                    _attempt,
                    Path(docx_abs).name,
                    exc_info=True,
                )
            try:
                for _ti in range(1, _doc.TablesOfContents.Count + 1):
                    _doc.TablesOfContents.Item(_ti).Update()
            except Exception:
                logger.warning(
                    "Word COM 更新目录失败（PDF 目录可能含占位文本，第 %d 次尝试 · %s）",
                    _attempt,
                    Path(docx_abs).name,
                    exc_info=True,
                )
            _doc.SaveAs(pdf_abs, FileFormat=17)  # 17 = wdFormatPDF
            _doc.Close(False)
            return pdf_abs
        except Exception as e:
            _last_err = e
            logger.info("win32com 转换失败（第 %d 次）: %s", _attempt + 1, e)
            _time.sleep(1.0 + _attempt)
        finally:
            # 释放顺序是消除 0x800706BE 的关键：Quit → 删除 Python 引用 → GC → 再 CoUninitialize
            try:
                if _doc is not None:
                    try:
                        _doc.Close(False)
                    except Exception:
                        logger.debug("Word COM 文档对象清理失败（第 %d 次尝试）", _attempt, exc_info=True)
            except Exception:  # noqa: BLE001 - 内层已兜底，此处为结构级安全网
                pass
            try:
                if _word is not None:
                    try:
                        _word.Quit()
                    except Exception:
                        logger.debug("Word COM 应用对象清理失败（第 %d 次尝试）", _attempt, exc_info=True)
            except Exception:  # noqa: BLE001 - 内层已兜底，此处为结构级安全网
                pass
            try:
                _doc = None
                _word = None
                _gc.collect()
                _time.sleep(0.3)
            except Exception:
                logger.debug(
                    "Word COM 引用释放/GC 清理失败（可能诱发下一次 Dispatch RPC 竞态，" "第 %d 次尝试）",
                    _attempt,
                    exc_info=True,
                )
            pythoncom.CoUninitialize()
    if _last_err is not None:
        logger.info("win32com 三次重试均失败，尝试下一策略: %s", _last_err)

    # 策略2: docx2pdf（同样封装 Word COM，线程内需自行初始化）
    try:
        import pythoncom
        from docx2pdf import convert

        pythoncom.CoInitialize()
        try:
            convert(docx_abs, pdf_abs)
        finally:
            pythoncom.CoUninitialize()
        return pdf_abs
    except ImportError:
        pass
    except Exception as e:
        logger.info("docx2pdf 转换失败，尝试下一策略: %s", e)

    # 策略3: libreoffice / soffice
    for cmd in ["libreoffice", "soffice"]:
        if shutil.which(cmd):
            try:
                out_dir = str(Path(pdf_abs).parent)
                subprocess.run(
                    [cmd, "--headless", "--convert-to", "pdf", "--outdir", out_dir, docx_abs],
                    check=True,
                    capture_output=True,
                    timeout=120,
                    # Windows 下 soffice 报错信息可能含中文，显式 utf-8 解码避免 GBK 崩溃
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                generated = Path(out_dir) / (Path(docx_abs).stem + ".pdf")
                if generated.exists():
                    if str(generated) != pdf_abs:
                        shutil.move(  # R177-DELETE-EXEMPT: 转换工具产出的临时 PDF 归位（非成品目录）
                            str(generated), pdf_abs
                        )
                    return pdf_abs
            except Exception as e:
                logger.info("%s 转换失败: %s", cmd, e)

    raise RuntimeError(
        "DOCX 转 PDF 失败：未找到可用的转换工具。"
        "请安装 Microsoft Word（推荐）或 LibreOffice，或使用 DOCX 格式导出后手动另存为 PDF。"
    )


@router.post("/pdf")
async def export_pdf(scheme_id: str, body: dict, db=Depends(get_db)):
    """导出 PDF（先生成 DOCX 再转换为 PDF）。

    转换策略：优先 Microsoft Word COM（win32com），其次 docx2pdf，最后 LibreOffice。
    所有策略均不可用时返回 501 错误并提示安装转换工具。

    ✅ 重构：准备逻辑与 DOCX 完全共用（`_prepare_export`），同时修复了旧实现
    （1）只认 `mermaid_code` 键 → JSON 数据型图表在 PDF 中静默消失；
    （2）中文方案名直写 HTTP 头 → 全体 PDF 导出 500。

    ✅ 编号统一（2026-09-26 · 显式跨校验器）：与 DOCX 共用导出前守卫，
    且与 DOCX 同序（守卫 → prepare）。

    ✅ P2（2026-10-13）：同键并发去重。PDF 链的重复成本尤重 —— 除 DOCX 构建外
    还有一次 Word COM 转换（秒~分钟级），故同样在去重锁内执行构建与缓存提交；
    守卫在锁外。键与判据见 _EXPORT_BUILD_LOCKS 前的模块注释。
    """
    await _guard_numbering_consistency(db, scheme_id)
    # ✅ R106 FIX-1：与 DOCX 链同构 —— 取消事件登记在去重锁之前，finally 注销。
    cancel_ev = _export_cancel_register(scheme_id)
    _lk = None
    try:
        _lk = await _export_dedup_acquire(("pdf", scheme_id, _export_config_hash(body)))
        # ✅ B-15（R182 · 2026-11-06）：与 DOCX 同一 high 级问题闸门（同位置：
        #    守卫/去重锁之后、_prepare_export 之前）。
        _blockers, _gate = await _export_high_issue_gate(scheme_id, db, body)
        if _blockers and not _gate["override"]:
            return _export_blocked_response(_blockers)
        _override_note = _export_override_note(_blockers, _gate)
        _override_headers = _export_override_headers(_gate)
        import tempfile

        prep = await _prepare_export(scheme_id, body, db, cancel_event=cancel_ev, defer_audit=True)
        scheme = prep["scheme"]

        config_hash, content_hash = _content_fingerprint(prep)
        # ✅ D-2（2026-10-01）：PDF 与 DOCX **必须区分缓存键**。
        #    export_cache 的唯一索引是 (scheme_id, config_hash, content_fingerprint)，
        #    不含格式维度 —— 若两者共用同一 content_hash，DOCX 与 PDF 会互相覆盖
        #    （后写入者因 INSERT OR IGNORE 被忽略，PDF 永远写不进缓存）。
        #    这里只给 **PDF** 的指纹加格式后缀：DOCX 现有键逐字不变 → 既有缓存
        #    **零失效**（无需用户重新导出一遍），是新格式平铺接入的最小改动。
        pdf_content_hash = content_hash + "|fmt=pdf"

        # ✅ 2026-10-11（文档质量报告 P0-B）：与 DOCX 同一方案名出口（PDF 走这里）。
        display_name = _display_scheme_name(scheme, prep.get("global_facts"))

        # 缓存检查（与 DOCX 同口径：命中则秒级返回，跳过最昂贵的 PDF 转换）
        cur = await db.execute(
            "SELECT result_path FROM export_cache WHERE scheme_id=? AND config_hash=? AND content_fingerprint=?",
            (scheme_id, config_hash, pdf_content_hash),
        )
        cached = await _db_fetch_one(cur, what="export_pdf 缓存查询")
        # ✅ 修复（2026-10-14 · P2）：同 DOCX，可用判据收敛到 _cache_row_viable。
        # ✅ R176 补漏（2026-11-04）：同 DOCX —— PDF 头尾校验同样下沉线程，
        #    判据不减（%PDF- 与 %%EOF 都必须满足），坏产物仍不复用。
        if cached and cached[0] and await asyncio.to_thread(_cache_row_viable, cached[0]):
            # ✅ R111（P2-5）：PDF 缓存命中日志埋点（纯可观测，与 DOCX 同口径）。
            logger.info("导出缓存命中（scheme=%s · pdf · 秒级返回历史产物）", scheme_id[:8])
            # ✅ R176 · D2-2（2026-11-04）：与 DOCX 同口径 —— 命中优先读旁车。
            prep["content_audit"] = await _resolve_content_audit(
                prep, content_hash=pdf_content_hash, cached_path=cached[0]
            )
            # ✅ R176 · D2-3（2026-11-04）：与 DOCX 同口径 —— 轮次+留痕单事务；
            #    失败整体回滚并转 503，不交付未留痕的产物。
            committed = await _bump_export_round_and_trace(db, scheme_id, "pdf", display_name, note=_override_note)
            if committed is None:
                raise HTTPException(503, "导出审计留痕写入失败：本次缓存命中已整体回滚、未交付文件，" "请稍后重试")
            round_no, export_filename = committed
            name_headers = {
                **_export_name_headers(export_filename, round_no),
                **_export_audit_headers(prep.get("content_audit") or {}),
                **_global_facts_status_headers(prep.get("global_facts_status")),
                **_override_headers,
            }
            # ✅ BUG-6（R175）：PDF 缓存命中同样登记读租约
            _export_read_lease_register(cached[0])
            return FileResponse(
                str(cached[0]),
                filename=export_filename,
                media_type=_PDF_MIME,
                headers={"X-Chart-Render-Stats": '{"cached":true}', "X-Cache-Status": "hit", **name_headers},
            )

        # 未命中：本次导出即一轮（PDF 与 DOCX 共用同一轮次计数，中间 DOCX 不额外计数）
        round_no = await _bump_export_round(db, scheme_id)
        export_filename = _build_export_filename(display_name, "pdf", round_no)
        # ✅ R176 · D2-2：未命中必然重算体检（无旁车可读）。
        prep["content_audit"] = await _resolve_content_audit(prep, content_hash=pdf_content_hash)
        name_headers = {
            **_export_name_headers(export_filename, round_no),
            **_export_audit_headers(prep.get("content_audit") or {}),
            **_global_facts_status_headers(prep.get("global_facts_status")),
            **_override_headers,
        }

        # ✅ R99（2026-10-17 · 缓存命中快速路径）：与 DOCX 同序 —— 未命中才渲染。
        #    PDF 链的重复成本更高（DOCX 构建 + 一次 Word COM 转换），故收益更大。
        await _prepare_export_render(prep)

        # 生成 DOCX 到临时文件
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_docx = str(Path(tmpdir) / f"{scheme_id}.docx")
            tmp_pdf = str(Path(tmpdir) / f"{scheme_id}.pdf")
            fix_stats = await asyncio.to_thread(_build_docx_sync, *_build_docx_task(tmp_docx, prep))
            # ✅ R106 FIX-1：构建段与转换段之间的取消检查点 —— 已取消则不再进入
            #    Word COM 转换（秒~分钟级成本）；TemporaryDirectory 退出自动清理。
            _check_export_cancelled(cancel_ev)
            # 转换为 PDF（CPU 密集，放线程池）
            try:
                await asyncio.to_thread(_convert_docx_to_pdf, tmp_docx, tmp_pdf)
            except RuntimeError as e:
                raise HTTPException(501, str(e))

            # 读取 PDF 内容返回（临时目录会被清理，所以必须读入内存）
            # ✅ R176 · P1-3：整文件读（可能数十 MB）下沉线程池；句柄在工作线程内新建。
            pdf_bytes = await asyncio.to_thread(_read_path_bytes, tmp_pdf)

        # ✅ 坏缓存守卫（D-2）：与 DOCX 同口径 —— 图表渲染失败 / AI 配图未生成 /
        #    配图位图下载失败时产物缺图，**不写缓存**，否则服务恢复后因指纹不变
        #    永远命中残缺 PDF（旧实现 PDF 链完全没有这道守卫）。
        render_stats = prep.get("render_stats") or {}
        # ✅ B1-2（R176 · 2026-11-04）：全局事实查询失败 → 产物缺「项目关键事实」，
        #    与 DOCX 同口径不写缓存（避免失败态被指纹长期固化）。
        _facts_query_failed = (prep.get("global_facts_status") or {}).get("code") == "query_failed"
        # ✅ R179：终稿无法自动消除的格式残留，PDF 与 DOCX 同口径降级。
        _content_residue = _export_content_residue(fix_stats)
        degraded = (
            render_stats.get("failed", 0) > 0
            or prep.get("ai_image_pending", 0) > 0
            or prep.get("ai_image_download_pending", 0) > 0
            # ✅ BUG-5（R175）：空章产物（封面/目录）不写缓存，与 DOCX 同口径
            or bool(prep.get("empty_sections"))
            or _facts_query_failed
            or _content_residue > 0
        )

        if not degraded:
            import os as _os

            # 落盘到 EXPORTS_DIR（缓存行只记路径，与 DOCX 一致；临时目录会被清理）
            out_path = EXPORTS_DIR / f"{scheme_id}_{config_hash[:8]}_{pdf_content_hash[:8]}.pdf"
            # ✅ BUG 修复（2026-10-14 · P0）：与 DOCX 同口径注册「在飞产物」。
            #    PDF 的 out_path 后缀是 .pdf（不含 .tmp.），落在 _gc_orphan_exports
            #    的扫描范围内；replace 成功到 commit 之间的窗口若被不同配置的并发
            #    DOCX/PDF 导出触发 GC，会被误判孤儿删除 → 缓存行永久指向已删文件。
            _export_inflight_register(out_path)
            # ✅ BUG 修复（2026-10-05 · F-1，交付物损坏）：旧实现
            #    `out_path.write_bytes(pdf_bytes)` **非原子**——并发同指纹 PDF 导出、
            #    或写盘中途崩溃/磁盘满时，都会残留**半截 PDF** 在目标路径。
            #    下一次同指纹导出会命中缓存（size>0 守卫拦不住 >0 字节的半截文件），
            #    直接 FileResponse 返回给用户 → 交付文档损坏。DOCX 分支早已用
            #    `os.replace` 原子替换 + tmp 文件（见 export_docx），PDF 分支漏做。
            #    现改为「先写唯一 tmp → DB INSERT 落盘路径 → os.replace 原子替换」。
            #    失败一律清理 tmp，不留孤儿。
            _tmp_pdf = EXPORTS_DIR / f"{scheme_id}_{config_hash[:8]}_{pdf_content_hash[:8]}.{uuid.uuid4().hex}.tmp.pdf"
            try:
                # ✅ R176 · P1-3：整文件写（可能数十 MB）下沉线程池；
                #    _tmp_pdf 是本次请求唯一的临时路径，句柄在工作线程内新建。
                await asyncio.to_thread(_tmp_pdf.write_bytes, pdf_bytes)
                # ✅ 2026-10-06：顺序治理 —— **先原子替换落盘、再写缓存行**（与 DOCX
                #    分支同序）。旧实现是「INSERT + commit → os.replace」，替换失败时
                #    DB 已指向一个**并不存在**的 out_path → 留下一行僵尸缓存
                #    （/cache-status 记为 stale、prune 才清），且下一次同指纹导出会
                #    先走一次「exists() 失败 → 全量重建」的无效往返。
                # ✅ 与 DOCX 分支同款循环重试：Windows 上 out_path 可能被 Word/
                #    预览窗口占用（WinError 32）；3 次退避重试。
                _replaced = False
                for _attempt in range(3):
                    try:
                        _os.replace(_tmp_pdf, out_path)
                        _replaced = True
                        break
                    except PermissionError:
                        if _attempt < 2:
                            await asyncio.sleep(0.3 * (_attempt + 1))
                if not _replaced:
                    logger.warning(
                        "PDF 缓存原子替换 3 次重试均失败（scheme=%s, out=%s），"
                        "本次不写入导出缓存（响应体仍为完整 PDF）",
                        scheme_id[:8],
                        out_path.name,
                    )
                    _tmp_pdf.unlink(missing_ok=True)  # R177-DELETE-EXEMPT: 替换失败时仅清理本次临时 PDF
                else:
                    # ✅ 收口（2026-10-14）：INSERT → commit → prune → commit 收敛为
                    #    _persist_export_cache_row（与 DOCX 分支同一实现）。
                    # ✅ P1（2026-10-04）：cache_key 补格式后缀。旧实现 DOCX 与 PDF 写入
                    #    **逐字相同**的 cache_key，而 cache_key 是 `/cache-status` 与运维
                    #    排查按格式辨识缓存行的唯一人读标识 —— 同一方案同一指纹下两行完全
                    #    同名，无法区分哪个是 docx、哪个是 pdf，清理/核对时极易误删。
                    #    只改 PDF 侧：DOCX 现有键逐字不变 → 既有缓存行零失效。
                    #    注意：查缓存走 (scheme_id, config_hash, content_fingerprint)
                    #    三元组，cache_key 不参与命中判定 → 本键名不影响任何命中。
                    # ✅ B1-3（R176 · 2026-11-04）：写入前复核 PDF 结构完整性
                    #    （截断/缺 trailer 的产物不得落缓存行，否则会长期被命中）。
                    if not _cache_row_viable(str(out_path)):
                        logger.warning(
                            "PDF 产物完整性判据未通过（scheme=%s），本次不写缓存: %s", scheme_id[:8], out_path.name
                        )
                    else:
                        await _persist_export_cache_row(
                            db,
                            scheme=scheme,
                            scheme_id=scheme_id,
                            config_hash=config_hash,
                            content_fingerprint=pdf_content_hash,
                            cache_key=f"{scheme_id}_{config_hash[:8]}|pdf",
                            out_path=out_path,
                            what="export_cache 写入(PDF)",
                        )
                        # ✅ R176 · D2-2：与 DOCX 同口径 —— 只在落缓存行时写旁车。
                        await _write_audit_sidecar(out_path, prep, pdf_content_hash)
            except Exception as e:
                # 缓存写入失败不得影响本次交付（用户仍拿到完整 PDF）
                try:
                    _tmp_pdf.unlink(missing_ok=True)  # R177-DELETE-EXEMPT: 写缓存失败时仅清理本次临时 PDF
                except Exception:
                    logger.debug("PDF 临时产物清理失败（.tmp 文件将由孤儿回收兜底）", exc_info=True)
                logger.warning("PDF 导出缓存写入失败（不影响本次交付）: %s", e, exc_info=True)

        from fastapi.responses import Response

        # ✅ 加固（D1）：PDF 链路同样合流跨章借图计数（与 DOCX 同口径）
        _merge_borrowed_stats(render_stats, fix_stats)
        # ✅ R111（P2-5）：PDF 缓存未命中日志埋点（纯可观测，与 DOCX 同口径；
        #    degraded 也记——它同样走了全量渲染 + Word 转换）。
        logger.info(
            "导出缓存未命中（scheme=%s · pdf · 本轮全量渲染，产物%s缓存）",
            scheme_id[:8],
            "已" if not degraded else "因缺图未写入",
        )
        headers = {
            "Content-Disposition": _attachment_disposition(export_filename),
            # ✅ R180 修复：同 DOCX，header 值须 latin-1 可编码
            "X-Chart-Render-Stats": json.dumps(render_stats, ensure_ascii=True),
            # ✅ 与 DOCX 对齐：补 X-Cache-Status，缺图时标记 degraded（旧实现完全没有）
            "X-Cache-Status": "degraded" if degraded else "miss",
            **name_headers,
        }
        if fix_stats:
            headers["X-Fix-Stats"] = json.dumps(fix_stats, ensure_ascii=False)
        if _facts_query_failed:
            headers["X-Export-Degraded"] = "global-facts-query-failed"
        if _content_residue > 0:
            _sig = headers.get("X-Export-Degraded")
            headers["X-Export-Degraded"] = f"{_sig},content-residue" if _sig else "content-residue"
        # ✅ G12：PDF 成稿同样落一条审核留痕
        # ✅ 收口（2026-10-14）：留痕 → 回收 → 注销 统一走 _finalize_export_delivery。
        #    GC 不传 protect：PDF 响应体是**内存字节**，本次产物不再依赖磁盘文件。
        #    覆盖三类残留：os.replace 成功但 INSERT 抛异常的 out_path（盘上孤儿）、
        #    陈旧缓存行指向的文件、历史 .tmp.* 临时产物（超过宽限期）。
        #    ⚠️ inflight_path 用条件表达式：out_path 仅在 `if not degraded:` 分支内
        #    定义，降级路径（缺图不写缓存）下该变量未绑定，直接引用会抛
        #    UnboundLocalError；None 表示无注册可注销（helper 内部判定）。
        await _finalize_export_delivery(
            db,
            scheme_id=scheme_id,
            kind="pdf",
            round_no=round_no,
            export_filename=export_filename,
            inflight_path=(out_path if not degraded else None),
            note=_override_note,
        )
        return Response(
            content=pdf_bytes,
            media_type=_PDF_MIME,
            headers=headers,
        )
    except asyncio.CancelledError:
        # ✅ R106 FIX-1：与 DOCX 链同构 —— 客户端断连即置位，孤儿构建线程在
        #    下一段边界退出（转换线程不可中断，跑完当前转换即止）。
        cancel_ev.set()
        raise
    except ExportCancelled:
        logger.info("导出已取消（pdf, scheme=%s）", scheme_id[:8])
        raise HTTPException(499, "导出已被用户停止")
    finally:
        _export_dedup_release(_lk)
        _export_cancel_deregister(scheme_id, cancel_ev)


@router.get("/cache-status")
async def cache_status(scheme_id: str, db=Depends(read_db)):
    """✅ 修复：补齐前端消费的 total/stale 字段，并过滤文件已丢失的僵尸行
    （旧实现只返回 {items}，前端 cacheStatus.total/stale 恒为 undefined）"""
    # ✅ BUG 修复（2026-10-06 · R46）：created_at 秒级精度，同一秒内多次导出时
    #    ORDER BY created_at DESC 次序不稳定 —— 本方案「最近的 5 份缓存」可能取
    #    成任意 5 行（刚导出的那份反而被挤掉），前端把「最旧产物」显示为「当前
    #    可复用成果」。与 _prune_export_cache 同口径补 rowid 次序兜底。
    cur = await db.execute(
        "SELECT * FROM export_cache WHERE scheme_id=? ORDER BY created_at DESC, rowid DESC LIMIT 5", (scheme_id,)
    )
    items = []
    for r in await _db_fetch_all(cur, what="cache_status"):
        item = dict(r)
        # ✅ BUG 修复：Path("") 等价 Path(".") 恒存在 → result_path 为空的僵尸行
        #    会被计为「有效缓存」，total/stale 口径失真。空路径直接判为不存在。
        _p = item.get("result_path") or ""
        item["exists"] = bool(_p) and Path(_p).exists()
        items.append(item)
    valid = [i for i in items if i["exists"]]
    return {"items": items, "total": len(valid), "stale": len(items) - len(valid)}
