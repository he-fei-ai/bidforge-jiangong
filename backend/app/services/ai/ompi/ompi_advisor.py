"""OMP advisor 旁观模型适配层：第二模型监督主 Agent 的每一轮。

对应 omp 上游能力（README「A second model, watching every turn」）：
  - 把「reviewer / advisor 模型」指派到 ``advisor`` 角色；
  - 它读主 Agent 每轮的动作，注入 note（quiet aside / concern / hard blocker）；
  - 独立上下文、独立模型，抓 doer 冲过的错；主 Agent 看到 note 后 course-correct，
    或说明为何不改。

本层提供**确定性判据版**（``AdvisorGuard``）：不依赖第二个 LLM，而是用
「结构化契约 + 可注入规则」对主 Agent 的产出做监督，产出与 omp advisor
同形态的 ``AdvisorNote``（level ∈ info/concern/blocker）。当真实 omp 启用时，
本层可被替换为 omp ``--model advisor`` 的旁观会话，接口不变。

与 omp 的区别：omp 的 advisor 是独立 LLM 会话；本层是**确定性规则监督**，
成本为零、可回归、无幻觉。语义等价物：两者都在主 Agent 之外产生
「独立视角的 note」，供主 Agent 纠偏或说明不改。

⚠️ 分层约束：services 层禁止 import routers；本层零网络、零数据库访问。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

#: note 级别：info（旁注）/ concern（关切）/ blocker（硬阻断，需主 Agent 答复）。
_LEVELS = ("info", "concern", "blocker")


@dataclass(frozen=True)
class AdvisorNote:
    """一条 advisor 监督意见。"""

    level: str = "info"
    text: str = ""

    def __post_init__(self) -> None:
        if self.level not in _LEVELS:
            object.__setattr__(self, "level", "info")

    def to_dict(self) -> dict[str, str]:
        return {"level": self.level, "text": self.text}


#: 常见「主 Agent 漏掉/越界」的确定性监督规则（可注入扩展）。
#: 每条规则是一个 (name, predicate) 对；predicate 接收上下文 dict，返回问题文本或 None。
_DEFAULT_RULES: list[tuple[str, Callable[[dict], str | None]]] = []


def _rule_placeholder(ctx: dict) -> str | None:
    """占位符残留检测：正文不应再带 TODO/待补充/{{X}}/__X__。"""
    text = str(ctx.get("produced_text") or "")
    ph = re.findall(r"__[A-Z_]{2,}__|\{\{[^}]{1,40}\}\}|【待填[^】]*】|TODO|TBD|XXX", text)
    if ph:
        return f"产出仍含占位符：{'、'.join(sorted(set(ph))[:5])}"
    return None


def _rule_claim(ctx: dict) -> str | None:
    """「完成宣言」检测：模型声称完成但产物文件缺失/为空。"""
    payload = str(ctx.get("payload") or "")
    claims_done = any(k in payload for k in ("已全部完成", "全部章节已生成", "验收通过"))
    if claims_done and not str(ctx.get("produced_text") or "").strip():
        return "Agent 宣称完成但产物为空——疑似幻觉式完成宣言，请核对工作区产物文件"
    return None


_DEFAULT_RULES = [
    ("占位符残留", _rule_placeholder),
    ("空产物完成宣言", _rule_claim),
]


class AdvisorGuard:
    """确定性 advisor 监督器（等价 omp ``advisor`` 角色的平台侧实现）。

    用法（挂在 OmpBatchOrchestrator 的 executor 之后 / verify 之前）：
        guard = AdvisorGuard()
        note = guard.review({"produced_text": body, "payload": ...})
        if note.level == "blocker":
            ...  # 进入 fix 轮或暂停

    规则形态（加法式，既有 2 元组完全兼容）：
        - ``(name, rule)``            —— rule 返回问题文本或 None；level 由
          已知规则名映射（占位符/空产物完成宣言→concern，其余→info）；
        - ``(name, rule, level)``     —— 显式声明 level（info/concern/blocker）；
        - rule 返回 ``(level, msg)``  —— 规则内动态声明级别（优先级最高）。
    """

    def __init__(
        self,
        rules: list[tuple[str, Callable[[dict], str | None], str | None]] | None = None,
    ):
        # 归一为 3 元组 (name, rule, declared_level|None)，容忍 2 元组形态。
        self._rules: list[tuple[str, Callable[[dict], str | None], str | None]] = []
        for item in (rules if rules is not None else _DEFAULT_RULES):
            name = str(item[0])
            rule = item[1]
            declared = str(item[2]) if len(item) >= 3 and item[2] else None
            self._rules.append((name, rule, declared))

    def review(self, ctx: dict) -> AdvisorNote | None:
        """对一次产出做监督，返回最高严重级的 note（无问题返回 None）。"""
        worst: AdvisorNote | None = None
        rank = {"info": 0, "concern": 1, "blocker": 2}
        for name, rule, declared in self._rules:
            try:
                out = rule(ctx or {})
            except Exception:  # noqa: BLE001 - 规则自身异常不得中断监督
                continue
            if not out:
                continue
            # 规则可返回 (level, msg) 动态声明级别（优先级最高）。
            if isinstance(out, tuple) and len(out) == 2:
                dyn_level, msg = str(out[0]), str(out[1])
            else:
                dyn_level, msg = None, str(out)
            level = declared or dyn_level or (
                "concern" if name in ("占位符残留", "空产物完成宣言") else "info"
            )
            if level not in rank:
                level = "info"
            note = AdvisorNote(level=level, text=f"[{name}] {msg}")
            if worst is None or rank[note.level] > rank[worst.level]:
                worst = note
        return worst

    def format_notes(self, notes: list[AdvisorNote]) -> str:
        """把 note 列表格式化为注入下一轮 Prompt 的旁注块（同 omp 注入形态）。"""
        if not notes:
            return ""
        lines = ["<!-- Advisor 监督注记 -->"]
        for n in notes:
            lines.append(f"- **{n.level}**：{n.text}")
        return "\n".join(lines)
