# -*- coding: utf-8 -*-
"""静默吞异常护栏（AST 扫描，不依赖 ruff）

背景：全仓 `except Exception: pass` 形态的静默吞异常点已经过专项清理归零；
现存少数刻意兜底统一带 `# noqa: BLE001` + 理由注释。本护栏防止该形态回潮：

判据（对 backend/app 全量 .py 做 AST 扫描）：
  * 拦截对象：`except Exception` / `except BaseException`（含裸 `except:`），
    且分支体**只含** pass / 注释（注释不进 AST，天然只算前两类）/ 常量表达式
    （docstring、`...`、纯字面量）——即「什么都没做」的兜底；
  * 豁免：except 行带 `# noqa: BLE001` 标注（刻意兜底须留理由）；
  * 不在拦截范围：窄类型异常（`except ValueError: pass` 等）、分支体内有
    实际语句（赋值 / 日志调用 / return / raise 等）的宽异常兜底。

判定刻意从宽（只抓「分支体纯空转」这一种形态）：Call 表达式一律视为有实际
语句，避免把 `logger.debug(...)` 之类的尽力而为日志误判为静默吞异常 ——
与 R57「判据必须能判别，不得把刻意放宽的写法误报成缺陷」同族。

判别力自证（R57 惯例）：本文件内置违规样例矩阵，证明扫描器真能抓到原始
缺陷形态、且对全部豁免形态不误报 —— 防止护栏本身退化成恒绿空转。
"""
import ast
import io
import os
import re

_HERE = os.path.dirname(os.path.abspath(__file__))
_BE = os.path.abspath(os.path.join(_HERE, os.pardir))
_APP = os.path.join(_BE, "app")

# 宽异常类型：这些名字出现在 except 头部即属拦截范围
# （type 为 None = 裸 except，_exc_type_name 返回 "bare"，同属拦截范围）
_BROAD_EXC_NAMES = ("Exception", "BaseException", "bare")
_NOQA_BLE001 = re.compile(r"#\s*noqa\b[^\n]*BLE001")


def _py_files(root):
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", "data", "logs")]
        for name in files:
            if name.endswith(".py"):
                yield os.path.join(dirpath, name)


def _read_text(path):
    """utf-8-sig：容忍历史文件 BOM（R57 已记录判据必须吃 BOM，否则被炸）。"""
    return io.open(path, encoding="utf-8-sig").read()


def _is_silent_body(handler):
    """分支体是否「纯空转」：只有 pass / 常量表达式（docstring、`...`、字面量）。

    注释不进 AST。Call（含日志）、赋值、return、raise、if 等一律视为有实际
    语句 → 不静默。
    """
    for stmt in handler.body:
        if isinstance(stmt, ast.Pass):
            continue
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
            continue
        return False
    return True


def _header_text(src_lines, handler):
    """取 except 头部（`except ...:` 所在行）的源码文本，用于识别 noqa 标注。

    多行 except 头部（括号换行）时以首个 body 语句行号为上界；单行
    `except Exception: pass` 时 body 行号与头部相同，取该行。
    """
    first_body_line = handler.body[0].lineno
    end = max(handler.lineno, first_body_line - 1)
    return "\n".join(src_lines[handler.lineno - 1:end])


def _exc_type_name(exc_type):
    """(A, B) 元组形态只要含宽异常即算宽（元组里混窄类型不放行，从严）。"""
    if exc_type is None:
        return "bare"
    if isinstance(exc_type, ast.Name):
        return exc_type.id
    if isinstance(exc_type, ast.Attribute):
        return exc_type.attr
    if isinstance(exc_type, ast.Tuple):
        names = [_exc_type_name(e) for e in exc_type.elts]
        for n in names:
            if n in _BROAD_EXC_NAMES:
                return n
        return None
    return None


def scan_source(src, filename="<src>"):
    """扫描一段源码，返回违规列表：[(行号, 函数名), ...]。"""
    tree = ast.parse(src, filename=filename)
    src_lines = src.splitlines()
    violations = []

    def visit(node, func_name):
        for child in ast.iter_child_nodes(node):
            child_func = func_name
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                child_func = child.name
            elif isinstance(child, ast.Lambda):
                child_func = func_name or "<lambda>"
            if isinstance(child, ast.ExceptHandler):
                type_name = _exc_type_name(child.type)
                if (type_name in _BROAD_EXC_NAMES and _is_silent_body(child)
                        and not _NOQA_BLE001.search(
                            _header_text(src_lines, child))):
                    violations.append((child.lineno, func_name or "<module>"))
            visit(child, child_func)

    visit(tree, None)
    return violations


