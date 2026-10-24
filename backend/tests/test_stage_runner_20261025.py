"""P3 阶段执行编排（continueTask 状态机）用例，可直接放入 backend/tests/ 运行。

覆盖可移植报告第五章 P3 的行为红线 a–g：

- a 执行异常消耗 executionRetries ≤ max_retries，超限上抛；
  build_retry_prompt 缺省/返回 None 时直接上抛；
- b stop 决策抛专用异常，消息逐字含阻塞问题清单；
- c accept 接受 value 与非阻塞 issues 并跳出内层循环；
- d 修复提示 = restored 还原说明 + businessPrompt → build_submission_repair_prompt；
- e continue_task 返回 None / complete / 无 prompt 结束，
  stage_index 按 Number.isFinite 语义推进，stage 变化时重置提交策略；
- f files / prepare_output_files 经注入钩子执行，无钩子跳过；
- g await_before_prompt / compact_before_prompt 钩子，异常上抛语义保留。
"""
from types import SimpleNamespace

import pytest

try:  # 镜像 _verify 运行（仓库树内形态）
    from app.services.agent.stage_runner import (
        RESTORED_NOTE_TEMPLATE,
        STOP_BLOCKING_PREFIX,
        StageExecutionResult,
        StageStoppedError,
        build_submission_repair_prompt,
        create_submission_policy,
        run_stage,
    )
except ImportError:  # 仓库外临时目录单文件直跑
    from stage_runner import (  # type: ignore[no-redef]
        RESTORED_NOTE_TEMPLATE,
        STOP_BLOCKING_PREFIX,
        StageExecutionResult,
        StageStoppedError,
        build_submission_repair_prompt,
        create_submission_policy,
        run_stage,
    )


# ── 通用夹具 ──────────────────────────────────────────────────────────────

def blocking(message: str, file: str = "outline.json") -> dict:
    return {"severity": "blocking", "file": file, "message": message}


def quality(message: str, file: str = "outline.json") -> dict:
    return {"severity": "quality", "file": file, "message": message}


def make_report(issues: list, value=None) -> dict:
    return {"value": {"ok": True} if value is None else value, "issues": issues}


def make_candidate(content: str = '{"outline": []}') -> dict:
    return {"output_file": "outline.json", "output_content": content}


class FakeStage:
    """预置 ``check_submission`` 步骤并记录提示词与 attempt 序号。

    ``steps`` 为 ``[(candidate, report, restored), ...]``；步骤用尽后重复
    最后一步，避免夹具与调用次数意外耦合。
    """

    def __init__(self, steps):
        self.steps = list(steps)
        self.prompts: list = []
        self.attempts: list = []

    async def prompt_once(self, prompt):
        self.prompts.append(prompt)
        return SimpleNamespace(turns=2)

    async def check(self, attempt):
        self.attempts.append(attempt)
        step = self.steps[min(len(self.attempts) - 1, len(self.steps) - 1)]
        return step


# ── 红线 a：执行重试预算 ──────────────────────────────────────────────────

async def test_执行异常_消耗执行重试预算_超限后上抛原始错误():
    calls = {"n": 0}
    requests = []

    async def prompt_once(prompt):
        calls["n"] += 1
        raise ValueError("模型调用失败")

    async def check(attempt):
        raise AssertionError("prompt 异常时不应进入提交校验")

    def build_retry(request, meta):
        requests.append((request, meta))
        return "上一轮失败，请修复后重试"

    with pytest.raises(ValueError, match="模型调用失败"):
        await run_stage("阶段提示词", prompt_once=prompt_once,
                        check_submission=check, build_retry_prompt=build_retry,
                        max_retries=2)
    # 预算语义：executionRetries 消耗 ≤ max_retries，共尝试 max_retries+1 次
    assert calls["n"] == 3
    assert [request["kind"] for request, _ in requests] == ["execution"] * 2
    assert [meta["attempt"] for _, meta in requests] == [1, 2]
    assert all(meta["max_retries"] == 2 for _, meta in requests)


