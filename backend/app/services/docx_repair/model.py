# -*- coding: utf-8 -*-
"""两轮次导出 · 第二轮检查/修复的数据模型（R147）。

纯数据结构，零 IO。三个对象贯穿检查（inspector）→ 修复（fixes）→
编排（pipeline）→ 报告（持久化 task_registry.checkpoint_json + SSE 下发）：

- :class:`RepairFinding`  检查器产出的一条缺陷（只读检查的结果）；
- :class:`RepairAction`   修复器产出的一条动作（改了什么 / 结果如何）；
- :class:`RepairReport`   一轮修复的完整报告（含未解决项与回滚标记）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

#: 检查类别（C1~C5，与前端徽标 / 报告分组一一对应）
CATEGORIES: tuple[str, ...] = (
    "numbering",     # C1 目录编号与层级
    "paragraph",     # C2 正文排版
    "fonts",         # C3 字号字体
    "figures",       # C4 图表渲染
    "compatibility",  # C5 兼容性安全
)

CATEGORY_LABELS: dict[str, str] = {
    "numbering": "目录编号与层级",
    "paragraph": "正文排版",
    "fonts": "字号字体",
    "figures": "图表渲染",
    "compatibility": "兼容性安全",
}

#: finding 严重级
SEV_ERROR = "error"      # 必须修复（修复失败 → unresolved / 回滚）
SEV_WARNING = "warning"  # 仅记录，不阻断交付

#: 未解决项的处置建议（按 code 聚合展示，前端「需人工」卡片用）
_CODE_SUGGESTIONS: dict[str, str] = {
    "num_level_jump": "人工确认标题层级（是否存在误设为标题的列表项）后调整大纲级别",
    "num_outline_mismatch": "对照目录树核对标题层级，必要时重新生成目录",
    "num_broken_binding": "人工确认该段是否应使用自动编号，或改用字面编号",
    "num_sequence": "人工核对标题编号方案（L1 章号/序号规则）后重排",
    "num_scheme_unknown": "人工确认该标题的编号方案（第X章 / 一、 / 1.）",
    "para_no_indent": "人工设置正文首行缩进 2 字符",
    "para_no_linespacing": "人工设置段前段后间距与 1.5 倍行距",
    "para_line_too_tight": "人工调整行距至 1.5 倍",
    "para_bad_align": "人工设置正文两端对齐",
    "para_num_conflict": "人工删除段首硬编号，改用自动编号",
    "para_seq_gap": "人工核对中文列表序号连续性",
    "font_no_eastasia": "人工补设 rFonts 的 eastAsia 字体",
    "font_ascii_cjk": "人工将西文字体槽改为 Times New Roman / 等线",
    "font_sz_odd": "人工统一字号（正文小四、标题按级别）",
    "heading_italic": "人工取消标题倾斜",
    "fig_no_caption": "人工为图片补加图题「图 X-Y 图名」",
    "fig_caption_no_title": "人工补齐图名",
    "fig_caption_format": "人工统一图题格式「图 X-Y 图名」",
    "fig_seq_gap": "人工重排图号并同步正文引用",
    "fig_broken_media": "人工用源文件替换损坏图片，或删除该图",
    "fig_not_centered": "人工将图片段落设为居中",
    "fig_oversize": "人工缩小图片至版心尺寸",
    "compat_missing_rel": "人工补齐关系项或引用的部件",
    "compat_broken_rel_target": "人工补齐缺失部件或删除悬空引用",
    "compat_missing_content_type": "人工在 [Content_Types].xml 补 Default 声明",
    "compat_w14_attr": "人工剔除 w14/w15 注释性属性",
    "compat_ns_decl": "人工剔除 w14/w15 专有命名空间声明",
}
_DEFAULT_SUGGESTION = "人工复核该项后手工调整"


def _suggestion(code: str) -> str:
    return _CODE_SUGGESTIONS.get(code, _DEFAULT_SUGGESTION)


@dataclass
class RepairFinding:
    """一条缺陷：检查器只读发现，修复器按 code 定向处理。"""

    category: str            # CATEGORIES 之一
    code: str                # 机器可读码（如 "num_level_jump"），修复器据此分发
    severity: str            # SEV_ERROR / SEV_WARNING
    location: str            # 人类可读定位（段落序号 / 部件名 / 图号…）
    message: str             # 人类可读描述（进 SSE 与报告）
    meta: dict = field(default_factory=dict)  # 修复器所需的结构化上下文

    def to_dict(self) -> dict:
        return {
            "category": self.category,
            "code": self.code,
            "severity": self.severity,
            "location": self.location,
            "message": self.message,
            "meta": dict(self.meta),
        }


@dataclass
class RepairAction:
    """一条修复动作：所有检查与修复动作都必须留痕（用户指令 §三）。"""

    category: str
    code: str
    location: str
    message: str             # 做了什么（如「移除悬挂 numPr 引用 numId=9」）
    result: str = "fixed"    # fixed / skipped / failed / unresolved
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "category": self.category,
            "code": self.code,
            "location": self.location,
            "message": self.message,
            "result": self.result,
            "detail": dict(self.detail),
        }


@dataclass
class RepairReport:
    """第二轮修复报告（终稿交付时持久化 + 下发前端展示）。"""

    findings: list[RepairFinding] = field(default_factory=list)
    actions: list[RepairAction] = field(default_factory=list)
    #: 程序修复两轮仍未收敛的 error 级 finding（Agent 辅助后的遗留）
    unresolved: list[RepairFinding] = field(default_factory=list)
    #: 初检存在、终检已消失的 finding（R149：报告「已修复」= 覆盖数）
    fixed_findings: list[RepairFinding] = field(default_factory=list)
    #: 初检存在、终检仍在但为 warning 级的 finding（「跳过」卡片）
    skipped: list[RepairFinding] = field(default_factory=list)
    #: fixed（正常）/ fallback_draft（修复链异常，回滚交付初稿）
    mode: str = "fixed"
    #: 各类检查的收敛结果 {category: {"found": n, "fixed": n, "recheck_clean": bool}}
    checks: dict = field(default_factory=dict)
    duration_ms: int = 0
    round1_stats: dict = field(default_factory=dict)  # 第一轮 _build_docx_sync 返回值

    def summary(self) -> dict:
        """报告汇总：detected/fixed/skipped/manual 均为 **finding 覆盖数**。

        - detected = 初检 finding 总数；
        - fixed    = 初检存在、终检消失的 finding 数（覆盖数，非动作条数）；
        - skipped  = 初检存在、终检仍在且 severity=warning 的 finding 数；
        - manual   = 终检仍在的 error 级 finding 数（需人工复核）；
        - unresolved_by_code = 需人工项按 code 聚合（count + 3 条样本 + 建议）。
        """
        by_cat: dict[str, dict] = {}
        for f in self.findings:
            c = by_cat.setdefault(f.category, {"found": 0, "error": 0,
                                              "warning": 0, "fixed": 0,
                                              "skipped": 0, "manual": 0})
            c["found"] += 1
            c["error" if f.severity == SEV_ERROR else "warning"] += 1
        for f in self.fixed_findings:
            c = by_cat.setdefault(f.category, {"found": 0, "error": 0,
                                              "warning": 0, "fixed": 0,
                                              "skipped": 0, "manual": 0})
            c["fixed"] += 1
        for f in self.skipped:
            c = by_cat.setdefault(f.category, {"found": 0, "error": 0,
                                              "warning": 0, "fixed": 0,
                                              "skipped": 0, "manual": 0})
            c["skipped"] += 1
        for f in self.unresolved:
            c = by_cat.setdefault(f.category, {"found": 0, "error": 0,
                                              "warning": 0, "fixed": 0,
                                              "skipped": 0, "manual": 0})
            c["manual"] += 1

        agg: dict[str, dict] = {}
        for f in self.unresolved:
            a = agg.setdefault(f.code, {
                "code": f.code,
                "category": f.category,
                "count": 0,
                "samples": [],
                "suggestion": _suggestion(f.code),
            })
            a["count"] += 1
            if len(a["samples"]) < 3:
                a["samples"].append({
                    "location": f.location,
                    "message": f.message,
                })

        return {
            "mode": self.mode,
            "detected": len(self.findings),
            "fixed": len(self.fixed_findings),
            "skipped": len(self.skipped),
            "manual": len(self.unresolved),
            # 兼容旧前端字段（语义：检出/动作/已修复覆盖数）
            "total_findings": len(self.findings),
            "total_actions": len(self.actions),
            "unresolved": len(self.unresolved),
            "unresolved_by_code": sorted(
                agg.values(), key=lambda x: (-x["count"], x["code"])),
            "by_category": {
                cat: by_cat.get(cat, {"found": 0, "error": 0, "warning": 0,
                                      "fixed": 0, "skipped": 0, "manual": 0})
                for cat in CATEGORIES
            },
            "duration_ms": self.duration_ms,
        }

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "summary": self.summary(),
            "findings": [f.to_dict() for f in self.findings],
            "actions": [a.to_dict() for a in self.actions],
            "unresolved": [f.to_dict() for f in self.unresolved],
            "checks": dict(self.checks),
            "duration_ms": self.duration_ms,
            "round1_stats": dict(self.round1_stats),
        }
