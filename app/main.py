from __future__ import annotations

import csv
import configparser
import io
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Annotated

from fastapi import Depends, FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete, desc, func, select, text, update
from sqlalchemy.orm import Session, joinedload
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from app import config as app_cfg
from app.auth_utils import (
    email_codes_match,
    hash_email_code,
    hash_password,
    new_six_digit_code,
    verify_password,
)
from app.config import BASE_DIR, CONFIG_PATH, SECRET_KEY
from app.database import SessionLocal, engine, get_db
from app.deps import (
    RequireAdmin,
    RequireLogin,
    get_current_user_optional,
    require_admin,
    require_user,
    require_user_api,
    require_user_relaxed,
)
from app.formatting import format_date_user, format_money
from app.mailer import send_email
from app.models import (
    ActiveTimer,
    PasswordReset,
    PaymentType,
    Project,
    ProjectStatus,
    Subtask,
    Tag,
    Task,
    TaskPriority,
    TaskStatus,
    ThemePreference,
    TimeEntry,
    TimeSource,
    User,
)

from app.services.earnings import (
    earnings_for_period,
    earnings_today_week_month,
    entry_duration_seconds,
    entry_earnings_for_display,
    entry_hours,
    total_earned_for_completed_project,
)

VERIFY_CODE_MINUTES = 15
RESET_CODE_MINUTES = 60
VERIFY_RESEND_COOLDOWN_SEC = 55


@asynccontextmanager
async def lifespan(_: FastAPI):
    from app.database import Base, SessionLocal

    Base.metadata.create_all(bind=engine)
    if engine.dialect.name == "sqlite":
        with engine.begin() as conn:
            user_cols = [row[1] for row in conn.execute(text("PRAGMA table_info(users)")).fetchall()]
            if "is_admin" not in user_cols:
                conn.execute(text("ALTER TABLE users ADD COLUMN is_admin BOOLEAN NOT NULL DEFAULT 0"))
            if "is_blocked" not in user_cols:
                conn.execute(text("ALTER TABLE users ADD COLUMN is_blocked BOOLEAN NOT NULL DEFAULT 0"))
            cols = [row[1] for row in conn.execute(text("PRAGMA table_info(time_entries)")).fetchall()]
            if "duration_seconds" not in cols:
                conn.execute(text("ALTER TABLE time_entries ADD COLUMN duration_seconds INTEGER"))
    db = SessionLocal()
    try:
        db.execute(update(User).where(User.currency != "RUB").values(currency="RUB"))
        if app_cfg.SETTINGS.ADMIN_EMAIL:
            admin = db.execute(select(User).where(User.email == app_cfg.SETTINGS.ADMIN_EMAIL)).scalar_one_or_none()
            if not admin and app_cfg.SETTINGS.ADMIN_PASSWORD:
                admin = User(
                    email=app_cfg.SETTINGS.ADMIN_EMAIL,
                    hashed_password=hash_password(app_cfg.SETTINGS.ADMIN_PASSWORD),
                    email_verified=True,
                    verification_token=None,
                    verification_expires_at=None,
                    currency="RUB",
                    is_admin=True,
                    is_blocked=False,
                )
                db.add(admin)
            elif admin:
                admin.is_admin = True
                admin.is_blocked = False
        db.commit()
    finally:
        db.close()
    yield


app = FastAPI(title=app_cfg.SETTINGS.APP_NAME, lifespan=lifespan)

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.globals["format_money"] = format_money
templates.env.globals["format_date_user"] = format_date_user
templates.env.globals["settings"] = app_cfg.SETTINGS


def refresh_template_globals() -> None:
    templates.env.globals["settings"] = app_cfg.SETTINGS


MAINT_ALLOWED_PATHS = frozenset(
    {
        "/guide",
        "/privacy",
        "/terms",
        "/pending-verification",
        "/verify-email",
        "/resend-verification-code",
        "/reset-password",
        "/forgot-password",
        "/register",
    }
)


class MaintenanceMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not app_cfg.SETTINGS.MAINTENANCE:
            return await call_next(request)
        path = request.url.path
        if path.startswith("/static") or path.startswith("/admin"):
            return await call_next(request)
        if path in ("/login", "/logout"):
            return await call_next(request)
        if path in MAINT_ALLOWED_PATHS:
            return await call_next(request)
        user_id = request.session.get("user_id")
        if user_id:
            db = SessionLocal()
            try:
                u = db.get(User, user_id)
                if u and u.is_admin:
                    return await call_next(request)
            finally:
                db.close()
        return templates.TemplateResponse(
            "maintenance.html",
            {
                "request": request,
                "user": None,
                "flash": [],
                "title": "Технические работы",
                "maintenance_logged_in": bool(user_id),
            },
            status_code=503,
        )


app.add_middleware(MaintenanceMiddleware)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, session_cookie="frilans_session")


@app.exception_handler(RequireLogin)
async def login_redirect(_: Request, exc: RequireLogin):
    return RedirectResponse(exc.path, status_code=302)


@app.exception_handler(RequireAdmin)
async def admin_redirect(request: Request, exc: RequireAdmin):
    flash(request, "Доступ только для администратора.", "error")
    return RedirectResponse(exc.path, status_code=302)


app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


def flash(request: Request, message: str, category: str = "info"):
    request.session.setdefault("flash", []).append({"msg": message, "cat": category})


def pop_flash(request: Request) -> list:
    return request.session.pop("flash", [])


def absolute_url(request: Request, path: str) -> str:
    clean_path = path if path.startswith("/") else f"/{path}"
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("host")
    if host:
        return f"{proto}://{host}{clean_path}"
    return f"{app_cfg.SETTINGS.BASE_URL}{clean_path}"


def parse_decimal_input(raw: str, *, field_name: str, min_value: float | None = None) -> float | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        value = float(text.replace(",", "."))
    except ValueError:
        raise ValueError(f"Поле '{field_name}' должно быть числом.")
    if min_value is not None and value < min_value:
        raise ValueError(f"Поле '{field_name}' должно быть не меньше {min_value}.")
    return value


def parse_date_input(raw: str, *, field_name: str) -> date | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(f"Поле '{field_name}' имеет неверную дату.")


def owned_project_or_none(db: Session, user_id: int, project_id: int) -> Project | None:
    project = db.get(Project, project_id)
    if not project or project.user_id != user_id:
        return None
    return project


def owned_task_or_none(db: Session, user_id: int, task_id: int) -> Task | None:
    task = db.get(Task, task_id)
    if not task:
        return None
    project = db.get(Project, task.project_id)
    if not project or project.user_id != user_id:
        return None
    return task


