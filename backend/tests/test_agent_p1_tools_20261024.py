"""P1 工具层用例（可直接放入 backend/tests/ 运行）。

覆盖：工作区路径越界防护、read/write/edit/grep/find/ls 六个原生工具、
json-validation 五种 stage、ask-user 三条参数校验、report-failure 双形态与
``continued`` 门禁。

约定：handler 均为 async（agent_loop 契约），用 ``asyncio.run`` 同步驱动，
不依赖 pytest-asyncio；本仓 pytest.ini 的 asyncio_mode=auto 下同样可跑。
"""
import asyncio
import json
import os
from pathlib import Path

import pytest
from app.services.agent.agent_loop import AgentTool, ToolCallOutcome
from app.services.agent.json_validation import build_json_validation_tool
from app.services.agent.report_failure import (
    CONTINUE_OPTION,
    CUSTOM_OPTION,
    TERMINATE_OPTION,
    TERMINATION_OPTIONS,
    build_report_failure_tool,
)
from app.services.agent.user_question import build_ask_user_tool
from app.services.agent.workspace_tools import (
    build_edit_tool,
    build_find_tool,
    build_grep_tool,
    build_ls_tool,
    build_read_tool,
    build_write_tool,
    normalize_workspace_file_path,
    resolve_workspace_file,
)


def run(coro):
    """同步驱动 async handler（不依赖 pytest-asyncio）。"""
    return asyncio.run(coro)


def make_ws(tmp_path) -> str:
    ws = tmp_path / "ws"
    ws.mkdir()
    return str(ws)


# ── 路径防护 ─────────────────────────────────────────────────────────────

def test_路径越界全部被拒(tmp_path):
    ws = make_ws(tmp_path)
    for bad in [
        "../x",              # POSIX 上跳
        "..\\x",             # 反斜杠上跳（归一后仍是上跳）
        "sub\\..\\..\\x",    # 中段上跳
        "C:\\x",             # Windows 盘符
        "C:/x",              # 归一后的盘符
        "/abs/path",         # POSIX 绝对路径
        "\\abs\\path",       # 反斜杠开头 → 归一为绝对路径
        "//server/share",    # UNC
    ]:
        with pytest.raises(ValueError):
            resolve_workspace_file(ws, bad)
    # 空串/空白拒绝
    with pytest.raises(ValueError):
        normalize_workspace_file_path("")
    with pytest.raises(ValueError):
        normalize_workspace_file_path("   ")


def test_反斜杠归一与合法路径(tmp_path):
    ws = make_ws(tmp_path)
    assert normalize_workspace_file_path("sub\\a.txt") == "sub/a.txt"
    rel, resolved = resolve_workspace_file(ws, "sub\\a.txt")
    assert rel == "sub/a.txt"
    assert resolved == os.path.join(os.path.abspath(ws), "sub", "a.txt")
    # 工作区根自身允许
    rel, resolved = resolve_workspace_file(ws, ".")
    assert resolved == os.path.abspath(ws)
    # 合法的中段 ..（未越界）允许并归一
    assert normalize_workspace_file_path("sub/../a.txt") == "a.txt"
    # Node path.normalize 的结尾斜杠语义保留
    assert normalize_workspace_file_path("sub/") == "sub/"


def test_工具层拒绝越界路径(tmp_path):
    ws = make_ws(tmp_path)
    tool = build_read_tool(ws)
    outcome = run(tool.handler({"path": "../secret.txt"}))
    assert outcome.is_error
    assert "超出当前工作区" in outcome.content


# ── read ────────────────────────────────────────────────────────────────

def test_read_基础读取与offset超界(tmp_path):
    ws = make_ws(tmp_path)
    (Path(ws) / "sample.txt").write_text("line1\nline2\nline3\n", encoding="utf-8")
    tool = build_read_tool(ws)
    out = run(tool.handler({"path": "sample.txt"}))
    assert not out.is_error
    assert out.content == "line1\nline2\nline3\n"
    # split('\n') 产生 4 行（含末尾空行），offset=10 超出文件末尾
    out = run(tool.handler({"path": "sample.txt", "offset": 10}))
    assert out.is_error
    assert "offset 10 超出文件末尾（共 4 行）" in out.content
    # offset=0 视为首行（对齐 pi 的 falsy 分支）
    out = run(tool.handler({"path": "sample.txt", "offset": 0}))
    assert not out.is_error
    assert out.content.startswith("line1")
    # 文件不存在 → 错误文本而非抛异常
    out = run(tool.handler({"path": "nope.txt"}))
    assert out.is_error and "无法读取文件" in out.content


