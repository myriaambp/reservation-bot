# Reservation Bot

A restaurant reservation chatbot powered by the Resy API and Google Gemini AI. Search for restaurants, find available time slots, book and cancel reservations, watch for cancellations with auto-book, snipe table releases, and get calendar reminders — all through natural language via a web chat interface or WhatsApp.

## Features

- **Natural language chat** — search restaurants, check availability, book and cancel tables
- **Two-phase booking** — shows cancellation policy, fees, and deadline before confirming
- **Auto-book watches** — arms a watch that instantly books when a matching slot opens (two-phase: shows terms first, then arms on confirmation)
- **Snipe mode** — for table release times: sleeps until 30s before release, then polls every 2 seconds for 5 minutes
- **Time range matching** — watch for exact times (`19:00`) or ranges (`19:00-21:30`)
- **Multi-date watches** — poll multiple dates in a single watch; first match on any date wins
- **Aggressive polling** — 10-second cycles for normal watches, 2-second cycles during snipe windows
- **Calendar invites** — auto-generates .ics files for the reservation and cancellation deadline (with 1-hour reminders)
- **Reservation management** — list upcoming reservations and cancel directly through chat
- **Multiple channels** — web UI (WebSocket), WhatsApp (Twilio), SMS (Twilio, ready for future use)
- **Dashboard** — log panel with tabs for watches (active/booked/stopped/expired), reservations, and cancellations

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure environment variables

```bash
cp .env.example .env
```

Fill in your `.env`:

```
RESY_API_KEY=your_resy_api_key
RESY_AUTH_TOKEN=your_resy_auth_token

# Google Gemini (optional — defaults shown)
GOOGLE_VERTEX_PROJECT_ID=agentic-ai-for-analytics
GOOGLE_VERTEX_REGION=us-central1

# Twilio (required for WhatsApp/SMS)
TWILIO_ACCOUNT_SID=your_twilio_sid
TWILIO_AUTH_TOKEN=your_twilio_auth_token
TWILIO_FROM_NUMBER=+1234567890
TWILIO_SMS_FROM_NUMBER=+1234567890
NOTIFY_PHONE_NUMBER=+1234567890
```

### 3. Run the server

```bash
python app.py
```

The web UI is available at [http://localhost:8080](http://localhost:8080).

### 4. WhatsApp setup (optional)

1. Start an ngrok tunnel: `ngrok http 8080`
2. In the Twilio Console, go to **Messaging > WhatsApp Sandbox**
3. Set the webhook URL to `https://<ngrok-url>/whatsapp`
4. Send a message to the sandbox number to start chatting

### 5. SMS setup (optional)

1. Get a Twilio phone number with SMS capability
2. Set `TWILIO_SMS_FROM_NUMBER` in `.env`
3. Set the number's messaging webhook to `https://<ngrok-url>/sms`

## Usage

Just type naturally:

- `"Find a table at Au Cheval for 2 on March 9"` — search and check availability
- `"9:15 PM"` — pick a time slot (shows cancellation terms before booking)
- `"Yes"` — confirm the reservation
- `"What reservations do I have?"` — list upcoming reservations
- `"Cancel my reservation"` — cancel a booking

### Watching and sniping

- `"Watch for cancellations between 7 and 9 PM"` — bot shows cancellation terms, then arms auto-book on confirmation
- `"Watch for 7pm, 8pm, or 9pm on March 15 and 16"` — multi-date, multi-time watch
- `"Snipe tables at 9 AM tomorrow"` — bot sleeps until release time, then polls aggressively every 2 seconds
- `"Stop watching"` — cancel active watches

### How auto-book watches work

1. You ask to watch for a time slot
2. Bot fetches the venue's cancellation terms and presents them
3. You confirm — watch is armed with auto-book enabled
4. Bot polls every 10 seconds (or 2 seconds in snipe mode)
5. On match: books instantly, sends confirmation + calendar links
6. On booking conflict (412): notifies you, keeps watching
7. On other errors: notifies you, retries next cycle

### Special commands (WhatsApp/SMS)

- `status` / `log` / `my reservations` — view active watches and bookings
- `stop watching` / `cancel watch` — cancel all active watches

## Project Structure

```
app.py              FastAPI server (web UI, WebSocket, WhatsApp/SMS webhooks)
chat.py             Gemini AI chat session with Resy tool calling
resy_api.py         Resy API client (search, slots, book, cancel, list)
calendar_utils.py   .ics calendar file generation
sms.py              Twilio messaging (WhatsApp + SMS)
log_utils.py        JSON reservation log persistence
templates/          Web chat UI with dashboard
```

## API Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/` | GET | Web chat UI |
| `/ws` | WebSocket | Real-time chat |
| `/whatsapp` | POST | Twilio WhatsApp webhook |
| `/sms` | POST | Twilio SMS webhook |
| `/cal/{id}` | GET | Download .ics calendar file |
| `/api/log` | GET | Reservation log (JSON) |
