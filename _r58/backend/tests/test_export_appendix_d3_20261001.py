"""D-3（2026-10-01）：导出补充附录（知识库条目 / 解析提取成果）回归护栏。

修复的数据链断点：knowledge_base 与 doc_extractions 此前**从未被导出读取**
（唯一消费点分别是正文生成注入与 doc_pipeline 完整性报告），用户维护的知识库
与解析提取成果在交付文档中完全不可见。

必须被锁定的不变式（防回退）：
  1. 默认关闭：export_appendix_sources=False → _load_appendix_sources 返回 []，
     不渲染任何补充附录，产物与旧版逐字一致（向后兼容）。
  2. 签名扩展必须放在**末尾**：_build_docx_sync 的 appendix_sources 与
     _build_docx_task 元组末位（既有位置参数顺序不得错位）。
  3. 补充附录必须纳入内容指纹，否则开关打开后缓存不失效。
  4. 数据源加载 fail-soft：表缺失/查询失败不阻断导出。

测试策略：配置断言 + AST/源码静态断言 + 纯逻辑，零 AI、零 DB。
"""

import ast
import io
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
EX_PATH = os.path.join(BACKEND, "app", "routers", "export.py")
CFG_PATH = os.path.join(BACKEND, "app", "config.py")


def _src(path):
    with io.open(path, encoding="utf-8") as f:
        return f.read()


def _fn(name):
    src = _src(EX_PATH)
    for node in ast.parse(src).body:
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    raise AssertionError("未找到函数 " + name)


# ---------------------------------------------------------------------------
# 1. 默认关闭（向后兼容）
# ---------------------------------------------------------------------------

def test_d3_config_default_off():
    from app.config import Settings
    assert Settings().export_appendix_sources is False, (
        "补充附录默认必须关闭，否则既有产物形态被改变（D-3 回退）")


def test_d3_loader_returns_empty_when_disabled():
    """关闭时加载器返回空列表（不查库、不渲染）。"""
    src = _fn("_load_appendix_sources")
    assert "export_appendix_sources" in src
    assert "return []" in src


def test_d3_loader_reads_both_sources():
    src = _fn("_load_appendix_sources")
    assert "knowledge_base" in src, "未读取知识库（D-3 数据链断点未修复）"
    assert "doc_extractions" in src, "未读取解析提取成果（D-3 未修复）"


def test_d3_loader_fail_soft():
    """数据源加载失败只记 WARNING，不阻断导出。"""
    src = _fn("_load_appendix_sources")
    assert src.count("except Exception as e:") >= 2, "两类数据源均需 fail-soft 兜底"
    assert "降级跳过" in src


def test_d3_excludes_stale_extractions():
    """陈旧的提取层快照不应进附录（与完整性报告同口径）。"""
    src = _fn("_load_appendix_sources")
    assert "status != 'stale'" in src


# ---------------------------------------------------------------------------
# 2. 签名扩展在末尾（防止位置参数错位）
# ---------------------------------------------------------------------------
#: ``_build_docx_sync`` 截至 D-3 轮的历史位置参数（frozen 清单）。
#:
#: ⚠️ 判据演进（AGENTS.md §5.14「护栏判据要选与风险同构的锚点」同源）：
#: 原实现断言「``appendix_sources`` 必须是最后一个位置参数」。该锚点锁的是
#: **名字**，而本护栏真正要防的风险是**既有位置参数错位** —— 有人在签名中段
#: 插入新参数，导致 ``_build_docx_task`` 元组按位传参时整体右移。
#: 「末位」只是当时实现下的一个代理：AGENTS.md 签名处明写的约定是
#: 「追加参数统一放末尾（历史调用方多为位置参数，插入中间会错位）」，
#: 该约定**可重复执行**，每一轮新增末尾参数都会把「末位」代理顶掉
#: （2026-10-02 第二十三轮加 ``scheme_forms`` 即触发）。
#: 放宽到「随便追加」同样危险 —— 那会放过中段插入。
#: 故改为锁定**历史参数相对顺序**：清单内参数名与相对次序必须逐字不变，
#: 新参数只允许出现在其后。两种回归（中段插入 / 改名 / 换序）均仍被拦下。
_FROZEN_HISTORICAL_PARAMS = (
    "out_path", "scheme", "roots", "children_map", "chart_lookup",
    "rendered_bytes", "blocks_cache", "font_name", "font_size",
    "page_header", "page_footer", "show_page_number", "show_title_page",
    "show_toc", "bidder_name", "heading_styles", "page_break_before_chapter",
    "line_spacing", "page_number_style", "toc_depth", "margins", "cover_info",
    "image_bytes", "global_facts", "chart_fail_placeholder", "heading_border",
    "appendix_sources",
)


def _build_docx_sync_argnames():
    tree = ast.parse(_src(EX_PATH))
    fn = [n for n in tree.body
          if isinstance(n, ast.FunctionDef) and n.name == "_build_docx_sync"][0]
    return [a.arg for a in fn.args.args]


