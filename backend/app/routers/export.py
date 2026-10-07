"""DOCX 导出路由（增强版 — 图表渲染 + 封面 + 目录 + 标题编号）"""
import asyncio
import base64
import hashlib
import ipaddress
import json
import logging
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
from app.db import get_db, read_db

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

# ✅ 《待补充清单》（2026-09-24，治 F 层人工补录兜底）：逐条扫描章节正文中的
#    占位符（规范字段占位 / 裸标记 / 模糊 ××），按字段与章节聚合，支持前端跳转定位。
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
            logger.warning("导出：读取 %s 失败（严格读模式 → 抛 DBReadError）: %s",
                           what, e)
            raise DBReadError(str(e)) from e
        logger.warning("导出：读取 %s 失败（降级为空结果集）: %s", what, e)
        return []


async def _db_fetch_one(cur, *, what: str):
    """R13 + 容错读：``fetchone`` 的唯一出口。cur 为 None / 读取异常 → None。"""
    if cur is None:
        logger.warning("导出：db.execute 返回 None（%s），降级为无行", what)
        return None
    try:
        return await cur.fetchone()
    except Exception as e:  # pragma: no cover - 连接层异常
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
    """R13 + 写路径可观测：``execute`` 的唯一出口。返回 False = 本次写入未生效。"""
    cur = await db.execute(sql, params)
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
_EXPORTER_VERSION = "22"


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
    ("placeholder", "数据占位符（××/xx/【待补充】）", "warn",
     re.compile(r"××+|(?<![A-Za-z0-9])[xX]{2}(?![A-Za-z0-9])|【待补充")),
    ("html_tag", "HTML 标签残留（应使用 GFM 表格）", "error",
     re.compile(r"</?(?:table|thead|tbody|tr|td|th|div|span|p|br)\b[^>]*>", re.I)),
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


