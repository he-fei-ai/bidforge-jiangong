"""R179 · 图表生成与渲染模块深度审计回归（2026-11-05）。

覆盖本批次修复的四类缺陷：

1. G-04：劳动力图「推断判据」与「渲染归一别名表」不同源 —— trades / stages /
   time / types / labels+values 等合法载荷被推断为空类型，登记与导出双侧整块丢弃。
2. G-05：Mermaid YAML frontmatter（``--- title: ... ---``）导致首关键字变成 ``---``，
   类型判空且校验误报「不支持的行」，带 frontmatter 的图块被整块删除。
3. D-03：HTTP Service 渲染成功路径不回填进程内 LRU，同进程重复调用仍走网络。
4. G-08：图表**下方**的「图 X-Y 题名」未被吸收，随后被「有题无图」清理误删。
"""

from __future__ import annotations

import json

import pytest
from app.services.ai.image_engine import validate_mermaid
from app.services.chart_validators import (
    _strip_yaml_frontmatter,
    detect_mermaid_chart_type,
    first_mermaid_keyword,
    infer_chart_type_from_payload,
    normalize_labor_data,
    strip_leading_mermaid_comments,
)
from app.services.content_blocks import _parse_content_blocks

# ---------------------------------------------------------------------------
# G-04 劳动力载荷别名：推断 / 归一 / 渲染三处同源
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        # 单阶段 trades 形态（normalize_labor_data 的 case ②）
        {"trades": [{"name": "木工", "peak": 30}, {"name": "电工", "peak": 18}]},
        # stages 阶段对象 + workers（case ② 的 stages 别名）
        {"stages": [{"name": "基础阶段", "workers": [{"name": "普工", "count": 12}]}]},
        # 时间轴 phases + data（case ③）
        {"time": ["第1月", "第2月"], "data": [[10, 20], [12, 18]]},
        # labels + values（case ⑥）
        {"labels": ["木工", "钢筋工"], "values": [16, 24]},
        # categories + series（case ⑤ Chart.js 形态）
        {"categories": ["基础", "主体"], "series": [{"name": "人数", "data": [15, 28]}]},
    ],
)
def test_labor_alias_payload_is_inferred_as_labor(payload):
    assert infer_chart_type_from_payload(payload) == "labor"
    # 推断为 labor 的载荷必须真的能被归一器消费，避免「登记成功、渲染空图」
    assert normalize_labor_data(payload) is not None


def test_non_labor_payload_unchanged():
    """对照组：既有类型不受别名扩展影响（防止误吞对比图/甘特图）。"""
    assert infer_chart_type_from_payload({"items": [{"label": "方案A", "value": 80}]}) == "comparison"
    assert (
        infer_chart_type_from_payload({"tasks": [{"name": "土方", "start": "2025-01-01", "end": "2025-01-05"}]})
        == "gantt"
    )
    assert infer_chart_type_from_payload({"zones": [{"name": "作业区"}]}) == "layout"
    # labels 单独出现（无 values/series）不得被判成劳动力图
    assert infer_chart_type_from_payload({"labels": ["甲", "乙"]}) == ""


# ---------------------------------------------------------------------------
# G-05 Mermaid YAML frontmatter
# ---------------------------------------------------------------------------

_FRONTMATTER_FLOW = "---\ntitle: 施工准备流程\n---\n" "flowchart TD\n  A[准备] --> B[开工] --> C[验收]"
_FRONTMATTER_GANTT = (
    "---\ntitle: 进度计划\n---\n"
    "gantt\n  title 总进度\n  dateFormat YYYY-MM-DD\n  section 施工\n"
    "  土方开挖: 2025-01-01, 10d\n  基础施工: 2025-01-11, 15d"
)
_FRONTMATTER_UNCLOSED = "---\ntitle: 残缺\nflowchart TD\n  A --> B"


def test_frontmatter_is_stripped_not_treated_as_keyword():
    assert first_mermaid_keyword(_FRONTMATTER_FLOW) == "flowchart"
    assert first_mermaid_keyword(_FRONTMATTER_GANTT) == "gantt"
    assert detect_mermaid_chart_type(_FRONTMATTER_FLOW, default="") == "flowchart"
    assert detect_mermaid_chart_type(_FRONTMATTER_GANTT, default="") == "gantt"
    # 前导无闭合 frontmatter 必须原样保留（绝不误吞正常内容）
    assert _strip_yaml_frontmatter(_FRONTMATTER_UNCLOSED) == _FRONTMATTER_UNCLOSED
    assert first_mermaid_keyword(_FRONTMATTER_UNCLOSED) == "---"


def test_frontmatter_blocks_pass_validation_and_normalization():
    """修复前：validate_mermaid 报「不支持的行 '---'」→ 图块被删。"""
    for code in (_FRONTMATTER_FLOW, _FRONTMATTER_GANTT):
        ok, _msg = validate_mermaid(code)
        assert ok is True
        normalized = strip_leading_mermaid_comments(code)
        assert normalized.startswith(("flowchart", "gantt")), normalized

    # 注释 + frontmatter 组合也要能剥干净
    mixed = "%% 流程图\n---\ntitle: 准备\n---\nflowchart LR\n  A --> B"
    assert first_mermaid_keyword(mixed) == "flowchart"
    assert strip_leading_mermaid_comments(mixed).startswith("flowchart")


