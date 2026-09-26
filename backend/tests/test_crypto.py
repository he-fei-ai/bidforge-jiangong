"""API Key 加密模块（services/crypto.py）回归测试（✅ D6 补齐）。

背景（技术债）：该模块承载「AI Key 加密落库」这一安全关键路径，却**没有任何单测** ——
密钥来源优先级、落盘失败降级、解密失败静默返回空串这些行为，
历史上多次以「页面显示密钥失效/明明填了 Key 却调不通」的形式暴露给用户。

本文件锁定三组行为：
A. 密钥来源与回退（FERNET_KEY > data/secret_key.key > 兜底）
   —— 含 ✅ 2026-09-23 加固：FERNET_KEY 非法时改用持久化密钥而不是丢一把随机密钥
      （否则环境变量写错一个字符 = 库里全部已存 Key 永久解不开）；
B. 持久化密钥文件（落盘 / 复用 / 空文件 / 只读盘降级）；
C. 加解密与格式判定（encrypt / decrypt / is_encrypted 的边界与不抛异常承诺）。
"""
import pytest
from cryptography.fernet import Fernet

from app.services import crypto


@pytest.fixture(autouse=True)
def reset_fernet(monkeypatch):
    """每个用例重置模块级加密器缓存（否则密钥来源切换不生效）。"""
    monkeypatch.setattr(crypto, "_fernet", None)


@pytest.fixture
def key_file(tmp_path, monkeypatch):
    """把持久化密钥文件指向临时目录，避免污染仓库 data/。"""
    path = tmp_path / "secret_key.key"
    monkeypatch.setattr(crypto, "_KEY_FILE", path)
    return path


# ===========================================================================
# A. 密钥来源与回退
# ===========================================================================
class TestKeySource:
    def test_env_key_takes_precedence_and_skips_file(self, key_file, monkeypatch):
        env_key = Fernet.generate_key().decode()
        monkeypatch.setattr(crypto.settings, "fernet_key", env_key, raising=False)

        token = crypto.encrypt_api_key("sk-abc")
        assert crypto.decrypt_api_key(token) == "sk-abc"
        # 环境变量可用时不应再去生成/读取持久化文件
        assert not key_file.exists()

    def test_no_env_key_uses_persistent_file(self, key_file, monkeypatch):
        monkeypatch.setattr(crypto.settings, "fernet_key", "", raising=False)
        token = crypto.encrypt_api_key("sk-abc")
        assert key_file.exists(), "无 FERNET_KEY 时应落盘持久化密钥"

        # 模拟进程重启：清空缓存后仍能解开（这正是持久化密钥的意义）
        crypto._fernet = None
        assert crypto.decrypt_api_key(token) == "sk-abc"

    def test_invalid_env_key_falls_back_to_persistent_key(self, key_file, monkeypatch):
        """✅ 加固点：FERNET_KEY 非法不得丢掉库里已存的密文。"""
        monkeypatch.setattr(crypto.settings, "fernet_key", "", raising=False)
        token = crypto.encrypt_api_key("sk-kept")     # 用持久化密钥加密

        # 运维把 FERNET_KEY 写错（典型事故）→ 必须仍能用持久化密钥解开
        crypto._fernet = None
        monkeypatch.setattr(crypto.settings, "fernet_key", "not-a-valid-key",
                            raising=False)
        assert crypto.decrypt_api_key(token) == "sk-kept"

    def test_invalid_env_key_does_not_crash_encryption(self, key_file, monkeypatch):
        monkeypatch.setattr(crypto.settings, "fernet_key", "!!!", raising=False)
        assert crypto.decrypt_api_key(crypto.encrypt_api_key("sk-x")) == "sk-x"

    def test_corrupted_persistent_file_falls_back_without_crash(
            self, key_file, monkeypatch):
        """持久化文件被手工改坏 → 兜底临时密钥，功能可用（仅重启后需重填）"""
        key_file.parent.mkdir(parents=True, exist_ok=True)
        key_file.write_text("garbage-not-a-key", encoding="utf-8")
        monkeypatch.setattr(crypto.settings, "fernet_key", "", raising=False)

        token = crypto.encrypt_api_key("sk-x")
        assert crypto.decrypt_api_key(token) == "sk-x"


