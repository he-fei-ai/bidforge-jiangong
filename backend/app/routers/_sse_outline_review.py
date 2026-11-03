"""目录审核与修复层（纯结构拆分自 sse_handlers.py）：提示词治理薄封装/截断/大纲审核修复轮/结构化简报/需求覆盖检查/补丁合并等。零行为变更：所有 facade 命名空间引用经 _sh 延迟绑定，保持 monkeypatch 语义不变。"""

import asyncio
import json
import re

import app.routers.sse_handlers as _sh  # noqa: F401 — facade 命名空间延迟绑定（monkeypatch 兼容）
from app.services.ai.task_registry import TaskTypeConflict  # noqa: F401 — 字符串注解引用

# ---------- 提示词治理 · SSE 侧薄封装（2026-09-24 · G5 / G6） ----------

#: 句子边界（截断时在这些标点之后断开，尽量不切碎句子）
_SENTENCE_END_CHARS = "。！？；\n"


def _truncate_context(text: str) -> str:
    """按 context_length_limit 对上下文做**句子边界**截断。

    ✅ 默认关闭（limit=0 → 原样返回）：护栏而非默认行为。

    截断必须落在句子边界上：直接 `text[:limit]` 会把最后一句切成半截
    （"第二句内容很长很长很长很长很长"），模型据此续写会顺着半句话编，
    产出与上下文语义不连贯的正文。找不到边界时（超长单句）才硬切，
    并追加省略号提示内容被截断。
    """
    if not text:
        return text
    limit = int(getattr(_sh.settings, "context_length_limit", 0) or 0)
    if limit <= 0 or len(text) <= limit:
        return text
    window = text[:limit]
    cut = -1
    for ch in _sh._SENTENCE_END_CHARS:
        pos = window.rfind(ch)
        if pos > cut:
            cut = pos
    if cut > 0:
        return window[:cut + 1]
    return window + "…"

def _apply_prompt_context_budget(text: str) -> str:
    """按 prompt_context_budget 对已标注分段的上下文做预算削减。

    ✅ **默认关闭（budget=0 → 原样返回）**：这是护栏而非默认行为，
    开启会改变发给模型的文本（截断），必须由用户显式配置。
    实际分配逻辑在 services/prompt_governance.allocate_context_budget
    （按「全局事实 > 目录树 > 资料摘要 > 知识库」优先级水填），
    本函数只做开关判断与异常降级 —— 治理异常绝不能阻断生成。
    """
    budget = int(getattr(_sh.settings, "prompt_context_budget", 0) or 0)
    if budget <= 0 or not text:
        return text
    try:
        from app.services.prompt_governance import apply_context_budget
        return apply_context_budget(text, budget)
    except Exception:
        _sh.logger.warning("上下文预算削减失败（使用原文继续）", exc_info=True)
        return text


def _guard_external_material(text: str) -> str:
    """对外部资料段落加"只读数据"围栏并做凭据脱敏（prompt_injection_defense 开启时）。

    ✅ **默认关闭**：绝不影响既有生成行为。
    只加围栏 + 告警，**不改写资料正文** —— 与「数据真实性红线」一致
    （资料被改写就等于伪造了用户上传的内容）。
    """
    if not getattr(_sh.settings, "prompt_injection_defense", False) or not text:
        return text
    try:
        from app.services.prompt_governance import (
            guard_external_segments,
            redact_sensitive,
        )
        # ✅ 顺序：先加围栏（标注"这是只读资料"），再脱敏凭据。
        #    脱敏放最后 —— 只动凭据形态的片段（sk-/api_key= 等），
        #    **不碰 GB/JGJ 等标准编号**（误伤会污染技术依据）。
        guarded = guard_external_segments(text)
        _redacted, _n = redact_sensitive(guarded)
        if _n:
            _sh.logger.warning("外部资料中检出 %d 处疑似凭据，已脱敏", _n)
        return _redacted
    except Exception:
        _sh.logger.warning("外部资料防护失败（使用原文继续）", exc_info=True)
        return text


# ---------- 长方案分步：子目录取回执行单元（批内并发 / 批内合并） ----------

def _as_children(obj) -> list:
    """从 AI 响应中取 outline 数组（畸形/缺失一律回退空列表）。

    弱模型常回 {"outline": null} 或 {"outline": {...}}；不归一化会让
    上层把 dict 当 list 迭代（逐字符变成子章节标题）。
    """
    children = obj.get("outline") if isinstance(obj, dict) else None
    return children if isinstance(children, list) else []


async def _finish_stopped_with_partial(task_id: str, holder: dict) -> str:
    """目录生成「用户停止」的唯一收尾出口：携带已生成成果 + 落 checkpoint。

    返回待 yield 的 SSE 行（event=stopped）。

    为什么必须是唯一出口（2026-09-20 修复）：
    旧实现在 4 个停止分支发**裸** stopped 事件（不带 outline、不写
    checkpoint）—— 目录已生成 20 章时用户点停止，成果确定性丢失，前端
    只能提示"后台任务已停止"，用户白等几分钟。

    顺序契约：**先落 checkpoint → 再置终态 → 最后 yield**。反过来会出现
    「finish_task 已把任务 pop 出内存、checkpoint 尚未写入」的窗口，
    断线重挂的 GET /sse/task/{id} 读到 completed/stopped 却拿不到成果。

    holder 处理：读出成果后清空（防 finally 兜底重复落库）；无成果时
    **不写** checkpoint（避免用空成果覆盖上一次的有效 checkpoint）。
    """
    outline = holder.get("outline") or []
    failed = holder.get("failed_chapters") or []
    payload = {'event': 'stopped', 'task_id': task_id}
    if outline:
        payload['outline'] = outline
        payload['partial'] = True
        if failed:
            payload['failed_chapters'] = list(failed)
            payload['failed_count'] = len(failed)
        try:
            await _sh._save_outline_checkpoint(task_id, payload)
        except Exception:
            _sh.logger.warning("检查点写入失败（task=%s · 目录生成 · 停止时成果）", task_id,
                           exc_info=True)
    # 清空 holder：防 finally 兜底把同一份成果再落一次库
    holder["outline"] = []
    holder["failed_chapters"] = []
    await _sh.finish_task(task_id, "stopped", "用户已停止")
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


async def _fetch_chapter_children(
    index: int,
    chapter: dict,
    *,
    sub_prompt: list,
    task_id: str,
    sem,
    validate_fn,
    timeout: int,
    push_stats,
) -> tuple[str, list]:
    """取回单章的二三级子目录 —— 长方案分步链路的**执行单元**。

    返回 (status, children)：status ∈ {"ok", "failed", "stopped"}。

    关键不变**（与 bid_analysis.run_with_pause_gate 同源教训）：
    **暂停闸门必须排在并发信号量之前**。写成「先 async with sem 再
    wait_resume」时，暂停挂起期间在途章把许可全部占住，同批其余章节
    饿死 —— 表现为"点了暂停，整批卡住不动"。

    失败语义：首次失败**重试恰好一次**（重试前再过一次闸门与停止检查），
    两次均失败才判 failed（由编排层计入 failed_chapters）。
    停止语义：进入前 / 排队拿到许可后各复检一次 is_stopped，已停止则
    **不发起 AI 调用**（省一次配额，也避免"点了停止还在烧 token"）。
    """
    # ① 闸门在前：暂停期间不占许可
    await _sh.wait_resume(task_id)
    if _sh.is_stopped(task_id):
        return "stopped", []

    async def _attempt() -> tuple[str, list]:
        """首试 + 重试恰好一次。

        两处调用各自显式携带 json_mode / temperature / scene（审计归因
        要求首试与重试都计入 outline_sublevel；漏打会让 /ai/stats 的调用
        次数与实际消耗对不上）。
        """
        try:
            obj, _raw = await _sh._await_with_stats(
                _sh.collect_json_response(
                    sub_prompt, validate_fn, timeout=timeout,
                    json_mode=True, temperature=0.2, scene="outline_sublevel",
                    repair_key=_sh.OUTLINE_REPAIR_KEY),
                push_stats, task_id=task_id)
            return "ok", _sh._as_children(obj)
        except asyncio.CancelledError:
            raise
        except Exception as e:          # noqa: BLE001 - 兜住任意 provider 异常
            # ✅ O8/O9 闸门：配额 / 认证 / 模型不存在类错误（402/403/404/429）
            #    默认**不重试** —— 这类错误重试几乎必然再失败，白烧一轮配额
            #    与超时；`AI_RETRY_ON_QUOTA_ERROR=True` 可恢复旧行为。
            if not _sh.AI_RETRY_ON_QUOTA_ERROR and _sh._is_quota_error(e):
                _sh.logger.warning("第%d章子目录生成失败（配额/认证类，不重试）: %s",
                               index + 1, e)
                return "failed", []
            _sh.logger.warning("第%d章子目录首次生成失败: %s，重试一次", index + 1, e)
            # 重试前再过一次闸门与停止检查：用户在首试失败后可能已点
            # 停止/暂停，此时不该继续烧配额。
            await _sh.wait_resume(task_id)
            if _sh.is_stopped(task_id):
                return "stopped", []
            try:
                obj, _raw = await _sh._await_with_stats(
                    _sh.collect_json_response(
                        sub_prompt, validate_fn, timeout=timeout,
                        json_mode=True, temperature=0.2, scene="outline_sublevel",
                        repair_key=_sh.OUTLINE_REPAIR_KEY),
                    push_stats, task_id=task_id)
                return "ok", _sh._as_children(obj)
            except asyncio.CancelledError:
                raise
            except Exception as e2:     # noqa: BLE001
                _sh.logger.warning("第%d章子目录重试仍失败: %s", index + 1, e2)
                return "failed", []

    if sem is not None:
        async with sem:
            # ② 拿到许可后复检：排队期间用户可能已点停止
            if _sh.is_stopped(task_id):
                return "stopped", []
            return await _attempt()
    return await _attempt()


