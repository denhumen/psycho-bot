from pathlib import Path
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine


def create_engine(db_path: Path) -> AsyncEngine:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path.as_posix()}")

    @event.listens_for(engine.sync_engine, "connect")
    def _on_connect(dbapi_conn: Any, _record: Any) -> None:
        # Вимикаємо неявні транзакції драйвера, щоб самим керувати BEGIN.
        dbapi_conn.isolation_level = None
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

    @event.listens_for(engine.sync_engine, "begin")
    def _on_begin(conn: Any) -> None:
        # Кожна транзакція одразу бере write-лок: процес один, навантаження мале,
        # зате немає SQLITE_BUSY при підвищенні read→write посеред транзакції.
        conn.exec_driver_sql("BEGIN IMMEDIATE")

    return engine


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
