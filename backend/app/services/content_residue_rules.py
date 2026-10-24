# -*- coding: utf-8 -*-
"""正文残留判据单一事实源（R129 · 从 export._AUDIT_RULES 上收）。

背景：导出前体检（routers/export.py::_AUDIT_RULES）与导出后校验
（services/export_validate.py）对「HTML 标签残留」「占位符残留」使用**同一判据**。
export_validate 属 services 层，受分层护栏约束（tests/test_outline_name_line_20260927.
py::test_services_never_import_routers）**禁止反向 import routers**——判据若只活在
export.py，导出后校验要么复制副本（漂移风险，R86 L3 同款反模式）、要么层级倒置
（本轮全量回归实拍抓到）。故把两个共用正则上收本模块：routers 侧引用本模块常量
（routers → services 是正确依赖方向），两侧天然同源。

刻意只上收**被两侧共用**的判据；_AUDIT_RULES 其余条目（剪贴板表格/LaTeX 空参/
投标用语/中英混杂）仅导出前体检使用，留在 export.py 不动。
"""
from __future__ import annotations

import re

#: 数据占位符（××/xx/【待补充】）—— 与 export._AUDIT_RULES "placeholder" 同源。
#: 判据语义：xx 仅在「前后都不是字母数字」时命中（防误伤 GB50300、H13x 等）。
PLACEHOLDER_RESIDUE_RE = re.compile(
    r"××+|(?<![A-Za-z0-9])[xX]{2}(?![A-Za-z0-9])|【待补充")

#: HTML 标签残留（应使用 GFM 表格/Markdown）—— 与 _AUDIT_RULES "html_tag" 同源。
HTML_TAG_RESIDUE_RE = re.compile(
    r"</?(?:table|thead|tbody|tr|td|th|div|span|p|br)\b[^>]*>", re.I)
