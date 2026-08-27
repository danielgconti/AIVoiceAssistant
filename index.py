"""Place an outbound call that hands the conversation to the assistant.

Running this costs money and rings a real phone. The `url` below must be a
publicly reachable /voice endpoint -- Twilio's servers fetch it, so localhost
will never work. Deploy first, or tunnel with ngrok and set PUBLIC_URL.

If the call fails, `python debug_twilio.py` reports what Twilio saw.
"""

import logging
import os
import sys

from dotenv import load_dotenv
from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("caller")

account_sid = os.getenv("TWILIO_ACCOUNT_SID")
auth_token = os.getenv("TWILIO_AUTH_TOKEN")
public_url = os.getenv(
    "PUBLIC_URL", "https://aivoiceassistant-production-6cb1.up.railway.app"
).rstrip("/")
to_number = os.getenv("TO_NUMBER", "REDACTED_PHONE_NUMBER")
from_number = os.getenv("FROM_NUMBER", "REDACTED_PHONE_NUMBER")

client = Client(account_sid, auth_token)


def callTwilio():
    """Call a specified number and hold a conversation"""
    if not (account_sid and auth_token):
        log.error("TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN are not set; check .env")
        sys.exit(1)
    if public_url.startswith("http://localhost") or "127.0.0.1" in public_url:
        log.error(
            "PUBLIC_URL is %s -- Twilio's servers cannot reach your machine. "
            "Deploy, or tunnel with ngrok and set PUBLIC_URL to the tunnel.",
            public_url,
        )
        sys.exit(1)

    log.info("calling %s from %s", to_number, from_number)
    log.info("Twilio will fetch %s/voice when the call is answered", public_url)

    try:
        call = client.calls.create(
            url=f"{public_url}/voice",
            to=to_number,
            from_=from_number,
            # Twilio POSTs here as the call progresses; the server logs each
            # one, including the error code when a call fails.
            status_callback=f"{public_url}/call-status",
            status_callback_event=["initiated", "ringing", "answered", "completed"],
            status_callback_method="POST",
        )
    except TwilioRestException as exc:
        log.error("Twilio rejected the call: [%s] %s", exc.code, exc.msg)
        if exc.more_info:
            log.error("more info: %s", exc.more_info)
        sys.exit(1)

    log.info("call created: sid=%s status=%s", call.sid, call.status)
    print(call.sid)
    return call


if __name__ == "__main__":
    callTwilio()
