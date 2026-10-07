"""项目关键事实（global_facts）构建管线（R47 债-5 下沉自 routers/sse_handlers）。

本模块把「目录生成 / 正文生成 / 一致性审计 / 一致性扫描」共用的事实构建逻辑
从超大路由器 ``sse_handlers`` 下沉为 service 层单一事实源，消除：

  * ``routers/compliance.py`` → ``routers/sse_handlers`` 的反向 import；
  * ``services/consistency_scanner.py`` → ``routers/sse_handlers`` 的反向 import。

依赖方向（service 不依赖 router）：
  * ``app.services.facts_extractor``（SQL 单一出口 / 项目 ID 反查）
  * ``app.services.facts_classification``（chapter 惰性派生）
  * ``app.services.scheme_basis``（方案名称相关性前置）
  * ``app.services.prompt_governance.allocate_char_budgets``（R47 债-2 已下沉）

行为契约：与历史 ``sse_handlers._build_facts_text`` 逐字一致；公开入口为
``build_facts_text``，sse_handlers 保留 ``_build_facts_text = build_facts_text``
别名以让旧调用点零改动。
"""
from __future__ import annotations

import logging

from app.config import settings
from app.services.content_data_contract import (
    build_global_data_dictionary,
    render_data_dictionary_block,
)
from app.services.prompt_governance import allocate_char_budgets as _allocate_char_budgets

logger = logging.getLogger("facts_builder")


#: 全局事实低置信度阈值：低于此值在注入正文时标注「低置信度」
LOW_CONFIDENCE_THRESHOLD = 0.5


def _facts_keywords(text: str) -> set[str]:
    """从章节标题/描述抽取关键字（中文 2-gram + ASCII 单词≥3），用于事实相关性预筛。"""
    import re
    kws: set[str] = set()
    if not text:
        return kws
    # 中文连续段 → 2-gram（最贴近中文切词、零依赖）
    for seg in re.findall(r"[一-鿿]+", text):
        for i in range(0, len(seg) - 1):
            kws.add(seg[i:i + 2])
    # ASCII 单词（≥3 字母，如 CFG、MJS、HDPE）
    for w in re.findall(r"[A-Za-z]{3,}", text):
        kws.add(w.lower())
    return kws


# 通用/总体类事实组：任何章节都应可见（项目级参数，不随章节主题消失）
_FACTS_GENERIC_GROUP_HINTS = ("概况", "总体", "通用", "项目信息", "工程概况", "项目概况", "编制依据")


def _filter_facts_rows(rows: list, leaf: dict) -> list:
    """按章节标题/描述对事实行做相关性预筛（对齐 OpenBidKit 的逐章精选注入）。

    - 无关键词（如章节无描述）→ 不过滤，返回全部（短方案/弱描述不退化）。
    - 通用类事实组始终保留（项目级参数）。
    - 命中任一关键词的事实保留。
    - 命中为空 → 回退全部（绝不因预筛丢事实）。
    """
    title = f"{(leaf.get('title') or '')} {(leaf.get('description') or '')}"
    kws = _facts_keywords(title)
    if not kws:
        return rows
    out: list = []
    # ✅ P0 修复（2026-09-27）：行结构自 2026-09-24 起由固定 3 元组变为 5 元组
    #    (gt, title, content, confidence, chapter)，旧实现 `for gt, t, content in rows`
    #    对生产行直接抛 `ValueError: too many values to unpack (expected 3, got 5)`。
    #    该异常发生在**每章构建上下文**阶段（sse_handlers.py:5091），被章节级
    #    try 吞成 section_error —— 只要项目存在全局事实且本章标题能抽出关键词，
    #    **所有章节 100% 失败**，表现为「正文 0 字完成（N/N 章失败）」。
    #    修法：按下标取前三列做判定，**整行原样保留**（不得裁回 3 元组，
    #    否则会丢掉 confidence / chapter 两个下游消费方依赖的字段）。
    for _row in rows:
        gt = _row[0] if len(_row) > 0 else ""
        t = _row[1] if len(_row) > 1 else ""
        content = _row[2] if len(_row) > 2 else ""
        if any(h in (gt or "") for h in _FACTS_GENERIC_GROUP_HINTS):
            out.append(_row)
            continue
        hay = f"{gt or ''} {t or ''} {content or ''}"
        if any(kw in hay for kw in kws):
            out.append(_row)
    return out if out else rows