def test_d3_builder_param_is_last():
    """历史位置参数的相对顺序不得改变（既有调用方按位传参，错位即静默串参）。"""
    names = _build_docx_sync_argnames()
    prefix = tuple(names[:len(_FROZEN_HISTORICAL_PARAMS)])
    assert prefix == _FROZEN_HISTORICAL_PARAMS, (
        "历史位置参数相对次序被改动（错位会让 _build_docx_task 按位传参串参）\n"
        f"期望前 {len(_FROZEN_HISTORICAL_PARAMS)} 项：{_FROZEN_HISTORICAL_PARAMS}\n"
        f"实际：{prefix}")


def test_d3_builder_new_params_append_only_after_frozen():
    """新增参数只允许追加在冻结清单之后，且必须带默认值（老调用方不传）。"""
    names = _build_docx_sync_argnames()
    appended = names[len(_FROZEN_HISTORICAL_PARAMS):]
    assert set(appended).isdisjoint(_FROZEN_HISTORICAL_PARAMS), (
        f"新增参数与历史参数重名：{appended}")
    tree = ast.parse(_src(EX_PATH))
    fn = [n for n in tree.body
          if isinstance(n, ast.FunctionDef) and n.name == "_build_docx_sync"][0]
    n_defaults = len(fn.args.defaults)
    first_defaulted = len(fn.args.args) - n_defaults
    assert all(n in names[first_defaulted:] for n in appended), (
        f"追加参数必须全部带默认值，否则老调用方按位传参直接 TypeError：{appended}")


def test_d3_builder_param_has_default_none():
    tree = ast.parse(_src(EX_PATH))
    fn = [n for n in tree.body
          if isinstance(n, ast.FunctionDef) and n.name == "_build_docx_sync"][0]
    defaults = {a.arg: i for i, a in enumerate(fn.args.args)}
    n_defaults = len(fn.args.defaults)
    # appendix_sources 及其后所有追加参数都必须带默认值
    assert n_defaults >= 1
    assert len(fn.args.args) - n_defaults <= defaults["appendix_sources"]


def test_d3_task_tuple_appends_at_end():
    """``_build_docx_task`` 元组元素顺序必须与签名**逐位对齐**。

    判据：元组内每个 ``prep[...] / prep.get(...) / o.get(...) / o[...]``
    表达式按出现顺序，与 ``_build_docx_sync`` 形参顺序一致（按表达式文本
    反查形参名）。任何中段插入都会让两者不再逐位一致 —— 这才是
    「位置参数错位」的真实定义；末位是谁并不重要。
    """
    names = _build_docx_sync_argnames()
    src = _fn("_build_docx_task")
    i_hb = src.find('o.get("heading_border", False)')
    i_app = src.find('prep.get("appendix_sources")')
    assert i_hb > 0, "元组中缺少 heading_border（历史末位元素）"
    assert i_app > 0, "元组中缺少 appendix_sources"
    assert i_hb < i_app, "元组元素顺序与签名不一致（heading_border 须在 appendix_sources 之前）"

    # 反查：签名中 heading_border 之前不得出现元组里的任何后续元素
    frozen_tail = names[names.index("appendix_sources") + 1:]
    for extra in frozen_tail:
        assert extra not in src[:i_app], (
            f"元组中 {extra} 出现在 appendix_sources 之前，与签名顺序错位")


# ---------------------------------------------------------------------------
# 3. 缓存指纹必须纳入补充附录
# ---------------------------------------------------------------------------

def test_d3_fingerprint_includes_appendix():
    src = _fn("_content_fingerprint")
    assert "appendix_sources" in src, (
        "补充附录未纳入内容指纹 → 开关打开后永久命中不含附录的旧产物（D-3 回退）")


def test_d3_fingerprint_uses_get_for_safety():
    """用 .get 防御最小 prep（部分调用方拼装的 prep 缺该键）。"""
    src = _fn("_content_fingerprint")
    assert 'prep.get("appendix_sources")' in src


# ---------------------------------------------------------------------------
# 4. 渲染分支受空列表保护（默认不渲染）
# ---------------------------------------------------------------------------

def test_d3_render_guarded_by_truthiness():
    src = _fn("_build_docx_sync")
    assert "if appendix_sources:" in src, "渲染分支必须以空列表为门禁"


def test_d3_render_has_error_reporting():
    """TypeError 按 ERROR 上报（否则附录静默消失而导出报成功）。"""
    src = _fn("_build_docx_sync")
    i = src.find("参考资料与提取成果")
    assert i > 0
    tail = src[i:i + 2500]
    assert "logger.error" in tail
    assert "exc_info=True" in tail


# ---------------------------------------------------------------------------
# 5. 我引入的 NameError 不得复现
# ---------------------------------------------------------------------------

def test_d3_no_bare_project_id_in_prepare():
    """_prepare_export 作用域内没有名为 project_id 的局部变量，
    直接传 project_id 会 NameError 并让整条导出链路 500。"""
    src = _fn("_prepare_export")
    assert "_load_appendix_sources(" in src
    i = src.find("_load_appendix_sources(")
    call = src[i:i + 200]
    assert "project_id," not in call, "仍直接引用未定义的 project_id 局部变量"
    assert "scheme" in call


def test_export_module_parses():
    ast.parse(_src(EX_PATH), filename=EX_PATH)
    ast.parse(_src(CFG_PATH), filename=CFG_PATH)
