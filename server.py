"""Twilio voice webhook + a bridge between the live call and OpenAI's Realtime API.

Twilio fetches /voice when the callee picks up. The TwiML we return opens a
bidirectional media stream to /media-stream, and from there this process sits
in the middle of two websockets:

    caller  <--G.711 mu-law-->  Twilio  <--/media-stream-->  us  <-->  OpenAI

Both legs speak 8 kHz mu-law, so audio is relayed as-is with no transcoding.
OpenAI's server-side VAD decides when the caller has started and stopped
talking; when it reports speech while the assistant is mid-sentence we clear
Twilio's playback buffer and tell OpenAI how much of its answer was actually
heard, so the assistant stops talking instead of finishing over the caller.

Every chunk in both directions is also handed to a CallRecorder, which writes
a stereo WAV plus the transcript when the call ends.

Debugging: set LOG_LEVEL=DEBUG to log every Realtime event type and periodic
frame counts. GET / reports which configuration is missing. If a call fails
before this process is even reached, run `python debug_twilio.py` -- Twilio's
own debugger records why it gave up on the webhook.
"""

import asyncio
import base64
import json
import logging
import os
import sys
import time
from contextlib import asynccontextmanager

import websockets
from dotenv import load_dotenv
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response

from twilio.twiml.voice_response import Connect, VoiceResponse

import report
import scenarios
from recording import CallRecorder

load_dotenv()

# A piped stdout is block-buffered: without this the logs below arrive in
# delayed blocks, or not at all if the process crashes first.
try:
    sys.stdout.reconfigure(line_buffering=True)
except (AttributeError, ValueError):  # not a real stream (tests, some hosts)
    pass

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-7s %(name)-9s %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("voice")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
REALTIME_MODEL = os.getenv("OPENAI_REALTIME_MODEL", "gpt-realtime")
VOICE = os.getenv("OPENAI_VOICE", "marin")
RECORDINGS_DIR = os.getenv("RECORDINGS_DIR", "recordings")

# The caller plays a patient testing a doctor's-office phone assistant. Which
# patient depends on the scenario chosen for this call -- see scenarios.py.
GREETING = (
    "The office assistant has just picked up. Open the call in character: a "
    "short, natural greeting and the first thing you want. Do not dump your "
    "whole agenda at once unless your call says to."
)
WRAP_UP = (
    "You are nearly out of time. Bring the call to a natural close now: get "
    "any last confirmation you need, thank them, and say goodbye."
)
# Stop nudging and hang up rather than letting a call run forever.
WRAP_UP_LEAD_SECONDS = 25

# The Realtime API renamed several events between the beta and GA releases;
# accept either spelling so this works against both.
AUDIO_DELTA_EVENTS = {"response.output_audio.delta", "response.audio.delta"}
AGENT_TRANSCRIPT_EVENTS = {
    "response.output_audio_transcript.done",
    "response.audio_transcript.done",
}
CALLER_TRANSCRIPT_EVENTS = {"conversation.item.input_audio_transcription.completed"}


def missing_config():
    """Configuration problems that will break a call, in plain English."""
    problems = []
    if not OPENAI_API_KEY:
        problems.append(
            "OPENAI_API_KEY is not set (add it to .env) -- the assistant "
            "cannot connect to OpenAI."
        )
    elif not OPENAI_API_KEY.startswith("sk-"):
        problems.append(
            "OPENAI_API_KEY does not look like an OpenAI key (expected it to "
            "start with 'sk-')."
        )
    return problems


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("=" * 68)
    log.info("starting AI voice assistant")
    log.info("  realtime model : %s", REALTIME_MODEL)
    log.info("  voice          : %s", VOICE)
    log.info("  recordings dir : %s", os.path.abspath(RECORDINGS_DIR))
    log.info("  openai key     : %s", "set" if OPENAI_API_KEY else "MISSING")
    log.info("  log level      : %s", logging.getLevelName(log.getEffectiveLevel()))
    log.info("  analysis model : %s", report.ANALYSIS_MODEL or "(off)")
    log.info("  scenarios      : %d loaded", len(scenarios.SCENARIOS))
    for problem in missing_config():
        log.error("CONFIG PROBLEM: %s", problem)
    log.info("=" * 68)
    yield
    log.info("shutting down")