async def _fetch_unit_children(
    unit_chapters: list,
    *,
    batch_prompt: list | None,
    single_prompts: list | None,
    task_id: str,
    sem,
    timeout: int,
    push_stats,
) -> tuple[str, list]:
    """取回一个「执行单元」内各章的子目录。

    unit_chapters: [(章序号 i, 章节点 dict), ...]
    single_prompts: 与 unit_chapters 等长的单章提示词列表（合并调用回退用）

    返回 (unit_status, per)：unit_status ∈ {"ok", "failed", "stopped"}；
    per 与 unit_chapters 等长，元素为 (章状态, children)。

    合并语义（OUTLINE_CHAPTER_BATCH_SIZE > 1 时）：
    - 单元内 1 章 → 直接委托 _fetch_chapter_children（逐字等于旧行为）；
    - 单元内多章 → 1 次批量调用；**缺章逐章回退单章**，批量整体失败
      （首试+重试）也逐章回退 —— 绝不整批判死（判死会让 1 章失败拖垮整单元）。
    """
    if _sh.is_stopped(task_id):
        return "stopped", []

    # 单元内单章：直接委托（保持与旧实现逐字一致）
    if len(unit_chapters) == 1:
        i, ch = unit_chapters[0]
        st, children = await _sh._fetch_chapter_children(
            i, ch, sub_prompt=(single_prompts[0] if single_prompts else []),
            task_id=task_id, sem=sem, validate_fn=_sh._sublevel_validate_fn,
            timeout=timeout, push_stats=push_stats)
        return ("stopped" if st == "stopped" else "ok"), [(st, children)]

    # 单元内多章：一次批量调用
    batch_obj = None
    last_err: Exception | None = None
    for attempt in (1, 2):
        try:
            obj, _raw = await _sh._await_with_stats(
                _sh.collect_json_response(
                    batch_prompt, _sh._sublevel_batch_validate_fn, timeout=timeout,
                    json_mode=True, temperature=0.2, scene="outline_sublevel",
                    repair_key=_sh.OUTLINE_REPAIR_KEY),
                push_stats, task_id=task_id)
            batch_obj = obj
            break
        except asyncio.CancelledError:
            raise
        except Exception as e:          # noqa: BLE001
            last_err = e
            # ✅ BUG 修复（2026-10-16 · 配额/认证类错误不重试）：与
            #    _fetch_chapter_children 的 O8/O9 闸门同口径 —— 402/403/404/429
            #    重试几乎必然再失败，白烧一轮配额与超时。旧实现批量路径缺少
            #    该闸门，配额耗尽时每轮都浪费 2 次批量调用后才逐章回退。
            if not _sh.AI_RETRY_ON_QUOTA_ERROR and _sh._is_quota_error(e):
                _sh.logger.warning("批量子目录（第%d次，%d章）配额/认证类错误（不重试）: %s",
                               attempt, len(unit_chapters), e)
                break
            _sh.logger.warning("批量子目录（第%d次，%d章）失败: %s",
                           attempt, len(unit_chapters), e)
    if batch_obj is None:
        _sh.logger.warning("批量子目录重试仍失败（%s），逐章回退单章调用", last_err)

    # 按 chapter_id 归位（章号以「章序号+1」的字符串为准，与提示词一致）
    by_id: dict[str, list] = {}
    if isinstance(batch_obj, dict):
        for c in batch_obj.get("chapters") or []:
            if not isinstance(c, dict):
                continue
            cid = str(c.get("chapter_id") or "").strip()
            ol = c.get("outline")
            if cid and isinstance(ol, list):
                by_id[cid] = ol

    per: list = []
    for idx, (i, ch) in enumerate(unit_chapters):
        cid = str(i + 1)
        if cid in by_id:
            per.append(("ok", by_id[cid]))
            continue
        # 缺章（批量失败 / 模型漏回该章）→ 逐章回退，绝不丢章
        _sh.logger.info("批量响应缺第%s章，回退单章调用", cid)
        st, children = await _sh._fetch_chapter_children(
            i, ch, sub_prompt=(single_prompts[idx] if single_prompts else []),
            task_id=task_id, sem=sem, validate_fn=_sh._sublevel_validate_fn,
            timeout=timeout, push_stats=push_stats)
        per.append((st, children))
    unit_status = "stopped" if any(s == "stopped" for s, _ in per) else "ok"
    return unit_status, per


def _attach_quality_report(outline: list, basis, review_obj: dict) -> dict:
    """生成后自动校验（要求三/四）：连续性 + 全面性 + 冗余，写日志与 review 载荷。

    - **不修改目录**：纯只读校验（删章/改编号是不可逆的用户数据变更）；
    - 报告写入 ``review_obj["quality"]``（前端"审核结论"区可见），并按级别落日志；
    - ⚠️ 刻意**不**把告警塞进 ``suggestions``：那里的文案是"已执行的修复动作"，
      混入校验告警会让用户误以为目录已被改过；需要人工处理时由 quality 段落呈现。
    """
    try:
        from app.services.outline_quality import (
            analyze_name_coverage,
            check_outline_continuity,
            check_structure_standards,
            find_redundant_titles,
            render_continuity_notice,
            render_coverage_notice,
            render_structure_notice,
        )
        cont = check_outline_continuity(outline)
        cov = analyze_name_coverage(basis, outline)
        redundant = find_redundant_titles(basis, outline) if cov.get("evaluated") else []
        # ✅ 2026-10-11（引入《文档生成规则分析与软件标准建立》目录结构规范）：
        #    章数 5-10 / 每章二级节数 2-15 / 深度 ≤4 的**建议性**核对。
        #    只进 quality 报告与本行日志，不进 issues、不改 cont["ok"]、
        #    不塞 suggestions（微型方案章数少于 5 属合法形态，阻断即误伤）。
        struct = check_structure_standards(outline)
        report = {
            "continuity": {"ok": cont.get("ok"), "nodes": cont.get("nodes"),
                           "max_level": cont.get("max_level"),
                           "issue_counts": cont.get("issue_counts"),
                           "generic_titles": cont.get("generic_titles") or []},
            "structure": struct,
            "name_coverage": {
                "evaluated": cov.get("evaluated"), "covered": cov.get("covered"),
                "total_items": cov.get("total_items"),
                "missing": list(cov.get("missing") or [])[:20],
            },
            "redundant_titles": redundant[:10],
        }
        if not cont.get("ok"):
            _sh.logger.warning("目录连续性校验未通过：%s", render_continuity_notice(cont))
        _struct_notice = render_structure_notice(struct)
        if _struct_notice:
            _sh.logger.info("%s（仅建议，不阻断）", _struct_notice)
        if cov.get("evaluated") and cov.get("missing"):
            _sh.logger.warning("目录全面性校验：%s", render_coverage_notice(cov["missing"]))
        if redundant:
            _sh.logger.info("目录冗余候选 %d 个（仅报告不删除）：%s",
                        len(redundant), "、".join(r["title"] for r in redundant[:5]))
        if isinstance(review_obj, dict):
            review_obj["quality"] = report
        return report
    except Exception:  # pragma: no cover - 校验不得影响生成结果
        _sh.logger.warning("目录质量校验异常（忽略，不影响生成结果）", exc_info=True)
        return {}


def _merge_unit_results(
    level1: list, unit_idx: list, per: list, full_outline: list,
    prior_l2: list, failed_chapters: list, unit_status: str = "",
) -> tuple[bool, int]:
    """把一个「执行单元」的子目录结果按章序归位到 full_outline（纯函数，可单测）。

    就地修改 full_outline / prior_l2 / failed_chapters 与 level1 里的章节点，
    返回 (stopped, 本次新增节点数)。

    ✅ BUG 修复（2026-09-26 · 分步生成「停止」丢成果 + 静默漏章）：
    1) 旧实现在单元内遇到 stopped 直接 break，随后才统一 append —— 于是
       **同一批（OUTLINE_CHAPTER_BATCH_SIZE > 1）里已经生成完的章节被整批丢弃**：
       用户点停止后拿到的部分目录少了最近一批的全部子目录，且 failed_chapters
       里也没有记录（表现为"停了但白跑几分钟"）。现改为「先把已完成章节并入、
       再返回 stopped」。
    2) `j >= len(per)`（单元被停止时 per 为空 / 模型少回章节）旧实现 continue
       静默跳过：章节以空 children 被 append，既不计 failed 也不判 stopped
       —— 全部章节"看似成功"，completed 事件里的失败章统计为 0，用户无从察觉
       整批空目录。现：单元状态是 stopped 如实上抛，否则按 failed 记账 + WARNING。

    ⚠️ 顺序不变式：full_outline 必须严格按章序追加（编号由位置推导，
    乱序 = 全树编号错乱），因此 stopped 分支也只允许追加「已按序完成的前缀」。
    """
    stopped = False
    added_nodes = 0
    pending: list = []

    for j, i in enumerate(unit_idx):
        ch = level1[i]
        if j >= len(per):
            if unit_status == "stopped":
                stopped = True
                break
            _sh.logger.warning("第%s章子目录结果缺失（单元返回 %d 条），按失败记账",
                           i + 1, len(per))
            st, children = "failed", []
        else:
            st, children = per[j]
        if st == "stopped":
            stopped = True
            break
        ch["children"] = children if isinstance(children, list) else []
        if st == "failed":
            failed_chapters.append(str(ch.get("title") or f"第{i + 1}章"))
        elif not ch["children"]:
            # ✅ R89 S1：「ok 但零子章」必须记账（静默丢章）。
            #
            # 根因：批量校验器 `_sublevel_batch_validate_fn` 只判「outline 是
            # list」，`outline: []` 判合法（该契约已被 test_outline_call_optimization
            # 锁定 —— 单章章确实可能存在无子级的场景，故**不改校验器**）；而此处
            # 旧实现只认 `st == "failed"` 才记账。弱模型偷懒回空数组时 st 是
            # "ok" ⇒ 全批不计失败 ⇒ 下方 `all_failed`（`len(failed_chapters) >=
            # len(level1)`）为假 ⇒ final_status 落 "completed"、failed_count 缺省、
            # 前端零告警 —— 30 章全空即得一份 30 个空壳一级章的「完成」目录。
            #
            # 这里只增记账、不动校验器契约：终态仍可能 completed，但
            # failed_chapters / failed_count 如实带出，前端有可见信号；全部章
            # 都空时 all_failed 自然成立，走 failed 终态而非 completed。
            failed_chapters.append(str(ch.get("title") or f"第{i + 1}章"))
            _sh.logger.warning("第%s章子目录为空（模型未返回任何二级章节），按失败记账",
                           i + 1)
        pending.append(ch)

    for ch in pending:
        full_outline.append(ch)
        # ✅ BUG 修复（2026-09-29）：与 _sublevel_validate_fn 同一根因 ——
        #    `str(x.get("title", ""))` 对 title=None 得 "None"（键缺失才得 ""）。
        #    章标题为 None 时 prior_l2 会写入字面量 "None / 子节标题"，
        #    随后灌入下一级的「已生成小节」提示词，污染上下文。
        _ch_title = str(ch.get("title") or "")
        for _sub in (ch.get("children") or []):
            # ✅ 崩溃点修复：守卫通过后紧接着 `_sub['title'].strip()` 直接对 None
            #    调方法 → AttributeError，整次目录生成失败。现取值前先归一为空串，
            #    并让守卫与取值用同一表达式（单一判据，杜绝两处再漂移）。
            _sub_title = str(_sub.get("title") or "").strip() if isinstance(_sub, dict) else ""
            if _sub_title:
                prior_l2.append(f"{_ch_title} / {_sub_title}")
        added_nodes += _sh._count_nodes(ch.get("children") or [])
    return stopped, added_nodes


