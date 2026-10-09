"""提示词变量提取/渲染：JSON 对象键上下文不应被当作变量（D1 加固回归测试）。

背景：提示词中常含未加引号的 JSON 示例（如 ``{tasks: [...]}``）。旧正则
``\\{([A-Za-z_]\\w{1,})\\}`` 会把 ``tasks`` 误当作变量：既污染变量列表、触发
"缺失变量"误告警，又可能在 render 阶段被静默替换从而篡改提示词中的 JSON。
现正则加入 ``(?!\\s*:)`` 排除 JSON 对象键上下文。
"""
from app.services.ai.prompts._registry import (
    extract_variables,
    has_residual_placeholders,
    render_prompt,
)


def test_extract_ignores_json_object_keys():
    tmpl = '请返回 {"tasks": []} 或 {tasks: []}，使用 {theme} 主题'
    vars_ = extract_variables(tmpl)
    assert "theme" in vars_
    assert "tasks" not in vars_


def test_render_does_not_corrupt_json_keys():
    out = render_prompt("{tasks: []} {title}", title="目录", tasks="SHOULD_NOT_APPLY")
    assert "{tasks: []}" in out          # JSON 键未被替换
    assert "目录" in out                 # 正常变量被替换
    assert "SHOULD_NOT_APPLY" not in out


def test_render_still_replaces_plain_placeholders():
    out = render_prompt("标题：{title}\n数量：{count}", title="A", count=3)
    assert "标题：A" in out
    assert "数量：3" in out


def test_chinese_colon_placeholder_still_works():
    # 中文冒号「：」不受 (?!\s*:) 影响
    out = render_prompt("{title}：正文", title="章节")
    assert out.startswith("章节：")


def test_residual_detection_ignores_json_keys():
    assert has_residual_placeholders("{tasks: []}") is False
    assert has_residual_placeholders("{title}") is True
