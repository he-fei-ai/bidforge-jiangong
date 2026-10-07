"""✅ 2026-10-06（G1/G3）：AI 配置模块新增契约的**跨语言 parity 护栏**。

背景
----
本轮后端在 AI 配置模块新增/扩展了四组契约：

  1. ``POST /ai/config/import/dry-run`` —— 导入预演（只校验不落库）；
  2. ``GET  /ai/configs/health``        —— 全部配置的可用性体检；
  3. ``ConfigImportIn`` 加法式新增 ``scene_routes`` / ``runtime`` —— 配置迁移时
     把场景模型路由与运行时设置一并带走；
  4. ``POST /ai/config/precheck-all`` 回传 ``skipped_no_url``。

前端已逐条接线（``types/aiConfig.ts`` 类型、``api/index.ts`` 方法、
``pages/AIConfigPage.tsx`` 展示）。本文件存在的意义是：**后端返回体改一个字
段而前端类型没跟上时立即失败**。

判据来源（关键设计）
--------------------
字段集合**不从前端类型反推**，而是用 AST 从**后端函数体**直接提取：

  * ``return {...}`` 的字面量键 → 顶层字段；
  * ``xxx.append({...})`` 的字面量键 → 列表元素字段（如 dry-run 的 ``planned``、
    health 的 ``items``）；
  * ``xxx = {...}`` 的字面量键 → 汇总字段（如 health 的 ``summary``）。

这样两端任一方向漂移（后端删字段 / 前端漏声明）都会失败，而不需要维护一份
手写字段清单（手写清单本身会漂移，是本仓反复出现的判据分叉根因，见 AGENTS §5.14）。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
APP_DIR = BACKEND_DIR / "app"
FRONTEND_DIR = BACKEND_DIR.parent / "frontend"

CONFIG_PY = APP_DIR / "routers" / "ai_config" / "config.py"
CONNECTIVITY_PY = APP_DIR / "routers" / "ai_config" / "connectivity.py"
MODELS_PY = APP_DIR / "models.py"
TYPES_TS = FRONTEND_DIR / "src" / "types" / "aiConfig.ts"
API_TS = FRONTEND_DIR / "src" / "api" / "index.ts"
PAGE_TSX = FRONTEND_DIR / "src" / "pages" / "AIConfigPage.tsx"


# ---------------------------------------------------------------------------
# 后端：从函数体提取返回体字段（不依赖运行时启动）
# ---------------------------------------------------------------------------

def _str_keys(node: ast.Dict) -> set[str]:
    """Dict 字面量里的字符串键集合（非字面量键一律忽略）。"""
    return {
        k.value for k in node.keys
        if isinstance(k, ast.Constant) and isinstance(k.value, str)
    }


def _own_children(node: ast.AST) -> list[ast.stmt]:
    """遍历语句树，但**不进入嵌套 def / lambda / class**。

    端点函数里通常内嵌 helper（如 ``configs_health`` 内的 ``_probe_one``、
    ``precheck_all`` 内的 ``_one``），它们各自返回不同的 dict 形状。
    若不剪枝，helper 的 return 会被算成端点契约的一部分 —— 这是本护栏
    首版实测踩出的假判据（把 network probe 的 ok/step/message 当成体检返回字段）。
    """
    out: list[ast.stmt] = []

    def rec(n: ast.AST) -> None:
        for child in ast.iter_child_nodes(n):
            if isinstance(child, (ast.AsyncFunctionDef, ast.FunctionDef,
                                  ast.Lambda, ast.ClassDef)):
                continue
            out.append(child)
            rec(child)

    rec(node)
    return out


def _func_node(src: str, name: str) -> ast.AsyncFunctionDef:
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"找不到函数 {name}（护栏锚点失效，请先确认端点仍在）")


def _return_keys(func: ast.AsyncFunctionDef) -> set[str]:
    """函数体（不含嵌套函数）所有 ``return {...}`` 的字面量键并集。"""
    keys: set[str] = set()
    for node in _own_children(func):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            keys |= _str_keys(node.value)
    assert keys, f"{func.name} 没有 dict 返回体"
    return keys


def _append_keys(func: ast.AsyncFunctionDef, target: str) -> set[str]:
    """``target.append({...})`` 的字面量键并集（列表元素形状）。"""
    keys: set[str] = set()
    for node in _own_children(func):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if (isinstance(fn, ast.Attribute) and fn.attr == "append"
                and isinstance(fn.value, ast.Name) and fn.value.id == target
                and node.args and isinstance(node.args[0], ast.Dict)):
            keys |= _str_keys(node.args[0])
    assert keys, f"{func.name} 中找不到 {target}.append({...})"
    return keys


def _assigned_keys(func: ast.AsyncFunctionDef, target: str) -> set[str]:
    """``target = {...}`` 的字面量键并集（汇总形状）。"""
    keys: set[str] = set()
    for node in _own_children(func):
        if not isinstance(node, ast.Assign):
            continue
        tgts = [t for t in node.targets if isinstance(t, ast.Name) and t.id == target]
        if tgts and isinstance(node.value, ast.Dict):
            keys |= _str_keys(node.value)
    assert keys, f"{func.name} 中找不到 {target} = dict 字面量"
    return keys


def _pydantic_fields(src: str, class_name: str) -> set[str]:
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return {
                stmt.target.id for stmt in node.body
                if isinstance(stmt, ast.AnnAssign)
                and isinstance(stmt.target, ast.Name)
            }
    raise AssertionError(f"找不到模型 {class_name}")


def _route_paths(path: Path, methods: set[str]) -> set[str]:
    """路由文件里注册的路径（小写归一，不含 /api/v1 前缀）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    paths: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name)
                and fn.value.id == "router"):
            continue
        if fn.attr.lower() not in methods:
            continue
        if (node.args and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)):
            paths.add(node.args[0].value.lower())
    assert paths, f"{path} 未解析出任何路由"
    return paths


