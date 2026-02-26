# Reservation Bot

A restaurant reservation chatbot powered by the Resy API and Google Gemini AI. Search for restaurants, find available time slots, watch for cancellations, and book reservations — all through natural language via a web chat interface, WhatsApp, or the command line.

## Features

- **Natural language chat** — ask the bot to search restaurants, check availability, and book tables
- **Cancellation watching** — polls Resy every 60 seconds and alerts you when a preferred time opens up
- **Three interfaces** — web UI (WebSocket), WhatsApp (Twilio webhook), and CLI
- **Reservation log** — tracks watches, bookings, and stops in a local JSON file

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

# Twilio (required for WhatsApp)
TWILIO_ACCOUNT_SID=your_twilio_sid
TWILIO_AUTH_TOKEN=your_twilio_auth_token
TWILIO_FROM_NUMBER=+1234567890
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

## Usage

### Web / WhatsApp

Just type naturally:

- `"Search Tatiana"` — find restaurants matching a query
- `"Find slots for 2 on June 15"` — check availability
- `"Watch for cancellations at 7pm or 8pm"` — start a cancellation watch
- `"Book it"` — confirm a reservation when a match is found
- `"Stop watching"` — cancel active watches (WhatsApp)

### CLI

```bash
python main.py         # interactive reservation flow
python main.py --log   # view reservation history
```

## Project Structure

```
app.py           FastAPI server (web UI, WebSocket, WhatsApp webhook)
chat.py          Gemini AI chat session with Resy tool calling
resy_api.py      Resy API client (search, slots, booking)
sms.py           Twilio WhatsApp messaging
log_utils.py     JSON reservation log persistence
main.py          Interactive CLI interface
templates/       Web chat UI
```