def _compact_outline_json(nodes: list, max_desc: int = 80) -> str:
    """把目录树渲染为**紧凑 JSON**（供审核修复轮作为「原始目录」输入）。

    ✅ 修复（2026-09-16）：旧实现 `json.dumps(outline, ensure_ascii=False)` 直接
    把**带 id/level/空 children/长描述**的完整目录塞进修复提示词 —— 长方案
    （30 章 × 20+ 节点，节点上限 1200）可轻易超过 10 万字符，必然超出模型上下文
    或触发超时，`outline_feedback_system`（审核建议回灌修复）整轮空转，用户看到的
    仍是未修正的目录。

    这里做**无损语义压缩**（只去掉对模型无用的字段，不删节点）：
      · 去掉 id / level / confidence 等程序生成字段；
      · description 截断到 max_desc（修复只需判断"这节写什么"，不需要全文）；
      · 空 children 不输出（省掉每节点 14 字符的 `"children":[]`）。
    实测（30 章 × 3 级真实目录）字符量约为原 JSON 的 **0.63~0.66**
    （描述越长收益越大），1200 节点目录可压到数万字符以内。
    """
    budget = [max(int(max_desc), 0)]

    def _walk(ns, depth: int = 0) -> list:
        out: list = []
        if depth > 10 or not isinstance(ns, list):
            return out
        for n in ns:
            if not isinstance(n, dict):
                continue
            item: dict = {"title": str(n.get("title", "")).strip()}
            desc = str(n.get("description", "")).strip()
            if desc and budget[0]:
                item["description"] = desc[:budget[0]]
            children = n.get("children")
            if isinstance(children, list) and children:
                kids = _walk(children, depth + 1)
                if kids:
                    item["children"] = kids
            out.append(item)
        return out

    try:
        return json.dumps({"outline": _walk(nodes)}, ensure_ascii=False)
    except (TypeError, ValueError):
        # 极端脏数据（循环引用等）不应阻断修复链路：退回空目录结构
        return '{"outline": []}'


def _outline_fix_looks_degraded(original: list, fixed: list) -> bool:
    """修复结果是否**退化**（节点数远少于原目录）。

    ✅ 修复（2026-09-16）：旧实现只做结构校验，通过后**整份替换**原目录 ——
    弱模型在修复轮只回一个"示例结构"（如 3 个节点）时，用户辛苦生成的几十章
    目录会被静默替换成 3 个节点（正文生成随即"章节全丢"）。这里给出覆盖率
    下限：修复结果节点数低于原目录的 OUTLINE_FIX_MIN_COVERAGE 时判定退化，
    调用方保留原目录（宁可未修复，不可丢目录）。
    """
    n_orig = _sh._count_nodes(original) if isinstance(original, list) else 0
    n_fixed = _sh._count_nodes(fixed) if isinstance(fixed, list) else 0
    if n_orig <= 0:
        return False
    return n_fixed < max(1, int(n_orig * _sh.OUTLINE_FIX_MIN_COVERAGE))


# ---------- 编制要求程序化覆盖预检 + 外科式补齐（2026-09-21） ----------

# ✅ 2026-10-02（第二十五轮 · 判据同源）：危大必备章节关键词表此前是九大章节
# 关键词的**第三份手抄副本**（另两份在 ``audit_rules`` 与本文件），已实证与
# 审核侧**双向分叉**：「工程概述 / 施工部署 / 组织机构 / 图纸」目录侧认、
# 审核侧不认 → 目录侧程序化预检放行、预检照报 CMP-01/03/06/07。
#
# 现改为**单一出口** ``outline_checkpoint.required_chapter_specs()``（判据指向
# ``audit_rules`` 注册表 + ``preflight_engine`` 实际谓词常量）。本模块保留的
# ``_DANGEROUS_REQUIRED_KEYWORDS`` 名字**仅作历史兼容别名**，内容由唯一出口
# 派生，不再是独立副本；护栏测试锁定其与唯一出口逐项一致。
from app.services import outline_checkpoint as _ocp


def _dangerous_required_keywords() -> tuple[tuple[str, tuple[str, ...]], ...]:
    """危大 10 章必备章节关键词（**由 outline_checkpoint 单一出口派生**）。

    Returns:
        ``((展示名, 关键词元组), ...)``—— 标签与顺序与原表**逐字一致**
        （九大法定章节简写 + 「监测方案」），由 ``test_classification_parity_20261001``
        锁定。仅**关键词**换成审核侧同源词（原表认「工程概述/施工部署/组织机构/
        图纸」而审核侧不认，是 CMP-01/03/06/07 误报的根因）。
    """
    return _ocp.dangerous_required_keywords()


#: 历史别名（保留供既有调用点与护栏断言；不再独立维护）。
_DANGEROUS_REQUIRED_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    _dangerous_required_keywords())


# `_outline_checkpoint_kwargs` 已随拆分回迁 facade（sse_handlers.py）：
# 其裸 ``render(**...)`` 调用点留在 facade，护栏扫描器要求 helper 定义
# 与调用点同文件可静态解析；本模块经 ``_sh.`` 延迟绑定访问，语义不变。


#: 目录**审核**提示词的「按清单核对」指令（模板 ``outline_review_system`` 的
#: ``{outline_checkpoint_audit_hint}`` 独占行）。
#:
#: ⚠️ 2026-10-03（R38 · D5）：此前这条指令以**固定文案**写在模板里，而它
#: 引用的清单来自 ``{outline_checkpoint_block}``——开关关闭时清单整行被丢弃、
#: 指令却仍在，模型会去核对一份**根本不存在**的清单，臆造「缺失章节」并写进
#: suggestions，进而触发外科式补齐加进并不存在的章节。故改为随块同生共死。
_OUTLINE_REVIEW_AUDIT_HINT = (
    "8.1 目录是否覆盖上方【审核检查点前置要求】列出的全部必备法定章节？"
    "任一缺失即判 passed=false，并在 suggestions 中列出缺失章节名")


def _outline_review_checkpoint_kwargs(scheme_name: str = "",
                                      scheme_type: str = "",
                                      is_hazardous: bool = False) -> dict:
    """审核提示词专用的检查点变量（共享块 + 按清单核对指令）。

    与 :func:`_outline_checkpoint_kwargs` 的区别：**只在审核链路追加
    ``outline_checkpoint_audit_hint``**。生成链路不得携带该指令（它说
    「判 passed=false」是审核语义，对生成提示词无意义且会误导）。

    块为空（开关关闭 / 构建失败）时同样返回空字典 —— 指令与清单必须
    同时在场或同时缺席。
    """
    kw = _sh._outline_checkpoint_kwargs(scheme_name, scheme_type, is_hazardous)
    if not kw:
        return {}
    kw["outline_checkpoint_audit_hint"] = _sh._OUTLINE_REVIEW_AUDIT_HINT
    return kw


def _split_requirement_items(requirements: str) -> list[str]:
    """把用户填写的「编制要求」拆成逐条条目（并剥离条目自带编号）。

    输入通常是评审要点清单：
        "1. 工程概况及特点\\n2）施工部署与进度计划\\n- 安全保证措施\\n短\\n"
    期望 → ["工程概况及特点", "施工部署与进度计划", "安全保证措施"]

    「短」这类 1 字条目被丢弃：拆分后仍无法做任何覆盖判定，
    留在 missing 里只会让外科补齐去"补"一条根本没有内容的条目。
    """
    if not requirements or not str(requirements).strip():
        return []
    items: list[str] = []
    for raw in str(requirements).splitlines():
        line = str(raw).strip().lstrip("-*·•").strip()
        if not line:
            continue
        # 剥离 "1." / "2）" / "（三）" / "一、" 等条目编号前缀
        line = _sh.strip_outline_numbering(line).strip()
        if len(line) < 2:      # 剥离后过短 → 不是有效条目
            continue
        if line not in items:   # 去重保序
            items.append(line)
    return items


def _common_run(a: str, b: str) -> str:
    """两个字符串的最长公共子串（朴素 DP，仅用于短标题比对）。"""
    if not a or not b:
        return ""
    best = ""
    prev = [0] * (len(b) + 1)
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > len(best):
                    best = a[i - cur[j]:i]
        prev = cur
    return best


