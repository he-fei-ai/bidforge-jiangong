"""API Key 加密存储（Fernet / AES-256）"""
import logging
import os
from cryptography.fernet import Fernet, InvalidToken

from app.config import DATA_DIR, settings

logger = logging.getLogger("crypto")

_fernet: Fernet | None = None

# 未配置 FERNET_KEY 时的持久化密钥文件（避免进程级临时密钥导致重启后
# 已保存的 AI Key 全部静默解密失败）
_KEY_FILE = DATA_DIR / "secret_key.key"


def _load_or_create_persistent_key() -> str:
    """从 data 目录加载持久化密钥；不存在则生成并落盘。

    用户仍可通过环境变量 FERNET_KEY 显式指定密钥（优先级更高），
    便于多实例部署共享同一密钥。
    """
    try:
        if _KEY_FILE.exists():
            key = _KEY_FILE.read_text(encoding="utf-8").strip()
            if key:
                return key
        key = Fernet.generate_key().decode()
        _KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        _KEY_FILE.write_text(key, encoding="utf-8")
        try:
            os.chmod(_KEY_FILE, 0o600)  # POSIX 下限制权限；Windows 尽力而为
        except Exception:
            pass
        return key
    except Exception:
        # 落盘失败（只读盘等）退回进程级临时密钥，功能仍可用但重启失效
        return Fernet.generate_key().decode()


def _get_fernet() -> Fernet:
    """取（并缓存）加密器实例。

    密钥优先级与降级链（✅ 2026-09-23 加固）：
      1. ``settings.fernet_key``（环境变量 FERNET_KEY，多实例共享密钥用）；
      2. 非法 → **放弃**环境变量并改用 ``data/secret_key.key`` 的持久化密钥。
         旧实现此处是「直接换一把随机密钥」，后果是：环境变量写错一个字符，
         库里**全部已保存的 API Key 永久解不开**（页面显示「密钥失效」），
         且重启后随机密钥变化，看起来像「加密功能坏了」。改用持久化密钥可自愈。
      3. 持久化文件也非法（被手工改坏）→ 最后兜底随机密钥，保证功能可用。
    """
    global _fernet
    if _fernet is None:
        key = settings.fernet_key
        if key:
            try:
                _fernet = Fernet(key.encode())
                return _fernet
            except Exception:
                logger.warning(
                    "FERNET_KEY 非法，改用 data/secret_key.key 的持久化密钥"
                    "（避免已保存的 API Key 全部解不开）")
        persisted = _load_or_create_persistent_key()
        try:
            _fernet = Fernet(persisted.encode())
        except Exception:
            logger.warning(
                "持久化密钥文件内容非法，本次使用临时密钥"
                "（重启后已保存的 API Key 需重新填写）")
            _fernet = Fernet(Fernet.generate_key())
    return _fernet


def encrypt_api_key(raw: str) -> str:
    if not raw:
        return ""
    return _get_fernet().encrypt(raw.encode()).decode()


def decrypt_api_key(enc: str) -> str:
    if not enc:
        return ""
    try:
        return _get_fernet().decrypt(enc.encode()).decode()
    except (InvalidToken, Exception):
        return ""


def is_encrypted(raw: str) -> bool:
    """判断字符串是否为已加密内容（自招投标方案平台移植）

    兼容两种格式：
    - ENC: 前缀（源项目约定）
    - Fernet token（本项目 encrypt_api_key 的输出，以 gAAAA 开头）
    """
    if not raw:
        return False
    if raw.startswith("ENC:"):
        return True
    # Fernet token 特征：base64url 且以 gAAAA 开头（含版本字节 0x80 的时间戳）
    return raw.startswith("gAAAA") and len(raw) > 50