def scan_tree(root, rel_base=None):
    """扫描目录下全部 .py，返回违规列表：[(相对路径, 行号, 函数名), ...]。

    rel_base：报告路径的相对基准（默认相对 root 本身）。跨盘符（Windows C:/J:）
    时 os.path.relpath 会抛 ValueError，故基准必须与 root 同盘。
    """
    rel_base = rel_base or root
    out = []
    for path in _py_files(root):
        src = _read_text(path)
        for lineno, func in scan_source(src, filename=path):
            out.append((os.path.relpath(path, rel_base), lineno, func))
    return out


class TestNoSilentBroadExcept:
    """backend/app 不得存在无 noqa: BLE001 标注的静默宽异常兜底。"""

    def test_app_has_no_silent_broad_except(self):
        violations = scan_tree(_APP, rel_base=_BE)
        assert violations == [], (
            "发现静默吞异常（except Exception/BaseException 分支体纯空转且无 "
            "# noqa: BLE001 标注）。刻意兜底请补 `# noqa: BLE001 - <理由>`；"
            "否则请记录日志/返回错误。违规明细：%s" % violations)


class TestDetectorDiscrimination:
    """判别力自证：扫描器必须真能抓到缺陷形态，且对豁免形态零误报。"""

    def test_flags_pass_body(self):
        src = "def f():\n    try:\n        g()\n    except Exception:\n        pass\n"
        assert scan_source(src) == [(4, "f")]

    def test_flags_docstring_only_body(self):
        src = ("def f():\n    try:\n        g()\n    except Exception:\n"
               "        \"\"\"什么都不做\"\"\"\n")
        assert scan_source(src) == [(4, "f")]

    def test_flags_ellipsis_body(self):
        src = "def f():\n    try:\n        g()\n    except BaseException: ...\n"
        assert scan_source(src) == [(4, "f")]

    def test_flags_bare_except(self):
        src = "def f():\n    try:\n        g()\n    except:\n        pass\n"
        assert scan_source(src) == [(4, "f")]

    def test_flags_module_level_and_reports_function_name(self):
        src = ("def outer():\n"
               "    def inner():\n"
               "        try:\n"
               "            g()\n"
               "        except Exception:\n"
               "            pass\n")
        # 行号定位在 except 头部行（第 5 行），函数名取最近嵌套函数
        assert scan_source(src) == [(5, "inner")]

    def test_noqa_is_exempt(self):
        src = ("def f():\n"
               "    try:\n"
               "        g()\n"
               "    except Exception:  # noqa: BLE001 - 刻意兜底，服务启动优先\n"
               "        pass\n")
        assert scan_source(src) == []

    def test_narrow_exception_is_not_in_scope(self):
        src = ("def f():\n"
               "    try:\n"
               "        g()\n"
               "    except ValueError:\n"
               "        pass\n"
               "    try:\n"
               "        g()\n"
               "    except (ValueError, KeyError):\n"
               "        pass\n")
        assert scan_source(src) == []

    def test_body_with_real_statements_is_not_flagged(self):
        src = ("import logging\n"
               "log = logging.getLogger(__name__)\n"
               "def f():\n"
               "    try:\n"
               "        g()\n"
               "    except Exception as e:\n"
               "        log.debug(\"忽略: %s\", e)\n"
               "    try:\n"
               "        g()\n"
               "    except Exception:\n"
               "        x = 1\n"
               "    try:\n"
               "        g()\n"
               "    except Exception:\n"
               "        return None\n"
               "    try:\n"
               "        g()\n"
               "    except Exception:\n"
               "        raise\n")
        assert scan_source(src) == []

    def test_tuple_with_broad_name_is_flagged(self):
        src = ("def f():\n"
               "    try:\n"
               "        g()\n"
               "    except (ValueError, Exception):\n"
               "        pass\n")
        assert scan_source(src) == [(4, "f")]

    def test_detector_works_on_real_file_in_tmp_dir(self):
        """端到端：把违规样例写成真实文件进临时目录，目录级扫描必须报出
        文件、行号与函数名 —— 证明 scan_tree 的文件遍历与报告链路可用。"""
        import tempfile
        src = ("def busted():\n"
               "    try:\n"
               "        g()\n"
               "    except Exception:\n"
               "        pass\n")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "violation_probe.py")
            with io.open(path, "w", encoding="utf-8") as fh:
                fh.write(src)
            # 报告路径相对扫描根（跨盘符环境下不能相对 backend 基准取 relpath）
            assert scan_tree(tmp) == [("violation_probe.py", 4, "busted")]