# ---------------------------------------------------------------------------
# D-03 HTTP 成功路径回填 LRU
# ---------------------------------------------------------------------------


def test_http_success_backfills_memory_cache(monkeypatch):
    """同一进程内第二次相同渲染请求必须命中 LRU，不再打 HTTP。"""
    from app.services.ai import mermaid_renderer as mr

    class _FakeClient:
        def __init__(self):
            self.calls = 0

        async def render_mermaid_via_http(self, *, code, theme, scale):
            self.calls += 1
            return b"\x89PNG\r\n\x1a\n" + b"x" * 200

    client = _FakeClient()
    monkeypatch.setattr(mr, "get_mermaid_service_client", lambda timeout=30: client)
    mr.clear_render_cache()

    code = "graph TD\n  A --> B"
    first = mr.render_mermaid_to_bytes(code, "flowchart", skip_http=False, allow_pil=False)
    assert first is not None and len(first.getvalue()) > 100
    assert client.calls == 1

    second = mr.render_mermaid_to_bytes(code, "flowchart", skip_http=False, allow_pil=False)
    assert second is not None and len(second.getvalue()) == len(first.getvalue())
    assert client.calls == 1, "HTTP 成功结果未回填 LRU，重复请求仍走网络"
    mr.clear_render_cache()


# ---------------------------------------------------------------------------
# G-08 图下题注吸收
# ---------------------------------------------------------------------------

_BELOW_CAPTION_FENCE = """施工准备阶段工艺流程如下图所示。

```mermaid
flowchart TD
  A[施工准备] --> B[测量放线] --> C[土方开挖]
```

图 3-1 施工准备阶段工艺流程图
"""


def test_figure_caption_below_chart_is_absorbed():
    blocks = _parse_content_blocks(_BELOW_CAPTION_FENCE)
    charts = [b for b in blocks if b.get("type") == "chart"]
    assert len(charts) == 1
    assert charts[0]["title"] == "施工准备阶段工艺流程图"
    # 题注已升格，正文中不得残留重复段落
    paragraphs = [b for b in blocks if b.get("type") == "paragraph"]
    assert not any("图 3-1" in str(p.get("text")) for p in paragraphs)


@pytest.mark.asyncio
async def test_http_only_chart_failure_exposes_no_pil_fallback_signal(db_conn, monkeypatch):
    """G-06：HTTP-only 类型（mindmap）渲染失败时，响应头必须可区分于语法坏图。

    PIL 本地渲染器只覆盖 flowchart/gantt/architecture/labor/comparison/layout/timeline；
    mindmap/sequence/class 等只能依赖 HTTP Service。旧实现所有失败都只累加
    ``failed``，用户无法判断「图坏了」还是「本机根本不会画」。
    """
    from app.routers.export import export_docx
    from app.services.ai import mermaid_renderer as mr
    from fastapi.responses import FileResponse

    def _always_none(*_args, **_kwargs):
        return None

    monkeypatch.setattr(mr._chart_cache, "get_or_render", _always_none)

    await db_conn.execute(
        "INSERT INTO schemes (id, project_id, name) VALUES (?,?,?)",
        ("s-g6-r179", "p1", "G06 图表降级信号"),
    )
    content = (
        "施工组织思路如下。\n\n"
        "```mermaid\nmindmap\n  root((施工组织))\n    施工准备\n    主体施工\n```\n\n"
        "工艺流程如下。\n\n"
        "```mermaid\nflowchart TD\n  A[准备] --> B[施工]\n```\n"
    )
    await db_conn.execute(
        "INSERT INTO sections (id, scheme_id, parent_id, sort_order, level, title, status, content) "
        "VALUES (?,?,?,?,?,?,?,?)",
        ("c-g6-r179", "s-g6-r179", "", 0, 1, "第一章 施工组织", "generated", content),
    )
    await db_conn.commit()

    resp = await export_docx(
        "s-g6-r179",
        {
            "project_name": "示例项目",
            "bid_section": "第一标段",
            "config": {},
            "chart_images": [],
            "fe_codes": [],
            "override_high_issues": True,
        },
        db=db_conn,
    )
    # 覆写路径必须留响应头，证明本轮是「已知悉并继续」而非闸门失效
    assert resp.headers.get("X-Export-Override") == "high-issues"
    assert isinstance(resp, FileResponse)
    assert resp.path and __import__("os").path.exists(resp.path)

    stats = json.loads(resp.headers.get("X-Chart-Render-Stats") or "{}")
    assert stats.get("failed") == 2, stats
    assert stats.get("no_pil_fallback") == 1, stats
    assert stats.get("no_pil_fallback_types") == ["mindmap"], stats


_BELOW_CAPTION_JSON = """各阶段劳动力投入见下图。

```chart-json
{"type":"labor","title":"劳动力曲线","time":["第1月","第2月"],"data":[[10,20],[12,18]]}
```

图 4-2 各阶段劳动力投入曲线
"""


def test_figure_caption_below_chart_json_is_absorbed():
    blocks = _parse_content_blocks(_BELOW_CAPTION_JSON)
    charts = [b for b in blocks if b.get("type") == "chart"]
    assert len(charts) == 1
    assert charts[0]["title"] == "各阶段劳动力投入曲线"
    assert json.loads(charts[0]["code"])["type"] == "labor"
