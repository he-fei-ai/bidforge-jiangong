"""导出内容校验/审计工具（从 export.py 拆出，2026-10-09）。

纯数据处理：图表 code 兜底、编号一致性、重复章节、交叉引用陈旧检测等。
无 DB / AI / HTTP 依赖，仅依赖正则与少量服务层纯函数。
"""
import logging
import re
from datetime import date

from app.routers._chart_pipeline import INLINE_CHART_FENCE_LANGS
# ⚠️ R86 L3 收敛后本文件不再需要 `json` / `parse_fence_line` / `content_blocks`
#    的 5 个子标题工具 —— 它们此前**只**被那 14 个副本函数体使用。
#    留着即死导入（ruff F401），删掉不改变任何行为（已由差分验证逐项比对）。

# ✅ R86 L3（2026-10-16）：本文件此前**物理复制**了 export.py 的 14 个判据（编号折算 / 排序键 / 重复章节 / 编号错位 / 子标题命名空间冲突 / 陈旧交叉引用 /
#   图表取码 / 未闭合围栏计数 / 审计日志格式化）。实测去掉 docstring 后 14 个函数体 AST **完全一致**，
#   即纯副本 —— 属本仓最高频反模式「同一判据两份副本」：任一侧改动即漂移，且漂移无任何测试能发现。
#   现统一委派到 export（唯一事实源），本文件不再持有副本；函数签名与 docstring 逐字保留，调用方零改动。
#   ⚠️ 同名函数 `audit_content` **刻意不收敛**：本文件版本是能力超集（另加全文级体检），收敛会丢功能。
from app.routers import export as _ex  # ✅ R86 L3：14 个与 export 逐字同源的判据统一委派

logger = logging.getLogger("export.audit")


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
    return _ex._build_chart_type_index(chart_lookup, order_rank)


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
    return _ex._find_fallback_code(chart_type_index, chart_type, current_section_id, order_rank)


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
    return _ex._resolve_chart_code(block, sec_id, chart_type, chart_lookup, chart_type_index, order_rank)


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

# ---------------------------------------------------------------------------
# ✅ 2026-10-10（文档质量报告 修复13/14 · 增强12）：三类专项体检（检测型）。
# 全部**只计数定位、绝不改写正文**（AGENTS.md §3.1.6 红线：改写等于替用户
# 决定内容）。规范配对/术语混用为全文级（跨章共现单章检不出），空话句式
# 全文归并计数。sections 字段在此承载命中片段（前 3 条）而非章节标题。
# ---------------------------------------------------------------------------

#: 规范编号 → 官方名称（高置信小表：只收常见施工规范，宁缺毋滥；
#: 键为无空格大写形态，查表前同样归一）
_STANDARD_TITLES: dict[str, str] = {
    "GB50202": "建筑地基基础工程施工质量验收标准",
    "GB50203": "砌体结构工程施工质量验收规范",
    "GB50204": "混凝土结构工程施工质量验收规范",
    "GB50208": "地下防水工程质量验收规范",
    "GB50300": "建筑工程施工质量验收统一标准",
    "GB50666": "混凝土结构工程施工规范",
    "GB50720": "建设工程施工现场消防安全技术规范",
    "GB55034": "建筑与市政施工现场安全卫生与职业健康通用规范",
    "JGJ18": "钢筋焊接及验收规程",
    "JGJ33": "建筑机械使用安全技术规程",
    "JGJ39": "托儿所、幼儿园建筑设计规范",
    "JGJ46": "施工现场临时用电安全技术规范",
    "JGJ59": "建筑施工安全检查标准",
    "JGJ80": "建筑施工高处作业安全技术规范",
    "JGJ107": "钢筋机械连接技术规程",
    "JGJ120": "建筑基坑支护技术规程",
    "JGJ130": "建筑施工扣件式钢管脚手架安全技术规范",
    "JGJ162": "建筑施工模板安全技术规范",
    "JGJ311": "建筑深基坑工程施工安全技术规范",
}
#: 「编号 + 《名称》」紧跟形态才做配对校验——编号单独出现不校验
#: （读者自会核对，误报面大）
_STANDARD_PAIR_RE = re.compile(
    r"\b((?:GB|JGJ|CJJ)\s*\d{2,5})\s*[《〈]([^》〉\n]{4,60})[》〉]")

