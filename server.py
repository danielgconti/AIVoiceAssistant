import os
from flask import Flask
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

account_sid = os.environ["TWILIO_ACCOUNT_SID"]
auth_token = os.environ["TWILIO_AUTH_TOKEN"]

client = Client(account_sid, auth_token)


@app.route("/voice", methods=["POST", "GET"])
def voice():
    response = VoiceResponse()

    response.say(
        "Hello! This is an automated call from my AI voice assistant. Hello! This is an automated call from my AI voice assistant. Hello! This is an automated call from my AI voice assistant.  Hello! This is an automated call from my AI voice assistant.",
        voice="alice"
    )

    return str(response)

if __name__ == "__main__":
    app.run(port=5000)