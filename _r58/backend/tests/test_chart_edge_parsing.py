"""回归测试：流程图边语法增强（2026-09-13）与甘特 JSON 分流修复"""
import json
from io import BytesIO
from unittest.mock import patch

import pytest

# ---------- 流程图边解析：中段标签 / 虚线 / 粗线 / 分号 ----------

def _parse(code):
    from app.services.ai.mermaid_flowchart import _parse_flowchart_structure
    return _parse_flowchart_structure(code)


def test_mid_edge_label_no_ghost_node():
    """BUG: `A5 -- "否" --> A3` 此前把 `A5 -- "否"` 注册成幽灵节点、边丢失。
    修复后 from=A5、label=否、to=A3，且无幽灵节点。"""
    s = _parse(
        'flowchart TD\n'
        '    A1["施工准备"] --> A2["钢筋绑扎"]\n'
        '    A2 --> A3["模板安装"]\n'
        '    A3 --> A4["混凝土浇筑"]\n'
        '    A4 --> A5{"验收合格?"}\n'
        '    A5 -- "否" --> A3\n'
        '    A5 -- "是" --> A6["养护"]\n'
    )
    ids = [n["id"] for n in s["nodes"]]
    assert "A5 -- 否" not in ids
    assert "A5 -- 是" not in ids
    assert ids == ["A1", "A2", "A3", "A4", "A5", "A6"]
    labels = {e["from"]: (e["to"], e["label"]) for e in s["edges"]}
    assert labels["A5"] in (("A3", "否"), ("A6", "是"))
    assert len(s["edges"]) == 6


def test_mid_edge_label_chinese_quotes_and_no_space():
    """中文引号 `--“验收”-->` 与无空格 `--"否"-->` 均应正确剥离引号。"""
    s = _parse(
        'flowchart TD\n'
        '    A --“验收”--> B\n'
        '    B --"否"--> C\n'
    )
    labels = {(e["from"], e["to"]): e["label"] for e in s["edges"]}
    assert labels[("A", "B")] == "验收"
    assert labels[("B", "C")] == "否"


def test_dotted_and_thick_edges():
    """虚线 -.-> / 带标签虚线 -. 文本 .-> / 粗线 ==> / 无箭头 --- 均应解析。"""
    s = _parse(
        'flowchart LR\n'
        '    A["入场"] -.-> B["测量"]\n'
        '    B -. 复测 .-> C["放线"]\n'
        '    C == 是 ==> D["复核"]\n'
        '    D --- E["验收"]\n'
    )
    edges = [(e["from"], e["to"], e["label"]) for e in s["edges"]]
    assert edges == [
        ("A", "B", None),
        ("B", "C", "复测"),
        ("C", "D", "是"),
        ("D", "E", None),
    ]


def test_chain_with_mid_label():
    """链式连线 + 中段标签组合：`B -- "合格" --> C` 与 `C --> D --> E`。"""
    s = _parse(
        'flowchart TD\n'
        '    A["开始"] --> B["处理"]\n'
        '    B -- "合格" --> C["结束"]\n'
        '    C --> D["归档"] --> E["完成"]\n'
    )
    edges = [(e["from"], e["to"], e["label"]) for e in s["edges"]]
    assert edges[1] == ("B", "C", "合格")
    assert ("C", "D", None) in edges and ("D", "E", None) in edges


def test_trailing_semicolon_endpoint():
    """行尾分号 A --> B; 不应产生幽灵节点 `B;`。"""
    s = _parse('flowchart TD\n    A["开工"] --> B["基础施工"];\n    B["基础施工"] --> C["主体"]')
    ids = [n["id"] for n in s["nodes"]]
    assert "B;" not in ids
    assert set(ids) == {"A", "B", "C"}
    assert len(s["edges"]) == 2


def test_pipe_label_regression():
    """既有管道标签语法不回归。"""
    s = _parse('flowchart TD\n    A["开始"] -->|"合格"| B["继续"]\n    A -->|"不合格"| C["返工"]')
    labels = {(e["from"], e["to"]): e["label"] for e in s["edges"]}
    assert labels[("A", "B")] == "合格"
    assert labels[("A", "C")] == "不合格"


# ---------- 甘特 JSON 载荷分流 ----------

