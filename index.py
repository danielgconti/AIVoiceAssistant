# Download the helper library from https://www.twilio.com/docs/python/install
import os
from twilio.rest import Client
from dotenv import load_dotenv
from twilio.twiml.voice_response import Record, VoiceResponse
import requests

load_dotenv()

account_sid = os.getenv("TWILIO_ACCOUNT_SID")
auth_token = os.getenv("TWILIO_AUTH_TOKEN")

client = Client(account_sid, auth_token)


def callTwilio():
    call = client.calls.create(
    url="https://aivoiceassistant-production-6cb1.up.railway.app/voice",
    to="REDACTED_PHONE_NUMBER",
    from_="REDACTED_PHONE_NUMBER",
    )
    print(call.sid)

    response = requests.post("http://localhost:5000/voice")
    print(response.text)


callTwilio()