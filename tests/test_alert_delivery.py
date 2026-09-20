import json
import logging
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

import alerts
from alert_channels.millerbot import MillerBotChannel
from alerts import ALERT_SEVERITY_MAP, Alert, AlertManager, AlertType


LOGGER = logging.getLogger("alert-delivery-tests")
BASELINE = Path(__file__).parent.joinpath("fixtures", "contracts", "baseline.json")
LEGACY_TYPES = {
    "leak": "critical",
    "malfunction_no_flow": "warning",
    "irregular_flow": "warning",
    "system_exit": "critical",
    "sensor_error": "warning",
}


class FakeClock:
    def __init__(self, now=datetime(2025, 6, 15, 10)):
        self.value = 0.0
        self.wall = now

    def monotonic(self):
        return self.value

    def now(self):
        return self.wall

    def advance(self, seconds, advance_wall=True):
        self.value += seconds
        if advance_wall:
            self.wall += timedelta(seconds=seconds)


class FakeChannel:
    def __init__(self, outcomes=(), default=True):
        self.outcomes = deque(outcomes)
        self.default = default
        self.calls = []
        self.threads = []

    def send(self, alert):
        self.calls.append(alert)
        self.threads.append(threading.current_thread())
        result = self.outcomes.popleft() if self.outcomes else self.default
        if isinstance(result, Exception):
            raise result
        return result


class BlockingChannel(FakeChannel):
    def __init__(self, first_result=True):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.second_attempt = threading.Event()
        self.first_result = first_result

    def send(self, alert):
        super().send(alert)
        if len(self.calls) == 1:
            self.entered.set()
            if not self.release.wait(5):
                raise RuntimeError("Test did not release its blocked fake channel")
            return self.first_result
        self.second_attempt.set()
        return True


class FakeSchedules:
    def shouldScheduleRun(self, schedule, check_date=None):
        season = "Summer" if 6 <= check_date.month <= 8 else "Winter"
        return (
            (not schedule.days or check_date.strftime("%a") in schedule.days)
            and (not schedule.seasons or season in schedule.seasons)
        )

    def calculateScheduleTime(self, schedule, now):
        if schedule.time_based_on == "fixed":
            hour, minute = map(int, schedule.fixed_start_time.split(":"))
        else:
            hour, minute = (6, 0) if schedule.time_based_on == "sunrise" else (20, 0)
        return now.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(
            minutes=getattr(schedule, "offset_minutes", 0)
        )


def make_config(channel_count=1):
    return SimpleNamespace(cfg=SimpleNamespace(alerts=SimpleNamespace(
        enabled=SimpleNamespace(**{name: True for name in LEGACY_TYPES}),
        leak_repeat_minutes=15,
        leak_detection_exclusions=[],
        channels=[SimpleNamespace(type="fake", name=str(i)) for i in range(channel_count)],
    )))


def exclusion(**kwargs):
    settings = {
        "time_based_on": "fixed", "fixed_start_time": "23:45", "duration": 60,
        "days": ["Mon"], "seasons": ["Summer"],
    }
    settings.update(kwargs)
    return SimpleNamespace(**settings)


@pytest.fixture
def manager_factory():
    managers = []

    def make(*channels, clock=None, config=None, channel_factory=None,
             queue_capacity=128, schedules=None):
        if config is None:
            config = make_config(len(channels))
        if channel_factory is None:
            channel_factory = lambda logger, cfg: channels[int(cfg.name)]
        manager = AlertManager(
            LOGGER, config, schedules if schedules is not None else FakeSchedules(),
            clock=clock if clock is not None else FakeClock(),
            channel_factory=channel_factory, queue_capacity=queue_capacity,
        )
        managers.append(manager)
        return manager

    yield make
    for manager in managers:
        assert manager.shutdown(timeout=1), "Alert worker leaked from a test"
        assert not manager.get_health()["worker_alive"]


def test_alert_identifiers_and_severities_are_compatible():
    expected = dict(LEGACY_TYPES)
    expected.update(
        safety_intervention="critical", actuation_failure="critical",
        monitoring_unavailable="warning",
    )
    assert {kind.value: severity.value for kind, severity in ALERT_SEVERITY_MAP.items()} == expected
    assert {kind.value for kind in AlertType} == set(expected)


