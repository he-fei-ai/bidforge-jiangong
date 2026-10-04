"""正文同步图表：正文生成时由 AI 内嵌图表代码块，此处负责提取、校验、修复与登记。

设计（与正文生成链路一体）：
- content_generation_system 提示词指示正文 AI 在合适位置直接内嵌 ```mermaid / ```chart-json 代码块
  （配图判定、Mermaid/JSON 生成、插入位置由正文 AI 一体完成，格式与正文相同）；
- 本模块在章节持久化时提取内联图表 → 校验/修复 → 登记到 chart_predictions
  （图表清单、导出指纹缓存、管理面板等既有链路继续生效）；
- 导出 DOCX/PDF 时渲染引擎按内联代码块渲染成图（export.py），正文阶段不渲染图片。

已移除（2026-09-13 优化）：正文后置的独立图表管线（Phase2 预测 → Phase3 生成 → Phase4 插标记）
与手动编排接口（charts.predict / place / remove）——图表编排职责完全并入正文生成。
"""
import json
import logging
import re
import uuid

from app.services.ai.image_engine import repair_mermaid, validate_mermaid
from app.services.chart_payload import build_chart_envelope
from app.services.chart_validators import (
    MERMAID_KEYWORD_TO_CHART_TYPE,
    detect_mermaid_chart_type,
    infer_chart_type_from_payload,
    normalize_architecture_tree,
    normalize_flowchart_data,
    validate_architecture_tree,
    validate_comparison_data,
    validate_flowchart_data,
    validate_gantt_plan,
    validate_labor_data,
    validate_layout_data,
    validate_timeline_data,
)
from app.services.content_blocks import (
    INLINE_CHART_FENCE_LANGS as _CONTENT_BLOCKS_INLINE_CHART_FENCE_LANGS,
)
from app.services.content_blocks import (
    MAX_INLINE_CODE_BLOCK_LINES as _CONTENT_BLOCKS_MAX_INLINE_CODE_BLOCK_LINES,
)

# ✅ 2026-09-27（T-2）：围栏工具已下沉到 services.content_blocks，
#    此处转出以保持本模块命名空间（内部上百处引用 + 既有测试 import 不变）。
#    连同下方两个常量一起**从唯一实现转发**，杜绝"下沉后残留第二份副本"的分叉。
from app.services.content_blocks import (  # noqa: E402
    parse_fence_line,
    read_fenced_block,
)

logger = logging.getLogger("chart_pipeline")

# 白名单 7 类
_ALL_CHART_TYPES = {"flowchart", "gantt", "architecture", "labor", "comparison", "layout", "timeline"}
# JSON 数据型图表（渲染器消费结构化 JSON 而非 Mermaid 语法）
# ✅ BUG 修复：原集合只有 {"layout", "timeline"}，而正文里的 ```chart-json 围栏
#    还可能是 labor / architecture —— 提示词明确要求这两类也以内联数据块形式出现。
#    它们此前落到 validate_mermaid：一段 JSON 必然被判为"不支持的 Mermaid 类型"，
#    随即触发"正则修复 → 仍失败 → **从正文删除该代码块**"，
#    于是 AI 生成的四合一劳动力图 / 组织架构图数据被整块删除（正文里凭空少一张图）。
# ✅ 补 gantt（2026-09-15）：渲染器（mermaid_gantt）本就支持 GanttPlan JSON
#    （实测 0.34s 画出 136KB），但 gantt 不在本集合里 → 正文里的 gantt 数据块
#    被当成 Mermaid 语法校验 → 必然失败 → 整块删除。甘特图是提示词里**优先级最高**
#    的图表类型（gantt > architecture = flowchart > …），这个缺口影响最大。
_JSON_CHART_TYPES = {"architecture", "labor", "layout", "timeline", "comparison", "gantt", "flowchart"}

# JSON 图表类型 → 专用校验器。表内没有的类型（如 comparison）按"结构上是 JSON 对象
# 即放行"处理（见下方 validator is None 分支）：宁可交给渲染器出图/出占位，
# 也不要因为"没有校验器"而把正文里的图表数据整块删掉。
# 提取为模块级常量：① 避免每次调用重建 dict；② 可被测试断言注册状态——
# gantt 曾因不在表里而被当成 Mermaid 语法校验、必然失败、整块删除。
# ✅ 2026-09-17 增补 comparison：此前它不在表内（一律放行），而渲染器对
#    「无 rows/items/data」「数值全为 0」的载荷返回 None —— 实测 3 例
#    （空对象仅 type / 只有 title+note / items 全 0）走的是「校验放行 → 渲染 None
#    → 导出红字占位」的 ⚠ 占位型错配。现由 validate_comparison_data 与渲染器
#    **共用同一个归一器**（normalize_comparison_data）闭环：放行 ⇔ 必出图。
_JSON_VALIDATORS = {
    "architecture": validate_architecture_tree,
    "labor": validate_labor_data,
    "layout": validate_layout_data,
    "timeline": validate_timeline_data,
    "gantt": validate_gantt_plan,
    "comparison": validate_comparison_data,
    # ✅ 2026-09-18：施工/工艺横向流程图 chart-json 载荷（steps/edges/variant）
    "flowchart": validate_flowchart_data,
}

# ✅ 修复：本模块此前自带一份只有 8 条的关键字映射，与 export.py（20 条）分叉 ——
# mindmap / journey / classDiagram / erDiagram / quadrantChart / gitGraph /
# sankey / block / kanban 的内联图表因此**从不被登记**进 chart_predictions：
# 前端预览能看到、导出文档里也有，但「图表清单」和导出预检里查不到它，
# 用户既无法定位、也无法用 AI 修复。现统一使用 chart_validators 的唯一映射表。
_MERMAID_TYPE_MAP = MERMAID_KEYWORD_TO_CHART_TYPE

# ✅ 程序级配图上限（对齐 OpenBidKit 编排 Agent 的"程序拍板"原则：
# AI 只负责提名，程序负责全局上限）。仅在正文生成链路启用（enforce_limits=True），
# 单测/手工链路默认关闭以保持既有行为：
# - 每章最多 1 个图表（提示词"每章最多 1 个"的程序级兜底）；
# - 同类型全方案不超过限额（提示词"同一类型全方案不超过 3 次"；ai_image 放宽到 6，
#   对齐 OpenBidKit 的 ai limit=6）。
_CHART_PER_SECTION_LIMIT = 1
_CHART_SCHEME_TYPE_DEFAULT_LIMIT = 3
_CHART_SCHEME_TYPE_LIMITS = {"ai_image": 6}

