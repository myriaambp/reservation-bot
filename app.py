"""FastAPI server for the reservation chat bot."""

import asyncio
import logging
import os
import re
from datetime import date, datetime, timedelta

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Form, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response

from calendar_utils import CAL_DIR, create_reservation_event, create_cancellation_reminder
from log_utils import load_log, save_log, log_entry
from resy_api import ResyClient, ResyBookingConflict
from chat import ChatSession
from sms import send_sms, send_message

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


def _md_to_whatsapp(text: str) -> str:
    """Convert Markdown formatting to WhatsApp-compatible plain text."""
    # Convert markdown bullet lists:  "* item" or "- item" → "• item"
    text = re.sub(r"^[\*\-]\s+", "• ", text, flags=re.MULTILINE)
    # Convert bold **text** to WhatsApp bold *text*
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
    # Convert markdown italic _text_ — WhatsApp already supports this
    # Convert markdown headers ## Header → *Header*
    text = re.sub(r"^#{1,3}\s+(.+)$", r"*\1*", text, flags=re.MULTILINE)
    return text

load_dotenv()

app = FastAPI()

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")


def _reconcile_log() -> list[dict]:
    """Reconcile booked log entries against Resy's actual reservations.

    Any upcoming booked entry whose confirmation_token is no longer in
    Resy's active list gets marked as cancelled (external cancellation).
    Returns the (possibly updated) entries list.
    """
    entries = load_log()
    today_str = date.today().isoformat()

    upcoming_booked = [
        e for e in entries
        if e and e.get("status") == "booked" and (e.get("date") or "") >= today_str
    ]
    if not upcoming_booked:
        return entries

    api_key = os.getenv("RESY_API_KEY")
    auth_token = os.getenv("RESY_AUTH_TOKEN")
    if not api_key or not auth_token:
        return entries

    try:
        resy = ResyClient(api_key, auth_token)
        active = resy.list_reservations()
        active_tokens = {r["resy_token"] for r in active if r.get("resy_token")}
    except Exception:
        log.debug("Resy reconciliation failed, returning log as-is")
        return entries

    changed = False
    for entry in upcoming_booked:
        token = entry.get("confirmation_token")
        if token and token not in active_tokens:
            entry["status"] = "cancelled"
            entry["source"] = "resy"
            entry["cancelled_at"] = datetime.now().isoformat()
            changed = True

    if changed:
        save_log(entries)

    return entries


@app.get("/")
async def index():
    return FileResponse(os.path.join(TEMPLATES_DIR, "index.html"))


@app.get("/api/log")
async def get_log():
    entries = _reconcile_log()
    return JSONResponse({"entries": entries, "today": date.today().isoformat()})


@app.get("/cal/{cal_id}")
async def get_calendar(cal_id: str):
    """Serve a generated .ics calendar file."""
    # Sanitize: only allow hex characters (uuid)
    if not cal_id.replace("-", "").isalnum():
        return Response(status_code=400)
    filepath = os.path.join(CAL_DIR, f"{cal_id}.ics")
    if not os.path.isfile(filepath):
        return Response(status_code=404)
    with open(filepath) as f:
        content = f.read()
    return Response(content=content, media_type="text/calendar")


def _matches_time(slot_start: str, preferred_times: list[str]) -> bool:
    """Check if a slot's start time matches any preferred time spec.

    preferred_times entries can be:
      "19:00"        → exact HH:MM match
      "19:00-21:30"  → slot HH:MM is within the range (inclusive)
    """
    # Extract HH:MM from slot_start like "2026-03-09 19:15:00"
    parts = slot_start.split(" ")
    if len(parts) < 2:
        return False
    time_part = parts[1]  # "19:15:00"
    hhmm_parts = time_part.split(":")
    if len(hhmm_parts) < 2:
        return False
    slot_hhmm = f"{hhmm_parts[0]}:{hhmm_parts[1]}"

    for pref in preferred_times:
        if "-" in pref and pref.count("-") == 1:
            # Range: "19:00-21:30"
            range_start, range_end = pref.split("-")
            if range_start.strip() <= slot_hhmm <= range_end.strip():
                return True
        else:
            # Exact match
            if slot_hhmm == pref.strip():
                return True
    return False