def test_read_limit与续读提示(tmp_path):
    ws = make_ws(tmp_path)
    content = "\n".join(f"L{i}" for i in range(1, 11))  # 10 行
    (Path(ws) / "a.txt").write_text(content, encoding="utf-8")
    tool = build_read_tool(ws)
    out = run(tool.handler({"path": "a.txt", "offset": 3, "limit": 2}))
    assert not out.is_error
    assert out.content.startswith("L3\nL4")
    assert "[6 more lines in file. Use offset=5 to continue.]" in out.content


def test_read_行数截断与offset提示(tmp_path):
    ws = make_ws(tmp_path)
    content = "\n".join(f"line{i}" for i in range(1, 2002))  # 2001 行
    (Path(ws) / "big.txt").write_text(content, encoding="utf-8")
    tool = build_read_tool(ws)
    out = run(tool.handler({"path": "big.txt"}))
    assert not out.is_error
    assert "line2000" in out.content
    assert "line2001" not in out.content
    assert "[Showing lines 1-2000 of 2001. Use offset=2001 to continue.]" in out.content


def test_read_首行超字节上限给中文offset提示(tmp_path):
    ws = make_ws(tmp_path)
    (Path(ws) / "huge.txt").write_text("x" * 60000, encoding="utf-8")  # 单行 60000B > 50KB
    tool = build_read_tool(ws)
    out = run(tool.handler({"path": "huge.txt"}))
    assert not out.is_error
    assert "请继续用 offset=2" in out.content
    assert "x" * 100 not in out.content


# ── write ───────────────────────────────────────────────────────────────

def test_write_创建目录并覆盖(tmp_path):
    ws = make_ws(tmp_path)
    tool = build_write_tool(ws)
    out = run(tool.handler({"path": "deep/nested/a.txt", "content": "第一版"}))
    assert not out.is_error
    target = Path(ws) / "deep" / "nested" / "a.txt"
    assert target.read_text(encoding="utf-8") == "第一版"
    assert f"Successfully wrote {len('第一版'.encode('utf-8'))} bytes" in out.content
    # 覆盖写
    out = run(tool.handler({"path": "deep/nested/a.txt", "content": "第二版"}))
    assert not out.is_error
    assert target.read_text(encoding="utf-8") == "第二版"
    # 越界拒绝
    out = run(tool.handler({"path": "..\\out.txt", "content": "x"}))
    assert out.is_error


# ── edit ────────────────────────────────────────────────────────────────

def test_edit_成功替换并保留行尾(tmp_path):
    ws = make_ws(tmp_path)
    (Path(ws) / "f.txt").write_bytes(b"a\r\nb\r\n")  # CRLF 原文
    tool = build_edit_tool(ws)
    out = run(tool.handler({"path": "f.txt",
                            "edits": [{"oldText": "b", "newText": "z"}]}))
    assert not out.is_error
    assert "Successfully replaced 1 block(s)" in out.content
    assert (Path(ws) / "f.txt").read_bytes() == b"a\r\nz\r\n"


def test_edit_重复oldText报错(tmp_path):
    ws = make_ws(tmp_path)
    (Path(ws) / "f.txt").write_text("x\ny\nx\n", encoding="utf-8")
    tool = build_edit_tool(ws)
    out = run(tool.handler({"path": "f.txt",
                            "edits": [{"oldText": "x", "newText": "v"}]}))
    assert out.is_error
    assert "2 处" in out.content
    assert "唯一" in out.content
    # 报错不改文件
    assert (Path(ws) / "f.txt").read_text(encoding="utf-8") == "x\ny\nx\n"


def test_edit_未找到与重叠报错(tmp_path):
    ws = make_ws(tmp_path)
    (Path(ws) / "f.txt").write_text("abcdef\n", encoding="utf-8")
    tool = build_edit_tool(ws)
    out = run(tool.handler({"path": "f.txt",
                            "edits": [{"oldText": "zzz", "newText": "v"}]}))
    assert out.is_error and "未找到" in out.content
    # bcd(1..4) 与 cde(2..5) 重叠
    out = run(tool.handler({"path": "f.txt", "edits": [
        {"oldText": "bcd", "newText": "B"},
        {"oldText": "cde", "newText": "C"},
    ]}))
    assert out.is_error and "重叠" in out.content
    # 空 oldText 拒绝
    out = run(tool.handler({"path": "f.txt",
                            "edits": [{"oldText": "", "newText": "v"}]}))
    assert out.is_error and "不能为空" in out.content


