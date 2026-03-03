"""Send messages via Twilio (SMS or WhatsApp)."""

import os

from twilio.rest import Client


def send_message(body: str, to: str, channel: str = "whatsapp") -> None:
    """Send a message via SMS or WhatsApp.

    Args:
        body: Message text.
        to: Destination number (e.g. "+1234567890" or "whatsapp:+1234567890").
        channel: "sms" or "whatsapp".
    """
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")

    if not all([account_sid, auth_token]):
        return

    client = Client(account_sid, auth_token)

    if channel == "whatsapp":
        from_number = os.getenv("TWILIO_FROM_NUMBER")
        if not from_number:
            return
        from_ = f"whatsapp:{from_number}"
        to_ = to if to.startswith("whatsapp:") else f"whatsapp:{to}"
    else:
        from_number = os.getenv("TWILIO_SMS_FROM_NUMBER", os.getenv("TWILIO_FROM_NUMBER"))
        if not from_number:
            return
        from_ = from_number
        to_ = to.replace("whatsapp:", "")  # strip prefix if present

    client.messages.create(body=body, from_=from_, to=to_)


# Backwards-compatible alias
def send_sms(body: str, to: str | None = None) -> None:
    """Send a WhatsApp message (legacy helper)."""
    to_number = to or os.getenv("NOTIFY_PHONE_NUMBER")
    if not to_number:
        return
    send_message(body, to=to_number, channel="whatsapp")