def audit_content(sections: list[dict]) -> dict:
    """扫描各章正文，产出可交付性问题清单（供导出预检 / 响应头 / 日志）。

    返回 ``{"total": int, "items": [...]}``；``items`` 每项含
    key / label / level / count / sections（最多 3 个章节标题，便于定位）。
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
    # 级别排序：error → warn，同级按数量降序（用户先看最要命的）
    order = {"error": 0, "warn": 1, "info": 2}
    items = sorted(hits.values(),
                   key=lambda x: (order.get(x["level"], 9), -x["count"]))
    return {"total": sum(x["count"] for x in items), "items": items}


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
    """
    return (sec.get("sort_order", 0) or 0,
            int(sec.get("level") or 1),
            str(sec.get("id") or ""))


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
            sub_counters: dict[int, int] = {}
            for block in blocks:
                if block.get("type") != "heading":
                    continue
                md_lv = block.get("level", 1)
                bpure = _strip_title_number(block.get("text", ""))
                text, _style = _compute_subheading(
                    section_prefix, level, md_lv, sub_counters, bpure,
                    sec.get("id") or "", has_children=has_children_demoted)
                block["_fixed_text"] = text
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
    """
    if not isinstance(report, dict):
        return
    try:
        await db.execute(
            "INSERT INTO placeholder_baselines (id, scheme_id, total,"
            " formatted_total, bare_total, fuzzy_total, field_count, section_count)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), scheme_id,
             int(report.get("total") or 0),
             int(report.get("formatted_total") or 0),
             int(report.get("bare_total") or 0),
             int(report.get("fuzzy_total") or 0),
             int(report.get("field_count") or 0),
             int(report.get("section_count") or 0)))
        # 写入端裁剪：只保留最近 N 行（created_at 秒级精度不足，按 rowid 兜底）
        await db.execute(
            "DELETE FROM placeholder_baselines WHERE scheme_id=? AND id NOT IN ("
            " SELECT id FROM placeholder_baselines WHERE scheme_id=?"
            " ORDER BY created_at DESC, rowid DESC LIMIT ?)",
            (scheme_id, scheme_id, _PLACEHOLDER_BASELINE_KEEP))
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


async def collect_export_issues(scheme_id: str, db) -> dict:
    """导出预检的核心计算（**不写库**，可被多处复用）。

    ✅ G1：此前本计算内联在 ``export_check`` 路由里，就绪度总检无法复用，
    只能各自实现一份判定 → 口径分叉。现抽成独立函数：
      · 导出页 → 展示问题清单 + ``preflight_summary``；
      · readiness_overview → 经 ``export_issues_to_findings`` 并入六维评分。
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
        cur, what="collect_export_issues:sections")]
    section_ids = {s["id"] for s in sections}
    parent_ids = {s["parent_id"] for s in sections if s.get("parent_id")}
    issues = []
    total_words = 0
    generated_count = 0
    for s in sections:
        has_children = s["id"] in parent_ids
        wc = s.get("word_count") or 0
        total_words += wc
        if wc > 0 or s.get("content"):
            generated_count += 1
        # 孤立节点：parent_id 非空但指向不存在的父节点
        pid = s.get("parent_id", "")
        if pid and pid not in section_ids:
            issues.append({"type": "orphan_node", "section_id": s["id"],
                           "title": s["title"], "parent_id": pid})
        # 空叶子章节（无内容且无子女）
        if not s.get("content") and not has_children:
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
        cur, what="collect_export_issues:chart_predictions")
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
    cur = await db.execute(
        "SELECT project_id FROM schemes WHERE id=?", (scheme_id,))
    scheme_row = await _db_fetch_one(
        cur, what="collect_export_issues:schemes.project_id")
    _ba_summary = None
    if scheme_row and scheme_row["project_id"]:
        ba_pid = scheme_row["project_id"]
        cur2 = await db.execute(
            "SELECT item_id, label, status, required FROM bid_analysis_items "
            "WHERE project_id=? AND COALESCE(required,0)=1",
            (ba_pid,))
        ba_required = [dict(r) for r in await _db_fetch_all(
            cur2, what="collect_export_issues:bid_analysis_items")]
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
        if scheme_row and scheme_row["project_id"]:
            cur = await db.execute(
                "SELECT COUNT(*) AS total, "
                "SUM(CASE WHEN COALESCE(is_resolved,0)=0 THEN 1 ELSE 0 END) AS unresolved, "
                "SUM(CASE WHEN COALESCE(is_simulated,0)=1 THEN 1 ELSE 0 END) AS simulated, "
                "SUM(CASE WHEN COALESCE(has_conflict,0)=1 THEN 1 ELSE 0 END) AS conflicted, "
                "SUM(CASE WHEN COALESCE(is_stale,0)=1 THEN 1 ELSE 0 END) AS stale, "
                "SUM(CASE WHEN COALESCE(is_resolved,0)=0 OR COALESCE(is_simulated,0)=1 "
                "OR COALESCE(has_conflict,0)=1 OR COALESCE(is_stale,0)=1 THEN 1 ELSE 0 END) "
                "AS blocked "
                "FROM global_facts WHERE scheme_id=? OR (project_id=? AND "
                "(scheme_id='' OR scheme_id IS NULL))",
                (scheme_id, scheme_row["project_id"]))
            frow = await _db_fetch_one(
                cur, what="collect_export_issues:global_facts 统计")
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
    #    只扫节号引用（从 outline_json 内部推导真实节号集合）——图号/表号漂移
    #    需导出引擎 dry-run 才能判定，Tier 2 再补；此处节号漂移已能覆盖最常见
    #    场景（目录重排后章节不存在/编号变化）。严重度 medium，不阻断导出。
    try:
        from app.config import settings as _s
        if _s.crossref_stale_detect_enabled:
            issues.extend(_detect_stale_cross_references(sections))
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

    empty_ratio = round((len(sections) - generated_count) / max(len(sections), 1) * 100, 1)
    # ✅ 新增（2026-09-19）：内容体检（占位符 / HTML 残留 / LaTeX 空参数 /
    #    投标用语 / 未闭合图表围栏）—— 独立字段，不进 issues（避免改变既有
    #    前端"问题计数"语义），供导出前弹窗与"待补充清单"定位使用。
    content_audit = audit_content(sections)
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
        "generated_count": generated_count,
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
    """预设按项目维度共享：从方案反查其 project_id。"""
    cur = await db.execute("SELECT project_id FROM schemes WHERE id=?", (scheme_id,))
    row = await _db_fetch_one(cur, what="_preset_project_id")
    return row["project_id"] if row else ""


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
    LEAD_IN_MAX_CHARS,  # ✅ R48（2026-10-06）：引导语长度上限收敛到唯一实现，
    _cn_pure_to_int,
    _compute_subheading,
    _detect_plain_heading,  # noqa: F401  兼容再导出：测试与诊断脚本经 app.routers.export 引用
    #                          消除本模块此前的裸 `60` 字面量（同一判据第 3 份副本）。
    _parse_content_blocks,
    _strip_duplicate_leading_title,
    _strip_title_number,
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
_INLINE_RE = re.compile(
    r"\*\*\*(?P<bi>.+?)\*\*\*"
    r"|\*\*(?P<b>.+?)\*\*"
    r"|\*(?P<i>.+?)\*"
    r"|~~(?P<s>.+?)~~"
    r"|`(?P<c>[^`]+)`"
    r"|!?\[(?P<l>[^\]\n]+)\]\((?P<lu>[^)\s]*)\)"
)


