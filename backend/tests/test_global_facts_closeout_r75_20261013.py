"""全局事实模块 · 读路径 name/value 唯一出口统一护栏（R75 · 2026-10-13）

本轮收口四项缺口的后端三项（G-D 前端另见 factsDiagnosticsR75.test.ts）：

G-A 【P2】``_facts_for_adjust_prompt`` 的 name/value 是读路径唯一出口
     ``_fact_name_value`` 之外的第二份切分（R54 §8.2 T4 登记遗留）：
     ① 粗体行带 *(注释)* 时 extract 侧把**剥注释后**的值喂给 AI，
     AI 照抄回传即把注释从库里抹掉；② 非粗体行以 content 回解名顶掉
     title 列；③ 值空时两侧回落规则不同源。
G-B 【P3】``resolve_conflict`` 的 prior_value / ``_apply_item_updates``
     的 old_value / ``_lookup_old`` / ``_carry_dimensions`` 同族分叉 ——
     带注释的**未变值**被回解成剥注释值，与新值比较即「已换值」，
     连带 is_simulated 错误清零 / value_changed 误判。
G-C 【P3】``check_danger_scheme`` 对非粗体段落行把**整段正文**当事实名
     喂给 extract_danger_params，与界面显示名称（title 列）分叉 ——
     危大阈值判据与用户所见不同源。

两层守护：
- A 组 · AST 静态契约锁：六个读路径函数体必须经 ``_fact_name_value``，
  直调 ``extract_value_from_markdown_line`` 的函数集合只允许写路径白名单
  （解析用户提交行本就需要底层原语）；反向锁「函数改名/删除不得让护栏空转」。

✅ R75b（2026-10-13 订正锚点）：唯一出口权威实现下沉到
``services/facts_extractor.fact_name_value``（跨模块桥接同层引用，免
routers→services 反向依赖造成第二份回解），routers 侧 ``_fact_name_value``
为薄包装。A 组白名单摘掉 ``_fact_name_value``（包装不再需要直调权，副本
回流即红）；「防出口掏空」反向锁改指 **services 本体** 仍用底层原语回解
非粗体行，并加锁 routers 包装必须委托 services。
- B 组 · 脏语料 parity 行为实证：adjust 提示词可见值与 list_facts 显示值
  （:994-996 组合公式）逐字一致；名称不整段、模拟标记剥离、注释保留。
- C 组 · 口径切换的实质性分叉证明：新旧出口在注释语料上**确实不同值**
  （防止护栏在出口被改回旧行为后仍恒绿）。
"""
from __future__ import annotations

import ast
import io
import os
import sys

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(APP))
sys.path.insert(0, APP)

GF_PATH = os.path.join(APP, "app", "routers", "global_facts.py")
FE_PATH = os.path.join(APP, "app", "services", "facts_extractor.py")

#: 本轮统一出口的六个读路径函数（G-A/G-B/G-C 全部落点）。
#: 其中 _lookup_old / _carry_dimensions 是嵌套闭包，ast.walk 同样能取到。
READ_PATH_FNS = [
    "_facts_for_adjust_prompt",   # G-A adjust 提示词
    "check_danger_scheme",        # G-C 危大阈值
    "_apply_item_updates",        # G-B 条目更新 old_value
    "resolve_conflict",           # G-B 人工裁决 prior_value
    "_lookup_old",                # G-B 同族回解
    "_carry_dimensions",          # G-B 分组重建回解
]

#: 允许直调底层原语 extract_value_from_markdown_line 的函数白名单 ——
#: 全部是**写路径**（解析用户/AI 提交的 Markdown 行），与读路径回解库行
#: 是不同职责：
#:   · _split_fact_lines：手工分组多行输入拆行；
#:   · update_fact：PATCH 的 content 字段是用户提交行，需底层解析。
#: ✅ R75b：``_fact_name_value`` 已从白名单摘除 —— 权威定义下沉 services 后
#: 它是纯委托薄包装，不再需要直调权；若包装里回流自持切分即红。
ALLOWED_DIRECT_EXTRACT_CALLERS = {
    "_split_fact_lines",
    "update_fact",
}


# ---------------------------------------------------------------- 测试基建 --
def _module_tree() -> ast.Module:
    with io.open(GF_PATH, encoding="utf-8") as f:
        return ast.parse(f.read())


def _extractor_tree() -> ast.Module:
    with io.open(FE_PATH, encoding="utf-8") as f:
        return ast.parse(f.read())


