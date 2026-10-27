import pathlib

p = pathlib.Path(r"j:\编程\专项方案工具箱\backend\app\services\agent\__init__.py")
raw = p.read_bytes()
assert b"\r\n" in raw and raw.count(b"\n") - raw.count(b"\r\n") == 0, "not pure CRLF"
text = raw.decode("utf-8")

# 1) __all__ 追加（锚在 __all__ 段内的 RESTORED_NOTE_TEMPLATE 行之后）
all_anchor = '    "RESTORED_NOTE_TEMPLATE",\r\n]'
assert text.count(all_anchor) == 1, f"__all__ anchor count={text.count(all_anchor)}"
all_add = (
    '    "RESTORED_NOTE_TEMPLATE",\r\n'
    "    # P3 —— Schema 本体与校验器工厂（outline_schemas）\r\n"
    '    "OUTLINE_JSON_SCHEMA",\r\n'
    '    "build_json_validation_schemas",\r\n'
    '    "build_output_validators",\r\n'
    "    # 接线阶段 —— Agent 六阶段编排入口（outline_agent_flow，遗留①）\r\n"
    '    "OUTLINE_AGENT_TASK_KEY",\r\n'
    '    "OutlineAgentStopped",\r\n'
    '    "OutlineAgentReportedFailure",\r\n'
    '    "OutlineAgentOutcome",\r\n'
    '    "disambiguate_prompt_tools",\r\n'
    '    "build_task_instruction",\r\n'
    '    "seed_files_from_context",\r\n'
    '    "workspace_root_for",\r\n'
    '    "build_outline_agent_tools",\r\n'
    '    "run_outline_agent",\r\n'
    '    "outline_agent_event_stream",\r\n'
    "]"
)
text = text.replace(all_anchor, all_add, 1)

# 2) _LAZY_MODULES 追加
lazy_anchor = '    "RESTORED_NOTE_TEMPLATE": "stage_runner",\r\n}'
assert text.count(lazy_anchor) == 1, f"lazy anchor count={text.count(lazy_anchor)}"
lazy_add = (
    '    "RESTORED_NOTE_TEMPLATE": "stage_runner",\r\n'
    '    "OUTLINE_JSON_SCHEMA": "outline_schemas",\r\n'
    '    "build_json_validation_schemas": "outline_schemas",\r\n'
    '    "build_output_validators": "outline_schemas",\r\n'
    '    "OUTLINE_AGENT_TASK_KEY": "outline_agent_flow",\r\n'
    '    "OutlineAgentStopped": "outline_agent_flow",\r\n'
    '    "OutlineAgentReportedFailure": "outline_agent_flow",\r\n'
    '    "OutlineAgentOutcome": "outline_agent_flow",\r\n'
    '    "disambiguate_prompt_tools": "outline_agent_flow",\r\n'
    '    "build_task_instruction": "outline_agent_flow",\r\n'
    '    "seed_files_from_context": "outline_agent_flow",\r\n'
    '    "workspace_root_for": "outline_agent_flow",\r\n'
    '    "build_outline_agent_tools": "outline_agent_flow",\r\n'
    '    "run_outline_agent": "outline_agent_flow",\r\n'
    '    "outline_agent_event_stream": "outline_agent_flow",\r\n'
    "}"
)
text = text.replace(lazy_anchor, lazy_add, 1)

p.write_bytes(text.encode("utf-8"))
raw2 = p.read_bytes()
print("loneLF now =", raw2.count(b"\n") - raw2.count(b"\r\n"))

import py_compile
py_compile.compile(str(p), doraise=True)
print("compile ok")

# 冒烟：惰性导出解析
import sys
sys.path.insert(0, str(pathlib.Path(r"j:\编程\专项方案工具箱\backend")))
from app.services import agent
for name in ("OUTLINE_JSON_SCHEMA", "build_output_validators",
             "OUTLINE_AGENT_TASK_KEY", "disambiguate_prompt_tools",
             "outline_agent_event_stream", "RESTORED_NOTE_TEMPLATE"):
    obj = getattr(agent, name)
    print("lazy ok:", name, type(obj).__name__)
