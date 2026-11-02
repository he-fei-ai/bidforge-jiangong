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
import threading
import uuid
from datetime import datetime
from io import BytesIO
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from app.config import EXPORTS_DIR
from app.db import get_db, read_db, retry_db_op

# ✅ 围栏保护阈值与图表围栏家族统一取自登记侧（_chart_pipeline），
#    两端必须同口径：旧实现登记侧按「500 行」判未闭合、导出侧按「8000 字符」判，
#    单位与阈值都不同，导致一份"登记侧判未闭合跳过、导出侧照常渲染"的幽灵图。
# ✅ 围栏读取本身（含"超长但闭合"的有界前视恢复）统一取自 read_fenced_block，
#    登记/改写/导出三侧共用同一扫描器，杜绝"合法超长块被登记却不被导出渲染"的幽灵图。
from app.routers._chart_pipeline import (
    INLINE_CHART_FENCE_LANGS,
    parse_fence_line,
)
from app.services import docx_math
from app.services.ai.json_response import strip_outline_numbering

# ✅ 图表关键字→内部类型映射统一取自 chart_validators（唯一事实来源），
#    避免本模块与 _chart_pipeline 的映射继续分叉（曾三份互不一致）。
from app.services.chart_validators import (
    MERMAID_KEYWORD_TO_CHART_TYPE,
)

# ✅ R130（三端格式契约 · 文字标准）：西文统一字体的唯一事实源。
#    样式级（Normal/Heading）与 run 级（图题/表格/封面）凡中文字体作
#    w:ascii 都会改为 Times New Roman，禁止在本文件再写字面量副本。
# ✅ R131-A（2026-10-23 · 图号归属漂移根因修复）：图号前缀剥离判据同样只从
#    契约模块取（由 FIG_TITLE_RE 派生），禁止在本文件复制正则字面量。
from app.services.content_format_contract import (
    FIG_TITLE_ANY_PREFIX_RE,
    FIG_TITLE_PREFIX_RE,
    FONT_LATIN,
    MIN_IMAGE_DPI,
)
from app.services.content_residue_rules import (
    HTML_TAG_RESIDUE_RE,
    PLACEHOLDER_RESIDUE_RE,
)

# ✅ R102（G2 · 排序键单一出口）：导出序与展示序/生成序共用同一归一键。
from app.services.content_utils import section_sort_order_key

# ✅ 《待补充清单》（2026-09-24，治 F 层人工补录兜底）：逐条扫描章节正文中的
#    占位符（规范字段占位 / 裸标记 / 模糊 ××），按字段与章节聚合，支持前端跳转定位。
from app.services.facts_extractor import FACT_SCOPE_PREDICATE, resolve_scheme_project_id
from app.services.placeholder_inventory import (
    DEFAULT_OCCURRENCE_CAP,
    build_placeholder_report,
    build_report_for_scheme,
    build_rerun_plan,
)

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
#   · ``export_pdf`` 的缓存查询（已随 R126 PDF 下线删除）→ 同上；
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
            logger.warning("导出：读取 %s 失败（严格读模式 → 抛 DBReadError）: %s",
                           what, e)
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
            logger.warning("导出：读取 %s 失败（严格读模式 → 抛 DBReadError）: %s",
                           what, e)
            raise DBReadError(str(e)) from e
        logger.warning("导出：读取 %s 失败（降级为无行）: %s", what, e)
        return None


def _merge_borrowed_stats(render_stats: dict, fix_stats: dict | None) -> dict:
    """把构建期统计的「跨章借图」并入渲染统计（单一出口，DOCX 链路；
    PDF 链已随 R126 下线）。

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
    cur = await retry_db_op(lambda: db.execute(sql, params),
                            max_retries=4, base_delay=0.1, jitter=0.05)
    if cur is None:
        logger.warning("导出：写操作 %s 未生效（db.execute 返回 None）", what)
        return False
    return True


def _norm_code(code: str) -> str:
    """规范化 mermaid 代码用于前后端匹配（折叠空白差异）"""
    return " ".join(str(code).split())

def _build_chart_type_index(chart_lookup: dict[tuple[str, str], str],
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
                before = [(abs(me - order_rank.get(sid, me)), i, code)
                          for i, (sid, code) in enumerate(bucket)
                          if order_rank.get(sid) is not None and order_rank[sid] < me]
                if before:
                    before.sort()
                    return before[0][2], bucket[before[0][1]][0]
    return bucket[0][1], bucket[0][0]


def _resolve_chart_code(block: dict, sec_id: str, chart_type: str,
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
#    v24：成稿版式收口（2026-10-20 · R117 · 依据导出稿逐项取证）：
#        ① 显式 A4 页面（python-docx 默认模板是 US Letter 21.59×27.94cm，
#           成稿页高比 A4 少 1.76cm，且与「中国工程文件 A4」不符）；
#        ② 标题 1~4 级默认字号阶梯化（旧默认 14/14/14/14 四级同大、同色，
#           视觉上无法分辨层级）+ 标题强制黑色（旧实现继承模板主题蓝
#           376092/4F81BD + w14:textFill，用户无颜色开关也改不掉）；
#        ③ 图与图题 keep_with_next / keep_together（旧实现图题可被分页推到
#           下一页，出现「图在这页、题在下页」）；表格数据行 cantSplit
#           （长表行内断页后跨页单元格错位 —— 实测导出稿 32 张表 195 行
#           无一带 cantSplit）。
#        ④ 首行「通栏标题行」横向合并（源表 ``| 标题 |  |  |`` ⇒ 成稿半条空色带，
#           且跨页重复的正是这条带、列名永不出现）+ 表头重复行顺移到列名行
#           （实测导出稿 表 2-4 中招）。
#        ⚠️ 刻意**未**改动（产品级契约，见 docs/正文导出成稿问题报告_R117_20261020.md）：
#           · 章节编号 L2 每章重排（heading_v2._format_number：L1 第一章 / L2 "1" /
#             L3 "1.1"）——改成"章号.节号"会改变全文目录与交叉引用，且
#             `_detect_duplicate_sections` 的「章作用域判重」语义与之绑定；
#           · TOC 域深度维持用户配置（\\o "1-N"）—— 导出稿 H5/H6 共 76 段实际是
#             「节内 body 命名空间」的伪标题（1）、/ X.Y.Z.W、），自动抬深度会把
#             它们全部拉进目录。
#        版式/产物字节变化烤进导出缓存，故沿用既有版本号机制整体失效。
#    v25：同章「逐字重复」正文块折叠（2026-10-20 · R117 FIX-10/11 · 生产库真实源文取证）：
#        AI 把一整节内容写进 ```markdown 围栏后，又在围栏闭合**之后**重述一遍
#        （实测 sections.content「通用工程标准」章：L1~25 与 L27~47 两套内容）。
#        全字符级复核（artifacts/r117/_probe16.txt）把重复分成两类，处理手段不同：
#        · 逐字相同 5 组（五个 ``**标准名**`` 加粗小标题，归一 20~24 字）
#          ⇒ `_collapse_repeated_blocks` 折叠，该章 blocks 24 → 19【产物字节变 ⇒ 升版】；
#        · 前缀相同 5 组（五段正文共享前 66~185 字后各写各的，137~301 字）
#          ⇒ `_report_near_duplicate_blocks` **只报警不改写**【产物字节不变 ⇒ 不升版】，
#          因为两侧各有独有尾句，删任一侧都是实质丢正文，导出器无资格替用户取舍。
#        折叠只在**单章作用域**内做：段落/列表项 ≥ 40 归一字符、
#        ``**整串加粗小标题**`` 形 ≥ 8；表格/图表/代码/图片等结构块一律不动；
#        WARNING 按章点名 + 计入 X-Fix-Stats 新键 `repeated_blocks` /
#        `near_duplicate_blocks`（后者前端同步展示，见 utils/exportResponse.ts）。
#        ⚠️ 刻意**不**跨章去重 —— 不同章引用同一条标准条文是正常写法。
#        折叠部分改变产物字节，故升版整体失效旧缓存。
#    v26：R126 公式 OMML 解析两缺陷修复（2026-10-21 · 探针实测复现，护栏
#        tests/test_r126_docx_math_20261021.py 内容级断言）：
#        ① FIX-B `\sqrt[n]{x}` 结构错误 —— tokenizer 新增 bracket 令牌类，
#           激活死代码 `_parse_optional_bracket`，度数进 m:deg、被开方数回 m:e
#           （旧实拍：`<m:rad><m:e>[3]</m:e></m:rad>` + 游离 x）；非度数位的
#           [ ]（如区间 [0,1]）仍按普通文本保留；
#        ② FIX-C 组内脚本附着 —— nary body / frac 参数组 / 脚本参数组 /
#           brace 组产物统一过 `_attach_scripts`，`x_i` 不再被静默拆成平排
#           x、i 两 run（R122 留痕局限②就此闭环）。
#        ③ PDF 导出功能整体下线（用户指令「导出只保留 DOCX」）：删
#           `export_pdf` / `_convert_docx_to_pdf` / `_PDF_MIME`，DOCX 产物字节
#           不因下线而变，但随 ①② 一并升版；存量 fmt=pdf 指纹行自然淘汰。
#        ④ FIX-C2/LIM（docx_math）：常用算子函数名映射（`\lim` 等不再整词丢失）、
#           空 nary 体补 Word 同款空参 run（`\sum_{i=1}^{n}` 不再回退源码文本）、
#           空脚本参数丢弃 + 无界 nary 改 Word 原生 subHide/supHide 形态
#           （`x_{}` / `\sum` 无界 不再产出 schema 违规空参）。
#        ①②④ 改变含公式章的产物字节 ⇒ 旧缓存（内含错位/违规 OMML）必须整体失效。
#        ⚠️ 刻意**未**改动（沿用 v24/v25 两条产品级契约）：章节编号 L2 每章
#           重排、TOC 域深度维持用户配置；另本轮刻意保留 `_safe_filename` 的
#           .pdf 已知扩展名消毒白名单（属文件名消毒防御面，不专属导出 PDF 链）。
#    v27：R127（2026-10-22 · DOCX 格式标准收口，用户任务「完全满足 DOCX 格式标准」）：
#        ① 列表改走 numbering.xml（`w:numPr` + abstractNum），「1、」「（一）」
#           「（1）」恢复中文序号原貌 —— 旧实现手写前缀文本（实测 253 个列表段
#           全部 numPr=0），列表样式被拍平且与正文混为一体；
#        ② 附录/封面表格补 `w:tblHeader`（跨页重复表头）+ `w:cantSplit`（行不
#           断页）+ fixed 布局，列宽按**当前节版心**自适应 —— 旧实现 15/39 张
#           附录表无表头重复、列宽硬编码 16cm（左右 3.18cm 页边距下溢出 1.35cm）；
#        ③ PAGE / NUMPAGES / TOC 域改 Word 标准写法：begin / instrText /
#           separate / 结果 / end 分存 5 个 run（旧实现 fldChar 与 instrText
#           同 run，WPS 偶发域不更新）；
#        ④ 正文改 `Body Text CN` 段落样式承载首行缩进（旧实现逐段写直接格式，
#           违反「用样式而非直接格式」的 OOXML 规范）；
#        ⑤ 图片宽度按当前节版心封顶（旧实现固定 16cm 上限）。
#        ①②③④⑤ 都改变产物字节 ⇒ 升版整体失效旧缓存（旧件列表被拍平/图片溢出）。
#    v29：R128（2026-10-22 · 遗留 3 收口 · 交叉引用 REF 域）：题注编号加书签
#         （图题/表题「图 X-Y / 表 X-Y」前缀包 w:bookmarkStart/End），正文对
#         **真实存在**图号/表号的引用在渲染完成后回填为 5-run REF 域
#         （可点击跳转、更新域后随编号刷新）。可见文本逐字节不变（域结果
#         run = 原匹配文本），但包域改变 XML 字节 ⇒ 旧缓存必须整体失效；
#         未登记/被回滚的编号保持纯文本（绝不生成指向不存在书签的坏域）。
#         刻意不改（C-1/C-2/C-3 登记于 R128 报告）：标题编号仍为导出器文本
#         编号（与目录/图号同源的既定契约）、export.py 四层拆分（独立排期）、
#         存量 v28 及更早缓存行（由 _prune_export_cache 自然淘汰）。
#    v30：R129 导出增强（2026-10-22 · 引入易标式导出机制收口）：
#        ① Word 2010+ 专有扩展清除 —— python-docx 默认模板的 document.xml
#           根元素声明 w14/wp14/wpg/wps/wpc 等 2010+ 命名空间且带
#           mc:Ignorable="w14 wp14"，落盘后由 services/docx_namespace
#           逐部件清理（只删未使用声明与残存注释属性，不动内容）；
#        ② 导出后读回校验（services/export_validate）—— Markdown/HTML/
#           HTML 实体/占位符残留 + 命名空间回潮 + 标题大纲映射；
#        ③ 标题层级 L1~L9（旧 L1~L7）：Heading 7/8/9 显式写 outlineLvl，
#           toc_depth 上限 7→9（目录域 \o "1-9" 随动）；
#        ④ 公式三级降级 OMML → 图片（matplotlib mathtext，可选依赖）→ 原文。
#        产物字节变化 ⇒ 旧缓存（v29 及更早）由 prune 自然淘汰。
#    v31：R131-A（2026-10-23 · 目录/正文编号一致性 P0 · 图号归属漂移根因修复）：
#        图题与 AI 配图 alt 剥离生成期残留的「图 X-Y」前缀（图表分支
#        `fig_title`、配图分支 `alt` 两处；判据 import 自 content_format_contract
#        的 FIG_TITLE_PREFIX_RE）。**可见题注文本变化**（「图 2-1 图 4-1 xxx」
#        →「图 2-1 xxx」）⇒ 旧缓存内含双图号题注，必须整体失效。
#    v32：目录编号与等级修正（2026-10-31 · 用户任务「已生成的文档中，目录编号与
#        等级需修正」，导出稿导航窗格逐项取证）：
#        ① 正文子标题降级（节内 body 命名空间 1）/ a、）不再固定压到 Heading 6/7，
#           改按「父级 + 相对深度」与未降级路径同一套深度规则（实测「1 技术参数」
#           下的 「1）、技术参数 — 主要控制参数」被顶到第 6 级，H2→H6 断级跳跃）；
#           编号文本仍是 1）/ a、，与 DB 子章节 X.X 的命名空间隔离不变。
#        ② 剥掉 AI 自带的字母枚举前缀（"a）" / "a)"）：旧正则只认「数字 + 右括号」，
#           正文落库成 "a、a）材料与环保控制参数"（与 a、 前缀叠加成双重编号）。
#        标题层级/编号文本变化 ⇒ 旧缓存（v31 及更早）必须整体失效。
#    v33：仅识别 L1~L4 级标题（R151 引入，R162 三级叶子标准下语义不变：
#        L3 为最小叶子、L4 为兼容层，文档大纲上限仍是四级）：
#        ① DB 章节 L5+ 不再产出 Heading 样式（降级为加粗正文段落，标题文本与
#           编号保留、正文照常输出）—— Word 导航窗格 / TOC 恰好只有 1~4 级；
#        ② 正文子标题 computed 级别 >4 时同样降级加粗正文（L3 叶子内的更深
#           小节 / L4 兼容叶子内的「1）」小节不再以 Heading 5+ 出现在文档大纲）；
#        ③ TOC 域深度封顶 4（\_build_docx_options 与 _add_toc_field 双侧钳位，
#           用户配置再深也无 L5+ 标题可收录）。
#        ⚠️ 推翻 v24「TOC 深度维持用户配置」契约（用户本轮指令明示仅识别
#        L1~L4）；判据单一事实源 = services/outline_rules。标题样式/层级
#        变化 ⇒ 旧缓存（v32 及更早）必须整体失效。
_EXPORTER_VERSION = "33"


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
_AUDIT_RULES: tuple[tuple[str, str, str, re.Pattern], ...] = (
    # (key, 人类可读名, 级别, 正则)
    # ✅ R129：placeholder / html_tag 两个正则上收至 services/content_residue_rules
    #    （导出后校验 export_validate 同判据共用，且 services 层禁止反向 import
    #    routers —— 层级护栏实拍抓到，见 tests/test_outline_name_line_20260927.py）。
    ("placeholder", "数据占位符（××/xx/【待补充】）", "warn",
     PLACEHOLDER_RESIDUE_RE),
    ("html_tag", "HTML 标签残留（应使用 GFM 表格）", "error",
     HTML_TAG_RESIDUE_RE),
    # ✅ 新增：Excel/Word 剪贴板表格占位标记（<fcel>/<lcel>/<ucel>/<xc>/<nl>）——
    #    导出渲染时已降级为纯文本（不再印进成稿），此处仍计入体检供用户回改源文。
    ("clipboard_table", "剪贴板表格残留标记（粘贴表格带入，应改用 GFM 表格）", "error",
     re.compile(r"</?(?:fcel|lcel|ucel|xc|nl)\s*/?>", re.I)),
    ("latex_square", "LaTeX 空参数占位 \\square", "error", re.compile(r"\\square")),
    ("bidding_terms", "投标场景用语（专项方案不应引用）", "warn",
     re.compile(r"招标文件|投标文件|评标办法|评分标准|废标条件|投标须知")),
    # ✅ R50 新增（2026-10-07）：中英混杂 —— 中文句子里夹连续两个小写英文单词
    #    （如"签署 material acceptance records 后方可"）。规范编号（GB/JGJ 大写）、
    #    化学式（HCHO）、单个英文术语不会误伤。命中即提示用户改回中文。
    ("mixed_language", "中英混杂（中文段落中夹英文短语）", "warn",
     re.compile(r"[\u4e00-\u9fa5]\s*[a-z]{4,}(?:\s+[a-z]{3,})+\s*[\u4e00-\u9fa5]")),
)
# 图表家族围栏（未闭合即视为生成被截断，残片渲染必失败）——
# 统一定义在 _chart_pipeline.INLINE_CHART_FENCE_LANGS，避免两端分叉。
_CHART_FENCE_LANGS = INLINE_CHART_FENCE_LANGS


def _count_unclosed_chart_fences(content: str) -> int:
    """统计正文中**未闭合**的代码围栏数量（截断残片）。

    ✅ BUG 修复（2026-09-22，未闭合 ai_image 围栏漏检）：
      旧实现只统计 mermaid / chart-json 两类围栏，未闭合的 ``ai_image`` 围栏
      （AI 配图占位，正文里同样由 AI 生成、同样会被 max_tokens 截断）以及
      ``python`` / ``json`` 等普通代码围栏一律漏检——而它们在
      ``_parse_content_blocks'' 中都会**吞噬截断点之后的全部正文**：
      ai_image 吞掉的正文被静默丢弃，普通代码围栏吞掉的正文被当代码块
      印进交付文档。审计既无计数、用户也无告警，等于"静默丢正文"。
      现改为统计**所有**未闭合围栏。
    """
    if not content:
        return 0
    lines = content.split("\n")
    n = 0
    i = 0
    while i < len(lines):
        # ✅ 2026-09-24 修复：围栏检测改用 _chart_pipeline.parse_fence_line（与登记/
        #    改写/解析三侧同源）。旧实现 `startswith("```") + [3:]` 漏检波浪号围栏、
        #    且对 4 反引号围栏取到错误 lang —— 与 _parse_content_blocks 的同一根因。
        pf = parse_fence_line(lines[i])
        if pf is None:
            i += 1
            continue
        open_char, _open_len, lang = pf
        lang = lang.strip().lower()
        i += 1
        closed = False
        while i < len(lines):
            pf_end = parse_fence_line(lines[i])
            if pf_end is not None and pf_end[0] == open_char:
                # 同种字符即视为闭合（与 read_fenced_block 的闭合判据一致，
                # 长度不严格要求 ≥ 开围栏，避免 4 反引号开 + 3 反引号闭的错配被判未闭合）
                closed = True
                i += 1
                break
            i += 1
        if not closed:
            n += 1
            if lang not in _CHART_FENCE_LANGS:
                logger.warning(
                    "正文含未闭合的普通代码围栏（lang=%s），"
                    "其后正文存在被当代码块输出的风险", lang)
    return n


def audit_content(sections: list[dict], *,
                  fences_fixed: int = 0,
                  mermaid_status: dict | None = None) -> dict:
    """扫描各章正文，产出可交付性问题清单（供导出预检 / 响应头 / 日志）。

    返回 ``{"total": int, "items": [...]}``；``items`` 每项含
    key / label / level / count / sections（最多 3 个章节标题，便于定位）。
    当 ``mermaid_status`` 不为 None 时，顶层新增 ``"mermaid_service"`` 键
    （纯加法，不改既有 total/items 结构，既有调用方零影响）。

    ✅ R99（2026-10-17 · 围栏自动修复可观测性）：新增 ``fences_fixed`` 形参。
    开启 ``auto_fix_unclosed_fences`` 后，围栏在体检**之前**就被补齐 →
    ``_count_unclosed_chart_fences`` 恒为 0 → 「未闭合围栏」这条 error 级提示
    在本次导出里**凭空消失**，而用户既看不到任何替代信号、也不知道围栏被改写过。
    实测更严重：补齐后「围栏之后的中文正文」会被并入图表源码整体丢弃
    （默认关闭时解析器会按句读行把正文还原出来，开启后反而丢得更多）。
    现显式产出一条 warn 级提示，如实告知「本次自动补齐了 N 处围栏 + 需人工核对」。
    缺省 0 = 不传该形参时代码路径与改造前**逐字一致**（既有调用方零影响）。

    ✅ R133-C（2026-10-24 · Mermaid 服务健康探测）：新增 ``mermaid_status`` 形参。
    由调用方在异步上下文中完成 2s 超时 + fail-soft 的 Mermaid HTTP 服务
    探测，将结果写回顶层 ``"mermaid_service"`` 键，供前端导出页展示
    「原生出图 / PIL v2 回退」的确定性结论。探测失败不阻断导出。
    """
    hits: dict[str, dict] = {}
    for sec in sections:
        content = sec.get("content") or ""
        title = sec.get("title") or ""
        if not content:
            continue
        for key, label, level, rx in _AUDIT_RULES:
            found = len(rx.findall(content))
            if found:
                slot = hits.setdefault(key, {"key": key, "label": label,
                                             "level": level, "count": 0,
                                             "sections": []})
                slot["count"] += found
                if len(slot["sections"]) < 3 and title not in slot["sections"]:
                    slot["sections"].append(title)
        unclosed = _count_unclosed_chart_fences(content)
        if unclosed:
            slot = hits.setdefault("unclosed_fence", {
                "key": "unclosed_fence",
                "label": "代码围栏未闭合（生成疑似被截断，其后正文有丢失风险）",
                "level": "error", "count": 0, "sections": []})
            slot["count"] += unclosed
            if len(slot["sections"]) < 3 and title not in slot["sections"]:
                slot["sections"].append(title)
        # ✅ R130（三端格式契约 · 第二层「导出前校验」）：与正文生成后的第一层
        #    自检（sse_handlers.format_contract）**共用同一份判据**
        #    （services.content_format_contract），两层结论天然一致、不存在双轨。
        #    只体检不篡改（对齐本块设计原则）；error→error、warning→warn、
        #    advisory→info（排序表已支持 info 档）。
        #    与 unclosed_fence 去重：契约的「代码块未闭合」与本块既有 error 项
        #    是同一事实，不再二次计数。
        try:
            from app.services.content_format_contract import check_format_contract
            _fc = check_format_contract(
                content, section_id=str(sec.get("id") or ""),
                section_title=title)
            for it in _fc.get("issues") or []:
                if (it.get("category") == "chart"
                        and str(it.get("message", "")).startswith("代码块未闭合")):
                    continue  # 与 unclosed_fence 同一事实，已在上方计数
                _lvl = {"error": "error", "warning": "warn"}.get(
                    it.get("severity"), "info")
                _cat = str(it.get("category") or "text")
                _cat_cn = {"text": "文字", "symbol": "符号", "formula": "公式",
                           "chart": "图表", "table": "表格", "number": "数字",
                           "numbering": "编号"}.get(_cat, _cat)
                _key = f"format_contract_{_cat}"
                slot = hits.setdefault(_key, {
                    "key": _key,
                    "label": f"格式契约·{_cat_cn}标准（正文与导出渲染标准不一致）",
                    "level": _lvl, "count": 0, "sections": []})
                slot["count"] += 1
                if len(slot["sections"]) < 3 and title not in slot["sections"]:
                    slot["sections"].append(title)
                # error 级并档（同 key 混级时取更严重，排序/展示不被 advisory 拉低）
                if _lvl == "error":
                    slot["level"] = "error"
        except Exception:
            logger.warning("章节 %s 格式契约导出前校验失败（忽略，不阻断导出）",
                           str(sec.get("id") or "")[:8], exc_info=True)
    if fences_fixed > 0:
        hits.setdefault("fence_auto_fixed", {
            "key": "fence_auto_fixed",
            "label": (f"本次导出已自动补齐 {fences_fixed} 处未闭合围栏"
                      "（围栏之后的中文正文可能被并入图表源码，请人工核对成稿）"),
            "level": "warn", "count": int(fences_fixed), "sections": []})
    # 级别排序：error → warn，同级按数量降序（用户先看最要命的）
    order = {"error": 0, "warn": 1, "info": 2}
    items = sorted(hits.values(),
                   key=lambda x: (order.get(x["level"], 9), -x["count"]))
    result = {"total": sum(x["count"] for x in items), "items": items}
    # ✅ R133-C：Mermaid 服务健康状态作为顶层新增键（纯加法，不影响 total/items）
    if mermaid_status is not None:
        result["mermaid_service"] = mermaid_status
    return result


async def _probe_mermaid_status() -> dict:
    """R133-C（2026-10-24）：Mermaid HTTP 服务健康探测（2s 硬超时 + fail-soft）。

    - 复用 mermaid_service 单例客户端的 get_available_backends()（Docker/Railway
      未配置 URL 时即时 False，mmdc 走 subprocess 探测——其内部超时 5s，本函数
      用 ``asyncio.wait_for(..., timeout=2)`` **硬封顶**，绝不阻塞导出主流程）；
    - 任何异常（含超时）一律记 WARNING 并降级为「不可用」，探测失败不阻断导出；
    - 结果由调用方写进 content_audit 顶层 ``mermaid_service`` 键（纯加法）。
    """
    try:
        from app.services.ai.mermaid_service import get_mermaid_service_client
        client = get_mermaid_service_client()
        backends = await asyncio.wait_for(
            client.get_available_backends(), timeout=2.0)
        return {"available": len(backends) > 0,
                "backends": [b.value for b in backends]}
    except Exception as e:
        logger.warning(
            "Mermaid 服务健康探测失败（降级为不可用，不影响导出）: %s", e)
        return {"available": False, "backends": []}


def _format_audit_log(audit: dict) -> str:
    """把体检结果压成一行日志（便于日志里直接看出文档可交付性）。"""
    if not audit.get("items"):
        return "导出前内容体检：未发现可交付性风险"
    parts = [f"{it['label']} {it['count']} 处" for it in audit["items"]]
    return "导出前内容体检：" + "；".join(parts)


def _count_child_namespace_subheadings(blocks: list[dict], section_prefix: str) -> int:
    """统计本节内容中落在「子章节命名空间」的子标题数（编号形如 ``{prefix}.N``）。

    内容子标题经 :func:`_compute_subheading` 编号后形如 ``{section_prefix}.{dotted}``：
    - ``dotted`` 只有 1 段（如 ``1.1``）→ 与本节 **DB 子章节**的编号在同一命名空间
      （DB 子章节同样从 ``{prefix}.1`` 起算），渲染时会发生编号重复；
    - ``dotted`` ≥ 2 段（如 ``1.1.1``）属更深层级，不与子章节直接冲突。

    依赖 ``blocks`` 中已算好的 ``_fixed_text``（write_section 渲染前会先统一计算）。
    """
    n = 0
    if not section_prefix:
        return 0
    pat = re.compile(r"^" + re.escape(section_prefix) + r"\.(\d+(?:\.\d+)*)\s")
    for block in blocks or []:
        if block.get("type") != "heading":
            continue
        m = pat.match(str(block.get("_fixed_text") or ""))
        if m and "." not in m.group(1):
            n += 1
    return n


def _section_sort_key(sec: dict):
    """章节排序键：``sort_order`` 为主序，同序号时按 level、id 兜底。

    ✅ BUG 修复（2026-09-20）：``sections.sort_order`` 的语义在不同写入路径下
    并不统一——目录落库按「同级内序号」从 0 赋值，而拖拽重排（``/reorder``）
    用 ``enumerate(order)`` 写全局序号。两种口径混用后**同级可能出现重复序号**，
    此时若只按 ``sort_order`` 排序，SQLite 返回行序不确定，会导致：
      1. 同一份内容两次导出章节顺序漂移（读者看到「章节串位」）；
      2. 内容指纹（依赖章节顺序）随之抖动 → 缓存永远 miss，重复渲染。
    故统一加确定性兜底键，所有排序点必须共用本函数。

    ✅ R99（2026-10-17 · 脏 level 不得炸整份导出）：``level`` 来自 DB（TEXT 列），
    脏数据可能是 ``"二"`` / ``"2.0"`` / 空串等非整数形态。原实现直接 ``int(...)``，
    一次脏值就让本函数抛 ``ValueError`` —— 而它被 ``_build_docx_sync._rank_order``
    在**章节递归之外**调用（不受 ``_write_section_safe`` 的逐章 fail-soft 保护），
    实测表现为「一份产物都出不来」。现统一走安全取整：非法值降级为 1（与
    ``write_section`` 内部的兜底同口径），对合法数据逐字不变。

    ✅ R102（G2 · 三序漂移收口）：``sort_order`` 同为 DB TEXT 列，脏字符串
    （``"3"``）与 int 混排会让本函数抛 ``TypeError``（与 R99 同一「递归外
    无保护」暴露面），且旧键与 ``sections._build_tree`` / ``order_sections_dfs``
    各写一份 ⇒ 落库序/展示序/导出序可能分叉。首元素现委托
    ``content_utils.section_sort_order_key`` 单一出口（int 归一，对合法数据
    恒等变换）。
    """
    try:
        level = int(sec.get("level") or 1)
    except (TypeError, ValueError):
        level = 1
    so, sid = section_sort_order_key(sec.get("sort_order"), sec.get("id"))
    return (so,
            level,
            sid)


def _detect_duplicate_sections(sections: list[dict]) -> list[dict]:
    """复现导出器的完整编号（DB 子章节 + 内容内部子标题），检测**渲染后**的重复编号。

    实测第 8 轮交付文档取证（脚手架专项施工方案）：
    - DB 存储裸标题（无编号）、目录树编号完全干净（0 重复）；
    - 但章节**内容内部**还有大量 Markdown 子标题（``## 项目概况``），导出时经
      ``_compute_subheading`` 编号成 ``{本节编号}.N``（如 ``1.1 项目概况``）；
    - 而本节的 **DB 子章节**又由 ``HeadingNumberingGeneratorV2`` 从 ``.1`` 起算
      （``1.1 工程规模与结构形式``）——两套计数器在同一 ``X.N`` 命名空间互不感知，
      交付文档出现成对的 ``1.1/1.1``、``2.1~2.4/2.1~2.3``。

    这里按与 ``write_section`` 完全一致的顺序模拟编号（含渲染端的计数器前移修复），
    凡同一章作用域内出现相同编号即判重复——正常文档不会误报，且若未来有人破坏
    渲染端的修复，本检测会重新报出，作为回归护栏。
    """
    by_id: dict = {s.get("id"): s for s in sections if s.get("id")}
    children_map: dict = {}
    roots: list = []
    for s in sections:
        pid = s.get("parent_id")
        if pid and pid in by_id:
            children_map.setdefault(pid, []).append(s)
        else:
            roots.append(s)
    roots.sort(key=_section_sort_key)
    for lst in children_map.values():
        lst.sort(key=_section_sort_key)

    from app.services.ai.heading_v2 import HeadingNumberingGeneratorV2
    gen = HeadingNumberingGeneratorV2()
    seen: dict[tuple, str] = {}   # (章作用域, 渲染编号) -> 首次出现标题
    dupes: list[dict] = []

    def flag(num: str, scope, title: str, sid):
        key = (scope, num)
        if key in seen:
            dupes.append({
                "type": "duplicate_section_number",
                "section_id": sid, "title": title,
                "detail": (f"章节编号「{num}」在本章内重复出现"
                           f"（此前见于「{seen[key]}」），导出目录与编号将失去唯一性"),
            })
        else:
            seen[key] = title

    def walk(sec: dict, parent_id: str, chapter_id):
        level = int(sec.get("level", 1) or 1)
        title = (sec.get("title") or "").strip()
        if level == 1:
            chapter_id = sec.get("id")
        scope = chapter_id if level >= 2 else None
        pure = _strip_title_number(title)
        num = gen.update_counter(level, parent_id)
        if num:
            flag(num, scope, title, sec.get("id"))
        # —— 内容内部子标题（与 write_section 同口径：prefix + _compute_subheading）——
        n_child_ns = 0
        content = sec.get("content") or ""
        if content and num:
            section_prefix = _section_number_prefix(f"{num} {pure}".strip())
            blocks = _parse_content_blocks(content)
            # 与 write_section 同口径：剥离正文开头与本节标题重复的块
            # （AI 常在正文首行自引用本节标题；渲染端会剥掉，检测器不剥会假阳性）
            blocks = _strip_duplicate_leading_title(blocks, pure, f"{num} {pure}".strip())
            # ✅ 加固（2026-10-06 · D4）：`_compute_subheading` 的 `has_children`
            #    必须与渲染端同口径。渲染端在「本节有 DB 子章节 **且**
            #    body_subheading_demote_with_children 开启」时把正文子标题降级为
            #    节内 body 命名空间（1）/ a）、…）—— 这些编号**不占 X.N**。
            #    旧实现不传该参数（默认 False）⇒ 预检按降级前的口径把它们算成
            #    "1.1/1.2"，于是**默认配置（降级开启）下预检报出渲染结果里根本
            #    不存在的重复编号**（实测：渲染为 `1）、项目概况 / 2）、建筑概况 /
            #    1 工程规模 / 1.1 规模描述`，零重复；预检却报「1.1 重复」）。
            #    这类「预检说有、成稿没有」的假阳性会直接把用户引向错误的整改方向
            #    （与 §4.23 已收口的 CMP-01 父节点误报同型）。
            _demote = True
            try:
                from app.config import settings as _s
                _demote = bool(_s.body_subheading_demote_with_children)
            except Exception:  # pragma: no cover - 配置不可用时沿用 fail-safe 默认
                _demote = True
            has_children_demoted = bool(children_map.get(sec.get("id"))) and _demote
            # ✅ R138（L-3 收口）：与落库规范化 / 导出写稿共用同一循环体；
            #    旧实现此处才是唯一正确传 has_children 的一处 —— 三份副本
            #    迟早会漂移，现统一由 helper 承担参数拼装。
            # ✅ R149（2026-10-31）：块选择同源 —— 与落库侧一致排除围栏派生
            #    标题（D-8g），否则同一份正文「预览/落库」与「预检判重宇宙」
            #    编号错位（围栏标题在成稿占号、预览不占）。
            number_body_subheadings(
                select_numbering_blocks(blocks),
                section_prefix=section_prefix, section_level=level,
                sec_id=sec.get("id") or "", has_children=has_children_demoted)
            n_child_ns = _count_child_namespace_subheadings(blocks, section_prefix)
            for block in blocks:
                if block.get("type") != "heading":
                    continue
                fixed = str(block.get("_fixed_text") or "")
                m = re.match(r"^(\d+(?:\.\d+)*)\s", fixed)
                if m:
                    # 内容子标题统一挂在「章作用域」下：章自身内容的 "1.1" 与
                    # 其子孙章节的 "1.1" 在读者眼里同为 "1.1"，属于同一判重宇宙。
                    flag(m.group(1), chapter_id, fixed, sec.get("id"))
        # —— 与 write_section 一致：有 DB 子章节时把子章节层级计数器前移 ——
        # 仅 level>=2 时前移有意义（正文子标题 "{本节编号}.N" 恰与 DB 子章节同级）；
        # level=1 章节的正文子标题 ("1.1") 属于更深命名空间，与其 L2 子章节（"1"/"2"）
        # 不冲突，前移反而会把子章节错误改成 "18/19"，必须与渲染端保持同口径。
        if level >= 2 and children_map.get(sec.get("id")) and n_child_ns:
            _idx = min(level, 7)
            gen.parent_ids[_idx] = sec.get("id")
            gen.counters[_idx] = n_child_ns
            for _i in range(_idx + 1, 8):
                gen.counters[_i] = 0
                gen.parent_ids[_i] = None
        for ch in children_map.get(sec.get("id"), []):
            walk(ch, sec.get("id"), chapter_id)

    for r in roots:
        walk(r, "", None)
    return dupes


def _stored_outline_number(sec: dict) -> str:
    """从章节行提取存储态编号（outline_json.id）；非法/缺失返回 ""。"""
    from app.services.numbering import stored_outline_id
    return stored_outline_id(sec)


def _detect_section_number_mismatch(sections: list[dict]) -> list[dict]:
    """预检：目录存储编号（outline_json.id）与结构位置推算编号的一致性对照。

    ✅ 编号统一（2026-09-25 · 跨模块转换校验）：章节结构的事实源是
    parent_id + sort_order 组成的树；canonical 编号可由树随时重算。
    若存储编号 ≠ 重算编号，说明增删/拖拽/整表重建后未重算或历史脏数据 ——
    正文生成提示词注入的是存储编号（_section_outline_number），导出展示却按
    结构重算（HeadingNumberingGeneratorV2），两侧将不一致（目录编号与正文
    套号错位）。此检测让该类漂移在导出前显式暴露，而不是成稿后人工比对。

    口径说明：
    - 只对照「存储编号合法」的章节；缺失/非法（UUID、空）不报 —— 历史行由
      读路径惰性派生兜底，提示词侧已有「编号缺失不虚构」约束，不构成编号错位；
    - 顺序键与渲染端一致（_section_sort_key：sort_order → level → id）；
    - 上限 20 条，避免整树漂移时响应体爆炸（detail 已含计数语义由前端聚合）。
    """
    by_id: dict = {s.get("id"): s for s in sections if s.get("id")}
    children_map: dict = {}
    roots: list = []
    for s in sections:
        pid = s.get("parent_id")
        if pid and pid in by_id:
            children_map.setdefault(pid, []).append(s)
        else:
            roots.append(s)
    roots.sort(key=_section_sort_key)
    for lst in children_map.values():
        lst.sort(key=_section_sort_key)

    issues: list[dict] = []

    def walk(ns: list, prefix: str):
        for idx, sec in enumerate(ns, 1):
            expected = f"{prefix}.{idx}" if prefix else str(idx)
            stored = _stored_outline_number(sec)
            if stored and stored != expected:
                issues.append({
                    "type": "section_number_mismatch",
                    "section_id": sec.get("id"),
                    "title": (sec.get("title") or "").strip(),
                    "stored": stored,
                    "expected": expected,
                    "detail": (f"章节存储编号「{stored}」与结构位置推算编号「{expected}」"
                               f"不一致（增删/拖拽后未重算或历史脏数据），"
                               f"正文生成与导出展示的编号将错位；重新保存目录或重排可修复"),
                })
            walk(children_map.get(sec.get("id"), []), expected)

    walk(roots, "")
    return issues[:20]


# ✅ E6 Tier 1 · 失效交叉引用检测（DLV-14）—— 2026-09-25
# 图号引用正则：「图 X-Y」（允许零宽空格/全角空格）
_STALE_FIG_REF_RE = re.compile(r"图\s*(\d+-\d+)")
# 表号引用正则：「表 X-Y」
_STALE_TBL_REF_RE = re.compile(r"表\s*(\d+-\d+)")
# 节号引用正则：「第X节」/「第X.X节」/「见X.X节」/「参考X」等
# 捕获组 1=「第…节」里的纯数字路径；组 2=「见/参考/根据」引导的数字路径
_STALE_SEC_REF_RE = re.compile(
    r"第\s*(\d+(?:\.\d+)*)\s*节"
    r"|(?:见|参考|根据|详见)\s*(?:第)?(\d+(?:\.\d+)+)\s*(?:节|章节)?")


def _detect_body_subheading_namespace_conflict(sections: list[dict]) -> list[dict]:
    """预检：正文子标题与 DB 子章节命名空间冲突（DLV-13 · high）。

    ✅ E3（2026-09-25）：有 DB 子章节的章节，正文子标题应走 body 命名空间
    （1）/ a、…）而非 X.X 点分格式。若配置降级关闭，计数器前移可处理撞号；
    但若配置开启降级（默认）、正文仍残留 AI 写的 X.X 点分子标题 → 规范化
    可能被跳过 / 失败 → 导出成稿里正文子标题与 DB 子章节真撞号（"1.1/1.1"）。
    本预检在导出前检测这类残留冲突，严重度 high（阻断导出）。
    """
    # 正则：Markdown 井号后接 X.X 点分编号，或纯文本子标题自带 X.X
    _HEADING_WITH_DOTTED_NUM_RE = re.compile(
        r"^\s*(?:#{1,6}\s+|\*\*?)?\d+(?:\.\d+)+(?:\.{1,})?\s+")

    # 构建 parent_ids 倒排索引
    parent_ids = {s.get("parent_id") for s in sections if s.get("parent_id")}
    # 用 config
    try:
        from app.config import settings as _s
        _demote = _s.body_subheading_demote_with_children
    except Exception:
        _demote = True
    # 只有配置开启降级时才报（关闭时计数器前移处理撞号，属旧预期行为）
    if not _demote:
        return []

    issues: list[dict] = []
    for sec in sections:
        sid = sec.get("id")
        if sid not in parent_ids:
            continue  # 无子章节 → 不可能撞号
        content = sec.get("content") or ""
        if not content.strip():
            continue
        # 扫逐行，检测正文里的点分子标题
        conflicts: list[str] = []
        for line in content.split("\n"):
            if _HEADING_WITH_DOTTED_NUM_RE.search(line):
                conflicts.append(line.strip()[:80])
        if conflicts:
            issues.append({
                "type": "body_subheading_namespace_conflict",
                "section_id": sid,
                "title": (sec.get("title") or "").strip(),
                "detail": (
                    f"本节有 DB 子章节，正文子标题应降级为节内 body 命名空间（1）/ a、…），"
                    f"但仍残留点分格式（{conflicts[:3]}）——"
                    f"规范化可能被跳过，导出成稿将出现「正文子标题/DB 子章节 1.1/1.1」撞号。"
                    f"检查配置 body_subheading_demote_with_children 是否开启，"
                    f"或重跑正文规范化。"
                ),
            })
    return issues[:20]


def collect_figure_table_number_superset(
        sections: list[dict]) -> dict[str, set[str]]:
    """导出引擎**最宽松**可能分配的图号 / 表号集合（预检用超集 → 零新增误报）。

    ✅ R99（2026-10-17 · DLV-14 Tier 2 接线）：本函数补上 ``_detect_stale_cross_references``
    长期缺失的图号 / 表号真实集合。此前 ``collect_export_issues`` 只调用
    ``_detect_stale_cross_references(sections)``（**不传 real_nums**），
    而该函数在 ``real_nums is None`` 时把 ``fig`` / ``tbl`` 初始化为空集合 →
    ``if real_nums["fig"]:`` / ``if real_nums["tbl"]:`` 两个分支**结构性不可达**。
    实测（artifacts/exp94/probe_confirm.py）：正文引用「图9-9 / 表9-9 / 图1-9 / 表1-9」，
    而真实分配只有「图 1-1、图 2-1、表 1-1」→ ``stale_cross_reference`` 产出 **0** 条。
    即 DLV-14 号称检测「图号/表号/节号漂移」，实际只能检测节号（第三条分支恒生效）。

    **为什么是「超集」而不是精确集合**：渲染侧的真实分配与「取码 → 渲染 →
    插入 → 回收图号」交织，且存在若干**只会减少**编号的路径 ——
    同码图表去重（``rendered_charts``）、渲染失败跳过（不占图号）、
    位图缺失跳过、块级异常回滚（``_DocxBodyRollback`` + ``_fig_snap``）。
    只读预检无法复现这些减法，因此本函数只统计**上界**：
      · 遍历顺序 = ``_section_sort_key``（sort_order → level → id）+ 深度优先，
        与 ``_build_docx_sync`` 的 ``write_section`` 逐字一致；
      · 编号原语 = 同一个 ``HeadingNumberingGeneratorV2``（``format_heading``
        的父级重置语义一字不差），章序号取自**同一个** ``_figure_chapter_num``；
      · 块类型判定 = 同一个 ``_parse_content_blocks``；
        ``chart`` / ``image`` / ``ai_image`` 计入图号，``table`` 计入表号。
    于是「真实分配 ⊆ 本超集」成立，预检只报**结构上不可能存在**的引用 ——
    对既有判定是**纯增量**（原先恒 0 的分支变为可触发），不会新增任何误报。

    **降级契约**：目录树异常（根节点不是一级章 / level 非整数 / 遍历抛错）时
    返回空集合，调用方按「无图号/表号集合」降级 = **修复前逐字行为**。
    之所以在根非一级章时放弃：``_figure_chapter_num`` 会回落到「第一个非零计数器」
    （可能是 L2/L3 序号），与本函数按一级章序号推导的口径可能分叉，宁可不报。

    Returns:
        ``{"fig": {"图1-1", ...}, "tbl": {"表1-1", ...}}``；异常/降级时两集合为空。
    """
    empty = {"fig": set(), "tbl": set()}
    try:
        nodes = {s.get("id"): s for s in sections if s.get("id")}
        children_map: dict[str, list] = {}
        roots: list = []
        for s in sections:
            pid = s.get("parent_id") or ""
            if pid and pid in nodes:
                children_map.setdefault(pid, []).append(s)
            else:
                roots.append(s)
        if not roots:
            return empty
        # 根必须是 level==1（否则章序号口径可能分叉 → 降级不报，见 docstring）
        for r in roots:
            try:
                if int(r.get("level") or 1) != 1:
                    return empty
            except (TypeError, ValueError):
                return empty

        from app.services.ai.heading_v2 import HeadingNumberingGeneratorV2
        gen = HeadingNumberingGeneratorV2()
        fig_nums: set[str] = set()
        tbl_nums: set[str] = set()
        # ⚠️ R99 护栏当场抓出的实现缺陷：计数器必须**按章累加**、跨子章节连续。
        # 渲染端 `write_section` 的键是 `f"ch{chapter_num}"`（一个一级章共用一个
        # 计数器，其下所有子章节的图接着往下数）。若这里按「节」各自从 1 起算，
        # 「一级章 + 2 个子章节各 1 图」只会得到 {图1-1}，而渲染真实分配是
        # {图1-1, 图1-2, 图1-3} —— 集合成了渲染结果的**子集**，
        # 对「图1-3」的合法引用会被误报 stale（比漏报更糟）。故此处按章记账。
        fig_last: dict[int, int] = {}
        tbl_last: dict[int, int] = {}

        def walk(secs: list, parent_id: str) -> None:
            for sec in sorted(secs, key=_section_sort_key):
                try:
                    level = max(1, min(int(sec.get("level") or 1), 8))
                except (TypeError, ValueError):
                    level = 1
                # 只推进计数器（标题传空串即可，编号语义与标题无关）
                gen.format_heading(level, "", parent_id)
                chapter_num = _figure_chapter_num(gen)
                try:
                    blocks = _parse_content_blocks(sec.get("content") or "")
                except Exception:
                    # 单章解析失败不影响其它章（与渲染端逐块 fail-soft 同精神）
                    blocks = []
                fig_n = 0
                tbl_n = 0
                for b in blocks:
                    btype = b.get("type")
                    if btype in ("chart", "image", "ai_image"):
                        fig_n += 1
                    elif btype == "table":
                        tbl_n += 1
                base = fig_last.get(chapter_num, 0)
                for i in range(base + 1, base + fig_n + 1):
                    fig_nums.add(f"图{chapter_num}-{i}")
                fig_last[chapter_num] = base + fig_n
                tbase = tbl_last.get(chapter_num, 0)
                for i in range(tbase + 1, tbase + tbl_n + 1):
                    tbl_nums.add(f"表{chapter_num}-{i}")
                tbl_last[chapter_num] = tbase + tbl_n
                sec_id = sec.get("id") or ""
                walk(children_map.get(sec_id, []), sec_id)

        walk(roots, "")
        return {"fig": fig_nums, "tbl": tbl_nums}
    except Exception:
        logger.warning("图号/表号真实集合推导失败（DLV-14 图/表分支按无集合降级）",
                       exc_info=True)
        return empty


def _detect_stale_cross_references(
        sections: list[dict],
        real_nums: dict[str, set[str]] | None = None) -> list[dict]:
    """预检：正文硬编码的图号/表号/节号与真实分配集合不一致时，报告 stale 引用。

    ✅ E6 Tier 1（2026-09-25 · DLV-14）：目录重排/章节增删后，正文里手写的
    「图3-2」「表2-1」「第2.3节」很可能已不在导出时真实分配的编号集合里。
    本函数扫各章 content 的引用，比对 real_nums 里的真实分配集合，不一致时
    返回 stale 项——预检报告里类型=``stale_cross_reference``，严重度 medium，
    不阻断导出（用户可人工确认引用仍有效）。

    Args:
        sections: 章节列表（含 id / parent_id / level / outline_json / content）。
        real_nums: 可选的预构建真实编号集合。None 时从 outline_json 内部推导
            节号集合（图号/表号集合为空——需要调用方显式传入导出引擎运行结果时
            才能同时检测图号/表号漂移）。
            结构：{"fig": set[str], "tbl": set[str], "sec": set[str]}

    Returns:
        issue 列表；元素为 {"type": "stale_cross_reference", "section_id", "title",
        "ref_type": "fig/tbl/sec", "ref": 原文引用片段, "detail": 说明}
    """
    # real_nums 默认推导：节号集合从 outline_json → stored_id_to_display +
    # stored_id_to_prefix + 第X章形式。
    if real_nums is None:
        real_nums = {"fig": set(), "tbl": set(), "sec": set()}
    else:
        # 防御性拷贝，避免调用方的 set 被误修改
        real_nums = {
            "fig": set(real_nums.get("fig", set()) or set()),
            "tbl": set(real_nums.get("tbl", set()) or set()),
            "sec": set(real_nums.get("sec", set()) or set()),
        }
    # 内部推导节号集合（如果调用方没传）
    if not real_nums.get("sec"):
        from app.services.numbering import stored_id_to_display, stored_id_to_prefix
        for s in sections:
            oj = s.get("outline_json") or ""
            sid = ""
            if isinstance(oj, dict):
                sid = str(oj.get("id") or "")
            else:
                try:
                    sid = str(json.loads(oj).get("id") or "")
                except Exception:
                    sid = ""
            if not sid:
                continue
            display = stored_id_to_display(sid)
            prefix = stored_id_to_prefix(sid)
            if display:
                real_nums["sec"].add(display)
                # 裸数字路径（如 "2" / "2.1" / "2.3"）也加进去，覆盖
                # 「见 2.3 节」「详见 2 章」这类省略「第/节」的引用写法
                real_nums["sec"].add(sid.split(".", 1)[0])
                if prefix and prefix != sid:
                    real_nums["sec"].add(prefix)
                    for p in prefix.split("."):
                        real_nums["sec"].add(p)
            if prefix:
                real_nums["sec"].add(prefix)

    issues: list[dict] = []
    seen: set[tuple] = set()  # 去重键：(section_id, ref_type, ref)

    def _add(section_id: str, title: str, ref_type: str, ref: str, detail: str):
        key = (section_id, ref_type, ref)
        if key in seen:
            return
        seen.add(key)
        issues.append({
            "type": "stale_cross_reference",
            "section_id": section_id,
            "title": title,
            "ref_type": ref_type,
            "ref": ref,
            "detail": detail,
        })

    for sec in sections:
        content = sec.get("content") or ""
        if not content.strip():
            continue
        sec_id = sec.get("id", "")
        title = (sec.get("title") or "").strip()

        # 图号
        if real_nums["fig"]:
            for m in _STALE_FIG_REF_RE.finditer(content):
                ref = m.group(1)
                full_ref = f"图{ref}"
                if full_ref not in real_nums["fig"]:
                    _add(sec_id, title, "fig", full_ref,
                         f"正文引用「{full_ref}」，但本次导出只分配了 "
                         f"{sorted(real_nums['fig'])}；目录重排或增删章节后图号已漂移，"
                         f"建议检查引用是否仍有效")

        # 表号
        if real_nums["tbl"]:
            for m in _STALE_TBL_REF_RE.finditer(content):
                ref = m.group(1)
                full_ref = f"表{ref}"
                if full_ref not in real_nums["tbl"]:
                    _add(sec_id, title, "tbl", full_ref,
                         f"正文引用「{full_ref}」，但本次导出只分配了 "
                         f"{sorted(real_nums['tbl'])}；目录重排后表号可能漂移")

        # 节号（内部总是推导 sec 集合）
        for m in _STALE_SEC_REF_RE.finditer(content):
            # 两个捕获组：组 1=「第…节」形式；组 2=引导词形式
            ref = (m.group(1) or m.group(2) or "").strip()
            if not ref:
                continue
            ref_text = f"第{ref}节" if m.group(1) else ref
            # 节号真实集合包含多种口径（第X章 / X / X.X / prefix），逐一比对
            in_real = (
                ref_text in real_nums["sec"]
                or ref in real_nums["sec"]
                or f"第{ref}章" in real_nums["sec"]
            )
            if not in_real:
                # 节号漂移最常见——说明目录重排后原章节不存在，需要用户确认
                _add(sec_id, title, "sec", ref_text,
                     f"正文引用「{ref_text}」，但当前目录结构中无此编号；"
                     f"章节可能已被删除/重排，建议人工确认引用目标")

    # 上限 20，避免超长文档整树漂移时响应体爆炸
    return issues[:20]




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
    result = await collect_export_issues(scheme_id, db)
    result["preflight_summary"] = await _readiness_preflight_summary(scheme_id, db)
    # ✅ 新增（2026-09-24）：占位符基线落库（监控旁路，失败不阻断预检返回）。
    #    每次预检落一行快照，GET /export/placeholder-history 供趋势对比。
    await _record_placeholder_baseline(
        scheme_id, result.get("placeholder_report") or {}, db)
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
    return await build_report_for_scheme(scheme_id, db,
                                         occurrence_cap=DEFAULT_OCCURRENCE_CAP)


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
            (str(uuid.uuid4()), scheme_id,
             int(report.get("total") or 0),
             int(report.get("formatted_total") or 0),
             int(report.get("bare_total") or 0),
             int(report.get("fuzzy_total") or 0),
             int(report.get("field_count") or 0),
             int(report.get("section_count") or 0)),
            what="placeholder_baselines 写入")
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
                what="placeholder_baselines 裁剪")
        await db.commit()
    except Exception as e:
        logger.warning("placeholder_baselines 写入失败（不影响预检返回）: scheme=%s err=%s",
                       scheme_id, e)
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
        (scheme_id, limit))
    # ✅ R13 守卫（2026-09-30 引入，2026-10-06 收敛到 _db_fetch_all 单一出口）：
    #    全局单连接下 db.execute 可能返回 None（连接/事务异常）。本端点是只读
    #    监控旁路，降级为空历史即可，不应 500。
    rows = [dict(r) for r in await _db_fetch_all(
        cur, what="placeholder_history")]
    return {"scheme_id": scheme_id, "history": rows, "keep": _PLACEHOLDER_BASELINE_KEEP}


async def collect_export_issues(scheme_id: str, db, *,
                                strict_read: bool = False) -> dict:
    """导出预检的核心计算（**不写库**，可被多处复用）。

    ✅ G1：此前本计算内联在 ``export_check`` 路由里，就绪度总检无法复用，
    只能各自实现一份判定 → 口径分叉。现抽成独立函数：
      · 导出页 → 展示问题清单 + ``preflight_summary``；
      · readiness_overview → 经 ``export_issues_to_findings`` 并入六维评分。

    Args:
        strict_read: **加法式**开关，缺省 ``False`` 时全部查询沿用 fail-soft
            （读失败降级为空结果集），**导出页行为逐字不变**。
            总检侧传 ``True``：把「查询失败」与「确实无行」区分开，抛
            ``DBReadError`` 让 ``compliance._readiness_overview_compute``
            的 ``except`` 真正生效。

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
        " WHERE scheme_id=? ORDER BY sort_order, level, id", (scheme_id,))
    sections = [dict(r) for r in await _db_fetch_all(
        cur, what="collect_export_issues:sections", strict=strict_read)]
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
        # 孤立节点：parent_id 非空但指向不存在的父节点
        pid = s.get("parent_id", "")
        if pid and pid not in section_ids:
            issues.append({"type": "orphan_node", "section_id": s["id"],
                           "title": s["title"], "parent_id": pid})
        # 空叶子章节（无内容且无子女；✅ R98 判据补 strip，与共用口径对齐
        #    「content 只有空白也算空」）
        if not (s.get("content") or "").strip() and not has_children:
            issues.append({"type": "empty_section", "section_id": s["id"], "title": s["title"]})
        # 状态为 empty 但实际有内容（数据不一致）
        if s.get("status") == "empty" and (s.get("content") or wc > 0):
            issues.append({"type": "status_inconsistent", "section_id": s["id"],
                           "title": s["title"], "detail": "status=empty 但有内容"})
        # 低字数叶子章节（可能生成不完整）
        if not has_children and 0 < wc < 100:
            issues.append({"type": "low_word_count", "section_id": s["id"],
                           "title": s["title"], "word_count": wc})
    # 图表未生成检查
    cur = await db.execute(
        "SELECT chart_type, status FROM chart_predictions WHERE scheme_id=?", (scheme_id,))
    chart_rows = await _db_fetch_all(
        cur, what="collect_export_issues:chart_predictions", strict=strict_read)
    chart_total = len(chart_rows)
    # ✅ 状态口径兼容："generated" 是正文同步图表与 fix-mermaid 的历史写法，
    #    语义上同样是"代码已生成完毕"，必须与 "done" 一并视为已完成，
    #    否则正文里已内嵌的图表会被预检误报为「图表未生成」。
    _DONE_STATUSES = ("done", "generated")
    chart_done = sum(1 for r in chart_rows if r["status"] in _DONE_STATUSES)
    chart_failed = [dict(r) for r in chart_rows
                    if r["status"] not in _DONE_STATUSES and r["status"] != "pending"]
    # ✅ P0 收尾（对齐 OpenBidKit 图表未生成告警）：正文同步图表登记即 done，
    #    仅 ai_image 在生成前为 pending（占位）。pending 不计入已完成，也不计入
    #    failed（非生成失败），但导出前应提醒用户「有配图尚未生成」，避免导出后
    #    正文缺少图片。这里统一把 pending 记为 chart_ungenerated 告警。
    chart_pending = [dict(r) for r in chart_rows if r["status"] == "pending"]
    for cf in chart_failed:
        issues.append({"type": "chart_failed", "chart_type": cf["chart_type"],
                       "status": cf["status"]})
    for cp in chart_pending:
        issues.append({"type": "chart_ungenerated", "chart_type": cp["chart_type"],
                        "status": "pending",
                        "detail": (
                            "该图表（AI 配图）正文仅占位；导出时将自动生成真实图片"
                            "（失败则该图跳过、不占图号）")})

    # ✅ P0-1：章节审核状态检查（generate_content 后自动置 pending）
    #    旧实现导出前无法感知"正文写好但尚未过审"，导致未审核章节混进交付文档。
    #    这里把 review_status!=approved 的已生成章节统计出来作为 warning。
    review_pending = 0
    review_rejected = 0
    review_missing = 0
    for s in sections:
        if not s.get("content"):
            continue  # 未生成正文的章节不参与审核检查
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
        issues.append({"type": "review_pending", "count": review_pending,
                       "detail": f"{review_pending} 章正文已生成但审核状态为 pending"})
    if review_rejected:
        issues.append({"type": "review_rejected", "count": review_rejected,
                       "detail": f"{review_rejected} 章正文审核未通过"})
    if review_missing:
        issues.append({"type": "review_missing", "count": review_missing,
                       "detail": f"{review_missing} 章正文已生成但无审核记录"})

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
            (ba_pid,))
        ba_required = [dict(r) for r in await _db_fetch_all(
            cur2, what="collect_export_issues:bid_analysis_items", strict=strict_read)]
        ba_total = len(ba_required)
        ba_success = sum(1 for r in ba_required if r["status"] == "success")
        ba_failed = [r for r in ba_required if r["status"] not in ("success", "idle")]
        if ba_total > 0 and ba_success < ba_total:
            _ba_summary = {"required_total": ba_total, "success": ba_success,
                           "failed_count": len(ba_failed),
                           "failed_items": [{"item_id": r["item_id"],
                                            "label": r["label"],
                                            "status": r["status"]}
                                           for r in ba_failed[:10]]}
            issues.append({
                "type": "bid_analysis_incomplete",
                "required_total": ba_total,
                "success": ba_success,
                "failed_count": len(ba_failed),
                "detail": f"必选项 {ba_total - ba_success}/{ba_total} 未成功提取"})

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
    _facts_summary = {"total": 0, "unresolved": 0, "simulated": 0,
                      "conflicted": 0, "stale": 0, "blocked": 0}
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
                (scheme_id, ba_pid))
            frow = await _db_fetch_one(
                cur, what="collect_export_issues:global_facts 统计",
                strict=strict_read)
            if frow:
                _facts_summary = {k: int(frow[k] or 0) for k in _facts_summary}
                _facts_status = {"ok": True, "code": "ok"}
        blocked_fact_count = int((_facts_summary.get("blocked") or 0))
        if blocked_fact_count > 0:
            issues.append({
                "type": "global_facts_blocked", "count": blocked_fact_count,
                "detail": (f"全局事实待处理 {blocked_fact_count} 项："
                           f"未确认 {_facts_summary['unresolved']}、"
                           f"模拟 {_facts_summary['simulated']}、"
                           f"冲突 {_facts_summary['conflicted']}、"
                           f"来源过期 {_facts_summary['stale']}")})
    except Exception:
        _facts_status = {"ok": False, "code": "query_failed"}
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
    try:
        from app.config import settings as _s
        if _s.crossref_stale_detect_enabled:
            issues.extend(_detect_stale_cross_references(
                sections,
                real_nums=collect_figure_table_number_superset(sections)))
    except Exception:
        # 配置缺失 / 函数签名变更等异常 → 静默降级（不阻断预检主流程）
        pass

    # ✅ E3（2026-09-25 · DLV-13 high）：正文子标题与 DB 子章节命名空间冲突。
    #    配置开启降级（默认）、但正文仍残留 AI 写的 X.X 点分子标题 → 规范化可能被
    #    跳过，导出成稿真撞号。本预检阻断导出（high 级），要求先修复再交付。
    try:
        issues.extend(_detect_body_subheading_namespace_conflict(sections))
    except Exception:
        pass

    empty_ratio = _cov["empty_ratio"]  # ✅ R98：与总检共用口径（见上方 _cov）
    # ✅ 新增（2026-09-19）：内容体检（占位符 / HTML 残留 / LaTeX 空参数 /
    #    投标用语 / 未闭合图表围栏）—— 独立字段，不进 issues（避免改变既有
    #    前端"问题计数"语义），供导出前弹窗与"待补充清单"定位使用。
    # ✅ R133-C：导出预检同样携带 Mermaid 服务健康探测结论（2s 硬超时 +
    #    fail-soft，见 _probe_mermaid_status），与导出主链路同口径。
    content_audit = audit_content(sections, mermaid_status=await _probe_mermaid_status())
    # ✅ 新增（2026-09-24）：《待补充清单》精简版（additive 键，不改变既有字段）。
    #    只有聚合计数与按字段/章节聚合，不带逐条 occurrences（响应体控制），
    #    逐条清单走 GET /export/placeholder-report 专用端点。
    try:
        _ph_full = build_placeholder_report(sections)
        placeholder_report = {k: _ph_full[k] for k in (
            "total", "formatted_total", "bare_total", "fuzzy_total",
            "bracket_total",
            "field_count", "section_count", "by_field", "by_section")}
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
        "placeholder_report": placeholder_report,
        "review_summary": {
            "pending": review_pending, "rejected": review_rejected,
            "missing": review_missing, "unapproved": review_unapproved},
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
                "/".join(sorted({str(i.get("type")) for i in items})), rid)
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
        findings.append({
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
        })
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
            " ORDER BY created_at DESC, rowid DESC LIMIT 1", (scheme_id,))
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