def _auto_book(
    resy: ResyClient,
    config_token: str,
    watch_date: str,
    party_size: int,
    venue_name: str,
    venue_id: int,
    match_time: str,
    terms: dict,
) -> tuple[bool, str, str | None, list[tuple[str, str]]]:
    """Book a slot immediately. Returns (success, message, resy_token, calendar_events).

    calendar_events is a list of (cal_id, label) tuples.
    """
    calendar_events: list[tuple[str, str]] = []

    # 1. Fresh book_token
    try:
        details = resy.get_details(config_token, watch_date, party_size)
    except Exception as e:
        return False, f"Could not get booking details: {e}", None, []

    book_token = details.get("book_token")
    payment_method_id = details.get("payment_method_id")
    if not book_token:
        return False, "Slot vanished before we could book it.", None, []

    # 2. Book
    try:
        result = resy.book(book_token, payment_method_id)
    except ResyBookingConflict as e:
        return False, f"Booking conflict: {e}", None, []
    except Exception as e:
        return False, f"Booking failed: {e}", None, []

    resy_token = result.get("resy_token", "N/A")

    # 3. Log
    log_entry({
        "status": "booked",
        "source": "bot",
        "venue": venue_name,
        "venue_id": venue_id,
        "date": watch_date,
        "time": match_time,
        "party_size": party_size,
        "confirmation_token": resy_token,
        "cancellation_deadline": terms.get("cancellation_deadline"),
        "cancellation_fee": terms.get("cancellation_fee"),
        "booked_at": datetime.now().isoformat(),
        "created_at": datetime.now().isoformat(),
    })

    # 4. Calendar events
    try:
        res_cal_id = create_reservation_event(
            venue_name=venue_name,
            reservation_date=watch_date,
            reservation_time=match_time,
            party_size=party_size,
        )
        calendar_events.append((res_cal_id, "reservation"))
    except Exception:
        log.exception("Auto-book: failed to create reservation calendar event")

    deadline = terms.get("cancellation_deadline")
    if deadline:
        try:
            cancel_cal_id = create_cancellation_reminder(
                venue_name=venue_name,
                deadline_utc=deadline,
                reservation_date=watch_date,
                reservation_time=match_time,
                party_size=party_size,
            )
            calendar_events.append((cancel_cal_id, "cancellation"))
        except Exception:
            log.exception("Auto-book: failed to create cancellation reminder")

    msg = (
        f"Snagged it! Booked {venue_name} on {watch_date} at {match_time} "
        f"for {party_size}.\n\n"
        f"Cancellation policy: {terms.get('cancellation_policy', 'N/A')}\n"
        f"Cancel by: {terms.get('cancellation_deadline', 'N/A')}\n"
        f"Fee: {terms.get('cancellation_fee', 'None')}"
    )
    return True, msg, resy_token, calendar_events


