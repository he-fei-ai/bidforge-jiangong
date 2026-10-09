"""R54 全局事实收口 · A/B 反向验证（变异 → 定向失败 → 还原 → sha256 比对）。

每条变异都必须「只让目标用例失败」，且还原后文件字节与变异前完全一致。
⚠️ 锚点统一按 \n 书写，比对/写回时按文件真实行尾转换（CRLF 文件上纯 \n
   锚点一个都匹配不到，这是本仓第三次踩的坑，见 AGENTS R49 记录）。
"""
import hashlib
import io
import json
import os
import subprocess
import sys

BACKEND = os.path.dirname(os.path.abspath(__file__))
GF = os.path.join(BACKEND, "app", "routers", "global_facts.py")
FE = os.path.join(BACKEND, "app", "services", "facts_extractor.py")
TESTS = os.path.join(BACKEND, "tests", "test_facts_derivation_closeout_r54_20261008.py")

MUTANTS = [
    {
        "id": "M1-F2",
        "desc": "摘掉 resolve_conflict 的维度重派生（回到「第 4 条写路径漏改」）",
        "file": GF,
        "anchor": (
            "    _dim_assigns = _rederive_dimension_columns(\n"
            "        old_name=_title_raw, new_name=_title_raw,\n"
            "        old_value=prior_value, new_value=new_value,\n"
            "        old_category=row[\"category\"] or \"\", new_category=row[\"category\"] or \"\",\n"
            "        fact_type=row[\"fact_type\"] or \"\", fact_key=row[\"fact_key\"] or \"\")\n"),
        "replace": "    _dim_assigns = []  # A/B MUTANT\n",
        "targets": [
            "TestResolveConflictRederives::test_value_change_rederives_both_columns",
            "TestResolveConflictRederives::test_simulated_candidate_keeps_gate_but_rederives",
            "TestResolveConflictRederives::test_shared_fact_adjudication_also_rederives",
        ],
    },
    {
        "id": "M2-F1",
        "desc": "/chapters 把「事实名」当取值喂给维度派生（旧缺陷本体）",
        "file": GF,
        "anchor": "        dims = _fact_dimension_fields(\n            r, r[\"name\"], r[\"value\"],\n",
        "replace": "        dims = _fact_dimension_fields(\n            r, r[\"name\"], r[\"name\"],\n",
        "targets": [
            "TestChapterViewParity::test_fact_attr_matches_list_facts",
            "TestChapterViewParity::test_distributions_identical_across_two_read_paths",
        ],
    },
    {
        "id": "M3-F1b",
        "desc": "/chapters 行内不写 name 键（chapter_field_completeness 看不到事实名）",
        "file": GF,
        "anchor": "        r[\"name\"] = _n or r.get(\"title\") or \"\"\n",
        "replace": "        r[\"name\"] = \"\"  # A/B MUTANT\n",
        "targets": [
            "TestChapterViewParity::test_covered_fields_sees_the_fact_name",
            "TestChapterViewParity::test_item_value_has_no_markdown_markup",
        ],
    },
    {
        "id": "M4-F3",
        "desc": "/adjust 作用域退回「只取方案私有行」（少了共享半句）",
        "file": GF,
        "anchor": "    scope_sql, params = _fact_scope_where(scheme_scope, real_pid)\n",
        "replace": ("    scope_sql = \" WHERE project_id=? AND scheme_id=?\"  # A/B MUTANT\n"
                    "    params = [real_pid, scheme_scope]\n"),
        "targets": [
            "TestAdjustScopeAndAtomicity::test_shared_fact_is_updateable",
            "TestAdjustScopeAndAtomicity::test_duplicate_add_is_dropped_with_reason",
        ],
    },
    {
        "id": "M5-F4",
        "desc": "/adjust 循环内每条 update 各自 commit（末尾回滚撤不掉）",
        "file": GF,
        "anchor": "                    }], commit=False)\n",
        "replace": "                    }], commit=True)  # A/B MUTANT\n",
        "targets": [
            "TestAdjustScopeAndAtomicity::test_batch_is_atomic_rollback_on_later_failure",
        ],
    },
    {
        "id": "M6-F5",
        "desc": "persist_extraction 不再回查项目共享层（同名事实双份入库）",
        "file": FE,
        "anchor": "    if scheme_id and project_id:\n",
        "replace": "    if False:  # A/B MUTANT\n",
        "targets": [
            "TestPersistExtractionCrossNamespace::test_conflicting_value_writes_back_to_shared_row",
            "TestPersistExtractionCrossNamespace::test_same_value_clears_stale_without_conflict",
        ],
    },
    {
        "id": "M7-F7",
        "desc": "服务层反查退回位置索引取值（Mapping 行下被 fail-soft 吞成空 pid）",
        "file": FE,
        "anchor": "        return row_field(prow, \"project_id\")\n",
        "replace": "        return str((prow[0] if prow else \"\") or \"\")  # A/B MUTANT\n",
        "targets": [
            "TestSchemeProjectIdSingleSource::test_service_exit_accepts_mapping_rows",
        ],
    },
    {
        "id": "M8-F7",
        "desc": "路由层反查出口退回位置索引取值",
        "file": GF,
        "anchor": "    return row_field(row, \"project_id\")\n",
        "replace": "    return str(row[0] or \"\")  # A/B MUTANT\n",
        "targets": [
            "TestSchemeProjectIdSingleSource::test_router_exit_on_mapping_rows",
            "TestSchemeProjectIdSingleSource::test_no_positional_project_id_extract_left",
        ],
    },
    {
        "id": "M9-F7",
        "desc": "三态出口退化成两态（方案不存在 == 未绑定项目）",
        "file": GF,
        "anchor": "    row = await cur.fetchone()\n    if row is None:\n        return None\n",
        "replace": "    row = await cur.fetchone()\n    if row is None:\n        return \"\"  # A/B MUTANT\n",
        "targets": [
            "TestSchemeProjectIdSingleSource::test_router_exit_is_tristate_not_two_in_one",
        ],
    },
    {
        "id": "M10-F7",
        "desc": "游标为空（R13）时的 503 退化成本仓明令禁止的 404",
        "file": GF,
        "anchor": "        raise HTTPException(503, \"数据服务暂时不可用，请稍后重试\")\n",
        "replace": "        raise HTTPException(404, \"方案不存在\")  # A/B MUTANT\n",
        "targets": [
            "TestSchemeProjectIdSingleSource::test_router_exit_cursor_none_raises_503_not_404",
        ],
    },
    {
        "id": "M11-F7",
        "desc": "确认共享事实时恢复「二次 SELECT + 位置取值」旧形态",
        "file": GF,
        "anchor": (
            "        #     「事实已变更」标记）。现直接复用已在手的行。\n"
            "        await _invalidate_fact_scope_cache(db, \"\", str(row[\"project_id\"] or \"\"))\n"
            "    return {\"ok\": True}\n"),
        "replace": (
            "        #     「事实已变更」标记）。现直接复用已在手的行。\n"
            "        cur = await db.execute(\"SELECT project_id FROM global_facts WHERE id=?\", (fact_id,))\n"
            "        prow = await cur.fetchone()\n"
            "        await _invalidate_fact_scope_cache(db, \"\", str(prow[0] or \"\") if prow else \"\")\n"
            "    return {\"ok\": True}\n"),
        "targets": [
            "TestSchemeProjectIdSingleSource::test_scheme_project_sql_is_single_source",
            "TestSchemeProjectIdSingleSource::test_no_positional_project_id_extract_left",
        ],
    },
]


