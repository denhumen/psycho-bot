"""Шаблони ручних нагадувань (CRUD). Без прив'язки до групи (інваріант 6)."""

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy import delete, select

from app.admin.auth import current_admin, get_ctx
from app.admin.web import flash, redirect, render
from app.db.models import AdminUser, ReminderTemplate

router = APIRouter()


@router.get("/templates")
async def list_templates(request: Request, admin: AdminUser = Depends(current_admin)) -> Response:
    async with get_ctx(request).read() as session:
        items = (await session.execute(select(ReminderTemplate).order_by(ReminderTemplate.title))).scalars().all()
    return await render(request, "templates.html", admin, items=items)


@router.post("/templates")
async def create_template(
    request: Request,
    title: str = Form(""),
    text: str = Form(""),
    is_active: str | None = Form(None),
    admin: AdminUser = Depends(current_admin),
) -> Response:
    if not title.strip() or not text.strip():
        flash(request, "Назва і текст обов'язкові.", "error")
        return redirect("/templates")
    async with get_ctx(request).db() as session, session.begin():
        session.add(ReminderTemplate(title=title.strip(), text=text.strip(), is_active=is_active is not None))
    flash(request, "Шаблон створено.")
    return redirect("/templates")


@router.post("/templates/{template_id}")
async def update_template(
    request: Request,
    template_id: int,
    title: str = Form(""),
    text: str = Form(""),
    is_active: str | None = Form(None),
    admin: AdminUser = Depends(current_admin),
) -> Response:
    if not title.strip() or not text.strip():
        flash(request, "Назва і текст обов'язкові.", "error")
        return redirect("/templates")
    async with get_ctx(request).db() as session, session.begin():
        item = await session.get(ReminderTemplate, template_id)
        if item is None:
            raise HTTPException(status_code=404)
        item.title, item.text, item.is_active = title.strip(), text.strip(), is_active is not None
    flash(request, "Шаблон збережено.")
    return redirect("/templates")


@router.post("/templates/{template_id}/delete")
async def delete_template(request: Request, template_id: int, admin: AdminUser = Depends(current_admin)) -> Response:
    # Шаблони — налаштування, не дані дослідження: надіслані тексти лишаються в messages.
    async with get_ctx(request).db() as session, session.begin():
        await session.execute(delete(ReminderTemplate).where(ReminderTemplate.id == template_id))
    flash(request, "Шаблон видалено.")
    return redirect("/templates")
