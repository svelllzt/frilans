# Frilans

## Русский

**Frilans** — веб-приложение для фрилансеров: проекты, задачи, учёт времени и отчёты. Стек: **FastAPI**, **SQLAlchemy**, **SQLite**, **Jinja2**. Подробнее об устройстве проекта — файл **`PROJECT.md`**.

### Требования

- Python 3.11+

### Установка

```bash
python -m venv .venv
.venv\Scripts\activate   # Windows
# source .venv/bin/activate  # Linux / macOS
pip install -r requirements.txt
```

### Конфигурация (`config.ini`)

В корне проекта файл `config.ini`:

| Секция `[app]` | Назначение |
|----------------|------------|
| `name` | Название приложения |
| `debug` | Режим отладки (`true` / `false`) |
| `maintenance` | Техработы: для обычных пользователей сайт недоступен (`true` / `false`) |
| `base_url` | Базовый URL для ссылок в письмах, если нет заголовка `Host` |
| `secret_key` | Секрет сессий (смените в продакшене) |
| `database_url` | URL БД (по умолчанию SQLite в каталоге проекта) |
| `admin_email`, `admin_password` | При первом запуске создаётся администратор с этим email |

Секция **`[smtp]`** — параметры почты (хост, порт, логин, пароль, `from_email`, TLS/SSL). Регистрация и сброс пароля идут **кодом из письма** (шесть цифр); письма уходят, когда заданы **`host`** и **`from_email`**.

Переменные окружения с префиксом `FRILANS_` переопределяют значения из INI (см. `app/config.py`).

### Запуск

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

После деплоя за **reverse proxy** (Nginx, Caddy и т.д.) ссылки в письмах обычно строятся по заголовкам **`Host`** и **`X-Forwarded-Proto`**. Поле **«Базовый URL»** в админке — запасной вариант и для сценариев без этих заголовков.

### Админ-панель

Пользователь с флагом `is_admin` (в т.ч. созданный из `admin_email` / `admin_password`) открывает **`/admin`**:

- включение **техработ**;
- переключение **debug**;
- **базовый URL** (домен или IP);
- настройки **SMTP**.

Сохранение формы записывает `config.ini` и **сразу перечитывает настройки** в процессе (перезапуск не обязателен). Смена **`database_url`** в INI по-прежнему требует перезапуска процесса, так как подключение к БД создаётся при старте.

### Демо-данные (опционально)

```bash
python example.py
```

---

## English

**Frilans** is a freelancer-oriented web app: projects, tasks, time tracking, and reports. Built with **FastAPI**, **SQLAlchemy**, **SQLite**, and **Jinja2**. See **`PROJECT.md`** for a longer architecture overview (Russian).

### Requirements

- Python 3.11+

### Setup

```bash
python -m venv .venv
source .venv/bin/activate   # Linux / macOS
# .venv\Scripts\activate    # Windows
pip install -r requirements.txt
```

### Configuration (`config.ini`)

Project root **`config.ini`**:

| `[app]` key | Purpose |
|-------------|---------|
| `name` | Application name |
| `debug` | Debug mode (`true` / `false`) |
| `maintenance` | Maintenance mode: regular users see a maintenance page (`true` / `false`) |
| `base_url` | Fallback base URL for email links when `Host` is missing |
| `secret_key` | Session signing secret (change in production) |
| `database_url` | Database URL (default: SQLite file in project folder) |
| `admin_email`, `admin_password` | Bootstrap admin user on first run |

**`[smtp]`** holds mail server settings. Sign-up and password reset use **six-digit codes sent by email** when **`host`** and **`from_email`** are set.

Environment variables prefixed with **`FRILANS_`** override INI values (see `app/config.py`).

### Run

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Behind a **reverse proxy**, email links normally use the **`Host`** and **`X-Forwarded-Proto`** headers. The admin **“Base URL”** field is a fallback when those headers are not available.

### Admin panel

Users with **`is_admin`** (including the bootstrap account from `admin_email` / `admin_password`) can open **`/admin`** to toggle **maintenance**, **debug**, set the **site base URL**, and configure **SMTP**.

Saving the form updates **`config.ini`** and **reloads settings in-process** (no restart required). Changing **`database_url`** still needs an application restart because the DB engine is created at startup.

### Demo data (optional)

```bash
python example.py
```