async def test_执行重试预算为零_首次异常即上抛():
    calls = {"n": 0}

    async def prompt_once(prompt):
        calls["n"] += 1
        raise RuntimeError("第一次就失败")

    async def check(attempt):
        raise AssertionError("不应到达提交校验")

    with pytest.raises(RuntimeError, match="第一次就失败"):
        await run_stage("阶段提示词", prompt_once=prompt_once,
                        check_submission=check, max_retries=0,
                        build_retry_prompt=lambda request, meta: "重试")
    assert calls["n"] == 1


async def test_执行异常_build_retry_prompt_返回_None_直接上抛原始错误():
    calls = {"n": 0}

    async def prompt_once(prompt):
        calls["n"] += 1
        raise ValueError("不可修复的执行错误")

    async def check(attempt):
        raise AssertionError("不应到达提交校验")

    def build_retry(request, meta):
        return None  # 不可修复

    with pytest.raises(ValueError, match="不可修复的执行错误"):
        await run_stage("阶段提示词", prompt_once=prompt_once,
                        check_submission=check, build_retry_prompt=build_retry,
                        max_retries=5)
    assert calls["n"] == 1  # 未消耗后续预算，直接上抛


async def test_未注入_build_retry_prompt_执行异常直接上抛():
    calls = {"n": 0}

    async def prompt_once(prompt):
        calls["n"] += 1
        raise ValueError("没有修复提示可用")

    async def check(attempt):
        raise AssertionError("不应到达提交校验")

    with pytest.raises(ValueError, match="没有修复提示可用"):
        await run_stage("阶段提示词", prompt_once=prompt_once,
                        check_submission=check, max_retries=3)  # 未注入
    assert calls["n"] == 1


# ── 红线 b：stop 兜底止损 ────────────────────────────────────────────────

async def test_stop_决策_抛出阻塞问题汇总异常_消息逐字():
    cand = make_candidate()
    rep = make_report([blocking("outline.json 缺少必填字段"),
                       quality("标题过短")])
    stage = FakeStage([(cand, rep, [])] * 4)
    calls = []

    def build_retry(request, meta):
        calls.append((request, meta))
        return "业务修复提示"

    with pytest.raises(StageStoppedError) as excinfo:
        await run_stage("阶段提示词", prompt_once=stage.prompt_once,
                        check_submission=stage.check,
                        build_retry_prompt=build_retry,
                        initial_stage="s0", output_file="outline.json")

    expected = ("已按最低完成目标再修复一轮，仍存在阻塞流程的问题，任务已停止：\n"
                "outline.json 缺少必填字段")
    assert str(excinfo.value) == expected  # 消息逐字（前缀 + 逐条 message）
    assert str(excinfo.value).startswith(STOP_BLOCKING_PREFIX)
    assert excinfo.value.issues == rep["issues"]  # 携带完整 issues（含非阻塞）
    assert excinfo.value.agent_validation_failed is True
    # 三档修复（normal → change-strategy → minimum）后第 4 次提交才 stop
    assert stage.attempts == [1, 2, 3, 4]
    assert [request["kind"] for request, _ in calls] == ["submission"] * 3
    assert [meta["attempt"] for _, meta in calls] == [1, 2, 3]  # attempt 在 meta
    assert [request["mode"] for request, _ in calls] == [
        "normal", "change-strategy", "minimum"]


# ── 红线 c：accept ────────────────────────────────────────────────────────

async def test_accept_接受非阻塞质量问题_携带_issues_并跳出内层循环():
    cand = make_candidate()
    issues = [quality("表格列数不一致"), quality("章节字数偏少")]
    rep = make_report(issues)
    stage = FakeStage([(cand, rep, [])] * 3)

    result = await run_stage("阶段提示词", prompt_once=stage.prompt_once,
                             check_submission=stage.check,
                             initial_stage="s0", initial_stage_index=3)
    assert isinstance(result, StageExecutionResult)
    assert result.validation_result == rep["value"]
    assert result.accepted_issues == issues  # 非阻塞 issues 随验收保留
    assert result.accepted_submission_issues == [{"stage": "s0",
                                                  "issues": issues}]
    assert result.prompt_calls == 3
    assert result.turns == 6
    assert result.stop_reason == "completed"
    assert result.execution_retries == 0
    assert result.submission_retries == 2
    assert [item["attempt"] for item in result.retry_attempts] == [1, 2]
    assert all(item["kind"] == "submission" for item in result.retry_attempts)
    assert stage.attempts == [1, 2, 3]
    assert result.stage_index == 3 and result.stage == "s0"