def test_gantt_json_not_parsed_as_mermaid_syntax():
    """BUG: `{"type":"gantt","tasks":[...]}` 因含 "gantt" 字样被 _parse_mermaid_gantt
    误解析（任务名变 `{"type"`、总工期错成默认 90）。修复后 JSON 载荷必须跳过
    Mermaid 语法分支、直走结构化渲染。"""
    from app.services.ai import mermaid_renderer as MR

    payload = {
        "type": "gantt",
        "title": "施工进度计划",
        "totalDays": 180,
        "tasks": [
            {"id": 1, "name": "施工准备", "start": 1, "end": 30,
             "dependencies": [], "isMilestone": False},
            {"id": 2, "name": "竣工验收", "start": 176, "end": 180,
             "dependencies": [1], "isMilestone": True},
        ],
    }
    code = json.dumps(payload, ensure_ascii=False)
    with patch(
        "app.services.ai.mermaid_renderer._parse_mermaid_gantt",
        wraps=MR._parse_mermaid_gantt,
    ) as mocked:
        buf = MR.render_mermaid_to_bytes(code, "gantt", skip_http=True)
        # JSON 载荷不得进入 Mermaid 语法解析分支
        assert not mocked.called
    assert buf is not None and len(buf.getvalue()) > 1000

    # 对照：纯 Mermaid 语法仍应走语法解析分支
    mermaid_code = "gantt\n    title 测试\n    任务1 : 2026-01-01, 10d"
    with patch(
        "app.services.ai.mermaid_renderer._parse_mermaid_gantt",
        wraps=MR._parse_mermaid_gantt,
    ) as mocked2:
        buf2 = MR.render_mermaid_to_bytes(mermaid_code, "gantt", skip_http=True)
        assert mocked2.called
    assert buf2 is not None and len(buf2.getvalue()) > 1000


# ---------- 甘特 JSON 容器别名（tasks / items / rows）口径一致性 ----------

# BUG（占位型错配，2026-09-17 修复）：渲染分流只认字面 "tasks" 键，
# 而校验侧 normalize_gantt_plan 支持 items / rows 别名 —— AI 用别名写甘特数据时
# 管线校验通过（块留在正文、登记 done），渲染侧却返回 None，
# 导出成红字占位「[图 X-Y … — 渲染失败]」。
# 证据：`_diagnostics/_chart_shape_audit.py`「items 别名+日期串」「rows 别名」两例。
_GANTT_ALIAS_PAYLOADS = [
    pytest.param(
        {"type": "gantt", "title": "进度计划",
         "items": [{"name": "进场准备", "start": "2026-01-01", "end": "2026-01-20"},
                   {"name": "主体施工", "start": "2026-01-20", "end": "2026-05-01",
                    "dependencies": ["进场准备"]}]},
        id="items-alias-with-date-strings"),
    pytest.param(
        {"type": "gantt", "title": "进度计划",
         "rows": [{"title": "测量放线", "start": 0, "end": 5},
                  {"title": "土方开挖", "start": 5, "end": 25,
                   "dependencies": ["测量放线"]}]},
        id="rows-alias"),
]


@pytest.mark.parametrize("payload", _GANTT_ALIAS_PAYLOADS)
def test_gantt_json_alias_containers_render(payload):
    """容器别名（items / rows）必须能被渲染 —— 不得返回 None。"""
    from app.services.ai import mermaid_renderer as MR

    code = json.dumps(payload, ensure_ascii=False)
    buf = MR.render_mermaid_to_bytes(code, "gantt", skip_http=True)
    assert buf is not None, f"甘特 JSON 别名载荷渲染返回 None（导出会成红字占位）: {payload!r}"
    assert len(buf.getvalue()) > 1000


@pytest.mark.parametrize("payload", _GANTT_ALIAS_PAYLOADS)
def test_gantt_alias_validator_and_renderer_agree(payload):
    """不变量：管线校验放行 ⇒ 渲染必须出图（两侧同向，锁定「占位型错配」回归）。

    校验放行而渲染 None 会让坏图以「校验已过」的名义进入正文与导出，
    既不会被删块也不会被修复 —— 这是 `_chart_shape_audit` 判定的真实错配。
    """
    from app.routers._chart_pipeline import _validate_inline_chart
    from app.services.ai import mermaid_renderer as MR

    code = json.dumps(payload, ensure_ascii=False)
    ok, _fixed = _validate_inline_chart("gantt", code)
    img = MR.render_mermaid_to_bytes(code, "gantt", skip_http=True)
    rendered = img is not None and len(img.getvalue()) > 100
    assert ok is True, "别名载荷应被归一后接受（与 AI 提示词容错口径一致）"
    assert rendered is True, "校验放行却渲染不出图 = 占位型错配"


