"""Place an outbound call that hands the conversation to the assistant.

Running this costs money and rings a real phone. The `url` below must be a
publicly reachable /voice endpoint -- Twilio's servers fetch it, so localhost
will never work. Deploy first, or tunnel with ngrok and set PUBLIC_URL.
"""

import os

from dotenv import load_dotenv
from twilio.rest import Client

load_dotenv()

account_sid = os.getenv("TWILIO_ACCOUNT_SID")
auth_token = os.getenv("TWILIO_AUTH_TOKEN")
public_url = os.getenv(
    "PUBLIC_URL", "https://aivoiceassistant-production-6cb1.up.railway.app"
)
to_number = os.getenv("TO_NUMBER", "REDACTED_PHONE_NUMBER")
from_number = os.getenv("FROM_NUMBER", "REDACTED_PHONE_NUMBER")

client = Client(account_sid, auth_token)


def callTwilio():
    """Call a specified number and hold a conversation"""
    call = client.calls.create(
        url=f"{public_url.rstrip('/')}/voice",
        to=to_number,
        from_=from_number,
    )
    print(call.sid)


if __name__ == "__main__":
    callTwilio()