def render_doc_page(
    request: Request,
    db: Session,
    *,
    title: str,
    body_template: str,
    nav: str = "",
) -> HTMLResponse:
    user = get_current_user_optional(request, db)
    flash_list = pop_flash(request)
    if user and user.email_verified:
        template_name = "doc_page_app.html"
    elif user and not user.email_verified:
        template_name = "doc_page_pending.html"
    else:
        template_name = "doc_page_auth.html"
    return templates.TemplateResponse(
        template_name,
        {
            "request": request,
            "user": user,
            "flash": flash_list,
            "title": title,
            "body_template": body_template,
            "nav": nav,
        },
    )


def plain_and_html_mail_code(title: str, intro: str, code: str, validity_line: str) -> tuple[str, str]:
    plain = f"{intro}\n\nКод: {code}\n{validity_line}"
    html = f"""
<!doctype html>
<html lang="ru">
  <body style="margin:0;padding:24px;background:#f5f3ee;font-family:Manrope,Arial,sans-serif;color:#2f2d29;">
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0">
      <tr>
        <td align="center">
          <table role="presentation" width="620" cellspacing="0" cellpadding="0" style="max-width:620px;background:#fff;border:1px solid #ded8cc;border-radius:14px;overflow:hidden;">
            <tr>
              <td style="padding:20px 24px;background:#ece6db;">
                <div style="font-size:18px;font-weight:700;">{app_cfg.SETTINGS.APP_NAME}</div>
              </td>
            </tr>
            <tr>
              <td style="padding:24px;">
                <h1 style="margin:0 0 12px;font-family:Spectral,Georgia,serif;font-size:26px;line-height:1.2;">{title}</h1>
                <p style="margin:0 0 18px;font-size:15px;line-height:1.6;color:#5f5a54;">{intro}</p>
                <p style="margin:0 0 8px;font-size:13px;color:#7b746b;">Код</p>
                <div style="font-size:32px;font-weight:700;letter-spacing:0.25em;font-family:ui-monospace,monospace;color:#2f2d29;">{code}</div>
                <p style="margin:16px 0 0;font-size:13px;color:#7b746b;">{validity_line}</p>
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>
""".strip()
    return plain, html


def persist_admin_config(
    base_url: str,
    debug: bool,
    maintenance: bool,
    host: str,
    port: int,
    username: str,
    password: str,
    from_email: str,
    use_tls: bool,
    use_ssl: bool,
    timeout_seconds: int,
) -> None:
    parser = configparser.ConfigParser()
    if CONFIG_PATH.exists():
        parser.read(CONFIG_PATH, encoding="utf-8")
    if "app" not in parser:
        parser["app"] = {}
    if "smtp" not in parser:
        parser["smtp"] = {}
    prev_pass = (parser["smtp"].get("password") or "").strip()
    pwd = (password or "").strip()
    if not pwd:
        pwd = prev_pass
    parser["app"]["base_url"] = (base_url or "").strip().rstrip("/") or app_cfg.SETTINGS.BASE_URL
    parser["app"]["debug"] = "true" if debug else "false"
    parser["app"]["maintenance"] = "true" if maintenance else "false"
    parser["smtp"]["host"] = host.strip()
    parser["smtp"]["port"] = str(port)
    parser["smtp"]["username"] = username.strip()
    parser["smtp"]["password"] = pwd
    parser["smtp"]["from_email"] = from_email.strip()
    parser["smtp"]["use_tls"] = "true" if use_tls else "false"
    parser["smtp"]["use_ssl"] = "true" if use_ssl else "false"
    parser["smtp"]["timeout_seconds"] = str(timeout_seconds)
    with open(CONFIG_PATH, "w", encoding="utf-8") as fp:
        parser.write(fp)


@app.get("/guide", response_class=HTMLResponse)
async def guide_page(request: Request, db: Annotated[Session, Depends(get_db)]):
    return render_doc_page(request, db, title="Как пользоваться", body_template="docs/guide_body.html", nav="guide")


@app.get("/privacy", response_class=HTMLResponse)
async def privacy_page(request: Request, db: Annotated[Session, Depends(get_db)]):
    return render_doc_page(request, db, title="Политика конфиденциальности", body_template="docs/privacy_body.html")


@app.get("/terms", response_class=HTMLResponse)
async def terms_page(request: Request, db: Annotated[Session, Depends(get_db)]):
    return render_doc_page(request, db, title="Правила сервиса", body_template="docs/terms_body.html")


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, db: Annotated[Session, Depends(get_db)]):
    if get_current_user_optional(request, db):
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "user": None, "flash": pop_flash(request), "title": "Вход"},
    )


@app.post("/login")
async def login_post(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
):
    user = db.execute(select(User).where(User.email == email.strip().lower())).scalar_one_or_none()
    if not user or not verify_password(password, user.hashed_password):
        flash(request, "Неверный email или пароль", "error")
        return RedirectResponse("/login", status_code=302)
    if user.is_blocked:
        flash(request, "Ваш аккаунт заблокирован администратором.", "error")
        return RedirectResponse("/login", status_code=302)
    if not user.email_verified:
        request.session["user_id"] = user.id
        return RedirectResponse("/pending-verification", status_code=302)
    request.session["user_id"] = user.id
    return RedirectResponse("/", status_code=302)


@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request, db: Annotated[Session, Depends(get_db)]):
    if get_current_user_optional(request, db):
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(
        "register.html",
        {"request": request, "user": None, "flash": pop_flash(request), "title": "Регистрация"},
    )


@app.post("/register")
async def register_post(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
):
    email = email.strip().lower()
    if db.execute(select(User).where(User.email == email)).scalar_one_or_none():
        flash(request, "Такой email уже зарегистрирован", "error")
        return RedirectResponse("/register", status_code=302)
    if len(password) < 8:
        flash(request, "Пароль не короче 8 символов", "error")
        return RedirectResponse("/register", status_code=302)

    user = User(
        email=email,
        hashed_password=hash_password(password),
        email_verified=False,
        verification_token=None,
        verification_expires_at=None,
    )
    db.add(user)
    db.flush()

    plain_code = new_six_digit_code()
    user.verification_token = hash_email_code(plain_code, user_id=user.id)
    user.verification_expires_at = datetime.utcnow() + timedelta(minutes=VERIFY_CODE_MINUTES)
    db.commit()

    request.session["user_id"] = user.id
    if app_cfg.SETTINGS.DEBUG:
        request.session["dev_email_code"] = plain_code

    validity = f"Действует {VERIFY_CODE_MINUTES} минут."
    mail_plain, mail_html = plain_and_html_mail_code(
        "Код подтверждения",
        "Введите этот код на странице подтверждения почты во Frilans.",
        plain_code,
        validity,
    )

    sent_ok = False
    mail_err = None
    if app_cfg.SETTINGS.SMTP_ENABLED:
        sent_ok, mail_err = send_email(
            subject=f"{app_cfg.SETTINGS.APP_NAME}: код подтверждения",
            to_email=user.email,
            plain_text=mail_plain,
            html_text=mail_html,
        )

    dbg = app_cfg.SETTINGS.DEBUG
    smtp_on = app_cfg.SETTINGS.SMTP_ENABLED
    if dbg:
        if sent_ok:
            flash(request, "Код ушёл на почту; дубликат есть на этой странице (режим отладки).", "success")
        elif smtp_on:
            flash(request, f"Письмо не отправилось ({mail_err}). Код показан ниже.", "info")
        else:
            flash(request, "Без SMTP код только на странице подтверждения.", "info")
    elif sent_ok:
        flash(request, "На почту отправлен код подтверждения.", "success")
    elif not smtp_on:
        flash(request, "Аккаунт создан, но почта на сервере не настроена — без кода не войти.", "error")
    else:
        flash(request, f"Код не отправился ({mail_err}). Запросите новый код позже.", "error")

    return RedirectResponse("/pending-verification", status_code=302)