def _check_requirements_coverage(
    requirements: str, outline: list, is_dangerous: bool = False,
    basis=None,
) -> tuple[bool, list[str]]:
    """程序化检查目录是否覆盖了编制要求 / 危大工程必备章节 / **方案名称维度**。

    返回 (covered, missing)：
    - covered=True → 全部命中，可跳过 AI 审核（省 1~2 次调用）；
    - missing     → 未命中的条目清单（供外科式补齐使用）。

    判定策略（**保守方向**：拿不准一律推向 missing，多走 AI，
    绝不因误判覆盖而放过真实缺失）：
    1. 双向子串匹配：条目是标题的子串，或标题（≥3 字）是条目的子串
       —— 避免"计"这种单字误命中；
    2. 条目含并列分隔符（、/，/与/及）时按**片段**匹配：任一 ≥3 字片段
       命中即覆盖（"计算书、相关图纸" → 命中"计算书及相关图纸"）；
    3. 标题与条目有 ≥4 字公共子串时也判覆盖（减少无谓的 AI 打扰）；
    4. 危大工程额外逐个检查 10 个必备章节关键词；
    5. ✅ 2026-09-27（要求四 · 全面性）：传入 ``basis`` 时追加
       `outline_quality.analyze_name_coverage` 的结果 —— 方案名称里解析出的
       **主要施工内容 / 工序 / 工艺 / 对象**若在目录（全层级）中无任何落点，
       一并计入 missing，从而复用**既有的外科式补齐**链路自动补章
       （而不是只靠提示词 0.1 条款自觉）。受 ``outline_name_coverage_check``
       控制（默认 True；关闭即回退修复前口径）；
    6. ✅ P1-4（2026-09-27）：第 1~2 条（严格双向子串匹配）**仍只看一级标题**
       —— 危大必备 10 章是**结构性**要求，必须有独立一级章节；
       但第 3 条（≥4 字公共子串的宽松匹配）扩展到**全层级标题**，
       于是"编制要求里写过的内容以二级小节形式落实"也算覆盖。
       放宽只作用在**最宽松**的那条规则上，误报率不增。
    """
    titles: list[str] = []
    for n in (outline or []):
        if not isinstance(n, dict):
            continue
        t = str(n.get("title") or "").strip()
        if t:
            titles.append(t)
    # ✅ P1-4：全层级标题（仅供规则 3 的宽松匹配使用）
    all_titles: list[str] = list(titles)
    try:
        from app.services.outline_quality import collect_titles
        all_titles += [str(e.get("title") or "") for e in collect_titles(outline)
                       if str(e.get("title") or "").strip()]
    except Exception:  # pragma: no cover - 纯兜底，取不到就退化为仅一级
        _sh.logger.debug("全层级标题收集失败（退化为仅一级标题参与匹配）", exc_info=True)
        all_titles = list(titles)

    def _hit(fragment: str) -> bool:
        frag = fragment.strip()
        if len(frag) < 2:
            return False
        for t in titles:
            if frag in t or (len(t) >= 3 and t in frag):
                return True
        return False

    missing: list[str] = []
    for item in _sh._split_requirement_items(requirements):
        if _hit(item):
            continue
        parts = [p.strip() for p in re.split(r"[、，,和与及]", item)
                 if len(p.strip()) >= 3]
        if parts and any(_hit(p) for p in parts):
            continue
        if any(len(_sh._common_run(item, t)) >= 4 for t in all_titles):
            continue
        missing.append(item)

    if is_dangerous:
        for label, keywords in _sh._DANGEROUS_REQUIRED_KEYWORDS:
            if not any(kw in t for kw in keywords for t in titles):
                missing.append(f"{label}（危大工程必备章节）")
    # ✅ 2026-10-02（第二十五轮 · 门控对齐 + 判据同源）：**新增**九大法定章节
    #    检查，与上面危大 10 章检查**并存且互补**（不是替换）：
    #
    #    - 上面那条是危大**结构性**要求（须独立一级章节，刻意只判 L1）；
    #    - 本条对齐预检 ``check_completeness``：九大法定章节**任何专项方案
    #      都要有**（预检对此**无条件**检查），且**全层级标题**匹配、判据取自
    #      ``outline_checkpoint`` 单一出口（= 审核侧同谓词）。
    #
    #    为什么必须新增：此前非危大专项方案（本软件主力场景，§4.22 定位切换后）
    #    目录侧**从未被要求过九章**，生产库实证 6 条 CMP 命中（3 block）。
    #    ⚠️ 本条**只新增判定、不放宽任何既有判定**，故此前能过的目录仍能过；
    #    此前靠 AI 审核兜住的目录改走外科补齐（多 1 次调用，省掉后续 CMP block）。
    if getattr(_sh.settings, "outline_checkpoint_check", True):
        try:
            _ck_missing = _ocp.outline_coverage_missing(outline, is_hazardous=False)
        except Exception:  # pragma: no cover - 自检是体检，不是闸门
            _sh.logger.warning("目录检查点覆盖校验异常（忽略，不阻断生成）",
                           exc_info=True)
            _ck_missing = []
        for label in _ck_missing:
            entry = f"{label}（法定必备章节）"
            if entry not in missing:
                missing.append(entry)
    # ⚠️ 早退（跳过 AI 审核）的判据**保持原样**，只按上面新增的 missing 生效：
    #    `_check_requirements_coverage` 现在恒定参与，但「程序化全过就跳过 AI 审核」
    #    仍**只在用户填了编制要求 / 提供了方案解析**时成立。
    #    为什么：AI 审核除九章齐备外还查标题质量、层级、跨章重复、投标章节等，
    #    无条件跳过会把这些检查一并丢掉（反而让其它规则的问题上升）。
    #    所以「未填编制要求 + 目录完整」= 旧行为（照旧走 AI 审核）；
    #    只有**确有缺失**时才改走外科补齐（本轮要达成的效果）。
    # ✅ 2026-09-27（要求四 · 全面性）：方案名称维度覆盖缺口并入 missing，
    #    复用外科式补齐链路自动补章（只读校验，失败仅告警不阻断）。
    if basis is not None and getattr(_sh.settings, "outline_name_coverage_check", True):
        try:
            from app.services.outline_quality import analyze_name_coverage
            cov = analyze_name_coverage(basis, outline)
            if cov.get("evaluated") and cov.get("missing"):
                missing.extend(cov["missing"])
        except Exception:  # pragma: no cover - 校验异常不阻断生成
            _sh.logger.warning("方案名称覆盖校验异常（忽略，不阻断生成）", exc_info=True)
    return (not missing), missing


def _restore_descriptions(original: list, target: list) -> None:
    """把原目录的 description 按「(层级路径, 标题)」回填到合并修复结果上（就地）。

    背景：审核提示词吃的是 _outline_skeleton 截断版目录（description 截到
    60 字以省 token），模型据此产出的 fixed_outline 里 description 也是截断的。
    若直接采用，用户目录中每章说明都凭空短一截 —— 审核省了一次调用，
    却污染了全部章节描述。

    键用「(路径, 标题)」而非 id：模型回传的 id 可能是编号而非原 id。
    仅当目标描述为空、或短于原描述（典型截断特征）时才回填，
    模型新写的更长描述予以保留。

    ✅ 改进（2026-10-09 · P1-8 收口）：审核修复轮可能增删 / 重排章节，
    导致 fixed_outline 的位置路径与原目录不再对齐 —— 旧实现此时完全无法
    回填 description，用户看到全篇 60 字截断描述（而原描述可能数百字）。
    现增加**标题回退匹配**：(path, title) 精确匹配失败后，按标题查找原目录；
    多个同名章节时取**位置路径最近**者（避免错误回填到不相关章节的描述）。
    回退命中时记 DEBUG 日志（可观测但不打扰正常流程）。
    """
    if not isinstance(original, list) or not isinstance(target, list):
        return

    # (path, title) -> description（精确匹配）+ title -> [(path, desc)]（回退匹配）
    orig_desc: dict = {}
    title_index: dict[str, list[tuple[str, str]]] = {}

    def _collect(nodes: list, prefix: str) -> None:
        idx = 0
        for n in nodes or []:
            if not isinstance(n, dict):
                continue
            idx += 1
            path = f"{prefix}.{idx}" if prefix else str(idx)
            title = str(n.get("title") or "").strip()
            desc = str(n.get("description") or "")
            if title:
                orig_desc[(path, title)] = desc
                title_index.setdefault(title, []).append((path, desc))
            children = n.get("children")
            if isinstance(children, list) and children:
                _collect(children, path)

    _collect(original, "")
    if not orig_desc:
        return

    def _path_distance(a: str, b: str) -> int:
        """位置路径距离：段数差 + 逐段不等计数（用于多同名时选最近者）。"""
        ap = a.split(".")
        bp = b.split(".")
        dist = abs(len(ap) - len(bp))
        for x, y in zip(ap, bp):
            if x != y:
                dist += 1
        return dist

    _fallback_count = [0]

    def _restore(nodes: list, prefix: str = "") -> None:
        idx = 0
        for n in nodes or []:
            if not isinstance(n, dict):
                continue
            idx += 1
            path = f"{prefix}.{idx}" if prefix else str(idx)
            title = str(n.get("title") or "").strip()
            # ① 精确匹配：(path, title)
            src = orig_desc.get((path, title))
            # ② 回退匹配：标题查找（修复轮增删/重排章节导致路径偏移时）
            if src is None and title:
                candidates = title_index.get(title)
                if candidates:
                    if len(candidates) == 1:
                        src = candidates[0][1]
                    else:
                        # 多同名：取位置路径最近者
                        best = min(candidates, key=lambda c: _path_distance(c[0], path))
                        src = best[1]
                    _fallback_count[0] += 1
            if src:
                cur = str(n.get("description") or "")
                if not cur.strip() or len(cur) <= len(src):
                    n["description"] = src
            children = n.get("children")
            if isinstance(children, list) and children:
                _restore(children, path)

    _restore(target)
    if _fallback_count[0]:
        _sh.logger.debug("description 回填使用了标题回退匹配（%d 处路径偏移）",
                     _fallback_count[0])


def _merge_patch_chapters(outline: list, new_chapters: list) -> list:
    """把外科式补齐产出的新章节合并进现有目录（按 insert_after 定位）。

    定位策略（**精确优先、歧义则追加末尾**，宁可位置不完美也不插错）：
    1. 标题**完全相等**的命中：唯一 → 插到它后面；多处同名 → 视为歧义，
       追加末尾（同名章节本就无法判断用户想挂在哪一章之后）；
    2. 无精确命中时退化为**互含匹配**（「安全文明施工」⊂「安全文明施工保证措施」）：
       同样只在唯一命中时插入，多处命中视为歧义 → 追加末尾；
    3. 都不满足 → 追加末尾。
    拿不准时追加末尾而非猜第一个位置：插错位置会让新章落在语义无关的
    章节之后，比追加末尾更难被用户一眼看出问题。

    合并后统一 renumber —— 位置即编号的唯一事实源；新章插入到中间时，
    其后所有章的编号都要顺移（否则导出编号断号/重号）。
    """
    merged = [n for n in (outline or []) if isinstance(n, dict)]
    for nc in (new_chapters or []):
        if not isinstance(nc, dict):
            continue
        title = str(nc.get("title") or "").strip()
        if not title:
            continue
        after = str(nc.get("insert_after") or "").strip()
        node = {k: v for k, v in nc.items() if k != "insert_after"}
        node["title"] = title
        children = node.get("children")
        node["children"] = children if isinstance(children, list) else []
        pos = -1
        if after:
            titles = [str(ex.get("title") or "").strip() for ex in merged]
            exact = [i for i, t in enumerate(titles) if t and t == after]
            if len(exact) == 1:                      # ① 精确且唯一
                pos = exact[0] + 1
            elif not exact:                          # ② 无精确 → 互含回退
                fuzzy = [i for i, t in enumerate(titles)
                         if t and (after in t or t in after)]
                if len(fuzzy) == 1:
                    pos = fuzzy[0] + 1
        if pos < 0:
            merged.append(node)
        else:
            merged.insert(pos, node)
    _sh.renumber_outline(merged)
    return merged


async def _try_outline_patch(
    outline: list, missing: list, *,
    scheme_name: str = "", scheme_type: str = "",
    project_brief: str = "", project_facts: str = "",
) -> list | None:
    """外科式补齐：只为缺失项生成新的一级章节（1 次小调用替代
    「AI 审核 + 整目录重写」2 次大调用）。

    返回合并后的新目录；失败 / 校验不通过返回 None
    （调用方回退完整 AI 审核链路）。
    """
    if not missing:
        return None
    try:
        prompt = _sh.render(
            "outline_patch_system",
            scheme_name=scheme_name,
            scheme_type=scheme_type,
            project_brief=(project_brief or "")[:1500],
            project_facts=(project_facts or "")[:_sh.PROJECT_FACTS_LIMIT_OUTLINE],
            chapter_titles="; ".join(
                str(n.get("title") or "") for n in (outline or [])
                if isinstance(n, dict))[:2000],
            missing_items="; ".join(missing[:20]),
        )
        obj, _raw = await asyncio.wait_for(
            _sh.collect_json_response(
                [{"role": "system", "content": prompt}],
                _sh._outline_patch_validate_fn,
                json_mode=True, temperature=0.2, scene="outline_fix",
                repair_key=_sh.OUTLINE_REPAIR_KEY,
                max_tokens=_sh.OUTLINE_FIX_MAX_TOKENS),
            timeout=_sh.OUTLINE_FIX_TIMEOUT)
        new_chapters = obj.get("new_chapters") if isinstance(obj, dict) else None
        if not isinstance(new_chapters, list) or not new_chapters:
            return None
        return _sh.normalize_outline(_sh._merge_patch_chapters(outline, new_chapters))
    except asyncio.TimeoutError:
        _sh.logger.warning("外科式补齐超时（%ds），回退完整 AI 审核（scheme=%s）",
                       _sh.OUTLINE_FIX_TIMEOUT, scheme_name)
    except Exception as e:      # noqa: BLE001
        _sh.logger.warning("外科式补齐失败，回退完整 AI 审核（scheme=%s）: %s", scheme_name, e)
    return None