# ── 红线 d：修复提示拼装（restored 还原说明 + businessPrompt）────────────

async def test_修复提示_还原文件说明在前_业务提示在后_逐字拼装():
    cand = make_candidate()
    rep = make_report([blocking("模板.json 结构错误")])
    clean = make_report([])
    stage = FakeStage([(cand, rep, ["程序规则.md", "模板.json"]),
                       (cand, clean, [])])

    def build_retry(request, meta):
        return "业务修复提示"

    result = await run_stage("阶段提示词", prompt_once=stage.prompt_once,
                             check_submission=stage.check,
                             build_retry_prompt=build_retry,
                             output_file="outline.json")
    decision = {"action": "repair", "mode": "normal", "attempt": 1}
    expected_details = ("程序已还原被改动的文件：程序规则.md、模板.json。"
                        "这些文件由程序维护，不要修改。\n业务修复提示")
    expected = build_submission_repair_prompt(rep, decision, expected_details)
    assert stage.prompts[1] == expected  # 整段提示逐字等于三段拼装结果
    assert stage.prompts[1].startswith(
        "程序已还原被改动的文件：程序规则.md、模板.json。"
        "这些文件由程序维护，不要修改。")  # 还原说明在最前
    assert result.prompt_calls == 2
    assert result.execution_retries == 0


async def test_修复提示_无还原文件_不带还原说明仅业务提示():
    assert (RESTORED_NOTE_TEMPLATE.format(files="a、b")
            == "程序已还原被改动的文件：a、b。这些文件由程序维护，不要修改。")
    cand = make_candidate()
    rep = make_report([blocking("缺少叶子节点")])
    clean = make_report([])
    stage = FakeStage([(cand, rep, []), (cand, clean, [])])

    def build_retry(request, meta):
        return "业务修复提示"

    await run_stage("阶段提示词", prompt_once=stage.prompt_once,
                    check_submission=stage.check,
                    build_retry_prompt=build_retry)
    decision = {"action": "repair", "mode": "normal", "attempt": 1}
    assert stage.prompts[1] == build_submission_repair_prompt(
        rep, decision, "业务修复提示")  # restored 为空 → 仅业务提示
    assert "程序已还原" not in stage.prompts[1]


# ── 红线 e：continue_task 流转与策略重置 ─────────────────────────────────

def make_factory(created: list):
    def factory():
        policy = create_submission_policy()
        created.append(policy)
        return policy

    return factory


async def test_stage_不变_提交策略不重置():
    created = []
    stage = FakeStage([(make_candidate(), make_report([]), [])])
    calls = {"n": 0}

    async def continue_task(candidate, meta):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"stage": "s0", "stage_index": 3,
                    "prompt": "继续执行当前阶段"}
        return None

    result = await run_stage("阶段提示词", prompt_once=stage.prompt_once,
                             check_submission=stage.check,
                             continue_task=continue_task,
                             submission_policy_factory=make_factory(created),
                             initial_stage="s0", initial_stage_index=3)
    assert len(created) == 1  # stage 未变 → 不调用工厂，策略保持
    assert result.stage == "s0" and result.stage_index == 3
    assert result.prompt_calls == 2


async def test_stage_变化_提交策略重置():
    created = []
    stage = FakeStage([(make_candidate(), make_report([]), [])])
    calls = {"n": 0}

    async def continue_task(candidate, meta):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"stage": "s1", "prompt": "下一阶段提示"}  # 无 stage_index
        return None

    result = await run_stage("阶段提示词", prompt_once=stage.prompt_once,
                             check_submission=stage.check,
                             continue_task=continue_task,
                             submission_policy_factory=make_factory(created),
                             initial_stage="s0", initial_stage_index=3)
    assert len(created) == 2  # stage 变化 → 重置提交策略（易标 L1193）
    assert result.stage == "s1" and result.stage_index == 4  # 缺键 → +1
    assert result.prompt_calls == 2