def _set_run_font(run, name: str, size=None, *, bold=None, italic=None,
                  strike=None, color=None):
    """统一设置 run 字体：同时写入 w:ascii / w:hAnsi 与 w:eastAsia。

    ✅ BUG 修复：旧实现只写 `run.font.name`（= w:ascii + w:hAnsi），中文实际
    由主题的 eastAsia 字体接管，在"宋体正文 + 黑体标题"等混排场景下中文会
    悄悄回退成默认字体，与配置不符。
    """
    from docx.oxml.ns import qn
    run.font.name = name
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

    转换失败时回退为原文文本（``$...$``），保证不丢内容。
    """
    from docx.oxml import parse_xml

    try:
        if display and whole_para:
            xml = docx_math.latex_to_omml_display(latex)
        else:
            xml = docx_math.latex_to_omml(latex)
        node = parse_xml(xml)
        paragraph._p.append(node)
    except Exception:  # pragma: no cover - 转换失败兜底
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
                   "table_truncated")
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
    stats = dict(st)
    if st["block_render_failed"] or st["table_truncated"]:
        logger.warning(
            "DOCX 导出存在降级：%d 处内容块渲染失败（已降级为纯文本）、"
            "%d 处表格超尺寸（已截断）—— 成稿可交付但版式不完整，"
            "请回查源文对应章节", st["block_render_failed"], st["table_truncated"])
    logger.info("DOCX 导出自动修复: %s", "、".join(parts))
    st.update({k: 0 for k in st})
    return stats


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
    _set_run_font(p.add_run(text), font_name, font_size, bold=True)
    return p


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
    from docx.shared import Pt

    def _split_row(raw: str) -> list[str]:
        # 先保护转义竖线 \| ，再按竖线切分，最后还原为普通 |
        safe = raw.strip().replace("\\|", "\x00").strip("|")
        return [c.replace("\x00", "|").strip() for c in safe.split("|")]

    # 跳过第 2 行（|:---| 对齐分隔行，由调用方保证存在）
    rows = [_split_row(r) for j, r in enumerate(tbl_lines) if j != 1]
    if not rows:
        return
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
                shd = OxmlElement("w:shd")
                shd.set(qn("w:val"), "clear")
                shd.set(qn("w:color"), "auto")
                shd.set(qn("w:fill"), "B6B1D1")
                cell._tc.get_or_add_tcPr().append(shd)

    # ✅ 增强：表头行跨页重复（长表格翻页后仍能看到列名）
    if len(rows) > 1:
        tr_pr = table.rows[0]._tr.get_or_add_trPr()
        if tr_pr.find(qn("w:tblHeader")) is None:
            tbl_header = OxmlElement("w:tblHeader")
            tbl_header.set(qn("w:val"), "true")
            tr_pr.append(tbl_header)
    # ✅ 增强：列宽按各列最长内容自适应分配（总宽约 16cm），避免长文本被挤成多行
    _assign_table_column_widths(table, rows, ncols)


def _assign_table_column_widths(table, rows: list[list[str]], ncols: int) -> None:
    """按各列最长内容（中文字符权重1、半角0.5）比例分配列宽，总宽约 16cm。

    同时声明固定布局（tblLayout=fixed）+ 表格总宽（tblW），否则 Word 会按内容
    重新折列宽，自适应列宽不生效。
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
    total_cm = 16.0
    widths_cm = [max(1.2, total_cm * w / total) for w in weights]
    scale = total_cm / (sum(widths_cm) or 1.0)  # 归一化，避免浮点误差溢出页边距
    widths_cm = [w * scale for w in widths_cm]
    twips = [int(round(c * 567)) for c in widths_cm]

    tbl_pr = table._tbl.tblPr
    tbl_w = tbl_pr.find(qn("w:tblW"))
    if tbl_w is None:
        tbl_w = OxmlElement("w:tblW")
        tbl_pr.append(tbl_w)
    tbl_w.set(qn("w:w"), str(sum(twips)))
    tbl_w.set(qn("w:type"), "dxa")
    layout = tbl_pr.find(qn("w:tblLayout"))
    if layout is None:
        layout = OxmlElement("w:tblLayout")
        tbl_pr.append(layout)
    layout.set(qn("w:type"), "fixed")
    for row in table.rows:
        for ci, cell in enumerate(row.cells):
            cell.width = Cm(widths_cm[ci])


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
        shd = OxmlElement("w:shd")
        shd.set(qn("w:val"), "clear")
        shd.set(qn("w:color"), "auto")
        shd.set(qn("w:fill"), "F2F2F2")
        c0._tc.get_or_add_tcPr().append(shd)
        shd1 = OxmlElement("w:shd")
        shd1.set(qn("w:val"), "clear")
        shd1.set(qn("w:color"), "auto")
        shd1.set(qn("w:fill"), "F2F2F2")
        c1._tc.get_or_add_tcPr().append(shd1)


