An AI agent that phones a doctor's office and behaves like a patient, to find out where the office's
automated phone assistant breaks.

The office assistant under test is meant to answer general questions, refill prescriptions, and set,
change, or delete appointments. Our caller runs one **scenario** per call — a patient with a specific
agenda designed to stress one weak spot — and every call is saved with its audio, transcript, and a
bug report.

The caller's voice comes from OpenAI's Realtime API, streamed straight into the live Twilio call. It
listens while the other side speaks and stops talking as soon as it is interrupted. The server runs on
your machine and Twilio reaches it through an ngrok tunnel, so recordings land in `recordings/` here.

## Setup

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
brew install ngrok        # or https://ngrok.com/download
```

`.env` needs:

```
export TWILIO_ACCOUNT_SID=...
export TWILIO_AUTH_TOKEN=...
export OPENAI_API_KEY=...
```

## Placing a test call

Three terminals:

```bash
python server.py     # 1. the caller, on :5050
ngrok http 5050      # 2. the tunnel Twilio dials in through
python index.py      # 3. pick a scenario and ring the phone
```

`index.py` shows a menu, or takes the scenario directly:

```bash
python index.py invalid-dates
python index.py 3               # by menu number
python index.py --list
```

It finds the ngrok URL on its own and checks the tunnel really reaches the server before spending
money on a call. Terminal 1 prints the conversation as it happens.

## The scenarios

| | scenario | what it probes |
|---|---|---|
| 1 | `invalid-dates` | Feb 30th, past dates, "Tuesday the 40th", 3am, Sundays — date validation and read-back |
| 2 | `midcall-switch` | booking → refill → back to booking → cancel instead; does state survive, or bleed |
| 3 | `marathon` | **the long one (8–10 min)** — details dropped early and referenced much later |
| 4 | `identity-drift` | two different birth dates, a nickname, "actually it's my husband's" — will it still refill |
| 5 | `interruptions` | talking over it constantly, changing values mid-sentence; does the last value win |
| 6 | `out-of-scope` | medical advice, prescribing, billing, and a chest-pain symptom slipped in casually |
| 7 | `batch-request` | three tasks in one breath; does the second medication quietly get dropped |
| 8 | `cancel-undo` | cancel without confirming, un-cancel, then cancel an appointment that never existed |
| 9 | `noisy-caller` | trailing off, silences, "oh-one-four-two", spelled surnames, a mumbled month |
| 10 | `persistent-pressure` | refusing to take no, an invented promise from "Marcy", asking for an exception four times |

Every call is capped at its scenario's length: the caller is told to wrap up 25 seconds before the
cap, and the call is hung up at the cap if it has not.

## What each call leaves behind

In `recordings/`, sharing one basename per call:

```
20260827-141203-08-cancel-undo-CAxxxx.wav    stereo — our caller left, the office assistant right
                                     .txt    readable transcript with timestamps
                                     .json   transcript plus call metadata
                                     .md     the bug report
recordings/BUGS.md                           one line per call, linking to each report
```

The name is `<when>-<NN-scenario>-<call sid>`, so the directory sorts by time and you can still see at
a glance which of the ten prompts a file came from (and `ls recordings/*-08-*` picks out one
scenario's calls).

The bug report names the scenario, lists the bugs found in the transcript, flags anything the scenario
meant to probe but never reached, and leaves a checklist and a notes section for your own review.
Analysis uses `ANALYSIS_MODEL` (default `gpt-4o`); set `ANALYSIS_MODEL=off` to skip it and write the
report with the bugs section blank.

## When a call fails

```bash
python debug_twilio.py     # checks config, the tunnel, and Twilio's own error log
LOG_LEVEL=DEBUG python server.py
```
