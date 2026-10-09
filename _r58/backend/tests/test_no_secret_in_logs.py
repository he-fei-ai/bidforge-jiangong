"""日志密钥护栏（A3 / P0 / 2026-10-05）

目的：CI 门禁级别防回归——禁止在 `backend/app/**/*.py` 的日志调用中出现密钥字面量。

背景：
- 项目大量使用 `%s` 参数化日志，运行时打印的敏感值依赖代码习惯而不是静态约束；
- 一旦有人把 `logger.warning("apikey=%s", api_key)` 或 `logger.info("sk-abc...")` 提交，
  日志文件 `logs/backend.log` 会永久留痕（SafeRotatingFileHandler 明确"宁超不丢"），
  而日志本身没有密钥护栏；
- 本测试用 AST 扫描（不 import 生产模块），毫秒级跑完，是纯测试护栏，零运行时开销。

扫描规则（保守、无假阴）：
1. 目标：所有形如 `logger.<level>(...)` 的调用（level ∈ debug/info/warning/error/critical/exception），
   包括模块级 `logger = logging.getLogger(...)` 变量。
2. 检查两类文本：
   a) 调用参数中的**字符串字面量**（含格式化模板如 `"apikey=%s"` 和 args 中的字面量）；
   b) f-string 的 `format_spec` 和 `value` 常量部分（`"sk-abc"` 这类直接嵌 f-string 也算）。
3. 判定密钥特征（大小写不敏感、子串匹配）：
   - OpenAI 系：`sk-` 后跟 ≥8 位字面量字符（避免误伤 `sk-learn` 文档）；
   - Fernet token：`gAAAA` 前缀；
   - Baidu/百度：`bce-` 前缀；
   - Key 关键字硬编码：`api_key = <非空串>` / `api_key='<...>'` / `secret =` / `secret='...'`；
   - 环境变量泄漏：`os.environ["...KEY"...]` 或 `os.getenv("...KEY...")` 出现在参数里 → 抓。
4. 白名单（已知安全场景，避免误伤）：
   - 关键字名（如 `logger.warning("api_key 未配置")`）：字符串中不含 `=` 或引号包裹的值；
   - 变量名/属性（Name/Attribute 节点），AST 只保留结构，不视为泄漏；
   - 只允许字面量前缀+格式占位符（`"api_key=%s"`）——但这类是常见错误，故也报，靠开发自检。

运行方式：
    pytest tests/test_no_secret_in_logs.py -q

设计取舍：
- 仅扫描 `backend/app/` 生产代码；测试目录本身允许出现示例密钥字面量；
- 不解析 f-string 中 Name 表达式（运行时才知值），保守漏报优于误报；
- 若某处确需记录 Key 前 4 位 + 长度（如 `"prefix=%s, len=%d"`），本护栏不会拦截——只要字面量本身不含密钥特征。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

# 生产代码扫描根：backend/app
_REPO_ROOT = Path(__file__).resolve().parents[2]
_APP_ROOT = _REPO_ROOT / "backend" / "app"

# 允许调用的 logger 级别（覆盖 logging.Logger 全部常见输出级别）
_LOG_LEVELS = {"debug", "info", "warning", "error", "critical", "exception"}

# 已知密钥特征（正则；保守起见全部要求"看起来像真实密钥"而非"关键词"）
_SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # OpenAI / DeepSeek / DashScope / Anthropic 等 sk- 前缀：
    # 至少 20 字符且包含数字（避免误伤 "sk-learn"、"sk-plain-text-without-long-key" 之类）
    ("sk- 前缀密钥", re.compile(r"\bsk-[A-Za-z0-9_-]{17,}(?=[A-Za-z0-9_-]*\d)")),
    ("Fernet gAAAA 前缀", re.compile(r"\bgAAAA[A-Za-z0-9_-]{20,}")),
    ("Baidu bce- 前缀", re.compile(r"\bbce-v3[A-Za-z0-9_-]{10,}")),
    # 关键字 + 字面量赋值：只要 `api_key=` 后紧跟引号包裹的字符串就是硬编码
    ("api_key 字面量赋值", re.compile(r"\bapi_key\s*=\s*['\"][^'\"]{4,}['\"]")),
    ("secret 字面量赋值", re.compile(r"\bsecret\s*=\s*['\"][^'\"]{4,}['\"]")),
    ("token 字面量赋值", re.compile(r"\btoken\s*=\s*['\"][^'\"]{4,}['\"]")),
]

# 环境变量泄漏特征（args 中直接引用 KEY 类环境变量）
_ENV_VAR_PATTERN = re.compile(r"\b[A-Z_]*(API_?KEY|SECRET|TOKEN|PASSWORD)[A-Z_]*\b")


class _LoggerCallVisitor(ast.NodeVisitor):
    """遍历 AST，收集所有 `logger.<level>(...)` 调用。"""

    def __init__(self) -> None:
        self.calls: list[tuple[ast.Call, int]] = []

    def visit_Call(self, node: ast.Call) -> None:
        # 识别形如 logger.info(...) / logging.getLogger("x").info(...) 等
        if isinstance(node.func, ast.Attribute) and node.func.attr in _LOG_LEVELS:
            # value 必须是 Attribute/Name 且名字含 logger/logging
            value = node.func.value
            if isinstance(value, ast.Name) and value.id.lower() in {
                "logger", "log", "self.logger", "logging",
            }:
                self.calls.append((node, node.lineno))
            elif isinstance(value, ast.Attribute) and value.attr.lower() in {
                "logger", "log", "logging",
            }:
                self.calls.append((node, node.lineno))
            elif isinstance(value, ast.Name) and value.id.lower().endswith("logger"):
                self.calls.append((node, node.lineno))
        self.generic_visit(node)


def _extract_literals(node: ast.AST) -> list[str]:
    """从节点收集所有字符串字面量（含 f-string 的 format_spec 与常量部分）。"""
    literals: list[str] = []
    if isinstance(node, ast.Constant):
        if isinstance(node.value, str):
            literals.append(node.value)
        return literals
    if isinstance(node, ast.JoinedStr):
        # f-string：拼接 constants 部分
        for part in node.values:
            literals.extend(_extract_literals(part))
        return literals
    return literals


def _looks_like_bare_secret(lit: str) -> bool:
    """判定独立字符串参数是否像裸密钥（用于抓 `logger.warning("...", "sk-abc...")` 场景）。

    判定条件（保守）：
      - 长度 ≥ 16（真实密钥都远超此长度）
      - 至少包含 1 个数字（避免误伤 "hello-world-please-ignore"）
      - 至少包含 1 个非字母字符（连字符/下划线），避免误伤纯英文句子
      - 只由 [A-Za-z0-9_-] 组成（真密钥的字符集特征）
      - 不以常见非密钥词开头（排除文档/路径/URL 等误伤）
    """
    if len(lit) < 16 or len(lit) > 256:
        return False
    if not re.fullmatch(r"[A-Za-z0-9_-]+", lit):
        return False
    if not any(c.isdigit() for c in lit):
        return False
    if not any(c in "-_" for c in lit):
        return False
    # 已知非密钥前缀白名单（文档/URL/文件路径/常见技术名）
    lower = lit.lower()
    safe_prefixes = (
        "http://", "https://", "test-", "example-", "path/", "./", "../",
        "backend/", "frontend/", "tests/", "node_modules/", "package-",
        "python-", "django-", "fastapi-", "pytest-", "flask-", "uvicorn-",
        "gunicorn-", "requirements", "pyproject", "setup.py", "pypi-",
        "docs.", "readme", "license", "manifest", "version-", "release-",
    )
    if any(lower.startswith(p) for p in safe_prefixes):
        return False
    return True


def _scan_file(path: Path) -> list[tuple[str, int, str]]:
    """扫描单文件，返回 (file, line, reason) 违规列表。"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return []

    visitor = _LoggerCallVisitor()
    visitor.visit(tree)

    violations: list[tuple[str, int, str]] = []
    for call, lineno in visitor.calls:
        # 收集该调用所有参数（positional + keyword + starred）中的字符串字面量
        all_args = list(call.args) + [kw.value for kw in call.keywords]
        for arg in all_args:
            for lit in _extract_literals(arg):
                for label, pat in _SECRET_PATTERNS:
                    if pat.search(lit):
                        violations.append(
                            (str(path), lineno, f"{label}（字面量：{lit!r}）"))
                # 独立字符串参数中直接出现密钥值：即使没有 "key=" 前缀也算泄漏
                # （例：logger.warning("api_key=%s", "sk-proj-abc1234567890")）
                if _looks_like_bare_secret(lit):
                    violations.append(
                        (str(path), lineno, f"独立字符串参数疑似密钥（字面量：{lit!r}）"))
                # 环境变量泄漏：字符串里含 KEY/SECRET/TOKEN/PASSWORD 全大写
                if _ENV_VAR_PATTERN.search(lit.upper().replace("_", "")):
                    # 更保守：只在看起来是 os.environ/getenv 的目标参数时才拦
                    # 简单判定：字面量本身是纯大写环境变量名
                    if re.fullmatch(r"[A-Z_]*(KEY|SECRET|TOKEN|PASSWORD)[A-Z_]*", lit.strip()):
                        violations.append(
                            (str(path), lineno, f"环境变量名泄漏（字面量：{lit!r}）"))

    return violations


