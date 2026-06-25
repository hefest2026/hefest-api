"""Registration business logic — all outbox writes are transactional."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import HTTPException, status
from tortoise.exceptions import IntegrityError
from tortoise.transactions import in_transaction

from hefest.models.event import Event, EventStatus
from hefest.models.notification_job import NotificationJob
from hefest.models.registration import Registration, RegistrationStatus
from hefest.models.user import User
from hefest.schemas.registration import (
    MyRegistrationResponse,
    RegistrationResponse,
    RegistrationSummary,
)


async def register_student(
    student: User,
    event_id: UUID,
) -> RegistrationResponse:
    """Register a student for a published event.

    Locks the event row to prevent overbooking under concurrent requests. The
    registration and outbox job are written in the same transaction.

    Args:
        student: The authenticated student.
        event_id: Target event UUID.

    Returns:
        RegistrationResponse with status CONFIRMED or WAITLISTED.

    Raises:
        HTTPException 404: Event not found or not published.
        HTTPException 409: Student already has an active registration.
    """
    async with in_transaction() as conn:
        event = (
            await Event.filter(id=event_id)
            .using_db(conn)
            .select_for_update()
            .get_or_none()
        )
        if event is None or event.status != EventStatus.published:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="event not found"
            )

        confirmed_count = await (
            Registration.filter(event_id=event_id, status=RegistrationStatus.confirmed)
            .using_db(conn)
            .count()
        )

        if confirmed_count < event.capacity:
            reg_status = RegistrationStatus.confirmed
            event_type = "RegistrationConfirmed"
            waitlist_position: int | None = None
        else:
            waitlist_count = await (
                Registration.filter(
                    event_id=event_id, status=RegistrationStatus.waitlisted
                )
                .using_db(conn)
                .count()
            )
            reg_status = RegistrationStatus.waitlisted
            event_type = "RegistrationWaitlisted"
            waitlist_position = waitlist_count + 1

        try:
            reg = await Registration.create(
                event_id=event_id,
                student=student,
                status=reg_status,
                using_db=conn,
            )
        except IntegrityError:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="already registered for this event",
            )

        payload: dict[str, str | int] = {
            "registration_id": str(reg.id),
            "event_id": str(event_id),
            "student_id": str(student.id),
            "event_title": event.title,
        }
        if waitlist_position is not None:
            payload["waitlist_position"] = waitlist_position

        await NotificationJob.create(
            event_id=event_id,
            event_type=event_type,
            payload=payload,
            idempotency_key=f"{reg.id}:{event_type}",
            using_db=conn,
        )

    return RegistrationResponse(
        id=reg.id,
        event_id=event_id,
        student_id=student.id,
        status=reg_status,
        registered_at=reg.registered_at,
        waitlist_position=waitlist_position,
    )


async def cancel_registration(student: User, reg_id: UUID) -> Registration:
    """Cancel the student's own registration, promoting the next waitlisted.

    Locks the event row to serialize concurrent cancellations for the same
    event. Cancellation is only allowed before the event starts.

    Args:
        student: The authenticated student.
        reg_id: Registration UUID to cancel.

    Returns:
        The cancelled Registration.

    Raises:
        HTTPException 404: Registration not found or not owned by the caller.
        HTTPException 409: Already cancelled, or event has already started.
    """
    async with in_transaction() as conn:
        reg = await (
            Registration.filter(id=reg_id, student_id=student.id)
            .using_db(conn)
            .get_or_none()
        )
        if reg is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="registration not found",
            )

        if reg.status == RegistrationStatus.cancelled:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="registration already cancelled",
            )

        # Lock the event row — serializes concurrent cancellations for this event.
        event = await (
            Event.filter(id=reg.event_id)
            .using_db(conn)
            .select_for_update()
            .get_or_none()
        )
        if event is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="event not found"
            )

        starts_at = event.starts_at
        if starts_at.tzinfo is None:
            starts_at = starts_at.replace(tzinfo=UTC)
        if starts_at <= datetime.now(UTC):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="event already started",
                headers={"X-Error-Code": "event_already_started"},
            )

        was_confirmed = reg.status == RegistrationStatus.confirmed

        reg.status = RegistrationStatus.cancelled
        reg.cancelled_at = datetime.now(UTC)
        await reg.save(update_fields=["status", "cancelled_at"], using_db=conn)

        if was_confirmed:
            next_waitlisted = await (
                Registration.filter(
                    event_id=reg.event_id,
                    status=RegistrationStatus.waitlisted,
                )
                .using_db(conn)
                .order_by("registered_at")
                .first()
            )
            if next_waitlisted is not None:
                next_waitlisted.status = RegistrationStatus.confirmed
                await next_waitlisted.save(update_fields=["status"], using_db=conn)

                await NotificationJob.create(
                    event_id=reg.event_id,
                    event_type="WaitlistPromoted",
                    payload={
                        "registration_id": str(next_waitlisted.id),
                        "event_id": str(reg.event_id),
                        "student_id": str(next_waitlisted.student_id),
                        "event_title": event.title,
                    },
                    idempotency_key=f"{next_waitlisted.id}:WaitlistPromoted",
                    using_db=conn,
                )

        await NotificationJob.create(
            event_id=reg.event_id,
            event_type="RegistrationCancelled",
            payload={
                "registration_id": str(reg.id),
                "event_id": str(reg.event_id),
                "student_id": str(student.id),
            },
            idempotency_key=f"{reg.id}:RegistrationCancelled",
            using_db=conn,
        )

    return reg


async def list_my_registrations(student: User) -> list[MyRegistrationResponse]:
    """Return all active registrations for the current student with positions.

    Waitlist position is computed at read time — no stored integer that can
    drift under concurrent writes.

    Args:
        student: The authenticated student.

    Returns:
        List of own registrations (confirmed and waitlisted, not cancelled).
    """
    regs = await Registration.filter(
        student=student, status__not=RegistrationStatus.cancelled
    ).all()

    result: list[MyRegistrationResponse] = []
    for reg in regs:
        position: int | None = None
        if reg.status == RegistrationStatus.waitlisted:
            ahead = await Registration.filter(
                event_id=reg.event_id,
                status=RegistrationStatus.waitlisted,
                registered_at__lt=reg.registered_at,
            ).count()
            position = ahead + 1

        result.append(
            MyRegistrationResponse(
                id=reg.id,
                event_id=reg.event_id,
                status=reg.status,
                registered_at=reg.registered_at,
                cancelled_at=reg.cancelled_at,
                waitlist_position=position,
            )
        )

    return result


async def list_event_registrations(
    organizer: User,
    event_id: UUID,
    *,
    waitlist_only: bool = False,
) -> list[RegistrationSummary]:
    """Return confirmed (or waitlisted) registrations for an organizer's event.

    Args:
        organizer: The authenticated organizer.
        event_id: Target event UUID.
        waitlist_only: If True return waitlisted rows ordered FIFO; else confirmed.

    Returns:
        List of RegistrationSummary objects.

    Raises:
        HTTPException 404: Event not found or not owned by the caller.
    """
    event = await Event.get_or_none(id=event_id)
    if event is None or event.organizer_id != organizer.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="event not found"
        )

    filter_status = (
        RegistrationStatus.waitlisted if waitlist_only else RegistrationStatus.confirmed
    )
    qs = Registration.filter(event_id=event_id, status=filter_status)
    if waitlist_only:
        qs = qs.order_by("registered_at")

    regs = await qs.all()
    return [RegistrationSummary.model_validate(r) for r in regs]
