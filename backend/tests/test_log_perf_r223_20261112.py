# -*- coding: utf-8 -*-
"""R223 · 后台日志驱动：生产慢请求 / 事件循环阻塞的根因收口护栏（2026-11-12）

背景（本轮日志取证）
--------------------
生产日志 `logs/backend.log`（2026-11-07 → 11-11）出现：

  - `loop: 事件循环延迟 1084~29150ms` × 15（最长 29.15s）；
  - 同期 `GET /schemes/{id}/sections` 被拖到 6.3~10.4s（其余时刻仅 20~40ms）；
  - `POST /review/autofix/confirm -> 200 (29726ms)`、`stage -> 200 (89420ms)`。

**根因（离线实测，非推断）**：在真实方案 `8d3594d4`（159 章）上直接跑预检引擎：

    check_duplication  6420 ms  ← 占整轮 96%
    run_preflight      6686 ms

`check_duplication` 内部 `find_cross_section_copies` 的两两精判达 **60086 对**并触顶
（`truncated=True` → CON-06 告警"结论可能不完整"）。该 6.4s 为纯 CPU、持有 GIL，
拖慢事件循环；而 `confirm` 的链式重算分支对每个 (章节, 规则) 各重算**一整轮预检**
（4 条 ≈ 27s ≈ 实测 29.7s），把放大做成 O(k)。

本文件锁定两项修复（均为"保结论不变"的精确剪枝 / 请求内单次计算）：

  FIX-1 `duplicate_detection._candidate_indices` / `_collect_near_groups`：
        只比前向对 + 按分档双阈值反导「交集下界」（低于下界不可能命中）。
  FIX-2 `review_autofix.confirm`：同一请求内只算一次总检 / 方案 / 事实。

两个文件的具体行号勿写进断言（本仓惯例：锁意图与行为，不锁实现行号）。
"""

from __future__ import annotations

import ast
import random
from pathlib import Path

import pytest
from app.routers import review_autofix as ra
from app.services import duplicate_detection as dd

REPO = Path(__file__).resolve().parents[2]
DD_PATH = REPO / "backend" / "app" / "services" / "duplicate_detection.py"
RA_PATH = REPO / "backend" / "app" / "routers" / "review_autofix.py"


# ---------------------------------------------------------------------------
# 工具：完整口径（新剪枝）与朴素两两，均返回"检出的无序章节对集合"
# ---------------------------------------------------------------------------
def _full_claimed(sections):
    """跑真实实现，返回本次检出的**全部**无序章节对 + 精判对数 + 截断标记。

    直接调内部函数（不经 ``MAX_COPY_GROUPS`` 截断），以便与朴素两两做**全量**对账。
    """
    by_sk, flat = dd._prepare_sections(sections)
    excluded = dd._to_exclude_set(None)
    claimed: set = set()
    dd._collect_exact_groups(by_sk, flat, excluded, claimed)
    _groups, truncated, compared = dd._collect_near_groups(flat, excluded, claimed)
    return claimed, compared, truncated


def _brute_claimed(sections):
    """朴素两两（无倒排、无交集下界），**保留**同性质的合法剪枝：

      - 只比 i<j（结论对称，等价去重）；
      - 跳过同章节；
      - 长度比 > 2 直接剪枝（Dice 有上界）。
    """
    by_sk, flat = dd._prepare_sections(sections)
    excluded = dd._to_exclude_set(None)
    claimed: set = set()
    for _sk, occ in by_sk.items():
        seen, dist = set(), []
        for sid, title in occ:
            if sid in seen:
                continue
            seen.add(sid)
            dist.append((sid, title))
        for i in range(len(dist)):
            for j in range(i + 1, len(dist)):
                key = frozenset((dist[i][0], dist[j][0]))
                if key not in excluded:
                    claimed.add(key)
    for i in range(len(flat)):
        a_id, _at, a_sk, _ag = flat[i]
        for j in range(i + 1, len(flat)):
            b_id, _bt, b_sk, _bg = flat[j]
            if a_id == b_id:
                continue
            if len(b_sk) > len(a_sk) * 2:
                continue
            key = frozenset((a_id, b_id))
            if key in excluded:
                continue
            if dd.sentences_near_match(a_sk, b_sk) is not None:
                claimed.add(key)
    return claimed


