# -*- coding: utf-8 -*-
"""图表模块 R13 判空 + 第 4 白名单 parity + 导出预检中文标签 parity（2026-10-04）。

三组护栏（均为「同一判据在多处各自实现」类缺陷的收敛，AGENTS.md §4.3/§4.7/§4.12 教训）：

1. **R13（`db.execute()` 返回 None）**：全仓 12 处 router 已加守卫，唯独
   ``routers/charts.py`` **零个** —— 清单端点 / AI 配图端点在 DB 异常态直接
   ``AttributeError`` → 500，写回路径则把「未执行的 UPDATE」谎报成
   ``written_back=True``。读路径按仓库既定口径 fail-closed **503**
   （见 ``routers/prompts.py::_fetch_content_row``、``doc_pipeline.py``），
   写回路径沿用既有单事务 fail-soft（rollback + ``written_back=false`` + WARNING）。

2. **第 4 白名单 `GET /api/v1/charts/types`**：前后端 + 登记侧 + 导出侧共 4 份
   「7 类图表」白名单，前三份已有 parity 护栏（``test_chart_json_registration_parity``
   的 ``_ALL_CHART_TYPES`` ↔ ``PIL_RENDERABLE_CHART_TYPES``、前端
   ``chartTypesParity.test.tsx``），唯独本端点此前零断言 —— 硬编码 7 条漏改即
   静默漂移（用户在类型筛选里看到的集合与实际可渲染集合不一致）。

3. **导出预检中文标签**：前端 ``SchemeWorkbenchPage.EXPORT_ISSUE_LABEL`` 只有 7 类，
   后端 ``export._EXPORT_ISSUE_RULE_MAP`` 有 16 类 —— 缺标签时界面回退显示英文
   裸代号（``global_facts_blocked：…``）。parity 由**后端** pytest 读前端源文件锁定
   （对齐既有 ``TestExportGateHighSeverityParity`` 的做法；前端测试不便读盘，
   见 AGENTS.md §6「本仓前端未装 @types/node」）。
"""
import ast
import re
from pathlib import Path

import pytest
from fastapi import HTTPException

REPO_ROOT = Path(__file__).resolve().parents[2]
FRONT_PAGE = REPO_ROOT / "frontend" / "src" / "pages" / "SchemeWorkbenchPage.tsx"
FRONT_CHART_TYPES = REPO_ROOT / "frontend" / "src" / "utils" / "chartTypes.ts"


class _NoneDB:
    """execute() 恒返回 None 的 DB 桩（模拟 R13 的连接/事务异常态）。"""

    async def execute(self, sql, *args, **kwargs):
        return None

    async def commit(self):
        return None

    async def rollback(self):
        return None