# ── grep ────────────────────────────────────────────────────────────────

def _make_grep_ws(tmp_path) -> str:
    ws = make_ws(tmp_path)
    (Path(ws) / "pkg").mkdir()
    (Path(ws) / "pkg" / "mod.py").write_text(
        "def alpha():\n    pass\n# beta\ndef gamma(x):\n", encoding="utf-8")
    (Path(ws) / "a.txt").write_text(
        "hello world\nHELLO again\nvalue a.c here\nvalue abc here\n",
        encoding="utf-8")
    (Path(ws) / "hits.txt").write_text(
        "hit1\nhit2\nhit3\nhit4\n", encoding="utf-8")
    return ws


def test_grep_正则与子目录搜索(tmp_path):
    ws = _make_grep_ws(tmp_path)
    tool = build_grep_tool(ws)
    out = run(tool.handler({"pattern": r"def \w+", "path": "pkg"}))
    assert not out.is_error
    assert "mod.py:1: def alpha():" in out.content
    assert "mod.py:4: def gamma(x):" in out.content
    # 无匹配
    out = run(tool.handler({"pattern": "绝对不会出现的字符串", "path": "pkg"}))
    assert out.content == "No matches found"


def test_grep_字面量与正则差异(tmp_path):
    ws = _make_grep_ws(tmp_path)
    tool = build_grep_tool(ws)
    out = run(tool.handler({"pattern": "a.c", "literal": True}))
    assert not out.is_error
    assert "value a.c here" in out.content
    assert "value abc here" not in out.content
    # 正则模式下 a.c 同时命中 a.c 与 abc
    out = run(tool.handler({"pattern": "a.c"}))
    assert "value abc here" in out.content
    # ignore_case
    out = run(tool.handler({"pattern": "hello", "ignore_case": True}))
    assert "a.txt:1: hello world" in out.content
    assert "a.txt:2: HELLO again" in out.content


def test_grep_limit与单行截断(tmp_path):
    ws = _make_grep_ws(tmp_path)
    tool = build_grep_tool(ws)
    out = run(tool.handler({"pattern": "hit", "path": "hits.txt", "limit": 2}))
    assert not out.is_error
    matched = [ln for ln in out.content.splitlines() if ln.startswith("hits.txt:")]
    assert len(matched) == 2
    assert ("2 matches limit reached. Use limit=4 for more, or refine pattern"
            in out.content)
    # 单行 500 字符截断
    (Path(ws) / "long.txt").write_text("needle " + "y" * 600, encoding="utf-8")
    out = run(tool.handler({"pattern": "needle", "path": "long.txt"}))
    assert not out.is_error
    assert "... [truncated]" in out.content
    assert "Some lines truncated to 500 chars." in out.content


def test_grep_越界路径拒绝(tmp_path):
    ws = _make_grep_ws(tmp_path)
    tool = build_grep_tool(ws)
    out = run(tool.handler({"pattern": "hit", "path": "../outside"}))
    assert out.is_error and "超出当前工作区" in out.content


# ── find ────────────────────────────────────────────────────────────────

def _make_find_ws(tmp_path) -> str:
    ws = make_ws(tmp_path)
    (Path(ws) / "a.txt").write_text("x", encoding="utf-8")
    (Path(ws) / "x.md").write_text("x", encoding="utf-8")
    (Path(ws) / "docs").mkdir()
    (Path(ws) / "docs" / "guide.md").write_text("x", encoding="utf-8")
    (Path(ws) / "src").mkdir()
    (Path(ws) / "src" / "app.ts").write_text("x", encoding="utf-8")
    (Path(ws) / "src" / "deep").mkdir()
    (Path(ws) / "src" / "deep" / "util.ts").write_text("x", encoding="utf-8")
    return ws


def test_find_文件名与路径glob(tmp_path):
    ws = _make_find_ws(tmp_path)
    tool = build_find_tool(ws)
    # 不含 / 的模式按文件名匹配（fd --glob 语义）
    out = run(tool.handler({"pattern": "*.txt"}))
    assert not out.is_error
    assert out.content.splitlines() == ["a.txt"]
    # ** 跨目录
    out = run(tool.handler({"pattern": "**/*.md"}))
    assert set(out.content.splitlines()) == {"x.md", "docs/guide.md"}
    # 含 / 的模式按相对全路径匹配，单层 * 不跨目录
    out = run(tool.handler({"pattern": "src/*.ts"}))
    assert out.content.splitlines() == ["src/app.ts"]