async def run_watch(resy: ResyClient, params: dict, notify):
    """Background task that polls for cancellations and auto-books on match.

    Args:
        resy: ResyClient instance.
        params: Watch parameters including:
            venue_id, venue_name, party_size, date, dates, preferred_times,
            auto_book, terms, snipe_at (optional), base_url (optional).
        notify: async callable(text, match=None, calendar_links=None) to deliver updates.
    """
    venue_id = params["venue_id"]
    venue_name = params["venue_name"]
    party_size = params["party_size"]
    dates = params.get("dates", [params["date"]])
    preferred_times = params["preferred_times"]
    auto_book = params.get("auto_book", False)
    terms = params.get("terms", {})
    snipe_at_str = params.get("snipe_at")
    base_url = params.get("base_url", "")

    NORMAL_INTERVAL = 10
    SNIPE_INTERVAL = 2
    SNIPE_WINDOW_SECS = 5 * 60  # 5 minutes of aggressive polling

    # Log the watch
    watch_log = {
        "status": "watching",
        "venue": venue_name,
        "venue_id": venue_id,
        "date": dates[0] if dates else params["date"],
        "dates": dates,
        "party_size": party_size,
        "preferred_times": preferred_times,
        "auto_book": auto_book,
        "terms": terms,
        "created_at": datetime.now().isoformat(),
    }
    if snipe_at_str:
        watch_log["snipe_at"] = snipe_at_str
    log_entry(watch_log)

    # Unique key for this watch entry in the log
    watch_created_at = watch_log["created_at"]

    def _update_watch(updates: dict):
        """Update this watch entry in the log by created_at."""
        entries = load_log()
        for e in entries:
            if e and e.get("created_at") == watch_created_at and e.get("status") == "watching":
                e.update(updates)
                break
        save_log(entries)

    def _all_dates_passed() -> bool:
        today = datetime.now().date()
        return all(
            today > datetime.fromisoformat(d).date()
            for d in dates
        )

    # --- Snipe mode: sleep until snipe window ---
    if snipe_at_str:
        try:
            snipe_at = datetime.fromisoformat(snipe_at_str)
        except ValueError:
            snipe_at = None

        if snipe_at:
            wake_at = snipe_at - timedelta(seconds=30)
            while datetime.now() < wake_at:
                # Sleep in 10-second chunks for cancellation responsiveness
                remaining = (wake_at - datetime.now()).total_seconds()
                await asyncio.sleep(min(10, max(0.5, remaining)))

    try:
        snipe_start = datetime.now() if snipe_at_str else None

        while True:
            if _all_dates_passed():
                _update_watch({"status": "expired", "expired_at": datetime.now().isoformat()})
                try:
                    await notify(
                        f"Watch expired: {venue_name} — all watched dates have passed. "
                        f"No slots opened up. Stopping watch."
                    )
                except Exception:
                    pass
                return

            # Determine poll interval
            poll_interval = NORMAL_INTERVAL
            if snipe_start:
                elapsed = (datetime.now() - snipe_start).total_seconds()
                if elapsed < SNIPE_WINDOW_SECS:
                    poll_interval = SNIPE_INTERVAL

            await asyncio.sleep(poll_interval)
            now_str = datetime.now().strftime("%H:%M:%S")

            if _all_dates_passed():
                _update_watch({"status": "expired", "expired_at": datetime.now().isoformat()})
                try:
                    await notify(
                        f"Watch expired: {venue_name} — all watched dates have passed. "
                        f"No slots opened up. Stopping watch."
                    )
                except Exception:
                    pass
                return

            # Poll each date
            for watch_date in dates:
                # Skip dates that have passed
                if datetime.now().date() > datetime.fromisoformat(watch_date).date():
                    continue

                try:
                    slots = resy.find_slots(venue_id, party_size, watch_date)
                except Exception as e:
                    try:
                        await notify(f"[{now_str}] Poll error for {watch_date}: {e}")
                    except Exception:
                        return
                    continue

                # Check for a match
                matched_slot = None
                for slot in slots:
                    start = slot.get("date", {}).get("start", "")
                    if _matches_time(start, preferred_times):
                        matched_slot = slot
                        break

                if not matched_slot:
                    continue

                match_time = matched_slot.get("date", {}).get("start", "unknown")
                config_token = matched_slot.get("config", {}).get("token", "")

                if auto_book:
                    # Auto-book immediately
                    success, msg, resy_token, cal_events = _auto_book(
                        resy, config_token, watch_date, party_size,
                        venue_name, venue_id, match_time, terms,
                    )

                    # Build calendar links
                    cal_links = []
                    if base_url:
                        for cal_id, label in cal_events:
                            cal_links.append({
                                "url": f"{base_url}/cal/{cal_id}",
                                "cal_id": cal_id,
                                "label": label,
                            })

                    if success:
                        _update_watch({
                            "status": "booked",
                            "booked_at": datetime.now().isoformat(),
                            "booked_time": match_time,
                            "booked_date": watch_date,
                        })
                        try:
                            await notify(msg, calendar_links=cal_links)
                        except Exception:
                            pass
                        return  # Done — watch fulfilled
                    else:
                        # Check if it was a booking conflict (412)
                        is_conflict = "conflict" in msg.lower()
                        try:
                            await notify(
                                f"[{now_str}] Auto-book failed: {msg}"
                                + (" Continuing to watch..." if is_conflict else " Retrying next cycle..."),
                            )
                        except Exception:
                            return
                        # Continue watching regardless of error type
                        continue
                else:
                    # Legacy notify-only mode (shouldn't happen with new flow, but safe fallback)
                    match_info = {
                        "time": match_time,
                        "config_token": config_token,
                        "venue_name": venue_name,
                        "venue_id": venue_id,
                        "date": watch_date,
                        "party_size": party_size,
                    }
                    try:
                        await notify(
                            f"[{now_str}] Match found: {match_time}!",
                            match=match_info,
                        )
                    except Exception:
                        return

    except asyncio.CancelledError:
        _update_watch({"status": "stopped", "stopped_at": datetime.now().isoformat()})


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()

    api_key = os.getenv("RESY_API_KEY")
    auth_token = os.getenv("RESY_AUTH_TOKEN")

    if not api_key or not auth_token:
        await ws.send_json({
            "type": "bot_message",
            "text": "Error: RESY_API_KEY and RESY_AUTH_TOKEN must be set in .env",
        })
        await ws.close()
        return

    resy = ResyClient(api_key, auth_token)
    session = ChatSession(resy)
    watch_tasks: list[asyncio.Task] = []

    async def ws_notify(text: str, match=None, calendar_links=None):
        payload = {"type": "watch_update", "text": text}
        if match is not None:
            payload["match"] = match
        if calendar_links:
            payload["calendar_links"] = calendar_links
        await ws.send_json(payload)

    try:
        while True:
            data = await ws.receive_json()

            if data.get("type") == "message":
                user_text = data.get("text", "").strip()
                if not user_text:
                    continue

                # Send typing indicator
                await ws.send_json({"type": "typing", "text": ""})

                try:
                    events = await session.process_message(user_text)
                except Exception as e:
                    log.exception("WebSocket process_message failed, resetting session")
                    session = ChatSession(resy)
                    await ws.send_json({
                        "type": "bot_message",
                        "text": "Sorry, something went wrong. Please try again.",
                    })
                    continue

                for event in events:
                    if event["type"] == "text":
                        await ws.send_json({
                            "type": "bot_message",
                            "text": event["text"],
                        })
                    elif event["type"] == "watch":
                        watch_params = event["params"]
                        # WebSocket doesn't have a base_url from request,
                        # so calendar links use relative paths via /cal/{id}
                        watch_params["base_url"] = ""
                        task = asyncio.create_task(
                            run_watch(resy, watch_params, ws_notify)
                        )
                        watch_tasks.append(task)
                        await ws.send_json({
                            "type": "bot_message",
                            "text": "Watch armed with auto-book! I'll book instantly when a match opens up.",
                        })
                    elif event["type"] == "calendar":
                        await ws.send_json({
                            "type": "calendar_link",
                            "url": f"/cal/{event['cal_id']}",
                            "label": event.get("label", "reservation"),
                        })

    except WebSocketDisconnect:
        pass
    finally:
        for task in watch_tasks:
            task.cancel()


