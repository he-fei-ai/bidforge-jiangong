"""前后端 API 契约一致性测试。

背景（技术债）：前端 `src/api/index.ts`（外加 `SchemeWorkbenchPage` / `Layout`
少量直连 `sseFetch` / `fetch`）里写死的 URL，与后端 FastAPI 真实路由之间
**没有任何自动校验**。后端改了路径、删了端点、换了 HTTP 方法，`tsc` 与
vitest 都发现不了，只能等运行时 404 / 405 —— 而这类问题往往在用户点下
「生成」「导出」之后才暴露。

本用例把「前端声明要调用的 (method, path)」与「后端 OpenAPI 里真实存在的
(method, path)」做**形状比对**（路径参数统一归一为 `{}`，容忍
`${id}` ↔ `{config_id}` 的参数名差异），从而在 pytest 阶段就拦住契约漂移。

约定 / 边界：
- 前端 axios 实例 `baseURL = "/api/v1"`，故 `api.get("/projects")` 实际命中
  `/api/v1/projects`；`sseFetch` / `sseGetStream` 内部同样拼 `/api/v1` 前缀。
- `fetch("/api/v1/health")` 这类已带完整前缀的字面量不做二次拼接。
- 仅检查**可静态提取**的调用；`` `${expr}` `` 归一成 `{}`。裸变量实参
  （如 `const url = "/upload-outline/parse" + qs; api.post(url, ...)`）
  通过同文件内 `const NAME = "<literal>"` 的简单定义回填。
- 只做「形状」比对，不校验请求体 / 查询参数 / 响应结构 —— 参数级契约
  仍需人工或后续 codegen 补齐。
"""
import re
from pathlib import Path

import pytest
from app.main import app

# --------------------------------------------------------------------------
# 扫描范围：集中式 api client + 少量绕过 client 直连的文件
# --------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[2]
_FRONTEND_SRC = _REPO_ROOT / "frontend" / "src"

SCAN_FILES = (
    _FRONTEND_SRC / "api" / "index.ts",
    _FRONTEND_SRC / "pages" / "SchemeWorkbenchPage.tsx",
    _FRONTEND_SRC / "components" / "Layout.tsx",
)

_API_PREFIX = "/api/v1"

# 字符串字面量：模板串 / 双引号 / 单引号
_LIT = r"`[^`]*`|\"[^\"]*\"|'[^']*'"
_IDENT = r"[A-Za-z_$][\w$]*"

# axios 封装调用：api.get("/x") / api.post(`/x/${id}`)
_XHR_RE = re.compile(rf"\bapi\.(get|post|put|patch|delete)\s*\(\s*({_LIT}|{_IDENT})")
# SSE：sseFetch 必为 POST，sseGetStream 必为 GET（见 api/index.ts 实现）
_SSE_POST_RE = re.compile(rf"\bsseFetch\s*\(\s*({_LIT}|{_IDENT})")
_SSE_GET_RE = re.compile(rf"\bsseGetStream\s*\(\s*({_LIT}|{_IDENT})")
# 裸 fetch：只取完全静态字面量，避免误取 api/index.ts 内 `fetch(`/api/v1${url}`)` 的实现
_RAW_FETCH_RE = re.compile(rf"\bfetch\s*\(\s*({_LIT})")
# 同文件内 `const url = "<literal>" ...` 的简单定义（用于回填裸变量实参）
_CONST_DEF_RE = re.compile(rf"\bconst\s+({_IDENT})\s*=\s*({_LIT})")
# helper 自身的定义（如 `export async function* sseGetStream(url: string, ...)`）——
# 其形参列表与调用语法无法用正则区分，必须先剔除，否则定义会被当成一次调用
_DECL_RE = re.compile(
    r"\b(?:function\s*\*?\s*|const\s+)(?:sseFetch|sseGetStream)\s*\([^)]*\)"
)

# 已知的非路由字面量（前端内部标识，不是后端端点）——如需新增请写明原因
_NOT_A_ROUTE: set[tuple[str, str]] = set()


def _shape(path: str) -> str:
    """归一化成可比较的「路径形状」：去掉查询串，路径参数 → {}。"""
    path = path.split("?", 1)[0].split("#", 1)[0]
    if not path.startswith("/"):
        path = "/" + path
    path = re.sub(r"\$\{[^}]*\}", "{}", path)  # 前端模板变量
    path = re.sub(r"\{[^}]*\}", "{}", path)    # OpenAPI 路径参数
    return path.rstrip("/") or "/"


def _unwrap(literal: str) -> str:
    """去掉字面量外层引号（模板串 / 双引号 / 单引号）。"""
    if len(literal) >= 2 and literal[0] in "\"'`" and literal[-1] == literal[0]:
        return literal[1:-1]
    return literal