def test_golden_envelope_and_exact_millerbot_post(monkeypatch):
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    original = baseline["alert"]
    occurrence = Alert(
        AlertType(original["type"]), original["valve_name"],
        datetime.fromisoformat(original["timestamp"]), original["message"], original["data"],
    )
    assert occurrence.to_dict() == original
    assert list(occurrence.to_dict()) == [
        "type", "severity", "valve_name", "timestamp", "message", "data",
    ]
    expected_request = baseline["channel_request"][0]
    cfg = SimpleNamespace(
        url=expected_request["url"], user_id=expected_request["json"]["user_id"],
        role=expected_request["json"]["role"],
        api_key=expected_request["headers"]["X-Api-Key"],
    )
    posts = []
    checked = []

    def fake_post(url, **kwargs):
        posts.append({"url": url, **kwargs})
        return SimpleNamespace(raise_for_status=lambda: checked.append(True))

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(time, "sleep", lambda *_: pytest.fail("Channel must not sleep"))
    channel = MillerBotChannel(LOGGER, cfg)
    assert posts == []
    assert channel.send(occurrence) is True
    assert posts == baseline["channel_request"]
    assert checked == [True]


@pytest.mark.parametrize("failure_stage,error_type", [
    ("post", requests.exceptions.Timeout),
    ("post", requests.exceptions.ConnectionError),
    ("status", requests.exceptions.HTTPError),
])
def test_millerbot_failure_is_one_sanitized_attempt(
        monkeypatch, caplog, failure_stage, error_type):
    calls = []
    status_checks = []
    secret = "secret-value-in-request-https://private.invalid"

    def check_status():
        status_checks.append(True)
        raise error_type(secret)

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        if failure_stage == "post":
            raise error_type(secret)
        return SimpleNamespace(raise_for_status=check_status)

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(time, "sleep", lambda *_: pytest.fail("Channel must not back off"))
    channel = MillerBotChannel(LOGGER, SimpleNamespace(
        url="https://alerts.invalid/proactive", user_id=123, api_key=secret, role="irrigation",
    ))
    occurrence = Alert(AlertType.SYSTEM_EXIT, None, datetime(2025, 6, 15), "Exit", {})
    assert channel.send(occurrence) is False
    assert len(calls) == 1
    assert calls[0][1]["timeout"] == 10
    assert len(status_checks) == (1 if failure_stage == "status" else 0)
    assert error_type.__name__ in caplog.text
    assert secret not in caplog.text
    assert "successfully" not in caplog.text


def test_millerbot_system_message_omits_empty_sections():
    channel = MillerBotChannel(LOGGER, SimpleNamespace(
        url="https://alerts.invalid", user_id=123, api_key="fixture", role="irrigation",
    ))
    occurrence = Alert(
        AlertType.SYSTEM_EXIT, None, datetime(2025, 6, 15, 10),
        "Shutdown requested; close command failed", {},
    )
    assert channel._format_message(occurrence) == (
        "[CRITICAL] system_exit\nTime: 2025-06-15T10:00:00\n"
        "Message: Shutdown requested; close command failed"
    )


def test_constructor_and_alert_are_local_and_snapshot_occurrence(manager_factory, caplog):
    channel = FakeChannel()
    manager = manager_factory(channel)
    data = {"nested": {"value": 2.5}}
    assert manager._worker is None
    assert manager.alert(AlertType.SENSOR_ERROR, "Observation", data=data, subject="private-sensor")
    assert channel.calls == []
    assert manager.get_health()["status"] == "not_started"
    assert manager.get_health()["queue_depth"] == 1
    assert "ALERT [WARNING] sensor_error: Observation" in caplog.text
    assert "private-sensor" not in caplog.text
    data["nested"]["value"] = 999
    assert manager.dispatch_pending()
    assert channel.calls[0].data == {"nested": {"value": 2.5}}
    assert channel.calls[0].valve_name is None
    assert "private-sensor" not in json.dumps(channel.calls[0].to_dict())
    formatter = MillerBotChannel(LOGGER, SimpleNamespace(url="", user_id=0, api_key="", role=""))
    text = formatter._format_message(channel.calls[0])
    assert "Valve:" not in text
    assert "private-sensor" not in text


def test_default_factory_and_clock_do_not_start_delivery(monkeypatch):
    channel = FakeChannel()
    constructed = []

    def factory(logger, cfg):
        constructed.append((logger, cfg.name))
        return channel

    monkeypatch.setattr(alerts, "channelFactory", factory)
    manager = AlertManager(LOGGER, make_config(), FakeSchedules())
    try:
        assert constructed == [(LOGGER, "0")]
        assert manager.alert(AlertType.SYSTEM_EXIT, "Shutdown requested")
        assert channel.calls == []
        assert manager.dispatch_pending()
        assert isinstance(channel.calls[0].timestamp, datetime)
    finally:
        assert manager.shutdown(timeout=0)


