"""主密钥轮转（services/crypto.py 轮转段）护栏。

背景（AGENTS.md §4.6 / 遗留项 G-F）：此前对外只有「删除 Key」的能力，
没有任何「轮换主密钥」的入口。而主密钥是**所有**已保存 API Key 的加密密钥 ——
更换它而不做迁移会让库里**全部**历史密文立刻变成「密钥失效（key_broken）」。
本轮新增 rotate_master_key / backup_master_key / decrypt_with_legacy_key /
reencrypt_token 四个纯函数（加法式，既有 encrypt_api_key / decrypt_api_key 未动）。

本文件锁定五组行为：
  A. 密钥合法性校验（不重造正则，判据 = 能否构造 Fernet 实例）
  B. 主密钥路径唯一出口（防「同一路径两处字面量」回流）
  C. 轮转前备份（失败用返回值表达，绝不抛）
  D. 轮转本体与核心不变式（换钥后旧密文必失效、新密文可用）
  E. 迁移窗口（历史密钥解旧密文 / 当前密钥重加密）
  F. 字节体检 + 静态护栏（既有导出签名不变、模块内无重名定义）
"""
import ast
import inspect
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from app.services import crypto


@pytest.fixture(autouse=True)
def reset_fernet(monkeypatch):
    """每个用例重置模块级加密器缓存（否则密钥来源切换不生效）。

    同时清空 FERNET_KEY：环境变量密钥优先级高于持久化密钥文件，若不清，
    用例里对 _KEY_FILE 的一切操作都会被环境变量绕过 —— 表现为
    「轮转了但旧密文照样能解开」的假失败。
    """
    monkeypatch.setattr(crypto, "_fernet", None)
    monkeypatch.setattr(crypto.settings, "fernet_key", "", raising=False)


@pytest.fixture
def key_file(tmp_path, monkeypatch):
    """把持久化密钥文件指向临时目录，避免污染仓库 data/。"""
    path = tmp_path / "secret_key.key"
    monkeypatch.setattr(crypto, "_KEY_FILE", path)
    return path


def _write_valid_key(path, raw=None):
    """把一把合法 Fernet 密钥写入路径，返回去掉尾部换行后的密钥串。"""
    key = (raw or "").strip() or Fernet.generate_key().decode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(key, encoding="utf-8")
    return key


def _read_key(path):
    return path.read_text(encoding="utf-8").strip() if path.exists() else ""


# ===========================================================================
# A. 密钥合法性校验
# ===========================================================================
class TestIsValidMasterKey:
    def test_generated_key_is_valid(self):
        assert crypto.is_valid_master_key(Fernet.generate_key().decode()) is True

    def test_empty_string_is_invalid(self):
        assert crypto.is_valid_master_key("") is False

    def test_none_like_empty_is_invalid(self):
        assert crypto.is_valid_master_key(None) is False

    def test_garbage_is_invalid(self):
        for bad in ("not-a-valid-key", "!!!", "short", "gAAAAAA",
                    "a" * 44, "=" * 44):
            assert crypto.is_valid_master_key(bad) is False, bad

    def test_whitespace_around_valid_key_is_accepted(self):
        good = Fernet.generate_key().decode()
        assert crypto.is_valid_master_key("  " + good + "\n") is True

    def test_too_short_key_is_invalid(self):
        # 43 字符（缺末尾 padding）不是合法 Fernet 密钥
        good = Fernet.generate_key().decode()
        assert crypto.is_valid_master_key(good[:-1]) is False