def _make_sections(n_sections: int, n_sent: int, seed: int = 11):
    """合成语料：含真实搬运（exact + 一字之差 near）+ 高频模板噪声。"""
    rnd = random.Random(seed)
    dup_exact = "基坑开挖前应完成坑面清除与净度验收工作并做好记录归档"
    near_a = "模板支撑体系搭设完成后必须经验收合格方可进入下道工序施工"
    near_b = "模板支撑体系搭设完成后必须经验收合格方可进行下道工序施工"
    common = ["工程要求进行验收", "施工前必须完成技术交底", "现场作业须符合规范"]
    secs = []
    for i in range(n_sections):
        parts = []
        for k in range(n_sent):
            if k % 7 == 0:
                parts.append(dup_exact)
            elif k % 11 == 0 and i % 2 == 0:
                parts.append(near_a)
            elif k % 11 == 0:
                parts.append(near_b)
            elif k % 5 == 0:
                parts.append(rnd.choice(common) + "第%03d项工序" % (i * 100 + k))
            else:
                parts.append("本章第%03d节专项措施应落实至班组并留存第%03d号记录" % (i, k))
        secs.append(
            {
                "id": "s%02d" % i,
                "title": "章节%02d" % i,
                "content": "。".join(parts),
                "word_count": n_sent * 30,
            }
        )
    return secs


# ---------------------------------------------------------------------------
# FIX-1 · 剪枝无损性（核心不变式）
# ---------------------------------------------------------------------------
def test_r223_pruning_matches_bruteforce():
    """⚠️ 核心不变式：新剪枝（前向对 + 分档下界）与朴素两两**结论逐对相等**。"""
    secs = _make_sections(22, 30)
    new, _cmp, _trunc = _full_claimed(secs)
    brute = _brute_claimed(secs)
    assert new, "语料含真实搬运，必须检出（防护栏空转）"
    assert brute, "朴素两两也必须检出（语料构造自证）"
    assert new == brute, (
        f"剪枝改变了结论：missing={sorted(brute - new)[:5]} extra={sorted(new - brute)[:5]}"
    )


def test_r223_pruning_matches_bruteforce_numeric_variants():
    """数字变形（骨架归一后成为 exact）同样不得漏检；朴素两两结论一致。"""
    secs = [
        {"id": "a", "title": "A", "content": "基坑开挖深度3米时应立即启动支护并监测", "word_count": 500},
        {"id": "b", "title": "B", "content": "基坑开挖深度9米时应立即启动支护并监测", "word_count": 500},
    ]
    new, _c, _t = _full_claimed(secs)
    assert frozenset(("a", "b")) in new, "数字变形的骨架子句必须被识别为搬运"
    assert new == _brute_claimed(secs)


def test_r223_forward_only_candidates():
    """`_candidate_indices` 行为锁：只召回后向候选（idx > self_idx）且升序。"""
    flat = [
        ("s0", "t0", "甲" * 40, dd.char_bigrams("甲" * 40)),
        ("s1", "t1", "甲" * 39 + "乙", dd.char_bigrams("甲" * 39 + "乙")),
        ("s2", "t2", "甲乙" + "丙" * 38, dd.char_bigrams("甲乙" + "丙" * 38)),
    ]
    inv = dd._build_bigram_inverted_index(flat)
    cands = dd._candidate_indices(flat[0][3], inv, 0, flat)
    assert cands == sorted(cands), "候选必须升序（结论可复现）"
    assert all(i > 0 for i in cands), "只应召回后向候选（idx > self_idx）"
    # 反向调用（self_idx=1）不得再召回下标 0（否则同一对被比两次）
    assert 0 not in dd._candidate_indices(flat[1][3], inv, 1, flat)


def test_r223_low_distinct_bigram_match_not_pruned():
    """⚠️ 回归锁（本轮实测踩中的漏判）：**区分度低**（重复短语）的真搬运不得被剪。

    曾把「分档阈值 × 字符长度」当交集下界放进索引层：重复短语的**去重 bigram 数**
    远小于字符数，下界被高估 → 真实搬运被误剪（2026-11-12 真实方案实测漏 7 对）。
    正确下界必须用 **bigram 集合基数**，且施加在拿到真实交集之后。
    """
    phrase = "工程要求进行验收"
    a = phrase * 5                     # 40 字，但去重 bigram 仅数个
    b = phrase * 4 + "工程要求进行验"
    secs = [
        {"id": "a", "title": "A", "content": a, "word_count": 500},
        {"id": "b", "title": "B", "content": b, "word_count": 500},
    ]
    new, _c, _t = _full_claimed(secs)
    assert frozenset(("a", "b")) in new, "低区分度真搬运被下界误剪（本轮回归）"
    assert new == _brute_claimed(secs)