# ---------------------------------------------------------------------------
# 前端：从 TS 源提取接口/方法声明
# ---------------------------------------------------------------------------

def _ts_interface_fields(src: str, name: str) -> set[str]:
    """``export interface Name { ... }`` 内声明的字段名集合（含嵌套字面量的键）。"""
    m = re.search(rf"export interface {re.escape(name)}\s*\{{", src)
    assert m, f"types/aiConfig.ts 未声明接口 {name}"
    start, depth, i = m.end(), 1, m.end()
    while i < len(src) and depth:
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
        i += 1
    body = src[start:i - 1]
    return {
        fm.group(1).strip().rstrip("?")
        for fm in re.finditer(r"^\s*([A-Za-z_$][\w$]*)\??\s*:", body, flags=re.M)
    }


def _ts_method_body(src: str, method: str) -> str:
    """``methodName: (args) => ...`` 起始处的一段方法体文本。"""
    m = re.search(rf"\b{re.escape(method)}\s*:\s*\(", src)
    assert m, f"api/index.ts 未声明方法 {method}"
    return src[m.start():m.start() + 900]


DRY_RUN_PATH = "/ai/config/import/dry-run"
HEALTH_PATH = "/ai/configs/health"
IMPORT_FIELDS = ("items", "overwrite", "set_first_active", "scene_routes", "runtime")


class TestImportDryRunParity:
    """导入预演：后端返回体 ⊆ 前端类型声明。"""

    def test_endpoint_is_registered(self):
        assert "/config/import/dry-run" in _route_paths(CONFIG_PY, {"post"})

    def test_top_level_keys_declared_in_frontend_type(self):
        src = CONFIG_PY.read_text(encoding="utf-8")
        keys = _return_keys(_func_node(src, "import_config_dry_run"))
        declared = _ts_interface_fields(TYPES_TS.read_text(encoding="utf-8"),
                                        "AIConfigImportDryRunResponse")
        missing = keys - declared
        assert not missing, (
            "后端导入预演返回的字段前端类型未声明：" + "、".join(sorted(missing))
            + "（前端拿不到这些值或退化为 any）"
        )

    def test_planned_item_keys_declared_in_frontend_type(self):
        src = CONFIG_PY.read_text(encoding="utf-8")
        keys = _append_keys(_func_node(src, "import_config_dry_run"), "planned")
        declared = _ts_interface_fields(TYPES_TS.read_text(encoding="utf-8"),
                                        "AIConfigImportDryRunResponse")
        missing = keys - declared
        assert not missing, "planned[] 元素的字段前端类型未声明：" + "、".join(sorted(missing))

    def test_api_method_posts_to_same_path(self):
        body = _ts_method_body(API_TS.read_text(encoding="utf-8"), "importConfigDryRun")
        assert DRY_RUN_PATH in body, "前端方法路径与后端注册路径不一致"

    def test_api_method_sends_every_import_field(self):
        """加法式字段不能在前端被静默丢弃（否则后端能力永久闲置）。"""
        fields = _pydantic_fields(MODELS_PY.read_text(encoding="utf-8"), "ConfigImportIn")
        missing = [f for f in IMPORT_FIELDS
                   if f not in _ts_method_body(API_TS.read_text(encoding="utf-8"),
                                               "importConfigDryRun")]
        assert not missing, f"导入预演请求体缺字段：{missing}"
        assert set(IMPORT_FIELDS) <= fields, (
            "后端 ConfigImportIn 字段集与前端请求体不一致（前端发送 "
            f"{{{'、'.join(IMPORT_FIELDS)}}}）"
        )