app = FastAPI(lifespan=lifespan)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Log every HTTP hit, and any unhandled error, with a traceback.

    Without this an exception in /voice is just a 500 to Twilio, which the
    caller hears as "an application error has occurred" with nothing in our
    logs explaining why.
    """
    started = time.monotonic()
    try:
        response = await call_next(request)
    except Exception:
        log.exception("UNHANDLED ERROR in %s %s", request.method, request.url.path)
        raise
    log.info(
        "%s %s -> %s (%.0f ms)",
        request.method,
        request.url.path,
        response.status_code,
        (time.monotonic() - started) * 1000,
    )
    return response


@app.get("/")
def health():
    """Config diagnostics. Hit this first when a call misbehaves.

    Always 200, even when misconfigured -- a healthcheck pointed here should
    not take the process down over a config problem it is meant to report.
    Read the "status" and "problems" fields. index.py calls this through the
    tunnel before dialling.
    """
    problems = missing_config()
    return JSONResponse(
        {
            "status": "error" if problems else "ok",
            "problems": problems,
            "model": REALTIME_MODEL,
            "voice": VOICE,
            "openai_key_present": bool(OPENAI_API_KEY),
            "twilio_credentials_present": bool(
                os.getenv("TWILIO_ACCOUNT_SID") and os.getenv("TWILIO_AUTH_TOKEN")
            ),
            "recordings_dir": os.path.abspath(RECORDINGS_DIR),
            "scenarios": [s.slug for s in scenarios.SCENARIOS],
        }
    )


async def read_form(request: Request) -> dict:
    """Twilio posts urlencoded params; never let parsing them fail a webhook."""
    if request.method != "POST":
        return dict(request.query_params)
    try:
        return dict(await request.form())
    except Exception:
        log.exception("could not parse the request body from Twilio")
        return {}


@app.api_route("/voice", methods=["GET", "POST"])
async def voice(request: Request):
    """TwiML handed to Twilio when the call connects: open a media stream."""
    form = await read_form(request)
    log.info(
        "/voice hit: CallSid=%s From=%s To=%s CallStatus=%s",
        form.get("CallSid"),
        form.get("From"),
        form.get("To"),
        form.get("CallStatus"),
    )
    log.debug("/voice params: %s", json.dumps(form, default=str))
    log.debug("/voice headers: %s", dict(request.headers))

    response = VoiceResponse()

    problems = missing_config()
    if problems:
        # Say the problem out loud rather than 500ing, so the failure is
        # audible and the reason is in the log instead of Twilio's generic
        # "an application error has occurred".
        for problem in problems:
            log.error("refusing to bridge call: %s", problem)
        response.say(
            "The assistant is not configured correctly on the server. "
            "Check the application logs.",
            voice="alice",
        )
        response.hangup()
        return Response(content=str(response), media_type="text/xml")

    host = request.headers.get("x-forwarded-host") or request.url.netloc
    stream_url = f"wss://{host}/media-stream"
    log.info("returning TwiML pointing Twilio at %s", stream_url)
    if "localhost" in host or "127.0.0.1" in host:
        log.warning(
            "stream host is %s -- Twilio's servers cannot reach that. Use a "
            "tunnel (ngrok) or the deployed URL.",
            host,
        )

    # Twilio preserves our query string, and <Parameter> is how a chosen
    # scenario reaches the websocket -- it comes back in the "start" event.
    requested = request.query_params.get("scenario")
    scenario = scenarios.resolve(requested) or scenarios.DEFAULT
    if requested and not scenarios.resolve(requested):
        log.warning("unknown scenario %r; falling back to %s", requested, scenario.slug)
    log.info("scenario for this call: %s (%s)", scenario.slug, scenario.title)

    connect = Connect()
    stream = connect.stream(url=stream_url)
    stream.parameter(name="scenario", value=scenario.slug)
    response.append(connect)
    twiml = str(response)
    log.debug("TwiML: %s", twiml)
    return Response(content=twiml, media_type="text/xml")


@app.api_route("/call-status", methods=["GET", "POST"])
async def call_status(request: Request):
    """Twilio status callbacks -- says how the call itself ended."""
    form = await read_form(request)
    log.info(
        "call status: sid=%s status=%s duration=%ss error=%s",
        form.get("CallSid"),
        form.get("CallStatus"),
        form.get("CallDuration"),
        form.get("ErrorCode"),
    )
    if form.get("ErrorCode"):
        log.error(
            "Twilio reported error %s on call %s: %s",
            form.get("ErrorCode"),
            form.get("CallSid"),
            form.get("ErrorMessage"),
        )
    log.debug("call status params: %s", json.dumps(form, default=str))
    return Response(status_code=204)


class Bridge:
    """One phone call: relays audio both ways and tracks playback position."""

    def __init__(self, twilio_ws: WebSocket, openai_ws):
        self.twilio_ws = twilio_ws
        self.openai_ws = openai_ws
        self.scenario = scenarios.DEFAULT
        self.recorder = CallRecorder(out_dir=RECORDINGS_DIR)
        self.stream_sid = None
        # Twilio stamps every inbound frame with milliseconds since the stream
        # began. That is the only real clock we have for "how much of the
        # assistant's answer has the caller actually heard".
        self.latest_media_ts = 0
        self.response_start_ts = None
        self.last_agent_item = None
        self.marks = []
        # Counters, all of them purely for the end-of-call summary.
        self.started = time.monotonic()
        self.frames_in = 0
        self.frames_out = 0
        self.interruptions = 0
        self.openai_errors = []
        self.event_types = {}
        self.response_active = False

    async def run(self):
        # The scenario arrives in Twilio's "start" event, which is the first
        # thing on the wire -- wait for it before briefing the model, or the
        # caller opens the call as the wrong patient.
        await self.await_start()
        await self.configure_session()
        clock = asyncio.create_task(self.enforce_time_limit())
        # return_exceptions so one side blowing up still lets the other side
        # unwind and the recording get saved -- and so we log the traceback.
        try:
            results = await asyncio.gather(
                self.pump_twilio_to_openai(),
                self.pump_openai_to_twilio(),
                return_exceptions=True,
            )
        finally:
            clock.cancel()
        for name, result in zip(("twilio->openai", "openai->twilio"), results):
            if isinstance(result, Exception):
                log.error("%s pump failed", name, exc_info=result)

    async def await_start(self, timeout=10):
        """Consume Twilio's opening events up to and including "start"."""
        while True:
            try:
                message = await asyncio.wait_for(
                    self.twilio_ws.receive_text(), timeout=timeout
                )
            except asyncio.TimeoutError:
                log.error("Twilio never sent a 'start' event; using %s", self.scenario.slug)
                return
            data = json.loads(message)
            if data.get("event") == "start":
                self.handle_start(data["start"])
                return
            log.debug("pre-start Twilio event: %s", data.get("event"))

    def handle_start(self, start: dict):
        self.stream_sid = start["streamSid"]
        self.recorder.stream_sid = start["streamSid"]
        self.recorder.call_sid = start.get("callSid")
        chosen = (start.get("customParameters") or {}).get("scenario")
        self.scenario = scenarios.get(chosen) or scenarios.DEFAULT
        if chosen and not scenarios.get(chosen):
            log.warning("unknown scenario %r from Twilio; using %s", chosen, self.scenario.slug)
        log.info(
            "stream started: streamSid=%s callSid=%s format=%s",
            self.stream_sid,
            start.get("callSid"),
            start.get("mediaFormat"),
        )
        log.info(
            "running scenario %s -- %s (cap %ds)",
            self.scenario.slug,
            self.scenario.title,
            self.scenario.max_seconds,
        )
        if start.get("mediaFormat", {}).get("encoding") not in (None, "audio/x-mulaw"):
            log.warning(
                "unexpected Twilio media format %s -- audio will sound like static",
                start.get("mediaFormat"),
            )

    async def enforce_time_limit(self):
        """Nudge the caller to wrap up, then hang up if it does not."""
        cap = self.scenario.max_seconds
        await asyncio.sleep(max(1, cap - WRAP_UP_LEAD_SECONDS))
        log.info("%ds left; asking the caller to wrap up", WRAP_UP_LEAD_SECONDS)
        # Overriding instructions on one response steers the next thing said
        # without putting a stage direction into the conversation as speech.
        for _ in range(20):  # wait out an in-flight reply so this is not cut off
            if not self.response_active:
                break
            await asyncio.sleep(0.5)
        await self.send_openai(
            {"type": "response.create", "response": {"instructions": WRAP_UP}}
        )
        await asyncio.sleep(WRAP_UP_LEAD_SECONDS)
        log.warning("call hit its %ds cap; hanging up", cap)
        await self.twilio_ws.close(code=1000)

    async def send_openai(self, event: dict):
        log.debug("-> openai: %s", event.get("type"))
        await self.openai_ws.send(json.dumps(event))

    async def configure_session(self):
        session = {
            "type": "realtime",
            "instructions": self.scenario.instructions,
            "output_modalities": ["audio"],
            "audio": {
                "input": {
                    "format": {"type": "audio/pcmu"},
                    # Server-side VAD: OpenAI detects when the caller starts
                    # and stops speaking, and cancels its own in-flight
                    # response when they start.
                    "turn_detection": {
                        "type": "server_vad",
                        "threshold": 0.5,
                        "prefix_padding_ms": 300,
                        "silence_duration_ms": 500,
                        "interrupt_response": True,
                    },
                    "transcription": {"model": "gpt-4o-mini-transcribe"},
                },
                "output": {"format": {"type": "audio/pcmu"}, "voice": VOICE},
            },
        }
        log.debug("session config: %s", json.dumps(session))
        await self.send_openai({"type": "session.update", "session": session})
        # We placed the call, so we speak first.
        await self.send_openai(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": GREETING}],
                },
            }
        )
        await self.send_openai({"type": "response.create"})
        log.info("session configured; opening line requested")

    # ---- caller -> OpenAI ----------------------------------------------

    async def pump_twilio_to_openai(self):
        try:
            async for message in self.twilio_ws.iter_text():
                data = json.loads(message)
                event = data.get("event")

                if event == "media":
                    self.latest_media_ts = int(data["media"]["timestamp"])
                    payload = data["media"]["payload"]
                    self.recorder.add_caller_audio(base64.b64decode(payload))
                    await self.send_openai(
                        {"type": "input_audio_buffer.append", "audio": payload}
                    )
                    self.frames_in += 1
                    if self.frames_in == 1:
                        log.info("first inbound audio frame from Twilio")
                    elif self.frames_in % 250 == 0:  # every ~5 s of audio
                        log.debug(
                            "audio: %d frames in / %d out, stream clock %.1fs",
                            self.frames_in,
                            self.frames_out,
                            self.latest_media_ts / 1000,
                        )
                elif event == "start":  # already consumed by await_start()
                    self.handle_start(data["start"])
                elif event == "mark":
                    if self.marks:
                        self.marks.pop(0)
                elif event == "stop":
                    log.info("Twilio sent 'stop' (call ended)")
                    break
                elif event == "connected":
                    log.info("Twilio websocket connected (protocol %s)", data.get("protocol"))
                else:
                    log.debug("unhandled Twilio event %r: %s", event, message[:200])
        except WebSocketDisconnect as exc:
            log.info("Twilio websocket disconnected (code %s)", exc.code)
        except Exception:
            log.exception("error while relaying caller audio to OpenAI")
            raise
        finally:
            await self.openai_ws.close()

    # ---- OpenAI -> caller ----------------------------------------------

    async def pump_openai_to_twilio(self):
        try:
            async for raw in self.openai_ws:
                event = json.loads(raw)
                kind = event.get("type")
                self.event_types[kind] = self.event_types.get(kind, 0) + 1

                if kind in AUDIO_DELTA_EVENTS and event.get("delta"):
                    await self.play(event)
                elif kind == "input_audio_buffer.speech_started":
                    log.info("caller started speaking")
                    await self.handle_interruption()
                elif kind == "input_audio_buffer.speech_stopped":
                    log.info("caller stopped speaking")
                elif kind in AGENT_TRANSCRIPT_EVENTS:
                    self.record_line("assistant", event.get("transcript"))
                elif kind in CALLER_TRANSCRIPT_EVENTS:
                    self.record_line("caller", event.get("transcript"))
                elif kind == "session.created":
                    log.info("OpenAI session created")
                elif kind == "session.updated":
                    log.info("OpenAI accepted the session config")
                elif kind == "response.created":
                    self.response_active = True
                elif kind == "response.done":
                    self.response_active = False
                    self.log_response_done(event)
                elif kind == "error":
                    detail = event.get("error", event)
                    self.openai_errors.append(detail)
                    log.error("OpenAI error event: %s", json.dumps(detail))
                else:
                    log.debug("<- openai: %s", kind)
        except websockets.exceptions.ConnectionClosed as exc:
            rcvd = getattr(exc, "rcvd", None)
            log.info(
                "OpenAI connection closed (code %s, reason %r)",
                getattr(rcvd, "code", None),
                getattr(rcvd, "reason", None),
            )
        except Exception:
            log.exception("error while relaying OpenAI audio to the caller")
            raise

    def log_response_done(self, event: dict):
        """A 'failed'/'incomplete' response here is the usual silent killer."""
        response = event.get("response", {})
        status = response.get("status")
        if status in (None, "completed"):
            log.debug("response completed")
            return
        details = response.get("status_details") or {}
        log.error(
            "OpenAI response %s: %s",
            status,
            json.dumps(details) if details else "(no details)",
        )

    async def play(self, event: dict):
        """Forward one chunk of assistant audio to the caller."""
        payload = event["delta"]
        self.recorder.add_agent_audio(base64.b64decode(payload))
        await self.twilio_ws.send_json(
            {
                "event": "media",
                "streamSid": self.stream_sid,
                "media": {"payload": payload},
            }
        )
        self.frames_out += 1
        if self.frames_out == 1:
            log.info("first assistant audio chunk sent to Twilio")
        if self.response_start_ts is None:
            self.response_start_ts = self.latest_media_ts
        if event.get("item_id"):
            self.last_agent_item = event["item_id"]
        # Twilio echoes marks back once the audio before them has played, so
        # a non-empty queue means the assistant is still being heard.
        self.marks.append("agent-audio")
        await self.twilio_ws.send_json(
            {
                "event": "mark",
                "streamSid": self.stream_sid,
                "mark": {"name": "agent-audio"},
            }
        )

    async def handle_interruption(self):
        """The caller started talking over the assistant -- cut it off."""
        if not self.marks or self.response_start_ts is None:
            log.debug("no assistant audio in flight; nothing to interrupt")
            return

        heard_ms = max(0, self.latest_media_ts - self.response_start_ts)
        self.interruptions += 1
        log.info(
            "interrupting assistant after %d ms (%d chunks still buffered)",
            heard_ms,
            len(self.marks),
        )
        if self.last_agent_item:
            # Trim the assistant's turn in OpenAI's history to what was
            # actually played, so its next reply follows from what the caller
            # really heard rather than the full unspoken answer.
            await self.send_openai(
                {
                    "type": "conversation.item.truncate",
                    "item_id": self.last_agent_item,
                    "content_index": 0,
                    "audio_end_ms": heard_ms,
                }
            )
        # Drop whatever Twilio still has buffered, and from the recording too.
        await self.twilio_ws.send_json({"event": "clear", "streamSid": self.stream_sid})
        self.recorder.truncate_agent_to_now()

        self.marks.clear()
        self.last_agent_item = None
        self.response_start_ts = None

    def record_line(self, role: str, text):
        if text:
            log.info("%s: %s", role, text.strip())
        else:
            log.debug("empty %s transcript event", role)
        self.recorder.add_transcript(role, text)

    def log_summary(self):
        log.info("-" * 68)
        log.info(
            "call summary: %.1fs wall, %d frames in, %d frames out, "
            "%d interruptions, %d transcript lines",
            time.monotonic() - self.started,
            self.frames_in,
            self.frames_out,
            self.interruptions,
            len(self.recorder.transcript),
        )
        if not self.frames_in:
            log.error(
                "no audio ever arrived from Twilio -- the media stream opened "
                "but the call carried no audio."
            )
        if not self.frames_out:
            log.error(
                "the assistant never produced audio. Check the OpenAI error "
                "lines above; a rejected session.update or a failed response "
                "is the usual cause."
            )
        for error in self.openai_errors:
            log.error("OpenAI error during call: %s", json.dumps(error))
        log.debug("OpenAI event counts: %s", json.dumps(self.event_types))
        log.info("-" * 68)


