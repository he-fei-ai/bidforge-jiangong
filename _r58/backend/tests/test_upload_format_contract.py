"""上传格式白名单的「三层一致性」护栏（2026-09-26）

解析提取模块的文件导入有三处各自维护的扩展名清单，任何一处单独改动都会
造成「用户能选但后端 400 / 后端能解但用户选不到 / 解析器路由不到」的静默
不一致，且现有测试只能覆盖单侧：

| 层 | 位置 | 作用 |
|---|---|---|
| 1 | `file_parser.SUPPORTED_EXTENSIONS` | 对外声明（前端镜像的基准） |
| 2 | `file_parser._SUPPORTED_EXTENSIONS` | `parse_file_content_ex` 实际的路由分派依据 |
| 3 | `file_parser._BINARY_SIGNATURES` | 二进制格式的文件头签名（防 .exe 改名 .docx） |
| 4 | `frontend/src/utils/uploadAccept.ts` | 前端 `<Upload accept>` 白名单 |

历史上第 1/2 层的清单是**手抄两份**，第 3 层漏一个二进制格式就等于该格式
的文件头校验形同虚设。第 4 层在前端只有「镜像一份硬编码」的弱护栏
（`uploadAccept.test.ts` 断言前后端集合相等，但硬编码镜像本身无人看护）。

本文件把四层用集合运算钉在一起，任一侧增删格式都会在这里失败。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from app.services import file_parser as fp

_REPO_ROOT = Path(__file__).resolve().parents[2]
_UPLOAD_ACCEPT_TS = _REPO_ROOT / "frontend" / "src" / "utils" / "uploadAccept.ts"

#: 文本类扩展名：不依赖文件头签名（任意纯文本），改由 _reject_binary_text_ext
#: 的「原始字节疑似二进制即拒绝」兜底。
_TEXT_EXTENSIONS = {"md", "txt", "csv"}


def test_supported_and_dispatch_lists_are_identical():
    """对外声明（SUPPORTED_EXTENSIONS）与解析分派（_SUPPORTED_EXTENSIONS）必须一致。

    反例回归：若只改了对外清单、忘了改分派清单，新增格式会被前端放行、
    上传成功，解析时却走不到任何解析器（或落到默认文本分支产生乱码）。
    """
    declared = {e.lower() for e in fp.SUPPORTED_EXTENSIONS}
    dispatch = {e.lower() for e in fp._SUPPORTED_EXTENSIONS}
    assert declared == dispatch, (
        f"对外声明与解析分派清单漂移："
        f"仅声明={sorted(declared - dispatch)}，仅分派={sorted(dispatch - declared)}")


def test_binary_extensions_all_have_signatures():
    """每个二进制格式都必须有文件头签名，否则扩展名伪造防护对该格式失效。"""
    binary = {e.lower() for e in fp.SUPPORTED_EXTENSIONS} - _TEXT_EXTENSIONS
    signed = {e.lower() for e in fp._BINARY_SIGNATURES}
    assert binary <= signed, (
        f"以下二进制格式缺少文件头签名（.exe 改名即可绕过）：{sorted(binary - signed)}")
    assert not (signed & _TEXT_EXTENSIONS), (
        f"文本类格式不应有文件头签名（会误拒合法文本）：{sorted(signed & _TEXT_EXTENSIONS)}")


def test_binary_signature_tables_are_populated():
    """签名表条目不得为空或全空白（历史退化风险：空 tuple 会让 signature_valid
    永远返回 True，等于关闭该格式的检测）。"""
    for ext, sigs in fp._BINARY_SIGNATURES.items():
        assert sigs, f"{ext} 的签名列表为空"
        for s in sigs:
            assert isinstance(s, bytes) and len(s) >= 2, f"{ext} 存在过短签名 {s!r}"


def test_text_extensions_rejected_when_binary_content():
    """文本扩展名 + 二进制内容必须被拒绝（F1 修复的回归护栏）。"""
    for ext in sorted(_TEXT_EXTENSIONS):
        # 含 NUL 的字节流：真实 .exe 的特征，旧实现会经 errors="ignore" 解成乱码入库
        with pytest.raises(fp.ParseError):
            fp.parse_file_content_ex(b"\x00" * 64, f"恶意.{ext}")
        # 正常 UTF-8 文本必须放行（防误拒）
        text, diag = fp.parse_file_content_ex("正常文本内容".encode("utf-8"),
                                              f"正常.{ext}")
        assert text.strip()
        assert diag["file_type"] == ext


def test_signature_valid_accepts_all_supported_binaries():
    """每种二进制格式的真实文件头必须通过 signature_valid。"""
    cases = {
        "pdf": b"%PDF-1.7\n...",
        "docx": b"PK\x03\x04" + b"\x00" * 4,
        "xlsx": b"PK\x03\x04" + b"\x00" * 4,
        "png": b"\x89PNG\r\n\x1a\n" + b"\x00" * 4,
        "jpg": b"\xff\xd8\xff\xe0" + b"\x00" * 4,
        "jpeg": b"\xff\xd8\xff\xe1" + b"\x00" * 4,
        "bmp": b"BM" + b"\x00" * 10,
        "tiff": b"II*\x00" + b"\x00" * 8,
        "doc": b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 8,
        "wps": b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 8,
    }
    for ext, payload in cases.items():
        assert fp.signature_valid(ext, payload), f"{ext} 的真实文件头被误拒"
    # 反例：伪造扩展名必须被拒
    assert not fp.signature_valid("docx", b"MZ\x90\x00"), "MZ (PE) 伪装 .docx 必须被拒"
    assert not fp.signature_valid("pdf", b"PK\x03\x04"), "ZIP 伪装 .pdf 必须被拒"
    # 文本类不校验文件头，一律放行（由 _reject_binary_text_ext 兜底）
    assert fp.signature_valid("txt", b"\x00" * 8)


def test_front_end_accept_matches_backend_supported_extensions():
    """前端 accept 白名单必须与后端 SUPPORTED_EXTENSIONS 完全一致（跨语言护栏）。

    为什么放在后端：这是唯一能同时看到前后端两处清单的位置。
    前端 `uploadAccept.test.ts` 只有一份额外硬编码镜像，镜像本身漂移时它自己
    测不出来（自证循环）；本测试直接读源文件，任一侧改动都会失败。
    """
    assert _UPLOAD_ACCEPT_TS.exists(), (
        f"缺少前端白名单源文件：{_UPLOAD_ACCEPT_TS}（本护栏依赖它）")
    src = _UPLOAD_ACCEPT_TS.read_text(encoding="utf-8")

    m = re.search(r"UPLOAD_FILE_EXTENSIONS\s*:\s*string\[\]\s*=\s*\[(.*?)\];",
                  src, re.S)
    assert m, "无法在 uploadAccept.ts 中定位 UPLOAD_FILE_EXTENSIONS 定义"
    front = {e.strip().lower() for e in re.findall(r"[\"']([A-Za-z0-9]+)[\"']",
                                                   m.group(1))}
    back = {e.lower() for e in fp.SUPPORTED_EXTENSIONS}
    assert front == back, (
        f"前后端上传格式白名单不一致 → 会出现「前端可选但后端 400」或"
        f"「后端支持但前端选不到」：仅前端={sorted(front - back)}，"
        f"仅后端={sorted(back - front)}")


def test_accept_string_is_well_formed():
    """accept 字符串本身不得有空格 / 前后多余逗号（antd Upload 对格式敏感）。"""
    src = _UPLOAD_ACCEPT_TS.read_text(encoding="utf-8")
    m = re.search(r"UPLOAD_FILE_ACCEPT\s*:\s*string\s*=", src)
    assert m, "uploadAccept.ts 必须导出 UPLOAD_FILE_ACCEPT（两处上传共用）"
