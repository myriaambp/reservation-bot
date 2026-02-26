"""Send WhatsApp notifications via Twilio."""

import os

from twilio.rest import Client


def send_sms(body: str, to: str | None = None) -> None:
    """Send a WhatsApp message using Twilio credentials from environment variables."""
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    from_number = os.getenv("TWILIO_FROM_NUMBER")
    to_number = to or os.getenv("NOTIFY_PHONE_NUMBER")

    if not all([account_sid, auth_token, from_number, to_number]):
        return

    client = Client(account_sid, auth_token)
    client.messages.create(
        body=body,
        from_=f"whatsapp:{from_number}",
        to=to_number if to_number.startswith("whatsapp:") else f"whatsapp:{to_number}",
    )
