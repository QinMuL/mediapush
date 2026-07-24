"""登录 / 登出路由。"""
from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from app.web.auth import SESSION_COOKIE, create_session_token, verify_password
from app.web.templates import templates

router = APIRouter()


@router.get("/login")
async def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login")
async def login_submit(
    request: Request,
    password: str = Form(...),
):
    container = request.app.state.container
    ok = await verify_password(container.session_factory, password)
    if not ok:
        return templates.TemplateResponse(
            request, "login.html", {"error": "密码错误"}, status_code=401
        )
    token = await create_session_token(
        container.session_factory, {"authed": True}
    )
    resp = RedirectResponse("/dashboard", status_code=303)
    resp.set_cookie(
        SESSION_COOKIE, token, max_age=7 * 24 * 3600, httponly=True, samesite="lax"
    )
    return resp


@router.post("/logout")
async def logout(request: Request):
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp
