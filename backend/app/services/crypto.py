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


def _is_valid_fernet_key(text: str) -> bool:
    """判定文本是否可作为 Fernet 密钥使用（非空 + 可构造）。

    ✅ R169：排他创建竞态下，失败方轮询读取胜出方正在写入的密钥文件——
    只有「完整落盘」的密钥才可用（半截内容构造 Fernet 必抛）。
    """
    if not text:
        return False
    try:
        Fernet(text.encode())
        return True
    except Exception:
        return False


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
            # ✅ R175（2026-11-03 · BUG-06）：文件存在但为空时，旧实现会走
            #    「生成新 Key → x 模式 FileExistsError → 重试 10×0.05s → raise
            #    → 外层临时密钥」路径，且因临时密钥本身合法而**不产生任何告警**：
            #    每次重启换一把钥，全部已存密文永久 key_broken，用户毫无感知。
            #    这里显式告警并删除空文件后重建（排他创建保证多进程只有一方胜出）。
            logger.warning(
                "持久化主密钥文件 %s 存在但内容为空（0 字节），将重新生成并写盘。"
                "此前用临时密钥保存的 API Key 无法自动恢复，请在界面重新填写",
                _KEY_FILE,
            )
            try:
                _KEY_FILE.unlink()
            except FileNotFoundError:
                pass
        key = Fernet.generate_key().decode()
        _KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        try:
            # ✅ R169：排他创建（O_EXCL 语义）——多进程同时首启只有一进程胜出
            #    写盘，失败方读取胜出方密钥（短暂重试等写入完成）。旧实现
            #    write_text 各写各的，后写者覆盖先写者：先写进程已加密落库的
            #    API Key 密文对后写进程即 key_broken（界面「密钥失效」、候选
            #    被静默跳过）。
            with open(_KEY_FILE, "x", encoding="utf-8") as f:
                f.write(key)
        except FileExistsError:
            import time as _time

            for _ in range(10):
                existing = ""
                if _KEY_FILE.exists():
                    existing = _KEY_FILE.read_text(encoding="utf-8").strip()
                if _is_valid_fernet_key(existing):
                    return existing
                _time.sleep(0.05)
            # 抢占方 0.5s 内仍未写出完整密钥（异常场景）→ 交外层降级临时密钥
            raise
        try:
            os.chmod(_KEY_FILE, 0o600)  # POSIX 下限制权限；Windows 尽力而为
        except Exception as e:
            logger.debug("密钥文件 chmod 600 失败（尽力而为，不影响密钥可用性）: %s", e)
        return key
    except Exception as e:
        # 落盘失败（只读盘等）退回进程级临时密钥，功能仍可用但重启失效。
        # ✅ R175（BUG-06）：不再静默 —— 临时密钥意味着重启后历史密文全部失效。
        logger.warning(
            "无法持久化主密钥（数据目录不可写？），本次启动使用进程级临时密钥；"
            "重启后已保存的 API Key 将无法解密，请检查 data 目录权限: %s",
            e,
        )
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
                    "FERNET_KEY 非法，改用 data/secret_key.key 的持久化密钥" "（避免已保存的 API Key 全部解不开）"
                )
        persisted = _load_or_create_persistent_key()
        try:
            _fernet = Fernet(persisted.encode())
        except Exception:
            logger.warning("持久化密钥文件内容非法，本次使用临时密钥" "（重启后已保存的 API Key 需重新填写）")
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


# ---------------------------------------------------------------------------
# 主密钥（at-rest 加密密钥）轮转
#
# 背景（AGENTS.md §4.6 / 遗留项 G-F）：此前对外只有「删除 Key」的能力，
# 没有任何「轮转主密钥」的入口。而主密钥是**所有**已保存 API Key 的加密密钥：
# 一旦更换 FERNET_KEY / data/secret_key.key 而不做迁移，库里**全部**历史密文
# 立刻变成「密钥失效（key_broken）」「状态」。合规场景又要求定期轮换主密钥，
# 于是「想轮换 → 不敢换 → 明文 Key 永久暴露在同一把密钥下」的僵局长期存在。
#
# 本轮新增以下纯函数（全部**加法式**，既有 encrypt_api_key / decrypt_api_key
# 一字未动，因此既有行为逐字不变）：
#   master_key_path()          —— 主密钥文件路径的唯一读取出口（测试与运维共用）
#   is_valid_master_key()      —— 校验字符串能否作为 Fernet 密钥
#   backup_master_key()        —— 轮转前把当前密钥文件快照一份（best-effort）
#   rotate_master_key()        —— 换新密钥：备份 → 落盘(chmod 600) → 清缓存
#   decrypt_with_legacy_key()  —— 用一把历史密钥解单个密文（迁移窗口用）
#   reencrypt_token()          —— 把一把能解开的密文用**当前**主密钥重新加密
#
# 迁移语义（刻意做成 fail-soft）：
#   rotate_master_key() 之后，**旧的密文无法再用新密钥解开**——这是 Fernet
#   的固有语义，不是 bug。正确用法是：rotate 之后用 reencrypt_token() 逐条把
#   库里的密文重新加密一遍（迁移窗口内可先用 decrypt_with_legacy_key() 兜住
#   旧密文）。任何一步失败都不会抛异常、不会破坏已存在的密钥文件。
# ---------------------------------------------------------------------------


