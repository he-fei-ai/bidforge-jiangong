"""导出预检·内容审计检测群（audit_content 与 *_detect_* 静态检测器，自 export.py 按导出阶段拆出，facade 延迟绑定，纯搬移零行为变更）"""

import ast
import hashlib
import inspect
import textwrap

import app.routers.export as _sh  # noqa: F401 — facade 命名空间延迟绑定（monkeypatch 兼容）
from app.services.fence_common import is_fence_close  # ✅ R168：闭合判据唯一事实源（叶子模块，无成环）

_AUDIT_RULES: tuple[tuple[str, str, str, _sh.re.Pattern], ...] = (
    # (key, 人类可读名, 级别, 正则)
    ("placeholder", "数据占位符（××/xx/【待补充】）", "warn",
     _sh.re.compile(r"××+|(?<![A-Za-z0-9])[xX]{2}(?![A-Za-z0-9])|【待补充")),
    ("html_tag", "HTML 标签残留（应使用 GFM 表格）", "error",
     _sh.re.compile(r"</?(?:table|thead|tbody|tr|td|th|div|span|p|br)\b[^>]*>", _sh.re.I)),
    # ✅ 新增：Excel/Word 剪贴板表格占位标记（<fcel>/<lcel>/<ucel>/<xc>/<nl>）——
    #    导出渲染时已降级为纯文本（不再印进成稿），此处仍计入体检供用户回改源文。
    ("clipboard_table", "剪贴板表格残留标记（粘贴表格带入，应改用 GFM 表格）", "error",
     _sh.re.compile(r"</?(?:fcel|lcel|ucel|xc|nl)\s*/?>", _sh.re.I)),
    ("latex_square", "LaTeX 空参数占位 \\square", "error", _sh.re.compile(r"\\square")),
    ("bidding_terms", "投标场景用语（专项方案不应引用）", "warn",
     _sh.re.compile(r"招标文件|投标文件|评标办法|评分标准|废标条件|投标须知")),
    # ✅ R50 新增（2026-10-07）：中英混杂 —— 中文句子里夹连续两个小写英文单词
    #    （如"签署 material acceptance records 后方可"）。规范编号（GB/JGJ 大写）、
    #    化学式（HCHO）、单个英文术语不会误伤。命中即提示用户改回中文。
    ("mixed_language", "中英混杂（中文段落中夹英文短语）", "warn",
     _sh.re.compile(r"[\u4e00-\u9fa5]\s*[a-z]{4,}(?:\s+[a-z]{3,})+\s*[\u4e00-\u9fa5]")),
)
# 图表家族围栏（未闭合即视为生成被截断，残片渲染必失败）——
# 统一定义在 _chart_pipeline.INLINE_CHART_FENCE_LANGS，避免两端分叉。
_CHART_FENCE_LANGS = _sh.INLINE_CHART_FENCE_LANGS


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
        pf = _sh.parse_fence_line(lines[i])
        if pf is None:
            i += 1
            continue
        open_char, _open_len, lang = pf
        lang = lang.strip().lower()
        i += 1
        closed = False
        while i < len(lines):
            # ✅ R168（2026-11-03 · R76 漏改面收口）：闭合判据对齐 fence_common.
            #    is_fence_close（全仓唯一判据，read_fenced_block 自 R76 起即用）：
            #    同种字符 + **信息串必须为空** + 长度容错。旧实现「同种围栏字符即
            #    视为闭合」是 R76 之前的旧口径 —— 「```js」这类带信息串的内容行
            #    会被误判闭合 → 渲染侧实际判未闭合（吞其后正文）而本预检漏计，
            #    导出预检 DLV-07 与渲染行为反向分叉；本函数注释声称的
            #    「与 read_fenced_block 一致」自 R76 起已失真。
            if is_fence_close(lines[i], open_char, _open_len,
                              tolerant_length=True):
                closed = True
                i += 1
                break
            i += 1
        if not closed:
            n += 1
            if lang not in _sh._CHART_FENCE_LANGS:
                _sh.logger.warning(
                    "正文含未闭合的普通代码围栏（lang=%s），"
                    "其后正文存在被当代码块输出的风险", lang)
    return n


