"""
安全模块 — 加密、鉴权、限流、输入清洗
=====================================

生产环境必需:
  - AES-256-GCM 加密 API Key
  - JWT 鉴权（Web UI）
  - 令牌桶限流
  - 安全 HTTP 头
  - 输入清洗
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import struct
import time
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger("qqbot.security")

# cryptography 是可选依赖（仅加密功能需要）
_AESGCM = None


def _get_aesgcm():
    """惰性加载 cryptography，未安装则报友好错误"""
    global _AESGCM
    if _AESGCM is None:
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM as _AESGCMClass
            _AESGCM = _AESGCMClass
        except ImportError:
            raise ImportError(
                "加密功能需要 cryptography 库。请运行: pip install cryptography"
            )
    return _AESGCM

# ═══════════════════════════════════════════════════════════
#  密钥管理
# ═══════════════════════════════════════════════════════════

_SECRET_DIR: str = ""
_MASTER_KEY: bytes = b""
_SECRETS_FILE: str = ""
_KEY_FILE: str = ""


def _data_dir() -> str:
    from .config import config
    return config.DATA_DIR


def init_security(secret_key: str = ""):
    """
    初始化安全模块。

    - 如果提供了 secret_key，使用它作为主密钥
    - 否则从 data/.master_key 读取或自动生成
    - 主密钥用于 AES 加密和 JWT 签名
    """
    global _SECRET_DIR, _MASTER_KEY, _SECRETS_FILE, _KEY_FILE
    _SECRET_DIR = _data_dir()
    os.makedirs(_SECRET_DIR, exist_ok=True)
    _KEY_FILE = os.path.join(_SECRET_DIR, ".master_key")
    _SECRETS_FILE = os.path.join(_SECRET_DIR, "secrets.enc")

    if secret_key:
        # 从提供的 key 派生 256-bit 密钥
        _MASTER_KEY = hashlib.sha256(secret_key.encode()).digest()
        logger.info("安全模块已初始化 (使用环境变量 SECRET_KEY)")
    elif os.path.exists(_KEY_FILE):
        with open(_KEY_FILE, "rb") as f:
            _MASTER_KEY = f.read()
        if len(_MASTER_KEY) != 32:
            logger.warning("主密钥长度异常，重新生成")
            _MASTER_KEY = _get_aesgcm().generate_key(bit_length=256)
            _save_master_key()
        logger.info("安全模块已初始化 (从文件加载)")
    else:
        _MASTER_KEY = _get_aesgcm().generate_key(bit_length=256)
        _save_master_key()
        logger.info(f"安全模块已初始化 (新生成主密钥，已保存到 {_KEY_FILE})")


def _save_master_key():
    with open(_KEY_FILE, "wb") as f:
        f.write(_MASTER_KEY)
    # Windows 下不设置权限，Linux/macOS 设 600
    try:
        os.chmod(_KEY_FILE, 0o600)
    except Exception:
        pass


def get_master_key_hash() -> str:
    """获取主密钥的 SHA256 指纹（用于验证密钥是否匹配）"""
    return hashlib.sha256(_MASTER_KEY).hexdigest()[:12]


# ═══════════════════════════════════════════════════════════
#  AES-256-GCM 加密
# ═══════════════════════════════════════════════════════════

def encrypt_value(plaintext: str) -> str:
    """
    AES-256-GCM 加密字符串。
    返回 Base64 编码的密文（包含 nonce）。
    格式: base64(nonce(12B) + ciphertext)
    """
    if not _MASTER_KEY:
        raise RuntimeError("安全模块未初始化，请先调用 init_security()")
    aesgcm = _get_aesgcm()(_MASTER_KEY)
    nonce = os.urandom(12)
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
    # nonce + ciphertext → base64
    return base64.b64encode(nonce + ciphertext).decode("ascii")


def decrypt_value(encrypted: str) -> str:
    """
    AES-256-GCM 解密字符串。
    """
    if not _MASTER_KEY:
        raise RuntimeError("安全模块未初始化，请先调用 init_security()")
    aesgcm = _get_aesgcm()(_MASTER_KEY)
    try:
        raw = base64.b64decode(encrypted)
        nonce, ciphertext = raw[:12], raw[12:]
        return aesgcm.decrypt(nonce, ciphertext, None).decode("utf-8")
    except Exception as e:
        logger.error(f"解密失败: {e}")
        raise ValueError("解密失败，主密钥可能已变更") from e


def encrypt_api_keys(keys: dict[str, str]) -> str:
    """加密整个 API key 字典，返回加密后的 base64"""
    return encrypt_value(json.dumps(keys, ensure_ascii=False))


def decrypt_api_keys(encrypted: str) -> dict[str, str]:
    """解密 API key 字典"""
    return json.loads(decrypt_value(encrypted))


# ═══════════════════════════════════════════════════════════
#  安全存储: API Keys
# ═══════════════════════════════════════════════════════════

def store_encrypted_secrets(secrets_dict: dict) -> bool:
    """加密并持久化保存 API keys"""
    try:
        encrypted = encrypt_api_keys(secrets_dict)
        with open(_SECRETS_FILE, "w", encoding="utf-8") as f:
            f.write(encrypted)
        _set_file_permissions(_SECRETS_FILE)
        logger.info("已加密保存 API keys")
        return True
    except Exception as e:
        logger.error(f"保存加密 secrets 失败: {e}")
        return False


def load_encrypted_secrets() -> dict:
    """加载并解密 API keys"""
    if not os.path.exists(_SECRETS_FILE):
        return {}
    try:
        with open(_SECRETS_FILE, "r", encoding="utf-8") as f:
            encrypted = f.read().strip()
        if encrypted:
            return decrypt_api_keys(encrypted)
    except Exception as e:
        logger.error(f"加载加密 secrets 失败: {e}")
    return {}


def _set_file_permissions(path: str):
    """设置文件为仅所有者可读写"""
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════
#  JWT 鉴权
# ═══════════════════════════════════════════════════════════

def create_jwt_token(payload: dict, expires_hours: int = 24) -> str:
    """
    创建签名的 JWT 风格 token。
    格式: base64url(header).base64url(payload).signature
    """
    header = {"alg": "HS256", "typ": "JWT"}
    now = int(time.time())
    payload_with_exp = {
        **payload,
        "iat": now,
        "exp": now + expires_hours * 3600,
        "jti": secrets.token_hex(8),
    }
    header_b64 = _b64url_encode(json.dumps(header, separators=(",", ":")).encode())
    payload_b64 = _b64url_encode(json.dumps(payload_with_exp, separators=(",", ":")).encode())
    message = f"{header_b64}.{payload_b64}"
    signature = _b64url_encode(
        hmac.new(_MASTER_KEY, message.encode(), hashlib.sha256).digest()
    )
    return f"{message}.{signature}"


def verify_jwt_token(token: str) -> Optional[dict]:
    """
    验证 JWT token，返回 payload 或 None。
    """
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        header_b64, payload_b64, signature_b64 = parts
        message = f"{header_b64}.{payload_b64}"
        expected_sig = _b64url_encode(
            hmac.new(_MASTER_KEY, message.encode(), hashlib.sha256).digest()
        )
        if not hmac.compare_digest(signature_b64, expected_sig):
            return None
        payload_bytes = _b64url_decode(payload_b64)
        payload = json.loads(payload_bytes)
        # 检查过期
        if payload.get("exp", 0) < time.time():
            logger.debug("JWT token 已过期")
            return None
        return payload
    except Exception as e:
        logger.debug(f"JWT 验证异常: {e}")
        return None


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    # 补齐 padding
    padding = 4 - len(s) % 4
    if padding != 4:
        s += "=" * padding
    return base64.urlsafe_b64decode(s)


# ═══════════════════════════════════════════════════════════
#  令牌桶限流器
# ═══════════════════════════════════════════════════════════

class RateLimiter:
    """
    内存令牌桶限流器。
    线程安全（单线程 async 场景下足够）。
    """

    def __init__(self, rate: int = 30, per_seconds: int = 60, burst: int = 10):
        """
        Args:
            rate: 每个时间窗口允许的请求数
            per_seconds: 时间窗口大小（秒）
            burst: 突发允许的最大令牌数
        """
        self.rate = rate
        self.per_seconds = per_seconds
        self.burst = burst
        self._buckets: dict[str, tuple[float, float]] = {}  # key → (tokens, last_update)

    def allow(self, key: str) -> bool:
        """
        检查是否允许请求。返回 True 表示放行。
        """
        now = time.time()
        tokens, last_update = self._buckets.get(key, (self.burst, now))
        # 补充令牌
        elapsed = now - last_update
        tokens = min(self.burst, tokens + elapsed * (self.rate / self.per_seconds))
        if tokens >= 1:
            tokens -= 1
            self._buckets[key] = (tokens, now)
            return True
        else:
            self._buckets[key] = (tokens, now)
            return False

    def reset(self, key: str):
        self._buckets.pop(key, None)

    def cleanup(self, max_age: float = 3600):
        """清理过期的桶"""
        now = time.time()
        stale = [k for k, (_, t) in self._buckets.items() if now - t > max_age]
        for k in stale:
            del self._buckets[k]


# 全局限流器实例
login_limiter = RateLimiter(rate=5, per_seconds=60, burst=5)     # 登录: 5次/分钟
api_limiter = RateLimiter(rate=60, per_seconds=60, burst=20)     # API: 60次/分钟


# ═══════════════════════════════════════════════════════════
#  输入清洗
# ═══════════════════════════════════════════════════════════

def sanitize_html(text: str) -> str:
    """转义 HTML 特殊字符（防 XSS）"""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#x27;")
    )


def sanitize_sql_like(text: str) -> str:
    """过滤 SQL 通配符（防止 LIKE 注入）"""
    return text.replace("%", "\\%").replace("_", "\\_")


def validate_qq(qq) -> Optional[int]:
    """验证 QQ 号格式，返回 int 或 None"""
    try:
        qq_int = int(qq)
        if 10000 <= qq_int <= 99999999999:
            return qq_int
    except (ValueError, TypeError):
        pass
    return None


def validate_ip(ip: str) -> bool:
    """验证 IP 地址格式"""
    import re as _re
    parts = ip.split(".")
    if len(parts) != 4:
        return False
    for p in parts:
        try:
            if not (0 <= int(p) <= 255):
                return False
        except ValueError:
            return False
    return True


# ═══════════════════════════════════════════════════════════
#  安全 HTTP 头
# ═══════════════════════════════════════════════════════════

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "X-XSS-Protection": "1; mode=block",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Server": "",  # 隐藏服务器信息
}


# ═══════════════════════════════════════════════════════════
#  密码哈希
# ═══════════════════════════════════════════════════════════

def hash_password(password: str) -> str:
    """PBKDF2-SHA256 密码哈希"""
    salt = os.urandom(16)
    key = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return base64.b64encode(salt + key).decode("ascii")


def verify_password(password: str, hashed: str) -> bool:
    """验证密码"""
    try:
        raw = base64.b64decode(hashed)
        salt, key = raw[:16], raw[16:]
        new_key = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
        return hmac.compare_digest(new_key, key)
    except Exception:
        return False