def test_find_limit与无匹配(tmp_path):
    ws = _make_find_ws(tmp_path)
    tool = build_find_tool(ws)
    out = run(tool.handler({"pattern": "**/*", "limit": 2}))
    assert len(out.content.splitlines()) >= 2
    assert ("2 results limit reached. Use limit=4 for more, or refine pattern"
            in out.content)
    out = run(tool.handler({"pattern": "*.sql"}))
    assert out.content == "No files found matching pattern"
    # path 指向非目录 → 报错
    out = run(tool.handler({"pattern": "*.txt", "path": "a.txt"}))
    assert out.is_error and "路径不存在" in out.content


# ── ls ──────────────────────────────────────────────────────────────────

def test_ls_字母序与目录后缀(tmp_path):
    ws = make_ws(tmp_path)
    (Path(ws) / "b.txt").write_text("x", encoding="utf-8")
    (Path(ws) / "A.txt").write_text("x", encoding="utf-8")
    (Path(ws) / "Zdir").mkdir()
    (Path(ws) / "ad").mkdir()
    tool = build_ls_tool(ws)
    out = run(tool.handler({}))
    assert not out.is_error
    # 大小写不敏感字母序，目录带 / 后缀
    assert out.content.splitlines() == ["A.txt", "ad/", "b.txt", "Zdir/"]


def test_ls_错误语义与limit(tmp_path):
    ws = make_ws(tmp_path)
    (Path(ws) / "f.txt").write_text("x", encoding="utf-8")
    (Path(ws) / "d").mkdir()
    tool = build_ls_tool(ws)
    # 不存在 / 非目录（Path not found、Not a directory 的中文语义）
    out = run(tool.handler({"path": "nope"}))
    assert out.is_error and "路径不存在" in out.content
    out = run(tool.handler({"path": "f.txt"}))
    assert out.is_error and "不是目录" in out.content
    # 空目录
    out = run(tool.handler({"path": "d"}))
    assert out.content == "(empty directory)"
    # limit 触发提示
    (Path(ws) / "g.txt").write_text("x", encoding="utf-8")
    out = run(tool.handler({"limit": 2}))
    assert "2 entries limit reached. Use limit=4 for more" in out.content


# ── json-validation ─────────────────────────────────────────────────────

def test_json_validation_五种stage(tmp_path):
    ws = make_ws(tmp_path)
    schemas = {
        "result.json": {
            "type": "object",
            "required": ["name"],
            "properties": {
                "name": {"type": "string"},
                "count": {"type": "integer", "minimum": 0},
            },
        },
    }
    tool = build_json_validation_tool(ws, schemas)

    # 1) 预置 Schema 命中 → success
    (Path(ws) / "result.json").write_text(
        '{"name": "标书", "count": 3}', encoding="utf-8")
    out = run(tool.handler({"file_path": "result.json"}))
    payload = json.loads(out.content)
    assert payload == {
        "valid": True,
        "stage": "success",
        "file_path": "result.json",
        "errors": [],
        "message": "JSON.parse 和 Ajv 校验均已通过。",
    }
    assert not out.is_error

    # 2) 数据不合规 → validation（isError），错误结构对齐 normalizeAjvErrors
    (Path(ws) / "result.json").write_text(
        '{"name": 123, "count": -1}', encoding="utf-8")
    out = run(tool.handler({"file_path": "result.json"}))
    payload = json.loads(out.content)
    assert out.is_error
    assert payload["valid"] is False
    assert payload["stage"] == "validation"
    assert payload["message"] == "校验未通过，请根据 errors 修复文件后重新校验。"
    assert {e["keyword"] for e in payload["errors"]} == {"type", "minimum"}
    paths = {e["instancePath"] for e in payload["errors"]}
    assert paths == {"/name", "/count"}
    for err in payload["errors"]:
        assert set(err) == {"instancePath", "schemaPath", "keyword", "message", "params"}

    # 3) 未预置 Schema 且调用未提供 → schema 阶段
    #    （易标 createToolResult 对所有失败 stage 统一用通用 message，
    #     具体原因放 errors[0].message，此处照抄该契约）
    (Path(ws) / "other.json").write_text('{"a": 1}', encoding="utf-8")
    out = run(tool.handler({"file_path": "other.json"}))
    payload = json.loads(out.content)
    assert out.is_error and payload["stage"] == "schema"
    assert payload["message"] == "校验未通过，请根据 errors 修复文件后重新校验。"
    assert payload["errors"][0]["message"] == (
        "任务未为 other.json 预置 Schema，调用时必须提供 schema")

    # 3b) 未预置但调用方提供 schema → 按提供 schema 校验通过
    out = run(tool.handler({"file_path": "other.json",
                            "schema": {"type": "object"}}))
    payload = json.loads(out.content)
    assert not out.is_error and payload["stage"] == "success"

    # 4) 非法 JSON → parse
    (Path(ws) / "result.json").write_text("{oops", encoding="utf-8")
    out = run(tool.handler({"file_path": "result.json"}))
    payload = json.loads(out.content)
    assert out.is_error and payload["stage"] == "parse"
    assert payload["errors"][0]["message"]

    # 5) 文件不存在 → read
    out = run(tool.handler({"file_path": "missing.json"}))
    payload = json.loads(out.content)
    assert out.is_error and payload["stage"] == "read"
    assert payload["file_path"] == "missing.json"


