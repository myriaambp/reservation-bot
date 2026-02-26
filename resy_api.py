"""Resy API client — search, find, details, and book."""

import json
import logging
import requests

log = logging.getLogger(__name__)

BASE_URL = "https://api.resy.com"


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
        """Return {'book_token': ..., 'payment_method_id': ...}."""
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

        log.debug("Got book_token=%s, payment_id=%s", book_token, payment_id)
        return {"book_token": book_token, "payment_method_id": payment_id}

    # ------------------------------------------------------------------
    # Step 3: Book the reservation
    # ------------------------------------------------------------------
    def book(self, book_token: str, payment_method_id: int) -> dict:
        """Submit the booking. Returns the API response dict."""
        resp = self.session.post(
            f"{BASE_URL}/3/book",
            data={
                "book_token": book_token,
                "struct_payment_method": json.dumps({"id": payment_method_id}),
            },
        )
        resp.raise_for_status()
        return resp.json()
