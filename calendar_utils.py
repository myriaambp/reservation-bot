"""Generate .ics calendar files for reservations and cancellation reminders."""

import os
import uuid
from datetime import datetime, timedelta, timezone

CAL_DIR = os.path.join(os.path.dirname(__file__), "cal")


def create_cancellation_reminder(
    venue_name: str,
    deadline_utc: str,
    reservation_date: str,
    reservation_time: str,
    party_size: int,
) -> str:
    """Generate a .ics file for the cancellation deadline.

    Args:
        venue_name: Restaurant name.
        deadline_utc: ISO 8601 UTC datetime string (e.g. "2026-03-04T13:30:00Z").
        reservation_date: YYYY-MM-DD.
        reservation_time: Human-readable time (e.g. "7:30 PM").
        party_size: Number of guests.

    Returns:
        The cal_id (uuid) identifying the generated file.
    """
    dt = datetime.fromisoformat(deadline_utc.replace("Z", "+00:00"))
    dtstart = dt.strftime("%Y%m%dT%H%M%SZ")
    # 30-minute event window
    dtend = dtstart
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    cal_id = uuid.uuid4().hex

    description = (
        f"Free cancellation deadline for your reservation at {venue_name}.\\n"
        f"Reservation: {reservation_date} at {reservation_time}, party of {party_size}.\\n"
        f"Cancel before this time to avoid fees."
    )

    ics = (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//ReservationBot//EN\r\n"
        "BEGIN:VEVENT\r\n"
        f"UID:{cal_id}@reservationbot\r\n"
        f"DTSTAMP:{now}\r\n"
        f"DTSTART:{dtstart}\r\n"
        f"DTEND:{dtend}\r\n"
        f"SUMMARY:Cancel deadline: {venue_name}\r\n"
        f"DESCRIPTION:{description}\r\n"
        "BEGIN:VALARM\r\n"
        "TRIGGER:-PT60M\r\n"
        "ACTION:DISPLAY\r\n"
        f"DESCRIPTION:1 hour until cancellation deadline for {venue_name}\r\n"
        "END:VALARM\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )

    os.makedirs(CAL_DIR, exist_ok=True)
    filepath = os.path.join(CAL_DIR, f"{cal_id}.ics")
    with open(filepath, "w") as f:
        f.write(ics)

    return cal_id


def create_reservation_event(
    venue_name: str,
    reservation_date: str,
    reservation_time: str,
    party_size: int,
    duration_minutes: int = 90,
) -> str:
    """Generate a .ics file for the reservation itself.

    Args:
        venue_name: Restaurant name.
        reservation_date: YYYY-MM-DD.
        reservation_time: Datetime or time string (e.g. "2026-03-09 21:15:00" or "9:15 PM").
        party_size: Number of guests.
        duration_minutes: How long to block on the calendar (default 90 min).

    Returns:
        The cal_id (uuid) identifying the generated file.
    """
    # Parse the reservation start time
    # Handle "2026-03-09 21:15:00" format
    if " " in reservation_time and "-" in reservation_time:
        dt = datetime.fromisoformat(reservation_time)
    else:
        # Combine date + time: "2026-03-09" + "21:15:00"
        dt = datetime.fromisoformat(f"{reservation_date} {reservation_time}")

    dtstart = dt.strftime("%Y%m%dT%H%M%S")
    dtend = (dt + timedelta(minutes=duration_minutes)).strftime("%Y%m%dT%H%M%S")
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    cal_id = uuid.uuid4().hex

    time_display = dt.strftime("%-I:%M %p")
    description = (
        f"Dinner at {venue_name}\\n"
        f"Party of {party_size}\\n"
        f"Booked via Resy"
    )

    ics = (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//ReservationBot//EN\r\n"
        "BEGIN:VEVENT\r\n"
        f"UID:{cal_id}@reservationbot\r\n"
        f"DTSTAMP:{now}\r\n"
        f"DTSTART:{dtstart}\r\n"
        f"DTEND:{dtend}\r\n"
        f"SUMMARY:Reservation at {venue_name}\r\n"
        f"DESCRIPTION:{description}\r\n"
        "BEGIN:VALARM\r\n"
        "TRIGGER:-PT60M\r\n"
        "ACTION:DISPLAY\r\n"
        f"DESCRIPTION:Reservation at {venue_name} in 1 hour\r\n"
        "END:VALARM\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )

    os.makedirs(CAL_DIR, exist_ok=True)
    filepath = os.path.join(CAL_DIR, f"{cal_id}.ics")
    with open(filepath, "w") as f:
        f.write(ics)

    return cal_id
