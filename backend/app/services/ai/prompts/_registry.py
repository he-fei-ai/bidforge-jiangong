"""提示词注册表。

所有提示词通过 _reg() 注册到 _ALL_PROMPTS 全局字典中。
供 routers 提供给前端展示和编辑使用。

增强功能：
- 变量提取与校验：自动提取模板中的 {变量占位符} 和 __变量名__，支持运行时验证
- 模板清洗：统一换行符、移除零宽字符
- 统一变量渲染：render_prompt() 函数一次性替换所有变量并验证
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

_ALL_PROMPTS: dict[str, dict] = {}

# ✅ R6 修复（2026-09-22）：启动时提示词模板变量预检。
#
# 背景：logs/backend.log 里曾出现 40 次同一签名告警
#     "Prompt 'content_generation_system' has missing variables (not passed in call): ['max']"
# 这类告警每次正文生成都会被触发（正文生成调用次数 × 章节数），既淹没真实错误，
# 又掩盖了"真的缺变量"（如 scheme_name / section_number 遗漏传参）的信号。
#
# 做法：给"典型误报变量"打白名单 —— 这些短名字几乎总是来自 JSON 示例（如 {max: ...}）
#     或代码块里的字面变量，而非真的需要 render_prompt 传入。
#     白名单命中时 validate_prompt_variables 会跳过，不再产生告警。
#     真实缺变量（scheme_name / section_number 等业务变量）不受影响，仍会告警。
#
# 白名单依据：`{max}` 是常见 JSON 示例字段；`{tasks}` `{type}` 等短字段同理。
# 长度 1~3 的变量名如果**同时**在提示词中出现了 JSON 示例（含 ":" 或 "["）则视为误报。
# 保守起见：白名单只影响告警，不影响 render_prompt 的实际替换行为。
#
# ✅ 2026-09-26（T8 · 白名单收窄）：旧版 40 个词条中含大量真实业务变量名
#    （title/name/content/status/type/value/count/total/...）。实测全部注册
#    模板后确认：仅 ``max`` 一个词条真正被「JSON 示例误报」路径依赖
#    （content_generation_system 的 {max} 示例）；code/content 虽在白名单，
#    但其上下文判定本就不像 JSON（FP=0），白名单对它们从未生效。
#    因此收窄为「极短名（≤3 字符）」—— 这些名字几乎不可能是业务变量
#    （业务变量均为 scheme_name / section_number 这类长名），
#    既保留 {max}/{min} 误报抑制，又杜绝未来真业务变量（如 {title}）被漏报。
#    已用探针验证：收窄后对全部现有模板的 FP 判定逐条一致（零行为变化）。
_FALSE_POSITIVE_VARS = frozenset({
    # 极短常见 JSON 字段名（长度 ≤3），业务变量名均 ≥4 字符
    "max", "min", "id", "ok", "no", "to", "on", "off",
    "url", "api", "tag", "key", "val",
})


def _looks_like_json_example(context: str, var: str) -> bool:
    """判断变量在模板里的**所有出现**是否都像 JSON 示例或 LaTeX 下标。

    ✅ 保守匹配：某次出现满足以下条件之一才视为「该处是示例」：
    1. 变量后面 10 个字符内出现 ASCII 冒号 ":" 或英文方括号 "["/"{"（JSON 特征）；
    2. 变量前面 15 个字符内出现 "$" 且后面 10 个字符内有 "$"（LaTeX 下标如 `$p_{max}$`）。
    中文冒号"："不算，避免"最大数量：{max}"这类自然语言被误判。

    ✅ 2026-09-26（T8-b · 首处判定缺陷）：旧实现 ``context.find(marker)`` 只检查
    **第一处**出现 —— 若同一变量先以 JSON 示例出现、后有真实使用（如示例
    `{max: 200}` 之后正文又写「不超过 {max} 条」），第一处命中示例特征就会
    把真实使用的缺失告警一并吞掉。现改为**全部出现**都像示例才判定误报：
    任何一处出现是真实使用（tail 无 JSON 特征）即返回 False，保留告警。
    变量完全未出现（marker 找不到）返回 False（谈不上误报）。
    """
    marker = f"{{{var}}}"
    start, found = 0, False
    while True:
        idx = context.find(marker, start)
        if idx < 0:
            break
        found = True
        start = idx + len(marker)
        tail = context[idx + len(marker): idx + len(marker) + 10]
        if (":" in tail) or ("[" in tail) or ("{" in tail):
            continue
        # LaTeX 下标：$p_{max}$ —— 前 15 字符里有 `$` 且后 10 字符里有 `$`
        head = context[max(0, idx - 15): idx]
        if "$" in head and "$" in tail:
            continue
        return False  # 该处出现是真实使用 → 整体不算误报
    return found


def _is_false_positive(key: str, var: str, template: str) -> bool:
    """判断 (key, var) 组合是否是 JSON 示例导致的误报。

    ✅ 保守策略：只有满足以下**全部**条件才视为误报：
    1. 变量名在 _FALSE_POSITIVE_VARS 白名单里；
    2. 变量在模板中的实际使用上下文**看起来像 JSON**（含冒号/方括号）；
    3. 模板整体不是明显的业务上下文（长度 > 200 字符，含较多中文）。

    不满足任一条则保留告警，让运维看到"真实"缺变量。

    ✅ 可选区块例外（2026-09-26）：变量在模板中**独占一整行**
    （`^\\s*\\{var\\}\\s*$`）时，语义是「有值才注入这一段」—— 调用方
    不传时 render_prompt 会整行丢弃，不会留下残留占位符。因此
    「未传」不是缺陷，不应报缺失（否则每次调用都刷无意义 WARNING）。
    ⚠️ 仅供**运行期缺失告警**（validate_prompt_variables）使用；
    契约漂移校验（check_prompt_variables）走的是「模板里到底有没有这个
    占位符」的语义，不能套用本例外 —— 否则整行区块会被误判成
    「声明了但没用」（declared_not_used）而报假漂移。
    """
    if var not in _FALSE_POSITIVE_VARS:
        return False
    return _looks_like_json_example(template, var)


#: 整行独占的可选区块占位符（行内无其它文字）。
#:
#: ✅ 2026-09-27（BUG-P0-1 · 判据收敛）：本常量是「可选区块」的**唯一事实源**，
#:    同时被三处消费，历史上三处各写一份且互相分叉：
#:      ① 渲染侧删行（render_prompt）
#:      ② 校验侧豁免（_is_optional_block_var / validate_prompt_variables）
#:      ③ 残留检测（has_residual_placeholders 用的 _RESIDUAL_PLACEHOLDER_PATTERN）
#:    旧实现 ① 用 ``[A-Za-z0-9_]*``（≥1 字符）、② 用 ``[A-Za-z_]\w{1,}``（≥2 字符），
#:    于是单字符变量 ``{x}``「被删行却仍报缺失」，非 ASCII 变量 ``{变量}``
#:    「被豁免却不删行、字面量残留进模型」—— 同一判据两个方向都分叉。
#:    现统一为：首字符 ASCII 字母/下划线，**其后 ≥0 个** ``\w`` 字符，
#:    即**整名 ≥1 字符**（``{x}`` 也算可选区块），非 ASCII 一律不算。
#:
#: ✅ 2026-10-07（BUG-C · 注释与代码/测试三方矛盾）：上句旧注释写的是
#:    「至少 1 个 \w 字符」，读起来像「整名 ≥2 字符」，而正则其实是 ``\w*``
#:    （整名 ≥1 字符）。更麻烦的是 2026-09-27 引入的**回归测试明确锁定**
#:    单字符也要算可选区块（`TestOptionalBlockParity::
#:    test_single_char_variable_is_treated_as_optional_block`）—— 也就是说
#:    「整名 ≥1 字符」才是被测试钉住的真实契约，注释是错的。
#:    ⚠️ 不要顺着旧注释把 ``\w*`` 改成 ``\w{1,}``：那会让 ``{x}`` 从
#:    「删行 + 豁免告警」变成「字面量残留进模型提示词」，两侧判据再次分叉。
#:    注：``{x}`` 不会被 ``extract_variables`` 提取（它用 ``\w{1,}``），
#:    因此「删行却不报缺失」在这个形态上是**自洽的**：没被提取 ⇒ 不会报缺失。
_OPTIONAL_BLOCK_RE = re.compile(
    r"^[ \t]*\{([A-Za-z_]\w*)\}[ \t]*$", re.MULTILINE)


def _is_optional_block_var(var: str, template: str) -> bool:
    """变量在模板中是否独占一整行（= 可选区块，未传即整行丢弃）。"""
    if not template or not var:
        return False
    for m in _OPTIONAL_BLOCK_RE.finditer(template):
        if m.group(1) == var:
            return True
    return False

# ⚠️ 排除 JSON 对象键上下文：`{key}:`（ASCII 冒号）是 JSON 示例而非变量占位符。
#    否则提示词里未加引号的 JSON 示例（如 {tasks: [...]}）会被误当作变量：
#    既污染变量列表 / 触发"缺失变量"误告警，又可能在 render 阶段被静默替换、
#    从而篡改提示词中的 JSON。正规占位符后接中文冒号「：」/换行/标点，不受影响。
_VARIABLE_PATTERN = re.compile(
    r"\{([A-Za-z_]\w{1,})\}(?!\s*:)"  # {variable}：首字符字母/下划线，总长 ≥2，排除 {key}:
    r"|"
    r"__([A-Z][A-Z0-9_]{2,})__"       # __VARIABLE__ 格式：必须大写字母开头，总长 ≥5（排除纯下划线）
)
_RESIDUAL_PLACEHOLDER_PATTERN = re.compile(
    r"\{[A-Za-z_]\w{1,}\}(?!\s*:)"
    r"|"
    r"__[A-Z][A-Z0-9_]{2,}__"
)
_ZERO_WIDTH_PATTERN = re.compile(r"[\u00AD\u200B\u200C\u200D\uFEFF]")


def _reg(key: str, category: str, label: str, value: str,
         requires: list[str] | None = None) -> str:
    """注册一个提示词到元信息表，自动提取变量列表。

    BUG-FIX（提示词重置失效）：
      原实现只有 "content" 一个字段，update_prompt() 会原地改写它，
      导致"出厂默认值"被用户编辑内容覆盖，POST /prompts/{key}/reset
      实际恢复的是编辑后的内容而非出厂默认。
      现新增只写一次的 "default_content" 字段作为不可变基线。

    ✅ G4 变量契约（2026-09-24）：可选声明 ``requires`` —— 调用方**必须**提供的
    变量白名单。不声明（默认 None）= 完全向后兼容，不做任何契约校验；
    声明后由 :func:`check_prompt_variables` 在启动期比对「声明」与「模板实际
    占位符」，双向漂移（声明了模板没用 / 模板用了没声明）都会被打 WARNING。
    这是把「缺变量只能靠运行期告警发现」升级为「启动期即可发现」的护栏。
    """
    # ✅ 变量列表只含「调用方需提供的变量」：{SHARED_*} 是运行时由
    #    _cache._resolve_shared_keys 动态解析的共享片段引用，并非调用方入参，
    #    不应进入 variables 列表（否则前端编辑器把它当「必需变量」展示、
    #    审计 diff 把启用/停用一段共享规则误记为「变量增删」）。
    variables = extract_user_variables(value)
    _ALL_PROMPTS[key] = {
        "key": key,
        "category": category,
        "label": label,
        "content": value,
        # 出厂默认基线：一经注册永不修改，仅供 reset / 对比使用
        "default_content": value,
        "variables": variables,
        # ✅ 2026-09-25（BUG-A · 契约漂移基线污染）：出厂默认变量集合基线。
        #   旧实现只有 variables 一个字段，update_prompt() 会原地改写它；
        #   而 check_prompt_variables() 拿 meta["variables"] 当「实际占位符」
        #   与契约表比对 —— 用户一旦通过编辑器改了模板占位符，内存注册表就
        #   被污染，下次启动期校验必然误报漂移（哪怕出厂模板与契约一致）。
        #   现在出厂基线单独保存、永不被 update_prompt/reset_prompt 修改，
        #   check_prompt_variables 只以出厂基线为准，契约漂移检测才能真正可靠。
        "default_variables": list(variables),
        # 变量契约白名单：声明 = 强制校验；未声明（None）= 不校验
        # ✅ R39（2026-10-04 · D6 根因收口）：旧实现是
        #   ``sorted(requires) if requires else None`` —— 空列表 `[]`
        #   （显式声明「本模板零变量」）与 `None`（未声明）在 Python 里同为假值，
        #   于是**注册时就已把显式空声明压成 None**，下游 `requires is None`
        #   的「跳过校验」判据仍会把零占位模板脱管。R38 只把判据侧改成
        #   `requires is None`，注册侧这处压扁一直没动 —— 属同一根因的另一半。
        #   现按 `requires is None` 判别：空列表原样落库为 `[]`。
        #   行为影响：零占位模板的 `requires` 由 None 变 []，**只影响
        #   check_prompt_variables 的「是否校验」判据**（这正是显式声明的意图），
        #   渲染、变量提取、DB 覆盖语义逐字不变。
        "requires": None if requires is None else sorted(requires),
    }
    return value


#: 惰性注册幂等锁（R39 · T1）。False = 尚未触发过注册钩子。
#:
#: ✅ 必须**先于**任何出口函数定义存在：钩子被挂在 ``_apply_variable_contracts``、
#: ``check_prompt_variables``、``list_prompts`` 等 6 个公共出口上，而
#: ``prompts/__init__.py`` 在本模块 import 完成后立刻调用
#: ``_apply_variable_contracts()`` —— 声明晚于首次调用会直接 NameError
#: （首版正是删旧块时把这条声明一起带走，3 个测试文件在收集期就 NameError）。
_LAZY_REGISTERED = False


def register_lazy_prompts() -> None:
    """幂等地注册「字面量定义在本包之外」的提示词。

    ✅ R39 · T1（2026-10-04）：投标分析域的 19 条提示词（18 条解析项 user +
    1 条通用 system）字面量在 ``services/bid_analysis_service.py``（唯一事实源，
    未搬家），而那个模块**不被每条读提示词的路径导入**。改在
    ``prompts/__init__.py`` 反向 import 它会与「service 又 import prompts」
    形成循环依赖，故用本钩子：``_registry`` 的所有公共出口在读注册表前
    先触发一次，幂等锁保证只注册一次。

    关联回归实测：只挂在 ``_cache.get_prompt`` 未命中分支时，启动期校验
    （``check_prompt_variables``）与管理接口（``list_prompts``）共 9 处护栏
    因「契约指向未注册的模板」失败 —— 读注册表的路径不会走 get_prompt。

    异常策略：任何失败仅记一次 WARNING、不重试、不抛 —— 注册失败不得阻断
    读取（调用侧自带的出厂字面量回退仍然有效）。

    ✅ R39 BUGFIX（2026-10-05）：幂等锁必须在 **import 成功之后**才置位，
    而不是在 try 之前无条件置位。原因：本模块尾部首次调用
    ``_apply_variable_contracts()`` 触发本钩子时，``bid_analysis_service``
    顶部的 ``from app.services.ai.prompts._cache import get_prompt`` 会遇到
    ``_cache`` 尚未完成加载的循环导入 —— 抛 ``ImportError`` 被本钩子的
    ``except`` 吞掉。若此时 ``_LAZY_REGISTERED`` 已被置 True，后续所有 6 处
    调用点（``get_prompt`` / ``list_prompts`` / ``update_prompt`` /
    ``_apply_variable_contracts`` …）都会短路 return，19 条 bid_ 键**永久缺失**
    （R39 T1 的 6 项护栏因此全线失败）。修复后异常路径下锁保持 False，
    后续任一次读注册表都会重试一次；递归安全 —— 失败后不再进入 try，
    成功路径只置位一次。
    """
    global _LAZY_REGISTERED
    if _LAZY_REGISTERED:
        return
    try:
        from app.services import bid_analysis_service  # noqa: F401
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger("prompts_registry").warning(
            "惰性注册投标分析提示词失败（可重试）: %s", e)
    else:
        _LAZY_REGISTERED = True


def get_default_prompt(key: str) -> str:
    """获取提示词的出厂默认内容（不受用户编辑影响）。

    用于「恢复默认」与「默认/当前是否被修改」判断。
    """
    register_lazy_prompts()
    meta = _ALL_PROMPTS.get(key)
    if not meta:
        return ""
    return meta.get("default_content", meta.get("content", ""))


def is_prompt_modified(key: str) -> bool:
    """判断提示词是否被用户修改过（与出厂默认比较）。"""
    register_lazy_prompts()
    meta = _ALL_PROMPTS.get(key)
    if not meta:
        return False
    return meta.get("content", "") != meta.get("default_content", "")


def get_prompt_variables(key: str) -> list[str]:
    """获取指定提示词所需的变量列表。"""
    register_lazy_prompts()
    meta = _ALL_PROMPTS.get(key)
    if meta:
        return meta.get("variables", [])
    return []


def validate_prompt_variables(key: str, **kwargs) -> list[str]:
    """验证提示词变量是否全部提供，返回缺失变量列表。

    ✅ R6 修复（2026-09-22）：过滤"JSON 示例误报"。
    背景：`{max}` 这类极短字段常出现在提示词的 JSON 示例里（如 `{max: 200}`），
    被 _VARIABLE_PATTERN 误抓后进入 variables 列表，导致 render_prompt 反复告警
    "missing variables"，淹没真实错误信号。
    过滤规则见 _is_false_positive()。真实缺变量（业务语义名）不受影响。
    """
    required = get_prompt_variables(key)
    kwargs_lower = {k.lower(): v for k, v in kwargs.items()}
    template = _ALL_PROMPTS.get(key, {}).get("content", "")
    # ✅ 整行独占的可选区块（2026-09-26）：未传时 render_prompt 整行丢弃，
    #    不留残留占位符 → 不算"缺失"，否则每次调用都刷无意义 WARNING。
    missing = [v for v in required
               if v.lower() not in kwargs_lower or kwargs_lower[v.lower()] is None
               if not _is_optional_block_var(v, template)]
    # 过滤误报：保留业务语义变量，只抑制"JSON 示例"类短字段
    missing = [v for v in missing if not _is_false_positive(key, v, template)]
    return missing


def clean_prompt_text(text: str) -> str:
    """清洗提示词文本：统一换行符、移除零宽字符、去首尾空白。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _ZERO_WIDTH_PATTERN.sub("", text)
    return text.strip()


