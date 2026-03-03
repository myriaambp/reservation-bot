"""Gemini integration for the reservation chat bot."""

import json
import logging
import os
from datetime import date, datetime

from google import genai
from google.genai import types
from resy_api import ResyClient, ResyBookingConflict
from log_utils import load_log, save_log, log_entry
from calendar_utils import create_cancellation_reminder, create_reservation_event

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a friendly restaurant reservation assistant powered by Resy.
Keep your tone warm and conversational — like a helpful friend, not a formal concierge.

FORMATTING: Use plain text only. No markdown, no asterisks, no bold formatting.
Use dashes (- item) for lists.

HOW TO HELP:
1. Search for the restaurant first. Show the user what you found and ask which one they want.
2. Once they pick a restaurant, find available slots. Show the times and ask which one works.
   If their preferred time isn't available, offer to watch for cancellations.
3. When they pick a time, call prepare_booking. This gives you cancellation policy, fees, and
   deadlines. Share ALL of these with the user and ask if they want to go ahead.
4. Only call confirm_booking after they say yes.

CANCELLING:
When the user wants to cancel, call list_reservations to show their upcoming reservations.
Ask which one they want to cancel. Once they pick one, confirm they're sure, then call
cancel_reservation with the resy_token.

IMPORTANT:
- Pass the EXACT time string from find_slots to prepare_booking.
- If a tool returns an error, share the error with the user.
- Never make up times, prices, or policies.
- Default to party of 2 if not specified.

AFTER BOOKING:
When confirm_booking succeeds, two calendar invites are automatically created:
one for the reservation itself and one for the cancellation deadline reminder.
Let the user know they'll get links to add them to their calendar.