# ===========================================================================
# B. 主密钥路径唯一出口
# ===========================================================================
class TestMasterKeyPathOutlet:
    def test_returns_the_same_object_as_internal_constant(self, key_file):
        """必须是同一对象（identity），而不是路径字符串的副本。

        若这里只回传字符串副本，轮转侧与测试侧就会各自持有路径，
        一旦有人改了 _KEY_FILE 的指向，两处口径立即分叉。
        """
        assert crypto.master_key_path() is crypto._KEY_FILE

    def test_matches_the_key_file_fixture(self, key_file):
        assert crypto.master_key_path() == key_file

    def test_module_has_single_key_path_literal(self):
        """`secret_key.key` 字面量只允许出现在 `_KEY_FILE` 的那一处赋值里。

        用 AST 判「可执行代码里的字符串常量」，而不是扫全文本 ——
        全文本扫描会被注释与 docstring 里的同名说明误伤
        （AGENTS.md §5.14：判据要锚定真正的取值点）。
        """
        tree = ast.parse(_src_text())
        assign_to_key_file = []
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            if not any(isinstance(t, ast.Name) and t.id == "_KEY_FILE"
                       for t in node.targets):
                continue
            literals = [n.value for n in ast.walk(node)
                        if isinstance(n, ast.Constant) and isinstance(n.value, str)]
            if "secret_key.key" in literals:
                assign_to_key_file.append(node)
        assert len(assign_to_key_file) == 1, (
            f"`_KEY_FILE` 的定义处应为 1 处，实测 {len(assign_to_key_file)} 处")

        # 任何函数体里都不得再出现该字面量（否则就是第二份路径取值）
        offenders = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Constant) and isinstance(sub.value, str) \
                            and sub.value == "secret_key.key":
                        offenders.append(node.name)
        assert not offenders, f"以下函数体内再次出现了密钥路径字面量：{sorted(set(offenders))}"


# ===========================================================================
# C. 轮转前备份
# ===========================================================================
class TestBackupMasterKey:
    def test_missing_file_returns_empty_string(self, tmp_path, monkeypatch):
        monkeypatch.setattr(crypto, "_KEY_FILE", tmp_path / "nope.key")
        assert crypto.backup_master_key() == ""

    def test_backup_has_identical_bytes_and_distinct_name(self, key_file):
        _write_valid_key(key_file)
        backup = crypto.backup_master_key()
        assert backup
        bak = Path(backup)
        assert bak != key_file
        assert bak.exists()
        assert bak.read_bytes() == key_file.read_bytes()

    def test_backup_does_not_overwrite_the_original(self, key_file):
        _write_valid_key(key_file)
        before = key_file.read_bytes()
        crypto.backup_master_key()
        assert key_file.read_bytes() == before

    def test_backup_succeeds_with_nested_dir(self, tmp_path, monkeypatch):
        """密钥文件父目录存在时应正常备份（不依赖任何外部条件）。"""
        path = tmp_path / "sub" / "secret_key.key"
        monkeypatch.setattr(crypto, "_KEY_FILE", path)
        _write_valid_key(path)
        assert crypto.backup_master_key()


