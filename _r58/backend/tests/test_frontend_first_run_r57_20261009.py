# -*- coding: utf-8 -*-
"""R57 · 前端「首跑」收口护栏（2026-10-09）

本机 Node 解析通道打通后（见 test_review_autofix_closeout_r55_20261008.py 的 I 组
三级出口），**首次**跑通全量前端测试：eslint / tsc / vitest 59 文件 1083 用例。
首跑当场抓到 4 处「写了从没跑过」的缺陷，本文件把它们的修复形态锁死：

F7a factsAdjust.test.tsx        getByText 多重命中 → getAllByText + toHaveLength(2)
F7b ReviewWorkflowPanel.test    includes 子串判定 → 精确标签（旧断言把合法的
                                「复审驳回 / 整改后通过」判成违规，且断言了根本不
                                存在的「重提」标签与未知状态的「复审」按钮）
F7c ReadinessDashboard.test     expect(...).toBe(true), "文案";  逗号运算符 → 死文案
                                + eslint no-unused-expressions error
F7d SchemeWorkbenchPage         let nameRef 只改 .current → eslint prefer-const error
                                （两处 error 足以让 CI 的 npm run lint 退出 1）

设计约束：本文件**不依赖 Node**（静态源码锁 + 纯 Python 单测），与 I 组的「真跑」
层互补 —— 按 R49 结论，形态锁与端到端执行两层缺一不可：前者在任何机器都能红，
后者抓「语法合法但跑不起来」。
"""
import importlib.util
import io
import os
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_FE = _ROOT / "frontend" / "src"

TYPES_AUDIT = _FE / "types" / "audit.ts"
COMPONENT = _FE / "components" / "review" / "ReviewWorkflowPanel.tsx"
T_WORKFLOW = _FE / "tests" / "ReviewWorkflowPanel.test.tsx"
T_FACTS = _FE / "tests" / "factsAdjust.test.tsx"
T_READY = _FE / "tests" / "ReadinessDashboard.test.tsx"
P_WORKBENCH = _FE / "pages" / "SchemeWorkbenchPage.tsx"
T_TOUCHED = (T_WORKFLOW, T_FACTS, T_READY, P_WORKBENCH)


def _src(p: Path) -> str:
    return io.open(str(p), encoding="utf-8").read()


