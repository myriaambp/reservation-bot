"""Interactive Resy reservation CLI."""

import os
import sys
import time
from datetime import datetime

from dotenv import load_dotenv
from resy_api import ResyClient
from log_utils import load_log, save_log, log_entry


def show_log():
    entries = load_log()
    if not entries:
        print("No entries yet.")
        return

    watching = [e for e in entries if e["status"] == "watching"]
    booked = [e for e in entries if e["status"] == "booked"]
    cancelled = [e for e in entries if e["status"] == "stopped"]

    if watching:
        print("\n--- Watching for cancellations ---")
        for e in watching:
            times = ", ".join(e["preferred_times"])
            print(f"  {e['venue']} | {e['date']} | party of {e['party_size']} | looking for: {times}")
            print(f"    started: {e['created_at']}")

    if booked:
        print("\n--- Confirmed reservations ---")
        for e in booked:
            print(f"  {e['venue']} | {e['date']} | {e.get('time', 'N/A')} | party of {e['party_size']}")
            token = e.get("confirmation_token", "N/A")
            print(f"    confirmed: {e.get('booked_at', 'N/A')} | token: {token}")

    if cancelled:
        print("\n--- Stopped watches ---")
        for e in cancelled:
            times = ", ".join(e["preferred_times"])
            print(f"  {e['venue']} | {e['date']} | was looking for: {times}")
            print(f"    stopped: {e.get('stopped_at', 'N/A')}")

    if not watching and not booked and not cancelled:
        print("No entries yet.")
    print()


def search_restaurant(client):
    """Search for a restaurant, allowing retries for typos."""
    while True:
        query = input("\nSearch for a restaurant: ").strip()
        if not query:
            print("No query entered.")
            return None

        try:
            venues = client.search_venues(query)
        except Exception as e:
            print(f"Search failed: {e}")
            return None

        if not venues:
            retry = input("No results found. Try a different search? (y/n): ").strip().lower()
            if retry == "y":
                continue
            return None

        print("\nResults:")
        for i, v in enumerate(venues, 1):
            cuisine = ", ".join(v["cuisine"]) if v["cuisine"] else "N/A"
            location = v["location"]
            neighborhood = v["neighborhood"]
            loc_display = f"{neighborhood}, {location}" if neighborhood else location
            print(f"  {i}. {v['name']} — {loc_display} ({cuisine})")

        print(f"  {len(venues) + 1}. None of these — search again")

        choice = input("\nSelect a restaurant (number): ").strip()
        try:
            idx = int(choice) - 1
        except ValueError:
            print("Invalid selection.")
            return None

        if idx == len(venues):
            continue

        if idx < 0 or idx >= len(venues):
            print("Invalid selection.")
            return None

        return venues[idx]


def watch_for_cancellations(client, venue, party_size, day):
    """Poll for preferred time slots and auto-book when one opens up."""
    raw = input("\nEnter preferred time(s), comma-separated (e.g. 14:30, 15:00): ").strip()
    if not raw:
        print("No times entered.")
        return

    preferred = [t.strip() for t in raw.split(",") if t.strip()]
    print(f"\nWatching for: {', '.join(preferred)}")
    print("Polling every 60 seconds. Press Ctrl+C to stop.\n")

    watch_entry = {
        "status": "watching",
        "venue": venue["name"],
        "venue_id": venue["id"],
        "date": day,
        "party_size": party_size,
        "preferred_times": preferred,
        "created_at": datetime.now().isoformat(),
    }
    log_entry(watch_entry)

    poll_interval = 60

    try:
        while True:
            now = datetime.now().strftime("%H:%M:%S")
            try:
                slots = client.find_slots(venue["id"], party_size, day)
            except Exception as e:
                print(f"[{now}] Poll error: {e}")
                time.sleep(poll_interval)
                continue

            # Check each slot for a preferred-time match
            matched_slot = None
            for slot in slots:
                start = slot.get("date", {}).get("start", "")
                # start looks like "2026-03-08 14:30:00"
                for pref in preferred:
                    if f" {pref}" in start:
                        matched_slot = slot
                        break
                if matched_slot:
                    break

            if not matched_slot:
                available = [s.get("date", {}).get("start", "?") for s in slots]
                print(f"[{now}] No match. Available: {', '.join(available) if available else 'none'}")
                time.sleep(poll_interval)
                continue

            # --- Match found — auto-book ---
            match_time = matched_slot.get("date", {}).get("start", "unknown")
            print(f"\n[{now}] Match found: {match_time}")

            config_id = matched_slot.get("config", {}).get("token")
            try:
                details = client.get_details(config_id, day, party_size)
            except Exception as e:
                print(f"Failed to get booking details: {e}")
                return

            book_token = details["book_token"]
            payment_method_id = details["payment_method_id"]

            if not book_token:
                print("Could not obtain a booking token.")
                return

            confirm = input(
                f"Book {venue['name']} for {party_size} on {day} at {match_time}? (y/n): "
            ).strip().lower()

            if confirm != "y":
                print("Skipped. Resuming watch...\n")
                time.sleep(poll_interval)
                continue

            try:
                result = client.book(book_token, payment_method_id)
                resy_token = result.get("resy_token", "N/A")
                print(f"\nReservation confirmed! Confirmation token: {resy_token}")
                # Update log: mark watch as booked
                entries = load_log()
                for e in entries:
                    if e is not None and e.get("venue_id") == venue["id"] and e.get("date") == day and e.get("status") == "watching":
                        e["status"] = "booked"
                        e["time"] = match_time
                        e["confirmation_token"] = resy_token
                        e["booked_at"] = datetime.now().isoformat()
                        break
                save_log(entries)
            except Exception as e:
                print(f"\nBooking failed: {e}")
            return

    except KeyboardInterrupt:
        # Update log: mark watch as stopped
        entries = load_log()
        for e in entries:
            if e is not None and e.get("venue_id") == venue["id"] and e.get("date") == day and e.get("status") == "watching":
                e["status"] = "stopped"
                e["stopped_at"] = datetime.now().isoformat()
                break
        save_log(entries)
        print("\n\nStopped watching. Goodbye!")