def test_r223_grams_reuse_equals_public_api():
    """`_near_match_from_grams`（复用预算集合）与 `sentences_near_match` 结论逐一相等。"""
    pairs = [
        ("模板支撑体系搭设完成后必须经验收合格方可进入下道工序施工",
         "模板支撑体系搭设完成后必须经验收合格方可进行下道工序施工"),
        ("基坑开挖前应完成坑面清除与净度验收工作并做好记录归档",
         "基坑开挖前应完成坑面清除与净度验收工作并做好记录归档"),
        ("现场作业须符合规范第%03d项工序工程要求进行验收" % 7,
         "现场作业须符合规范第%9d项工序工程要求进行验收" % 7),
        ("甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉",
         "完全不相干的另外一句技术措施描述内容甲乙丙"),
    ]
    checked = 0
    for a, b in pairs:
        sk_a = dd.build_skeleton_key(a)
        sk_b = dd.build_skeleton_key(b)
        ga = dd.char_bigrams(sk_a)
        gb = dd.char_bigrams(sk_b)
        pub = dd.sentences_near_match(sk_a, sk_b)
        got = dd._near_match_from_grams(sk_a, sk_b, ga, gb)
        assert (pub is None) == (got is None), f"口径分叉：{sk_a!r} / {sk_b!r}"
        if pub is not None and got is not None:
            assert pub.get("reason") == got.get("reason")
            assert abs(float(pub.get("dice") or 0) - float(got.get("dice") or 0)) < 1e-9
        checked += 1
    assert checked == len(pairs), "样本必须全部被实际比对（防护栏空转）"


def test_r223_tier_bound_not_pushed_into_index_layer():
    """源码锁：`_candidate_indices` **不得**接受/使用交集下界参数。

    索引层计数跳过高频 gram，只是真实交集的**下界**；用它收紧会漏判（回归锁见
    ``test_r223_low_distinct_bigram_match_not_pruned``）。下界只能施加在拿到
    真实 bigram 集合之后。
    """
    import inspect

    sig = inspect.signature(dd._candidate_indices)
    assert set(sig.parameters) == {"a_bg", "inv", "self_idx", "flat"}, (
        f"_candidate_indices 签名被改动（下界不得下沉到索引层）：{list(sig.parameters)}"
    )
    src = DD_PATH.read_text(encoding="utf-8")
    assert "_MIN_SHARED_BIGRAMS)" in src  # 判定仍以常量阈值为准
    assert "shared = len(a_grams & b_grams)" in src, "真实交集计算必须仍在近邻循环内"


def test_r223_candidate_indices_backward_compatible_default():
    """缺省 min_shared=None 时必须退回旧常量口径（既有单测契约不破坏）。"""
    flat = [
        ("a", "A", "甲乙丙丁戊己庚辛", dd.char_bigrams("甲乙丙丁戊己庚辛")),
        ("b", "B", "甲乙丙丁戊己庚辛", dd.char_bigrams("甲乙丙丁戊己庚辛")),
    ]
    inv = dd._build_bigram_inverted_index(flat)
    assert dd._candidate_indices(flat[0][3], inv, 0, flat) == [1]


def test_r223_pruning_cuts_pairwise_comparisons():
    """性能棘轮：大样本下精判对数被压到远小于朴素组合数，且不再触顶截断。

    旧实现在真实方案（159 章）上精判 60086 对并触顶（truncated=True）。
    """
    secs = _make_sections(80, 60, seed=5)
    _by, flat = dd._prepare_sections(secs)
    _claimed, compared, truncated = _full_claimed(secs)
    # 朴素组合数（i<j 的上界）
    n = len(flat)
    naive_upper = n * (n - 1) // 2
    assert n > 2000, f"样本规模不足，棘轮失去意义：sentences={n}"
    assert compared < naive_upper // 50, (
        f"剪枝不足：compared={compared} naive_upper={naive_upper}"
    )
    assert not truncated, "收紧剪枝后不应再触顶（结论完整）"


def test_r223_existing_losslessness_guard_still_holds():
    """既有不变式回归：高频噪声淹没下仍必须检出真实搬运。"""
    noise = "。".join("工程要求进行验收终清%04d" % i for i in range(400))
    dup = "基坑元坑面清除与净度验收要求应在开控坑前完成"
    secs = [
        {"id": "a", "title": "施工工艺", "content": dup + "。本章专有内容。" + noise},
        {"id": "b", "title": "安全保证", "content": dup + "。另章专有内容。" + noise},
    ]
    assert dd.find_cross_section_copies(secs)["groups"], "真实搬运被误剪"


