"""Place a test call: our caller plays a patient, the number you dial answers.

Pick the scenario the caller should run -- each one probes a different weak
spot in the doctor's-office assistant on the other end:

    python index.py                    # menu
    python index.py invalid-dates      # straight to one
    python index.py --list             # just print them

Running this costs money and rings a real phone. The server runs on your
machine, so Twilio reaches it through an ngrok tunnel:

    python server.py           # terminal 1
    ngrok http 5050            # terminal 2
    python index.py            # terminal 3

The tunnel's URL is discovered automatically; PUBLIC_URL overrides it. Before
dialling, this checks that the tunnel really does reach the server, because a
misrouted tunnel costs a call to discover otherwise.

If the call still fails, `python debug_twilio.py` reports what Twilio saw.
"""

import json
import logging
import os
import sys
import urllib.error
import urllib.request

from dotenv import load_dotenv
from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client

import scenarios
from tunnel import local_port, ngrok_tunnel, no_tunnel_help, public_url

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("caller")

account_sid = os.getenv("TWILIO_ACCOUNT_SID")
auth_token = os.getenv("TWILIO_AUTH_TOKEN")
to_number = os.getenv("TO_NUMBER", "+REDACTED_PHONE_NUMBER")
from_number = os.getenv("FROM_NUMBER", "REDACTED_PHONE_NUMBER")

client = Client(account_sid, auth_token)


def check_reachable(url):
    """Confirm the tunnel reaches our server and the server is happy."""
    try:
        with urllib.request.urlopen(f"{url}/", timeout=10) as response:
            health = json.load(response)
    except Exception as exc:
        log.error("%s/ is not answering: %s: %s", url, type(exc).__name__, exc)
        log.error("Is `python server.py` running, and is ngrok pointed at it?")
        return False

    if "problems" not in health:
        log.error(
            "%s/ answered, but not with our server's health JSON -- the tunnel "
            "is pointed at something else.",
            url,
        )
        return False
    for problem in health["problems"]:
        log.error("server reports: %s", problem)
    return not health["problems"]


def choose_scenario(argument=None):
    """Resolve a slug or menu number, prompting if nothing was given."""
    if argument:
        scenario = scenarios.resolve(argument)
        if not scenario:
            log.error("no scenario called %r", argument)
            print("\n" + scenarios.listing())
            sys.exit(1)
        return scenario

    print("\nWhich edge case should the caller test?\n")
    print(scenarios.listing())
    try:
        answer = input("\nnumber or name: ")
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(1)
    scenario = scenarios.resolve(answer)
    if not scenario:
        log.error("no scenario called %r", answer.strip())
        sys.exit(1)
    return scenario


def callTwilio(scenario=None):
    """Call a specified number and run one test scenario"""
    scenario = scenario or scenarios.DEFAULT
    if not (account_sid and auth_token):
        log.error("TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN are not set; check .env")
        sys.exit(1)

    url = public_url()
    if not url:
        log.error(no_tunnel_help())
        sys.exit(1)
    if url.startswith("http://") or "localhost" in url or "127.0.0.1" in url:
        log.error(
            "PUBLIC_URL is %s -- Twilio's servers cannot reach that. It needs "
            "to be the tunnel's public https address.",
            url,
        )
        sys.exit(1)

    _, forwarded_to = ngrok_tunnel()
    if forwarded_to and not forwarded_to.endswith(f":{local_port()}"):
        log.warning(
            "ngrok is forwarding to %s but the server defaults to port %d -- "
            "set PORT to match if the check below fails.",
            forwarded_to,
            local_port(),
        )

    log.info("tunnel: %s -> %s", url, forwarded_to or "(not ngrok)")
    if not check_reachable(url):
        log.error("not placing the call; fix the above first")
        sys.exit(1)
    log.info("server is reachable through the tunnel")

    log.info("scenario: %s -- %s", scenario.slug, scenario.title)
    log.info("probing: %s (about %s)", scenario.probes, scenario.minutes)
    log.info("calling %s from %s", to_number, from_number)
    log.info("Twilio will fetch %s/voice when the call is answered", url)

    try:
        call = client.calls.create(
            # Twilio passes our query string straight back to /voice, which is
            # how the scenario reaches the server.
            url=f"{url}/voice?scenario={scenario.slug}",
            to=to_number,
            from_=from_number,
            # Twilio POSTs here as the call progresses; the server logs each
            # one, including the error code when a call fails.
            status_callback=f"{url}/call-status",
            status_callback_event=["initiated", "ringing", "answered", "completed"],
            status_callback_method="POST",
        )
    except TwilioRestException as exc:
        log.error("Twilio rejected the call: [%s] %s", exc.code, exc.msg)
        if exc.more_info:
            log.error("more info: %s", exc.more_info)
        sys.exit(1)

    log.info("call created: sid=%s status=%s", call.sid, call.status)
    log.info("watch the server terminal for the conversation")
    log.info("the bug report lands in recordings/ when the call ends")
    print(call.sid)
    return call


if __name__ == "__main__":
    argument = sys.argv[1] if len(sys.argv) > 1 else None
    if argument in ("--list", "-l", "list"):
        print(scenarios.listing())
        sys.exit(0)
    callTwilio(choose_scenario(argument))
