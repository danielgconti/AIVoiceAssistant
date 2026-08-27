# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

An AI agent that carries on a real conversation over the phone: Twilio places the call, OpenAI's
Realtime API supplies the voice on our end, and each call is saved as a stereo WAV plus a transcript.

## Commands

```bash
source venv/bin/activate          # gitignored; Python 3.11
pip install -r requirements.txt

python server.py                  # uvicorn dev server on :5050 (webhook + media-stream socket)
uvicorn server:app --host 0.0.0.0 --port $PORT   # how Railway runs it (see Procfile)

python index.py                   # places a real outbound call — costs money, rings a real phone
python debug_twilio.py            # why did the last call fail? (read-only)

LOG_LEVEL=DEBUG python server.py  # every Realtime event type + periodic frame counts
```

There are no tests, linters, or build steps in this repo.

Not port 5000: on macOS that belongs to AirPlay Receiver (`ControlCenter`), which *answers*
requests rather than failing loudly. `server.py` probes the port and says so before uvicorn starts.

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

## Debugging a failed call

Work outside-in; each layer has its own evidence.

1. `python debug_twilio.py` — checks local credentials, fetches `/` and `/voice` on the deployed
   server, then prints recent calls and **Twilio's debugger alerts**. Those alerts are the only
   record of failures that happen before our code runs: error `11200` with a 5xx means the app is
   not running (a `gunicorn server:app` start command cannot run this ASGI app), `12100` means the
   TwiML was malformed, `31920` means the `<Stream>` websocket handshake failed.
2. `GET /` on the Railway URL — reports which configuration is missing. Always 200 (a Railway
   healthcheck may point at it); read the `status` and `problems` fields.
3. The Railway deploy log — `/voice` logs the CallSid and the wss URL it handed out; `/media-stream` logs
   the OpenAI handshake, the first frame in each direction, every interruption, and an end-of-call
   summary. `log_summary` calls out the two silent failures explicitly: no frames in (the stream
   opened but carried no audio) and no frames out (the assistant never spoke — look for the OpenAI
   `error` events or a `response.done` with status `failed` logged just above).

The logs live in Railway's deploy log, not on your machine. `server.py` sets stdout to
line-buffered at import so they appear as they happen rather than in delayed blocks. Set
`LOG_LEVEL=DEBUG` as a Railway service variable to raise the detail without redeploying.

Design choices that exist for debuggability: a missing `OPENAI_API_KEY` returns spoken TwiML
explaining itself rather than a 500, because Twilio turns a 500 into "an application error has
occurred" with nothing in our logs; `describe_ws_error` maps the Realtime handshake's HTTP status
to a cause (401 = bad key, 404 = bad model name); and an HTTP middleware logs a traceback for any
unhandled route exception.

## Environment

`.env` (gitignored) supplies `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, and `OPENAI_API_KEY`. Railway
needs the same as service variables. Optional: `OPENAI_REALTIME_MODEL`, `OPENAI_VOICE`,
`ASSISTANT_INSTRUCTIONS`, `ASSISTANT_GREETING`, `RECORDINGS_DIR`, `PUBLIC_URL`, `PORT`, `LOG_LEVEL`.

Note that `index.py` reads credentials with `os.getenv` (silently `None` if missing) while `server.py`
uses `os.environ[...]` for the OpenAI key (fails fast at import).