def test_retries_follow_bounded_backoff_not_incident_suppression(
        manager_factory, monkeypatch, caplog):
    clock = FakeClock()
    channel = FakeChannel(default=False)
    manager = manager_factory(channel, clock=clock)
    monkeypatch.setattr(time, "sleep", lambda *_: pytest.fail("Dispatch must not sleep"))
    assert manager.alert(AlertType.SENSOR_ERROR, "Sensor stale", subject="weather")
    for index, when in enumerate((0, 2, 6, 14, 30), start=1):
        clock.advance(when - clock.monotonic())
        assert manager.dispatch_pending()
        assert len(channel.calls) == index
        assert not manager.dispatch_pending()
        assert not manager.alert(AlertType.SENSOR_ERROR, "Still stale", subject="weather")
    clock.advance(10000)
    assert not manager.dispatch_pending()
    health = manager.get_health()
    assert health["queue_depth"] == 0
    assert health["active_incidents"] == 1
    assert health["failed_incidents"] == 1
    assert health["errors"]["failed_attempts"] == 5
    assert health["errors"]["retry_exhausted"] == 1
    assert health["retries_scheduled"] == 4
    assert health["delivered"] == 0
    assert not health["healthy"]
    for delay in (2, 4, 8, 16):
        assert f"retry in {delay}s" in caplog.text
    assert "retries exhausted" in caplog.text
    assert manager.clear_alert_state(AlertType.SENSOR_ERROR, subject="weather")
    assert manager.get_health()["healthy"]
    assert manager.alert(AlertType.SENSOR_ERROR, "New incident", subject="weather")
    assert manager.dispatch_pending()
    assert len(channel.calls) == 6


def test_only_failed_channels_retry_and_success_does_not_resolve(
        manager_factory, caplog):
    clock = FakeClock()
    good = FakeChannel()
    bad = FakeChannel([False, RuntimeError("do-not-log-request-secret"), True])
    manager = manager_factory(good, bad, clock=clock)
    assert manager.alert(AlertType.ACTUATION_FAILURE, "Close command failed", valve_name="Valve")
    assert manager.dispatch_pending()
    assert manager.dispatch_pending()
    assert manager.get_health()["retrying_channels"] == 1
    assert not manager.alert(AlertType.ACTUATION_FAILURE, "Still failing", valve_name="Valve")
    clock.advance(2)
    assert manager.dispatch_pending()
    clock.advance(4)
    assert manager.dispatch_pending()
    assert len(good.calls) == 1
    assert len(bad.calls) == 3
    assert not manager.dispatch_pending()
    health = manager.get_health()
    assert health["delivered"] == 1
    assert health["delivered_channels"] == 2
    assert health["errors"]["channel_exceptions"] == 1
    assert health["active_incidents"] == 1
    assert health["healthy"]
    assert "RuntimeError" in caplog.text
    assert "do-not-log-request-secret" not in caplog.text
    assert not manager.alert(AlertType.ACTUATION_FAILURE, "Still failing", valve_name="Valve")


@pytest.mark.parametrize("result", [False, None, 1, "success"])
def test_only_true_is_confirmed_channel_success(manager_factory, result):
    manager = manager_factory(FakeChannel([result]))
    assert manager.alert(AlertType.MONITORING_UNAVAILABLE, "Unavailable", subject="flow")
    assert manager.dispatch_pending()
    assert manager.get_health()["errors"]["failed_attempts"] == 1
    assert manager.get_health()["delivered"] == 0


def test_subject_recovery_isolated_from_other_subjects_and_valves(manager_factory):
    channel = FakeChannel()
    manager = manager_factory(channel)
    for subject in ("sensor-a", "sensor-b"):
        assert manager.alert(AlertType.SENSOR_ERROR, "Offline", subject=subject)
        assert manager.dispatch_pending()
    assert all(occurrence.valve_name is None for occurrence in channel.calls)
    assert not manager.clear_alert_state(AlertType.SENSOR_ERROR)
    assert not manager.clear_alert_state(AlertType.SENSOR_ERROR, valve_name="sensor-a")
    assert manager.clear_alert_state(AlertType.SENSOR_ERROR, subject="sensor-a")
    assert not manager.alert(AlertType.SENSOR_ERROR, "Offline", subject="sensor-b")
    assert manager.alert(AlertType.SENSOR_ERROR, "Offline", subject="sensor-a")
    assert manager.dispatch_pending()
    assert len(channel.calls) == 3
    assert manager.get_health()["active_incidents"] == 2


