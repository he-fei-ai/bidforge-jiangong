"""OMP 引擎层（路由 / 诊断 / 任务存储）骨架护栏（2026-11-13）。

覆盖：
  - 引擎路由：auto/omp/main、降级标注、任务级覆盖、加法式标注不覆盖既有键；
  - 诊断预检：闸门结论、禁用工具检出、指标汇总、成本估算；
  - 任务存储：指纹稳定性、save/load/clear、损坏文件静默降级、产物缓存判定。

全程纯函数 / 临时文件，不拉起真实 omp 二进制。
"""

from __future__ import annotations

import os
import tempfile

import pytest
from app.services.ai.omp_diagnostics import (
    collect_task_metrics,
    estimate_cost,
    preflight,
)
from app.services.ai.omp_engine_router import (
    ENGINE_ORDER,
    annotate_result,
    default_omp_available,
    select_engine,
)
from app.services.ai.omp_task_store import OmpTaskStore, cache_key, input_fingerprint

# ---------- 引擎路由 ----------


def test_engine_order_is_omp_first():
    """降级链顺序（意图）：omp 优先 → local（本地编排档）→ main（平台主链）兜底。

    ✅ R236（2026-11-13）：原断言是「全等 ("omp","main")」⇒ 锁**实现**；
    按附件方案 §5.6 补上 local 档后重锚为**相对顺序**断言（意图保留、强度不减：
    既锁 omp 在首，又锁 local 必须排在 main 之前）。
    """
    assert ENGINE_ORDER[0] == "omp"
    assert "main" in ENGINE_ORDER
    assert "local" in ENGINE_ORDER
    assert ENGINE_ORDER.index("local") < ENGINE_ORDER.index("main")
    assert ENGINE_ORDER.index("omp") < ENGINE_ORDER.index("local")


def test_select_engine_main_explicit():
    c = select_engine(task_type="outline", prefer="main", omp_available=True)
    assert c.engine == "main" and c.degraded is False


def test_select_engine_omp_available_and_degraded():
    """偏好 omp 但不可用 ⇒ 必须降级并标注 degraded（意图）。

    ✅ R236（2026-11-13）：降级链补上 local 档后，降级落点由 main 变 local
    （main 仍在兜底位）⇒ 原断言锁的是**具体档位**；此处重锚为**意图**：
    degraded 必为 True、engine 必落在降级档、且**绝不可落回 omp 本身**。
    """
    ok = select_engine(task_type="outline", prefer="omp", omp_available=True)
    assert ok.engine == "omp" and ok.degraded is False
    bad = select_engine(task_type="outline", prefer="omp", omp_available=False)
    assert bad.degraded is True
    assert bad.engine in ("local", "main")
    assert bad.engine != "omp"
    # local 档不可用时兜底 main（降级链末端）
    worst = select_engine(
        task_type="outline", prefer="omp", omp_available=False, local_available=False
    )
    assert worst.engine == "main" and worst.degraded is True


def test_select_engine_auto_supported_and_unsupported():
    assert select_engine(task_type="outline", prefer="auto", omp_available=True).engine == "omp"
    assert select_engine(task_type="unknown", prefer="auto", omp_available=True).engine == "main"
    assert select_engine(task_type="outline", prefer="auto", omp_available=False).engine == "main"


def test_select_engine_override_wins():
    c = select_engine(
        task_type="outline", prefer="omp", omp_available=False, overrides={"outline": "main"}
    )
    assert c.engine == "main" and c.degraded is False


def test_annotate_result_additive_not_overriding():
    out = annotate_result(
        {"engine_used": "main", "ok": True},
        select_engine(task_type="outline", prefer="omp", omp_available=False),
    )
    assert out["engine_used"] == "main"  # 既有键不被覆盖
    assert out["degraded"] is True and out["engine_reason"]
    assert out["ok"] is True


def test_annotate_result_wraps_non_dict():
    out = annotate_result("raw", select_engine(task_type="outline", prefer="main"))
    assert out["result"] == "raw" and out["engine_used"] == "main"