#: 同义术语组（报告实证 4 组；「安全生产」组刻意剔除——正式场合两种
#: 表述各有语义侧重，不算混用）
_TERMINOLOGY_MIX_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("外脚手架", ("外架", "外脚手架", "外脚手架体系")),
    ("模板支撑架", ("模板支撑架", "模板支架", "模板支撑体系")),
    ("建设单位", ("建设单位", "业主单位", "业主方")),
    ("出地面", ("出地面", "出正负零", "出±0.000")),
)

#: 空话套话句式（info 级：AI 初稿高频，提示精简；「严格按照」刻意剔除——
#: 工程文档里它是正常表述，检测会满屏误报）
_FILLER_PATTERNS: tuple[tuple[str, str], ...] = (
    ("高度重视", r"高度重视[^。；\n]{0,12}(?:工作|落实|管理|安全|质量)"),
    ("进一步提高", r"进一步(?:提高|加强|完善|强化)[^。；\n]{0,10}"
                   r"(?:水平|能力|管理|工作)"),
    ("切实做好", r"切实(?:做好|加强|落实)[^。；\n]{0,12}"),
    ("奠定基础", r"为[^。；\n]{2,20}奠定(?:了)?(?:坚实|良好)?基础"),
    ("确保万无一失", r"确保[^。；\n]{0,10}万无一失"),
    ("全面提升", r"全面(?:提升|推进|加强)[^。；\n]{0,10}(?:水平|能力|建设)"),
)


def _detect_standard_pairing(content: str) -> dict | None:
    """规范编号-名称配对校验（报告实证：JGJ 39 被写成临时用电规范）。

    只在「编号 + 《名称》」紧跟形态下比对注册表；编号在表中而名称双向
    都不包含 → error（交付文档引用错规范是硬伤）。表中没有的编号不校验
    （宁漏勿误）。
    """
    found: list[str] = []
    for m in _STANDARD_PAIR_RE.finditer(content):
        code = re.sub(r"\s+", "", m.group(1)).upper()
        expected = _STANDARD_TITLES.get(code)
        if not expected:
            continue
        title = str(m.group(2) or "").strip()
        tn, en = _norm_ref_text(title), _norm_ref_text(expected)
        if tn and en and tn not in en and en not in tn:
            found.append(f"{m.group(1)}《{title}》应为《{expected}》")
    if not found:
        return None
    return {"key": "standard_pairing", "level": "error", "count": len(found),
            "label": "规范编号与名称不配对（引用错规范，须人工复核）",
            "sections": found[:3]}


def _norm_ref_text(text: str) -> str:
    """规范名称归一（去空白/标点、全半角统一），供配对比对。"""
    s = str(text or "")
    s = (s.replace("（", "(").replace("）", ")")
          .replace("，", ",").replace("、", "").replace("　", ""))
    return re.sub(r"[\s()（）,，.-]+", "", s)


def _detect_terminology_mix(content: str) -> dict | None:
    """术语混用检测（报告实证：同一对象全文出现多种叫法）。info 级。"""
    mixed: list[str] = []
    for canonical, variants in _TERMINOLOGY_MIX_GROUPS:
        used = [v for v in variants if v in content]
        if len(used) >= 2:
            mixed.append(f"{canonical}: " + " / ".join(used))
    if not mixed:
        return None
    return {"key": "terminology_mix", "level": "info", "count": len(mixed),
            "label": "同一对象多种叫法（建议全文统一术语）",
            "sections": mixed[:3]}


def _detect_filler_phrases(content: str) -> dict | None:
    """空话套话句式检测（报告实证 6 类高频句式）。info 级，提示精简。"""
    total = 0
    samples: list[str] = []
    for label, pattern in _FILLER_PATTERNS:
        cnt = len(re.findall(pattern, content))
        if cnt:
            total += cnt
            if len(samples) < 3:
                samples.append(f"{label}×{cnt}")
    if not total:
        return None
    return {"key": "filler_phrase", "level": "info", "count": total,
            "label": "空话套话句式（建议精简为具体措施）",
            "sections": samples}