# ============================================================
# 1) R13 判空
# ============================================================
class TestChartsR13Guards:
    """读路径 fail-closed 503、写回路径 fail-soft（既有单事务语义）。"""

    async def test_list_charts_none_cursor_is_503_not_500(self):
        """清单端点：cur 为 None 必须 503，不得 AttributeError → 500。

        也不得返回空清单 —— 那会把数据库瞬时故障伪装成「本方案没有图表」
        （同 R39 ①「把故障伪装成业务空数据」的教训）。
        """
        from app.routers.charts import list_charts
        with pytest.raises(HTTPException) as ei:
            await list_charts("scheme-r13", db=_NoneDB())
        assert ei.value.status_code == 503

    async def test_load_section_none_cursor_is_503(self):
        from app.routers.charts import _load_section
        with pytest.raises(HTTPException) as ei:
            await _load_section(_NoneDB(), "sec-r13")
        assert ei.value.status_code == 503

    async def test_generate_ai_image_none_cursor_is_503(self, monkeypatch):
        """AI 配图端点：读章节失败必须 503，且**不得**降级成 404「章节不存在」
        （否则用户会去找章节而不是排查数据库）。"""
        from app.config import settings
        from app.routers.charts import generate_ai_image

        monkeypatch.setattr(settings, "ai_image_manual_enabled", True)
        with pytest.raises(HTTPException) as ei:
            await generate_ai_image({"section_id": "sec-r13"}, db=_NoneDB())
        assert ei.value.status_code == 503

    async def test_generate_ai_image_default_gate_still_409(self, monkeypatch):
        """反向：默认 ai_image_manual_enabled=False 时仍在 409 门禁处拒绝
        （守卫不得把 v17「无人工生图入口」约束顶掉）。"""
        from app.config import settings
        from app.routers.charts import generate_ai_image

        monkeypatch.setattr(settings, "ai_image_manual_enabled", False)
        with pytest.raises(HTTPException) as ei:
            await generate_ai_image({"section_id": "sec-r13"}, db=_NoneDB())
        assert ei.value.status_code == 409

    async def test_fix_mermaid_writeback_none_cursor_fails_soft(
            self, db_conn, monkeypatch, caplog):
        """写回路径：SELECT 返回 None → 沿用单事务 fail-soft

        （rollback + written_back=false + WARNING），不得让 AttributeError
        冒出变成 500（那会连带丢掉 AI 已修好的 code 预览）。同时**不得**
        谎报 written_back=True。"""
        import logging

        import app.routers.charts as charts_mod
        from app.routers.charts import fix_mermaid
        from app.services.chart_payload import build_chart_envelope

        await db_conn.execute(
            "INSERT INTO chart_predictions (id, section_id, scheme_id, chart_type,"
            " needed, purpose, priority, status, data_json) VALUES"
            " ('pid-r13','sec1','s1','flowchart',1,'测试',5,'done',?)",
            (build_chart_envelope(code="graph TD\n    A --> B", title="流程"),))
        await db_conn.commit()

        async def _fake_chat(messages, **kw):
            return "graph TD\n    A --> B\n    B --> C"
        monkeypatch.setattr(charts_mod, "chat_with_fallback", _fake_chat)

        class _WriteBackNoneDB:
            """仅 chart_predictions 的回写 SELECT 返回 None，其余委托真实连接。"""

            def __init__(self, real):
                self._real = real

            async def execute(self, sql, *args, **kwargs):
                if sql.strip().startswith("SELECT id, data_json FROM chart_predictions"):
                    return None
                return await self._real.execute(sql, *args, **kwargs)

            async def commit(self):
                return await self._real.commit()

            async def rollback(self):
                return await self._real.rollback()

        with caplog.at_level(logging.WARNING, logger="app.routers.charts"):
            res = await fix_mermaid(
                {"code": "graph TD\n    A --> B", "prediction_id": "pid-r13"},
                db=_WriteBackNoneDB(db_conn))

        # 响应契约：AI 修复结果照常回传（validated），但写回如实为 false
        assert res["validated"] is True
        assert "B --> C" in res["code"]
        assert res["written_back"] is False
        assert res["content_updated"] is False
        # 库里必须仍是坏代码（证明没有半截写入 / 没有谎报）
        cur = await db_conn.execute(
            "SELECT data_json FROM chart_predictions WHERE id='pid-r13'")
        stored = (await cur.fetchone())[0]
        assert "B --> C" not in stored
        # 可诊断性：日志点名 R13，而不是含糊的 'NoneType' AttributeError
        assert any("R13" in r.message for r in caplog.records), (
            "写回失败日志未点名 R13，排障时无法区分数据库故障与修复失败")

    async def test_fix_mermaid_unvalidated_result_never_writes(
            self, db_conn, monkeypatch):
        """未过校验的候选一律不写库（既有契约回归锁，防止本轮守卫改动顺手放宽）。"""
        import app.routers.charts as charts_mod
        from app.routers.charts import fix_mermaid

        async def _bad_chat(messages, **kw):
            return "这不是合法的 Mermaid 也不是 JSON"
        monkeypatch.setattr(charts_mod, "chat_with_fallback", _bad_chat)

        res = await fix_mermaid({"code": "graph TD\n    A --> B"}, db=db_conn)
        assert res["validated"] is False
        assert res["written_back"] is False