# ---------------------------------------------------------------------------
# Twilio webhooks (WhatsApp + SMS)
# ---------------------------------------------------------------------------

# Per-channel state: each channel gets its own ChatSession so conversations
# don't bleed across WhatsApp and SMS.
_channel_state: dict[str, dict] = {}  # channel → {"resy", "session", "watch_tasks"}


def _get_channel_state(channel: str) -> dict:
    """Lazily initialize per-channel ResyClient, ChatSession, and watch tasks."""
    if channel not in _channel_state:
        api_key = os.getenv("RESY_API_KEY")
        auth_token = os.getenv("RESY_AUTH_TOKEN")
        if not api_key or not auth_token:
            raise RuntimeError("RESY_API_KEY and RESY_AUTH_TOKEN must be set in .env")
        resy = ResyClient(api_key, auth_token)
        _channel_state[channel] = {
            "resy": resy,
            "session": ChatSession(resy),
            "watch_tasks": [],
        }
    return _channel_state[channel]


async def _handle_twilio_message(request: Request, channel: str):
    """Shared handler for both WhatsApp and SMS webhooks."""
    form = await request.form()
    body = (form.get("Body") or "").strip()
    from_number = form.get("From", "")

    if not body:
        return Response(status_code=200)

    def _reply(text: str):
        try:
            send_message(text, to=from_number, channel=channel)
        except Exception as e:
            log.error("Failed to send %s to %s: %s", channel, from_number, e)

    try:
        state = _get_channel_state(channel)
    except RuntimeError as e:
        _reply(str(e))
        return Response(status_code=200)

    resy_client = state["resy"]
    session = state["session"]
    watch_tasks = state["watch_tasks"]

    # Prune finished tasks
    watch_tasks[:] = [t for t in watch_tasks if not t.done()]

    # Handle special commands
    lower = body.lower().strip()
    _is_status_cmd = (
        lower in ("log", "status", "dashboard", "my watches", "my reservations", "my bookings")
        or re.search(r"\b(show|view|get|check)\b.+\b(log|status|reservations|watches|bookings|dashboard)\b", lower)
        or re.search(r"\b(my|the)\s+(log|status|reservations|watches|bookings)\b", lower)
    )
    if _is_status_cmd:
        entries = _reconcile_log()
        today_str = date.today().isoformat()
        watching = [e for e in entries if e and e.get("status") == "watching"
                    and (e.get("date") or "") >= today_str]
        watch_booked = [e for e in entries if e and e.get("status") == "booked"
                        and e.get("auto_book")]
        booked = [e for e in entries if e and e.get("status") == "booked"]
        cancelled = [e for e in entries if e and e.get("status") == "cancelled"]
        upcoming = [e for e in booked if (e.get("date") or "") >= today_str]
        past = [e for e in booked if (e.get("date") or "") < today_str]

        lines = []
        if watching:
            lines.append("Active watches:")
            for e in watching:
                times = ", ".join(e.get("preferred_times", []))
                lines.append(f"- {e.get('venue', '?')} on {e.get('date', '?')} for {e.get('party_size', '?')} ({times})")

        if upcoming:
            lines.append("\nUpcoming reservations:")
            for e in upcoming:
                line = f"- {e.get('venue', '?')} on {e.get('date', '?')} at {e.get('time', '?')} for {e.get('party_size', '?')}"
                deadline = e.get("cancellation_deadline")
                if deadline:
                    line += f"\n  Cancel by: {deadline}"
                    fee = e.get("cancellation_fee")
                    if fee:
                        line += f" (fee: {fee})"
                lines.append(line)

        if past:
            lines.append("\nPast reservations:")
            for e in past:
                lines.append(f"- {e.get('venue', '?')} on {e.get('date', '?')} at {e.get('time', '?')}")

        if cancelled:
            lines.append("\nCancelled:")
            for e in cancelled:
                source = "via bot" if e.get("source") == "bot" else "on Resy"
                line = f"- {e.get('venue', '?')} on {e.get('date', '?')}"
                if e.get("cancelled_at"):
                    line += f" (cancelled {e['cancelled_at'][:10]}, {source})"
                else:
                    line += f" ({source})"
                lines.append(line)

        if not lines:
            lines.append("No watches or reservations yet.")

        _reply("\n".join(lines))
        return Response(status_code=200)

    if lower in ("stop watching", "cancel watch", "stop watch", "cancel watching"):
        if watch_tasks:
            for t in watch_tasks:
                t.cancel()
            watch_tasks.clear()
            _reply("All watches cancelled.")
        else:
            _reply("No active watches to cancel.")
        return Response(status_code=200)

    # Process message through ChatSession
    try:
        events = await session.process_message(body)
    except Exception:
        log.exception("%s process_message failed, resetting session", channel)
        state["session"] = ChatSession(resy_client)
        try:
            _reply("Sorry, something went wrong. Please try again.")
        except Exception:
            pass
        return Response(status_code=200)

    base_url = str(request.base_url).rstrip("/")

    # Consolidate all text events into a single message
    text_parts: list[str] = []
    for event in events:
        if event["type"] == "text":
            text_parts.append(event["text"])
        elif event["type"] == "watch":
            _ch = channel
            _to = from_number
            _base = base_url

            async def _notify(text: str, match=None, calendar_links=None, _channel=_ch, _dest=_to, _bu=_base):
                msg = text
                if match:
                    msg += (
                        f"\n\nMatch: {match['venue_name']} on {match['date']}"
                        f" at {match['time']} for {match['party_size']}."
                    )
                if calendar_links:
                    for cl in calendar_links:
                        label = cl.get("label", "reservation")
                        url = cl.get("url", "")
                        if label == "cancellation":
                            msg += f"\n\nAdd cancellation deadline reminder to your calendar: {url}"
                        else:
                            msg += f"\n\nAdd reservation to your calendar: {url}"
                send_message(msg, to=_dest, channel=_channel)

            watch_params = event["params"]
            watch_params["base_url"] = base_url
            task = asyncio.create_task(
                run_watch(resy_client, watch_params, _notify)
            )
            watch_tasks.append(task)
            text_parts.append("Watch armed with auto-book! I'll book instantly when a match opens up.")
        elif event["type"] == "calendar":
            cal_url = f"{base_url}/cal/{event['cal_id']}"
            label = event.get("label", "reservation")
            if label == "cancellation":
                text_parts.append(f"Add cancellation deadline reminder to your calendar: {cal_url}")
            else:
                text_parts.append(f"Add reservation to your calendar: {cal_url}")

    if text_parts:
        combined = "\n\n".join(text_parts)
        if channel == "whatsapp":
            combined = _md_to_whatsapp(combined)
        _reply(combined)

    return Response(status_code=200)


@app.post("/whatsapp")
async def whatsapp_webhook(request: Request):
    return await _handle_twilio_message(request, channel="whatsapp")


@app.post("/sms")
async def sms_webhook(request: Request):
    return await _handle_twilio_message(request, channel="sms")


if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=8080, reload=True)