@app.get("/pending-verification", response_class=HTMLResponse)
async def pending_verification(request: Request, db: Annotated[Session, Depends(get_db)]):
    user = require_user_relaxed(request, db)
    if user.email_verified:
        return RedirectResponse("/", status_code=302)
    dev_code = ""
    if app_cfg.SETTINGS.DEBUG:
        dev_code = request.session.get("dev_email_code") or ""
    return templates.TemplateResponse(
        "pending_verification.html",
        {
            "request": request,
            "user": user,
            "dev_email_code": dev_code,
            "flash": pop_flash(request),
            "title": "Подтверждение почты",
        },
    )


@app.post("/verify-email")
async def verify_email_post(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    code: Annotated[str, Form()],
):
    user = require_user_relaxed(request, db)
    if user.email_verified:
        return RedirectResponse("/", status_code=302)

    now = datetime.utcnow()
    if (
        not user.verification_token
        or not user.verification_expires_at
        or user.verification_expires_at < now
    ):
        flash(request, "Код просрочен — нажмите «Отправить снова».", "error")
        return RedirectResponse("/pending-verification", status_code=302)

    if not email_codes_match(code, user.verification_token, user_id=user.id):
        flash(request, "Код не подошёл.", "error")
        return RedirectResponse("/pending-verification", status_code=302)

    user.email_verified = True
    user.verification_token = None
    user.verification_expires_at = None
    db.commit()
    request.session.pop("dev_email_code", None)
    flash(request, "Почта подтверждена.", "success")
    return RedirectResponse("/", status_code=302)


@app.post("/resend-verification-code")
async def resend_verification_code(request: Request, db: Annotated[Session, Depends(get_db)]):
    user = require_user_relaxed(request, db)
    if user.email_verified:
        return RedirectResponse("/", status_code=302)

    since = float(request.session.get("verify_resend_at") or 0)
    if time.time() - since < VERIFY_RESEND_COOLDOWN_SEC:
        flash(request, "Чуть терпения — между письмами нужна пауза.", "info")
        return RedirectResponse("/pending-verification", status_code=302)

    plain_code = new_six_digit_code()
    user.verification_token = hash_email_code(plain_code, user_id=user.id)
    user.verification_expires_at = datetime.utcnow() + timedelta(minutes=VERIFY_CODE_MINUTES)
    db.commit()

    request.session["verify_resend_at"] = time.time()
    if app_cfg.SETTINGS.DEBUG:
        request.session["dev_email_code"] = plain_code

    validity = f"Действует {VERIFY_CODE_MINUTES} минут."
    mail_plain, mail_html = plain_and_html_mail_code(
        "Новый код подтверждения",
        "Вы запросили повторную отправку кода для Frilans.",
        plain_code,
        validity,
    )

    if app_cfg.SETTINGS.SMTP_ENABLED:
        send_email(
            subject=f"{app_cfg.SETTINGS.APP_NAME}: код подтверждения",
            to_email=user.email,
            plain_text=mail_plain,
            html_text=mail_html,
        )

    flash(request, "Отправили ещё одно письмо с кодом.", "success")
    return RedirectResponse("/pending-verification", status_code=302)


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=302)


@app.get("/forgot-password", response_class=HTMLResponse)
async def forgot_password_page(request: Request):
    return templates.TemplateResponse(
        "forgot_password.html",
        {"request": request, "user": None, "flash": pop_flash(request), "title": "Восстановление пароля"},
    )


@app.post("/forgot-password")
async def forgot_password_post(request: Request, db: Annotated[Session, Depends(get_db)], email: Annotated[str, Form()]):
    user = db.execute(select(User).where(User.email == email.strip().lower())).scalar_one_or_none()
    if not user:
        flash(request, "Если такой аккаунт есть, мы отправим код на почту.", "info")
        return RedirectResponse("/forgot-password", status_code=302)

    db.execute(delete(PasswordReset).where(PasswordReset.user_id == user.id))
    plain_code = new_six_digit_code()
    code_hash = hash_email_code(plain_code, user_id=user.id)
    pr = PasswordReset(
        user_id=user.id,
        token=code_hash,
        expires_at=datetime.utcnow() + timedelta(minutes=RESET_CODE_MINUTES),
    )
    db.add(pr)
    db.commit()

    if app_cfg.SETTINGS.DEBUG:
        request.session["dev_reset_code"] = plain_code
        request.session["dev_reset_email"] = user.email

    validity = f"Действует {RESET_CODE_MINUTES} минут."
    mail_plain, mail_html = plain_and_html_mail_code(
        "Код для сброса пароля",
        "Если вы не запрашивали сброс — просто удалите письмо.",
        plain_code,
        validity,
    )

    sent_ok = False
    mail_err = None
    if app_cfg.SETTINGS.SMTP_ENABLED:
        sent_ok, mail_err = send_email(
            subject=f"{app_cfg.SETTINGS.APP_NAME}: сброс пароля",
            to_email=user.email,
            plain_text=mail_plain,
            html_text=mail_html,
        )

    dbg = app_cfg.SETTINGS.DEBUG
    smtp_on = app_cfg.SETTINGS.SMTP_ENABLED
    if dbg:
        if sent_ok:
            flash(request, "Код на почте; при отладке он также на странице сброса.", "success")
        elif smtp_on:
            flash(request, f"Почта не ушла ({mail_err}). Код запомнили — введите на странице сброса.", "info")
        else:
            flash(request, "Без SMTP код только в сессии отладки — откройте форму сброса.", "info")
    elif sent_ok:
        flash(request, "Отправили код — введите его на странице сброса пароля.", "success")
    elif not smtp_on:
        flash(request, "Почта не настроена. Обратитесь к администратору.", "error")
    else:
        flash(request, f"Не удалось отправить код ({mail_err}).", "error")

    return RedirectResponse("/reset-password", status_code=302)


