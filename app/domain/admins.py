from typing import Literal

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AdminUser

Role = Literal["owner", "researcher"]
ROLES: tuple[Role, ...] = ("owner", "researcher")
MIN_PASSWORD_LENGTH = 10

_hasher = PasswordHasher()
# Перевірка «порожнього» хешу для неіснуючого логіна — однаковий час відповіді.
_DUMMY_HASH = _hasher.hash("dummy-password-for-timing")


def hash_password(password: str) -> str:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"пароль має містити щонайменше {MIN_PASSWORD_LENGTH} символів")
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerificationError, InvalidHashError):
        return False


async def by_username(session: AsyncSession, username: str) -> AdminUser | None:
    return (await session.execute(select(AdminUser).where(AdminUser.username == username))).scalar_one_or_none()


async def create_admin(session: AsyncSession, username: str, password: str, role: Role) -> AdminUser:
    username = username.strip()
    if not username:
        raise ValueError("логін не може бути порожнім")
    if role not in ROLES:
        raise ValueError(f"роль має бути однією з {ROLES}")
    if await by_username(session, username) is not None:
        raise ValueError(f"користувач {username!r} уже існує")
    admin = AdminUser(username=username, password_hash=hash_password(password), role=role)
    session.add(admin)
    await session.flush()
    return admin
