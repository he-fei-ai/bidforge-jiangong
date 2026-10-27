import io, sys, py_compile, pathlib
p = pathlib.Path(r"j:\编程\专项方案工具箱\backend\app\services\agent\outline_agent_flow.py")
data = p.read_bytes()
print("size:", len(data))
print("loneLF:", data.count(b"\n") - data.count(b"\r\n"))
text = data.decode("utf-8")
needles = [
    "next_stage_index = (meta.get",
    "_chat_with_control",
    "except asyncio.CancelledError:",
    'state["stage"] = "children_generation"',
    "disambiguate_prompt_tools",
    "OutlineAgentStopped",
    "outline_agent_event_stream",
]
for n in needles:
    print(("OK  " if n in text else "MISS"), n)
try:
    py_compile.compile(str(p), doraise=True)
    print("compile ok")
except Exception as e:
    print("compile FAIL:", e)
