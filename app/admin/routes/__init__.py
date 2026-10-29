from fastapi import APIRouter, Depends

from app.admin.auth import csrf_protect
from app.admin.routes import auth, dashboard, participants, reminder_templates


def build_router() -> APIRouter:
    # CSRF перевіряється для кожного POST адмінки на рівні роутера.
    router = APIRouter(dependencies=[Depends(csrf_protect)])
    for module in (auth, dashboard, participants, reminder_templates):
        router.include_router(module.router)
    return router
