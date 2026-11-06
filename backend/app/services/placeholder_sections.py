"""目录占位章节判定（唯一事实源）。

需求（2026-11-05）：凡目录标题命中「计算书 / 附图 / 附件 / 布置图 / 施工平面
布置图 / 计算书及相关图纸 / 节点详图 / 系统图」等交付物类表述的章节，**不再
生成正文**，只登记固定占位文本；下游字数校验、预检、导出与前端展示均据此
识别，避免误报「正文为空 / 字数缺失」。

设计约束（AGENTS 4.3「同一判据单一事实源」）：
  · 标题命中判定只在本模块实现，正文生成、预检、导出、一致性扫描与前端
    标记均调用本模块函数，不在各调用点复制关键词或归一化逻辑；
  · 归一化处理全角/半角、空格、括号、书名号等差异（NFKC + 去标点），
    因此「计算书及相关图纸（含附图）」「计算书 及 相关 图纸《附图》」都能命中；
  · 关键词清单与总开关来自 ``app.config.settings``，默认启用、默认清单即需求
    所列条目；配置为空时回落到内置默认清单（置开关 False 才是关闭能力）。
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime

from app.config import settings

#: 命中章节统一登记的占位正文（下游以 ``PLACEHOLDER_STATUS`` 识别，不靠文本比较）
PLACEHOLDER_CONTENT = "另行补充内容"

#: 占位章节落库状态（区别于 generated/empty/failed/pending）
PLACEHOLDER_STATUS = "placeholder"

#: 默认关键词清单（需求原文 + 常见命名变体）
DEFAULT_PLACEHOLDER_KEYWORDS: tuple[str, ...] = (
    "计算书",
    "计算书及相关图纸",
    "附图",
    "附件",
    "布置图",
    "施工平面布置图",
    "平面布置图",
    "节点详图",
    "详图",
    "系统图",
    "图纸",
)

#: 关键词分隔符：兼容中英文逗号、分号、顿号、竖线（环境变量用逗号串即可）
_KEYWORD_SPLIT_RE = re.compile(r"[,，;；、|]+")

#: 标题归一化时剔除的标点/空白（全角经由 NFKC 已转半角，仍同时覆盖）
_TITLE_NOISE_RE = re.compile(r"[\s\u3000（）()【】\[\]{}〈〉《》「」『』“”\"'‘’、，,。.；;：:·\-—–_/\\|]+")


def normalize_section_title(title: str | None) -> str:
    """标题归一化：NFKC（全角→半角）+ 小写 + 去掉空白与标点。

    只做「消除排版差异」，不改写汉字本身；空标题返回空串（不命中）。
    """
    text = unicodedata.normalize("NFKC", str(title or ""))
    text = text.strip().lower()
    return _TITLE_NOISE_RE.sub("", text)


def _configured_keywords() -> tuple[str, ...]:
    """读取配置关键词（逗号串），归一化后返回；空配置回落到默认清单。"""
    raw = str(getattr(settings, "placeholder_section_keywords", "") or "")
    parts = []
    for item in _KEYWORD_SPLIT_RE.split(raw):
        normalized = normalize_section_title(item)
        if normalized:
            parts.append(normalized)
    if not parts:
        return tuple(normalize_section_title(k) for k in DEFAULT_PLACEHOLDER_KEYWORDS)
    return tuple(parts)


def is_placeholder_title(
    title: str | None,
    *,
    enabled: bool | None = None,
    keywords: tuple[str, ...] | list[str] | None = None,
) -> bool:
    """章节标题是否命中占位规则（唯一判定函数）。

    Args:
        title: 章节标题（可含编号、括号、书名号等）。
        enabled: 显式开关；None 时读 ``settings.placeholder_sections_enabled``。
        keywords: 显式关键词（未归一化亦可）；None 时读配置。
    """
    if enabled is None:
        enabled = bool(getattr(settings, "placeholder_sections_enabled", True))
    if not enabled:
        return False
    normalized_title = normalize_section_title(title)
    if not normalized_title:
        return False
    if keywords is None:
        candidates = _configured_keywords()
    else:
        candidates = tuple(normalize_section_title(k) for k in keywords if normalize_section_title(k))
    return any(keyword in normalized_title for keyword in candidates)


def is_placeholder_section(section: dict | None) -> bool:
    """落库章节行是否已登记为占位章节（下游判定用，不看标题）。"""
    return str((section or {}).get("status") or "") == PLACEHOLDER_STATUS


def split_placeholder_leaves(leaves: list[dict]) -> tuple[list[dict], list[dict]]:
    """把目标叶子拆成 ``(需要生成, 占位跳过)`` 两组。

    占位组不进入生成任务、不计入进度分母，也不参与字数分配 AI 调用。
    """
    generating: list[dict] = []
    placeholders: list[dict] = []
    for leaf in leaves or []:
        if is_placeholder_title(leaf.get("title")):
            placeholders.append(leaf)
        else:
            generating.append(leaf)
    return generating, placeholders


async def persist_placeholder_sections(db, leaves: list[dict], *, updated_at: str | None = None) -> int:
    """把占位章节登记为固定文本，返回写入条数。

    行为（覆盖人工编辑的既定策略）：
      · 覆盖该章节既有正文（含人工编辑内容）为 ``另行补充内容``；
      · ``word_count=0`` / ``word_status=normal`` —— 不触发字数缺失或低字数告警；
      · ``status=placeholder`` —— 前端据此显示「另行补充」标签，与失败/为空区分；
      · 清空该章旧图表登记，避免导出仍引用已不再生成的图表。
    章节标题、层级、sort_order 与 outline_json **一律不动**，因此目录编号不跳号。
    """
    section_ids = [str(leaf.get("id")) for leaf in leaves or [] if leaf.get("id")]
    if not section_ids:
        return 0
    timestamp = updated_at or datetime.now().isoformat()
    await db.executemany(
        "UPDATE sections SET content=?, word_count=0, word_status='normal', status=?, updated_at=? WHERE id=?",
        [(PLACEHOLDER_CONTENT, PLACEHOLDER_STATUS, timestamp, section_id) for section_id in section_ids],
    )
    await db.executemany(
        "DELETE FROM chart_predictions WHERE section_id=?",
        [(section_id,) for section_id in section_ids],
    )
    # 方案总字数随章节字数变化重算（占位章节 word_count=0，不再贡献字数）；
    # 仅在调用方传入的行带有 scheme_id 时执行，桩数据/单元测试不强制。
    for scheme_id in sorted({str(leaf.get("scheme_id")) for leaf in leaves or [] if leaf.get("scheme_id")}):
        await db.execute(
            "UPDATE schemes SET word_count=(SELECT COALESCE(SUM(word_count),0) FROM sections "
            "WHERE scheme_id=?) WHERE id=?",
            (scheme_id, scheme_id),
        )
    await db.commit()
    # 同步内存快照：all_sections 与 leaves 持有同一批 dict 引用，更新后
    # 同级上下文/前序摘要不会读到旧的「计算书正文」，避免提示词自相矛盾。
    for leaf in leaves or []:
        if not leaf.get("id"):
            continue
        leaf["content"] = PLACEHOLDER_CONTENT
        leaf["word_count"] = 0
        leaf["word_status"] = "normal"
        leaf["status"] = PLACEHOLDER_STATUS
    return len(section_ids)