async def test_continue_task_返回_None_直接结束():
    created = []
    events = []
    activities = []
    stage = FakeStage([(make_candidate(), make_report([]), [])])

    async def continue_task(candidate, meta):
        return None

    result = await run_stage("阶段提示词", prompt_once=stage.prompt_once,
                             check_submission=stage.check,
                             continue_task=continue_task,
                             submission_policy_factory=make_factory(created),
                             on_event=events.append, on_activity=activities.append)
    assert isinstance(result, StageExecutionResult)
    assert result.prompt_calls == 1
    assert events == []  # 无流转 → 不发 task_output / task_input
    assert activities == []  # 无质量问题 → 不发 accept 活动
    assert result.stage == "" and result.stage_index == 0
    assert len(created) == 1


async def test_continue_task_complete_为真或缺_prompt_直接结束():
    candidates = (
        {"complete": True, "prompt": "下一阶段提示"},
        {"complete": False, "prompt": ""},  # prompt 为空视为结束
        {"stage": "s1", "stage_index": 5},  # 缺 prompt 视为结束
    )
    for continuation in candidates:
        stage = FakeStage([(make_candidate(), make_report([]), [])])

        async def continue_task(candidate, meta, _c=continuation):
            return _c

        result = await run_stage("阶段提示词", prompt_once=stage.prompt_once,
                                 check_submission=stage.check,
                                 continue_task=continue_task,
                                 initial_stage="s0", initial_stage_index=7)
        assert result.prompt_calls == 1
        assert result.stage == "s0" and result.stage_index == 7  # 未推进


async def test_stage_index_推进_Number_isFinite_语义():
    cases = [("3", 3), ("abc", 8), (None, 0)]  # (注入值, 期望)；Number(null) = 0
    for sent, expected in cases:
        stage = FakeStage([(make_candidate(), make_report([]), [])])
        continuation = {"stage": "s1", "prompt": "下一阶段提示"}
        continuation["stage_index"] = sent  # 键存在
        calls = {"n": 0}

        async def continue_task(candidate, meta, _c=continuation, _n=calls):
            _n["n"] += 1
            return _c if _n["n"] == 1 else None

        result = await run_stage("阶段提示词", prompt_once=stage.prompt_once,
                                 check_submission=stage.check,
                                 continue_task=continue_task,
                                 initial_stage="s0", initial_stage_index=7)
        assert result.stage_index == expected, sent
    # 键缺省 → stage_index + 1
    stage = FakeStage([(make_candidate(), make_report([]), [])])
    calls = {"n": 0}

    async def continue_task(candidate, meta):
        calls["n"] += 1
        return {"stage": "s1", "prompt": "下一阶段提示"} if calls["n"] == 1 else None

    result = await run_stage("阶段提示词", prompt_once=stage.prompt_once,
                             check_submission=stage.check,
                             continue_task=continue_task,
                             initial_stage="s0", initial_stage_index=7)
    assert result.stage_index == 8


# ── 红线 f：files / prepare_output_files 钩子 ────────────────────────────

async def test_阶段流转_files_与_prepare_output_files_经钩子执行():
    written = []
    prepared = []
    stage = FakeStage([(make_candidate(), make_report([]), [])])
    continuation = {"stage": "s1", "prompt": "下一阶段提示",
                    "files": [{"path": "leaf-context.json", "content": "{}"}],
                    "prepare_output_files": ["outline.json"]}
    calls = {"n": 0}

    async def continue_task(candidate, meta):
        calls["n"] += 1
        return continuation if calls["n"] == 1 else None

    async def write_files(files, prepare):
        written.append((files, prepare))

    async def prepare_files(files):
        prepared.append(files)

    result = await run_stage("阶段提示词", prompt_once=stage.prompt_once,
                             check_submission=stage.check,
                             continue_task=continue_task,
                             write_files=write_files,
                             prepare_output_files=prepare_files)
    assert written == [([{"path": "leaf-context.json", "content": "{}"}],
                        ["outline.json"])]
    assert prepared == [["outline.json"]]
    assert result.prompt_calls == 2 and result.stage == "s1"


