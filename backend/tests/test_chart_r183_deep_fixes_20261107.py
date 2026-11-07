"""图表生成与渲染模块 · R183 深度修复回归（2026-11-07）

覆盖 5 处「校验器/解析器与渲染器口径不一致 → 合法图表被删块」及图题/缓存缺陷：

D1 数字节点 ID（`1[施工准备]`）是 Prompt 明确允许的写法，旧校验正则要求首字母，
   导致可正常出图的流程图被判「节点数不足（0<3）」整块删除。
D2 混合写法 `A[开始] --> B --> C`（带标签 + 裸节点）在三节点以上仍合法，
   旧校验只数 `[`/`{`，节点数算成 1 → 删除。
D3 虚线/粗线/无箭头连线（`-.->` / `==>` / `---`）是 Mermaid 合法边，
   旧校验以 `"-->"` 子串判连接关系 → 删除（渲染器可出图）。
D4 引导语「施工流程图如下：」不含旧窄正则命中词序 → 图题退化为类型通用名，
   违反图名提取优先级（载荷 title > Mermaid title > 引导语 > 通用名）。
D5 载荷 title 自带「图 3-1」前缀时，导出图题拼成「图 3-1 图 3-1 …」重复编号。
D6 ChartCache.get_or_render 命中缓存时不刷新 LRU 时间戳，热点图会被误淘汰。
"""

import json

import pytest


def _validate(chart_type, code):
    from app.routers._chart_pipeline import _validate_inline_chart

    return _validate_inline_chart(chart_type, code)


def _kept(code):
    from app.routers._chart_pipeline import build_inline_chart_plan

    content = "施工流程图如下：\n\n```mermaid\n" + code + "\n```\n"
    new_content, rows = build_inline_chart_plan("sch", "sec", content)
    return ("```mermaid" in new_content), rows


def _renders(code):
    from app.services.ai.mermaid_renderer import render_mermaid_to_bytes

    buf = render_mermaid_to_bytes(code, "flowchart", skip_http=True, allow_pil=True)
    return buf is not None and len(buf.getvalue()) > 1000


# ---------------------------------------------------------------- D1 数字节点 ID


@pytest.mark.parametrize(
    "code",
    [
        "flowchart TD\n1[施工准备] --> 2[基础施工]\n2 --> 3[主体结构]\n3 --> 4[竣工验收]",
        "flowchart TD\n1[施工准备] --> 2[基础施工]\n2 --> 3[主体结构]",
    ],
)
def test_numeric_node_ids_kept_and_renderable(code):
    ok, _ = _validate("flowchart", code)
    assert ok is True
    assert _renders(code) is True
    kept, rows = _kept(code)
    assert kept is True
    assert len(rows) == 1


# ------------------------------------------------- D2 混合「标签节点 + 裸节点」链


@pytest.mark.parametrize(
    "code",
    [
        "graph TD\nA[开始] --> B --> C --> D",
        "flowchart TD\nA[施工准备] --> B --> C[竣工验收]",
    ],
)
def test_mixed_label_and_bare_nodes_kept(code):
    ok, _ = _validate("flowchart", code)
    assert ok is True
    assert _renders(code) is True
    kept, rows = _kept(code)
    assert kept is True
    assert len(rows) == 1


# --------------------------------------------- D3 非 "-->" 连线（虚线/粗线/无箭头）


DOTTED_EDGE_CODE = (
    "flowchart LR\n"
    '    A["入场"] -.-> B["测量"]\n'
    '    B -. 复测 .-> C["放线"]\n'
    '    C == 是 ==> D["复核"]\n'
    '    D --- E["验收"]\n'
)


def test_dotted_thick_plain_edges_kept_and_renderable():
    ok, _ = _validate("flowchart", DOTTED_EDGE_CODE)
    assert ok is True
    assert _renders(DOTTED_EDGE_CODE) is True
    kept, rows = _kept(DOTTED_EDGE_CODE)
    assert kept is True
    assert len(rows) == 1


# --------------------------------------------------------- D4 引导语抽取图题（宽判据）


@pytest.mark.parametrize(
    "lead_in,expected",
    [
        ("施工流程图如下：", "施工流程图"),
        # 宽判据里 `如下` 后不带「图/表」的变体（E2E 实测曾退化为通用名）
        ("主体施工工艺流程如下：", "主体施工工艺流程"),
    ],
)
def test_broad_lead_in_variant_yields_figure_title(lead_in, expected):
    from app.services.content_blocks import _parse_content_blocks

    content = (
        lead_in + "\n\n"
        "```mermaid\nflowchart TD\n    A[施工准备] --> B[基础施工]\n    B --> C[主体结构]\n"
        "    C --> D[竣工验收]\n```\n"
    )
    blocks = _parse_content_blocks(content)
    charts = [b for b in blocks if b.get("type") == "chart"]
    assert len(charts) == 1
    assert charts[0]["title"] == expected


# ------------------------------------------- D5 载荷 title 自带图号 → 去重防叠写


def test_payload_title_leading_figure_number_stripped():
    from app.services.content_blocks import _parse_content_blocks

    payload = {
        "type": "labor",
        "title": "图 3-1 主体阶段劳动力配置",
        "phases": ["基础"],
        "categories": ["木工"],
        "data": [[20]],
    }
    content = "```chart-json\n" + json.dumps(payload, ensure_ascii=False) + "\n```\n"
    blocks = _parse_content_blocks(content)
    charts = [b for b in blocks if b.get("type") == "chart"]
    assert len(charts) == 1
    assert charts[0]["title"] == "主体阶段劳动力配置"


