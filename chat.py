"""Gemini integration for the reservation chat bot."""

import json
import os
from datetime import date, datetime

from google import genai
from google.genai import types
from resy_api import ResyClient
from log_utils import load_log, save_log, log_entry

SYSTEM_PROMPT = """You are a helpful restaurant reservation assistant powered by Resy. You help users search for restaurants, find available time slots, book reservations, and watch for cancellations.

When a user asks to find a restaurant or make a reservation:
1. Search for the restaurant by name using search_restaurant
2. Find available slots using find_slots with the venue_id from search results
3. Present the available times to the user
4. If they want to book, use book_reservation with the config_token from the slot they chose
5. If their preferred time isn't available, offer to watch for cancellations using watch_for_cancellations

Be conversational and helpful. Format times in a readable way (e.g., "2:30 PM" instead of "14:30:00").
When presenting search results, show the restaurant name, location, and cuisine.
When presenting time slots, list them clearly and ask which one the user wants.

Always confirm before booking. If the user says to book a specific time, find the matching slot and use its config_token.

Today's date is {today}. When a user says a date like "March 8" without a year, assume the current or next occurrence of that date."""

# Gemini tool declarations
TOOLS = [
    types.Tool(function_declarations=[
        types.FunctionDeclaration(
            name="search_restaurant",
            description="Search for restaurants on Resy by name. Returns a list of matching venues with id, name, location, neighborhood, and cuisine.",
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "query": types.Schema(type="STRING", description="Restaurant name to search for"),
                },
                required=["query"],
            ),
        ),
        types.FunctionDeclaration(
            name="find_slots",
            description="Find available reservation time slots for a restaurant on a given date. Returns a list of available slots with times and config tokens.",
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "venue_id": types.Schema(type="INTEGER", description="Resy venue ID"),
                    "party_size": types.Schema(type="INTEGER", description="Number of guests"),
                    "date": types.Schema(type="STRING", description="Date in YYYY-MM-DD format"),
                },
                required=["venue_id", "party_size", "date"],
            ),
        ),
        types.FunctionDeclaration(
            name="book_reservation",
            description="Book a specific reservation slot. Requires a config_token from find_slots results. This will get booking details and complete the reservation.",
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "config_token": types.Schema(type="STRING", description="Config token from the selected slot"),
                    "date": types.Schema(type="STRING", description="Date in YYYY-MM-DD format"),
                    "party_size": types.Schema(type="INTEGER", description="Number of guests"),
                    "venue_name": types.Schema(type="STRING", description="Name of the restaurant (for logging)"),
                    "venue_id": types.Schema(type="INTEGER", description="Resy venue ID (for logging)"),
                    "time": types.Schema(type="STRING", description="Time of the reservation (for logging)"),
                },
                required=["config_token", "date", "party_size", "venue_name"],
            ),
        ),
        types.FunctionDeclaration(
            name="watch_for_cancellations",
            description="Start watching for a preferred time slot to open up. Polls every 60 seconds and notifies when a matching slot becomes available. Use when the user wants a time that isn't currently available.",
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "venue_id": types.Schema(type="INTEGER", description="Resy venue ID"),
                    "venue_name": types.Schema(type="STRING", description="Restaurant name"),
                    "party_size": types.Schema(type="INTEGER", description="Number of guests"),
                    "date": types.Schema(type="STRING", description="Date in YYYY-MM-DD format"),
                    "preferred_times": types.Schema(
                        type="ARRAY",
                        items=types.Schema(type="STRING"),
                        description="List of preferred times in HH:MM format (e.g. ['14:30', '15:00'])",
                    ),
                },
                required=["venue_id", "venue_name", "party_size", "date", "preferred_times"],
            ),
        ),
        types.FunctionDeclaration(
            name="get_log",
            description="Retrieve the reservation log showing all bookings and active watches.",
            parameters=types.Schema(
                type="OBJECT",
                properties={},
            ),
        ),
    ])
]