def test_recovery_discards_retry_and_new_incident_has_new_budget(manager_factory):
    channel = FakeChannel([False, True])
    clock = FakeClock()
    manager = manager_factory(channel, clock=clock)
    assert manager.alert(AlertType.SENSOR_ERROR, "Old", subject="sensor")
    assert manager.dispatch_pending()
    assert manager.clear_alert_state(AlertType.SENSOR_ERROR, subject="sensor")
    clock.advance(100)
    assert not manager.dispatch_pending()
    assert manager.alert(AlertType.SENSOR_ERROR, "New", subject="sensor")
    assert manager.dispatch_pending()
    assert [occurrence.message for occurrence in channel.calls] == ["Old", "New"]
    assert manager.get_health()["cancelled"] == 1
    assert manager.get_health()["delivered"] == 1


def test_recovery_while_sending_cannot_modify_new_incident(manager_factory):
    channel = BlockingChannel(first_result=False)
    manager = manager_factory(channel)
    try:
        assert manager.alert(AlertType.SENSOR_ERROR, "Old", subject="sensor")
        assert manager.start()
        assert channel.entered.wait(1)
        assert manager.clear_alert_state(AlertType.SENSOR_ERROR, subject="sensor")
        assert manager.alert(AlertType.SENSOR_ERROR, "New", subject="sensor")
        channel.release.set()
        assert channel.second_attempt.wait(1)
        assert manager.shutdown(timeout=1)
        assert [occurrence.message for occurrence in channel.calls] == ["Old", "New"]
        health = manager.get_health()
        assert health["delivered"] == 1
        assert health["errors"]["failed_attempts"] == 0
        assert health["active_incidents"] == 1
        assert health["queue_depth"] == 0
    finally:
        channel.release.set()


def test_leak_repeat_uses_monotonic_time_and_pending_occurrences_coalesce(manager_factory):
    clock = FakeClock()
    channel = FakeChannel()
    manager = manager_factory(channel, clock=clock)
    assert manager.alert(AlertType.LEAK, "Flow while idle")
    assert manager.dispatch_pending()
    clock.advance(899)
    assert not manager.alert(AlertType.LEAK, "Still flowing")
    clock.advance(1)
    assert manager.alert(AlertType.LEAK, "Still flowing")
    assert manager.dispatch_pending()
    clock.wall -= timedelta(days=1)
    clock.advance(900, advance_wall=False)
    assert manager.alert(AlertType.LEAK, "Still flowing")
    clock.advance(900)
    assert not manager.alert(AlertType.LEAK, "Still pending")
    assert manager.dispatch_pending()
    assert len(channel.calls) == 3
    assert manager.clear_alert_state(AlertType.LEAK)
    assert manager.alert(AlertType.LEAK, "New incident")


@pytest.mark.parametrize("when,expected", [
    (datetime(2025, 6, 16, 23, 44), False),
    (datetime(2025, 6, 16, 23, 45), True),
    (datetime(2025, 6, 17, 0, 15), True),
    (datetime(2025, 6, 17, 0, 45), False),
    (datetime(2025, 6, 18, 0, 15), False),
    (datetime(2025, 12, 16, 0, 15), False),
])
def test_overnight_exclusions_keep_day_and_season_filters(manager_factory, when, expected):
    cfg = make_config()
    cfg.cfg.alerts.leak_detection_exclusions = [exclusion()]
    manager = manager_factory(FakeChannel(), config=cfg, clock=FakeClock(when))
    assert manager.is_in_exclusion_window(when) is expected
    assert manager.alert(AlertType.LEAK, "Observation") is not expected
    assert manager.get_health()["active_incidents"] == (0 if expected else 1)


@pytest.mark.parametrize("basis,offset,when", [
    ("sunrise", 15, datetime(2025, 6, 17, 6, 15, tzinfo=timezone.utc)),
    ("sunset", -30, datetime(2025, 6, 17, 19, 30, tzinfo=timezone.utc)),
])
def test_solar_exclusions_use_schedule_helpers(manager_factory, basis, offset, when):
    cfg = make_config(0)
    cfg.cfg.alerts.leak_detection_exclusions = [
        exclusion(time_based_on=basis, offset_minutes=offset, duration=30, days=["Tue"])
    ]
    manager = manager_factory(config=cfg)
    assert manager.is_in_exclusion_window(when)
    assert not manager.is_in_exclusion_window(when + timedelta(minutes=30))
    assert not manager.is_in_exclusion_window(when - timedelta(days=1))


def test_naive_exclusion_reference_accepts_timezone_aware_schedule(manager_factory):
    class AwareSchedules(FakeSchedules):
        def calculateScheduleTime(self, schedule, now):
            return super().calculateScheduleTime(schedule, now).replace(tzinfo=timezone.utc)

    cfg = make_config(0)
    cfg.cfg.alerts.leak_detection_exclusions = [exclusion()]
    manager = manager_factory(config=cfg, schedules=AwareSchedules())
    assert manager.is_in_exclusion_window(datetime(2025, 6, 17, 0, 15))