async def _record_export_review_trace(db, scheme_id: str, fmt: str,
                                      round_no: int, filename: str) -> None:
    """✅ G12：导出成稿写回审核留痕（工程可追溯性硬要求）。

    背景：导出是整条链路里唯一"内容离开系统"的动作，但此前 export.py 全文件
    没有任何 review_records 写入 —— 事后**无法回答"这份成稿是不是评审通过后的
    版本"**。现在每次成功导出落一条**方案级**评审记录：

    - ``from_status = to_status = 方案当前审核状态``（只留痕，不改变状态机）；
    - ``comment`` 含格式 / 导出轮次 / 文件名，可回溯到具体那份文档。

    写入失败只记日志并 rollback，**绝不阻塞导出** —— 文件已是用户此刻要的东西。
    """
    from app.routers.review import _scheme_project_id, _write_record

    try:
        cur = await db.execute(
            "SELECT name, review_status FROM schemes WHERE id=?", (scheme_id,))
        row = await _db_fetch_one(cur, what="_record_export_review_trace:schemes")
        status = (row["review_status"] or "") if row else ""
        name = (row["name"] or "") if row else scheme_id
        await _write_record(
            db, scheme_id, "", name, status, status, "系统",
            f"已导出 {fmt.upper()}（第 {round_no} 轮）：{filename}",
            await _scheme_project_id(db, scheme_id))
        await db.commit()
    except Exception as e:
        logger.warning("导出留痕写入失败（不影响导出结果）: %s", e)
        try:
            await db.rollback()
        except Exception:
            pass


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
        "WHERE project_id=? ORDER BY is_default DESC, updated_at DESC", (pid,))
    rows = await _db_fetch_all(cur, what="list_export_presets")
    return {"presets": [{
        "id": r["id"], "name": r["name"],
        "config": json.loads(r["config_json"] or "{}"),
        "is_default": bool(r["is_default"]),
        "updated_at": r["updated_at"],
    } for r in rows]}


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
        "VALUES (?,?,?,?,0,?,?)", (preset_id, pid, name, json.dumps(config, ensure_ascii=False), now, now),
        what="create_export_preset")
    await db.commit()
    return {"id": preset_id, "name": name, "config": config}


@router.put("/presets/{preset_id}")
async def update_export_preset(scheme_id: str, preset_id: str, body: dict, db=Depends(get_db)):
    """更新预设（名称 / 配置）。"""
    pid = await _preset_project_id(db, scheme_id)
    cur = await db.execute(
        "SELECT id FROM export_presets WHERE id=? AND project_id=?", (preset_id, pid))
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
    await _db_exec(db, f"UPDATE export_presets SET {','.join(sets)} WHERE id=? AND project_id=?",
                   params, what="update_export_preset")
    await db.commit()
    return {"ok": True}


@router.post("/presets/{preset_id}/default")
async def set_default_export_preset(scheme_id: str, preset_id: str, db=Depends(get_db)):
    """将某预设设为项目默认（同项目其它预设取消默认）。"""
    pid = await _preset_project_id(db, scheme_id)
    cur = await db.execute(
        "SELECT id FROM export_presets WHERE id=? AND project_id=?", (preset_id, pid))
    if not await _db_fetch_one(cur, what="set_default_export_preset 存在性"):
        raise HTTPException(404, "预设不存在")
    await _db_exec(db, "UPDATE export_presets SET is_default=0 WHERE project_id=?",
                   (pid,), what="set_default_export_preset 清默认")
    await _db_exec(db, "UPDATE export_presets SET is_default=1 WHERE id=? AND project_id=?",
                   (preset_id, pid), what="set_default_export_preset 设默认")
    await db.commit()
    return {"ok": True}


@router.delete("/presets/{preset_id}")
async def delete_export_preset(scheme_id: str, preset_id: str, db=Depends(get_db)):
    """删除预设。"""
    pid = await _preset_project_id(db, scheme_id)
    await _db_exec(db, "DELETE FROM export_presets WHERE id=? AND project_id=?",
                   (preset_id, pid), what="delete_export_preset")
    await db.commit()
    return {"ok": True}


# ✅ 修复：本模块原自带一份 20 条的关键字映射，与 _chart_pipeline（8 条）分叉。
# 现已统一到 chart_validators 的唯一映射表，保留同名别名以兼容既有引用点与诊断脚本。
# 注：原先 pie / xychart 归一为 comparison（而非 "pie"/"xychart"）是有意为之——
# 渲染器只认识 comparison，产出渲染器不认识的类型会让 PIL 兜底必然失败，
# 且因缺少 _CHART_TYPE_MAP 条目而拿不到「对比图」默认图题（该语义在统一表中保持）。
_MERMAID_TYPE_MAP = MERMAID_KEYWORD_TO_CHART_TYPE