def _called_names(node: ast.AST) -> set:
    """函数节点（含其内部嵌套闭包）出现的所有调用名（id 或 attr）。"""
    out = set()
    for c in ast.walk(node):
        if isinstance(c, ast.Call):
            fn = c.func
            nm = getattr(fn, "id", None) or getattr(fn, "attr", None)
            if nm:
                out.add(nm)
    return out


def _defs_by_name(tree: ast.Module) -> dict:
    out: dict[str, list] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.setdefault(node.name, []).append(node)
    return out


def _gf():
    from app.routers import global_facts as gf
    return gf


#: 脏语料：四类「显示侧与旧回解侧会分叉」的真实落库形态
DIRTY_ROWS = [
    {   # 1) 粗体行带 *(注释)*：旧 extract 侧剥注释，显示侧保留
        "id": "d1", "title": "旧名称-开挖深度", "category": "tech_param",
        "content": "- **基坑开挖深度**: 12.5m *(根据图纸)*",
    },
    {   # 2) 非粗体整段正文：旧危大侧把整段当名称，显示侧名称取 title 列
        "id": "d2", "title": "基坑概况说明", "category": "overview",
        "content": "本场区基坑深度约12.5米，采用桩锚支护体系，地下水位约-2.0米。",
    },
    {   # 3) 粗体行带模拟值标记：标记必须先于粗体被剥离，不残留在值里
        "id": "d3", "title": "工期", "category": "schedule",
        "content": "- **合同工期**: 90天 ⚠️*(模拟值)*",
    },
    {   # 4) content 为空：值回落 content（空串），名称回落 title，不得异常
        "id": "d4", "title": "项目经理", "category": "personnel",
        "content": "",
    },
    {   # 5) content 为 None（历史行/NULL）：同上，全程 None 安全
        "id": "d5", "title": "总工", "category": "personnel",
        "content": None,
    },
]


def _display_pair(row: dict) -> tuple:
    """复刻 list_facts(:994-996) 的显示组合公式 —— parity 的对照侧。"""
    n, v = _gf()._fact_name_value(row)
    return (n or (row.get("title") or ""), v or (row.get("content") or ""))


class TestReadPathSingleSourceASTLock:
    """A 组 · AST 静态契约锁（防「同一判据两份实现」回流）。"""

    def setup_method(self):
        self.tree = _module_tree()
        self.defs = _defs_by_name(self.tree)

    def test_all_read_path_functions_still_exist(self):
        """前提锁：六个落点任一被改名/删除，护栏必须显式失败而非空转恒绿。"""
        for fn in READ_PATH_FNS:
            assert fn in self.defs, f"{fn} 不在 global_facts.py（护栏锚点漂移）"

    def test_read_path_functions_route_through_single_exit(self):
        """六个读路径函数体必须调用 _fact_name_value，不得直调底层原语。"""
        for fn in READ_PATH_FNS:
            for node in self.defs[fn]:
                calls = _called_names(node)
                assert "_fact_name_value" in calls, (
                    f"{fn} 未走读路径唯一出口 _fact_name_value")
                assert "extract_value_from_markdown_line" not in calls, (
                    f"{fn} 直调 extract_value_from_markdown_line —— "
                    f"name/value 第二份切分回流（R75 G-A/G-B/G-C 同族缺陷复活）")

    def test_direct_extract_callers_are_exactly_write_side_allowlist(self):
        """全模块直调底层原语的函数集合 ⊆ 写路径白名单。"""
        offenders = {
            name for name, nodes in self.defs.items()
            for node in nodes
            if "extract_value_from_markdown_line" in _called_names(node)
        }
        assert offenders <= ALLOWED_DIRECT_EXTRACT_CALLERS, (
            f"出现白名单外的直调点：{sorted(offenders - ALLOWED_DIRECT_EXTRACT_CALLERS)}")

    def test_single_exit_authority_is_delegate_plus_services_fallback(self):
        """✅ R75b 反向锁改形：防「唯一出口被掏空」的锁改指 services 本体。

        旧锁要求 routers._fact_name_value 函数体直调底层原语兜底非粗体行；
        权威定义下沉后该锁必红（锚点漂移，AGENTS 反复记账的同族缺陷）。
        现锁两件事：① routers 包装**纯委托** services.fact_name_value
        （不得再直调底层原语 —— 副本回流即红）；② services 本体继续用
        底层原语回解非粗体行（防出口被掏空成粗体行专判）。
        """
        fv = self.defs.get("_fact_name_value")
        assert fv, "routers._fact_name_value 不在 global_facts.py（护栏锚点漂移）"
        wrapper_calls = _called_names(fv[0])
        assert "fact_name_value" in wrapper_calls, (
            "routers._fact_name_value 不再委托 services 权威本体 —— 包装被掏空/改写")
        assert "extract_value_from_markdown_line" not in wrapper_calls, (
            "routers 薄包装内回流自持切分 —— 同一判据两份实现复活（R75b 下沉成果丢失）")
        auth = _defs_by_name(_extractor_tree()).get("fact_name_value")
        assert auth, (
            "services.fact_name_value 不在 facts_extractor.py —— 权威本体被改名/删除")
        assert any(
            "extract_value_from_markdown_line" in _called_names(n) for n in auth), (
            "services.fact_name_value 不再用底层原语回解非粗体行 —— 唯一出口被掏空")


