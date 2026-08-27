"""Capture the audio and the transcript of a live Twilio call.

Twilio media streams carry 8 kHz mono G.711 mu-law, base64-encoded, in both
directions. We keep both directions as raw mu-law while the call is running
(cheap: 8 KB per second per leg) and only decode to 16-bit PCM when the call
ends and the WAV file is written.

The caller's inbound stream is the call's clock -- Twilio sends it in real
time, 20 ms at a time. The assistant's audio arrives from OpenAI in bursts
that play out later, so it is written into a parallel track at whatever offset
the caller's track has reached. That keeps the two channels roughly in sync
and makes barge-in truncation easy: whatever assistant audio sits past the
caller's clock has not been played yet, so it can simply be dropped.
"""

import json
import struct
import wave
from datetime import datetime, timezone
from pathlib import Path

SAMPLE_RATE = 8000
SILENCE = 0xFF  # mu-law encoding of zero


def _build_ulaw_table():
    """G.711 mu-law -> signed 16-bit PCM, as 256 little-endian 2-byte values."""
    table = []
    for byte in range(256):
        u = ~byte & 0xFF
        magnitude = ((u & 0x0F) << 3) + 0x84
        magnitude <<= (u & 0x70) >> 4
        sample = (0x84 - magnitude) if (u & 0x80) else (magnitude - 0x84)
        table.append(struct.pack("<h", max(-32768, min(32767, sample))))
    return table


_ULAW_TO_PCM16 = _build_ulaw_table()


def ulaw_to_pcm16(data: bytes) -> bytes:
    return b"".join(map(_ULAW_TO_PCM16.__getitem__, data))


class CallRecorder:
    """Accumulates both audio legs plus the running transcript of one call."""

    def __init__(self, out_dir="recordings", call_sid=None, stream_sid=None):
        self.out_dir = Path(out_dir)
        self.call_sid = call_sid
        self.stream_sid = stream_sid
        self.started_at = datetime.now(timezone.utc)
        self._caller = bytearray()  # inbound mu-law; doubles as the call clock
        self._agent = bytearray()  # outbound mu-law, aligned to the same clock
        self.transcript = []

    # ---- audio ---------------------------------------------------------

    def add_caller_audio(self, ulaw: bytes):
        self._caller += ulaw

    def add_agent_audio(self, ulaw: bytes):
        if len(self._agent) < len(self._caller):
            self._agent += bytes([SILENCE]) * (len(self._caller) - len(self._agent))
        self._agent += ulaw

    def truncate_agent_to_now(self):
        """Drop assistant audio that was cut off by an interruption."""
        del self._agent[len(self._caller):]

    # ---- transcript ----------------------------------------------------

    def add_transcript(self, role: str, text: str):
        text = (text or "").strip()
        if not text:
            return
        self.transcript.append(
            {"role": role, "at": round(len(self._caller) / SAMPLE_RATE, 2), "text": text}
        )

    # ---- output --------------------------------------------------------

    @property
    def duration(self) -> float:
        return max(len(self._caller), len(self._agent)) / SAMPLE_RATE

    def _basename(self) -> str:
        stamp = self.started_at.strftime("%Y%m%d-%H%M%S")
        return f"{stamp}-{self.call_sid or self.stream_sid or 'call'}"

    def save(self):
        """Write <basename>.wav / .json / .txt. Returns the paths written."""
        if not self._caller and not self._agent:
            return {}

        self.out_dir.mkdir(parents=True, exist_ok=True)
        base = self.out_dir / self._basename()

        frames = max(len(self._caller), len(self._agent))
        left = ulaw_to_pcm16(bytes(self._caller).ljust(frames, bytes([SILENCE])))
        right = ulaw_to_pcm16(bytes(self._agent).ljust(frames, bytes([SILENCE])))

        # Caller on the left channel, assistant on the right.
        stereo = bytearray(frames * 4)
        stereo[0::4] = left[0::2]
        stereo[1::4] = left[1::2]
        stereo[2::4] = right[0::2]
        stereo[3::4] = right[1::2]

        wav_path = base.with_suffix(".wav")
        with wave.open(str(wav_path), "wb") as wav:
            wav.setnchannels(2)
            wav.setsampwidth(2)
            wav.setframerate(SAMPLE_RATE)
            wav.writeframes(bytes(stereo))

        json_path = base.with_suffix(".json")
        json_path.write_text(
            json.dumps(
                {
                    "call_sid": self.call_sid,
                    "stream_sid": self.stream_sid,
                    "started_at": self.started_at.isoformat(),
                    "duration_seconds": round(self.duration, 2),
                    "audio_file": wav_path.name,
                    "transcript": self.transcript,
                },
                indent=2,
            )
        )

        txt_path = base.with_suffix(".txt")
        txt_path.write_text(
            "\n".join(
                f"[{entry['at']:>7.2f}s] {entry['role']:>9}: {entry['text']}"
                for entry in self.transcript
            )
            + "\n"
        )

        return {"audio": wav_path, "json": json_path, "text": txt_path}
