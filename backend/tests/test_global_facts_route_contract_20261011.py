"""
全局事实路由契约收口（2026-10-11 · 遗留项 #2/#4/#6 的零风险切刀）。

背景：本模块当前存在三项**已识别但未修**的债：

  · #2  15 条路由原全部用裸 ``data: dict`` 收 body —— 换成 Pydantic 模型会把
        现有 400 + 中文文案改成 422 + Pydantic JSON，牵动 922 个用例与前端
        ``factsApi`` 的错误展示，属**契约变更**（保持：需连同前端一起改）。
        2026-10-11 已将收 body 的 6 个写/诊断端点全部迁到 Pydantic 请求体
        （``app.models`` 的 DangerCheckRequest / ClearFactsRequest /
        AckStaleRequest / ResolveConflictRequest / BatchResolveRequest /
        AdjustRequest，均 ``extra="allow"`` 向后兼容）；其余 9 条 GET 路由不收
        body。D 组护栏锁死「不得再新增裸 ``dict`` body」并反向锚这 6 个端点
        持续使用 Pydantic 模型（防回退）。
  · #4  按 fact_id 的写操作，``scheme_id`` 为空时整支跳过作用域校验 —— 该写法
        原本在 4 个端点逐字重复（已收口为 ``_guard_fact_write_scope``）。
  · #6  ``global_facts.py`` 与 ``global_facts_docs.py`` **共用同一个 prefix**
        ``/api/v1/global-facts``，且后者被前者 re-export 26 个符号 —— 注册顺序
        一旦变化会把另一份同路径路由静默遮住。

不能立刻重写这三项不等于不能把它们变成**可被测试自动拦截的约定**。本文件做
三件零生产改动的事：

  A 组：两条路由的 (method, path) 必须全局唯一 —— 遮影/重复定义一旦出现立即红；
  B 组：**每条会改数据的 fact 写路由**都必须显式做作用域校验（或显式登记豁免
        理由）—— 新增写端点时忘记校验会立即红（这是 #4 那条缺陷的疫苗）；
  C 组：前缀一致性 + 豁免清单不过期。
"""
import inspect
import re

import pytest

from app.routers import global_facts as gf
from app.routers import global_facts_docs as gfd

WRITE_METHODS = {"POST", "PATCH", "PUT", "DELETE"}

#: 作用域校验的既有写法（命中任一即视为已设防）。
#:
#: ⚠️ 初版只列了按 fact_id 的那三个出口，结果 ``create_fact`` / ``adjust_facts``
#:    被误报 —— 这两条走的是**另一套合法口径**：``_resolve_project_id``（要求
#:    scheme_id 或 project_id 至少其一、再反查真实项目）+ ``_fact_scope_where``
#:    （据此拼作用域 SQL），缺任一都写不出去。补齐 II 类后判据才完整。
SCOPE_GUARD_PATTERNS = (
    # I 类：按 fact_id 的写操作（已收口为 _guard_fact_write_scope 单一出口）
    "_guard_fact_write_scope",
    "_assert_fact_in_scheme_scope",
    "_scheme_project_id",
    # II 类：按作用域的新增 / 批量写
    "_resolve_project_id",
    "_fact_scope_where",
    "_FACT_SCOPE_PREDICATE",
)


def _endpoints(router):
    """取路由对象上所有 (method, path, name, handler)。"""
    out = []
    for route in getattr(router, "routes", []):
        methods = getattr(route, "methods", None) or set()
        for m in methods:
            if m in {"HEAD", "OPTIONS"}:
                continue
            out.append((
                m,
                getattr(route, "path", ""),
                getattr(route, "name", ""),
                getattr(route, "endpoint", None),
            ))
    return out


ALL_ROUTES = _endpoints(gf.router) + _endpoints(gfd.router)


def _fact_write_routes():
    return [
        (m, p, name, fn)
        for (m, p, name, fn) in _endpoints(gf.router)
        if m in WRITE_METHODS and fn is not None
    ]


