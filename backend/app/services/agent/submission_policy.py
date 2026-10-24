"""P2 — 提交校验与修复止损策略（移植自易标 ``piSubmissionPolicy.cjs``）。

来源：``C:\\...\\yibiao_outline\\electron_services_pi_piSubmissionPolicy.cjs``
的 ``createSubmissionPolicy`` 与 ``buildSubmissionRepairPrompt``，逐分支 1:1 移植。

``report`` 契约（阶段产物校验报告，由调用方按 validation 层产出组装）：

- ``issues``（必需）：``list[dict]``，每项至少含：
    - ``severity``：``'blocking'``（阻塞，必须修复）或 ``'quality'``（质量瑕疵）；
    - ``file``：出问题的产物文件名；
    - ``path``：JSON 内路径（可选，对应 Ajv ``instancePath``，缺省记为 ``'/'``）；
    - ``message``：中文问题描述。
- ``progress``（可选）：剩余量快照（数字）。给出时优先于 ``quality`` issue 计数
  作为 ``remaining``（对应易标 ``report.progress ?? quality 数``）；``None``
  视同未给出而回退到 quality 计数。
- ``minimumGoal``（可选）：本轮最低完成目标文案，仅 minimum 档修复提示使用。

红线（易标 2.30.20 一致）：提交修复**没有次数上限**，靠 ``stop`` 兜底止损 ——
``minimum`` 档一旦发起过（``minimumAttempted``），此后只看 blocking：有则
``stop``，清零即 ``accept``，不再进入修复分支。
"""
from __future__ import annotations


class _SubmissionPolicy:
    """阶段独立的提交修复状态（对应易标 ``createSubmissionPolicy`` 的闭包变量）。

    内部状态与易标逐字段对应：``previous``（上一轮快照）、``stalled``（连续
    无进展轮数）、``repairCount``（已发起修复次数）、``minimumAttempted``
    （是否已进入过最低目标档）。
    """

    def __init__(self) -> None:
        self._previous: dict | None = None
        self._stalled = 0
        self._repair_count = 0
        self._minimum_attempted = False

    @property
    def attempts(self) -> int:
        """已发起的提交修复次数（易标 ``get attempts()``，只读）。"""
        return self._repair_count

    def inspect(self, report: dict) -> dict:
        """按相邻两轮的阻塞问题及质量剩余量判断进展，返回下一步动作。

        返回结构（易标 1:1）：

        - ``{'action': 'repair', 'mode': ..., 'attempt': ...}`` —— 继续修复，
          ``mode`` 三档：``normal``（正常）/ ``change-strategy``（换方法）/
          ``minimum``（最低目标，仅一次）；``attempt`` 为本次序号；
        - ``{'action': 'accept'}`` —— 验收通过；
        - ``{'action': 'stop'}`` —— 停止任务（``minimumAttempted`` 后仍有
          blocking 的兜底止损）。

        判定顺序与易标一致：``minimumAttempted`` 后只看 blocking；无 issue
        直接 accept；``stalled>=2 且无 blocking`` → accept；否则按 ``stalled``
        定档并发起 ``repairCount += 1``。
        """
        blocking = [issue for issue in report["issues"]
                    if issue.get("severity") == "blocking"]
        if self._minimum_attempted:
            return {"action": "stop" if blocking else "accept"}
        if not report["issues"]:
            return {"action": "accept"}

        quality_remaining = sum(
            1 for issue in report["issues"]
            if issue.get("severity") == "quality"
        )
        progress = report.get("progress")
        current = {
            "blocking": len(blocking),
            "remaining": progress if progress is not None else quality_remaining,
        }
        if self._previous is not None:
            previous = self._previous
            improved = current["blocking"] < previous["blocking"] or (
                current["blocking"] == 0 and previous["blocking"] == 0
                and current["remaining"] < previous["remaining"]
            )
            self._stalled = 0 if improved else self._stalled + 1
        self._previous = current

        if self._stalled >= 2 and not blocking:
            return {"action": "accept"}
        mode = ("minimum" if self._stalled >= 2
                else "change-strategy" if self._stalled == 1
                else "normal")
        if mode == "minimum":
            self._minimum_attempted = True
        self._repair_count += 1
        return {"action": "repair", "mode": mode, "attempt": self._repair_count}


def create_submission_policy() -> _SubmissionPolicy:
    """创建一个业务阶段独立的提交策略实例（易标 ``createSubmissionPolicy``）。

    实例暴露只读属性 ``attempts`` 与方法 ``inspect(report)``。
    """
    return _SubmissionPolicy()


def build_submission_repair_prompt(
    report: dict,
    decision: dict,
    business_prompt: str = "",
) -> str:
    """拼装提交修复提示（易标 ``buildSubmissionRepairPrompt``，三档文案逐字移植）。

    - ``business_prompt``：业务提示（文件与工具细节），空串/None 会被
      ``filter(Boolean)`` 丢弃；
    - ``decision``：``inspect`` 返回的 repair 决策，至少含 ``mode`` 与
      ``attempt``；
    - ``report.minimumGoal``：仅 minimum 档使用，缺省或空值时回退到易标
      默认目标文案。

    返回四段以 ``\\n\\n`` 连接：业务提示、本轮待修复问题（minimum 档只列
    blocking）、第 N 次提交修复声明、档位指令。
    """
    issues = ([issue for issue in report["issues"]
               if issue.get("severity") == "blocking"]
              if decision["mode"] == "minimum" else report["issues"])
    messages = "\n".join(f"{index + 1}. {issue['message']}"
                         for index, issue in enumerate(issues))
    if decision["mode"] == "minimum":
        instruction = (
            "连续两次修复未取得进展。现在进行最后一轮最低目标修复。\n"
            f"最低完成目标：{report.get('minimumGoal') or '解决下列阻塞问题，使当前产物能被后续流程正常读取和使用。'}\n"
            "本轮只要求消除上述阻塞问题，暂不要求消除一致性、表格、字数等质量瑕疵；"
            "此前业务提示中的质量完成条件本轮不再作为验收条件。"
            "完成最低目标后重新提交；如果仍有阻塞问题，任务将停止。"
        )
    elif decision["mode"] == "change-strategy":
        instruction = (
            "上一轮修复未取得进展。先核对最新产物并定位原方法未解决问题的原因，"
            "再更换具体处理方法；不要重复相同操作。"
            "可使用当前可用工具和命令验证新的处理方法，修复后重新提交。"
        )
    else:
        instruction = "保留已完成成果，根据当前问题修复后重新提交。"
    parts = [
        business_prompt,
        f"本轮待修复问题：\n{messages}",
        f"这是本阶段第 {decision['attempt']} 次提交修复。",
        instruction,
    ]
    return "\n\n".join(part for part in parts if part)