# ===========================================================================
# D. 轮转本体与核心不变式
# ===========================================================================
class TestRotateMasterKey:
    def test_rejects_invalid_new_key_and_leaves_key_file_untouched(self, key_file):
        _write_valid_key(key_file)
        before = key_file.read_bytes()
        res = crypto.rotate_master_key("not-a-valid-key")
        assert res["ok"] is False
        assert res["error"]
        assert key_file.read_bytes() == before

    def test_generated_key_succeeds_and_replaces_file(self, key_file):
        _write_valid_key(key_file)
        old_key = _read_key(key_file)
        res = crypto.rotate_master_key()
        assert res["ok"] is True
        assert res["new_key_from"] == "generated"
        assert res["error"] == ""
        new_key = _read_key(key_file)
        assert new_key
        assert new_key != old_key
        assert crypto.is_valid_master_key(new_key)

    def test_provided_key_is_used_verbatim(self, key_file):
        provided = Fernet.generate_key().decode()
        res = crypto.rotate_master_key(provided)
        assert res["ok"] is True
        assert res["new_key_from"] == "provided"
        assert _read_key(key_file) == provided

    def test_creates_key_file_when_absent(self, tmp_path, monkeypatch):
        path = tmp_path / "new_dir" / "secret_key.key"
        monkeypatch.setattr(crypto, "_KEY_FILE", path)
        res = crypto.rotate_master_key()
        assert res["ok"] is True
        assert path.exists()

    def test_rotation_clears_the_encrypted_cache(self, key_file):
        """不清缓存会表现为「已轮换但解密仍用旧密钥」的静默失配。"""
        _write_valid_key(key_file)
        crypto.encrypt_api_key("seed")  # 强制初始化缓存
        assert crypto._fernet is not None
        crypto.rotate_master_key()
        assert crypto._fernet is None

    def test_backup_is_created_before_rotation(self, key_file):
        _write_valid_key(key_file)
        res = crypto.rotate_master_key()
        assert res["ok"] is True
        assert res["backup_path"]
        assert Path(res["backup_path"]).exists()

    def test_write_failure_reports_structured_error(self, tmp_path, monkeypatch):
        """落盘失败：不得抛异常、不落盘、也不清空在用缓存。"""
        blocker = tmp_path / "blocker"
        blocker.write_text("x", encoding="utf-8")   # 父路径是文件 → mkdir 必失败
        key_path = blocker / "secret_key.key"
        monkeypatch.setattr(crypto, "_KEY_FILE", key_path)

        crypto._get_fernet()       # 先让缓存建立
        assert crypto._fernet is not None

        res = crypto.rotate_master_key()
        assert res["ok"] is False
        assert res["error"]
        assert not key_path.exists()        # 失败不落盘
        assert crypto._fernet is not None   # 失败不得清掉在用密钥


class TestRotateKeyInvariant:
    """核心不变式：Fernet 密文只能用产生它的那把密钥解开。"""

    def test_old_ciphertext_becomes_undecryptable_after_rotate(self, key_file):
        _write_valid_key(key_file)
        plaintext = "sk-live-1234567890"
        ct = crypto.encrypt_api_key(plaintext)
        old_key = _read_key(key_file)
        assert crypto.decrypt_api_key(ct) == plaintext

        res = crypto.rotate_master_key()
        assert res["ok"] is True

        # 换钥后旧密文必须读不出（这是期望行为，不是缺陷）
        assert crypto.decrypt_api_key(ct) == ""
        # 而历史密钥仍能解出 —— 这就是「迁移窗口」存在的前提
        assert crypto.decrypt_with_legacy_key(ct, old_key) == plaintext
        assert crypto.decrypt_with_legacy_key(
            ct, Fernet.generate_key().decode()) == ""

    def test_reencrypted_ciphertext_survives_a_second_rotation(self, key_file):
        """两次轮转 + 每次迁移：明文必须逐字保留。"""
        _write_valid_key(key_file)
        plaintext = "sk-第二轮轮转测试-中文"
        ct = crypto.encrypt_api_key(plaintext)
        key1 = _read_key(key_file)

        crypto.rotate_master_key()
        migrated = crypto.reencrypt_token(ct, legacy_key=key1)
        assert migrated
        assert crypto.decrypt_api_key(migrated) == plaintext
        key2 = _read_key(key_file)

        crypto.rotate_master_key()
        migrated2 = crypto.reencrypt_token(migrated, legacy_key=key2)
        assert migrated2
        assert crypto.decrypt_api_key(migrated2) == plaintext