def extract_variables(template: str) -> list[str]:
    """从模板字符串中提取所有 {变量名} 或 __变量名__ 占位符（原始，含 SHARED_*）。"""
    return sorted(set(v for match in _VARIABLE_PATTERN.findall(template) for v in match if v))


def _is_shared_reference(name: str) -> bool:
    """判断变量名是否为 {SHARED_*} 共享片段引用。

    这类占位符由 ``_cache._resolve_shared_keys`` 在运行时解析为对应共享
    提示词的当前内容（DB 优先），并非调用方需提供的入参变量。
    """
    return name.startswith("SHARED_")


def extract_user_variables(template: str) -> list[str]:
    """提取「调用方需提供的变量」，排除运行时自动解析的 {SHARED_*} 共享片段引用。

    与 ``extract_variables`` 的区别：本函数用于注册表 variables 字段、前端
    列表展示与审计 diff —— 这些场景只关心「调用方需传参的变量」，
    SHARED_* 由系统自动注入，混入会误导编辑器与审计口径。
    """
    return sorted(set(v for v in extract_variables(template)
                      if not _is_shared_reference(v)))


def has_residual_placeholders(text: str) -> bool:
    """检查文本中是否还有未替换的变量占位符。"""
    return bool(_RESIDUAL_PLACEHOLDER_PATTERN.search(text))