from app.services.content_blocks import (
    _LEAD_IN_HINT_RE,  # ✅ 2026-10-05（D4）：引导语识别正则收敛到唯一实现，删除本地副本
    _RE_TABLE_SEP,  # ✅ R145（2026-10-29）：GFM 表格分隔行正则，_add_table_from_markup
    LEAD_IN_MAX_CHARS,  # ✅ R48（2026-10-06）：引导语长度上限收敛到唯一实现，
    _cn_pure_to_int,
    _compute_subheading,  # noqa: F401  兼容再导出：测试/诊断脚本经 app.routers.export 引用
    _detect_plain_heading,  # noqa: F401  兼容再导出：测试与诊断脚本经 app.routers.export 引用
    #                          消除本模块此前的裸 `60` 字面量（同一判据第 3 份副本）。
    _parse_content_blocks,
    #                        动态定位分隔行（表头前短行前置后不再固定 index 1）
    _strip_duplicate_leading_title,
    _strip_title_number,
    number_body_subheadings,
    select_numbering_blocks,
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
        return _CN_ORDINAL_DIGITS[n // 10] + "十" + (
            _CN_ORDINAL_DIGITS[n % 10] if n % 10 else "")
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


def _breaks_ordered_sequence(block: dict) -> bool:
    """该内容块是否应**中断**有序列表的连续编号（R117 · 2026-10-20）。

    ## 为什么不再「遇非列表块一律重置」

    旧契约是「遇到任何非 list_item 块就把整条有序序列清零」。但 AI 生成的中文
    施工方案里，列举项之间**天然夹着正文段落、表格、图片**：

        1、被吊构件参数梳理
        （一段 120 字的说明）
        表 2-2 主要吊装构件参数表
        （表格）
        1、汽车吊选型依据        ← 作者意图是「2、」

    实测（室外汽车吊吊装施工专项方案 第 1 轮导出）6 个章节的顶格列举全部被
    渲染成一串 ``1、`` / ``1.`` —— 编号失去区分度，读者无法引用「第 2 条」。
    对这类文档，「列表被正文打断」不是两个独立列表，而是同一组列举的延续。

    ## 只在这两种情况下重置

    1. **标题块** —— 结构边界，新标题下的枚举必然是新序列（沿用既有语义）；
    2. **以冒号/中文冒号收尾的段落** —— 引导句（"主要参数如下："），其后
       是一次**新**的列举，必须从 1 起算。

    其余块（正文段落、表格、图片、图表、引用、分隔线、代码）一律**不重置**，
    序号续接。真正需要重启的第二种情况（同一层换标记样式 ``1.`` → ``1、``）
    仍由「该层 marker 变化 → seq 归零」的既有契约兜底，不受本函数影响。
    """
    btype = block.get("type")
    if btype == "heading":
        return True
    if btype == "paragraph":
        text = str(block.get("text") or "").strip()
        return text.endswith(("：", ":"))
    return False


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


def _ensure_body_text_style(doc, first_line_indent) -> str:
    """获取或创建正文段落样式，返回样式名。

    ✅ R127（2026-10-22 · DOCX 标准符合性）：旧实现正文段落使用 Normal +
    **直接格式**（first_line_indent / space_after 逐段写进 pPr）。OOXML 虽允许
    直接格式，但长文档中 1300+ 段重复内联格式会放大文件、且无法通过样式统一
    调整。现收敛为 ``Body Text CN`` 段落样式，正文只引用样式、不再写直接缩进。
    """
    from docx.enum.style import WD_STYLE_TYPE
    from docx.oxml.ns import qn
    from docx.shared import Pt

    name = "Body Text CN"
    for st in doc.styles:
        if st.type == WD_STYLE_TYPE.PARAGRAPH and st.name == name:
            return name
    style = doc.styles.add_style(name, WD_STYLE_TYPE.PARAGRAPH)
    style.base_style = doc.styles["Normal"]
    pf = style.paragraph_format
    pf.first_line_indent = first_line_indent
    pf.space_after = Pt(3)
    ppr = style.element.get_or_add_pPr()
    # 正文不属于大纲层级，清除继承的 outlineLvl，避免误入目录
    for ol in ppr.findall(qn("w:outlineLvl")):
        ppr.remove(ol)
    return name


# ---------------------------------------------------------------------------
# ✅ R127（2026-10-22）：列表使用 DOCX 原生编号（numbering.xml + w:numPr）
# ---------------------------------------------------------------------------
# 旧实现把「1、」「（一）」「1. 」等前缀作为**正文文本**写入 run，并给段落挂
# List Paragraph 样式 + 悬挂缩进。视觉上正确，但 Word/WPS 不认为它们是列表：
#   · 无法使用「继续编号 / 重新编号 / 调整级别」；
#   · 复制到其它文档后编号变成死文本；
#   · 预检/辅助技术无法识别列表语义。
# 现改为：前缀不再写入 run，由 numbering.xml 的 abstractNum/lvlText 渲染；
# 每个列表实例使用独立 w:num（带 startOverride，保证与导出器既有分层计数一致）。
_NUMBERING_LEVELS = 9          # Word 最多 9 级
_LIST_INDENT_STEP_TWIPS = 420  # 每级 0.74cm ≈ 420 twips


def _marker_numbering_spec(marker: str, level: int) -> tuple[str, str, str]:
    """返回 (numFmt, lvlText, suff)；level 从 0 开始。"""
    n = level + 1
    if marker == "num_dun":
        return "decimal", f"%{n}、", "nothing"
    if marker == "num_paren_r":
        return "decimal", f"%{n}）", "nothing"
    if marker == "num_paren_lr":
        return "decimal", f"（%{n}）", "nothing"
    if marker == "paren_ascii":
        # 尾随空格由 w:suff="space" 提供，避免与 lvlText 内空格叠加
        return "decimal", f"%{n})", "space"
    if marker == "cn_num_paren":
        # ST_NumberFormat 的 chineseCounting = 一、二、三…… 与 _cn_ordinal 一致
        return "chineseCounting", f"（%{n}）", "nothing"
    if marker == "bullet":
        return "bullet", "•", "space"
    return "decimal", f"%{n}.", "space"


class _DocxListNumbering:
    """为一次 DOCX 构建管理列表编号定义（abstractNum / num）。"""

    def __init__(self, doc):
        from docx.oxml.ns import qn

        self._qn = qn
        self._root = doc.part.numbering_part.element
        used_abs = [int(a.get(qn("w:abstractNumId")) or 0)
                    for a in self._root.findall(qn("w:abstractNum"))]
        used_num = [int(n.get(qn("w:numId")) or 0)
                    for n in self._root.findall(qn("w:num"))]
        self._next_abstract = (max(used_abs) + 1) if used_abs else 1
        self._next_num = (max(used_num) + 1) if used_num else 1
        self._abstract_by_sig: dict[tuple[str, ...], int] = {}
        self._bullet_num_id: int | None = None

    def _ensure_abstract(self, signature: tuple[str, ...]) -> int:
        from docx.oxml import parse_xml
        from docx.oxml.ns import nsdecls

        if signature in self._abstract_by_sig:
            return self._abstract_by_sig[signature]
        aid = self._next_abstract
        self._next_abstract += 1
        lvls = []
        for lvl in range(_NUMBERING_LEVELS):
            if lvl < len(signature):
                marker = signature[lvl]
            elif signature and signature[0] == "bullet":
                marker = "bullet"
            else:
                marker = "ascii"
            num_fmt, lvl_text, suff = _marker_numbering_spec(marker, lvl)
            left = _LIST_INDENT_STEP_TWIPS * (lvl + 1)
            lvls.append(
                f'<w:lvl w:ilvl="{lvl}">'
                f'<w:start w:val="1"/>'
                f'<w:numFmt w:val="{num_fmt}"/>'
                f'<w:suff w:val="{suff}"/>'
                f'<w:lvlText w:val="{lvl_text}"/>'
                f'<w:lvlJc w:val="left"/>'
                f'<w:pPr><w:ind w:left="{left}" '
                f'w:hanging="{_LIST_INDENT_STEP_TWIPS}"/></w:pPr>'
                f'</w:lvl>')
        xml = (f'<w:abstractNum {nsdecls("w")} '
               f'w:abstractNumId="{aid}">'
               f'<w:multiLevelType w:val="hybridMultilevel"/>'
               f'{"".join(lvls)}</w:abstractNum>')
        el = parse_xml(xml)
        # CT_Numbering 要求 abstractNum 全部排在 num 之前
        first_num = self._root.find(self._qn("w:num"))
        if first_num is not None:
            first_num.addprevious(el)
        else:
            self._root.append(el)
        self._abstract_by_sig[signature] = aid
        return aid

    def new_num(self, signature: tuple[str, ...],
                start_values: list[int]) -> int:
        """新建列表实例；start_values = 各层级当前序号（Word 从该值起显示）。"""
        from docx.oxml import parse_xml
        from docx.oxml.ns import nsdecls

        aid = self._ensure_abstract(tuple(signature))
        nid = self._next_num
        self._next_num += 1
        overrides = "".join(
            f'<w:lvlOverride w:ilvl="{i}">'
            f'<w:startOverride w:val="{max(1, int(v))}"/>'
            f'</w:lvlOverride>'
            for i, v in enumerate(start_values))
        xml = (f'<w:num {nsdecls("w")} w:numId="{nid}">'
               f'<w:abstractNumId w:val="{aid}"/>{overrides}</w:num>')
        el = parse_xml(xml)
        cleanup = self._root.find(self._qn("w:numIdMacAtCleanup"))
        if cleanup is not None:
            cleanup.addprevious(el)
        else:
            self._root.append(el)
        return nid

    def bullet_num(self) -> int:
        if self._bullet_num_id is None:
            self._bullet_num_id = self.new_num(("bullet",), [])
        return self._bullet_num_id

    def apply(self, paragraph, num_id: int, level: int) -> None:
        """把 w:numPr 写入段落 pPr（按 CT_PPr 顺序插到正确位置）。"""
        from docx.oxml import OxmlElement

        ppr = paragraph._p.get_or_add_pPr()
        num_pr = OxmlElement("w:numPr")
        ilvl = OxmlElement("w:ilvl")
        ilvl.set(self._qn("w:val"), str(max(0, int(level))))
        num_id_el = OxmlElement("w:numId")
        num_id_el.set(self._qn("w:val"), str(int(num_id)))
        num_pr.append(ilvl)
        num_pr.append(num_id_el)
        ppr.insert_element_before(
            num_pr,
            "w:suppressLineNumbers", "w:pBdr", "w:shd", "w:tabs",
            "w:suppressAutoHyphens", "w:kinsoku", "w:wordWrap",
            "w:overflowPunct", "w:topLinePunct", "w:autoSpaceDE",
            "w:autoSpaceDN", "w:bidi", "w:adjustRightInd", "w:snapToGrid",
            "w:spacing", "w:ind", "w:contextualSpacing", "w:mirrorIndents",
            "w:suppressOverlap", "w:jc", "w:textDirection",
            "w:textAlignment", "w:textboxTightWrap", "w:outlineLvl",
            "w:divId", "w:cnfStyle", "w:rPr", "w:sectPr", "w:pPrChange")


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
_INLINE_RE = re.compile(
    r"\*\*\*(?P<bi>.+?)\*\*\*"
    r"|\*\*(?P<b>.+?)\*\*"
    r"|\*(?P<i>.+?)\*"
    r"|~~(?P<s>.+?)~~"
    r"|`(?P<c>[^`]+)`"
    r"|!?\[(?P<l>[^\]\n]+)\]\((?P<lu>[^)\s]*)\)"
)


def _is_cjk_font(name: str) -> bool:
    """字体名是否为中日韩字体名（含 CJK 统一表意文字）——西文字体名不含。"""
    return any("一" <= ch <= "鿿" for ch in str(name or ""))


def _set_run_font(run, name: str, size=None, *, bold=None, italic=None,
                  strike=None, color=None):
    """统一设置 run 字体：中文字体写 w:eastAsia，西文槽按文字标准分流。

    ✅ BUG 修复：旧实现只写 `run.font.name`（= w:ascii + w:hAnsi），中文实际
    由主题的 eastAsia 字体接管，在"宋体正文 + 黑体标题"等混排场景下中文会
    悄悄回退成默认字体，与配置不符。
    ✅ R130（文字标准 · 西文统一 Times New Roman）：name 为中文字体
    （宋体/黑体/仿宋/微软雅黑…）时，w:ascii/w:hAnsi 改写 Times New Roman、
    中文仍走 eastAsia=name —— 成稿里的数字/英文统一衬线西文，与 Normal /
    Heading 样式级口径一致；name 本身是西文字体（Consolas 等行内代码）时
    行为与旧实现完全相同（ascii/hAnsi/eastAsia 三槽同值）。
    """
    from docx.oxml.ns import qn
    latin = FONT_LATIN if _is_cjk_font(name) else name
    run.font.name = latin
    rfonts = run._element.get_or_add_rPr().get_or_add_rFonts()
    rfonts.set(qn("w:eastAsia"), name)
    if size is not None:
        from docx.shared import Pt
        run.font.size = Pt(size)
    if bold is not None:
        run.bold = bold
    if italic is not None:
        run.italic = italic
    if strike is not None:
        run.font.strike = strike
    if color is not None:
        from docx.shared import RGBColor
        run.font.color.rgb = RGBColor(*color)
    return run


def _add_markdown_runs(paragraph, text: str):
    """解析 Markdown 行内标记并添加为带格式的 run（公式已剥离的纯文本段）。

    支持 ``**粗体**`` / ``*斜体*`` / ``***粗斜体***`` / ``~~删除线~~`` /
    ``` `行内代码` ``` / ``[显示文本](链接)`` 与 ``![图片说明](url)``。

    ✅ 增强：链接/图片只保留显示文本 —— 旧实现把 `[附件1](http://…)` 这类
    Markdown 原文整段写进成稿，交付文档里出现裸语法。
    """
    pos = 0
    for m in _INLINE_RE.finditer(text):
        if m.start() > pos:
            paragraph.add_run(text[pos:m.start()])
        if m.group("bi"):
            run = paragraph.add_run(m.group("bi"))
            run.bold = True
            run.italic = True
        elif m.group("b"):
            run = paragraph.add_run(m.group("b"))
            run.bold = True
        elif m.group("i"):
            run = paragraph.add_run(m.group("i"))
            run.italic = True
        elif m.group("s"):
            run = paragraph.add_run(m.group("s"))
            run.font.strike = True
        elif m.group("c") is not None:
            _set_run_font(paragraph.add_run(m.group("c")), "Consolas")
        elif m.group("l") is not None:
            paragraph.add_run(m.group("l"))
        pos = m.end()
    if pos < len(text):
        paragraph.add_run(text[pos:])


def _add_omml_formula(paragraph, latex: str, display: bool, whole_para: bool):
    """将 LaTeX 公式转为 Word 原生公式（OMML）节点插入段落。

    - ``display`` 且 ``whole_para``：插入块级公式 ``<m:oMathPara>``（居中独占段）；
    - 其余：插入内联公式 ``<m:oMath>``。

    ✅ R129 三级降级（引入易标式「复杂公式降级为图片」）：
      1) OMML 原生公式（首选，可编辑、随字体缩放）；
      2) OMML 失败（不支持的结构 / 空参数防护网拒绝）→ matplotlib mathtext
         渲染为 PNG 行内图片（可选依赖，未安装时跳过此级）；
      3) 图片也失败 → 回退为原文文本（``$...$``），保证不丢内容。
    每级失败都留痕（formula_image_fallback 计数进 X-Fix-Stats）。
    """
    from docx.oxml import parse_xml
    from docx.shared import Pt

    try:
        if display and whole_para:
            xml = docx_math.latex_to_omml_display(latex)
        else:
            xml = docx_math.latex_to_omml(latex)
        node = parse_xml(xml)
        paragraph._p.append(node)
        return
    except Exception:  # pragma: no cover - 转换失败兜底
        pass
    # 降级第 2 级：LaTeX → PNG 图片（best-effort，matplotlib 可选依赖）
    try:
        png = docx_math.latex_to_png_bytes(latex)
    except Exception:
        png = None
    if png:
        try:
            _fix_stats()["formula_image_fallback"] += 1
            run = paragraph.add_run()
            # 高度对齐正文字号，宽度按图片纵横比自动适配
            run.add_picture(BytesIO(png), height=Pt(14))
            logger.warning("LaTeX 公式 OMML 失败，降级为图片: %r", latex[:60])
            return
        except Exception:
            logger.warning("公式图片嵌入失败，回退原文: %r", latex[:60])
    # 降级第 3 级：保留原文文本（行为与 R129 之前逐字一致）
    logger.warning("LaTeX 公式转换失败，保留原文: %r", latex[:60])
    paragraph.add_run(("$$" if display else "$") + latex + ("$$" if display else "$"))


def _collapse_redundant_spaces(text: str) -> str:
    """保守压缩纯文本片段中的冗余空白。

    ✅ F2 修复：dangling 残式（如 ``$N = $``）右值位置为空，公式节点前后
    残留的空格会在成稿里形成连续双空格。这里仅把 **2 个及以上连续的
    空格/制表符** 归一为单个空格，不触碰单个空格、换行与中文，避免误伤
    正常排版与中英文混排间距。
    """
    if not text:
        return text
    return re.sub(r"[ \t]{2,}", " ", text)


def _add_runs_with_inline_format(paragraph, text: str):
    """按公式 + 行内标记混合流写入段落。

    ✅ 乱码修复（软件内建方案）：
    1. ``clean_text`` 先清理控制字符 / U+FFFD / GBK 乱码 / 编码 mojibake /
       中文文档中西里尔误植；
    2. ``$...$`` / ``$$...$$`` / ``\\(...\\)`` / ``\\[...\\]`` 公式转为
       Word 原生公式（OMML），不再以 LaTeX 源码形式残留在导出文档中。
    """
    _accumulate_fix_stats(text)
    text = docx_math.clean_text(text)
    pos = 0
    for disp, latex, m in docx_math.iter_formulas(text):
        if m.start() > pos:
            _add_markdown_runs(paragraph, _collapse_redundant_spaces(text[pos:m.start()]))
        whole = text.strip() == m.group(0).strip()
        _add_omml_formula(paragraph, latex, disp, whole)
        pos = m.end()
    if pos < len(text):
        _add_markdown_runs(paragraph, _collapse_redundant_spaces(text[pos:]))


# 本次导出会话的修复统计（导出完成时输出日志并清零）。
# ✅ BUG 修复（并发数据竞争）：DOCX 构建经 `asyncio.to_thread` 在**多个工作线程**中
#    并发执行，而旧实现用模块级全局 dict 累加统计，两个导出同时进行时会出现：
#      · 后完成的导出把先完成者累加的计数一并读出并清零（统计张冠李戴）；
#      · `dict[k] += n` 本身非原子，并发自增还会静默丢计数。
#    DOCX 构建全程都在**单一线程内**完成，改用线程局部存储（threading.local）
#    即可天然隔离各次导出，无需加锁。
_FIX_STATS_KEYS = ("formulas", "block_formulas", "replacement_chars",
                   "control_chars", "gbk_mojibake", "latin1_mojibake",
                   "cyrillic",
                   # ✅ 新增（2026-10-06 · P0 逐块 fail-soft 可观测性）：
                   #   write_section 内单个内容块渲染抛异常时降级为纯文本/跳过，
                   #   绝不让一份文档因一个坏块整体 500。降级次数必须可观测
                   #   （日志 + X-Fix-Stats 响应头），否则「成稿少了一块」变成
                   #   与「静默丢数据」同级的不可见缺陷。
                   #   加法式扩展：既有 7 个键的语义与取值一字未动。
                   "block_render_failed",
                    # ✅ 新增（2026-10-06 · P1 表格尺寸上限）：
                    #   GFM 表格行列/单元格数超限时截断的次数（正常表格恒为 0）。
                    "table_truncated",
                    # ✅ R99 新增（2026-10-17 · 围栏自动修复可观测性）：
                    #   导出前置「自动补齐未闭合围栏」修好的围栏数。开启该开关后
                    #   DLV-07 的 error 级提示会被补齐动作抹掉，而围栏之后的正文
                    #   可能被并入图表源码（实测开启后丢得比关闭时更多），用户若
                    #   无任何提示就等于「静默丢正文」。计数随 X-Fix-Stats 下发，
                    #   与 audit_content 的 fence_auto_fixed 提示互为佐证。
                    #   加法式扩展：既有 9 个键的语义与取值一字未动（未开启该
                    #   开关时恒为 0）。
                    "fences_fixed",
                    # ✅ 2026-10-17（文档质量报告 符号/文字/公式问题）：
                    #   HTML 实体残留（`&gt;`/`&lt;`）/ 中英混排误植词 / 带边界空白
                    #   LaTeX 残留的自动修复计数。加法式扩展：既有键的语义与取值
                    #   一字未动，新键仅在源文含对应残留时非零。
                    "html_entities", "en_residue", "latex_residue",
                    # ✅ 2026-10-17（导出文档发现的问题）：表格「两块侧叠」错位行
                    #   被拆分还原的次数（普通表恒为 0）。加法式扩展：既有键语义
                    #   与取值一字未动。
                    "table_unstacked",
                    # ✅ R117 FIX-10（2026-10-20）：同章内**逐字重复**的正文块被折叠
                    #   的次数（正常章节恒为 0；AI 把同一节内容在 ```markdown 围栏
                    #   内外各写一遍时非零）。加法式扩展：既有键语义与取值一字未动。
                    "repeated_blocks",
                    # ✅ R117 FIX-11（2026-10-20）：同章内「前缀相同、后文分歧」的
                    #   **近似重复**正文块数。这类块**刻意不改写成稿**（两侧各有独有
                    #   尾句，删任一侧都是真丢内容），只做归因报警 ⇒ 计数非零不代表
                    #   产物字节变化。加法式扩展：既有键语义与取值一字未动。
                    "near_duplicate_blocks",
                    # ✅ R128（2026-10-22 · 遗留 3 收口）：正文「图 X-Y / 表 X-Y」
                    #   引用被升级为 REF 域（可点击、更新域后随编号刷新）的处数。
                    #   只对真实存在的题注书签包域；未登记编号保持纯文本。
                    "cross_ref_fields",
                    # ✅ R129（2026-10-22 · 引入易标式导出机制收口）：
                    #   导出后校验与 Word 2010+ 扩展清除的可观测性。加法式扩展：
                    #   既有键的语义与取值一字未动，新键仅在对应清理/残留发生时非零。
                    "ns_parts_sanitized",        # 清理了 2010+ 命名空间声明的部件数
                    "postcheck_markdown_residue", # 产物中 Markdown 语法残留段落数
                    "postcheck_html_residue",     # 产物中 HTML 标签残留段落数
                    "postcheck_entity_residue",   # 产物中 HTML 实体残留段落数
                    "postcheck_placeholder_residue", # 产物中占位符残留段落数
                    "formula_image_fallback")     # 公式 OMML 失败降级为图片的处数
_FIX_STATS_LOCAL = threading.local()


def _fix_stats() -> dict:
    """返回当前线程的修复统计累加器（线程内单例）。"""
    d = getattr(_FIX_STATS_LOCAL, "stats", None)
    if d is None:
        d = {k: 0 for k in _FIX_STATS_KEYS}
        _FIX_STATS_LOCAL.stats = d
    return d


def _accumulate_fix_stats(text: str):
    """累加本次导出的公式/乱码修复统计（供导出完成日志）。"""
    try:
        s = docx_math.scan_issues(text)
        st = _fix_stats()
        for k in st:
            st[k] += s.get(k, 0)
    except Exception:  # pragma: no cover
        pass


def _log_fix_stats() -> dict:
    """输出导出修复统计日志并清零，返回统计 dict（供响应头带给前端）。"""
    st = _fix_stats()
    if not any(st.values()):
        return {}
    parts = []
    if st["formulas"]:
        parts.append(f"公式转数学排版 {st['formulas']} 处"
                     f"（块级 {st['block_formulas']}）")
    if st["replacement_chars"]:
        parts.append(f"替换字符 {st['replacement_chars']} 处")
    if st["control_chars"]:
        parts.append(f"控制字符 {st['control_chars']} 处")
    if st["gbk_mojibake"]:
        parts.append(f"GBK 乱码 {st['gbk_mojibake']} 处")
    if st["latin1_mojibake"]:
        parts.append(f"编码乱码 {st['latin1_mojibake']} 处")
    if st["cyrillic"]:
        parts.append(f"西里尔误植 {st['cyrillic']} 处")
    if st["block_render_failed"]:
        parts.append(f"内容块降级为纯文本 {st['block_render_failed']} 处"
                     "（成稿已保留文字，版式降级）")
    if st["table_truncated"]:
        parts.append(f"超宽/超长表格截断 {st['table_truncated']} 处")
    if st["fences_fixed"]:
        parts.append(f"未闭合围栏自动补齐 {st['fences_fixed']} 处"
                     "（围栏之后的正文可能被并入图表源码，请核对成稿）")
    if st["html_entities"]:
        parts.append(f"HTML 实体还原 {st['html_entities']} 处（&gt;/&lt; → >/<）")
    if st["en_residue"]:
        parts.append(f"中英混排误植还原 {st['en_residue']} 处（英文词 → 中文）")
    if st["latex_residue"]:
        parts.append(f"公式边界空白归一 {st['latex_residue']} 处（$ x $ → $x$）")
    if st["repeated_blocks"]:
        parts.append(f"同章逐字重复正文块折叠 {st['repeated_blocks']} 处"
                     "（源文同一内容写了两遍，成稿仅保留首次）")
    if st["near_duplicate_blocks"]:
        parts.append(f"同章近似重复正文块 {st['near_duplicate_blocks']} 处"
                     "（前段相同、后文各自扩写，成稿**未改动**，请回查源文）")
    if st["cross_ref_fields"]:
        parts.append(f"正文交叉引用升级为可点击 REF 域 {st['cross_ref_fields']} 处"
                     "（更新域后随编号刷新）")
    # ✅ R129：导出后校验 + Word 2010+ 扩展清除（新键仅在发生时展示）
    if st["ns_parts_sanitized"]:
        parts.append(f"清除 Word 2010+ 专有命名空间声明 {st['ns_parts_sanitized']} 个部件"
                     "（OOXML Transitional 纯净导出）")
    if st["postcheck_markdown_residue"]:
        parts.append(f"产物检出 Markdown 语法残留 {st['postcheck_markdown_residue']} 处"
                     "（渲染链路漏洞，请回查源文）")
    if st["postcheck_html_residue"]:
        parts.append(f"产物检出 HTML 标签残留 {st['postcheck_html_residue']} 处")
    if st["postcheck_entity_residue"]:
        parts.append(f"产物检出 HTML 实体残留 {st['postcheck_entity_residue']} 处")
    if st["postcheck_placeholder_residue"]:
        parts.append(f"产物检出占位符残留 {st['postcheck_placeholder_residue']} 处")
    if st["formula_image_fallback"]:
        parts.append(f"公式降级为图片 {st['formula_image_fallback']} 处"
                     "（OMML 转换失败的三级降级）")
    stats = dict(st)
    if st["block_render_failed"] or st["table_truncated"]:
        logger.warning(
            "DOCX 导出存在降级：%d 处内容块渲染失败（已降级为纯文本）、"
            "%d 处表格超尺寸（已截断）—— 成稿可交付但版式不完整，"
            "请回查源文对应章节", st["block_render_failed"], st["table_truncated"])
    if st["fences_fixed"]:
        logger.warning(
            "DOCX 导出前置自动补齐了 %d 处未闭合围栏：围栏之后的正文会被并入"
            "图表源码并整体跳过（CommonMark 口径），成稿请人工核对缺失段落",
            st["fences_fixed"])
    if st["repeated_blocks"]:
        logger.warning(
            "DOCX 导出折叠了 %d 处**同章内逐字重复**的正文块 —— 源文把同一节内容"
            "写了两遍（实测形态：一整节写进 ```markdown 围栏，围栏闭合后又在围栏外"
            "重述一遍，其中逐字相同的部分已折叠、后文扩写的部分见下一条）。"
            "成稿仅保留逐字重复的首次出现，明细已按章节点名；"
            "请回查源文或重新生成该章", st["repeated_blocks"])
    if st["near_duplicate_blocks"]:
        logger.warning(
            "DOCX 导出检出 %d 处**同章内近似重复**的正文块（前 60 字归一后相同、"
            "后文分歧）—— 这类块**成稿一字未改**：两侧各有独有的尾句，删除任一侧"
            "都是实质性丢内容，导出器无权替用户取舍。明细已按章节点名，"
            "请回查源文该章或重新生成", st["near_duplicate_blocks"])
    # ✅ R129：导出后校验检出残留 ⇒ 与内容块降级同级警告（成稿可交付但有脏点）
    if (st["postcheck_markdown_residue"] or st["postcheck_html_residue"]
            or st["postcheck_entity_residue"] or st["postcheck_placeholder_residue"]):
        logger.warning(
            "DOCX 导出后校验检出残留：Markdown %d / HTML %d / 实体 %d / 占位符 %d —— "
            "明细见本次导出日志，请回查对应章节源文",
            st["postcheck_markdown_residue"], st["postcheck_html_residue"],
            st["postcheck_entity_residue"], st["postcheck_placeholder_residue"])
    logger.info("DOCX 导出自动修复: %s", "、".join(parts))
    st.update({k: 0 for k in st})
    return stats


#: R117 FIX-10 · 可参与「逐字重复」判定的块类型 —— 只有**正文性**块才可能
#: 出现整段复述；表格 / 图表 / 代码 / 图片是结构块，重复出现即视为刻意安排。
_REPEATABLE_BLOCK_TYPES = ("paragraph", "list_item")
#: 普通正文折叠阈值（归一后字符数）。低于此值时「同一短语出现两次」在正常
#: 行文里很常见（并列小标题、固定术语），折叠会误删内容。
_REPEAT_MIN_CHARS = 40
#: ``**小标题**`` 形的降档阈值。这类块**整串**就是一对 ``**`` 包裹，
#: 同一章内逐字出现两次几乎必为复述。
_REPEAT_BOLD_MIN_CHARS = 8
#: 纯加粗小标题形（内部不再含 ``**``，用以排除「``**名称**：说明…``」行内加粗）。
_RE_BOLD_ONLY = re.compile(r"^\*\*([^*\n]{4,})\*\*$")
#: 归一化：剥掉全部空白（换行、缩进、全角空格）后比对，不做任何语义改写。
_WS_ONLY_RE = re.compile(r"\s+")
#: R117 FIX-11 · 近似重复判定的「公共前缀」门槛（归一后字符数）。取 60 的取证
#: 依据：生产实测五对近重复的首个分歧位置分别在 66 / 74 / 91 / 163 / 185 字，
#: 全部 ≥66；而正常行文里两段不同内容恰好逐字撞满前 60 字的概率极低。
#: 门槛刻意取高而非取低：公共前缀越短，越容易把「同一标准号 / 同一句式开头的
#: 两句不同论述」误报成重复，报警一旦失真用户就会忽略它。
_NEAR_DUP_PREFIX_CHARS = 60


def _collapse_repeated_blocks(blocks: list[dict], *, where: str = "") -> list[dict]:
    """折叠同一章节内**逐字重复**的正文块（R117 FIX-10 · 2026-10-20）。

    取证（``artifacts/r117/_probe13.txt`` / ``_probe14.txt`` / **全字符级复核**
    ``_probe16.txt``）：生产库 ``sections.content`` 的「通用工程标准」一章把同一批
    内容写了**两遍** —— 第 1~25 行在 ` ```markdown ` 围栏内，第 27~47 行在围栏闭合
    **之后**重述一遍。逐字程度分两类（务必分清，早期探针按前 40/60 字截断判重
    曾把两类混谈）：
      * **逐字相同 5 组**：五个 ``**标准名 + 现场检查标准**`` 加粗小标题
        （归一后 20~24 字，如 ``**JGJ59-2011现场检查标准**``）⇒ 本函数折叠，
        blocks 24 → 19；
      * **仅前缀相同 5 组**：五段正文（137~301 字）共享前 **66~185** 字后各自
        往下写 ⇒ **不折叠**（见 :func:`_report_near_duplicate_blocks`）。
    导出器忠实渲染两份 ⇒ 同一批小标题与同一段落前半在 8 万字方案里重复出现，
    用户必须手工删掉一半，而此前**没有任何检出面拦住它**。

    判据（保守，四条同时满足才折叠，普通章节零影响）：
      ① 块类型 ∈ :data:`_REPEATABLE_BLOCK_TYPES`（段落 / 列表项）；
      ② **归一（剥掉全部空白）后完全相同** —— 只处理"逐字复述"，不碰"近似改写"；
      ③ 长度达标：普通正文 ≥ :data:`_REPEAT_MIN_CHARS`，
         `**小标题**` 形 ≥ :data:`_REPEAT_BOLD_MIN_CHARS`；
      ④ 保留**首次**出现，只丢弃后续副本（被保留块自身一字未改）。

    阈值为何分两档：40 字以下短句重复在正常行文里常见，必须整段比对才安全；
    但本例的 `**JGJ 59-2011 现场检查标准**` 只有 18 字，若一刀切 40 字就会
    留下"正文已折叠、加粗小标题仍出现两次"的半截重复 —— 故只对
    **整串被 `**` 包裹**的块降档，行内加粗（`**名称**：说明…`）不受影响。

    全 fail-soft：任何异常原样返回 blocks（绝不因去重而丢整章内容）。

    ⚠️ **判重作用域刻意限定为单章**（``seen`` 随每次调用重置）—— 不同章节
    引用同一条标准条文、同一段安全要求是专项方案的正常写法，跨章去重会
    把第五章「安全检查标准」引用第二章原文的那一段**误删**，属实质性
    丢内容，远比留一份重复更糟。
    """
    try:
        seen: set[str] = set()
        out: list[dict] = []
        dropped: list[str] = []
        for b in blocks:
            if (not isinstance(b, dict)
                    or b.get("type") not in _REPEATABLE_BLOCK_TYPES):
                out.append(b)
                continue
            raw = str(b.get("text") or "")
            if not raw.strip():
                out.append(b)
                continue
            key = _WS_ONLY_RE.sub("", raw)
            mb = _RE_BOLD_ONLY.match(key)
            if mb:
                key, floor = f"bold:{mb.group(1)}", _REPEAT_BOLD_MIN_CHARS
            else:
                floor = _REPEAT_MIN_CHARS
            if len(key) >= floor and key in seen:
                dropped.append(raw)
                continue
            seen.add(key)
            out.append(b)
        if dropped:
            st = _fix_stats()
            st["repeated_blocks"] = st.get("repeated_blocks", 0) + len(dropped)
            logger.warning(
                "正文重复（%s）：同章检出 %d 个逐字重复正文块，成稿仅保留首次 —— "
                "样例：%s", where or "未标注章节", len(dropped),
                " / ".join(x.strip()[:24] for x in dropped[:3]))
        return out
    except Exception as exc:  # noqa: BLE001  # fail-soft：宁可不折叠
        logger.warning("正文重复折叠跳过（%s）：%s —— 原样保留全部块", where, exc)
        return blocks


def _report_near_duplicate_blocks(blocks: list[dict], *, where: str = "") -> int:
    """检出同章内「前缀相同、后文分歧」的**近似重复**正文块，只报警、不改写。

    R117 FIX-11（2026-10-20）。与 :func:`_collapse_repeated_blocks` 是**一对**：
    那边折叠逐字相同的副本（零内容损失），这边处理占比更大的另一种形态 ——
    AI 把同一节内容复述两遍时，段落**前 66~185 字逐字相同、之后各写各的**
    （取证 ``artifacts/r117/_probe16.txt``：「通用工程标准」一章 5 对，两侧长度
    分别 137~301 / 176~230 / 187~249 / 195~301 / 200~300 字，首个分歧位置
    66 / 74 / 91 / 163 / 185 字）。

    ⚠️ **刻意不折叠**（这是本轮最重要的边界，不是偷懒）：
      近重复的两侧**各自持有独有尾句**，删除任一侧都是**实质性丢正文** ——
      「短的那段」不是「长的那段」的前缀子串（首个分歧出现在中段），
      所以连「保留信息量更大的一侧」这种保守做法都做不到。导出器没有资格
      替用户判断哪一段的后续论述更重要，也不该用「合并 / 截断」自造第三版文字
      （那是改写正文，属生成域，不属导出域）。
      ⇒ 正确动作只有两个：**让缺陷可见**（本函数）+ **回到源头重新生成该章**。

    判据（只统计、不动 blocks）：块类型 ∈ :data:`_REPEATABLE_BLOCK_TYPES`，
    非纯加粗小标题形，归一后长度 ≥ :data:`_NEAR_DUP_PREFIX_CHARS`，且与本章
    已见过的另一块**共享前 :data:`_NEAR_DUP_PREFIX_CHARS` 字而全文不同**。
    逐字相同的块不在此计（已由折叠处理，避免双计）。

    返回检出的近重复**副本**数（正常章节恒为 0）；fail-soft：异常返回 0，
    绝不影响导出主链路。
    """
    try:
        buckets: dict[str, list[str]] = {}
        pairs: list[tuple[str, str]] = []
        for b in blocks:
            if (not isinstance(b, dict)
                    or b.get("type") not in _REPEATABLE_BLOCK_TYPES):
                continue
            key = _WS_ONLY_RE.sub("", str(b.get("text") or ""))
            if len(key) < _NEAR_DUP_PREFIX_CHARS or _RE_BOLD_ONLY.match(key):
                continue
            head = key[:_NEAR_DUP_PREFIX_CHARS]
            prev = buckets.setdefault(head, [])
            for p in prev:
                if p != key:  # 全文相同 ⇒ 属逐字重复，交给折叠那边计
                    pairs.append((p, key))
                    break
            prev.append(key)
        if pairs:
            st = _fix_stats()
            st["near_duplicate_blocks"] = (
                st.get("near_duplicate_blocks", 0) + len(pairs))
            logger.warning(
                "正文近似重复（%s）：同章检出 %d 处前 %d 字相同、后文分歧的正文块 —— "
                "成稿**未作改动**（两侧各有独有内容，删任一侧即丢正文），样例前 30 字：%s",
                where or "未标注章节", len(pairs), _NEAR_DUP_PREFIX_CHARS,
                " / ".join(x[1][:30] for x in pairs[:3]))
        return len(pairs)
    except Exception as exc:  # noqa: BLE001  # fail-soft：检出不影响导出
        logger.warning("正文近似重复检测跳过（%s）：%s", where, exc)
        return 0


#: 附录事实单元格行首的 Markdown 列表符号（``- `` / ``* `` / ``+ ``）。
_APPENDIX_LIST_LEAD_RE = re.compile(r"^\s*[-*+]\s+")


def _appendix_cell_paragraph(cell, text: str, font_name: str,
                             font_size: float = 10.5, *, first: bool = False):
    """向附录事实表单元格写入一行内容：剥离开头列表符号并解析行内 Markdown。

    旧实现直接 ``add_run`` 写入全局事实原文，``- **名称**: 内容`` 等列表 +
    加粗语法会以源码形式残留在成稿单元格内（实测附录 14 张表全部中招）。
    本函数：① 剥离行首 ``- ``/``* `` 列表符号；② 复用
    ``_add_runs_with_inline_format`` 解析 ``**加粗**`` / 公式 / 链接等行内标记；
    ③ 统一中文字体。仅改变附录事实单元格的渲染，正文与其他表格不受影响。
    """
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt

    lines = [ln for ln in (text or "").split("\n")]
    p = cell.paragraphs[0] if first else cell.add_paragraph()
    started = False
    for raw in lines:
        cleaned = _APPENDIX_LIST_LEAD_RE.sub("", raw.strip())
        target = p if not started else cell.add_paragraph()
        target.alignment = WD_ALIGN_PARAGRAPH.LEFT
        target.paragraph_format.first_line_indent = Pt(0)
        _add_runs_with_inline_format(target, cleaned)
        for run in target.runs:
            _set_run_font(run, font_name, font_size)
        p = target
        started = True
    return p


def _add_table_caption(doc, text: str, font_name: str = "宋体", font_size: float = 10.5):
    """插入表题「表 {章号}-{序号} 表名」（GB/T：表题位于表格**上方**，居中加粗）。

    ✅ 新增：与图题（位于图下方）配套，使表格也有规范编号，满足交付评审要求。
    ``keep_with_next`` 保证表题不与其表格被分页拆散。
    """
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Cm, Pt

    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    pf = p.paragraph_format
    pf.first_line_indent = Cm(0)
    pf.space_before = Pt(6)
    pf.space_after = Pt(2)
    pf.keep_with_next = True
    # ✅ R128（遗留 3 收口）：题注编号前缀加书签（正文「表 X-Y」引用可升级
    #    为 REF 域）；视觉与旧实现逐字节一致（书签为不可见元素）。
    _add_caption_text_with_ref_bookmark(p, text, font_name, font_size)
    return p


#: 表题开头「表 X-Y」/「表：」等编号前缀（剥离后再由导出统一编号，避免叠写）。
_LEADING_TABLE_NUM_RE = re.compile(
    r"^表\s*(?:[:：]\s*|[0-9一二三四五六七八九十][0-9.\-–—]*[\s、.：:]+)")


def _strip_leading_table_number(text: str) -> str:
    """剥离表题中 AI 自带的「表 X-Y」编号前缀。

    ✅ 2026-10-17（文档质量报告 表号问题根因 B）：实测交付文档出现
    「表 6-3 表 6-1 木工班组劳动力配置计划」——AI 把表题连同编号写进表格
    **首行首格**（首格 = 「表 6-1 木工班组劳动力配置计划」），导出的自动表题
    取首行前 3 列拼接后再前置统一编号，于是「表 6-3」与「表 6-1」叠写。
    表题编号由导出**唯一**生成，源文自带的编号一律剥离，杜绝双表号。
    """
    t = str(text or "").strip()
    if not t.startswith("表"):
        return t
    stripped = _LEADING_TABLE_NUM_RE.sub("", t).strip()
    return stripped or t


def _strip_leading_figure_number(text: str) -> str:
    """剥离图题 / 配图 alt 中生成期自带的「图 X-Y」编号前缀。

    ✅ R131-A（2026-10-23 · 目录/正文编号一致性 P0 · 图号归属漂移根因修复）：
    图号在渲染期由 ``_figure_chapter_num`` 按**当前章节树**现算并前置
    （见 ``write_section`` 图表 / 配图两分支），而生成期写进图表载荷 title、
    Mermaid title 指令、``![图 4-1 …](url)`` alt 的「图 4-1 …」被**原样保留**
    ⇒ 成稿出现「图 2-1 图 4-1 劳动力配置计划」：前半是当前树新号，后半是
    **过期章号** —— 章节移动后即「图号归属漂移 + 重号」（验收标准所指形态）。
    表侧同型问题已于 2026-10-17 用 :func:`_strip_leading_table_number` 收口
    （表号叠写根因 B），图侧一直缺失，此处对齐同口径。

    判据复用唯一事实源 ``content_format_contract.FIG_TITLE_PREFIX_RE``（由
    ``FIG_TITLE_RE`` 派生，本文件不持有第二份字面量）—— 契约判定为规范图题
    的才剥，判据与形态检查天然同源。fail-soft：异常或剥离后为空一律返回原文，
    绝不阻断渲染。

    ✅ R164（2026-11-01 · 图题残留前导标点）：``FIG_TITLE_PREFIX_RE`` 只吃到
    「图 3-1」为止，源文常见的冒号分隔形态 ``图 3-1：施工工艺流程`` 剥完前缀
    还剩一个悬空冒号 ⇒ 成稿图题写成「图 2-1 ：施工工艺流程」（实测复现）。
    此处**只**剥前缀后紧跟的标点/空白，不引入第二份图号正则：判据仍是契约
    的那一份，标点集合由本函数就地声明（不涉及「图号形态」口径）。

    ✅ R165（2026-11-01 · 非 ``-`` 分隔形态残留）：派生自规范式的
    ``FIG_TITLE_PREFIX_RE`` 只认 ``图 {数字}-{数字}``，源文/载荷写成
    ``图 3.1`` / ``图 A-1`` / ``图 3-1-2`` / ``图 3`` 时**整段不剥**，
    成稿叠成「图 2-1 图 3.1 施工工艺流程」。改用契约模块的**宽口径**剥离判据
    ``FIG_TITLE_ANY_PREFIX_RE``（仍由契约模块唯一定义，本文件仍无字面量），
    多段编号一次剥全（``图 3-1-2`` 不再留 ``-2``）。
    """
    try:
        t = str(text or "").strip()
        if not t:
            return t
        m = FIG_TITLE_ANY_PREFIX_RE.match(t) or FIG_TITLE_PREFIX_RE.match(t)
        if not m:
            return t
        rest = t[m.end():].lstrip("：:，,、；;。.·-–— \t　")
        return rest or t
    except Exception:  # noqa: BLE001 — 剥离失败必须保留原文，绝不阻断导出
        return str(text or "").strip()


#: 自动表题里必须剔除的「无信息表头单元格」—— 把它们拼进表题只会得到
#: 「表 2-5 序号、项目、参数指标」这类读不出内容的标题（R117 实测）。
_AUTO_CAPTION_JUNK_CELLS = frozenset({
    "序号", "编号", "代码", "备注", "说明", "单位", "数量", "No", "NO", "no", "№",
})
#: 表头单元格里的行内/独占公式（``高度 $H$ (m)`` / ``$[\Delta]$``）。
_RE_CAPTION_MATH = re.compile(r"\${1,2}[^$]*\${1,2}")


def _sanitize_auto_caption(cells: list[str]) -> str:
    """把 GFM 表头单元格拼成**可读**的自动表题（R117 · 2026-10-20）。

    两处实测缺陷（室外汽车吊吊装施工专项方案 第 1 轮导出）：

    1. ``表 4-5 构件类型、高度 $H$ (m)、垂直度允许偏差 $[\\Delta]$ (mm)`` ——
       表题以“单 run”写入（:func:`_add_table_caption`），**不走**行内公式渲染，
       于是 ``$H$`` / ``$[\\Delta]$`` 的 LaTeX 原文被印进成稿题注。
    2. ``表 1-8 序号、项目、参数指标`` / ``表 3-5 岗位名称、每人负责数量、备注``
       —— 表头里的「序号/备注」等**无信息列名**被拼进标题，读者无法判断表的内容。

    故自动表题在拼接前：剥公式、剥首尾分隔符、丢弃无信息列名、去重、压缩空白。
    只作用于**自动生成**的表题，源文自带表题（``captions`` 分支）逐字保留。
    """
    out: list[str] = []
    for raw in cells:
        c = _RE_CAPTION_MATH.sub("", str(raw or ""))
        c = c.replace("$", "").strip(" 　")
        c = c.strip("、，,；;")
        c = re.sub(r"[ \t　]{2,}", " ", c).strip()
        if not c or c in _AUTO_CAPTION_JUNK_CELLS:
            continue
        if c not in out:
            out.append(c)
    return "、".join(out)


#: GFM 表格尺寸上限（2026-10-06 · P1 性能/稳定性）。
#: 实测无上限时单表耗时随行列乘积急剧上升：60 列×20 行 = 2.4s、
#: 200 列×40 行 = **39.5s**、400 列×60 行 > 110s 未完成（建单元格 XML 是
#: O(rows×cols) 次 OXML 节点构造）。而 `_build_docx_sync` 全程在
#: `asyncio.to_thread` 的**单个工作线程**里跑、无内部超时 → 一张畸形宽表
#: 就能把导出线程长期占死，用户侧表现为"导出卡住不动"。
#: 正常工程表格（列 ≲ 20、行 ≲ 100）远低于下列阈值，产物逐字节不变。
_TABLE_MAX_COLS = 63     # Word 表格硬上限即 63 列
_TABLE_MAX_ROWS = 400
_TABLE_MAX_CELLS = 12000


def _unstack_or_trim_table_rows(rows: list[list[str]]) -> list[list[str]]:
    """把「两块侧叠」的错位表格拆回规范行，并去掉尾部整列全空的多余列。

    ✅ BUG 修复（2026-10-17 · 导出文档发现的问题）：AI 偶发把**两条记录塞进一行**
    的「两块并列」形态（实测第 6 章「劳动力配置计划」表：表头只有 5 列有名称、
    其后 6 列全空，而首条数据行在空白分隔列之后又填了第二条记录）。旧实现
    ``ncols = max(len(r))`` → 整表被撑成 **11 列**、两条记录挤在同一行，成稿既宽
    又错位、无法阅读。

    判据（保守，只在形态高度可信时才改写；普通表逐字不动）：
      1. 先去掉**尾部整列全空**的多余列（所有行该列都为空 ⇒ 必为冗余）；
      2. 再找**整列全空**的「分隔列」，把表切成 ≥2 个列块；
      3. 仅当表头**只填充第一个列块**（其后各列块表头全空）时，判为侧叠；
    命中则每个数据行按「非空列块」拆成多行（列块内取值左对齐），让侧叠的
    第二条记录回到独立行；不命中一律原样返回（零误伤）。

    设计取舍：只**重排**不**丢弃**（AGENTS.md §3.1.6「宁可不改，不丢内容」）——
    侧叠的第二条记录被提升为独立行而非截掉。
    """
    if not rows:
        return rows
    ncols = max(len(r) for r in rows)
    if ncols < 2:
        return rows
    grid = [list(r) + [""] * (ncols - len(r)) for r in rows]
    empty_col = [all(not g[c].strip() for g in grid) for c in range(ncols)]

    # 1) 尾部整列全空 → 去掉（安全：所有行该列都为空）
    while ncols > 1 and empty_col[ncols - 1]:
        ncols -= 1
    grid = [g[:ncols] for g in grid]
    empty_col = empty_col[:ncols]

    # 2) 列块 = 被「整列全空」分隔的连续非空列区间
    blocks: list[tuple[int, int]] = []
    start = None
    for c in range(ncols):
        if empty_col[c]:
            if start is not None:
                blocks.append((start, c))
                start = None
        elif start is None:
            start = c
    if start is not None:
        blocks.append((start, ncols))
    if len(blocks) < 2:
        return grid
    # 3) 表头只填第一块（其后各块表头全空）才认定侧叠
    header = grid[0]
    if any(any(header[a:b]) for a, b in blocks[1:]):
        return grid

    out: list[list[str]] = [header[blocks[0][0]:blocks[0][1]]]
    width = len(out[0])
    for g in grid[1:]:
        for a, b in blocks:
            seg = g[a:b]
            if any(x.strip() for x in seg):
                out.append(seg)
                width = max(width, len(seg))
    logger.warning(
        "导出：表格侧叠行已拆分还原（原 %d 行 × %d 列 → %d 行 × %d 列）"
        "——源文存在「两条记录挤在同一行」的错位，成稿已按记录分行。",
        len(rows), ncols, len(out), width)
    _fix_stats()["table_unstacked"] = _fix_stats().get("table_unstacked", 0) + 1
    return [r + [""] * (width - len(r)) for r in out]


def _merge_table_title_band(table, rows: list[list[str]]) -> bool:
    """首行是「通栏标题行」时横向合并为单格。

    ✅ R117 FIX-9（2026-10-20）：实测导出稿第 16 张表（表 2-4）首行是
    ``['吊装作业关键文件核查清单', '', '']`` —— 源表用一个「单格标题行」当表头，
    成稿就渲染成「左格有字、其余两格空着」的半条色带；更糟的是跨页重复
    （``w:tblHeader``）打的正是这一行，翻页后重复出来的仍是半条空色带，
    **列名永远不出现**。

    识别条件（保守，四条同时满足）：
      ① 表格 ≥ 2 列、≥ 2 行；
      ② 首行**只有第 1 格**有内容（其余格全空）；
      ③ 第 2 行至少 2 格有内容（证明它是真正的列名/数据行，而非另一条标题）；
      ④ 首行尚未合并过（``tc`` 数量仍等于列数）—— 保证幂等。

    合并后居中，并清掉合并带入的空段落（python-docx 的 ``merge`` 会把每格的
    段落都串进来，否则色带被撑成 3 行高）。返回 True 供调用方把「表头重复行」
    从第 0 行改到第 1 行。
    """
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    ncols = len(table.columns)
    if ncols < 2 or len(rows) < 2:
        return False
    header = [str(x or "").strip() for x in rows[0]]
    if not header or not header[0] or any(header[1:]):
        return False
    second = [str(x or "").strip() for x in rows[1]]
    if sum(1 for x in second if x) < 2:
        return False
    if len(table.rows[0]._tr.tc_lst) < ncols:
        return False  # 已合并过（横向合并后 tc 数 < 列数）

    merged = table.rows[0].cells[0].merge(table.rows[0].cells[-1])
    for p in list(merged.paragraphs)[1:]:
        if not p.text.strip():
            p._element.getparent().remove(p._element)
    for p in merged.paragraphs:
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    return True


def _add_table_from_markup(doc, tbl_lines: list[str], font_name: str = "宋体"):
    """将 GFM 表格行转为 Word 表格。

    表头：`font_name` 五号加粗、居中、暗板岩蓝浅色 60% 底纹（B6B1D1）；
    表体：`font_name` 五号居中；整表居中、单元格垂直居中、表头跨页重复。

    ✅ 2026-10-06（P1）：行列/单元格数设上限并**留痕**（`table_truncated`
    计入 X-Fix-Stats），超限时按「行 → 列」顺序截断而非让导出线程被占死。
    """
    from docx.enum.table import WD_ALIGN_VERTICAL, WD_TABLE_ALIGNMENT
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Cm, Pt

    def _split_row(raw: str) -> list[str]:
        # 先保护转义竖线 \| ，再按竖线切分，最后还原为普通 |
        safe = raw.strip().replace("\\|", "\x00").strip("|")
        return [c.replace("\x00", "|").strip() for c in safe.split("|")]

    # 跳过分隔行（|:---| 对齐分隔行）。旧实现硬编码 ``j != 1`` 假设分隔行在
    # 第 2 行；R145 起表头前短行（标题带/列组标签）前置后分隔行位置后移，
    # 按正则动态定位（兼容旧格式分隔行在 index 1 的逐字节不变行为）。
    sep_pos = [j for j, r in enumerate(tbl_lines) if _RE_TABLE_SEP.match(r.strip())]
    rows = [_split_row(r) for j, r in enumerate(tbl_lines) if j not in sep_pos]
    if not rows:
        return
    # ✅ 2026-10-17（导出文档发现的问题）：先还原「两块侧叠」的错位行 + 去掉尾部
    #    整列全空的多余列，再据此确定列数（否则 max(len) 会把整表撑成 11 列）。
    rows = _unstack_or_trim_table_rows(rows)
    ncols = max(len(r) for r in rows)
    # ---- 尺寸上限（先列后行，保证表头行永远保留）----
    raw_cols, raw_rows = ncols, len(rows)
    truncated = False
    if ncols > _TABLE_MAX_COLS:
        ncols = _TABLE_MAX_COLS
        truncated = True
    if len(rows) > _TABLE_MAX_ROWS:
        rows = rows[:_TABLE_MAX_ROWS]
        truncated = True
    if len(rows) * ncols > _TABLE_MAX_CELLS:
        keep = max(1, _TABLE_MAX_CELLS // max(1, ncols))
        if keep < len(rows):
            rows = rows[:keep]
            truncated = True
    if truncated:
        _fix_stats()["table_truncated"] = _fix_stats().get("table_truncated", 0) + 1
        logger.warning(
            "导出：表格超尺寸已截断（原始 %d 列 × %d 行 → %d 列 × %d 行，"
            "上限 %d 列 / %d 行 / %d 单元格）—— 成稿可交付但该表内容不完整，"
            "请回查源文", raw_cols, raw_rows, ncols, len(rows),
            _TABLE_MAX_COLS, _TABLE_MAX_ROWS, _TABLE_MAX_CELLS)
    table = doc.add_table(rows=len(rows), cols=ncols)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = True
    # ✅ BUG 修复：旧实现 `table.allow_autofit = True` —— python-docx 的 Table
    #    根本没有该属性（已核实），赋值只是往对象上挂了个无人读取的属性（死代码），
    #    容易被误读为"已开启自适应布局"。

    for ri, row_data in enumerate(rows):
        for ci in range(ncols):
            cell_text = row_data[ci] if ci < len(row_data) else ""
            cell = table.rows[ri].cells[ci]
            cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
            p = cell.paragraphs[0]
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            p.paragraph_format.first_line_indent = Pt(0)
            if cell_text:
                # 单元格内也解析 **加粗** 等行内标记（旧实现整格按纯文本输出）
                _add_runs_with_inline_format(p, cell_text)
            for run in p.runs:
                _set_run_font(run, font_name, 10.5,
                              bold=True if ri == 0 else run.bold)
            if ri == 0:
                # ✅ R109：按 CT_TcPr 序列插入 w:shd（旧实现 append 到末尾 →
                #    与已写入的 w:vAlign 顺序颠倒，WPS 拒绝打开）
                _set_cell_shading(cell, "B6B1D1")

    # ✅ R117 FIX-9（2026-10-20）：首行「通栏标题行」横向合并。
    #    ⚠️ R146（2026-10-29 · R117 回归修复）：本步**必须先于**下面的「表头前短行
    #    gridSpan」执行。通栏标题带（仅首格有内容）恰好也是「内容末列 < ncols-1」的
    #    短行，旧序被短行逻辑先并成 1 个 tc，随后 `_merge_table_title_band` 的
    #    幂等判据「tc 数仍等于列数」判为 False → `_band_merged=False`，连带
    #    ① `w:tblHeader` 落回标题带（翻页重复的仍是空色带、列名永不出现）；
    #    ② 合并格只拿到首列宽（固定布局下标题带被压成 1 列宽逐字换行）。
    _band_merged = _merge_table_title_band(table, rows)

    # ✅ R145（2026-10-29 · 导出后校验检出 Markdown 残留）：表头前短行
    #    （列数 < ncols）按 gridSpan 合并尾部空单元格（生产实拍
    #    ``| 项目 | 技术参数 |`` 在 3 列表头前 → 渲染成「项目 | 技术参数（跨 2 列）」），
    #    避免短行右侧出现孤立空单元格；与上方 `_merge_table_title_band` 互补：
    #    后者只管「仅首格有内容」的通栏标题带（已合并时本逻辑跳过），
    #    本逻辑管「部分列有内容」的列组标签行。
    #    ⚠️ 刻意只作用于**表头行**（ri==0）：`_unstack_or_trim_table_rows` 已把短行
    #    补齐到 ncols，按「内容末列 < ncols-1」检测短行；数据行的尾部空格是真实空列
    #    （如「备注」列某行为空），合并会丢列语义，故不合并。
    if not _band_merged:
        _row0_data = rows[0] if rows else []
        _last_content = max(
            (c for c in range(len(_row0_data)) if str(_row0_data[c] or "").strip()),
            default=-1)
        if 0 <= _last_content < ncols - 1:
            _merged = table.rows[0].cells[_last_content].merge(
                table.rows[0].cells[ncols - 1])
            for _p in _merged.paragraphs:
                _p.alignment = WD_ALIGN_PARAGRAPH.CENTER

    # ✅ R117 FIX-8（2026-10-20）：每一行都禁止跨页断开（含表头行）。
    #    实测导出稿 195 行无一带 cantSplit —— 长表翻页会把同一行劈成两半。
    for _row in table.rows:
        _set_row_cant_split(_row)

    # ✅ 增强：表头行跨页重复（长表格翻页后仍能看到列名）
    # ✅ R117 FIX-9：首行已被合并成通栏标题带时，重复的是**列名行**（第 2 行）——
    #    重复一条标题带既无信息量，又会把真正的列名继续压掉。
    _hdr_idx = 1 if _band_merged else 0
    if len(rows) > 1:
        tr_pr = table.rows[_hdr_idx]._tr.get_or_add_trPr()
        if tr_pr.find(qn("w:tblHeader")) is None:
            tbl_header = OxmlElement("w:tblHeader")
            tbl_header.set(qn("w:val"), "true")
            tr_pr.append(tbl_header)
    # ✅ 增强：列宽按各列最长内容自适应分配（总宽约 16cm），避免长文本被挤成多行
    _widths_cm = _assign_table_column_widths(table, rows, ncols)
    if _band_merged:
        # 合并单元格横跨**全部列**：固定布局（tblLayout=fixed）下必须显式给全表宽，
        # 否则沿用首列宽 —— 标题带被压成 1.2cm 宽、文字逐字换行。
        table.rows[0].cells[0].width = Cm(sum(_widths_cm))


def _assign_table_column_widths(table, rows: list[list[str]], ncols: int) -> list[float]:
    """按各列最长内容（中文字符权重1、半角0.5）比例分配列宽，总宽约 16cm。

    同时声明固定布局（tblLayout=fixed）+ 表格总宽（tblW），否则 Word 会按内容
    重新折列宽，自适应列宽不生效。

    ✅ R117 FIX-9：返回各列宽度（cm）——首行通栏合并后需要按列宽**求和**给
    合并单元格显式宽度，否则固定布局下它只拿到首列宽。
    """
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Cm

    def _w(text: str) -> float:
        s = (text or "").replace("\n", " ")
        return sum(1.0 if ord(ch) > 0x2E80 else 0.5 for ch in s)

    weights = [0.0] * ncols
    for r in rows:
        for ci in range(ncols):
            weights[ci] = max(weights[ci], _w(r[ci] if ci < len(r) else ""))
    total = sum(weights) or 1.0
    # ✅ R127（2026-10-22 · 表格溢出修复）：旧实现硬编码 16cm 总宽。当用户选择
    #    Word 标准页边距（左右 3.17cm）时版心仅 14.65cm，实测正文表宽 16cm
    #    超出页面 1.35cm（右侧被裁切/打印越界）。现按当前节实际版心收敛。
    total_cm = 16.0
    try:
        sec = table.part.document.sections[-1]
        total_cm = (sec.page_width - sec.left_margin - sec.right_margin) / 360000
    except Exception:
        pass
    total_cm = max(4.0, min(float(total_cm), 24.0))
    widths_cm = [max(1.2, total_cm * w / total) for w in weights]
    scale = total_cm / (sum(widths_cm) or 1.0)  # 归一化，避免浮点误差溢出页边距
    widths_cm = [w * scale for w in widths_cm]
    twips = [int(round(c * 567)) for c in widths_cm]

    tbl_pr = table._tbl.tblPr
    tbl_w = tbl_pr.find(qn("w:tblW"))
    if tbl_w is None:
        tbl_w = OxmlElement("w:tblW")
        tbl_pr.insert_element_before(
            tbl_w, "w:jc", "w:tblCellSpacing", "w:tblInd", "w:tblBorders",
            "w:shd", "w:tblLayout", "w:tblCellMar", "w:tblLook",
            "w:tblCaption", "w:tblDescription")
    tbl_w.set(qn("w:w"), str(sum(twips)))
    tbl_w.set(qn("w:type"), "dxa")
    layout = tbl_pr.find(qn("w:tblLayout"))
    if layout is None:
        layout = OxmlElement("w:tblLayout")
        tbl_pr.insert_element_before(
            layout, "w:tblCellMar", "w:tblLook", "w:tblCaption",
            "w:tblDescription")
    layout.set(qn("w:type"), "fixed")
    for row in table.rows:
        for ci, cell in enumerate(row.cells):
            cell.width = Cm(widths_cm[ci])
    return widths_cm


def _finalize_simple_table(table, *, header_rows: int = 1,
                           widths_cm: tuple[float, ...] = (4.2, 11.8)) -> None:
    """收口两列「名称 / 内容」表：固定布局、行不跨页、表头跨页重复。

    ✅ R127（2026-10-22）：附录事实表 / 参考资料表此前直接用 ``add_table``
    逐行拼装，实测 15 张表**既无 tblHeader 也无 cantSplit、且非 fixed 布局**：
    跨页后表头丢失、长内容行被从中间劈开、列宽随内容漂移。正文 GFM 表格早已
    由 ``_add_table_from_markup`` 处理，本 helper 补齐附录/表单类表格。
    """
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Cm

    ncols = len(table.columns)
    if not ncols:
        return
    for row in table.rows:
        _set_row_cant_split(row)
    if header_rows and len(table.rows) >= header_rows:
        tr_pr = table.rows[header_rows - 1]._tr.get_or_add_trPr()
        if tr_pr.find(qn("w:tblHeader")) is None:
            el = OxmlElement("w:tblHeader")
            el.set(qn("w:val"), "true")
            tr_pr.append(el)
    widths = list(widths_cm) if len(widths_cm) == ncols else None
    if widths is None:
        step = 14.0 / ncols
        widths = [step] * ncols
    # 按当前节的实际版心宽度收敛，避免自定义页边距时表格溢出页面
    available_cm = 16.0
    try:
        sec = table.part.document.sections[-1]
        available_cm = (sec.page_width - sec.left_margin - sec.right_margin) / 360000
    except Exception:
        pass
    total_w = sum(widths)
    if total_w > available_cm > 0:
        widths = [w * available_cm / total_w for w in widths]
    tbl_pr = table._tbl.tblPr
    tbl_w = tbl_pr.find(qn("w:tblW"))
    if tbl_w is None:
        tbl_w = OxmlElement("w:tblW")
        # CT_TblPr 严格序列：tblW 在 jc/tblBorders/shd/tblLayout 之前
        tbl_pr.insert_element_before(
            tbl_w, "w:jc", "w:tblCellSpacing", "w:tblInd", "w:tblBorders",
            "w:shd", "w:tblLayout", "w:tblCellMar", "w:tblLook",
            "w:tblCaption", "w:tblDescription")
    tbl_w.set(qn("w:w"), str(int(round(sum(widths) * 567))))
    tbl_w.set(qn("w:type"), "dxa")
    layout = tbl_pr.find(qn("w:tblLayout"))
    if layout is None:
        layout = OxmlElement("w:tblLayout")
        tbl_pr.insert_element_before(
            layout, "w:tblCellMar", "w:tblLook", "w:tblCaption",
            "w:tblDescription")
    layout.set(qn("w:type"), "fixed")
    for row in table.rows:
        for ci, cell in enumerate(row.cells):
            cell.width = Cm(widths[ci])


def _add_cover_info_table(doc, info: dict, font_name: str) -> None:
    """在封面插入项目信息表（两列：标签 / 值，整体居中）。

    仅当 info 含非空值时渲染；空值条目自动跳过，避免封面出现空白行。
    标签列右对齐加粗、值列左对齐，整体 14cm 宽、浅灰底纹。
    """
    from docx.enum.table import WD_ALIGN_VERTICAL, WD_TABLE_ALIGNMENT
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Cm

    if not isinstance(info, dict):
        return
    pairs = [(str(k), str(v).strip()) for k, v in info.items() if str(v).strip()]
    if not pairs:
        return
    for _ in range(1):
        doc.add_paragraph()
    table = doc.add_table(rows=len(pairs), cols=2)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.style = "Table Grid"
    table.autofit = False
    tbl_pr = table._tbl.tblPr
    layout = tbl_pr.find(qn("w:tblLayout"))
    if layout is None:
        layout = OxmlElement("w:tblLayout")
        tbl_pr.append(layout)
    layout.set(qn("w:type"), "fixed")
    tbl_w = tbl_pr.find(qn("w:tblW"))
    if tbl_w is None:
        tbl_w = OxmlElement("w:tblW")
        tbl_pr.append(tbl_w)
    tbl_w.set(qn("w:w"), str(int(14 * 567)))
    tbl_w.set(qn("w:type"), "dxa")
    for i, (k, v) in enumerate(pairs):
        c0, c1 = table.rows[i].cells
        c0.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
        c1.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
        c0.width = Cm(4)
        c1.width = Cm(10)
        p0 = c0.paragraphs[0]
        p0.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        _set_run_font(p0.add_run(k), font_name, 12, bold=True)
        p1 = c1.paragraphs[0]
        p1.alignment = WD_ALIGN_PARAGRAPH.LEFT
        _set_run_font(p1.add_run(v), font_name, 12)
        # ✅ R109：同上，改走按序列插入的单一实现（并防重复 w:shd）
        _set_cell_shading(c0, "F2F2F2")
        _set_cell_shading(c1, "F2F2F2")
    # ✅ R127：封面信息表同样禁止行跨页（无表头，不设 tblHeader）
    _finalize_simple_table(table, header_rows=0, widths_cm=(4.0, 10.0))




# ---------------------------------------------------------------------------
# ✅ 2026-10-11（引入《文档生成规则分析与软件标准建立》第四部分 4.2 导出标准）：
#    可选**版式预设**。既有默认（2.5cm 四边 / 行距 1.15）是历轮锁定的字节契约，
#    一字不动；预设仅在 config.layout_preset 显式点名时整体覆盖行距与四边距。
#    "engineering" 取通用 Word「工程公文」档：上下 2.54cm、左右 3.18cm、1.5 倍行距。
# ---------------------------------------------------------------------------
LAYOUT_PRESETS: dict[str, dict] = {
    "engineering": {
        "line_spacing": 1.5,
        "margins": {"top": 2.54, "bottom": 2.54, "left": 3.18, "right": 3.18},
    },
}


def _resolve_layout_preset(config: dict, base_opts: dict) -> dict:
    """按 config.layout_preset 覆盖版式字段（纯函数，未知/缺省键原样返回）。

    返回**新 dict**（base_opts 不被就地修改）；预设命中时只覆盖
    line_spacing 与 margins 两项 —— 字体/字号/标题样式等由用户显式配置继续生效。
    """
    name = str((config or {}).get("layout_preset", "") or "").strip().lower()
    preset = LAYOUT_PRESETS.get(name)
    if not preset:
        return dict(base_opts)
    out = dict(base_opts)
    out["line_spacing"] = max(1.0, min(float(preset["line_spacing"]), 3.0))
    _mg = dict(preset["margins"])
    # 用户在 config.margins 里显式给过的键保持原值（预设补缺，不强改细调）
    _user_margins = (config or {}).get("margins")
    if isinstance(_user_margins, dict):
        for _k, _v in _user_margins.items():
            try:
                if _v is not None and float(_v) > 0:
                    _mg[_k] = float(_v)
            except (TypeError, ValueError):
                continue
    out["margins"] = _mg
    return out


# ---------------------------------------------------------------------------# ---------------------------------------------------------------------------
# 专项施工方案法定前置表单（2026-10-02 · 第二十三轮）
# ---------------------------------------------------------------------------
# 依据（均为部文原文，不凭记忆编写）：
# · 住建部令第37号 第十一条：专项施工方案应当由施工单位技术负责人审核签字、
#   加盖单位公章，并由总监理工程师审查签字、加盖执业印章后方可实施；
#   危大工程实行分包并由分包单位编制的，应当由总承包单位技术负责人及分包
#   单位技术负责人**共同**审核签字并加盖单位公章。
# · 住建部令第37号 第十二条：专家应当从专家库中选取，符合专业要求且
#   **人数不得少于 5 名**；与本工程有利利害关系的人员不得以专家身份参加。
# · 住建部令第37号 第十三条：专家论证会后应当形成**论证报告**，对专项
#   施工方案提出**通过、修改后通过或者不通过**的一致意见，专家签字确认。
# · 建办质〔2018〕31号 三、参会人员五类；四、专家论证主要内容三项。
# · 建办质〔2018〕31号 二、第(九)项：计算书**及相关施工图纸** → 图纸附件清单。
#
# 红线：与正文同一口径 —— **不编造**人名、证书编号、单位名称与日期，
# 签字栏一律留空由各方手签（表单本就是待签状态，留空才是正确产物）。
# 空值行不渲染，避免交付文档出现「XXX：」这类空栏。


def _add_form_table(doc, rows: list[tuple[str, str]], font_name: str = "宋体",
                    col1_cm: float = 4.0, col2_cm: float = 10.0,
                    keep_empty: bool = False) -> None:
    """插入「标签 / 值」两列表单（审批表、论证报告表共用）。

    与 :func:`_add_cover_info_table` 同款固定布局（14cm 宽、灰底纹），
    区别是不强制换页 —— 供正文前的法定表单复用。

    ``keep_empty`` 区分两类行（⚠️ 缺了它会静默吞掉整张签字表）：
    · False（默认）：值列为空的行**跳过** —— 用于「工程名称 / 方案编号」
      这类抬头行，没填就不该在交付文档里留「工程名称：」的空栏；
    · True：值列为空也**保留** —— 用于签字栏。签字栏的「空」不是缺失，
      恰恰是正确产物（表单交付态即「待签」，由各方线下手签）；
      把它当空值过滤掉，等于把整张审批表 / 论证报告表的签字栏抹平，
      文档看起来"干净"却彻底失去法律效力。
    """
    from docx.enum.table import WD_ALIGN_VERTICAL, WD_TABLE_ALIGNMENT
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Cm

    pairs = [(str(k), str(v or "").strip()) for k, v in rows
             if keep_empty or str(v or "").strip()]
    if not pairs:
        return
    table = doc.add_table(rows=len(pairs), cols=2)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.style = "Table Grid"
    table.autofit = False
    tbl_pr = table._tbl.tblPr
    layout = tbl_pr.find(qn("w:tblLayout"))
    if layout is None:
        layout = OxmlElement("w:tblLayout")
        tbl_pr.append(layout)
    layout.set(qn("w:type"), "fixed")
    tbl_w = tbl_pr.find(qn("w:tblW"))
    if tbl_w is None:
        tbl_w = OxmlElement("w:tblW")
        tbl_pr.append(tbl_w)
    tbl_w.set(qn("w:w"), str(int(14 * 567)))
    tbl_w.set(qn("w:type"), "dxa")
    for i, (k, v) in enumerate(pairs):
        c0, c1 = table.rows[i].cells
        for cell in (c0, c1):
            cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
            cell.width = Cm(col1_cm if cell is c0 else col2_cm)
            # ✅ R109：同上，改走按序列插入的单一实现（并防重复 w:shd）
            _set_cell_shading(cell, "F2F2F2")
        p0 = c0.paragraphs[0]
        p0.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        _set_run_font(p0.add_run(k), font_name, 10.5, bold=True)
        p1 = c1.paragraphs[0]
        p1.alignment = WD_ALIGN_PARAGRAPH.LEFT
        _add_runs_with_inline_format(p1, v)


def _add_prelim_page_title(doc, title: str, font_name: str,
                           basis: str = "") -> None:
    """前置页页题（居中加粗 + 编制依据注），并为其后留白。"""
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt

    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_after = Pt(12)
    _set_run_font(p.add_run(title), font_name, 16, bold=True)
    if basis:
        b = doc.add_paragraph()
        b.alignment = WD_ALIGN_PARAGRAPH.CENTER
        b.paragraph_format.space_after = Pt(10)
        _set_run_font(b.add_run(basis), font_name, 9)


def _scheme_form_header(scheme: dict, cover_info: dict | None) -> list[tuple[str, str]]:
    """四张前置表共用的「工程 / 方案」抬头行（空值自动过滤）。"""
    info = cover_info if isinstance(cover_info, dict) else {}
    # ✅ 2026-10-11（文档质量报告 P0-B）：抬头行同样过模板残留清洗。
    #    残留串被置空后由 `_add_form_table` 的「空值行跳过」自动隐藏该行，
    #    表单不会因此变形；此处不做项目名兜底（表头应来自封面信息，不猜）。
    return [
        ("工程名称", _sanitize_template_residue_title(info.get("工程名称", ""))),
        ("方案名称", _sanitize_template_residue_title((scheme or {}).get("name", ""))),
        ("方案编号", info.get("方案编号", "")),
        ("编制单位", info.get("编制单位", "")),
    ]


def _add_compilation_note_page(doc, scheme: dict, cover_info: dict | None,
                               font_name: str) -> None:
    """编制说明（专项施工方案标准组成，说明编制背景与执行要求）。

    仅陈述可从方案自身与封面信息确定的内容；不从事实库取未经确认的数值，
    不出现「待补充」类占位（与正文「数据真实性红线」同款要求）。
    """
    info = cover_info if isinstance(cover_info, dict) else {}
    project_name = _sanitize_template_residue_title(info.get("工程名称", "")).strip()
    scheme_name = _sanitize_template_residue_title((scheme or {}).get("name", "")).strip()
    parts = [
        f"本方案为{project_name}的{scheme_name}，"
        f"由施工单位技术负责人组织工程技术人员编制，"
        f"经施工单位技术负责人审核签字并加盖单位公章、"
        f"总监理工程师审查签字后方可实施。"
        if project_name and scheme_name else
        "本方案由施工单位组织工程技术人员编制，"
        "经施工单位技术负责人审核签字并加盖单位公章、"
        "总监理工程师审查签字后方可实施。",
        "本方案依据国家、行业及地方现行法律法规、部门规章、"
        "强制性工程建设规范和本专业技术标准，并结合本工程设计文件、"
        "施工组织设计与现场实际条件编制，编制依据详见「编制依据」章节。",
        "本方案是指导本专项工程施工的作业文件，"
        "施工现场应严格执行；因设计变更、规划调整等原因确需调整的，"
        "修改后的专项施工方案应当重新履行审核（及论证）程序。",
        "本方案的技术参数与施工做法已结合现场条件核定，"
        "现场实际条件与本方案不一致时，应及时反馈编制单位核实，"
        "不得擅自修改后直接实施。",
    ]
    for text in parts:
        p = doc.add_paragraph()
        p.paragraph_format.first_line_indent = None
        _add_runs_with_inline_format(p, text)


def _add_scheme_approval_table(doc, scheme: dict, cover_info: dict | None,
                               font_name: str) -> None:
    """专项施工方案审批表（住建部令第37号 第十一条）。

    签字栏留空 —— 表单交付态即「待签」，由各方线下手签，
    严禁预填人名 / 证书编号（AGENTS.md 数据真实性红线）。
    """
    from docx.shared import Pt

    _add_prelim_page_title(doc, "专项施工方案审批表", font_name,
                           "依据《危险性较大的分部分项工程安全管理规定》"
                           "（住建部令第37号）第十一条")
    _add_form_table(doc, _scheme_form_header(scheme, cover_info), font_name)
    doc.add_paragraph()
    _add_form_table(doc, [
        ("编制人（签字）", ""),
        ("编制日期", ""),
        ("施工单位技术负责人（审核签字）", ""),
        ("审核日期", ""),
        ("总承包单位技术负责人（分包工程共同审核）", ""),
        ("专业分包单位技术负责人（分包工程共同审核）", ""),
        ("总监理工程师（审查签字）", ""),
        ("审查日期", ""),
    ], font_name, col1_cm=6.0, col2_cm=8.0, keep_empty=True)
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(10)
    _add_runs_with_inline_format(
        p, "注：本表签字并加盖单位公章（总监理工程师加盖执业印章）齐全后方可实施；"
           "实行分包的专项施工方案，由总承包单位与分包单位技术负责人共同审核签字。")


def _add_expert_review_form(doc, scheme: dict, cover_info: dict | None,
                            font_name: str) -> None:
    """专家论证会论证报告（住建部令第37号 第十二/十三条 + 建办质〔2018〕31号 三/四）。

    论证结论固定三选一（通过 / 修改后通过 / 不通过），专家栏按
    「人数不得少于 5 名」给出 5 行空栏；论证内容三项照 31 号文原文逐字列出。
    """
    from docx.shared import Pt

    _add_prelim_page_title(doc, "专项施工方案专家论证报告", font_name,
                           "依据《危险性较大的分部分项工程安全管理规定》"
                           "（住建部令第37号）第十二条、第十三条，"
                           "建办质〔2018〕31号第三条、第四条")
    _add_form_table(doc, _scheme_form_header(scheme, cover_info), font_name)
    doc.add_paragraph()

    p = doc.add_paragraph()
    _set_run_font(p.add_run("一、专家论证主要内容（建办质〔2018〕31号 第四条）"),
                  font_name, 12, bold=True)
    for item in (
        "（一）专项施工方案内容是否完整、可行；",
        "（二）专项施工方案计算书和验算依据、施工图是否符合有关标准规范；",
        "（三）专项施工方案是否满足现场实际情况，并能够确保施工安全。",
    ):
        q = doc.add_paragraph()
        _add_runs_with_inline_format(q, item)

    doc.add_paragraph()
    p = doc.add_paragraph()
    _set_run_font(p.add_run("二、论证结论（住建部令第37号 第十三条，三选一）"),
                  font_name, 12, bold=True)
    _add_form_table(doc, [("论证结论", "□ 通过    □ 修改后通过    □ 不通过"),
                          ("论证意见（修改后通过时须写明具体修改内容）", "")],
                    font_name, col1_cm=6.0, col2_cm=8.0)

    doc.add_paragraph()
    p = doc.add_paragraph()
    _set_run_font(p.add_run("三、参会人员（建办质〔2018〕31号 第三条）"),
                  font_name, 12, bold=True)
    _add_form_table(doc, [
        ("专家（不得少于 5 名）", ""),
        ("建设单位项目负责人", ""),
        ("勘察、设计单位项目技术负责人及相关人员", ""),
        ("施工总承包 / 分包单位项目负责人、项目技术负责人、"
         "专项施工方案编制人员、项目专职安全生产管理人员", ""),
        ("监理单位项目总监理工程师及专业监理工程师", ""),
    ], font_name, col1_cm=6.0, col2_cm=8.0, keep_empty=True)

    doc.add_paragraph()
    p = doc.add_paragraph()
    _set_run_font(p.add_run("四、专家签字确认（专家对论证报告负责并签字确认）"),
                  font_name, 12, bold=True)
    # 住建部令第37号 第十二条：专家人数不得少于 5 名 —— 固定 5 行空栏
    _add_form_table(doc, [(f"专家 {i}（签字）", "") for i in range(1, 6)],
                    font_name, col1_cm=6.0, col2_cm=8.0, keep_empty=True)
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(10)
    _add_runs_with_inline_format(
        p, "注：专家应当从地方人民政府住房城乡建设主管部门建立的专家库中选取，"
           "符合专业要求且人数不得少于 5 名；与本工程有利害关系的人员"
           "不得以专家身份参加专家论证会。结论为「修改后通过」的，"
           "应当按论证意见修改完善后重新履行审核程序后方可实施。")


def _add_drawing_appendix_page(doc, scheme: dict, font_name: str) -> None:
    """施工图纸附件清单（建办质〔2018〕31号 第(九)项「计算书及相关施工图纸」）。

    随文附图逐项登记；无图纸时给出编号栏 + 名称栏空表由现场填写，
    不预填图号（图号由导出器统一编排，正文不得自写 —— 与正文图号纪律一致）。
    """
    from docx.shared import Pt

    _add_prelim_page_title(doc, "施工图纸附件清单", font_name,
                           "依据建办质〔2018〕31号 专项施工方案内容 第（九）项")
    _add_form_table(doc, _scheme_form_header(scheme, None), font_name)
    doc.add_paragraph()
    p = doc.add_paragraph()
    _set_run_font(p.add_run("随本方案附以下施工图纸，图纸应与正文「计算书及相关施工图纸」"
                            "章节相互对应、编号连续："), font_name, 10.5)
    doc.add_paragraph()
    rows = [("图号", "图纸名称", "比例 / 说明")]
    rows += [(str(i), "", "") for i in range(1, 9)]
    _add_table_from_markup(doc, [
        "| " + " | ".join(rows[0]) + " |",
        "| " + " | ".join(["---"] * 3) + " |",
        *["| " + " | ".join(r) + " |" for r in rows[1:]],
    ], font_name)
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(10)
    _add_runs_with_inline_format(
        p, "注：图号由导出器统一编排并与正文图题连续；本清单仅登记图名与内容，"
           "不预填图号。危大工程涉及第三方监测的，监测点布置图一并列入本清单。")


def _set_margins(section, margins: dict) -> None:
    """设置页面边距（单位 cm，缺省 2.5）。"""
    from docx.shared import Cm

    if not isinstance(margins, dict):
        margins = {}

    def _cm(key, default):
        try:
            return Cm(float(margins.get(key, default)))
        except (TypeError, ValueError):
            return Cm(default)

    section.left_margin = _cm("left", 2.5)
    section.right_margin = _cm("right", 2.5)
    section.top_margin = _cm("top", 2.5)
    section.bottom_margin = _cm("bottom", 2.5)


def _content_width_cm(doc) -> float:
    """当前节「正文可用宽度」（cm）—— 图片/表格共用的版心口径。

    ✅ R127（2026-10-22）：图片宽度旧实现固定封顶 ``_CHART_MAX_WIDTH_CM``
    （16cm，对应默认 2.5cm 页边距下的版心）。启用 ``engineering`` 版式预设
    （左右 3.18cm）时版心只剩 14.65cm，16cm 宽图会横向溢出页面 —— 这里按
    实际节属性换算，取不到时回退 16cm（与旧行为逐字一致）。
    """
    fallback = _CHART_MAX_WIDTH_CM
    try:
        sec = doc.sections[-1]
        width = (sec.page_width - sec.left_margin - sec.right_margin) / 360000
    except Exception:
        return fallback
    if not width or width <= 0:
        return fallback
    return float(width)


# ---------------------------------------------------------------------------
# ✅ 2026-09-25 修复（P0 · 图表尺寸溢出页面 / 窄图被拉伸）
#    旧实现只按「PNG 像素 × 2.54 / 96dpi」估算物理宽度、且只用 16cm 宽度封顶，
#    **完全无视图片高度**。而本项目渲染器产出的是 2.5x 超采样栅格图
#    （后端 PIL 2.5x + 300 DPI 元数据 / 前端 canvas 2.5x），纵向流程图
#    （`flowchart TD` 十几个节点，专项方案里极常见）实测 700×4070 px：
#      · 旧算法 → 宽度封顶 16cm，高度 = 16 × 4070/700 = **93.03cm**
#        （python-docx 默认页面为 Letter，正文可用高度仅 22.94cm；A4 为 24.7cm）
#        → 单张图比页面高 3.8 倍，Word 里必然溢出/被裁切，成稿不可用；
#      · 新算法：优先用 PNG 自带 DPI 元数据（本项目渲染器统一写 300 DPI）换算
#        真实物理尺寸，再同时受「正文栏宽 16cm」与「单图最大高度 22cm」约束，
#        等比缩放 → 同一张图实测 3.78×22.00cm，完整落在页面内，图内文字仍保持
#        300 DPI 有效分辨率。
#    向后兼容：宽图（占实际图表 99%）两种算法的结果都被 16cm 宽度上限夹住，
#    产出尺寸逐字节一致；只有"旧算法会溢出页面"的极端窄高图行为发生变化。
_CHART_MAX_WIDTH_CM = 16.0
_CHART_MAX_HEIGHT_CM = 22.0
_CHART_DPI_FALLBACK = 96.0


def _read_image_dpi(info_dpi) -> float:
    """从 PIL 的 ``img.info["dpi"]`` 取值（容错：元组/标量/字符串/缺失均可用）。

    返回 0.0 表示"无有效 DPI"（由 `_fit_image_cm` 回退到 96dpi 口径）。
    """
    if not info_dpi:
        return 0.0
    if isinstance(info_dpi, (tuple, list)):
        if not info_dpi:
            return 0.0
        info_dpi = info_dpi[0]
    try:
        dpi = float(info_dpi)
    except (TypeError, ValueError):
        return 0.0
    # 1 或更小的"DPI"是脏数据（0/1 会让物理尺寸爆表），按缺失处理
    return dpi if dpi > 1.0 else 0.0


def _fit_image_cm(w_px, h_px, dpi: float | None = None,
                  max_w: float = _CHART_MAX_WIDTH_CM,
                  max_h: float = _CHART_MAX_HEIGHT_CM) -> tuple[float, float]:
    """按像素尺寸 + DPI 计算图片插入尺寸（cm），**同时**受宽/高上限约束。

    纯函数（不依赖 docx / PIL 打开文件），便于单测直接断言尺寸口径。

    Args:
        w_px / h_px: 图片像素尺寸；非法值 → 回退 ``(max_w, 0.0)``（按栏宽插入）。
        dpi: 图片 DPI 元数据；``None``/``<=1`` → 回退 `_CHART_DPI_FALLBACK`(96)。
        max_w / max_h: 正文栏宽、单图最大高度上限（cm）。

    Returns:
        ``(宽 cm, 高 cm)``，等比缩放后均已封顶，保留两位小数。
        ``h_cm`` 为 0.0 时表示"尺寸不可知"，调用方只传宽度即可（由 Word 按
        图片自身比例缩放）。
    """
    try:
        w_px = float(w_px)
        h_px = float(h_px)
    except (TypeError, ValueError):
        return max_w, 0.0
    if w_px <= 0 or h_px <= 0:
        return max_w, 0.0
    _dpi = _read_image_dpi(dpi) or _CHART_DPI_FALLBACK

    w_cm = w_px * 2.54 / _dpi
    h_cm = h_px * 2.54 / _dpi
    scale = 1.0
    if w_cm > max_w:
        scale = min(scale, max_w / w_cm)
    if h_cm > max_h:
        scale = min(scale, max_h / h_cm)
    return round(w_cm * scale, 2), round(h_cm * scale, 2)


def _figure_chapter_num(heading_gen) -> int:
    """图号 / 表号中的"章序号"（>0 恒成立）。

    ✅ P0 修复（2026-09-27 · 图号命名空间塌缩 / 图号虚跳）：
      旧实现三处（表格题注 / 图表 / AI 配图）都写死
      ``heading_gen.counters[0] or 1`` —— ``counters[0]`` 是 **L1 专属**计数器。
      当目录树里没有 L1（用户把一级目录删成只剩 L2 子树、或导入的方案本身就
      以 L2 为根）时它恒为 0，于是**全文所有章节的图表都被塞进 ch1 命名空间**，
      产出"图 1-1、图 1-2 …"跨章节连续编号 —— 图号等于失去了章节归属，
      正文里"图 1-3"可能被误读成第一章的第三张图（与图号虚跳同源）。

      修复：上溯到**当前实际存在的最高层级祖先**计数器。正常含 L1 的目录树
      行为与旧实现逐字一致（counters[0] 非零即返回它），仅在缺 L1 时才回退到
      L2 / L3 … 的计数器 —— 此时每个平级章节各自占据独立命名空间，符合
      "图号归属章节"的语义。
    """
    counters = getattr(heading_gen, "counters", None) or []
    for value in counters:
        try:
            n = int(value or 0)
        except (TypeError, ValueError):
            continue
        if n > 0:
            return n
    return 1


#: python-docx（底层 OOXML ``a:blip``）真正支持的图片格式。
#: WEBP / AVIF / HEIC 等现代格式 PIL 能解码，但 docx 写入时会抛
#: ``UnrecognizedImageError`` —— 即"图号已占用但成稿里没有图"。
_DOCX_SAFE_IMAGE_FORMATS = frozenset({"PNG", "JPEG", "GIF", "BMP", "TIFF"})


def _image_format_supported(img_bytes) -> bool:
    """图片字节流是否为 docx 可安全插入的格式。

    ✅ BUG 修复（2026-09-27 · P1）：``_chart_ok`` 原先只验"是不是图片 + 长度 > 100"，
    不验"docx 能不能插入"。实测 WEBP 通过校验后，图号已被自增占用，
    ``doc.add_picture`` 却抛 ``UnrecognizedImageError`` 被 except 吞掉 ——
    成稿只剩一个孤立的「图 1-1 测试配图」图题，且因 ``ai_image_pending=0`` /
    ``render_stats["failed"]=0`` 而**被写入 export_cache**，服务恢复后永久命中坏产物。

    与 ``_chart_ok`` 同口径：判定必须发生在**占用图号之前**，否则又是虚跳编号。
    """
    try:
        from PIL import Image as PILImage
        with PILImage.open(BytesIO(img_bytes.getvalue())) as im:
            return (im.format or "").upper() in _DOCX_SAFE_IMAGE_FORMATS
    except Exception:
        return False


def _chart_ok(img_bytes) -> bool:
    """图表渲染结果是否可用（与 ``_add_inline_chart_from_bytes`` 判定口径完全一致）。

    单独抽出是因为"跳过 vs 占位"的决策必须发生在**占用图号之前**，
    否则会出现"图 1-2 却没有 1-1"的虚跳编号。
    """
    try:
        if not (img_bytes and len(img_bytes.getvalue()) > 100):
            return False
    except Exception:  # pragma: no cover - BytesIO 异常即视为渲染失败
        return False
    # 格式白名单：PIL 能解码 ≠ docx 能插入（见 _image_format_supported）
    return _image_format_supported(img_bytes)


# ---------------------------------------------------------------------------
# ✅ BUG 修复（2026-10-06 · P0 逐块 fail-soft）：单个内容块渲染抛异常 → 整份导出 500
# ---------------------------------------------------------------------------
# 现象：write_section 的块分发里，**只有图表 / 配图两条分支**做了 fail-soft
#    （渲染失败即跳过并回退图号）。table / code / heading / list_item / quote /
#    普通段落这 6 类分支**完全没有保护**：任何一个块渲染抛异常都会冒到
#    `_build_docx_sync` 之外 → export_docx 返回 500，
#    用户**一分钱都拿不到**（连"降级产物"都没有）。
#    实测（monkeypatch `_add_table_from_markup` 抛异常）：PROPAGATED RuntimeError。
#    对交付类文档而言，"少一个表格的版式"远好于"整份方案导不出来"。
# 修法：把**整个块分发**包成一次事务 ——
#    ① 记录 body 元素快照 + 图号/表号/去重集合快照；
#    ② 异常时回滚文档元素与全部计数器（不留半截表格、不留已占图号）；
#    ③ 降级为**纯文本段落**保留内容（文字绝不丢），并累加
#       `block_render_failed` 供日志 + X-Fix-Stats 响应头观测。


def _block_plain_text(block: dict) -> str:
    """把内容块降级为可读的纯文本（表格/代码块保留原始行）。"""
    bt = block.get("type")
    if bt == "table":
        return "\n".join(str(x) for x in (block.get("lines") or []))
    if bt == "code":
        return "\n".join(str(x) for x in (block.get("lines") or []))
    for key in ("_fixed_text", "text"):
        v = block.get(key)
        if v:
            return str(v)
    return ""


def _degrade_block_to_text(doc, block: dict, font_name: str, font_size: float) -> bool:
    """块渲染失败后的降级出口：纯文本段落兜底 + 统计累加。返回是否成功降级。"""
    from docx.shared import Cm, Pt

    st = _fix_stats()
    st["block_render_failed"] = st.get("block_render_failed", 0) + 1
    logger.warning("导出：内容块（type=%s）渲染失败，已降级为纯文本保留：%s",
                   block.get("type"), str(block.get("text") or "")[:40])
    txt = _block_plain_text(block)
    if not txt.strip():
        return False
    try:
        p = doc.add_paragraph()
        _add_runs_with_inline_format(p, txt)
        p.paragraph_format.first_line_indent = Cm(0)
        p.paragraph_format.space_after = Pt(3)
        return True
    except Exception as e:  # pragma: no cover - 连纯文本都失败则放弃该块
        logger.warning("导出：内容块降级为纯文本仍失败（该块内容缺失）: %s", e)
        return False


def _pop_orphan_lead_in(doc) -> bool:
    """删除文末"孤儿引导语"段落（图表被跳过时留下的"如下图所示："）。

    ✅ 2026-09-25 修复（图文逻辑连贯 · 容错降级的一致性）：
    图表在导出时被跳过（渲染失败 / 同代码去重 / 无可用代码 / AI 配图未生成）时，
    只 `continue` 会把它上方那句**专为引出该图而写**的引导语（"施工工艺流程
    如下图所示："）留在正文里 —— 成稿出现"见下图"却无图的悬空引用，
    评审一眼可见（与"渲染失败红字"同样属于交付硬伤）。
    现在跳过图表块时顺带回收该引导语：**仅当**文末最后一个 body 元素就是
    紧邻的段落、文本 ≤60 字、且以"如下图/见下图/如图所示"类引导语收尾时
    才删除，绝不触碰标题、列表、表格与正文中间的长句。

    Returns:
        是否执行了删除。
    """
    try:
        from docx.oxml.ns import qn
        paras = doc.paragraphs
        if not paras:
            return False
        last = paras[-1]
        body = doc.element.body
        # 文末唯一合法尾随元素是节属性 sectPr；倒数第一个"内容元素"必须就是
        # 这个段落，否则（如刚写入表格）不能删 —— 否则会误删表格前的段落。
        content_els = [el for el in body if el.tag != qn("w:sectPr")]
        if not content_els or content_els[-1] is not last._element:
            return False
        try:
            if last.style is not None and (last.style.name or "").startswith("Heading"):
                return False
        except Exception:
            pass
        text = (last.text or "").strip()
        if not text or len(text) > LEAD_IN_MAX_CHARS:
            return False
        if not _LEAD_IN_HINT_RE.search(text):
            return False
        last._element.getparent().remove(last._element)
        logger.info("图表被跳过：已同步移除其孤儿引导语「%s」", text[:30])
        return True
    except Exception as e:  # pragma: no cover - 版式清理失败不应阻断导出
        logger.debug("孤儿引导语清理失败（忽略）: %s", e)
        return False


class _DocxBodyRollback:
    """记录 ``doc.element.body`` 的子元素数，用于**失败时回滚本次新增元素**。

    ✅ BUG 修复（2026-10-06 · P0 图号重号，确定性复现）：
      ``_add_inline_chart_from_bytes`` / ``_add_illustration_from_bytes`` 的
      ``doc.add_picture()`` 与图题段落是**两次独立写入**。若图片已写入成功、
      随后图题步骤抛异常（字体名非法 / runs 被外部改写 / docx 内部异常），
      函数 ``return False`` 而**图片仍留在文档里**；调用方按契约回退图号
      （``figure_counters -= 1``）→ 下一张图重新拿到**同一个图号**，
      成稿出现**两张图都标注「图 1-1」**的重号。
      实测（``_exp_probe3``）：两张不同流程图，第一张图题阶段抛错 →
      ``pics=2 captions=['图 1-1 施工流程图', '图 1-1 施工流程图']``。
      修复：把「插入」变成**原子操作** —— 失败时删除本次新增的 body 子元素，
      使「返回 False」严格等价于「本次未向文档写入任何元素」，
      调用方的图号回退才是安全的。

    ⚠️ 实现要点（踩过一次坑）：**不能**用「记录 ``len(body)`` 再删尾部切片」
    的方式。``python-docx`` 的 ``add_picture`` / ``add_paragraph`` 把新元素
    插入在**节属性 ``w:sectPr`` 之前**，于是新元素的下标 < 快照下标，
    尾部切片只切得到 sectPr（还要跳过）→ 一个元素都删不掉，实测
    ``ins2(fail)=False`` 但 ``pics=1``、body 仍是 ``['p','sectPr']``。
    正确做法是**快照已有元素的对象身份**（lxml 在持有强引用期间对同一元素
    恒返回同一代理对象，故 ``is`` 比较可靠）。
    """

    __slots__ = ("_body", "_seen")

    def __init__(self, doc):
        self._body = doc.element.body
        # 持有强引用：lxml 只在无引用时重建代理对象，持有后 ``is`` 比较才稳定
        self._seen = list(self._body)

    def rollback(self) -> int:
        """删除本次调用新增的 body 子元素（跳过节属性 sectPr），返回删除数。"""
        from docx.oxml.ns import qn

        removed = 0
        try:
            for el in list(self._body):
                if any(el is s for s in self._seen):
                    continue  # 调用前就存在的元素，绝不动
                if el.tag == qn("w:sectPr"):
                    continue  # 节属性是文档骨架，绝不可删
                self._body.remove(el)
                removed += 1
        except Exception as e:  # pragma: no cover - 回滚失败不应掩盖原异常
            logger.warning("导出：图片插入失败后回滚文档元素失败（可能有残留元素）: %s", e)
        return removed


def _write_chart_placeholder_line(doc, caption_text: str, reason: str) -> None:
    """写红字占位行「[图 X-Y 图名 — 渲染失败/插入失败]」（仅占位模式）。"""
    from docx.shared import Cm, RGBColor

    p = doc.add_paragraph()
    p.paragraph_format.first_line_indent = Cm(0)
    run = p.add_run(f"[{caption_text} — {reason}]")
    run.font.color.rgb = RGBColor(0xFF, 0x00, 0x00)


def _insert_image_with_caption(doc, img_bytes, caption_text: str, *,
                               font_name: str = "宋体", font_size: float = 10.5,
                               log_label: str = "图片",
                               log_height_scale: bool = False) -> bool:
    """DOCX 位图插入的**唯一实现**：尺寸换算 → 居中插图 → 居中图题。

    ✅ 重构（2026-10-06 · D3）：`_add_inline_chart_from_bytes` 与
    `_add_illustration_from_bytes` 此前是两份**逐行平行**的实现（差异只有图题
    来源、可用性判据、占位文案三处），任何版式修复都必须改两遍 —— 本仓反复踩的
    「同一业务判据在 2~3 处各自实现」同构陷阱（v20 的 DPI 修复就改过两次）。
    现抽为单一实现，两个入口只保留**各自的差异**（可用性判据 + 占位形态），
    使「返回 False ⇒ 未写入任何元素」这一图号回退契约也只有一份。

    失败时用 ``_DocxBodyRollback`` 删除本次写入的元素，保证调用方可以安全回退图号。
    """
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Cm

    _rb = _DocxBodyRollback(doc)
    try:
        from PIL import Image as PILImage
        stream = BytesIO(img_bytes.getvalue())
        try:
            _im = PILImage.open(stream)
            _iw_px, _ih_px = _im.size
            _dpi = _read_image_dpi(_im.info.get("dpi"))
        except Exception:
            _iw_px = _ih_px = 0
            _dpi = 0.0
        # ✅ 2026-09-25：按「栏宽 16cm × 高度 22cm」双上限等比缩放。
        #    旧实现只按 96dpi 估宽 + 仅宽度封顶，纵向长图（700×4070px）
        #    会被撑到 16×93cm，远超页面高度（Letter 22.94cm / A4 24.7cm）。
        _w_cm, _h_cm = _fit_image_cm(_iw_px, _ih_px, _dpi)
        # ✅ R127：宽度再按当前节版心封顶（默认 2.5cm 边距下版心 16cm，
        #    与 _CHART_MAX_WIDTH_CM 相同 ⇒ 字节不变；engineering 预设下收窄到
        #    14.65cm，图片不再横向溢出）。高度由 add_picture 按原比例推导。
        _avail_cm = _content_width_cm(doc)
        if _avail_cm and _w_cm > _avail_cm:
            _scale = _avail_cm / _w_cm
            _w_cm = _avail_cm
            _h_cm = _h_cm * _scale
        # ✅ R130（图表标准 · 质量要求「分辨率≥150dpi」）：按**实际入稿尺寸**
        #    反算有效分辨率（像素 / 厘米 → dpi），低于 MIN_IMAGE_DPI 只告警
        #    不阻断（fail-soft，与图号回退契约无关）——低清图仍可交付，
        #    但必须在日志可见，便于定位上游出图质量问题。
        if _iw_px and _w_cm:
            try:
                _eff_dpi = _iw_px / (_w_cm / 2.54)
                if _eff_dpi < MIN_IMAGE_DPI:
                    logger.warning(
                        "%s 有效分辨率 %.0fdpi 低于商业级下限 %ddpi"
                        "（%dpx → %.2fcm），建议提升出图尺寸",
                        log_label, _eff_dpi, MIN_IMAGE_DPI, _iw_px, _w_cm)
            except Exception:
                pass
        if log_height_scale and _h_cm >= _CHART_MAX_HEIGHT_CM:
            logger.info(
                "%s 按高度上限缩放至 %.2f×%.2fcm（页面可用高度约 %.1fcm，"
                "原图 %dx%dpx dpi=%s）",
                log_label, _w_cm, _h_cm, _CHART_MAX_HEIGHT_CM,
                _iw_px, _ih_px, _dpi or _CHART_DPI_FALLBACK)
        stream.seek(0)
        doc.add_picture(stream, width=Cm(_w_cm))
        _pic_p = doc.paragraphs[-1]
        _pic_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        # ✅ R117 FIX-7（2026-10-20）：图与图题**同页**。
        #    实测导出稿 `图 3-1` / `图 4-1` 的图题段落 keep_with_next=None
        #    （表题侧早已是 True，两张表题/图题口径不一致）—— 长文档重新分页时
        #    会出现「图在本页底部、图题被推到下一页」，读者对不上图号。
        #    keep_together 同时禁止图片段落自身被拆到两页（图片本体不可拆，
        #    但段落里的前导空行可以，故一并锁死）。
        _pic_p.paragraph_format.keep_with_next = True
        _pic_p.paragraph_format.keep_together = True
        caption = doc.add_paragraph()
        caption.alignment = WD_ALIGN_PARAGRAPH.CENTER
        caption.paragraph_format.first_line_indent = Cm(0)
        caption.paragraph_format.keep_together = True
        # ✅ R128（遗留 3 收口）：题注编号前缀加书签（正文「图 X-Y」引用可升级
        #    为 REF 域）；视觉与旧实现逐字节一致（书签为不可见元素）。
        _add_caption_text_with_ref_bookmark(caption, caption_text, font_name, font_size)
        return True
    except Exception as e:
        logger.warning("%s 插入失败: %s", log_label, e)
        _rb.rollback()
        return False


def _add_inline_chart_from_bytes(doc, chart_type: str, img_bytes, figure_num: str,
                                 default_title: str = "",
                                 font_name: str = "宋体", font_size: float = 10.5,
                                 placeholder: bool = False):
    """在 DOCX 中插入已渲染的图表图片（预渲染字节流）+ 规范图题。

    图题遵循规范「图 {章号}-{序号} {图名}」，居中加粗，字号取正文字号。
    ✅ BUG 修复：旧实现拼成「{图名} {章号}-{序号}」（如"施工进度计划 1-1"），
    图名与图号顺序颠倒，不符合图题惯例。
    ✅ 增强：图题字体/字号跟随导出配置（旧实现硬编码"宋体 / 10.5pt"，
    当用户把正文设为微软雅黑或四号时，图题会与正文明显不一致）。

    ✅ 图号虚跳收口（2026-10-03）：插入成功返回 ``True``、失败返回 ``False``，
    **不直接占用图号**（图号由调用方在成功后才递增）。失败处理：
    · ``placeholder=False``（默认）：**先回滚本次写入的文档元素**再返回
      ``False``，调用方回退图号 + 回收孤儿引导语，避免「占号却无图」的错号 /
      虚跳，**以及「图已插入却回退图号」导致的重号**（见 _DocxBodyRollback）；
    · ``placeholder=True``：仍写红字「图 X-Y — 渲染失败 / 插入失败」
      （图号已被调用方占用，属可见错误，用于排查哪张图没出来）；同样先回滚，
      保证「红字占位」与「真实图片」不同时出现在同一图号上。

    ✅ D3 重构（2026-10-06）：版式实现下沉到 ``_insert_image_with_caption``
    （与 AI 配图共用），本函数只保留图表侧**特有**的差异：字节量判据、
    占位红字形态、以及按图表类型的缩放日志。
    """
    caption_text = f"图 {figure_num} {default_title}".strip()
    # 可用性判据：字节缺失/过小。此处**不**重复校验格式白名单 —— 调用方
    # （write_section）已用 _chart_ok（= 长度 + 格式）先行判定，避免重复 IO；
    # 若调用方绕过 _chart_ok，格式问题会在 add_picture 抛错并被回滚。
    if not (img_bytes and len(img_bytes.getvalue()) > 100):
        if placeholder:
            _write_chart_placeholder_line(doc, caption_text, "渲染失败")
        return False
    if _insert_image_with_caption(
            doc, img_bytes, caption_text, font_name=font_name,
            font_size=font_size, log_label=f"图表({chart_type})",
            log_height_scale=True):
        return True
    if placeholder:
        _write_chart_placeholder_line(doc, caption_text, "插入失败")
    return False


def _add_illustration_from_bytes(doc, img_bytes, figure_num: str, alt: str = "",
                                 font_name: str = "宋体", font_size: float = 10.5,
                                 placeholder: bool = False):
    """在 DOCX 中插入 AI 配图（文生图，已下载的位图字节流）+ 规范图题。

    与图表版的区别（**仅此两点**，版式实现共用 `_insert_image_with_caption`）：
    - 可用性判据多一道格式白名单（``_image_format_supported``）—— 配图来自
      远端 URL，格式不受控（WEBP/AVIF 会被 PIL 解码但 docx 拒收）；
    - 失败时**不写红字报错**（占位模式下只留图题）—— 交付文档里出现报错文本
      比少一张图更糟。

    ✅ 图号虚跳收口（2026-10-03）：插入成功返回 ``True``、失败返回 ``False``，
    不直接占用图号。失败（含格式不安全 / 文件损坏 / 图题阶段异常）时**先回滚
    本次写入的文档元素**再返回 ``False``，由调用方回退图号 + 回收孤儿引导语，
    与 chart 分支同口径；否则会出现「图片已在文档里、图号却回退」的重号。
    ``placeholder`` 形参保留仅为签名一致（AI 配图分支当前无占位模式）。
    """
    from docx.shared import Cm

    caption_text = f"图 {figure_num} {alt or '配图'}".strip()
    if not (img_bytes and len(img_bytes.getvalue()) > 100
            and _image_format_supported(img_bytes)):
        return False
    if _insert_image_with_caption(
            doc, img_bytes, caption_text, font_name=font_name,
            font_size=font_size, log_label=f"AI 配图({caption_text})"):
        return True
    if placeholder:
        p = doc.add_paragraph()
        p.paragraph_format.first_line_indent = Cm(0)
        _set_run_font(p.add_run(caption_text), font_name, font_size, bold=True)
    return False


def _add_word_field(paragraph, instr: str, placeholder: str = "1"):
    """在段落中插入一个简单 Word 域，返回承载**域结果**的 run。

    XML 结构（R127 起按 Word 自身写法拆成 5 个 run）：
    ``fldChar(begin)`` → ``instrText`` → ``fldChar(separate)`` →
    ``w:t(占位结果)`` → ``fldChar(end)``。

    ✅ R127（2026-10-22 · DOCX 标准符合性）：旧实现把 5 个元素塞进**同一个
    ``w:r``**。CT_R 虽允许混排，但 Word/WPS 实际写出的域总是分 run 存放；
    同 run 结构在部分 WPS 版本会触发「域代码损坏」提示或被当普通文本。
    现按主流实现拆分，且返回结果 run，使页码字体只作用于数字本身。
    """
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    begin_run = paragraph.add_run()
    fld_begin = OxmlElement("w:fldChar")
    fld_begin.set(qn("w:fldCharType"), "begin")
    begin_run._r.append(fld_begin)

    instr_run = paragraph.add_run()
    instr_el = OxmlElement("w:instrText")
    instr_el.set(qn("xml:space"), "preserve")
    instr_el.text = instr
    instr_run._r.append(instr_el)

    sep_run = paragraph.add_run()
    fld_sep = OxmlElement("w:fldChar")
    fld_sep.set(qn("w:fldCharType"), "separate")
    sep_run._r.append(fld_sep)

    result_run = paragraph.add_run(placeholder)

    end_run = paragraph.add_run()
    fld_end = OxmlElement("w:fldChar")
    fld_end.set(qn("w:fldCharType"), "end")
    end_run._r.append(fld_end)
    return result_run


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
    try:
        # ✅ R151：文档大纲只到 Heading 4，域深度同步封顶 4（与 _build_docx_options 同口径）
        depth = max(1, min(int(depth or 3), 4))
    except (TypeError, ValueError):
        depth = 3
    # ✅ R127：与 PAGE/NUMPAGES 共用拆 run 的域写法（占位文本在刷新前可见）
    _add_word_field(
        paragraph,
        rf' TOC \o "1-{depth}" \h \z \u \* MERGEFORMAT ',
        "（Word 打开后自动生成目录，请按 F9 更新域）")


# ---------------------------------------------------------------------------
# ✅ R128（2026-10-22 · 遗留 3 收口）：交叉引用 REF 域 —— 题注编号加书签，
#    正文「图 X-Y / 表 X-Y」引用在渲染完成后回填为可点击的 Word REF 域。
# ---------------------------------------------------------------------------
#: 题注文本开头的编号前缀（「图 3-1 …」/「表 3-1 …」/「图3-1」紧排形态）。
_CAPTION_LEADING_NUM_RE = re.compile(r"^(?P<kind>图|表)\s*(?P<num>\d+-\d+)")
#: 正文引用形态（与预检 ``_STALE_FIG_REF_RE`` 同族但**刻意不复用**：那是
#: 「全文扫描找 stale」的诊断正则，这里是「渲染期定点包域」的写入正则，
#: 生命周期与语义都不同，合并反而制造第二处耦合）。
_REF_NUM_RE = re.compile(r"(?P<kind>图|表)\s*(?P<num>\d+-\d+)")
#: 书签前缀对齐 Word 自身的 REF 书签命名习惯（_Ref…，域更新后语义一致）。
_REF_BM_PREFIX = "_Ref_"
#: 并发隔离：与 ``_FIX_STATS_LOCAL`` 同款线程局部（DOCX 构建在多工作线程并发）。
#: 只存 id 计数器 —— 书签「登记表」就是文档本身（回填前做存在性校验，
#: 题注被 rollback 删除时登记自然失效，不指错号）。
_REFX_LOCAL = threading.local()


def _refx_state() -> dict:
    st = getattr(_REFX_LOCAL, "state", None)
    if st is None:
        st = {"next_id": 9000}
        _REFX_LOCAL.state = st
    return st


def _reset_refx() -> None:
    """测试辅助：重置书签 id 计数（生产不调用）。"""
    st = _refx_state()
    st["next_id"] = 9000


def _ref_bookmark_name(kind: str, num: str) -> str:
    """编号 → 书签名（OOXML 书签名不含 ``-``，归一为下划线；NFKC 兼容全角数字）。"""
    import unicodedata
    num_ascii = unicodedata.normalize("NFKC", num)
    return f"{_REF_BM_PREFIX}{'fig' if kind == '图' else 'tbl'}_{num_ascii.replace('-', '_')}"


def _add_caption_text_with_ref_bookmark(paragraph, caption_text: str,
                                        font_name: str, font_size: float):
    """题注写入：编号前缀（加书签）+ 其余文本。

    书签只包「图 X-Y / 表 X-Y」本身（REF 域引用的是**编号**而非整条题注，
    否则正文「如图 3-1 所示」更新域后会变成「如图 3-1 标题文字所示」）。
    视觉与旧实现逐字节一致（书签为不可见元素）。若题注被外层 rollback 删除，
    登记残留由回填 pass 的「书签存在性校验」兜住（不指错号）。
    无编号前缀的题注（历史形态）保持整串单 run，行为逐字不变。
    """
    m = _CAPTION_LEADING_NUM_RE.match(caption_text)
    if not m:
        _set_run_font(paragraph.add_run(caption_text), font_name, font_size, bold=True)
        return
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    num_run = paragraph.add_run(m.group(0))
    _set_run_font(num_run, font_name, font_size, bold=True)
    st = _refx_state()
    bid = str(st["next_id"])
    st["next_id"] += 1
    bm_start = OxmlElement("w:bookmarkStart")
    bm_start.set(qn("w:id"), bid)
    bm_start.set(qn("w:name"), _ref_bookmark_name(m.group("kind"), m.group("num")))
    bm_end = OxmlElement("w:bookmarkEnd")
    bm_end.set(qn("w:id"), bid)
    num_run._r.addprevious(bm_start)
    num_run._r.addnext(bm_end)
    rest = caption_text[m.end():]
    if rest:
        _set_run_font(paragraph.add_run(rest), font_name, font_size, bold=True)


def _backfill_cross_reference_fields(doc) -> int:
    """渲染完成后，把正文对已登记图号/表号的纯文本引用升级为 REF 域。

    三重保守判据（防「坏域比没有域更糟」——指向不存在书签的 REF 会让
    Word/WPS 打开时报「引用源不存在」错误）：
    ① 只对**真实存在的书签**包域 —— 题注插入成功才有书签；渲染失败被回滚的
       图号（含题注段被回滚的登记残留）无书签，正文引用保持纯文本；
    ② 跳过题注自身段落（含 ``_Ref_`` 书签的段）与已含域的段落（防域嵌套）；
    ③ 匹配必须完整落在**单个 run** 的文本内（跨 run 边界跳过，保持纯文本）。
    可见文本逐字节不变：域结果 run 写**原匹配文本**（含原文空格形态），
    ``updateFields=true`` 保证 Word/WPS 打开即刷新。
    fail-soft：任何异常只 WARNING，绝不阻断导出交付。
    """
    import copy as _copy

    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    try:
        existing = set()
        for bm in doc.element.body.iter(qn("w:bookmarkStart")):
            nm = bm.get(qn("w:name")) or ""
            if nm.startswith(_REF_BM_PREFIX):
                existing.add(nm)
        if not existing:
            return 0
        inserted = 0

        def _mk_text_run(rpr, s: str):
            nr = OxmlElement("w:r")
            if rpr is not None:
                nr.append(_copy.deepcopy(rpr))
            t = OxmlElement("w:t")
            t.set(qn("xml:space"), "preserve")
            t.text = s
            nr.append(t)
            return nr

        def _fld_run(rpr, kind: str):
            nr = OxmlElement("w:r")
            if rpr is not None:
                nr.append(_copy.deepcopy(rpr))
            fc = OxmlElement("w:fldChar")
            fc.set(qn("w:fldCharType"), kind)
            nr.append(fc)
            return nr

        for p in list(doc.element.body.iter(qn("w:p"))):
            # ② 题注自身段（含 _Ref_ 书签）不得再包域；已含域的段防嵌套
            if any((bs.get(qn("w:name")) or "").startswith(_REF_BM_PREFIX)
                   for bs in p.findall(qn("w:bookmarkStart"))):
                continue
            if p.findall(".//" + qn("w:fldChar")):
                continue
            for r in list(p.findall(qn("w:r"))):
                # 只处理纯文本 run（含域字符 / instrText / drawing 的跳过）
                if (r.find(qn("w:fldChar")) is not None
                        or r.find(qn("w:instrText")) is not None
                        or r.find(qn("w:drawing")) is not None):
                    continue
                t_els = r.findall(qn("w:t"))
                if len(t_els) != 1:
                    continue  # ③ 复杂 run（多 w:t 等）保持纯文本
                text = t_els[0].text or ""
                if not text:
                    continue
                segs = []  # ("t", 文本) | ("f", (书签名, 可见文本))
                pos = 0
                for m in _REF_NUM_RE.finditer(text):
                    name = _ref_bookmark_name(m.group("kind"), m.group("num"))
                    if name not in existing:
                        continue  # ① 未登记 / 已回滚 → 保持纯文本
                    if m.start() > pos:
                        segs.append(("t", text[pos:m.start()]))
                    segs.append(("f", (name, m.group(0))))
                    pos = m.end()
                if not segs:
                    continue
                if pos < len(text):
                    segs.append(("t", text[pos:]))
                rpr = r.find(qn("w:rPr"))
                for kind_seg, payload in segs:
                    if kind_seg == "t":
                        r.addprevious(_mk_text_run(rpr, payload))
                    else:
                        name, visible = payload
                        r.addprevious(_fld_run(rpr, "begin"))
                        ir = OxmlElement("w:r")
                        if rpr is not None:
                            ir.append(_copy.deepcopy(rpr))
                        it = OxmlElement("w:instrText")
                        it.set(qn("xml:space"), "preserve")
                        it.text = f" REF {name} \\h "
                        ir.append(it)
                        r.addprevious(ir)
                        r.addprevious(_fld_run(rpr, "separate"))
                        # 域结果 = 原匹配文本 ⇒ 可见文本逐字节不变
                        r.addprevious(_mk_text_run(rpr, visible))
                        r.addprevious(_fld_run(rpr, "end"))
                        inserted += 1
                p.remove(r)
        if inserted:
            _fix_stats()["cross_ref_fields"] += inserted
            logger.info(
                "交叉引用：正文 %d 处图/表号引用已升级为可点击 REF 域"
                "（Word/WPS 更新域后随编号刷新）", inserted)
        return inserted
    except Exception as e:
        logger.warning("交叉引用 REF 域回填失败（成稿保持纯文本引用）: %s", e)
        return 0


# ---------------------------------------------------------------------------
# ✅ 版式增强：XML 级排版能力（底纹 / 边框 / 代码块 / 域自动刷新 / 文件名）
# ---------------------------------------------------------------------------
_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

# CT_PPr 为严格序列，w:pBdr / w:shd 必须排在下列元素之前
_PPR_AFTER_PBDR = (
    "w:shd", "w:tabs", "w:suppressAutoHyphens", "w:kinsoku", "w:wordWrap",
    "w:overflowPunct", "w:topLinePunct", "w:autoSpaceDE", "w:autoSpaceDN",
    "w:bidi", "w:adjustRightInd", "w:snapToGrid", "w:spacing", "w:ind",
    "w:contextualSpacing", "w:mirrorIndents", "w:suppressOverlap", "w:jc",
    "w:textDirection", "w:textAlignment", "w:textboxTightWrap", "w:outlineLvl",
    "w:divId", "w:cnfStyle", "w:rPr", "w:sectPr", "w:pPrChange",
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
    "w:noWrap", "w:tcMar", "w:textDirection", "w:tcFitText",
    "w:vAlign", "w:hideMark",
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


# CT_TrPr 为严格序列，w:cantSplit 必须排在下列元素之前
_TRPR_AFTER_CANT_SPLIT = (
    "w:trHeight", "w:tblHeader", "w:tblCellSpacing", "w:jc", "w:hidden",
)


def _set_row_cant_split(row) -> None:
    """禁止表格行跨页断开（``w:cantSplit``）。

    ✅ R117 FIX-8（2026-10-20）：实测导出稿 32 张表 / 195 行**无一行**带
    ``cantSplit`` —— 长表翻页时同一行会被从中间劈开（上半行留在本页底部、
    下半行跑到下一页顶部），行内文字与框线错位。表头行同样要加：表头跨页
    重复时若自身被劈开，重复出来的只是半行。

    ✅ R109 同型教训：手写 XML 必须按 CT_TrPr 顺序插入（``trPr`` 里
    ``cantSplit`` 在 ``trHeight``/``tblHeader`` 之前），否则 WPS 拒绝打开。
    落盘前 ``_normalize_docx_strict_sequence`` 仍会兜底重排。
    """
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    tr_pr = row._tr.get_or_add_trPr()
    if tr_pr.find(qn("w:cantSplit")) is not None:
        return  # 幂等：重复调用不会堆出多个 w:cantSplit（CT_TrPr 只允许 0~1 个）
    cant_split = OxmlElement("w:cantSplit")
    tr_pr.insert_element_before(cant_split, *_TRPR_AFTER_CANT_SPLIT)


# ECMA-376 中具有**严格子元素序列**、且导出侧会手写的属性容器。
# 顺序表取自 CT_PPr / CT_RPr / CT_TblPr / CT_TcPr / CT_TrPr / CT_SectPr。
_STRICT_CT_SEQUENCE: dict[str, tuple[str, ...]] = {
    "pPr": (
        "pStyle", "keepNext", "keepLines", "pageBreakBefore", "framePr",
        "widowControl", "numPr", "suppressLineNumbers", "pBdr", "shd", "tabs",
        "suppressAutoHyphens", "kinsoku", "wordWrap", "overflowPunct",
        "topLinePunct", "autoSpaceDE", "autoSpaceDN", "bidi", "adjustRightInd",
        "snapToGrid", "spacing", "ind", "contextualSpacing", "mirrorIndents",
        "suppressOverlap", "jc", "textDirection", "textAlignment",
        "textboxTightWrap", "outlineLvl", "divId", "cnfStyle", "rPr", "sectPr",
        "pPrChange",
    ),
    "rPr": (
        "rStyle", "rFonts", "b", "bCs", "i", "iCs", "caps", "smallCaps", "strike",
        "dstrike", "outline", "shadow", "emboss", "imprint", "noProof",
        "snapToGrid", "vanish", "webHidden", "color", "spacing", "w", "kern",
        "position", "sz", "szCs", "highlight", "u", "effect", "bdr", "shd",
        "fitText", "vertAlign", "rtl", "cs", "em", "lang", "eastAsianLayout",
        "specVanish", "oMath",
    ),
    "tblPr": (
        "tblStyle", "tblpPr", "tblOverlap", "bidiVisual", "tblStyleRowBandSize",
        "tblStyleColBandSize", "tblW", "jc", "tblCellSpacing", "tblInd",
        "tblBorders", "shd", "tblLayout", "tblCellMar", "tblLook", "tblCaption",
        "tblDescription",
    ),
    "tcPr": (
        "cnfStyle", "tcW", "gridSpan", "hMerge", "vMerge", "tcBorders", "shd",
        "noWrap", "tcMar", "textDirection", "tcFitText", "vAlign", "hideMark",
    ),
    "trPr": (
        "cnfStyle", "divId", "gridBefore", "gridAfter", "wBefore", "wAfter",
        "cantSplit", "trHeight", "tblHeader", "tblCellSpacing", "jc", "hidden",
    ),
    "sectPr": (
        "footnotePr", "endnotePr", "type", "pgSz", "pgMar", "paperSrc",
        "pgBorders", "lnNumType", "pgNumType", "cols", "formProt", "vAlign",
        "noEndnote", "titlePg", "textDirection", "bidirectional", "rtlGutter",
        "docGrid", "printerSettings", "sectPrChange",
    ),
}


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
            "WPS 会拒绝打开）—— 成稿内容一字不变，仅调整属性元素书写顺序", total)
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
        prefix = "".join("\u00a0\u00a0\u00a0\u00a0" if ch == "\t" else "\u00a0"
                         for ch in line[:lead])
        run.add_text(prefix + line[lead:])
    _set_paragraph_shading(p, "F2F2F2")
    _set_paragraph_borders(
        p, top=("single", 4, "D9D9D9"), left=("single", 4, "D9D9D9"),
        bottom=("single", 4, "D9D9D9"), right=("single", 4, "D9D9D9"))
    return p


#: Word 2010+ 扩展命名空间（``<w14:textFill>`` 主题色填充就在其中）。
_W14_NS = "http://schemas.microsoft.com/office/word/2010/wordml"


def _strip_heading_theme_fill(style) -> int:
    """删除标题样式 rPr 里的 ``<w14:textFill>``，返回删除个数（R117 · 2026-10-20）。

    **为什么只设 ``font.color.rgb`` 还不够**：python-docx 自带模板的
    ``Heading 2/3/4/7`` 样式带 ``<w14:textFill><w14:solidFill><w14:schemeClr
    w14:val="accent1"/>`` —— 按 OOXML 扩展规则，``w14:textFill`` 的取值
    **优先于** ``<w:color>``，于是成稿标题在 Word 2010+ 里仍是模板主题蓝
    （实测导出稿 H1=#376092、H2~H4=#4F81BD），而导出配置**没有颜色开关**，
    用户无从修改。工程报审件按惯例为黑色，故这里显式删除。
    """
    try:
        rpr = style.element.rPr
    except Exception:  # pragma: no cover - 极旧 python-docx 无 rPr 访问器
        return 0
    if rpr is None:
        return 0
    removed = 0
    for el in list(rpr):
        if el.tag == f"{{{_W14_NS}}}textFill":
            rpr.remove(el)
            removed += 1
    return removed


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
    for tag in ("w:hdrShapeDefaults", "w:compat", "w:rsids", "w:shapeDefaults",
                "w:decimalSymbol", "w:listSeparator"):
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
    （``filename="{方案名}"``），而 HTTP 头只能承载 latin-1 —— 中文方案名
    会在编码响应头时抛 UnicodeEncodeError。DOCX 主链路走 FileResponse，
    由 Starlette 自行编码；本函数保留给需要显式响应头的下载/回退链路。
    """
    from urllib.parse import quote
    safe = _safe_filename(filename)
    ascii_fallback = re.sub(r"[^\x20-\x7e]", "_", safe) or "export"
    return (f'attachment; filename="{ascii_fallback}"; '
            f"filename*=UTF-8''{quote(safe, safe='')}")


# ---------------------------------------------------------------------------
# 导出文件名规则：专项方案名称 + 导出日期 + 导出轮次
#   例：某工程基坑支护专项方案_20260916_第3轮.docx
# ---------------------------------------------------------------------------
def _build_export_filename(scheme_name: str, ext: str, round_no: int,
                           when: "datetime | None" = None) -> str:
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
    "招投标项目", "投标项目", "投标标书", "招标文件", "标书编制",
    "项目名称", "工程名称", "项目名", "工程名",
    "未命名项目", "新建项目", "测试项目", "示例项目", "模板项目",
    "未命名", "无标题", "未填写", "待填写", "请填写", "占位", "待定", "暂定",
    # ⚠️ 刻意不含 `示例`/`模板`/`项目`/`工程` 这四个泛词：只由它们拼成的名字
    #    （如「示例工程」）可能是真实落库工程名，整串置空会静默吃掉用户数据 ——
    #    残留串要被判掉，靠「招投标/投标/招标」前缀 + 日期时间片段即可。
)

#: 「没有任何语义」的片段：日期 / 时间 / 年份 / 括号提示 / 占位下划线。
#: 残留串通常 = 占位关键词 + 这些片段（如「招投标项目 2026/10/7 19:56:43」）。
_TEMPLATE_RESIDUE_FRAG_RE = re.compile(
    r"\d{4}\s*[-/年.]\s*\d{1,2}\s*[-/月.]\s*\d{1,2}\s*日?"   # 2026/10/7、2026-10-7
    r"|\d{4}\s*[-/年.]\s*\d{1,2}\s*[-/月.]"                  # 2026/10
    r"|\d{1,2}:\d{2}(?::\d{2})?"                             # 19:56:43
    r"|\d{4}"                                                # 单年份
    r"|【[^】]{0,16}】|\[[^\]]{0,16}\]|（[^）]{0,16}）|\([^)]{0,16}\)"
    r"|[_＿]{2,}|X{2,}|x{2,}|×{2,}|＊{2,}"                   # ___、XX、××
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
        val = _sanitize_template_residue_title(
            item.get("content") or item.get("value") or "")
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

    语义：**每次导出请求计一轮**（DOCX 单链路，R126 起 PDF 导出已下线）
    —— 保证同一天多次导出的文件名互不重复，不会出现
    浏览器/系统自动追加的 "xxx(1).docx" 之类的重名副本。
    失败时回退为第 1 轮，绝不阻塞导出主流程。
    """
    try:
        await _db_exec(db,
            "UPDATE schemes SET export_round = COALESCE(export_round, 0) + 1 WHERE id=?",
            (scheme_id,), what="_bump_export_round")
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
            pass
    return 1


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
        "X-Global-Facts-Status": quote(
            json.dumps(status, ensure_ascii=False, separators=(",", ":")),
            safe=""),
    }


def _export_audit_headers(audit: dict) -> dict:
    """把导出前内容体检结果回传前端（占位符 / HTML 残留 / 投标用语 / 未闭合围栏）。

    响应头 Value 必须是 latin-1 安全字节，JSON 里含中文会直接抛
    ``UnicodeEncodeError``（与 X-Export-Filename 同一原因）→ 百分号编码。
    无风险时不发头，避免前端统计条出现无意义的空提示。

    ✅ R133-C：``audit["mermaid_service"]``（Mermaid 服务健康结论）在
    ``items`` 为空时也要回传 —— 否则「服务可用/不可用」的确定性结论在
    零风险文档上凭空消失。此时只回传 mermaid_service 一键（不发空 items）。
    """
    if not audit:
        return {}
    from urllib.parse import quote

    if audit.get("items"):
        return {"X-Content-Audit": quote(json.dumps(audit, ensure_ascii=False), safe="")}
    if "mermaid_service" in audit:
        return {"X-Content-Audit": quote(json.dumps(
            {"mermaid_service": audit["mermaid_service"]},
            ensure_ascii=False), safe="")}
    return {}


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
    #    由 export_docx 登记并经 _build_docx_task 末尾透传）。
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
    # ✅ R117 FIX-5（2026-10-20）：显式 A4 页面。
    #    python-docx 的默认模板是 **US Letter**（8.5×11in = 21.59×27.94cm），
    #    旧实现从未设置 page_width/page_height —— 实测导出稿
    #    `室外汽车吊吊装施工专项方案_20261020_第1轮.docx` 的 sectPr 即 Letter。
    #    中国工程文件（含专项施工方案报审件）统一 A4 21×29.7cm，
    #    且 2.5cm 左右边距下 A4 的正文栏宽正好 16cm，与
    #    `_CHART_MAX_WIDTH_CM` / `_assign_table_column_widths` 的 16cm 口径一致。
    _page = doc.sections[0]
    _page.page_width = Cm(21.0)
    _page.page_height = Cm(29.7)
    style = doc.styles["Normal"]
    # ✅ R130（文字标准）：西文统一 Times New Roman（w:ascii/w:hAnsi），
    #    中文由 w:eastAsia 接管（宋体/仿宋，随导出配置）——旧实现把
    #    font_name 同时写进西文槽，成稿里的数字/英文随正文中文字体走。
    style.font.name = FONT_LATIN
    style.font.size = Pt(font_size)
    style.element.rPr.rFonts.set(qn("w:eastAsia"), font_name)
    # ✅ P0 修复（2026-09-27）：同标题样式，删除 Normal 上残留的主题字体属性，
    #    否则 w:asciiTheme 会反向覆盖上面 style.font.name 写入的显式字体。
    for _theme_attr in ("w:asciiTheme", "w:hAnsiTheme",
                        "w:eastAsiaTheme", "w:cstheme"):
        if style.element.rPr.rFonts.get(qn(_theme_attr)) is not None:
            del style.element.rPr.rFonts.attrib[qn(_theme_attr)]
    # ✅ 增强：正文行距与段后间距（旧实现完全未设置，长文档通读体验差）
    style.paragraph_format.line_spacing = line_spacing
    style.paragraph_format.space_after = Pt(0)

    for lvl in range(1, 10):
        hs = heading_styles[lvl]
        h_style = doc.styles[f"Heading {lvl}"]
        # ✅ R130（文字标准）：标题西文 Times New Roman，中文黑体（eastAsia，
        #    见下方 rFonts.set）——旧实现 font.name = 中文字体把西文槽也占了。
        h_style.font.name = FONT_LATIN
        h_style.font.size = Pt(hs["font_size"])
        h_style.font.bold = hs["bold"]
        h_style.font.italic = False
        # ✅ R117 FIX-6（2026-10-20）：标题强制黑色 + 删模板主题填充。
        #    旧实现只写 name/size/bold，从不写 w:color —— 成稿继承 python-docx
        #    模板的 Heading 主题蓝（实测导出稿 H1=#376092、H2~H4=#4F81BD），且带
        #    <w14:textFill> 主题色填充（Word 2010+ 优先于 w:color）；导出配置里
        #    没有颜色项，用户改不掉。工程报审件按惯例为黑色。
        h_style.font.color.rgb = RGBColor(0, 0, 0)
        _strip_heading_theme_fill(h_style)
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
        for _theme_attr in ("w:asciiTheme", "w:hAnsiTheme",
                            "w:eastAsiaTheme", "w:cstheme"):
            if _rfonts.get(qn(_theme_attr)) is not None:
                del _rfonts.attrib[qn(_theme_attr)]
        # ✅ 增强：标题与后文同页（避免标题孤行落在页尾），并留段前段后间距
        hpf = h_style.paragraph_format
        hpf.keep_with_next = True
        # ✅ R117 FIX-6（2026-10-20）：段前间距也做成层级阶梯。
        #    旧实现只有 12/8 两档，而 1~4 级默认字号相同（14pt），四级标题
        #    仅靠 4pt 段前差区分 —— 成稿里 H2/H3/H4 视觉上几乎无法分辨。
        hpf.space_before = Pt({1: 18, 2: 14, 3: 12, 4: 10}.get(lvl, 8))
        hpf.space_after = Pt(6)
        # ✅ 默认：所有标题行距与正文一致（1.15 倍）
        hpf.line_spacing = line_spacing
        # ✅ R129：L7~L9 显式写入 outlineLvl（Word 大纲级别 6/7/8）——
        #    python-docx 模板的 Heading 7~9 虽自带大纲级别，但显式重写
        #    保证任何模板状态下「深层标题可被 TOC \o "1-9" 域收录、
        #    Word 导航窗格层级正确」。幂等：先删后写。
        if lvl >= 7:
            _ol_el = h_style.element.get_or_add_pPr().get_or_add_outlineLvl()
            _ol_el.set(qn("w:val"), str(lvl - 1))

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
        _set_run_font(title_p.add_run(_display_scheme_name(scheme, global_facts)),
                      font_name, 28, bold=True)
        if bidder_name:
            bidder_p = doc.add_paragraph()
            bidder_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            _set_run_font(bidder_p.add_run(bidder_name), font_name, 16)
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
        _set_run_font(hp.add_run(page_header), font_name, 9)
        _set_paragraph_borders(hp, bottom=("single", 6, "AAAAAA"))

    # 页脚
    if page_footer or show_page_number:
        fp = sec.footer.paragraphs[0]
        fp.alignment = WD_ALIGN_PARAGRAPH.CENTER
        if page_footer:
            _set_run_font(fp.add_run(page_footer), font_name, 9)
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
    order_rank: dict[str, int] = {}
    # ✅ 加固（D1）：本次构建期间「跨章借图」的 (章节, 类型, 借自) 去重集合
    _BORROWED_CHARTS: set[tuple] = set()

    def _rank_order(secs: list):
        for _s in sorted(secs, key=_section_sort_key):
            if _s["id"] not in order_rank:
                order_rank[_s["id"]] = len(order_rank)
            _rank_order(children_map.get(_s["id"], []))

    _rank_order(roots)
    # P0-3 性能优化：倒排索引一次构建，write_section 兜底查找从 O(N) 降为 O(1)
    chart_type_index = _build_chart_type_index(chart_lookup, order_rank)
    # 正文首个标题之前不允许分页，否则封面/目录之后会多出一张空白页
    body_state = {"started": False}
    # 中文正文首行缩进 2 字符（随正文字号缩放，旧实现固定 0.74cm）
    body_first_line_indent = Cm(round(font_size * 2 * 0.0352778, 3))
    # ✅ R127：列表编号定义（numbering.xml）在本构建内共享，跨章节复用
    list_numbering = _DocxListNumbering(doc)

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

        # ✅ R151 引入 / R162 语义不变：仅识别 L1~L4 级标题（三级叶子 + 四级兼容层）。
        #    L5+ 的 DB 章节不再产出 Heading 样式 —— Word 导航窗格 / TOC 只会
        #    出现 1~4 级；标题文本与编号保留、降级为加粗正文段落，正文内容
        #    照常输出（数据不丢，只是不再进入文档大纲）。落库侧
        #    normalize_outline 已把 >4 级裁剪进父级 description，此分支是
        #    防御性兜底（历史库 / 直写 DB 的脏数据）。
        if level <= 4:
            # ✅ R129：标题样式层级上限 7→8（DB 大纲共 8 级；L8 旧行为=借用
            #    Heading 7 外观，现归位 Heading 8，外观同参数、outlineLvl 归位 7）
            #    （R151 起实际封顶 Heading 4，历史注释保留供追溯）
            h = doc.add_paragraph(style=f"Heading {min(level, 8)}")
            _add_runs_with_inline_format(h, full_title)
            _finalize_heading_runs(h)
        else:
            h = doc.add_paragraph()
            _degraded = h.add_run(full_title)
            _degraded.bold = True
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
            # ✅ R117 FIX-10（2026-10-20）：折叠**同章内逐字重复**的正文块。
            #    判重刻意只在**单章作用域**内做（seen 随每次调用重置）——
            #    跨章复用同一条标准条文是正常写法，跨章去重会误删正文。
            blocks = _collapse_repeated_blocks(blocks, where=full_title or title)
            # ✅ R117 FIX-11（2026-10-20）：「前缀相同、后文分歧」的近似重复**只报警
            #    不改写**（两侧各有独有尾句，删任一侧即丢正文）——纯可观测，
            #    blocks 一个字节未动。
            _report_near_duplicate_blocks(blocks, where=full_title or title)
            # ✅ E3（2026-09-25）：有 DB 子章节 + 配置开启 → 正文子标题降级为节内
            # body 命名空间（1）/ a、…），与 DB 子章节彻底隔离。无子女或配置关
            # 闭 → 保持旧点分编号，由后续计数器前移（L2991+）处理撞号。
            try:
                from app.config import settings as _s
                _demote = _s.body_subheading_demote_with_children
            except Exception:
                _demote = True
            has_children_demoted = bool(children_map.get(sec_id)) and _demote
            # ✅ R138（L-3 收口）：循环体与参数拼装收口到 number_body_subheadings
            #    （with_style=True 是写稿侧独有的需求，其余两处不写该键）。
            # ✅ R149（2026-10-31）：块选择同源 —— 与落库侧一致排除围栏派生
            #    标题（D-8g 契约），围栏标题成稿保留原文不占号，预览=落库=成稿。
            number_body_subheadings(
                select_numbering_blocks(blocks),
                section_prefix=section_prefix, section_level=level,
                sec_id=sec_id, has_children=has_children_demoted,
                with_style=True)

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
            ordered_stack: list[list] = []   # [[marker, seq], ...]，下标 = indent_lvl
            # ✅ R127：当前 Word 列表实例（numId + 标记签名）；结构边界重置
            list_ctx: dict | None = None
            for block in blocks:
                if _breaks_ordered_sequence(block):
                    # ✅ R117（2026-10-20）：只在**结构边界**（标题 / 冒号引导句）
                    #    重置。旧的「遇任何非列表块一律清零」会让"1、A / 正文 /
                    #    1、B"这类被正文与表格打断的同一组列举全部渲染成 ``1、``。
                    ordered_stack = []
                    list_ctx = None
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
                                doc, f"表 {table_num} {_strip_leading_table_number(caption)}",
                                font_name, font_size)
                        else:
                            # ✅ R51（2026-10-07）：表格无表名行 → 成稿表格裸奔无表题。
                            #    R50 仅补 WARNING 供排障，但用户成稿里 38 张表零编号零表题。
                            #    现从表格首行（GFM header）自动生成表题：取前 3 列名拼接，
                            #    确保每张表都有编号与标题。计数器始终递增（修复编号断裂）。
                            _tbl_lines = block.get("lines") or []
                            _first = (_tbl_lines[0] if _tbl_lines else "").strip()
                            _auto_caption = ""
                            if _first and "|" in _first and not re.match(r'^[\|\s\-:]+$', _first):
                                cells = [c.strip() for c in _first.split("|") if c.strip()]
                                # ✅ R117：剥公式 / 丢无语义列名（序号、备注…）/ 去重
                                _auto_caption = _sanitize_auto_caption(cells[:4])
                            if not _auto_caption:
                                _auto_caption = "相关数据"
                            # ✅ 2026-10-17（表号叠写根因 B）：AI 把表题连同编号写进首格
                            #    （首格「表 6-1 …」）→ 拼出的自动表题带编号 → 与导出编号叠写。
                            #    此处剥离首格自带的「表 X-Y」，只保留统一编号。
                            _auto_caption = _strip_leading_table_number(_auto_caption)
                            _add_table_caption(doc, f"表 {table_num} {_auto_caption}",
                                               font_name, font_size)
                            logger.warning(
                                "导出：章节 %s 表格无表名行，自动生成表题: 表 %s %s（首行: %s）",
                                str(sec_id)[:8], table_num, _auto_caption,
                                _first[:80])
                        _add_table_from_markup(doc, block["lines"], font_name)
                    elif block["type"] == "chart":
                        chart_type = block["chart_type"]
                        # ✅ 加固（2026-10-06 · D1）：取码收敛到唯一出口
                        #    _resolve_chart_code，并回传「是否跨章借图 + 借自谁」。
                        mermaid_code, borrowed_from = _resolve_chart_code(
                            block, sec_id, chart_type, chart_lookup,
                            chart_type_index, order_rank)
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
                                sec_id[:8], chart_type, borrowed_from[:8])
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
                        # ✅ R131-A（2026-10-23）：生成期残留的「图 X-Y」前缀在此剥离 ——
                        #    下方 fig_num 由**当前章节树**现算，残留前缀会与新号叠写成
                        #    「图 2-1 图 4-1 …」（章节移动后携带过期章号 = 归属漂移 + 重号）。
                        fig_title = _strip_leading_figure_number(
                            (block.get("title") or "").strip() or default_title)
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
                                sec_id[:8], chart_type)
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
                            doc, chart_type, _chart_bytes,
                            fig_num, fig_title, font_name, font_size,
                            placeholder=chart_fail_placeholder)
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
                            sec_id[:8], block.get("title") or "未命名")
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
                                "章节 %s 存在无位图的 AI 配图引用（%s），本次导出已跳过"
                                "且不占用图号（避免图号虚跳）",
                                sec_id[:8], (block.get("alt") or "").strip() or "未命名")
                            _pop_orphan_lead_in(doc)
                            continue
                        chapter_num = _figure_chapter_num(heading_gen)
                        fig_key = f"ch{chapter_num}"
                        figure_counters[fig_key] = figure_counters.get(fig_key, 0) + 1
                        # ✅ 图号虚跳收口（2026-10-03）：插入成功才保留图号；
                        #    插入失败（格式合法但文件损坏）→ 回退图号 + 回收孤儿引导语。
                        _ok = _add_illustration_from_bytes(
                            doc, _img_bytes,
                            f"{chapter_num}-{figure_counters[fig_key]}",
                            # ✅ R131-A（2026-10-23）：alt 同样剥离生成期「图 X-Y」前缀
                            #    （与图表分支同口径，避免 AI 配图题注双图号）。
                            _strip_leading_figure_number(
                                (block.get("alt") or "").strip()),
                            font_name, font_size)
                        if not _ok:
                            figure_counters[fig_key] = figure_counters.get(fig_key, 0) - 1
                            _pop_orphan_lead_in(doc)
                    elif block["type"] == "heading":
                        # ✅ R129：正文子标题样式上限 7→9（L8/L9 走 Heading 8/9，
                        #    显式 outlineLvl 7/8，可被 TOC \o "1-9" 收录）
                        # ✅ R151 引入 / R162 语义不变：文档大纲只允许 L1~L4 ——
                        #    超出 4 级的正文子标题（如 L3 叶子内的更深小节、
                        #    L4 兼容叶子内的「1）」小节）
                        #    不再使用 Heading 样式，降级为加粗正文段落（编号文本
                        #    保留），Word 导航窗格 / TOC 因此恰好只有四级。
                        _want_lv = min(block.get("_heading_style", min(level + block.get("level", 1), 9)), 9)
                        if _want_lv <= 4:
                            h = doc.add_paragraph(style=f"Heading {_want_lv}")
                            _add_runs_with_inline_format(h, block.get("_fixed_text", block.get("text", "")))
                            _finalize_heading_runs(h)
                        else:
                            h = doc.add_paragraph()
                            _sub = h.add_run(block.get("_fixed_text", block.get("text", "")))
                            _sub.bold = True
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
                                ordered_stack = ordered_stack[:indent_lvl + 1]
                            while len(ordered_stack) <= indent_lvl:
                                ordered_stack.append([marker, 0])
                            entry = ordered_stack[indent_lvl]
                            if entry[0] != marker:
                                entry[0] = marker
                                entry[1] = 0
                            entry[1] += 1
                            # 仅用于悬挂缩进宽度估算；前缀文本不再写入正文
                            prefix = _ordered_prefix(entry[1], marker)
                            signature = tuple(e[0] for e in ordered_stack)
                            if list_ctx is None or list_ctx["signature"] != signature:
                                num_id = list_numbering.new_num(
                                    signature, [e[1] for e in ordered_stack])
                                list_ctx = {"signature": signature,
                                            "num_id": num_id}
                            else:
                                num_id = list_ctx["num_id"]
                        else:
                            prefix = "• "
                            num_id = list_numbering.bullet_num()
                        # ✅ 使用 "List Paragraph" 样式（Word 识别为列表项）
                        _lp_style = _ensure_list_paragraph_style(doc)
                        p = doc.add_paragraph(style=_lp_style)
                        _add_runs_with_inline_format(p, block["text"])
                        # ✅ R127：前缀交由 numbering.xml 渲染（Word 原生列表）
                        list_numbering.apply(p, num_id, indent_lvl)
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
                        # ✅ R127：正文改用 Body Text CN 样式，不再逐段写直接格式
                        p = doc.add_paragraph(
                            style=_ensure_body_text_style(
                                doc, body_first_line_indent))
                        _add_runs_with_inline_format(p, block.get("text", ""))
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
                    _idx = min(level, 7)   # 子章节层级 = level + 1（0 基索引恰为 level）
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
            logger.error("导出：章节「%s」渲染失败（已跳过该章，其余内容不受影响）: %s",
                         str(sec.get("title") or "")[:40], e, exc_info=True)
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
                gh = doc.add_paragraph(gt, style=apx_style)
                _set_run_font(gh.runs[0] if gh.runs else gh.add_run(gt),
                              font_name, heading_styles[2]["font_size"], bold=True)
                tbl = doc.add_table(rows=1, cols=2)
                try:
                    tbl.style = "Table Grid"
                except Exception:
                    pass
                hc = tbl.rows[0].cells
                _set_run_font(hc[0].paragraphs[0].add_run("名称"),
                              font_name, 10.5, bold=True)
                _set_run_font(hc[1].paragraphs[0].add_run("取值 / 内容"),
                              font_name, 10.5, bold=True)
                for f in items:
                    rc = tbl.add_row().cells
                    _set_run_font(rc[0].paragraphs[0].add_run(str(f.get("title") or "")),
                                  font_name, 10.5)
                    _appendix_cell_paragraph(rc[1], str(f.get("content") or ""),
                                             font_name, first=True)
                _finalize_simple_table(tbl)
        except TypeError as e:
            # ✅ P0 修复（2026-09-27）：TypeError 是**编程错误**（如把 bold 当位置参数
            #    传给 keyword-only 形参），不是"附录渲染不出来"的可恢复异常。
            #    旧实现把它和真正的运行期波动一起降级为 WARNING，于是整张
            #    「项目关键事实」附录静默消失、导出却报"成功"——最隐蔽的一类缺陷。
            #    现按 ERROR 上报，保留原降级（不阻断正文导出）。
            logger.error(
                "导出：渲染全局事实附录遇到编程错误（附录将缺失，"
                "正文不受影响）: %s", e, exc_info=True)
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
                gh = doc.add_paragraph(gtitle, style=apx_style)
                _set_run_font(gh.runs[0] if gh.runs else gh.add_run(gtitle),
                              font_name, heading_styles[2]["font_size"], bold=True)
                tbl = doc.add_table(rows=1, cols=2)
                try:
                    tbl.style = "Table Grid"
                except Exception:
                    pass
                hc = tbl.rows[0].cells
                _set_run_font(hc[0].paragraphs[0].add_run("名称"),
                              font_name, 10.5, bold=True)
                _set_run_font(hc[1].paragraphs[0].add_run("内容 / 要点"),
                              font_name, 10.5, bold=True)
                for it in items:
                    rc = tbl.add_row().cells
                    _set_run_font(rc[0].paragraphs[0].add_run(str(it.get("name") or "")),
                                  font_name, 10.5)
                    _appendix_cell_paragraph(rc[1], str(it.get("value") or ""),
                                             font_name, first=True)
                _finalize_simple_table(tbl)
        except TypeError as e:
            # 与事实附录同口径：TypeError 是编程错误，按 ERROR 上报（否则附录
            # 静默消失而导出报成功，是最隐蔽的一类缺陷）。
            logger.error(
                "导出：渲染补充附录遇到编程错误（附录将缺失，正文不受影响）: %s",
                e, exc_info=True)
        except Exception as e:
            logger.warning("导出：渲染补充附录失败（降级跳过，不影响正文）: %s", e)

    # ✅ R128（遗留 3 收口）：交叉引用 REF 域回填 —— 必须在**全部题注书签就位、
    #    渲染成败尘埃落定之后**（此刻回填绝不指错号），且在统计读取（下方
    #    _log_fix_stats 读取并清零）与严格序列归一之前，否则 cross_ref_fields
    #    计数进不了 X-Fix-Stats 响应头（本轮首跑护栏当场抓到的接线顺序缺陷）。
    _backfill_cross_reference_fields(doc)
    # ✅ R129：导出后校验 · 段落级残留扫描（内存 doc，save 前执行 ⇒ 计数
    #    进线程局部统计、随下方 _log_fix_stats 输出日志与 X-Fix-Stats）。
    #    判据与导出前体检同源（懒导入 _AUDIT_RULES），只体检不篡改；
    #    大纲映射（Heading 1~9 → 层级+标题）随返回值透出，供目录导航核对。
    _postcheck: dict | None = None
    try:
        from app.services.export_validate import validate_document
        _postcheck = validate_document(doc)
        _fs_local = _fix_stats()
        _fs_local["postcheck_markdown_residue"] += _postcheck["markdown_residue"]
        _fs_local["postcheck_html_residue"] += _postcheck["html_residue"]
        _fs_local["postcheck_entity_residue"] += _postcheck["entity_residue"]
        _fs_local["postcheck_placeholder_residue"] += _postcheck["placeholder_residue"]
    except Exception:
        logger.warning("导出后校验（段落级）执行失败，跳过", exc_info=True)
        _postcheck = None
    # 注意：局部变量勿命名为 `_fix_stats`，否则会遮蔽同名模块级函数
    fix_stats = _log_fix_stats()
    # ✅ 加固（D1）：把「本次构建跨章借图」的明细回传给调用方，写进
    #    X-Chart-Render-Stats（前端提示 + 审计）。加法式：dict 里多一个键，
    #    既有渲染统计键语义一字未动。
    borrowed = [
        {"section_id": sid, "chart_type": ct, "borrowed_from": src}
        for (sid, ct, src) in sorted(_BORROWED_CHARTS)
    ]
    if borrowed:
        logger.warning(
            "导出：本次有 %d 处图表使用了**跨章借图**（该章节没有自己的图表登记，"
            "借用了同类图）—— 成稿图文可能与章节内容不符，请回查对应章节的图表登记",
            len(borrowed))
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
            out_path, _fsize,
            fix_stats.get("sections", 0) if isinstance(fix_stats, dict) else 0,
            fix_stats.get("tables", 0) if isinstance(fix_stats, dict) else 0,
            fix_stats.get("figures", 0) if isinstance(fix_stats, dict) else 0)
    except Exception:
        logger.info("导出：DOCX 已落盘（路径=%s）", out_path)
    # ✅ R129：Word 2010+ 专有扩展清除（zip 级原子重写，fail-soft）。
    #    python-docx 默认模板根元素声明 w14/wp14/wpg/wps/wpc 等 2010+
    #    命名空间（mc:Ignorable="w14 wp14"），全部清除 ⇒ 产物根元素只留
    #    w/r/wp/m 等基础命名空间，满足「避免 Word 2010+ 专有扩展」约束。
    _ns_report: dict | None = None
    try:
        from app.services.docx_namespace import sanitize_docx_file
        _ns_report = sanitize_docx_file(out_path)
        if _ns_report.get("parts_changed"):
            logger.info("导出：已清除 Word 2010+ 专有命名空间声明（部件=%s，属性=%d）",
                        sorted(_ns_report["parts_changed"]),
                        _ns_report.get("attrs_stripped", 0))
    except Exception:
        logger.warning("导出：Word 2010+ 命名空间清除失败（不影响交付）",
                       exc_info=True)
    # ✅ R129：导出后校验 · 文件级（专有命名空间回潮检查），fail-soft
    _ns_clean = True
    try:
        from app.services.docx_namespace import check_docx_file
        _ns_clean = bool(check_docx_file(out_path).get("clean"))
    except Exception:
        logger.warning("导出：命名空间校验执行失败（不影响交付）", exc_info=True)
    if borrowed:
        fix_stats = dict(fix_stats or {})
        fix_stats["chart_fallback_borrowed"] = len(borrowed)
        fix_stats["chart_fallback_borrowed_details"] = borrowed[:20]
    # ✅ R129：把文件级结果并入返回值（X-Fix-Stats 透出给前端）。
    #    ⚠️ 刻意**不**把 outline_map（中文标题）放进响应头：HTTP 头只允许
    #    latin-1（实测 X-Fix-Stats 带中文标题 → starlette init_headers
    #    UnicodeEncodeError，整条导出 500）；只透出条数 outline_entries，
    #    完整映射走 INFO 日志（目录导航核对以日志 + 导出前体检为准）。
    if _ns_report or _postcheck or not _ns_clean:
        fix_stats = dict(fix_stats or {})
        if _ns_report:
            fix_stats["ns_parts_sanitized"] = len(_ns_report.get("parts_changed") or {})
        if _postcheck:
            _omap = _postcheck.get("outline_map") or []
            fix_stats["outline_entries"] = len(_omap)
            if _omap:
                logger.info(
                    "导出：大纲映射 %d 条（目录收录范围核对）：%s",
                    len(_omap),
                    " | ".join(f"L{x['level']} {x['text']}" for x in _omap[:20]))
        fix_stats["ns_clean"] = _ns_clean
    return fix_stats


