"""FastAPI server for the reservation chat bot."""

import asyncio
import logging
import os
import re
from datetime import datetime

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Form, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response

from calendar_utils import CAL_DIR
from log_utils import load_log, save_log, log_entry
from resy_api import ResyClient
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


@app.get("/")
async def index():
    return FileResponse(os.path.join(TEMPLATES_DIR, "index.html"))


@app.get("/api/log")
async def get_log():
    return JSONResponse(load_log())


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


async def run_watch(resy: ResyClient, params: dict, notify):
    """Background task that polls for cancellations.

    Args:
        resy: ResyClient instance.
        params: Watch parameters (venue_id, venue_name, party_size, date, preferred_times).
        notify: async callable(text, match=None) to deliver updates.
                If it raises, the loop stops.
    """
    venue_id = params["venue_id"]
    venue_name = params["venue_name"]
    party_size = params["party_size"]
    date = params["date"]
    preferred_times = params["preferred_times"]
    poll_interval = 60

    # Log the watch
    watch_entry = {
        "status": "watching",
        "venue": venue_name,
        "venue_id": venue_id,
        "date": date,
        "party_size": party_size,
        "preferred_times": preferred_times,
        "created_at": datetime.now().isoformat(),
    }
    log_entry(watch_entry)

    try:
        while True:
            await asyncio.sleep(poll_interval)
            now = datetime.now().strftime("%H:%M:%S")

            try:
                slots = resy.find_slots(venue_id, party_size, date)
            except Exception as e:
                try:
                    await notify(f"[{now}] Poll error: {e}")
                except Exception:
                    return
                continue

            # Check for a match
            matched_slot = None
            for slot in slots:
                start = slot.get("date", {}).get("start", "")
                for pref in preferred_times:
                    if f" {pref}" in start:
                        matched_slot = slot
                        break
                if matched_slot:
                    break

            if matched_slot:
                match_time = matched_slot.get("date", {}).get("start", "unknown")
                config_token = matched_slot.get("config", {}).get("token", "")
                match_info = {
                    "time": match_time,
                    "config_token": config_token,
                    "venue_name": venue_name,
                    "venue_id": venue_id,
                    "date": date,
                    "party_size": party_size,
                }
                try:
                    await notify(
                        f"[{now}] Match found: {match_time}! Type 'book it' to confirm or I'll keep watching.",
                        match=match_info,
                    )
                except Exception:
                    return
            # No match — silently continue polling

    except asyncio.CancelledError:
        # Mark watch as stopped in log
        entries = load_log()
        for e in entries:
            if (e and e.get("venue_id") == venue_id
                    and e.get("date") == date
                    and e.get("status") == "watching"):
                e["status"] = "stopped"
                e["stopped_at"] = datetime.now().isoformat()
                break
        save_log(entries)


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

    async def ws_notify(text: str, match=None):
        payload = {"type": "watch_update", "text": text}
        if match is not None:
            payload["match"] = match
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
                        task = asyncio.create_task(
                            run_watch(resy, event["params"], ws_notify)
                        )
                        watch_tasks.append(task)
                        await ws.send_json({
                            "type": "bot_message",
                            "text": "Started watching for cancellations. I'll send updates here.",
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
    lower = body.lower()
    if lower in ("log", "status", "my watches", "my reservations"):
        entries = load_log()
        watching = [e for e in entries if e and e.get("status") == "watching"]
        booked = [e for e in entries if e and e.get("status") == "booked"]

        lines = []
        if watching:
            lines.append("Active watches:")
            for e in watching:
                times = ", ".join(e.get("preferred_times", []))
                lines.append(f"- {e.get('venue', '?')} on {e.get('date', '?')} for {e.get('party_size', '?')} ({times})")
        if booked:
            lines.append("\nConfirmed reservations:")
            for e in booked:
                lines.append(f"- {e.get('venue', '?')} on {e.get('date', '?')} at {e.get('time', '?')} for {e.get('party_size', '?')}")
        if not lines:
            lines.append("No active watches or reservations.")

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

            async def _notify(text: str, match=None, _channel=_ch, _dest=_to):
                msg = text
                if match:
                    msg += (
                        f"\n\nMatch: {match['venue_name']} on {match['date']}"
                        f" at {match['time']} for {match['party_size']}."
                        f" Reply 'book it' to confirm."
                    )
                send_message(msg, to=_dest, channel=_channel)

            task = asyncio.create_task(
                run_watch(resy_client, event["params"], _notify)
            )
            watch_tasks.append(task)
            text_parts.append("Started watching for cancellations. I'll message you with updates.")
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
