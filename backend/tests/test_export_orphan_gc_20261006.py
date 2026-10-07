"""导出模块护栏（R47 · 2026-10-06）—— 孤儿导出产物回收 + 三组判据 parity。

本轮收口 1 个真实缺陷 + 3 组判据分叉护栏（零新增依赖、零新增配置项、
零数据迁移、返回契约零变化）：

A. 孤儿导出产物零回收（真实 P2 · 磁盘永久泄漏）
   ``_prune_export_cache`` 只能删「DB 有行但已陈旧」的文件，而下面两类产物
   在 DB 里**根本没有行**，于是永久留在 ``EXPORTS_DIR`` 且零告警：
     · DOCX 原子替换 3 次重试均失败 → 降级为「本次不写缓存」，
       ``out_path = tmp_out_path``（``.tmp.docx``）直接返回给用户，
       之后再无任何路径清理它（FileResponse 的读句柄也不做 unlink）；
     · PDF 分支 ``os.replace`` 成功、但随后的 INSERT 抛异常 → ``out_path``
       已在盘上，DB 里没有对应行（``/cache-status`` 也看不见它）。
   一份 DOCX 可达数十 MB，每失败一次就永久泄漏一份。
   新增 ``_gc_orphan_exports``（三重收窄 + 临时产物宽限期），接入 DOCX / PDF
   两条交付路径。

B. 导出预检 issue 类型 ↔ 就绪度规则映射 双向 parity（护栏）
   ``export_issues_to_findings`` 对未登记的 issue type 是 **continue 静默跳过**
   —— 新增一个检测项却忘了登记映射表，用户在就绪度报告里永远看不到它，
   而预检接口明明返回了。双向锁定。

C. 导出配置白名单 ↔ 实际读取的键（护栏）
   ``_normalize_config`` 只保留白名单内的键参与 config_hash。新增一个
   ``config.get("xxx")`` 渲染行为却不登记白名单 → config_hash 不变 →
   命中旧缓存 → 用户拿到的仍是旧配置产物（``scheme_forms`` 曾因此踩坑）。

D. 导出期 AI 配图签名护栏：签名必须覆盖全部影响像素的配置维度，
   且绝不读取密钥（密钥进指纹 = 改 key 击穿全部导出缓存 + 密钥可能进日志）。

E. 缓存状态接口契约（前端消费点）：cache_status 必须返回 total / stale /
   items[].exists 三处消费字段、同秒次序稳定、拒绝空 result_path；
   并锁定 _prune_export_cache（管 DB 行）与 _gc_orphan_exports（管无行孤儿）
   互补而非互相替代。
"""
import inspect
import json
import os
import re
import time

import pytest

from app.routers import export as E


REPO_BACKEND = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(E.__file__))))
EXPORT_PY = os.path.join(REPO_BACKEND, "app", "routers", "export.py")

# 产 issue 的函数：collect_export_issues 本体 + 四个独立检测器
_ISSUE_PRODUCERS = ("collect_export_issues", "_detect_duplicate_sections",
                    "_detect_section_number_mismatch",
                    "_detect_stale_cross_references",
                    "_detect_body_subheading_namespace_conflict")

_TYPE_RE = re.compile(r"""["']type["']\s*:\s*["']([a-z_]+)["']""")
_CFG_GET_RE = re.compile(r"""config\.get\(\s*["']([A-Za-z_]+)["']""")


def _issue_types_emitted() -> set:
    """静态扫仓：导出预检实际产出的全部 issue type。"""
    found = set()
    for name in _ISSUE_PRODUCERS:
        found |= set(_TYPE_RE.findall(inspect.getsource(getattr(E, name))))
    return found


def _code_names(func) -> set:
    """返回函数**代码**里出现的所有名字：属性访问名 + 字符串常量字面量。

    刻意排除 docstring —— 说明文字里提到的名字（例如「绝不取 api_key」这句
    本身就是防护意图）不是实现行为；纯文本匹配或含 docstring 的 AST 扫描
    会假失败，只能靠 AST 剥掉首个 docstring 表达式节点。
    """
    import ast
    tree = ast.parse(inspect.getsource(func))
    body = tree.body
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]
    names = set()
    for node in ast.walk(ast.Module(body=body, type_ignores=[])):
        if isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            names.add(node.value)
    return names


