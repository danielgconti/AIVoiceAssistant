"""Write a per-call bug report next to the recording.

The transcript alone does not tell you much six calls later, so each call gets
a markdown file naming the scenario, what it was probing, what to look for,
and the conversation itself. If an analysis model is configured, the "Bugs
observed" section is filled in from the transcript against that scenario's
checks; otherwise the section is left for you to fill in by hand.

A one-line entry per call is also appended to recordings/BUGS.md, so the whole
run reads top to bottom.
"""

import json
import logging
import os
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger("report")

ANALYSIS_MODEL = os.getenv("ANALYSIS_MODEL", "gpt-4o")
COMPLETIONS_URL = "https://api.openai.com/v1/chat/completions"

ANALYST_BRIEF = """You are reviewing a recorded test call. A tester posing as a patient
phoned a doctor's office whose phone is answered by an automated assistant. Your job is
to report bugs in the OFFICE ASSISTANT's behaviour -- not the tester's.

Report only what the transcript actually shows. Do not speculate about what might have
happened off-transcript, and do not invent problems to fill space. If the assistant
handled something correctly, do not list it as a bug.

Answer in GitHub-flavoured markdown with exactly these sections:

### Bugs
A bullet per bug. Start each with a bold short label, then what happened, then quote the
line that shows it. Order worst first. If there are none, write "None observed."

### Correct behaviour worth noting
Brief bullets, only where the assistant clearly got a hard case right. Omit if none.

### Inconclusive
Anything the scenario meant to probe that the call never actually reached, and why."""


def transcript_text(transcript):
    return "\n".join(f"[{line['at']:>7.2f}s] {line['role']}: {line['text']}" for line in transcript)


def analyse(scenario, transcript, api_key):
    """Ask a text model to name the bugs. Returns markdown, or None."""
    if not ANALYSIS_MODEL or ANALYSIS_MODEL.lower() in ("off", "none", ""):
        return None
    if not api_key:
        log.warning("no OPENAI_API_KEY; skipping automated analysis")
        return None
    if not transcript:
        return None

    checks = "\n".join(f"- {check}" for check in scenario.checks)
    prompt = (
        f"SCENARIO: {scenario.title}\n"
        f"WHAT IT PROBES: {scenario.probes}\n\n"
        f"WHAT THE TESTER WAS TOLD TO DO:\n{scenario.body.strip()}\n\n"
        f"WHAT TO LOOK FOR:\n{checks}\n\n"
        f"TRANSCRIPT (the tester is 'assistant'; the office assistant being "
        f"tested is 'caller'):\n{transcript_text(transcript)}"
    )
    payload = {
        "model": ANALYSIS_MODEL,
        "messages": [
            {"role": "system", "content": ANALYST_BRIEF},
            {"role": "user", "content": prompt},
        ],
    }
    request = urllib.request.Request(
        COMPLETIONS_URL,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            body = json.load(response)
        return body["choices"][0]["message"]["content"].strip()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        log.error("analysis failed: HTTP %s %s", exc.code, detail)
        if exc.code == 404:
            log.error(
                "model %r was rejected -- set ANALYSIS_MODEL to one your "
                "account has, or ANALYSIS_MODEL=off to skip analysis.",
                ANALYSIS_MODEL,
            )
    except Exception as exc:
        log.error("analysis failed: %s: %s", type(exc).__name__, exc)
    return None


def write(recorder, scenario, paths, api_key=None):
    """Write <basename>.md and append a line to BUGS.md. Returns the path."""
    if not paths:
        return None

    bugs = analyse(scenario, recorder.transcript, api_key)
    audio = Path(paths["audio"])
    report_path = audio.with_suffix(".md")

    checks = "\n".join(f"- [ ] {check}" for check in scenario.checks)
    lines = [
        f"# {scenario.title}",
        "",
        f"- **Scenario:** {scenario.number}. `{scenario.slug}` — probes {scenario.probes}",
        f"- **Call:** {recorder.call_sid or '(unknown sid)'}",
        f"- **When:** {recorder.started_at:%Y-%m-%d %H:%M:%S} UTC",
        f"- **Duration:** {recorder.duration:.0f}s (target {scenario.minutes})",
        f"- **Audio:** [`{audio.name}`](./{audio.name})",
        "",
        "## Bugs observed",
        "",
        bugs if bugs else "_Automated analysis unavailable — fill in by hand._",
        "",
        "## What this call was checking",
        "",
        checks,
        "",
        "## Notes",
        "",
        "_(your own observations)_",
        "",
        "## Transcript",
        "",
        "```",
        transcript_text(recorder.transcript) or "(no transcript captured)",
        "```",
        "",
    ]
    report_path.write_text("\n".join(lines))

    index = audio.parent / "BUGS.md"
    if not index.exists():
        index.write_text("# Bug log\n\nOne line per test call, newest at the bottom.\n\n")
    with index.open("a") as handle:
        handle.write(
            f"- {recorder.started_at:%Y-%m-%d %H:%M} · **{scenario.number}.** `{scenario.slug}` · "
            f"{recorder.duration:.0f}s · [report](./{report_path.name})"
            f"{'' if bugs else ' · _not yet analysed_'}\n"
        )

    log.info("wrote report %s", report_path)
    return report_path
