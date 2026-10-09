"""Password hashing and verification using Argon2."""

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHash, VerificationError

_PASSWORD_HASHER = PasswordHasher()
_MIN_PASSWORD_LENGTH = 12


def hash_password(password: str) -> str:
    """Hash a password after enforcing the minimum length policy."""
    if len(password) < _MIN_PASSWORD_LENGTH:
        raise ValueError(f"password must contain at least {_MIN_PASSWORD_LENGTH} characters")
    return _PASSWORD_HASHER.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """Return whether a plaintext password matches an Argon2 hash."""
    try:
        return _PASSWORD_HASHER.verify(password_hash, password)
    except (InvalidHash, VerificationError):
        return False