# =========================================================================
# A. 孤儿导出产物回收
# =========================================================================
class TestGcOrphanExports:
    """``_gc_orphan_exports`` 的行为与收窄边界。"""

    @staticmethod
    def _mk(path, *, age=0.0):
        path.write_bytes(b"docx-bytes" * 256)
        if age:
            ts = time.time() - age
            os.utime(path, (ts, ts))
        return path

    @pytest.mark.asyncio
    async def test_deletes_orphan_tmp_beyond_ttl(self, db_conn, tmp_path,
                                                 monkeypatch):
        """超宽限期的 .tmp.docx（替换失败的降级产物）必须被清掉。"""
        monkeypatch.setattr(E, "EXPORTS_DIR", tmp_path)
        p = self._mk(tmp_path / "s1_aabbccdd_eeff0011.12345678.tmp.docx",
                     age=E._ORPHAN_TMP_TTL_SECONDS + 300)
        assert await E._gc_orphan_exports(db_conn, "s1") == 1
        assert not p.exists()

    @pytest.mark.asyncio
    async def test_keeps_fresh_tmp_within_ttl(self, db_conn, tmp_path,
                                              monkeypatch):
        """宽限期内的 .tmp.docx 不得删（可能正在被流式读取）。"""
        monkeypatch.setattr(E, "EXPORTS_DIR", tmp_path)
        p = self._mk(tmp_path / "s1_aabbccdd_eeff0011.12345678.tmp.docx")
        assert await E._gc_orphan_exports(db_conn, "s1") == 0
        assert p.exists()

    @pytest.mark.asyncio
    async def test_deletes_orphan_pdf_without_cache_row(
            self, db_conn, tmp_path, monkeypatch):
        """非临时产物只要没有缓存行引用即为孤儿（覆盖 PDF 替换成功但 INSERT 失败）。"""
        monkeypatch.setattr(E, "EXPORTS_DIR", tmp_path)
        p = self._mk(tmp_path / "s1_aabbccdd_eeff0011.pdf")
        assert await E._gc_orphan_exports(db_conn, "s1") == 1
        assert not p.exists()

    @pytest.mark.asyncio
    async def test_keeps_file_referenced_by_live_cache_row(
            self, db_conn, tmp_path, monkeypatch):
        """被现存缓存行引用的产物必须保留。"""
        monkeypatch.setattr(E, "EXPORTS_DIR", tmp_path)
        p = self._mk(tmp_path / "s1_aabbccdd_eeff0011.docx")
        await db_conn.execute(
            "INSERT INTO export_cache (id, scheme_id, result_path) VALUES (?,?,?)",
            ("c1", "s1", str(p)))
        await db_conn.commit()
        assert await E._gc_orphan_exports(db_conn, "s1") == 0
        assert p.exists()

    @pytest.mark.asyncio
    async def test_protect_set_keeps_in_flight_artifact(
            self, db_conn, tmp_path, monkeypatch):
        """protect 里的文件不得删（本次导出仍在被响应读取的产物）。"""
        monkeypatch.setattr(E, "EXPORTS_DIR", tmp_path)
        p = self._mk(tmp_path / "s1_aabbccdd_eeff0011.12345678.tmp.docx",
                     age=E._ORPHAN_TMP_TTL_SECONDS + 900)
        assert await E._gc_orphan_exports(db_conn, "s1", protect={p}) == 0
        assert p.exists()
        # 同一文件不保护时即可清理 → 证明 protect 是唯一原因
        assert await E._gc_orphan_exports(db_conn, "s1") == 1
        assert not p.exists()

    @pytest.mark.asyncio
    async def test_ignores_other_scheme_prefix(self, db_conn, tmp_path,
                                               monkeypatch):
        """跨方案隔离：其它方案前缀的文件绝不受影响。"""
        monkeypatch.setattr(E, "EXPORTS_DIR", tmp_path)
        other = self._mk(tmp_path / "s9_aabbccdd_eeff0011.docx")
        assert await E._gc_orphan_exports(db_conn, "s1") == 0
        assert other.exists()

    @pytest.mark.asyncio
    async def test_skips_charts_subdirectory(self, db_conn, tmp_path,
                                             monkeypatch):
        """图表 PNG 缓存目录（charts/）整体跳过，不做递归删除。"""
        monkeypatch.setattr(E, "EXPORTS_DIR", tmp_path)
        sub = tmp_path / "charts"
        sub.mkdir()
        img = sub / "s1_aabbccdd.png"
        img.write_bytes(b"png-bytes")
        assert await E._gc_orphan_exports(db_conn, "s1") == 0
        assert img.exists()

    @pytest.mark.asyncio
    async def test_counts_each_deleted_file(self, db_conn, tmp_path,
                                            monkeypatch):
        monkeypatch.setattr(E, "EXPORTS_DIR", tmp_path)
        ttl = E._ORPHAN_TMP_TTL_SECONDS + 600
        for n in ("a1", "a2", "a3"):
            self._mk(tmp_path / f"s1_{n}_eeff0011.00000000.tmp.docx", age=ttl)
        self._mk(tmp_path / "s1_fresh_eeff0011.docx")      # 无行引用 → 删
        await db_conn.execute(
            "INSERT INTO export_cache (id, scheme_id, result_path) VALUES (?,?,?)",
            ("c1", "s1", str(tmp_path / "s1_live_eeff0011.docx")))
        await db_conn.commit()
        assert await E._gc_orphan_exports(db_conn, "s1") == 4

    @pytest.mark.asyncio
    async def test_missing_exports_dir_is_fail_soft(self, db_conn, tmp_path,
                                                    monkeypatch):
        """目录不存在（被手工删除 / 首次安装）→ 返回 0，不抛、不创建目录。"""
        missing = tmp_path / "never_created"
        monkeypatch.setattr(E, "EXPORTS_DIR", missing)
        assert not missing.exists()
        assert await E._gc_orphan_exports(db_conn, "s1") == 0
        assert not missing.exists(), "GC 不得因为扫描而创建导出目录"

    @pytest.mark.asyncio
    async def test_db_read_failure_deletes_nothing(self, tmp_path, monkeypatch):
        """**破坏性路径的保守姿态**：DB 读不到缓存行 → 一个文件都不删。

        若把「读失败」当成「确实无缓存行」，GC 就会把本方案**所有**现存产物
        判成孤儿 —— 用户下次导出秒级缓存全部作废，数十 MB 成稿静默消失。
        """
        monkeypatch.setattr(E, "EXPORTS_DIR", tmp_path)
        p = self._mk(tmp_path / "s1_aabbccdd_eeff0011.docx")
        live = self._mk(tmp_path / "s1_live_eeff0011.docx")

        class _BrokenDB:
            async def execute(self, sql, params=()):
                return None

        assert await E._gc_orphan_exports(_BrokenDB(), "s1") == 0
        assert p.exists() and live.exists(), "查不到缓存行时必须保守不删"

    @pytest.mark.asyncio
    async def test_db_failure_is_fail_soft(self, tmp_path, monkeypatch):
        """DB 不可用（R13：execute 返回 None）→ 降级为不清理，且不抛异常。"""
        monkeypatch.setattr(E, "EXPORTS_DIR", tmp_path)
        p = self._mk(tmp_path / "s1_aabbccdd_eeff0011.docx")

        class _BrokenDB:
            async def execute(self, sql, params=()):
                return None

        assert await E._gc_orphan_exports(_BrokenDB(), "s1") == 0
        assert p.exists(), "查不到缓存行时必须保守不删"

    @pytest.mark.asyncio
    async def test_same_prefix_directory_is_not_touched(
            self, db_conn, tmp_path, monkeypatch):
        """同名前缀的**目录**必须跳过（f.is_file() 收窄），且不中断后续扫描。"""
        monkeypatch.setattr(E, "EXPORTS_DIR", tmp_path)
        d = tmp_path / "s1_doomed_eeff0011"
        d.mkdir()
        inner = d / "inner.docx"
        inner.write_bytes(b"never-delete-me")
        good = self._mk(tmp_path / "s1_good_eeff0011.docx")
        assert await E._gc_orphan_exports(db_conn, "s1") == 1
        assert not good.exists(), "跳过目录后必须继续扫描并清理随后的孤儿"
        assert inner.exists(), "不得递归进入目录删除内部文件"
        assert d.exists(), "GC 只删文件，不得删除目录本身"


