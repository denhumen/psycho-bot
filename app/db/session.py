from pathlib import Path
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

READ_ONLY_OPTION = "study_read_only"


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
        # Транзакції запису одразу беруть write-лок (BEGIN IMMEDIATE): без SQLITE_BUSY
        # при підвищенні read→write, атомарне призначення слотів. Читання в WAL не
        # блокується і нікого не блокує — для нього звичайний BEGIN.
        if conn.get_execution_options().get(READ_ONLY_OPTION):
            conn.exec_driver_sql("BEGIN")
        else:
            conn.exec_driver_sql("BEGIN IMMEDIATE")

    return engine


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Сесії для запису. Мережеві виклики всередині транзакції заборонені."""
    return async_sessionmaker(engine, expire_on_commit=False)


def create_read_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Сесії лише для читання."""
    return async_sessionmaker(engine.execution_options(**{READ_ONLY_OPTION: True}), expire_on_commit=False)