# ✅ BUG 修复（2026-09-22，幽灵图口径分叉）：单代码块最大行数保护阈值。
#    旧实现本模块内部私有 500 行，而导出侧（export.py）另有一份 8000 **字符**上限，
#    单位与阈值都不同：一份 500 行 / 约 3000 字符的超长围栏会被**登记侧**判为
#    "未闭合 → 跳过登记"，却在**导出侧**正常解析并渲染成图 —— chart_predictions 里
#    查不到它，导出预检与图表清单也统计不到，用户既无法定位也无法修复（幽灵图）。
#    ✅ 2026-09-27（T-2 下沉后的收口）：真正消费该阈值的是
#    `content_blocks.read_fenced_block` 的默认参数 `max_lines`，而本模块的
#    `MAX_INLINE_CODE_BLOCK_LINES = 500` 是**下沉时残留的第二份副本** —— 注释声称
#    "唯一常量"，实际存在两个可独立修改的来源，任一侧被改都会让登记/导出/改写
#    三侧再次分叉（正是本行注释要杜绝的那个 BUG）。现改为**从 content_blocks 转发**，
#    保持本模块命名空间（既有测试与上百处引用照常 import 不变），
#    同时保证"改一处即三侧同生效"。
MAX_INLINE_CODE_BLOCK_LINES = _CONTENT_BLOCKS_MAX_INLINE_CODE_BLOCK_LINES
# 所有可能被"AI 截断 → 围栏未闭合"的图表家族围栏（登记侧 + 导出侧共用）
# ✅ 同样转发自 content_blocks，消除第二份副本。
INLINE_CHART_FENCE_LANGS = _CONTENT_BLOCKS_INLINE_CHART_FENCE_LANGS

# 围栏内行尾出现中文句读 = 该"代码块"实际混入了正文段落（未闭合块吞正文的特征）。
# 图表代码行（Mermaid 语句 / JSON）不会以句读收尾；与导出侧
# _parse_content_blocks 的"以句读行还原正文"启发式共用同一判据。
_CJK_TAIL_RE = re.compile(r"[，。；：！？…]\s*$")