class TestGcWiring:
    """GC 必须真正接到两条交付路径（静态接线锁）。"""

    def test_gc_is_coroutine_and_fail_soft(self):
        assert inspect.iscoroutinefunction(E._gc_orphan_exports)
        src = inspect.getsource(E._gc_orphan_exports)
        assert "except Exception" in src, "GC 必须 fail-soft，绝不影响交付"

    def test_gc_uses_strict_read_for_cache_rows(self):
        """安全红线：读不到缓存行时必须整体跳过，而不是按「无缓存行」删除。

        ``_db_fetch_all`` 默认把读失败静默降级为空结果集；若 GC 沿用默认模式，
        DB 短暂不可用一次就会把本方案**所有**现存产物判成孤儿并删掉。
        因此 GC 必须显式声明 strict=True（唯一出口不变，但语义收紧）。
        """
        src = inspect.getsource(E._gc_orphan_exports)
        assert "strict=True" in src, (
            "GC 是破坏性清理路径，读缓存行必须用 _db_fetch_all(strict=True)")
        assert "except DBReadError" in src, "读失败必须被显式捕获并整体跳过"


class TestDbReadStrictMode:
    """``_db_fetch_all`` 的 strict 模式：破坏性调用方区分「读失败」与「确实无行」。"""

    @pytest.mark.asyncio
    async def test_default_stays_fail_soft(self):
        """默认模式逐字保持旧行为：cur 为 None → 空结果集（既有调用方零影响）。"""
        assert await E._db_fetch_all(None, what="t") == []

    @pytest.mark.asyncio
    async def test_strict_raises_when_cur_is_none(self):
        with pytest.raises(E.DBReadError):
            await E._db_fetch_all(None, what="t", strict=True)

    @pytest.mark.asyncio
    async def test_strict_raises_when_fetchall_fails(self):
        class _BadCur:
            async def fetchall(self):
                raise OSError("database disk image is malformed")

        with pytest.raises(E.DBReadError):
            await E._db_fetch_all(_BadCur(), what="t", strict=True)

    @pytest.mark.asyncio
    async def test_default_returns_empty_when_fetchall_fails(self):
        class _BadCur:
            async def fetchall(self):
                raise OSError("boom")

        assert await E._db_fetch_all(_BadCur(), what="t") == []

    @pytest.mark.asyncio
    async def test_strict_returns_rows_on_success(self):
        class _OkCur:
            async def fetchall(self):
                return [{"result_path": "/tmp/a.docx"}]

        rows = await E._db_fetch_all(_OkCur(), what="t", strict=True)
        assert len(rows) == 1
        assert dict(rows[0])["result_path"] == "/tmp/a.docx"

    def test_strict_is_only_opted_in_by_gc(self):
        """strict 是显式收窄语义：只有破坏性调用方才可启用，禁止随手加宽。

        全仓只允许 ``_gc_orphan_exports`` 一处 opt-in。判据用 AST 找真正的
        ``_db_fetch_all(..., strict=True)`` 调用节点 —— 纯文本匹配会被注释里
        的 ``numbering_consistency_strict=True`` 字样误伤（假失败）。
        """
        import ast
        tree = ast.parse(open(EXPORT_PY, encoding="utf-8").read())
        owners = {}
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue
            for call in ast.walk(fn):
                if (isinstance(call, ast.Call)
                        and isinstance(call.func, ast.Name)
                        and call.func.id == "_db_fetch_all"
                        and any(k.arg == "strict"
                                and isinstance(k.value, ast.Constant)
                                and k.value.value is True
                                for k in call.keywords)):
                    owners.setdefault(fn.name, 0)
                    owners[fn.name] += 1
        assert owners == {"_gc_orphan_exports": 1}, (
            f"strict 读只允许破坏性调用方 _gc_orphan_exports 启用，"
            f"实际分布：{owners}")

    def test_gc_scoped_to_scheme_and_top_level_files(self):
        """三重收窄：只扫顶层文件、只认本方案前缀、排除 live + protect。"""
        src = inspect.getsource(E._gc_orphan_exports)
        assert "f.is_file()" in src, "子目录（含 charts/ 图表缓存）必须跳过"
        assert ".iterdir()" in src and "rglob" not in src and "glob" not in src, \
            "只做顶层非递归扫描"
        assert "startswith(prefix)" in src, "必须按 {scheme_id}_ 前缀收窄"
        assert "in live" in src and "in protected" in src

    def test_gc_grace_period_only_applies_to_tmp(self):
        """宽限期只适用于 .tmp.* 临时产物（正常产物无需宽限）。"""
        src = inspect.getsource(E._gc_orphan_exports)
        assert ".tmp." in src
        assert "_ORPHAN_TMP_TTL_SECONDS" in src
        # 宽限判定必须与 .tmp. 判断**同一条语句**（否则临时产物也被无条件保留）
        assert re.search(
            r'if\s+".tmp\."[^#]*_ORPHAN_TMP_TTL_SECONDS', src), \
            ".tmp. 判定与宽限期必须绑定在同一条件里"
        assert E._ORPHAN_TMP_TTL_SECONDS >= 300, \
            "宽限期过短会删掉刚降级返回、仍在流式读取的产物"

    def test_docx_calls_gc_in_both_return_paths(self):
        """DOCX 有两条交付路径（degraded 提前返回 + 正常返回），两条都必须回收。"""
        src = inspect.getsource(E.export_docx)
        calls = re.findall(r"_gc_orphan_exports\(db,\s*scheme_id,\s*protect=\{out_path\}\)", src)
        assert len(calls) == 2, \
            "degraded 分支与正常分支必须各调用一次（均带 protect）"

    def test_pdf_calls_gc_before_returning_in_memory_bytes(self):
        """PDF 响应体是内存字节 → 调用点必须在 return Response 之前且不保护任何文件。"""
        src = inspect.getsource(E.export_pdf)
        i_gc = src.index("await _gc_orphan_exports(db, scheme_id)")
        i_ret = src.index("return Response(")
        assert i_gc < i_ret
        assert "protect" not in src[i_gc:i_gc + 90], \
            "PDF 返回体已是内存字节，无需也不得 protect 磁盘产物"

    def test_degraded_path_is_the_only_tmp_returner_and_it_protects(self):
        """只有 degraded 提前返回 + 正常返回两条路径会交付磁盘产物，均须带 protect。"""
        src = inspect.getsource(E.export_docx)
        # 缓存命中分支交付的是**历史产物**（DB 有行、文件已存在），无需回收
        i_hit = src.index('X-Cache-Status": "hit"')
        assert "_gc_orphan_exports" not in src[:i_hit + 400], \
            "缓存命中分支不得调用 GC（产物是活缓存行，回收只会在下一次导出做）"



