An AI agent made to carry on a conversation over the phone, that can transcribe and save the audio from the conversation for later analysis.

The assistant's voice comes from OpenAI's Realtime API, streamed straight into the live Twilio call.
It listens while the caller speaks and stops talking as soon as they interrupt. Every call is written
to `recordings/` as a stereo WAV (caller left, assistant right) with `.json` and `.txt` transcripts.

## Setup

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

`.env` needs:

```
export TWILIO_ACCOUNT_SID=...
export TWILIO_AUTH_TOKEN=...
export OPENAI_API_KEY=...
```

## Running

The server must be reachable from the public internet — Twilio dials it. Either deploy it, or tunnel:

```bash
python server.py                      # terminal 1 (:5050)
ngrok http 5050                       # terminal 2
PUBLIC_URL=https://<id>.ngrok.app python index.py   # places the call
```

## When a call fails

```bash
python debug_twilio.py     # checks config, the deployed server, and Twilio's own error log
LOG_LEVEL=DEBUG python server.py
```
