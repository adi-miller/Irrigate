import builtins
import csv
import hashlib
import io
import json
import logging
import threading
import time
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import pytz

import valve_metrics
from valve_metrics import CSV_FIELDS, MetricsStore, load_baselines


LOGGER = logging.getLogger("test.metrics_store")
DAY = date(2026, 9, 18)
NOON = datetime(2026, 9, 18, 12, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self, wall=NOON):
        self.wall = wall
        self.ticks = 100.0

    def now(self):
        return self.wall

    def monotonic(self):
        return self.ticks


def contribution(seconds, liters, complete=True):
    return {"seconds": seconds, "liters": liters, "complete": complete}


def store_at(directory, clock=None):
    store = MetricsStore(directory, LOGGER, clock or FakeClock())
    assert store.load(), store.get_health()
    return store


def assert_totals(record, seconds, liters, complete=True):
    assert record["seconds"] == pytest.approx(seconds)
    assert record["liters"] == pytest.approx(liters)
    assert record["complete"] is complete


def csv_rows(directory):
    with (directory / "valve_metrics.csv").open(encoding="utf-8", newline="") as source:
        reader = csv.DictReader(source)
        assert reader.fieldnames == CSV_FIELDS
        return list(reader)


def write_history(directory, rows):
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(CSV_FIELDS)
    writer.writerows(rows)
    raw = output.getvalue().encode("utf-8")
    (directory / "valve_metrics.csv").write_bytes(raw)
    return raw


def forbidden_io(*args, **kwargs):
    raise AssertionError("Controller-facing accounting must not perform filesystem I/O")


def test_constructor_is_disk_free_even_with_corrupt_state(tmp_path, monkeypatch):
    state = tmp_path / "runtime_state.json"
    state.write_bytes(b"unreadable accounting")
    with monkeypatch.context() as patch:
        patch.setattr(builtins, "open", forbidden_io)
        patch.setattr(valve_metrics.os, "open", forbidden_io)
        patch.setattr(valve_metrics.os, "stat", forbidden_io)
        patch.setattr(valve_metrics.os, "makedirs", forbidden_io)
        store = MetricsStore(tmp_path, LOGGER, FakeClock())
        assert not store.get_health()["loaded"]
    assert not store.load()
    assert store.get_health()["persistence_error"]
    assert not store.get_health()["recovery_complete"]
    assert state.read_bytes() == b"unreadable accounting"


def test_controller_methods_are_memory_only_and_return_detached_values(tmp_path, monkeypatch):
    store = store_at(tmp_path)
    with monkeypatch.context() as patch:
        for name in ("open", "stat", "makedirs", "replace", "fsync"):
            patch.setattr(valve_metrics.os, name, forbidden_io)
        patch.setattr(builtins, "open", forbidden_io)
        store.add_interval(NOON, 10, {"north": contribution(10, 1.125)})
        store.finalize_before(DAY + timedelta(days=1))
        store.request_flush()
        totals = store.daily_totals(DAY)
        totals["north"]["liters"] = 1000
        quality = store.get_quality()
        quality[DAY.isoformat()]["north"] = False
        assert_totals(store.daily_totals(DAY)["north"], 10, 1.125)
        assert store.get_quality()[DAY.isoformat()]["north"]
        assert store.get_health()["persistence_error"] is None
    assert list(tmp_path.iterdir()) == []


def test_recording_requires_explicit_load_after_startup_reconciliation(tmp_path):
    store = MetricsStore(tmp_path, LOGGER, FakeClock())
    with pytest.raises(RuntimeError, match="startup close-all"):
        store.add_interval(NOON, 1, {"north": contribution(1, 0.1)})
    assert not store.flush()
    assert list(tmp_path.iterdir()) == []


def test_running_interval_splits_at_configured_local_midnight(tmp_path):
    local = pytz.timezone("Asia/Jerusalem")
    start = local.localize(datetime(2026, 9, 18, 23, 59, 50))
    store = store_at(tmp_path, FakeClock(start))
    # The supplied wall instant can be in UTC; dates still use the configured zone.
    store.add_interval(
        start.astimezone(timezone.utc), 20,
        {"north": contribution(20, 1.25), "south": contribution(10, 0.5, False)},
        unattributed_liters=0.75, unavailable_seconds=6,
    )
    for day in (DAY, DAY + timedelta(days=1)):
        assert_totals(store.daily_totals(day)["north"], 10, 0.625)
        assert_totals(store.daily_totals(day)["south"], 5, 0.25, False)
    store.finalize_before(DAY + timedelta(days=1))
    assert store.flush()
    rows = csv_rows(tmp_path)
    assert {(row["valve_name"], row["date"]) for row in rows} == {
        ("north", "2026-09-18"), ("south", "2026-09-18"),
    }
    restored = store_at(tmp_path, FakeClock(start))
    assert_totals(restored.daily_totals("2026-09-19")["north"], 10, 0.625)
    assert restored.get_health()["unattributed_liters"] == pytest.approx(0.75)
    assert restored.get_health()["unavailable_seconds"] == pytest.approx(6)
    assert restored.get_health()["incomplete_rows"] == 2


