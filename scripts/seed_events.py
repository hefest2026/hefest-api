"""Seed sample published events for development.

Finds the organizer@adviz.bg account and creates five realistic Bulgarian
school events with varied dates, descriptions (including URLs), and capacities.

Safe to run multiple times: events are identified by title+organizer — existing
ones are skipped.

Usage:
    PYTHONPATH=. uv run python scripts/seed_events.py
    PYTHONPATH=. uv run python scripts/seed_events.py --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime, timedelta

from tortoise import Tortoise

from hefest.config import TORTOISE_ORM
from hefest.models.event import Event, EventStatus
from hefest.models.user import User

ORGANIZER_EMAIL = "organizer@adviz.bg"

_now = datetime.now(UTC)


def _dt(days: int, hour: int = 10, minute: int = 0) -> datetime:
    return (_now + timedelta(days=days)).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )


SEED_EVENTS: list[dict[str, object]] = [
    {
        "title": "Уъркшоп по Python за начинаещи",
        "description": (
            "Практически уъркшоп за ученици без предварителен"
            " опит в програмирането.\n\n"
            "Ще разгледаме основните концепции на Python:"
            " променливи, функции, цикли и работа с файлове."
            " Всеки участник ще напише собствена малка програма.\n\n"
            "Материали: https://docs.python.org/3/tutorial/\n"
            "Регистрация: https://forms.adviz.bg/python-workshop"
        ),
        "starts_at": _dt(7, 14, 0),
        "ends_at": _dt(7, 17, 0),
        "location": "Кабинет по информатика, ет. 2, стая 204",
        "capacity": 20,
    },
    {
        "title": "Лекция: Изкуствен интелект в ежедневието",
        "description": (
            "Открита лекция за това как AI системите вече влияят на живота ни — "
            "от препоръчителни алгоритми до медицинска диагностика.\n\n"
            "Лектор: инж. Мария Стоянова, ML инженер в Sofia AI Lab.\n\n"
            "Слайдовете ще бъдат публикувани след лекцията на: "
            "https://sofia-ai-lab.bg/lectures\n"
            "Събитието е подходящо за ученици от 9-12 клас."
        ),
        "starts_at": _dt(14, 16, 30),
        "ends_at": _dt(14, 18, 0),
        "location": "Актова зала, партер",
        "capacity": 60,
    },
    {
        "title": "Олимпиада по математика — вътрешноучилищен кръг",
        "description": (
            "Вътрешноучилищен кръг на олимпиадата по математика"
            " за учебната 2025/2026 г.\n\n"
            "Участват ученици от 8-12 клас."
            " Продължителност: 4 астрономически часа.\n"
            "Разрешени помагала: линийка, пергел, молив."
            " Не се разрешават калкулатори.\n\n"
            "Програма и теми от предишни години:"
            " https://math.mon.bg/olimpiadi"
        ),
        "starts_at": _dt(21, 8, 0),
        "ends_at": _dt(21, 12, 0),
        "location": "Класни стаи 301, 302 и 303, ет. 3",
        "capacity": 45,
    },
    {
        "title": "Хакатон: Решаване на екологични проблеми с технологии",
        "description": (
            "24-часов хакатон за ученически отбори (2-4 човека). "
            "Предизвикателството: разработете технологично решение на реален "
            "екологичен проблем в общината.\n\n"
            "Осигурени: храна, интернет, работни места. Носете лаптопи.\n\n"
            "Ментори и партньори: https://greentech.bg/hackathon-2026\n"
            "Регламент: https://greentech.bg/rules\n\n"
            "Победителите получават покана за националния финал."
        ),
        "starts_at": _dt(35, 9, 0),
        "ends_at": _dt(36, 9, 0),
        "location": "Иновационен хъб, бул. България 102, Бургас",
        "capacity": 40,
    },
    {
        "title": "Онлайн семинар: Как да кандидатстваш в чужбина",
        "description": (
            "Семинар за ученици от 11-12 клас,"
            " които обмислят университет в чужбина.\n\n"
            "Теми:\n"
            "- Избор на университет и специалност\n"
            "- Есета и мотивационни писма\n"
            "- Финансиране и стипендии\n"
            "- Личен опит на студенти от Оксфорд, ETH Цюрих и TU Делфт\n\n"
            "Платформа: https://meet.adviz.bg/abroad-seminar\n"
            "Допълнителни ресурси: https://studyabroad.bg"
        ),
        "starts_at": _dt(10, 17, 0),
        "ends_at": _dt(10, 19, 0),
        "location": "https://meet.adviz.bg/abroad-seminar",
        "capacity": 10,
    },
]


async def seed_events(*, dry_run: bool) -> None:
    """Upsert sample published events for the organizer account.

    Args:
        dry_run: When True, print planned actions without writing to the database.
    """
    await Tortoise.init(config=TORTOISE_ORM)

    organizer = await User.get_or_none(email=ORGANIZER_EMAIL)
    if organizer is None:
        print(
            f"ERROR: organizer account not found ({ORGANIZER_EMAIL})."
            " Run seed.py first."
        )
        await Tortoise.close_connections()
        return

    for data in SEED_EVENTS:
        title: str = str(data["title"])
        existing = await Event.get_or_none(organizer=organizer, title=title)

        if existing is not None:
            print(f"SKIP     {title!r}")
            continue

        if dry_run:
            print(f"[dry-run] CREATE  {title!r}")
            continue

        await Event.create(
            organizer=organizer,
            title=title,
            description=data["description"],
            starts_at=data["starts_at"],
            ends_at=data.get("ends_at"),
            location=data["location"],
            capacity=data["capacity"],
            status=EventStatus.published,
        )
        print(f"CREATED  {title!r}")

    await Tortoise.close_connections()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be seeded without writing to the database.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    asyncio.run(seed_events(dry_run=args.dry_run))
