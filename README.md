# Reservation Bot

A restaurant reservation chatbot powered by the Resy API and Google Gemini AI. Search for restaurants, find available time slots, book and cancel reservations, watch for cancellations, and get calendar reminders — all through natural language via a web chat interface or WhatsApp.

## Features

- **Natural language chat** — search restaurants, check availability, book and cancel tables
- **Two-phase booking** — shows cancellation policy, fees, and deadline before confirming
- **Calendar invites** — auto-generates .ics files for the reservation and cancellation deadline (with 1-hour reminders)
- **Cancellation watching** — polls Resy every 60 seconds and alerts you when a preferred time opens up
- **Reservation management** — list upcoming reservations and cancel directly through chat
- **Multiple channels** — web UI (WebSocket), WhatsApp (Twilio), SMS (Twilio, ready for future use)
- **Reservation log** — tracks bookings, watches, and stops in a local JSON file

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
- `"Watch for cancellations at 7pm or 8pm"` — start a cancellation watch
- `"Stop watching"` — cancel active watches

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
templates/          Web chat UI
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