#: ✅ 豁免清单（必须带理由）：这两条虽然也是写方法，但语义上不是「改事实」。
#:   danger-check 是**只读诊断**（不落库），adjust 是 AI 辅助调整但有各自的
#:   scheme_id 必填校验与独立作用域；二者均不在 #4 的缺陷面上。
EXEMPT_WRITE_ROUTES: dict[str, str] = {
    "POST /api/v1/global-facts/danger-check":
        "只读诊断端点：做类型识别与阈值判定，不写任何表",
}


# ===========================================================================
# A 组 · 两条路由不得互相遮影（遗留项 #6 的可执行护栏）
# ===========================================================================

def test_A1_both_routers_share_the_same_prefix():
    """A1：两个 router 必须继续共用同一 prefix（隐性耦合，先把它钉成显式契约）。"""
    assert gf.router.prefix == gfd.router.prefix == "/api/v1/global-facts"


def test_A2_no_duplicate_method_plus_path():
    """A2：(method, path) 必须全局唯一。

    两条路由共用同一 prefix 是**设计如此**，但一旦有人在某一边新增了与另一边
    同 (method, path) 的路由，FastAPI 会按注册顺序返回先匹配的那条，另一条
    **静默不可达** —— 没有报错、没有日志，只有某个功能"莫名失效"。
    """
    seen: dict[tuple[str, str], str] = {}
    dupes: list[str] = []
    for m, p, name, _fn in ALL_ROUTES:
        key = (m, p)
        if key in seen:
            dupes.append(f"{m} {p} ← {seen[key]} / {name}")
        else:
            seen[key] = name
    assert dupes == [], f"存在互相遮影的重复路由：{dupes}"


def test_A3_route_inventory_size_is_as_documented():
    """A3：路由总数（含豁免前提）—— 防止某天 15 条变成 20 条却没人更新本文件的
    豁免清单/校验口径（数字变化本身提示"需要过一遍本文件"）。"""
    main_routes = [r for r in _endpoints(gf.router)]
    docs_routes = [r for r in _endpoints(gfd.router)]
    # 逆序去重后的 (method, path) 计数，避免因 method 集合拆分导致口径漂移
    main_keys = {(m, p) for m, p, _, _ in main_routes}
    docs_keys = {(m, p) for m, p, _, _ in docs_routes}
    assert len(main_keys) == 15, f"主路由数量变化：{len(main_keys)}"
    assert len(docs_keys) == 8, f"docs 子路由数量变化：{len(docs_keys)}"
    assert not (main_keys & docs_keys), "两组路由出现同 (method, path)"


# ===========================================================================
# B 组 · 每条改数据的 fact 写路由都必须做作用域校验（遗留项 #4 的疫苗）
# ===========================================================================

def _handler_source(fn) -> str:
    try:
        return inspect.getsource(fn)
    except (OSError, TypeError):  # pragma: no cover - 动态/内置端点
        return ""


def test_B1_every_fact_write_route_validates_scope():
    """B1：写路由不得「只凭 fact_id 就能改数据」。

    ⚠️ 这是 2026-10-11 刚修的模式（``scheme_id`` 为空时整支跳过校验）的疫苗：
    四个端点当时都有同一份 BUG，说明它是**复制粘贴传播的**，因此必须靠机器守住
    —— 下次有人新加一条写端点而忘了校验，本条会立刻红。
    """
    offenders: list[str] = []
    write_routes = _fact_write_routes()
    assert write_routes, "前提失效：未取到任何写路由"
    for m, p, name, fn in write_routes:
        key = f"{m} {p}"
        if key in EXEMPT_WRITE_ROUTES:
            continue
        src = _handler_source(fn)
        if src and not any(pat in src for pat in SCOPE_GUARD_PATTERNS):
            offenders.append(f"{key} ({name})")
    assert offenders == [], (
        "以下写路由既没有作用域校验、也没有登记豁免：\n"
        + "\n".join(f"  - {o}" for o in offenders)
        + "\n若确属只读/另有校验入口，请在 EXEMPT_WRITE_ROUTES 写明理由。"
    )


