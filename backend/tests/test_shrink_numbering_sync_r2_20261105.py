"""字数压缩写正文前必须补编号规范化（2026-11-05 · 第二轮编号漂移复查）。

背景：``sections.shrink_section`` 是**整章 AI 改写**，模型会重排/重编号子标题；
它此前直接 UPDATE 落库，是全仓唯一两条「整章改写但不归一化」的写路径之一
（另一条是 review_autofix，归属审核线）。结果是一次错误编号永久留库，
之后的预检/复核必然判「与目录不一致」。
"""

import inspect

import app.routers.sections as sections


def _src(fn) -> str:
    return inspect.getsource(fn)


def test_shrink_normalizes_before_update():
    src = _src(sections.shrink_section)
    norm_pos = src.find("normalize_section_content_subheadings(")
    update_pos = src.find('"UPDATE sections SET content=?')
    assert norm_pos != -1, "shrink_section 未补编号规范化"
    assert update_pos != -1
    assert norm_pos < update_pos, "规范化必须在 UPDATE 之前执行，否则落库仍是错编号"


def test_shrink_normalization_is_fail_soft():
    src = _src(sections.shrink_section)
    # 规范化块必须自带 try/except，不得让编号问题吞掉用户的压缩结果
    window = src[src.find("# ✅ 2026-11-05（R2 · 编号漂移复查）") : src.find('"UPDATE sections SET content=?')]
    assert "except Exception" in window
    assert "保留压缩正文" in window