def _row_chapter(row) -> str:
    """从事实行取九大章节 key（历史 3/4 元组行无该列 → 返回空串）。

    `_load_facts_rows` 自 2026-09-24 起多读一列 chapter，行结构由固定
    3 元组变为 5 元组（gt, title, content, confidence, chapter）。
    历史上任何直接构造 3/4 元组行的调用方（含单测）必须继续可用，
    故按长度自适应而非索引硬取。
    """
    try:
        return str(row[4] or "") if len(row) >= 5 else ""
    except (TypeError, IndexError, KeyError):
        return ""


def _chapter_inject_enabled() -> bool:
    """是否启用「章节内事实前置」（配置 ``facts_chapter_inject``）。

    ✅ BUG 修复（2026-10-01 · 死开关）：该配置项自 2026-09-24 声明以来
    **从未被任何代码读取**——「章节内事实前置」在 ``chapter`` 非空时恒启用，
    而配置注释与 AGENTS.md §4.8 都写着「默认关闭」。后果：用户改
    ``FACTS_CHAPTER_INJECT=false`` 想关掉章节前置，界面/正文行为**没有任何变化**，
    属「配了不生效」的静默失效。

    默认值对齐 2026-09-27 起的**实际**行为（当时已把 ``chapter=`` 接到调用点，
    章节前置事实上已生效），故开关默认 ``True`` 而非注释里的 ``False``——
    否则接通开关等于默认关掉已生效的能力，构成回归。设 ``False`` 即回到
    「逐章注入全量事实文本（不排序）」的旧顺序。

    读配置失败按 ``True`` 兜底：与既有实际行为一致，避免配置层异常让
    九大章节分类突然对正文零影响。
    """
    try:
        return bool(settings.facts_chapter_inject)
    except Exception:  # pragma: no cover - 配置层异常兜底
        return True