async def test_阶段流转_无写入钩子_跳过文件写入继续流转():
    stage = FakeStage([(make_candidate(), make_report([]), [])])
    continuation = {"stage": "s1", "prompt": "下一阶段提示",
                    "files": [{"path": "a.json", "content": "{}"}],
                    "prepare_output_files": ["outline.json"]}
    calls = {"n": 0}

    async def continue_task(candidate, meta):
        calls["n"] += 1
        return continuation if calls["n"] == 1 else None

    result = await run_stage("阶段提示词", prompt_once=stage.prompt_once,
                             check_submission=stage.check,
                             continue_task=continue_task)  # 未注入任何钩子
    assert result.prompt_calls == 2 and result.stage == "s1"  # 正常流转


# ── 红线 g：await_before_prompt / compact_before_prompt ──────────────────

async def test_await_before_prompt_异常上抛_不再发出下一阶段提示():
    stage = FakeStage([(make_candidate(), make_report([]), [])])

    async def boom():
        raise RuntimeError("续跑前置步骤失败")

    async def continue_task(candidate, meta):
        return {"stage": "s1", "prompt": "下一阶段提示",
                "await_before_prompt": boom}

    with pytest.raises(RuntimeError, match="续跑前置步骤失败"):
        await run_stage("阶段提示词", prompt_once=stage.prompt_once,
                        check_submission=stage.check,
                        continue_task=continue_task)
    assert stage.prompts == ["阶段提示词"]  # 异常在发提示前上抛


async def test_await_before_prompt_正常等待_随后进入下一阶段():
    stage = FakeStage([(make_candidate(), make_report([]), [])])
    ran = []
    calls = {"n": 0}

    async def before():
        ran.append("before")

    async def continue_task(candidate, meta):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"stage": "s1", "prompt": "下一阶段提示",
                    "await_before_prompt": before}
        return None

    result = await run_stage("阶段提示词", prompt_once=stage.prompt_once,
                             check_submission=stage.check,
                             continue_task=continue_task)
    assert ran == ["before"]
    assert result.prompt_calls == 2
    assert stage.prompts[1] == "下一阶段提示"


async def test_compact_before_prompt_经注入钩子执行_缺省跳过():
    compactions = []
    stage = FakeStage([(make_candidate(), make_report([]), [])])
    calls = {"n": 0}
    continuation = {"stage": "s1", "prompt": "下一阶段提示",
                    "compact_before_prompt": True}

    async def continue_task(candidate, meta):
        calls["n"] += 1
        return continuation if calls["n"] == 1 else None

    async def run_compaction(target_stage, cont):
        compactions.append((target_stage, cont["prompt"]))

    await run_stage("阶段提示词", prompt_once=stage.prompt_once,
                    check_submission=stage.check,
                    continue_task=continue_task, run_compaction=run_compaction)
    assert compactions == [("s1", "下一阶段提示")]

    # 本仓默认无压缩：未注入 run_compaction 时跳过，不报错
    stage2 = FakeStage([(make_candidate(), make_report([]), [])])
    calls2 = {"n": 0}

    async def continue_task2(candidate, meta):
        calls2["n"] += 1
        return continuation if calls2["n"] == 1 else None

    result = await run_stage("阶段提示词", prompt_once=stage2.prompt_once,
                             check_submission=stage2.check,
                             continue_task=continue_task2)
    assert result.prompt_calls == 2


# ── 观测回调与 meta 注入 ─────────────────────────────────────────────────