def test_default_omp_available_is_bool():
    assert isinstance(default_omp_available(), bool)


# ---------- 诊断 ----------


def test_preflight_reports_structure_without_raising():
    rep = preflight(enabled=False, binary="omp-definitely-missing", model="", tools="")
    assert rep.ok is False
    assert rep.checks.get("gate_ready") is False
    assert rep.problems
    assert isinstance(rep.to_dict(), dict)


def test_preflight_forbidden_tools_detected():
    """2026-11-13 · 放开命令执行：bash 不再是禁用工具；改用 curl 验证禁用仍生效。"""
    rep = preflight(enabled=False, binary="x", model="m", tools="read,write,curl")
    assert rep.checks.get("tools_safe") is False
    assert any("curl" in p for p in rep.problems)


def test_preflight_safe_tools_pass():
    rep = preflight(enabled=False, binary="x", model="m", tools="read,write,grep")
    assert rep.checks.get("tools_safe") is True


def test_collect_task_metrics_from_report():
    from app.services.ai.omp_workflow import TaskResult, WorkflowReport

    report = WorkflowReport(
        stage="content",
        results=[
            TaskResult(task_id="a", status="completed", payload="x" * 10, attempts=1, fix_rounds=0),
            TaskResult(task_id="b", status="degraded", payload="y" * 20, attempts=2, fix_rounds=1),
            TaskResult(task_id="c", status="paused", payload="", attempts=3, fix_rounds=2),
        ],
        totals={},
    )
    m = collect_task_metrics(report)
    assert m["tasks"] == 3 and m["completed"] == 1 and m["degraded"] == 1 and m["paused"] == 1
    assert m["attempts"] == 6 and m["fix_rounds"] == 3 and m["output_chars"] == 30


def test_estimate_cost_zero_rates_unknown():
    c = estimate_cost(input_chars=1000, output_chars=500)
    assert c["estimated_cost"] == 0.0 and c["rate_known"] is False


def test_estimate_cost_with_rates():
    c = estimate_cost(input_chars=2000, output_chars=1000, input_per_1k=0.5, output_per_1k=1.5)
    assert c["estimated_cost"] == pytest.approx(2.5) and c["rate_known"] is True


# ---------- 任务存储 ----------


def test_input_fingerprint_stable_and_unambiguous():
    a = input_fingerprint("scheme1", "outline", "facts")
    assert a == input_fingerprint("scheme1", "outline", "facts")
    assert a != input_fingerprint("scheme1", "outline", "facts2")
    # 分隔符避免拼接歧义
    assert input_fingerprint("ab", "c") != input_fingerprint("a", "bc")


def test_cache_key_uses_parts():
    assert cache_key(scheme_id="s", stage="content", inputs="x") == input_fingerprint(
        "s", "content", "x"
    )


def test_task_store_roundtrip_and_clear():
    with tempfile.TemporaryDirectory() as d:
        store = OmpTaskStore(os.path.join(d, "omp_tasks.json"))
        assert store.save("k1", {"status": "running", "stage": "outline"}) is True
        st = store.load("k1")
        assert st and st["status"] == "running" and "updated_at" in st
        assert store.is_resumable("k1") is True
        assert store.clear("k1") is True
        assert store.load("k1") is None
        assert store.clear("k1") is False


def test_task_store_completed_artifact_cache():
    with tempfile.TemporaryDirectory() as d:
        store = OmpTaskStore(os.path.join(d, "omp_tasks.json"))
        assert store.cached_artifact("k") is None
        store.save("k", {"status": "completed", "artifact": {"documents": {"a": "x"}}})
        assert store.cached_artifact("k") == {"documents": {"a": "x"}}
        assert store.is_resumable("k") is False


def test_task_store_corrupt_file_degrades():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "omp_tasks.json")
        with open(p, "w", encoding="utf-8") as f:
            f.write("{not json")
        store = OmpTaskStore(p)
        assert store.load("k") is None
        assert store.save("k", {"status": "running"}) is True
        assert store.load("k")["status"] == "running"