def _render_by_line(template: str, pattern: re.Pattern | None,
                    replace) -> str:
    """逐行渲染模板，并把「本次未传值的整行独占占位符」整行丢弃。

    ✅ 2026-09-27（BUG-P0-1 · 注入内容被静默删行）：
    旧实现先对全文做一次替换、再用 ``re.sub(r"^[ \\t]*\\{\\w+\\}[ \\t]*$", ...)``
    无条件删行。这条正则作用在**替换结果**上，于是「调用方注入的外部原文里
    恰好有一行形如 ``{heading}``」也会被整行删掉 —— 资料正文、模型原始输出、
    Mermaid 代码都被篡改，且无任何日志。

    本实现把「行」这个概念限定在**模板自身**：

    1. 先按 ``_OPTIONAL_BLOCK_RE`` 判定该行是否「整行就是一个占位符」；
    2. 是且该变量本次没传（``pattern`` 不匹配它 / ``kwargs`` 里没有）→ 丢整行；
    3. 其余情况只在本行内做替换 —— 替换出来的多行内容**原样保留**，
       不会再被「删行」逻辑二次扫描。

    因此「第 3 步产生的多行内容」与「第 2 步的行判定」在结构上不可能互相
    干扰，BUG-P0-1 从根上消失，而不是靠加特判绕过。

    :param template: 模板原文（未渲染）。
    :param pattern: 已编译的替换正则；``None`` 表示无任何变量可替换
                    （此时只做第 2 步的删行）。
    :param replace: ``re.sub`` 的替换回调。
    """
    out: list[str] = []
    # 保留原文的行尾形态（\n / \r\n / 无尾换行），避免仅因渲染就改变文本形态。
    # 注意：split("\n") 产生的最后一个元素对应「原文末尾换行之后」的空串，
    # 它本身**不带**换行 —— 逐行补 eol 时必须跳过它，否则会凭空多出一个 \n。
    raw_lines = template.split("\n")
    last_idx = len(raw_lines) - 1
    for idx, raw in enumerate(raw_lines):
        eol = "" if idx == last_idx else "\n"
        line = raw
        if idx != last_idx and line.endswith("\r"):
            line, eol = line[:-1], "\r\n"
        m = _OPTIONAL_BLOCK_RE.match(line)
        if m and (pattern is None or not pattern.search(line)):
            # 整行独占、且本次没有对应实参 → 可选区块，整行丢弃
            continue
        out.append((pattern.sub(replace, line) if pattern else line) + eol)
    return "".join(out)