def main():
    load_dotenv()

    if "--log" in sys.argv:
        show_log()
        return

    api_key = os.getenv("RESY_API_KEY")
    auth_token = os.getenv("RESY_AUTH_TOKEN")

    if not api_key or not auth_token:
        print("Error: RESY_API_KEY and RESY_AUTH_TOKEN must be set in .env or environment.")
        sys.exit(1)

    client = ResyClient(api_key, auth_token)

    # --- Search ---
    venue = search_restaurant(client)
    if not venue:
        return

    # --- Party size & date ---
    try:
        party_size = int(input("\nParty size: ").strip())
    except ValueError:
        print("Invalid party size.")
        return

    day = input("Date (YYYY-MM-DD): ").strip()

    # --- Find slots ---
    try:
        slots = client.find_slots(venue["id"], party_size, day)
    except Exception as e:
        print(f"Failed to fetch time slots: {e}")
        return

    if not slots:
        print("No available time slots.")
        return

    print("\nAvailable times:")
    for i, slot in enumerate(slots, 1):
        start = slot.get("date", {}).get("start", "unknown")
        slot_type = slot.get("config", {}).get("type", "")
        print(f"  {i}. {start}  ({slot_type})")

    watch_option = len(slots) + 1
    print(f"  {watch_option}. None of these — watch for a specific time")

    # --- Select slot ---
    slot_choice = input("\nSelect a time slot (number): ").strip()
    try:
        choice_idx = int(slot_choice)
    except ValueError:
        print("Invalid selection.")
        return

    if choice_idx == watch_option:
        watch_for_cancellations(client, venue, party_size, day)
        return

    try:
        selected_slot = slots[choice_idx - 1]
    except IndexError:
        print("Invalid selection.")
        return

    config_id = selected_slot.get("config", {}).get("token")
    start_time = selected_slot.get("date", {}).get("start", "unknown")

    # --- Get booking details ---
    try:
        details = client.get_details(config_id, day, party_size)
    except Exception as e:
        print(f"Failed to get booking details: {e}")
        return

    book_token = details["book_token"]
    payment_method_id = details["payment_method_id"]

    if not book_token:
        print("Could not obtain a booking token.")
        return

    # --- Confirm ---
    confirm = input(
        f"\nBook {venue['name']} for {party_size} on {day} at {start_time}? (y/n): "
    ).strip().lower()

    if confirm != "y":
        print("Booking cancelled.")
        return

    # --- Book ---
    try:
        result = client.book(book_token, payment_method_id)
        resy_token = result.get("resy_token", "N/A")
        print(f"\nReservation confirmed! Confirmation token: {resy_token}")
        log_entry({
            "status": "booked",
            "venue": venue["name"],
            "venue_id": venue["id"],
            "date": day,
            "time": start_time,
            "party_size": party_size,
            "confirmation_token": resy_token,
            "booked_at": datetime.now().isoformat(),
            "created_at": datetime.now().isoformat(),
        })
    except Exception as e:
        print(f"\nBooking failed: {e}")


if __name__ == "__main__":
    main()