class TestEveryCursorAssignIsGuarded:
    """静态锁：charts.py 内**每一处** `cur = await db.execute(...)` 要么

    被 `if <name> is None` 显式守卫、要么位于 try 块内（由既有 except 兜底）。
    防止后续新增查询点悄悄退回 R13 裸取 —— 与 ``TestRowcountIsCentralized`` 同思路
    （静态扫仓比逐个用例枚举更能覆盖「未来新增」）。
    """

    @staticmethod
    def _unguarded_assigns(source: str) -> list[str]:
        tree = ast.parse(source)
        parents: dict = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parents[child] = parent

        def _in_try(node) -> bool:
            cur = node
            while cur in parents:
                cur = parents[cur]
                if isinstance(cur, ast.Try):
                    return True
            return False

        def _uses_name(node, name: str) -> bool:
            return any(isinstance(n, ast.Name) and n.id == name
                       for n in ast.walk(node))

        def _is_execute_assign(node) -> bool:
            if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Await):
                return False
            call = node.value.value
            return (isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "execute")

        def _ternary_guard(node, name: str) -> bool:
            """`await cur.fetchall() if cur is not None else []` 形态的**行内守卫**。

            全仓多处采用该写法（如 prompts.py::list_prompts），与 `if cur is None:`
            语句形态等价，必须同样被认作已守卫 —— 否则静态锁会大面积假阳性，
            逼着后人删护栏（AGENTS.md §5.14「护栏判据选错锚点」）。
            """
            for n in ast.walk(node):
                if not isinstance(n, ast.IfExp):
                    continue
                t = n.test
                if isinstance(t, ast.Compare) and isinstance(t.left, ast.Name) \
                        and t.left.id == name \
                        and any(isinstance(o, (ast.Is, ast.IsNot)) for o in t.ops):
                    return True
            return False

        def _has_guard(node, name: str) -> bool:
            """赋值之后、**触碰该游标之前**是否存在 `if <name> is None:`。

            ⚠️ A/B 反向验证实测：若把判据放宽为「同一函数体内任意位置存在该守卫」，
            摘掉守卫后**不会**被检出 —— 同一函数里游标被多次赋值，后一处守卫会被
            当成前一处的守卫（假阴性 = 护栏架空）。故必须按**语句顺序**扫描，
            遇到先使用游标 / 新一轮 execute 赋值 / 函数结束均判为未守卫。
            """
            stmt = node
            container = None
            while stmt in parents:
                parent = parents[stmt]
                for field in ("body", "orelse", "finalbody", "handlers"):
                    lst = getattr(parent, field, None)
                    if isinstance(lst, list) and stmt in lst:
                        container = lst
                        break
                if container is not None:
                    break
                stmt = parent
            if container is None:
                return False
            for later in container[container.index(stmt) + 1:]:
                if _ternary_guard(later, name):
                    return True
                if isinstance(later, ast.If):
                    t = later.test
                    if isinstance(t, ast.Compare) and isinstance(t.left, ast.Name) \
                            and t.left.id == name \
                            and any(isinstance(o, (ast.Is, ast.IsNot)) for o in t.ops):
                        return True
                    if _uses_name(later, name):
                        return False
                    continue
                if _is_execute_assign(later):
                    return False  # 尚未守卫就换了下一条查询
                if _uses_name(later, name):
                    return False  # 先用了游标（.fetchall()/.fetchone()）才守卫 = 无效
            return False

        bad: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Await):
                continue
            call = node.value.value
            if not isinstance(call, ast.Call):
                continue
            func = call.func
            if not (isinstance(func, ast.Attribute) and func.attr == "execute"):
                continue
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if not names:
                continue
            if _in_try(node):
                continue  # 由既有 except 接住（fail-soft + 日志）
            if not all(_has_guard(node, nm) for nm in names):
                line = getattr(node, "lineno", 0)
                bad.append(f"L{line}: {'/'.join(names)}")
        return bad

    def test_no_unguarded_execute_in_charts_router(self):
        src = (REPO_ROOT / "backend" / "app" / "routers" / "charts.py").read_text(
            encoding="utf-8")
        bad = self._unguarded_assigns(src)
        assert not bad, (
            "charts.py 存在未判空的 db.execute 游标赋值（R13 回归）：" + ", ".join(bad))

    def test_guard_detector_itself_catches_bare_assign(self):
        """护栏防空转：去掉守卫的样本必须被检出（否则静态锁等于没有）。"""
        bare = (
            "async def f(db):\n"
            "    cur = await db.execute('SELECT 1')\n"
            "    row = await cur.fetchone()\n"
            "    return row\n"
        )
        assert self._unguarded_assigns(bare) == ["L2: cur"]
        guarded = (
            "async def f(db):\n"
            "    cur = await db.execute('SELECT 1')\n"
            "    if cur is None:\n"
            "        return None\n"
            "    row = await cur.fetchone()\n"
            "    return row\n"
        )
        assert self._unguarded_assigns(guarded) == []
        in_try = (
            "async def f(db):\n"
            "    try:\n"
            "        cur = await db.execute('SELECT 1')\n"
            "        row = await cur.fetchone()\n"
            "    except Exception:\n"
            "        return None\n"
            "    return row\n"
        )
        assert self._unguarded_assigns(in_try) == []
        ternary = (
            "async def f(db):\n"
            "    cur = await db.execute('SELECT 1')\n"
            "    rows = await cur.fetchall() if cur is not None else []\n"
            "    return rows\n"
        )
        assert self._unguarded_assigns(ternary) == [], "行内（三元）守卫被误判为未守卫"
        use_before_check = (
            "async def f(db):\n"
            "    cur = await db.execute('SELECT 1')\n"
            "    rows = await cur.fetchall()\n"
            "    if cur is None:\n"
            "        return []\n"
            "    return rows\n"
        )
        assert self._unguarded_assigns(use_before_check) == ["L2: cur"], (
            "先用游标后判空必须被检出（守卫来得太晚 = 形同虚设）")