def render_prompt(template: str, **kwargs) -> str:
    """统一的变量渲染函数，替换模板中的所有变量占位符。

    支持两种变量格式（均为大小写不敏感）：
    - {variable_name} 格式
    - __VARIABLE_NAME__ 格式

    ✅ 单遍替换修复：原实现对「已替换后的文本」逐变量做 re.sub（多遍替换），
    用户资料（material）/ 模型原始输出（invalid_content）等先注入的大段文本中
    若恰好含与后续变量同名的占位符字样（如 {summary}、{heading}），会被
    二次替换，造成提示词结构错位 / 内容污染。现改为单遍扫描一次完成，
    替换值不再被重新扫描。
    """
    lookup = {str(k).lower(): v for k, v in kwargs.items() if v is not None}
    if lookup:
        names = sorted((re.escape(n) for n in lookup), key=len, reverse=True)
        # (?!\s*:) 与提取正则一致：JSON 对象键（{key}:）不参与替换，避免篡改 JSON 示例
        pattern = re.compile(
            r"\{(" + "|".join(names) + r")\}(?!\s*:)"
            r"|"
            r"__(" + "|".join(names) + r")__",
            flags=re.IGNORECASE,
        )
    else:
        pattern = None

    def _replace(m: re.Match) -> str:
        name = (m.group(1) or m.group(2) or "").lower()
        return str(lookup[name])


    #
    # ✅ 2026-09-27（BUG-P0-1 · 注入内容被静默删行，数据丢失）：
    #    旧实现在替换**之后**对 result 无条件删「整行就是一个 {word}」
    #    的行，于是「注入值里恰好有一行形如 {word}」也被删掉。而调用方
    #    注入的恰好是**外部原文**（facts_extractor 的 material、
    #    json_response 的 invalid_content、charts 的 code、consistency_scanner
    #    的 section_content…）—— 实测用真实模板 facts_json_fix_system
    #    复现：invalid_content 里的 ``{heading}`` 整行消失，AI 拿到的是被悄悄
    #    篡改过的「待修复原文」，比不修更糟，且与紧邻的残留检测
    #    （那段明确用 template 而非 result）自相矛盾。
    #    现改为**逐行处理**：只对「模板自身独占一整行的占位符」
    #    判是否删行，注入值永远不参与行判定；且与 _is_optional_block_var
    #    共用 _OPTIONAL_BLOCK_RE（唯一事实源），消除「删行正则 vs 豁免正则」分叉。
    result = _render_by_line(template, pattern, _replace)

    # ✅ 只在【模板】层面检测未解析占位符：
    #    旧实现检测的是"渲染后文本"，会把注入的文档正文 / 模型原始输出中
    #    形如 {xxx} 或 __XXX__ 的内容误报为"未解析占位符"，既产生日志噪音，
    #    又误导排查方向（例如把标书正文里的填空下划线当成提示词变量缺失）。
    if has_residual_placeholders(template):
        template_vars = extract_variables(template)
        kwargs_lower = {k.lower() for k in kwargs if kwargs[k] is not None}
        unresolved = [v for v in template_vars
                      if v.lower() not in kwargs_lower
                      and not v.startswith("SHARED_")]
        # ✅ 2026-09-23（正文生成深度审计 · P2 日志噪音）：此处此前**没有**套用
        #    _is_false_positive 过滤，与 validate_prompt_variables() 的两条告警
        #    路径口径分叉 —— 后者早已过滤（R6），本处仍在报。运行库实测
        #    2026-09-23 20:54~20:55 单个正文任务打出 6 条
        #    "Prompt template has unresolved placeholders: ['max']"
        #    （logs/backend.log，trace=d283bb90c239），历史上更曾有 40 次/天。
        #    `{max}` 来自提示词里的 JSON 示例，并非真缺变量，
        #    噪音会淹没真实的 scheme_name/section_number 漏传信号
        #    （AGENTS.md §5.3 已登记该遗留项）。现与 validate 路径共用同一判据。
        unresolved = [v for v in unresolved
                      if not _is_false_positive("", v, template)
                      # ✅ 2026-10-02（检查点前置）：与 validate_prompt_variables
                      #    同一口径 —— 整行独占的可选区块（未传即整行丢弃）不是
                      #    「未解析」，否则开关型注入点每次关闭都刷一条 WARNING。
                      if not _is_optional_block_var(v, template)]
        if unresolved:
            logger.warning("Prompt template has unresolved placeholders: %s", unresolved)

    return result


def get_prompt(key: str) -> str:
    """获取提示词原始内容（无变量渲染）。"""
    p = _ALL_PROMPTS.get(key)
    if not p:
        raise KeyError(f"提示词不存在: {key}")
    return p["content"]


def render(key: str, **kwargs) -> str:
    """按变量渲染提示词（DB 优先，回退硬编码）。

    走 _cache.get_prompt() 逻辑，确保前端编辑保存到 DB 的提示词生效。
    """
    from app.services.ai.prompts._cache import get_prompt as _cache_get
    return _cache_get(key, **kwargs)


def list_prompts(category: str | None = None) -> list[dict]:
    """列出所有提示词，可按分类过滤。

    附带 default_content / modified 字段，便于前端展示"已修改"标记
    与提供"恢复默认"预览。
    """
    register_lazy_prompts()
    items = [dict(p) for p in _ALL_PROMPTS.values()]
    for p in items:
        p["modified"] = p.get("content", "") != p.get("default_content", "")
    if category:
        items = [p for p in items if p["category"] == category]
    return items


