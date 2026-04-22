from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User


class RequireLogin(Exception):
    def __init__(self, path: str = "/login"):
        self.path = path


class RequireAdmin(Exception):
    def __init__(self, path: str = "/"):
        self.path = path


def get_current_user_optional(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> User | None:
    uid = request.session.get("user_id")
    if not uid:
        return None
    return db.get(User, int(uid))


def require_user(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> User:
    user = get_current_user_optional(request, db)
    if not user:
        raise RequireLogin("/login")
    if user.is_blocked:
        raise RequireLogin("/login")
    if not user.email_verified:
        raise RequireLogin("/pending-verification")
    return user


def require_user_relaxed(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> User:
    user = get_current_user_optional(request, db)
    if not user:
        raise RequireLogin("/login")
    if user.is_blocked:
        raise RequireLogin("/login")
    return user


def require_user_api(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> User:
    user = get_current_user_optional(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="unauthorized")
    if user.is_blocked:
        raise HTTPException(status_code=403, detail="blocked")
    if not user.email_verified:
        raise HTTPException(status_code=403, detail="email_not_verified")
    return user


def require_admin(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> User:
    user = require_user(request, db)
    if not user.is_admin:
        raise RequireAdmin("/")
    return user