def _render_facts_text(
    rows: list, *, relevant_to: dict | None = None,
    max_total: int = 6000, per_fact: int = 300,
    chapter: str = "",
) -> str:
    """把 global_facts 行渲染为「项目关键事实」文本（纯函数，便于单测与逐章精选复用）。

    过滤规则（与正文生成一致，避免把待裁决/编造值当确定性事实注入）：
    按 group_title 分组聚合，每条事实截断 per_fact 字、总量截断 max_total 字
    （超量即停止，保留已写入小节的完整性）。

    ✅ 逐章精选（relevant_to，对齐 OpenBidKit 的逐章精选注入）：当传入章节 leaf 时，
    先按章节标题/描述关键字预筛相关性更高的事实；命中为空则回退全量（绝不丢事实）。
    """
    if relevant_to is not None:
        rows = _filter_facts_rows(rows, relevant_to)

    # ✅ 章节内事实前置（开关 facts_chapter_inject）：把属于本章
    #    （chapter 匹配）的事实排到最前，其余保持原序跟在后面。
    #    ✅ BUG 修复（2026-10-01）：此处此前**不读开关**，章节前置在
    #    ``chapter`` 非空时恒启用，而配置注释宣称「默认关闭」——开关是死的。
    #    现按 _chapter_inject_enabled() 门控（默认 True，与既有实际行为对齐）。
    #    稳定性契约：**绝不丢事实** —— 只是重排，不是筛选；超预算时
    #    被舍弃的只会是尾部（非本章）事实，本章事实必须优先保留。
    if chapter and _chapter_inject_enabled():
        hit: list = []
        rest: list = []
        for r in rows:
            (hit if _row_chapter(r) == chapter else rest).append(r)
        rows = hit + rest

    # ---------------------------------------------------------------
    # ✅ P1 修复（2026-09-27 · 尾部事实整段消失 → 假性【待补充】）：
    #   旧实现是**头部优先**的 `break`：一旦 total+len(fact) > max_total
    #   就直接跳出循环，后面的事实在提示词里**完全不可见**。对典型方案
    #   （group_title 分组 + 每组若干条，总量常超 6000 字）而言，危大参数、
    #   验收标准、应急资源这类**排在后面的事实**会被成批丢掉；AI 看不到 →
    #   判定"事实缺失" → 写出【待补充：XXX】。
    #   修复：与目录生成侧 `_budgeted_truncate_sections` 同一思路，改用
    #   **按比例分配预算**（复用 `_allocate_char_budgets`，同一口径），
    #   让每条事实都拿到保底额度 —— 宁可都短一点，**不丢任何一条**。
    #   未超预算时分配器返回恒等数组，输出与旧实现逐字一致（回归护栏见
    #   tests/test_content_facts_budget_20260927.py）。
    # ---------------------------------------------------------------
    prepared: list[tuple[str, str, float | None]] = []
    seen_groups: list[str] = []
    for _row in rows:
        gt, _title, content = _row[0], _row[1], _row[2]
        if gt not in seen_groups:
            seen_groups.append(gt)
        # confidence 是 4/5 元组行的第 4 项（3 元组历史行无此列 → 不标注）
        try:
            _conf = float(_row[3]) if len(_row) >= 4 and _row[3] is not None else None
        except (TypeError, ValueError):
            _conf = None
        prepared.append((gt, (content or '')[:per_fact], _conf))

    header_cost = sum(len(f"### {g}\n") for g in seen_groups)
    fact_budget = int(max_total) - header_cost
    fact_lens = [len(c) + 1 for _g, c, _cf in prepared]  # +1 换行
    n = len(prepared)
    allocs = [0] * n
    if n and fact_budget > 0:
        allocs = _allocate_char_budgets(fact_lens, fact_budget)
        # ① 硬上限：_allocate_char_budgets 的保底按 `budget//n*2` 估算，
        #    n 较大时保底之和会超过 budget —— 必须按比例回收，
        #    否则会突破 max_total 这个硬契约（提示词长度上限）。
        total_alloc = sum(allocs)
        if total_alloc > fact_budget:
            allocs = [(a * fact_budget) // total_alloc for a in allocs]
        # ② 保底：预算允许时给每条事实至少 1 个字符，实现「不丢任何一条」。
        #    预算实在不够（事实数远多于预算）时，超出部分仍会被硬上限挡掉，
        #    此时「不丢」不可兼得「不超预算」——按老口径以外层契约为准。
        deficit = fact_budget - sum(allocs)
        i = 0
        while deficit > 0 and i < n:
            room = fact_lens[i] - allocs[i]
            if room > 0:
                give = min(room, deficit)
                allocs[i] += give
                deficit -= give
            i += 1

    parts: list[str] = []
    total = 0
    _cur_gt = None
    for (gt, body, _conf), alloc in zip(prepared, allocs):
        if alloc <= 0:
            # 该条连保底都没拿到：跳过（组标题也不应为此单独出现）
            continue
        if gt != _cur_gt:
            header = f"### {gt}\n"
            # ✅ 修复（2026-09-16）：组标题也必须过预算 —— 旧实现无条件追写组标题，
            #    仅在末尾用 `[:max_total]` 切片兜底，于是超预算时**组标题会被截成
            #    "### 基坑支"** 这样的半截文本。超预算时整组跳过（不写半截标题）。
            if total + len(header) >= max_total:
                continue
            parts.append(header)
            total += len(header)
            _cur_gt = gt
        # ✅ 低置信度标注（2026-09-23）：低于阈值时必须让模型知道"这条数字
        #    把握不大"，否则它会把低可信的提取值当作确定参数写进正文
        #    （实测基坑深度等关键参数偶尔只信到 0.3，成稿却写成确定值）。
        prefix = ""
        if _conf is not None and _conf < LOW_CONFIDENCE_THRESHOLD:
            prefix = f"（低置信度 {_conf:.2f}，请以工程实际为准）\n"
        # 硬上限：按实际剩余预算夹一次（组标题开销与低置信度前缀都计入）
        room = max_total - total - len(prefix) - 1
        if room <= 0:
            continue
        fact = prefix + body[:max(0, min(alloc - 1, room))] + "\n"
        parts.append(fact)
        total += len(fact)
    return "".join(parts)


async def _load_facts_rows(db, scheme_id: str) -> list:
    """读取方案下**可注入正文**的全局事实行（剔除矛盾值、未确认模拟值与已过期值）。

    与 `_render_facts_text` 拆分的原因：正文生成需要对每个叶子章节做
    「逐章精选」（`relevant_to`），若逐章都查一次 DB，N 章就是 N 次查询；
    这里只查一次、在内存中按章过滤，兼顾正确性与性能。

    ✅ P0 修复（2026-09-27 · 过期/模拟事实被当成确定值注入正文）：
      旧实现在两条 SQL 里都只写了 `has_conflict=0 AND is_resolved=1`，
      **漏掉 `is_simulated=0`（AI 编造值）与 `is_stale=0`（来源已被新提取
      取代的过期值）**。而 `services/facts_extractor.py::_FACTS_INJECT_WHERE`
      —— 唯一事实源 —— 一直带着这两个条件；导出门控也用它。
      后果（数据真实性红线）：重新提取后标记为 is_stale=1 的旧值、以及
      标注「待确认」的模拟值，仍会被当作项目确定事实喂给正文模型，
      成稿里出现与投标文件不一致的数字。
      修复：直接复用 `_FACTS_INJECT_WHERE` 单一事实源，杜绝三处分叉。

    全局事实表缺失/查询异常时降级为空列表，绝不阻断生成。

    ✅ P1 修复（2026-09-29 · 惰性派生漏改点）：旧实现直接读 chapter 原始列，
      没有 ``routers/global_facts.list_facts``（_fact_dimension_fields）的惰性派生
      兜底。而 chapter 列只在**提取管线**写值，**手工新增**（create_fact）与
      **分组编辑**（update_fact 模式 2）两条写路径都不写它 → 这批事实的 chapter
      恒为空 → ``facts_chapter_inject`` 的「本章事实前置」对它们**完全失效**：
      界面「章节视图」显示已分类，正文生成却匹配不到本章事实（隐蔽错配）。
      现按 ``facts_classification.dimensions_for_row`` 同一口径在内存惰性派生，
      不写库、不改变已标注行的值（尊重人工归类）。

    ✅ P2 修复（2026-09-29 · 口径分叉）：WHERE/ORDER BY 改用
      ``build_injectable_facts_query`` 单一出口，与导出侧 ``_query_global_facts``
      真正共用同一段 SQL（此前本函数自维护，而该 helper 的 docstring 声称
      「与 _load_facts_rows 共用」—— 声明与实现不符，改门控一处必漏一处）。

    行结构契约：**5 元组** (gt, title, content, confidence, chapter)。
    ``_render_facts_text`` / `_filter_facts_rows` / `_row_chapter` 均按此下标消费，
    故派生所需列取回后必须投影回 5 元组，不得直接外泄。
    """
    try:
        # 函数级 import，避免模块级引入 facts_extractor 的重依赖
        from app.services.facts_extractor import (
            build_injectable_facts_query,
            resolve_scheme_project_id,
        )
    except Exception as e:  # 单一出口不可用时降级为空，绝不放宽门控
        logger.warning("事实注入门控不可用（降级为无事实）: %s", e)
        return []
    try:
        from app.services.facts_classification import dimensions_for_row
    except Exception:  # pragma: no cover - 派生不可用时仅跳过 chapter 派生
        dimensions_for_row = None
    try:
        # gt 列表达式与导出侧一致（空分组标题降级为「其他事实」）
        _SELECT = (
            "COALESCE(NULLIF(group_title,''), '其他事实') AS gt, "
            "title, content, confidence, chapter, "
            # 以下列仅供 chapter 惰性派生，不进入返回行
            "COALESCE(category,'') AS category, "
            "COALESCE(fact_type,'') AS fact_type, "
            "COALESCE(fact_key,'') AS fact_key, "
            "COALESCE(source_ref,'') AS source_ref"
        )
        # 方案事实 + 同项目的项目级事实（scheme_id 为空）一起注入，避免
        # “全局事实”在项目层保存后正文生成看不到。项目 ID 由方案反查，
        # 反查失败时安全回退为仅方案级查询。
        project_id = await resolve_scheme_project_id(db, scheme_id)
        sql, params = build_injectable_facts_query(scheme_id, project_id, _SELECT)
        cur = await db.execute(sql, params)
        out: list = []
        for r in await cur.fetchall():
            if isinstance(r, (list, tuple)):
                # 位置序列（测试 mock / 历史调用方）原样透传，保持既有行契约：
                # 3 元组仍是 3 元组，5 元组仍是 5 元组（_row_chapter 按长度自适应）。
                out.append(tuple(r))
                continue
            if not hasattr(r, "keys"):
                continue
            d = dict(r)
            chapter = str(d.get("chapter") or "")
            if not chapter and dimensions_for_row is not None:
                # 库列有值 → 原样回传；为空 → 按确定性规则派生（历史行/手工行不丢）
                try:
                    chapter = str(dimensions_for_row(d).get("chapter") or "")
                except Exception:
                    chapter = ""
            out.append((
                str(d.get("gt") or "其他事实"),
                str(d.get("title") or ""),
                str(d.get("content") or ""),
                d.get("confidence"),
                chapter,
            ))
        return out
    except Exception as e:  # 全局事实表缺失/查询异常不应阻断生成
        logger.warning("构建项目关键事实失败（降级为无）: %s", e)
        return []


def _rank_facts_by_basis(rows: list, basis) -> tuple[list, int]:
    """按「与方案名称的相关性」把**事实分组**整体前置（组内原序，绝不丢事实）。

    ✅ 兑现 ``config.outline_basis_relevance`` / ``outline_relevance_boost`` 的承诺
       （2026-09-27 之前这两个配置**零读取点**，`scheme_basis.rank_by_relevance`
       也**零调用** —— 属典型「配了不生效 / 死代码」）。
    为什么必须前置：目录生成注入的事实有 3000 字硬预算，超量即**停止**，
    尾部的项目参数（监测、验收…）对目录完全不可见；若不按方案名称排序，
    与本方案强相关的事实可能正好落在被截掉的尾部（顺序即见性）。
    为什么按**组**而不是按行排序：``_render_facts_text`` 以 group_title 分组渲染
    （``### 组名``），按行打散会产生重复组标题，破坏上下文连贯性。
    """
    if basis is None or not rows or not getattr(
            settings, "outline_basis_relevance", False):
        return rows, 0
    try:
        from app.services.scheme_basis import relevance_score
        kws = basis.keywords()
        if not kws:
            return rows, 0
        scores: dict[str, int] = {}
        for r in rows:
            gt = str(r[0] or "")
            if gt not in scores:
                scores[gt] = relevance_score(f"{r[0]} {r[1]} {r[2]}", kws)
        if not any(scores.values()):
            return rows, 0
        order = {g: i for i, g in enumerate(
            sorted(scores, key=lambda g: -scores[g]))}
        out = sorted(rows, key=lambda r: order.get(str(r[0] or ""), 999))
        return out, sum(1 for v in scores.values() if v > 0)
    except Exception:  # pragma: no cover - 纯优化，失败回退原序
        logger.warning("按方案名称相关性前置事实分组失败（保持原序）", exc_info=True)
        return rows, 0


async def build_facts_text(
    db, scheme_id: str, max_total: int = 6000, per_fact: int = 300,
    relevant_to: dict | None = None, basis=None,
) -> str:
    """从 global_facts 构建结构化「项目关键事实」文本（目录生成 / 正文生成共用）。

    ✅ 增强：旧实现仅在正文生成内联构建 facts_text，目录生成完全缺失，
    导致目录无法引用已提取的设计参数（开挖深度、搭设高度、地质条件等），
    只能凭原始文档全文泛泛生成。现两路生成共用同一构建逻辑，事实口径一致。

    过滤规则（与正文生成一致，避免把待裁决/编造值当确定性事实注入）：
    - 跳过存在矛盾的事实（has_conflict=0）
    - 仅注入已审核确认（is_resolved=1）的事实 —— 提取结果默认 is_resolved=0 待审核

    ✅ 逐章精选（relevant_to）：传入章节 leaf 时按其标题/描述预筛相关事实
    （命中为空回退全量），用于降低长方案下全量注入的上下文膨胀。

    ✅ 方案名称主线（basis，2026-09-27）：按与方案名称的相关性把**相关分组前置**
    （_rank_facts_by_basis，只重排不删除）—— 固定预算下让相关事实全部可见。
    """
    rows = await _load_facts_rows(db, scheme_id)
    rows, _hit = _rank_facts_by_basis(rows, basis)
    facts_text = _render_facts_text(
        rows, relevant_to=relevant_to, max_total=max_total, per_fact=per_fact)
    # ✅ R52（2026-10-07）：数据字典注入 — 将全局事实的权威取值表
    #    拼到 facts 文本最前面，供正文/目录提示词消费。
    #    开关 content_data_dictionary 默认 True；关闭时提示词逐字回到引入前。
    if settings.content_data_dictionary:
        try:
            _dd = build_global_data_dictionary(rows)
            _dd_block = render_data_dictionary_block(_dd)
            if _dd_block:
                facts_text = _dd_block + "\n" + facts_text
        except Exception:
            logger.warning("数据字典注入失败（忽略，不影响生成）", exc_info=True)
    return facts_text