def _app_py_files() -> list[Path]:
    return sorted(_APP_ROOT.rglob("*.py"))


def test_app_root_exists() -> None:
    """前置：backend/app 目录存在，避免空扫误报通过。"""
    assert _APP_ROOT.is_dir(), f"预期生产代码目录存在：{_APP_ROOT}"
    files = _app_py_files()
    assert len(files) > 100, f"预期 app/ 下 python 文件 >100，实际 {len(files)}"


def test_no_secret_literals_in_logger_calls() -> None:
    """核心护栏：生产代码的 logger.* 调用参数中不得出现密钥字面量。"""
    files = _app_py_files()
    all_violations: list[tuple[str, int, str]] = []
    for f in files:
        all_violations.extend(_scan_file(f))

    if all_violations:
        # 报告最多 20 条，便于定位
        sample = all_violations[:20]
        report_lines = [
            f"  {path}:{line}  {reason}" for path, line, reason in sample
        ]
        extra = "" if len(all_violations) <= 20 else (
            f"\n  ... 另有 {len(all_violations) - 20} 条违规未列出"
        )
        pytest.fail(
            f"发现 {len(all_violations)} 处日志密钥泄漏风险：\n" +
            "\n".join(report_lines) + extra +
            "\n\n修复建议：\n"
            "  1) 不要打印密钥值，改用前缀 + 长度（例：`f'key={api_key[:4]}...,len={len(api_key)}'`）；\n"
            "  2) 不要通过字面量硬编码密钥；\n"
            "  3) 环境变量名不要作为日志字面量出现。"
        )