@app.get("/reset-password", response_class=HTMLResponse)
async def reset_password_page(request: Request):
    dev_code = ""
    dev_email = ""
    if app_cfg.SETTINGS.DEBUG:
        dev_code = request.session.get("dev_reset_code") or ""
        dev_email = request.session.get("dev_reset_email") or ""
    return templates.TemplateResponse(
        "reset_password.html",
        {
            "request": request,
            "user": None,
            "flash": pop_flash(request),
            "title": "Новый пароль",
            "dev_reset_code": dev_code,
            "dev_reset_email": dev_email,
        },
    )


@app.post("/reset-password")
async def reset_password_post(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    email: Annotated[str, Form()],
    code: Annotated[str, Form()],
    password: Annotated[str, Form()],
):
    email_norm = email.strip().lower()
    user = db.execute(select(User).where(User.email == email_norm)).scalar_one_or_none()
    if not user:
        flash(request, "Проверьте email и код.", "error")
        return RedirectResponse("/reset-password", status_code=302)

    pr = db.scalars(
        select(PasswordReset)
        .where(PasswordReset.user_id == user.id)
        .order_by(desc(PasswordReset.id))
        .limit(1)
    ).first()

    now = datetime.utcnow()
    if not pr or pr.expires_at < now:
        flash(request, "Код устарел — запросите сброс ещё раз.", "error")
        return RedirectResponse("/reset-password", status_code=302)

    if not email_codes_match(code, pr.token, user_id=user.id):
        flash(request, "Неверный код.", "error")
        return RedirectResponse("/reset-password", status_code=302)

    if len(password) < 8:
        flash(request, "Пароль не короче 8 символов.", "error")
        return RedirectResponse("/reset-password", status_code=302)

    user.hashed_password = hash_password(password)
    db.delete(pr)
    db.commit()

    request.session.pop("dev_reset_code", None)
    request.session.pop("dev_reset_email", None)

    flash(request, "Пароль обновлён — можно входить.", "success")
    return RedirectResponse("/login", status_code=302)


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, db: Annotated[Session, Depends(get_db)]):
    user = require_user(request, db)
    stats = earnings_today_week_month(db, user.id)
    timer_row = db.execute(select(ActiveTimer).where(ActiveTimer.user_id == user.id)).scalar_one_or_none()
    timer_task = None
    if timer_row:
        timer_task = db.get(Task, timer_row.task_id)
        if timer_task:
            timer_task = db.execute(
                select(Task).options(joinedload(Task.project)).where(Task.id == timer_task.id)
            ).scalar_one_or_none()
    open_tasks = (
        db.execute(
            select(Task)
            .join(Project)
            .where(
                Project.user_id == user.id,
                Task.status != TaskStatus.done.value,
                Project.status == ProjectStatus.active.value,
            )
            .order_by(
                Task.deadline.is_(None),
                Task.deadline,
                Task.priority.desc(),
            )
            .limit(5)
            .options(joinedload(Task.project))
        )
        .scalars()
        .all()
    )
    all_open = (
        db.execute(
            select(Task)
            .join(Project)
            .where(
                Project.user_id == user.id,
                Task.status != TaskStatus.done.value,
                Project.status == ProjectStatus.active.value,
            )
            .order_by(Project.name, Task.title)
            .options(joinedload(Task.project))
        )
        .scalars()
        .all()
    )
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "user": user,
            "stats": stats,
            "timer": timer_row,
            "timer_task": timer_task,
            "open_tasks": open_tasks,
            "quick_tasks": all_open,
            "flash": pop_flash(request),
            "title": "Главная",
            "nav": "home",
        },
    )


@app.get("/profile", response_class=HTMLResponse)
async def profile_page(request: Request, db: Annotated[Session, Depends(get_db)]):
    user = require_user(request, db)
    n_projects = db.scalar(select(func.count(Project.id)).where(Project.user_id == user.id)) or 0
    n_tasks = (
        db.scalar(
            select(func.count(Task.id))
            .join(Project, Task.project_id == Project.id)
            .where(Project.user_id == user.id)
        )
        or 0
    )
    return templates.TemplateResponse(
        "profile.html",
        {
            "request": request,
            "user": user,
            "n_projects": n_projects,
            "n_tasks": n_tasks,
            "flash": pop_flash(request),
            "title": "Профиль",
            "nav": "profile",
        },
    )


@app.get("/projects", response_class=HTMLResponse)
async def projects_list(request: Request, db: Annotated[Session, Depends(get_db)]):
    user = require_user(request, db)
    show_archived = request.query_params.get("archived") == "1"
    q = select(Project).where(Project.user_id == user.id)
    if not show_archived:
        q = q.where(Project.status != ProjectStatus.archived.value)
    projects = db.execute(q.order_by(Project.name)).scalars().all()
    return templates.TemplateResponse(
        "projects.html",
        {
            "request": request,
            "user": user,
            "projects": projects,
            "show_archived": show_archived,
            "flash": pop_flash(request),
            "title": "Проекты",
            "nav": "projects",
            "PaymentType": PaymentType,
            "ProjectStatus": ProjectStatus,
        },
    )


@app.get("/projects/{project_id}/edit", response_class=HTMLResponse)
async def project_edit_page(request: Request, project_id: int, db: Annotated[Session, Depends(get_db)]):
    user = require_user(request, db)
    p = owned_project_or_none(db, user.id, project_id)
    if not p:
        return RedirectResponse("/projects", status_code=302)
    return templates.TemplateResponse(
        "project_edit.html",
        {
            "request": request,
            "user": user,
            "p": p,
            "flash": pop_flash(request),
            "title": "Редактирование проекта",
            "nav": "projects",
            "PaymentType": PaymentType,
        },
    )


@app.post("/projects/new")
async def project_create(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    name: Annotated[str, Form()],
    description: Annotated[str, Form()] = "",
    payment_type: Annotated[str, Form()] = PaymentType.hourly.value,
    hourly_rate: Annotated[str, Form()] = "",
    fixed_amount: Annotated[str, Form()] = "",
    client_name: Annotated[str, Form()] = "",
):
    user = require_user(request, db)
    try:
        hr = parse_decimal_input(hourly_rate, field_name="Ставка / час", min_value=0) if payment_type == PaymentType.hourly.value else None
        fa = parse_decimal_input(fixed_amount, field_name="Фикс. сумма", min_value=0) if payment_type == PaymentType.fixed.value else None
    except ValueError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse("/projects", status_code=302)
    p = Project(
        user_id=user.id,
        name=name.strip(),
        description=description,
        payment_type=payment_type,
        hourly_rate=hr,
        fixed_amount=fa,
        client_name=client_name.strip(),
        status=ProjectStatus.active.value,
    )
    db.add(p)
    db.commit()
    flash(request, "Проект создан", "success")
    return RedirectResponse("/projects", status_code=302)