@pytest.mark.parametrize(
    "start,elapsed,expected",
    [
        (datetime(2026, 3, 8), 24 * 3600, {"2026-03-08": 23 * 3600, "2026-03-09": 3600}),
        (datetime(2026, 11, 1), 26 * 3600, {"2026-11-01": 25 * 3600, "2026-11-02": 3600}),
        (datetime(2026, 9, 18), 49 * 3600, {
            "2026-09-18": 24 * 3600, "2026-09-19": 24 * 3600, "2026-09-20": 3600,
        }),
    ],
)
def test_elapsed_time_not_naive_day_seconds_controls_splits(tmp_path, start, elapsed, expected):
    zone = pytz.timezone("America/New_York")
    start = zone.localize(start)
    store = store_at(tmp_path, FakeClock(start))
    store.add_interval(start, elapsed, {"north": contribution(elapsed, elapsed / 60)})
    for day, seconds in expected.items():
        assert_totals(store.daily_totals(day)["north"], seconds, seconds / 60)
    assert sum(store.daily_totals(day)["north"]["seconds"] for day in expected) == elapsed


def test_pauses_and_zero_intervals_do_not_create_operated_rows(tmp_path):
    store = store_at(tmp_path)
    store.add_interval(NOON, 120, {})
    store.add_interval(NOON, 0, {"north": contribution(0, 0)})
    store.add_interval(NOON, 120, {"north": contribution(0, 0, False)})
    assert store.daily_totals(DAY) == {}
    store.finalize_before(DAY + timedelta(days=1))
    assert store.flush()
    assert not (tmp_path / "valve_metrics.csv").exists()


def test_multiple_open_segments_and_manual_work_accumulate_fractions(tmp_path):
    store = store_at(tmp_path)
    store.add_interval(NOON, 5.25, {"north": contribution(5.25, 0.75)})
    store.add_interval(NOON + timedelta(seconds=5.25), 15, {})
    store.add_interval(NOON + timedelta(seconds=20.25), 7.5, {"north": contribution(7.5, 1.0625)})
    assert_totals(store.daily_totals(DAY)["north"], 12.75, 1.8125)
    assert store.flush()
    restored = store_at(tmp_path)
    assert_totals(restored.daily_totals(DAY)["north"], 12.75, 1.8125)
    restored.finalize_before("2026-09-19")
    assert restored.flush()
    row = csv_rows(tmp_path)[0]
    assert row == {
        "valve_name": "north", "date": "2026-09-18", "total_seconds": "12.75",
        "total_liters": "1.81", "avg_liters_per_minute": "8.53",
    }


def test_restore_never_infers_operation_or_volume_during_downtime(tmp_path):
    clock = FakeClock()
    store = store_at(tmp_path, clock)
    store.add_interval(
        NOON, 40.75, {"north": contribution(40.75, 3.123456, False)},
        unattributed_liters=2.345, unavailable_seconds=3.5,
    )
    assert store.flush()
    restored = store_at(tmp_path, clock)
    assert_totals(restored.daily_totals(DAY)["north"], 40.75, 3.123456, False)
    clock.wall += timedelta(days=10)
    clock.ticks += 10 * 86400
    much_later = store_at(tmp_path, clock)
    assert_totals(much_later.daily_totals(DAY)["north"], 40.75, 3.123456, False)
    assert much_later.daily_totals(clock.now().date()) == {}
    assert much_later.get_health()["unattributed_liters"] == pytest.approx(2.345)
    assert much_later.get_health()["unavailable_seconds"] == pytest.approx(3.5)
    assert not (tmp_path / "valve_metrics.csv").exists()
    much_later.add_interval(clock.now(), 1, {"north": contribution(1, 0.25)})
    assert_totals(much_later.daily_totals(clock.now().date())["north"], 1, 0.25)