def test_gantt_tasks_key_still_preferred_over_aliases():
    """字面 tasks 键仍优先（不因别名支持而改变既有分流语义）。"""
    from app.services.ai import mermaid_renderer as MR

    payload = {
        "type": "gantt", "title": "进度计划",
        "tasks": [{"id": 1, "name": "规范任务", "start": 1, "end": 10}],
        # 同载荷里同时给出别名容器时，tasks 必须是唯一生效来源
        "items": [{"name": "不应生效", "start": "2026-01-01", "end": "2026-02-01"}],
    }
    buf = MR.render_mermaid_to_bytes(json.dumps(payload, ensure_ascii=False),
                                    "gantt", skip_http=True)
    assert isinstance(buf, BytesIO) and len(buf.getvalue()) > 1000


def test_gantt_mermaid_with_init_block():
    """%%{init}%% 头部 + gantt 语法仍可正常渲染。"""
    from app.services.ai import mermaid_renderer as MR

    code = (
        '%%{init: {"gantt": {"useWidth": false}}}%%\n'
        'gantt\n'
        '    title 专项施工进度\n'
        '    dateFormat YYYY-MM-DD\n'
        '    section 准备\n'
        '    场地平整 : 2026-03-01, 20d\n'
        '    结构封顶 : milestone, 2026-06-25, 0d\n'
    )
    buf = MR.render_mermaid_to_bytes(code, "gantt", skip_http=True, duration=120)
    assert buf is not None and len(buf.getvalue()) > 1000


# ---------- 边标签避让（渲染输出） ----------

def test_back_edge_label_rendered():
    """回边 `A5 -- "否" --> A3` 的标签"否"必须出现在输出图上
    （此前被节点框覆盖，导出图里看不到）。"""
    from app.services.ai.mermaid_flowchart import _render_flowchart_image_v2
    from PIL import Image

    code = (
        'flowchart TD\n'
        '    A1["施工准备"] --> A2["钢筋绑扎"]\n'
        '    A2 --> A3["模板安装"]\n'
        '    A3 --> A4["混凝土浇筑"]\n'
        '    A4 --> A5{"验收合格?"}\n'
        '    A5 -- "否" --> A3\n'
        '    A5 -- "是" --> A6["养护"]\n'
    )
    buf = _render_flowchart_image_v2(code)
    assert buf is not None
    # 渲染过程不抛异常即可；标签存在性由结构断言 + 人工目检覆盖
    im = Image.open(buf)
    assert im.size[0] > 0 and im.size[1] > 0


# ---------- 校验侧 vs 渲染侧口径一致性（2026-09-17 图表增强） ----------
# 背景：`_chart_pipeline` 的策略是「校验失败 → 从正文删除该图表块」，因此
#   · 校验拒绝 + 渲染能出图 = ❗删块型错配（图被悄悄删掉）
#   · 校验通过 + 渲染返回 None = ⚠ 占位型错配（导出只剩红字[渲染失败]）
# 下列用例把每一处修复锁成不变量：**放行 ⇔ 必出图**。

def _validate(ct, code):
    from app.routers._chart_pipeline import _validate_inline_chart
    return _validate_inline_chart(ct, code)


def _renders(ct, code) -> bool:
    from app.services.ai import mermaid_renderer as MR
    buf = MR.render_mermaid_to_bytes(code, ct, skip_http=True)
    return buf is not None and len(buf.getvalue()) > 100


@pytest.mark.parametrize("payload", [
    {"type": "comparison", "title": "占比",
     "items": [{"label": "A", "value": 40}, {"label": "B", "value": 60}]},
    {"type": "comparison", "headers": ["方案", "费用"],
     "rows": [{"方案": "A", "费用": 80}, {"方案": "B", "费用": 120}]},
    {"type": "comparison", "headers": ["方案", "材料费", "人工费"],
     "rows": [{"label": "A", "values": [80, 50]}, {"label": "B", "values": [60, 70]}]},
    {"type": "comparison", "data": [{"name": "A", "value": 30}, {"name": "B", "value": 70}]},
])
def test_comparison_validator_and_renderer_agree(payload):
    """comparison 四种自然形态：校验放行 ⇔ 渲染出图（两侧共用归一器）。"""
    code = json.dumps(payload, ensure_ascii=False)
    ok, _ = _validate("comparison", code)
    assert ok is True
    assert _renders("comparison", code) is True


