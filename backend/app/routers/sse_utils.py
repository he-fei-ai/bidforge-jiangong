"""SSE 处理器的纯工具函数（文本处理 / 类型归一化 / 中文编号）。

从 sse_handlers.py 拆出，仅含无副作用、不依赖 db / AI 调用的纯函数。
sse_handlers.py 通过 ``from .sse_utils import ...`` 引用，保持向后兼容。
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# 中文数字编号
# ---------------------------------------------------------------------------
_CN_NUMBERS = ["", "一", "二", "三", "四", "五", "六", "七", "八", "九", "十",
                "十一", "十二", "十三", "十四", "十五", "十六", "十七", "十八", "十九", "二十"]
_CN_DIGITS = ["零", "一", "二", "三", "四", "五", "六", "七", "八", "九"]


def _cn_number(n: int) -> str:
    """1-99 的序数 → 中文数字（章节号用）。

    ✅ 增强：旧实现只有到「二十」的静态表，超过 20 章的一级章节号会退化为
    阿拉伯数字（"第21章"），与文档其它位置的中文编号风格不一致。
    现 1-99 全量支持（21 → 二十一、30 → 三十、35 → 三十五），
    100 及以上仍回退阿拉伯数字（专项方案目录极少出现）。
    """
    if n <= 0:
        return str(n)
    if n < len(_CN_NUMBERS):
        return _CN_NUMBERS[n]
    if n >= 100:
        return str(n)
    tens, ones = divmod(n, 10)
    return _CN_DIGITS[tens] + "十" + (_CN_DIGITS[ones] if ones else "")


# ---------------------------------------------------------------------------
# Markdown / 文本尾处理（续写上下文保护）
# ---------------------------------------------------------------------------
def _next_fence_open(tail: str) -> int:
    """tail 中首个围栏开/闭标记的位置（反引号或波浪号，取较早者）。"""
    a = tail.find("```")
    b = tail.find("~~~")
    cands = [x for x in (a, b) if x >= 0]
    return min(cands) if cands else -1


def _safe_tail(text: str, limit: int = 2000) -> str:
    """取正文尾部最多 limit 字（续写提示词的「前文结尾」上下文）。

    两层围栏保护（反引号 ``` 与波浪号 ~~~ 同口径、各自独立）：
      · 用**整篇正文**判断切点奇偶（切点前某类围栏数为奇数 ⇒ 切点在对应代码块内）
        → 前移跳过该块剩余部分与闭合围栏，使上下文从散文开始；
      · 再对尾部做「文末未闭合块」裁剪 —— 某类围栏数为奇数说明最后一个块未闭合，
        整块丢弃（宁缺勿滥），两类围栏独立裁剪、互不干扰。

    行边界：非围栏场景下回退到最近换行，避免以半行开头。
    """
    if not text:
        return ""
    if len(text) <= limit:
        tail = text
    else:
        cut = len(text) - limit
        bt_before = text[:cut].count("```")
        tl_before = text[:cut].count("~~~")
        if bt_before % 2 == 1 or tl_before % 2 == 1:
            # 切点落在某类代码块内部：跳过该块剩余内容与其闭合围栏
            tail = text[cut:]
            nxt = _next_fence_open(tail)
            if nxt < 0:
                return ""            # 该块一直未闭合到结尾 → 无可用散文尾部
            # 跳过完整围栏标记（支持 4+ 反引号/波浪号围栏；旧实现只跳 3 字符，
            # 对 ````/~~~~ 围栏会残留 1 个标记字符，污染续写上下文）。
            i = nxt
            while i < len(tail) and tail[i] in ("`", "~"):
                i += 1
            tail = tail[i:]
            if tail.startswith("\n"):
                tail = tail[1:]
        else:
            # 回退到最近换行边界（首行过长时保留，避免上下文过短）
            tail = text[cut:]
            nl = tail.find("\n")
            if 0 <= nl < 200:
                tail = tail[nl + 1:]
    # 文末未闭合块裁剪：两类围栏各自独立裁剪（反引号行为保持与历史一致）
    if tail.count("```") % 2 == 1:
        last = tail.rfind("```")
        tail = tail[:last] if last > 0 else ""
    if tail.count("~~~") % 2 == 1:
        last = tail.rfind("~~~")
        tail = tail[:last] if last > 0 else ""
    return tail.strip()