def test_controller_attribution_is_never_redistributed_between_valves(tmp_path):
    store = store_at(tmp_path)
    store.add_interval(
        NOON, 10, {"north": contribution(10, 2), "south": contribution(10, 0, False)},
        unattributed_liters=5, unavailable_seconds=10,
    )
    assert_totals(store.daily_totals(DAY)["north"], 10, 2)
    assert_totals(store.daily_totals(DAY)["south"], 10, 0, False)
    assert store.get_health()["unattributed_liters"] == 5
    assert store.get_health()["unavailable_seconds"] == 10


def test_repeated_midnight_ticks_flushes_and_restarts_export_once(tmp_path):
    start = NOON.replace(hour=23, minute=59, second=50)
    store = store_at(tmp_path)
    store.add_interval(start, 20, {"north": contribution(20, 2)})
    for _ in range(3):
        store.finalize_before("2026-09-19")
        store.request_flush()
        assert store.flush()
    assert len(csv_rows(tmp_path)) == 1
    store = store_at(tmp_path)
    for _ in range(3):
        store.finalize_before("2026-09-20")
        assert store.flush()
    assert [(row["date"], row["total_seconds"]) for row in csv_rows(tmp_path)] == [
        ("2026-09-18", "10.0"), ("2026-09-19", "10.0"),
    ]
    store = store_at(tmp_path)
    assert store.flush()
    assert len(csv_rows(tmp_path)) == 2


@pytest.mark.parametrize("after_write", [False, True], ids=["before-csv", "after-csv"])
@pytest.mark.parametrize("restart", [False, True], ids=["retry", "restart"])
def test_csv_failure_and_restart_retry_do_not_duplicate_or_lose_quality(
    tmp_path, monkeypatch, after_write, restart
):
    original = write_history(tmp_path, [["old", "2026-09-01", 60, 1, 1]])
    store = store_at(tmp_path)
    store.add_interval(NOON, 12.5, {"north": contribution(12.5, 0.7654321, False)})
    store.finalize_before("2026-09-19")
    real_write = store._atomic_write

    def fail_csv(path, payload):
        if path == store.metrics_file:
            if after_write:
                real_write(path, payload)
            raise OSError("simulated CSV failure")
        real_write(path, payload)

    with monkeypatch.context() as patch:
        patch.setattr(store, "_atomic_write", fail_csv)
        assert not store.flush()
        assert "simulated CSV failure" in store.get_health()["persistence_error"]
    actual = (tmp_path / "valve_metrics.csv").read_bytes()
    assert actual.startswith(original)
    assert (actual != original) is after_write
    restored = store_at(tmp_path) if restart else store
    assert_totals(restored.daily_totals(DAY)["north"], 12.5, 0.7654321, False)
    assert not restored.get_quality()["2026-09-18"]["north"]
    assert restored.flush()
    assert restored.flush()
    assert len(csv_rows(tmp_path)) == 2
    assert sum(row["valve_name"] == "north" for row in csv_rows(tmp_path)) == 1


@pytest.mark.parametrize(
    "failure,expected_seconds",
    [("write", 2), ("fsync", 2), ("replace", 2), ("directory_fsync", 5), ("backup_replace", 5)],
)
def test_checkpoint_failure_preserves_an_accepted_recoverable_file(
    tmp_path, monkeypatch, failure, expected_seconds
):
    original_csv = write_history(tmp_path, [["old", "2026-09-01", 60, 1, 1]])
    store = store_at(tmp_path)
    store.add_interval(NOON, 2, {"north": contribution(2, 0.2)})
    assert store.flush()
    accepted = (tmp_path / "runtime_state.json").read_bytes()
    store.add_interval(NOON + timedelta(seconds=2), 3, {"north": contribution(3, 0.3)})
    store.finalize_before("2026-09-19")
    real_open = builtins.open
    real_replace = valve_metrics.os.replace

    class FailedWrite:
        def __init__(self, file):
            self.file = file

        def __enter__(self):
            self.file.__enter__()
            return self

        def __exit__(self, *args):
            return self.file.__exit__(*args)

        def write(self, payload):
            self.file.write(payload[:10])
            raise OSError("simulated partial state write")

    def fail_open(path, mode="r", *args, **kwargs):
        file = real_open(path, mode, *args, **kwargs)
        if str(path).startswith(store.state_file + ".pending-") and mode == "xb":
            return FailedWrite(file)
        return file

    def fail_replace(source, destination):
        target = store.backup_file if failure == "backup_replace" else store.state_file
        if destination == target:
            raise OSError("simulated replace failure")
        return real_replace(source, destination)

    def fail_sync(*args):
        raise OSError("simulated fsync failure")

    with monkeypatch.context() as patch:
        if failure == "write":
            patch.setattr(builtins, "open", fail_open)
        elif failure == "fsync":
            patch.setattr(valve_metrics.os, "fsync", fail_sync)
        elif failure == "directory_fsync":
            patch.setattr(store, "_fsync_directory", fail_sync)
        else:
            patch.setattr(valve_metrics.os, "replace", fail_replace)
        assert not store.flush()
        assert store.get_health()["persistence_error"]
    if expected_seconds == 2:
        assert (tmp_path / "runtime_state.json").read_bytes() == accepted
    assert (tmp_path / "valve_metrics.csv").read_bytes() == original_csv
    assert not list(tmp_path.glob("*.pending-*"))
    restored = store_at(tmp_path)
    assert_totals(restored.daily_totals(DAY)["north"], expected_seconds, expected_seconds / 10)
    assert store.flush()
    assert store.get_health()["persistence_error"] is None
    restored = store_at(tmp_path)
    assert_totals(restored.daily_totals(DAY)["north"], 5, 0.5)
    assert restored.flush()
    assert len(csv_rows(tmp_path)) == 2


