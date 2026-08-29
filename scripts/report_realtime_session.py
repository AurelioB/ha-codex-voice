#!/usr/bin/env python3
"""Build an on-demand realtime session report from existing logs."""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_DEVICE_TIME = re.compile(
    r"^(?P<month>[A-Z][a-z]{2})\s+(?P<day>\d{1,2})\s+(?P<time>\d{2}:\d{2}:\d{2})"
)
_BRIDGE_TIME = re.compile(r"^(?P<time>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})")
_CAPTURE_METRICS = re.compile(
    r"bridge_capture packets=(?P<packets>\d+) suppressed_packets=(?P<suppressed>\d+) "
    r"source_peak=(?P<source_peak>\d+) source_rms=(?P<source_rms>\d+) "
    r"provider_peak=(?P<provider_peak>\d+) provider_rms=(?P<provider_rms>\d+) "
    r"saturated_samples=(?P<saturated>\d+)"
)
_AEC_METRICS = re.compile(
    r"aec3_capture captured_frames=(?P<captured>\d+) "
    r"coherent_mic_frames=(?P<coherent>\d+) "
    r"primary_only_mic_frames=(?P<primary>\d+) "
    r"dropped_frames=(?P<dropped>\d+) recoveries=(?P<recoveries>\d+) "
    r"processing_failures=(?P<failures>\d+)"
)
_DEBUG_TRANSCRIPT = re.compile(
    r"Realtime debug transcript: role=(?P<role>\w+) text=(?P<text>.+)$"
)
_INPUT_SHAPE = re.compile(
    r"Realtime native input transcript: fragments=(?P<fragments>\d+) "
    r"fragment_chars=(?P<fragment_chars>\d+) final_chars=(?P<final_chars>\d+)"
)
_BARGE = re.compile(
    r"Realtime native barge: sequence=(?P<sequence>\d+) source=(?P<source>\w+) "
    r"milestone=(?P<milestone>\w+) elapsed_ms=(?P<elapsed>\d+)"
)


@dataclass(slots=True)
class TimelineEvent:
    """One relevant content or lifecycle event on the merged UTC timeline."""

    timestamp: str
    kind: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SessionReport:
    """One capture-open interval and its derived diagnostic observations."""

    started_at: str
    ended_at: str | None = None
    transcripts: list[dict[str, str]] = field(default_factory=list)
    input_shapes: list[dict[str, int]] = field(default_factory=list)
    capture: dict[str, int] | None = None
    aec3: dict[str, int] | None = None
    barge: list[dict[str, Any]] = field(default_factory=list)
    timeline: list[TimelineEvent] = field(default_factory=list)
    observations: list[str] = field(default_factory=list)


def _parse_device_time(line: str, *, year: int) -> datetime | None:
    match = _DEVICE_TIME.match(line)
    if match is None:
        return None
    value = f"{year} {match['month']} {match['day']} {match['time']}"
    return datetime.strptime(value, "%Y %b %d %H:%M:%S").replace(tzinfo=UTC)


def _parse_bridge_time(line: str) -> datetime | None:
    match = _BRIDGE_TIME.match(line)
    if match is None:
        return None
    return datetime.strptime(match["time"], "%Y-%m-%d %H:%M:%S,%f").replace(tzinfo=UTC)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_events(
    device_log: str,
    bridge_log: str,
    *,
    year: int,
) -> list[tuple[datetime, TimelineEvent]]:
    """Return relevant events sorted on one UTC timeline."""

    events: list[tuple[datetime, TimelineEvent]] = []
    for line in device_log.splitlines():
        timestamp = _parse_device_time(line, year=year)
        if timestamp is None:
            continue
        if "codex-voice realtime_capture_open" in line:
            events.append((timestamp, TimelineEvent(_iso(timestamp), "capture_open")))
            continue
        capture = _CAPTURE_METRICS.search(line)
        if capture is not None:
            events.append(
                (
                    timestamp,
                    TimelineEvent(
                        _iso(timestamp),
                        "capture_summary",
                        {key: int(value) for key, value in capture.groupdict().items()},
                    ),
                )
            )
            continue
        aec = _AEC_METRICS.search(line)
        if aec is not None:
            events.append(
                (
                    timestamp,
                    TimelineEvent(
                        _iso(timestamp),
                        "aec3_summary",
                        {key: int(value) for key, value in aec.groupdict().items()},
                    ),
                )
            )

    for line in bridge_log.splitlines():
        timestamp = _parse_bridge_time(line)
        if timestamp is None:
            continue
        transcript = _DEBUG_TRANSCRIPT.search(line)
        if transcript is not None:
            try:
                parsed = ast.literal_eval(transcript["text"])
                text = parsed if isinstance(parsed, str) else str(parsed)
            except (SyntaxError, ValueError):
                text = transcript["text"].strip("'\"")
            events.append(
                (
                    timestamp,
                    TimelineEvent(
                        _iso(timestamp),
                        "transcript",
                        {"role": transcript["role"], "text": text},
                    ),
                )
            )
            continue
        shape = _INPUT_SHAPE.search(line)
        if shape is not None:
            events.append(
                (
                    timestamp,
                    TimelineEvent(
                        _iso(timestamp),
                        "input_shape",
                        {key: int(value) for key, value in shape.groupdict().items()},
                    ),
                )
            )
            continue
        barge = _BARGE.search(line)
        if barge is not None:
            detail: dict[str, Any] = dict(barge.groupdict())
            detail["sequence"] = int(detail["sequence"])
            detail["elapsed"] = int(detail["elapsed"])
            events.append((timestamp, TimelineEvent(_iso(timestamp), "barge", detail)))
            continue
        if "Direct realtime terminal intent:" in line:
            events.append(
                (timestamp, TimelineEvent(_iso(timestamp), "terminal_intent"))
            )
        elif "Realtime handshake timing:" in line:
            events.append((timestamp, TimelineEvent(_iso(timestamp), "handshake")))
    return sorted(events, key=lambda item: item[0])