@app.post("/projects/{project_id}/edit")
async def project_edit(
    request: Request,
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    name: Annotated[str, Form()],
    description: Annotated[str, Form()] = "",
    payment_type: Annotated[str, Form()] = PaymentType.hourly.value,
    hourly_rate: Annotated[str, Form()] = "",
    fixed_amount: Annotated[str, Form()] = "",
    client_name: Annotated[str, Form()] = "",
):
    user = require_user(request, db)
    p = owned_project_or_none(db, user.id, project_id)
    if not p:
        return RedirectResponse("/projects", status_code=302)
    try:
        hr = parse_decimal_input(hourly_rate, field_name="Ставка / час", min_value=0) if payment_type == PaymentType.hourly.value else None
        fa = parse_decimal_input(fixed_amount, field_name="Фикс. сумма", min_value=0) if payment_type == PaymentType.fixed.value else None
    except ValueError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse(f"/projects/{project_id}/edit", status_code=302)
    p.name = name.strip()
    p.description = description
    p.payment_type = payment_type
    p.hourly_rate = hr
    p.fixed_amount = fa
    p.client_name = client_name.strip()
    db.commit()
    flash(request, "Сохранено", "success")
    return RedirectResponse("/projects", status_code=302)


@app.post("/projects/{project_id}/delete")
async def project_delete(request: Request, project_id: int, db: Annotated[Session, Depends(get_db)]):
    user = require_user(request, db)
    p = owned_project_or_none(db, user.id, project_id)
    if not p:
        return RedirectResponse("/projects", status_code=302)
    task_ids = db.execute(select(Task.id).where(Task.project_id == p.id)).scalars().all()
    if task_ids:
        cnt = db.scalar(
            select(func.count(TimeEntry.id)).where(TimeEntry.task_id.in_(task_ids))
        )
        if cnt and cnt > 0:
            flash(request, "Нельзя удалить: есть записи времени", "error")
            return RedirectResponse("/projects", status_code=302)
    db.delete(p)
    db.commit()
    flash(request, "Проект удалён", "success")
    return RedirectResponse("/projects", status_code=302)


@app.post("/projects/{project_id}/complete")
async def project_complete(request: Request, project_id: int, db: Annotated[Session, Depends(get_db)]):
    user = require_user(request, db)
    p = owned_project_or_none(db, user.id, project_id)
    if not p:
        return RedirectResponse("/projects", status_code=302)
    p.status = ProjectStatus.completed.value
    p.completed_at = datetime.utcnow()
    p.earned_total_snapshot = float(total_earned_for_completed_project(p, db))
    db.commit()
    flash(request, "Проект завершён", "success")
    return RedirectResponse("/projects", status_code=302)


@app.post("/projects/{project_id}/archive")
async def project_archive(request: Request, project_id: int, db: Annotated[Session, Depends(get_db)]):
    user = require_user(request, db)
    p = owned_project_or_none(db, user.id, project_id)
    if not p:
        return RedirectResponse("/projects", status_code=302)
    p.status = ProjectStatus.archived.value
    db.commit()
    flash(request, "В архиве", "success")
    return RedirectResponse("/projects", status_code=302)


@app.get("/tasks", response_class=HTMLResponse)
async def tasks_list(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    project_id: Annotated[int | None, Query()] = None,
    status_f: Annotated[str | None, Query(alias="status")] = None,
    priority: Annotated[str | None, Query()] = None,
):
    user = require_user(request, db)
    q = (
        select(Task)
        .join(Project)
        .where(Project.user_id == user.id)
        .options(joinedload(Task.project), joinedload(Task.tags), joinedload(Task.subtasks))
    )
    if project_id:
        q = q.where(Task.project_id == project_id)
    if status_f:
        q = q.where(Task.status == status_f)
    if priority:
        q = q.where(Task.priority == priority)
    tasks = db.execute(q.order_by(Task.deadline.is_(None), Task.deadline, Task.title)).scalars().unique().all()
    projects = db.execute(select(Project).where(Project.user_id == user.id).order_by(Project.name)).scalars().all()
    all_tags = db.execute(select(Tag).where(Tag.user_id == user.id).order_by(Tag.name)).scalars().all()
    return templates.TemplateResponse(
        "tasks.html",
        {
            "request": request,
            "user": user,
            "tasks": tasks,
            "projects": projects,
            "all_tags": all_tags,
            "filter_project": project_id,
            "filter_status": status_f,
            "filter_priority": priority,
            "flash": pop_flash(request),
            "title": "Задачи",
            "nav": "tasks",
            "TaskStatus": TaskStatus,
            "TaskPriority": TaskPriority,
        },
    )


def _sync_task_tags(db: Session, task: Task, user: User, tags_csv: str):
    if not tags_csv.strip():
        task.tags.clear()
        return
    names = [t.strip().lower() for t in tags_csv.split(",") if t.strip()]
    task.tags.clear()
    for name in names:
        tag = db.execute(select(Tag).where(Tag.user_id == user.id, Tag.name == name)).scalar_one_or_none()
        if not tag:
            tag = Tag(user_id=user.id, name=name)
            db.add(tag)
            db.flush()
        task.tags.append(tag)


@app.post("/tasks/new")
async def task_new(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    project_id: Annotated[int, Form()],
    title: Annotated[str, Form()],
    status: Annotated[str, Form()] = TaskStatus.open.value,
    priority: Annotated[str, Form()] = TaskPriority.medium.value,
    deadline: Annotated[str, Form()] = "",
    estimated_hours: Annotated[str, Form()] = "",
    tags: Annotated[str, Form()] = "",
):
    user = require_user(request, db)
    p = owned_project_or_none(db, user.id, project_id)
    if not p:
        return RedirectResponse("/tasks", status_code=302)
    try:
        dl = parse_date_input(deadline, field_name="Дедлайн")
        est = parse_decimal_input(estimated_hours, field_name="Оценка, ч", min_value=0)
    except ValueError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse("/tasks", status_code=302)
    t = Task(
        project_id=p.id,
        title=title.strip(),
        status=status,
        priority=priority,
        deadline=dl,
        estimated_hours=est,
    )
    db.add(t)
    db.flush()
    _sync_task_tags(db, t, user, tags)
    db.commit()
    flash(request, "Задача создана", "success")
    return RedirectResponse("/tasks", status_code=302)