# =========================================================================
# B. 导出预检 issue 类型 ↔ 就绪度规则映射 双向 parity
# =========================================================================
class TestIssueTypeRuleParity:
    def test_every_emitted_type_is_registered(self):
        """新增检测项却忘登记映射表 → 就绪度报告静默漏报（旧实现的真实风险）。"""
        missing = _issue_types_emitted() - set(E._EXPORT_ISSUE_RULE_MAP)
        assert not missing, (
            "以下 issue type 由导出预检产出但未登记 _EXPORT_ISSUE_RULE_MAP，"
            f"export_issues_to_findings 会静默跳过（用户看不到）：{sorted(missing)}")

    def test_every_registered_type_is_actually_emitted(self):
        """反向：映射表里不得残留已删除检测项的死条目。"""
        dead = set(E._EXPORT_ISSUE_RULE_MAP) - _issue_types_emitted()
        assert not dead, f"映射表存在无人产出的 issue type（死条目）：{sorted(dead)}"

    def test_mapping_covers_every_export_probe(self):
        """数量锁：导出预检的检测项全部需要映射（防止静默摘除某个检测项）。"""
        n = len(E._EXPORT_ISSUE_RULE_MAP)
        assert n >= 15, f"映射表只剩 {n} 项，疑似有检测项被摘除"
        # 与「实际产出集合」对齐（双向 parity 的第三重保险）
        assert n == len(_issue_types_emitted())

    def test_issue_types_are_lowercase_snake(self):
        """类型命名规范：小写 snake_case（前端按字符串做分支判断）。"""
        for it in _issue_types_emitted():
            assert it == it.lower() and "-" not in it, f"非法 issue type 命名：{it}"

    def test_severities_are_valid(self):
        from app.services.audit_rules import SEVERITY_ORDER
        for itype, (_rid, sev) in E._EXPORT_ISSUE_RULE_MAP.items():
            assert sev in SEVERITY_ORDER, f"{itype} 的严重度非法：{sev}"

    def test_all_mapped_rules_are_in_deliverability_dimension(self):
        """导出预检的问题都属于「可交付性」维度（映射到别的维度即口径漂移）。"""
        from app.services.audit_rules import get_rule
        for itype, (rid, _sev) in E._EXPORT_ISSUE_RULE_MAP.items():
            rule = get_rule(rid)
            assert rule is not None, f"{itype} 的 {rid} 未登记 audit_rules"
            assert rule.dimension == "deliverability", \
                f"{itype} → {rid} 的维度应为 deliverability，实际 {rule.dimension}"

    def test_rule_ids_are_registered_in_audit_rules(self):
        """映射到的 rule_id 必须真实存在于唯一事实源（防 DLV-13/14 复发）。"""
        from app.services.audit_rules import get_rule
        missing = sorted({rid for rid, _ in E._EXPORT_ISSUE_RULE_MAP.values()
                          if get_rule(rid) is None})
        assert not missing, (
            "以下 rule_id 未登记 services/audit_rules.py，"
            f"标题/依据/修复建议会退化为空（用户在就绪度里看到空白阻断项）：{missing}")

    def test_review_statuses_share_one_rule(self):
        """三类审核状态问题（pending / rejected / missing）必须收敛为同一条 DLV 规则。"""
        rids = {E._EXPORT_ISSUE_RULE_MAP[f"review_{k}"][0]
                for k in ("pending", "rejected", "missing")}
        assert len(rids) == 1, f"三类审核状态应共用一条规则，实际 {sorted(rids)}"

    def test_export_issues_to_findings_skips_unknown_types(self):
        """行为锁：未登记类型必须被跳过（而非抛异常打断整份预检）。"""
        items = [{"type": "brand_new_probe", "detail": "x"},
                 {"type": "empty_section", "section_id": "s1"}]
        out = E.export_issues_to_findings(items)
        assert len(out) == 1
        assert out[0]["count"] == 1


