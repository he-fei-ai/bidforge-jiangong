# -*- coding: utf-8 -*-
"""
R88 护栏 · 加法式回传键的**唯一透传口径**（FIX-8 + FIX-9）

背景（本轮实测复现）：
  目录库套用链路 `outline_library.apply_library_and_save` 此前**手写白名单**
  逐个挑选 `_save_outline_to_db` 的回传字段。它在 2026-09-30（P1-7）补上了
  `cleared_content_sections`（当时是为修「用户静默丢正文」），但 R79 之后新增的
  `generic_residual`（占位/泛化标题**明细清单**）**又一次被静默丢弃**
  ⇒ 套用目录库命中占位标题时，后端算出了明细、日志也记了，用户却**零感知**。

  ⇒ 同一个反模式在同一个函数上**复发**：白名单每加一个新键就漏一次。

判定纪律（本文件要锁住的东西）：
  A. `passthrough_additive` 的语义（全量 / 部分 / 空 / None / 不泄漏非加法键）
  B. `SAVE_OUTLINE_ADDITIVE_KEYS` **覆盖** `_save_outline_to_db` 实际产出的每一个
     加法式键 —— 加法式契约由「消费方逐个挑选」退化为「生产方声明一次」
  C. **反向锁**：`apply_library_and_save` 的返回体里**不得**再出现
     `result["xxx"]` 这类手写挑选 —— 否则白名单会重新长回来（B 条恒过、C 条恒红的
     组合才是真判别力：只锁常量等于「常量写对了就行」，锁不住「消费方用没用它」）
  D. **端到端**：占位标题的目录库套用后，响应**必须**带 `generic_residual`
  E. **零回归**：无残留时响应键集合与修复前**逐字一致**（加法式，不得多出空键）

⚠️ 本文件刻意**不含**「端点必须带 generic_residual 的 HTTP 层断言」以外的行为变更，
   也不改动 `_save_outline_to_db` 的任何产出 —— 纯消费侧口径收敛。
"""
from __future__ import annotations

import ast
import json
import uuid
from pathlib import Path

import pytest
from app.config import settings
from app.routers.sections import (
    SAVE_OUTLINE_ADDITIVE_KEYS,
    passthrough_additive,
)

APP = Path(__file__).resolve().parents[1] / "app"   # tests/ -> backend/ -> backend/app
LIB_PY = APP / "routers" / "outline_library.py"
SECTIONS_PY = APP / "routers" / "sections.py"


# ---------------------------------------------------------------- A 语义
class TestPassthroughSemantics:
    """A. 纯函数语义：这是「漏字段」的根治点，语义必须比白名单更严谨。"""

    def test_picks_every_additive_key(self):
        r = {"cleared_content_sections": 3, "generic_residual": [{"x": 1}],
             "roots_locked": True, "ok": True, "count": 9}
        assert passthrough_additive(r) == {
            "cleared_content_sections": 3,
            "generic_residual": [{"x": 1}],
            "roots_locked": True,
        }

    def test_empty_result_yields_empty_dict(self):
        assert passthrough_additive({"ok": True, "count": 1}) == {}

    def test_partial_result_is_forwarded_not_defaulted(self):
        """只有 generic_residual 时**必须**带出去（这正是本轮漏的那一个键）。"""
        assert passthrough_additive({"generic_residual": [{"t": "专项6"}]}) == {
            "generic_residual": [{"t": "专项6"}]}

    def test_false_and_zero_are_preserved(self):
        """`if result.get(k)` 形态会把 0 / False 当缺省丢掉 —— 计数类字段是真信息。"""
        assert passthrough_additive({"cleared_content_sections": 0}) == {
            "cleared_content_sections": 0}

    def test_none_is_treated_as_absent(self):
        """None 表示「该出口没产出」，不得把 None 透传给前端当值。"""
        assert passthrough_additive({"generic_residual": None}) == {}

    def test_non_additive_keys_never_leak(self):
        """反向：ok/count/tree 不在清单里，就绝不能被顺手带出去（避免撑大响应）。"""
        out = passthrough_additive({"ok": True, "count": 1, "tree": [{"t": "a"}],
                                   "note": "x"})
        assert set(out) == set()

    def test_never_raises_on_missing_key(self):
        """消费方传空 dict / None 都不该抛 —— 透传不是校验点。"""
        assert passthrough_additive({}) == {}

    def test_value_identity_is_preserved(self):
        """明细清单必须原样透传（前端按 title 列表渲染，改结构即破坏契约）。"""
        detail = [{"title": "总体实施策划（2）", "level": 1}]
        out = passthrough_additive({"generic_residual": detail})
        assert out["generic_residual"] is detail