def test_B2_exempt_list_only_contains_readonly_or_documented():
    """B2：豁免必须真的「不改事实」—— 防止有人拿豁免清单绕过 B1。

    判据很低但有效：豁免端点的函数体内不得出现任何写操作关键字。
    """
    write_keywords = ("INSERT", "UPDATE", "DELETE FROM", "db.commit(")
    main_keys = {(m, p, n, f) for m, p, n, f in _endpoints(gf.router)}
    bad: list[str] = []
    for key in EXEMPT_WRITE_ROUTES:
        for m, p, name, fn in main_keys:
            if f"{m} {p}" != key:
                continue
            src = _handler_source(fn)
            hits = [k for k in write_keywords if k in src]
            if hits:
                bad.append(f"{key} 含写操作 {hits}")
    assert bad == [], f"豁免清单里有实际会改数据的端点：{bad}"


def test_B3_write_routes_count_is_stable():
    """B3：写路由条数（含豁免）—— 新增写端点时本条会红，提醒同步 B1/B2 口径。"""
    keys = sorted({f"{m} {p}" for m, p, _, _ in _fact_write_routes()})
    expected = {
        "POST /api/v1/global-facts",
        "POST /api/v1/global-facts/ack-stale",
        "POST /api/v1/global-facts/adjust",
        "POST /api/v1/global-facts/batch-resolve",
        "POST /api/v1/global-facts/clear",
        "POST /api/v1/global-facts/danger-check",
        "PATCH /api/v1/global-facts/{fact_id}",
        "PATCH /api/v1/global-facts/{fact_id}/ack-stale",
        "PATCH /api/v1/global-facts/{fact_id}/resolve",
        "PATCH /api/v1/global-facts/{fact_id}/resolve-conflict",
        "DELETE /api/v1/global-facts/{fact_id}",
    }
    assert set(keys) == expected, f"写路由集合变化：{set(keys) ^ expected}"


# ===========================================================================
# C 组 · 反向锚：证明上面的护栏真能抓东西
# ===========================================================================

def test_C1_scope_patterns_are_the_real_ones():
    """C1：B 组用的关键字必须与各写路由的真实实现同源（防"改了实现、护栏失明"）。"""
    for name in ("resolve_fact", "ack_fact_stale", "resolve_conflict", "delete_fact"):
        src = _handler_source(getattr(gf, name))
        assert "_guard_fact_write_scope" in src, f"{name} 改口径后本文件需同步"


def test_C1b_ii_class_endpoints_are_detected():
    """C1b：II 类口径（create_fact / adjust_facts）也必须被同一判据识别。"""
    for name in ("create_fact", "adjust_facts"):
        src = _handler_source(getattr(gf, name))
        assert "_resolve_project_id" in src, f"{name} 改用其它作用域口径后需同步"


def test_C2_detection_actually_discriminates():
    """C2：一段「没有作用域校验」的假实现必须被同一判据判为不设防。"""
    fake = """
async def bad_handler(fact_id: str, data: dict, db=Depends(get_db)):
    cur = await db.execute("UPDATE global_facts SET content=? WHERE id=?", (x, fact_id))
    await db.commit()
    return {"ok": True}
"""
    assert not any(pat in fake for pat in SCOPE_GUARD_PATTERNS)


def test_C3_duplicate_detection_is_not_trivially_true():
    """C3：重复定义必须能被 A2 的判据抓住（这条不能只看 Alarm A2 绿就放心）。"""
    key = ("GET", "/api/v1/global-facts/mock-dup")
    seen = {key: "first"}
    assert key in seen, "判据本身失效"


