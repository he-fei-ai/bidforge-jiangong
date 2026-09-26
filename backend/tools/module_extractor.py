"""增量式核心功能模块提取工具（零外部依赖，纯标准库）。

设计目标（对应需求）：
  1. 深度探索并提取项目核心功能模块的结构、依赖、路由、配置参数。
  2. 多轮次增量提取：已完成的模块在后续运行中【自动跳过】，只提取
     新发现 / 尚未完成 / 源文件已变更（签名不符）的模块。
  3. 关键信息持久化到本地状态库（默认 backend/data/module_map.json），
     支持断电续跑，保证流程连续性。
  4. 人工/AI 补写的 summary 语义字段在重跑时被【保留】，不会被机械覆盖。

用法：
  python -m tools.module_extractor plan            # 只看本轮将要提取哪些（不写盘）
  python -m tools.module_extractor run --limit 12  # 本轮最多提取 12 个待处理模块
  python -m tools.module_extractor run --all       # 一次性提取全部待处理模块
  python -m tools.module_extractor status          # 查看历史进度总览
  python -m tools.module_extractor show <module_id># 查看单个模块详情
  python -m tools.module_extractor annotate <id> --summary "..."  # 补写语义摘要

增量判定采用「签名」：对模块所含每个文件计算 sha1(相对路径 + 内容)，
再聚合为模块签名。只要源文件未改动且状态为 completed，即跳过。
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# ------------------------------------------------------------------ 路径常量
TOOLS_DIR = Path(__file__).resolve().parent            # backend/tools
BACKEND_DIR = TOOLS_DIR.parent                          # backend
PROJECT_ROOT = BACKEND_DIR.parent                       # 工作区根
STATE_PATH = BACKEND_DIR / "data" / "module_map.json"   # 持久化状态库
STATE_VERSION = 1

# 提取时忽略的噪声（测试/缓存/迁移脚本等）
_IGNORE_NAME_SUBSTR = ("__pycache__", ".pytest", "seed_data", "check_db")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _rel(p: Path) -> str:
    """相对项目根的统一 posix 风格路径，保证跨机器签名稳定。"""
    return p.resolve().relative_to(PROJECT_ROOT).as_posix()


# ------------------------------------------------------------------ 模块发现
def discover_modules() -> dict[str, dict]:
    """扫描源码树，产出候选模块定义。

    返回 {module_id: {"name","kind","domain","files":[abs Path,...]}}
    粒度：一个后端路由 / 一个服务 / 一个核心层文件 / 一个前端页面 = 一个模块。
    模块间的真实依赖通过后续 AST import 分析得到。
    """
    found: dict[str, dict] = {}

    def add(mid: str, name: str, kind: str, domain: str, files: list[Path]):
        files = [f for f in files if not any(s in str(f) for s in _IGNORE_NAME_SUBSTR)]
        if files:
            found[mid] = {"name": name, "kind": kind, "domain": domain, "files": files}

    # 1) 后端核心层
    core_dir = BACKEND_DIR / "app"
    for f in sorted(core_dir.glob("*.py")):
        if f.name == "__init__.py":
            continue
        add(f"core:{f.stem}", f.stem, "core", "backend", [f])

    # 2) 后端路由（每个 router = 一个功能域模块）
    routers_dir = core_dir / "routers"
    for f in sorted(routers_dir.glob("*.py")):
        if f.name == "__init__.py":
            continue
        kind = "router_helper" if f.name.startswith("_") else "router"
        add(f"router:{f.stem}", f.stem, kind, "backend", [f])

    # 3) 后端服务（递归，含 services/ai/**、prompts/**、providers/**）
    services_dir = core_dir / "services"
    for f in sorted(services_dir.rglob("*.py")):
        if f.name == "__init__.py":
            continue
        sub = f.relative_to(services_dir).parent.as_posix()
        domain_path = f"services/{sub}" if sub != "." else "services"
        add(f"service:{f.relative_to(services_dir).with_suffix('').as_posix()}",
            f.stem, "service", domain_path, [f])

    # 4) 前端页面 / 组件 / api / hooks / utils
    fe_pages = PROJECT_ROOT / "frontend" / "src" / "pages"
    if fe_pages.exists():
        for f in sorted(fe_pages.glob("*.tsx")):
            add(f"fe_page:{f.stem}", f.stem, "frontend_page", "frontend", [f])
    fe_api = PROJECT_ROOT / "frontend" / "src" / "api" / "index.ts"
    if fe_api.exists():
        add("fe_api:index", "api/index", "frontend_api", "frontend", [fe_api])
    fe_hooks = PROJECT_ROOT / "frontend" / "src" / "hooks"
    if fe_hooks.exists():
        for f in sorted(fe_hooks.glob("*.ts")):
            add(f"fe_hook:{f.stem}", f.stem, "frontend_hook", "frontend", [f])

    return found


# ------------------------------------------------------------------ 签名计算
def compute_signature(files: list[Path]) -> str:
    """对模块内所有文件计算稳定聚合签名（相对路径 + 内容）。"""
    h = hashlib.sha1()
    for f in sorted(files, key=lambda x: _rel(x)):
        try:
            data = f.read_bytes()
        except OSError:
            data = b"<unreadable>"
        h.update(_rel(f).encode("utf-8"))
        h.update(b"\x00")
        h.update(hashlib.sha1(data).hexdigest().encode("ascii"))
        h.update(b"\x00")
    return h.hexdigest()[:16]


# ------------------------------------------------------------------ 结构提取
_FASTAPI_METHODS = ("get", "post", "put", "patch", "delete", "websocket")
# 「大文件」阈值：超过则计入 large_files 遥测，便于在状态库中定位需要重点
# 关注的巨型文件（如 5000+ 行的前端页面、上千行的路由器）。
_LARGE_FILE_LOC = 800


def _read_source(path: Path) -> tuple[str, int]:
    """一次性读取源文件，返回 (文本, 字节数)。读失败返回 ("", -1)。

    合并了「签名」与「结构解析」各自读盘的需求，大文件下 IO 减半。
    """
    try:
        data = path.read_bytes()
    except OSError:
        return "", -1
    return data.decode("utf-8", "replace"), len(data)


def _py_structure(path: Path) -> dict:
    """AST 解析单个 .py：类/函数/路由/内部依赖/外部依赖/配置引用/LOC/体积。

    大文件优化：单次 ast.walk 同时完成符号/依赖收集与路由识别（旧实现遍历两遍）；
    健壮性：读盘失败或语法错误均降级为 parse_error 记录，绝不向上抛异常。
    """
    empty = {"loc": 0, "classes": [], "functions": [], "routes": [],
             "internal_deps": [], "external_deps": [], "config_params": [],
             "size_bytes": 0, "parse_error": True}
    src, size = _read_source(path)
    if size < 0:
        return empty
    loc = src.count("\n") + 1
    try:
        tree = ast.parse(src)
    except (SyntaxError, ValueError):
        return {**empty, "loc": loc, "size_bytes": size}

    classes, functions, routes = [], [], []
    internal_deps: set[str] = set()
    external_deps: set[str] = set()
    config_refs: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            classes.append(node.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if getattr(node, "col_offset", 0) == 0:  # 顶层函数
                functions.append(node.name)
            # 路由：@router.get("/x") / @app.post(...) —— 复用同一趟遍历
            for dec in node.decorator_list:
                if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute):
                    fname = dec.func.attr
                    if fname in _FASTAPI_METHODS and dec.args and isinstance(dec.args[0], ast.Constant):
                        routes.append({"method": fname.upper(), "path": str(dec.args[0].value)})
        elif isinstance(node, ast.Import):
            for a in node.names:
                root = a.name.split(".")[0]
                (internal_deps if a.name.startswith("app") else external_deps).add(a.name if a.name.startswith("app") else root)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod.startswith("app") or (node.level and node.level > 0):
                target = mod if mod.startswith("app") else f"(relative){mod}"
                internal_deps.add(target)
            elif mod:
                external_deps.add(mod.split(".")[0])
        elif isinstance(node, ast.Attribute):
            # settings.xxx / config.xxx 配置引用
            base = node.value
            if isinstance(base, ast.Name) and base.id in ("settings", "config") and isinstance(node.ctx, ast.Load):
                config_refs.add(f"{base.id}.{node.attr}")

    # 从 settings. 归一到裸参数名，便于阅读
    cfg = sorted({c.split(".", 1)[1] for c in config_refs})
    return {
        "loc": loc,
        "classes": sorted(set(classes)),
        "functions": sorted(set(functions)),
        "routes": routes,
        "internal_deps": sorted(internal_deps),
        "external_deps": sorted(external_deps),
        "config_params": cfg,
        "size_bytes": size,
        "parse_error": False,
    }


def _ts_structure(path: Path) -> dict:
    """轻量正则解析 .ts/.tsx：import、API 调用、导出符号、LOC、体积。"""
    empty = {"loc": 0, "exports": [], "internal_deps": [], "external_deps": [],
             "endpoints_called": [], "size_bytes": 0, "parse_error": True}
    src, size = _read_source(path)
    if size < 0:
        return empty
    imports = re.findall(r"""from\s+['"]([^'"]+)['"]""", src)
    internal = sorted({i for i in imports if i.startswith((".", "@/"))})
    external = sorted({i for i in imports if not i.startswith((".", "@/"))})
    api_calls = sorted(set(re.findall(r"""(?:api|request|axios|http)\s*\.\s*(get|post|put|patch|delete)\s*(?:<[^>]*>)?\s*\(\s*[`'"]([^`'"]+)""", src)))
    endpoints = [f"{m.upper()} {u}" for m, u in api_calls]
    exports = re.findall(r"""export\s+(?:default\s+)?(?:async\s+)?(?:function|const|class)\s+(\w+)""", src)
    return {
        "loc": src.count("\n") + 1,
        "exports": sorted(set(exports)),
        "internal_deps": internal,
        "external_deps": external,
        "endpoints_called": endpoints,
        "size_bytes": size,
        "parse_error": False,
    }


# 需要跨文件求并集的列表型字段（py/ts 两类结构键的并集，缺键自动忽略）。
_LIST_KEYS = ("classes", "functions", "internal_deps", "external_deps",
              "config_params", "exports", "endpoints_called")


def _aggregate_structures(per_file: list[dict]) -> dict:
    """把模块内【多个文件】的结构合并为一份，并保留逐文件明细。

    - 顶层字段（loc/classes/.../config_params）向后兼容，值改为跨文件聚合；
    - 新增 size_bytes/file_count/max_file_loc/large_files/files_detail，
      让「多文件、大文件」模块的构成一目了然（可追溯、可定位巨型文件）。
    """
    merged: dict[str, set] = {k: set() for k in _LIST_KEYS}
    routes: list[dict] = []
    seen_routes: set[tuple] = set()
    total_loc = total_size = max_file_loc = large_files = 0
    parse_error = False
    files_detail: list[dict] = []
    for s in per_file:
        loc = s.get("loc", 0)
        size = max(s.get("size_bytes", 0), 0)
        total_loc += loc
        total_size += size
        max_file_loc = max(max_file_loc, loc)
        if loc >= _LARGE_FILE_LOC:
            large_files += 1
        if s.get("parse_error"):
            parse_error = True
        for k in _LIST_KEYS:
            merged[k].update(s.get(k, []) or [])
        for r in s.get("routes", []) or []:
            key = (r.get("method"), r.get("path"))
            if key not in seen_routes:
                seen_routes.add(key)
                routes.append(r)
        files_detail.append({
            "path": s.get("_path", ""),
            "loc": loc,
            "size_bytes": size,
            "parse_error": bool(s.get("parse_error")),
        })
    out: dict = {k: sorted(merged[k]) for k in _LIST_KEYS}
    out.update({
        "loc": total_loc,
        "routes": routes,
        "size_bytes": total_size,
        "file_count": len(per_file),
        "max_file_loc": max_file_loc,
        "large_files": large_files,
        "parse_error": parse_error,
        "files_detail": sorted(files_detail, key=lambda d: -d["loc"]),
    })
    return out


# ------------------------------------------------------------------ 状态库
def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"meta": {"version": STATE_VERSION, "project": PROJECT_ROOT.name,
                     "created_at": _now(), "rounds": 0, "updated_at": _now()},
            "modules": {}}


def save_state(state: dict) -> None:
    """原子写入（临时文件 + os.replace），避免中途断电损坏状态库。"""
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    state["meta"]["updated_at"] = _now()
    fd, tmp = tempfile.mkstemp(dir=str(STATE_PATH.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_PATH)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


# 提取优先级：核心层与路由/服务属「核心功能模块」，优先于前端。
_KIND_PRIORITY = {"core": 0, "router": 1, "router_helper": 2, "service": 3,
                  "frontend_page": 4, "frontend_api": 5, "frontend_hook": 6}


def _prio(mid: str, catalog: dict[str, dict]) -> tuple:
    k = catalog[mid]["kind"]
    return (_KIND_PRIORITY.get(k, 9), mid)


def pending_modules(state: dict, catalog: dict[str, dict],
                    sigs: dict[str, str] | None = None) -> list[str]:
    """返回本轮待提取模块 id：未完成 / 签名变更 / 全新。已完成的自动跳过。

    按「核心功能模块优先」排序（core/router/service 先于 frontend）。
    sigs 为预先算好的签名字典，传入可避免与提取阶段重复读盘计算。
    """
    done = state["modules"]
    sigs = sigs or {}
    pending = []
    for mid, meta in catalog.items():
        rec = done.get(mid)
        sig = sigs.get(mid) or compute_signature(meta["files"])
        if rec is None or rec.get("status") != "completed" or rec.get("signature") != sig:
            pending.append(mid)
    return sorted(pending, key=lambda m: _prio(m, catalog))


def _catalog_signatures(catalog: dict[str, dict]) -> dict[str, str]:
    """一次性计算全部候选模块签名，供筛选与提取复用，
    避免对同一大文件在 plan/run 阶段重复读盘 + 哈希。"""
    return {mid: compute_signature(meta["files"]) for mid, meta in catalog.items()}


# ------------------------------------------------------------------ 提取动作
def _parse_one_file(f: Path) -> dict:
    """按扩展名选择解析器，单文件解析失败不中断整模块。"""
    st = _ts_structure(f) if f.suffix in (".ts", ".tsx") else _py_structure(f)
    st["_path"] = _rel(f)
    return st


def extract_one(mid: str, meta: dict, state: dict, round_no: int,
                signature: str | None = None) -> dict:
    files = meta["files"]
    kind = meta["kind"]
    # 【多文件】逐个解析后聚合（旧实现只取 files[0]，会丢失同模块其余文件）
    per_file = [_parse_one_file(f) for f in files]
    struct = _aggregate_structures(per_file)

    prev = state["modules"].get(mid, {})
    rec = {
        "id": mid,
        "name": meta["name"],
        "kind": kind,
        "domain": meta["domain"],
        "files": [_rel(f) for f in files],
        "signature": signature or compute_signature(files),
        "status": "completed",
        "extracted_at": _now(),
        "extraction_round": round_no,
        "structure": struct,
        # 保留人工/AI 语义补写（重跑不清空）
        "summary": prev.get("summary", ""),
    }
    state["modules"][mid] = rec
    return rec


# ------------------------------------------------------------------ 命令实现
def cmd_plan(args) -> int:
    catalog = discover_modules()
    state = load_state()
    sigs = _catalog_signatures(catalog)
    pend = pending_modules(state, catalog, sigs)
    done = state["modules"]
    completed_now = len([m for m in catalog if done.get(m, {}).get("status") == "completed"])
    stale = [m for m in done if m not in catalog]
    print(f"[plan] 目录模块总数={len(catalog)}  已完成(将跳过)={completed_now}  "
          f"待提取/变更={len(pend)}  已移除(源文件消失)={len(stale)}")
    print("本轮将提取（自动跳过已完成）：")
    for m in (pend[:args.limit] if args.limit else pend):
        print(f"  - {m}")
    return 0


def cmd_run(args) -> int:
    catalog = discover_modules()
    state = load_state()
    sigs = _catalog_signatures(catalog)
    if getattr(args, "force", False):
        # 强制重提取：忽略「已完成且签名未变」的跳过逻辑，为全量模块补齐新字段
        pend = sorted(catalog, key=lambda m: _prio(m, catalog))
    else:
        pend = pending_modules(state, catalog, sigs)
    if not pend:
        state["meta"]["rounds"] = state["meta"].get("rounds", 0)
        print("[run] 没有待处理模块：所有已发现模块均已提取且源文件未变更（全部跳过）。"
              "如需补齐新字段可用 run --force。")
        return 0
    state["meta"]["rounds"] = state["meta"].get("rounds", 0) + 1
    rnd = state["meta"]["rounds"]
    target = pend if (args.all or getattr(args, "force", False)) else pend[: (args.limit or 10)]
    extracted = []
    for mid in target:
        rec = extract_one(mid, catalog[mid], state, rnd, sigs.get(mid))
        extracted.append(rec)
    save_state(state)
    skipped = len(catalog) - len(extracted)
    print(f"[run] 第 {rnd} 轮完成：本轮提取 {len(extracted)} 个，跳过（已完成/未选中）{skipped} 个，"
          f"累计已提取 {len(state['modules'])} 个。状态库 -> {_rel(STATE_PATH)}")
    for r in extracted:
        s = r["structure"]
        extra = ""
        if r["kind"] == "router":
            extra = f" routes={len(s.get('routes', []))}"
        elif r["kind"] == "service":
            extra = f" deps={len(s.get('internal_deps', []))}"
        big = f" big={s.get('large_files', 0)}" if s.get("large_files") else ""
        multi = f" files={s.get('file_count', 1)}" if s.get("file_count", 1) > 1 else ""
        print(f"  ✔ {r['id']}  kind={r['kind']} loc={s.get('loc','?')}{extra}{multi}{big}")
    return 0


def cmd_status(args) -> int:
    catalog = discover_modules()
    state = load_state()
    done = state["modules"]
    sigs = _catalog_signatures(catalog)
    pend = pending_modules(state, catalog, sigs)
    by_kind: dict[str, list[int]] = {}
    for rec in done.values():
        by_kind.setdefault(rec["kind"], []).append(1)
    multi = len([rec for rec in done.values() if rec.get("structure", {}).get("file_count", 1) > 1])
    big = len([rec for rec in done.values() if rec.get("structure", {}).get("large_files")])
    print(f"项目: {state['meta'].get('project')}  轮次: {state['meta'].get('rounds',0)}  "
          f"更新: {state['meta'].get('updated_at')}")
    print(f"发现模块: {len(catalog)}  已提取: {len([m for m in done if done[m]['status']=='completed'])}  "
          f"待提取/变更: {len(pend)}")
    print(f"含巨型文件(≥{_LARGE_FILE_LOC}行)的模块: {big}  多文件模块: {multi}")
    print("按类型分布（已提取）：")
    for k, v in sorted(by_kind.items()):
        print(f"  {k}: {len(v)}")
    if pend and args.verbose:
        print("待提取清单：")
        for m in pend:
            print(f"  - {m}")
    return 0


def cmd_show(args) -> int:
    state = load_state()
    rec = state["modules"].get(args.module_id)
    if not rec:
        print(f"未找到模块或该模块尚未提取: {args.module_id}")
        return 2
    print(json.dumps(rec, ensure_ascii=False, indent=2))
    return 0


def cmd_annotate(args) -> int:
    state = load_state()
    rec = state["modules"].get(args.module_id)
    if not rec:
        print(f"未找到模块: {args.module_id}（请先 run 提取该模块）")
        return 2
    if args.summary:
        rec["summary"] = args.summary
    save_state(state)
    print(f"已更新 {args.module_id} 的语义摘要。")
    return 0


# ------------------------------------------------------------------ CLI
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="增量式核心功能模块提取工具")
    sub = p.add_subparsers(dest="cmd", required=True)

    pp = sub.add_parser("plan", help="预览本轮将提取哪些模块（不写盘）")
    pp.add_argument("--limit", type=int, default=0, help="仅预览前 N 个")
    pp.set_defaults(func=cmd_plan)

    pr = sub.add_parser("run", help="执行一轮增量提取并落盘")
    pr.add_argument("--limit", type=int, default=10, help="本轮最多提取 N 个待处理模块")
    pr.add_argument("--all", action="store_true", help="提取全部待处理模块")
    pr.add_argument("--force", action="store_true",
                    help="强制重提取全部已发现模块（忽略跳过），用于补齐新增的多文件/大文件字段")
    pr.set_defaults(func=cmd_run)

    ps = sub.add_parser("status", help="查看提取进度总览")
    ps.add_argument("-v", "--verbose", action="store_true")
    ps.set_defaults(func=cmd_status)

    psh = sub.add_parser("show", help="查看单个模块详情")
    psh.add_argument("module_id")
    psh.set_defaults(func=cmd_show)

    pa = sub.add_parser("annotate", help="为已提取模块补写语义摘要")
    pa.add_argument("module_id")
    pa.add_argument("--summary", required=True)
    pa.set_defaults(func=cmd_annotate)
    return p


def main(argv=None) -> int:
    if sys.platform.startswith("win"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