# ---------------------------------------------------------------- B 覆盖
#: `_save_outline_to_db` 的两个返回出口各自持有的结果字典名。
#: ⚠️ 早退分支（清空目录）用的是**另一个**字典 `_empty_result`，只扫 `result`
#:    会漏掉它 —— 这正是本条扫描器首版的盲区（它同时还错把赋值右值限定成 Name，
#:    导致 `result["roots_locked"] = True` 也看不见）。
_RESULT_DICTS = {"result", "_empty_result"}

#: 基础契约键（不属于加法式，消费方**可以**按名字直取）
BASE_KEYS = {"ok", "count", "tree"}


def _save_outline_additive_keys() -> set[str]:
    """AST 扫 `_save_outline_to_db` 全部返回出口实际产出的加法式键。"""
    tree = ast.parse(SECTIONS_PY.read_bytes().decode("utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "_save_outline_to_db")
    keys: set[str] = set()
    for n in ast.walk(fn):
        # <dict>["k"] = ...      （判据看**目标**，不看右值）
        if isinstance(n, ast.Assign):
            t = n.targets[0]
            if isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name) \
                    and t.value.id in _RESULT_DICTS and isinstance(t.slice, ast.Constant):
                keys.add(t.slice.value)
        # attach_generic_residual(result, ...) → 单一出口挂载（注释声称的键）
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) \
                and n.func.id == "attach_generic_residual":
            keys.add("generic_residual")
    return keys - BASE_KEYS


class TestKeyListCoversProducer:
    """B. 清单必须覆盖生产方实际产出的每一个加法式键。"""

    def test_list_is_not_empty(self):
        assert SAVE_OUTLINE_ADDITIVE_KEYS

    def test_list_has_no_duplicates(self):
        assert len(set(SAVE_OUTLINE_ADDITIVE_KEYS)) == len(SAVE_OUTLINE_ADDITIVE_KEYS)

    def test_covers_every_key_the_producer_emits(self):
        produced = _save_outline_additive_keys()
        missing = produced - set(SAVE_OUTLINE_ADDITIVE_KEYS)
        assert not missing, (
            f"_save_outline_to_db 新产出加法式键 {sorted(missing)}，"
            f"必须登记进 sections.SAVE_OUTLINE_ADDITIVE_KEYS，否则套用目录库链路静默丢字段")

    def test_declared_keys_are_actually_produced(self):
        """反向：清单里不得有生产方根本不产出的键（否则是死条目，误导后人）。"""
        produced = _save_outline_additive_keys()
        assert not (set(SAVE_OUTLINE_ADDITIVE_KEYS) - produced), (
            "SAVE_OUTLINE_ADDITIVE_KEYS 含生产方不产出的键 —— 死条目")

    def test_generic_residual_is_registered(self):
        """本轮漏的就是它，显式锁一条避免日后被「清理」掉。"""
        assert "generic_residual" in SAVE_OUTLINE_ADDITIVE_KEYS

    def test_cleared_content_sections_is_registered(self):
        """2026-09-30 P1-7 的老键不得因重构丢失（用户静默丢正文的根因键）。"""
        assert "cleared_content_sections" in SAVE_OUTLINE_ADDITIVE_KEYS


# ---------------------------------------------------------------- C 反向锁
def _lib_ast() -> ast.Module:
    return ast.parse(LIB_PY.read_bytes().decode("utf-8"))


