from __future__ import annotations

import configparser
import os
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config.ini"


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(value: str | None, default: int) -> int:
    try:
        return int((value or "").strip())
    except ValueError:
        return default


@dataclass
class Settings:
    APP_NAME: str
    DEBUG: bool
    MAINTENANCE: bool
    SECRET_KEY: str
    BASE_URL: str
    DATABASE_URL: str
    SMTP_HOST: str
    SMTP_PORT: int
    SMTP_USERNAME: str
    SMTP_PASSWORD: str
    SMTP_FROM_EMAIL: str
    SMTP_USE_TLS: bool
    SMTP_USE_SSL: bool
    SMTP_TIMEOUT_SECONDS: int
    ADMIN_EMAIL: str
    ADMIN_PASSWORD: str

    @property
    def SMTP_ENABLED(self) -> bool:
        return bool(self.SMTP_HOST and self.SMTP_FROM_EMAIL)


def load_settings() -> Settings:
    parser = configparser.ConfigParser()
    if CONFIG_PATH.exists():
        parser.read(CONFIG_PATH, encoding="utf-8")

    app = parser["app"] if "app" in parser else {}
    smtp = parser["smtp"] if "smtp" in parser else {}

    app_name = os.environ.get("FRILANS_APP_NAME", app.get("name", "Frilans"))
    debug = _bool(os.environ.get("FRILANS_DEBUG", app.get("debug")), default=True)
    maintenance = _bool(os.environ.get("FRILANS_MAINTENANCE", app.get("maintenance")), default=False)
    secret_key = os.environ.get(
        "FRILANS_SECRET_KEY",
        app.get("secret_key", "dev-change-me-in-production-use-long-random-string"),
    )
    base_url = os.environ.get("FRILANS_BASE_URL", app.get("base_url", "http://127.0.0.1:8000")).rstrip("/")
    database_url = os.environ.get("FRILANS_DATABASE_URL", app.get("database_url", f"sqlite:///{BASE_DIR / 'frilans.db'}"))

    smtp_host = os.environ.get("FRILANS_SMTP_HOST", smtp.get("host", "")).strip()
    smtp_port = _int(os.environ.get("FRILANS_SMTP_PORT", smtp.get("port")), 587)
    smtp_username = os.environ.get("FRILANS_SMTP_USERNAME", smtp.get("username", "")).strip()
    smtp_password = os.environ.get("FRILANS_SMTP_PASSWORD", smtp.get("password", "")).strip()
    smtp_from_email = os.environ.get("FRILANS_SMTP_FROM", smtp.get("from_email", "")).strip()
    smtp_use_tls = _bool(os.environ.get("FRILANS_SMTP_USE_TLS", smtp.get("use_tls")), default=True)
    smtp_use_ssl = _bool(os.environ.get("FRILANS_SMTP_USE_SSL", smtp.get("use_ssl")), default=False)
    smtp_timeout = _int(os.environ.get("FRILANS_SMTP_TIMEOUT", smtp.get("timeout_seconds")), 15)
    admin_email = os.environ.get("FRILANS_ADMIN_EMAIL", app.get("admin_email", "")).strip().lower()
    admin_password = os.environ.get("FRILANS_ADMIN_PASSWORD", app.get("admin_password", "")).strip()

    return Settings(
        APP_NAME=app_name,
        DEBUG=debug,
        MAINTENANCE=maintenance,
        SECRET_KEY=secret_key,
        BASE_URL=base_url,
        DATABASE_URL=database_url,
        SMTP_HOST=smtp_host,
        SMTP_PORT=smtp_port,
        SMTP_USERNAME=smtp_username,
        SMTP_PASSWORD=smtp_password,
        SMTP_FROM_EMAIL=smtp_from_email,
        SMTP_USE_TLS=smtp_use_tls,
        SMTP_USE_SSL=smtp_use_ssl,
        SMTP_TIMEOUT_SECONDS=smtp_timeout,
        ADMIN_EMAIL=admin_email,
        ADMIN_PASSWORD=admin_password,
    )


SETTINGS = load_settings()
SECRET_KEY = SETTINGS.SECRET_KEY
DATABASE_URL = SETTINGS.DATABASE_URL


def reload_settings() -> Settings:
    """Перечитать config.ini и обновить глобальные значения (без перезапуска процесса)."""
    global SETTINGS, SECRET_KEY, DATABASE_URL
    SETTINGS = load_settings()
    SECRET_KEY = SETTINGS.SECRET_KEY
    DATABASE_URL = SETTINGS.DATABASE_URL
    return SETTINGS
