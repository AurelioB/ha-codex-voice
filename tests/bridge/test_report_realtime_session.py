from __future__ import annotations

from datetime import UTC, datetime

from scripts.report_realtime_session import build_reports, parse_events, render_markdown


def test_report_groups_existing_logs_without_runtime_integration() -> None:
    device = """
Aug 13 06:53:04 host info app: codex-voice realtime_capture_open attempt=1/3
Aug 13 06:53:17 host info app: codex-voice bridge_capture packets=208 suppressed_packets=0 source_peak=475 source_rms=180 provider_peak=3773 provider_rms=1430 saturated_samples=0
Aug 13 06:53:27 host info app: codex-voice realtime_capture_open attempt=1/3
Aug 13 06:53:45 host info app: codex-voice bridge_capture packets=275 suppressed_packets=0 source_peak=968 source_rms=294 provider_peak=7689 provider_rms=2336 saturated_samples=0
"""
    bridge = """
2026-08-13 06:53:10,068 INFO bridge.service: Realtime native input transcript: fragments=2 fragment_chars=8 final_chars=8
2026-08-13 06:53:10,069 INFO bridge.service: Realtime debug transcript: role=user text='Hola tú'
2026-08-13 06:53:17,389 INFO bridge.service: Realtime debug transcript: role=assistant text='Hola.'
2026-08-13 06:53:35,769 INFO bridge.service: Realtime native input transcript: fragments=4 fragment_chars=29 final_chars=29
2026-08-13 06:53:35,770 INFO bridge.service: Realtime debug transcript: role=user text='Cuenta hasta diez, por favor.'
2026-08-13 06:53:40,863 INFO bridge.service: Realtime debug transcript: role=assistant text='Uno, dos, tres.'
"""

    reports = build_reports(parse_events(device, bridge, year=2026))

    assert len(reports) == 2
    assert reports[0].capture is not None
    assert reports[0].capture["provider_rms"] == 1430
    assert reports[1].input_shapes == [
        {"fragments": 4, "fragment_chars": 29, "final_chars": 29}
    ]
    assert reports[1].transcripts[0]["text"] == "Cuenta hasta diez, por favor."
    assert reports[1].observations == [
        "No transport, clipping, or capture anomaly detected."
    ]


def test_report_includes_dual_mic_health_and_anomalies() -> None:
    device = """
Aug 13 06:52:57 host info app: codex-voice aec3_capture captured_frames=2000 coherent_mic_frames=500 primary_only_mic_frames=1500 dropped_frames=0 recoveries=0 processing_failures=0
Aug 13 06:53:04 host info app: codex-voice realtime_capture_open attempt=1/3
Aug 13 06:53:17 host info app: codex-voice bridge_capture packets=20 suppressed_packets=2 source_peak=475 source_rms=180 provider_peak=32768 provider_rms=1430 saturated_samples=3
"""
    reports = build_reports(parse_events(device, "", year=2026))

    assert reports[0].aec3 is not None
    assert reports[0].aec3["coherent"] == 500
    assert "Provider gain saturated 3 PCM samples." in reports[0].observations
    assert "Echo policy suppressed 2 packets." in reports[0].observations
    markdown = render_markdown(reports)
    assert "Dual microphone coherent frames: 25.0%" in markdown


def test_bridge_timestamp_is_utc() -> None:
    events = parse_events(
        "Aug 13 06:53:04 host info app: codex-voice realtime_capture_open attempt=1/3",
        "2026-08-13 06:53:05,123 INFO bridge.service: Direct realtime terminal intent: source=tool",
        year=2026,
    )

    assert events[0][0] == datetime(2026, 8, 13, 6, 53, 4, tzinfo=UTC)
    assert events[1][1].timestamp == "2026-08-13T06:53:05.123Z"


def test_empty_report_explains_that_the_window_has_no_sessions() -> None:
    assert "No realtime sessions were found" in render_markdown([])