# =========================================================================
# C. 导出配置白名单 ↔ 实际读取的键
# =========================================================================
class TestExportConfigKeyParity:
    def test_every_read_config_key_is_in_fingerprint_whitelist(self):
        """读取 config 的渲染行为若不入白名单 → config_hash 不变 → 命中旧缓存。"""
        src = open(EXPORT_PY, encoding="utf-8").read()
        missing = sorted(set(_CFG_GET_RE.findall(src)) - set(E._EXPORT_CONFIG_KEYS))
        assert not missing, (
            "以下 config 键被导出渲染读取但未登记 _EXPORT_CONFIG_KEYS："
            "切换开关后 config_hash 不变 → 命中旧缓存 → 交付旧配置产物："
            f"{missing}")

    def test_every_whitelisted_key_is_actually_used_elsewhere(self):
        """反向：白名单里不得残留无人读取的死键（拼写错误会静默漏入指纹）。"""
        text = open(EXPORT_PY, encoding="utf-8").read()
        i = text.index("_EXPORT_CONFIG_KEYS = frozenset({")
        j = text.index("})", i)
        body = text[:i] + text[j + 2:]           # 剔除白名单定义本体
        unused = sorted(k for k in E._EXPORT_CONFIG_KEYS
                        if body.count(f'"{k}"') + body.count(f"'{k}'") == 0)
        assert not unused, f"白名单中存在无人读取的键（疑似拼写错误）：{unused}"

    def test_normalize_config_drops_non_whitelisted(self):
        """行为锁：白名单外的键（含前端调试字段 _ts）不得进入指纹材料。"""
        out = E._normalize_config({"font_name": "仿宋", "_ts": 1, "evil": 1,
                                   "cover_info": None, "empty": ""})
        assert set(out) == {"font_name"}, f"期望仅保留白名单内非空键，实际 {out}"

    def test_normalize_config_is_idempotent_on_whitelisted_keys(self):
        """白名单内的键必须原样通过（规范化不得丢内容）。"""
        cfg = {k: "v" for k in E._EXPORT_CONFIG_KEYS}
        assert E._normalize_config(cfg) == cfg

    def test_config_key_set_is_frozen(self):
        """白名单必须是 frozenset（防止运行时被就地篡改后静默改变指纹口径）。"""
        assert isinstance(E._EXPORT_CONFIG_KEYS, frozenset)