# ---------------------------------------------------------------------------
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
            shd = OxmlElement("w:shd")
            shd.set(qn("w:val"), "clear")
            shd.set(qn("w:color"), "auto")
            shd.set(qn("w:fill"), "F2F2F2")
            cell._tc.get_or_add_tcPr().append(shd)
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
    return [
        ("工程名称", info.get("工程名称", "")),
        ("方案名称", (scheme or {}).get("name", "")),
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
    project_name = str(info.get("工程名称", "") or "").strip()
    scheme_name = str((scheme or {}).get("name", "") or "").strip()
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
#    `_build_docx_sync` 之外 → export_docx / export_pdf 返回 500，
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
        if log_height_scale and _h_cm >= _CHART_MAX_HEIGHT_CM:
            logger.info(
                "%s 按高度上限缩放至 %.2f×%.2fcm（页面可用高度约 %.1fcm，"
                "原图 %dx%dpx dpi=%s）",
                log_label, _w_cm, _h_cm, _CHART_MAX_HEIGHT_CM,
                _iw_px, _ih_px, _dpi or _CHART_DPI_FALLBACK)
        stream.seek(0)
        doc.add_picture(stream, width=Cm(_w_cm))
        doc.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER
        caption = doc.add_paragraph()
        caption.alignment = WD_ALIGN_PARAGRAPH.CENTER
        caption.paragraph_format.first_line_indent = Cm(0)
        _set_run_font(caption.add_run(caption_text), font_name, font_size, bold=True)
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
    （``filename="{方案名}.pdf"``），而 HTTP 头只能承载 latin-1 —— 中文方案名
    会在编码响应头时抛 UnicodeEncodeError，导致 **PDF 导出整体 500**
    （DOCX 走 FileResponse 由 Starlette 自行编码，所以该缺陷长期只暴露在 PDF 链路）。
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


async def _bump_export_round(db, scheme_id: str) -> int:
    """累加并返回该方案的导出轮次（持久化在 schemes.export_round）。

    语义：**每次导出请求计一轮**（DOCX / PDF 共用同一计数器，PDF 内部生成的
    中间 DOCX 不额外计数）—— 保证同一天多次导出的文件名互不重复，不会出现
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
    """
    if not audit or not audit.get("items"):
        return {}
    from urllib.parse import quote
    return {"X-Content-Audit": quote(json.dumps(audit, ensure_ascii=False), safe="")}


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

    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = font_name
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
        for _theme_attr in ("w:asciiTheme", "w:hAnsiTheme",
                            "w:eastAsiaTheme", "w:cstheme"):
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
        _set_run_font(title_p.add_run(scheme.get("name", "")), font_name, 28, bold=True)
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
                    pure = _strip_title_number(block.get("text", ""))
                    text, style_lv = _compute_subheading(
                        section_prefix, level, md_lv, sub_counters, pure, sec_id,
                        has_children=has_children_demoted)
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
            ordered_stack: list[list] = []   # [[marker, seq], ...]，下标 = indent_lvl
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
                            _add_table_caption(doc, f"表 {table_num} {caption}",
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
                                if len(cells) >= 2:
                                    _auto_caption = "、".join(cells[:3])
                                elif cells:
                                    _auto_caption = cells[0]
                            if not _auto_caption:
                                _auto_caption = "相关数据"
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
                            (block.get("alt") or "").strip(), font_name, font_size)
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
                                ordered_stack = ordered_stack[:indent_lvl + 1]
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
                        p = doc.add_paragraph(style=_lp_style)
                        _add_runs_with_inline_format(p, prefix + block["text"])
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
        except TypeError as e:
            # 与事实附录同口径：TypeError 是编程错误，按 ERROR 上报（否则附录
            # 静默消失而导出报成功，是最隐蔽的一类缺陷）。
            logger.error(
                "导出：渲染补充附录遇到编程错误（附录将缺失，正文不受影响）: %s",
                e, exc_info=True)
        except Exception as e:
            logger.warning("导出：渲染补充附录失败（降级跳过，不影响正文）: %s", e)

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


async def _prepare_export(scheme_id: str, body: dict, db) -> dict:
    """导出前的公共准备：解析配置 → 读取章节与图表 → 预渲染图表图片。

    DOCX 与 PDF 两条链路共用，避免"改了一处漏一处"（历史上正是如此丢失了
    PDF 里的 JSON 数据型图表）。
    """
    from app.config import settings as _settings

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

    # ✅ 渲染策略（v12 起契约调整）：
    # - HTTP Service 优先（mermaid 原生渲染）；Mermaid 类图表渲染失败时**默认跳过该图
    #   且不占图号**（交付文档不写红字报错；可传 chart_fail_placeholder=true 恢复占位）。
    #   "不静默"承诺保留在系统侧信号：X-Chart-Render-Stats / X-Content-Audit 响应头、
    #   导出日志与 /export/check 预检。
    # - layout/timeline/labor 的 JSON 数据载荷不受 allow_pil_fallback 限制
    #   （PIL 是其唯一渲染路径）。
    # - 用户显式传 allow_pil_fallback=True 可开启 PIL 兜底。
    allow_pil_fallback = bool(config.get("allow_pil_fallback", False))

    # 统一渲染轨：接收前端 mermaid.js（与预览一致）渲染的 PNG。
    # 命中的图表直接嵌入（所见即所得）；未命中/失败的图表回退后端渲染轨。
    fe_images, fe_codes = _parse_frontend_images(body)
    # 渲染轨统计（写入响应头 X-Chart-Render-Stats，前端展示给用户）
    render_stats = {"fe": 0, "backend_ok": 0, "failed": 0}
    # ✅ 加固（D1 · 2026-10-06）：跨章借图此前**零信号** —— 该章节没有自己的
    #    图表登记时，导出会借一张同类图填进去，成稿图文可能不符而系统侧
    #    （响应头 / 预检 / 日志）无任何提示。现随渲染统计一并回传
    #    `fallback_borrowed`（由 _build_docx_sync 的 fix_stats 合流后写入）。
    render_stats["fallback_borrowed"] = 0

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
    _blocks_cache: dict[str, list[dict]] = {}

    def _get_cached_blocks(sec_id: str, content: str) -> list[dict]:
        if sec_id not in _blocks_cache:
            _blocks_cache[sec_id] = _parse_content_blocks(content or "")
        return _blocks_cache[sec_id]

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
    rendered_bytes: dict[tuple[str, str], object] = {}

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
                await _render_one(ct, code)

        await asyncio.gather(*[_render_one_limited(ct, code) for ct, code in unique_codes])
        logger.info(
            "渲染轨统计: 前端图 %d, 后端 %d, 失败 %d（总计 %d, allow_pil=%s）",
            render_stats["fe"], render_stats["backend_ok"], render_stats["failed"],
            len(unique_codes), allow_pil_fallback)

    # ✅ AI 配图全自动化（v17）：```ai_image 占位块在导出时自动生成真实图片并
    #    就地改写为 image 块 —— 随后下面的 URL 收集/下载循环会一并取回位图，
    #    write_section 走既有 image 分支插图与编号。生成失败的块保持占位、
    #    整块跳过（不占图号、无红字）。
    _ai_converted = await _auto_generate_ai_image_blocks(
        sections, _blocks_cache, config)
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
    image_bytes: dict[str, BytesIO] = {}
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

    # ✅ 导出前内容体检（只读、不改正文）：与预检 /check 同口径，
    #    通过响应头 X-Content-Audit 回传前端，并在日志中留痕。
    content_audit = audit_content(sections)
    logger.info("%s", _format_audit_log(content_audit))

    return {
        "scheme": scheme,
        "sections": sections,
        "content_audit": content_audit,
        "global_facts": global_facts,
        "global_facts_status": global_facts_status,
        "chart_fp": chart_fp,
        "chart_lookup": chart_lookup,
        "rendered_bytes": rendered_bytes,
        "image_bytes": image_bytes,
        "blocks_cache": _blocks_cache,
        "roots": roots,
        "children_map": children_map,
        "fe_codes": fe_codes,
        "render_stats": render_stats,
        # ✅ P1（2026-09-27）：AI 配图未生成数随 prep 回传给 export_docx 的
        #    坏缓存守卫（跨函数，无法直接用局部变量）。
        "ai_image_pending": _ai_pending,
        # ✅ P2（2026-09-30）：生图成功但位图下载失败的配图数（第二缺口），
        #    同样随 prep 回传并计入坏缓存守卫。
        "ai_image_download_pending": _ai_download_pending,
        # ✅ D-3（2026-10-01）：补充附录数据源。默认关闭 → 空列表 → 不渲染。
        # ⚠️ project_id 取自 scheme（本函数作用域内没有名为 project_id 的局部变量，
        #    直接引用会 NameError 并让**整条导出链路 500**）。
        "appendix_sources": await _load_appendix_sources(
            db, scheme_id, str((scheme or {}).get("project_id", "") or "")),
        "config": config,
        "heading_styles": _load_heading_styles(config),
        "docx_options": {
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
            "cover_info": config.get("cover_info") if isinstance(
                config.get("cover_info"), dict) else None,
            "margins": config.get("margins") if isinstance(
                config.get("margins"), dict) else None,
            # ✅ 新增（2026-09-19）：图表渲染失败时的产物形态开关。
            #    False（默认）= 跳过该图、不占图号，交付文档不出现红字报错
            #    （"不静默"承诺由响应头 X-Chart-Render-Stats / X-Content-Audit、
            #    日志与导出预检承担）；True = 保留 V7.0 的红色占位段（排查定位用）。
            "chart_fail_placeholder": bool(config.get("chart_fail_placeholder", False)),
            # ✅ 2026-09-22 引入：一级章节标题底部边框（章框），默认关闭向后兼容
            "heading_border": bool(config.get("heading_border", False)),
            # ✅ 2026-10-02（第二十三轮）：专项施工方案法定前置表单四开关。
            #    逐项 bool 化 + 只保留为真的键 → 全关时得到空 dict，
            #    空 dict 在 _build_docx_sync 内等价于「不渲染」，与旧产物逐字一致。
            #    类型防御：非 dict 的脏预设静默忽略，避免构建期 500。
            "scheme_forms": {
                k: True for k in ("compilation_note", "approval",
                                  "expert_review", "drawing_appendix")
                if bool((config.get("scheme_forms") or {}).get(k)
                        if isinstance(config.get("scheme_forms"), dict) else False)
            },
        },
    }


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
    """组装 `_build_docx_sync` 的调用参数（DOCX / PDF 两条链路共用）。"""
    o = prep["docx_options"]
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
    )