def audit_content(sections: list[dict], *,
                  fences_fixed: int = 0) -> dict:
    """扫描各章正文，产出可交付性问题清单（供导出预检 / 响应头 / 日志）。

    返回 ``{"total": int, "items": [...]}``；``items`` 每项含
    key / label / level / count / sections（最多 3 个章节标题，便于定位）。

    ✅ R99（2026-10-17 · 围栏自动修复可观测性）：新增 ``fences_fixed`` 形参。
    开启 ``auto_fix_unclosed_fences`` 后，围栏在体检**之前**就被补齐 →
    ``_count_unclosed_chart_fences`` 恒为 0 → 「未闭合围栏」这条 error 级提示
    在本次导出里**凭空消失**，而用户既看不到任何替代信号、也不知道围栏被改写过。
    实测更严重：补齐后「围栏之后的中文正文」会被并入图表源码整体丢弃
    （默认关闭时解析器会按句读行把正文还原出来，开启后反而丢得更多）。
    现显式产出一条 warn 级提示，如实告知「本次自动补齐了 N 处围栏 + 需人工核对」。
    缺省 0 = 不传该形参时代码路径与改造前**逐字一致**（既有调用方零影响）。
    """
    hits: dict[str, dict] = {}
    for sec in sections:
        content = sec.get("content") or ""
        title = sec.get("title") or ""
        if not content:
            continue
        for key, label, level, rx in _sh._AUDIT_RULES:
            found = len(rx.findall(content))
            if found:
                slot = hits.setdefault(key, {"key": key, "label": label,
                                             "level": level, "count": 0,
                                             "sections": []})
                slot["count"] += found
                if len(slot["sections"]) < 3 and title not in slot["sections"]:
                    slot["sections"].append(title)
        unclosed = _sh._count_unclosed_chart_fences(content)
        if unclosed:
            slot = hits.setdefault("unclosed_fence", {
                "key": "unclosed_fence",
                "label": "代码围栏未闭合（生成疑似被截断，其后正文有丢失风险）",
                "level": "error", "count": 0, "sections": []})
            slot["count"] += unclosed
            if len(slot["sections"]) < 3 and title not in slot["sections"]:
                slot["sections"].append(title)
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
    return {"total": sum(x["count"] for x in items), "items": items}


def _format_audit_log(audit: dict) -> str:
    """把体检结果压成一行日志（便于日志里直接看出文档可交付性）。"""
    if not audit.get("items"):
        return "导出前内容体检：未发现可交付性风险"
    parts = [f"{it['label']} {it['count']} 处" for it in audit["items"]]
    return "导出前内容体检：" + "；".join(parts)


# ---------------------------------------------------------------------------
# ✅ R177（2026-11-04）：审计规则指纹（导出旁车的失效判据）
# ---------------------------------------------------------------------------
# 背景：旁车缓存了 `audit_content` 的结论，规则一改就必须让旧结论失效。
# 此前靠人工 `_AUDIT_SIDECAR_VERSION += 1`，属于"一定会忘"的约定 —— 现改为
# **对规则本身取指纹**，改规则/改判定代码即自动失效旧旁车。
#
# 覆盖面（三者缺一不可，缺任何一个都会让"改规则但指纹不变"成为可能）：
#   1) 规则表 `_AUDIT_RULES` 的 key / 标签 / 级别 / 正则 pattern / flags；
#      —— 经 `_sh` 读取，即 `audit_content` 实际使用的那份绑定（运行时替换同样生效）。
#   2) `audit_content` 的 AST：聚合、去重、排序、`fences_fixed` 分支都在这里。
#   3) `_count_unclosed_chart_fences` 与 `is_fence_close` 的 AST：未闭合围栏
#      这条 error 级的全部判定逻辑（含闭合判据唯一事实源）。
#
# 为什么用 AST 而不是源码文本：注释/空行/换行风格不影响判定结果，用 AST 可避免
# 「纯注释提交导致全量旁车失效」；docstring 仍计入 AST（宁可敏感不可漏）。
# 代价：动态生成/被替换的可调用对象取不到源码时退化为模块名+限定名 —— 同样会
# 让指纹变化（失效而非错配）。

def _ast_digest(fn) -> str:
    """函数 AST 的稳定文本（注释与格式不敏感；取不到源码时退化为全名）。"""
    try:
        src = textwrap.dedent(inspect.getsource(fn))
    except (OSError, TypeError):
        return f"{getattr(fn, '__module__', '')}:{getattr(fn, '__qualname__', repr(fn))}"
    return ast.dump(ast.parse(src))


