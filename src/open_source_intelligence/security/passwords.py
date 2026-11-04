"""基于 Argon2 的密码哈希与校验。"""

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHash, VerificationError

_PASSWORD_HASHER = PasswordHasher()
_MIN_PASSWORD_LENGTH = 12


def hash_password(password: str) -> str:
    """先执行最小长度策略，再对密码做哈希。"""
    if len(password) < _MIN_PASSWORD_LENGTH:
        raise ValueError(f"password must contain at least {_MIN_PASSWORD_LENGTH} characters")
    return _PASSWORD_HASHER.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """判断明文密码是否与 Argon2 哈希匹配。"""
    try:
        return _PASSWORD_HASHER.verify(password_hash, password)
    except (InvalidHash, VerificationError):
        return False