# 影响 DOCX/PDF 产物内容的全部配置键白名单（与 _prepare_export 读取保持同步）
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
    DOCX 与 PDF 两条链路共用（均经此守卫，避免"改了一处漏一处"）。

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
        if cr["result_path"] and Path(cr["result_path"]).exists():
            keep_paths.add(cr["result_path"])
    stale_ids: list[str] = []
    for i, cr in enumerate(cache_rows):
        # ✅ BUG 修复（2026-09-20）：result_path 为 NULL 时 Path(None) 抛 TypeError；
        #    为空串时 Path("") 等价 Path(".") 恒存在 → 僵尸行被当成「有效缓存」。
        rp = cr.get("result_path") or ""
        if i >= 5 or not (rp and Path(rp).exists()):
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
                if key in live or key in protected:
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


@router.post("/docx")
async def export_docx(scheme_id: str, body: dict, db=Depends(get_db)):
    """导出 DOCX（封面 / 目录 / 标题编号 / 图表 / 页眉页脚页码）。"""
    # ✅ 编号统一（2026-09-26 · 显式跨校验器）：导出前编号一致性守卫
    #    （DOCX/PDF 共用实现，见 _guard_numbering_consistency）
    # ✅ D-1（2026-10-01）：守卫**前置**到 _prepare_export 之前。
    #    旧顺序是「先 prepare、后守卫」，而 _prepare_export 内部会执行图表渲染
    #    与 AI 生图（**真实计费**）；严格模式（numbering_consistency_strict=True）
    #    下守卫抛 409 时，这些成本已经付出且产物全部作废，用户只看到一次失败。
    #    PDF 链（export_pdf）本来就是「守卫 → prepare」，此处对齐为同一顺序。
    #    守卫只依赖 (db, scheme_id)，不依赖 prep，前置无副作用。
    await _guard_numbering_consistency(db, scheme_id)
    prep = await _prepare_export(scheme_id, body, db)
    scheme = prep["scheme"]
    render_stats = prep["render_stats"]
    config_hash, content_hash = _content_fingerprint(prep)

    # 导出文件名 = 方案名称 + 导出日期 + 导出轮次（本次导出即一轮，先自增再命名）
    round_no = await _bump_export_round(db, scheme_id)
    export_filename = _build_export_filename(scheme.get("name"), "docx", round_no)
    name_headers = {**_export_name_headers(export_filename, round_no),
                    **_export_audit_headers(prep.get("content_audit") or {}),
                    **_global_facts_status_headers(prep.get("global_facts_status"))}

    # 缓存检查：内容与配置未变时直接返回历史产物（秒级响应）
    cur = await db.execute(
        "SELECT result_path FROM export_cache WHERE scheme_id=? AND config_hash=? AND content_fingerprint=?",
        (scheme_id, config_hash, content_hash))
    cached = await _db_fetch_one(cur, what="export_docx 缓存查询")
    if cached and cached[0] and Path(cached[0]).exists() and Path(cached[0]).stat().st_size > 0:
        # ✅ G12：缓存命中同样是「用户拿到了一份文档」，同样落审核留痕
        await _record_export_review_trace(db, scheme_id, "docx", round_no, export_filename)
        # ✅ 契约完整：缓存命中也回传统计/缓存标识响应头，前端统计条行为一致
        return FileResponse(
            cached[0], filename=export_filename,
            headers={"X-Chart-Render-Stats": '{"cached":true}',
                     "X-Cache-Status": "hit", **name_headers},
            media_type=_DOCX_MIME)

    # ✅ 性能优化：out_path 提前计算（仅依赖 scheme_id/hash，与 DOCX 内容无关）
    out_path = EXPORTS_DIR / f"{scheme_id}_{config_hash[:8]}_{content_hash[:8]}.docx"

    # ✅ 并发修复：相同内容指纹的并发导出（双击/多标签页）会同时对同一个
    # out_path 各自 doc.save()，可能产出损坏的 DOCX 并被写入 export_cache，
    # 之后每次缓存命中都返回坏文件。现先写唯一临时文件，完成后原子替换。
    tmp_out_path = EXPORTS_DIR / f"{scheme_id}_{config_hash[:8]}_{content_hash[:8]}.{uuid.uuid4().hex}.tmp.docx"

    # ✅ 性能优化：DOCX 构建（CPU 密集）整体移到线程池，避免阻塞事件循环
    # ✅ 资源守卫：构建异常时必须清理临时文件，否则数十 MB 的 tmp.docx 永久残留。
    try:
        fix_stats = await asyncio.to_thread(_build_docx_sync, *_build_docx_task(tmp_out_path, prep))
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
        await _record_export_review_trace(db, scheme_id, "docx", round_no, export_filename)
        # ✅ 孤儿产物回收（2026-10-06）：cacheable=False 时本次返回的是
        #    .tmp.docx（降级、未写缓存行）→ 无 DB 行可清理，只能由这里回收。
        #    protect 传入 out_path：该文件即将被流式返回，不得在本次回收。
        await _gc_orphan_exports(db, scheme_id, protect={out_path})
        return FileResponse(
            str(out_path), filename=export_filename,
            headers=headers, media_type=_DOCX_MIME)

    # 缓存记录
    # ✅ B5（2026-09-23）：INSERT OR IGNORE + (scheme_id, content_fingerprint) 唯一约束，
    #    使同指纹并发导出只持久化一行（另一并发请求虽仍各自构建 docx，但 os.replace 保证
    #    out_path 不坏，且缓存行不重复），实现严格原子。
    # ✅ 2026-10-06：原子替换失败（``cacheable=False``）时**跳过**缓存写入，
    #    避免 result_path 永久指向 ``.tmp.docx`` 孤儿文件（见上方 warning）。
    if cacheable:
        cache_id = str(uuid.uuid4())
        await _db_exec(
            db,
            "INSERT OR IGNORE INTO export_cache (id, project_id, scheme_id, config_hash, content_fingerprint, cache_key, result_path)"
            " VALUES (?,?,?,?,?,?,?)",
            (cache_id, scheme.get("project_id", ""), scheme_id, config_hash, content_hash,
             f"{scheme_id}_{config_hash[:8]}", str(out_path)),
            what="export_cache 写入(DOCX)")

        # ✅ 缓存保留策略（最多 5 份/方案）：2026-10-03（T1）提取为共享 helper，
        #    与 PDF 分支共用（原内联逻辑逐字迁移，见 _prune_export_cache 注释）。
        await _prune_export_cache(db, scheme_id, protect_path=out_path)

    # ✅ 加固（D1）：把构建期统计到的「跨章借图」并入渲染统计，一并回传
    #    X-Chart-Render-Stats（前端展示 + 用户知情）。加法式合并，既有键不变。
    _merge_borrowed_stats(render_stats, fix_stats)
    headers = {"X-Chart-Render-Stats": json.dumps(render_stats, ensure_ascii=False),
               "X-Cache-Status": "miss", **name_headers}
    if fix_stats:
        headers["X-Fix-Stats"] = json.dumps(fix_stats, ensure_ascii=False)
    # ✅ G12：导出成稿落一条审核留痕（from_status = to_status，只留痕不改状态机）
    await _record_export_review_trace(db, scheme_id, "docx", round_no, export_filename)
    # ✅ 孤儿产物回收（2026-10-06）：顺带清理本方案历史遗留的孤儿产物
    #    （缓存行已陈旧删除 / 上次原子替换失败的 .tmp.docx）。protect 传入
    #    本次产物，避免把它当成孤儿删掉。
    await _gc_orphan_exports(db, scheme_id, protect={out_path})
    return FileResponse(
        str(out_path),
        filename=export_filename,
        headers=headers,
        media_type=_DOCX_MIME)