@app.post("/tasks/{task_id}/edit")
async def task_edit(
    request: Request,
    task_id: int,
    db: Annotated[Session, Depends(get_db)],
    title: Annotated[str, Form()],
    status: Annotated[str, Form()] = TaskStatus.open.value,
    priority: Annotated[str, Form()] = TaskPriority.medium.value,
    deadline: Annotated[str, Form()] = "",
    estimated_hours: Annotated[str, Form()] = "",
    tags: Annotated[str, Form()] = "",
):
    user = require_user(request, db)
    t = owned_task_or_none(db, user.id, task_id)
    if not t:
        return RedirectResponse("/tasks", status_code=302)
    try:
        parsed_deadline = parse_date_input(deadline, field_name="Дедлайн")
        parsed_est = parse_decimal_input(estimated_hours, field_name="Оценка, ч", min_value=0)
    except ValueError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse("/tasks", status_code=302)
    t.title = title.strip()
    t.status = status
    t.priority = priority
    t.deadline = parsed_deadline
    t.estimated_hours = parsed_est
    _sync_task_tags(db, t, user, tags)
    db.commit()
    flash(request, "Сохранено", "success")
    return RedirectResponse("/tasks", status_code=302)


@app.post("/tasks/{task_id}/delete")
async def task_delete(request: Request, task_id: int, db: Annotated[Session, Depends(get_db)]):
    user = require_user(request, db)
    t = owned_task_or_none(db, user.id, task_id)
    if not t:
        return RedirectResponse("/tasks", status_code=302)
    db.delete(t)
    db.commit()
    flash(request, "Задача удалена", "success")
    return RedirectResponse("/tasks", status_code=302)


@app.post("/tasks/{task_id}/subtask")
async def subtask_add(
    request: Request,
    task_id: int,
    db: Annotated[Session, Depends(get_db)],
    title: Annotated[str, Form()],
):
    user = require_user(request, db)
    t = owned_task_or_none(db, user.id, task_id)
    if not t:
        return RedirectResponse("/tasks", status_code=302)
    db.add(Subtask(task_id=t.id, title=title.strip()))
    db.commit()
    return RedirectResponse("/tasks", status_code=302)


@app.post("/subtasks/{subtask_id}/toggle")
async def subtask_toggle(request: Request, subtask_id: int, db: Annotated[Session, Depends(get_db)]):
    user = require_user(request, db)
    st = db.get(Subtask, subtask_id)
    if not st:
        return RedirectResponse("/tasks", status_code=302)
    task = db.get(Task, st.task_id)
    p = db.get(Project, task.project_id)
    if p.user_id != user.id:
        return RedirectResponse("/tasks", status_code=302)
    st.done = not st.done
    db.commit()
    return RedirectResponse("/tasks", status_code=302)


@app.get("/time", response_class=HTMLResponse)
async def time_page(request: Request, db: Annotated[Session, Depends(get_db)]):
    user = require_user(request, db)
    entries = (
        db.execute(
            select(TimeEntry)
            .join(Task)
            .join(Project)
            .where(Project.user_id == user.id)
            .order_by(TimeEntry.work_date.desc(), TimeEntry.id.desc())
            .options(joinedload(TimeEntry.task).joinedload(Task.project))
            .limit(200)
        )
        .scalars()
        .unique()
        .all()
    )
    tasks = (
        db.execute(
            select(Task)
            .join(Project)
            .where(Project.user_id == user.id, Project.status == ProjectStatus.active.value)
            .order_by(Task.title)
            .options(joinedload(Task.project))
        )
        .scalars()
        .unique()
        .all()
    )
    return templates.TemplateResponse(
        "time.html",
        {
            "request": request,
            "user": user,
            "entries": entries,
            "tasks": tasks,
            "flash": pop_flash(request),
            "title": "Учёт времени",
            "nav": "time",
            "TimeSource": TimeSource,
            "today": date.today(),
        },
    )


@app.post("/time/manual")
async def time_manual(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    task_id: Annotated[int, Form()],
    work_date: Annotated[str, Form()],
    duration_minutes: Annotated[int, Form()],
    comment: Annotated[str, Form()] = "",
):
    user = require_user(request, db)
    t = db.get(Task, task_id)
    if not t:
        return RedirectResponse("/time", status_code=302)
    p = db.get(Project, t.project_id)
    if p.user_id != user.id:
        return RedirectResponse("/time", status_code=302)
    wd = datetime.strptime(work_date.strip(), "%Y-%m-%d").date()
    e = TimeEntry(
        task_id=t.id,
        work_date=wd,
        duration_minutes=max(1, duration_minutes),
        duration_seconds=max(1, duration_minutes) * 60,
        source=TimeSource.manual.value,
        comment=comment,
    )
    db.add(e)
    db.commit()
    flash(request, "Запись добавлена", "success")
    return RedirectResponse("/time", status_code=302)


@app.post("/time/{entry_id}/edit")
async def time_edit(
    request: Request,
    entry_id: int,
    db: Annotated[Session, Depends(get_db)],
    work_date: Annotated[str, Form()],
    duration_minutes: Annotated[int, Form()],
    comment: Annotated[str, Form()] = "",
):
    user = require_user(request, db)
    e = db.get(TimeEntry, entry_id)
    if not e:
        return RedirectResponse("/time", status_code=302)
    t = db.get(Task, e.task_id)
    p = db.get(Project, t.project_id)
    if p.user_id != user.id:
        return RedirectResponse("/time", status_code=302)
    e.work_date = datetime.strptime(work_date.strip(), "%Y-%m-%d").date()
    e.duration_minutes = max(1, duration_minutes)
    e.duration_seconds = max(1, duration_minutes) * 60
    e.comment = comment
    db.commit()
    flash(request, "Обновлено", "success")
    return RedirectResponse("/time", status_code=302)


@app.post("/time/{entry_id}/delete")
async def time_delete(request: Request, entry_id: int, db: Annotated[Session, Depends(get_db)]):
    user = require_user(request, db)
    e = db.get(TimeEntry, entry_id)
    if not e:
        return RedirectResponse("/time", status_code=302)
    t = db.get(Task, e.task_id)
    p = db.get(Project, t.project_id)
    if p.user_id != user.id:
        return RedirectResponse("/time", status_code=302)
    db.delete(e)
    db.commit()
    flash(request, "Удалено", "success")
    return RedirectResponse("/time", status_code=302)