Today is {today}."""

TOOLS = [
    types.Tool(function_declarations=[
        types.FunctionDeclaration(
            name="search_restaurant",
            description="Search for restaurants on Resy by name.",
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "query": types.Schema(type="STRING", description="Restaurant name"),
                },
                required=["query"],
            ),
        ),
        types.FunctionDeclaration(
            name="find_slots",
            description="Find available time slots for a restaurant on a date. Stores the venue context for subsequent booking calls.",
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "venue_id": types.Schema(type="INTEGER", description="Resy venue ID from search results"),
                    "venue_name": types.Schema(type="STRING", description="Restaurant name"),
                    "party_size": types.Schema(type="INTEGER", description="Number of guests (default 2)"),
                    "date": types.Schema(type="STRING", description="Date in YYYY-MM-DD format"),
                },
                required=["venue_id", "venue_name", "party_size", "date"],
            ),
        ),
        types.FunctionDeclaration(
            name="prepare_booking",
            description="Prepare a reservation for a specific time. Returns cancellation policy and fees. Present these terms to the user and wait for confirmation before calling confirm_booking. Pass the EXACT time string from find_slots.",
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "time": types.Schema(type="STRING", description="Time from find_slots results, e.g. '2026-03-09 21:15:00'"),
                },
                required=["time"],
            ),
        ),
        types.FunctionDeclaration(
            name="confirm_booking",
            description="Finalize the reservation after the user has accepted the cancellation terms.",
            parameters=types.Schema(
                type="OBJECT",
                properties={},
            ),
        ),
        types.FunctionDeclaration(
            name="watch_for_cancellations",
            description="Watch for a preferred time slot to open up. Polls periodically and notifies when a match is found.",
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "preferred_times": types.Schema(
                        type="ARRAY",
                        items=types.Schema(type="STRING"),
                        description="Preferred times in HH:MM 24h format, e.g. ['14:30', '15:00']",
                    ),
                },
                required=["preferred_times"],
            ),
        ),
        types.FunctionDeclaration(
            name="create_calendar_reminder",
            description="Create a calendar reminder (.ics file) for the cancellation deadline of the most recent booking. Use when the user asks for a calendar reminder.",
            parameters=types.Schema(type="OBJECT", properties={}),
        ),
        types.FunctionDeclaration(
            name="get_log",
            description="Retrieve the reservation log showing bookings and active watches.",
            parameters=types.Schema(type="OBJECT", properties={}),
        ),
        types.FunctionDeclaration(
            name="list_reservations",
            description="List the user's upcoming Resy reservations. Use when they ask about their reservations or want to cancel one.",
            parameters=types.Schema(type="OBJECT", properties={}),
        ),
        types.FunctionDeclaration(
            name="cancel_reservation",
            description="Cancel a reservation. Must call list_reservations first to get the resy_token. Always confirm with the user before cancelling.",
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "resy_token": types.Schema(type="STRING", description="The resy_token from list_reservations"),
                },
                required=["resy_token"],
            ),
        ),
    ])
]


# ---------------------------------------------------------------------------
# Time parsing helper
# ---------------------------------------------------------------------------

def _parse_hhmm(time_str: str) -> str | None:
    """Extract HH:MM (24h) from various time formats."""
    s = time_str.strip()
    # "2026-03-09 22:00:00" → take time part
    if " " in s:
        _, time_part = s.rsplit(" ", 1)
        if ":" in time_part:
            parts = time_part.split(":")
            return f"{parts[0]}:{parts[1]}"
    # "22:00:00" or "22:00" (no AM/PM)
    upper = s.upper()
    if ":" in s and "AM" not in upper and "PM" not in upper:
        parts = s.split(":")
        return f"{parts[0]}:{parts[1]}"
    # "10:00 PM", "9.45pm", "5pm", "9:15 AM", etc.
    for suffix in ("PM", "AM"):
        if suffix in upper:
            raw = upper.replace(suffix, "").strip().rstrip(".")
            raw = raw.replace(".", ":")  # "9.45" → "9:45"
            parts = raw.split(":")
            try:
                hour = int(parts[0])
                minute = int(parts[1]) if len(parts) > 1 else 0
            except ValueError:
                return None
            if suffix == "PM" and hour != 12:
                hour += 12
            elif suffix == "AM" and hour == 12:
                hour = 0
            return f"{hour:02d}:{minute:02d}"
    return None


class ChatSession:
    def __init__(self, resy_client: ResyClient):
        self.resy = resy_client

        # Server-side state — Gemini never sees config tokens or book tokens
        self._slot_cache: dict[str, dict] = {}   # start_time → raw slot dict
        self._venue_context: dict | None = None   # venue_id, venue_name, party_size, date
        self._pending_booking: dict | None = None # config_token, time, details (terms only)
        self._last_booking: dict | None = None    # saved after confirm for calendar on demand
        self._pending_calendars: list[tuple[str, str]] = []  # (cal_id, label) to send

        self.client = genai.Client(
            vertexai=True,
            project=os.getenv("GOOGLE_VERTEX_PROJECT_ID", "agentic-ai-for-analytics"),
            location=os.getenv("GOOGLE_VERTEX_REGION", "us-central1"),
        )
        self.chat = self.client.chats.create(
            model="gemini-2.0-flash",
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT.format(today=date.today().isoformat()),
                tools=TOOLS,
            ),
        )

    # ------------------------------------------------------------------
    # Slot lookup
    # ------------------------------------------------------------------

    def _resolve_slot(self, requested_time: str) -> dict | None:
        """Find a cached slot matching the requested time (any format)."""
        # Exact match first
        if requested_time in self._slot_cache:
            return self._slot_cache[requested_time]
        # Fuzzy match by HH:MM
        req_hhmm = _parse_hhmm(requested_time)
        if req_hhmm:
            for cached_time, cached_slot in self._slot_cache.items():
                if _parse_hhmm(cached_time) == req_hhmm:
                    return cached_slot
        return None

    # ------------------------------------------------------------------
    # Tool execution
    # ------------------------------------------------------------------

    def _execute_tool(self, tool_name: str, tool_input: dict) -> dict:
        try:
            if tool_name == "search_restaurant":
                venues = self.resy.search_venues(tool_input["query"])
                if not venues:
                    return {"error": "No restaurants found matching that search."}
                return {"venues": venues}

            elif tool_name == "find_slots":
                venue_id = int(tool_input["venue_id"])
                party_size = int(tool_input.get("party_size", 2))
                day = tool_input["date"]
                venue_name = tool_input.get("venue_name", "Unknown")

                self._venue_context = {
                    "venue_id": venue_id,
                    "venue_name": venue_name,
                    "party_size": party_size,
                    "date": day,
                }

                slots = self.resy.find_slots(venue_id, party_size, day)
                if not slots:
                    return {"error": "No available time slots found for that date."}

                self._slot_cache = {}
                simplified = []
                for s in slots:
                    start = s.get("date", {}).get("start", "unknown")
                    self._slot_cache[start] = s
                    simplified.append({
                        "time": start,
                        "type": s.get("config", {}).get("type", ""),
                    })
                return {"slots": simplified}

            elif tool_name == "prepare_booking":
                if not self._venue_context:
                    return {"error": "No venue context. Call find_slots first."}

                requested_time = tool_input.get("time", "")
                slot = self._resolve_slot(requested_time)
                if not slot:
                    available = list(self._slot_cache.keys())
                    return {
                        "error": f"Time '{requested_time}' not found in available slots. "
                        f"Available: {available}"
                    }

                ctx = self._venue_context
                config_token = slot["config"]["token"]
                details = self.resy.get_details(
                    config_token, ctx["date"], ctx["party_size"],
                )

                if not details["book_token"]:
                    return {"error": "Could not get a booking token. The slot may no longer be available."}

                # Store config_token (not book_token) so confirm_booking can
                # re-fetch a fresh book_token right before booking.
                # This avoids the 412 PRECONDITION FAILED error caused by
                # expired book_tokens.
                self._pending_booking = {
                    "config_token": config_token,
                    "time": requested_time,
                    "terms": {
                        "cancellation_policy": details.get("cancellation_policy"),
                        "cancellation_fee": details.get("cancellation_fee"),
                        "cancellation_deadline": details.get("cancellation_deadline"),
                        "payment_type": details.get("payment_type"),
                        "payment_total": details.get("payment_total"),
                    },
                }

                return {
                    "status": "ready",
                    "message": "Present these terms to the user. Do NOT call confirm_booking until they agree.",
                    "cancellation_policy": details.get("cancellation_policy"),
                    "cancellation_fee": details.get("cancellation_fee"),
                    "cancellation_deadline": details.get("cancellation_deadline"),
                    "payment_type": details.get("payment_type"),
                    "payment_total": details.get("payment_total"),
                }

            elif tool_name == "confirm_booking":
                if not self._pending_booking:
                    return {"error": "No pending booking. Call prepare_booking first."}
                if not self._venue_context:
                    return {"error": "No venue context. Start over with find_slots."}

                pending = self._pending_booking
                ctx = self._venue_context

                # Re-fetch a fresh book_token right before booking.
                # Book tokens expire quickly, so we can't reuse the one
                # from prepare_booking — that causes 412 errors.
                try:
                    fresh_details = self.resy.get_details(
                        pending["config_token"], ctx["date"], ctx["party_size"],
                    )
                except Exception as e:
                    log.exception("Failed to refresh booking details")
                    return {"error": f"Could not refresh booking details: {e}"}

                book_token = fresh_details.get("book_token")
                payment_method_id = fresh_details.get("payment_method_id")

                if not book_token:
                    return {"error": "The slot is no longer available. Try picking another time."}

                try:
                    result = self.resy.book(book_token, payment_method_id)
                except ResyBookingConflict as e:
                    log.warning("Booking conflict: %s", e)
                    return {"error": str(e)}
                except Exception as e:
                    log.exception("Booking failed")
                    return {"error": f"Booking failed: {e}"}

                resy_token = result.get("resy_token", "N/A")

                log_entry({
                    "status": "booked",
                    "venue": ctx["venue_name"],
                    "venue_id": ctx["venue_id"],
                    "date": ctx["date"],
                    "time": pending["time"],
                    "party_size": ctx["party_size"],
                    "confirmation_token": resy_token,
                    "booked_at": datetime.now().isoformat(),
                    "created_at": datetime.now().isoformat(),
                })

                result_for_gemini = {"result": "Reservation confirmed!"}

                # Create calendar event for the reservation itself
                try:
                    res_cal_id = create_reservation_event(
                        venue_name=ctx["venue_name"],
                        reservation_date=ctx["date"],
                        reservation_time=pending["time"],
                        party_size=ctx["party_size"],
                    )
                    self._pending_calendars.append((res_cal_id, "reservation"))
                    result_for_gemini["reservation_calendar_created"] = True
                except Exception:
                    log.exception("Failed to create reservation calendar event")

                # Create calendar reminder for cancellation deadline
                deadline = pending["terms"].get("cancellation_deadline")
                if deadline:
                    try:
                        cancel_cal_id = create_cancellation_reminder(
                            venue_name=ctx["venue_name"],
                            deadline_utc=deadline,
                            reservation_date=ctx["date"],
                            reservation_time=pending["time"],
                            party_size=ctx["party_size"],
                        )
                        self._pending_calendars.append((cancel_cal_id, "cancellation"))
                        result_for_gemini["cancellation_reminder_created"] = True
                    except Exception:
                        log.exception("Failed to create cancellation reminder")

                # Save for on-demand calendar creation
                self._last_booking = {
                    "venue_name": ctx["venue_name"],
                    "date": ctx["date"],
                    "time": pending["time"],
                    "party_size": ctx["party_size"],
                    "cancellation_deadline": deadline,
                }
                self._pending_booking = None
                return result_for_gemini

            elif tool_name == "create_calendar_reminder":
                if not self._last_booking:
                    return {"error": "No recent booking found. Book a reservation first."}
                deadline = self._last_booking.get("cancellation_deadline")
                if not deadline:
                    return {"error": "This booking has no cancellation deadline."}
                booking = self._last_booking
                cal_id = create_cancellation_reminder(
                    venue_name=booking["venue_name"],
                    deadline_utc=deadline,
                    reservation_date=booking["date"],
                    reservation_time=booking["time"],
                    party_size=booking["party_size"],
                )
                self._pending_calendars.append((cal_id, "cancellation"))
                return {"result": "Calendar reminder created. A download link will be sent to you."}

            elif tool_name == "watch_for_cancellations":
                if not self._venue_context:
                    return {"error": "No venue context. Call find_slots first."}
                ctx = self._venue_context
                params = {
                    "venue_id": ctx["venue_id"],
                    "venue_name": ctx["venue_name"],
                    "party_size": ctx["party_size"],
                    "date": ctx["date"],
                    "preferred_times": list(tool_input.get("preferred_times", [])),
                }
                return {"__watch__": True, "params": params}

            elif tool_name == "get_log":
                entries = load_log()
                if not entries:
                    return {"result": "No reservation log entries yet."}
                return {"entries": entries}

            elif tool_name == "list_reservations":
                reservations = self.resy.list_reservations()
                if not reservations:
                    return {"result": "You don't have any reservations."}
                # Only show upcoming ones (today or later)
                today = date.today().isoformat()
                upcoming = [r for r in reservations if (r.get("day") or "") >= today]
                if not upcoming:
                    return {"result": "You don't have any upcoming reservations."}
                # Store tokens for cancel lookups
                self._reservation_tokens = {
                    r["resy_token"]: r for r in upcoming
                }
                # Return simplified info to Gemini (no tokens exposed)
                simplified = []
                for r in upcoming:
                    simplified.append({
                        "venue_name": r["venue_name"],
                        "day": r["day"],
                        "time_slot": r["time_slot"],
                        "num_seats": r["num_seats"],
                        "cancel_allowed": r["cancel_allowed"],
                        "cancellation_policy": r["cancellation_policy"],
                        "resy_token": r["resy_token"],
                    })
                return {"reservations": simplified}

            elif tool_name == "cancel_reservation":
                resy_token = tool_input.get("resy_token", "")
                if not resy_token:
                    return {"error": "Missing resy_token. Call list_reservations first."}
                try:
                    result = self.resy.cancel(resy_token)
                except Exception as e:
                    log.exception("Cancel failed")
                    return {"error": f"Cancellation failed: {e}"}
                return {"result": "Reservation cancelled successfully."}

            else:
                return {"error": f"Unknown tool: {tool_name}"}

        except Exception as e:
            log.exception("Tool %s failed", tool_name)
            return {"error": str(e)}

    # ------------------------------------------------------------------
    # Message processing
    # ------------------------------------------------------------------

    def _extract_parts(self, response) -> tuple[list, list[str]]:
        """Safely extract function calls and text from a Gemini response."""
        function_calls = []
        texts = []
        try:
            candidates = response.candidates or []
            if not candidates:
                return [], []
            parts = candidates[0].content.parts or []
            for part in parts:
                if part.function_call:
                    function_calls.append(part.function_call)
                elif part.text:
                    texts.append(part.text)
        except (AttributeError, IndexError) as e:
            log.warning("Could not parse Gemini response: %s", e)
        return function_calls, texts

    async def process_message(self, user_text: str) -> list[dict]:
        """Process a user message. Returns list of event dicts:
          {"type": "text", "text": "..."}
          {"type": "watch", "params": {...}}
          {"type": "calendar", "cal_id": "..."}
        """
        events: list[dict] = []
        self._pending_calendars = []

        response = self.chat.send_message(user_text)

        max_rounds = 10  # safety limit
        for _ in range(max_rounds):
            function_calls, texts = self._extract_parts(response)
            for t in texts:
                events.append({"type": "text", "text": t})

            if not function_calls:
                break

            function_responses = []
            for fc in function_calls:
                log.info("Gemini called %s(%s)", fc.name, dict(fc.args))
                result = self._execute_tool(fc.name, dict(fc.args))

                if isinstance(result, dict) and result.get("__watch__"):
                    params = result["params"]
                    events.append({"type": "watch", "params": params})
                    result = {
                        "result": f"Now watching for cancellations at "
                        f"{', '.join(params['preferred_times'])} "
                        f"at {params['venue_name']} on {params['date']}."
                    }

                function_responses.append(
                    types.Part.from_function_response(name=fc.name, response=result)
                )

            try:
                response = self.chat.send_message(function_responses)
            except Exception:
                log.exception("Gemini send_message failed after tool execution")
                events.append({"type": "text", "text": "Sorry, I had trouble processing that. Please try again."})
                break

        for cal_id, label in self._pending_calendars:
            events.append({"type": "calendar", "cal_id": cal_id, "label": label})

        return events