# ===========================================================================
# A · 状态 → 操作按钮映射：跨文件 parity（契约漂移在 CI 之前就该红）
# ===========================================================================
class TestReviewStatusActionParity:

    def test_next_actions_keys_equal_review_status_union(self):
        """NEXT_ACTIONS 的键必须与 ReviewStatus 联合类型**完全一致**。

        少一个键 → 该状态的行只剩「重置」（用户看不出能做什么）；多一个键 →
        按钮指向契约外的状态（后端按状态机拦截 → 报错误导）。两侧都静默。
        """
        t = _src(TYPES_AUDIT)
        i = t.index("ReviewStatus =")
        decl = t[i:t.index(";", i)]
        # 必须先剥掉左侧声明名：首个成员是**空串字面量**，按 | 切时它和
        # 'ReviewStatus =' 同处第一段（'ReviewStatus = ""'），不以引号开头 → 被
        # 静默丢弃 → 契约外的比对反而"通过"。首跑就是这里少了 ""。
        rhs = decl.split("=", 1)[1]
        union = set()
        for chunk in rhs.split("|"):
            chunk = chunk.strip()
            if chunk.startswith('"'):
                union.add(chunk.strip('"'))
        assert union == {"", "pending", "reviewing", "approved", "rejected"}, union

        c = _src(COMPONENT)
        j = c.index("const NEXT_ACTIONS")
        block = c[j:c.index("};", j)]
        keys = set()
        for line in block.splitlines():
            line = line.strip()
            if line.startswith('"":') or line.startswith('"'):
                keys.add(line.split(":")[0].strip().strip('"'))
            elif line.startswith("pending:") or line.startswith("reviewing:") \
                    or line.startswith("approved:") or line.startswith("rejected:"):
                keys.add(line.split(":")[0].strip())
        assert keys == union, (keys, union)

    def test_approved_and_rejected_labels_are_exact_in_test(self):
        """组件里的合法去向标签是「复审驳回」「整改后通过」—— 用例必须按**精确标签**
        断言（旧用例用 includes 子串，把这两个合法标签自己判成违规）。"""
        s = _src(T_WORKFLOW)
        assert s.count('toContain("复审驳回")') == 1
        assert s.count('toContain("整改后通过")') == 1
        assert s.count('t === "通过" || t === "驳回"') == 2
        # 旧的子串判定不得回流
        assert 't.includes("通过")' not in s
        assert 't.includes("驳回")' not in s
        # 「重提」这个标签在组件里根本不存在，旧断言却锁了它
        assert 'includes("重提")' not in s

    def test_unknown_status_degrades_to_reset_only(self):
        """未知/契约外状态（用例拿 skipped 做样本）必须降级为**只有**「重置」：
        组件侧靠 ``NEXT_ACTIONS[...] || []`` 兜底，用例侧锁白名单为空。"""
        c = _src(COMPONENT)
        # 外层必须用单引号：Python 相邻字符串字面量会自动拼接，双引号串里再写
        # 一对双引号会被吃掉，断言目标悄悄变成畸形文本（首跑第二处中招）。
        assert '(NEXT_ACTIONS[r.review_status ?? ""] || [])' in c, "兜底空数组被摘掉"
        s = _src(T_WORKFLOW)
        assert 'const ACTION_LABELS: string[] = ["开始审核", "通过", "驳回", "复审驳回", "整改后通过"];' in s
        assert 'expect(skipped.filter((t) => ACTION_LABELS.includes(t))).toEqual([]);' in s
        # 「重置」入口：review_status 非空的四种行都要锁
        assert s.count('toContain("重置")') >= 4


# ===========================================================================
# B · 逗号运算符：断言后面的「失败提示文案」其实是死代码
# ===========================================================================
def _comma_operator_hits(root: Path):
    """扫描 .ts/.tsx：``expect(...).toBe(true),`` 结尾 + 下一行是裸字符串字面量。

    刻意不用正则、不写任何转义字面量（splitlines 而非 split）—— 本仓已两次被
    「文本工具展开转义」咬到（R55 F6 / R56 字节体检）。
    """
    hits = []
    for dirpath, _dirs, files in os.walk(str(root)):
        if "node_modules" in dirpath:
            continue
        for name in files:
            if not name.endswith((".ts", ".tsx")):
                continue
            p = Path(dirpath) / name
            lines = _src(p).splitlines()
            for i, ln in enumerate(lines[:-1]):
                st = ln.strip()
                if st.endswith("),") and "expect(" in st and \
                        lines[i + 1].strip().startswith('"'):
                    hits.append("%s:%d" % (p.name, i + 1))
    return hits


class TestNoCommaOperatorAssertions:

    def test_frontend_has_zero_comma_operator_expect(self):
        """`expect(...).toBe(true), "说明";` 语法合法（逗号运算符），断言仍执行，
        但说明文字成为永不显示的死代码，且 eslint no-unused-expressions 判 error
        → CI 的 npm run lint 退出 1。全 frontend/src 必须零命中。"""
        assert _comma_operator_hits(_FE) == []

    def test_readiness_dashboard_force_recompute_assertion(self):
        """F7c 落地形态：说明改写成注释，断言以分号收尾。"""
        s = _src(T_READY)
        # 锁整行而非尾片段：同一模式在本文件出现 3 次（overviewCalls / calls 两处），
        # 短锚点的 count==1 是护栏写错，不是产品有问题。
        assert s.count(
            'expect(overviewCalls.some((c) => c[0] === "s1" '
            '&& c[1] === true)).toBe(true);') == 1
        # 只锁「以逗号运算符收尾的**代码行**」，不锁裸子串 —— 本文件 524 行的
        # 注释里逐字引用了旧写法作反例，子串锁会把它判成回流（首跑第二轮的假阳性）。
        bad = [ln.strip() for ln in s.splitlines()
               if ln.strip().endswith(').toBe(true),')]
        assert bad == [], bad