# ============================================================
# 2) 第 4 白名单：GET /charts/types
# ============================================================
class TestChartTypesEndpointParity:
    """`GET /api/v1/charts/types` 的 key/label 必须与三侧白名单同口径。"""

    async def _payload(self):
        from app.routers.charts import list_chart_types
        return await list_chart_types()

    async def test_keys_match_all_whitelists(self):
        from app.routers._chart_pipeline import _ALL_CHART_TYPES
        from app.services.chart_validators import PIL_RENDERABLE_CHART_TYPES

        payload = await self._payload()
        keys = {t["key"] for t in payload["types"]}
        assert keys == set(_ALL_CHART_TYPES), (
            f"/types 端点与登记侧白名单漂移：端点={sorted(keys)} "
            f"登记侧={sorted(_ALL_CHART_TYPES)}")
        assert keys == set(PIL_RENDERABLE_CHART_TYPES)
        assert len(keys) == 7, "7 类图表值域被改动（AGENTS.md §4.3 唯一值域）"

    async def test_labels_match_chart_type_labels(self):
        from app.services.chart_validators import CHART_TYPE_LABELS

        payload = await self._payload()
        labels = {t["key"]: t["label"] for t in payload["types"]}
        assert labels == CHART_TYPE_LABELS, (
            f"/types 中文名与 CHART_TYPE_LABELS 漂移：{labels} vs {CHART_TYPE_LABELS}")

    async def test_descriptions_non_empty_and_unique(self):
        payload = await self._payload()
        descs = [t.get("description", "") for t in payload["types"]]
        assert all(d.strip() for d in descs), "存在空 description"
        assert len(set(descs)) == len(descs), "description 重复（疑似复制粘贴未改）"

    def test_frontend_whitelist_matches(self):
        """前端 `chartTypes.ts::RENDERABLE_CHART_TYPES` 必须与后端同 7 类
        （端点自身与后端一致性由上面两条断言传递保证，三者两两同值）。"""
        src = FRONT_CHART_TYPES.read_text(encoding="utf-8")
        m = re.search(r"RENDERABLE_CHART_TYPES[^=]*=\s*new Set\(\[(.*?)\]\)",
                      src, re.S)
        assert m, "未在 chartTypes.ts 找到 RENDERABLE_CHART_TYPES 定义"
        front_keys = set(re.findall(r'"([a-z_]+)"', m.group(1)))

        from app.routers._chart_pipeline import _ALL_CHART_TYPES
        assert front_keys == set(_ALL_CHART_TYPES), (
            f"前后端白名单漂移：前端={sorted(front_keys)} 后端={sorted(_ALL_CHART_TYPES)}")


# ============================================================
# 3) 导出预检中文标签 parity（后端 pytest 读前端源文件）
# ============================================================
class TestExportIssueLabelParity:
    """前端 EXPORT_ISSUE_LABEL 必须覆盖后端 `_EXPORT_ISSUE_RULE_MAP` 全部类型。"""

    @staticmethod
    def _frontend_labels() -> dict[str, str]:
        src = FRONT_PAGE.read_text(encoding="utf-8")
        m = re.search(
            r"const EXPORT_ISSUE_LABEL[^=]*=\s*\{(.*?)\n\};", src, re.S)
        assert m, "未在 SchemeWorkbenchPage.tsx 找到 EXPORT_ISSUE_LABEL 定义"
        pairs = re.findall(r'([A-Za-z_][A-Za-z0-9_]*)\s*:\s*"([^"]*)"', m.group(1))
        return dict(pairs)

    def test_every_backend_type_has_chinese_label(self):
        from app.routers.export import _EXPORT_ISSUE_RULE_MAP

        labels = self._frontend_labels()
        missing = set(_EXPORT_ISSUE_RULE_MAP) - set(labels)
        assert not missing, (
            "导出预检问题类型缺中文标签（界面会显示英文裸代号）：" + ", ".join(sorted(missing)))

    def test_labels_are_chinese(self):
        """标签必须是中文 —— 防止用英文代号「补全」使 parity 形同虚设。"""
        cjk = re.compile(r"[\u4e00-\u9fff]")
        for key, label in self._frontend_labels().items():
            assert cjk.search(label), f"{key} 的标签不是中文：{label!r}"

    def test_label_source_matches_parity_guard_pointer(self):
        """注释必须指向本护栏文件 —— 后端新增 issue 类型时作者能找到入口。"""
        src = FRONT_PAGE.read_text(encoding="utf-8")
        assert "test_charts_r13_and_types_parity_20261004" in src
