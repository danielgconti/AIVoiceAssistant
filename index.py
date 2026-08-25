# Download the helper library from https://www.twilio.com/docs/python/install
import os
from twilio.rest import Client
from dotenv import load_dotenv
from twilio.twiml.voice_response import Record, VoiceResponse
import requests

# Load the environment variables
load_dotenv()

# Find your Account SID and Auth Token at twilio.com/console
# and set the environment variables. See http://twil.io/secure
account_sid = os.environ["TWILIO_ACCOUNT_SID"]
auth_token = os.environ["TWILIO_AUTH_TOKEN"]
client = Client(account_sid, auth_token)



def callTwilio():
    call = client.calls.create(
    url="http://demo.twilio.com/docs/voice.xml",
    to="REDACTED_PHONE_NUMBER",
    from_="REDACTED_PHONE_NUMBER",
    )
    print(call.sid)

    response = requests.post("http://localhost:5000/voice")
    print(response.text)


callTwilio()