def master_key_path() -> "os.PathLike":
    """主密钥文件路径的唯一读取出口。

    暴露此出口是为了让轮转、备份与测试共用**同一个**路径常量，避免在别处
    再抄一份 `DATA_DIR / "secret_key.key"`（本仓反复记录的「同一事实两处字面量」
    反模式）。
    """
    return _KEY_FILE


def is_valid_master_key(key: str) -> bool:
    """判断一个字符串能否作为 Fernet 主密钥。

    Fernet 要求密钥是 32 字节 urlsafe base64（44 字符，结尾含 ``=``).
    这里用「能否成功构造 Fernet 实例」作为唯一判据，不重造正则——
    否则将来 Fernet 校验规则变化时会产生第二套口径。
    """
    if not key:
        return False
    try:
        Fernet(key.strip().encode())
        return True
    except Exception:
        return False


def master_key_source() -> dict:
    """「当前生效的主密钥来自哪里」的**只读**唯一出口。

    镜像 :func:`_get_fernet` 的密钥来源优先级（环境变量 → 持久化文件 → 临时），
    但**不触碰**进程内 Fernet 缓存、不落盘、不产生任何副作用 —— 专为「主密钥
    轮转 / 存量密文迁移」工作流提供决策依据：轮转前须先知道当前密钥是「来自
    FERNET_KEY 环境变量」还是「来自 data/secret_key.key 文件」，二者对「轮转
    持久化文件」的语义完全不同（env 生效时轮转文件不影响解密，会白白制造
    「已轮转但仍用旧密钥」的静默分叉）。

    返回::

        {"source": "env" | "persisted_file" | "temporary",
         "key_file_exists": bool,
         "effective_key": str}   # 当前生效的主密钥明文（仅供内部迁移，严禁外泄到响应体）

    ⚠️ ``effective_key`` 是敏感值，只允许在**进程内**作为迁移的 legacy 密钥使用；
    任何面向前端的响应体都不得回传它（本模块的端点只回传来源标签与计数）。
    """
    file_exists = _KEY_FILE.exists()
    env_key = (settings.fernet_key or "").strip()
    if env_key and is_valid_master_key(env_key):
        return {"source": "env", "key_file_exists": file_exists, "effective_key": env_key}
    if file_exists:
        try:
            content = _KEY_FILE.read_text(encoding="utf-8").strip()
            if content and is_valid_master_key(content):
                return {"source": "persisted_file", "key_file_exists": True, "effective_key": content}
        except Exception as e:
            # 读文件失败等同「无可用文件密钥」，交给下方 temporary 兜底
            logger.debug("读取主密钥文件失败（%s，按无文件密钥处理）: %s", _KEY_FILE, e)
    return {"source": "temporary", "key_file_exists": file_exists, "effective_key": ""}


def ensure_master_key_ready() -> dict:
    """✅ R175（BUG-06）：启动期主密钥自检。

    首次 ``_get_fernet()`` 会按「FERNET_KEY 环境变量 → data/secret_key.key →
    进程级临时密钥」初始化；空文件/损坏文件在 ``_load_or_create_persistent_key``
    内已被修复（重建）或显式告警。本函数把最终来源在**启动期**落一条日志 ——
    避免「临时密钥」只在首次保存/读取密钥时才被发现，而那时历史密文可能已经
    不可解。返回值与 :func:`master_key_source` 一致（不含明文密钥的业务响应）。
    """
    _get_fernet()
    src = master_key_source()
    if src["source"] == "temporary":
        logger.warning(
            "主密钥当前为进程级临时密钥（不可持久化）：服务重启后已保存的 API Key "
            "将全部无法解密。请检查 FERNET_KEY 环境变量与 data 目录写权限；"
            "修复后可在「文本模型配置 → 主密钥轮转」重新加密存量密文。"
        )
    elif src["source"] == "persisted_file":
        logger.info("主密钥来自持久化文件 data/secret_key.key（可跨重启）")
    else:
        logger.info("主密钥来自 FERNET_KEY 环境变量（多实例共享）")
    return src


def backup_master_key() -> "str":
    """把当前持久化主密钥文件复制一份快照，返回快照路径字符串。

    返回空串表示「无密钥文件 / 复制失败」，**绝不抛异常**：轮转是维护动作，
    备份失败不应阻断，但调用方必须能观察到失败（用返回值而非异常）。
    """
    import shutil
    import time

    try:
        if not _KEY_FILE.exists():
            return ""
        ts = time.strftime("%Y%m%d_%H%M%S")
        bak = _KEY_FILE.with_suffix(_KEY_FILE.suffix + f".bak-{ts}")
        shutil.copyfile(_KEY_FILE, bak)
        try:
            os.chmod(bak, 0o600)  # 与原密钥文件同权限；Windows 下尽力而为
        except Exception as e:
            logger.debug("备份密钥文件 chmod 600 失败（尽力而为）: %s", e)
        return str(bak)
    except Exception:
        logger.warning("主密钥备份失败（不影响轮转本身）", exc_info=True)
        return ""


