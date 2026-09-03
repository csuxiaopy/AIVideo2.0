from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from backend.config import ROOT, get_settings


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


settings = get_settings()
engine = create_engine(settings.database_url, pool_pre_ping=True)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@contextmanager
def session_scope():
    session: Session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def create_schema() -> None:
    from backend import models  # noqa: F401

    Base.metadata.create_all(engine)


def upgrade_schema() -> None:
    """Create missing tables, then apply conditional Alembic upgrades to legacy databases."""
    create_schema()
    from alembic import command
    from alembic.config import Config

    config = Config(str(ROOT / "alembic.ini"))
    command.upgrade(config, "head")


def bootstrap_admin() -> None:
    from backend import models
    from backend.security import hash_password, validate_username

    with session_scope() as session:
        if (session.query(models.User).count() or 0) > 0:
            return
        username = settings.admin_username.strip()
        password = settings.admin_password
        if not username or not password:
            raise RuntimeError(
                "系统尚无账号，请设置 ADMIN_USERNAME 和 ADMIN_PASSWORD 后重新启动"
            )
        try:
            username = validate_username(username).lower()
            password_hash = hash_password(password)
        except ValueError as exc:
            raise RuntimeError(f"初始管理员配置无效：{exc}") from exc
        session.add(models.User(
            username=username,
            display_name=settings.admin_display_name.strip() or "系统管理员",
            password_hash=password_hash,
            role="admin",
            enabled=True,
        ))
