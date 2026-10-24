# -*- coding: utf-8 -*-
"""Word 2010+ 专有扩展清除器（OOXML Transitional 纯净版导出 · R129）。

【背景（实测取证，python-docx 1.2.0 默认模板）】
python-docx 由默认模板构建的 ``word/document.xml`` 根元素声明了
wpc / wps / wpg / wpi / wne / w14 / wp14 等 Word 2010+ 专有命名空间，
且带 ``mc:Ignorable="w14 wp14"``：

    <w:document xmlns:wpc=".../wordprocessingCanvas"
                xmlns:wps=".../wordprocessingShape"
                xmlns:wpg=".../wordprocessingGroup"
                xmlns:w14=".../word/2010/wordml"
                xmlns:wp14=".../word/2010/wordprocessingDrawing"
                mc:Ignorable="w14 wp14">

这些命名空间是 Word 2010 引入的扩展载体（w14:paraId/textId 段落标识、
w14:textFill 文本效果、wps/wpg/wpc 增强图形容器），**不是** OOXML
Transitional 的必选部分；WPS / LibreOffice 对它们的支持度参差，
是跨软件打开异常的已知来源（R127 真机取证：WPS 对 OMML 等非标准
内容远比 Word 严格）。历史轮次只清了样式里的 ``w14:textFill``
（R117 FIX-6），**根元素命名空间声明从未清理**。

【本模块职责】
1. :func:`sanitize_docx_file` —— 对落盘后的 DOCX 逐部件清理：
   a) 根元素上删除「未使用」的 2010+ 命名空间声明（w14/w15/wp14/w16*/
      wpg/wps/wpc/wpi），仅当前缀在部件 XML 其余部分无任何引用
      （元素标签或属性名），**绝不删除仍被引用的前缀**（防 XML 损坏）；
   b) ``mc:Ignorable`` 剔除被删前缀；列表为空则整属性删除；
      ``mc`` 前缀在部件内无引用时连 ``xmlns:mc`` 一并删除；
   c) 防御性删除 ``w14:paraId/textId/anchorId/editId`` 等残存属性
      （模板残存，删除安全：均为可选注释性属性）；
2. :func:`check_docx_file` —— 只读校验：报告仍含 2010+ 命名空间声明
   / mc:Ignorable 残留 / w14 属性引用的部件（导出后校验用，不改文件）。

【兼容性契约】
- 只动「声明与属性」，不动任何内容元素/文本；清理前后 python-docx
  必须都能重开（护栏测试锁定）。
- 全程 fail-soft 由调用方负责：本函数抛异常时调用方记 WARNING 继续
  交付（宁可多一个声明，不可丢一份成稿）。
"""
from __future__ import annotations

import os
import re
import tempfile
import time
import zipfile
from typing import Any

#: Word 2010+ 专有命名空间前缀（含 2016 系）——目标：产物根元素一律不再声明。
#: 刻意不含 w10/v/o/mo/mv（Office 2003~2008 时代遗留 VML/Mac 命名空间，
#: 非「Word 2010+ 扩展」范畴；且 python-docx 模板部件内可能实际引用 v:）。
_BANNED_PREFIXES: tuple[str, ...] = (
    "w14", "wp14", "w15",
    "w16cid", "w16se", "w16sdtdh", "w16du", "w16sym",
    "wpg", "wps", "wpc", "wpi",
)

#: w14 残存属性（模板/上游写入的可选注释性属性，删除不影响内容与版式）。
_BANNED_ATTR_RE = re.compile(
    r"\s+(?:w14|wp14|w15):(paraId|textId|anchorId|editId|attributeTrackingId)"
    r'="[^"]*"'
)

