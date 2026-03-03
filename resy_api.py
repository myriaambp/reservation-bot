"""Resy API client — search, find, details, and book."""

import json
import logging
from datetime import datetime

import requests

log = logging.getLogger(__name__)

BASE_URL = "https://api.resy.com"


class ResyBookingConflict(Exception):
    """Raised when the user already has a reservation at the venue on this day."""

    def __init__(self, venue_name: str, existing_day: str, existing_time: str):
        self.venue_name = venue_name
        self.existing_day = existing_day
        self.existing_time = existing_time
        # Format "2026-03-09" → "March 9" and "17:00:00" → "5:00 PM"
        try:
            day_fmt = datetime.strptime(existing_day, "%Y-%m-%d").strftime("%B %-d")
        except (ValueError, TypeError):
            day_fmt = existing_day
        try:
            time_fmt = datetime.strptime(existing_time, "%H:%M:%S").strftime("%-I:%M %p")
        except (ValueError, TypeError):
            time_fmt = existing_time
        super().__init__(
            f"You already have a reservation at {venue_name} on {day_fmt} "
            f"at {time_fmt}. Cancel the existing one first, or pick a different restaurant."
        )


class ResyClient:
    def __init__(self, api_key: str, auth_token: str):
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f'ResyAPI api_key="{api_key}"',
            "x-resy-auth-token": auth_token,
            "x-resy-universal-auth": auth_token,
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://resy.com",
            "Referer": "https://resy.com/",
            "X-Origin": "https://resy.com",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.6 Safari/605.1.15"
            ),
        })

    # ------------------------------------------------------------------
    # Venue search
    # ------------------------------------------------------------------
    def search_venues(self, query: str, per_page: int = 5) -> list[dict]:
        """Search for venues by name. Returns list of venue dicts."""
        resp = self.session.post(
            f"{BASE_URL}/3/venuesearch/search",
            json={"query": query, "per_page": per_page, "types": ["venue"]},
        )
        resp.raise_for_status()
        data = resp.json()

        results = []
        for hit in data.get("search", {}).get("hits", []):
            results.append({
                "id": hit.get("id", {}).get("resy"),
                "name": hit.get("name"),
                "location": hit.get("location", {}).get("name", ""),
                "neighborhood": hit.get("neighborhood", ""),
                "cuisine": hit.get("cuisine", []),
            })
        return results

    # ------------------------------------------------------------------
    # Step 1: Find available slots
    # ------------------------------------------------------------------
    def find_slots(self, venue_id: int, party_size: int, day: str) -> list[dict]:
        """Return list of available slots for a venue on a given day."""
        resp = self.session.get(
            f"{BASE_URL}/4/find",
            params={
                "venue_id": venue_id,
                "party_size": party_size,
                "day": day,
                "lat": 0,
                "long": 0,
            },
        )
        resp.raise_for_status()
        data = resp.json()

        venues = data.get("results", {}).get("venues", [])
        if not venues:
            return []

        slots = venues[0].get("slots", [])
        log.debug("Found %d slots for venue %s on %s", len(slots), venue_id, day)
        return slots

    # ------------------------------------------------------------------
    # Step 2: Get booking details (book_token + payment_method_id)
    # ------------------------------------------------------------------
    def get_details(self, config_id: str, day: str, party_size: int) -> dict:
        """Return book_token, payment_method_id, and cancellation/payment terms."""
        resp = self.session.get(
            f"{BASE_URL}/3/details",
            params={
                "config_id": config_id,
                "day": day,
                "party_size": party_size,
            },
        )
        resp.raise_for_status()
        data = resp.json()

        book_token = data.get("book_token", {}).get("value")
        payment_id = (
            data.get("user", {})
            .get("payment_methods", [{}])[0]
            .get("id")
        )

        # Extract cancellation terms — every nested value can be None
        cancellation = data.get("cancellation") or {}
        display = cancellation.get("display") or {}
        policy_list = display.get("policy") or []
        cancellation_policy = policy_list[0] if policy_list else None

        fee = cancellation.get("fee") or {}
        fee_display = fee.get("display") or {}
        cancellation_fee = fee_display.get("amount")

        # Deadline lives in fee, refund, or credit — check all three
        cancellation_deadline = (
            fee.get("date_cut_off")
            or (cancellation.get("refund") or {}).get("date_cut_off")
            or (cancellation.get("credit") or {}).get("date_cut_off")
        )

        payment = data.get("payment") or {}
        payment_config = payment.get("config") or {}
        payment_type = payment_config.get("type")
        payment_amounts = payment.get("amounts") or {}
        payment_total = payment_amounts.get("total", 0.0)

        log.debug("Got book_token=%s, payment_id=%s", book_token, payment_id)
        return {
            "book_token": book_token,
            "payment_method_id": payment_id,
            "cancellation_policy": cancellation_policy,
            "cancellation_deadline": cancellation_deadline,
            "cancellation_fee": cancellation_fee,
            "payment_type": payment_type,
            "payment_total": payment_total,
        }

    # ------------------------------------------------------------------
    # Step 3: Book the reservation
    # ------------------------------------------------------------------
    def book(self, book_token: str, payment_method_id: int) -> dict:
        """Submit the booking. Returns the API response dict.

        Raises:
            ResyBookingConflict: If the user already has a reservation at
                this venue on the same day (HTTP 412).
            requests.HTTPError: For other HTTP errors.
        """
        resp = self.session.post(
            f"{BASE_URL}/3/book",
            data={
                "book_token": book_token,
                "struct_payment_method": json.dumps({"id": payment_method_id}),
            },
        )
        if resp.status_code == 412:
            # Resy returns 412 when the user already has a reservation
            # at this venue on the same day.
            data = resp.json()
            specs = data.get("specs") or {}
            venue = data.get("venue") or {}
            raise ResyBookingConflict(
                venue_name=venue.get("name", "this restaurant"),
                existing_day=specs.get("day", ""),
                existing_time=specs.get("time_slot", ""),
            )
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # List user reservations
    # ------------------------------------------------------------------
    def list_reservations(self) -> list[dict]:
        """Return the user's upcoming reservations."""
        resp = self.session.get(f"{BASE_URL}/3/user/reservations")
        resp.raise_for_status()
        data = resp.json()

        venues_map = data.get("venues") or {}
        results = []
        for r in data.get("reservations", []):
            venue_info = r.get("venue") or {}
            venue_id = str(venue_info.get("id", ""))
            venue_data = venues_map.get(venue_id) or {}
            venue_name = venue_data.get("name", "Unknown")

            cancellation = r.get("cancellation") or {}
            results.append({
                "venue_name": venue_name,
                "venue_id": venue_info.get("id"),
                "day": r.get("day"),
                "time_slot": r.get("time_slot"),
                "num_seats": r.get("num_seats"),
                "resy_token": r.get("resy_token"),
                "cancel_allowed": cancellation.get("allowed", False),
                "cancellation_policy": (r.get("cancellation_policy") or [None])[0],
                "reservation_id": r.get("reservation_id"),
            })
        return results

    # ------------------------------------------------------------------
    # Cancel a reservation
    # ------------------------------------------------------------------
    def cancel(self, resy_token: str) -> dict:
        """Cancel a reservation by its resy_token."""
        resp = self.session.post(
            f"{BASE_URL}/3/cancel",
            data={"resy_token": resy_token},
        )
        resp.raise_for_status()
        return resp.json()
