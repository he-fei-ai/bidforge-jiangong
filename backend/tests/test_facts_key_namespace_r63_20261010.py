"""全局事实 fact_key 命名空间碰撞收口（2026-10-10 · 事实/正文联合轮次）。

背景（本轮实测发现的 P0）：
``normalize_key`` 的 md5 兜底分支先用
``re.sub(r'[^0-9A-Za-z一-鿿]+', '_', name)`` 把名称里「非字母数字、非 CJK」
的字符**一律压成一个下划线**。该替换是有损且不可逆的：

  * ``normalize_key('坡角α') == normalize_key('坡角β') == 'fact_f6122c97'``
    （修复前实测值；``深度①``/``深度②``、``温度°C``/``温度℃`` 同理）；
  * ``merge_and_deduplicate`` 按 fact_key 聚类 ⇒ 两条互不相同的事实落进同一簇，
    一条成为事实值、另一条沦为它的「矛盾候选值」且原名整条消失。
    端到端 = 静默丢事实 + 凭空造出假矛盾 + 后续 chapter/fact_attr 派生按
    幸存者的 name 计算。三者都**没有任何报错**。

修法：新增 ``_fact_key_seed`` —— 分隔符类字符（空白/``-``/``_``）仍折叠成一个
``_``，**其余符号按 Unicode 码点转义**。于是：

  * 名称只含「CJK + 字母数字 + 分隔符」 → 种子与旧实现逐字节相同（零迁移）；
  * 名称含其它符号 → 与「抹掉符号后同形的其它名称」区分开。

本文件锁定该双向契约，并给出 A/B 反向验证（把种子退回有损写法时必须变红）。

同时钉住本轮同批修复的两个健壮性缺陷：
  * ``extract_value_from_markdown_line(None)`` 不再抛 AttributeError；
  * ``_render_facts_text`` 对长度 < 3 的事实行不再抛 IndexError。
"""
import hashlib
import re

from app.services.facts_extractor import (
    FactItem,
    _fact_key_seed,
    _NAME_KEY_INDEX,
    extract_value_from_markdown_line,
    merge_and_deduplicate,
    normalize_key,
)

ALPHA = chr(0x03B1)      # 希腊字母 alpha
BETA = chr(0x03B2)       # 希腊字母 beta
GAMMA = chr(0x03B3)      # 希腊字母 gamma
CIRC_1 = chr(0x2460)     # 带圈数字 ①
CIRC_2 = chr(0x2461)     # 带圈数字 ②
CYR_A = chr(0x0430)      # 西里尔字母 а
CYR_B = chr(0x0431)      # 西里尔字母 б


def _legacy_seed(name: str) -> str:
    """修复前旧实现的哈希种子口径（逐字复现，用于向后兼容对账）。"""
    return re.sub(r'[^0-9A-Za-z一-鿿]+', '_', name.strip()).strip('_')


def _legacy_key(name: str) -> str:
    return f"fact_{hashlib.md5(_legacy_seed(name).encode('utf-8')).hexdigest()[:8]}"


def _falls_through_to_hash(name: str) -> bool:
    """该名称是否真的走到 md5 兜底分支（别名表 / 纯小写英文分支会提前返回）。"""
    for cn, _en in _NAME_KEY_INDEX:
        if cn in name:
            return False
    return not re.match(r'^[a-z_]+$', name)


# ===========================================================================
# A 组 · 向后兼容：既有 key 一个都不许变
# ===========================================================================

#: 只由「CJK + 字母数字 + 分隔符」组成、且会落到 md5 兜底分支的名称。
#: 这些名称的种子必须与旧实现逐字节相同，否则既有库里的 fact_key 会整体漂移，
#: 连带影响 persist_extraction 的 ``_protected_by_key``（手工事实可能被误删）。
SEPARATOR_ONLY_NAMES = (
    "电线规格Z", "风管尺寸A", "桩端持力层B", "降水井数量",
    "基坑 深度", "基坑-深度", "基坑_深度", "回填土压实系数",
    "卸料平台限重", "悬挑工字钢型号",
)


def test_seed_is_byte_identical_to_legacy_for_separator_only_names():
    """A1：分隔符类名称的哈希种子必须与旧实现逐字节相同（零迁移前提）。"""
    for n in SEPARATOR_ONLY_NAMES:
        assert _falls_through_to_hash(n), f"{n!r} 命中别名词表，用例前提失效"
        assert _fact_key_seed(n) == _legacy_seed(n), f"种子漂移: {n!r}"


def test_key_is_identical_to_legacy_for_separator_only_names():
    """A2：同上，但走到 key（含 fact_ 前缀与截断），这是对外的真实契约。"""
    for n in SEPARATOR_ONLY_NAMES:
        assert normalize_key(n) == _legacy_key(n), f"key 漂移: {n!r}"


