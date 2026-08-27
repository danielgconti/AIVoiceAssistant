"""Diagnose a failing call without having to place another one.

    python debug_twilio.py

Checks, in the order things usually break:

1. local configuration -- are the three credentials actually present
2. the tunnel -- is ngrok up, and does /voice through it return usable TwiML
3. recent calls -- how Twilio says they ended
4. Twilio's debugger alerts -- Twilio's own record of why it gave up

Step 4 is the one that explains "an application error has occurred": Twilio
logs the exact HTTP status and body it got back from the webhook.
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

from dotenv import load_dotenv

from tunnel import local_port, ngrok_tunnel, no_tunnel_help, public_url

load_dotenv()

ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
PUBLIC_URL = public_url()


def section(title):
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


def check_config():
    section("1. local configuration")
    for name in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "OPENAI_API_KEY"):
        value = os.getenv(name)
        if value:
            print(f"  OK      {name} = {value[:6]}...{value[-4:]} ({len(value)} chars)")
        else:
            print(f"  MISSING {name}")
    tunnel_url, forwarded_to = ngrok_tunnel()
    if PUBLIC_URL:
        source = "PUBLIC_URL" if os.getenv("PUBLIC_URL") else "ngrok"
        print(f"  OK      tunnel ({source}) = {PUBLIC_URL}")
        if forwarded_to:
            print(f"          forwarding to {forwarded_to} (server port is {local_port()})")
    else:
        print("  MISSING tunnel -- ngrok is not running and PUBLIC_URL is unset")


def fetch(url, data=None):
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(data).encode() if data else None,
        headers={"Content-Type": "application/x-www-form-urlencoded"} if data else {},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def check_server():
    section("2. the tunnel and the local server behind it")
    if not PUBLIC_URL:
        print("  skipped: " + no_tunnel_help().replace("\n", "\n  "))
        return

    status, body = fetch(f"{PUBLIC_URL}/")
    print(f"  GET  / -> {status}")
    if status is None:
        print(f"  unreachable through the tunnel: {body}")
        print("  Twilio cannot reach it either, so every call will fail.")
        print(f"  Check that `python server.py` is running on port {local_port()}")
        print("  and that ngrok is forwarding to that same port.")
        return
    try:
        for key, value in json.loads(body).items():
            print(f"      {key}: {value}")
    except ValueError:
        print(f"      {body[:400]}")

    status, body = fetch(
        f"{PUBLIC_URL}/voice",
        {
            "CallSid": "CAdebug0000000000000000000000000000",
            "From": "+15550000000",
            "To": "+15550000001",
            "CallStatus": "in-progress",
            "Direction": "outbound-api",
        },
    )
    print(f"\n  POST /voice -> {status}")
    print(f"      {body[:600]}")
    if status != 200:
        print("\n  Twilio needs a 200 here. Anything else and the caller hears")
        print("  'an application error has occurred'. The body above, or the")
        print("  server's own logs, will say why.")
    elif "<Stream" not in body:
        print("\n  The TwiML has no <Stream>, so the call will not reach OpenAI.")
        print("  The server is probably reporting a configuration problem above.")
    else:
        print("\n  TwiML looks right.")


def twilio_client():
    if not (ACCOUNT_SID and AUTH_TOKEN):
        print("  skipped: Twilio credentials are not set locally")
        return None
    from twilio.rest import Client

    return Client(ACCOUNT_SID, AUTH_TOKEN)


def check_calls(client):
    section("3. recent calls")
    if not client:
        return
    calls = client.calls.list(limit=5)
    if not calls:
        print("  no calls on this account yet")
    for call in calls:
        print(
            f"  {call.start_time}  {call.sid}  {call.direction:>12}  "
            f"status={call.status}  duration={call.duration}s"
        )


def check_alerts(client):
    section("4. Twilio debugger alerts (Twilio's own view of the failure)")
    if not client:
        return
    alerts = client.monitor.v1.alerts.list(limit=10)
    if not alerts:
        print("  no alerts -- Twilio did not record any errors.")
        print("  If the call still failed, the problem is inside the call")
        print("  itself (the media stream), so read the server logs.")
    for alert in alerts:
        print(f"\n  {alert.date_created}  error {alert.error_code}  [{alert.log_level}]")
        # alert_text is a urlencoded query string, not prose.
        fields = urllib.parse.parse_qs(alert.alert_text or "")
        for key, values in fields.items():
            if key not in ("ErrorCode", "LogLevel"):
                print(f"    {key}: {values[0]}")
        if alert.request_url:
            print(f"    on {alert.request_method} {alert.request_url}")
        if alert.response_body:
            print(f"    response body: {alert.response_body[:300]}")
        if alert.more_info:
            print(f"    more: {alert.more_info}")
        explain(alert, fields)


def explain(alert, fields):
    """Translate the common Twilio error codes into what to actually change."""
    message = fields.get("Msg", [""])[0]
    if str(alert.error_code) == "11200":
        print("    ->  Twilio could not get a usable response from the webhook.")
        if "502" in message or "503" in message or "504" in message:
            print("        A 5xx here means the tunnel was up but nothing usable")
            print("        was behind it -- `python server.py` not running, or")
            print("        ngrok forwarding to a different port than the server.")
        elif "11200" in message or "timeout" in message.lower():
            print("        The server did not answer in time. Look for the")
            print("        matching /voice line in the server terminal.")
        else:
            print("        Check the server terminal for the request at that time.")
    elif str(alert.error_code) == "11205":
        print("    ->  Twilio could not connect to the webhook host at all.")
    elif str(alert.error_code) == "12100":
        print("    ->  The TwiML was malformed. Look at the response body above.")
    elif str(alert.error_code) == "31920":
        print("    ->  The <Stream> websocket handshake failed. Check that the")
        print("        wss:// host in the TwiML is the public one and is up.")


if __name__ == "__main__":
    check_config()
    check_server()
    try:
        client = twilio_client()
        check_calls(client)
        check_alerts(client)
    except Exception as exc:
        print(f"\n  could not query Twilio: {type(exc).__name__}: {exc}")
        sys.exit(1)
    print()
