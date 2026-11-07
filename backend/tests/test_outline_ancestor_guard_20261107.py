"""三处「向上父链」挂死点守卫（R184 · 2026-11-07）。

背景：sections 的父链不变量完全靠应用代码守 —— `schema_sql.py` 里
`parent_id TEXT DEFAULT ''` 既无 CHECK 也无 FK。历史上（2026-09-18 前的
update_section 无环检测、外部脚本直改库、旧快照回退/方案复制）都可能留下
自父（A→A）或环（A→B→A）。

旧实现有三处**只靠 `parent_id in map` 终止**的向上遍历：

  * `_sse_outline_build._build_parent_chain`  —— 正文标题链
  * `sse_handlers.generate_content._find_unit` —— 字数预算单元
  * `sse_handlers.generate_content._build_generation_context` 的 parent_points

命中环时它们**不抛错、不返回**，而是无限循环：正文生成任务不终态、重试仍挂
（比算错更难恢复）。现在三处统一为「seen 集合 + MAX_OUTLINE_DEPTH」双上限：
重复节点或超过三级深度即终止并记 warning；父节点不在当前 map 时 while 条件
立即终止（与旧行为一致）。

本测试锁三件事：
  1. `_build_parent_chain`（模块级函数）在自父/双点环/超深/父不在 map 下都能
     有限步返回；
  2. 另两处**嵌套函数**（无法直接 import）从真实源码 AST 抽取后在子进程里执行，
     同样必须有限步返回 —— 若有人删掉守卫，子进程会超时，用例即红；
  3. 正常的二级/三级目录父链保持原有形态（守卫对合法数据是恒等变换）。
"""

import ast
import json
import subprocess
import sys
import textwrap
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
SSE_PATH = APP / "routers" / "sse_handlers.py"
BACKEND = Path(__file__).resolve().parents[1]

# 先导入 facade，避免直接 import _sse_outline_build 触发反向循环导入
import app.routers.sse_handlers  # noqa: F401,E402
from app.routers._sse_outline_build import _build_parent_chain  # noqa: E402


def _row(sid: str, parent: str, level: int, title: str, desc: str = "") -> dict:
    return {
        "id": sid,
        "parent_id": parent,
        "title": title,
        "description": desc,
        "level": level,
        "sort_order": 0,
        "outline_json": "{}",
    }


SELF_PARENT = [_row("S", "S", 3, "自父", "d")]
CYCLE = [_row("A", "B", 3, "A", "da"), _row("B", "A", 3, "B", "db")]
DEEP = [
    _row(f"d{i}", (f"d{i + 1}" if i < 20 else ""), 3, f"d{i}", f"dd{i}") for i in range(21)
]
ORPHAN = [_row("O", "GHOST", 3, "O", "do")]
NORMAL = [
    _row("r1", "", 1, "第一章 总则"),
    _row("c1", "r1", 2, "1.1 范围"),
    _row("l1", "c1", 3, "1.1.1 细则"),
]


def _by_parent(rows: list[dict]) -> dict:
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["parent_id"] or "", []).append(r)
    return out


def _chain(rows: list[dict], sid: str) -> str:
    return _build_parent_chain(rows, sid, {r["id"]: r for r in rows}, _by_parent(rows))


def test_build_parent_chain_normal_tree_keeps_shape() -> None:
    """正常三级目录：叶子父链 = 根 + 父两级，且顺序自根向下。"""
    chain = _chain(NORMAL, "l1")
    parts = chain.split(" > ")
    assert len(parts) == 2
    assert "第一章" in parts[0] or "总则" in parts[0]
    assert "范围" in parts[1]


def test_build_parent_chain_terminates_on_self_parent() -> None:
    chain = _chain(SELF_PARENT, "S")
    assert chain.strip()  # 有限步返回，且不抛错


def test_build_parent_chain_terminates_on_cycle() -> None:
    chain = _chain(CYCLE, "A")
    assert "A" in chain and "B" in chain


def test_build_parent_chain_truncates_deep_chain() -> None:
    """超深链按 MAX_OUTLINE_DEPTH 截断（正常三级目录最多 2 个祖先）。"""
    chain = _chain(DEEP, "d0")
    assert len(chain.split(" > ")) == 3


