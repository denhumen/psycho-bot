from alembic import context
from sqlalchemy import create_engine, pool

from app.db.models import Base

config = context.config
url = config.get_main_option("sqlalchemy.url")
if not url:
    from app.config import Settings

    url = f"sqlite:///{Settings().database_path.as_posix()}"


def run() -> None:
    if context.is_offline_mode():
        context.configure(url=url, target_metadata=Base.metadata, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()
        return
    engine = create_engine(url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=Base.metadata, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()


run()