def test_consecutive_separators_still_collapse_to_one_underscore():
    """A3：连续分隔符仍折叠成单个 `_`（旧替换原本就是这个语义，不得破坏）。"""
    assert _fact_key_seed("基坑  深--度") == _legacy_seed("基坑  深--度")
    assert _fact_key_seed("基坑  深--度") == "基坑_深_度"


def test_empty_and_separator_only_name_keeps_legacy_seed():
    """A4：空串 / 纯分隔符名称的种子仍为空串（与旧实现一致）。"""
    assert _fact_key_seed("") == _legacy_seed("") == ""
    assert _fact_key_seed("---") == _legacy_seed("---") == ""


# ===========================================================================
# B 组 · 修复本身：原先碰撞的名称现在必须分开
# ===========================================================================

#: ⚠️ 入队前提：两个名称的差异部分必须**全部由非分隔符类符号构成**，
#:    否则旧口径下不会碰撞（例如 ``°C`` 里的 ``C`` 是字母数字会被保留），
#:    用例的「前提校验」会被自己推翻。
COLLIDE_PAIRS = (
    ("坡角" + ALPHA, "坡角" + BETA),
    ("深度" + CIRC_1, "深度" + CIRC_2),
    ("肥槽回填" + ALPHA, "肥槽回填" + GAMMA),
    ("场地类别" + CYR_A, "场地类别" + CYR_B),
)


def test_legacy_seed_really_collided():
    """B1（前提校验 · 反向锚）：修法不是空谈——旧口径下这些命名确实撞在一起。

    没有这条，下面「修复后必须不同」的断言可能在某天被无意义地满足
    （例如有人把传给 normalize_key 的名字改掉，测试依旧全绿）。
    """
    for a, b in COLLIDE_PAIRS:
        assert _legacy_seed(a) == _legacy_seed(b), f"旧口径下本应碰撞: {a!r}/{b!r}"
        assert _legacy_key(a) == _legacy_key(b)


def test_distinct_symbols_yield_distinct_keys():
    """B2：同一好好 name 前缀 + 不同符号 → 不同 fact_key。"""
    for a, b in COLLIDE_PAIRS:
        assert normalize_key(a) != normalize_key(b), f"仍然碰撞: {a!r}/{b!r}"


def test_symbol_family_is_fully_distinguishable():
    """B3：一组 8 个不同符号在 fact_key 命名空间里必须产出 8 个互不相同的键。"""
    syms = [ALPHA, BETA, GAMMA, CIRC_1, CIRC_2, chr(0x2103), chr(0x00B0), CYR_A]
    keys = {normalize_key("v" + s) for s in syms}
    assert len(keys) == len(syms), "符号族仍有合并，命名空间非单射"


def test_key_shape_is_stable():
    """B4：key 形态不变（``fact_`` + 8 位十六进制），避免下游正则/列宽被打破。"""
    k = normalize_key("这条一定会走兜底分支的名称Xyz")
    assert k.startswith("fact_")
    assert re.fullmatch(r"fact_[0-9a-f]{8}", k), k


# ===========================================================================
# C 组 · 端到端：不再静默合并事实
# ===========================================================================


def _item(name, value, conf=0.9):
    return FactItem(name=name, value=value, key=normalize_key(name),
                    category="overview", source="doc", confidence=conf)


def test_two_distinct_symbol_facts_survive_dedupe():
    """C1（核心）：原先会合并成 1 条并伪造矛盾，现在必须两条都活着、且无矛盾。"""
    out = merge_and_deduplicate([
        _item("坡角" + ALPHA, "30"),
        _item("坡角" + BETA, "45"),
    ])
    assert len(out) == 2, "事实被静默合并"
    assert {i.name for i in out} == {"坡角" + ALPHA, "坡角" + BETA}, "原名丢失"
    assert all(not i.has_conflict for i in out), "凭空产生了假矛盾"
    assert {i.value for i in out} == {"30", "45"}


def test_genuine_same_key_multi_source_still_conflicts():
    """C2（反向）：真正同源多值的事实仍必须被判为矛盾 —— 不得被修复过度矫正。

    这是对 C1 的必须配对：**只看 C1 的红/绿无法区分「碰撞被修复」与
    「矛盾检测整体失效」**。
    """
    out = merge_and_deduplicate([
        FactItem(name="总工期那是", value="365", key="duration",
                 category="overview", source="A", confidence=0.9),
        FactItem(name="总工期那是", value="400", key="duration",
                 category="overview", source="B", confidence=0.6),
    ])
    assert len(out) == 1
    assert out[0].has_conflict is True
    assert {c["value"] for c in out[0].conflict_values} == {"400"}