def test_state_and_quality_are_fsynced_before_csv_replacement(tmp_path, monkeypatch):
    store = store_at(tmp_path)
    store.add_interval(NOON, 5, {"north": contribution(5, 0.5, False)})
    store.finalize_before("2026-09-19")
    events = []
    real_fsync = valve_metrics.os.fsync
    real_replace = valve_metrics.os.replace

    def fsync(descriptor):
        events.append(("fsync", descriptor))
        return real_fsync(descriptor)

    def replace(source, destination):
        events.append(("replace", destination))
        return real_replace(source, destination)

    monkeypatch.setattr(valve_metrics.os, "fsync", fsync)
    monkeypatch.setattr(valve_metrics.os, "replace", replace)
    assert store.flush()
    replaced = [value for kind, value in events if kind == "replace"]
    assert replaced == [store.state_file, store.backup_file, store.metrics_file]
    previous_replace = -1
    for index, (kind, _) in enumerate(events):
        if kind == "replace":
            assert any(event[0] == "fsync" for event in events[previous_replace + 1:index])
            previous_replace = index


def test_last_good_backup_recovers_but_preserves_corruption_and_warning(tmp_path):
    store = store_at(tmp_path)
    store.add_interval(NOON, 6.25, {"north": contribution(6.25, 0.333333, False)})
    store.finalize_before("2026-09-19")
    assert store.flush()
    original_csv = (tmp_path / "valve_metrics.csv").read_bytes()
    corrupt = b'{"version": 1, "state":'
    (tmp_path / "runtime_state.json").write_bytes(corrupt)
    restored = store_at(tmp_path)
    assert_totals(restored.daily_totals(DAY)["north"], 6.25, 0.333333, False)
    health = restored.get_health()
    assert health["persistence_error"]
    assert not health["recovery_complete"]
    preserved = list(tmp_path.glob("runtime_state.json.corrupt-*"))
    assert len(preserved) == 1
    assert preserved[0].read_bytes() == corrupt
    assert restored.flush()
    again = store_at(tmp_path)
    assert again.get_health()["persistence_error"]
    assert not again.get_quality()["2026-09-18"]["north"]
    assert (tmp_path / "valve_metrics.csv").read_bytes() == original_csv


def test_empty_successful_flush_cannot_clear_recovered_corruption(tmp_path):
    store = store_at(tmp_path)
    assert store.flush()
    corrupt = b"unknown accounting must not become healthy empty state"
    (tmp_path / "runtime_state.json").write_bytes(corrupt)
    restored = store_at(tmp_path)
    warning = restored.get_health()["persistence_error"]
    assert warning
    assert restored.daily_totals(DAY) == {}
    for _ in range(2):
        assert restored.flush()
        assert restored.get_health()["persistence_error"] == warning
        assert not restored.get_health()["recovery_complete"]
    restarted = store_at(tmp_path)
    assert restarted.get_health()["persistence_error"] == warning
    assert not restarted.get_health()["recovery_complete"]
    preserved = list(tmp_path.glob("runtime_state.json.corrupt-*"))
    assert len(preserved) == 1
    assert preserved[0].read_bytes() == corrupt
    assert not (tmp_path / "valve_metrics.csv").exists()