async def connect_openai():
    """Open the Realtime websocket, explaining any handshake failure."""
    url = f"wss://api.openai.com/v1/realtime?model={REALTIME_MODEL}"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
    log.info("connecting to OpenAI Realtime: %s", url)
    try:
        try:
            ws = await websockets.connect(
                url, additional_headers=headers, max_size=None
            )
        except TypeError:  # websockets < 14 spells it differently
            ws = await websockets.connect(url, extra_headers=headers, max_size=None)
    except Exception as exc:
        log.error("could not connect to OpenAI Realtime: %s", describe_ws_error(exc))
        raise
    log.info("connected to OpenAI Realtime")
    return ws


def describe_ws_error(exc: Exception) -> str:
    """Turn a websockets handshake failure into something actionable."""
    # websockets moved the status code around between versions, and wraps the
    # real cause, so walk the chain rather than trusting one attribute.
    status = None
    current = exc
    while current is not None and status is None:
        status = getattr(getattr(current, "response", None), "status_code", None)
        if status is None:
            status = getattr(current, "status_code", None)
        current = current.__cause__ or current.__context__
    hints = {
        401: "OPENAI_API_KEY is missing, wrong, or revoked",
        403: "this API key is not allowed to use the Realtime API",
        404: f"model {REALTIME_MODEL!r} does not exist (set OPENAI_REALTIME_MODEL)",
        429: "rate limited or out of quota on this OpenAI account",
    }
    if status:
        return f"HTTP {status} -- {hints.get(status, 'unexpected status')}: {exc}"
    cause = exc.__cause__ or exc.__context__
    described = f"{type(exc).__name__}: {exc}"
    if cause:
        described += f" (caused by {type(cause).__name__}: {cause})"
    return described