async def test_观测回调_收到_retry_与_阶段流转事件():
    events = []
    activities = []
    prompt_calls = {"n": 0}
    check_calls = {"n": 0}
    cont_calls = {"n": 0}
    quality_report = make_report([quality("列宽不一致")])
    clean_report = make_report([])
    continuation = [{"stage": "s1", "prompt": "下一阶段提示"}, None]

    async def prompt_once(prompt):
        prompt_calls["n"] += 1
        if prompt_calls["n"] == 1:
            raise RuntimeError("第一次执行失败")
        return {"turns": 1}

    async def check(attempt):
        check_calls["n"] += 1
        # 连续 3 轮同量质量问题 → stalled=2 时 accept（携带非阻塞 issues）
        report = (quality_report if check_calls["n"] <= 3 else clean_report)
        return make_candidate(), report, []

    async def read_output():
        return "旧产物内容"

    async def continue_task(candidate, meta):
        cont_calls["n"] += 1
        return continuation[cont_calls["n"] - 1]

    def build_retry(request, meta):
        return "修复提示"

    result = await run_stage("阶段提示词", prompt_once=prompt_once,
                             check_submission=check,
                             build_retry_prompt=build_retry,
                             read_output=read_output,
                             continue_task=continue_task,
                             on_event=events.append,
                             on_activity=activities.append,
                             initial_stage="s0", output_file="outline.json")
    assert [event["type"] for event in events] == [
        "retry", "retry", "retry", "task_output", "task_input"]
    assert events[0]["maximum"] == 1  # 执行档带 maximum（易标 L1066 条件展开）
    assert "maximum" not in events[1]  # 提交档不带 maximum
    assert "maximum" not in events[2]
    assert events[0]["prompt"] == "修复提示"
    assert events[3]["workflow_stage"] == "s0"  # 完成阶段的快照
    assert events[4]["workflow_stage"] == "s1"
    assert [item["kind"] for item in activities] == [
        "retry", "retry", "retry", "submission.accepted", "workflow.continue"]
    assert activities[3]["message"] == (
        "仍有 1 项质量问题，保留当前结果继续后续流程：列宽不一致")
    assert result.prompt_calls == 5
    assert result.execution_retries == 1
    assert result.submission_retries == 2
    assert result.retry_attempts[0]["output_chars"] == len("旧产物内容")
    assert result.retry_attempts[0]["kind"] == "execution"


async def test_注入回调_收到易标语义的_request_与_meta():
    seen = {"requests": [], "exec": [], "sub": [], "continue": []}
    stage = FakeStage([(make_candidate(), make_report([blocking("缺字段")]), []),
                       (make_candidate(), make_report([]), [])])
    clean_value = {"accepted": True}
    stage.steps[1] = (make_candidate(), make_report([], value=clean_value), [])

    def build_retry(request, meta):
        seen["requests"].append(request)
        bucket = "exec" if request["kind"] == "execution" else "sub"
        seen[bucket].append(meta)
        return "修复提示"

    async def continue_task(candidate, meta):
        seen["continue"].append(meta)
        return None

    async def failing_once(prompt):
        if not stage.prompts:
            stage.prompts.append(prompt)
            raise ValueError("首轮执行失败")
        return await stage.prompt_once(prompt)

    result = await run_stage("阶段提示词", prompt_once=failing_once,
                             check_submission=stage.check,
                             build_retry_prompt=build_retry,
                             continue_task=continue_task,
                             meta_extra={"task_id": "T-42"},
                             initial_stage="s0", initial_stage_index=1,
                             output_file="outline.json", max_retries=2)
    assert isinstance(seen["requests"][0]["error"], ValueError)
    assert seen["requests"][1]["kind"] == "submission"
    assert seen["requests"][1]["mode"] == "normal"
    exec_meta = seen["exec"][0]
    assert exec_meta["attempt"] == 1 and exec_meta["max_retries"] == 2
    assert exec_meta["retry_attempts"] == []
    assert exec_meta["stage"] == 1 and exec_meta["workflow_stage"] == "s0"
    assert exec_meta["output_file"] == "outline.json"
    assert exec_meta["task_id"] == "T-42"  # meta_extra 打底
    sub_meta = seen["sub"][0]
    assert sub_meta["attempt"] == 1
    assert "max_retries" not in sub_meta  # 提交档 meta 不带 max_retries
    continue_meta = seen["continue"][0]
    assert continue_meta["validation_result"] == clean_value
    assert continue_meta["accepted_issues"] == []
    assert stage.attempts == [1, 2]  # 执行失败不消耗提交 attempt
    assert result.execution_retries == 1
    assert result.submission_retries == 1