@pytest.mark.parametrize("damage", ["truncated", "checksum", "schema", "version", "duplicate-json-key"])
def test_unrecoverable_state_never_falls_back_to_healthy_zero_or_overwrites_history(tmp_path, damage):
    original_csv = write_history(tmp_path, [["old", "2026-09-01", 60, 1, 1]])
    store = store_at(tmp_path)
    store.add_interval(NOON, 5, {"north": contribution(5, 0.5)})
    assert store.flush()
    raw = (tmp_path / "runtime_state.json").read_bytes()
    envelope = json.loads(raw)
    if damage == "truncated":
        damaged = raw[:20]
    elif damage == "duplicate-json-key":
        damaged = raw.replace(b'"version":1', b'"version":1,"version":1')
    else:
        if damage == "checksum":
            envelope["state"]["days"]["2026-09-18"]["valves"]["north"]["liters"] = 999
        elif damage == "version":
            envelope["version"] = 2
        else:
            del envelope["state"]["days"]
            envelope["sha256"] = hashlib.sha256(
                valve_metrics._canonical_json(envelope["state"])
            ).hexdigest()
        damaged = json.dumps(envelope).encode("utf-8")
    for filename in ("runtime_state.json", "runtime_state.json.bak"):
        (tmp_path / filename).write_bytes(damaged)
    restored = MetricsStore(tmp_path, LOGGER, FakeClock())
    assert not restored.load()
    assert restored.get_health()["persistence_error"]
    assert restored.get_health()["write_blocked"]
    assert not restored.get_health()["recovery_complete"]
    warning = restored.get_health()["persistence_error"]
    for _ in range(2):
        assert not restored.flush()
        assert restored.get_health()["persistence_error"] == warning
    restored.add_interval(NOON, 3, {"north": contribution(3, 0.3)})
    assert_totals(restored.daily_totals(DAY)["north"], 3, 0.3)
    restored.finalize_before("2026-09-19")
    assert not restored.flush()
    for filename in ("runtime_state.json", "runtime_state.json.bak"):
        assert (tmp_path / filename).read_bytes() == damaged
    assert (tmp_path / "valve_metrics.csv").read_bytes() == original_csv
    valve = SimpleNamespace(
        baseline_lpm=999, baseline_trend=50, baseline_std_dev=10, baseline_sample_count=99,
    )
    restored.load_baselines({"old": valve})
    assert valve.baseline_lpm is None
    assert valve.baseline_trend is None
    assert valve.baseline_std_dev is None
    assert valve.baseline_sample_count == 0
    assert restored.get_health()["persistence_error"] == warning


@pytest.mark.parametrize("damage", ["bad-header", "partial-row", "changed-history", "missing-history"])
def test_corrupt_or_changed_csv_is_preserved_and_accounting_recovery_is_explicit(tmp_path, damage):
    write_history(tmp_path, [["old", "2026-09-01", 60, 1, 1]])
    store = store_at(tmp_path)
    store.add_interval(NOON, 5, {"north": contribution(5, 0.5)})
    assert store.flush()
    path = tmp_path / "valve_metrics.csv"
    raw = path.read_bytes()
    damaged = {
        "bad-header": b"wrong,header\n",
        "partial-row": raw + b'"unclosed',
        "changed-history": raw.replace(b"60,1,1", b"60,2,2"),
        "missing-history": None,
    }[damage]
    if damaged is None:
        path.unlink()
    else:
        path.write_bytes(damaged)
    restored = MetricsStore(tmp_path, LOGGER, FakeClock())
    assert not restored.load()
    assert_totals(restored.daily_totals(DAY)["north"], 5, 0.5)
    assert restored.get_health()["persistence_error"]
    assert not restored.flush()
    assert (path.read_bytes() if path.exists() else None) == damaged


def test_old_csv_bytes_values_and_duplicates_remain_untouched(tmp_path):
    original = (
        b"valve_name,date,total_seconds,total_liters,avg_liters_per_minute\n"
        b"old,2026-09-01,6.000,0.60,6.00\n"
        b"old,2026-09-01,6.000,0.60,6.00"
    )
    path = tmp_path / "valve_metrics.csv"
    path.write_bytes(original)
    store = store_at(tmp_path)
    assert store.get_health()["historical_duplicate_rows"] == 1
    store.add_interval(NOON, 10, {"north": contribution(10, 1)})
    store.finalize_before("2026-09-19")
    assert store.flush()
    assert path.read_bytes().startswith(original)
    assert path.read_bytes().count(b"old,2026-09-01,6.000,0.60,6.00") == 2
    assert len(csv_rows(tmp_path)) == 3
    restored = store_at(tmp_path)
    assert restored.flush()
    assert len(csv_rows(tmp_path)) == 3