def update_prompt(
    key: str, content: str  # noqa: F811  content 与文件尾导入的 content 模块同名（刻意，见文件尾）
) -> bool:
    """更新提示词内容（仅内存，持久化由路由层处理）。

    BUG-FIX：仅改写 "content"，保留不可变的 "default_content"。
    """
    if key not in _ALL_PROMPTS:
        return False
    _ALL_PROMPTS[key]["content"] = content
    _ALL_PROMPTS[key]["variables"] = extract_user_variables(content)
    return True


def reset_prompt(key: str) -> str | None:
    """将提示词内容恢复为出厂默认，返回恢复后的内容（key 不存在返回 None）。"""
    if key not in _ALL_PROMPTS:
        return None
    default = _ALL_PROMPTS[key].get("default_content", _ALL_PROMPTS[key].get("content", ""))
    _ALL_PROMPTS[key]["content"] = default
    _ALL_PROMPTS[key]["variables"] = extract_user_variables(default)
    return default


# ---------------------------------------------------------------------------
# ✅ G4 变量契约表（2026-09-24 · 遗留项 4 闭环）
# ---------------------------------------------------------------------------
#: 模板 key → 调用方**必须**提供的变量白名单。
#:
#: 为什么集中在这里而不散落在各模板文件：
#:   _reg() 的模板正文是多行三引号字符串，在其调用处追加参数极易误改到
#:   引号边界；集中声明一处维护、一处校验，且新增/删除模板时契约一目了然。
#:
#: 声明口径 = **已核对全部渲染调用点的实际传参**（不是猜的）：
#:   · content_generation_system  ← sse_handlers.py 正文渲染（4/4 全传）
#:   · content_continue_system    ← sse_handlers.py 续写渲染（3/3 全传）
#:   · outline_short_system       ← sse_handlers.py 短方案目录（4/4 全传）
#:   · outline_level1_system      ← sse_handlers.py 长方案一级目录（6/6 全传）
#:   · outline_review_system      ← sse_handlers.py 目录审核（6/6 全传）
#:   · outline_patch_system       ← sse_handlers.py 外科式补齐（6/6 全传）
#:   · outline_feedback_system    ← sse_handlers.py 反馈修正（6/6 全传）
#:
#: 未列入的模板 = 调用点尚未逐一核对，保持「不校验」以避免误报。
PROMPT_VARIABLE_CONTRACTS: dict[str, list[str]] = {
    "content_generation_system": [
        "scheme_name", "scheme_type", "section_number", "standards_text",
        # ✅ E3（2026-09-25 · 提示词条件注入）：根据本章是否有 DB 子章节、
        #    body_subheading_demote_with_children 配置，动态生成正文子标题编号规则片段。
        "subheading_rule",
        # ✅ 2026-10-02（检查点前置）：审核检查点 → 生成约束（唯一事实源
        #    services/content_checkpoint）；两变量未传时独占行占位符整行丢弃，
        #    其余调用点（DB 定制模板渲染等）行为不变。
        "chapter_checkpoint_block", "content_checkpoint_block",
    ],
    "content_continue_system": [
        "scheme_name", "scheme_type", "standards_text",
        # ✅ 2026-10-02：续写轮同样携带检查点硬约束（与首轮同一实现）。
        "content_checkpoint_block",
    ],
    "outline_short_system": [
        "scheme_name", "scheme_type", "construction_scope", "project_facts",
        # ✅ 2026-09-26（目录生成三项依据收敛）：{scheme_basis} = 方案名称
        #   确定性解析产物（类型/工序/工艺/对象/危大分类，services/scheme_basis）；
        #   {standards_text} = standards_registry 匹配到的编制依据规范。
        #   两者均「无法匹配时渲染为空串 = 不注入」，故不传时模板仍可正常渲染。
        "scheme_basis", "standards_text",
        # ✅ 2026-10-02（第二十五轮 · 目录侧检查点前置）：{outline_checkpoint_block}
        #   = services/outline_checkpoint 派生的必备法定章节清单（唯一事实源，
        #   判据指向 audit_rules + preflight_engine 谓词）。未传时独占行整行丢弃。
        "outline_checkpoint_block",
    ],
    "outline_level1_system": [
        "scheme_name", "scheme_type", "construction_scope",
        "project_brief", "reference_outline", "project_facts",
        "scheme_basis", "standards_text", "outline_checkpoint_block",
    ],
    "outline_review_system": [
        "scheme_name", "scheme_type", "construction_scope",
        "is_dangerous", "outline_json", "project_facts",
        "scheme_basis", "outline_checkpoint_block",
        # R38 D5: the "8.1 audit against the checklist" instruction
        # shares the block's fate (absent when outline_checkpoint_check is off).
        "outline_checkpoint_audit_hint",
    ],
    "outline_patch_system": [
        "scheme_name", "scheme_type", "project_brief",
        "project_facts", "chapter_titles", "missing_items",
    ],
    "outline_feedback_system": [
        "scheme_name", "scheme_type", "project_brief",
        "project_facts", "original_outline", "review_suggestions",
    ],
    # 全局事实模板：调用方已逐项核对，启动期纳入统一变量契约防漂移。
    "facts_extract_system": [
        "zone_type", "priority_weight", "priority_hint", "heading",
        "zone_hint", "material", "chunk_index", "NORM_DICT_BLOCK", "summary",
    ],
    "facts_json_fix_system": ["issues", "target_description", "invalid_content"],
    # ✅ R38（2026-10-03 · P1-c）：通用 Schema 自适应修复提示词。
    #   非目录任务（符合性检查 / 一致性审计 / 冲突仲裁 / 事实补全 / 章节识别 …）
    #   此前一律复用 outline_json_fix_system，而那份提示词通篇在讲目录结构
    #   （child→children、补二级目录、description 推导），会主动诱导模型
    #   把 `{"conflicts": …}` 这类结果改成目录形状的占位 JSON。
    "json_schema_fix_system": ["issues", "target_description", "invalid_content"],
    "global_facts_adjust_system": ["current_facts", "instruction"],
    # ✅ 2026-09-26（T9 · 契约表覆盖率扩展）：以下 25 个模板由「模板实际占位符
    #    自动提取（同 check_prompt_variables 口径过滤误报）+ 高价值模板调用方
    #    传参人工抽查（outline_sublevel_system / consistency_scan_batch_user /
    #    consistency_repair_user / expert_review_system 四处 100% 对齐）」生成。
    #    纳入后任一模板占位符被改而契约未同步，启动期 check_prompt_variables
    #    立即检出（test_all_declared_contracts_match_templates 同步兜底）。
    # --- 目录生成域 ---
    "outline_sublevel_system": [
        "chapter_desc", "chapter_id", "chapter_title", "construction_scope",
        "other_outline", "prior_chapters", "project_brief", "project_facts",
        "requirements", "scheme_name", "scheme_type",
        # ✅ R38（2026-10-03 · P1-d）：调用点 sse_handlers.py:4306 一直在传
        #   standards_text，但本模板**没有**该占位符 → render_prompt 静默丢弃。
        #   结果：一级目录阶段（outline_short/level1 都注入本方案类别匹配的
        #   现行标准清单）看得到编制依据规范，二三级子目录阶段完全看不到，
        #   「规范章节宜作为该章骨架依据」这条要求在子层落不了地。
        #   现补占位符（措辞与 outline_short_system / outline_level1_system
        #   逐字一致）+ 契约登记。纯增量：调用方早已传参，无调用点改动。
        "standards_text",
    ],
    "outline_sublevel_batch_system": [
        "chapter_count", "chapters_text", "construction_scope", "other_outline",
        "prior_chapters", "project_brief", "project_facts", "requirements",
        "scheme_name", "scheme_type",
        "standards_text",  # ✅ R38：同上，见 outline_sublevel_system 注释
    ],
    "outline_adjust_system": [
        "current_outline", "instruction", "scheme_name", "scheme_type",
    ],
    "outline_json_fix_system": [
        "invalid_content", "issues", "target_description",
    ],
    "outline_recognition_system": ["raw_text"],
    # --- 正文生成域 ---
    "content_shrink_system": [
        "current_words", "max_rounds", "round_no", "scheme_name",
        "scheme_type", "section_title", "target_words",
    ],
    "word_budget_allocate_system": ["units_json"],
    # --- 一致性 Agent 域 ---
    "consistency_scan_user": [
        "design_docs_summary", "global_facts", "project_docs_summary",
        "section_content", "section_id", "section_title", "standards_summary",
    ],
    "consistency_scan_batch_user": [
        "design_docs_summary", "global_facts", "project_docs_summary",
        "section_count", "sections_block", "standards_summary",
    ],
    "consistency_repair_user": [
        "authoritative_sources", "conflicts_in_section", "global_facts",
        "section_content", "section_id", "section_title",
    ],
    "consistency_arbitrate_user": [
        "conflicts", "design_docs_summary", "global_facts",
        "project_requirements", "standards_summary",
    ],
    "consistency_audit_system": [
        "content", "facts", "scheme_name", "scheme_type",
    ],
    # --- 审核与预检域 ---
    # ✅ 2026-10-03（AI 语义检查侧消费事实）：两条 AI 链路经跨模块只读桥接
    #    load_resolved_facts_for_scope 装配 {global_facts}（与预检/正文/导出
    #    同一 fail-closed 门控），无事实时由路由层降级为「（无）」。
    "expert_review_system": [
        "attachments", "check_items", "global_facts", "outline_tree",
        "scheme_name", "scheme_type",
    ],
    "compliance_check_system": [
        "checklist", "content", "global_facts", "scheme_name", "scheme_type",
    ],
    # --- 审核预检 · 问题定向自动修复（services/review_autofix.py） ---
    "review_autofix_user": [
        "global_facts", "instruction", "issue", "must_contain",
        "must_not_contain", "rule_id", "rule_title", "scheme_name",
        "scheme_type", "section_content", "section_id", "section_title",
        "standards_text", "targets",
    ],
    # --- 图表修复域（chart-json / mermaid 修复共用 {code}/{error} 口径） ---
    "chart_json_fix": [
        "chart_type", "code", "error", "scheme_type",
    ],
    "chart_json_fix_architecture": ["code", "error", "scheme_type"],
    "chart_json_fix_comparison": ["code", "error", "scheme_type"],
    "chart_json_fix_gantt": ["code", "error", "scheme_type"],
    "chart_json_fix_labor": ["code", "error", "scheme_type"],
    "chart_json_fix_layout": ["code", "error", "scheme_type"],
    "chart_mermaid_fix": ["code", "error", "scheme_type"],
    "chart_mermaid_fix_comparison": ["code", "error", "scheme_type"],
    "chart_mermaid_fix_flowchart": ["code", "error", "scheme_type"],
    "chart_mermaid_fix_gantt": ["code", "error", "scheme_type"],
    # --- 配图域 ---
    "ILLUSTRATION_PROMPT_OPTIMIZE": ["section_title", "style"],
    # --- ✅ R38 收口（2026-10-03）：剩余未登记模板全部接入契约表 ---
    # 一致性修复·定点编辑 user（消费方 consistency_edits.build_edits_prompt，
    # 5/5 全传；章节原文由调用方在 render 结果后追加，不走占位符）
    "consistency_repair_edits_user": [
        "authoritative_sources", "conflicts_in_section", "global_facts",
        "section_id", "section_title",
    ],
    # 事实整理域（services/facts_enrich.py，调用点逐项核过：全传）
    "facts_finalize_system": ["current_facts"],
    "facts_knowledge_patch_system": ["current_facts", "knowledge_text"],
    # --- ✅ R38 D6：**空列表 = 显式声明「零占位符」**（区别于未声明 None）---
    #   启动期校验这些模板出厂确实无占位符；若日后有人往模板里加了 {x}
    #   而未同步契约，check_prompt_variables 会报 used_not_declared 漂移，
    #   用户在前端编辑时也会收到「新增了未在契约表中声明的变量」告警。
    #   共享片段（被拼接进宿主模板，自身不得含待填变量）：
    "SHARED_FORBIDDEN_WORDS": [],
    "SHARED_OUTPUT_SPEC": [],
    "SHARED_SCOPE_RULES": [],
    "SHARED_SCOPE_RULES_BRIEF": [],
    #   纯指令型 system 模板（变量全部在对应 *_user 侧，system 零占位）：
    "consistency_scan_system": [],
    "consistency_scan_batch_system": [],
    "consistency_arbitrate_system": [],
    "consistency_repair_system": [],
    "consistency_repair_edits_system": [],
    "review_autofix_system": [],
    # --- ✅ R39 T1（2026-10-04）：投标分析域 19 条提示词接入契约表 ---
    # system：通用约束纯指令，零占位。
    "bid_analysis_system": [],
    # user：唯一占位符是 ``__CONTEXT__``，由 ``build_item`` 用 ``.replace``
    # 注入（刻意避开 ``.format``，见该函数注释）—— 注入方就是调用方，故如实
    # 登记为 ``["CONTEXT"]``：既让启动期漂移校验归零，也让前端编辑器如实显示
    # 「这个模板需要一个项目资料文本变量」。不登记会报 used_not_declared
    # （实测 18 处漂移），给它加全局豁免则等于掩盖其它模板里的同类误用。
    "bid_item_projectBasicInfo": ["CONTEXT"],
    "bid_item_schemeBasicInfo": ["CONTEXT"],
    "bid_item_overviewParams": ["CONTEXT"],
    "bid_item_compilationBasis": ["CONTEXT"],
    "bid_item_siteConditions": ["CONTEXT"],
    "bid_item_deploymentSchedule": ["CONTEXT"],
    "bid_item_constructionTechnique": ["CONTEXT"],
    "bid_item_resourceAllocation": ["CONTEXT"],
    "bid_item_safetyMeasures": ["CONTEXT"],
    "bid_item_qualityAcceptance": ["CONTEXT"],
    "bid_item_emergencyResponse": ["CONTEXT"],
    "bid_item_calcAndDrawings": ["CONTEXT"],
    "bid_item_constructionDeployment": ["CONTEXT"],
    "bid_item_constructionProcess": ["CONTEXT"],
    "bid_item_workInterfaceDivision": ["CONTEXT"],
    "bid_item_materialManagement": ["CONTEXT"],
    "bid_item_equipmentManagement": ["CONTEXT"],
    "bid_item_engineeringMethods": ["CONTEXT"],
}