def _detect_generic_titles(outline: list) -> list[dict]:
    """泛化/占位标题检出（配置门控 + 异常软失败，全链路单一入口）。

    - 受 ``settings.outline_check_generic_titles``（默认 True）控制：置 False 即
      完整回到本项引入前的行为（不检出、不早退闸门、不强制修复）；
    - 检测异常**绝不阻断生成主链路**：返回空列表并留 WARNING。
    """
    if not getattr(_sh.settings, "outline_check_generic_titles", True):
        return []
    try:
        from app.services.outline_quality import detect_generic_titles
        return detect_generic_titles(outline)
    except Exception as e:  # noqa: BLE001 - 自检是体检，不是闸门
        _sh.logger.warning("目录泛化标题检测异常（忽略）: %s", e, exc_info=True)
        return []


def _detect_duplicate_titles(outline: list) -> list[dict]:
    """同名章节检出（fail-soft：异常返回空列表，绝不阻断生成主链路）。

    ✅ R89 S5：程序化早退的**第三道闸门**。判据复用
    `outline_quality.check_outline_continuity` 的 `duplicate_titles`（单一事实源，
    与审核侧 / 导出侧同口径），不另造判据。

    为何单独立函数：`_detect_generic_titles` 受 `outline_check_generic_titles`
    控制（占位标题检测），而重复标题检测语义不同，不该绑在同一开关下；
    两者都只读不写、都 fail-soft，本函数自带 try/except 兜底，**零新增配置项**。
    """
    try:
        from app.services.outline_quality import check_outline_continuity
        return check_outline_continuity(outline).get("duplicate_titles") or []
    except Exception as e:      # noqa: BLE001 - 自检是体检，不是闸门
        _sh.logger.warning("目录重复标题检测异常（忽略）: %s", e, exc_info=True)
        return []


#: 出口口径名：早退前先做过占位标题重写的标记（前端「审核依据」区可区分）
_TITLE_FIXED_MODE_SUFFIX = "titled"


def _residual_generic_notice(hits: list[dict]) -> list[str]:
    """残留占位标题的透明警告（参照 R71 收尾警告口径：绝不谎报已修好）。"""
    if not hits:
        return []
    sample = "、".join(f"「{h.get('title')}」" for h in hits[:5])
    return [
        f"⚠ 自动修复后目录仍有 {len(hits)} 处泛化/占位标题（{sample}）："
        f"请人工核对并改写这些标题，或重新生成目录（系统未强行改名，"
        f"避免把真实章节标题改坏）"]


def _coverage_recheck(requirements: str, tree: list, is_dangerous: bool,
                      basis=None) -> tuple[bool, list[str]]:
    """程序化出口的**覆盖回归复核**（✅ R73b F6/F7：纯确定性、零 AI 调用、不改树）。

    为何需要：两条程序化早退的「过」字是**在改名/补齐之前**判的，而两个动作都会
    动标题集：
      ① F6 占位标题曾以宽松匹配（≥4 字公共子串，规则 3）替某条编制要求「顶」住
         落点，换成具体标题后该要求反而没了落点；
      ② F7 外科补齐只要「有产出」就宣告「已补齐 N 项缺失」，从不回头验证补的章
         是否覆盖缺失项 —— 模型回任意一章都能过关，那是谎报。
    出口前用**同一把尺子**（`_check_requirements_coverage`，不另造判据）再量一次，
    不平就不得以 programmatic* 早退。

    复核自身异常按 ``(True, [])`` 处理（fail-soft：复核是加法式护栏，异常时
    不该比修复前更严，与 `_detect_generic_titles` 同一口径）。
    """
    try:
        return _sh._check_requirements_coverage(requirements, tree, is_dangerous,
                                           basis=basis)
    except Exception as e:  # noqa: BLE001
        _sh.logger.warning("程序化出口覆盖复核异常（按不额外阻断处理）: %s", e,
                       exc_info=True)
        return True, []