# ===========================================================================
# E. 迁移窗口
# ===========================================================================
class TestMigrationWindow:
    def test_decrypt_with_legacy_key_roundtrip(self, key_file):
        key = _write_valid_key(key_file)
        ct = Fernet(key.encode()).encrypt(b"legacy-secret").decode()
        assert crypto.decrypt_with_legacy_key(ct, key) == "legacy-secret"

    def test_decrypt_with_legacy_key_empty_inputs(self, key_file):
        _write_valid_key(key_file)
        other = Fernet.generate_key().decode()
        assert crypto.decrypt_with_legacy_key("", other) == ""
        assert crypto.decrypt_with_legacy_key("gAAAAAAA", "") == ""
        assert crypto.decrypt_with_legacy_key("", "") == ""

    def test_decrypt_with_legacy_key_garbage_ciphertext_returns_empty(self, key_file):
        key = _write_valid_key(key_file)
        assert crypto.decrypt_with_legacy_key("not-a-token", key) == ""

    def test_decrypt_with_legacy_key_rejects_wrong_key(self, key_file):
        key = _write_valid_key(key_file)
        ct = Fernet(key.encode()).encrypt(b"secret").decode()
        assert crypto.decrypt_with_legacy_key(
            ct, Fernet.generate_key().decode()) == ""

    def test_reencrypt_token_is_a_noop_on_undecryptable_input(self, key_file):
        _write_valid_key(key_file)
        assert crypto.reencrypt_token("") == ""
        assert crypto.reencrypt_token("garbage") == ""

    def test_reencrypt_needs_legacy_key_after_rotation(self, key_file):
        """不传历史密钥时轮转后的旧密文无法迁移 —— 该参数是结构上必需的。"""
        _write_valid_key(key_file)
        old_key = _read_key(key_file)
        ct = crypto.encrypt_api_key("sk-需要迁移")
        crypto.rotate_master_key()
        assert crypto.reencrypt_token(ct) == ""                   # 缺历史密钥 → 搬不动
        migrated = crypto.reencrypt_token(ct, legacy_key=old_key)
        assert migrated
        assert crypto.decrypt_api_key(migrated) == "sk-需要迁移"

    def test_reencrypt_token_preserves_plaintext_not_ciphertext(self, key_file):
        """Fernet 含随机 nonce 与时间戳：重加密结果密文不同，但语义等价。"""
        _write_valid_key(key_file)
        plaintext = "sk-语义等价"
        ct = crypto.encrypt_api_key(plaintext)
        new_ct = crypto.reencrypt_token(ct)
        assert new_ct != ct
        assert crypto.decrypt_api_key(new_ct) == plaintext

    def test_reencrypt_then_rotate_migrates_every_token(self, key_file):
        """运维推荐顺序：先轮转、再用当前密钥重加密 —— 全部可读。"""
        _write_valid_key(key_file)
        ct_old = crypto.encrypt_api_key("sk-A")
        crypto.rotate_master_key()
        ct_new = crypto.encrypt_api_key("sk-B")

        assert crypto.decrypt_api_key(ct_old) == ""       # 旧密文失效
        assert crypto.decrypt_api_key(ct_new) == "sk-B"
        assert crypto.decrypt_with_legacy_key(
            ct_old, _old_key_of(key_file)) == "sk-A"


def _old_key_of(key_file):
    """从轮转产生的备份里取出旧密钥（备份文件名含时间戳）。"""
    parent = key_file.parent
    cands = sorted(parent.glob("secret_key.key.bak-*"))
    return _read_key(cands[0]) if cands else ""


# ===========================================================================
# F. 字节体检 + 静态护栏
# ===========================================================================
def _src_path():
    return Path(__file__).resolve().parents[1] / "app" / "services" / "crypto.py"


def _src_text():
    return _src_path().read_text(encoding="utf-8", errors="replace")


def _src_bytes():
    return _src_path().read_bytes()


def _func_node(name):
    tree = ast.parse(_src_text())
    return next(n for n in tree.body
                if isinstance(n, ast.FunctionDef) and n.name == name)