def _convert_docx_to_pdf(docx_path: str, pdf_path: str) -> str:
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
                pass
            _doc = _word.Documents.Open(docx_abs, ReadOnly=True)
            try:
                _doc.Fields.Update()
            except Exception:
                pass
            try:
                for _ti in range(1, _doc.TablesOfContents.Count + 1):
                    _doc.TablesOfContents.Item(_ti).Update()
            except Exception:
                pass
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
                        pass
            except Exception:
                pass
            try:
                if _word is not None:
                    try:
                        _word.Quit()
                    except Exception:
                        pass
            except Exception:
                pass
            try:
                _doc = None
                _word = None
                _gc.collect()
                _time.sleep(0.3)
            except Exception:
                pass
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
                    check=True, capture_output=True, timeout=120,
                    # Windows 下 soffice 报错信息可能含中文，显式 utf-8 解码避免 GBK 崩溃
                    text=True, encoding="utf-8", errors="replace")
                generated = Path(out_dir) / (Path(docx_abs).stem + ".pdf")
                if generated.exists():
                    if str(generated) != pdf_abs:
                        shutil.move(str(generated), pdf_abs)
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
    """
    import tempfile

    # ✅ 编号统一（2026-09-26 · 显式跨校验器）：PDF 与 DOCX 共用导出前守卫
    await _guard_numbering_consistency(db, scheme_id)

    prep = await _prepare_export(scheme_id, body, db)
    scheme = prep["scheme"]

    config_hash, content_hash = _content_fingerprint(prep)
    # ✅ D-2（2026-10-01）：PDF 与 DOCX **必须区分缓存键**。
    #    export_cache 的唯一索引是 (scheme_id, config_hash, content_fingerprint)，
    #    不含格式维度 —— 若两者共用同一 content_hash，DOCX 与 PDF 会互相覆盖
    #    （后写入者因 INSERT OR IGNORE 被忽略，PDF 永远写不进缓存）。
    #    这里只给 **PDF** 的指纹加格式后缀：DOCX 现有键逐字不变 → 既有缓存
    #    **零失效**（无需用户重新导出一遍），是新格式平铺接入的最小改动。
    pdf_content_hash = content_hash + "|fmt=pdf"

    # 导出文件名 = 方案名称 + 导出日期 + 导出轮次（PDF 与 DOCX 共用同一轮次计数，
    # 这里内部的中间 DOCX 不额外计数，避免一次 PDF 导出消耗两轮）
    round_no = await _bump_export_round(db, scheme_id)
    export_filename = _build_export_filename(scheme.get("name"), "pdf", round_no)
    name_headers = {**_export_name_headers(export_filename, round_no),
                    **_export_audit_headers(prep.get("content_audit") or {}),
                    **_global_facts_status_headers(prep.get("global_facts_status"))}

    # 缓存检查（与 DOCX 同口径：命中则秒级返回，跳过最昂贵的 PDF 转换）
    cur = await db.execute(
        "SELECT result_path FROM export_cache WHERE scheme_id=? AND config_hash=? AND content_fingerprint=?",
        (scheme_id, config_hash, pdf_content_hash))
    cached = await _db_fetch_one(cur, what="export_pdf 缓存查询")
    if cached and cached[0] and Path(cached[0]).exists() and Path(cached[0]).stat().st_size > 0:
        await _record_export_review_trace(db, scheme_id, "pdf", round_no, export_filename)
        return FileResponse(
            str(cached[0]), filename=export_filename, media_type=_PDF_MIME,
            headers={"X-Chart-Render-Stats": '{"cached":true}',
                     "X-Cache-Status": "hit", **name_headers})

    # 生成 DOCX 到临时文件
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_docx = str(Path(tmpdir) / f"{scheme_id}.docx")
        tmp_pdf = str(Path(tmpdir) / f"{scheme_id}.pdf")
        fix_stats = await asyncio.to_thread(_build_docx_sync, *_build_docx_task(tmp_docx, prep))
        # 转换为 PDF（CPU 密集，放线程池）
        try:
            await asyncio.to_thread(_convert_docx_to_pdf, tmp_docx, tmp_pdf)
        except RuntimeError as e:
            raise HTTPException(501, str(e))

        # 读取 PDF 内容返回（临时目录会被清理，所以必须读入内存）
        with open(tmp_pdf, "rb") as f:
            pdf_bytes = f.read()

    # ✅ 坏缓存守卫（D-2）：与 DOCX 同口径 —— 图表渲染失败 / AI 配图未生成 /
    #    配图位图下载失败时产物缺图，**不写缓存**，否则服务恢复后因指纹不变
    #    永远命中残缺 PDF（旧实现 PDF 链完全没有这道守卫）。
    render_stats = prep.get("render_stats") or {}
    degraded = (render_stats.get("failed", 0) > 0
                or prep.get("ai_image_pending", 0) > 0
                or prep.get("ai_image_download_pending", 0) > 0)

    if not degraded:
        import os as _os
        # 落盘到 EXPORTS_DIR（缓存行只记路径，与 DOCX 一致；临时目录会被清理）
        out_path = EXPORTS_DIR / f"{scheme_id}_{config_hash[:8]}_{pdf_content_hash[:8]}.pdf"
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
            _tmp_pdf.write_bytes(pdf_bytes)
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
                    scheme_id[:8], out_path.name)
                _tmp_pdf.unlink(missing_ok=True)
            else:
                await _db_exec(
                    db,
                    "INSERT OR IGNORE INTO export_cache (id, project_id, scheme_id, config_hash,"
                    " content_fingerprint, cache_key, result_path) VALUES (?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), scheme.get("project_id", ""), scheme_id,
                     config_hash, pdf_content_hash,
                     # ✅ P1（2026-10-04）：cache_key 补格式后缀。
                     #    旧实现 DOCX 与 PDF 写入**逐字相同**的 cache_key
                     #    （export.py:4590 与此处），而 cache_key 是 `/cache-status`
                     #    与运维排查按格式辨识缓存行的唯一人读标识 —— 同一方案同一
                     #    指纹下两行完全同名，无法区分哪个是 docx、哪个是 pdf，
                     #    清理/核对时极易误删。只改 PDF 侧：DOCX 现有键逐字不变 →
                     #    既有缓存行零失效（与 D-2 的「最小改动」取舍一致）。
                     #    注意：查缓存走 (scheme_id, config_hash, content_fingerprint)
                     #    三元组，cache_key 不参与命中判定 → 本次改名不影响任何命中。
                     f"{scheme_id}_{config_hash[:8]}|pdf", str(out_path)),
                    what="export_cache 写入(PDF)")
                await db.commit()
                # ✅ T1（2026-10-03）：PDF 分支此前只 INSERT 从不裁剪 → export_cache 行
                #    与 EXPORTS_DIR 磁盘文件随导出次数无限增长。接入与 DOCX 同一保留
                #    策略（跨格式合计 5 份/方案）；置于既有 fail-soft try 内，裁剪失败
                #    不得影响本次交付（用户仍拿到完整 PDF）。
                await _prune_export_cache(db, scheme_id, protect_path=out_path)
        except Exception as e:
            # 缓存写入失败不得影响本次交付（用户仍拿到完整 PDF）
            try:
                _tmp_pdf.unlink(missing_ok=True)
            except Exception:
                pass
            logger.warning("PDF 导出缓存写入失败（不影响本次交付）: %s", e, exc_info=True)

    from fastapi.responses import Response
    # ✅ 加固（D1）：PDF 链路同样合流跨章借图计数（与 DOCX 同口径）
    _merge_borrowed_stats(render_stats, fix_stats)
    headers = {
        "Content-Disposition": _attachment_disposition(export_filename),
        "X-Chart-Render-Stats": json.dumps(render_stats, ensure_ascii=False),
        # ✅ 与 DOCX 对齐：补 X-Cache-Status，缺图时标记 degraded（旧实现完全没有）
        "X-Cache-Status": "degraded" if degraded else "miss",
        **name_headers,
    }
    if fix_stats:
        headers["X-Fix-Stats"] = json.dumps(fix_stats, ensure_ascii=False)
    # ✅ G12：PDF 成稿同样落一条审核留痕
    await _record_export_review_trace(db, scheme_id, "pdf", round_no, export_filename)
    # ✅ 孤儿产物回收（2026-10-06）：PDF 响应体是**内存字节**，本次产物不再
    #    依赖磁盘文件，因此这里无需 protect。覆盖三类残留：
    #      · os.replace 成功但 INSERT 抛异常 → out_path 在盘上、DB 无行（永久孤儿）；
    #      · 缓存行已陈旧被 _prune_export_cache 删除的文件；
    #      · 历史遗留的 .tmp.* 临时产物（超过宽限期）。
    await _gc_orphan_exports(db, scheme_id)
    return Response(
        content=pdf_bytes,
        media_type=_PDF_MIME,
        headers=headers,
    )


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