def _backend_routes() -> set[tuple[str, str]]:
    """后端真实存在的 (METHOD, 路径形状) 集合。"""
    schema = app.openapi()
    routes: set[tuple[str, str]] = set()
    for path, operations in schema["paths"].items():
        for method in operations:
            routes.add((method.upper(), _shape(path)))
    return routes


def _resolve(raw: str, consts: dict[str, str]) -> str | None:
    """把调用实参解析成路径字面量；裸变量走同文件 const 定义回填。"""
    if raw[:1] in "\"'`":
        return _unwrap(raw)
    lit = consts.get(raw)
    return _unwrap(lit) if lit else None


def _frontend_calls(text: str) -> set[tuple[str, str | None]]:
    """提取前端声明的 (大写方法, 完整路径)。方法为 None 表示由调用方决定。"""
    # 只保留「值形如路径」的 const 定义，避免同名变量（如 url）跨作用域串味
    consts = {
        m.group(1): m.group(2)
        for m in _CONST_DEF_RE.finditer(text)
        if _unwrap(m.group(2)).startswith("/")
    }
    # helper 定义区间：位于其中的匹配是形参而非调用
    decl_spans = [(m.start(), m.end()) for m in _DECL_RE.finditer(text)]

    calls: set[tuple[str, str | None]] = set()

    def emit(raw: str, method: str | None, pos: int) -> None:
        if any(start <= pos < end for start, end in decl_spans):
            return
        lit = _resolve(raw, consts)
        if lit is None or not lit.startswith("/"):
            return
        # 去掉路径里拼接的查询串（如 `/x?a=1` / `/x` + qs 的静态部分）
        full = lit if lit.startswith("/api/") else f"{_API_PREFIX}{lit}"
        calls.add((method or "ANY", full.split("?", 1)[0]))

    for m in _XHR_RE.finditer(text):
        emit(m.group(2), m.group(1).upper(), m.start())
    for m in _SSE_POST_RE.finditer(text):
        emit(m.group(1), "POST", m.start())
    for m in _SSE_GET_RE.finditer(text):
        emit(m.group(1), "GET", m.start())
    for m in _RAW_FETCH_RE.finditer(text):
        raw = m.group(1)
        if "${" in raw:  # 动态拼接（helper 实现内部），无法静态判定
            continue
        emit(raw, None, m.start())
    return calls


def _collected(scanned: list[Path] | None = None) -> list[tuple[str, str]]:
    """返回 (来源文件, "METHOD 路径形状") 明细，便于失败时定位。"""
    routes = _backend_routes()
    out: list[tuple[str, str]] = []
    for src in (scanned if scanned is not None else SCAN_FILES):
        text = src.read_text(encoding="utf-8")
        for method, full in sorted(_frontend_calls(text)):
            shape = _shape(full)
            if method == "ANY":
                hit = any(r[1] == shape for r in routes)
            else:
                hit = (method, shape) in routes
            if not hit and (method, shape) not in _NOT_A_ROUTE:
                out.append((src.name, f"{method} {shape}"))
    return out


# --------------------------------------------------------------------------
# 测试
# --------------------------------------------------------------------------
def test_scan_files_exist():
    """被扫描的前端文件必须存在（改名/移动后此用例先失败，而不是静默放过）。"""
    missing = [str(p) for p in SCAN_FILES if not p.exists()]
    assert not missing, f"契约测试扫描目标缺失，请更新 SCAN_FILES：{missing}"


def test_frontend_api_paths_match_backend_routes():
    """前端声明的每个 (method, path) 都必须能在后端 OpenAPI 里找到对应路由。"""
    mismatches = _collected()
    assert not mismatches, (
        "检测到前端调用与后端路由的契约漂移（前端 URL 在后端 OpenAPI 中不存在）：\n"
        + "\n".join(f"  - {fname}: {sig}" for fname, sig in mismatches)
        + "\n\n请二选一修复：① 修正前端 URL；② 若确认应为合法端点，检查后端路由是否漏注册。"
    )


def test_scanner_actually_collects_calls():
    """防止正则失效导致「空集合恒通过」的假绿。"""
    total = sum(len(_frontend_calls(p.read_text(encoding="utf-8"))) for p in SCAN_FILES)
    assert total >= 100, f"提取到的前端 API 调用过少（{total}），疑似扫描正则失效"