@pytest.mark.parametrize("payload", [
    {"type": "comparison", "title": "方案比选"},
    {"type": "comparison", "note": "暂无数据"},
    {"type": "comparison", "items": [{"label": "A", "value": 0}, {"label": "B", "value": 0}]},
])
def test_comparison_unrenderable_payload_rejected_by_pipeline(payload):
    """渲染不出的 comparison 载荷必须在**管线侧被拒**。

    修复前 comparison 无校验器（一律放行）→ 块保留、status=done → 导出时
    渲染器返回 None → 交付文档出现红字占位「[图 X-Y … — 渲染失败]」。
    """
    code = json.dumps(payload, ensure_ascii=False)
    ok, _ = _validate("comparison", code)
    assert ok is False
    assert _renders("comparison", code) is False


def test_architecture_duplicate_sibling_labels_kept_and_rendered():
    """同级重名不再删整张架构图（降级为告警），且必然出图。"""
    payload = {"type": "architecture",
               "root": {"label": "项目部",
                        "children": [{"label": "技术组"}, {"label": "技术组"}]}}
    code = json.dumps(payload, ensure_ascii=False)
    ok, _ = _validate("architecture", code)
    assert ok is True
    assert _renders("architecture", code) is True


def test_labor_single_zero_phase_kept_all_zero_rejected():
    """单个阶段全 0 → 保留出图；所有阶段全 0（整图无数值信息）→ 拒绝。"""
    one_zero = {"type": "labor", "phases": ["准备", "主体"],
                "categories": ["普工", "钢筋工"], "data": [[0, 0], [30, 20]]}
    code = json.dumps(one_zero, ensure_ascii=False)
    ok, _ = _validate("labor", code)
    assert ok is True
    assert _renders("labor", code) is True

    all_zero = {"type": "labor", "phases": ["准备", "主体"],
                "categories": ["普工", "钢筋工"], "data": [[0, 0], [0, 0]]}
    assert _validate("labor", json.dumps(all_zero, ensure_ascii=False))[0] is False


@pytest.mark.parametrize("code", [
    # 单行分号（旧实现：`graph` 行整体被跳过 → 节点数算成 0 → 判"节点数不足"→ 删块）
    'flowchart TD; A["准备"] --> B["施工"]; B --> C["验收"]',
    # 行末分号
    'flowchart TD\n    A["准备"] --> B["施工"];\n    B --> C["验收"]',
    # 首部 %% 注释
    '%% 施工流程\nflowchart TD\n    A["准备"] --> B["施工"]\n    B --> C["验收"]',
])
def test_mermaid_statement_and_comment_forms_kept_and_rendered(code):
    """分号 / 首部注释写法：校验放行 **且** 必出图（两侧共用归一实现）。"""
    ok, _ = _validate("flowchart", code)
    assert ok is True
    assert _renders("flowchart", code) is True


def test_renderer_normalizes_statement_separator_after_unwrap():
    """`{"code": "<mermaid>"}` 解包后的内层代码也必须归一。

    旧实现把 `;` → 换行的归一化放在「包装器解包」**之前**，内层单行分号 Mermaid
    从未被处理 → 所有按行解析的 PIL 渲染器吞掉连线（图残缺）。
    """
    from app.services.ai import mermaid_renderer as MR

    inner = 'flowchart TD; A["施工准备"] --> B["主体施工"]; B --> C["竣工验收"]'
    buf = MR.render_mermaid_to_bytes(json.dumps({"code": inner}, ensure_ascii=False),
                                    "flowchart", skip_http=True)
    assert buf is not None and len(buf.getvalue()) > 1000


def test_normalize_mermaid_statements_keeps_quoted_semicolon():
    """分号归一化只作用于引号外 / 括号外：标签内的分号必须原样保留。"""
    from app.services.chart_validators import normalize_mermaid_statements

    assert normalize_mermaid_statements('A["准备;验收"] --> B') == 'A["准备;验收"] --> B'
    assert normalize_mermaid_statements('A["准备"] --> B; B --> C') == 'A["准备"] --> B\n B --> C'