# ===========================================================================
# C · 字节与接线：R57 触碰的 4 个前端文件
# ===========================================================================
class TestR57ByteAndWiring:

    @pytest.mark.parametrize("path", [str(p) for p in T_TOUCHED],
                             ids=[p.name for p in T_TOUCHED])
    def test_no_lone_cr_no_bom_no_replacement_char(self, path):
        b = io.open(path, "rb").read()
        assert b.count(b"\r") == 0, "%s 混入 CR（本仓这些文件是纯 LF）" % path
        assert b[:3] != b"\xef\xbb\xbf", "%s 不应有 BOM" % path
        assert b.decode("utf-8").count(chr(0xFFFD)) == 0, path

    def test_workbench_name_ref_is_const(self):
        """F7d：``let nameRef`` → ``const nameRef``（只改 .current，绑定从不重新
        赋值）。eslint prefer-const 是 error 级，回流会让 CI lint 步骤再红。"""
        s = _src(P_WORKBENCH)
        assert s.count("let nameRef") == 0
        assert s.count('const nameRef: { current: string } = { current: "" };') == 1

    def test_facts_adjust_uses_get_all_by_text(self):
        """F7a：两条同理由的丢弃项必须**逐条列出**，getByText 会因多重命中抛错。"""
        s = _src(T_FACTS)
        assert s.count("screen.getAllByText(new RegExp(FACTS_ADJUST_IGNORE_REASON_LABELS.unknown_fact_id))") == 1
        assert "toHaveLength(2)" in s
        assert "screen.getByText(new RegExp(FACTS_ADJUST_IGNORE_REASON_LABELS.unknown_fact_id))" not in s


# ===========================================================================
# D · Node 解析出口本身（I 组「真跑」层的前提，坏了会退化成静默 skip）
# ===========================================================================
CLOSEOUT = Path(__file__).resolve().parent / "test_review_autofix_closeout_r55_20261008.py"


