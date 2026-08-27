# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

An AI agent that carries on a real conversation over the phone: Twilio places the call, OpenAI's
Realtime API supplies the voice on our end, and each call is saved as a stereo WAV plus a transcript.

## Commands

```bash
source venv/bin/activate          # gitignored; Python 3.11
pip install -r requirements.txt

python server.py                  # uvicorn dev server on :5000 (webhook + media-stream socket)
uvicorn server:app --host 0.0.0.0 --port $PORT   # how Railway runs it (see Procfile)

python index.py                   # places a real outbound call — costs money, rings a real phone
```

There are no tests, linters, or build steps in this repo.

## Architecture

Two entry points that talk to each other only through Twilio's cloud:

- `index.py` — the **caller**. Creates an outbound call via the Twilio REST client, handing Twilio a
  webhook `url` that Twilio fetches once the callee picks up.
- `server.py` — the **webhook and the bridge**. FastAPI, deployed on Railway at
  `https://aivoiceassistant-production-6cb1.up.railway.app`.
- `recording.py` — audio/transcript capture, no network or framework code.

The call itself is a bridge between two websockets:

```
caller <--G.711 mu-law--> Twilio <--/media-stream--> server.py <--> OpenAI Realtime
```

`GET/POST /voice` returns TwiML containing `<Connect><Stream url="wss://<host>/media-stream">`, so
Twilio opens a **bidirectional** media stream back to this process. `Bridge` in `server.py` then runs
two pumps concurrently: caller audio → `input_audio_buffer.append`, and OpenAI audio deltas → Twilio
`media` frames. Both legs are 8 kHz mono G.711 mu-law (`audio/pcmu`), so nothing is transcoded —
base64 payloads are relayed verbatim.

Things that are easy to break:

- **The wss host.** `/voice` builds the stream URL from the request's `x-forwarded-host`/host header.
  Twilio's servers dial it, so it must be publicly reachable — `localhost:5000` never works for a real
  call. Locally, tunnel (ngrok) and set `PUBLIC_URL` for `index.py`.
- **Interruption handling.** OpenAI's server VAD emits `input_audio_buffer.speech_started` when the
  caller talks. `Bridge.handle_interruption` then sends Twilio a `clear` (drops buffered playback) and
  sends OpenAI `conversation.item.truncate` with how many ms of the answer were actually *heard* —
  computed from Twilio's inbound `media.timestamp` clock, not from wall time. The `marks` queue is
  what tells us the assistant is still audible: Twilio echoes each `mark` back once the audio before
  it has played out. Drop any of these three pieces and the assistant talks over the caller or
  believes it said things the caller never heard.
- **Realtime event names** differ between the beta and GA APIs (`response.audio.delta` vs
  `response.output_audio.delta`, etc.). The `*_EVENTS` sets at the top of `server.py` accept either.

Recording (`recording.py`): inbound mu-law is the call's clock, since Twilio sends it in real time.
Assistant audio is written into a parallel track at whatever offset the inbound track has reached, and
on a barge-in everything past that offset is dropped — it was never played. On hangup both tracks are
decoded to 16-bit PCM and written as one stereo WAV (caller left, assistant right) into `recordings/`
alongside `.json` and `.txt` transcripts. mu-law decoding is a hand-rolled 256-entry table because
`audioop` was removed in Python 3.13.

WSGI does not do websockets, which is why this is FastAPI/uvicorn rather than Flask/gunicorn.

Phone numbers default to hardcoded values in `index.py`, overridable via `TO_NUMBER`/`FROM_NUMBER`.

## Environment

`.env` (gitignored) supplies `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, and `OPENAI_API_KEY`. Railway
needs the same as service variables. Optional: `OPENAI_REALTIME_MODEL`, `OPENAI_VOICE`,
`ASSISTANT_INSTRUCTIONS`, `ASSISTANT_GREETING`, `RECORDINGS_DIR`, `PUBLIC_URL`.

Note that `index.py` reads credentials with `os.getenv` (silently `None` if missing) while `server.py`
uses `os.environ[...]` for the OpenAI key (fails fast at import).
