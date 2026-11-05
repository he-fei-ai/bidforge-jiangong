"""✅ D1（2026-10-11 · 离散正文编排阶段）

在正式撰写某一章节正文之前，由「独立的 LLM 步」为每章产出一份结构化编排决策
（写作聚焦 / 建议素材 / 建议事实 / 是否表格），作为**软建议**注入正文生成阶段。

设计原则（对齐本软件 R57 纪律）：
- 本模块**只做软建议**（建议优先使用），**不硬限制**模型可用素材/事实范围，
  刻意回避 R57 已论证的「预编排=陈旧结论陷阱」硬约束风险。
- 编排失败（LLM 异常 / JSON 解析失败）一律**回退为「无编排」**，绝不阻断正文生成。
- 纯函数（`ContentPlanData.validate` / `render_content_plan_guidance`）不依赖 IO，
  便于单测；IO 部分（`plan_section_content`）以注入 `chat_fn` 解耦，便于 mock。

该能力默认关闭（`config.content_plan_stage_enabled=False`），关闭时调用方
（sse_handlers.generate_content）不会触发任何额外 LLM 调用，行为与本项引入前逐字一致。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

#: ✅ R178（D-4 · 2026-11-05）：编排步的输出预算地板。
#: 旧实现硬编码 ``max_tokens=600`` —— 对**推理模型**必然被思考过程吃光
#: （生产日志：custom 模型 600→1200 翻倍后仍「思考吞噬 max_tokens（正文为空）」），
#: 对非推理模型也偏紧（编排 JSON 含 writing_focus 句 + knowledge_names/fact_titles
#: 两个列表 + table_purpose，容易触顶 → 截断 → 进 JSON 修复轮 → 仍失败 →
#: 静默降级为「无编排」）。生产实证：8 天内 158 条「离散编排阶段 LLM 调用失败，
#: 回退为无编排」——**编排阶段事实上长期处于关闭状态，而用户毫不知情**。
#: 现提到与正文首稿同口径的地板（1536）；provider 层对推理模型还会再抬到
#: ``ai_reasoning_output_floor``（默认 4096）。
CONTENT_PLAN_MAX_TOKENS = 1536


@dataclass
class ContentPlanData:
    """单章离散编排决策。

    - writing_focus: 本章写作聚焦（1-2 句）
    - knowledge_names: 建议优先使用的知识库素材名（必须落在候选池内）
    - fact_titles: 建议优先引用的全局事实标题（必须落在候选池内）
    - table_needed: 是否建议本章使用表格
    - table_purpose: 为何需要表格（无需时为空串）
    """

    writing_focus: str = ""
    knowledge_names: list[str] = field(default_factory=list)
    fact_titles: list[str] = field(default_factory=list)
    table_needed: bool = False
    table_purpose: str = ""

    @classmethod
    def empty(cls) -> "ContentPlanData":
        return cls()

    def to_json(self) -> str:
        return json.dumps(
            {
                "writing_focus": self.writing_focus,
                "knowledge_names": self.knowledge_names,
                "fact_titles": self.fact_titles,
                "table_needed": self.table_needed,
                "table_purpose": self.table_purpose,
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, raw: str | dict) -> "ContentPlanData":
        """反序列化已落库的编排决策：不做候选池过滤（池过滤只发生在 LLM 产出时的
        validate，落库后名称即权威），仅做类型兜底。"""
        if isinstance(raw, str):
            try:
                d = json.loads(raw)
            except Exception:  # noqa: BLE001 - 坏 JSON 直接视为空编排
                return cls.empty()
        else:
            d = raw
        if not isinstance(d, dict):
            return cls.empty()

        kn = d.get("knowledge_names") or []
        ft = d.get("fact_titles") or []
        kn = [str(x).strip() for x in kn if isinstance(x, str) and x.strip()] if isinstance(kn, list) else []
        ft = [str(x).strip() for x in ft if isinstance(x, str) and x.strip()] if isinstance(ft, list) else []
        tn = d.get("table_needed", False)
        if isinstance(tn, str):
            tn = tn.strip().lower() in ("true", "是", "yes", "1")
        return cls(
            writing_focus=str(d.get("writing_focus") or "").strip(),
            knowledge_names=kn,
            fact_titles=ft,
            table_needed=bool(tn),
            table_purpose=str(d.get("table_purpose") or "").strip(),
        )

    @classmethod
    def validate(
        cls,
        d: dict,
        *,
        knowledge_pool: list[str] | None = None,
        fact_pool: list[str] | None = None,
    ) -> "ContentPlanData":
        """纯函数：把 LLM 原始 dict 规整为本软件受控结构。

        只保留落在候选池内的素材/事实名（防止模型编造条目），并对类型做兜底。
        """
        knowledge_pool = knowledge_pool or []
        fact_pool = fact_pool or []
        if not isinstance(d, dict):
            return cls.empty()

        writing_focus = str(d.get("writing_focus") or "").strip()

        kn_raw = d.get("knowledge_names") or []
        if not isinstance(kn_raw, list):
            kn_raw = []
        kn = [str(x).strip() for x in kn_raw if str(x).strip()]
        # 仅保留候选池内的（模型若编造候选外条目一律丢弃）
        kn = [x for x in kn if x in knowledge_pool]

        ft_raw = d.get("fact_titles") or []
        if not isinstance(ft_raw, list):
            ft_raw = []
        ft = [str(x).strip() for x in ft_raw if str(x).strip()]
        ft = [x for x in ft if x in fact_pool]

        tn = d.get("table_needed", False)
        if isinstance(tn, str):
            tn = tn.strip().lower() in ("true", "是", "yes", "1")
        tn = bool(tn)

        table_purpose = str(d.get("table_purpose") or "").strip()

        return cls(
            writing_focus=writing_focus,
            knowledge_names=kn,
            fact_titles=ft,
            table_needed=tn,
            table_purpose=table_purpose,
        )


def render_content_plan_guidance(plan: ContentPlanData | None) -> str:
    """纯函数：把编排决策渲染成可注入「本章任务差异段」的软建议文本。

    返回空串表示无编排（调用方据此不注入任何内容，等价于旧行为）。
    """
    if plan is None:
        return ""
    parts = ["【本章离散编排要点（程序预规划，仅供行文聚焦参考，不限制可用素材范围）】："]
    if plan.writing_focus:
        parts.append(f"- 写作聚焦：{plan.writing_focus}")
    if plan.knowledge_names:
        parts.append("- 建议优先使用的知识库素材：" + "、".join(plan.knowledge_names))
    if plan.fact_titles:
        parts.append("- 建议优先引用的全局事实：" + "、".join(plan.fact_titles))
    if plan.table_needed:
        why = f"（{plan.table_purpose}）" if plan.table_purpose else ""
        parts.append(f"- 建议本章使用表格{why}")
    else:
        parts.append("- 本章可不强制使用表格")
    return "\n".join(parts) + "\n"


def resolve_selected_fact_rows(
    titles: list[str] | None,
    facts_rows: list,
) -> list:
    """✅ R122-G1（引入易标「编排选事实 → 生成注事实」闭环）。

    按编排阶段**选中**的事实标题，从完整事实行集中取出对应整行 —— 对齐易标
    contentGenerationTask.cjs:134/171 的语义：AI 在编排时只看标题清单，
    生成时由**程序**按标题取回完整事实注入（`appendSelectedFactsMessage`）。

    与 :meth:`ContentPlanData.validate` 同为**白名单钳制**思想：只返回行集内
    真存在的行，编排若编造候选外标题一律落空（不会注入臆造事实）。

    行契约与 facts_builder 一致：6 元组 (gt, title, content, confidence,
    chapter, value_kind)，title 按下标 r[1] 取（历史短行按空串处理，与
    sse_handlers D1 候选池构建同口径）；命中的**整行原样返回**（不裁列）。

    Args:
        titles: 编排选中的事实标题（可含重复/空白/非字符串项，一律容错）。
        facts_rows: 完整事实行集（``_load_facts_rows`` 产物）。

    Returns:
        选中行列表，按**行集原始顺序**排列并去重（同一标题多行全部保留）。
    """
    wanted = {str(t or "").strip() for t in (titles or []) if str(t or "").strip()}
    if not wanted:
        return []
    out: list = []
    seen_ids: set[int] = set()
    for row in facts_rows or []:
        try:
            title = str(row[1] or "") if len(row) > 1 else ""
        except (TypeError, IndexError):
            continue
        if title.strip() in wanted and id(row) not in seen_ids:
            seen_ids.add(id(row))
            out.append(row)
    return out


async def plan_section_content(
    chat_fn,
    *,
    leaf: dict,
    word_budget: int,
    parent_chain: str,
    sibling_lines: str,
    project_brief: str,
    knowledge_names: list[str],
    fact_titles: list[str],
    scheme_name: str = "",
    scheme_type: str = "",
) -> ContentPlanData:
    """独立 LLM 步：为单章产出结构化编排决策。

    任何异常（网络/JSON 解析/校验失败）都回退为「无编排」，绝不抛出 —— 保证
    编排阶段失败不会阻断正文生成（对齐本软件「编排是加法式建议」的定位）。

    Args:
        chat_fn: 与 chat_with_fallback 同签名的异步函数，用于发起 LLM 请求。
        leaf: 叶子章节 dict（需含 title/description 等）。
        knowledge_names / fact_titles: 候选池（决定模型可挑选的范围）。
    """
    from app.services.ai.prompts._registry import render

    sys_prompt = render("content_plan_system")
    knowledge_names_text = "\n".join(f"- {n}" for n in knowledge_names) or "（无）"
    fact_titles_text = "\n".join(f"- {t}" for t in fact_titles) or "（无）"

    try:
        user_prompt = render(
            "content_plan_user",
            scheme_name=scheme_name,
            scheme_type=scheme_type,
            section_number=str(leaf.get("outline_number") or leaf.get("number") or ""),
            section_title=str(leaf.get("title") or ""),
            section_description=str(leaf.get("description") or ""),
            word_budget=int(word_budget or 0),
            parent_chain=parent_chain or "（无）",
            sibling_lines=sibling_lines or "（无）",
            project_brief=project_brief or "（无）",
            knowledge_names_text=knowledge_names_text,
            fact_titles_text=fact_titles_text,
        )
    except Exception:  # noqa: BLE001 - 提示词渲染失败直接回退
        logger.warning("离散编排阶段提示词渲染失败，回退为无编排", exc_info=True)
        return ContentPlanData.empty()

    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user_prompt},
    ]

    try:
        from app.services.ai.json_response import collect_json_response_with_chat

        obj, _raw = await collect_json_response_with_chat(
            chat_fn,
            messages,
            validate_fn=None,
            scene="content_plan",
            max_tokens=CONTENT_PLAN_MAX_TOKENS,
            json_mode=False,
        )
        return ContentPlanData.validate(obj, knowledge_pool=knowledge_names, fact_pool=fact_titles)
    except Exception:  # noqa: BLE001 - 编排失败绝不阻断正文生成
        logger.warning("离散编排阶段 LLM 调用失败，回退为无编排", exc_info=True)
        return ContentPlanData.empty()