def build_reports(
    events: list[tuple[datetime, TimelineEvent]],
) -> list[SessionReport]:
    """Group events by device capture-open boundaries."""

    reports: list[SessionReport] = []
    current: SessionReport | None = None
    pending_aec: TimelineEvent | None = None
    for _, event in events:
        if event.kind == "aec3_summary":
            pending_aec = event
            if current is not None:
                current.aec3 = dict(event.detail)
            continue
        if event.kind == "capture_open":
            if current is not None:
                current.ended_at = event.timestamp
                _observe(current)
            current = SessionReport(started_at=event.timestamp)
            if pending_aec is not None:
                current.aec3 = dict(pending_aec.detail)
            current.timeline.append(event)
            reports.append(current)
            continue
        if current is None:
            continue
        current.timeline.append(event)
        if event.kind == "transcript":
            current.transcripts.append(
                {"role": str(event.detail["role"]), "text": str(event.detail["text"])}
            )
        elif event.kind == "input_shape":
            current.input_shapes.append(
                {key: int(value) for key, value in event.detail.items()}
            )
        elif event.kind == "capture_summary":
            current.capture = {key: int(value) for key, value in event.detail.items()}
            current.ended_at = event.timestamp
        elif event.kind == "barge":
            current.barge.append(dict(event.detail))
    if current is not None:
        _observe(current)
    return reports


def _observe(report: SessionReport) -> None:
    if report.capture is None:
        report.observations.append("Capture summary is not available yet.")
    else:
        capture = report.capture
        if capture["packets"] == 0:
            report.observations.append("No microphone packets entered the session.")
        if capture["saturated"]:
            report.observations.append(
                f"Provider gain saturated {capture['saturated']} PCM samples."
            )
        if capture["suppressed"]:
            report.observations.append(
                f"Echo policy suppressed {capture['suppressed']} packets."
            )
    if report.aec3 is not None:
        unhealthy = sum(
            report.aec3[key] for key in ("dropped", "recoveries", "failures")
        )
        if unhealthy:
            report.observations.append("Native capture reported a health failure.")
    if not any(item["role"] in {"input", "user"} for item in report.transcripts):
        report.observations.append("No final user transcript was logged.")
    if not report.observations:
        report.observations.append(
            "No transport, clipping, or capture anomaly detected."
        )


def render_markdown(reports: list[SessionReport]) -> str:
    """Render reports for a human debugging session."""

    lines = ["# Realtime session report", ""]
    if not reports:
        lines.extend(
            [
                "No realtime sessions were found in the selected log window.",
                "",
            ]
        )
        return "\n".join(lines)
    for index, report in enumerate(reports, start=1):
        lines.extend(
            [
                f"## Session {index}",
                "",
                f"- Started: `{report.started_at}`",
                f"- Ended: `{report.ended_at or 'still active / unknown'}`",
            ]
        )
        if report.capture is not None:
            capture = report.capture
            lines.append(
                "- Capture: "
                f"{capture['packets']} packets; source RMS {capture['source_rms']}; "
                f"provider RMS {capture['provider_rms']}; "
                f"{capture['saturated']} saturated samples"
            )
        if report.aec3 is not None:
            aec = report.aec3
            total = aec["coherent"] + aec["primary"]
            ratio = 0.0 if total == 0 else 100.0 * aec["coherent"] / total
            lines.append(f"- Dual microphone coherent frames: {ratio:.1f}%")
        lines.extend(["", "### Transcript", ""])
        if report.transcripts:
            lines.extend(
                f"- **{item['role']}**: {item['text']}" for item in report.transcripts
            )
        else:
            lines.append("- No transcript text logged.")
        lines.extend(["", "### Observations", ""])
        lines.extend(f"- {value}" for value in report.observations)
        lines.append("")
    return "\n".join(lines)


def _run(command: list[str]) -> str:
    completed = subprocess.run(
        command,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return completed.stdout


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--live", action="store_true", help="read Docker and ADB logs")
    source.add_argument("--bridge-log", type=Path, help="saved bridge log")
    parser.add_argument("--device-log", type=Path, help="saved device log")
    parser.add_argument("--container", default="ha-codex-voice-bridge-1")
    parser.add_argument("--adb-serial")
    parser.add_argument("--since", default="4h")
    parser.add_argument("--latest", type=int, default=3)
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser.parse_args()


def main() -> int:
    """Collect or read logs and print the requested report format."""

    args = _arguments()
    if args.latest < 1 or args.latest > 100:
        raise SystemExit("--latest must be in 1..100")
    if args.live:
        bridge_log = _run(["docker", "logs", "--since", args.since, args.container])
        adb = ["adb"]
        if args.adb_serial:
            adb.extend(["-s", args.adb_serial])
        device_log = _run([*adb, "shell", "cat", "/tmp/messages"])  # noqa: S108
    else:
        if args.device_log is None:
            raise SystemExit("--device-log is required with --bridge-log")
        bridge_log = args.bridge_log.read_text(encoding="utf-8", errors="replace")
        device_log = args.device_log.read_text(encoding="utf-8", errors="replace")
    reports = build_reports(
        parse_events(device_log, bridge_log, year=datetime.now(UTC).year)
    )[-args.latest :]
    if args.as_json:
        json.dump([asdict(report) for report in reports], sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        print(render_markdown(reports))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
