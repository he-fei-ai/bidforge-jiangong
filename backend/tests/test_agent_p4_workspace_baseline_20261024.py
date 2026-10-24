"""P4 任务工作区受保护文件基线用例（可直接放入 backend/tests/ 运行）。

覆盖：protect/set_group/release/files/restore/save_record/load_record 全部方法、
修改/删除/仅改 mtime 的还原判定、未保护文件不受影响、越界拒绝、
manifest 落盘后重开继承登记、save() 清理未被引用的原件副本。

约定：文件读写一律走字节接口，避免 Windows 文本模式换行翻译干扰哈希比对。
"""
import hashlib
import json
import os
from pathlib import Path

import pytest
from app.services.agent.workspace_baseline import create_workspace_baseline


def make_dirs(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    return str(ws), str(baseline)


def write(ws, rel, data: bytes) -> Path:
    path = Path(ws) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def manifest_groups(baseline) -> dict:
    text = (Path(baseline) / "manifest.json").read_text(encoding="utf-8")
    return json.loads(text)["groups"]


def copies(baseline) -> set[str]:
    return set(os.listdir(Path(baseline) / "files"))


# ── restore 的三种判定分支 ────────────────────────────────────────────────

def test_protect_后修改文件_restore_还原并返回相对路径(tmp_path):
    ws, baseline = make_dirs(tmp_path)
    write(ws, "a.txt", b"original")
    base = create_workspace_baseline(ws, baseline)
    base.protect("g", ["a.txt"])
    assert base.files("g") == ["a.txt"]
    write(ws, "a.txt", b"tampered-by-agent")
    assert base.restore() == ["a.txt"]
    assert (Path(ws) / "a.txt").read_bytes() == b"original"
    assert base.restore() == []  # 已还原，再次校验不再变更


def test_restore_内容未变只改_mtime_不还原只刷新登记(tmp_path):
    ws, baseline = make_dirs(tmp_path)
    path = write(ws, "a.txt", b"same-content")
    base = create_workspace_baseline(ws, baseline)
    base.protect("g", ["a.txt"])
    before = manifest_groups(baseline)["g"]["a.txt"]
    touched = path.stat().st_mtime_ns + 2_000_000_000
    os.utime(path, ns=(touched, touched))
    assert base.restore() == []
    assert path.read_bytes() == b"same-content"
    refreshed = manifest_groups(baseline)["g"]["a.txt"]
    assert refreshed["mtime"] == touched  # mtime 已刷新为文件当前值
    assert refreshed["hash"] == before["hash"]
    assert refreshed["size"] == before["size"]
    assert base.restore() == []  # 刷新后快路径重新成立


def test_restore_大小与修改时间都未变_视为未改动跳过(tmp_path):
    ws, baseline = make_dirs(tmp_path)
    path = write(ws, "a.txt", b"abcdefgh")
    base = create_workspace_baseline(ws, baseline)
    base.protect("g", ["a.txt"])
    original_mtime = path.stat().st_mtime_ns
    write(ws, "a.txt", b"XYZWXYZW")  # 同长度不同内容
    os.utime(path, ns=(original_mtime, original_mtime))
    assert base.restore() == []
    assert path.read_bytes() == b"XYZWXYZW"  # 快路径直接跳过（对齐 JS 的 size+mtimeMs 判定）


def test_restore_删除被保护文件_恢复(tmp_path):
    ws, baseline = make_dirs(tmp_path)
    path = write(ws, "a.txt", b"original")
    base = create_workspace_baseline(ws, baseline)
    base.protect("g", ["a.txt"])
    path.unlink()
    assert base.restore() == ["a.txt"]
    assert path.read_bytes() == b"original"


def test_未保护的文件改动不受影响(tmp_path):
    ws, baseline = make_dirs(tmp_path)
    write(ws, "a.txt", b"a-original")
    write(ws, "b.txt", b"b-original")
    base = create_workspace_baseline(ws, baseline)
    base.protect("g", ["a.txt"])
    write(ws, "a.txt", b"a-tampered")
    write(ws, "b.txt", b"b-tampered")
    assert base.restore() == ["a.txt"]
    assert (Path(ws) / "a.txt").read_bytes() == b"a-original"
    assert (Path(ws) / "b.txt").read_bytes() == b"b-tampered"  # 未保护，保持改动


def test_restore_跨分组重复登记只返回一次(tmp_path):
    ws, baseline = make_dirs(tmp_path)
    write(ws, "a.txt", b"original")
    base = create_workspace_baseline(ws, baseline)
    base.protect("g1", ["a.txt"])
    base.protect("g2", ["a.txt"])
    write(ws, "a.txt", b"tampered")
    assert base.restore() == ["a.txt"]
    assert (Path(ws) / "a.txt").read_bytes() == b"original"


# ── 分组：release / set_group ─────────────────────────────────────────────

def test_release_后改动不再还原(tmp_path):
    ws, baseline = make_dirs(tmp_path)
    write(ws, "a.txt", b"original")
    base = create_workspace_baseline(ws, baseline)
    base.protect("g", ["a.txt"])
    base.release("g", ["a.txt"])
    assert base.files("g") == []
    write(ws, "a.txt", b"agent-edit")
    assert base.restore() == []
    assert (Path(ws) / "a.txt").read_bytes() == b"agent-edit"


def test_set_group_整组替换不影响其他组(tmp_path):
    ws, baseline = make_dirs(tmp_path)
    write(ws, "a.txt", b"a-original")
    write(ws, "b.txt", b"b-original")
    write(ws, "c.txt", b"c-original")
    base = create_workspace_baseline(ws, baseline)
    base.protect("A", ["a.txt"])
    base.protect("B", ["b.txt"])
    base.set_group("A", ["c.txt"])
    assert base.files("A") == ["c.txt"]
    assert base.files("B") == ["b.txt"]
    write(ws, "a.txt", b"a-edit")  # 已被整组替换掉，不再还原
    write(ws, "b.txt", b"b-edit")  # 仍在 B 组，必须还原
    assert base.restore() == ["b.txt"]
    assert (Path(ws) / "a.txt").read_bytes() == b"a-edit"
    assert (Path(ws) / "b.txt").read_bytes() == b"b-original"


# ── 越界拒绝 ──────────────────────────────────────────────────────────────

def test_越界路径抛错且不留下登记(tmp_path):
    ws, baseline = make_dirs(tmp_path)
    base = create_workspace_baseline(ws, baseline)
    bad_paths = [
        "../x",
        str(Path(ws).parent / "outside.txt"),
        os.path.join(os.path.dirname(ws), "y.txt"),
    ]
    for bad in bad_paths:
        with pytest.raises(ValueError) as excinfo:
            base.protect("g", [bad])
        assert str(excinfo.value) == f"受保护文件必须位于工作区内：{bad}"
    with pytest.raises(ValueError) as excinfo:
        base.set_group("g", ["../x"])
    assert str(excinfo.value) == "受保护文件必须位于工作区内：../x"
    with pytest.raises(ValueError) as excinfo:
        base.release("g", ["../x"])
    assert str(excinfo.value) == "受保护文件必须位于工作区内：../x"
    assert base.files("g") == []


# ── 持久化与副本清理 ──────────────────────────────────────────────────────

def test_manifest_落盘后重新创建_继承登记并可用原件还原(tmp_path):
    ws, baseline = make_dirs(tmp_path)
    write(ws, "sub/nested.txt", b"original")
    base = create_workspace_baseline(ws, baseline)
    base.protect("g", ["sub/nested.txt"])
    assert (Path(baseline) / "manifest.json").is_file()
    reopened = create_workspace_baseline(ws, baseline)
    assert reopened.files("g") == ["sub/nested.txt"]  # 相对键为 POSIX 分隔符
    write(ws, "sub/nested.txt", b"tampered")
    assert reopened.restore() == ["sub/nested.txt"]
    assert (Path(ws) / "sub/nested.txt").read_bytes() == b"original"


def test_save_清理不再被引用的原件副本(tmp_path):
    ws, baseline = make_dirs(tmp_path)
    write(ws, "a.txt", b"content-a")
    write(ws, "b.txt", b"content-b")
    base = create_workspace_baseline(ws, baseline)
    base.protect("g1", ["a.txt"])
    base.protect("g2", ["b.txt"])
    hash_a = hashlib.sha256(b"content-a").hexdigest()
    hash_b = hashlib.sha256(b"content-b").hexdigest()
    assert copies(baseline) == {hash_a, hash_b}
    base.release("g1", ["a.txt"])
    assert copies(baseline) == {hash_b}  # a 的原件副本被清理
    base.set_group("g2", [])
    assert copies(baseline) == set()


def test_protect_不登记不存在与非文件路径(tmp_path):
    ws, baseline = make_dirs(tmp_path)
    (Path(ws) / "adir").mkdir()
    base = create_workspace_baseline(ws, baseline)
    base.protect("g", ["ghost.txt", "adir"])
    assert base.files("g") == []


# ── 业务记录 ──────────────────────────────────────────────────────────────

def test_save_record_load_record_与缺省返回_None(tmp_path):
    ws, baseline = make_dirs(tmp_path)
    base = create_workspace_baseline(ws, baseline)
    assert base.load_record("missing") is None
    base.save_record("check", {"kind": "image", "ok": True, "中文": 1})
    assert base.load_record("check") == {"kind": "image", "ok": True, "中文": 1}
    bad = Path(baseline) / "records" / "bad.json"
    bad.write_bytes(b"{not-json")
    assert base.load_record("bad") is None  # 损坏内容同样 fail-soft


# ── 包导出 ────────────────────────────────────────────────────────────────

def test_包导出_create_workspace_baseline(tmp_path):
    from app.services.agent import create_workspace_baseline as exported

    assert exported is create_workspace_baseline
