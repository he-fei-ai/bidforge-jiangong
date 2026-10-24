"""P4 — 任务工作区受保护文件基线（原件副本 + 提交前还原 + 任务记录）。

移植来源（易标 Yibiao 2.30.20 / electron/services/pi/piWorkspaceBaseline.cjs）：

1:1 移植 ``piWorkspaceBaseline.cjs`` 的 ``createWorkspaceBaseline`` 及其内部
``hashOf`` / ``toRelative`` / ``save`` / ``snapshot`` / ``entriesFor``；
方法按 Python 命名改写：``setGroup`` → ``set_group``、``saveRecord`` →
``save_record``、``loadRecord`` → ``load_record``（``protect`` / ``release`` /
``files`` / ``restore`` 名称不变）。

程序写入、Agent 不应改动的文件在工作区外保存原件副本（sha256 内容寻址），
提交校验前 ``restore()`` 把被改/被删的文件还原；分组（group）可追加
（``protect``）/ 整体替换（``set_group``）/ 解除（``release``）；登记随任务
目录持久保存（``manifest.json`` + ``files/`` + ``records/``），暂停恢复后继续生效。

与 JS 源码的行为差异（仅 mtime 精度与形参形态两处，其余逐条对齐）：

- mtime 精度：JS 用 ``fs.statSync().mtimeMs``（浮点毫秒）。Python 若用秒级
  ``st_mtime``，同一秒内改写文件会得到相同值，导致 ``restore()`` 的
  「大小与修改时间都未变 → 跳过」快路径漏判已改动文件（内容变了却直接跳过、
  不予还原），因此本模块改用纳秒 ``st_mtime_ns``：精度不低于 JS 的毫秒。
  方向是安全的——精度过细只会让 mtime 被判为「变了」而多走一次内容哈希比对，
  哈希相同则仅刷新登记的 mtime（与 JS「变了但内容相同」分支一致），绝不误还原；
  不采用 ``st_mtime``（秒级）正是因为它会漏判。若两端共读同一份 manifest，
  JS 写入的毫秒数值与纳秒不相等，同样只会多走一次哈希比对，结果仍正确。
- 形参形态：JS 的 ``createWorkspaceBaseline({workspaceDir, baselineDir})``
  对象解构改为两个位置参数 ``create_workspace_baseline(workspace_dir, baseline_dir)``。

其余语义与源码一致：越界文案逐字保留、原件按 sha256 内容寻址并在 ``save()``
时清理不再被引用的副本、写 ``.tmp`` → ``rename`` 原子落盘 manifest、
``snapshot`` 对不存在/非文件不登记、``restore()`` 返回还原的相对路径列表、
``load_record`` 读不到返回 ``None``（fail-soft）。

仅用标准库（hashlib / json / os / shutil / stat），不依赖本仓其他模块。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
from collections.abc import Iterable
from typing import Any

#: 基线目录布局常量（与 JS 源码顶部同名字面量一致）。
MANIFEST_FILE = "manifest.json"
COPIES_DIR = "files"
RECORDS_DIR = "records"


def _hash_of(buffer: bytes) -> str:
    """sha256 十六进制摘要（移植自 ``hashOf``）。"""
    return hashlib.sha256(buffer).hexdigest()


def _to_relative(workspace_dir: str, file: Any) -> str:
    # 登记键统一为工作区内的 POSIX 相对路径，暂停恢复后仍能对应同一文件。
    workspace = os.path.abspath(workspace_dir)
    try:
        relative = os.path.relpath(os.path.join(workspace, str(file or "")), workspace)
    except ValueError:
        # Windows 下不同盘符的绝对路径：ntpath.relpath 直接抛 ValueError，
        # 对应 JS path.relative 对不同盘返回绝对 to 路径、再被 isAbsolute 判越界。
        raise ValueError(f"受保护文件必须位于工作区内：{file}") from None
    relative = os.path.normpath(relative)
    # JS path.relative 对同一路径返回 ""，Python relpath 返回 "."，二者都表示
    # 「相对路径为空」（即指向工作区本身），与源码同样判定为越界。
    if relative in ("", ".") or relative.startswith("..") or os.path.isabs(relative):
        raise ValueError(f"受保护文件必须位于工作区内：{file}")
    return "/".join(relative.split(os.sep))


class WorkspaceBaseline:
    """受保护文件基线登记表（对应 JS ``createWorkspaceBaseline`` 返回的对象字面量）。"""

    def __init__(self, workspace_dir: str, baseline_dir: str) -> None:
        self._workspace = os.path.abspath(workspace_dir)
        self._baseline = os.path.abspath(baseline_dir)
        self._manifest_path = os.path.join(self._baseline, MANIFEST_FILE)
        self._copies_dir = os.path.join(self._baseline, COPIES_DIR)
        self._groups: dict[str, dict[str, dict[str, Any]]] = self._load_groups()

    # manifest 缺失/损坏一律视为空登记（对应 JS 读取处的裸 try/catch）。
    def _load_groups(self) -> dict[str, dict[str, dict[str, Any]]]:
        try:
            with open(self._manifest_path, encoding="utf-8") as handle:
                data = json.load(handle)
            return data.get("groups") or {}
        except (OSError, ValueError, TypeError, AttributeError):
            return {}

    # 同时清理不再被任何分组引用的原件副本。
    def _save(self) -> None:
        os.makedirs(self._copies_dir, exist_ok=True)
        used = {
            entry["hash"]
            for entries in self._groups.values()
            for entry in entries.values()
        }
        for name in os.listdir(self._copies_dir):
            if name not in used:
                try:
                    os.remove(os.path.join(self._copies_dir, name))
                except FileNotFoundError:
                    pass  # 对应 fs.rmSync(..., {force: true})：忽略已缺失的副本
        tmp_path = f"{self._manifest_path}.tmp"
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump({"groups": self._groups}, handle, ensure_ascii=False, indent=2)
        os.replace(tmp_path, self._manifest_path)

    # 记录当前内容和文件状态；文件不存在时不登记。
    def _snapshot(self, relative: str) -> dict[str, Any] | None:
        absolute = os.path.join(self._workspace, *relative.split("/"))
        if not os.path.isfile(absolute):
            return None
        with open(absolute, "rb") as handle:
            buffer = handle.read()
        content_hash = _hash_of(buffer)
        copy_path = os.path.join(self._copies_dir, content_hash)
        if not os.path.exists(copy_path):
            os.makedirs(self._copies_dir, exist_ok=True)
            with open(copy_path, "wb") as handle:
                handle.write(buffer)
        info = os.stat(absolute)
        return {"hash": content_hash, "size": info.st_size, "mtime": info.st_mtime_ns}

    # 先归一全部登记键再快照：任一文件越界时在写入任何副本之前就抛错，
    # 对齐 JS 先对整个 files 跑 toRelative、再逐个 snapshot 的求值顺序。
    def _entries_for(self, files: Iterable[str]) -> dict[str, dict[str, Any]]:
        relatives = [_to_relative(self._workspace, file) for file in files]
        entries: dict[str, dict[str, Any]] = {}
        for relative in relatives:
            entry = self._snapshot(relative)
            if entry is not None:  # 对应 .filter(([, entry]) => entry)
                entries[relative] = entry
        return entries

    # 追加或刷新登记；程序自己改写受保护文件后调用。
    def protect(self, group: str, files: Iterable[str]) -> None:
        items = list(files)
        if not items:
            return
        merged = dict(self._groups.get(group) or {})
        merged.update(self._entries_for(items))
        self._groups[group] = merged
        self._save()

    # 以当前内容整体替换分组。
    def set_group(self, group: str, files: Iterable[str]) -> None:
        self._groups[group] = self._entries_for(list(files))
        self._save()

    # 解除登记，交给 Agent 修改的文件不再还原。
    def release(self, group: str, files: Iterable[str]) -> None:
        entries = dict(self._groups.get(group) or {})
        for file in files:
            entries.pop(_to_relative(self._workspace, file), None)
        self._groups[group] = entries
        self._save()

    def files(self, group: str) -> list[str]:
        """分组当前登记的相对路径（POSIX 分隔符，按键插入序）。"""
        return list(self._groups.get(group) or {})

    # 大小与修改时间未变的文件视为未改动，其余按内容哈希比对后还原；返回还原的相对路径。
    def restore(self) -> list[str]:
        # 有序 dict 同时满足 JS Set 的「跨分组去重 + 保留首次出现顺序」。
        restored: dict[str, None] = {}
        for entries in self._groups.values():
            for relative, entry in entries.items():
                absolute = os.path.join(self._workspace, *relative.split("/"))
                try:
                    info = os.stat(absolute)
                except OSError:
                    info = None
                is_file = info is not None and stat.S_ISREG(info.st_mode)
                if (
                    is_file
                    and info.st_size == entry["size"]
                    and info.st_mtime_ns == entry["mtime"]
                ):
                    continue
                if is_file:
                    with open(absolute, "rb") as handle:
                        current = handle.read()
                    if _hash_of(current) == entry["hash"]:
                        entry["mtime"] = info.st_mtime_ns  # 变了但哈希相同 → 只刷新 mtime
                        continue
                os.makedirs(os.path.dirname(absolute), exist_ok=True)
                shutil.copyfile(os.path.join(self._copies_dir, entry["hash"]), absolute)
                entry["mtime"] = os.stat(absolute).st_mtime_ns
                restored[relative] = None
        self._save()
        return list(restored)

    # 业务在同一任务目录保存校验依据（如图片块原文），不放入 Agent 工作区。
    def save_record(self, name: str, value: Any) -> None:
        file_path = os.path.join(self._baseline, RECORDS_DIR, f"{name}.json")
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        tmp_path = f"{file_path}.tmp"
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp_path, file_path)

    def load_record(self, name: str) -> Any:
        """读取业务记录；文件缺失或内容损坏时返回 ``None``（fail-soft）。"""
        try:
            file_path = os.path.join(self._baseline, RECORDS_DIR, f"{name}.json")
            with open(file_path, encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return None


def create_workspace_baseline(workspace_dir: str, baseline_dir: str) -> WorkspaceBaseline:
    """创建工作区受保护文件基线（移植自 ``piWorkspaceBaseline.cjs`` 的 ``createWorkspaceBaseline``）。

    ``workspace_dir``：Agent 工作区目录，受保护文件必须位于其内；
    ``baseline_dir``：工作区外的基线目录，``manifest.json`` / ``files/`` /
    ``records/`` 随任务目录持久保存。打开时读取既有 manifest，继承此前登记的分组。
    """
    return WorkspaceBaseline(workspace_dir, baseline_dir)