def test_scanner_detects_drift(tmp_path):
    """自检：喂入后端不存在的端点必须被标记，否则本契约测试形同虚设。"""
    fake = tmp_path / "Fake.ts"
    fake.write_text(
        'import api from "./index";\n'
        'export const good = () => api.get("/ai/config");\n'
        'export const bad = () => api.get("/definitely-not-a-real-endpoint");\n',
        encoding="utf-8",
    )
    assert _collected([fake]) == [
        ("Fake.ts", "GET /api/v1/definitely-not-a-real-endpoint")
    ]


def test_scanner_ignores_helper_declarations(tmp_path):
    """自检：helper 的「定义」不是「调用」，不得被误提取（回归护栏）。"""
    fake = tmp_path / "Fake.ts"
    fake.write_text(
        'const url = "/ai/config";\n'
        "export async function* sseGetStream(url: string, o?: any) {}\n"
        "export async function* sseFetch(url: string, b?: any) {}\n",
        encoding="utf-8",
    )
    assert _frontend_calls(fake.read_text(encoding="utf-8")) == set()


def test_backend_ai_config_routes_present():
    """回归护栏：ai_config 包化拆分后，全部路由必须仍被注册。"""
    routes = _backend_routes()
    expected = {
        ("GET", "/api/v1/ai/config"),
        ("POST", "/api/v1/ai/config"),
        ("DELETE", "/api/v1/ai/config/{}"),
        ("PATCH", "/api/v1/ai/config/{}/toggle"),
        ("PUT", "/api/v1/ai/fallback-chain"),
        ("GET", "/api/v1/ai/config/export"),
        ("POST", "/api/v1/ai/config/import"),
        ("POST", "/api/v1/ai/config/test"),
        ("POST", "/api/v1/ai/config/precheck"),
        ("POST", "/api/v1/ai/config/precheck-all"),
        ("POST", "/api/v1/ai/config/fetch-models"),
        ("GET", "/api/v1/ai/health"),
        ("GET", "/api/v1/ai/models"),
        ("POST", "/api/v1/ai/custom-models"),
        ("GET", "/api/v1/ai/stats"),
        ("GET", "/api/v1/ai/audit-logs"),
        ("DELETE", "/api/v1/ai/audit-logs"),
        # ✅ 2026-09-23 新增：密钥清除 / 配置变更审计 / 场景模型路由
        ("DELETE", "/api/v1/ai/config/{}/key"),
        ("GET", "/api/v1/ai/config/audit-logs"),
        ("GET", "/api/v1/ai/scene-routes"),
        ("PUT", "/api/v1/ai/scene-routes"),
        # ✅ 2026-09-23 新增：多环境切换 / 配置版本回滚 / 运行时厂商开关
        ("GET", "/api/v1/ai/env"),
        ("PUT", "/api/v1/ai/env"),
        ("POST", "/api/v1/ai/config/{}/rollback"),
        ("GET", "/api/v1/ai/runtime"),
        ("PUT", "/api/v1/ai/runtime/disabled-providers"),
    }
    missing = sorted(expected - routes)
    assert not missing, f"ai_config 路由缺失：{missing}"


def test_ai_config_request_injection_not_exposed_as_param():
    """回归护栏：路由签名里的 `request: Request = None` 必须被识别成「请求对象注入」。

    背景（2026-09-23）：为兼容「单元测试直接调用路由函数」的既有写法，
    这些端点把 `request` 写成了带默认值的 `Request`。默认值本身不影响注入，
    但**注解不能写成 `Request | None`** —— 联合类型会让 FastAPI 识别不出 Request，
    转而把它当作请求体/查询参数（注入失效、审计拿不到 client_ip、
    OpenAPI 里多出一个名为 request 的参数）。本用例锁定该形状。
    """
    schema = app.openapi()["paths"]
    for path, method in [
        ("/api/v1/ai/config", "post"),
        ("/api/v1/ai/config/{config_id}", "delete"),
        ("/api/v1/ai/config/{config_id}/key", "delete"),
        ("/api/v1/ai/config/{config_id}/toggle", "patch"),
        ("/api/v1/ai/fallback-chain", "put"),
        ("/api/v1/ai/config/import", "post"),
        ("/api/v1/ai/scene-routes", "put"),
        ("/api/v1/ai/env", "put"),
        ("/api/v1/ai/config/{config_id}/rollback", "post"),
        ("/api/v1/ai/runtime/disabled-providers", "put"),
        ("/api/v1/prompts/{key}", "patch"),
        ("/api/v1/prompts/{key}/reset", "post"),
    ]:
        params = [q.get("name") for q in schema[path][method].get("parameters", [])]
        assert "request" not in params, (
            f"{method.upper()} {path} 的 Request 注入失效（注解被写成联合类型？）")


if __name__ == "__main__":  # pragma: no cover - 便于本地直接查看漂移清单
    pytest.main([__file__, "-v"])