class ChatSession:
    def __init__(self, resy_client: ResyClient):
        self.resy = resy_client
        self.client = genai.Client(
            vertexai=True,
            project=os.getenv("GOOGLE_VERTEX_PROJECT_ID", "agentic-ai-for-analytics"),
            location=os.getenv("GOOGLE_VERTEX_REGION", "us-central1"),
        )
        system = SYSTEM_PROMPT.format(today=date.today().isoformat())
        self.chat = self.client.chats.create(
            model="gemini-2.0-flash",
            config=types.GenerateContentConfig(
                system_instruction=system,
                tools=TOOLS,
            ),
        )

    def _execute_tool(self, tool_name: str, tool_input: dict) -> dict:
        """Execute a tool call and return the result as a dict for Gemini."""
        try:
            if tool_name == "search_restaurant":
                venues = self.resy.search_venues(tool_input["query"])
                if not venues:
                    return {"result": "No restaurants found matching that search."}
                return {"venues": venues}

            elif tool_name == "find_slots":
                slots = self.resy.find_slots(
                    int(tool_input["venue_id"]),
                    int(tool_input["party_size"]),
                    tool_input["date"],
                )
                if not slots:
                    return {"result": "No available time slots found for that date."}
                simplified = []
                for s in slots:
                    simplified.append({
                        "time": s.get("date", {}).get("start", "unknown"),
                        "end": s.get("date", {}).get("end", "unknown"),
                        "type": s.get("config", {}).get("type", ""),
                        "config_token": s.get("config", {}).get("token", ""),
                    })
                return {"slots": simplified}

            elif tool_name == "book_reservation":
                details = self.resy.get_details(
                    tool_input["config_token"],
                    tool_input["date"],
                    int(tool_input["party_size"]),
                )
                book_token = details["book_token"]
                payment_method_id = details["payment_method_id"]
                if not book_token:
                    return {"result": "Could not obtain a booking token."}
                result = self.resy.book(book_token, payment_method_id)
                resy_token = result.get("resy_token", "N/A")
                log_entry({
                    "status": "booked",
                    "venue": tool_input.get("venue_name", "Unknown"),
                    "venue_id": tool_input.get("venue_id", 0),
                    "date": tool_input["date"],
                    "time": tool_input.get("time", ""),
                    "party_size": int(tool_input["party_size"]),
                    "confirmation_token": resy_token,
                    "booked_at": datetime.now().isoformat(),
                    "created_at": datetime.now().isoformat(),
                })
                return {"result": f"Reservation confirmed! Confirmation token: {resy_token}"}

            elif tool_name == "watch_for_cancellations":
                return {"__watch__": True, "params": dict(tool_input)}

            elif tool_name == "get_log":
                entries = load_log()
                if not entries:
                    return {"result": "No reservation log entries yet."}
                return {"entries": entries}

            else:
                return {"error": f"Unknown tool: {tool_name}"}

        except Exception as e:
            return {"error": str(e)}

    async def process_message(self, user_text: str) -> list[dict]:
        """Process a user message and return a list of response events.

        Each event is a dict:
          {"type": "text", "text": "..."} — bot message
          {"type": "watch", "params": {...}} — start a watch
        """
        events = []

        response = self.chat.send_message(user_text)

        while True:
            # Check for function calls
            function_calls = []
            for part in response.candidates[0].content.parts:
                if part.function_call:
                    function_calls.append(part.function_call)
                elif part.text:
                    events.append({"type": "text", "text": part.text})

            if not function_calls:
                break

            # Execute all function calls and build responses
            function_responses = []
            for fc in function_calls:
                result = self._execute_tool(fc.name, dict(fc.args))

                # Check if this is a watch request
                if isinstance(result, dict) and result.get("__watch__"):
                    params = result["params"]
                    events.append({"type": "watch", "params": params})
                    result = {
                        "result": f"Started watching for cancellations at {', '.join(params.get('preferred_times', []))} at {params.get('venue_name', '')} on {params.get('date', '')}."
                    }

                function_responses.append(
                    types.Part.from_function_response(
                        name=fc.name,
                        response=result,
                    )
                )

            # Send function results back to Gemini
            response = self.chat.send_message(function_responses)

        return events
