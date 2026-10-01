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

def test_d3_builder_param_is_last():
    src = _src(EX_PATH)
    tree = ast.parse(src)
    fn = None
    for node in ast.parse(src).body:
        if isinstance(node, ast.FunctionDef) and node.name == "_build_docx_sync":
            fn = node
    assert fn is not None
    names = [a.arg for a in fn.args.args]
    assert names[-1] == "appendix_sources", (
        f"appendix_sources 必须是最后一个位置参数，当前末位是 {names[-1]}")


def test_d3_builder_param_has_default_none():
    tree = ast.parse(_src(EX_PATH))
    fn = [n for n in tree.body
          if isinstance(n, ast.FunctionDef) and n.name == "_build_docx_sync"][0]
    defaults = {a.arg: i for i, a in enumerate(fn.args.args)}
    n_defaults = len(fn.args.defaults)
    # appendix_sources 是末位参数，必须带默认值
    assert n_defaults >= 1
    assert len(fn.args.args) - n_defaults <= defaults["appendix_sources"]


def test_d3_task_tuple_appends_at_end():
    """appendix_sources 必须是元组**最后一个元素**（既有位置参数顺序不得错位）。

    判据：该行之后只剩元组的右括号与函数收尾（允许尾逗号），
    且它排在 heading_border 之后。
    """
    src = _fn("_build_docx_task")
    i_app = src.find('prep.get("appendix_sources")')
    i_hb = src.find('o.get("heading_border", False)')
    assert i_app > 0, "元组中缺少 appendix_sources"
    assert i_hb > 0, "元组中缺少 heading_border（原有末位元素）"
    assert i_hb < i_app, "appendix_sources 必须排在 heading_border 之后"
    # 该元素之后不得再出现任何其它元组元素（prep[...] / o.get(...) / o[...]）
    tail = src[i_app + len('prep.get("appendix_sources")'):]
    for marker in ("prep[", "o.get(", 'prep.get("'):
        assert marker not in tail, (
            f"appendix_sources 之后仍有元组元素（{marker}），会导致位置参数错位")


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
