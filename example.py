from __future__ import annotations

from datetime import date, datetime, timedelta
from random import randint

from sqlalchemy import select

from app.auth_utils import hash_password
from app.database import SessionLocal
from app.models import PaymentType, Project, ProjectStatus, Task, TaskPriority, TaskStatus, TimeEntry, TimeSource, User


def ensure_user(db, email: str, password: str, *, is_admin: bool = False) -> User:
    existing = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if existing:
        existing.is_admin = is_admin or existing.is_admin
        existing.is_blocked = False
        existing.email_verified = True
        return existing

    user = User(
        email=email,
        hashed_password=hash_password(password),
        email_verified=True,
        verification_token=None,
        verification_expires_at=None,
        currency="RUB",
        is_admin=is_admin,
        is_blocked=False,
    )
    db.add(user)
    db.flush()
    return user


def create_demo_data() -> None:
    db = SessionLocal()
    try:
        ensure_user(db, "admin@frilans.local", "admin12345", is_admin=True)
        users = [
            ensure_user(db, "demo1@example.com", "password123"),
            ensure_user(db, "demo2@example.com", "password123"),
        ]
        for idx, user in enumerate(users, start=1):
            hourly = Project(
                user_id=user.id,
                name=f"Сайт клиента #{idx}",
                description="Лендинг и адаптив",
                payment_type=PaymentType.hourly.value,
                hourly_rate=randint(700, 1500),
                client_name=f"Клиент {idx}",
                status=ProjectStatus.active.value,
            )
            fixed = Project(
                user_id=user.id,
                name=f"Фирстиль #{idx}",
                description="Фиксированный пакет дизайна",
                payment_type=PaymentType.fixed.value,
                fixed_amount=randint(18000, 40000),
                client_name=f"Компания {idx}",
                status=ProjectStatus.completed.value,
                completed_at=datetime.utcnow() - timedelta(days=2),
            )
            db.add_all([hourly, fixed])
            db.flush()

            task = Task(
                project_id=hourly.id,
                title="Верстка главной",
                status=TaskStatus.in_progress.value,
                priority=TaskPriority.high.value,
                deadline=date.today() + timedelta(days=3),
                estimated_hours=6,
            )
            db.add(task)
            db.flush()

            for days_ago in range(5):
                seconds = randint(900, 7800)
                db.add(
                    TimeEntry(
                        task_id=task.id,
                        work_date=date.today() - timedelta(days=days_ago),
                        duration_minutes=max(1, round(seconds / 60)),
                        duration_seconds=seconds,
                        source=TimeSource.manual.value,
                        comment="Демо запись",
                    )
                )
        db.commit()
        print("Demo data created. Admin login: admin@frilans.local / admin12345")
    finally:
        db.close()


if __name__ == "__main__":
    create_demo_data()
