"""2026-09-27 性能基线（Performance Baseline）。

目的：在没有专业压测框架的前提下，为七模块链路上被高频调用的纯函数 /
轻量解析函数建立**可回归的上界护栏**，使后续改动一旦引入数量级退化
（例如忘记 memo、在 O(n) 循环里做 O(n) 查找、把 break 换成全量扫描）
能在 CI 阶段就被发现，而不是等生产变慢后靠人肉感知。

设计原则（避免脆弱测试）：
1. **计数断言优先**：能用调用次数/循环次数锁定的，用确定性断言（永不 flaky）。
2. **时间断言次之**：预算取实测值的 20~50 倍，只拦数量级退化，
   不受 CI 机器负载波动影响。
3. 全部用例**不联网、不写 DB**，总耗时 < 2s。

复跑：``python -m pytest tests/test_perf_baseline_20260927.py -q``
"""
import time

# 宽松预算（秒）。见各用例注释。
_BUDGET = {
    "renumber": 5.0,
    "parse_blocks": 5.0,
    "facts_text": 5.0,
    "allocate": 2.0,
    "numbering": 2.0,
    "figure_chapter": 2.0,
    "strip": 3.0,
    "chart_key": 2.0,
    "subheading": 10.0,
}


def _elapsed(fn, *a, **k):
    t0 = time.perf_counter()
    fn(*a, **k)
    return time.perf_counter() - t0


# ============================================================
# 1. 目录编号重排（目录生成 / 拖拽排序的共同入口）
# ============================================================

def _mk_tree(depth, width, prefix=""):
    out = []
    for i in range(width):
        nid = f"{prefix}.{i + 1}" if prefix else str(i + 1)
        node = {"id": nid, "title": f"章节{i + 1}标题", "level": 1}
        node["children"] = (_mk_tree(depth - 1, width, nid)
                            if depth > 1 else [])
        out.append(node)
    return out


def test_renumber_outline_nodes_perf():
    """8000+ 节点（约 200 章）的递归重排必须秒级。

    关键性质：`_path` 环引用守卫与 `_MAX_TREE_DEPTH=200` 失控兜底使递归**有界**，整体 O(节点数)；
    若守卫被去掉，遇到环形 children 会直接爆栈。
    """
    from app.services.numbering import renumber_outline_nodes
    tree = _mk_tree(depth=3, width=20)   # 20 + 400 + 8000
    dt = _elapsed(renumber_outline_nodes, tree)
    assert dt < _BUDGET["renumber"], f"重排 8k+ 节点耗时 {dt:.3f}s"
    assert [n["id"] for n in tree] == [str(i) for i in range(1, 21)]


def test_renumber_guards_against_cycles():
    """反例：自引用须被 _path 环守卫终止（不得 RecursionError）；60 级合法深链在 _MAX_TREE_DEPTH=200 内**完整编号**（2026-10-08 D-1：旧上限 20 会静默截断 → 现不截断）。"""
    from app.services.numbering import renumber_outline_nodes
    node = {"id": "1", "title": "A"}
    node["children"] = [node]
    renumber_outline_nodes([node])
    deep = {"id": "1", "title": "L0", "children": []}
    cur = deep
    for i in range(1, 60):
        nxt = {"id": f"1.{i}", "title": f"L{i}", "children": []}
        cur["children"] = [nxt]
        cur = nxt
    renumber_outline_nodes([deep])
    # D-1 回归锁：60 级合法深链末端必须被**重排**成规范编号（构造期预置的是
    # 垃圾 "1.59"，只有 renumber 真正下探到第 60 层才会覆写为 60 段 "1.1…1"）；
    # 旧上限 20 会在 _depth>20 提前 return，末端残留 "1.59" 且无 level → 本断言即红。
    assert cur["id"] == "1." * 59 + "1", f"末端未完整重排: {cur.get('id')!r}"
    assert cur["level"] == 60, f"末端 level 与深度不对齐: {cur.get('level')!r}"


# ============================================================
# 2. 正文块解析（导出与正文落库共用的最热路径）
# ============================================================

def test_parse_content_blocks_perf():
    """2000+ 段正文的块解析（围栏/表格/列表识别）必须秒级。"""
    from app.services.content_blocks import _parse_content_blocks
    body = []
    for i in range(500):
        body += [f"## {i + 1} 小节标题",
                 f"这是第 {i + 1} 段的正文内容。" * 4,
                 "- 列表项 A", "- 列表项 B"]
    content = "\n".join(body)
    dt = _elapsed(_parse_content_blocks, content)
    assert dt < _BUDGET["parse_blocks"], f"2000 段解析耗时 {dt:.3f}s"
    assert len(_parse_content_blocks(content)) >= 2000, "块解析明显漏解析"


# ============================================================
# 3. 事实预算分配（本轮改动点：新增一次 O(n) 预处理 + 比例分配）
# ============================================================

def test_render_facts_text_perf():
    """5000 条事实的渲染必须秒级，且**不因分配器引入二次方**。

    计数护栏：prepared 预处理 O(n)、_allocate_char_budgets O(n)、
    渲染循环 O(n) —— 整体 O(n)。分配器若被塞进循环即退化为 O(n^2)。
    """
    from app.routers.sse_handlers import _render_facts_text
    rows = [(f"组{i % 20}", f"t{i}", "甲" * 200, 0.9) for i in range(5000)]
    dt = _elapsed(_render_facts_text, rows, max_total=6000, per_fact=300)
    assert dt < _BUDGET["facts_text"], f"5000 条事实渲染耗时 {dt:.3f}s"