@pytest.mark.parametrize("when", [
    datetime(2025, 3, 9, 3),
    datetime(2025, 3, 10, 3, 30),
])
def test_nonexistent_dst_occurrence_is_skipped_without_exclusion_error(
        manager_factory, when):
    class DstSchedules(FakeSchedules):
        def calculateScheduleTime(self, schedule, now):
            if (schedule.fixed_start_time == "02:30"
                    and now.date() == datetime(2025, 3, 9).date()):
                return None
            return super().calculateScheduleTime(schedule, now)

    cfg = make_config()
    cfg.cfg.alerts.leak_detection_exclusions = [
        exclusion(fixed_start_time="02:30", days=[], seasons=[]),
    ]
    channel = FakeChannel()
    manager = manager_factory(
        channel, config=cfg, clock=FakeClock(when), schedules=DstSchedules(),
    )
    assert not manager.is_in_exclusion_window(when)
    assert manager.alert(AlertType.LEAK, "Observation")
    assert channel.calls == []
    assert manager.get_health()["errors"]["exclusion_errors"] == 0

    cfg.cfg.alerts.leak_detection_exclusions.append(
        exclusion(fixed_start_time="03:00", days=[], seasons=[]),
    )
    assert manager.reload_config()
    assert manager.is_in_exclusion_window(when)
    assert manager.get_health()["errors"]["exclusion_errors"] == 0


def test_exclusion_cancels_pending_leak_retry_without_implying_recovery(manager_factory):
    cfg = make_config()
    cfg.cfg.alerts.leak_detection_exclusions = [
        exclusion(fixed_start_time="10:00", duration=1, days=[], seasons=[])
    ]
    clock = FakeClock(datetime(2025, 6, 15, 9, 59, 59))
    channel = FakeChannel([False])
    manager = manager_factory(channel, config=cfg, clock=clock)
    assert manager.alert(AlertType.LEAK, "Observation")
    assert manager.dispatch_pending()
    clock.advance(2)
    assert manager.dispatch_pending()
    assert len(channel.calls) == 1
    assert manager.get_health()["queue_depth"] == 0
    assert manager.get_health()["active_incidents"] == 1
    assert not manager.alert(AlertType.LEAK, "Excluded")
    clock.advance(900)
    assert manager.alert(AlertType.LEAK, "Repeat")
    assert manager.dispatch_pending()
    assert len(channel.calls) == 2


def test_broken_exclusion_does_not_silence_leak_and_reports_failure(manager_factory, caplog):
    class BrokenSchedules(FakeSchedules):
        def shouldScheduleRun(self, schedule, check_date=None):
            raise ValueError("private-schedule-data")

    cfg = make_config()
    cfg.cfg.alerts.leak_detection_exclusions = [exclusion()]
    manager = manager_factory(FakeChannel(), config=cfg, schedules=BrokenSchedules())
    assert manager.alert(AlertType.LEAK, "Observation")
    assert manager.get_health()["errors"]["exclusion_errors"] == 1
    assert "Leak exclusion evaluation failed (ValueError)" in caplog.text
    assert "private-schedule-data" not in caplog.text


def test_overflow_is_explicit_bounded_and_rejected_incident_can_retry(
        manager_factory, caplog):
    channel = FakeChannel()
    manager = manager_factory(channel, queue_capacity=1)
    assert manager.alert(AlertType.SENSOR_ERROR, "A", subject="a")
    assert not manager.alert(AlertType.SENSOR_ERROR, "A again", subject="a")
    before = time.monotonic()
    assert not manager.alert(AlertType.SENSOR_ERROR, "B", subject="b")
    assert time.monotonic() - before < 0.25
    health = manager.get_health()
    assert health["queue_depth"] == 1
    assert health["active_incidents"] == 1
    assert health["errors"]["overflow"] == 1
    assert health["coalesced"] == 1
    assert not health["healthy"]
    assert "outbox overflow" in caplog.text
    assert manager.dispatch_pending()
    assert manager.alert(AlertType.SENSOR_ERROR, "B", subject="b")
    assert manager.dispatch_pending()
    assert len(channel.calls) == 2
    assert manager.get_health()["healthy"]