# ---------------------------------------------------------------------------
# ✅ 2026-10-10（文档质量报告 修复10/18 · 内容问题5/6）：工期一致性 + 里程碑合理性
# 检测型（**只体检、不篡改**）：报告实证正文声明 617 日历天、甘特图却显示 718 天，
# 且里程碑「二次结构及抹灰(9/30)」晚于「外架及塔吊拆除(10/30)」不合常理。此处扫描
# 正文内部自陈的工期数字与里程碑日期，做一致性/合理性体检，供用户回改源文。
# 全部仅计数定位，绝不改写正文（AGENTS.md §3.1.6 红线）。
# ---------------------------------------------------------------------------

#: 工期声明（只吃「工期/日历天」语境，避免「养护 28 天」「龄期 7 天」误命中）。
_SCHEDULE_TOTAL_RE = re.compile(
    r"(?:总工期|建设工期|合同工期|工期)[^，。；\n]{0,8}?(\d{2,4})\s*(?:日历)?天")

#: 里程碑「名称 日期」：名称 ≤16 字（含中文括号/±/斜杠），日期为
#: 20xx-x-x / 20xx年x月x日 / 20xx.x.x 三种形态。
_MILESTONE_RE = re.compile(
    r"([\u4e00-\u9fa5A-Za-z0-9±（）()\s/、－-]{1,16})"
    r"(20\d{2}[-年/.]\d{1,2}[-月/.]\d{1,2}日?)")
#: 二次结构 / 外架拆除 工序逻辑参照（二次结构及抹灰须在外部脚手架拆除前完成）。
_MILESTONE_SECOND_STRUCTURE = ("二次结构", "砌体", "抹灰")
_MILESTONE_SCAFFOLD_DOWN = ("外架", "脚手架拆除", "架体拆除", "塔吊拆除")


def _parse_milestone_date(text: str) -> date | None:
    """解析里程碑日期（20xx-x-x / 20xx年x月x日 / 20xx.x.x），非法返回 None。"""
    s = str(text or "").strip()
    m = re.match(r"^(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})", s)
    if not m:
        m = re.match(r"^(20\d{2})年(\d{1,2})月(\d{1,2})日?", s)
    if not m:
        return None
    y, mo, d = (int(x) for x in m.groups())
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return None
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def _detect_schedule_conflicts(content: str) -> dict | None:
    """工期声明自相矛盾检测（报告实证：正文 617 日历天 vs 甘特图 718 天）。

    只扫「工期/日历天」语境下的数字，同一份正文若出现 **2 个及以上不同** 总工期
    声明即判矛盾（用户须统一为一个权威值）。命中即 error（交付文档工期不可信是硬伤）。
    """
    found: set[int] = set()
    for m in _SCHEDULE_TOTAL_RE.finditer(content):
        found.add(int(m.group(1)))
    if len(found) <= 1:
        return None
    vals = sorted(found)
    return {"key": "schedule_conflict", "level": "error", "count": len(vals),
            "label": "工期声明自相矛盾（正文出现多个不同总工期，须统一为权威值）",
            "sections": [f"总工期声明：{v}日历天" for v in vals]}


