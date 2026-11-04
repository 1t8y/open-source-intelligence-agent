"""构造数据库引擎与会话工厂的辅助函数。"""

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker


def create_database_engine(database_url: str, *, echo: bool = False) -> Engine:
    """创建带连接健康检查的连接池 SQLAlchemy 引擎。"""
    return create_engine(database_url, echo=echo, pool_pre_ping=True)


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """创建会话：不隐式 flush，也不让对象在提交后过期。"""
    return sessionmaker(
        bind=engine,
        autoflush=False,
        expire_on_commit=False,
    )