@app.get("/api/timer")
async def api_timer_status(request: Request, db: Annotated[Session, Depends(get_db)]):
    user = require_user_api(request, db)
    row = db.execute(select(ActiveTimer).where(ActiveTimer.user_id == user.id)).scalar_one_or_none()
    if not row:
        return {"active": False}
    t = db.execute(select(Task).options(joinedload(Task.project)).where(Task.id == row.task_id)).scalar_one_or_none()
    return {
        "active": True,
        "task_id": row.task_id,
        "task_title": t.title if t else "",
        "project_name": t.project.name if t and t.project else "",
        "started_at": row.started_at.isoformat() + "Z",
    }


@app.post("/api/timer/start")
async def api_timer_start(request: Request, db: Annotated[Session, Depends(get_db)], task_id: Annotated[str, Form()] = ""):
    user = require_user_api(request, db)
    raw = (task_id or "").strip()
    if not raw:
        return JSONResponse(
            {"ok": False, "error": "no_task_id", "message": "Выберите задачу в списке."},
            status_code=400,
        )
    try:
        tid = int(raw)
    except ValueError:
        return JSONResponse(
            {"ok": False, "error": "invalid", "message": "Некорректный номер задачи."},
            status_code=400,
        )
    existing = db.execute(select(ActiveTimer).where(ActiveTimer.user_id == user.id)).scalar_one_or_none()
    if existing:
        return JSONResponse(
            {"ok": False, "error": "already_running", "message": "Сначала остановите текущий таймер."},
            status_code=400,
        )
    t = db.get(Task, tid)
    if not t:
        return JSONResponse({"ok": False, "error": "no_task", "message": "Задача не найдена."}, status_code=404)
    p = db.get(Project, t.project_id)
    if p.user_id != user.id or p.status != ProjectStatus.active.value:
        return JSONResponse(
            {"ok": False, "error": "forbidden", "message": "Проект неактивен или чужая задача."},
            status_code=403,
        )
    db.merge(ActiveTimer(user_id=user.id, task_id=tid, started_at=datetime.utcnow()))
    db.commit()
    return {"ok": True, "message": ""}


@app.post("/api/timer/stop")
async def api_timer_stop(request: Request, db: Annotated[Session, Depends(get_db)]):
    user = require_user_api(request, db)
    row = db.execute(select(ActiveTimer).where(ActiveTimer.user_id == user.id)).scalar_one_or_none()
    if not row:
        return JSONResponse({"ok": False, "error": "no_timer"}, status_code=400)
    end = datetime.utcnow()
    delta = end - row.started_at
    seconds = max(1, int(delta.total_seconds()))
    minutes = max(1, int(round(seconds / 60)))
    e = TimeEntry(
        task_id=row.task_id,
        work_date=end.date(),
        duration_minutes=minutes,
        duration_seconds=seconds,
        source=TimeSource.timer.value,
        comment="",
    )
    db.add(e)
    db.delete(row)
    db.commit()
    return {"ok": True, "minutes": minutes, "seconds": seconds}


@app.get("/reports", response_class=HTMLResponse)
async def reports_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    date_from: Annotated[date | None, Query(alias="from")] = None,
    date_to: Annotated[date | None, Query(alias="to")] = None,
    project_ids: Annotated[list[int] | None, Query()] = None,
    tag_ids: Annotated[list[int] | None, Query()] = None,
):
    user = require_user(request, db)
    today = date.today()
    if not date_from:
        date_from = today - timedelta(days=30)
    if not date_to:
        date_to = today

    entries = (
        db.execute(
            select(TimeEntry)
            .join(Task)
            .join(Project)
            .where(
                Project.user_id == user.id,
                TimeEntry.work_date >= date_from,
                TimeEntry.work_date <= date_to,
            )
            .options(joinedload(TimeEntry.task).joinedload(Task.project), joinedload(TimeEntry.task).joinedload(Task.tags))
        )
        .scalars()
        .unique()
        .all()
    )
    if project_ids:
        entries = [e for e in entries if e.task.project_id in project_ids]
    if tag_ids:
        entries = [e for e in entries if any(tg.id in tag_ids for tg in e.task.tags)]

    rows = []
    total = Decimal("0")
    for e in entries:
        proj = e.task.project
        amt = entry_earnings_for_display(proj, entry_duration_seconds(e))
        hours = float(entry_hours(e))
        rows.append(
            {
                "project": proj.name,
                "task": e.task.title,
                "hours": hours,
                "amount": amt,
                "payment_type": proj.payment_type,
            }
        )
        total += amt

    fixed_projects = (
        db.execute(
            select(Project).where(
                Project.user_id == user.id,
                Project.payment_type == PaymentType.fixed.value,
                Project.status == ProjectStatus.completed.value,
                Project.completed_at.isnot(None),
            )
        )
        .scalars()
        .all()
    )
    for p in fixed_projects:
        if not p.completed_at:
            continue
        cday = p.completed_at.date()
        if date_from <= cday <= date_to:
            if project_ids and p.id not in project_ids:
                continue
            amt = Decimal(str(p.fixed_amount or 0))
            rows.append(
                {
                    "project": p.name,
                    "task": "(фикс. проект)",
                    "hours": None,
                    "amount": amt,
                    "payment_type": PaymentType.fixed.value,
                }
            )
            total += amt

    by_weekday: dict[int, Decimal] = defaultdict(lambda: Decimal("0"))
    _, daily = earnings_for_period(db, user.id, date_from, date_to)
    for d, v in daily.items():
        by_weekday[d.weekday()] += v

    projects = db.execute(select(Project).where(Project.user_id == user.id).order_by(Project.name)).scalars().all()
    tags = db.execute(select(Tag).where(Tag.user_id == user.id).order_by(Tag.name)).scalars().all()

    plan_fact = []
    for t in (
        db.execute(
            select(Task)
            .join(Project)
            .where(Project.user_id == user.id)
            .options(joinedload(Task.project))
        )
        .scalars()
        .unique()
        .all()
    ):
        if not t.estimated_hours:
            continue
        spent = db.scalar(select(func.coalesce(func.sum(TimeEntry.duration_minutes), 0)).where(TimeEntry.task_id == t.id)) or 0
        plan_fact.append(
            {
                "task": t.title,
                "project": t.project.name,
                "planned": t.estimated_hours,
                "actual_hours": spent / 60.0,
            }
        )

    wd_vals = list(by_weekday.values())
    weekday_bar_max = max(wd_vals) if wd_vals else Decimal("0")

    return templates.TemplateResponse(
        "reports.html",
        {
            "request": request,
            "user": user,
            "rows": rows,
            "total": total,
            "date_from": date_from,
            "date_to": date_to,
            "project_ids": project_ids or [],
            "tag_ids": tag_ids or [],
            "projects": projects,
            "tags": tags,
            "by_weekday": dict(by_weekday),
            "weekday_bar_max": weekday_bar_max,
            "plan_fact": plan_fact[:30],
            "flash": pop_flash(request),
            "title": "Отчёты",
            "nav": "reports",
            "PaymentType": PaymentType,
        },
    )