# ===========================================================================
# B. 持久化密钥文件
# ===========================================================================
class TestPersistentKeyFile:
    def test_creates_file_with_valid_key(self, key_file):
        key = crypto._load_or_create_persistent_key()
        assert key_file.read_text(encoding="utf-8").strip() == key
        Fernet(key.encode())  # 生成的必须是合法 Fernet 密钥

    def test_reuses_existing_key(self, key_file):
        first = crypto._load_or_create_persistent_key()
        second = crypto._load_or_create_persistent_key()
        assert first == second

    def test_empty_file_is_regenerated(self, key_file):
        key_file.parent.mkdir(parents=True, exist_ok=True)
        key_file.write_text("   ", encoding="utf-8")
        key = crypto._load_or_create_persistent_key()
        assert key.strip()
        assert Fernet(key.encode())

    def test_unwritable_path_returns_temp_key(self, tmp_path, monkeypatch):
        """只读盘 / 目录不可建 → 返回临时密钥而不抛异常（功能仍可用）。"""
        blocker = tmp_path / "blocker"
        blocker.write_text("x", encoding="utf-8")     # 同名普通文件挡住 mkdir
        monkeypatch.setattr(crypto, "_KEY_FILE", blocker / "sub" / "secret.key")

        key = crypto._load_or_create_persistent_key()
        assert key and Fernet(key.encode())

        # 且加密链路整体可用（不因落盘失败而崩）
        assert crypto.decrypt_api_key(crypto.encrypt_api_key("sk-x")) == "sk-x"

    def test_key_file_not_created_when_env_key_used(self, key_file, monkeypatch):
        monkeypatch.setattr(crypto.settings, "fernet_key",
                            Fernet.generate_key().decode(), raising=False)
        crypto.encrypt_api_key("sk-x")
        assert not key_file.exists()


# ===========================================================================
# C. 加解密与格式判定
# ===========================================================================
class TestEncryptDecrypt:
    def test_empty_input_returns_empty(self, key_file):
        assert crypto.encrypt_api_key("") == ""
        assert crypto.decrypt_api_key("") == ""

    def test_roundtrip_with_unicode_and_special_chars(self, key_file):
        for raw in ("sk-abc123", "含中文的密钥", "a b\tc\n", "x" * 500,
                    "sk-!@#$%^&*()_+-=[]{}|;:'\",.<>?/"):
            enc = crypto.encrypt_api_key(raw)
            assert enc != raw
            assert crypto.decrypt_api_key(enc) == raw

    def test_decrypt_garbage_returns_empty_not_raise(self, key_file):
        assert crypto.decrypt_api_key("not-a-token") == ""
        assert crypto.decrypt_api_key("gAAAA-not-real") == ""

    def test_ciphertext_is_not_reversible_with_other_key(self, key_file, monkeypatch):
        monkeypatch.setattr(crypto.settings, "fernet_key", "", raising=False)
        enc = crypto.encrypt_api_key("sk-secret")

        crypto._fernet = None
        monkeypatch.setattr(crypto.settings, "fernet_key",
                            Fernet.generate_key().decode(), raising=False)
        assert crypto.decrypt_api_key(enc) == "", "换密钥后解不开应返回空串（前端据此提示重新填写）"


class TestIsEncrypted:
    def test_empty_and_plaintext_are_false(self):
        assert crypto.is_encrypted("") is False
        assert crypto.is_encrypted("sk-plain-key-1234567890") is False

    def test_enc_prefix_is_true(self):
        assert crypto.is_encrypted("ENC:whatever") is True

    def test_fernet_token_is_true(self, key_file):
        assert crypto.is_encrypted(crypto.encrypt_api_key("sk-abc")) is True

    def test_short_gaaaa_prefix_is_false(self):
        """仅前缀匹配不算密文（避免把短脏值误判为已加密）"""
        assert crypto.is_encrypted("gAAAA") is False
        assert crypto.is_encrypted("gAAAA" + "x" * 10) is False