def _apply_variable_contracts() -> None:
    """把集中式契约表套用到已注册的模板元信息（在所有模板模块 import 之后调用）。

    幂等：重复调用只会重写同样的值。未知 key 跳过（模板可能已被动态删除）。
    """
    register_lazy_prompts()
    for key, requires in PROMPT_VARIABLE_CONTRACTS.items():
        meta = _ALL_PROMPTS.get(key)
        if meta is None:
            continue
        meta["requires"] = sorted(requires)


# ---------------------------------------------------------------------------
# ✅ G4 变量契约校验（2026-09-24 · 遗留项 4 闭环）
# ---------------------------------------------------------------------------
def check_prompt_variables(verbose: bool = False,
                           strict: bool = False) -> list[dict]:
    """校验全部注册提示词的「变量契约」，返回不一致明细。

    检查两类双向漂移（仅对声明了 ``requires`` 的模板生效）：
      1. ``declared_not_used``  —— 声明了必需变量，但模板里根本没这个占位符
          （契约写错 / 模板被改过而契约没同步）；
      2. ``used_not_declared``  —— 模板里有占位符，但没进契约白名单
          （调用方可能忘传 → 运行期残留占位符）。

    :param verbose: True 时把明细打到 WARNING 日志（供 main.lifespan 启动期调用）；
                    False = 静默（不打印日志），**但仍如实返回 issues**，
                    调用方可据此自行汇总告警。
    :param strict:  True 且存在漂移时抛出 :class:`PromptContractError`
                    （阻断启动）。默认 False = 不改变既有启动行为（向后兼容）。
    :return: 不一致列表（空列表 = 契约全部一致）

    ✅ 2026-09-25（BUG-B · verbose=False 语义漂移 / 配置项静默失效）：
      main.py 的启动期调用是 ``check_prompt_variables(verbose=settings.prompt_strict_variables)``,
      而 config 注释写着「False = 不改变启动行为 / True 开启」。旧实现里
      verbose=False 时**完全不打印任何日志**，于是：
        * 契约漂移在默认配置下**静默无声**（既不告警也不阻断），与「默认关闭、
          开启才生效」的注释正好相反；
        * ``prompt_strict_variables`` 从未真正「开启校验」—— 它只控制
          「是否把明细打到日志」，属于配置项静默失效。
      现按 docstring 口径修回：verbose 只决定**是否打印明细日志**，返回值恒为
      完整漂移明细（调用方 main.py 已改为无条件汇总告警）。``strict`` 是显式
      新增能力（默认关闭），开启时才阻断启动。

    说明：默认路径只做**校验与告警**，绝不修改模板内容、不阻断服务启动；
    未声明 ``requires`` 的模板（绝大多数）完全跳过，保证零副作用。
    """
    register_lazy_prompts()
    issues: list[dict] = []
    for key, meta in sorted(_ALL_PROMPTS.items()):
        requires = meta.get("requires")
        # ✅ R38 D6：区分「未声明」(None=跳过) 与「显式声明零变量」([]=校验)
        #    —— 旧判据 `if not requires` 把两者混为一谈，导致契约表里的空
        #    声明恒不生效（R38 报告 D6 的根因）。
        if requires is None:
            continue
        # ✅ 2026-09-25（BUG-A · 契约漂移基线污染）：以出厂默认基线比对
        #   （meta["content"]/["variables"] 会被 update_prompt 原地改写，用它
        #   当基线会导致「出厂模板与契约一致、但用户改过一次就误报漂移」）。
        template = meta.get("default_content") or meta.get("content", "")
        raw_actual = {v for v in (meta.get("default_variables")
                                  or meta.get("variables") or [])
                      if not _is_false_positive(key, v, template)}
        # 与 validate_prompt_variables 同口径：大小写不敏感比较
        actual_lower = {v.lower() for v in raw_actual}
        declared_lower = {v.lower() for v in requires}
        declared_not_used = sorted(
            v for v in requires if v.lower() not in actual_lower)
        used_not_declared = sorted(
            v for v in raw_actual if v.lower() not in declared_lower)
        if not (declared_not_used or used_not_declared):
            continue
        issues.append({
            "key": key,
            "declared_not_used": declared_not_used,
            "used_not_declared": used_not_declared,
        })
        if verbose:
            parts = []
            if declared_not_used:
                parts.append(f"声明了但模板未使用={declared_not_used}")
            if used_not_declared:
                parts.append(f"模板使用但未声明={used_not_declared}")
            logger.warning(
                "提示词变量契约不一致 %s：%s", key, "；".join(parts))
    if strict and issues:
        raise PromptContractError(
            f"提示词变量契约存在 {len(issues)} 处漂移："
            + "；".join(f"{i['key']}(未用={i['declared_not_used']}"
                        f"/未声明={i['used_not_declared']})" for i in issues))
    return issues