@pytest.mark.parametrize("history_count", [9, 10])
def test_partial_new_rows_are_excluded_from_baselines_after_restart(tmp_path, history_count):
    history = [
        ["north", "2026-09-{:02d}".format(day), 60, 2, 2]
        for day in range(1, history_count + 1)
    ]
    write_history(tmp_path, history)
    store = store_at(tmp_path)
    store.add_interval(
        NOON - timedelta(days=1), 60, {"north": contribution(60, 100, False)},
        unavailable_seconds=30,
    )
    store.finalize_before(DAY)
    assert store.flush()
    restored = store_at(tmp_path)
    valve = SimpleNamespace()
    restored.load_baselines({"north": valve})
    assert valve.baseline_sample_count == history_count
    assert valve.baseline_lpm == (2 if history_count == 10 else None)
    assert not restored.get_quality()["2026-09-17"]["north"]
    assert len(csv_rows(tmp_path)) == history_count + 1
    no_quality = SimpleNamespace()
    load_baselines({"north": no_quality}, LOGGER, restored.metrics_file, now=DAY)
    assert no_quality.baseline_sample_count == history_count + 1
    assert no_quality.baseline_lpm > 2
    explicit = SimpleNamespace()
    load_baselines(
        {"north": explicit}, LOGGER, metrics_file=restored.metrics_file,
        quality=restored.get_quality(), now=DAY.isoformat(),
    )
    assert explicit.baseline_lpm == valve.baseline_lpm


def test_baseline_wrapper_uses_store_path_configured_local_date_and_durable_quality(tmp_path, monkeypatch):
    local = pytz.timezone("Pacific/Kiritimati")
    now = local.localize(datetime(2037, 1, 5, 0, 30))
    today = now.date()
    assert now.astimezone(timezone.utc).date() < today
    history = [
        ["north", (today - timedelta(days=days)).isoformat(), 60, 2, 2]
        for days in range(7, 16)
    ]
    history += [
        ["north", today.isoformat(), 60, 2, 2],
        ["north", (today + timedelta(days=1)).isoformat(), 60, 1000, 1000],
    ]
    write_history(tmp_path, history)
    store = store_at(tmp_path, FakeClock(now))
    store.add_interval(now - timedelta(days=2), 60, {"north": contribution(60, 100, False)})
    store.finalize_before(today)
    assert store.flush()
    restored = store_at(tmp_path, FakeClock(now))
    monkeypatch.setattr(valve_metrics, "METRICS_FILE", str(tmp_path / "wrong-metrics.csv"))
    valve = SimpleNamespace()
    restored.load_baselines({"north": valve})
    assert valve.baseline_sample_count == 10
    assert valve.baseline_lpm == 2
    assert valve.baseline_std_dev == 0
    assert valve.baseline_trend is None


def test_legacy_two_argument_baseline_algorithm_remains_compatible(tmp_path, monkeypatch):
    values = list(range(1, 15))
    write_history(tmp_path, [
        ["north", (DAY - timedelta(days=14 - index)).isoformat(), 60, value, value]
        for index, value in enumerate(values)
    ])

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 18, 12, tzinfo=tz)

    monkeypatch.setattr(valve_metrics, "datetime", FrozenDatetime)
    monkeypatch.setattr(valve_metrics, "METRICS_FILE", str(tmp_path / "valve_metrics.csv"))
    valve = SimpleNamespace()
    load_baselines({"north": valve}, LOGGER)
    weights = [0.5 if i < 14 / 3 else 1 if i < 28 / 3 else 1.5 for i in range(14)]
    baseline = sum(value * weight for value, weight in zip(values, weights)) / sum(weights)
    assert valve.baseline_sample_count == 14
    assert valve.baseline_lpm == round(baseline, 2)
    assert valve.baseline_trend == round(30 / baseline * 100, 2)
    assert valve.baseline_std_dev == round(valve_metrics.statistics.stdev(values), 2)