def test_build_parent_chain_orphan_parent_is_top_level() -> None:
    assert _chain(ORPHAN, "O") == "（顶级章节）"


# ---------------- 嵌套函数：AST 抽取 + 子进程执行 ----------------

RUNNER = r'''
import json, logging, sys
sys.path.insert(0, r"__BACKEND__")
ROWS = json.load(open(r"__ROWS_FILE__", encoding="utf-8"))
by_id = {r["id"]: r for r in ROWS}
scheme_id = "SAMPLE"
logger = logging.getLogger("probe")
MAX_OUTLINE_DEPTH = 3
__SRC__
print(json.dumps(__CALL__, ensure_ascii=False))
'''


def _segment_func(src: str, name: str) -> str:
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return textwrap.dedent(ast.get_source_segment(src, node))
    raise AssertionError(f"源码中找不到 {name}")


def _segment_points_block(src: str) -> str:
    tree = ast.parse(src)
    wnode = None
    for node in ast.walk(tree):
        if isinstance(node, ast.While):
            test = ast.unparse(node.test)
            if "_pid" in test and "nodes_map" in test:
                wnode = node
    assert wnode is not None, "源码中找不到 parent_points 的 while 循环"
    start = None
    for node in ast.walk(tree):
        if (isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
                and node.target.id == "parent_points" and node.lineno < wnode.lineno):
            start = node.lineno
    assert start is not None
    return "\n".join(src.splitlines()[start - 1 : wnode.end_lineno])


def _probe_wrapper(block: str) -> str:
    return (
        "def _probe(leaf, nodes_map, scheme_id, logger, MAX_OUTLINE_DEPTH):\n"
        "    parent_points = []\n"
        '    _pid = leaf.get("parent_id", "")\n'
        + textwrap.indent(textwrap.dedent(block), "    ")
        + "\n    parent_points.reverse()\n    return parent_points\n"
    )


def _run_in_subprocess(tmp_path: Path, src: str, call: str, rows: list[dict]) -> str:
    """在子进程里执行抽取出的源码；超出 8s 视为「仍会挂死」→ 用例失败。"""
    rows_file = tmp_path / "rows.json"
    rows_file.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    runner = (RUNNER.replace("__BACKEND__", str(BACKEND))
                    .replace("__ROWS_FILE__", str(rows_file))
                    .replace("__SRC__", src)
                    .replace("__CALL__", call))
    script = tmp_path / "runner.py"
    script.write_text(runner, encoding="utf-8")
    proc = subprocess.run([sys.executable, str(script)], capture_output=True, timeout=8)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")[-800:]
    return proc.stdout.decode("utf-8", "replace").strip()


def test_find_unit_terminates_on_cycle(tmp_path: Path) -> None:
    src = _segment_func(SSE_PATH.read_text(encoding="utf-8"), "_find_unit")
    for rows, target in ((SELF_PARENT, "S"), (CYCLE, "A")):
        out = _run_in_subprocess(tmp_path, src, f'_find_unit(by_id["{target}"])', rows)
        assert out == "null"


def test_generation_parent_points_terminate_on_cycle(tmp_path: Path) -> None:
    src = _probe_wrapper(_segment_points_block(SSE_PATH.read_text(encoding="utf-8")))
    out = _run_in_subprocess(
        tmp_path, src,
        '_probe(by_id["A"], by_id, scheme_id, logger, MAX_OUTLINE_DEPTH)', CYCLE,
    )
    assert json.loads(out) == ["- A：da", "- B：db"]


def test_nested_walkers_keep_orphan_behavior(tmp_path: Path) -> None:
    """父节点不在 map：两处都必须立即返回（旧行为不变）。"""
    unit_src = _segment_func(SSE_PATH.read_text(encoding="utf-8"), "_find_unit")
    out = _run_in_subprocess(tmp_path, unit_src, '_find_unit(by_id["O"])', ORPHAN)
    assert out == "null"
    points_src = _probe_wrapper(_segment_points_block(SSE_PATH.read_text(encoding="utf-8")))
    out = _run_in_subprocess(
        tmp_path, points_src,
        '_probe(by_id["O"], by_id, scheme_id, logger, MAX_OUTLINE_DEPTH)', ORPHAN,
    )
    assert json.loads(out) == []