class PromptContractError(RuntimeError):
    """提示词变量契约漂移且开启严格模式（``prompt_contract_fail_fast``）时抛出。

    仅显式开启严格模式时使用 —— 默认路径只打 WARNING、不阻断启动，
    保证向后兼容（AGENTS.md §3.1.3：新增能力默认向后兼容）。
    """


#: 保存期校验的问题级别 → 中文说明（前端直接展示）。
PROMPT_ISSUE_LABELS = {
    "unknown_shared_ref": "未注册的共享片段引用",
    "contract_var_removed": "相对出厂默认删掉了契约变量",
    "contract_var_added": "新增了契约表中没有的变量",
}


def validate_prompt_content(
    key: str, content: str  # noqa: F811  content 与文件尾导入的 content 模块同名（刻意，见文件尾）
) -> list[dict]:
    """保存提示词前的**静态体检**，返回问题清单（空列表 = 通过）。

    ✅ 2026-09-27（BUG-P1-C · 保存时零校验，坏模板一路跑到模型面前）：
    旧实现的 PATCH 只检查「key 存在 / 长度不超限 / content 是字符串」，
    于是用户可以把模板改成任意形态并立即对所有生成任务生效：

    * 引用一个**不存在的** ``{SHARED_FOO}`` —— 运行时保留字面量，
      提示词正文里就多出一段 ``{ SHARED_FOO }`` 垃圾，模型可能当成指令读；
    * 删掉 ``content_generation_system`` 的 ``{scheme_name}`` —— 该变量
      不再注入，模板里「【方案名称】：」后面直接空白，而**没有任何反馈**；
    * 增删契约表里声明的变量 —— 启动期 ``check_prompt_variables`` 只比对
      **出厂默认**（有意为之，见该函数注释「防基线污染」），所以用户改过
      的模板**永远不参与契约校验**，漂移彻底无人把关。

    本函数**不做任何阻断**（返回清单，由路由决定是否 400），保持
    「保存成功 ≠ 一定正确」这一向后兼容语义；默认路由只把
    ``errors`` 级问题升级为 400，其余作为 ``warnings`` 回传前端提示。

    :param key: 提示词 key。
    :param content: 待保存的正文。
    :return: ``[{"level","code","message","detail"}]``；level ∈ error/warning。
    """
    issues: list[dict] = []
    if content is None:
        return issues
    text = content or ""

    # ① 未注册的 {SHARED_*} 引用 —— 运行时必然保留字面量
    used_shared = sorted(set(re.findall(r"\{(SHARED_[A-Z0-9_]+)\}", text)))
    unknown_shared = [k for k in used_shared if k not in _ALL_PROMPTS]
    if unknown_shared:
        avail = sorted(k for k in _ALL_PROMPTS if k.startswith("SHARED_"))
        issues.append({
            "level": "error",
            "code": "unknown_shared_ref",
            "message": (f"引用了不存在的共享片段：{', '.join(unknown_shared)}；"
                        f"可用片段：{', '.join(avail) or '（无）'}"),
            "detail": unknown_shared,
        })

    # ② 共享片段自引用 / 环引用 —— 运行时会被 BUG-P0-2 的防护挡下并留字面量
    #    ✅ 判据修正（2026-09-30）：**必须检查待保存的 `text` 自身**。
    #    旧实现只读注册表里 SHARED 片段的**旧内容**（`_ALL_PROMPTS[sk]`），
    #    而 PATCH 路由的顺序是「先 validate_prompt_content → 再 update_prompt 落库」
    #    （routers/prompts.py:187 / :214）—— 于是**首次**把
    #    `SHARED_OUTPUT_SPEC` 改成含 `{SHARED_OUTPUT_SPEC}` 的内容时，
    #    校验读到的仍是出厂默认（不含自引用）→ 判定通过 → 坏内容被写入 DB。
    #    实测（探针复现）：首次保存 3 个 SHARED 片段的自引用内容，
    #    `validate_prompt_content` 一律返回 `[]`（无 error），而二次保存才报错。
    #    修法：以 `text` 为准判断「本次保存是否引入自引用」。
    for sk in used_shared:
        if sk not in _ALL_PROMPTS:
            continue
        # 本次保存的正文优先（首次保存自引用即在此命中）；
        # 否则回退注册表旧内容（识别"已入库的互引"）。
        body = text if key == sk else str(
            _ALL_PROMPTS[sk].get("content")
            or _ALL_PROMPTS[sk].get("default_content") or "")
        if re.search(r"\{" + re.escape(sk) + r"\}", body):
            issues.append({
                "level": "error",
                "code": "shared_self_reference",
                "message": f"共享片段 {sk} 的内容里引用了自身，会造成循环展开",
                "detail": [sk],
            })
            break

    # ③ 契约变量增删（相对出厂默认的占位符集合）
    #    ✅ 判据收敛（2026-09-30）：**必须复用 `_is_false_positive`**。
    #    旧实现直接用 `extract_user_variables` 的原始结果，既不过滤 JSON/LaTeX
    #    示例误报，也不套可选区块豁免 —— 而 `validate_prompt_variables`
    #    （:241）与 `check_prompt_variables` 走的是同一套过滤。于是同一个变量
    #    在「运行期缺失告警」里被正确豁免，在「保存期体检」里却被报成漂移。
    #    实测（探针复现）：`content_generation_system` 的**出厂默认**里含
    #    `$p_{max}$`（LaTeX 下标示例，`_is_false_positive` 判定 True），
    #    但 ③ 分支未过滤 → 用户**原样保存出厂模板也会弹一条**
    #    「新增了未在契约表中声明的变量 max」的假告警。
    meta = _ALL_PROMPTS.get(key) or {}
    # ✅ R38 D6：`or []` 会把「未声明(None)」与「显式零声明([])」压扁成同一
    #    语义，现保留区分：显式声明的模板（含空声明）都进入增删占位符告警
    #    —— 用户往零占位模板里加 {x} 时会收到「新增了未在契约表中声明的变量」。
    requires = meta.get("requires")
    if requires is not None:

        def _user_vars(tpl: str) -> set[str]:
            """提取「调用方需提供的变量」，并套用与运行期告警**同一套**误报/豁免判据。

            - `_is_false_positive`：JSON 示例 / LaTeX 下标（如 `$p_{max}$`）；
            - `_is_optional_block_var`：整行独占的可选区块（未传即整行丢弃）。
            两者都只影响**告警**口径，不改变模板实际渲染结果，故此处过滤安全。
            """
            out = set()
            for v in extract_user_variables(tpl):
                if _is_false_positive(key, v, tpl):
                    continue
                if _is_optional_block_var(v, tpl):
                    continue
                out.add(v.lower())
            return out

        default_text = meta.get("default_content") or meta.get("content") or ""
        default_vars = _user_vars(default_text)
        cur_vars = _user_vars(text)
        removed = sorted(v for v in requires
                         if v.lower() in default_vars and v.lower() not in cur_vars)
        added = sorted(v for v in cur_vars
                       if v.lower() not in {r.lower() for r in requires})
        if removed:
            issues.append({
                "level": "warning",
                "code": "contract_var_removed",
                "message": (f"删掉了契约变量 {', '.join(removed)}："
                            f"调用方仍会传入，但模板已不再使用；"
                            f"若非有意请恢复"),
                "detail": removed,
            })
        if added:
            issues.append({
                "level": "warning",
                "code": "contract_var_added",
                "message": (f"新增了未在契约表中声明的变量 {', '.join(added)}："
                            f"运行时永远不会被填充，位置将留空"),
                "detail": added,
            })
    return issues


from app.services.ai.prompts import (
    analysis,  # noqa: E402,F401
    charts,  # noqa: E402,F401
    consistency_repair,  # noqa: E402,F401  （全文一致性 Agent 修复：扫描/仲裁/修复）
    content,  # noqa: E402,F401,F811  （content 模块与上方函数参数同名，刻意导入以触发注册）
    illustration,  # noqa: E402,F401  （自招投标平台移植：配图编排/方案）
    outline,  # noqa: E402,F401
    review_autofix,  # noqa: E402,F401  （审核预检问题定向修复）
)

# ✅ G4 变量契约：模板全部注册后再套用契约表（见上方 PROMPT_VARIABLE_CONTRACTS）
_apply_variable_contracts()


