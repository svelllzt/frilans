from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app.models import PaymentType, Project, ProjectStatus, Task, TimeEntry

_Q2 = Decimal("0.01")


def _quantize_money(value: Decimal) -> Decimal:
    return value.quantize(_Q2)


def entry_duration_seconds(entry: TimeEntry) -> int:
    if entry.duration_seconds is not None:
        return max(0, int(entry.duration_seconds))
    return max(0, int(entry.duration_minutes) * 60)


def entry_hours(entry: TimeEntry) -> Decimal:
    return Decimal(entry_duration_seconds(entry)) / Decimal(3600)


def entry_earnings_for_display(project: Project, duration_seconds: int) -> Decimal:
    if project.payment_type != PaymentType.hourly.value:
        return Decimal("0")
    if not project.hourly_rate:
        return Decimal("0")
    hours = (Decimal(duration_seconds) / Decimal(3600)) * Decimal(str(project.hourly_rate))
    return _quantize_money(hours)


def total_earned_for_completed_project(project: Project, db: Session) -> Decimal:
    if project.payment_type == PaymentType.hourly.value:
        entries = (
            db.execute(
                select(TimeEntry).where(
                    TimeEntry.task_id.in_(select(Task.id).where(Task.project_id == project.id))
                )
            )
            .scalars()
            .all()
        )
        if not project.hourly_rate:
            return Decimal("0")
        total_seconds = sum(entry_duration_seconds(e) for e in entries)
        hours = (Decimal(total_seconds) / Decimal(3600)) * Decimal(str(project.hourly_rate))
        return _quantize_money(hours)
    if project.payment_type == PaymentType.fixed.value:
        return _quantize_money(Decimal(str(project.fixed_amount or 0)))
    return Decimal("0")


def earnings_for_period(
    db: Session,
    user_id: int,
    start: date,
    end: date,
) -> tuple[Decimal, dict[date, Decimal]]:
    entries = (
        db.execute(
            select(TimeEntry)
            .join(Task, TimeEntry.task_id == Task.id)
            .join(Project, Task.project_id == Project.id)
            .where(
                Project.user_id == user_id,
                TimeEntry.work_date >= start,
                TimeEntry.work_date <= end,
            )
            .options(joinedload(TimeEntry.task).joinedload(Task.project))
        )
        .scalars()
        .unique()
        .all()
    )

    daily: dict[date, Decimal] = defaultdict(lambda: Decimal("0"))
    total = Decimal("0")

    for entry in entries:
        proj = entry.task.project
        if proj.payment_type == PaymentType.hourly.value:
            amt = entry_earnings_for_display(proj, entry_duration_seconds(entry))
            daily[entry.work_date] = daily[entry.work_date] + amt
            total = total + amt

    fixed_projects = (
        db.execute(
            select(Project).where(
                Project.user_id == user_id,
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
        if start <= cday <= end:
            amt = _quantize_money(Decimal(str(p.fixed_amount or 0)))
            daily[cday] = daily[cday] + amt
            total = total + amt

    return total, dict(daily)


def earnings_today_week_month(db: Session, user_id: int, now: datetime | None = None) -> dict:
    now = now or datetime.utcnow()
    today = now.date()
    monday = today - timedelta(days=today.weekday())
    month_start = date(today.year, today.month, 1)

    t_total, _ = earnings_for_period(db, user_id, today, today)
    w_total, _ = earnings_for_period(db, user_id, monday, today)
    m_total, _ = earnings_for_period(db, user_id, month_start, today)

    return {
        "today": t_total,
        "week": w_total,
        "month": m_total,
        "today_start": today,
        "week_start": monday,
        "month_start": month_start,
    }