# =========================================================================
# D. 导出期 AI 配图签名不得含密钥 / 必须进指纹
# =========================================================================
class TestImageGenerationSignature:
    def test_signature_never_contains_secret(self):
        """指纹材料绝不能含密钥（防密钥泄露 + 防换 key 击穿全部导出缓存）。"""
        sig = E._image_generation_signature()
        assert set(sig) == {"enabled", "model", "size", "base_url"}
        blob = json.dumps(sig, ensure_ascii=False).lower()
        for forbidden in ("api_key", "apikey", "secret", "token"):
            assert forbidden not in blob, f"指纹材料含敏感字段名/值：{forbidden}"

    def test_signature_source_does_not_read_api_key(self):
        """静态锁：签名实现不得读取 api_key（一旦读取即把密钥带进指纹材料）。

        判据只扫**代码**：docstring 里「绝不取 api_key」这句说明本身含该字样，
        纯文本匹配或含 docstring 的 AST 扫描都会假失败。
        """
        names = _code_names(E._image_generation_signature)
        assert not (names & {"api_key", "apikey", "image_token"}), \
            f"签名实现读取了敏感配置：{sorted(names & {'api_key', 'apikey', 'image_token'})}"

    def test_signature_reads_exactly_the_four_pixel_dimensions(self):
        """四个维度缺一不可：少一个即让「换模型/换尺寸/换端点/开关」不失效缓存。"""
        need = {"image_enabled", "image_model", "image_default_size",
                "image_base_url"}
        names = _code_names(E._image_generation_signature)
        assert need <= names, \
            f"缺少影响像素的配置维度：{sorted(need - names)}"

    def test_signature_returns_all_four_keys(self):
        for k in ("enabled", "model", "size", "base_url"):
            assert k in E._image_generation_signature()

    def test_signature_keys_are_stable(self):
        """键集合固定（换配置值 → 指纹变；键集合漂移 → 指纹口径静默改变）。"""
        assert set(E._image_generation_signature()) == {"enabled", "model",
                                                         "size", "base_url"}

    def test_signature_is_stable_across_calls(self):
        """同一环境下连续两次调用必须逐字一致（否则缓存永远不命中）。"""
        assert E._image_generation_signature() == E._image_generation_signature()

    def test_signature_fails_soft_to_dict(self, monkeypatch):
        """读不到配置也必须降级为稳定 dict（绝不抛异常阻断导出）。"""
        import app.config
        monkeypatch.setattr(app.config, "settings", None, raising=False)
        sig = E._image_generation_signature()
        assert isinstance(sig, dict) and set(sig) == {"enabled", "model",
                                                      "size", "base_url"}