def test_mermaid_title_directive_leading_figure_number_stripped():
    from app.services.content_blocks import _parse_content_blocks

    content = (
        "施工进度安排如下图所示：\n\n"
        "```mermaid\ngantt\n    title 图 2-1 施工进度计划\n    dateFormat YYYY-MM-DD\n"
        "    section 准备\n    施工准备 :a1, 2026-01-01, 10d\n"
        "    section 主体\n    基础施工 :a2, after a1, 20d\n"
        "    milestone 竣工 :m1, after a2, 0d\n```\n"
    )
    blocks = _parse_content_blocks(content)
    charts = [b for b in blocks if b.get("type") == "chart"]
    assert len(charts) == 1
    assert charts[0]["title"] == "施工进度计划"


# ------------------------------------------------ D6 缓存命中刷新 LRU 访问时间戳


def test_cache_hit_refreshes_lru_access_order(tmp_path, monkeypatch):
    from io import BytesIO

    from app.services.ai import mermaid_renderer as MR

    cache = MR.ChartCache(cache_dir=str(tmp_path))

    def _fake_render(*_a, **_k):
        return BytesIO(b"x" * 2048)

    monkeypatch.setattr(MR, "render_mermaid_to_bytes", _fake_render)
    first = cache.get_or_render("flowchart TD\nA --> B", "flowchart")
    assert first is not None
    # 取实际写入的缓存 key（get_or_render 默认 allow_pil=True / skip_http=False）
    keys = [k for k in cache._access_order if not k.endswith(".fmt")]
    assert len(keys) == 1
    key = keys[0]
    # 模拟「久未访问」：把时间戳拨回 10 天后，再命中一次
    cache._access_order[key] = 1.0
    second = cache.get_or_render("flowchart TD\nA --> B", "flowchart")
    assert second is not None
    refreshed = cache._access_order.get(key)
    assert refreshed != 1.0, "缓存命中必须刷新 LRU 访问时间戳，否则热点图会被误淘汰"


# ------------------------------------- E2E：正文 → 校验收敛 → 图号连续 → DOCX 成稿


def _png_bytes():
    from io import BytesIO

    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (60, 40), (30, 120, 200)).save(buf, "PNG")
    return buf.getvalue()


E2E_CONTENT = (
    "## 施工部署\n"
    # ① 非法：孤立节点（无连线）→ 登记阶段删除、不占图号
    "施工组织关系如下图所示：\n"
    "```mermaid\ngraph TD\n    X[孤立节点]\n    Y[另一节点]\n```\n"
    # ② 合法：数字节点 ID + 宽引导语 → 图题取引导语
    "主体施工工艺流程如下：\n"
    "```mermaid\nflowchart TD\n"
    "    1[施工准备] --> 2[基础施工]\n    2 --> 3[主体结构]\n    3 --> 4[竣工验收]\n```\n"
    # ③ 合法：甘特图，title 指令自带图号 → 图题应剥离前缀
    "总进度计划如下图所示：\n"
    "```mermaid\ngantt\n    title 图 2-1 施工进度计划\n    dateFormat YYYY-MM-DD\n"
    "    section 准备\n    施工准备 :a1, 2026-01-01, 10d\n"
    "    section 主体\n    基础施工 :a2, after a1, 20d\n"
    "    竣工 :milestone, m1, after a2, 0d\n```\n"
    # ④ 合法：劳动力 chart-json，载荷 title 自带图号 → 图题应剥离前缀
    '```chart-json\n{"type": "labor", "title": "图 3-1 劳动力配置", "phases": ["基础"], '
    '"categories": ["木工"], "data": [[20]]}\n```\n'
)


def test_end_to_end_mixed_charts_figure_numbers_continuous(tmp_path):
    from app.routers._chart_pipeline import build_inline_chart_plan
    from app.routers.export import _DEFAULT_HEADING_STYLES, _build_docx_sync

    content, rows = build_inline_chart_plan("sch-e2e", "s1", E2E_CONTENT)
    # ① 非法图被删除；②③④ 三张合法图入库
    assert len(rows) == 3, rows
    assert "孤立节点" not in content, "非法图应在登记阶段删块"

    from app.routers.export import _parse_content_blocks

    blocks = [b for b in _parse_content_blocks(content) if b.get("type") == "chart"]
    rendered = {(b["chart_type"], b["code"]): __import__("io").BytesIO(_png_bytes()) for b in blocks}

    secs = [{"id": "s1", "parent_id": "", "level": 1, "sort_order": 0, "title": "第一章 施工部署", "content": content}]
    out = str(tmp_path / "e2e.docx")
    _build_docx_sync(
        out_path=out,
        scheme={"name": "t", "project_id": "P1"},
        roots=secs,
        children_map={"": secs},
        chart_lookup={},
        rendered_bytes=rendered,
        blocks_cache={},
        font_name="宋体",
        font_size=12,
        page_header="",
        page_footer="",
        show_page_number=False,
        show_title_page=False,
        show_toc=False,
        bidder_name="",
        heading_styles=_DEFAULT_HEADING_STYLES,
        page_break_before_chapter=True,
        line_spacing=1.15,
        page_number_style="simple",
        toc_depth=3,
        margins=None,
        cover_info=None,
        image_bytes={},
        global_facts=[],
        chart_fail_placeholder=False,
    )
    from docx import Document

    caps = [p.text.strip() for p in Document(out).paragraphs if p.text.strip().startswith("图 ")]
    assert caps == [
        "图 1-1 主体施工工艺流程",
        "图 1-2 施工进度计划",
        "图 1-3 劳动力配置",
    ], caps
    full = "\n".join(p.text for p in Document(out).paragraphs)
    assert "图 1-4" not in full, "非法图不得虚占图号"
    assert "图 2-1" not in full and "图 3-1" not in full, "载荷自带的旧图号必须被剥离"