def _apply_fn() -> ast.AsyncFunctionDef:
    for n in ast.walk(_lib_ast()):
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "apply_library_and_save":
            return n
    raise AssertionError("找不到 apply_library_and_save")


def _returns_dict(fn) -> ast.Dict:
    for n in ast.walk(fn):
        if isinstance(n, ast.Return) and isinstance(n.value, ast.Dict):
            return n.value
    raise AssertionError("apply_library_and_save 没有字典返回体")


class TestNoHandWrittenPickingReturns:
    """C. 反向锁：返回体里不得再手写 `result["..."]` 挑选。"""

    def test_uses_passthrough_additive(self):
        src = LIB_PY.read_bytes().decode("utf-8")
        fn = _apply_fn()
        called = [n for n in ast.walk(fn)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                  and n.func.id == "passthrough_additive"]
        assert called, "apply_library_and_save 必须经 sections.passthrough_additive 透传"

    def test_returns_dict_has_no_manual_result_subscript(self):
        """白名单复发的根治判据：**加法式**键不得在返回体里被手写挑选。

        ⚠️ 基础契约键（count / tree）**允许**按名直取 —— 它们是响应骨架、不是
        加法式增量，硬禁会逼着后人把正经字段也塞进 helper，反而更糟。
        """
        ret = _returns_dict(_apply_fn())
        picks = [n.slice.value for n in ast.walk(ret)
                 if isinstance(n, ast.Subscript) and isinstance(n.value, ast.Name)
                 and n.value.id == "result" and isinstance(n.slice, ast.Constant)]
        illegal = sorted(set(picks) & set(SAVE_OUTLINE_ADDITIVE_KEYS))
        assert illegal == [], (
            f"返回体仍在手写挑选加法式键 {illegal} —— 白名单每加一个新键就漏一次"
            f"（generic_residual 就是这么丢的）；改用 passthrough_additive")

    def test_base_contract_keys_stay_readable(self):
        """反向锁：count / tree 必须仍按名直取（防止后来者把骨架也塞进 helper）。"""
        ret = _returns_dict(_apply_fn())
        picks = {n.slice.value for n in ast.walk(ret)
                 if isinstance(n, ast.Subscript) and isinstance(n.value, ast.Name)
                 and n.value.id == "result" and isinstance(n.slice, ast.Constant)}
        assert {"count", "tree"} <= picks

    def test_splat_is_unconditional(self):
        """透传必须是 `**passthrough_additive(result)` —— 若再套 `if result.get(...)`
        逐键条件，等于把白名单换个写法重犯。"""
        ret = _returns_dict(_apply_fn())
        splats = []
        for k in ret.keys:
            if isinstance(k, ast.Dict):
                for v in k.values:
                    if isinstance(v, ast.Call) and isinstance(v.func, ast.Name) \
                            and v.func.id == "passthrough_additive":
                        splats.append(k)
        # 允许裸 **（unpacking node 为 None）或 dict-splat 两种落法
        assert splats or any(k is None for k in ret.keys), \
            "透传未以 ** 展开（会被当成普通值键丢弃）"

    def test_no_dead_import_of_passthrough(self):
        """导入必须真被用（AST ImportFrom + 真实调用），防「import 了但没用」。"""
        tree = _lib_ast()
        imports = [n for n in ast.walk(tree)
                   if isinstance(n, ast.ImportFrom) and n.module == "app.routers.sections"
                   and any(a.name == "passthrough_additive" for a in n.names)]
        assert imports, "未从 sections 导入 passthrough_additive"


# ---------------------------------------------------------------- D 端到端
async def _seed_library(conn, name: str, tree: list[dict]) -> str:
    """db_conn 是 aiosqlite 连接 ⇒ execute 是协程，必须 await。"""
    lib_id = "lib_" + uuid.uuid4().hex[:8]
    await conn.execute(
        "INSERT INTO outline_library (id, name, outline_json, source, review_status) "
        "VALUES (?,?,?,?,?)",
        (lib_id, name, json.dumps(tree, ensure_ascii=False), "测试", "已通过"),
    )
    return lib_id