async def _review_and_fix_outline(
    outline: list, scheme_type: str, is_dangerous: bool, project_brief: str = "",
    scheme_name: str = "", project_facts: str = "", requirements: str = "",
    phase_cb=None, construction_scope: str = "",
    scheme_basis: str | None = None, basis=None,
    standards_text: str = "",
) -> tuple[list, dict]:
    """目录审核 + 超时保护的自动修复。返回 (outline, review_obj)。

    phase_cb: 可选同步回调 `fn(phase)`，在进入「审核」「修复」子阶段时调用，
    供调用方切换进度阶段（审核+修复最长 180s，期间调用方正 await 本协程、
    无法 yield 事件，进度改由心跳通道的 stats_provider 带出）。

    scheme_basis: 【方案名称解析】区块文本（_outline_scheme_basis 的产物）。
    ✅ 2026-09-26：旧实现写死 `scheme_basis=construction_scope`，同一段文字在
    审核提示词里以两个标签各注入一次（重复 token、且审核员按「工序/工艺/对象」
    判缺失，而生成侧从未收到该区块 → 系统性误判不通过、白跑一轮修复调用）。
    默认 None 时回退为 construction_scope（保持既有直调方行为不变）。

    standards_text: 【编制依据规范】文本（_outline_standards_text 的产物）。
    ✅ R89 G3：外科式标题重写需要它才能确定标题的具体工序/部位；默认 "" 表示
    不注入（渲染为空串），既有未传参的调用点行为不变。
    """
    if scheme_basis is None:
        scheme_basis = construction_scope
    if not outline:
        return outline, {"passed": False, "suggestions": ["目录为空，无法审核"]}

    def _notify(phase: str):
        """上报子阶段（回调异常绝不影响审核主流程）。"""
        if phase_cb is None:
            return
        try:
            phase_cb(phase)
        except Exception:
            _sh.logger.debug("目录审核阶段回调失败（已忽略）", exc_info=True)

    # ---------- C：程序化覆盖预检（OUTLINE_REVIEW_MODE=auto，默认） ----------
    # 省调用次数的关键路径：编制要求 / 法定必备章节若已被**确定性**覆盖，
    # 就不必再花 1~2 次大调用去问 AI「过不过」（AI 审核还会随机地
    # 对同一目录给出不同结论）。任一环节拿不准都回退完整 AI 审核。
    #
    # ✅ 2026-10-02（第二十五轮 · 门控对齐）：原门控是
    # ``(requirements or basis is not None)`` —— **未填「编制要求」时整段程序化
    # 预检被跳过**，于是非危大专项方案（本软件主力场景，§4.22 定位切换后）
    # 目录侧**从未被检查过九大法定章节**，而预检 ``check_completeness`` 对
    # CMP-01~09 是**无条件**检查的 → 生产库实证 6 条 CMP 命中（3 block）。
    #
    # 现改为：``_check_requirements_coverage`` **恒定参与**（其内部对
    # 「编制要求为空」本就返回空 missing，不产生额外判定），九大法定章节
    # 由 ``outline_checkpoint`` 单一出口按**审核侧同谓词**检查。
    # ⚠️ 保留 ``settings.outline_checkpoint_check`` 开关（默认 True）：设 False
    # 即完整回到本轮之前的门控与判据（可回退，不删代码）。
    if _sh.OUTLINE_REVIEW_MODE != "always" and (
            (requirements or basis is not None)
            or getattr(_sh.settings, "outline_checkpoint_check", True)):
        _covered, _missing = _sh._check_requirements_coverage(
            requirements, outline, is_dangerous, basis=basis)
        # ✅ R73 根因 A：占位标题**检出闸门**。旧实现两条程序化早退都在
        #    detect_generic_titles 之前 return，于是「填了编制要求 / 有方案解析」
        #    的目录（长方案常态）里的「专项6」「总体实施策划（2）」从未被检出、
        #    更从未被修，直接落库。现在：检出即**不得以 passed=True 早退**，
        #    先走一次小调用标题重写；重写后仍脏 → 落回完整 AI 审核 + 修复链路。
        _hits = _sh._detect_generic_titles(outline)
        if _hits:
            _sh.logger.info("程序化预检出口检出 %d 处泛化/占位标题，闸门不得早退",
                        len(_hits))
        # ✅ R89 S5：重复标题同为早退闸门（AI 审核的「跨章重复」检查不能跳过）。
        # 根因：`_level1_validate_fn` 只判 outline 非空 + title 非空 + 无 4 级，
        # **不判重复标题**；模型回同名两章时早退直接 passed=True，重复章进成品，
        # 而 failed_chapters 以**标题**为键（下方记账处），两章失败会同时高亮，
        # 用户无从分辨。现在：有重复即不早退，落回完整 AI 审核链路（审核提示词
        # 本就要求检查跨章重复）。
        _dups = _sh._detect_duplicate_titles(outline)
        if _dups:
            _sh.logger.info("程序化预检出口检出 %d 处重复标题，闸门不得早退", len(_dups))
        # ⚠️ 早退仍**只在用户填了编制要求 / 提供了方案解析**时成立
        #    （见 _check_requirements_coverage 末尾的说明）：否则会连带丢掉
        #    AI 审核对标题质量 / 层级 / 跨章重复 / 投标章节的检查。
        if not _missing and (requirements or basis is not None):
            if not _hits and not _dups:
                _sh.logger.info("程序化覆盖预检全过，跳过 AI 审核（省 1~2 次调用）")
                return outline, {"passed": True, "review_mode": "programmatic",
                                 "suggestions": ["✅ 程序化预检：编制要求与危大必备"
                                                 "章节均已覆盖，跳过 AI 审核"]}
            # 脏目录：先试外科式标题重写（1 次小调用，结构零风险）
            _titled = await _sh._try_outline_title_fix(
                outline, _hits, scheme_name=scheme_name, scheme_type=scheme_type,
                project_brief=project_brief, project_facts=project_facts,
                construction_scope=construction_scope, requirements=requirements,
                scheme_basis=scheme_basis, standards_text=standards_text,
                _collect=_sh.collect_json_response, _render=_sh.render)
            if _titled is not None and not _sh._detect_generic_titles(_titled):
                # ✅ R73b F6：改名后必须回头复检覆盖。旧实现只复检「标题脏不脏」，
                #    不复检「要求还盖不盖得住」—— 占位标题曾顶住的那条要求会在这
                #    一步静默失去落点，而出口仍以 passed=True 宣称通过。
                _covered2, _new_missing = _sh._coverage_recheck(
                    requirements, _titled, is_dangerous, basis)
                if not _covered2 and _new_missing:
                    outline = _titled
                    _missing = _new_missing
                    _sh.logger.info("占位标题已重写（%d 处），但覆盖出现新缺口 %d 项，"
                                "不得早退，转入补齐/审核链路：%s",
                                len(_hits), len(_new_missing), _new_missing[:5])
                else:
                    _sh.logger.info("占位标题已外科式重写（%d 处），出口干净", len(_hits))
                    return _titled, {
                        "passed": True, "review_mode": f"programmatic+{_sh._TITLE_FIXED_MODE_SUFFIX}",
                        "suggestions": [
                            f"✅ 程序化预检：必备章节均已覆盖；已将 {len(_hits)} 处"
                            f"泛化/占位标题重写为具体标题"]}
            # 重写不可用 / 仍有残留 → 不早退，落回完整 AI 审核链路
        if requirements or is_dangerous or basis is not None or (
                getattr(_sh.settings, "outline_checkpoint_check", True)):
            _notify("fix")
            _patched = await _sh._try_outline_patch(
                outline, _missing, scheme_name=scheme_name,
                scheme_type=scheme_type, project_brief=project_brief,
                project_facts=project_facts) if _missing else None
            if _patched:
                # ✅ R73 根因 A（第二条早退）：补齐后的目录仍可能带占位标题，
                #    旧实现直接以 passed=True 返回 → 同样从未被检出。先重写再判出口。
                _p_hits = _sh._detect_generic_titles(_patched)
                _p_titled = _patched
                if not _p_hits:
                    _p_missing: list[str] = []
                else:
                    _titled2 = await _sh._try_outline_title_fix(
                        _patched, _p_hits, scheme_name=scheme_name,
                        scheme_type=scheme_type, project_brief=project_brief,
                        project_facts=project_facts,
                        construction_scope=construction_scope, requirements=requirements,
                        scheme_basis=scheme_basis, standards_text=standards_text,
                        _collect=_sh.collect_json_response, _render=_sh.render)
                    if _titled2 is not None and not _sh._detect_generic_titles(_titled2):
                        _p_titled = _titled2
                        _p_missing = []
                    else:
                        # 仍有残留：带着补齐成果落回完整 AI 审核（不丢已补的章）
                        outline = _patched
                        _sh.logger.info("外科补齐后仍残留占位标题，转入完整 AI 审核链路")
                        _p_missing = None
                if _p_missing is not None:
                    # ✅ R73b F6/F7：补齐与改名都动过标题集，出口前回头复检。
                    #    旧实现只看「补齐调用有没有产出」，从不验证产出是否真盖住了
                    #    missing —— 模型回一章无关内容就能骗到「✅ 已外科式补齐 N 项」。
                    _cov_ok, _still_missing = _sh._coverage_recheck(
                        requirements, _p_titled, is_dangerous, basis)
                    if not _cov_ok and _still_missing:
                        outline = _p_titled
                        _missing = _still_missing
                        _sh.logger.info("外科补齐后仍有 %d 项未真正覆盖，不得以程序化"
                                    "出口宣称通过，转入 AI 审核：%s",
                                    len(_still_missing), _still_missing[:5])
                    elif _p_hits:
                        _sh.logger.info("外科补齐 %d 项 + 占位标题重写 %d 处均完成且复检通过",
                                    len(_missing), len(_p_hits))
                        return _p_titled, {
                            "passed": True,
                            "review_mode": (f"programmatic+surgical"
                                            f"+{_sh._TITLE_FIXED_MODE_SUFFIX}"),
                            "suggestions": [
                                f"✅ 已外科式补齐 {len(_missing)} 项缺失"
                                f"（{'；'.join(_missing[:5])}），"
                                f"并将 {len(_p_hits)} 处泛化/占位标题重写为具体标题"]}
                    else:
                        _sh.logger.info("外科式补齐完成（缺失 %d 项）且复检通过", len(_missing))
                        return _p_titled, {
                            "passed": True, "review_mode": "programmatic+surgical",
                            "suggestions": [
                                f"✅ 已按程序化预检外科式补齐 {len(_missing)} 项缺失"
                                f"（{'；'.join(_missing[:5])}）"]}
            # 补齐失败 → 落回完整 AI 审核链路（不写 review_mode，
            # 前端据此判断「审核依据仍是 AI 审核结论」）
    # 说明（✅ 2026-10-02 已更新）：程序化预检的**触发门控**已放宽 ——
    #   「编制要求」为空时，九大法定章节检查（outline_checkpoint 单一出口）
    #   仍会执行，因预检 check_completeness 对 CMP-01~09 是无条件检查。
    #   「编制要求」本身无内容时其条目检查自然为空，不产生额外判定。
    #   关闭 settings.outline_checkpoint_check 即完整回到旧门控与旧判据。

    review_prompt = _sh.render("outline_review_system",
                           scheme_name=scheme_name,
                           scheme_type=scheme_type,
                           # ✅ 必须传 construction_scope / scheme_basis：
                           #    模板含这两个占位符，未传时**字面量 {scheme_basis} 会
                           #    残留进发给模型的提示词** —— 模型要么把它当正文、
                           #    要么按未知变量编造内容，审核结论随之失真。
                           #    2026-09-26：两者改为**不同来源**（施工内容清单 /
                           #    方案名称解析区块），不再同值双注入。
                           construction_scope=construction_scope,
                           scheme_basis=scheme_basis,
                           is_dangerous="是" if is_dangerous else "否",
                           # ✅ R38 D4：审核链路**有意不截断**事实（只读核对，
                           #    事实越全审得越准；与生成侧预算解耦，勿顺手对齐）
                           project_facts=project_facts or "",
                           outline_json=json.dumps(_sh._outline_skeleton(outline), ensure_ascii=False),
                           # NOTE(R38 D5): MUST be the review variant, not the shared one --
                           # the "8.1 audit against the checklist" instruction has to live and
                           # die with the checklist block. With the shared variant, turning
                           # outline_checkpoint_check off leaves a dangling reference to a
                           # checklist that is not in the prompt at all.
                           **_sh._outline_review_checkpoint_kwargs(
                               scheme_name, scheme_type, is_dangerous))
    # ✅ 编制要求覆盖检查（对齐 OpenBidKit score-planning 的「评分大项必须映射为目录分支」）：
    #    用户在方案设置里填写的编制要求/评审要点，逐条对应目录章节；缺失即审核不通过。
    if requirements:
        review_prompt += (
            "\n\n【编制要求覆盖检查（最高优先级）】目录必须逐条覆盖以下编制要求；"
            "任何一条在目录中没有对应章节（或仅含糊带过），即判 passed=false，"
            "并在 suggestions 中逐条列出缺失项及建议补充的章节：\n" + requirements)
    # ✅ BUG 修复（2026-10-04）：outline_review_system 第 10 条把「合并修复输出」
    #    的触发条件写成"调用方在追加指令中要求合并修复"，但本调用方此前从未
    #    追加任何要求合并修复的指令，导致模型永远不输出 fixed_outline，第 10 条
    #    的合并快路径形同虚设。此处补上显式触发指令，让第 3581 行的合并修复
    #    分支有机会命中，从而省下一轮独立修复调用（OUTLINE_FIX_TIMEOUT 的 120s）。
    review_prompt += (
        "\n\n【合并修复】本次调用要求合并修复：若判定 passed=false，"
        "请务必在同一 JSON 中额外输出 \"fixed_outline\" 字段（按 suggestions "
        "修正后的完整三级目录数组，节点 description 保持原文不要改写）；"
        "passed=true 时不要输出 fixed_outline。")
    _notify("review")
    try:
        review_obj, _ = await asyncio.wait_for(
            _sh.collect_json_response(
                [{"role": "system", "content": review_prompt}],
                lambda o: [] if "passed" in o else ["缺少 passed 字段"],
                json_mode=True, temperature=0.2, scene="outline_review",
                repair_key=_sh.OUTLINE_REPAIR_KEY,
                max_tokens=_sh.OUTLINE_REVIEW_MAX_TOKENS),
            timeout=_sh.OUTLINE_REVIEW_TIMEOUT)
    except asyncio.TimeoutError:
        _sh.logger.warning("目录审核超时（%ds），跳过审核直接完成（scheme=%s）",
                       _sh.OUTLINE_REVIEW_TIMEOUT, scheme_name)
        review_obj = {"passed": True, "suggestions": ["审核超时已跳过"]}
    except Exception as e:
        _sh.logger.warning("目录审核失败（scheme=%s）: %s，跳过审核直接完成", scheme_name, e)
        review_obj = {"passed": True, "suggestions": [f"审核失败: {e}"]}

    if not isinstance(review_obj, dict):
        # 弱模型可能把审核结果写成顶层数组/裸值，兜底避免后续 .get 抛 AttributeError
        review_obj = {"passed": True, "suggestions": ["审核结果结构异常，已跳过"]}
    # ✅ BUG 修复：统一把 passed / suggestions 归一化为 bool / list[str]
    #（字符串 "false" 不再被当作通过；字符串 suggestions 不再被按字符拆分）
    suggestions = _sh._coerce_suggestions(review_obj.get("suggestions"))
    # ✅ 2026-10-10（目录占位/泛化标题收口）：AI 审核可能漏报泛化占位标题与
    #    「（N）」编号冲突后缀（生产实证「专项6」「总体实施策划（2）」）。
    #    ✅ R73 加强：先试**外科式标题重写**（只送占位节点 + 上下文，结构零风险）；
    #    重写干净则不强迫走「整份重写」（长方案下极易被退化护栏拒绕而「保留原目录」）；
    #    仍有残留才追加针对性修复建议并强制进入修复链路。
    _generic_hits = _sh._detect_generic_titles(outline)
    if _generic_hits:
        _titled3 = await _sh._try_outline_title_fix(
            outline, _generic_hits, scheme_name=scheme_name,
            scheme_type=scheme_type, project_brief=project_brief,
            project_facts=project_facts,
            construction_scope=construction_scope, requirements=requirements,
            scheme_basis=scheme_basis, standards_text=standards_text,
            _collect=_sh.collect_json_response, _render=_sh.render)
        if _titled3 is not None:
            outline = _titled3
            _still = _sh._detect_generic_titles(outline)
            if not _still:
                _sh.logger.info("占位标题已由外科式重写全部修正（%d 处）",
                            len(_generic_hits))
                _generic_hits = []
            else:
                _generic_hits = _still
    if _generic_hits:
        _generic_sugg = [
            f"标题「{g['title']}」（路径{g['path']}，类型{g['kind']}）为泛化/占位名称"
            f"或带（N）编号冲突后缀，请改为具体到施工对象/部位/工序的真实标题，"
            f"并去除（N）类后缀"
            for g in _generic_hits[:12]
        ]
        suggestions = suggestions + _generic_sugg
        review_obj["passed"] = False
        _sh.logger.info("目录检出 %d 处泛化/占位标题，强制进入修复链路", len(_generic_hits))
    # ✅ 审核判定收紧（原实现 default=True 语义过宽）：
    #    模型漏填 passed 时，旧逻辑一律「视为通过」→ 审核形同虚设。
    #    现区分三种情形：
    #      - passed 缺失且给出了具体建议 → 按不通过处理（修复有明确输入）；
    #      - passed 缺失且无任何建议 → 视为通过（避免无依据地空跑一轮修复）；
    #      - passed 存在 → 按 _coerce_bool 归一化（"false"/"否"/0 等均判不通过）。
    _passed_raw = review_obj.get("passed")
    if _passed_raw is None:
        needs_fix = bool(suggestions)
    else:
        needs_fix = not _sh._coerce_bool(_passed_raw, default=True)

    if needs_fix and not suggestions:
        # 判不通过却没给建议：旧实现直接静默跳过修复，用户拿到未修正的目录
        # 且看不到任何原因。这里补一条兜底建议，让修复轮有输入、日志有归因。
        suggestions = ["审核判定目录未通过但未给出具体建议，请按编制要求与危大工程规范补齐必要章节"]
        _sh.logger.info("目录审核判不通过但未给出建议，已注入兜底修复建议")

    if needs_fix:
        _notify("fix")

        # ---------- A：审核 + 修复合并（省 1 次大调用） ----------
        # 审核提示词第 10 条已要求模型在 passed=false 时同轮输出 fixed_outline。
        # 采用条件（三者全满足，缺一即回退独立修复调用）：
        #   ① 结构合法（_outline_fix_validate_fn）；
        #   ② 未退化（节点覆盖率 ≥ OUTLINE_FIX_MIN_COVERAGE）；
        #   ③ 描述可还原（模型回传的是 60 字截断骨架版，须恢复原文）。
        # 弱模型常只回一个"示例结构"，直接整份替换会把用户几十章目录换成
        # 三章（正文随之全丢）—— 这是最危险的失败模式，故宁可不合并。
        _merged_fixed = review_obj.get("fixed_outline")
        if isinstance(_merged_fixed, list) and _merged_fixed:
            _issues = _sh._outline_fix_validate_fn({"outline": _merged_fixed})
            if not _issues and _sh._outline_fix_looks_degraded(outline, _merged_fixed):
                _issues = [
                    f"审核轮合并修复结果节点数异常（{_sh._count_nodes(_merged_fixed)}"
                    f" < 原目录 {_sh._count_nodes(outline)} 的 "
                    f"{int(_sh.OUTLINE_FIX_MIN_COVERAGE * 100)}%）"]
            if not _issues:
                # ✅ 恢复原描述：审核输入是 _outline_skeleton 截断版，
                # 模型回传的 description 同样是截断的；按 (id, title) 回填原文。
                _sh._restore_descriptions(outline, _merged_fixed)
                outline = _sh.normalize_outline(_merged_fixed)
                # ✅ R73 根因 B：修复采用后**必须复检**。旧实现无条件置 passed=True
                #    并声称「已自动修复」，而弱模型的 fixed_outline 常照旧把占位标题
                #    原样回吐 ⇒ 结论与日志都不准。残留时如实置 False + 给可核对警告。
                _residual = _sh._detect_generic_titles(outline)
                review_obj["generic_residual"] = len(_residual)
                review_obj["passed"] = not _residual
                if _residual:
                    suggestions = (_sh._residual_generic_notice(_residual)
                                   + suggestions)
                    _sh.logger.warning("审核轮合并修复后仍残留 %d 处占位标题（不判通过）",
                                   len(_residual))
                else:
                    suggestions = (["✅ 已根据审核意见自动修复（审核轮合并输出）"]
                                   + suggestions)
                review_obj["suggestions"] = suggestions
                return outline, review_obj
            _sh.logger.warning("审核轮合并修复结果不可用，回退独立修复调用（scheme=%s）: %s",
                           scheme_name, _issues[:2])

        async def _do_fix():
            # ✅ 统一提示词源：消费注册条目 outline_feedback_system（旧内联版与注册条目双份漂移，
            # 前端编辑该条目永不生效）
            # ✅ 修复（2026-09-16）：原来传入 json.dumps(outline) 完整目录（含
            #    id/level/空 children/长描述，长方案可超 10 万字符），必然超上下文
            #    或超时 ⇒ 修复轮整轮空转。改用 _compact_outline_json 压缩（同语义、
            #    字符量约降 60%），并记日志便于排查。命中路径由 json 保持合法。
            _orig_json = _sh._compact_outline_json(outline)
            _sh.logger.info("目录修复轮输入：原目录 %d 节点 / 压缩后 %d 字符",
                        _sh._count_nodes(outline), len(_orig_json))
            fix_prompt = _sh.render("outline_feedback_system",
                                original_outline=_orig_json,
                                review_suggestions="; ".join(suggestions),
                                scheme_name=scheme_name,
                                scheme_type=scheme_type,
                                project_brief=(project_brief or "")[:1500],
                                project_facts=(project_facts or "")[:_sh.PROJECT_FACTS_LIMIT_OUTLINE])
            return await _sh.collect_json_response(
                [{"role": "system", "content": fix_prompt}],
                _sh._outline_fix_validate_fn,
                json_mode=True, temperature=0.2, scene="outline_fix",
                repair_key=_sh.OUTLINE_REPAIR_KEY,
                max_tokens=_sh.OUTLINE_FIX_MAX_TOKENS)

        try:
            fix_obj, _ = await asyncio.wait_for(_do_fix(), timeout=_sh.OUTLINE_FIX_TIMEOUT)
            fixed_outline = fix_obj.get("outline", []) if isinstance(fix_obj, dict) else []
            if not isinstance(fixed_outline, list):
                fixed_outline = []
            # ✅ BUG 修复：修复结果只做"结构性"校验（strict_depth=False），
            # 且节点上限放宽到 OUTLINE_FIX_MAX_NODES —— 修复轮输入/输出都是
            # 完整目录，长方案极易超过生成链路的 500 节点上限，沿用旧校验会把
            # 修复结果整份丢弃、"按审核建议修复"空转。
            validation_issues = _sh._outline_fix_validate_fn({"outline": fixed_outline})
            # ✅ 修复（2026-09-16）：结构合法 ≠ 结果可用。弱模型常只回一个"示例
            #    结构"，旧实现会把它整份替换掉用户几十章的目录（正文生成的章节
            #    随之全丢）。这里追加节点覆盖率下限判定。
            if not validation_issues and _sh._outline_fix_looks_degraded(outline, fixed_outline):
                validation_issues = [
                    f"修复结果节点数异常（{_sh._count_nodes(fixed_outline)} < "
                    f"原目录 {_sh._count_nodes(outline)} 的 {int(_sh.OUTLINE_FIX_MIN_COVERAGE * 100)}%）"
                ]
            if validation_issues:
                _sh.logger.warning("修复后目录校验仍不通过: %s", validation_issues[:3])
                suggestions = suggestions + ["修复后校验仍不通过，保留原目录"]
            else:
                # 修复结果落库前统一裁剪到三级 + 重排编号，与主链路保持一致
                outline = _sh.normalize_outline(fixed_outline)
                # ✅ R73 根因 B（独立修复轮同样复检）
                _residual2 = _sh._detect_generic_titles(outline)
                review_obj["generic_residual"] = len(_residual2)
                review_obj["passed"] = not _residual2
                # ✅ 保留原始审核建议（追加而非覆盖）：前端「审核结论」区块要展示
                #    本轮到底按哪些建议改的，旧实现直接替换成一句话，审核依据丢失。
                if _residual2:
                    suggestions = (_sh._residual_generic_notice(_residual2)
                                   + suggestions)
                    _sh.logger.warning("独立修复轮后仍残留 %d 处占位标题（不判通过）",
                                   len(_residual2))
                else:
                    suggestions = ["✅ 已根据审核意见自动修复"] + suggestions
        except asyncio.TimeoutError:
            _sh.logger.warning("目录自动修复超时（%ds），跳过修复保留原目录（scheme=%s）",
                           _sh.OUTLINE_FIX_TIMEOUT, scheme_name)
            suggestions = suggestions + ["自动修复超时，保留原目录"]
        except Exception as e:
            _sh.logger.warning("目录自动修复失败（scheme=%s）: %s，保留原目录", scheme_name, e)
            suggestions = suggestions + [f"自动修复失败: {e}"]

    # 统一回写为 list[str]，保证下游（SSE payload / 前端）拿到的形态稳定
    review_obj["suggestions"] = suggestions
    # ✅ R73：未走修复采用的出口同样给一个稳定的残留读数（前端/护栏可消费）
    review_obj.setdefault("generic_residual",
                          len(_sh._detect_generic_titles(outline)))
    return outline, review_obj


