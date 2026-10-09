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


# ===========================================================================
# ✅ 修复（2026-09-26 · BUG-7「配置配了不生效」）：正文链路常量接线回归
# ---------------------------------------------------------------------------
# 背景：config.py 早就有 content_* 共 7 项配置（含环境变量覆盖 + 默认值断言），
# 但 sse_handlers 把它们**硬编码成字面量**，运维改环境变量完全无效。
# test_sse_handlers_constants_wired_from_settings 是为此设的护栏，一直红着。
# 下面补齐「接线真的到位」与「温度真的传进 AI 调用」两层断言。
# ===========================================================================


def test_content_constants_track_settings_changes():
    """常量取值必须与 settings 逐项绑定（不 reload 模块，避免污染全局状态）。

    ⚠️ 早期版本用 `importlib.reload(sse_handlers)` 验证"改 settings 后常量跟着变"，
    实测会**重建模块级单例**（信号量 / 熔断器 / 配置缓存），既拖慢又可能让
    后续用例拿到不同的模块对象 —— 已改为直接比对 `常量 == settings.x`，
    同样能证明"接线"（若有人改回硬编码，断言即失败）。
    """
    from app.config import settings
    from app.routers import sse_handlers as sh

    assert sh.CONTENT_REQUEST_TIMEOUT == int(settings.content_request_timeout)
    assert sh.CONTENT_TOTAL_TIMEOUT == int(settings.content_total_timeout)
    assert sh.CONTENT_SECTION_RETRIES == max(0, int(settings.content_section_retries))
    assert sh.CONTENT_RETRY_BACKOFF == float(settings.content_retry_backoff)
    assert sh.CONTENT_RATE_LIMIT_BACKOFF == float(settings.content_rate_limit_backoff)
    assert sh.CONTENT_CONTINUE_MAX_ROUNDS == max(0, int(settings.content_continue_max_rounds))
    assert sh.CONTENT_TEMPERATURE == settings.content_temperature


def test_content_constants_not_hardcoded():
    """反向护栏：常量定义处**不得**再出现硬编码字面量。

    修复前 sse_handlers 写的是 `CONTENT_SECTION_RETRIES = 1` 等字面量，
    与上方「等于 settings.x」的断言互为对照：任一侧被回退，另一侧即失败。
    """
    from app.routers import sse_handlers as sh
    src = open(sh.__file__, encoding="utf-8").read()
    # 只看可执行代码行：注释里为了说明历史而引用旧字面量属正常，
    # 直接全文匹配会把说明文字误判为"仍是硬编码"。
    code_lines = []
    for raw in src.splitlines():
        code = raw.split("#", 1)[0]
        if code.strip():
            code_lines.append(code)
    code = "\n".join(code_lines)
    for literal in (
            "CONTENT_SECTION_RETRIES = 1",
            "CONTENT_RETRY_BACKOFF = 6.0",
            "CONTENT_RATE_LIMIT_BACKOFF = 20.0",
            "CONTENT_CONTINUE_MAX_ROUNDS = 2",
    ):
        assert literal not in code, (
            f"{literal!r} 是硬编码字面量，应改为绑定 settings（否则环境变量不生效）")


def test_content_temperature_reaches_ai_calls():
    """content_temperature 必须真的传到 chat_with_fallback（否则仍是死配置）。

    ✅ 修复前：CONTENT_TEMPERATURE 常量根本不存在，三处正文 AI 调用
    （draft / continue / shrink）都不传 temperature，配置项 100% 静默失效。
    """
    from app.routers import sse_handlers as sh
    src = open(sh.__file__, encoding="utf-8").read()
    # 三个正文场景各一处
    for scene in ("content_draft", "content_continue", "content_shrink"):
        idx = src.find(f'scene="{scene}"')
        assert idx != -1, f"未找到 {scene} 调用点"
        # 取该调用点前 400 字符的实参区，必须含 temperature=CONTENT_TEMPERATURE
        window = src[max(0, idx - 400):idx]
        assert "temperature=CONTENT_TEMPERATURE" in window, (
            f"{scene} 调用点未传 temperature=CONTENT_TEMPERATURE，"
            f"配置项将不生效")


def test_content_constants_defaults_unchanged():
    """默认值必须与修复前的硬编码逐字相同（保证旧行为不变、向后兼容）。"""
    from app.routers import sse_handlers as sh
    assert sh.CONTENT_REQUEST_TIMEOUT == 300
    assert sh.CONTENT_TOTAL_TIMEOUT == 660
    assert sh.CONTENT_SECTION_RETRIES == 1
    assert sh.CONTENT_RETRY_BACKOFF == 6.0
    assert sh.CONTENT_RATE_LIMIT_BACKOFF == 20.0
    assert sh.CONTENT_CONTINUE_MAX_ROUNDS == 2
    assert sh.CONTENT_TEMPERATURE is None