class TestConfigsHealthParity:
    """全部配置体检：后端返回体 ⊆ 前端类型声明。"""

    def test_endpoint_is_registered(self):
        assert "/configs/health" in _route_paths(CONNECTIVITY_PY, {"get"})

    def _func(self):
        return _func_node(CONNECTIVITY_PY.read_text(encoding="utf-8"), "configs_health")

    def test_top_level_keys_declared(self):
        keys = _return_keys(self._func())
        declared = _ts_interface_fields(TYPES_TS.read_text(encoding="utf-8"),
                                        "AIConfigsHealthResponse")
        missing = keys - declared
        assert not missing, "体检返回的顶层字段前端类型未声明：" + "、".join(sorted(missing))

    def test_item_keys_declared(self):
        keys = _append_keys(self._func(), "items")
        declared = _ts_interface_fields(TYPES_TS.read_text(encoding="utf-8"),
                                        "AIConfigsHealthItem")
        missing = keys - declared
        assert not missing, "体检 items[] 的字段前端类型未声明：" + "、".join(sorted(missing))

    def test_summary_keys_declared(self):
        keys = _assigned_keys(self._func(), "summary")
        declared = _ts_interface_fields(TYPES_TS.read_text(encoding="utf-8"),
                                        "AIConfigsHealthResponse")
        missing = keys - declared
        assert not missing, "体检 summary 的字段前端类型未声明：" + "、".join(sorted(missing))

    def test_api_method_gets_same_path(self):
        body = _ts_method_body(API_TS.read_text(encoding="utf-8"), "configsHealth")
        assert HEALTH_PATH in body, "前端方法路径与后端注册路径不一致"


class TestPrecheckAllSkippedParity:
    """批量预检：`skipped_no_url` 必须被前端消费（不消费 = 用户误判配置不存在）。"""

    def test_return_keys_declared_and_consumed(self):
        src = CONNECTIVITY_PY.read_text(encoding="utf-8")
        keys = _return_keys(_func_node(src, "precheck_all"))
        declared = _ts_interface_fields(TYPES_TS.read_text(encoding="utf-8"),
                                        "AIPrecheckAllResponse")
        missing = keys - declared
        assert not missing, "precheck-all 返回字段前端类型未声明：" + "、".join(sorted(missing))
        page = PAGE_TSX.read_text(encoding="utf-8")
        assert "skipped_no_url" in page, "页面未消费 precheck-all 的 skipped_no_url"


class TestPageConsumesNewAbilities:
    """页面必须真正消费新增能力（类型对了但不展示 = 用户永远看不见）。"""

    def setup_method(self):
        self.page = PAGE_TSX.read_text(encoding="utf-8")

    def test_health_button_is_wired(self):
        assert "configsHealth" in self.page, "页面未调用配置体检"
        assert "setConfigsHealthResult" in self.page, "页面未保存体检结果"
        # 体检的五个维度必须逐条展开，而不是只报一个总数
        for dim in ("key_broken", "no_url", "out_of_env", "disabled", "network_unreachable"):
            assert dim in self.page, f"体检结果未展示维度 {dim}"

    def test_import_carries_scene_routes_and_runtime(self):
        assert "importConfigDryRun" in self.page, "导入前未跑预演"
        assert "scene_routes" in self.page, "导入未读取导出文件的 scene_routes"
        assert "migrated_scene_routes" in self.page, "迁移计数未回显给用户"
        assert "migrated_runtime_keys" in self.page, "运行时迁移计数未回显给用户"

    def test_export_reports_additive_counts(self):
        assert "scene_route_count" in self.page, "导出提示未说明附带场景路由"
        assert "runtime_keys" in self.page, "导出提示未说明附带运行时设置"


class TestImportBackwardCompatibility:
    """向后兼容红线：新增字段必须可选且默认 None（旧导出文件行为逐字不变）。"""

    def test_import_optional_fields_default_to_none(self):
        src = MODELS_PY.read_text(encoding="utf-8")
        tree = ast.parse(src)
        found = False
        for node in ast.walk(tree):
            if not (isinstance(node, ast.ClassDef) and node.name == "ConfigImportIn"):
                continue
            found = True
            for stmt in node.body:
                if not (isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)):
                    continue
                name = stmt.target.id
                if name in ("scene_routes", "runtime"):
                    assert isinstance(stmt.value, ast.Constant) and stmt.value.value is None, (
                        f"ConfigImportIn.{name} 必须默认 None："
                        "旧版导出文件不含这两个键，非 None 默认值会改变既有行为"
                    )
                else:
                    assert stmt.value is not None, (
                        f"ConfigImportIn.{name} 缺少默认值，会破坏旧调用方"
                    )
        assert found, "models.py 中找不到 ConfigImportIn"

    def test_frontend_types_declare_migrated_fields(self):
        declared = _ts_interface_fields(TYPES_TS.read_text(encoding="utf-8"),
                                        "AIConfigImportResponse")
        want = {"migrated_scene_routes", "migrated_runtime_keys"}
        missing = want - declared
        assert not missing, "导入响应新增的迁移计数未在前端类型声明：" + "、".join(sorted(missing))


def test_backend_import_response_actually_returns_migrated_counts():
    """前端类型声明了两个迁移计数，后端必须真的回传（否则是空契约）。"""
    src = CONFIG_PY.read_text(encoding="utf-8")
    keys = _return_keys(_func_node(src, "import_config"))
    assert {"migrated_scene_routes", "migrated_runtime_keys"} <= keys, (
        "POST /ai/config/import 未回传迁移计数："
        + "、".join(sorted({"migrated_scene_routes", "migrated_runtime_keys"} - keys))
    )


