"""声明式基类、SQL 类型约定与共享时间戳字段。"""

from datetime import UTC, datetime
from typing import Annotated

from sqlalchemy import BigInteger, DateTime, Integer, MetaData, SmallInteger, Text
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
    """返回一个可移植的 `BIGINT`，在 MySQL 上为无符号类型。"""
    mysql_type = mysql.BIGINT(unsigned=True)
    return BigInteger().with_variant(mysql_type, "mysql").with_variant(Integer(), "sqlite")


def unsigned_smallint() -> SmallInteger:
    """返回一个可移植的 `SMALLINT`，在 MySQL 上为无符号类型。"""
    mysql_type = mysql.SMALLINT(unsigned=True)
    return SmallInteger().with_variant(mysql_type, "mysql").with_variant(Integer(), "sqlite")


def medium_text() -> Text:
    """返回一个可移植的 `TEXT` 列，在 MySQL 上映射为 `MEDIUMTEXT`。"""
    mysql_type = mysql.MEDIUMTEXT()
    return Text().with_variant(mysql_type, "mysql")


def utc_now() -> datetime:
    """返回当前带时区的 UTC 时间戳。"""
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
    """把带时区的 UTC 时间戳存为无时区的 MySQL `DATETIME` 值。"""

    impl = DateTime
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[datetime]:
        """在 MySQL 上使用微秒精度，其它方言使用标准 `DATETIME`。"""
        if dialect.name == "mysql":
            mysql_type = mysql.DATETIME(fsp=6)
            return dialect.type_descriptor(mysql_type)
        return dialect.type_descriptor(DateTime())

    def process_bind_param(
        self,
        value: datetime | None,
        dialect: Dialect,
    ) -> datetime | None:
        """写入前把带时区的时间戳统一换算到 UTC。"""
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
        """为从数据库读出的时间戳补上 UTC 时区。"""
        del dialect
        if value is None:
            return None
        return value.replace(tzinfo=UTC)


class Base(DeclarativeBase):
    """所有 ORM 模型共用的基类。"""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampMixin:
    """提供由数据库管理的创建与更新时间戳。"""

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