class TestAdjustPromptDisplayParity:
    """B 组 · adjust 提示词可见值与界面显示值逐字一致（G-A 行为实证）。"""

    def test_adjust_rows_match_display_composition(self):
        gf = _gf()
        out = gf._facts_for_adjust_prompt([dict(r) for r in DIRTY_ROWS])
        assert [o["fact_id"] for o in out] == [r["id"] for r in DIRTY_ROWS]
        for o, row in zip(out, DIRTY_ROWS):
            name, value = _display_pair(row)
            assert o["name"] == name, (
                f"{row['id']}: AI 看到的名称与界面显示分叉（{o['name']!r} vs {name!r}）")
            assert o["value"] == value, (
                f"{row['id']}: AI 看到的取值与界面显示分叉（{o['value']!r} vs {value!r}）")

    def test_danger_name_is_not_whole_paragraph(self):
        """G-C 行为等价：非粗体段落的「名称」取 title 列，不是整段正文。"""
        gf = _gf()
        row = dict(DIRTY_ROWS[1])
        n, v = gf._fact_name_value(row)
        name = n or row.get("title") or ""
        assert "\n" not in name
        assert name == "基坑概况说明", "名称被 content 回解顶掉 title 列（整段当名称）"

    def test_simulated_marker_stripped_and_annotation_preserved(self):
        """模拟标记必须剥净；*(注释)* 必须保留（显示与喂 AI 同值，照抄不回滚注释）。"""
        gf = _gf()
        n, v = gf._fact_name_value(dict(DIRTY_ROWS[2]))
        assert (n, v) == ("合同工期", "90天"), f"模拟标记未剥净：{v!r}"
        n2, v2 = gf._fact_name_value(dict(DIRTY_ROWS[0]))
        assert n2 == "基坑开挖深度"
        assert "*(根据图纸)*" in v2, f"注释被剥掉（AI 照抄即抹注释）：{v2!r}"

    def test_empty_and_none_content_are_safe(self):
        gf = _gf()
        for row in (DIRTY_ROWS[3], DIRTY_ROWS[4]):
            out = gf._facts_for_adjust_prompt([dict(row)])
            assert out[0]["name"] == row["title"]
            assert out[0]["value"] == (row["content"] or "")


class TestCaliberSwitchIsMaterial:
    """C 组 · 新旧出口在注释语料上确实不同值 —— 证明口径切换实质性生效。

    若未来有人把 _facts_for_adjust_prompt 改回直调底层原语，A 组静态锁会红；
    本组则锁住「两者的差异真实存在」，防止 A 组护栏因底层原语行为被改
    （比如 extract 不再剥注释）而失去意义。
    """

    def test_bare_extract_differs_from_single_exit_on_annotation(self):
        from app.services.facts_extractor import extract_value_from_markdown_line
        gf = _gf()
        content = DIRTY_ROWS[0]["content"]
        _, old_value = extract_value_from_markdown_line(content)
        _, new_value = gf._fact_name_value(dict(DIRTY_ROWS[0]))
        assert "*(根据图纸)*" not in old_value, (
            "底层原语不再剥注释？本对照前提失效，请同步修订 C 组与 A 组白名单")
        assert "*(根据图纸)*" in new_value

    def test_single_exit_falls_back_to_title_not_content_name(self):
        from app.services.facts_extractor import extract_value_from_markdown_line
        gf = _gf()
        content = DIRTY_ROWS[1]["content"]
        old_name, _ = extract_value_from_markdown_line(content)
        new_name, _ = gf._fact_name_value(dict(DIRTY_ROWS[1]))
        assert old_name != new_name, (
            "段落行的名称分叉不复存在？前提失效请修订护栏")
        assert new_name == DIRTY_ROWS[1]["title"]