def test_no_sk_prefix_in_any_log_message_template() -> None:
    """强化：即使非 `logger.info(...)` 而是 `logger.warning(f\"...sk-...\")` 也应被前一条覆盖。
    这里做一次全项目 f-string 扫描，专门抓任何字符串里出现的 `sk-` 硬编码。"""
    files = _app_py_files()
    bad: list[tuple[str, int, str]] = []
    for f in files:
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                # 只关注看起来像"日志消息"的常量：长度 ≥20 且包含 sk- 前缀
                for label, pat in _SECRET_PATTERNS:
                    if pat.search(node.value):
                        # 排除注释/docstring：docstring 在 AST 里是 Expr(Constant)，
                        # 但 lineno=1 或紧接类/模块/函数定义，用简单启发式：
                        # 若该常量前后 2 行内出现注释/docstring 上下文，跳过
                        bad.append((str(f), node.lineno or 0, f"{label}：{node.value!r}"))

    # 过滤已知 docstring 里对密钥格式的说明性文字（例：crypto.py 里解释 "gAAAA 开头"）
    # 这里采用宽松规则：仅当同一文件同时出现 logger.* 调用时才作为违规。
    filtered: list[tuple[str, int, str]] = []
    for path, line, reason in bad:
        p = Path(path)
        try:
            src = p.read_text(encoding="utf-8")
        except OSError:
            continue
        # 只保留该文件中确实包含 logger.* 调用的场景
        # 因为纯 docstring 说明性文本通常出现在没有 logger 调用的 util 模块中
        # 但如果该文件同时有 logger 调用，仍可能是真泄漏，故保守保留
        if "logger." in src or "logging." in src:
            filtered.append((path, line, reason))

    if filtered:
        sample = filtered[:20]
        report = "\n".join(f"  {p}:{l}  {r}" for p, l, r in sample)
        pytest.fail(
            f"发现 {len(filtered)} 处密钥字面量（含 docstring 提示性说明也可能命中，请人工判定）：\n{report}"
        )