# ===========================================================================
# D 组 · 同批健壮性修复
# ===========================================================================


def test_extract_value_tolerates_none():
    """D1：content 列为 NULL 的行不得让回解出口抛 AttributeError。

    ``load_resolved_facts_for_scope``（审核 / 预检 / 符合性 / 修复四条链路）
    依赖本函数，一行 NULL content 即可让整条链路 500。
    """
    assert extract_value_from_markdown_line(None) == ("", "")
    assert extract_value_from_markdown_line("") == ("", "")


def test_extract_value_keeps_existing_behaviour():
    """D2：正常输入的输出逐字不变（不得借健壮性修复改掉既有口径）。"""
    assert extract_value_from_markdown_line("- **基坑深度**: 12.5m") == (
        "基坑深度", "12.5m")
    assert extract_value_from_markdown_line("- 名称：值") == ("名称", "值")


def test_render_facts_text_tolerates_short_rows():
    """D3：长度 < 3 的事实行不得让「事实 → 提示词」唯一出口抛 IndexError。"""
    from app.services.facts_builder import _render_facts_text

    assert isinstance(_render_facts_text([("组",)]), str)
    assert isinstance(_render_facts_text([("组", "名")]), str)
    # 5 元组正常行仍照常渲染
    assert "值" in _render_facts_text([("组", "名", "值", 0.9, "")])


def test_render_facts_text_skips_unsequenceable_row():
    """D4：非序列行（None）跳过而不是炸掉整段事实注入。"""
    from app.services.facts_builder import _render_facts_text

    out = _render_facts_text([None, ("组", "名", "值", 0.9, "")])
    assert "值" in out


# ===========================================================================
# E 组 · A/B 反向验证：把修复退回有损写法，A/B 组必须同时变红
# ===========================================================================


def test_M0_control_all_green():
    """M0 对照：不注入变异时本文件全部断言成立（由 pytest 整体通过保证）。"""
    assert normalize_key("坡角" + ALPHA) != normalize_key("坡角" + BETA)


def test_M1_lossy_seed_reintroduces_collision(monkeypatch):
    """M1：把种子退回旧的有损写法 → 必须重现「事实被静默合并 + 假矛盾」。

    ⚠️ ``normalize_key`` 带 ``lru_cache``：只 monkeypatch ``_fact_key_seed``
    **不足以**让变异生效（命中缓存时根本不去查模块全局）。这个坑是本文件自己
    踩出来的：首版 M1/M2 在与其它用例同文件时全部表现为「假红」，单独跑却全绿
    —— 教训同 AGENTS.md R59 记的「测试数据把被变异的行为等价化了」，这里是
    「缓存把变异的结果钉住了」。故变异前后都必须显式 ``cache_clear()``。
    """
    import app.services.facts_extractor as fe

    fe.normalize_key.cache_clear()
    monkeypatch.setattr(fe, "_fact_key_seed", _legacy_seed)
    try:
        # 变异必须真的生效，否则本条只是「看起来像 A/B」的常绿用例
        assert fe._fact_key_seed is _legacy_seed
        assert fe.normalize_key("坡角" + ALPHA) == fe.normalize_key("坡角" + BETA)
        out = fe.merge_and_deduplicate([
            _item("坡角" + ALPHA, "30"),
            _item("坡角" + BETA, "45"),
        ])
        assert len(out) == 1, "退回旧写法后必须重现「事实被合并」"
        assert out[0].has_conflict is True, "退回旧写法后必须重现「凭空假矛盾」"
    finally:
        monkeypatch.undo()
        fe.normalize_key.cache_clear()


def test_M2_dropping_separator_folding_is_detected_by_group_a(monkeypatch):
    """M2：把种子换成「直接用原名」的最省事写法 → A 组（向后兼容）会立刻变红。

    这是给「既然要保证单射，干脆 `md5(name)` 算了」这条捷径准备的反向锚：
    它确实修得了 B 组，但会让 A 组那批既有 key 全部漂移 ⇒ 既有库里手工事实
    的 ``_protected_by_key`` 失配。本条证明 A 组能抓住这种改法。

    ⚠️ 同样必须 ``cache_clear()``（理由见 M1 的 docstring）。
    """
    import app.services.facts_extractor as fe

    fe.normalize_key.cache_clear()
    monkeypatch.setattr(fe, "_fact_key_seed", lambda name: name.strip())
    try:
        drifted = [n for n in SEPARATOR_ONLY_NAMES
                   if fe._fact_key_seed(n) != _legacy_seed(n)]
        assert drifted, "A1 失效：某种会让既有 key 漂移的写法没被 A 组抓住"
        assert fe.normalize_key("基坑 深度") != _legacy_key("基坑 深度")
    finally:
        monkeypatch.undo()
        fe.normalize_key.cache_clear()
