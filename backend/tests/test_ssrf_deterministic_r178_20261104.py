"""R178 补强 · SSRF 护栏确定性回归（不依赖真实 DNS）。

背景（R178 验收报告 §4.1 B4/B5）：
    原 SSRF 用例在保存 AI 配置时走**实时 DNS**，本机对 `api.openai.com` 的解析
    在 20 分钟内出现过 3 种不同地址，其中几次落入被禁网段 → 同一份代码「今天绿
    明天红」。护栏测试的结果不能取决于外网。

本文件把**解析入口**（`provider_factory.socket.getaddrinfo`）打桩成确定性输入，
真实判定逻辑（字面 IP / 回环 / 链路本地 / 云元数据 / 多记录 fail-closed /
例外开关）照常执行 —— 不绕过 `_host_is_forbidden`，因此也能拦住「守卫被整体
摘掉」这类回归。

覆盖：
  A 私网/回环/链路本地/元数据地址 → 拒绝（字面 IP 与域名两路）
  B 公网地址 → 放行（字面 IP 与域名两路）
  C `AI_ALLOW_PRIVATE_BASE_URL=true` → 同一私网地址放行（例外开关真接线）
  D 字面 IP 与域名两条路径决策一致（不能只有字面 IP 被拦）
  E 多记录任一条被禁 → 整体拒绝（fail-closed）
  F 拒绝文案可读、给出逃生开关、且不泄露 URL 中的密钥
"""

from __future__ import annotations

import socket

import pytest
from app.services.ai import provider_factory as PF

PUBLIC_IP = "93.184.216.34"
FORBIDDEN_IPS = ["127.0.0.1", "169.254.169.254", "10.0.0.1"]
ALL_FORBIDDEN_LITERALS = FORBIDDEN_IPS + [
    "172.16.0.1",
    "192.168.1.10",
    "[::1]",
    "localhost",
]


def _stub_dns(monkeypatch, *ips: str) -> None:
    """把解析入口固定为给定 IP 序列；返回形态与 socket.getaddrinfo 一致。"""
    monkeypatch.setattr(
        PF.socket,
        "getaddrinfo",
        lambda *_a, **_k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in ips],
    )


@pytest.fixture(autouse=True)
def _private_switch_off(monkeypatch):
    """每个用例从「默认拒绝私网」开始，避免用例间串味。"""
    monkeypatch.setattr(PF.settings, "ai_allow_private_base_url", False)


class TestForbidden:
    @pytest.mark.parametrize("host", ALL_FORBIDDEN_LITERALS)
    def test_literal_host_rejected(self, host):
        """字面地址（含回环 / 链路本地元数据 / 私有网段 / localhost）必须拒绝。"""
        with pytest.raises(ValueError) as ei:
            PF.normalize_base_url(f"http://{host}/v1")
        assert "安全策略拒绝" in str(ei.value)

    @pytest.mark.parametrize("ip", FORBIDDEN_IPS)
    def test_domain_resolving_to_forbidden_rejected(self, monkeypatch, ip):
        """域名解析到私网/回环/元数据地址同样拒绝（不能只拦字面 IP）。"""
        _stub_dns(monkeypatch, ip)
        with pytest.raises(ValueError) as ei:
            PF.normalize_base_url("https://sink.example.com/v1")
        assert "安全策略拒绝" in str(ei.value)

    def test_any_forbidden_record_rejects_whole_host(self, monkeypatch):
        """多记录中只要有一条落入禁区 → 整体拒绝（fail-closed，不取第一条）。"""
        _stub_dns(monkeypatch, PUBLIC_IP, "10.0.0.1")
        with pytest.raises(ValueError):
            PF.normalize_base_url("https://mixed.example.com/v1")


class TestAllowed:
    def test_public_literal_ip_allowed(self):
        assert PF.normalize_base_url("https://1.1.1.1/v1") == "https://1.1.1.1/v1"

    def test_public_domain_allowed(self, monkeypatch):
        _stub_dns(monkeypatch, PUBLIC_IP)
        assert PF.normalize_base_url("https://api.example.com/v1") == "https://api.example.com/v1"


class TestParity:
    @pytest.mark.parametrize("ip", FORBIDDEN_IPS)
    def test_literal_and_domain_share_same_verdict(self, monkeypatch, ip):
        """同一地址经字面 IP 与经域名解析，必须得到**逐字相同**的拒绝结论。"""
        _stub_dns(monkeypatch, ip)
        with pytest.raises(ValueError) as literal:
            PF.normalize_base_url(f"https://{ip}/v1")
        with pytest.raises(ValueError) as domain:
            PF.normalize_base_url("https://sink.example.com/v1")
        assert str(literal.value) == str(domain.value)


class TestPrivateEscapeHatch:
    def test_switch_allows_private_literal(self, monkeypatch):
        """开关必须真的接线：同一私网地址在开关打开后放行（本机 Ollama/vLLM 场景）。"""
        monkeypatch.setattr(PF.settings, "ai_allow_private_base_url", True)
        assert PF.normalize_base_url("http://127.0.0.1:11434/v1") == "http://127.0.0.1:11434/v1"

    def test_switch_allows_domain_resolving_private(self, monkeypatch):
        monkeypatch.setattr(PF.settings, "ai_allow_private_base_url", True)
        _stub_dns(monkeypatch, "10.0.0.1")
        assert PF.normalize_base_url("https://ollama.example.com/v1") == "https://ollama.example.com/v1"


class TestRejectionMessage:
    def test_message_is_actionable_and_does_not_leak_secret(self):
        """拒绝文案要说明原因与逃生开关，且不得把 URL 里的凭据回显给用户/日志。"""
        with pytest.raises(ValueError) as ei:
            PF.normalize_base_url("http://user:sk-r178-SECRET@10.0.0.1/v1")
        text = str(ei.value)
        assert "内网/回环/链路本地" in text
        assert "AI_ALLOW_PRIVATE_BASE_URL=true" in text
        assert "sk-r178-SECRET" not in text
        assert "user:" not in text