def _detect_milestone_issues(content: str) -> dict | None:
    """里程碑排期合理性检测（报告实证：二次结构晚于外架拆除、间隔过短）。

    提取正文中「名称 + 日期」形态的全部里程碑，校验：
      ① 相邻里程碑间隔是否过短（<7 天，明显属于漏排/错排）；
      ② 工序逻辑：二次结构/砌体/抹灰完成时间是否早于外部脚手架/塔吊拆除。
    全部为 **检测型**（warn 级，提示用户核对），绝不改写正文。
    ⚠️ 不检「全局日期升序」：正文里程碑常按分组/章节罗列而非严格时间轴，
    强制升序会大量误报（违背「正常文档零误报」红线），故只保留确定性强的
    ② 工序逻辑与 ① 相邻过短间隔两类信号。
    """
    hits: list[tuple[str, date]] = []
    for m in _MILESTONE_RE.finditer(content):
        name = m.group(1).strip(" （()）\t")
        d = _parse_milestone_date(m.group(2))
        if d and name:
            hits.append((name, d))
    if len(hits) < 2:
        return None

    issues: list[str] = []
    # ① 相邻间隔过短（<7 天）→ 漏排/错排
    for i in range(len(hits) - 1):
        gap = (hits[i + 1][1] - hits[i][1]).days
        if 0 <= gap < 7:
            issues.append(f"{hits[i][0]}→{hits[i + 1][0]} 间隔仅 {gap} 天，过短")
    # ② 工序逻辑：二次结构须在外部脚手架拆除前
    sec = next((d for n, d in hits
                if any(k in n for k in _MILESTONE_SECOND_STRUCTURE)), None)
    demol = next((d for n, d in hits
                  if any(k in n for k in _MILESTONE_SCAFFOLD_DOWN)), None)
    if sec and demol and sec > demol:
        issues.append("二次结构/抹灰完成时间晚于外架或塔吊拆除，不合常理"
                      "（应在此之前完成）")

    if not issues:
        return None
    return {"key": "milestone_issue", "level": "warn", "count": len(issues),
            "label": "里程碑排期不合理（间隔/工序逻辑，建议核对）",
            "sections": issues[:3]}

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
    return _ex._count_unclosed_chart_fences(content)


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
    # ✅ 2026-10-10（文档质量报告 修复13/14 · 增强12）：三类全文级专项体检
    #    （术语混用/规范配对需全文比照：跨章共现单章检不出）。检测型：
    #    只计数定位、绝不改写正文（AGENTS.md §3.1.6）。
    _full_text = "\n".join(str(sec.get("content") or "") for sec in sections)
    for _probe in (_detect_standard_pairing(_full_text),
                   _detect_terminology_mix(_full_text),
                   _detect_filler_phrases(_full_text),
                   _detect_schedule_conflicts(_full_text),
                   _detect_milestone_issues(_full_text)):
        if _probe:
            hits[_probe["key"]] = _probe
    items = sorted(hits.values(),
                   key=lambda x: (order.get(x["level"], 9), -x["count"]))
    return {"total": sum(x["count"] for x in items), "items": items}


def _format_audit_log(audit: dict) -> str:
    """把体检结果压成一行日志（便于日志里直接看出文档可交付性）。"""
    return _ex._format_audit_log(audit)


def _count_child_namespace_subheadings(blocks: list[dict], section_prefix: str) -> int:
    """统计本节内容中落在「子章节命名空间」的子标题数（编号形如 ``{prefix}.N``）。

    内容子标题经 :func:`_compute_subheading` 编号后形如 ``{section_prefix}.{dotted}``：
    - ``dotted`` 只有 1 段（如 ``1.1``）→ 与本节 **DB 子章节**的编号在同一命名空间
      （DB 子章节同样从 ``{prefix}.1`` 起算），渲染时会发生编号重复；
    - ``dotted`` ≥ 2 段（如 ``1.1.1``）属更深层级，不与子章节直接冲突。

    依赖 ``blocks`` 中已算好的 ``_fixed_text``（write_section 渲染前会先统一计算）。
    """
    return _ex._count_child_namespace_subheadings(blocks, section_prefix)


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
    return _ex._section_sort_key(sec)


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
    return _ex._detect_duplicate_sections(sections)


def _stored_outline_number(sec: dict) -> str:
    """从章节行提取存储态编号（outline_json.id）；非法/缺失返回 ""。"""
    return _ex._stored_outline_number(sec)


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
    return _ex._detect_section_number_mismatch(sections)


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
    return _ex._detect_body_subheading_namespace_conflict(sections)


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
    return _ex._detect_stale_cross_references(sections, real_nums)


# ---------------------------------------------------------------------------
# 编号前缀提取（从 export.py 拆出，供 audit 与正文编号共用）
# ---------------------------------------------------------------------------

def _section_number_prefix(full_title: str) -> str:
    """从已格式化的章节标题中提取编号前缀，用于正文内子标题的嵌套编号。

    例：
        '第一章 施工组织设计' -> '1'
        '1.2 进度计划'       -> '1.2'
        '1.1.1 立面设计'     -> '1.1.1'
        '（一） 设计标准'     -> ''（中文序号前缀无法嵌套，子标题直接用自身序号）
    无编号标题返回 ''。
    """
    return _ex._section_number_prefix(full_title)


def _cn_numeral_to_int(cn: str) -> str:
    """中文数字串转整数（阿拉伯数字）。

    支持纯数字 / 十位组合 / 百位组合 / 千位组合 / 混入阿拉伯数字。
    解析失败时返回原字符串（不做静默吞错）。
    """
    return _ex._cn_numeral_to_int(cn)