@pytest.mark.parametrize("zero_or_rounds_to_zero", [0, 0.001])
def test_invalid_future_and_zero_baselines_are_explicit_and_never_repaired(
    tmp_path, caplog, zero_or_rounds_to_zero
):
    valid = [
        ["north", "2026-09-{:02d}".format(day), 60, zero_or_rounds_to_zero, zero_or_rounds_to_zero]
        for day in range(1, 11)
    ]
    invalid = [
        ["north", "2026-09-19", 60, 10000, 10000],
        ["north", "not-a-date", 60, 1, 1],
        ["north", "2026-09-17", 60, "nan", 2],
        ["north", "2026-09-17", 60, 1, "inf"],
        ["north", "2026-09-17", -1, 1, 1],
        ["north", "2026-09-17", 0, 1, 1],
    ]
    original = write_history(tmp_path, valid + invalid)
    valve = SimpleNamespace(baseline_lpm=999)
    with caplog.at_level(logging.WARNING):
        load_baselines({"north": valve}, LOGGER, tmp_path / "valve_metrics.csv", now=DAY)
    assert valve.baseline_sample_count == 10
    assert valve.baseline_lpm is None
    assert valve.baseline_trend is None
    assert "Future-dated row" in caplog.text
    assert "No usable positive flow baseline" in caplog.text
    assert (tmp_path / "valve_metrics.csv").read_bytes() == original


def test_late_or_clock_rollback_changes_stay_in_sidecar_with_explicit_limitations(tmp_path):
    clock = FakeClock()
    store = store_at(tmp_path, clock)
    store.add_interval(NOON, 60, {"north": contribution(60, 1)})
    store.finalize_before("2026-09-19")
    assert store.flush()
    original = (tmp_path / "valve_metrics.csv").read_bytes()
    clock.wall = NOON - timedelta(hours=2)
    store.add_interval(
        clock.now(), 10,
        {"north": contribution(10, 0.25), "new-manual-valve": contribution(10, 0.2)},
        unattributed_liters=0.5, unavailable_seconds=2,
    )
    store.finalize_before("2026-09-18")
    assert store.flush()
    assert (tmp_path / "valve_metrics.csv").read_bytes() == original
    restored = store_at(tmp_path)
    assert_totals(restored.daily_totals(DAY)["north"], 70, 1.25)
    assert_totals(restored.daily_totals(DAY)["new-manual-valve"], 10, 0.2)
    health = restored.get_health()
    assert health["export_limited"]
    assert health["export_limitation"]
    assert health["late_adjustment_seconds"] == 20
    assert health["late_adjustment_liters"] == pytest.approx(0.45)
    assert health["unattributed_liters"] == 0.5
    assert health["unavailable_seconds"] == 2
    assert not any(restored.get_quality()["2026-09-18"].values())
    envelope = json.loads((tmp_path / "runtime_state.json").read_text(encoding="utf-8"))
    assert envelope["state"]["exports"]["2026-09-18"]["north"]["seconds"] == 60
    assert envelope["state"]["late_adjustments"]["2026-09-18"]["valves"]["north"]["seconds"] == 10
    assert envelope["state"]["finalize_before"] == "2026-09-19"


@pytest.mark.parametrize(
    "elapsed,records,kwargs",
    [
        (-1, {}, {}),
        (float("nan"), {}, {}),
        (1, {"north": contribution(2, 1)}, {}),
        (1, {"north": contribution(1, -1)}, {}),
        (1, {"north": contribution(1, float("inf"))}, {}),
        (1e-300, {"north": contribution(1e-300, 1e308)}, {}),
        (1, {"north": contribution(0, 1)}, {}),
        (1, {"north": contribution(1, 1, "yes")}, {}),
        (1, {}, {"unavailable_seconds": 2}),
        (0, {}, {"unattributed_liters": 1}),
    ],
)
def test_invalid_intervals_do_not_partially_mutate_accounting(tmp_path, elapsed, records, kwargs):
    store = store_at(tmp_path)
    with pytest.raises(ValueError):
        store.add_interval(NOON, elapsed, records, **kwargs)
    assert store.daily_totals(DAY) == {}
    assert store.get_health()["unattributed_liters"] == 0
    assert store.get_health()["unavailable_seconds"] == 0


def test_naive_wall_time_is_rejected(tmp_path):
    store = store_at(tmp_path)
    with pytest.raises(ValueError, match="aware"):
        store.add_interval(NOON.replace(tzinfo=None), 1, {"north": contribution(1, 0.1)})