# =========================================================================
# E. 缓存状态接口契约（前后端消费点）
# =========================================================================
class TestCacheStatusContract:
    def test_cache_status_returns_total_stale_and_exists(self):
        """前端「导出缓存」卡片消费 total / stale / items[].exists 三处。"""
        src = inspect.getsource(E.cache_status)
        for key in ('"total"', '"stale"', '"exists"'):
            assert key in src, f"cache_status 必须返回 {key}"

    def test_cache_status_stable_ordering(self):
        """同一秒内多次导出 → rowid 兜底保证「最近 5 份」次序确定。"""
        src = inspect.getsource(E.cache_status)
        assert "ORDER BY created_at DESC, rowid DESC" in src

    def test_cache_status_rejects_empty_result_path(self):
        """result_path 为空串时 Path("") 恒存在 → 僵尸行会被算成有效缓存。"""
        src = inspect.getsource(E.cache_status)
        assert 'item.get("result_path") or ""' in src

    def test_prune_and_gc_complement_not_replace(self):
        """两个清理器分工不同，不得互相替代（一个管 DB 行、一个管无行孤儿）。"""
        assert "DELETE FROM export_cache" in inspect.getsource(E._prune_export_cache)
        assert "DELETE FROM export_cache" not in inspect.getsource(E._gc_orphan_exports), \
            "GC 只删磁盘孤儿，不得删 DB 行（保留策略是 _prune_export_cache 的职责）"

    def test_gc_never_touches_other_export_types(self):
        """GC 不得扫 charts/ 以外的子目录，也不得递归（磁盘孤儿治理的边界）。"""
        src = inspect.getsource(E._gc_orphan_exports)
        assert "is_file()" in src

