"""_restore_descriptions 标题回退匹配改进测试（2026-10-09 P1-8 收口）。"""
import copy


def _make_outline():
    return [
        {"title": "工程概况", "description": "本章详细描述工程基本情况建设地点结构类型建筑面积等关键参数（完整描述远超60字截断）",
         "children": [
             {"title": "工程基本情况", "description": "包括项目名称建设单位设计单位等基本信息", "children": []},
             {"title": "周边环境", "description": "描述基坑周边建筑物地下管线道路等情况", "children": []},
         ]},
        {"title": "编制依据", "description": "列出本方案编制所依据的法律法规技术标准施工合同等",
         "children": [{"title": "法律法规", "description": "住建部令第37号建办质2018年31号等", "children": []}]},
        {"title": "施工工艺技术", "description": "详细描述主要施工工艺流程技术参数质量控制措施", "children": []},
    ]


def _truncate(nodes, n=60):
    for x in nodes:
        if isinstance(x, dict):
            d = str(x.get("description") or "")
            if len(d) > n:
                x["description"] = d[:n]
            c = x.get("children")
            if isinstance(c, list):
                _truncate(c, n)


def test_exact_match():
    from app.routers.sse_handlers import _restore_descriptions
    o = _make_outline()
    t = copy.deepcopy(o)
    _truncate(t)
    _restore_descriptions(o, t)
    for i, (orig, tgt) in enumerate(zip(o, t)):
        assert tgt["description"] == orig["description"]


def test_reordered():
    from app.routers.sse_handlers import _restore_descriptions
    o = _make_outline()
    t = [copy.deepcopy(o[2]), copy.deepcopy(o[0]), copy.deepcopy(o[1])]
    _truncate(t)
    _restore_descriptions(o, t)
    assert t[0]["description"] == o[2]["description"]
    assert t[1]["description"] == o[0]["description"]


def test_added_chapter():
    from app.routers.sse_handlers import _restore_descriptions
    o = _make_outline()
    nc = {"title": "安全", "description": "新增", "children": []}
    t = [nc] + [copy.deepcopy(n) for n in o]
    _truncate(t)
    _restore_descriptions(o, t)
    assert t[0]["description"] == "新增"
    assert t[1]["description"] == o[0]["description"]


def test_removed():
    from app.routers.sse_handlers import _restore_descriptions
    o = _make_outline()
    t = [copy.deepcopy(o[0]), copy.deepcopy(o[2])]
    _truncate(t)
    _restore_descriptions(o, t)
    assert t[0]["description"] == o[0]["description"]
    assert t[1]["description"] == o[2]["description"]


def test_ambiguous():
    from app.routers.sse_handlers import _restore_descriptions
    o = [
        {"title": "A", "description": "第一章A描述（完整版）", "children": []},
        {"title": "B", "description": "B描述", "children": []},
        {"title": "A", "description": "第三章A描述（不同内容）", "children": []},
    ]
    t = [copy.deepcopy(o[0]), copy.deepcopy(o[2]), copy.deepcopy(o[1])]
    _truncate(t)
    _restore_descriptions(o, t)
    assert t[0]["description"] == o[0]["description"]
    assert t[1]["description"] == o[2]["description"]


def test_longer_preserved():
    from app.routers.sse_handlers import _restore_descriptions
    o = [{"title": "X", "description": "短", "children": []}]
    t = [{"title": "X", "description": "这是一段非常长的模型新写的描述应该予以保留", "children": []}]
    _restore_descriptions(o, t)
    assert "模型新写" in t[0]["description"]


def test_no_match():
    from app.routers.sse_handlers import _restore_descriptions
    o = [{"title": "A", "description": "Adesc", "children": []}]
    t = [{"title": "B", "description": "Btrunc", "children": []}]
    _restore_descriptions(o, t)
    assert t[0]["description"] == "Btrunc"


def test_non_list():
    from app.routers.sse_handlers import _restore_descriptions
    _restore_descriptions(None, [])
    _restore_descriptions([], None)


def test_nested_fallback():
    from app.routers.sse_handlers import _restore_descriptions
    o = _make_outline()
    t = copy.deepcopy(o)
    t[0]["children"] = [copy.deepcopy(o[0]["children"][0])]
    t[1]["children"] = [copy.deepcopy(o[0]["children"][1])]
    _truncate(t)
    _restore_descriptions(o, t)
    found = False
    for ch in t[1].get("children", []):
        if ch.get("title") == "周边环境":
            assert ch["description"] == o[0]["children"][1]["description"]
            found = True
    assert found