def _dedup_continuation(prev: str, cont: str, min_overlap: int = 40) -> str:
    """检测续写内容与前文尾部的段落级重复，返回去除重复前缀后的续写文本。

    弱模型高频把最后一段原样重写一遍。按续写文本的段落前缀与前文尾部匹配，
    整段已在前文出现（≥min_overlap 字）即剥离；再处理续写开头与前文结尾
    的字符级重叠（模型从某句中间接续的场景）。

    ✅ BUG 修复（2026-09-16）：字符级重叠原实现用
    `for probe_len in range(min(300, len(b)), 40, -20)`（步长 20）试探
    「b 的前缀在 tail 中出现」——只有重叠长度恰好落在 len(b) − 20k 这一串
    离散点上才会命中，其余情况全部漏检（命题：真实文本的重复长度是任意的）。
    漏检的后果是续写把前文最后一段/句子原样复述一遍，正文出现成段重复。
    现改为**精确求最长重叠**（两层，均为 O(300×n) 的可忽略开销）：
      ① 接缝对齐（主路径）：b 的前缀恰好是 tail 的后缀 —— 模型重抄了刚看到的
         结尾再往下写，这是最常见的重复形态；
      ② 兜底：b 的长前缀在 tail 任意位置原文出现（弱模型跨段复述）。
    命中即剥离重叠部分（并去掉句首残留标点），无需再依赖步长运气。
    """
    if not cont:
        return cont
    # ✅ 健壮性：prev 可能为 None（调用方漏传/章节正文缺失），
    #    旧实现直接 prev[-3000:] 会抛 TypeError 并中断续写链路。
    tail = (prev or "")[-3000:]
    rest = cont.strip()
    # 逐段剥离：只要 cont 开头的整段已在前文尾部出现
    for _ in range(8):
        if not rest:
            break
        first_nl = rest.find("\n")
        head = rest if first_nl < 0 else rest[:first_nl]
        head_s = head.strip()
        if len(head_s) >= min_overlap and head_s in tail:
            rest = rest[first_nl + 1:].lstrip("\n") if first_nl >= 0 else ""
            continue
        break
    # 字符级：续写开头恰是前文结尾的延续重复（求最长重叠前缀）
    b = rest.lstrip()
    if len(b) >= min_overlap:
        max_probe = min(300, len(b))
        cut = 0
        for n in range(max_probe, min_overlap - 1, -1):
            if tail.endswith(b[:n]):
                cut = n
                break
        if not cut:
            for n in range(max_probe, min_overlap - 1, -1):
                if b[:n] in tail:
                    cut = n
                    break
        if cut:
            b = b[cut:].lstrip("，。；、\n ")
    return b or cont.strip()


# ---------------------------------------------------------------------------
# 模型返回值稳健归一化（防弱模型把布尔/列表写成字符串）
# ---------------------------------------------------------------------------
def _coerce_bool(value, default: bool = False) -> bool:
    """把模型返回的"布尔"值稳健归一化为 bool。

    ✅ BUG 修复：旧实现用 `not review_obj.get("passed", True)` 直接判定审核结果，
    而弱模型常把 passed 写成字符串（"false" / "no" / "0"）。Python 中非空字符串
    恒为真 → "审核不通过"被误判为通过，"审核-自动修复"链路被整轮静默跳过，
    用户以为目录已按审核建议修正，实际原样返回。
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("true", "yes", "y", "1", "通过", "是", "pass", "passed", "ok"):
        return True
    if text in ("false", "no", "n", "0", "不通过", "否", "fail", "failed"):
        return False
    return default


def _coerce_suggestions(value) -> list[str]:
    """把模型返回的 suggestions 稳健归一化为 list[str]。

    ✅ BUG 修复：旧实现直接对 suggestions 做 `"; ".join(...)` 与
    `suggestions + [...]`：
      1) 模型把 suggestions 写成字符串时（很常见），`"; ".join("补监测方案")`
         会按"单个字符"拆分 → 修复提示词退化为 "补; 充; 监; 测; 方; 案"；
      2) 异常分支里的 `"字符串" + ["..."]` 抛 TypeError，且该语句位于 except
         块内，异常会逃逸出 _review_and_fix_outline，把整次目录生成打成失败。
    """
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, (list, tuple, set)):
        out: list[str] = []
        for item in value:
            if isinstance(item, str):
                t = item.strip()
                if t:
                    out.append(t)
            elif isinstance(item, dict):
                for key in ("suggestion", "text", "item", "content", "value"):
                    v = item.get(key)
                    if v is not None and str(v).strip():
                        out.append(str(v).strip())
                        break
            elif item is not None:
                t = str(item).strip()
                if t:
                    out.append(t)
        return out
    text = str(value).strip()
    return [text] if text else []