# ── ask-user ────────────────────────────────────────────────────────────

def _options(custom_flags=(False, True)):
    opts = []
    for i, custom in enumerate(custom_flags):
        opts.append({
            "label": f"选项{i + 1}",
            "description": f"说明{i + 1}",
            "custom": custom,
        })
    return opts


def test_ask_user_三条参数校验():
    calls = []

    async def channel(request):
        calls.append(request)
        return {"selected_option": "选项1"}

    tool = build_ask_user_tool(channel)
    # ① 数量不在 2–5
    out = run(tool.handler({"question": "选哪个？", "options": [_options()[0]]}))
    assert out.is_error and "2 至 5" in out.content
    out = run(tool.handler({"question": "选哪个？", "options": _options((False,) * 6)}))
    assert out.is_error and "2 至 5" in out.content
    # ② 首项不能是 custom
    first_custom = _options((True, False))
    out = run(tool.handler({"question": "选哪个？", "options": first_custom}))
    assert out.is_error
    assert out.content == "推荐选项不能要求用户补充输入，请调整选项后重新提问"
    # ③ custom 最多 1 个
    out = run(tool.handler({"question": "选哪个？",
                            "options": _options((False, True, True))}))
    assert out.is_error
    assert out.content == "自定义输入选项最多只能有一个，请调整选项后重新提问"
    # 校验失败不触发通道
    assert calls == []


def test_ask_user_成功问答与prompt守则():
    captured = {}

    async def channel(request):
        captured.update(request)
        return {"selected_option": "选项1", "is_custom": False, "answer": ""}

    tool = build_ask_user_tool(channel)
    options = _options((False, True))
    out = run(tool.handler({"question": "目录按推荐生成吗？", "options": options},
                           tool_call_id="call_9"))
    payload = json.loads(out.content)
    assert payload["answered"] is True
    assert payload["selected_option"] == "选项1"
    assert not out.is_error
    assert captured["question"] == "目录按推荐生成吗？"
    assert captured["options"] == options
    # 易标 requestUserQuestion({tool_call_id, question, options}) 契约：
    # 前端按 tool_call_id 关联本次提问的回答
    assert captured["tool_call_id"] == "call_9"
    # sequential + 8 条 promptGuidelines 原文
    assert tool.sequential is True
    assert isinstance(tool, AgentTool)
    assert len(tool.prompt_guidelines) == 8
    assert tool.prompt_guidelines[0] == "已有材料足以判断时自主执行，不要调用 ask-user。"
    assert tool.prompt_guidelines[-1].startswith("普通选项设置 custom=false")
    # OpenAI 下发契约
    spec = tool.to_openai()
    assert spec["type"] == "function"
    assert spec["function"]["name"] == "ask-user"


# ── report-failure ──────────────────────────────────────────────────────

def test_report_failure_TERMINATION_OPTIONS逐字移植():
    assert [o["label"] for o in TERMINATION_OPTIONS] == [
        "降低质量继续", "终止任务", "补充处理要求"]
    assert TERMINATION_OPTIONS[0] == {
        "label": "降低质量继续",
        "description": "放宽质量要求，只保证流程能完成，结果需要人工核对",
        "custom": False,
    }
    assert TERMINATION_OPTIONS[1] == {
        "label": "终止任务",
        "description": "结束当前任务，可补充资料或调整设置后重新执行",
        "custom": False,
    }
    assert TERMINATION_OPTIONS[2] == {
        "label": "补充处理要求",
        "description": "输入您希望 Agent 如何继续处理",
        "custom": True,
    }


