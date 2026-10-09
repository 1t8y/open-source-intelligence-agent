"""Declarative base, SQL type conventions, and shared timestamp fields."""

from datetime import UTC, datetime
from typing import Annotated

from sqlalchemy import BigInteger, DateTime, MetaData, SmallInteger
from sqlalchemy.dialects import mysql
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator, TypeEngine

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def unsigned_bigint() -> BigInteger:
    """Return a portable BIGINT that is unsigned on MySQL."""
    mysql_type = mysql.BIGINT(unsigned=True)  # type: ignore[no-untyped-call]
    return BigInteger().with_variant(mysql_type, "mysql")


def unsigned_smallint() -> SmallInteger:
    """Return a portable SMALLINT that is unsigned on MySQL."""
    mysql_type = mysql.SMALLINT(unsigned=True)  # type: ignore[no-untyped-call]
    return SmallInteger().with_variant(mysql_type, "mysql")


def utc_now() -> datetime:
    """Return the current timezone-aware UTC timestamp."""
    return datetime.now(UTC)


uint64_pk = Annotated[
    int,
    mapped_column(unsigned_bigint(), primary_key=True, autoincrement=True),
]
uint16_pk = Annotated[
    int,
    mapped_column(unsigned_smallint(), primary_key=True, autoincrement=True),
]


class UTCDateTime(TypeDecorator[datetime]):
    """Persist aware UTC timestamps as naive MySQL DATETIME values."""

    impl = DateTime
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[datetime]:
        """Use microsecond precision on MySQL and standard DATETIME elsewhere."""
        if dialect.name == "mysql":
            mysql_type = mysql.DATETIME(fsp=6)  # type: ignore[no-untyped-call]
            return dialect.type_descriptor(mysql_type)
        return dialect.type_descriptor(DateTime())

    def process_bind_param(
        self,
        value: datetime | None,
        dialect: Dialect,
    ) -> datetime | None:
        """Normalize aware timestamps to UTC before writing."""
        del dialect
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("UTC timestamps must be timezone-aware")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(
        self,
        value: datetime | None,
        dialect: Dialect,
    ) -> datetime | None:
        """Attach UTC to timestamps read from the database."""
        del dialect
        if value is None:
            return None
        return value.replace(tzinfo=UTC)


class Base(DeclarativeBase):
    """Base class shared by all ORM models."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampMixin:
    """Provide database-managed creation and update timestamps."""

    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        default=utc_now,
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )
