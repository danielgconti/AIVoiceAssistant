# Architecture

A harness that phones a real doctor's office, plays a patient with an agenda, and records where the
office's automated assistant breaks. Twilio places the call, OpenAI's Realtime API supplies the
patient's voice, and every call lands on disk as a stereo WAV, a transcript, and a bug report.

This document explains how the pieces fit together and why several of them look odd. `CLAUDE.md` is
the short operational version; `README.md` is the user-facing one.

## Which side is being tested

The single most important orientation fact, and the one that gets misread when editing prompts:
**our side is the tester, not the assistant.** We are the patient. The thing under test is the
automated attendant that answers the office's phone, which is supposed to handle general questions,
prescription refills, and setting / changing / deleting appointments.

Transcript roles come from the Realtime API's point of view, so they are inverted relative to the
experiment: `assistant` is *our tester*, and `caller` is the *office assistant being tested*. Every
downstream consumer — the analysis prompt in `report.py`, the `.txt` and `.json` transcripts —
carries that inversion, and the analysis brief states it explicitly so the model reports bugs on the
right side of the call.

## Two entry points, one meeting place

The system has two programs that never speak to each other directly. `index.py` places an outbound
call through Twilio's REST API and exits. `server.py` waits for Twilio to call *it* back — first
over HTTP for instructions, then over a websocket carrying the live audio. Everything they share
travels through Twilio's cloud and back down the ngrok tunnel.

```
                         your machine                        |        elsewhere
                                                             |
   OpenAI Realtime  <--- append -----  server.py  <-- GET /voice --  Twilio  -- dials -->  the
   (patient's voice) --- deltas ---->  /voice     --- TwiML ------>  (REST +              office
    server-side VAD                    /call-status                   Media               (under
    transcripts                        /media-stream <== u-law ==>    Streams)             test)
                                       Bridge + CallRecorder             ^
                                             |                           |
                                             | on hangup                 | create call
                                             v                           |
                                       recordings/                    index.py
                                       .wav .txt .json .md    ---------/
```

Audio is 8 kHz mono G.711 mu-law on both legs (`audio/pcmu`), so `server.py` relays base64 payloads
verbatim — nothing is transcoded.

## The modules

| File | Responsibility |
|---|---|
| `index.py` | The caller. Resolves a scenario from a menu, a slug, or a number; finds the tunnel; pre-flights the server through it; creates the outbound call with a `status_callback` for progress events. |
| `server.py` | The webhook and the bridge. FastAPI on uvicorn: `/` health, `/voice` TwiML, `/call-status` callbacks, and the `/media-stream` websocket where the `Bridge` class runs the call. |
| `scenarios.py` | Ten `Scenario` dataclasses. Each carries the prompt body, a length cap, a probe description, and a `checks` list. No I/O. |
| `recording.py` | Audio and transcript capture. Accumulates both mu-law legs, decodes to PCM, writes the stereo WAV and both transcripts. No network, no framework. |
| `report.py` | The post-call bug report. Sends the transcript and the scenario's checks to a text model, writes `<basename>.md`, appends a line to `recordings/BUGS.md`. |
| `tunnel.py` | Asks the local ngrok agent on `127.0.0.1:4040` for its current public URL, so a hostname that regenerates on every restart never has to be copied by hand. `PUBLIC_URL` overrides it. |
| `debug_twilio.py` | Read-only post-mortem: credentials, a live fetch of `/` and `/voice`, recent calls, and Twilio's own debugger alerts. |

## Life of a call

Each step hands the next one something it cannot get any other way, which is why the ordering
constraints below matter.

1. `index.py` resolves the scenario, discovers the ngrok URL, and fetches `/` through the tunnel. A
   misrouted tunnel or a missing key is caught here, before any money is spent.
2. It calls `client.calls.create()` with `url=<tunnel>/voice?scenario=<slug>` and a status callback,
   prints the call SID, and exits.
3. Twilio dials the office. When someone — or something — picks up, Twilio fetches the webhook,
   query string intact.
4. `/voice` builds the stream URL from the request's `x-forwarded-host` header, which through ngrok
   is the tunnel's public hostname, and returns `<Connect><Stream url="wss://.../media-stream">`
   with the scenario slug attached as a `<Parameter>`.
5. Twilio opens the media-stream websocket. `Bridge.await_start` consumes messages until the `start`
   event arrives, which is where the scenario, the stream SID, and the call SID come from.
6. `configure_session` sends `session.update` with the scenario's instructions and the audio format,
   then seeds a greeting instruction and a `response.create` — we placed the call, so we speak first.
7. Two pumps run concurrently under `asyncio.gather` until either side closes, with a third task
   watching the clock. Audio, transcripts, and interruptions all flow through here.
8. On hangup the recording is saved in a `finally` block, the summary is logged, and the report is
   written in a worker thread.

### How a scenario reaches the call

A scenario is chosen in one process and needed in another, and the only channel between them is
Twilio. It survives four hops:

```
index.py                /voice                  TwiML                   start event      Bridge
?scenario=cancel-undo -> query string preserved -> <Parameter name=...> -> customParameters -> instructions
```

Because the scenario only becomes known at the `start` event, `await_start` must run *before*
`configure_session`. Reverse them and the tester opens the call as the wrong patient. For the same
reason `CallRecorder.scenario_label` is assigned in `handle_start` rather than in the constructor —
the filename cannot be known any earlier. An unrecognised slug logs a warning and falls back to
`scenarios.DEFAULT` rather than failing the call.

## Inside the bridge

`Bridge` holds one call. Its session config asks for `audio/pcmu` in both directions, server-side VAD
(threshold 0.5, 300 ms prefix padding, 500 ms of silence to end a turn, `interrupt_response` on), and
input transcription with `gpt-4o-mini-transcribe`. Two coroutines then run against each other:

- **Caller → OpenAI.** Every `media` frame updates `latest_media_ts`, is handed to the recorder, and
  is forwarded as `input_audio_buffer.append`. `mark` events pop the marks queue; `stop` ends the
  loop.
- **OpenAI → caller.** Audio deltas are relayed to Twilio as `media` frames, each followed by a
  `mark`. Transcript events are recorded, `response.created`/`response.done` track whether a response
  is in flight, and `error` events are collected for the end-of-call summary.

The Realtime API renamed events between beta and GA (`response.audio.delta` →
`response.output_audio.delta`, and the same for transcript events). The `*_EVENTS` sets at the top of
`server.py` accept either spelling. Add new spellings there rather than in the dispatch chain.

## Barge-in: three things must happen at once

When the office assistant starts talking, OpenAI's VAD emits `input_audio_buffer.speech_started`. At
that instant our tester has usually *generated* far more audio than the far end has actually *heard*
— Twilio is still playing out a buffer. Getting the response right means knowing where the playback
head is, and the only honest clock for that is Twilio's own `media.timestamp` on inbound frames, not
wall time.

```
              response_start_ts                 speech_started fires here
                      |                                    |
  our tester's audio  |======== heard by the office =======|///// buffered, never played /////|
                      |                                    |
                      |<---------- heard_ms -------------->|
                      |   latest_media_ts - response_start_ts
                                                           |
  the office speaking                                      |============ talks over us ========|

  inbound media.timestamp ------------------------------------------------------------------->
  (20 ms per frame, the only real clock)

  on interrupt:  1. Twilio: clear (drop the buffer)
                 2. OpenAI: conversation.item.truncate (audio_end_ms = heard_ms)
                 3. recorder: truncate_agent_to_now() (drop the same tail)
```

The `marks` queue is what proves audio is still audible: Twilio echoes each mark back once the audio
before it has played out, so a non-empty queue means the tester is still being heard. All three
consequences use the same `heard_ms` boundary — Twilio drops what it has buffered, OpenAI trims its
own turn to what was actually played so its next reply follows from reality, and the recorder deletes
the same tail so the WAV matches the call. Drop any one of the three and the tester either talks over
the far end or believes it said things nobody heard.

## The clock that ends the call

Each scenario carries a `max_seconds`. `Bridge.enforce_time_limit` sleeps until 25 seconds before it,
waits out any in-flight response (a second `response.create` while one is running is an error, hence
the `response_active` flag), and sends a wrap-up instruction. Crucially the nudge rides on
`response.create`'s *per-response* `instructions` rather than a new conversation item — a
conversation item would be read aloud as a stage direction. At the cap the websocket is closed, which
ends the call.

## Recording: one track is the clock

Both legs are kept as raw mu-law for the duration of the call — cheap, at 8 KB per second per leg —
and decoded only when the file is written. The inbound track is the clock, because Twilio delivers it
in real time at 20 ms per frame. The tester's audio arrives from OpenAI in bursts that will play out
later, so it is written into a parallel track at whatever offset the inbound track has currently
reached, padding with mu-law silence to get there.

```
                                                        len(_caller), the write head
                                                                     |
  left  (the office, real time) |=============================================================|
                                                                     |
  right (our tester, in bursts) |== burst ==|... silence ...|== burst |- deleted on barge-in -|
                                                                     |
                                -> both padded to equal length, decoded, interleaved: stereo WAV
```

Because the tester's track is always padded up to the caller track's current length before a burst is
appended, the two channels stay in sync without any timestamp bookkeeping, and truncation becomes a
single slice: anything past the write head has not been played, so `truncate_agent_to_now()` just
deletes it. At hangup both tracks are padded to equal length, decoded through a hand-built 256-entry
mu-law table, and interleaved into one stereo WAV — caller left, tester right.

## What a call leaves behind

Four files share one basename, `<when>-<NN-scenario>-<call sid>`, so the directory sorts by time and
still groups by scenario: `20260827-141203-08-cancel-undo-CAxxxx` plus `.wav`, `.txt`, `.json`, and
`.md`. A one-line entry per call is appended to `recordings/BUGS.md`.