def rotate_master_key(new_key: "str | None" = None) -> "dict":
    """轮换持久化主密钥（at-rest 加密密钥）。

    步骤：① 校验新密钥 → ② 备份旧密钥文件 → ③ 落盘（chmod 600）
    → ④ 清空进程内缓存，使新密钥立即生效。

    返回结构（**加法式**，全部为可判定的布尔/字符串，便于写断言）：
      ok                    —— 是否成功
      new_key_from          —— "generated"（未传 new_key，自动生成）/ "provided"
      backup_path           —— 旧密钥快照路径（失败为空串）
      error                 —— 失败原因（成功为空串）

    ⚠️ 关键不变式：**新密钥与旧密钥不同时是合法的**。Fernet 密文只能用产生
    它的那把密钥解开，因此 rotate 成功后，旧密文一律 ``decrypt_api_key == ""``。
    调用方必须在同一次维护窗口内用 ``reencrypt_token()`` 把库里密文重加密，
    否则所有已保存的 API Key 都会显示「密钥失效」。该不变式由护栏锁定。
    """
    key = (new_key or "").strip()
    if key:
        if not is_valid_master_key(key):
            return {
                "ok": False,
                "new_key_from": "",
                "backup_path": "",
                "error": "new_key 不是合法的 Fernet 密钥（应为 44 字符 urlsafe base64）",
            }
    else:
        key = Fernet.generate_key().decode()

    backup = backup_master_key()

    global _fernet
    try:
        # 与 _load_or_create_persistent_key 同口径：目录不存在则创建，写后收紧权限
        _KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        _KEY_FILE.write_text(key, encoding="utf-8")
        try:
            os.chmod(_KEY_FILE, 0o600)
        except Exception as e:
            logger.debug("轮转后密钥文件 chmod 600 失败（尽力而为）: %s", e)
    except Exception:
        logger.warning("主密钥落盘失败，已放弃本次轮转（旧密钥不受影响）", exc_info=True)
        return {
            "ok": False,
            "new_key_from": "provided" if new_key else "generated",
            "backup_path": backup,
            "error": "密钥文件写入失败（只读盘等）",
        }

    # ④ 清缓存：不清会让本进程继续持有旧 Fernet 实例，表现为「已轮换但
    #    解密仍用旧密钥」的静默失配。
    _fernet = None

    logger.info("主密钥已轮换（来源=%s，备份=%s）", "provided" if new_key else "generated", backup or "无")
    return {"ok": True, "new_key_from": "provided" if new_key else "generated", "backup_path": backup, "error": ""}


def decrypt_with_legacy_key(enc: "str", legacy_key: "str") -> "str":
    """用**指定的历史密钥**解单个密文，解不开返回空串。

    用途：主密钥轮换后的**迁移窗口**内，让尚未重加密的旧密文仍可读出，
    避免维护窗口内所有历史 Key 集体失效。绝不抛异常（InvalidToken / 非法
    密钥都归一为空串），与 ``decrypt_api_key`` 的容错语义一致。
    """
    if not enc or not legacy_key:
        return ""
    try:
        token = legacy_key.strip().encode()
        return Fernet(token).decrypt(enc.encode()).decode()
    except Exception:
        return ""


def reencrypt_token(enc: "str", legacy_key: "str" = "") -> "str":
    """把一把密文用**当前**主密钥重新加密，返回新密文。

    这是主密钥轮转的**配套迁移动作**：rotate 之后逐条调用本函数即可把
    历史密文整体搬到新密钥下。失败（密文为空 / 解不开 / 加密异常）一律
    返回空串，调用方据空串判断「这条没搬成功」，不会抛异常中断整批迁移。

    ``legacy_key``（主密钥轮换后才需要的迁移参数）：
      rotate 之后旧密文**无法**再用新密钥解开，故必须先传一把**历史密钥**
      才能读出明文。给了 ``legacy_key`` 时按「当前密钥 → 历史密钥」顺序尝试；
      不传则只认当前密钥（用于同密钥下的重加密 / 修复）。
      ⚠️ 这个参数是必要的：缺它时 rotate 后的旧密文将永远无法迁移，
      「先轮转、再迁移」的工作流在结构上就走不通。

    ⚠️ 幂等性说明：Fernet 密文含时间戳与随机 nonce，**重复调用不会得到
    相同密文**；但「解开后重加密」的结果语义等价，因此可安全重试。
    """
    plaintext = decrypt_api_key(enc)
    if not plaintext and legacy_key:
        plaintext = decrypt_with_legacy_key(enc, legacy_key)
    if not plaintext:
        return ""
    try:
        return encrypt_api_key(plaintext)
    except Exception:
        logger.warning("密文重加密失败（保留原密文）", exc_info=True)
        return ""