def test_unrecovered_incident_bookkeeping_is_bounded(manager_factory, monkeypatch, caplog):
    monkeypatch.setattr(AlertManager, "MIN_INCIDENT_CAPACITY", 2)
    manager = manager_factory(queue_capacity=1)
    assert manager.alert(AlertType.SENSOR_ERROR, "A", subject="a")
    assert manager.alert(AlertType.SENSOR_ERROR, "B", subject="b")
    assert not manager.alert(AlertType.SENSOR_ERROR, "C", subject="c")
    assert not manager.alert(AlertType.SENSOR_ERROR, "A again", subject="a")
    health = manager.get_health()
    assert health["active_incidents"] == health["incident_capacity"] == 2
    assert health["errors"]["incident_overflow"] == 1
    assert "incident capacity exhausted" in caplog.text
    assert manager.clear_alert_state(AlertType.SENSOR_ERROR, subject="a")
    assert manager.alert(AlertType.SENSOR_ERROR, "C", subject="c")
    assert manager.get_health()["active_incidents"] == 2


def test_start_is_idempotent_and_shutdown_drains_on_worker(manager_factory):
    channel = FakeChannel()
    manager = manager_factory(channel)
    assert manager.alert(AlertType.SYSTEM_EXIT, "Close was attempted before this event")
    assert channel.calls == []
    assert manager.start()
    worker = manager._worker
    assert manager.start()
    assert manager._worker is worker
    assert manager.shutdown(timeout=1)
    assert len(channel.calls) == 1
    assert channel.threads == [worker]
    assert worker is not threading.current_thread()
    assert not worker.is_alive()
    health = manager.get_health()
    assert health["status"] == "stopped"
    assert not health["accepting"]
    assert health["queue_depth"] == 0


def test_blocked_send_does_not_block_alert_or_bounded_shutdown(
        manager_factory, caplog):
    channel = BlockingChannel()
    manager = manager_factory(channel, queue_capacity=1)
    try:
        assert manager.alert(AlertType.SYSTEM_EXIT, "Close was attempted")
        assert manager.start()
        worker = manager._worker
        assert channel.entered.wait(1)
        before = time.monotonic()
        assert not manager.alert(AlertType.SYSTEM_EXIT, "Duplicate")
        assert not manager.alert(AlertType.SAFETY_INTERVENTION, "Safety action attempted")
        assert not manager.dispatch_pending()
        assert time.monotonic() - before < 0.25
        before = time.monotonic()
        assert not manager.shutdown(timeout=0.05)
        assert time.monotonic() - before < 0.3
        assert not manager.start()
        assert manager._worker is worker
        health = manager.get_health()
        assert health["worker_alive"]
        assert health["status"] == "stopping"
        assert health["errors"]["shutdown_timeouts"] == 1
        assert health["errors"]["overflow"] == 1
        assert not health["healthy"]
        assert "shutdown timed out" in caplog.text
    finally:
        channel.release.set()
        assert manager.shutdown(timeout=1)
    assert not worker.is_alive()
    assert len(channel.calls) == 1
    assert manager.get_health()["status"] == "stopped"


def test_shutdown_without_start_retains_work_and_restart_preserves_incidents(manager_factory):
    channel = FakeChannel()
    manager = manager_factory(channel)
    assert manager.alert(AlertType.SYSTEM_EXIT, "Occurrence")
    assert manager.shutdown(timeout=0)
    assert channel.calls == []
    assert not manager.dispatch_pending()
    assert not manager.alert(AlertType.SAFETY_INTERVENTION, "Stopped")
    assert manager.get_health()["errors"]["rejected_after_shutdown"] == 1
    assert manager.get_health()["queue_depth"] == 1
    assert manager.start()
    assert manager.shutdown(timeout=1)
    assert len(channel.calls) == 1
    assert manager.start()
    assert not manager.alert(AlertType.SYSTEM_EXIT, "Same incident")
    assert manager.clear_alert_state(AlertType.SYSTEM_EXIT)
    assert manager.alert(AlertType.SYSTEM_EXIT, "New incident")
    assert manager.shutdown(timeout=1)
    assert len(channel.calls) == 2


def test_shutdown_timeout_is_unhealthy_without_overflow(manager_factory):
    channel = BlockingChannel()
    manager = manager_factory(channel)
    try:
        assert manager.alert(AlertType.SYSTEM_EXIT, "Close was attempted")
        assert manager.start()
        assert channel.entered.wait(1)
        assert not manager.shutdown(timeout=0)
        health = manager.get_health()
        assert health["errors"]["overflow"] == 0
        assert health["errors"]["shutdown_timeouts"] == 1
        assert not health["healthy"]
    finally:
        channel.release.set()
        assert manager.shutdown(timeout=1)
    assert manager.get_health()["healthy"]