def test_slow_persistence_does_not_block_accounting_or_bounded_shutdown(tmp_path, monkeypatch):
    store = store_at(tmp_path)
    store.add_interval(NOON, 1, {"north": contribution(1, 0.1)})
    entered = threading.Event()
    release = threading.Event()
    real_write = store._atomic_write

    def slow_write(path, payload):
        if path == store.state_file and not entered.is_set():
            entered.set()
            if not release.wait(5):
                raise OSError("test did not release blocked storage")
        real_write(path, payload)

    monkeypatch.setattr(store, "_atomic_write", slow_write)
    store.start()
    store.request_flush()
    try:
        assert entered.wait(2), "Persistence worker did not start"
        started = time.monotonic()
        store.add_interval(NOON + timedelta(seconds=1), 2, {"north": contribution(2, 0.2)})
        store.finalize_before("2026-09-19")
        store.request_flush()
        assert_totals(store.daily_totals(DAY)["north"], 3, 0.3)
        assert store.get_health()["uncheckpointed_changes"]
        assert time.monotonic() - started < 0.5
        started = time.monotonic()
        assert not store.shutdown(timeout=0.02)
        assert time.monotonic() - started < 0.5
        assert "timed out" in store.get_health()["persistence_error"]
    finally:
        release.set()
        store._worker.join(3)
    assert not store._worker.is_alive()
    restored = store_at(tmp_path)
    assert_totals(restored.daily_totals(DAY)["north"], 3, 0.3)
    assert len(csv_rows(tmp_path)) == 1


def test_worker_periodic_checkpoint_and_optional_start_shutdown(tmp_path, monkeypatch):
    store = store_at(tmp_path)
    assert store.get_health()["checkpoint_interval_seconds"] == 30
    store.checkpoint_interval_seconds = 0.02
    store.add_interval(NOON, 2, {"north": contribution(2, 0.2)})
    persisted = threading.Event()
    real_write = store._atomic_write

    def observed_write(path, payload):
        real_write(path, payload)
        if path == store.backup_file:
            persisted.set()

    monkeypatch.setattr(store, "_atomic_write", observed_write)
    store.start()
    try:
        assert persisted.wait(2), "Worker did not checkpoint without an explicit request"
    finally:
        assert store.shutdown(timeout=3)
    assert not store.get_health()["uncheckpointed_changes"]
    restored = store_at(tmp_path)
    restored.add_interval(NOON, 1, {"north": contribution(1, 0.1)})
    assert restored.shutdown(timeout=3)
    assert_totals(store_at(tmp_path).daily_totals(DAY)["north"], 3, 0.3)


def test_recording_during_export_keeps_late_evidence_without_changing_frozen_csv(tmp_path, monkeypatch):
    store = store_at(tmp_path)
    store.add_interval(NOON, 10, {"north": contribution(10, 1)})
    store.finalize_before("2026-09-19")
    entered = threading.Event()
    release = threading.Event()
    real_write = store._atomic_write

    def slow_write(path, payload):
        if path == store.metrics_file and not entered.is_set():
            entered.set()
            if not release.wait(5):
                raise OSError("test did not release CSV export")
        real_write(path, payload)

    monkeypatch.setattr(store, "_atomic_write", slow_write)
    store.start()
    store.request_flush()
    try:
        assert entered.wait(2)
        store.add_interval(NOON, 5, {"north": contribution(5, 0.75, False)})
        assert store.get_health()["export_limited"]
    finally:
        release.set()
        assert store.shutdown(timeout=3)
    restored = store_at(tmp_path)
    assert_totals(restored.daily_totals(DAY)["north"], 15, 1.75, False)
    assert csv_rows(tmp_path)[0]["total_seconds"] == "10.0"
    assert restored.get_health()["late_adjustment_seconds"] == 5
    assert not restored.get_quality()["2026-09-18"]["north"]


def test_missing_primary_recovers_mirrored_checkpoint_with_warning(tmp_path):
    store = store_at(tmp_path)
    store.add_interval(NOON, 5, {"north": contribution(5, 0.5)})
    assert store.flush()
    (tmp_path / "runtime_state.json").unlink()
    restored = store_at(tmp_path)
    assert_totals(restored.daily_totals(DAY)["north"], 5, 0.5)
    assert restored.get_health()["persistence_error"]
    assert restored.flush()
    assert (tmp_path / "runtime_state.json").exists()


def test_changed_csv_after_load_is_not_overwritten_by_flush(tmp_path):
    original = write_history(tmp_path, [["old", "2026-09-01", 60, 1, 1]])
    store = store_at(tmp_path)
    store.add_interval(NOON, 10, {"north": contribution(10, 1)})
    assert store.flush()
    accepted_state = (tmp_path / "runtime_state.json").read_bytes()
    damaged = original + b"foreign,2026-09-17,60,2,2\r\n"
    (tmp_path / "valve_metrics.csv").write_bytes(damaged)
    store.finalize_before("2026-09-19")
    assert not store.flush()
    assert store.get_health()["write_blocked"]
    assert store.get_health()["persistence_error"]
    assert (tmp_path / "runtime_state.json").read_bytes() == accepted_state
    assert (tmp_path / "valve_metrics.csv").read_bytes() == damaged
