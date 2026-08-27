"""Find the public URL that Twilio should call back on.

The server runs on your machine, so Twilio needs a tunnel to reach it. ngrok
mints a new hostname every time it restarts (on the free tier), which is
tedious to copy around -- so instead of hardcoding it, ask the local ngrok
agent what its URL is. Setting PUBLIC_URL overrides the lookup.
"""

import json
import os
import urllib.error
import urllib.request

NGROK_API = "http://127.0.0.1:4040/api/tunnels"


def ngrok_tunnel(timeout=2):
    """The local ngrok agent's https tunnel as (public_url, forwarded_addr).

    Returns (None, None) if ngrok is not running -- its agent serves this API
    on port 4040 for as long as it is up.
    """
    try:
        with urllib.request.urlopen(NGROK_API, timeout=timeout) as response:
            tunnels = json.load(response).get("tunnels", [])
    except Exception:
        return None, None

    for tunnel in tunnels:
        if tunnel.get("public_url", "").startswith("https://"):
            return tunnel["public_url"].rstrip("/"), tunnel.get("config", {}).get("addr")
    return None, None


def public_url():
    """PUBLIC_URL if set, else whatever ngrok is currently serving."""
    explicit = os.getenv("PUBLIC_URL")
    if explicit:
        return explicit.rstrip("/")
    url, _ = ngrok_tunnel()
    return url


def local_port():
    return int(os.getenv("PORT", "5050"))


NO_TUNNEL_HELP = """no tunnel found. Twilio has to reach your machine from the
internet, so start the server and a tunnel first:

    python server.py           # terminal 1
    ngrok http {port}            # terminal 2
    python index.py            # terminal 3

The ngrok URL is picked up automatically while ngrok is running. To use some
other tunnel, set PUBLIC_URL to its https address."""


def no_tunnel_help():
    return NO_TUNNEL_HELP.format(port=local_port())