The report is written in a worker thread after the recording is safely on disk, and is wrapped so
that a failed analysis can never take the recording down with it. The analysis prompt is given the
scenario body *and* its `checks`, so "wrong" is defined per scenario rather than left to the model's
taste, and it is told to report only what the transcript shows and to list separately whatever the
call never actually reached. `ANALYSIS_MODEL=off` skips the call and leaves the section for you to
fill in by hand; the checklist and notes sections are there either way.

`Scenario.number` is assigned from each scenario's position in the `SCENARIOS` list at import, and
that number goes into every filename. Reordering the list silently changes what past filenames mean.
**Append, don't reorder.**

## Choices that look odd until they don't

- **Local, not hosted.** The recordings are the point. On an ephemeral host they are written to a
  container filesystem that is wiped on redeploy and cannot be downloaded. Running locally puts them
  in `recordings/` where they can be used. The leftover `Procfile` is not part of the flow.
- **ASGI, not WSGI.** WSGI does not do websockets, which is the whole mechanism here. A start command
  like `gunicorn server:app` cannot run this app at all — that mismatch is what produced the 502s
  Twilio logged before the switch.
- **Port 5050, not 5000.** On macOS, port 5000 belongs to AirPlay Receiver, which *answers* requests
  instead of failing loudly. `server.py` probes the port and explains itself before uvicorn starts.
- **A hand-built mu-law table.** `audioop` was removed in Python 3.13, so `recording.py` generates
  its own 256-entry decode table at import. No dependency, no version cliff.
- **The health endpoint always returns 200.** Read `status` and `problems` instead. A healthcheck
  pointed here should not take the process down over the very config problem it exists to report.
- **Missing key → spoken TwiML.** Twilio turns a 500 into "an application error has occurred" with
  nothing in our logs, so a missing `OPENAI_API_KEY` produces a `<Say>` that names the problem out
  loud and an error line in the log.

## Debugging outside-in

| Symptom | Where the evidence is |
|---|---|
| The call fails before our code runs at all | `python debug_twilio.py` — Twilio's debugger alerts are the only record. `11200` with a 5xx means the tunnel was up but the server behind it was not (or ngrok points at the wrong port); `12100` means malformed TwiML; `31920` means the `<Stream>` websocket handshake failed. |
| Something is misconfigured | `GET /` names it in plain English. `index.py` already fetches this through the tunnel before dialling, so a misrouted tunnel costs nothing to find. |
| The stream opened but carried no audio | The end-of-call summary calls out "no frames in" explicitly. Check that Twilio's `start` event reported `audio/x-mulaw`. |
| Our tester never spoke | "No frames out" in the summary, then the OpenAI `error` events or a `response.done` with status `failed` logged just above — a rejected `session.update` is the usual cause. |
| The Realtime handshake failed | `describe_ws_error` walks the exception chain for an HTTP status and maps it: 401 bad key, 403 no Realtime access, 404 bad model name, 429 out of quota. |
| Twilio reached the wrong host | `/voice` logs the wss URL it handed out, and warns when it produces a `wss://127.0.0.1` URL that no real call can use. |
| Nothing appears in the logs at all | stdout is set to line-buffered at import, so piped logs arrive as they happen. If a route raised, the HTTP middleware logged the traceback. |

## Configuration

`.env` is gitignored and read with `os.getenv` everywhere, so a missing value is `None` rather than
an import-time crash — a crashed webhook gives Twilio a 502 and you a generic error with nothing in
the logs.

| Variable | Default | Effect |
|---|---|---|
| `TWILIO_ACCOUNT_SID` | — | Required to place a call. |
| `TWILIO_AUTH_TOKEN` | — | Required to place a call. |
| `OPENAI_API_KEY` | — | Required for both the voice and the analysis. |
| `OPENAI_REALTIME_MODEL` | `gpt-realtime` | The voice model behind our patient. |
| `OPENAI_VOICE` | `marin` | Which voice the patient speaks in. |
| `ANALYSIS_MODEL` | `gpt-4o` | Post-call analysis; `off` skips it. |
| `PUBLIC_URL` | ngrok lookup | Skip tunnel discovery and use this URL. |
| `PORT` | `5050` | Where uvicorn listens. |
| `RECORDINGS_DIR` | `recordings` | Where calls are written. |
| `LOG_LEVEL` | `INFO` | `DEBUG` adds every Realtime event type and periodic frame counts. |
| `TO_NUMBER` / `FROM_NUMBER` | hardcoded in `index.py` | Who gets called, and from where. |

## If you change this, watch these

- **Ordering in `Bridge.run`.** `await_start` before `configure_session`, always — the scenario does
  not exist until the `start` event.
- **The three barge-in messages.** They are a set. Removing the recorder truncation alone
  desynchronises the WAV from what was actually heard.
- **The `SCENARIOS` list order.** It names files. Append.
- **The wrap-up path.** It must stay on per-response `instructions`; a conversation item gets read
  aloud.
- **New Realtime event spellings.** Add them to the `*_EVENTS` sets, not to the `elif` chain.
- **The `finally` block in `media_stream`.** It is the only thing guaranteeing a recording survives a
  failed call. Nothing added there may be allowed to raise past it.