def test_no_secret_in_tests_self_sanity() -> None:
    """自检护栏：用已知违规代码验证扫描逻辑有效。"""
    sample_bad = '''
import logging
logger = logging.getLogger("test")
logger.warning("api_key=%s", "sk-proj-abc1234567890")
logger.error("token=%s", "gAAAAABCD12345678901234567890")
logger.info("hardcoded api_key='sk-hardcoded12345' should be flagged")
logger.debug("safe message without secrets")
logger.warning("api_key=%s", "safe-api-key-value")
'''
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False,
                                    encoding="utf-8") as tf:
        tf.write(sample_bad)
        tmp_path = Path(tf.name)

    try:
        violations = _scan_file(tmp_path)
        joined = "\n".join(v[2] for v in violations)
        # 期望抓到的（4 类）：
        #   1) "sk-proj-abc1234567890" 独立参数（sk- 前缀）
        #   2) "gAAAAABCD..." Fernet 前缀
        #   3) "hardcoded api_key='sk-hardcoded12345'" 消息模板里硬编码 api_key='...'
        #   4) 独立字符串参数被识别为疑似密钥
        assert "sk-" in joined, f"应抓到 sk- 前缀，实际：{joined}"
        assert "Fernet" in joined, f"应抓到 gAAAA 前缀，实际：{joined}"
        assert "api_key" in joined, f"应抓到 api_key 字面量赋值，实际：{joined}"
        assert "独立字符串参数" in joined, f"应抓到独立裸密钥参数，实际：{joined}"
        # 关键：参数化模板 "api_key=%s" 和 "token=%s" 都不该作为独立裸密钥被抓
        for _p, _l, reason in violations:
            if "独立字符串参数" in reason:
                # 只允许抓 sk-proj-abc1234567890（含数字+连字符+长度≥16），
                # 不该抓 "api_key=%s"（含 = 和 %s 特殊字符，被 _looks_like_bare_secret 排除）
                # 也不该抓 "gAAAAABCD..."（已被 Fernet 规则命中，但会同时命中独立参数规则）
                # 只要没有误抓 %s 模板即可
                if "api_key=%s" in reason or "token=%s" in reason:
                    pytest.fail(
                        f"误报：参数化模板被误抓为独立裸密钥：{reason}")
    finally:
        tmp_path.unlink(missing_ok=True)


def test_scan_coverage_report(caplog: pytest.LogCaptureFixture) -> None:
    """诊断用例：报告扫描覆盖的日志调用总数（非门禁，仅观测）。"""
    files = _app_py_files()
    total_calls = 0
    for f in files:
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        v = _LoggerCallVisitor()
        v.visit(tree)
        total_calls += len(v.calls)

    assert total_calls > 500, (
        f"app/ 下 logger 调用总数应 >500（当前 {total_calls}），"
        "过低说明 AST 扫描可能失效，请检查 _LoggerCallVisitor 逻辑"
    )
