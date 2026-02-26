"""FastAPI server for the reservation chat bot."""

import asyncio
import os
from datetime import datetime

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Form, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response

from log_utils import load_log, save_log, log_entry
from resy_api import ResyClient
from chat import ChatSession
from sms import send_sms

load_dotenv()

app = FastAPI()

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")


@app.get("/")
async def index():
    return FileResponse(os.path.join(TEMPLATES_DIR, "index.html"))


@app.get("/api/log")
async def get_log():
    return JSONResponse(load_log())


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
            else:
                available = [s.get("date", {}).get("start", "?") for s in slots]
                try:
                    await notify(
                        f"[{now}] No match. Available: {', '.join(available) if available else 'none'}"
                    )
                except Exception:
                    return

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
                    await ws.send_json({
                        "type": "bot_message",
                        "text": f"Sorry, something went wrong: {e}",
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

    except WebSocketDisconnect:
        pass
    finally:
        for task in watch_tasks:
            task.cancel()


# ---------------------------------------------------------------------------
# WhatsApp webhook (Twilio)
# ---------------------------------------------------------------------------

_wa_resy: ResyClient | None = None
_wa_session: ChatSession | None = None
_wa_watch_tasks: list[asyncio.Task] = []


def _init_wa_state():
    """Lazily initialize the global ResyClient and ChatSession for WhatsApp."""
    global _wa_resy, _wa_session
    if _wa_resy is None:
        api_key = os.getenv("RESY_API_KEY")
        auth_token = os.getenv("RESY_AUTH_TOKEN")
        if not api_key or not auth_token:
            raise RuntimeError("RESY_API_KEY and RESY_AUTH_TOKEN must be set in .env")
        _wa_resy = ResyClient(api_key, auth_token)
        _wa_session = ChatSession(_wa_resy)


@app.post("/whatsapp")
async def whatsapp_webhook(request: Request):
    form = await request.form()
    body = (form.get("Body") or "").strip()
    from_number = form.get("From", "")

    if not body:
        return Response(status_code=200)

    try:
        _init_wa_state()
    except RuntimeError as e:
        send_sms(str(e), to=from_number)
        return Response(status_code=200)

    # Prune finished tasks
    _wa_watch_tasks[:] = [t for t in _wa_watch_tasks if not t.done()]

    # Handle stop/cancel commands
    lower = body.lower()
    if lower in ("stop watching", "cancel watch", "stop watch", "cancel watching"):
        if _wa_watch_tasks:
            for t in _wa_watch_tasks:
                t.cancel()
            _wa_watch_tasks.clear()
            send_sms("All watches cancelled.", to=from_number)
        else:
            send_sms("No active watches to cancel.", to=from_number)
        return Response(status_code=200)

    # Process message through ChatSession
    try:
        events = await _wa_session.process_message(body)
    except Exception as e:
        send_sms(f"Sorry, something went wrong: {e}", to=from_number)
        return Response(status_code=200)

    def _reply(text: str):
        try:
            send_sms(text, to=from_number)
        except Exception:
            pass

    for event in events:
        if event["type"] == "text":
            _reply(event["text"])
        elif event["type"] == "watch":
            async def wa_notify(text: str, match=None, _to=from_number):
                msg = text
                if match:
                    msg += (
                        f"\n\nMatch: {match['venue_name']} on {match['date']}"
                        f" at {match['time']} for {match['party_size']}."
                        f" Reply 'book it' to confirm."
                    )
                send_sms(msg, to=_to)

            task = asyncio.create_task(
                run_watch(_wa_resy, event["params"], wa_notify)
            )
            _wa_watch_tasks.append(task)
            _reply("Started watching for cancellations. I'll message you with updates.")

    return Response(status_code=200)


if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=8080, reload=True)
