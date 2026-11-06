"""降级信号一致性元测试（R179 · 2026-11-05）。

目的：把「后端发了降级信号、前端没人消费」这类**承诺落空**变成红灯。
覆盖两个方向：
  · 后端 `X-*` 降级响应头 → 前端 `exportResponse.ts` 必须解析；
  · 后端 `X-Export-Degraded` 的每个信号值 → 前端必须有对应文案（未知信号兜底
    文案也算消费，但新信号必须显式登记，不能靠兜底掩盖）；
  · 反向：前端解析/展示的每个头与信号，后端必须真实存在（防前端臆造承诺）。

任何一侧新增后另一侧没跟上，本文件即失败（一致性契约的唯一出口）。
"""

import re
from pathlib import Path

BACKEND_APP = Path(__file__).resolve().parents[1] / "app"
FRONTEND_SRC = Path(__file__).resolve().parents[2] / "frontend" / "src"
EXPORT_RESPONSE_TS = FRONTEND_SRC / "utils" / "exportResponse.ts"

# 降级类响应头（必须一一对应前端解析函数；新增此类头必须同步接前端）
DEGRADATION_HEADERS = {
    "x-export-degraded",
    "x-global-facts-status",
    "x-chart-render-stats",
    "x-fix-stats",
}

_HEADER_LITERAL = re.compile(r"""["'](x-[A-Za-z0-9-]+)["']""", re.IGNORECASE)
_SIGNAL_LITERAL = re.compile(r'"([a-z][a-z0-9]*(?:-[a-z0-9]+)+)"')
_FRONTEND_PARSE = re.compile(r"""headerValue\(\s*headers\s*,\s*["'](x-[A-Za-z0-9-]+)["']""")
_FRONTEND_KEY = re.compile(r"""["']([a-z0-9]+(?:-[a-z0-9]+)*)["']\s*:""")


def _py_files() -> list[Path]:
    return sorted(BACKEND_APP.rglob("*.py"))


def _ts_files() -> list[Path]:
    return sorted(FRONTEND_SRC.rglob("*.ts")) + sorted(FRONTEND_SRC.rglob("*.tsx"))


def _backend_headers() -> set[str]:
    found: set[str] = set()
    for path in _py_files():
        found.update(m.group(1).lower() for m in _HEADER_LITERAL.finditer(path.read_text(encoding="utf-8")))
    return found


def _frontend_parsed_headers() -> set[str]:
    found: set[str] = set()
    for path in _ts_files():
        found.update(m.group(1).lower() for m in _FRONTEND_PARSE.finditer(path.read_text(encoding="utf-8")))
    return found


def _backend_export_degraded_signals() -> set[str]:
    """只取真实赋值行的字面量（忽略注释/文档里的举例，避免假阳性）。"""
    found: set[str] = set()
    for path in _py_files():
        lines = path.read_text(encoding="utf-8").splitlines()
        for idx, line in enumerate(lines):
            if 'headers["X-Export-Degraded"]' not in line:
                continue
            block = "\n".join(lines[idx : idx + 3])
            found.update(m.group(1) for m in _SIGNAL_LITERAL.finditer(block))
    found.discard("x-export-degraded")
    return found


def _frontend_degraded_signals() -> set[str]:
    text = EXPORT_RESPONSE_TS.read_text(encoding="utf-8")
    match = re.search(r"const EXPORT_DEGRADED_TEXT[^=]*=\s*\{", text)
    assert match, "exportResponse.ts 缺少 EXPORT_DEGRADED_TEXT 映射表"
    start = match.end()
    end = text.index("};", start)
    return {m.group(1) for m in _FRONTEND_KEY.finditer(text[start:end])}


# ---------------------------------------------------------------------------
# 防「空扫描假绿」：源文件必须真的被读到
# ---------------------------------------------------------------------------


def test_scan_sources_are_non_empty():
    assert len(_py_files()) > 100
    assert len(_ts_files()) > 50
    assert _backend_headers()
    assert _frontend_parsed_headers()


# ---------------------------------------------------------------------------
# 后端 → 前端
# ---------------------------------------------------------------------------


def test_every_degradation_header_is_emitted_by_backend():
    missing = DEGRADATION_HEADERS - _backend_headers()
    assert not missing, f"降级头集合里的头在后端已不存在（请同步本测试）：{sorted(missing)}"


def test_every_degradation_header_is_consumed_by_frontend():
    missing = DEGRADATION_HEADERS - _frontend_parsed_headers()
    assert not missing, (
        "后端降级响应头没有前端消费（承诺落空）；新增头必须同步 exportResponse.ts 解析：" f"{sorted(missing)}"
    )


def test_backend_degraded_signals_have_frontend_copy():
    backend = _backend_export_degraded_signals()
    assert backend, "未从后端源码提取到任何 X-Export-Degraded 信号（扫描口径失效）"
    missing = backend - _frontend_degraded_signals()
    assert not missing, (
        "后端新增加的导出降级信号在前端 EXPORT_DEGRADED_TEXT 没有登记（用户会只看到兜底文案）：" f"{sorted(missing)}"
    )


# ---------------------------------------------------------------------------
# 前端 → 后端（防臆造承诺）
# ---------------------------------------------------------------------------


def test_frontend_degraded_signals_are_all_backend_emitted():
    frontend = _frontend_degraded_signals()
    backend = _backend_export_degraded_signals()
    invented = frontend - backend
    assert not invented, f"前端登记了后端从不发出的降级信号（文案承诺落空）：{sorted(invented)}"


def test_every_frontend_parsed_header_exists_in_backend():
    frontend = _frontend_parsed_headers()
    invented = frontend - _backend_headers()
    assert not invented, f"前端解析了后端不存在的响应头（拼写漂移/旧名残留）：{sorted(invented)}"