def test_allocate_char_budgets_linear():
    """比例分配器：2 万个分节必须线性完成。"""
    from app.routers.sse_handlers import _allocate_char_budgets
    dt = _elapsed(_allocate_char_budgets, [100] * 20000, 100000)
    assert dt < _BUDGET["allocate"], f"2 万分节分配耗时 {dt:.3f}s"
    # 确定性：未超预算时是恒等映射
    assert _allocate_char_budgets([5, 7, 9], 1000) == [5, 7, 9]
    # 超预算时保底不得超过各分节自身长度
    assert all(a <= 3 for a in _allocate_char_budgets([3] * 100, 100))


def test_facts_text_budget_is_hard_cap():
    """硬上限：任何 max_total 下输出都不得超过（提示词长度是契约）。"""
    from app.routers.sse_handlers import _render_facts_text
    for n, body, budget in ((200, 100, 500), (50, 300, 1000), (500, 200, 2000)):
        rows = [(f"g{i % 7}", f"t{i}", "甲" * body, 0.9) for i in range(n)]
        out = _render_facts_text(rows, max_total=budget, per_fact=300)
        assert len(out) <= budget, f"n={n} budget={budget} 输出 {len(out)}"


# ============================================================
# 4. 编号转换（目录生成 / 正文 / 导出三模块的公共口径）
# ============================================================

def test_numbering_conversion_perf():
    """10 万次存储态→展示态转换必须秒级（导出逐标题调用）。"""
    from app.services.numbering import stored_id_to_display, stored_id_to_prefix
    ids = ["1", "12", "1.2", "12.34", "1.2.3", "1.2.3.4",
           "1.2.3.4.5.6.7"] * 15000
    t0 = time.perf_counter()
    for sid in ids:
        stored_id_to_display(sid)
        stored_id_to_prefix(sid)
    dt = time.perf_counter() - t0
    assert dt < _BUDGET["numbering"], f"{len(ids)} 次转换耗时 {dt:.3f}s"


def test_figure_chapter_num_perf():
    """图号章序号在每张图/每个表题注处调用，必须常数级。"""
    from app.routers.export import _figure_chapter_num

    class _Gen:
        counters = [3, 1, 2, 0, 0, 0, 0, 0]

    t0 = time.perf_counter()
    for _ in range(100000):
        _figure_chapter_num(_Gen())
    dt = time.perf_counter() - t0
    assert dt < _BUDGET["figure_chapter"], f"10 万次耗时 {dt:.3f}s"
    assert _figure_chapter_num(_Gen()) == 3   # 含 L1 时与旧实现一致


# ============================================================
# 5. 标题剥号（导出 + 落库双侧调用）
# ============================================================

def test_strip_outline_numbering_perf():
    from app.services.numbering import strip_outline_numbering
    titles = ["第一章 工程概况", "2.1 相关法律法规", "3.2.4 计算书",
              "（一）基本规定", "1）现场管理", "十二、应急预案",
              "2023年版规范 第3.4.5条", "普通标题无编号"] * 6000
    t0 = time.perf_counter()
    for t in titles:
        strip_outline_numbering(t)
    dt = time.perf_counter() - t0
    assert dt < _BUDGET["strip"], f"剥号耗时 {dt:.3f}s"
    # 确定性：纯编号标题原样返回（不误伤）
    assert strip_outline_numbering("1.2.3") == "1.2.3"
    assert strip_outline_numbering("第一章 工程概况") == "工程概况"


# ============================================================
# 6. 图表缓存键（本轮改动：键首段新增版本号）
# ============================================================

def test_chart_cache_key_perf():
    """5000 次缓存键构造（含长载荷 sha1 分支）必须秒级。"""
    from app.services.ai import mermaid_renderer as mr
    short = "graph TD; A-->B;"
    long_payload = "graph TD\n" + "\n".join(
        f"  N{i}-->N{i + 1}" for i in range(2000))
    t0 = time.perf_counter()
    for _ in range(5000):
        mr._render_cache_key(short, "flowchart", 90, "天", 5, False, True)
        mr._render_cache_key(long_payload, "gantt", 90, "天", 5, False, True)
    dt = time.perf_counter() - t0
    assert dt < _BUDGET["chart_key"], f"缓存键构造耗时 {dt:.3f}s"
    # 确定性：超长载荷走 sha1（避免「前 8192 字符相同」造成串图）
    assert mr._code_cache_ident(long_payload).startswith("sha1:")
    assert mr._code_cache_ident(short) == short


# ============================================================
# 7. 回归风险：本轮改动是否引入额外的全表扫描 / N+1
# ============================================================

def test_fact_dimension_derivation_is_not_n_plus_one():
    """反例：事实行级派生不得逐行查库（N 条事实 = N 次往返）。"""
    import inspect

    from app.routers import global_facts as gf
    assert "await db.execute" not in inspect.getsource(gf._fact_dimension_fields)


def test_render_facts_text_allocates_once():
    """分配器只能在循环外调用一次（循环内调用 = O(n^2)）。"""
    import inspect

    from app.routers import sse_handlers as sh
    assert inspect.getsource(sh._render_facts_text).count(
        "_allocate_char_budgets(") == 1



def test_subheading_renumber_200_sections():
    """200 章的正文子标题编号规范化（落库前必跑）总量必须可控。"""
    from app.services.numbering import renumber_section_body_subheadings
    total = 0.0
    for i in range(200):
        content = "\n".join(
            [f"### {i + 1}.{j + 1} 小标题" for j in range(10)]
            + [f"正文段落 {j}。" for j in range(20)])
        total += _elapsed(renumber_section_body_subheadings,
                          content, f"1.{i + 1}", 2, f"章节{i + 1}")
    assert total < _BUDGET["subheading"], f"200 章规范化总耗时 {total:.3f}s"
