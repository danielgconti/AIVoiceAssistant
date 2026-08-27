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
"""

import asyncio
import base64
import json
import os

import websockets
from dotenv import load_dotenv
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse, Response
from twilio.twiml.voice_response import Connect, VoiceResponse

import report
import scenarios
from recording import CallRecorder

load_dotenv()

OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
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

app = FastAPI()


@app.get("/", response_class=PlainTextResponse)
def health():
    return "AI voice assistant is running."


@app.api_route("/voice", methods=["GET", "POST"])
async def voice(request: Request):
    """TwiML handed to Twilio when the call connects: open a media stream."""
    host = request.headers.get("x-forwarded-host") or request.url.netloc
    response = VoiceResponse()
    connect = Connect()
    connect.stream(url=f"wss://{host}/media-stream")
    response.append(connect)
    return Response(content=str(response), media_type="text/xml")


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

    async def run(self):
        # The scenario arrives in Twilio's "start" event, which is the first
        # thing on the wire -- wait for it before briefing the model, or the
        # caller opens the call as the wrong patient.
        await self.await_start()
        await self.configure_session()
        await asyncio.gather(
            self.pump_twilio_to_openai(),
            self.pump_openai_to_twilio(),
        )

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
        self.recorder.scenario_label = self.scenario.label
        log.info(
            "stream started: streamSid=%s callSid=%s format=%s",
            self.stream_sid,
            start.get("callSid"),
            start.get("mediaFormat"),
        )
        log.info(
            "running scenario %s -- %s (cap %ds)",
            self.scenario.label,
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
        await self.openai_ws.send(json.dumps(event))

    async def configure_session(self):
        await self.send_openai(
            {
                "type": "session.update",
                "session": {
                    "type": "realtime",
                    "instructions": INSTRUCTIONS,
                    "output_modalities": ["audio"],
                    "audio": {
                        "input": {
                            "format": {"type": "audio/pcmu"},
                            # Server-side VAD: OpenAI detects when the caller
                            # starts and stops speaking, and cancels its own
                            # in-flight response when they start.
                            "turn_detection": {
                                "type": "server_vad",
                                "threshold": 0.5,
                                "prefix_padding_ms": 300,
                                "silence_duration_ms": 500,
                                "interrupt_response": True,
                            },
                            "transcription": {"model": "gpt-4o-mini-transcribe"},
                        },
                        "output": {
                            "format": {"type": "audio/pcmu"},
                            "voice": VOICE,
                        },
                    },
                },
            }
        )
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
                elif event == "start":
                    start = data["start"]
                    self.stream_sid = start["streamSid"]
                    self.recorder.stream_sid = start["streamSid"]
                    self.recorder.call_sid = start.get("callSid")
                    print(f"stream started: {self.stream_sid}")
                elif event == "mark":
                    if self.marks:
                        self.marks.pop(0)
                elif event == "stop":
                    print("stream stopped by Twilio")
                    break
        except WebSocketDisconnect:
            print("Twilio hung up")
        finally:
            await self.openai_ws.close()

    # ---- OpenAI -> caller ----------------------------------------------

    async def pump_openai_to_twilio(self):
        try:
            async for raw in self.openai_ws:
                event = json.loads(raw)
                kind = event.get("type")

                if kind in AUDIO_DELTA_EVENTS and event.get("delta"):
                    await self.play(event)
                elif kind == "input_audio_buffer.speech_started":
                    await self.handle_interruption()
                elif kind in AGENT_TRANSCRIPT_EVENTS:
                    self.record_line("assistant", event.get("transcript"))
                elif kind in CALLER_TRANSCRIPT_EVENTS:
                    self.record_line("caller", event.get("transcript"))
                elif kind == "error":
                    print(f"OpenAI error: {json.dumps(event.get('error', event))}")
        except websockets.exceptions.ConnectionClosed:
            print("OpenAI connection closed")

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
            return

        heard_ms = max(0, self.latest_media_ts - self.response_start_ts)
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
            print(f"{role}: {text.strip()}")
        self.recorder.add_transcript(role, text)


async def connect_openai():
    url = f"wss://api.openai.com/v1/realtime?model={REALTIME_MODEL}"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
    try:
        return await websockets.connect(url, additional_headers=headers, max_size=None)
    except TypeError:  # websockets < 14 spells it differently
        return await websockets.connect(url, extra_headers=headers, max_size=None)


@app.websocket("/media-stream")
async def media_stream(twilio_ws: WebSocket):
    await twilio_ws.accept()
    openai_ws = await connect_openai()
    bridge = Bridge(twilio_ws, openai_ws)
    try:
        await bridge.run()
    finally:
        await openai_ws.close()
        saved = bridge.recorder.save()
        if saved:
            print(
                f"saved {bridge.recorder.duration:.1f}s call: "
                f"{saved['audio']} (+ .json/.txt transcript)"
            )
        else:
            print("no audio captured; nothing saved")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