#: settings.xml 中的 w14 可选设置元素（python-docx 默认模板自带）：
#: ``<w14:docId w14:val="…"/>``（文档 GUID，Word 会自行再生成）与
#: ``<w14:defaultImageDpi w14:val="…"/>``（图片缺 DPI 时的假设值；本仓
#: 导入图片一律显式 EMU 定尺寸，不受影响）。两者均为 Word 2010+ 可选
#: 注释性设置，删除安全；删除后 w14 前缀在部件内即无引用，声明随之清除。
_SETTINGS_W14_EL_RE = re.compile(
    r"<w14:(?:docId|defaultImageDpi)\b(?:[^>\"]|\"[^\"]*\")*/>")

#: 待清理的 XML 主部件（word/ 下正文、样式、设置、编号、页眉页脚等）。
#: 刻意排除 theme*.xml（其内部的 a14/adraw 扩展属于 DrawingML 主题定义，
#: 独立部件、被 styles.xml 关系引用，动它收益为零、风险为正）。
_PART_RE = re.compile(
    r"^word/(?:document|styles|settings|numbering|fontTable"
    r"|header\d*|footer\d*|footnotes|endnotes)\.xml$"
)

#: 根元素开标签（容忍 XML 声明前缀）；非贪婪到第一个 ``>``。
_OPEN_TAG_RE = re.compile(r"<([A-Za-z0-9]+:[A-Za-z0-9]+)((?:[^>\"]|\"[^\"]*\")*)>")


def _split_root_attrs(attr_text: str) -> list[tuple[str, str]]:
    """把根元素属性串拆成 (整段文本, 前缀) 对；仅保留 xmlns:* 属性。"""
    out: list[tuple[str, str]] = []
    for m in re.finditer(r'xmlns:([A-Za-z0-9]+)="[^"]*"', attr_text):
        out.append((m.group(0), m.group(1)))
    return out


def _sanitize_part_xml(xml: str, *, name_is_settings: bool = False
                       ) -> tuple[str, list[str], int]:
    """清理单个 XML 部件文本，返回 (新文本, 被删前缀, 被删属性数)。"""
    m = _OPEN_TAG_RE.search(xml)
    if not m:
        return xml, [], 0
    tag_start, tag_end = m.span()
    open_tag = m.group(0)
    rest = xml[tag_end:]

    # 1) 防御性删除 2010+ 残存属性（先删，再统计前缀使用，避免自我干扰）
    new_rest, n_attrs = _BANNED_ATTR_RE.subn("", rest)
    # 1b) settings.xml 定点清除 w14 可选设置元素（删除后前缀即无引用）
    if name_is_settings:
        new_rest, n_el = _SETTINGS_W14_EL_RE.subn("", new_rest)
        n_attrs += n_el
    xml_body_changed = n_attrs > 0
    open_tag_changed = False

    attrs = _split_root_attrs(m.group(2))
    removed_prefixes: list[str] = []
    new_open_tag = open_tag
    for attr_text, prefix in attrs:
        if prefix not in _BANNED_PREFIXES:
            continue
        # 前缀仍被部件其余部分引用（元素标签或属性名）→ 保留声明，绝不删
        if re.search(rf"<{prefix}:|[\s\"'(]{prefix}:[A-Za-z]", new_rest):
            continue
        new_open_tag = new_open_tag.replace(attr_text, "")
        removed_prefixes.append(prefix)
        open_tag_changed = True

    # 2) mc:Ignorable 剔除被删前缀；空则整属性删除
    ign = re.search(r'\smc:Ignorable="([^"]*)"', new_open_tag)
    if ign and open_tag_changed:
        kept = [p for p in ign.group(1).split() if p not in removed_prefixes]
        if kept:
            new_open_tag = new_open_tag.replace(
                ign.group(0), f' mc:Ignorable="{" ".join(kept)}"')
        else:
            new_open_tag = new_open_tag.replace(ign.group(0), "")
        open_tag_changed = True
    # 3) mc 前缀无引用（无 Ignorable、无 mc: 元素）→ 连声明一并删除
    if re.search(r'\smc:Ignorable="[^"]*"', new_open_tag) is None and \
            "mc:" not in new_rest and 'xmlns:mc=' in new_open_tag:
        new_open_tag = re.sub(r'\sxmlns:mc="[^"]*"', "", new_open_tag, count=1)
        open_tag_changed = True

    if not (open_tag_changed or xml_body_changed):
        return xml, [], 0
    new_xml = xml[:tag_start] + new_open_tag + new_rest
    return new_xml, removed_prefixes, n_attrs