@app.websocket("/media-stream")
async def media_stream(twilio_ws: WebSocket):
    await twilio_ws.accept()
    log.info("Twilio opened the media stream websocket")

    try:
        openai_ws = await connect_openai()
    except Exception:
        # Nothing to bridge to -- close cleanly so Twilio ends the call
        # instead of the socket dying mid-handshake.
        log.exception("aborting call: no OpenAI connection")
        await twilio_ws.close(code=1011)
        return

    bridge = Bridge(twilio_ws, openai_ws)
    try:
        await bridge.run()
    except Exception:
        log.exception("bridge failed")
    finally:
        await openai_ws.close()
        bridge.log_summary()
        try:
            saved = bridge.recorder.save()
        except Exception:
            log.exception("failed to save the recording")
            saved = None
        if saved:
            log.info(
                "saved %.1fs call: %s (+ .json/.txt transcript)",
                bridge.recorder.duration,
                saved["audio"],
            )
            try:
                # Analysis calls out to a text model, so it must not run on
                # the event loop and must never lose us the recording.
                path = await asyncio.to_thread(
                    report.write,
                    bridge.recorder,
                    bridge.scenario,
                    saved,
                    OPENAI_API_KEY,
                )
                if path:
                    log.info("bug report: %s", path)
            except Exception:
                log.exception("failed to write the bug report")
        else:
            log.warning("no audio captured; nothing saved")


if __name__ == "__main__":
    import socket

    import uvicorn

    # Not 5000: on macOS that port belongs to AirPlay Receiver, which answers
    # requests instead of failing loudly and is baffling to debug.
    port = int(os.getenv("PORT", "5050"))
    with socket.socket() as probe:
        try:
            probe.bind(("0.0.0.0", port))
        except OSError as exc:
            log.error("cannot bind port %d: %s", port, exc)
            log.error(
                "Something else is listening. On macOS, System Settings > "
                "General > AirDrop & Handoff > AirPlay Receiver holds port "
                "5000. Set PORT to another value."
            )
            sys.exit(1)
    uvicorn.run(app, host="0.0.0.0", port=port)