# ---------------------------------------------------------------------------
# FIX-2 · 请求内只算一次总检
# ---------------------------------------------------------------------------
def test_finding_from_payload_contract():
    """纯函数取值契约：命中 / section 过滤 / 未命中返回 None。"""
    payload = {"findings": [{"rule_id": "CON-05", "section_id": "x"}, {"rule_id": "DLV-09", "section_id": ""}]}
    assert ra._finding_from_payload(payload, "CON-05", "x")["rule_id"] == "CON-05"
    assert ra._finding_from_payload(payload, "CON-05", "y") is None
    assert ra._finding_from_payload(payload, "DLV-09")["rule_id"] == "DLV-09"
    assert ra._finding_from_payload(payload, "NOPE") is None
    assert ra._finding_from_payload({}, "CON-05") is None


@pytest.mark.asyncio
async def test_resolve_finding_with_payload_skips_recompute(monkeypatch):
    """传入 payload 时**不得**再调 `_readiness_overview_compute`（计数器为 0）。"""
    from app.routers import compliance as comp

    calls = {"n": 0}

    async def _fake_compute(*a, **k):
        calls["n"] += 1
        return {"findings": [{"rule_id": "CON-05", "section_id": "s1"}]}

    monkeypatch.setattr(comp, "_readiness_overview_compute", _fake_compute)
    # 直传 payload：零重算
    got = await ra._resolve_finding(None, "sid", "CON-05", "s1", payload={"findings": [{"rule_id": "CON-05", "section_id": "s1"}]})
    assert got["section_id"] == "s1"
    assert calls["n"] == 0, "传 payload 后仍发生了重算"
    # 缺省（None）：回退自算一次（旧行为逐字兼容）
    got2 = await ra._resolve_finding(None, "sid", "CON-05", "s1")
    assert got2["rule_id"] == "CON-05"
    assert calls["n"] == 1, "缺省路径应恰好自算一次"


def _confirm_src() -> str:
    src = RA_PATH.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "confirm":
            return ast.get_source_segment(src, node) or ""
    raise AssertionError("未找到 confirm 函数")


def test_confirm_recomputes_overview_at_most_once_source_lock():
    """源码锁：confirm 内 `_readiness_overview_compute` 恰 1 处（惰性 `_ov_once`），
    两处 `_resolve_finding` 均传 payload，且循环体内不再各查方案/事实。"""
    body = _confirm_src()
    # 只数**调用**（import 行同名不计）；最多一次实际重算。
    assert body.count("await _readiness_overview_compute(") <= 1, "confirm 内必须只算一次总检"
    assert "_ov_once" in body, "缺少请求级惰性缓存"
    assert body.count("payload=await _ov_once()") == 2, "两处 _resolve_finding 都必须复用缓存"
    assert "_scheme_facts_once" in body, "方案/事实必须请求内只查一次"
    # 循环体内不得再直接各查一次
    assert "scheme = await _load_scheme(db, scheme_id)" not in body
    assert "facts, standards_text = await _build_facts(db, scheme)" not in body


def test_confirm_cache_is_lazy_not_eager():
    """惰性而非急切：缓存定义处**不得**直接 await 计算（纯前缀分支零开销）。"""
    body = _confirm_src()
    assert "_ov_payload: dict | None = None" in body
    assert "_scheme_facts: dict = {}" in body
    # 定义 _ov_once 时不应已经算过
    head = body.split("async def _ov_once")[0]
    assert "_readiness_overview_compute" not in head, "缓存被提前急切计算（纯前缀路径凭空多算一轮）"


def test_resolve_finding_signature_is_keyword_only_payload():
    """契约：payload 为**仅关键字**可选参数，缺省 None（旧调用方逐字兼容）。"""
    import inspect

    sig = inspect.signature(ra._resolve_finding)
    p = sig.parameters["payload"]
    assert p.kind is inspect.Parameter.KEYWORD_ONLY
    assert p.default is None
    assert inspect.iscoroutinefunction(ra._resolve_finding)


# ---------------------------------------------------------------------------
# 字节体检 / 静态卫生
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path", [DD_PATH, RA_PATH])
def test_r223_no_replacement_char_and_parseable(path):
    raw = path.read_bytes()
    assert b"\xef\xbf\xbd" not in raw, f"{path.name} 出现 U+FFFD（中文损坏）"
    assert not raw.startswith(b"\xef\xbb\xbf"), f"{path.name} 出现 BOM"
    ast.parse(raw.decode("utf-8"), filename=str(path))


def test_r223_source_files_keep_crlf():
    """生产后端源保持 CRLF（本仓约定；新增测试文件用 LF）。"""
    for path in (DD_PATH, RA_PATH):
        raw = path.read_bytes()
        assert raw.count(b"\n") == raw.count(b"\r\n"), f"{path.name} 行尾漂移（非纯 CRLF）"
