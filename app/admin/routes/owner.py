"""Сторінки лише для owner: коди, експорт, бекап, користувачі."""

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response
from sqlalchemy import select

from app.admin.auth import get_ctx, require_owner
from app.admin.web import flash, redirect, render
from app.clock import clock
from app.db.models import AdminUser
from app.domain.admins import ROLES, create_admin, hash_password
from app.domain.backup import backup_bytes, backup_filename, send_backup
from app.domain.codes import code_stats, parse_codes, upload_codes
from app.domain.events import admin_actor, log_event
from app.domain.export import build_export, zip_files
from app.domain.state import LAST_BACKUP_AT, get_time, set_time

router = APIRouter(dependencies=[Depends(require_owner)])

INVALID_SHOWN = 20


def _attachment(data: bytes, filename: str, media_type: str) -> Response:
    return Response(
        data, media_type=media_type, headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


# --- коди ------------------------------------------------------------------


@router.get("/codes")
async def codes_page(request: Request, admin: AdminUser = Depends(require_owner)) -> Response:
    ctx = get_ctx(request)
    async with ctx.read() as session:
        total, used = await code_stats(session)
    report = request.session.pop("codes_report", None)
    return render(
        request,
        "codes.html",
        admin,
        total=total,
        used=used,
        free=total - used,
        report=report,
        validation=ctx.settings.code_validation,
    )


@router.post("/codes")
async def codes_upload(
    request: Request,
    codes: str = Form(""),
    file: UploadFile | None = None,
    admin: AdminUser = Depends(require_owner),
) -> Response:
    ctx = get_ctx(request)
    raw = parse_codes(codes)
    if file is not None and file.filename:
        try:
            raw += parse_codes((await file.read()).decode("utf-8-sig"))
        except UnicodeDecodeError:
            flash(request, "Файл має бути в кодуванні UTF-8.", "error")
            return redirect("/codes")
    async with ctx.db() as session, session.begin():
        report = await upload_codes(session, ctx.settings, raw, uploaded_by=admin.username)
        log_event(
            session,
            "codes_uploaded",
            actor=admin_actor(admin.id),
            added=len(report.added),
            duplicates=len(report.duplicates),
            invalid=len(report.invalid),
        )
    request.session["codes_report"] = {
        "added": len(report.added),
        "duplicates": len(report.duplicates),
        "invalid": len(report.invalid),
        "invalid_examples": report.invalid[:INVALID_SHOWN],
    }
    return redirect("/codes")


# --- експорт ---------------------------------------------------------------


@router.get("/export")
async def export_page(request: Request, admin: AdminUser = Depends(require_owner)) -> Response:
    return render(request, "export.html", admin)


@router.post("/export")
async def export_download(request: Request, admin: AdminUser = Depends(require_owner)) -> Response:
    ctx = get_ctx(request)
    async with ctx.read() as session:
        files = await build_export(session, ctx.settings, ctx.content)
    async with ctx.db() as session, session.begin():
        log_event(session, "export_downloaded", actor=admin_actor(admin.id))
    stamp = clock.now().astimezone(ctx.settings.tz).strftime("%Y-%m-%d-%H%M")
    return _attachment(zip_files(files), f"study-export-{stamp}.zip", "application/zip")


# --- бекап -----------------------------------------------------------------


@router.get("/backup")
async def backup_page(request: Request, admin: AdminUser = Depends(require_owner)) -> Response:
    ctx = get_ctx(request)
    async with ctx.read() as session:
        last = await get_time(session, LAST_BACKUP_AT)
    return render(request, "backup.html", admin, last_backup=last, chat_configured=ctx.settings.backup_chat_id is not None)


@router.post("/backup/download")
async def backup_download(request: Request, admin: AdminUser = Depends(require_owner)) -> Response:
    ctx = get_ctx(request)
    data = await backup_bytes(ctx.settings.database_path)
    async with ctx.db() as session, session.begin():
        log_event(session, "backup_downloaded", actor=admin_actor(admin.id))
    return _attachment(data, backup_filename(clock.now(), ctx), "application/vnd.sqlite3")


@router.post("/backup/send")
async def backup_send(request: Request, admin: AdminUser = Depends(require_owner)) -> Response:
    ctx = get_ctx(request)
    chat_id = ctx.settings.backup_chat_id
    if chat_id is None:
        flash(request, "BACKUP_CHAT_ID не задано.", "error")
        return redirect("/backup")
    try:
        await send_backup(ctx, chat_id)
    except Exception:
        flash(request, "Не вдалося надіслати бекап у Telegram.", "error")
        return redirect("/backup")
    async with ctx.db() as session, session.begin():
        await set_time(session, LAST_BACKUP_AT, clock.now())
        log_event(session, "backup_sent", actor=admin_actor(admin.id), trigger="manual")
    flash(request, "Бекап надіслано в Telegram.")
    return redirect("/backup")


# --- користувачі -----------------------------------------------------------


@router.get("/users")
async def users_page(request: Request, admin: AdminUser = Depends(require_owner)) -> Response:
    async with get_ctx(request).read() as session:
        users = (await session.execute(select(AdminUser).order_by(AdminUser.username))).scalars().all()
    return render(request, "users.html", admin, users=users, roles=ROLES)


@router.post("/users")
async def users_create(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    role: str = Form("researcher"),
    admin: AdminUser = Depends(require_owner),
) -> Response:
    try:
        async with get_ctx(request).db() as session, session.begin():
            created = await create_admin(session, username, password, role)  # type: ignore[arg-type]
            log_event(session, "admin_user_created", actor=admin_actor(admin.id), user_id=created.id, role=role)
    except ValueError as exc:
        flash(request, f"Не вдалося створити: {exc}.", "error")
        return redirect("/users")
    flash(request, f"Користувача {username.strip()} створено.")
    return redirect("/users")


async def _existing_user(request: Request, user_id: int) -> AdminUser:
    async with get_ctx(request).read() as session:
        user = await session.get(AdminUser, user_id)
    if user is None:
        raise HTTPException(status_code=404)
    return user


@router.post("/users/{user_id}/active")
async def users_set_active(
    request: Request, user_id: int, active: str = Form(...), admin: AdminUser = Depends(require_owner)
) -> Response:
    if user_id == admin.id:
        flash(request, "Не можна деактивувати самого себе.", "error")
        return redirect("/users")
    await _existing_user(request, user_id)
    async with get_ctx(request).db() as session, session.begin():
        user = await session.get(AdminUser, user_id)
        assert user is not None
        user.is_active = active == "true"
        log_event(
            session,
            "admin_user_activated" if user.is_active else "admin_user_deactivated",
            actor=admin_actor(admin.id),
            user_id=user_id,
        )
    flash(request, "Збережено.")
    return redirect("/users")


@router.post("/users/{user_id}/password")
async def users_reset_password(
    request: Request, user_id: int, password: str = Form(""), admin: AdminUser = Depends(require_owner)
) -> Response:
    await _existing_user(request, user_id)
    try:
        password_hash = hash_password(password)
    except ValueError as exc:
        flash(request, f"Пароль не змінено: {exc}.", "error")
        return redirect("/users")
    async with get_ctx(request).db() as session, session.begin():
        user = await session.get(AdminUser, user_id)
        assert user is not None
        user.password_hash = password_hash
        log_event(session, "admin_password_reset", actor=admin_actor(admin.id), user_id=user_id)
    flash(request, "Пароль змінено.")
    return redirect("/users")