def audit_rules_fingerprint() -> str:
    """审计规则自身的稳定指纹（规则数据 + 判定代码），供导出旁车失效判定。"""
    parts = ["rules"]
    for key, label, level, rx in _sh._AUDIT_RULES:
        parts.append(f"{key}|{label}|{level}|{rx.pattern}|{rx.flags}")
    parts.append("code:" + _ast_digest(audit_content))
    parts.append("code:" + _ast_digest(_count_unclosed_chart_fences))
    parts.append("dep:" + _ast_digest(is_fence_close))
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]


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
    pat = _sh.re.compile(r"^" + _sh.re.escape(section_prefix) + r"\.(\d+(?:\.\d+)*)\s")
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
    so, sid = _sh.section_sort_order_key(sec.get("sort_order"), sec.get("id"))
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
    roots.sort(key=_sh._section_sort_key)
    for lst in children_map.values():
        lst.sort(key=_sh._section_sort_key)

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
        pure = _sh._strip_title_number(title)
        num = gen.update_counter(level, parent_id)
        if num:
            flag(num, scope, title, sec.get("id"))
        # —— 内容内部子标题（与 write_section 同口径：prefix + _compute_subheading）——
        n_child_ns = 0
        content = sec.get("content") or ""
        if content and num:
            section_prefix = _sh._section_number_prefix(f"{num} {pure}".strip())
            blocks = _sh._parse_content_blocks(content)
            # 与 write_section 同口径：剥离正文开头与本节标题重复的块
            # （AI 常在正文首行自引用本节标题；渲染端会剥掉，检测器不剥会假阳性）
            blocks = _sh._strip_duplicate_leading_title(blocks, pure, f"{num} {pure}".strip())
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
                bpure = _sh._strip_title_number(block.get("text", ""))
                text, _style = _sh._compute_subheading(
                    section_prefix, level, md_lv, sub_counters, bpure,
                    sec.get("id") or "", has_children=has_children_demoted)
                block["_fixed_text"] = text
            n_child_ns = _sh._count_child_namespace_subheadings(blocks, section_prefix)
            for block in blocks:
                if block.get("type") != "heading":
                    continue
                fixed = str(block.get("_fixed_text") or "")
                m = _sh.re.match(r"^(\d+(?:\.\d+)*)\s", fixed)
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
    roots.sort(key=_sh._section_sort_key)
    for lst in children_map.values():
        lst.sort(key=_sh._section_sort_key)

    issues: list[dict] = []

    def walk(ns: list, prefix: str):
        for idx, sec in enumerate(ns, 1):
            expected = f"{prefix}.{idx}" if prefix else str(idx)
            stored = _sh._stored_outline_number(sec)
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
_STALE_FIG_REF_RE = _sh.re.compile(r"图\s*(\d+-\d+)")
# 表号引用正则：「表 X-Y」
_STALE_TBL_REF_RE = _sh.re.compile(r"表\s*(\d+-\d+)")
# 节号引用正则：「第X节」/「第X.X节」/「见X.X节」/「参考X」等
# 捕获组 1=「第…节」里的纯数字路径；组 2=「见/参考/根据」引导的数字路径
_STALE_SEC_REF_RE = _sh.re.compile(
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
    _HEADING_WITH_DOTTED_NUM_RE = _sh.re.compile(
        r"^\s*(?:#{1,6}\s+|\*\*?)?\d+(?:\.\d+)+(?:\.{1,})?\s+")

    # 构建 parent_ids 倒排索引
    parent_ids = {s.get("parent_id") for s in sections if s.get("parent_id")}
    # 用 config
    try:
        from app.config import settings as _s
        _demote = _s.body_subheading_demote_with_children
    except Exception:
        _sh.logger.debug("读取 body_subheading_demote_with_children 失败（沿用默认 True）",
                     exc_info=True)
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
            for sec in sorted(secs, key=_sh._section_sort_key):
                try:
                    level = max(1, min(int(sec.get("level") or 1), 8))
                except (TypeError, ValueError):
                    level = 1
                # 只推进计数器（标题传空串即可，编号语义与标题无关）
                gen.format_heading(level, "", parent_id)
                chapter_num = _sh._figure_chapter_num(gen)
                try:
                    blocks = _sh._parse_content_blocks(sec.get("content") or "")
                except Exception:
                    # 单章解析失败不影响其它章（与渲染端逐块 fail-soft 同精神）
                    _sh.logger.debug(
                        "章节 %s 正文块解析失败（图号/表号集合推导按空内容处理）",
                        sec.get("id") or "", exc_info=True)
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
        _sh.logger.warning("图号/表号真实集合推导失败（DLV-14 图/表分支按无集合降级）",
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
                    sid = str(_sh.json.loads(oj).get("id") or "")
                except Exception:
                    _sh.logger.debug(
                        "outline_json 解析失败（该章节号不入真实集合，"
                        "可能影响 stale 引用检测）", exc_info=True)
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
            for m in _sh._STALE_FIG_REF_RE.finditer(content):
                ref = m.group(1)
                full_ref = f"图{ref}"
                if full_ref not in real_nums["fig"]:
                    _add(sec_id, title, "fig", full_ref,
                         f"正文引用「{full_ref}」，但本次导出只分配了 "
                         f"{sorted(real_nums['fig'])}；目录重排或增删章节后图号已漂移，"
                         f"建议检查引用是否仍有效")

        # 表号
        if real_nums["tbl"]:
            for m in _sh._STALE_TBL_REF_RE.finditer(content):
                ref = m.group(1)
                full_ref = f"表{ref}"
                if full_ref not in real_nums["tbl"]:
                    _add(sec_id, title, "tbl", full_ref,
                         f"正文引用「{full_ref}」，但本次导出只分配了 "
                         f"{sorted(real_nums['tbl'])}；目录重排后表号可能漂移")

        # 节号（内部总是推导 sec 集合）
        for m in _sh._STALE_SEC_REF_RE.finditer(content):
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
