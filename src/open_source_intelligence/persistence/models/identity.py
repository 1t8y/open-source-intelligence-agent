"""用户、角色与角色分配的模型。"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..base import (
    Base,
    TimestampMixin,
    uint16_pk,
    uint64_pk,
    unsigned_bigint,
    unsigned_smallint,
)


class User(Base, TimestampMixin):
    """应用的用户账号。"""

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'disabled')", name="status_valid"),
        CheckConstraint("data_scope IN ('personal', 'team')", name="data_scope_valid"),
    )

    id: Mapped[uint64_pk]
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    data_scope: Mapped[str] = mapped_column(String(32), nullable=False, default="personal")

    role_links: Mapped[list[UserRole]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )


class Role(Base):
    """稳定的角色定义，例如 `ADMIN`、`EDITOR` 或 `VIEWER`。"""

    __tablename__ = "roles"
    __table_args__ = (CheckConstraint("code IN ('ADMIN', 'EDITOR', 'VIEWER')", name="code_valid"),)

    id: Mapped[uint16_pk]
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)

    user_links: Mapped[list[UserRole]] = relationship(
        back_populates="role",
        cascade="all, delete-orphan",
    )


class UserRole(Base):
    """把角色分配给用户的关联表。"""

    __tablename__ = "user_roles"

    user_id: Mapped[int] = mapped_column(
        unsigned_bigint(),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    role_id: Mapped[int] = mapped_column(
        unsigned_smallint(),
        ForeignKey("roles.id", ondelete="CASCADE"),
        primary_key=True,
    )

    user: Mapped[User] = relationship(back_populates="role_links")
    role: Mapped[Role] = relationship(back_populates="user_links")
