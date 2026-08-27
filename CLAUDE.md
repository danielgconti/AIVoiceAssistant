# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

A test harness that phones a doctor's office and plays a patient, to find where the office's
automated phone assistant breaks. Twilio places the call, OpenAI's Realtime API supplies our
caller's voice, and each call is saved as a stereo WAV, a transcript, and a bug report.

The assistant under test handles general questions, prescription refills, and setting / changing /
deleting appointments. **Our side is the tester, not the assistant** — a common misreading when
editing prompts. In the transcripts, `assistant` is our tester and `caller` is the office
assistant being tested, because the labels come from the Realtime API's point of view.

**The server runs locally, not on a host.** Twilio reaches it through an ngrok tunnel. That is a
deliberate choice, not a stopgap: the recordings are the point of the project, and on an ephemeral
host they are written to a container filesystem that is wiped on every redeploy and cannot be
downloaded. Running locally puts them in `recordings/` where they can actually be used.

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
- `server.py` — the **webhook and the bridge**. FastAPI on uvicorn, run locally.
- `recording.py` — audio/transcript capture, no network or framework code.
- `scenarios.py` — the ten test scenarios. Each carries the prompt body, a length cap, and a
  `checks` list that doubles as the report checklist and the analysis brief.
- `report.py` — the post-call bug report, written next to the recording.
- `tunnel.py` — asks the local ngrok agent (`127.0.0.1:4040`) for its public URL, so the
  regenerated-on-every-restart hostname never has to be copied by hand. `PUBLIC_URL` overrides it.

The call itself is a bridge between two websockets:

```
caller <--G.711 mu-law--> Twilio <--/media-stream--> server.py <--> OpenAI Realtime
```

**How a scenario reaches the call.** `index.py` puts it in the webhook query string
(`/voice?scenario=cancel-undo`); Twilio preserves that when it fetches the webhook; `/voice` turns it
into a `<Parameter>` inside `<Stream>`; Twilio hands it back in the websocket's `start` event as
`start.customParameters`. `Bridge.await_start` therefore consumes messages up to `start` *before*
briefing the model — configure the session first and the caller opens the call as the wrong patient.
An unknown slug falls back to `scenarios.DEFAULT` with a warning rather than failing the call.

`GET/POST /voice` returns TwiML containing `<Connect><Stream url="wss://<host>/media-stream">`, so
Twilio opens a **bidirectional** media stream back to this process. `Bridge` in `server.py` then runs
two pumps concurrently: caller audio → `input_audio_buffer.append`, and OpenAI audio deltas → Twilio
`media` frames. Both legs are 8 kHz mono G.711 mu-law (`audio/pcmu`), so nothing is transcoded —
base64 payloads are relayed verbatim.

Things that are easy to break:

- **The wss host.** `/voice` builds the stream URL from the request's `x-forwarded-host`/host header,
  which through ngrok is the tunnel's hostname — exactly what Twilio needs to dial back into. Hit
  `/voice` directly on localhost and you get a `wss://127.0.0.1` URL that no real call can use; the
  route logs a warning when it produces one.
- **Interruption handling.** OpenAI's server VAD emits `input_audio_buffer.speech_started` when the
  caller talks. `Bridge.handle_interruption` then sends Twilio a `clear` (drops buffered playback) and
  sends OpenAI `conversation.item.truncate` with how many ms of the answer were actually *heard* —
  computed from Twilio's inbound `media.timestamp` clock, not from wall time. The `marks` queue is
  what tells us the assistant is still audible: Twilio echoes each `mark` back once the audio before
  it has played out. Drop any of these three pieces and the assistant talks over the caller or
  believes it said things the caller never heard.
- **The call-length cap.** `Bridge.enforce_time_limit` sends a wrap-up instruction 25 s before the
  scenario's `max_seconds` and closes the websocket at the cap, which ends the call. The nudge rides
  on `response.create`'s per-response `instructions` rather than a conversation item, so the model is
  steered without a stage direction being read aloud; it waits out any in-flight response first
  (`response_active`), because a second `response.create` while one is running is an error.
- **Realtime event names** differ between the beta and GA APIs (`response.audio.delta` vs
  `response.output_audio.delta`, etc.). The `*_EVENTS` sets at the top of `server.py` accept either.

Reports (`report.py`): written after the recording, in a thread, and never allowed to take the
recording down with it. The analysis prompt gets the scenario body and its `checks` so "wrong" is
defined per scenario, and is told to report only what the transcript shows and to list what the call
never reached. `ANALYSIS_MODEL=off` skips it and leaves the section blank.

Recording (`recording.py`): inbound mu-law is the call's clock, since Twilio sends it in real time.
Assistant audio is written into a parallel track at whatever offset the inbound track has reached, and
on a barge-in everything past that offset is dropped — it was never played. On hangup both tracks are
decoded to 16-bit PCM and written as one stereo WAV (caller left, assistant right) into `recordings/`
alongside `.json` and `.txt` transcripts. mu-law decoding is a hand-rolled 256-entry table because
`audioop` was removed in Python 3.13.

Files are named `<when>-<NN-scenario>-<call sid>` (`20260827-141203-08-cancel-undo-CAxxxx.wav`). The
number is the scenario's menu position, assigned from its index in `SCENARIOS` — so reordering that
list renumbers past filenames' meaning. Append rather than reorder. `CallRecorder.scenario_label` is
set in `handle_start`, not in the constructor, because the scenario is not known until Twilio sends
the `start` event.

WSGI does not do websockets, which is why this is FastAPI/uvicorn rather than Flask/gunicorn.

Phone numbers default to hardcoded values in `index.py`, overridable via `TO_NUMBER`/`FROM_NUMBER`.

## Environment

`.env` (gitignored) supplies `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, and `OPENAI_API_KEY`. Railway
needs the same as service variables. Optional: `OPENAI_REALTIME_MODEL`, `OPENAI_VOICE`,
`ASSISTANT_INSTRUCTIONS`, `ASSISTANT_GREETING`, `RECORDINGS_DIR`, `PUBLIC_URL`.

There is no `ASSISTANT_INSTRUCTIONS` any more — instructions come from the chosen scenario.

Both files read credentials with `os.getenv`, so a missing one is `None` rather than an import-time
crash — `server.py` reports it through `missing_config()` instead, because a crashed webhook gives
Twilio a 502 and you a generic "application error" with nothing in the logs.