# ---------------------------------------------------------------------------
# ✅ 重构：导出链路共用同一套"准备逻辑"（R126 起 PDF 下线，现存 DOCX 单链路）
# ---------------------------------------------------------------------------
# 旧实现中 export_docx 与 export_pdf 各自复制了一份"config 解析 + 章节树构建 +
# 图表取码 + 预渲染"（约 140 行近乎逐行重复）。两份实现的漂移已经造成真实缺陷：
# export_docx 已改用 chart_payload.extract_chart_payload 统一解析，
# 而 export_pdf 仍停留在 json.loads(...).get("mermaid_code")，于是
# "信封 + data" 形态的 JSON 数据型图表（组织架构 / 劳动力 / 总平面 / 时间线）
# 在 PDF 中全部静默消失。此处收敛为单一实现，杜绝再次漂移。
_DEFAULT_HEADING_STYLES = {
    # ✅ R130 FIX（2026-10-22 · 六类标准 · 文字标准）：
    #    「标题黑体」——旧默认 1~9 级全是宋体，成稿标题与正文同字体，
    #    层级只能靠字号/加粗区分，不符合工程文件「标题黑体、正文宋体」惯例。
    #    默认改为黑体（eastAsia）；西文统一 Times New Roman（见样式构建处）。
    #    仍可由导出配置 heading_styles 逐级覆盖（用户预设的宋体等保持生效）。
    # ✅ R117 FIX-6（2026-10-20）：默认字号改为**层级阶梯**。
    #    旧默认 1~4 级全是 14pt（5~7 级 12pt）—— 成稿里"章/节/小节/目"
    #    四级标题同大、同色（模板主题蓝），只能靠段前 4pt 之差区分，
    #    实测导出稿的 H2/H3/H4 在纸面上几乎不可分辨。
    #    新阶梯对齐中文工程文件惯例：三号(16) → 四号(14) → 四号(14) → 小四(12)，
    #    并把「4 级起降为小四」作为视觉分水岭；仍可由导出配置逐级覆盖。
    1: {"font_name": "黑体", "font_size": 16, "bold": True},
    2: {"font_name": "黑体", "font_size": 14, "bold": True},
    3: {"font_name": "黑体", "font_size": 14, "bold": True},
    4: {"font_name": "黑体", "font_size": 12, "bold": True},
    5: {"font_name": "黑体", "font_size": 12, "bold": False},
    6: {"font_name": "黑体", "font_size": 12, "bold": False},
    7: {"font_name": "黑体", "font_size": 12, "bold": False},
    # ✅ R129：层级扩展到 L9（引入易标式 L1~L9 逐级样式）。L8/L9 主要服务
    #    正文深层子标题（level + block.level 可达 9）；默认外观延续 L7 的
    #    小四不加粗，仅靠缩进与 outlineLvl 区分层级，仍可逐级覆盖。
    8: {"font_name": "黑体", "font_size": 12, "bold": False},
    9: {"font_name": "黑体", "font_size": 11, "bold": False},
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
    """合并用户配置与默认值，返回 1~9 级标题样式（每级 font_name/font_size/bold）。

    ✅ 防御：heading_styles 子项非 dict、font_size 非数字/越界时静默回退默认，
    避免 merged.update/ Pt() 抛异常导致整个导出 500（脏预设可反复触发）。
    ✅ R129：层级 7→9（引入易标式 L1~L9，L7~L9 显式写 outlineLvl）。
    """
    raw_user = config.get("heading_styles", {})
    user = raw_user if isinstance(raw_user, dict) else {}
    styles: dict[int, dict] = {}
    for lvl in range(1, 10):
        merged = dict(_DEFAULT_HEADING_STYLES[lvl])
        user_lvl = user.get(str(lvl))
        if isinstance(user_lvl, dict):
            merged.update(user_lvl)
        merged["font_size"] = _clamp_font_size(merged.get("font_size"),
                                               _DEFAULT_HEADING_STYLES[lvl]["font_size"])
        styles[lvl] = merged
    return styles


def _parse_frontend_images(body: dict) -> tuple[dict[str, BytesIO], list[str]]:
    """解析前端提交的 mermaid.js 渲染结果（dataURL → BytesIO）。"""
    fe_images: dict[str, BytesIO] = {}
    fe_codes: list[str] = []
    for item in (body.get("chart_images") or []):
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
            hostname, port or (443 if parsed.scheme.lower() == "https" else 80),
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
    redirects_left = int(max_redirects if max_redirects is not None
                          else _settings.image_download_max_redirects)
    redirects_left = max(0, min(redirects_left, 10))
    request_timeout = float(timeout if timeout is not None
                            else _settings.image_download_timeout)
    current_url = _validate_remote_image_url(url)
    async with httpx.AsyncClient(
            timeout=request_timeout, follow_redirects=False, trust_env=False) as client:
        for _ in range(redirects_left + 1):
            async with client.stream("GET", current_url) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location", "")
                    if not location or _ == redirects_left:
                        raise ValueError("图片 URL 重定向次数超限")
                    current_url = _validate_remote_image_url(
                        urljoin(current_url, location))
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
                    if (
                        blk.get("type") == "ai_image"
                        and str(blk.get("code") or "") == code
                    ):
                        return i
            return len(sections)

        _keep = select_ai_image_codes(groups, _order_of, _max_ai)
        if len(groups) > _max_ai:
            logger.info(
                "AI 配图全局预算生效: 候选 %d 张 → 按文档分段择优保留 %d 张（max_ai_images=%d）",
                len(groups), len(_keep), _max_ai)
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
                url = await generate_illustration_image(
                    None, prompt, style, "16:9",
                    _settings.image_provider, _img_cfg)
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
    converted = sum(1 for bs in groups.values() for b in bs
                    if b["type"] == "image")
    logger.info(
        "AI 配图自动生成: %d/%d 块改写成功，共 %d 个唯一占位码"
        "（失败占位块导出时跳过、不占图号）",
        converted, sum(len(bs) for bs in groups.values()), len(groups))
    return converted


async def _query_global_facts(
    db, scheme_id: str, status: dict | None = None,
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
            status.update({"ok": True, "code": "ok" if facts else "empty",
                           "count": len(facts)})
        return facts
    except Exception as e:
        if status is not None:
            status.update({"ok": False, "code": "query_failed", "count": 0})
        logger.warning("导出：读取全局事实失败（降级跳过）: %s", e)
        return []


def _count_undownloaded_image_blocks(
    sections: list[dict], blocks_cache: dict[str, list[dict]],
    image_bytes: dict[str, object]) -> int:
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


async def _prepare_export(scheme_id: str, body: dict, db,
                          cancel_event: threading.Event | None = None) -> dict:
    """导出前的公共准备：解析配置 → 读取章节与图表 → 预渲染图表图片。

    导出链路共用，避免"改了一处漏一处"（历史上曾如此丢失 PDF 侧的
    JSON 数据型图表；R126 起 PDF 已下线，现仅 DOCX 单链路）。

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
    scheme = await _db_fetch_one(cur, what="_prepare_export:schemes")
    if not scheme:
        raise HTTPException(404, "方案不存在")
    scheme = dict(scheme)

    cur = await db.execute(
        # ✅ BUG 修复（2026-09-20）：sort_order 在不同写入路径下语义不一致
        # （落库为同级序号 / 拖拽重排为全局序号），同级重复序号时仅按 sort_order
        # 排序的行次不确定 → 章节顺序在两次导出间漂移，且内容指纹随之抖动
        # （缓存永远 miss、重复渲染）。统一加 level、id 作确定性兜底。
        "SELECT * FROM sections WHERE scheme_id=? ORDER BY sort_order, level, id",
        (scheme_id,))
    sections = [dict(r) for r in await _db_fetch_all(
        cur, what="_prepare_export:sections")]

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
                "内容自动改写：HTML 表格转原生 %d 处；英文串写修正 %d 处；"
                "相邻重复块合并 %d 处", _rw["tables"], _rw["english"], _rw["dupes"])

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
    # ✅ R146（2026-10-29 · 日志 TOP2 error）：表格缺 GFM 分隔行 → `_split_table_block`
    #    返回 [] → 整张表降级裸文本。导出前置确定性补齐（与生成侧同一纯函数，
    #    判据只救「整段无分隔行」的连续表格行，幂等/无损/围栏内不动）。
    #    开关取向与生成侧一致：读**服务端 settings**（content_autofix_table_separators，
    #    默认 True），不占导出 config 白名单 —— 该修复是确定性纠错，不是版式选项，
    #    无需逐次导出可选（config 键须有 UI 落点，见 test_export_closeout 的
    #    TestFrontendBackendConfigParity）。
    from app.config import settings as _afs_settings
    if bool(getattr(_afs_settings, "content_autofix_table_separators", True)):
        from app.services.content_utils import auto_fix_table_separators
        _tbl_total = 0
        for sec in sections:
            raw = sec.get("content") or ""
            if not raw:
                continue
            new, tbl_log = auto_fix_table_separators(raw)
            if tbl_log:
                sec["content"] = new
                _tbl_total += len(tbl_log)
        if _tbl_total:
            logger.info("导出前置自动补齐表格 GFM 分隔行：共 %d 处", _tbl_total)
    # ✅ R99：围栏补齐会抹掉 DLV-07 的 error 级提示（体检看到的是修好后的正文），
    #   而围栏之后的中文正文会被并入图表源码整体跳过 —— 计数必须随 prep 传下去，
    #   一路进 audit_content 的 fence_auto_fixed 提示与 X-Fix-Stats 的 fences_fixed。

    # ✅ 修复（2026-09-17）：补齐交付文档的事实维度（此前漏渲染，见 _query_global_facts）
    global_facts_status: dict = {}
    global_facts = await _query_global_facts(
        db, scheme_id, status=global_facts_status)

    # ✅ 修复：图表数据纳入内容指纹（旧实现指纹只含正文与配置，
    # 重新编排/删除图表/修复代码后正文未变 → 导出仍命中旧缓存，返回旧图）
    cur = await db.execute(
        "SELECT section_id, chart_type, data_json FROM chart_predictions WHERE scheme_id=?",
        (scheme_id,))
    chart_fp = [(r["section_id"], r["chart_type"], r["data_json"] or "")
                for r in await _db_fetch_all(cur, what="_prepare_export:chart_predictions")]

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
    #    故 export_docx 可在两者之间插入缓存查询（export_pdf 已随 R126 下线）。
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
        "render_stats": {"fe": 0, "backend_ok": 0, "failed": 0,
                         "fallback_borrowed": 0},
        # ✅ R99：由 _prepare_export_render **就地填充**（同一 dict 对象）
        "rendered_bytes": {},
        "image_bytes": {},
        "ai_image_pending": 0,
        "ai_image_download_pending": 0,
        "fences_fixed": _fix_total,
    }

    # ✅ 导出前内容体检（只读、不改正文）：与预检 /check 同口径，
    #    通过响应头 X-Content-Audit 回传前端，并在日志中留痕。
    #    ✅ R99：fences_fixed 只在「本次确实补齐过围栏」时产出提示项，
    #    缺省 0 时与改造前逐字一致。
    # ✅ R133-C（2026-10-24 · Mermaid 服务健康探测）：导出预检时探测
    #    Mermaid HTTP 服务可用性（2s 硬超时 + fail-soft，探测失败不阻断
    #    导出）。结果写入 content_audit 的 mermaid_service 顶层键，供前端
    #    导出页展示「原生出图 / PIL v2 回退」的确定性结论。
    _mermaid_status = await _probe_mermaid_status()
    plan["content_audit"] = audit_content(
        sections, fences_fixed=_fix_total, mermaid_status=_mermaid_status)
    logger.info("%s", _format_audit_log(plan["content_audit"]))

    # ✅ D-3（2026-10-01）：补充附录数据源。默认关闭 → 空列表 → 不渲染。
    # ⚠️ project_id 取自 scheme（本函数作用域内没有名为 project_id 的局部变量，
    #    直接引用会 NameError 并让**整条导出链路 500**）。
    plan["appendix_sources"] = await _load_appendix_sources(
        db, scheme_id, str((scheme or {}).get("project_id", "") or ""))
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

    本段只被 ``_prepare_export``（导出链路共用入口，R126 起仅剩 DOCX 单链路）
    与 ``export_docx`` 的缓存未命中分支调用（``export_pdf`` 已随 R126 下线）；
    禁用快速路径时仍走 ``_prepare_export`` = 取数段 + 本段，与改造前逐字一致。

    ✅ R100（2026-10-17）两处修复：
    ① **`_settings` 悬空引用（R98 移交、R99 拆分继留）**：本段远程位图下载
      （`img_sem` 取 `image_max_concurrency`）引用的 `_settings` 原 import 留在
      取数段函数体内，拆分后本作用域根本没有该名字 —— 正文含远程图片 URL 的
      方案一导出即 **NameError → 500**（既有测试从未覆盖「渲染段 + 远程图」组合，
      故基线全绿掩盖了它）。现于本段内显式 import。
    ② **禁用快速路径时的二次渲染回归**：`export_cache_fast_path=False` 时
      `_prepare_export` 内部已调本段一次，而 `export_docx` 的调用点
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
    fe_images = plan.get("fe_images") or {}
    allow_pil_fallback = bool(config.get("allow_pil_fallback", False))
    render_stats: dict = {"fe": 0, "backend_ok": 0, "failed": 0,
                         # ✅ 加固（D1）：跨章借图计数（由 _build_docx_sync 的
                         #   fix_stats 合流后写入），缺省键保持既有形状。
                         "fallback_borrowed": 0}

    def _get_cached_blocks(sec_id: str, content: str) -> list[dict]:
        if sec_id not in _blocks_cache:
            _blocks_cache[sec_id] = _parse_content_blocks(content or "")
        return _blocks_cache[sec_id]

    # 预收集需要渲染的图表代码（去重）
    # ✅ 图表同步生成：内联块自带代码优先；无内联码的标记块仍走 chart_predictions
    # P0-3 性能优化：倒排索引一次构建，避免每块 O(N) 兜底扫描（O(N²) → O(N)）
    # ✅ 加固（D1）：传入文档顺序 rank，使跨章借图的选择确定化
    order_rank = {s["id"]: i for i, s in enumerate(sections)}
    chart_type_index = _build_chart_type_index(chart_lookup, order_rank)
    seen_codes: set[tuple[str, str]] = set()
    unique_codes: list[tuple[str, str]] = []
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
            code, _borrowed = _resolve_chart_code(
                block, sec["id"], ct, chart_lookup, chart_type_index, order_rank)
            if code and (ct, code) not in seen_codes:
                seen_codes.add((ct, code))
                unique_codes.append((ct, code))

    # 预渲染（to_thread 线程池，避免阻塞事件循环）
    # ✅ R99：直接复用 plan 里的同一个 dict（就地填充），
    #    不得新建局部 dict —— 否则渲染结果不会回传到 plan。
    rendered_bytes = plan["rendered_bytes"]

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
                _chart_cache.get_or_render, code, ct, "png", False, allow_pil_fallback)
            if rendered_bytes[(ct, code)] is not None:
                render_stats["backend_ok"] += 1
            else:
                render_stats["failed"] += 1
        except Exception as e:
            logger.warning("图表预渲染失败 (%s): %s", ct, e)
            rendered_bytes[(ct, code)] = None
            render_stats["failed"] += 1

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
            render_stats["fe"], render_stats["backend_ok"], render_stats["failed"],
            len(unique_codes), allow_pil_fallback)

    # ✅ R106 FIX-1：图表段与 AI 配图段之间的取消检查点（命中即抛，
    #    后续「计费生图 + 位图下载 + DOCX 构建」全部不再发生）。
    _check_export_cancelled(_cancel_ev)

    # ✅ AI 配图全自动化（v17）：```ai_image 占位块在导出时自动生成真实图片并
    #    就地改写为 image 块 —— 随后下面的 URL 收集/下载循环会一并取回位图，
    #    write_section 走既有 image 分支插图与编号。生成失败的块保持占位、
    #    整块跳过（不占图号、无红字）。
    _ai_converted = await _auto_generate_ai_image_blocks(
        sections, _blocks_cache, config, cancel_event=_cancel_ev)
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
            "导出：%d 处 AI 配图未生成（占位块残留，成稿将缺图且不写缓存）"
            "；成功改写 %d 处", _ai_pending, _ai_converted)

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
                        from PIL import Image as PILImage
                        PILImage.open(data).verify()
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
        _ai_download_pending = _count_undownloaded_image_blocks(
            sections, _blocks_cache, image_bytes)
    if _ai_download_pending:
        render_stats["ai_image_download_pending"] = _ai_download_pending
        logger.warning(
            "导出：%d 处 AI 配图位图下载失败（成稿将缺图且不写缓存），"
            "已下载 %d/%d 个唯一 URL", _ai_download_pending,
            len(image_bytes), len(unique_image_urls))


    plan["render_stats"] = render_stats
    plan["ai_image_pending"] = _ai_pending
    plan["ai_image_download_pending"] = _ai_download_pending
    # ✅ R100：置幂等哨兵（禁用快速路径时 export_docx 的第二次进入
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
        # ✅ R151（2026-10-31）：目录树只到 L4，TOC 深度同步封顶 4
        #    （用户配置再深也无 L5+ 标题可收录，\o "1-N" N>4 形同虚设）。
        "toc_depth": max(1, min(_int("toc_depth", 3), 4)),
        # ✅ 增强 v7：封面项目信息表 + 页边距（缺省 None → 走默认行为，向后兼容）
        # ✅ 类型防御：脏预设/直调 API 传入非 dict 时静默忽略，避免构建期 500
        "cover_info": config.get("cover_info") if isinstance(
            config.get("cover_info"), dict) else None,
        "margins": config.get("margins") if isinstance(
            config.get("margins"), dict) else None,
        # ✅ 新增（2026-09-19）：图表渲染失败时的产物形态开关。
        "chart_fail_placeholder": bool(config.get("chart_fail_placeholder", False)),
        # ✅ 2026-09-22 引入：一级章节标题底部边框（章框），默认关闭向后兼容
        "heading_border": bool(config.get("heading_border", False)),
        # ✅ 2026-10-02（第二十三轮）：专项施工方案法定前置表单四开关。
        "scheme_forms": {
            k: True for k in ("compilation_note", "approval",
                              "expert_review", "drawing_appendix")
            if bool((config.get("scheme_forms") or {}).get(k)
                    if isinstance(config.get("scheme_forms"), dict) else False)
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
    try:
        cur = await db.execute(
            "SELECT title, usage_text, content FROM knowledge_base "
            "WHERE project_id=? ORDER BY id LIMIT 200", (project_id,))
        items = []
        for r in await _db_fetch_all(cur, what="_load_appendix_sources:knowledge_base"):
            d = dict(r)
            val = str(d.get("content") or d.get("usage_text") or "")
            items.append({"name": str(d.get("title") or ""),
                          "value": val[:500]})
        if items:
            groups.append({"title": "知识库条目", "items": items})
    except Exception as e:
        logger.warning("导出：加载知识库附录数据源失败（降级跳过）: %s", e)

    # ② 解析提取成果（四层存储的提取层）
    try:
        cur = await db.execute(
            "SELECT extract_type, extract_data FROM doc_extractions "
            "WHERE project_id=? AND status != 'stale' ORDER BY id LIMIT 200",
            (project_id,))
        items = []
        for r in await _db_fetch_all(cur, what="_load_appendix_sources:doc_extractions"):
            d = dict(r)
            items.append({"name": str(d.get("extract_type") or ""),
                          "value": str(d.get("extract_data") or "")[:500]})
        if items:
            groups.append({"title": "解析提取成果", "items": items})
    except Exception as e:
        logger.warning("导出：加载解析提取成果附录数据源失败（降级跳过）: %s", e)

    return groups


def _build_docx_task(out_path: str, prep: dict) -> tuple:
    """组装 `_build_docx_sync` 的调用参数（DOCX 单链路；PDF 已随 R126 下线）。

    ✅ 2026-10-11：本函数是导出链路唯一收敛点，版式预设（layout_preset，
    引入自《文档生成规则分析与软件标准建立》4.2 导出标准）在这里套用一次
    即两侧同时生效；未配置预设时 `_resolve_layout_preset` 原样返回，
    产物与改造前逐字一致。
    """
    o = _resolve_layout_preset(prep.get("config") or {}, prep["docx_options"])
    return (
        str(out_path), prep["scheme"], prep["roots"], prep["children_map"],
        prep["chart_lookup"], prep["rendered_bytes"], prep["blocks_cache"],
        o["font_name"], o["font_size"], o["page_header"], o["page_footer"],
        o["show_page_number"], o["show_title_page"], o["show_toc"],
        o["bidder_name"], prep["heading_styles"],
        o["page_break_before_chapter"], o["line_spacing"],
        o["page_number_style"], o["toc_depth"],
        o.get("margins"), o.get("cover_info"),
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


# 影响 DOCX 产物内容的全部配置键白名单（与 _prepare_export 读取保持同步）
_EXPORT_CONFIG_KEYS = frozenset({
    "font_name", "font_size", "page_header", "page_footer",
    "show_page_number", "show_title_page", "show_toc",
    "bidder_name", "chapter_page_break", "page_number_style",
    "line_spacing", "toc_depth", "cover_info", "margins",
    "allow_pil_fallback", "chart_fail_placeholder", "heading_styles",
    "heading_border",
    # ✅ v15：内容自动改写开关（影响产物内容，必须纳入指纹，切换即失效旧缓存）
    "auto_rewrite_content",
    # ✅ 2026-09-22：未闭合围栏自动修复同样影响产物内容，纳入指纹
    "auto_fix_unclosed_fences",
    # ✅ v17：AI 配图导出期自动生成开关（默认 True，影响产物内容，纳入指纹）
    "ai_image_auto_generate",
    # ✅ 2026-10-02：专项施工方案法定前置表单（编制说明/审批表/专家论证报告/
    #    图纸附件清单）—— 影响产物内容，必须纳入指纹，否则切换开关后
    #    config_hash 不变 → 命中旧缓存 → 用户看不到刚打开的表单。
    "scheme_forms",
    # ✅ 2026-10-11：版式预设（行距 + 四边距整体切换）影响产物版式，纳入指纹。
    "layout_preset",
})


def _normalize_config(config: dict) -> dict:
    """导出配置白名单规范化（用于指纹计算，防止缓存无谓击穿）。

    ✅ 优化：前端序列化带来的任意多余字段（调试字段 `_ts`、未知键、空值）
    都会改变 config_hash → 缓存永远不命中。此函数只保留白名单内、
    值非 None/空串 的键，并递归清洗嵌套 dict（heading_styles/margins/cover_info）。
    注意：须与 _prepare_export 读取的键保持同步。
    """
    if not isinstance(config, dict):
        return {}

    def _clean(d: dict) -> dict:
        cleaned: dict = {}
        for k, v in d.items():
            if not k or k.startswith("_") or v is None or v == "":
                continue
            if isinstance(v, dict):
                sub = _clean(v)
                if sub:
                    cleaned[k] = sub
                continue
            cleaned[k] = v
        return cleaned

    return {k: _clean(v) if isinstance(v, dict) else v
            for k, v in config.items()
            if k in _EXPORT_CONFIG_KEYS and v is not None and v != ""}


def _image_generation_signature() -> dict:
    """导出期 AI 配图生成参数的指纹片段（参与 ``_content_fingerprint``）。

    ✅ BUG 修复（2026-09-27 · P1）：配图在导出期由
    ``_auto_generate_ai_image_blocks`` 真实生成，其像素结果由下列配置决定；
    这些键此前**不在** ``_EXPORT_CONFIG_KEYS``（那是前端 ``config`` 体白名单），
    也不在任何指纹字段里 → 换模型/换尺寸后指纹不变，命中旧缓存拿到旧模型的图。

    只取「影响像素」的键，**绝不取 api_key**（密钥变化不应让缓存失效，
    也不应把密文写进任何可被日志/响应头回显的指纹材料）。
    读配置失败一律降级为固定 dict，绝不阻断导出。
    """
    try:
        from app.config import settings as _s
        return {
            "enabled": bool(_s.image_enabled),
            "model": str(getattr(_s, "image_model", "") or ""),
            "size": str(getattr(_s, "image_default_size", "") or ""),
            "base_url": str(getattr(_s, "image_base_url", "") or ""),
        }
    except Exception:  # pragma: no cover - 配置不可用时退化为稳定值
        return {"enabled": False, "model": "", "size": "", "base_url": ""}


def _content_fingerprint(prep: dict) -> tuple[str, str]:
    """计算 (config_hash, content_fingerprint)。

    ✅ BUG 修复：旧指纹只含 (章节 id, 正文)。**改标题 / 调整章节顺序 / 移动层级**
    都不会改变指纹 → 命中旧缓存返回"改标题之前"的文档。现纳入标题、层级、
    父级与排序序号；另将前端渲染签名与导出器版本一并纳入指纹。

    ✅ 修复（2026-10-05 · 高风险回归 R-1）：前端 mermaid.js 渲染的 PNG 实际字节
    进入指纹。旧实现仅哈希 mermaid_code，若前端渲染结果（如缩放/DPI/主题/版本）
    与 code 相同而像素不同（浏览器兼容、样式变更、并发竞态拿错图），指纹不变
    → 命中旧 DOCX 缓存拿到旧像素图；此处对每个 fe_images 项计算 md5 后并入
    "fe_render_pixels" 字段（与既有 "fe_render"=codes 维度并列，不破坏向后兼容）。

    ✅ 修复（2026-10-05 · 高风险回归 R-2）：``prep["image_config"]`` 覆盖
    ``_image_generation_signature()`` 中受影响键。旧实现仅读取服务器 settings，
    测试环境 monkeypatch 到 prep（或未来前端传入 per-request 覆盖配置）后指纹不变
    → 换模型/尺寸仍命中旧缓存。合并策略：先取 settings 签名（保留生产路径），
    再按 prep["image_config"] 逐项覆盖 model / size（api_key 永不进指纹）。
    """
    # ✅ 缓存失效修复（2026-09-23）：渲染器版本号纳入指纹（见下方 "renderer" 键）。
    from app.services.ai.mermaid_renderer import _RENDERER_VERSION
    config = _normalize_config(prep["config"])
    fe_sig = ""
    if prep["fe_codes"]:
        fe_sig = hashlib.md5(
            "\n---\n".join(sorted(prep["fe_codes"])).encode()).hexdigest()[:12]
    # R-1：前端像素指纹（同一 mermaid_code 也可能对应不同 PNG 字节）
    fe_pixels_sig = ""
    _fe_images = prep.get("fe_images") or {}
    if _fe_images:
        _parts: list[str] = []
        for _code_key, _b in _fe_images.items():
            try:
                _bio = _b if isinstance(_b, BytesIO) else BytesIO(bytes(_b))
                _payload = _bio.getvalue()
                _bio.seek(0)
                _parts.append(f"{_code_key}|{hashlib.md5(_payload).hexdigest()}")
            except Exception:
                continue
        if _parts:
            fe_pixels_sig = hashlib.md5(
                "\n---\n".join(sorted(_parts)).encode()).hexdigest()[:16]
    # ✅ BUG 修复（2026-10-11 · 跨模块数据链 R70 · 服务端开关不入指纹）：
    #    `body_subheading_demote_with_children` 直接决定渲染端的编号口径 ——
    #    开启时「有 DB 子章节」的正文子标题被降级为节内编号（1）/ a）/ …），
    #    **产物里的标题层级与编号随之改变**（:579/:710/:3736 三处读取）。
    #    但它取自服务端 settings，不在 `_EXPORT_CONFIG_KEYS`（那是**请求体**
    #    白名单，而该键根本不由前端传入）内 ⇒ 运维切换此开关后 config_hash 与
    #    content_fingerprint 均不变 → 命中旧缓存，用户改了配置却拿到降级前的文档。
    #    与 `scheme_forms`「切换开关必须失效缓存」同一类缺陷，现显式纳入指纹。
    #    默认值 True 与渲染端 `except: _demote = True` 的兜底口径保持一致。
    try:
        from app.config import settings as _demote_settings
        _demote_on = bool(
            getattr(_demote_settings, "body_subheading_demote_with_children", True))
    except Exception:  # pragma: no cover - 配置不可用时沿用渲染端同款兜底
        _demote_on = True
    # R-2：合并 prep["image_config"] 覆盖服务器 settings（保持 settings 路径不变）
    _img_sig = _image_generation_signature()
    _img_cfg = prep.get("image_config") or {}
    if isinstance(_img_cfg, dict):
        _mc = _img_cfg.get("image_model")
        if _mc is not None:
            _img_sig["model"] = str(_mc)
        _ms = _img_cfg.get("image_default_size") or _img_cfg.get("image_size")
        if _ms is not None:
            _img_sig["size"] = str(_ms)
    content_hash = hashlib.md5(
        json.dumps({
            # ✅ BUG 修复（2026-09-18）：封面标题/页眉取自 schemes.name，旧指纹未含
            #    方案名 → 改名后正文与配置未变会命中旧缓存，交付文档封面仍是旧名。
            "scheme": ((prep.get("scheme") or {}).get("name", ""),
                       (prep.get("scheme") or {}).get("project_id", "")),
            "sections": [(s["id"], s.get("parent_id", ""), s.get("sort_order"),
                          s.get("level"), s.get("title", ""), s.get("content") or "")
                         for s in prep["sections"]],
            "charts": sorted(prep["chart_fp"]),
            # ✅ 修复（2026-09-17）：事实变更应触发缓存更新（此前指纹未含事实，
            # invalidate_export_cache 形同空转）。用 .get 防御部分调用方拼装的
            # 最小 prep（缺该键视为无事实），避免 _content_fingerprint 抛 KeyError。
            "global_facts": [(f.get("gt", ""), f.get("title", ""), f.get("content") or "")
                             for f in (prep.get("global_facts") or [])],
            # ✅ D-3：补充附录内容纳入指纹 —— 否则开关打开/数据源变更后
            #    指纹不变，会永久命中不含附录的旧产物（与 2026-09-17 事实变更
            #    不失效是同一类缺陷）。用 .get 防御最小 prep。
            "appendix_sources": [
                (g.get("title", ""),
                 [(i.get("name", ""), i.get("value", "")) for i in (g.get("items") or [])])
                for g in (prep.get("appendix_sources") or [])
            ],
            # 事实查询失败与正常空事实必须使用不同的缓存键。
            "global_facts_status": prep.get("global_facts_status") or {},
            "config": config,
            "fe_render": fe_sig,
            # R-1：前端 PNG 像素字节指纹（补 fe_render 只看 code 的盲区）
            "fe_render_pixels": fe_pixels_sig,
            "exporter": _EXPORTER_VERSION,
            # ✅ 缓存失效修复（2026-09-27 · P1）：配图侧的生成参数纳入指纹。
            #    mermaid 侧早已用 _RENDERER_VERSION 解决"渲染器变了旧图被烤进二进制"，
            #    但图片侧遗漏：用户在 AI 配置里换 image_model / 改 image_default_size
            #    后再导出，content_fingerprint 不变 → 命中旧缓存拿到**旧模型生成的配图**，
            #    且响应头不提示任何异常。此处显式纳入这几个真正影响像素结果的键。
            # R-2：使用已合并 prep["image_config"] 覆盖的 _img_sig。
            "image_gen": _img_sig,
            # ✅ R70（2026-10-11）：正文子标题降级开关（服务端 settings，非请求体）
            #    —— 直接改变产物编号层级，必须参与指纹，否则切换后命中旧产物。
            "subheading_demote": _demote_on,
            # ✅ 缓存失效修复（2026-09-23）：纯渲染逻辑改进（mermaid/PIL 渲染器升级，
            #    _RENDERER_VERSION 未变但逻辑已变）也应失效 DOCX 缓存，否则旧图被烤进
            #    二进制后永远命中。图表数据(data_json)变化固然会让 chart_fp 改变，
            #    但渲染器自身变化不反映在数据里，须显式纳入指纹。
            "renderer": _RENDERER_VERSION,
        }, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
    config_hash = hashlib.md5(json.dumps(config, sort_keys=True).encode()).hexdigest()
    return config_hash, content_hash


# ✅ BUG 修复（2026-09-27 · P0）：本装饰器此前**错贴在本辅助函数上**，
#    而真正的导出端点 ``export_docx`` 没有任何装饰器 →
#    ``POST /sches/{id}/export/docx`` 实际返回 ``"不一致章节数=0"`` 这一 9 字节 JSON
#    字符串，前端 ``exportApi.docx`` 以 ``responseType:"blob"`` 接收 →
#    用户下载到一个打不开的「.docx」，**DOCX 导出功能整体失效**。
#    回归护栏见 tests/test_audit_fixes_20260927.py::TestExportRouteRegistration。
def _summarize_numbering_consistency(report: dict) -> str:
    """将编号一致性报告压缩为单行可读摘要（用于严格模式 409 详情）。"""
    parts = []
    for sec in report.get("sections", []):
        if not sec.get("consistent"):
            sample = ""
            for d in (sec.get("diffs") or [])[:1]:
                sample = " | ".join(d.get("new", [])) if d.get("new") else ""
            parts.append(f"[{sec.get('section_id', '?')[:8]}]{sample}")
    return "; ".join(parts) or f"不一致章节数={report.get('mismatched', 0)}"


async def _guard_numbering_consistency(db, scheme_id: str) -> None:
    """✅ 编号统一（2026-09-26 · 显式跨校验器）：导出前校验落库正文子标题编号与
    当前 outline 编号一致（捕获 D4 类漂移残留，原始需求 五.6 后半）。
    导出经此守卫（历史上曾为 DOCX/PDF 两链路共用，R126 起 PDF 下线）。

    严格模式（numbering_consistency_strict）直接 409 阻断产出不一致文档
    （CI 校验场景）；默认仅告警——导出成稿本身仍由 _compute_subheading 重算
    保证正确，落库正文可用 /sections/numbering-consistency/repair 修复。
    校验器自身异常绝不阻断导出（降级为 debug 日志）。
    """
    try:
        from app.config import settings as _settings
        _strict = bool(_settings.numbering_consistency_strict)
    except Exception:
        _strict = False
    if _strict:
        from app.services.numbering import validate_scheme_numbering_consistency
        _rep = await validate_scheme_numbering_consistency(db, scheme_id)
        if not _rep["consistent"]:
            raise HTTPException(
                409,
                detail={"error": "numbering_inconsistency",
                        "mismatched": _rep["mismatched"],
                        "summary": _summarize_numbering_consistency(_rep)})
    else:
        try:
            from app.services.numbering import validate_scheme_numbering_consistency
            _rep = await validate_scheme_numbering_consistency(db, scheme_id)
            if not _rep["consistent"]:
                logger.warning(
                    "导出前编号一致性校验发现 %d 章落库正文与目录编号不一致（scheme=%s），"
                    "建议调用 /numbering-consistency/repair 或触发结构重排以同步。",
                    _rep["mismatched"], scheme_id[:8])
        except Exception as _e:  # noqa: BLE001
            logger.debug("编号一致性预检失败（不影响导出）: %s", _e)


def _cache_row_viable(result_path: str) -> bool:
    """缓存行是否仍可用：result_path 非空、文件存在且**非空文件**。

    ✅ BUG 修复（2026-10-14 · P2 判据不一致）：命中判据（export_docx，PDF 已下线）
    要求 `Path(...).stat().st_size > 0`，而本函数的两处判据只查 `.exists()`：
      · 一份 0 字节（写盘中断 / 磁盘满）的产物既永远不会被返回给用户（命中判据拦住），
        又被本函数当成「有效缓存」占用 5 份保留配额；
      · 于是它会在裁剪时把**更新且可用**的缓存行挤出保留窗口，
        用户表现为「明明没改内容，缓存却突然失效、重新付一次全量渲染成本」。
    抽成共享判据，使「可用缓存」在读写两侧同一口径。
    """
    if not result_path:
        return False
    try:
        p = Path(result_path)
        return p.is_file() and p.stat().st_size > 0
    except OSError:
        return False


def _artifact_intact(result_path: str) -> bool:
    """✅ R126 FIX-D（2026-10-21 · P2）：交付前产物完整性判据（仅 DOCX ZIP 签名）。

    背景：`_cache_row_viable` 只查 `st_size > 0`，一份**写盘中断被截断的半截文件**
    （头部几百字节有效、ZIP 中央目录缺失）仍能通过命中判据直接交付给用户，
    Word/WPS 打开即报「文件已损坏」/「文字程序意外中断」——用户侧表现为导出文档打不开。

    判据（刻意从简、零依赖、O(尾部) 读）：
      1. 头 4 字节 == `PK\x03\x04`（本地文件头签名，ZIP 规范固定）；
      2. 尾部 64KB 内存在 `PK\x05\x06`（EOCD 中央目录结束记录签名——
         doc.save() 正常收尾才会写入；截断文件必然缺失）。

    刻意不改 `_cache_row_viable`（prune 保留判据语义逐字不变，防止存量
    0 字节/半截行的清理行为漂移）；本函数只接入 **DOCX 命中点**：
    命中不通过 ⇒ 走未命中全链路重建（fail-soft，不影响成功路径产物字节）。
    """
    try:
        p = Path(result_path)
        if not p.is_file():
            return False
        size = p.stat().st_size
        if size < 8:  # 头签名 4B + EOCD 最低 22B 的绝对下界之下的必然残件
            return False
        with p.open("rb") as f:
            if f.read(4) != b"PK\x03\x04":
                return False
            f.seek(max(0, size - 65536))
            return b"PK\x05\x06" in f.read(65536)
    except OSError:
        return False


async def _prune_export_cache(db, scheme_id: str, protect_path: Path) -> None:
    """✅ 缓存保留策略（最多 5 份/方案，与前端 tooltip 说明对齐）——
    删除陈旧/僵尸行并连带清理磁盘文件。2026-10-03（T1）：从 DOCX 分支内联逻辑
    提取为共享 helper —— PDF 分支此前只 INSERT 从不裁剪，DB 行与 EXPORTS_DIR
    磁盘文件随导出次数无限增长；现两分支共用同一保留策略（跨格式合计 5 份）。

    ✅ BUG 修复（保留策略误删新产物）：created_at 精度只到秒，同一秒内插入多行时
    `ORDER BY created_at DESC` 次序不稳定 —— 刚插入的本行可能被排到第 5 位之后，
    其磁盘文件随即被当作"陈旧缓存"删除，紧接着的 FileResponse 就会 404/500。
    故增加 rowid 次序兜底（rowid 单调递增，等价于插入顺序），并显式保护当前产物。
    R13 守卫：db.execute 返回 None（连接/事务异常）时 fail-soft 跳过裁剪。
    """
    cur = await db.execute(
        "SELECT id, result_path FROM export_cache WHERE scheme_id=?"
        " ORDER BY created_at DESC, rowid DESC",
        (scheme_id,))
    cache_rows = [dict(r) for r in await _db_fetch_all(
        cur, what="_prune_export_cache")]
    if not cache_rows:
        # 无行 = 查询失败或本方案确无缓存；两种情况都无需裁剪（fail-soft）
        return
    keep_paths = {str(protect_path)}
    for cr in cache_rows[:5]:
        if _cache_row_viable(cr.get("result_path") or ""):
            keep_paths.add(cr["result_path"])
    stale_ids: list[str] = []
    for i, cr in enumerate(cache_rows):
        # ✅ BUG 修复（2026-09-20）：result_path 为 NULL 时 Path(None) 抛 TypeError；
        #    为空串时 Path("") 等价 Path(".") 恒存在 → 僵尸行被当成「有效缓存」。
        # ✅ 修复（2026-10-14 · P2）：改用 _cache_row_viable（含 st_size>0），
        #    与 export_docx 的命中判据同一口径（export_pdf 已随 R126 下线）—— 0 字节僵尸产物
        #    不再占用 5 份保留配额。
        rp = cr.get("result_path") or ""
        if i >= 5 or not _cache_row_viable(rp):
            stale_ids.append(cr["id"])
    if stale_ids:
        # 删除记录前把磁盘文件一并清理（保留下来的 5 份 + 当前产物不删）
        for cr in cache_rows:
            rp = cr.get("result_path") or ""
            if cr["id"] in stale_ids and rp and rp not in keep_paths:
                try:
                    Path(rp).unlink(missing_ok=True)
                except OSError:
                    pass
        placeholders = ",".join("?" * len(stale_ids))
        await _db_exec(db, f"DELETE FROM export_cache WHERE id IN ({placeholders})", stale_ids,
                    what="_prune_export_cache 清理")


#: 孤儿临时产物的宽限期（秒）。DOCX 原子替换失败会降级为「本次不写缓存」，
#: 返回给用户的产物仍是那个 ``.tmp.docx`` —— 而 FileResponse 在 handler
#: 返回**之后**才开始流式读取该文件，因此临时产物必须留一个宽限期，否则
#: 下一次导出就会把正在被读取的临时产物顺手清掉。正常交付产物
#: （``.docx`` / ``.pdf``）不需要宽限：它们要么被现存缓存行引用（不会命中
#: 孤儿判定），要么本身就是孤儿（可以立即清理）。
_ORPHAN_TMP_TTL_SECONDS = 600


async def _gc_orphan_exports(db, scheme_id: str,
                             protect: set[Path] | None = None) -> int:
    """✅ 孤儿导出产物回收（2026-10-06）。

    背景：``_prune_export_cache`` 只能清理「DB 有行但已陈旧」的文件，而下面
    两类产物**在 DB 里根本没有行**，于是永久留在 ``EXPORTS_DIR``，随导出
    次数累积且零告警：

      · DOCX 原子替换 3 次重试均失败 → 降级为「本次不写缓存」，
        ``out_path = tmp_out_path``（``.tmp.docx``）直接返回给用户，之后再无人清理；
      · PDF 分支 ``os.replace`` 成功、但随后的 INSERT 抛异常 → ``out_path``
        已在盘上，DB 里没有对应行（``/cache-status`` 也不会显示它）。

    一份 DOCX 可达数十 MB，每失败一次就永久泄漏一份。

    判定三重收窄（宁可少删，绝不误删）：

      ① 只扫 ``EXPORTS_DIR`` 的**顶层文件**，子目录整体跳过 ——
         图表 PNG 缓存目录 ``charts/`` 绝不受影响；
      ② 只处理文件名以 ``{scheme_id}_`` 起头的文件 —— 导出器只创建这种
         命名，其它方案的产物天然不在范围内（跨方案误删在结构上不可能）；
      ③ 排除本方案**现存缓存行**指向的路径，以及调用方传入的 ``protect``
         （本次导出仍在被响应读取的产物）。

    ``.tmp.*`` 额外要求年龄超过 :data:`_ORPHAN_TMP_TTL_SECONDS`（见其注释）；
    非临时产物只要没有缓存行引用即为孤儿，直接清理。

    全程 fail-soft：任何异常只记 WARNING 并返回已删除数，**绝不**影响本次
    导出交付。返回删除的文件数（0 = 无孤儿、或全部被判定为受保护）。

    ⚠️ **缓存行读取失败时整体跳过，而不是按「无缓存行」继续**：本函数是
    唯一的**破坏性**清理路径。若 DB 短暂不可用（R13：``db.execute`` 返回
    None）而 ``live`` 恰好为空集，孤儿判定就会把本方案**所有**现存产物判成
    孤儿 —— 用户下次导出秒级缓存全部作废，数十 MB 的成稿被静默删掉。
    因此这里用 :func:`_db_fetch_all` 的 ``strict=True`` 模式（唯一出口不变，
    但读失败抛 :class:`DBReadError` 而非降级为空集），只有成功读到结果集
    才允许删除。
    """
    import os
    import time

    deleted = 0
    try:
        # 先确认「哪些产物仍被缓存行引用」；读不到就整体跳过（保守不删）。
        try:
            rows = await _db_fetch_all(
                await db.execute(
                    "SELECT result_path FROM export_cache WHERE scheme_id=?",
                    (scheme_id,)),
                what="_gc_orphan_exports 缓存行", strict=True)
        except DBReadError as e:
            logger.warning(
                "导出：孤儿回收跳过（无法确认哪些产物仍被缓存行引用，"
                "保守不动任何文件；scheme=%s）: %s", scheme_id[:8], e)
            return deleted
        live = {os.path.normcase(str(dict(r).get("result_path") or ""))
                for r in rows if dict(r).get("result_path")}
        protected = {os.path.normcase(str(p)) for p in (protect or set())}
        # ✅ BUG 修复（2026-10-14 · P0）：跳过「在飞产物」。不同配置指纹的并发导出
        #    持不同的去重锁，可在本函数执行期间正把产物 os.replace 到最终路径、
        #    但 INSERT+commit 尚未发生 —— 此时它既不在 live（DB 无行）、也不在任何
        #    调用方的 protect 里，且文件名不含 .tmp. → 会被误判孤儿 unlink，
        #    导致对端请求 500/404 或缓存行永久指向已删文件。
        #    见模块内 _EXPORT_INFLIGHT_PATHS 的注释（在同模块下方定义，运行时解析）。
        inflight = _export_inflight_snapshot()
        now = time.time()
        prefix = f"{scheme_id}_"
        try:
            entries = list(EXPORTS_DIR.iterdir())
        except OSError:
            return deleted
        for f in entries:
            try:
                if not f.is_file() or not f.name.startswith(prefix):
                    continue
                key = os.path.normcase(str(f))
                if key in live or key in protected or key in inflight:
                    continue
                if ".tmp." in f.name and (now - f.stat().st_mtime) < _ORPHAN_TMP_TTL_SECONDS:
                    continue  # 宽限期内：可能是刚降级返回、仍在流式读取的产物
                f.unlink(missing_ok=True)
                deleted += 1
            except OSError:
                continue
    except Exception as e:  # noqa: BLE001
        logger.warning("孤儿导出产物回收失败（不影响本次导出）: %s", e)
    if deleted:
        logger.info("导出：已回收 %d 个孤儿导出产物（scheme=%s, dir=%s）",
                    deleted, scheme_id[:8], EXPORTS_DIR.name)
    return deleted


# ✅ P2 收口（2026-10-13）：同「方案 + 格式 + 配置」并发导出的重复成本。
#
# 缺陷：缓存查询发生在 `_prepare_export` **之后** ⇒ 第二个同键请求（双击 /
# 多标签页）到达时先行请求尚未落盘，必然 miss，于是两者**各自**付一次全量成本：
#   · `_prepare_export` 内的图表预渲染（有下游缓存，代价较小）；
#   · `_auto_generate_ai_image_blocks` —— **真实计费**。image_engine 的 MD5 成功
#     缓存是**运行期**缓存：并发时两个请求同时错过、各自调用一次 provider；
#   · `_build_docx_sync`（大文档数分钟）；PDF 链（曾再加一次 Word COM 转换，已下线）。
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
    except Exception:  # pragma: no cover - 注册失败只失去保护，不影响正确性
        pass


def _export_inflight_unregister(path: Path | None) -> None:
    """注销在飞产物路径（幂等；None 表示本次降级未落盘、无需注销）。"""
    try:
        with _EXPORT_INFLIGHT_GUARD:
            _EXPORT_INFLIGHT_PATHS.discard(_export_inflight_key(path or Path()))
    except Exception:  # pragma: no cover
        pass


def _export_inflight_snapshot() -> set[str]:
    """在飞路径快照（供 _gc_orphan_exports 判定），避免持锁期间做 IO。"""
    with _EXPORT_INFLIGHT_GUARD:
        return set(_EXPORT_INFLIGHT_PATHS)


# ---------------------------------------------------------------------------
# ✅ 导出终局序列收口（2026-10-14 · 遗留建议执行）：
#    DOCX / PDF 两分支的「写缓存行 → 裁剪 → 提交」与「留痕 → 回收 → 注销」
#    序列曾各自手写两份，历史缺陷全部源于双写分叉：
#      · PDF 长期只 INSERT 不裁剪（T1，行数无限增长）；
#      · DOCX 的 INSERT/prune 长期无显式 commit（P1，靠旁路 helper 副作用）；
#      · PDF 的提交顺序修复（2026-10-13）只落在 PDF 侧，DOCX 侧口径不同。
#    现把两段序列各自收敛为单一 helper，两分支只允许传参不同。
# ---------------------------------------------------------------------------
async def _persist_export_cache_row(db, *, scheme: dict, scheme_id: str,
                                    config_hash: str, content_fingerprint: str,
                                    cache_key: str, out_path: Path,
                                    what: str) -> None:
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
            (str(uuid.uuid4()), scheme.get("project_id", ""), scheme_id, config_hash,
             content_fingerprint, cache_key, str(out_path)),
            what=what)
        await db.commit()
        return ok

    async def _prune_and_commit() -> None:
        await _prune_export_cache(db, scheme_id, protect_path=out_path)
        await db.commit()

    try:
        await retry_db_op(_insert_and_commit, max_retries=4,
                          base_delay=0.1, jitter=0.05)
    except Exception:
        try:
            await db.rollback()
        except Exception:
            pass  # rollback 本身再失败也无可补救，仅放弃缓存行
        logger.warning(
            "导出缓存行持久化失败（%s），本次产物不落缓存、下次将重新生成；"
            "交付不受影响", what, exc_info=True)
        return
    try:
        await retry_db_op(_prune_and_commit, max_retries=4,
                          base_delay=0.1, jitter=0.05)
    except Exception:
        try:
            await db.rollback()
        except Exception:
            pass
        # 裁剪失败仅影响保留策略（旧行多留一轮），下一轮导出的裁剪自然自愈
        logger.warning(
            "导出缓存裁剪失败（%s），旧缓存行将由下次导出自愈", what,
            exc_info=True)


async def _finalize_export_delivery(db, *, scheme_id: str, kind: str,
                                    round_no: int, export_filename: str,
                                    gc_protect: set[Path] | None = None,
                                    inflight_path: Path | None = None) -> None:
    """导出交付前的统一收尾：审核留痕 → 孤儿回收 → 解除在飞保护。

    三步的顺序是安全契约，不得调换：
      1. 留痕先行（其内部 commit 顺带把此前未提交的写入落盘）；
      2. GC 在留痕之后（GC 判定依赖已提交的缓存行）；
      3. 注销在飞保护必须在 GC 之后（GC 期间本产物仍需受保护）。
    inflight_path=None 表示本次降级未落盘最终产物、无注册可注销
    （PDF degraded 分支的 out_path 未绑定场景，旧实现靠 `if not degraded:`
    守卫，现由 None 判定承担）。
    """
    await _record_export_review_trace(db, scheme_id, kind, round_no, export_filename)
    await _gc_orphan_exports(db, scheme_id, protect=gc_protect)
    if inflight_path is not None:
        _export_inflight_unregister(inflight_path)



def _export_config_hash(body: dict) -> str:
    """仅由请求体配置算出的配置指纹。

    与 ``_content_fingerprint(prep)[0]`` **同值**（后者同为
    ``md5(json.dumps(_normalize_config(body["config"]), sort_keys=True))``），
    但可在昂贵的 ``_prepare_export`` **之前**算出 ⇒ 能用作并发去重锁的键。
    """
    return hashlib.md5(json.dumps(
        _normalize_config((body or {}).get("config", {}) or {}),
        sort_keys=True).encode()).hexdigest()


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
        logger.info("导出并发去重：format=%s scheme=%s 检测到同键导出在飞，"
                    "等待其提交缓存后再判定", key[0], str(key[1])[:8])
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
    except Exception:  # pragma: no cover - 注销失败只失登记表，不损失正确性
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
    （实现见 _guard_numbering_consistency；DOCX 单链路消费，PDF 已下线）。

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
        # ✅ R99（2026-10-17 · 缓存命中快速路径）：取数段与渲染段之间插入缓存查询。
        #    `_prepare_export` 在快速路径开启时**只做取数段**（DB 读 + 块解析 +
        #    指纹材料），渲染段延后到「确认未命中」之后才执行 —— 命中即零渲染。
        #    关闭 `export_cache_fast_path` 时 `_prepare_export` 内部已跑完渲染段，
        #    此处 `if` 判定为假 → 与改造前逐字一致。
        prep = await _prepare_export(scheme_id, body, db, cancel_event=cancel_ev)
        scheme = prep["scheme"]
        config_hash, content_hash = _content_fingerprint(prep)

        # 导出文件名 = 方案名称 + 导出日期 + 导出轮次（本次导出即一轮，先自增再命名）
        round_no = await _bump_export_round(db, scheme_id)
        # ✅ 2026-10-11（文档质量报告 P0-B）：下载名与封面标题同一出口，避免
        #    「封面印着模板残留串、下载名却是导出文档」两份署名对不上。
        export_filename = _build_export_filename(
            _display_scheme_name(scheme, prep.get("global_facts")), "docx", round_no)
        name_headers = {**_export_name_headers(export_filename, round_no),
                        **_export_audit_headers(prep.get("content_audit") or {}),
                        **_global_facts_status_headers(prep.get("global_facts_status"))}

        # 缓存检查：内容与配置未变时直接返回历史产物（秒级响应）
        cur = await db.execute(
            "SELECT result_path FROM export_cache WHERE scheme_id=? AND config_hash=? AND content_fingerprint=?",
            (scheme_id, config_hash, content_hash))
        cached = await _db_fetch_one(cur, what="export_docx 缓存查询")
        # ✅ 修复（2026-10-14 · P2）：可用判据收敛到 _cache_row_viable，与
        #    _prune_export_cache 的保留判据同一口径，避免「命中判据/保留判据」分叉。
        # ✅ R126 FIX-D（2026-10-21 · P2）：命中判据再叠加产物完整性校验
        #    （ZIP 头/EOCD 签名）——半截/被截断的缓存文件不再直接交付，
        #    拒绝后走未命中全链路重建；拒绝原因落一行日志（R111 可观测口径）。
        if cached and cached[0] and _cache_row_viable(cached[0]):
            if not _artifact_intact(cached[0]):
                logger.warning(
                    "导出缓存产物完整性校验未通过（scheme=%s · docx · path=%s）"
                    "→ 丢弃缓存改走全量重建", scheme_id[:8], cached[0])
            else:
                # ✅ R111（P2-5）：缓存命中日志埋点（纯可观测；此前命中态只回
                #    X-Cache-Status 响应头，运维无法从 backend.log 统计命中率）。
                logger.info(
                    "导出缓存命中（scheme=%s · docx · 秒级返回历史产物）", scheme_id[:8])
                # ✅ G12：缓存命中同样是「用户拿到了一份文档」，同样落审核留痕
                await _record_export_review_trace(db, scheme_id, "docx", round_no, export_filename)
                # ✅ 契约完整：缓存命中也回传统计/缓存标识响应头，前端统计条行为一致
                return FileResponse(
                    cached[0], filename=export_filename,
                    headers={"X-Chart-Render-Stats": '{"cached":true}',
                             "X-Cache-Status": "hit", **name_headers},
                    media_type=_DOCX_MIME)

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
            tmp_out_path.unlink(missing_ok=True)
            _export_inflight_unregister(_inflight_final)
            raise
        except Exception:
            tmp_out_path.unlink(missing_ok=True)
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
                "DOCX 缓存原子替换 3 次重试均失败（scheme=%s, out=%s），"
                "本次不写入导出缓存（响应体仍为完整产物）",
                scheme_id[:8], EXPORTS_DIR.name)

        # ✅ 坏缓存守卫：存在图表渲染失败 / AI 配图未生成 / 配图位图下载失败时产物含缺图，
        #    不写入缓存——否则 AI 渲染服务恢复后，因内容指纹不变永远命中残缺文档。
        if (render_stats.get("failed", 0) > 0
                or prep.get("ai_image_pending", 0) > 0
                or prep.get("ai_image_download_pending", 0) > 0):
            _merge_borrowed_stats(render_stats, fix_stats)
            headers = {"X-Chart-Render-Stats": json.dumps(
                           {**render_stats,
                            "ai_image_pending": prep.get("ai_image_pending", 0),
                            "ai_image_download_pending": prep.get("ai_image_download_pending", 0)},
                           ensure_ascii=False),
                       "X-Cache-Status": "degraded", **name_headers}
            if fix_stats:
                headers["X-Fix-Stats"] = json.dumps(fix_stats, ensure_ascii=False)
            # ✅ 收口（2026-10-14）：留痕 → 回收 → 注销 统一走 _finalize_export_delivery
            #    （共享 helper，原 PDF 分支已随 R126 下线；GC protect 传 out_path——该文件即将被流式返回，
            #    不得在本次回收；cacheable=False 时本次无缓存行，只能靠这里回收孤儿）。
            await _finalize_export_delivery(
                db, scheme_id=scheme_id, kind="docx", round_no=round_no,
                export_filename=export_filename, gc_protect={out_path},
                inflight_path=_inflight_final)
            return FileResponse(
                str(out_path), filename=export_filename,
                headers=headers, media_type=_DOCX_MIME)

        # 缓存记录
        # ✅ B5（2026-09-23）：INSERT OR IGNORE + (scheme_id, content_fingerprint) 唯一约束，
        #    使同指纹并发导出只持久化一行（另一并发请求虽仍各自构建 docx，但 os.replace 保证
        #    out_path 不坏，且缓存行不重复），实现严格原子。
        # ✅ 2026-10-06：原子替换失败（``cacheable=False``）时**跳过**缓存写入，
        #    避免 result_path 永久指向 ``.tmp.docx`` 孤儿文件（见上方 warning）。
        # ✅ 收口（2026-10-14）：INSERT → commit → prune → commit 收敛为
        #    _persist_export_cache_row（共享 helper；提交顺序沿用原 PDF 分支侧
        #    2026-10-13 确立的双提交口径，详见 helper docstring）。
        if cacheable:
            await _persist_export_cache_row(
                db, scheme=scheme, scheme_id=scheme_id, config_hash=config_hash,
                content_fingerprint=content_hash,
                cache_key=f"{scheme_id}_{config_hash[:8]}",
                out_path=out_path, what="export_cache 写入(DOCX)")

        # ✅ 加固（D1）：把构建期统计到的「跨章借图」并入渲染统计，一并回传
        #    X-Chart-Render-Stats（前端展示 + 用户知情）。加法式合并，既有键不变。
        _merge_borrowed_stats(render_stats, fix_stats)
        headers = {"X-Chart-Render-Stats": json.dumps(render_stats, ensure_ascii=False),
                   "X-Cache-Status": "miss", **name_headers}
        # ✅ R111（P2-5）：缓存未命中日志埋点（纯可观测）。
        logger.info(
            "导出缓存未命中（scheme=%s · docx · 本轮全量渲染，产物已写缓存）",
            scheme_id[:8])
        if fix_stats:
            headers["X-Fix-Stats"] = json.dumps(fix_stats, ensure_ascii=False)
        # ✅ G12：导出成稿落一条审核留痕（from_status = to_status，只留痕不改状态机）
        # ✅ 收口（2026-10-14）：留痕 → 回收 → 注销 统一走 _finalize_export_delivery
        #    （protect 传本次产物，避免被当成孤儿删掉）。
        await _finalize_export_delivery(
            db, scheme_id=scheme_id, kind="docx", round_no=round_no,
            export_filename=export_filename, gc_protect={out_path},
            inflight_path=_inflight_final)
        return FileResponse(
            str(out_path),
            filename=export_filename,
            headers=headers,
            media_type=_DOCX_MIME)
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


@router.get("/cache-status")
async def cache_status(scheme_id: str, db=Depends(read_db)):
    """✅ 修复：补齐前端消费的 total/stale 字段，并过滤文件已丢失的僵尸行
    （旧实现只返回 {items}，前端 cacheStatus.total/stale 恒为 undefined）"""
    # ✅ BUG 修复（2026-10-06 · R46）：created_at 秒级精度，同一秒内多次导出时
    #    ORDER BY created_at DESC 次序不稳定 —— 本方案「最近的 5 份缓存」可能取
    #    成任意 5 行（刚导出的那份反而被挤掉），前端把「最旧产物」显示为「当前
    #    可复用成果」。与 _prune_export_cache 同口径补 rowid 次序兜底。
    cur = await db.execute(
        "SELECT * FROM export_cache WHERE scheme_id=? ORDER BY created_at DESC, rowid DESC LIMIT 5",
        (scheme_id,))
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