def sanitize_docx_file(path: str | os.PathLike) -> dict[str, Any]:
    """对落盘 DOCX 执行 Word 2010+ 扩展清理（zip 级重写，原子替换）。

    返回 ``{"parts_changed": {部件名: [被删前缀]}, "attrs_stripped": int}``；
    无可清理内容时 ``parts_changed`` 为空 dict、文件保持原字节（不重写）。
    """
    parts_changed: dict[str, list[str]] = {}
    attrs_stripped = 0
    entries: list[tuple[str, bytes]] = []
    dirty = False
    with zipfile.ZipFile(path) as zin:
        names = zin.namelist()
        for name in names:
            data = zin.read(name)
            if _PART_RE.match(name):
                xml = data.decode("utf-8")
                new_xml, removed, n_attrs = _sanitize_part_xml(
                    xml, name_is_settings=(name == "word/settings.xml"))
                if removed or n_attrs:
                    parts_changed[name] = removed
                    attrs_stripped += n_attrs
                    data = new_xml.encode("utf-8")
                    dirty = True
            entries.append((name, data))

    if not dirty:
        return {"parts_changed": {}, "attrs_stripped": 0}

    # 原子替换：写临时文件 → os.replace（与导出主链路同口径）。
    # ✅ Windows 竞争窗口：同目标的并发读 / AV 扫描 newly-created 临时文件
    #    会使 os.replace 抛 PermissionError —— 与 export 主链同款 3 次有界重试。
    dir_name = os.path.dirname(os.fspath(path))
    fd, tmp = tempfile.mkstemp(suffix=".docx", dir=dir_name)
    try:
        with os.fdopen(fd, "wb") as ftmp:
            with zipfile.ZipFile(ftmp, "w", zipfile.ZIP_DEFLATED) as zout:
                for name, data in entries:
                    zout.writestr(name, data)
        last_exc: Exception | None = None
        for _attempt in range(3):
            try:
                os.replace(tmp, path)
                return {"parts_changed": parts_changed,
                        "attrs_stripped": attrs_stripped}
            except PermissionError as exc:
                last_exc = exc
                time.sleep(0.3 * (_attempt + 1))
        raise last_exc  # type: ignore[misc]
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def check_docx_file(path: str | os.PathLike) -> dict[str, Any]:
    """只读校验：报告各部件是否仍含 Word 2010+ 扩展痕迹（不改文件）。

    返回 ``{"clean": bool, "issues": {部件: [问题描述]}}``；
    ``clean=True`` 表示所有受查部件均无 2010+ 命名空间声明、
    mc:Ignorable 与 w14 系属性残留。
    """
    issues: dict[str, list[str]] = {}
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            if not _PART_RE.match(name):
                continue
            xml = z.read(name).decode("utf-8", errors="replace")
            m = _OPEN_TAG_RE.search(xml)
            found: list[str] = []
            if m:
                for attr_text, prefix in _split_root_attrs(m.group(2)):
                    if prefix in _BANNED_PREFIXES:
                        found.append(f"根元素声明 xmlns:{prefix}")
                ign = re.search(r'\smc:Ignorable="([^"]*)"', m.group(0))
                if ign:
                    banned = [p for p in ign.group(1).split()
                              if p in _BANNED_PREFIXES]
                    if banned:
                        found.append(f'mc:Ignorable 含 {",".join(banned)}')
            for pm in re.finditer(r"<(w14|wp14|w15|wpg|wps|wpc|wpi):", xml):
                found.append(f"存在专有元素 <{pm.group(1)}:...>")
                break
            if _BANNED_ATTR_RE.search(xml):
                found.append("存在 w14/wp14/w15 注释性属性（paraId 等）")
            if found:
                issues[name] = found
    return {"clean": not issues, "issues": issues}