class TestByteAndStaticGuards:
    def test_no_bom_no_replacement_char_no_lone_cr(self):
        data = _src_bytes()
        assert data[:3] != b"\xef\xbf\xbd"
        assert data.count(b"\xef\xbf\xbd") == 0
        assert data.count(b"\x00") == 0
        assert data.count(b"\x07") == 0
        # 全仓是纯 CRLF：CR 数必须等于 CRLF 数（裸 LF / 孤立 CR 都是损坏信号）
        assert data.count(b"\r") == data.count(b"\r\n")

    def test_existing_api_signatures_unchanged(self):
        """既有导出签名一字未动 —— 加法式改造的兼容红线。"""
        assert str(inspect.signature(crypto.encrypt_api_key)) == "(raw: str) -> str"
        assert str(inspect.signature(crypto.decrypt_api_key)) == "(enc: str) -> str"
        assert str(inspect.signature(crypto.is_encrypted)) == "(raw: str) -> bool"

    def test_new_functions_have_keyword_safe_signatures(self):
        assert str(inspect.signature(crypto.rotate_master_key)) == \
            "(new_key: 'str | None' = None) -> 'dict'"
        assert str(inspect.signature(crypto.decrypt_with_legacy_key)) == \
            "(enc: 'str', legacy_key: 'str') -> 'str'"
        assert str(inspect.signature(crypto.reencrypt_token)) == \
            "(enc: 'str', legacy_key: 'str' = '') -> 'str'"
        assert str(inspect.signature(crypto.backup_master_key)) == "() -> 'str'"
        assert str(inspect.signature(crypto.is_valid_master_key)) == \
            "(key: str) -> bool"
        assert str(inspect.signature(crypto.master_key_path)) == "() -> 'os.PathLike'"

    def test_no_duplicate_top_level_names(self):
        """editor 重复插入会把同一函数写两份（历史踩坑形态）。"""
        tree = ast.parse(_src_text())
        names = [n.name for n in tree.body
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                                   ast.ClassDef))]
        dupes = sorted({n for n in names if names.count(n) > 1})
        assert not dupes, f"crypto.py 存在重名顶层定义：{dupes}"

    def test_rotate_clears_the_encrypted_cache(self):
        """摘掉清缓存那行会让「已轮换仍用旧钥」静默发生。"""
        fn = _func_node("rotate_master_key")
        assigns = [n for n in ast.walk(fn)
                   if isinstance(n, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == "_fernet"
                           for t in n.targets)]
        assert assigns, "rotate_master_key 必须显式清空模块级加密器缓存"
        const = assigns[-1].value
        assert isinstance(const, ast.Constant) and const.value is None

    def test_rotate_writes_through_the_single_key_path_outlet(self):
        """轮转必须经 _KEY_FILE 落盘，不得再抄一份路径字面量。"""
        fn = _func_node("rotate_master_key")
        refs = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
        assert "_KEY_FILE" in refs
        literals = {n.value for n in ast.walk(fn)
                    if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        assert "secret_key.key" not in literals

    def test_fail_soft_helpers_never_raise(self):
        """三个辅助函数体内必须有 try/except 兜底（不抛异常承诺）。"""
        for name in ("backup_master_key", "decrypt_with_legacy_key",
                     "reencrypt_token"):
            fn = _func_node(name)
            assert any(isinstance(n, ast.Try) for n in ast.walk(fn)), \
                f"{name} 缺少 try/except 兜底"

    def test_reencrypt_actually_calls_the_legacy_fallback(self):
        """迁移兜底必须**真的被调用**，而不只是「函数名在场」。

        「源码里含有 decrypt_with_legacy_key 这个字符串」是假护栏：
        注释和 docstring 里提到它同样会命中，而接线被摘掉时依然全绿。
        这里用 AST 找 reencrypt_token 函数体里的**真实调用点**。
        """
        fn = _func_node("reencrypt_token")
        calls = [n for n in ast.walk(fn)
                 if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Name)
                 and n.func.id == "decrypt_with_legacy_key"]
        assert calls, (
            "reencrypt_token 必须实际调用 decrypt_with_legacy_key，"
            "否则主密钥轮换后的旧密文永远无法迁移")