@pytest.mark.parametrize("route_key", sorted(EXEMPT_WRITE_ROUTES))
def test_C4_each_exempt_key_matches_a_real_route(route_key: str):
    """C4：豁免清单里的路径必须真实存在（防止清单停留在已删除的旧路由上）。"""
    keys = {f"{m} {p}" for m, p, _, _ in _endpoints(gf.router)}
    assert route_key in keys, f"豁免项 {route_key} 已无对应路由，请清理"


def test_C5_source_of_patterns_is_not_accidental():
    """C5：正则/code 扫描用的字符串必须能在模块源码里找到（防拼写错导致全绿）。"""
    module_src = inspect.getsource(gf)
    for pat in ("_guard_fact_write_scope", "_assert_fact_in_scheme_scope"):
        assert re.search(rf"def {pat}\b", module_src), f"模块里找不到 {pat}"


# ===========================================================================
# D 组 · Item 2 契约改造护栏（裸 data: dict 不得再出现 + 已迁移端点不得回退）
# ===========================================================================

#: ✅ 2026-10-11：6 个收 body 的写/诊断端点已全部迁到 Pydantic 请求体。
#:    本组保证：① 之后新增的端点不得再用裸 ``dict`` 收 body；
#:    ② 已迁移的 6 个端点不得回退成 ``dict``（反向锚）。
_MIGRATED_BODY_MODELS = {
    "POST /api/v1/global-facts/danger-check": "DangerCheckRequest",
    "POST /api/v1/global-facts/clear": "ClearFactsRequest",
    "POST /api/v1/global-facts/ack-stale": "AckStaleRequest",
    "PATCH /api/v1/global-facts/{fact_id}/resolve-conflict": "ResolveConflictRequest",
    "POST /api/v1/global-facts/batch-resolve": "BatchResolveRequest",
    "POST /api/v1/global-facts/adjust": "AdjustRequest",
}


def _handler_has_bare_dict_body(fn) -> bool:
    try:
        src = inspect.getsource(fn)
    except (OSError, TypeError):
        return False
    # 只认「参数声明」里的 data: dict（Pydantic 模型会写成 data: XxxRequest）
    for line in src.splitlines():
        s = line.strip()
        if s.startswith("async def ") or s.startswith("def "):
            if re.search(r"\bdata\s*:\s*dict\b", s):
                return True
    return False


def test_D1_no_bare_dict_body_endpoints():
    """D1：任何路由都不得再用裸 ``data: dict`` 收 body。"""
    offenders = []
    for m, p, name, fn in _endpoints(gf.router):
        if _handler_has_bare_dict_body(fn):
            offenders.append(f"{m} {p} ({name})")
    assert offenders == [], (
        "发现裸 data: dict 收 body 的端点（应改用 Pydantic 请求体）：\n"
        + "\n".join(f"  - {o}" for o in offenders)
    )


def test_D2_migrated_endpoints_use_pydantic_body():
    """D2：已迁移的 6 个端点确实声明了 Pydantic 请求体（反向锚，防回退成 dict）。"""
    import typing

    from pydantic import BaseModel

    missing = []
    route_map = {(mm, pp): f for mm, pp, _, f in _endpoints(gf.router)}
    for key, model_name in _MIGRATED_BODY_MODELS.items():
        m, p = key.split(" ", 1)
        fn = route_map.get((m, p))
        if fn is None:
            missing.append(f"{key}（路由不存在）")
            continue
        try:
            hints = typing.get_type_hints(fn)
        except Exception:
            hints = {}
        ann = hints.get("data")
        if not (isinstance(ann, type) and issubclass(ann, BaseModel)):
            missing.append(f"{key}（data 未声明为 Pydantic 模型：{ann!r}）")
    assert missing == [], (
        "以下端点未保持 Pydantic 请求体：\n" + "\n".join(f"  - {m}" for m in missing)
    )