def _load_closeout_module():
    path = str(CLOSEOUT)
    spec = importlib.util.spec_from_file_location("_r55_closeout", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestNodeResolver:

    def test_env_var_wins_over_path_and_probes(self, monkeypatch):
        m = _load_closeout_module()
        seen = []
        monkeypatch.setattr(m, "_node_ok", lambda p: (seen.append(p) or True))
        monkeypatch.setenv(m._NODE_ENV, "C:\\fake\\env-node.exe")
        monkeypatch.setattr(m.shutil, "which", lambda _n: "C:\\fake\\path-node.exe")
        monkeypatch.setattr(m.glob, "glob", lambda _p: ["C:\\fake\\probe-node.exe"])
        assert m._resolve_frontend_node() == "C:\\fake\\env-node.exe"
        assert seen == ["C:\\fake\\env-node.exe"]          # 命中即止，不多探

    def test_path_wins_over_probes(self, monkeypatch):
        m = _load_closeout_module()
        monkeypatch.delenv(m._NODE_ENV, raising=False)
        monkeypatch.setattr(m, "_node_ok", lambda p: True)
        monkeypatch.setattr(m.shutil, "which", lambda _n: "C:\\fake\\path-node.exe")
        monkeypatch.setattr(m.glob, "glob", lambda _p: ["C:\\fake\\probe-node.exe"])
        assert m._resolve_frontend_node() == "C:\\fake\\path-node.exe"

    def test_probes_used_last_and_returned(self, monkeypatch):
        m = _load_closeout_module()
        monkeypatch.delenv(m._NODE_ENV, raising=False)
        monkeypatch.setattr(m, "_node_ok", lambda p: p.endswith("b.exe"))
        monkeypatch.setattr(m.shutil, "which", lambda _n: None)
        monkeypatch.setattr(m.glob, "glob", lambda _p: ["C:\\x\\a.exe", "C:\\x\\b.exe"])
        assert m._resolve_frontend_node() == "C:\\x\\b.exe"

    def test_bad_env_var_fails_loud_instead_of_falling_back(self, monkeypatch):
        """环境变量写错必须**报错**，不能悄悄退回 PATH —— 否则用户以为真跑了，
        实际拿到的是另一套运行时/根本没跑（skip 在报告里长得像「通过」）。"""
        m = _load_closeout_module()
        monkeypatch.setenv(m._NODE_ENV, "C:\\not\\exist\\node.exe")
        monkeypatch.setattr(m, "_node_ok", lambda p: False)
        monkeypatch.setattr(m.shutil, "which", lambda _n: "C:\\windows\\node.exe")
        with pytest.raises(AssertionError):
            m._resolve_frontend_node()

    def test_all_three_levels_empty_returns_blank(self, monkeypatch):
        m = _load_closeout_module()
        monkeypatch.delenv(m._NODE_ENV, raising=False)
        monkeypatch.setattr(m.shutil, "which", lambda _n: None)
        monkeypatch.setattr(m.glob, "glob", lambda _p: [])
        assert m._resolve_frontend_node() == ""

    def test_fe_ready_needs_deps_not_only_node(self, tmp_path):
        """门控判据本身：有 node 无依赖（CI backend job 的真实形态）必须 **False**
        —— 这一条才是「退回只看 node」能被抓住的地方。"""
        m = _load_closeout_module()
        assert m._fe_ready("", m._FE_DIR) is False                 # 无 node
        assert m._fe_ready("C:\\fake\\node.exe", tmp_path) is False  # 有 node、无 node_modules
        real_deps = all((m._FE_DIR / "node_modules" / s).exists()
                        for s in m._FE_SCRIPTS)
        assert m._fe_ready("C:\\fake\\node.exe", m._FE_DIR) is real_deps
        assert m._FE_READY is m._fe_ready(m._NODE, m._FE_DIR)       # 落库值 = 纯函数值

    def test_gate_source_locks_fe_ready_not_node(self):
        """门控文本必须挂在 _FE_READY 上；直接挂 _NODE 的写法不得回流。"""
        s = _src(CLOSEOUT)
        assert "_FE_READY = _fe_ready(_NODE, _FE_DIR)" in s
        assert "@pytest.mark.skipif(not _FE_READY," in s
        assert "skipif(not _NODE" not in s

    def test_skip_condition_requires_node_modules_too(self):
        """CI 的 backend job：runner **预装 node** 但不装前端依赖 —— 若门控只看
        node，三个真跑用例会当场失败而非 skip。门控必须是 _FE_READY。"""
        m = _load_closeout_module()
        cls = m.TestFrontendRealExecution
        marks = [mk for mk in getattr(cls, "pytestmark", [])
                 if mk.name == "skipif"]
        assert len(marks) == 1
        # skipif 的条件是**位置参数**（Mark(name, args=(cond,), kwargs 只有 reason），
        # 不在 kwargs["condition"] —— 取错位置拿到 None，会把「门控写错」误报成
        # 「门控缺失」。
        cond = marks[0].args[0]
        assert cond is (not m._FE_READY), "skipif 条件必须是 _FE_READY 的取反"
        expected = bool(m._NODE) and all(
            (m._FE_DIR / "node_modules" / s).exists() for s in m._FE_SCRIPTS)
        assert m._FE_READY == expected
        # 关键分歧点（CI backend job 有 node、无 node_modules）：必须 skip 而非失败，
        # 所以 _FE_READY 绝不能只看 _NODE。此处不假设本机一定有依赖。
        if m._NODE and not m._FE_READY:
            assert any(not (m._FE_DIR / "node_modules" / s).exists()
                       for s in m._FE_SCRIPTS)
