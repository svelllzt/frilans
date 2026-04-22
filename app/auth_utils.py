import hashlib
import hmac
import secrets

from passlib.context import CryptContext

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def new_token() -> str:
    return secrets.token_urlsafe(32)


def new_six_digit_code() -> str:
    return f"{secrets.randbelow(900000) + 100000}"


def _secret_bytes() -> bytes:
    from app.config import SECRET_KEY

    return SECRET_KEY.encode("utf-8")


def hash_email_code(code: str, *, user_id: int) -> str:
    digits = "".join(c for c in (code or "").strip() if c.isdigit())
    payload = f"{user_id}:{digits}" if len(digits) == 6 else f"{user_id}:__bad__"
    return hmac.new(_secret_bytes(), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def email_codes_match(entered: str, stored_hex: str | None, *, user_id: int) -> bool:
    if not stored_hex:
        return False
    return hmac.compare_digest(hash_email_code(entered, user_id=user_id), stored_hex)
