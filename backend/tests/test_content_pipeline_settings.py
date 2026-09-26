"""✅ 正文生成链路参数配置化（B2，2026-09-20）守卫测试。

旧实现 CONTENT_REQUEST_TIMEOUT 等 6 项硬编码在 sse_handlers.py，无法按部署
环境（弱模型 / 慢链路 / 限流敏感）调优。现全部进 config.Settings（可环境变量
覆盖），sse_handlers 模块常量绑定为 settings 取值 —— 常量名不变，既有测试不破。
"""

import os


def test_settings_content_pipeline_defaults():
    from app.config import Settings
    s = Settings()
    assert s.content_request_timeout == 300
    assert s.content_total_timeout == 660
    assert s.content_section_retries == 1
    assert s.content_retry_backoff == 6.0
    assert s.content_rate_limit_backoff == 20.0
    assert s.content_continue_max_rounds == 2
    # ✅ 2026-09-22 引入：正文生成采样温度，默认 None（沿用 provider 默认，保持旧行为）
    assert s.content_temperature is None


def test_settings_content_pipeline_env_override():
    from app.config import Settings
    env = {
        "CONTENT_REQUEST_TIMEOUT": "120",
        "CONTENT_TOTAL_TIMEOUT": "360",
        "CONTENT_SECTION_RETRIES": "0",
        "CONTENT_RETRY_BACKOFF": "3.0",
        "CONTENT_RATE_LIMIT_BACKOFF": "10.0",
        "CONTENT_CONTINUE_MAX_ROUNDS": "1",
        "CONTENT_TEMPERATURE": "0.3",
    }
    old = {k: os.environ.get(k) for k in env}
    try:
        os.environ.update(env)
        s = Settings()
        assert s.content_request_timeout == 120
        assert s.content_total_timeout == 360
        assert s.content_section_retries == 0
        assert s.content_retry_backoff == 3.0
        assert s.content_rate_limit_backoff == 10.0
        assert s.content_continue_max_rounds == 1
        assert s.content_temperature == 0.3
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_settings_content_temperature_none_coercion():
    """环境变量常见写法（空串 / none / null）必须安全解析为 None，而非 float 解析报错。"""
    from app.config import Settings
    for raw in ("none", "None", "NULL", ""):
        old = os.environ.get("CONTENT_TEMPERATURE")
        try:
            os.environ["CONTENT_TEMPERATURE"] = raw
            s = Settings()
            assert s.content_temperature is None, f"期望 None，实际 {s.content_temperature!r}"
        finally:
            if old is None:
                os.environ.pop("CONTENT_TEMPERATURE", None)
            else:
                os.environ["CONTENT_TEMPERATURE"] = old


def test_sse_handlers_constants_wired_from_settings():
    """模块常量必须来自 Settings（防止回退为硬编码字面量）。"""
    from app.config import Settings
    from app.routers import sse_handlers as sh
    s = Settings()
    assert sh.CONTENT_REQUEST_TIMEOUT == int(s.content_request_timeout)
    assert sh.CONTENT_TOTAL_TIMEOUT == int(s.content_total_timeout)
    assert sh.CONTENT_SECTION_RETRIES == max(0, int(s.content_section_retries))
    assert sh.CONTENT_RETRY_BACKOFF == float(s.content_retry_backoff)
    assert sh.CONTENT_RATE_LIMIT_BACKOFF == float(s.content_rate_limit_backoff)
    assert sh.CONTENT_CONTINUE_MAX_ROUNDS == max(0, int(s.content_continue_max_rounds))
    # ✅ 2026-09-22 引入：正文生成温度常量必须绑定自 Settings
    assert sh.CONTENT_TEMPERATURE == s.content_temperature


def test_sse_handlers_timeout_invariants():
    """既有不变量（test_content_deep_audit E-3/E-4 同源）：总超时 > 单 provider 超时，
    续写与首稿共用总超时。"""
    from app.routers import sse_handlers as sh
    assert sh.CONTENT_TOTAL_TIMEOUT > sh.CONTENT_REQUEST_TIMEOUT
    src = open(sh.__file__, encoding="utf-8").read()
    assert "CONTENT_REQUEST_TIMEOUT + 60" not in src
    assert src.count("timeout=CONTENT_TOTAL_TIMEOUT") >= 2