def test_shutdown_finishing_cannot_stop_a_concurrent_explicit_restart(
        manager_factory, monkeypatch):
    manager = manager_factory(FakeChannel())
    assert manager.start()
    old_worker = manager._worker
    original_join = old_worker.join
    restarted = []

    def join_then_restart(timeout=None):
        original_join(timeout)
        assert not old_worker.is_alive()
        assert manager.start()
        restarted.append(manager._worker)

    monkeypatch.setattr(old_worker, "join", join_then_restart)
    assert manager.shutdown(timeout=1)
    assert manager._worker is restarted[0]
    assert manager.get_health()["status"] == "running"
    assert manager.get_health()["accepting"]
    assert manager.shutdown(timeout=1)
    assert not restarted[0].is_alive()


def test_explicit_restart_keeps_retry_budget_and_successes(manager_factory):
    clock = FakeClock()
    good = FakeChannel()
    flaky = FakeChannel([False, True])
    manager = manager_factory(good, flaky, clock=clock)
    assert manager.alert(AlertType.SENSOR_ERROR, "Offline")
    assert manager.dispatch_pending()
    assert manager.dispatch_pending()
    assert manager.shutdown(timeout=0)
    clock.advance(2)
    assert manager.start()
    assert manager.shutdown(timeout=1)
    assert len(good.calls) == 1
    assert len(flaky.calls) == 2
    assert manager.get_health()["errors"]["failed_attempts"] == 1
    assert manager.get_health()["delivered"] == 1


def test_worker_exception_is_visible_and_requires_explicit_restart(
        manager_factory, monkeypatch, caplog):
    channel = FakeChannel()
    manager = manager_factory(channel)
    assert manager.alert(AlertType.SENSOR_ERROR, "Offline")
    attempts = []

    def broken_dispatch():
        attempts.append(True)
        raise RuntimeError("private-worker-context")

    with monkeypatch.context() as patch:
        patch.setattr(manager, "dispatch_pending", broken_dispatch)
        assert manager.start()
        manager._worker.join(1)
        assert not manager._worker.is_alive()
        health = manager.get_health()
        assert health["status"] == "failed"
        assert health["errors"]["worker_exceptions"] == 1
        assert health["queue_depth"] == 1
        assert not health["healthy"]
        assert not health["accepting"]
        assert attempts == [True]
        assert "worker stopped unexpectedly (RuntimeError)" in caplog.text
        assert "private-worker-context" not in caplog.text
    assert manager.start()
    assert manager.shutdown(timeout=1)
    assert len(channel.calls) == 1
    assert manager.get_health()["healthy"]


@pytest.mark.parametrize("failure_stage", ["constructor", "start"])
def test_worker_start_failure_is_visible_and_can_be_explicitly_retried(
        manager_factory, monkeypatch, caplog, failure_stage):
    channel = FakeChannel()
    manager = manager_factory(channel)
    assert manager.alert(AlertType.SENSOR_ERROR, "Offline")

    def failed(*args, **kwargs):
        raise RuntimeError("private-start-context")

    with monkeypatch.context() as patch:
        if failure_stage == "constructor":
            patch.setattr(threading, "Thread", failed)
        else:
            patch.setattr(threading.Thread, "start", failed)
        assert not manager.start()
        health = manager.get_health()
        assert health["status"] == "failed"
        assert health["errors"]["worker_exceptions"] == 1
        assert not health["worker_alive"]
        assert not health["healthy"]
    assert "private-start-context" not in caplog.text
    assert manager.start()
    assert manager.shutdown(timeout=1)
    assert len(channel.calls) == 1


def test_reload_preserves_unchanged_channels_and_partial_delivery(manager_factory):
    clock = FakeClock()
    good = FakeChannel()
    flaky = FakeChannel([False, True])
    channels = [good, flaky]
    constructed = []

    def factory(logger, cfg):
        constructed.append(cfg.name)
        return channels[int(cfg.name)]

    manager = manager_factory(config=make_config(2), clock=clock, channel_factory=factory)
    assert manager.alert(AlertType.SENSOR_ERROR, "Offline")
    assert manager.dispatch_pending()
    assert manager.dispatch_pending()
    manager.config.cfg.alerts.leak_repeat_minutes = 5
    assert manager.reload_config()
    assert constructed == ["0", "1"]
    assert manager.leak_repeat_minutes == 5
    clock.advance(2)
    assert manager.dispatch_pending()
    assert len(good.calls) == 1
    assert len(flaky.calls) == 2
    assert manager.get_health()["delivered"] == 1


def test_reload_replaces_pending_channel_without_resending_successes(manager_factory):
    good, old, new = FakeChannel(), FakeChannel(default=False), FakeChannel()
    channels = {"0": good, "1": old, "new": new}
    manager = manager_factory(
        config=make_config(2), channel_factory=lambda logger, cfg: channels[cfg.name],
    )
    assert manager.alert(AlertType.SENSOR_ERROR, "Offline")
    assert manager.dispatch_pending()
    assert manager.dispatch_pending()
    manager.config.cfg.alerts.channels[1].name = "new"
    assert manager.reload_config()
    assert manager.dispatch_pending()
    assert len(good.calls) == len(old.calls) == len(new.calls) == 1
    assert manager.get_health()["queue_depth"] == 0