# ---------- 目录生成 ----------

async def _build_structured_brief(db, project_id: str, raw_brief: str,
                                  max_chars: int = 4000, basis=None) -> str:
    """构建目录生成用的「项目摘要」：优先消费结构化提取成果，回退原始摘录。

    数据来源：bid_analysis_items（「结构化提取」18 项的 success 结果），
    经 format_downstream_context 拼为结构化 Markdown；原始文档摘录作为
    附录补充（截 800 字），保证未覆盖的信息仍可见。

    ✅ 方案名称主线（basis，2026-09-27）：按与方案名称的相关性把相关小节前置
    （_rank_sections_by_basis，只重排不删除）—— 4000 字预算下"相关项优先可见"。

    ✅ 容错：结构化提取未跑过 / 表不存在 / 单条内容损坏时一律回退 raw_brief，
    不影响目录生成主流程。
    """
    try:
        cur = await db.execute(
            "SELECT item_id, label, output_type, content FROM bid_analysis_items "
            "WHERE project_id=? AND status='success' AND content IS NOT NULL "
            "AND content != ''", (project_id,))
        rows = await cur.fetchall()
        if not rows:
            return raw_brief
        from app.services.bid_analysis_service import (
            format_downstream_context,
            is_missing_result,
        )
        items: dict[str, dict] = {}
        for r in rows:
            content = r["content"] or ""
            output_type = r["output_type"] or "markdown"
            if is_missing_result(content, output_type):
                continue
            items[r["item_id"]] = {
                "item_id": r["item_id"],
                "label": r["label"] or r["item_id"],
                "output_type": output_type,
                "status": "success",
                "content": content,
            }
        if not items:
            return raw_brief
        structured = format_downstream_context(items)
        if not structured.strip():
            return raw_brief
        # ✅ 2026-09-27：按方案名称相关性前置小节（只重排不删除，见 _rank_sections_by_basis）
        structured, _ranked = _sh._rank_sections_by_basis(structured, basis)
        # ✅ 边界感知截断（m4 · 2026-09-23）：按小节比例分配预算后再做
        #    **边界感知**截断（truncate_to_boundary），不再硬切 —— 硬切会把
        #    小节或 ``` 围栏切成两半，模型看到半截代码块会当成坏数据忽略。
        from app.utils.text_splitter import truncate_to_boundary
        structured, rep = _sh._budgeted_truncate_sections(structured, max_chars)
        structured = truncate_to_boundary(structured, max_chars)
        if rep.get("truncated_sections"):
            _sh.logger.info("结构化项目摘要按小节比例截断：%d/%d 小节被截（预算 %d 字）",
                        rep["truncated_sections"], rep["sections"], max_chars)
        appendix = f"\n\n【原始资料摘录】\n{raw_brief[:800]}" if raw_brief.strip() else ""
        # ⚠️ 不得在此再做 `[:max_chars+900]` 硬切片 —— 那会把刚按比例
        #    分配好的尾部小节再次砍掉（比例截断等于白做）。此处直接拼接。
        return structured + appendix
    except Exception:
        _sh.logger.warning("结构化项目摘要构建失败，回退原始资料摘录", exc_info=True)
        return raw_brief