@app.get("/reports/export.csv")
async def reports_export(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    date_from: Annotated[date | None, Query(alias="from")] = None,
    date_to: Annotated[date | None, Query(alias="to")] = None,
):
    user = require_user(request, db)
    today = date.today()
    if not date_from:
        date_from = today - timedelta(days=30)
    if not date_to:
        date_to = today
    entries = (
        db.execute(
            select(TimeEntry)
            .join(Task)
            .join(Project)
            .where(
                Project.user_id == user.id,
                TimeEntry.work_date >= date_from,
                TimeEntry.work_date <= date_to,
            )
            .options(joinedload(TimeEntry.task).joinedload(Task.project))
        )
        .scalars()
        .unique()
        .all()
    )
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Проект", "Задача", "Часы", "Сумма"])
    for e in entries:
        proj = e.task.project
        amt = entry_earnings_for_display(proj, entry_duration_seconds(e))
        w.writerow([proj.name, e.task.title, round(float(entry_hours(e)), 4), str(amt)])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="frilans-report-{date_from}-{date_to}.csv"'},
    )


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, db: Annotated[Session, Depends(get_db)]):
    user = require_user(request, db)
    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "user": user,
            "flash": pop_flash(request),
            "title": "Настройки",
            "nav": "settings",
            "ThemePreference": ThemePreference,
        },
    )


@app.post("/settings/theme-quick")
async def settings_theme_quick(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    theme: Annotated[str, Form()],
):
    user = require_user(request, db)
    if theme in (t.value for t in ThemePreference):
        user.theme = theme
        db.commit()
    return JSONResponse({"ok": True})


@app.post("/settings/profile")
async def settings_profile(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    date_format: Annotated[str, Form()] = "DD.MM.YYYY",
    theme: Annotated[str, Form()] = ThemePreference.system.value,
):
    user = require_user(request, db)
    user.currency = "RUB"
    user.date_format = date_format if date_format in ("DD.MM.YYYY", "MM/DD/YYYY") else "DD.MM.YYYY"
    user.theme = theme if theme in (t.value for t in ThemePreference) else ThemePreference.system.value
    db.commit()
    flash(request, "Настройки сохранены", "success")
    return RedirectResponse("/settings", status_code=302)


@app.post("/settings/password")
async def settings_password(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    current_password: Annotated[str, Form()],
    new_password: Annotated[str, Form()],
):
    user = require_user(request, db)
    if not verify_password(current_password, user.hashed_password):
        flash(request, "Текущий пароль неверен", "error")
        return RedirectResponse("/settings", status_code=302)
    if len(new_password) < 8:
        flash(request, "Новый пароль не короче 8 символов", "error")
        return RedirectResponse("/settings", status_code=302)
    user.hashed_password = hash_password(new_password)
    db.commit()
    flash(request, "Пароль обновлён", "success")
    return RedirectResponse("/settings", status_code=302)


@app.post("/settings/delete-account")
async def settings_delete(request: Request, db: Annotated[Session, Depends(get_db)], confirm: Annotated[str, Form()] = ""):
    user = require_user(request, db)
    if confirm.strip().upper() != "DELETE":
        flash(request, "Введите DELETE для подтверждения", "error")
        return RedirectResponse("/settings", status_code=302)
    db.delete(user)
    db.commit()
    request.session.clear()
    return RedirectResponse("/login", status_code=302)


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request, db: Annotated[Session, Depends(get_db)]):
    admin = require_admin(request, db)
    users = db.execute(select(User).order_by(User.created_at.desc())).scalars().all()
    projects_count = db.scalar(select(func.count(Project.id))) or 0
    tasks_count = db.scalar(select(func.count(Task.id))) or 0
    time_entries_count = db.scalar(select(func.count(TimeEntry.id))) or 0
    active_timers_count = db.scalar(select(func.count(ActiveTimer.user_id))) or 0
    user_cards = []
    for u in users:
        p_count = db.scalar(select(func.count(Project.id)).where(Project.user_id == u.id)) or 0
        t_count = db.scalar(
            select(func.count(Task.id)).where(Task.project_id.in_(select(Project.id).where(Project.user_id == u.id)))
        ) or 0
        user_cards.append({"user": u, "projects": p_count, "tasks": t_count})
    return templates.TemplateResponse(
        "admin.html",
        {
            "request": request,
            "user": admin,
            "flash": pop_flash(request),
            "title": "Админ-панель",
            "nav": "admin",
            "users": user_cards,
            "projects_count": projects_count,
            "tasks_count": tasks_count,
            "time_entries_count": time_entries_count,
            "active_timers_count": active_timers_count,
            "settings": app_cfg.SETTINGS,
        },
    )


@app.post("/admin/user/{user_id}/toggle-block")
async def admin_toggle_block(request: Request, user_id: int, db: Annotated[Session, Depends(get_db)]):
    admin = require_admin(request, db)
    target = db.get(User, user_id)
    if not target:
        flash(request, "Пользователь не найден.", "error")
        return RedirectResponse("/admin", status_code=302)
    if target.id == admin.id:
        flash(request, "Нельзя заблокировать самого себя.", "error")
        return RedirectResponse("/admin", status_code=302)
    target.is_blocked = not target.is_blocked
    db.commit()
    flash(request, "Статус блокировки обновлён.", "success")
    return RedirectResponse("/admin", status_code=302)


@app.post("/admin/config")
async def admin_save_config(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    base_url: Annotated[str, Form()] = "",
    debug: Annotated[str | None, Form()] = None,
    maintenance: Annotated[str | None, Form()] = None,
    host: Annotated[str, Form()] = "",
    port: Annotated[int, Form()] = 587,
    username: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    from_email: Annotated[str, Form()] = "",
    use_tls: Annotated[str | None, Form()] = None,
    use_ssl: Annotated[str | None, Form()] = None,
    timeout_seconds: Annotated[int, Form()] = 15,
):
    require_admin(request, db)
    persist_admin_config(
        base_url=base_url,
        debug=debug in ("true", "on", "1"),
        maintenance=maintenance in ("true", "on", "1"),
        host=host,
        port=max(1, port),
        username=username,
        password=password,
        from_email=from_email,
        use_tls=use_tls in ("true", "on", "1"),
        use_ssl=use_ssl in ("true", "on", "1"),
        timeout_seconds=max(5, timeout_seconds),
    )
    app_cfg.reload_settings()
    refresh_template_globals()
    flash(request, "Настройки сохранены в config.ini и применены без перезапуска.", "success")
    return RedirectResponse("/admin", status_code=302)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=True)