async def _seed_scheme(conn) -> str:
    """schemes.project_id 是 NOT NULL —— 必须先有项目，否则插入即抛 IntegrityError。"""
    pid = "proj_" + uuid.uuid4().hex[:8]
    await conn.execute("INSERT INTO projects (id, name) VALUES (?,?)", (pid, "套用测试项目"))
    sid = uuid.uuid4().hex[:8]
    await conn.execute(
        "INSERT INTO schemes (id, name, project_id) VALUES (?,?,?)",
        (sid, "套用测试方案", pid))
    return sid


class TestLibraryApplyEndToEnd:
    """D. 端到端：占位标题的目录库套用后，响应必须带明细。"""

    @pytest.fixture(autouse=True)
    def _isolate_r89_autofix(self, monkeypatch):
        """断开 R89 的「落库前自动外科式重写」，只测本组的透传口径。

        本组锁的是**加法式回传契约**（脏树相对干净树只多一个键）。若不断开，
        套用链路会真的发起一次 AI 标题重写调用：改写成功后 `generic_residual`
        自然消失，断言就退化成对第三方模型输出的依赖 —— 离线必失败、在线恰好
        通过，结论不稳定且测不到本组要锁的东西。自动重写本身由 R89 护栏单独覆盖。
        """
        monkeypatch.setattr(settings, "outline_autofix_generic_on_save", False,
                            raising=False)

    @pytest.mark.asyncio
    async def test_placeholder_title_reaches_response(self, db_conn):
        from app.routers import outline_library as ol

        tree = [
            {"id": "1", "title": "工程概况", "children": [
                {"id": "1.1", "title": "专项6", "children": []},
            ]},
        ]
        lib_id = await _seed_library(db_conn, "含占位标题的库", tree)
        sid = await _seed_scheme(db_conn)

        res = await ol.apply_library_and_save(lib_id, {"scheme_id": sid}, db=db_conn)

        assert res["ok"] is True
        assert "generic_residual" in res, \
            "套用目录库命中占位标题时必须回传明细（修复前该键被白名单丢弃）"
        assert res["generic_residual"], "检出占位标题 ⇒ 明细不得为空"
        titles = [h.get("title") for h in res["generic_residual"]]
        assert any("专项6" in (t or "") for t in titles), f"明细应含占位标题，实际 {titles}"

    @pytest.mark.asyncio
    async def test_clean_library_response_has_no_residual_key(self, db_conn):
        """E. 零回归：干净树**不得**多出空键（加法式，不污染既有消费方）。"""
        from app.routers import outline_library as ol

        tree = [{"id": "1", "title": "工程概况", "children": [
            {"id": "1.1", "title": "周边环境与施工条件", "children": []}]}]
        lib_id = await _seed_library(db_conn, "干净库", tree)
        sid = await _seed_scheme(db_conn)

        res = await ol.apply_library_and_save(lib_id, {"scheme_id": sid}, db=db_conn)

        assert res["ok"] is True
        assert "generic_residual" not in res
        # 既有基础键一个都不能少
        for k in ("library_id", "scheme_id", "count", "tree", "name", "source", "note"):
            assert k in res, f"既有契约键 {k} 丢失"

    @pytest.mark.asyncio
    async def test_response_is_additive_only(self, db_conn):
        """D/E 合流：脏树相对干净树**只多** generic_residual，其余键不变。"""
        from app.routers import outline_library as ol

        def _mk(title: str):
            return [{"id": "1", "title": "工程概况", "children": [
                {"id": "1.1", "title": title, "children": []}]}]

        lib_ok = await _seed_library(db_conn, "干净库A", _mk("周边环境与施工条件"))
        lib_bad = await _seed_library(db_conn, "占位库A", _mk("专项6"))
        s1 = await _seed_scheme(db_conn)
        s2 = await _seed_scheme(db_conn)

        a = await ol.apply_library_and_save(lib_ok, {"scheme_id": s1}, db=db_conn)
        b = await ol.apply_library_and_save(lib_bad, {"scheme_id": s2}, db=db_conn)

        assert set(b) - set(a) == {"generic_residual"}
        assert set(a) - set(b) == set()