def test_channel_initialization_failure_is_visible_and_reload_can_deliver(
        manager_factory, caplog):
    good, repaired = FakeChannel(), FakeChannel()
    broken = [True]
    constructed = []

    def factory(logger, cfg):
        constructed.append(cfg.name)
        if cfg.name == "0":
            return good
        if broken[0]:
            raise ValueError("do-not-log-channel-api-key")
        return repaired

    manager = manager_factory(config=make_config(2), channel_factory=factory)
    assert not manager.get_health()["healthy"]
    assert manager.get_health()["configuration_errors"] == 1
    assert "Failed to initialize alert channel (ValueError)" in caplog.text
    assert "do-not-log-channel-api-key" not in caplog.text
    assert manager.alert(AlertType.SENSOR_ERROR, "Offline")
    assert manager.dispatch_pending()
    assert not manager.dispatch_pending()
    assert manager.get_health()["queue_depth"] == 1
    broken[0] = False
    assert manager.reload_config()
    assert manager.dispatch_pending()
    assert constructed == ["0", "1", "1"]
    assert len(good.calls) == len(repaired.calls) == 1
    assert manager.get_health()["configuration_errors"] == 0
    assert manager.get_health()["healthy"]


def test_all_unavailable_channels_keep_a_bounded_outbox_until_reload(manager_factory):
    channel = FakeChannel()
    broken = [True]

    def factory(logger, cfg):
        if broken[0]:
            raise ValueError("Unavailable")
        return channel

    manager = manager_factory(
        config=make_config(), channel_factory=factory, queue_capacity=1,
    )
    assert manager.alert(AlertType.SENSOR_ERROR, "A", subject="a")
    assert not manager.dispatch_pending()
    assert not manager.alert(AlertType.SENSOR_ERROR, "B", subject="b")
    health = manager.get_health()
    assert health["channel_count"] == 0
    assert health["delivery_status"] == "unavailable"
    assert health["queue_depth"] == 1
    assert health["unavailable_channels"] == 1
    broken[0] = False
    assert manager.reload_config()
    assert manager.dispatch_pending()
    assert len(channel.calls) == 1
    assert manager.alert(AlertType.SENSOR_ERROR, "B", subject="b")
    assert manager.dispatch_pending()
    assert manager.get_health()["healthy"]


def test_disable_cancels_retry_and_reenable_permits_new_incident(manager_factory):
    channel = FakeChannel([False, True])
    manager = manager_factory(channel)
    assert all(manager.enabled[kind] for kind in AlertType)
    assert manager.alert(AlertType.MONITORING_UNAVAILABLE, "Unavailable", subject="flow")
    assert manager.dispatch_pending()
    manager.config.cfg.alerts.enabled.monitoring_unavailable = False
    assert manager.reload_config()
    assert manager.get_health()["active_incidents"] == 0
    assert manager.get_health()["queue_depth"] == 0
    assert not manager.alert(AlertType.MONITORING_UNAVAILABLE, "Unavailable", subject="flow")
    manager.config.cfg.alerts.enabled.monitoring_unavailable = True
    assert manager.reload_config()
    assert manager.alert(AlertType.MONITORING_UNAVAILABLE, "Unavailable", subject="flow")
    assert manager.dispatch_pending()
    assert len(channel.calls) == 2


def test_health_is_pure_detached_and_contains_no_alert_identity(manager_factory, monkeypatch):
    clock = FakeClock()
    manager = manager_factory(FakeChannel(), clock=clock)
    assert manager.alert(
        AlertType.SENSOR_ERROR, "private-message", data={"private-data": True},
        subject="private-resource",
    )
    before = manager.get_health()
    monkeypatch.setattr(clock, "now", lambda: pytest.fail("Health must not read a clock"))
    monkeypatch.setattr(clock, "monotonic", lambda: pytest.fail("Health must not read a clock"))
    assert manager.get_health() == before
    before["errors"]["overflow"] = 123
    health = manager.get_health()
    assert health["errors"]["overflow"] == 0
    assert "private" not in json.dumps(health)


@pytest.mark.parametrize("capacity", [0, -1, True, 1.5])
def test_queue_capacity_validation(capacity):
    with pytest.raises(ValueError, match="positive integer"):
        AlertManager(LOGGER, make_config(0), FakeSchedules(), queue_capacity=capacity)
