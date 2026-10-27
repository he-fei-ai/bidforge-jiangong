import pathlib, py_compile

p = pathlib.Path(r"j:\编程\专项方案工具箱\backend\app\routers\sse_handlers.py")
raw = p.read_bytes()
lone = raw.count(b"\n") - raw.count(b"\r\n")
print("before loneLF =", lone)
text = raw.decode("utf-8")
nl = "\r\n" if "\r\n" in text else "\n"

anchor = (
    '            _prog["stepwise"] = use_stepwise' + nl + nl +
    "            # ✅ R58（2026-10-10）：一次性直出的**结果标志**。"
)
assert text.count(anchor) == 1, f"anchor count={text.count(anchor)}"

block = nl.join([
    '            _prog["stepwise"] = use_stepwise',
    "",
    "            # ✅ 接线阶段（遗留① · 2026-10-25 · 《目录生成工具修正移植_实施报告》"
    "第五章)：",
    "            #    以 stage_runner 为骨架的 Agent 编排分支。"
    " outline_agent_enabled 默认 False，",
    "            #    本分支为死代码，legacy 链路逐字不动（保基线）；置 True 后转",
    "            #    outline_agent_event_stream（initial-outline → children_generation",
    "            #    单链），产出与 legacy 同形状的 progress/completed/stopped 事件，",
    "            #    落库仍由前端 save-outline 完成；checkpoint → finish_task → yield",
    "            #    终态顺序在流内保持（R89 S2 降级口径同款）。异常/取消原样上抛，",
    "            #    由外层既有 error / CancelledError 分支（带 _partial_holder 成果）收尾。",
    "            if settings.outline_agent_enabled:",
    "                from app.services.agent import (",
    "                    outline_agent_event_stream as _agent_event_stream,",
    "                )",
    "",
    "                async for _agent_ev in _agent_event_stream(",
    "                    task_id=task_id, scheme_id=scheme_id, scheme=scheme,",
    "                    context=_assembled, word_budget=word_budget,",
    "                    partial_holder=_partial_holder,",
    "                    save_checkpoint=_save_outline_checkpoint,",
    "                ):",
    "                    # 进度事件合并运行统计（与 legacy _set_phase 同口径：",
    "                    # stats 后并，progress 由 last_p 单调折算）。",
    '                    if _agent_ev.get("event") == "progress":',
    "                        _ap = _agent_ev.get('progress')",
    "                        if isinstance(_ap, (int, float)):",
    '                            _prog["last_p"] = max(',
    "                                float(_ap), float(_prog.get('last_p') or 0.0))",
    "                        _agent_ev = {**_agent_ev,",
    "                                     **_snapshot_outline_stats(_prog)}",
    '                    yield "data: " + json.dumps(',
    "                        _agent_ev, ensure_ascii=False) + " + repr("\n\n").replace("'", '"') + nl if False else nl.join([
        '                    yield "data: " + json.dumps(',
        "                        _agent_ev, ensure_ascii=False) + \"\\n\\n\"",
    ]),
    "                return",
    "",
    "            # ✅ R58（2026-10-10）：一次性直出的**结果标志**。",
])

# 上面的三元表达式写法易错，改为直接构造字符串
block = nl.join([
    '            _prog["stepwise"] = use_stepwise',
    "",
    "            # ✅ 接线阶段（遗留① · 2026-10-25 · 《目录生成工具修正移植_实施报告》第五章)：",
    "            #    以 stage_runner 为骨架的 Agent 编排分支。outline_agent_enabled 默认 False，",
    "            #    本分支为死代码，legacy 链路逐字不动（保基线）；置 True 后转",
    "            #    outline_agent_event_stream（initial-outline → children_generation 单链），",
    "            #    产出与 legacy 同形状的 progress/completed/stopped 事件，",
    "            #    落库仍由前端 save-outline 完成；checkpoint → finish_task → yield",
    "            #    终态顺序在流内保持（R89 S2 降级口径同款）。异常/取消原样上抛，",
    "            #    由外层既有 error / CancelledError 分支（带 _partial_holder 成果）收尾。",
    "            if settings.outline_agent_enabled:",
    "                from app.services.agent import (",
    "                    outline_agent_event_stream as _agent_event_stream,",
    "                )",
    "",
    "                async for _agent_ev in _agent_event_stream(",
    "                    task_id=task_id, scheme_id=scheme_id, scheme=scheme,",
    "                    context=_assembled, word_budget=word_budget,",
    "                    partial_holder=_partial_holder,",
    "                    save_checkpoint=_save_outline_checkpoint,",
    "                ):",
    "                    # 进度事件合并运行统计（与 legacy _set_phase 同口径：stats 后并，",
    "                    # progress 由 last_p 单调折算，保证进度条不倒退）。",
    '                    if _agent_ev.get("event") == "progress":',
    "                        _ap = _agent_ev.get('progress')",
    "                        if isinstance(_ap, (int, float)):",
    '                            _prog["last_p"] = max(',
    "                                float(_ap), float(_prog.get('last_p') or 0.0))",
    "                        _agent_ev = {**_agent_ev,",
    "                                     **_snapshot_outline_stats(_prog)}",
    '                    yield "data: " + json.dumps(',
    '                        _agent_ev, ensure_ascii=False) + "\\n\\n"',
    "                return",
    "",
    "            # ✅ R58（2026-10-10）：一次性直出的**结果标志**。",
])

text = text.replace(anchor, block, 1)
p.write_bytes(text.encode("utf-8"))
raw2 = p.read_bytes()
print("after loneLF =", raw2.count(b"\n") - raw2.count(b"\r\n"))
py_compile.compile(str(p), doraise=True)
print("compile ok")