def test_report_failure_简单形态():
    reported = []
    tool = build_report_failure_tool(lambda reason: reported.append(reason))
    out = run(tool.handler({"reason": "缺少上年度报价资料"}))
    # 报告失败即任务终点：stop 让循环收束
    assert out.stop is True
    assert not out.is_error
    payload = json.loads(out.content)
    assert payload == {"reported": True, "reason": "缺少上年度报价资料"}
    assert reported == ["缺少上年度报价资料"]
    # 通道未初始化
    tool2 = build_report_failure_tool(None)
    out = run(tool2.handler({"reason": "x"}))
    assert out.is_error and out.content == "任务失败报告通道未初始化"


def test_report_failure_双形态_继续与continued门禁():
    reported = []
    decisions = []

    async def decide(request):
        decisions.append(request)
        if not request["auto_answer"]:
            raise RuntimeError("终止决策必须人工确认")
        return {"selected_option": CONTINUE_OPTION["label"],
                "is_custom": False, "answer": ""}

    tool = build_report_failure_tool(lambda r: reported.append(r), decide)
    # 第一次请求终止：允许自动作答 → 用户选继续，放宽要求继续执行
    out1 = run(tool.handler({"reason": "资料不足"}, tool_call_id="call_7"))
    assert not out1.is_error and out1.stop is False
    assert out1.content.startswith("用户选择不终止任务，要求继续执行。")
    assert "不得以同一原因再次请求终止" in out1.content
    assert out1.payload == {"terminated": False,
                            "selected_option": "降低质量继续"}
    assert decisions[0]["auto_answer"] is True
    # 易标 requestTerminationDecision 携带 tool_call_id，前端按调用关联回答
    assert decisions[0]["tool_call_id"] == "call_7"
    assert decisions[0]["question"].startswith("Agent 认为当前任务无法按原要求继续：")
    assert "是否终止当前任务？" in decisions[0]["question"]
    # 第二次同因请求终止：continued 门禁关闭自动作答 → 被拒
    out2 = run(tool.handler({"reason": "资料不足"}))
    assert out2.is_error and out2.stop is False
    assert decisions[1]["auto_answer"] is False
    assert "终止决策必须人工确认" in out2.content
    # 全程未真正上报失败
    assert reported == []


def test_report_failure_双形态_终止收束():
    reported = []

    async def decide(request):
        return {"selected_option": TERMINATE_OPTION["label"],
                "is_custom": False, "answer": ""}

    tool = build_report_failure_tool(lambda r: reported.append(r), decide)
    out = run(tool.handler({"reason": "无法获取招标原文"}))
    assert out.stop is True and not out.is_error
    payload = json.loads(out.content)
    assert payload == {"terminated": True, "reason": "无法获取招标原文"}
    assert reported == ["无法获取招标原文（已按您的选择终止任务）"]


def test_report_failure_双形态_自定义补充要求():
    async def decide(request):
        return {"selected_option": CUSTOM_OPTION["label"],
                "is_custom": True, "answer": "按第二套目录"}

    tool = build_report_failure_tool(lambda r: None, decide)
    out = run(tool.handler({"reason": "原目录不适用"}))
    assert not out.is_error and out.stop is False
    assert "用户补充要求：按第二套目录" in out.content
    assert "该要求优先执行" in out.content
    assert out.payload == {"terminated": False,
                           "selected_option": "补充处理要求",
                           "user_reply": "按第二套目录"}


# ── 包契约 ──────────────────────────────────────────────────────────────

def test_包惰性导出():
    from app.services.agent import (  # noqa: PLC0415
        build_ls_tool,
        normalize_workspace_file_path,
        resolve_workspace_file,
    )
    assert callable(build_ls_tool)
    assert callable(normalize_workspace_file_path)
    assert callable(resolve_workspace_file)


def test_工具to_openai契约(tmp_path):
    tool = build_read_tool(make_ws(tmp_path))
    assert isinstance(tool, AgentTool)
    spec = tool.to_openai()
    assert spec["type"] == "function"
    assert spec["function"]["name"] == "read"
    assert spec["function"]["parameters"]["required"] == ["path"]
    # read 工具未显式声明 additionalProperties（与 P0/易标参数契约一致）
    assert "additionalProperties" not in spec["function"]["parameters"]
    assert isinstance(tool.prompt_guidelines, list) and tool.prompt_guidelines