def _read(path):
    with io.open(path, "r", encoding="utf-8", newline="") as f:
        return f.read()


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _fit(text, needle):
    """把 \n 锚点转换成目标文件的真实行尾。"""
    if "\r\n" in text:
        return needle.replace("\r\n", "\n").replace("\n", "\r\n")
    return needle.replace("\r\n", "\n")


def _run_targets(targets):
    node_ids = [f"{TESTS}::{c}::" if False else f"{TESTS}::{c}" for c in targets]
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    p = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "--basetemp=D:\\pt2", "--no-header", "-x" if False else "-q"] + node_ids,
        cwd=BACKEND, env=env, capture_output=True, text=True, encoding="utf-8",
        errors="replace")
    tail = (p.stdout or "").strip().splitlines()[-3:]
    return p.returncode, " | ".join(tail)


def main():
    report = []
    for m in MUTANTS:
        path = m["file"]
        orig = _read(path)
        sha_before = _sha(orig)
        anchor = _fit(orig, m["anchor"])
        repl = _fit(orig, m["replace"])
        n = orig.count(anchor)
        entry = {"id": m["id"], "desc": m["desc"], "anchor_count": n}
        if n != 1:
            entry["status"] = "SKIP(anchor 不唯一)"
            report.append(entry)
            continue
        try:
            with io.open(path, "w", encoding="utf-8", newline="") as f:
                f.write(orig.replace(anchor, repl, 1))
            rc, out = _run_targets(m["targets"])
            entry["mutant_rc"] = rc
            entry["mutant_out"] = out
            entry["status"] = "OK(定向失败)" if rc != 0 else "FAIL(变异仍通过=假护栏)"
        finally:
            with io.open(path, "w", encoding="utf-8", newline="") as f:
                f.write(orig)
            restored = _read(path)
            entry["restore_sha_match"] = _sha(restored) == sha_before
        report.append(entry)

    # 还原后必须全绿（证明没有把文件改坏）
    rc, out = _run_targets([
        "TestResolveConflictRederives",
        "TestChapterViewParity",
        "TestAdjustScopeAndAtomicity",
        "TestPersistExtractionCrossNamespace",
        "TestSingleSourceExits",
        "TestFrontendContractParity",
        "TestSchemeProjectIdSingleSource",
    ])
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print("RESTORED_RUN rc=%d :: %s" % (rc, out))


if __name__ == "__main__":
    main()