# ---------- 围栏行解析（唯一事实来源，2026-09-24） ----------
# 独立成行的围栏标记：最多 3 个前导空白 + 反引号/波浪号 ≥ 3 个 + 可选语言标签。
# 与 content_utils._FENCE_LINE_RE 保持同口径，避免"检测侧/登记侧/导出侧"再次分叉。
_FENCE_LINE_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})([^`~\n]*)$")


def is_fence_line(line: str) -> bool:
    """该行是否为代码围栏行（≥3 个反引号或波浪号，最多 3 个前导空白）。"""
    return parse_fence_line(line) is not None


def is_chart_fence_lang(lang: str) -> bool:
    """该围栏语言标签是否属于图表家族（mermaid / chart-json / ai_image）。"""
    return lang.strip().lower() in INLINE_CHART_FENCE_LANGS




def iter_inline_chart_fences(content: str) -> list[tuple[str, str, str, int]]:
    """扫描正文中全部**图表家族**代码围栏，返回 ``[(lang, code, state, ordinal)]``。

    - ``lang``    ：围栏语言标签（已小写）
    - ``code``    ：围栏内的正文（未 strip，交由调用方按需处理）
    - ``state``   ：见 read_fenced_block（closed / recovered / eof / truncated）
    - ``ordinal`` ：该围栏在所有图表家族围栏中的**出现序号**（含未闭合/空块/未知类型，
      从 0 递增）。这是正文中的稳定定位锚点，供 `_apply_chart_fence_edits` 精确
      改写单个块，避免按代码内容匹配而误伤内容相同的其它块。

    ✅ 唯一事实来源（2026-09-24）：登记侧 ``_scan_inline_charts``、导出侧
    ``export._parse_content_blocks``、判定侧 ``has_inline_charts`` 一律经由本函数，
    避免"什么算图表围栏"在三侧各自实现而再次分叉（幽灵图的历史根因）。
    """
    if not content:
        return []
    lines = content.split("\n")
    items: list[tuple[str, str, str, int]] = []
    i = 0
    while i < len(lines):
        pf = parse_fence_line(lines[i])
        if pf is None:
            i += 1
            continue
        open_char, open_len, lang = pf
        lang = lang.strip().lower()
        i += 1
        code_lines, state, i = read_fenced_block(
            lines, i, open_char=open_char, open_len=open_len)
        if not is_chart_fence_lang(lang):
            # 非图表家族围栏（python/json/text……）：既不算图表，也整体跳过，
            # 不能让它的闭合行被误当作本模块的扫描边界。
            continue
        items.append((lang, "\n".join(code_lines), state, len(items)))
    return items


def _scan_inline_charts(content: str) -> list[tuple[str, str]]:
    """扫描正文中**全部**内联图表块，返回 [(chart_type, code)]（按出现顺序，不去重）。

    ✅ BUG 修复（同类型第 2 个块彻底脱离管线）：
      旧实现只有一个"扫描 + 按类型去重"的入口，`register_inline_charts` 直接消费
      去重结果，导致**同类型的第 2、3 个图表块从不进入校验/修复/限额判定**：
        · 程序级上限"每章最多 1 个图表"对同类型重复块完全失效（照样进正文、照样导出）；
        · 第 2 个块若是语法非法的 Mermaid，不会被修复、也不会被删除 →
          导出文档出现"渲染失败"红字占位，违背"宁缺勿滥"设计；
        · 图表清单与导出预检只统计第 1 个块，用户无法定位多出来的那张图。
      现把"扫描"与"去重"拆开：扫描产出全部块（供校验/修复/限额/裁剪使用），
      去重只作为**对外清单口径**保留（见 extract_inline_charts）。
    """
    if not content:
        return []
    return [(ct, code) for ct, code, _ord in _scan_chart_fences_full(content)]


def _scan_chart_fences_full(content: str) -> list[tuple[str, str, int]]:
    """扫描全部**可判定类型**的内联图表，返回 ``[(chart_type, code, ordinal)]``。

    与 ``_scan_inline_charts`` 的区别：额外带出围栏在正文中的出现序号 ``ordinal``
    （见 iter_inline_chart_fences），供 ``_apply_chart_fence_edits`` 精确定位改写。
    """
    results: list[tuple[str, str, int]] = []
    for lang, code_raw, state, ordinal in iter_inline_chart_fences(content):
        if state in ("truncated", "eof"):
            # 未闭合块：不提取（含后续正文的坏块不应被当作合法图表）
            # ✅ 修复（2026-10-03 · 未闭合=不是图 · 幽灵登记收口）：旧实现只跳
            #    truncated —— 直到文档末尾仍无闭合围栏的残片（其后没有正文，
            #    truncated 从不触发，state 恒为 eof）照样被登记进 chart_predictions，
            #    而导出侧对未闭合 mermaid 块一律跳过 → 「清单里有、成稿里没有、
            #    绕过每章≤1/同类型≤3 配图上限」的幽灵登记。导出侧 chart-json/
            #    ai_image 的 eof 漏检也已同批收口（content_blocks._parse_content_blocks），
            #    登记/导出/改写三侧统一为「未闭合 = 不是图」。
            continue
        code = code_raw.strip()
        if not code:
            continue
        if lang == "mermaid":
            # ✅ 唯一映射表（跳过 %% 注释行）；未识别的关键字不登记，
            #    与"未知类型不进图表清单"的既有语义保持一致。
            ct = detect_mermaid_chart_type(code, default="")
            if ct:
                results.append((ct, code, ordinal))
        elif lang == "ai_image":
            # ✅ AI 文生图（对齐 OpenBidKit 的 ai 插图类型）：正文内嵌占位块，
            #    此处仅登记为 pending；真实图片在导出 DOCX 时由
            #    export._auto_generate_ai_image_blocks 自动生成（图表全自动，
            #    无人工生图入口）。见 AGENTS.md §4.3。
            results.append(("ai_image", code, ordinal))
        elif lang == "chart-json":
            # ✅ BUG 修复（2026-09-27 · 幽灵图 / 登记-导出口径分叉）：
            #    旧实现**只认载荷里显式写的 `type` 键**，缺失/非法即整块跳过登记。
            #    而导出侧 `content_blocks._parse_content_blocks` 对同一块会调用
            #    `infer_chart_type_from_payload` **按结构兜底推断**类型
            #    （root/children→architecture、tasks→gantt、steps/edges→flowchart …）。
            #    两侧口径分叉的直接后果（实测探针确认）：
            #      · 一个**不带 type 字段**的合法 chart-json 块（AI 很常见地省略它），
            #        导出时被正常解析成 chart 块 → 渲染成图 → **占用图号**；
            #      · 但 `chart_predictions` 里**查无此图** → 不进「图表清单」、
            #        不进导出预检、用户无法定位也无法用 AI 修复；
            #      · 更糟：它**绕过了「每章 ≤1」与「同类型全方案 ≤3」的配图上限**
            #        （上限判定全部发生在登记侧），AI 可以无限塞图撑爆版面。
            #    即"成稿有图、系统查不到、限额失效"三重错配，与本模块反复修过的
            #    幽灵图同源。修法：登记侧改用**与导出侧同一个推断函数**，
            #    让「能不能渲染」在两侧只有一个判据（单一事实来源）。
            try:
                obj = json.loads(code)
            except (json.JSONDecodeError, TypeError):
                logger.warning("内联 chart-json 解析失败，跳过")
                continue
            if not isinstance(obj, dict):
                continue
            ct = infer_chart_type_from_payload(obj)
            if ct and ct in _ALL_CHART_TYPES:
                results.append((ct, code, ordinal))
            else:
                # 推断不出类型 = 结构上不是可渲染图表，与导出侧「整块跳过」同口径。
                # 绝不静默丢弃：留 INFO 便于排查"正文有 chart-json 围栏却没出图"。
                logger.info(
                    "内联 chart-json 结构无法判定为可渲染图表（type=%r），跳过登记",
                    str(obj.get("type", "") or "").strip() or "(缺失)")
    return results


def extract_inline_charts(content: str) -> list[tuple[str, str]]:
    """从正文中提取内联图表，返回 [(chart_type, code)]（**每类型保留第一个**）。

    支持：
    - ```mermaid 围栏：按首关键字推断类型（跳过 %% 注释行）
    - ```chart-json 围栏：JSON 的 "type" 字段即类型（labor/layout 等）
    - ```ai_image 围栏：AI 文生图占位

    本函数的"同类型去重"是**对外清单口径**（图表清单 / 导出预检按类型统计）。
    需要"全部块"（校验、修复、限额、裁剪）请用 `_scan_inline_charts` ——
    两者拆分的缘由见 `_scan_inline_charts` 的 BUG 说明。
    """
    seen: set[str] = set()
    deduped: list[tuple[str, str]] = []
    for ct, code in _scan_inline_charts(content):
        if ct in seen:
            continue
        seen.add(ct)
        deduped.append((ct, code))
    return deduped


def has_inline_charts(content: str) -> bool:
    """正文是否已含内联图表块（三种围栏：mermaid / chart-json / ai_image）。

    ✅ 修复：原实现漏判 ```ai_image —— 该围栏同样会被 `_scan_inline_charts`
    识别并登记为 chart_predictions(chart_type='ai_image')，是**一等图表类型**，
    漏判会让「只有 AI 配图的章节」被调用方误认为无图（如导出/清单口径分叉）。

    ✅ BUG 修复（2026-09-24）：原实现是**子串匹配**，有两个口径漏洞：
      · 子串出现在**其他围栏内部**（如 ```python 代码示例里写着 ````mermaid````）
        也会返回 True，而登记侧根本不会把它当图表 → 调用方误判"本章有图"；
      · 子串匹配无法区分"真图表围栏"与"嵌套在长围栏内的示意文本"。
    现改为经由唯一事实来源 iter_inline_chart_fences，与登记/导出侧必然一致。

    ✅ 口径收紧（2026-10-03 · R38 遗留收口）：「未闭合 = 不是图」的第四处统一。
    登记侧 _scan_chart_fences_full / 导出解析侧 _parse_content_blocks /
    改写侧 _apply_chart_fence_edits 均已跳过 eof/truncated，本函数此前仍按
    「围栏存在」计数 —— 若未来被用于「本章是否已有图 → 是否补生成」的粗判，
    未闭合残片会被误计为已有图。现仅**闭合且可判定**的图表围栏返回 True；
    当前生产零消费点（仅护栏测试引用），收紧不改变任何现有行为。
    """
    return any(state not in ("truncated", "eof")
               for _lang, _code, state, _ord in iter_inline_chart_fences(content))


def _rewrite_code_block(content: str, old_code: str,
                        new_code: str | None) -> str:
    """按围栏替换/删除指定代码块（new_code=None 时删除）

    ✅ 三侧口径统一（2026-09-23）：围栏读取改用与登记侧同源的共用扫描器
    `read_fenced_block`，因而同样具备"超长但闭合"的有界前视恢复能力。
    旧实现只对 "未闭合"（>行数上限）一律原样保留，无法匹配/删除一份
    500<行数≤1000 的合法超长块（与登记侧判定不一致）：当 build_inline_chart_plan
    需修复/删除该块时，`_rewrite_code_block` 找不到它→既不写回修复码也不删，
    导致正文留坏图、 chart_predictions 却存了修复码的双向分叉。
    现："truncated"（真正未闭合）原样保留、不参与改写；其余（含 recovered/eof/closed）
    按块内容匹配后替换/删除，与 extract 的"跳过未闭合块"语义一致。
    """
    if not content:
        return content
    out: list[str] = []
    lines = content.split("\n")
    i = 0
    changed = False
    # ✅ BUG 修复（2026-09-24，见 parse_fence_line）：围栏识别改用共用解析器，
    #    并且写回时**保留原开围栏的字符与长度**。旧实现两处都有问题：
    #      · `stripped.startswith("```")` + `stripped[3:]` 使 4/5 反引号围栏的
    #        lang 变成 "`mermaid"，匹配不到 old_code → 删除/修复形同虚设，
    #        配图上限（每章≤1、同类型全方案≤3）可被"多写一个反引号"整体绕过；
    #      · 写回一律用 3 个反引号，把 4 反引号围栏降级 → 正文出现新的未闭合围栏，
    #        其后正文被整段吞进代码块（用户可直接观察到的正文破损）。
    while i < len(lines):
        pf = parse_fence_line(lines[i])
        if pf is None:
            out.append(lines[i])
            i += 1
            continue
        open_char, open_len, lang_raw = pf
        fence_line = lines[i]
        lang = lang_raw.strip().lower()
        marker = open_char * open_len
        i += 1
        code_lines, state, i = read_fenced_block(
            lines, i, open_char=open_char, open_len=open_len)
        if state == "truncated":
            # 真正未闭合块：原样保留（含原围栏行），不做任何改写
            out.append(fence_line)
            out.extend(code_lines)
            continue
        code = "\n".join(code_lines).strip()
        if lang in INLINE_CHART_FENCE_LANGS and code == old_code:
            if new_code is not None:
                out.append(f"{marker}{lang}\n{new_code}\n{marker}")
            changed = True
            continue
        out.append(f"{marker}{lang}\n{code}\n{marker}")
    return "\n".join(out) if changed else content


# 引导语尾部特征（"施工工艺流程如下图所示：" / "各阶段人数见下图。" 等）——
# 与 export._LEAD_IN_HINT_RE 同口径，用于"删块 → 同步回收孤儿引导语"。
# 收紧为「必须以 图/表 类引导词收尾」，避免误删"各阶段投入如下："这类正常的
# 列表/表格引导句。
_ORPHAN_LEAD_IN_RE = re.compile(
    r"(?:如下图|见下图|如下图示|如下图所示|见下图所示|如图|图示|详见下图)"
    r"\s*(?:所示)?\s*[:：。]?\s*$")
# 引导语行长度上限（超过即视为正文长句，不删）
_LEAD_IN_MAX_CHARS = 60
# 显然不是"独立引导语段落"的行首标记（标题 / 列表 / 引用 / 表格 / 围栏 / 编号）
# —— 这些行即使以"如下图所示"收尾也不能删（它们是结构元素，删了会破坏层级）。
_LEAD_IN_NOT_PARA_RE = re.compile(
    r"^(?:#{1,6}\s|[-*+•·]\s|>|\||`{3,}|~{3,}"
    r"|\d+(?:\.\d+)*[.)、．\s]|[（(]\s*[\d一二三四五六七八九十]+\s*[）)])")


def _drop_dangling_lead_in(out: list[str]) -> bool:
    """删除 ``out`` 末尾**紧邻**（允许中间空行）的孤儿引导语行。

    ✅ 修复（2026-09-25 · 图文逻辑连贯）：
      图表块因**校验修复失败 / 超配图上限**被从正文删除时，只删代码块会留下
      专为引出该图而写的那句话（"施工工艺流程如下图所示："）——正文出现"见下图"
      却无图的悬空引用，比少一张图更显破绽（与导出侧
      ``export._pop_orphan_lead_in`` 同一口径、同一目的：删图必须删引导语）。

    仅当满足全部条件时删除：
      · 从末尾往前跳过空行后的第一个非空行；
      · 该行是普通段落（不是标题 / 列表 / 引用 / 表格 / 围栏行）；
      · 长度 ≤ 60 字，且以"如下图/见下图/如图所示"类引导语收尾。

    Returns:
        是否执行了删除。
    """
    idx = len(out) - 1
    while idx >= 0 and not out[idx].strip():
        idx -= 1
    if idx < 0:
        return False
    line = out[idx].strip()
    if len(line) > _LEAD_IN_MAX_CHARS:
        return False
    if _LEAD_IN_NOT_PARA_RE.match(line):
        return False
    if not _ORPHAN_LEAD_IN_RE.search(line):
        return False
    del out[idx:]
    return True


def _apply_chart_fence_edits(content: str, edits: dict[int, str | None]) -> str:
    """按围栏序号**精确**改写正文中的图表围栏块（单次遍历，定位无歧义）。

    Args:
        content: 原正文。
        edits: ``{围栏序号: 新代码 or None}``，序号来自
            ``_scan_chart_fences_full`` 的第三个分量（与 iter_inline_chart_fences
            的 ordinal 同源）。``None`` 表示删除整个块（含开/闭围栏），
            前后正文原样保留。

    ✅ BUG 修复（2026-09-24，幽灵图 / 清单与正文分叉）：旧的
    ``_rewrite_code_block(content, old_code, None)`` 是**按代码内容**匹配，
    内容相同的多个块会**全部**被删掉。而限额裁剪（每章≤1）的意图是"保留第 1 个、
    删除第 2 个"——实际结果却是 `chart_predictions` 里登记了 1 张图、正文里一张图
    都没有：导出侧渲染不出来、图表清单/导出预检却能看到它（用户无法定位），
    正是反复出现的"幽灵图"。现改为按序号定位，只动指定的那一个块。

    实现说明：
      · 单次遍历 + 序号自增，与 iter_inline_chart_fences 的 ordinal 严格对应
        （序号计数口径相同：只对图表家族围栏计数，非图表围栏不占号）；
      · 未被改写的围栏**逐行原样回写**（连原始开/闭围栏行一起保留），
        不做任何归一化，避免无关内容被顺手改写；
      · 被改写的块保留原开围栏的字符与长度（4 反引号不会被降级成 3 个）。
    """
    if not content or not edits:
        return content
    lines = content.split("\n")
    out: list[str] = []
    fence_ord = -1
    changed = False
    i = 0
    while i < len(lines):
        pf = parse_fence_line(lines[i])
        if pf is None:
            out.append(lines[i])
            i += 1
            continue
        open_char, open_len, lang_raw = pf
        open_line = lines[i]
        lang = lang_raw.strip().lower()
        marker = open_char * open_len
        i += 1
        code_lines, state, i = read_fenced_block(
            lines, i, open_char=open_char, open_len=open_len)
        # 闭合围栏行（closed/recovered 时下一行就是它）；eof/truncated 时不存在
        close_line = lines[i - 1] if state in ("closed", "recovered") else None

        def _emit_block():
            """把当前围栏原样回写（含开/闭围栏行），不做任何归一化。"""
            out.append(open_line)
            out.extend(code_lines)
            if close_line is not None:
                out.append(close_line)

        if not is_chart_fence_lang(lang):
            _emit_block()
            continue
        fence_ord += 1          # 与 iter_inline_chart_fences 同口径：未闭合块也占号
        if state in ("truncated", "eof"):
            # 未闭合块（含 EOF 残片）：原样保留，不做任何改写
            # ✅ 2026-10-03：与登记侧「未闭合=不是图」同口径 —— 扫描不再产出
            #    eof 序号，此处防御兼做护栏：即使调用方误传 eof 序号，也不能
            #    借修复/删块之名给它补写闭合围栏，把截断残片洗白成合法图表。
            _emit_block()
            continue
        if fence_ord not in edits:
            _emit_block()
            continue
        new_code = edits[fence_ord]
        if new_code is None:
            changed = True
            # ✅ 2026-09-25：删块必须同时删引导语（"如下图所示："留在正文里却
            #    没有图 = 图文不连贯；与 export._pop_orphan_lead_in 同口径）。
            if _drop_dangling_lead_in(out):
                logger.info("图表块 [%s] 已删除，其孤儿引导语一并移除", lang)
            continue
        out.append(f"{marker}{lang}\n{str(new_code).strip()}\n{marker}")
        changed = True
    return "\n".join(out) if changed else content


def _validate_inline_chart(chart_type: str, code: str) -> tuple[bool, str]:
    """内联图表校验：mermaid 语法校验 → 正则修复 → 再校验；
    chart-json 按类型专用校验。

    Returns: (是否有效, 修复后代码；修复失败时第二项为 "")。

    ✅ 修复要点：JSON 数据载荷与 Mermaid 语法载荷按**代码形态**分流，
    与渲染器的 `code.startswith("{")` 判定保持一致。
    """
    # ✅ 修复：按**载荷形态**分流，而不是仅凭 chart_type。
    #    同一个 chart_type 可能来自两种围栏：```mermaid pie → comparison、
    #    ```chart-json {"type":"comparison"}。仅按类型分流会把 Mermaid 语法的
    #    pie/xychart 图当成 JSON 去 json.loads → 必然失败 → 删块。
    #    渲染器本身就是按 `code.startswith("{")` 分流的，这里与它保持一致。
    if chart_type in _JSON_CHART_TYPES and code.lstrip().startswith(("{", "[")):
        try:
            obj = json.loads(code)
        except (json.JSONDecodeError, TypeError):
            return False, ""
        if not isinstance(obj, dict):
            return False, ""
        normalized = dict(obj)
        ms = normalized.get("milestones")
        if isinstance(ms, list):
            normalized["milestones"] = [
                {**m, "title": m.get("title") or m.get("name")} if isinstance(m, dict) else m
                for m in ms]
        # ✅ 架构图载荷形态很多（{label,children} / {root:{...}} / {root:"名称",nodes:[...]}），
        #    校验前按与**渲染器同一套**规则归一，避免"渲染器能画、校验器判非法 → 删块"。
        if chart_type == "architecture":
            normalized = normalize_architecture_tree(normalized) or normalized
        # ✅ 流程图 chart-json 载荷（steps/nodes 别名、edges 三种形态、variant→type）
        if chart_type == "flowchart":
            normalized = normalize_flowchart_data(normalized) or normalized
        validator = _JSON_VALIDATORS.get(chart_type)
        if validator is None:
            # ✅ 无专用校验器的 JSON 类型：结构上是 JSON 对象即放行 —— 宁可交给
            #    渲染器出图，也不要因为"没有校验器"而把正文里的图表数据整块删掉
            #    （旧行为会让这些块落进 mermaid 校验并被删除）。
            #    注：7 类白名单现全部有归一器/校验器（comparison 于 2026-09-17 补齐），
            #    本分支仅作新增类型的"宽进"兜底。
            return True, code
        try:
            is_valid, _ = validator(normalized)
        except Exception as _e:
            # 校验器自身异常不应导致删块（宁保留正文、不误删）
            logger.warning("图表 %s 校验器异常，按通过处理: %s", chart_type, _e)
            is_valid = True
        return (True, code) if is_valid else (False, "")
    # Mermaid：语法校验 → 正则修复 → 再校验
    is_valid, _ = validate_mermaid(code)
    if is_valid:
        return True, code
    repaired = repair_mermaid(code)
    if repaired and validate_mermaid(repaired)[0]:
        return True, repaired
    return False, ""


async def _load_scheme_type_counts(db, scheme_id: str,
                                   exclude_section_id: str) -> dict[str, int]:
    """读取全方案各类型已登记图表数（**排除当前章节**）。

    ✅ P1-3（2026-09-17）：从 `register_inline_charts` 中抽出该查询 —— 它不需要
    写锁，可在写事务之外执行，从而把「计数 + 校验 + 修复」等读/CPU 操作整体移出
    `sse_handlers._persist_section` 的全局写锁临界区（旧实现持锁做完整套计算，
    高并发档下所有章节都在落库点排队，实际并发退化为串行）。

    语义等价：旧实现"先 DELETE 本章登记、再按 scheme_id 计数"，
    现改为"计数时排除本章"——同一章节不会被并发写入时结果一致。
    """
    try:
        cur = await db.execute(
            "SELECT chart_type, COUNT(*) AS n FROM chart_predictions "
            "WHERE scheme_id=? AND section_id<>? GROUP BY chart_type",
            (scheme_id, exclude_section_id))
        # ✅ R13 修复（2026-09-22）：与 apply_inline_chart_plan 内的同名查询同源问题——
        #    全局单连接上 execute() 可能返回 None，直接 .fetchall() 会崩。
        if cur is None:
            logger.warning(
                "配图上限统计：db.execute 返回 None（连接/事务异常），跳过限额")
            return {}
        return {r["chart_type"]: r["n"] for r in await cur.fetchall()}
    except Exception as e:
        logger.warning("配图上限统计失败（本次跳过限额）: %s", e)
        return {}


def build_inline_chart_plan(scheme_id: str, section_id: str, content: str, *,
                            enforce_limits: bool = False,
                            scheme_type_counts: dict[str, int] | None = None,
                            ) -> tuple[str, list[tuple]]:
    """**纯计算**：扫描 → 校验/修复 → 限额裁剪，产出修正后的正文与待写入行。

    不访问数据库（P1-3：可在全局写锁**之外**执行）；返回的 rows 由调用方在
    事务内批量写入（见 `apply_inline_chart_plan`）。

    质量保障规则（与旧实现完全一致）：
    - 每一块都过校验（mermaid 语法 / chart-json 专用校验器）；
    - 校验失败走修复（mermaid 正则修复）；修复成功用修复后代码替换正文并登记；
    - 修复失败则从正文删除该代码块（宁缺勿滥，避免导出坏图）；
    - enforce_limits=True 时启用程序级配图上限（每章≤1、同类型全方案限额）。

    Returns:
        ``(修正后的正文, rows)``；rows 每项为 ``INSERT INTO chart_predictions``
        的参数元组（不含 commit）。
    """
    # ✅ P0 类型防御（2026-09-29 · 「内联图表登记失败: tuple has no split」在线事故）：
    #    上游 auto_fix_unclosed_fences 返回 (str, list[dict])，若调用方漏解包直接透传，
    #    content 就会是 tuple，content.split("\n") 抛 AttributeError。
    #    根因同构于 sse_handlers.py:4870 的 auto_fix 返回契约修复（「改一处漏一处」）。
    #    此处作为**最终防线**：若 content 非 str，尝试 tuple/list 取首元素、bytes 解码；
    #    彻底无法恢复时降级为空字符串并打 WARNING + exc_info，便于回溯上游调用点。
    if not isinstance(content, str):
        if isinstance(content, (tuple, list)) and content:
            logger.warning(
                "build_inline_chart_plan: content 非 str（type=%s），尝试取首元素修复；"
                "scheme=%s section=%s",
                type(content).__name__, scheme_id[:8], section_id[:8])
            content = content[0] if isinstance(content[0], str) else str(content[0])
        elif isinstance(content, bytes):
            content = content.decode("utf-8", errors="replace")
        else:
            logger.warning(
                "build_inline_chart_plan: content 非 str 且无法修复（type=%s），"
                "降级为空字符串；scheme=%s section=%s",
                type(content).__name__, scheme_id[:8], section_id[:8],
                exc_info=True)
            content = str(content) if content else ""

    charts = _scan_chart_fences_full(content)
    if not charts:
        return content, []

    counts: dict[str, int] = dict(scheme_type_counts or {})
    rows: list[tuple] = []
    section_chart_count = 0
    batch_type_counts: dict[str, int] = {}
    # ✅ BUG 修复（2026-09-24）：所有正文改写统一累积到 edits（{围栏序号: 新代码/None}），
    #    最后一次性精确应用（见 _apply_chart_fence_edits）。旧实现对每块单独调用
    #    _rewrite_code_block(content, code, None) 按代码内容匹配，内容相同的块会被
    #    一起删掉 —— 限额裁剪本应"留第 1 个、删第 2 个"，结果变成两张都删。
    edits: dict[int, str | None] = {}
    # ✅ 清单口径：同一类型只登记一条（保持 /charts/list 与导出预检的既有语义）。
    #    注意这与"每一块都要过校验/修复/限额"并不矛盾 —— 校验是全量的，
    #    去重只作用于"写入 chart_predictions"这一步。
    registered_types: set[str] = set()
    for ct, code, ordinal in charts:
        # ✅ 配图上限：每章最多 1 个；同类型全方案不超过限额。
        #    超限块直接从正文移除（与"校验失败删块"同一宁缺勿滥语义）。
        if enforce_limits:
            if section_chart_count >= _CHART_PER_SECTION_LIMIT:
                edits[ordinal] = None
                logger.info("章节 %s 图表 [%s] 超出每章 %d 个上限，已移除",
                            section_id[:8], ct, _CHART_PER_SECTION_LIMIT)
                continue
            _limit = _CHART_SCHEME_TYPE_LIMITS.get(
                ct, _CHART_SCHEME_TYPE_DEFAULT_LIMIT)
            _used = counts.get(ct, 0) + batch_type_counts.get(ct, 0)
            if _used >= _limit:
                edits[ordinal] = None
                logger.info("章节 %s 图表 [%s] 全方案已达 %d 个上限，已移除",
                            section_id[:8], ct, _limit)
                continue
        # ✅ AI 文生图（ai_image，对齐 OpenBidKit 的 ai 插图，v17 全自动口径）：正文生成
        #    阶段仅登记占位（status=pending）；真实图片在**导出 DOCX 时**由
        #    export._auto_generate_ai_image_blocks 全自动生成并就地改写为 image 块
        #    （前端无「生成配图」按钮、无 /charts/generate-ai-image 调用，图表全自动，
        #    见 AGENTS.md §4.3）。生成失败的占位块在导出时整块跳过（不占图号、无红字）。
        if ct == "ai_image":
            try:
                _obj = json.loads(code)
                _prompt = str((_obj or {}).get("prompt") or "").strip()
                _title = str((_obj or {}).get("title") or "AI 配图")
            except Exception:
                _prompt, _title = "", "AI 配图"
            if not _prompt:
                edits[ordinal] = None
                logger.warning("章节 %s 内联 ai_image 缺少 prompt，已从正文删除",
                               section_id[:8])
                continue
            _payload = build_chart_envelope(code=code, title=_title, reason="ai_image")
            if not _payload:
                continue
            if "ai_image" in registered_types:
                # 同类型第 2 块：已通过校验，但不重复登记（清单口径）
                continue
            registered_types.add("ai_image")
            rows.append((str(uuid.uuid4()), section_id, scheme_id, "ai_image",
                         _title, 5, "pending", _payload))
            section_chart_count += 1
            batch_type_counts["ai_image"] = batch_type_counts.get("ai_image", 0) + 1
            continue
        ok, fixed = _validate_inline_chart(ct, code)
        if not ok:
            edits[ordinal] = None
            logger.warning("章节 %s 内联图表 [%s] 校验修复失败，已从正文删除",
                           section_id[:8], ct)
            continue
        if fixed != code:
            edits[ordinal] = fixed
            code = fixed
        # ✅ 修复：JSON 数据型图表必须走规范信封的 data 分支。
        #    build_chart_envelope 是写入侧唯一构造器，旧实现一律写
        #    {"mermaid_code": <JSON 文本>}，把结构化数据塞进"代码"键 ——
        #    形状上被误判为 envelope:mermaid，与 chart_payload 的契约相悖，
        #    任何"按载荷形状分流"的消费端都会走错分支。
        #    分流同样按**代码形态**（与 _validate_inline_chart / 渲染器一致），
        #    而不是按 chart_type —— 否则 ```mermaid pie（同为 comparison 类型）
        #    会被当成 JSON 去解析而丢掉。
        if code.lstrip().startswith(("{", "[")):
            try:
                payload_json = build_chart_envelope(
                    data=json.loads(code), title="正文同步生成")
            except (json.JSONDecodeError, TypeError):
                logger.warning("章节 %s 的图表数据块不是合法 JSON，跳过登记",
                               section_id[:8])
                continue
        else:
            payload_json = build_chart_envelope(code=code, title="正文同步生成")
        if not payload_json:
            continue
        # ✅ 配图上限计数：**所有通过校验的图表块**都要计入（含同类型的第 2 块），
        #    否则"每章最多 1 个"对同类型重复块失效。
        section_chart_count += 1
        batch_type_counts[ct] = batch_type_counts.get(ct, 0) + 1
        if ct in registered_types:
            # 同类型第 2 块：已通过校验，但不重复登记（清单口径）
            logger.info("章节 %s 图表 [%s] 同类型重复块，已校验但不重复登记",
                        section_id[:8], ct)
            continue
        registered_types.add(ct)
        # ✅ 状态口径统一（B1 修复）：正文同步生成的图表代码已就绪，统一写
        #    "generated" —— 与 fix-mermaid / generate-ai_image 同一完成态；
        #    export_check 与 preflight_engine 的 CHART_DONE_STATUSES 同时接纳
        #    'done' 与 'generated'，二者语义等价（均为「图表已生成/完成」）。
        rows.append((str(uuid.uuid4()), section_id, scheme_id, ct,
                     "正文同步生成", 5, "generated", payload_json))
    return _apply_chart_fence_edits(content, edits), rows


async def apply_inline_chart_plan(db, section_id: str, rows: list[tuple],
                                  content: str | None = None) -> str | None:
    """在调用方事务内写入图表登记（**不 commit**）。

    ✅ 全量同步：无论本次是否含图，都先清理该章节的历史登记，避免"正文里没有的图"
    残留成僵尸行（导出会把僵尸图追加到章节末尾、导出预检也会把它计为异常）。

    ✅ P1-3（2026-09-17）：本函数只做最小事务（DELETE + 逐行 INSERT），
    CPU 密集的扫描/校验/修复已前移到 `build_inline_chart_plan`（锁外执行）。

    ✅ B2 修复：并发配图上限竞态。build_inline_chart_plan 用「锁外快照」判断全方案
    同类型上限，fast 档多章并行生成时各自基于同一快照通过判定，锁内写入后会超过
    限额。现改为在写锁内（本函数由 sse_handlers._db_write_lock 串行调用）重查当前
    全方案各类型已登记数，按限额逐条写入、超额的图跳过登记，杜绝超发。

    ✅ B2 配套修复（2026-09-18，幽灵图）：锁内复核超限时**正文也必须同步裁剪**——
    旧实现只 `continue` 跳过登记，正文里那张图仍在，导出时会照常渲染，但
    chart_predictions 无登记 → 图表清单/导出预检看不到它（用户无法定位修复）。
    现把 `content` 传入时返回「已删除超限图表块」的正文（不传则行为与旧版一致，
    仅返回 None）。

    Args:
        db: 数据库连接（调用方事务）。
        section_id: 章节 id。
        rows: `build_inline_chart_plan` 产出的 INSERT 参数元组列表。
        content: 可选，当前章节正文；传入时返回裁剪后的正文。

    Returns:
        传入 content 时返回裁剪后的正文；否则返回 None。
    """
    await db.execute("DELETE FROM chart_predictions WHERE section_id=?", (section_id,))
    if not rows:
        return content
    scheme_id = rows[0][2] if len(rows[0]) > 2 else ""
    try:
        cur = await db.execute(
            "SELECT chart_type, COUNT(*) AS n FROM chart_predictions "
            "WHERE scheme_id=? AND section_id<>? GROUP BY chart_type",
            (scheme_id, section_id))
        # ✅ R13 修复（2026-09-22）：全局单连接上，先前的 DELETE 之后 SELECT
        #    可能返回 None（aiosqlite 在连接状态异常 / 事务冲突时）。若 cur 为 None
        #    直接 .fetchall() 会抛 "'NoneType' object has no attribute 'fetchall'"，
        #    在日志里连续出现 24 次（见日志 R13），导致该路径上配图上限复核被静默跳过。
        #    此处显式判空，与下方 except 分支保持同一 fallback（live = {}）。
        if cur is None:
            logger.warning(
                "配图上限事务内复核：db.execute 返回 None（连接/事务异常），"
                "跳过上限复核，按原计划写入")
            live = {}
        else:
            live = {r["chart_type"]: r["n"] for r in await cur.fetchall()}
    except Exception as e:
        logger.warning("配图上限事务内复核失败（按原计划写入）: %s", e)
        live = {}
    running: dict[str, int] = {}
    skipped_types: set[str] = set()
    for row in rows:
        ct = row[3] if len(row) > 3 else ""
        _limit = _CHART_SCHEME_TYPE_LIMITS.get(ct, _CHART_SCHEME_TYPE_DEFAULT_LIMIT)
        if live.get(ct, 0) + running.get(ct, 0) >= _limit:
            logger.info("章节 %s 图表 [%s] 全方案已达 %d 个上限（事务内复核），跳过登记",
                        section_id[:8], ct, _limit)
            skipped_types.add(ct)
            continue
        # ✅ BUG 修复（2026-10-05 · F-3，R13 同构）：aiosqlite 在事务冲突 / 连接
        #    异常时 `await db.execute(...)` 可能返回 None（SELECT 早已在同一函数内
        #    做过 None 兜底），INSERT 却**静默吞掉**——chart_predictions 里缺一条，
        #    但正文里那张图仍在（`_apply_chart_fence_edits` 只在 skipped_types 里
        #    删，本条不属 skipped）。→ **幽灵图**：导出照渲、清单看不见。
        #    现对返回值 + 异常都显式判定，任一失败都补进 skipped_types：
        #    下一轮正文裁剪会精确定位并删除该块，保持落库与正文两侧一致。
        try:
            _res = await db.execute(
                "INSERT INTO chart_predictions "
                "(id, section_id, scheme_id, chart_type, needed, purpose, "
                "priority, status, data_json) VALUES (?,?,?,?,1,?,?,?,?)", row)
        except Exception as _e:
            logger.warning(
                "章节 %s 图表 [%s] 事务内 INSERT 失败，已降级为跳过登记（正文将同步裁剪）: %s",
                section_id[:8], ct, _e)
            skipped_types.add(ct)
            continue
        if _res is None:
            logger.warning(
                "章节 %s 图表 [%s] INSERT 返回 None（连接/事务异常），"
                "已降级为跳过登记（正文将同步裁剪）",
                section_id[:8], ct)
            skipped_types.add(ct)
            continue
        running[ct] = running.get(ct, 0) + 1
    if content is not None and skipped_types:
        # ✅ BUG 修复（2026-09-24）：改为按围栏序号精确删除（_apply_chart_fence_edits）。
        #    旧实现按代码内容匹配，内容相同的块会被一起删掉，可能误删**其它类型**
        #    但内容恰好相同的块；现在只删本轮复核判定超限的那几块。
        _edits = {ord_: None for ct, _code, ord_ in _scan_chart_fences_full(content)
                  if ct in skipped_types}
        if _edits:
            content = _apply_chart_fence_edits(content, _edits)
        logger.info("章节 %s 超限图表类型 %s 已同步从正文移除（避免幽灵图）",
                    section_id[:8], sorted(skipped_types))
    return content


async def register_inline_charts(db, scheme_id: str, section_id: str,
                                 content: str, *,
                                 enforce_limits: bool = False) -> tuple[int, str]:
    """把正文中内联的图表登记到 chart_predictions（既有入口，行为不变）。

    ✅ 增强（正文同步图表质量保障）：
    - **全量同步**：先清理该章节的历史登记，再按新正文登记（重新生成后不留僵尸图）；
    - 每块先校验（mermaid 语法 / chart-json 专用校验器）；
    - 校验失败走修复（mermaid 正则修复）；修复成功用修复后代码替换正文并登记；
    - 修复失败则从正文删除该代码块（宁缺勿滥，避免导出坏图）；
    - enforce_limits=True 时启用程序级配图上限（每章≤1、同类型全方案限额）；
    - 返回 (登记数量, 修正后的正文)；不 commit，由调用方与正文更新同事务提交。

    ✅ BUG 修复（同类型第 2 个块脱离管线）：本函数消费 `_scan_inline_charts`
    （**全部块**）而不是 `extract_inline_charts`（按类型去重）。旧实现用去重结果，
    使同类型的第 2、3 个图表块既不被校验/修复（坏图照样进成稿），
    也不被"每章≤1"上限裁剪（上限形同虚设），还不进图表清单。
    现在：**每一块都要过校验/修复/限额**；登记仍保持"每类型只登记一个"
    的清单口径（`registered_types` 去重），不改变 /charts/list 的对外语义。

    ✅ P1-3（2026-09-17）重构：本函数现由
    ``_load_scheme_type_counts`` + ``build_inline_chart_plan`` + ``apply_inline_chart_plan``
    组合而成，语义与旧实现一致。正文生成链路（`sse_handlers._persist_section`）
    已改为直接调用后两者，以便把纯计算移出全局写锁临界区。
    """
    scheme_type_counts: dict[str, int] = {}
    if enforce_limits:
        scheme_type_counts = await _load_scheme_type_counts(db, scheme_id, section_id)
    new_content, rows = build_inline_chart_plan(
        scheme_id, section_id, content,
        enforce_limits=enforce_limits, scheme_type_counts=scheme_type_counts)
    # ✅ 防御（2026-09-22）：apply_inline_chart_plan 在"未传入 content"时返回 None，
    #    而本函数必定传入正文。此处兜底回退到裁剪前的正文，避免返回 None 让
    #    调用方（sse_handlers._persist_section）把整章正文覆盖成空。
    new_content = await apply_inline_chart_plan(db, section_id, rows, new_content) \
        or new_content
    if rows:
        logger.info("章节 %s 同步登记 %d 个内联图表", section_id[:8], len(rows))
    return len(rows), new_content