async def _outline_input_audit(db, project_id: str) -> str:
    """目录生成的输入侧差集审计（只读，仅供日志/排查，不参与生成决策）。

    返回已成功完成结构化提取的条目名（分号连接，截断 30 条）。
    用途：用户报"目录漏了本该有的章节"时，可直接比对「提取到了什么」
    与「目录写出了什么」，无需手工查库。
    """
    try:
        cur = await db.execute(
            "SELECT item_id, label, status FROM bid_analysis_items "
            "WHERE project_id=? ORDER BY item_id", (project_id,))
        rows = await cur.fetchall()
        done = [str(r["label"] or r["item_id"]) for r in rows
                if (r["status"] or "") == "success"]
        return "；".join(done[:30])
    except Exception:
        _sh.logger.debug("目录输入差集审计读取失败（忽略）", exc_info=True)
        return ""


def _scheme_basis_obj(scheme: dict):
    """方案名称解析对象（``scheme_basis.SchemeBasis``），失败降级为 None。

    单点构建：相关性前置（_rank_*_by_basis）、全面性/冗余校验
    （outline_quality）都消费**同一个**对象，避免各处重复解析口径漂移。
    """
    if not getattr(_sh.settings, "outline_name_basis", False):
        return None
    try:
        from app.services.scheme_basis import parse_scheme_basis
        return parse_scheme_basis(str(scheme.get("name") or ""))
    except Exception:
        _sh.logger.warning("方案名称解析对象构建失败（目录生成降级继续）", exc_info=True)
        return None


def _scheme_is_dangerous(scheme: dict) -> bool:
    """是否危大工程：**schemes.type 下拉值命中** 或 **方案名称字面命中六大类危大**。

    ✅ 2026-09-27（要求一/四）：旧实现只认 type 下拉值，而危大判定是目录生成
    最强的结构性约束（必备 10 章 + 提示词"危大工程专项方案必须包含…"）。
    用户新建方案时 type 常常停在默认「其它」，于是"深基坑支护专项方案"被
    当成普通方案生成目录 → 缺计算书/监测方案/应急处置等必备章节。
    config.outline_name_basis 的注释早已写明"type 命中 DANGEROUS_TYPES **或**
    名称字面命中六大类危大"，此处补齐实现；type 命中仍然生效（行为只增不减），
    关闭 outline_name_basis 时回退旧口径（纯 type），保持可关。
    """
    if str(scheme.get("type") or "") in _sh.DANGEROUS_TYPES:
        return True
    if not getattr(_sh.settings, "outline_name_basis", False):
        return False
    try:
        from app.services.scheme_basis import parse_scheme_basis
        return bool(parse_scheme_basis(str(scheme.get("name") or "")).is_dangerous)
    except Exception:
        _sh.logger.warning("方案名称危大判定失败（回退 type 口径）", exc_info=True)
        return False


def _outline_construction_scope(scheme: dict) -> str:
    """方案名称 → 【方案名称主要施工内容】清单文本（章节划分的直接依据）。

    ✅ BUG 修复（2026-09-26 · 目录生成提示词依据错位）：旧实现返回的是
    ``scheme_basis.parse_scheme_basis().prompt_text()`` —— 那是一份含「方案类型 /
    主要施工内容 / 施工工序 / 施工工艺 / 施工对象 / 危大分类」六个维度的**多行区块**，
    被塞进标签为「主要施工内容」的变量里（标签与内容不符），并且与模板中的
    ``{scheme_basis}`` 变量**内容完全重复**：审核提示词里同一段文字出现两次，
    而生成提示词里 ``{scheme_basis}`` 因为从来没人传值被整行丢弃（见
    _outline_scheme_basis）。现按变量本意拆分：本函数只产出「主要施工内容」清单
    （services/scheme_scope 是唯一实现），其余维度由 _outline_scheme_basis 供给。

    受 settings.outline_name_basis 控制（默认 True）。关闭 / 名称拆不出任何内容项时
    返回空串，提示词对应占位符渲染为空 —— 与该开关引入前的行为逐字一致。
    """
    if not getattr(_sh.settings, "outline_name_basis", False):
        return ""
    try:
        from app.services.scheme_scope import (
            extract_construction_scope,
            render_scope_for_prompt,
        )
        name = str(scheme.get("name") or "")
        # 拆不出条目时返回空串（而非 render_scope_for_prompt 的兜底占位文案）：
        # 保持「无内容 → 整段不注入」的既有语义，调用点的 `if construction_scope:`
        # 分支才不会为占位文案额外拼一条 user 消息。
        return render_scope_for_prompt(name) if extract_construction_scope(name) else ""
    except Exception:
        _sh.logger.warning("方案名称解析失败（目录生成降级继续）", exc_info=True)
        return ""


def _outline_scheme_basis(scheme: dict) -> str:
    """方案名称确定性解析 → 提示词【方案名称解析】区块文本。

    ✅ BUG 修复（2026-09-26 · 「配了不生效」类静默失效）：目录模板
    outline_short_system / outline_level1_system / outline_review_system 自
    2026-09-23「四项依据」改造起就声明了 ``{scheme_basis}``
    （PROMPT_VARIABLE_CONTRACTS 已登记），但**两条生成链路从未传值** ——
    render_prompt 对「整行独占占位符」按可选区块处理（未解析即整行丢弃），
    于是发给模型的提示词里从来没有【方案名称解析】这一段，而模板 0.1 条款
    （"若提供了【方案名称解析】（含方案类型/施工工序/施工工艺/施工对象），
    则二级章节必须逐一落实其中列出的每一项施工工序与施工工艺…"）**永远不生效**，
    每次目录生成还额外打一条 "unresolved placeholders: ['scheme_basis']" WARNING。
    审核链路反而把 construction_scope 当 scheme_basis 传了同一份值（重复注入）。
    现补齐生成侧注入、审核侧改用本函数，三处口径统一。

    同样受 settings.outline_name_basis 控制；无任何可解析维度时返回空串 =
    整段不注入（保持「信息不足时不得编造」红线）。
    """
    if not getattr(_sh.settings, "outline_name_basis", False):
        return ""
    try:
        from app.services.scheme_basis import parse_scheme_basis
        text = str(parse_scheme_basis(
            str(scheme.get("name") or "")).prompt_text() or "").strip()
    except Exception:
        _sh.logger.warning("方案名称解析（scheme_basis）构建失败（目录生成降级继续）",
                       exc_info=True)
        return ""
    if not text:
        return ""
    # ✅ P1-3（2026-09-27 · 补齐遗留项）：把**标准章节模板**注入提示词。
    #    `outline_templates` 有 24 套标准章节模板（match_template/_TEMPLATE_NAMES），
    #    `parse_scheme_basis` 也已解析出 `template_key`，但此前**从未进入提示词** ——
    #    目录骨架完全依赖模板里硬编码的九大章节列表，模板能力形同虚设。
    #    受 settings.outline_template_inject 控制（默认 True = 生效；
    #    关闭即回退到"只用通用骨架"的修复前行为）。
    if getattr(_sh.settings, "outline_template_inject", True):
        try:
            from app.services.outline_templates import _TEMPLATE_NAMES
            key = parse_scheme_basis(str(scheme.get("name") or "")).template_key
            tname = str(_TEMPLATE_NAMES.get(key) or "").strip()
            if tname:
                text += f"\n标准章节模板：{tname}（其章节骨架为通用九大章节结构的"\
                        f"专项变体，优先于通用骨架；缺失的专项子节用名称解析的工序/工艺/对象补齐）"
        except Exception:
            _sh.logger.debug("标准章节模板注入失败（降级为仅通用骨架）", exc_info=True)
    return ("【方案名称解析（按方案名称字面确定性拆解，章节划分与三级标题须逐项落实，"
            "不得虚构名称之外的内容）】：\n" + text)


def _outline_standards_text(scheme: dict) -> str:
    """按方案类别匹配的编制依据规范文本（供提示词引用，防杜撰编号）。

    受 settings.outline_standards_inject 控制（默认 True）；关闭或异常时
    返回空串，提示词对应占位符渲染为空。
    """
    if not getattr(_sh.settings, "outline_standards_inject", False):
        return ""
    try:
        return _sh.get_standards_text(
            str(scheme.get("type") or ""), str(scheme.get("name") or ""))
    except Exception:
        _sh.logger.warning("编制依据规范构建失败（目录生成降级继续）", exc_info=True)
        return ""


async def _register_task_exclusive(
    kind: str, project_id: str, scheme_id: str, conflict_types: tuple = (),
) -> tuple[str | None, "TaskTypeConflict | None"]:
    """注册任务并把跨类型互斥冲突转成返回值（F5 · 2026-10-05）。

    跨类型互斥的**判定**在 ``task_registry.register_task`` 的 ``_register_lock``
    内完成（锁内、本任务 INSERT 之后），这里只负责把 ``TaskTypeConflict`` 变成
    调用方可用的 ``(None, exc)`` 二元组 —— 这样两个 SSE 生成器的
    ``event_stream`` 里**不会新增 try 块**：既有静态护栏要求「进度/计数器等
    预初始化必须在外层 try 之前」，在 event_stream 头部插 try 会把该护栏
    锚定的「第一个 try」顶掉（2026-10-05 全量跑当场抓到 3 例）。
    """
    try:
        return await _sh.register_task(kind, project_id, scheme_id,
                                   conflict_types=conflict_types), None
    except TaskTypeConflict as tc:
        return None, tc


def _stream_end_guard(holder: dict):
    """生成 :func:`with_heartbeat` 的 ``on_end`` 同步回调（R106 FIX-6）。

    ``holder["task_id"]`` 由各链的 ``event_stream`` 在注册成功后写入。SSE 泵
    结束时（正常完成 / 客户端断开 / 取消风暴）本回调在 with_heartbeat 的
    finally 里被**同步**调用 —— 同步代码不受协程取消影响，一定会执行；而生成器
    自己的落终态收尾（checkpoint → finish_task）是一串 await，实测会被取消风暴
    打断，留下「内存 running、无人可清」的僵尸（活体 E2E · task=b3dc5ae0…）。
    交给 task_registry.mark_stream_ended 宽限回收：正常收尾仍优先，到期仍在
    running 才回收，语义与 event_stream finally 既有的「客户端断开，任务已终止」
    一致。回调自身抛异常只记 WARNING，绝不干扰流收尾。
    """
    def _on_end():
        tid = holder.get("task_id")
        if not tid:
            return
        try:
            _sh._tr.mark_stream_ended(tid)
        except Exception:
            _sh.logger.warning("mark_stream_ended 调用失败（task=%s）", tid, exc_info=True)
    return _on_end


