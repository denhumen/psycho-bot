"""python -m app.cli <команда>

  create-admin --username X --role owner|researcher   пароль вводиться інтерактивно
  backup-now [--to PATH]                              у файл або (без --to) у BACKUP_CHAT_ID
  export --to DIR                                     CSV-файли експорту в каталог
  purge-contacts --confirm                            видалити chat_id усіх учасників (після дослідження)
"""

import argparse
import asyncio
import getpass
import sys
from pathlib import Path

from sqlalchemy import delete, func, select

from app.bot.router import create_bot
from app.bot.sender import Sender
from app.clock import clock
from app.config import Settings
from app.context import AppContext
from app.db.migrate import run_migrations
from app.db.models import Contact, Participant, Status
from app.db.session import create_engine, create_read_sessionmaker, create_sessionmaker
from app.domain.admins import ROLES, create_admin
from app.domain.backup import backup_filename, backup_to, send_backup
from app.domain.content import load_content
from app.domain.events import log_event
from app.domain.export import build_export
from app.domain.state import LAST_BACKUP_AT, set_time

PURGE_PHRASE = "ВИДАЛИТИ КОНТАКТИ"
CLI_ACTOR = "cli"


def _context(settings: Settings) -> AppContext:
    run_migrations(settings.database_path)
    engine = create_engine(settings.database_path)
    db = create_sessionmaker(engine)
    content = load_content(settings.content_dir, settings.support_contact)
    bot = create_bot(settings.bot_token.get_secret_value()) if settings.bot_token.get_secret_value() else None
    return AppContext(
        settings=settings,
        engine=engine,
        db=db,
        read=create_read_sessionmaker(engine),
        content=content,
        sender=Sender(bot, db, content),  # type: ignore[arg-type]
    )


async def _close(ctx: AppContext) -> None:
    if ctx.sender.bot is not None:
        await ctx.sender.bot.session.close()
    await ctx.engine.dispose()


async def cmd_create_admin(ctx: AppContext, args: argparse.Namespace) -> int:
    password = getpass.getpass("Пароль: ")
    if password != getpass.getpass("Повторіть пароль: "):
        print("Паролі не збігаються.", file=sys.stderr)
        return 1
    try:
        async with ctx.db() as session, session.begin():
            admin = await create_admin(session, args.username, password, args.role)
            log_event(session, "admin_user_created", actor=CLI_ACTOR, user_id=admin.id, role=args.role)
    except ValueError as exc:
        print(f"Помилка: {exc}", file=sys.stderr)
        return 1
    print(f"Створено користувача {args.username} ({args.role}).")
    return 0


async def cmd_backup_now(ctx: AppContext, args: argparse.Namespace) -> int:
    if args.to:
        target = Path(args.to)
        if target.is_dir():
            target = target / backup_filename(clock.now(), ctx)
        await asyncio.to_thread(backup_to, ctx.settings.database_path, target)
        print(f"Копію БД збережено: {target}")
        return 0
    chat_id = ctx.settings.backup_chat_id
    if chat_id is None or ctx.sender.bot is None:
        print("Для надсилання в Telegram потрібні BOT_TOKEN і BACKUP_CHAT_ID (або вкажіть --to).", file=sys.stderr)
        return 1
    await send_backup(ctx, chat_id)
    async with ctx.db() as session, session.begin():
        await set_time(session, LAST_BACKUP_AT, clock.now())
        log_event(session, "backup_sent", actor=CLI_ACTOR, trigger="cli")
    print("Бекап надіслано в Telegram.")
    return 0


async def cmd_export(ctx: AppContext, args: argparse.Namespace) -> int:
    folder = Path(args.to)
    folder.mkdir(parents=True, exist_ok=True)
    async with ctx.read() as session:
        files = await build_export(session, ctx.settings, ctx.content)
    for name, data in files.items():
        (folder / name).write_bytes(data)
    async with ctx.db() as session, session.begin():
        log_event(session, "export_downloaded", actor=CLI_ACTOR)
    print(f"Експорт збережено в {folder} ({len(files)} файлів). Файли містять розподіл за групами!")
    return 0


async def cmd_purge_contacts(ctx: AppContext, args: argparse.Namespace) -> int:
    if not args.confirm:
        print("Додайте --confirm.", file=sys.stderr)
        return 1
    async with ctx.read() as session:
        contacts = (await session.execute(select(func.count()).select_from(Contact))).scalar_one()
        in_progress = (
            await session.execute(
                select(func.count())
                .select_from(Participant)
                .where(Participant.status.in_([Status.REGISTERED, Status.CONSENTED, Status.ACTIVE]))
            )
        ).scalar_one()
    print(f"Буде видалено {contacts} контактів (chat_id). Бот більше не зможе писати жодному учаснику.")
    if in_progress:
        print(f"УВАГА: {in_progress} учасників ще не завершили участь!")
    phrase = input(f"Щоб підтвердити, введіть «{PURGE_PHRASE}»: ")
    if phrase.strip() != PURGE_PHRASE:
        print("Скасовано.")
        return 1
    # Єдиний DELETE даних дослідження в застосунку — явна анонімізація після завершення.
    async with ctx.db() as session, session.begin():
        await session.execute(delete(Contact))
        log_event(session, "contacts_purged", actor=CLI_ACTOR, count=contacts)
    print(f"Видалено {contacts} контактів.")
    return 0


COMMANDS = {
    "create-admin": cmd_create_admin,
    "backup-now": cmd_backup_now,
    "export": cmd_export,
    "purge-contacts": cmd_purge_contacts,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="Службові команди дослідження")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create-admin", help="створити користувача адмінки")
    create.add_argument("--username", required=True)
    create.add_argument("--role", required=True, choices=ROLES)
    backup = commands.add_parser("backup-now", help="зробити бекап БД зараз")
    backup.add_argument("--to", help="файл або каталог; без нього — надіслати в BACKUP_CHAT_ID")
    export = commands.add_parser("export", help="експорт CSV у каталог")
    export.add_argument("--to", required=True)
    purge = commands.add_parser("purge-contacts", help="видалити контакти після завершення дослідження")
    purge.add_argument("--confirm", action="store_true")
    return parser


async def run(argv: list[str], settings: Settings | None = None) -> int:
    args = build_parser().parse_args(argv)
    ctx = _context(settings or Settings())
    try:
        return await COMMANDS[args.command](ctx, args)
    finally:
        await _close(ctx)


def main() -> None:
    sys.exit(asyncio.run(run(sys.argv[1:])))


if __name__ == "__main__":
    main()
