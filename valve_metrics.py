"""Daily accounting, with disk access restricted to load/flush/baseline helpers.

MetricsStore is a single-writer store. Call load() after startup close-all, then
optionally start() its worker. Recording, totals, health, finalization and flush
requests do not access files. A successful checkpoint saves all accepted totals;
the default worker waits at most 30 seconds between flushes. Worst-case crash
loss is 30 seconds plus the durations of the previous and in-flight flushes:
checkpoints cover their starting snapshot, not concurrent recording. There is
no fixed recovery bound during stalled/failed storage or a timed-out shutdown.

runtime_state.json and runtime_state.json.bak are checksummed, versioned JSON
checkpoints, not commands to resume irrigation. Both are durable before CSV
export. Frozen export intents allow retry after a crash at either side of the
CSV replacement without duplicating valve/date keys. Atomic copy-on-append
preserves the historical CSV byte-for-byte as a prefix, including duplicates;
it never reserializes or repairs old rows. Directory fsync is used on POSIX
(Windows does not support it). Failed/corrupt files are never silently reset.
"""

import copy
import csv
import hashlib
import io
import json
import math
import os
import statistics
import threading
import time
import uuid
from datetime import date as Date, datetime, time as WallTime, timedelta
from collections import defaultdict

METRICS_FILE = os.path.join("data", "valve_metrics.csv")
CSV_FIELDS = [
    "valve_name", "date", "total_seconds", "total_liters", "avg_liters_per_minute"
]
CHECKPOINT_INTERVAL_SECONDS = 30.0


def _date_key(value):
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, Date):
        return value.isoformat()
    parsed = Date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("Dates must use YYYY-MM-DD")
    return value


def _nonnegative(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("{} must be a finite nonnegative number".format(name))
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise ValueError("{} must be a finite nonnegative number".format(name))
    return value


def _record(value):
    if not isinstance(value, dict) or set(value) != {"seconds", "liters", "complete"}:
        raise ValueError("Invalid valve contribution")
    seconds = _nonnegative(value["seconds"], "seconds")
    liters = _nonnegative(value["liters"], "liters")
    if type(value["complete"]) is not bool or (seconds == 0 and liters > 0):
        raise ValueError("Invalid contribution quality or volume without duration")
    if seconds > 0:
        _nonnegative(liters / seconds * 60, "average flow")
    return {"seconds": seconds, "liters": liters, "complete": value["complete"]}


def _empty_day():
    return {"valves": {}, "unattributed_liters": 0.0, "unavailable_seconds": 0.0}


def _merge_record(records, name, contribution):
    if contribution["seconds"] == 0:
        return
    previous = records.setdefault(name, {"seconds": 0.0, "liters": 0.0, "complete": True})
    previous["seconds"] += contribution["seconds"]
    previous["liters"] += contribution["liters"]
    previous["complete"] = previous["complete"] and contribution["complete"]


def _canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key: {}".format(key))
        result[key] = value
    return result


class _LocalClock:
    def now(self):
        return datetime.now().astimezone()

    def monotonic(self):
        return time.monotonic()


class MetricsStore:
    """In-memory accounting; load()/flush() must run outside the actuator lock.

    load() returns False when persistence is blocked. Accounting may continue in
    memory in that case, but health explicitly reports incomplete recovery.
    A recovered backup also leaves a durable warning: it cannot prove that no
    newer accounting was lost. Unreadable state is preserved, or corrupt JSON
    is renamed to a same-directory ``.corrupt-*`` file before recovery writes.

    ``get_quality()`` returns {date_string: {valve_name: complete_bool}} for
    load_baselines(..., quality=...). Prefer store.load_baselines(valves), which
    supplies that metadata, this store's CSV path, and its configured date.
    Neither baseline helper belongs inside the actuator lock.
    """

    def __init__(self, directory, logger, clock=None):
        self.directory = os.fspath(directory)
        self.metrics_file = os.path.join(self.directory, "valve_metrics.csv")
        self.state_file = os.path.join(self.directory, "runtime_state.json")
        self.backup_file = self.state_file + ".bak"
        self.logger = logger
        self.clock = clock or _LocalClock()
        self._timezone = self.clock.now().tzinfo if clock is not None else None
        self.checkpoint_interval_seconds = CHECKPOINT_INTERVAL_SECONDS
        self._lock = threading.Lock()
        self._flush_lock = threading.Lock()
        self._worker_lock = threading.Lock()
        self._requested = threading.Event()
        self._stop = threading.Event()
        self._worker = None
        self._shutdown_result = False
        self._loaded = False
        self._write_blocked = False
        self._io_error = None
        self._csv_keys = set()
        self._legacy_dates = set()
        self._revision = 0
        self._persisted_revision = -1
        self._checkpoint_monotonic = None
        self._state = self._new_state()
        self._rebuild_counters()

    @staticmethod
    def _new_state():
        return {
            "generation": 0,
            "days": {},
            "exports": {},
            "late_adjustments": {},
            "finalize_before": None,
            "history": {
                "length": 0, "sha256": hashlib.sha256(b"").hexdigest(),
                "keys": [], "duplicate_rows": 0,
            },
            "recovery_warning": None,
        }

    def _rebuild_counters(self):
        days = self._state["days"]
        late = self._state["late_adjustments"]
        self._unattributed = sum(day["unattributed_liters"] for day in days.values())
        self._unavailable = sum(day["unavailable_seconds"] for day in days.values())
        self._incomplete = {
            (date, name) for date, day in days.items()
            for name, record in day["valves"].items() if not record["complete"]
        }
        self._late_seconds = sum(
            row["seconds"] for day in late.values() for row in day["valves"].values()
        )
        self._late_liters = sum(
            row["liters"] for day in late.values() for row in day["valves"].values()
        )
        self._legacy_dates = {key[0] for key in self._state["history"]["keys"]}

    @staticmethod
    def _read_csv(path):
        try:
            with open(path, "rb") as source:
                raw = source.read()
        except FileNotFoundError:
            return b"", {}
        reader = csv.reader(io.StringIO(raw.decode("utf-8-sig"), newline=""), strict=True)
        if next(reader, None) != CSV_FIELDS:
            raise ValueError("Invalid metrics CSV header; historical file was not changed")
        rows = defaultdict(list)
        for number, fields in enumerate(reader, 2):
            if len(fields) != len(CSV_FIELDS) or not fields[0].strip():
                raise ValueError("Invalid metrics CSV row {}".format(number))
            date = _date_key(fields[1])
            values = [_nonnegative(float(item), "CSV value") for item in fields[2:]]
            _record({"seconds": values[0], "liters": values[1], "complete": True})
            rows[(date, fields[0])].append(values)
        return raw, dict(rows)

    @classmethod
    def _decode_state(cls, raw):
        envelope = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        if not isinstance(envelope, dict) or set(envelope) != {"version", "sha256", "state"}:
            raise ValueError("Invalid checkpoint envelope")
        if type(envelope["version"]) is not int or envelope["version"] != 1:
            raise ValueError("Unsupported checkpoint version")
        state = envelope["state"]
        if hashlib.sha256(_canonical_json(state)).hexdigest() != envelope["sha256"]:
            raise ValueError("Checkpoint checksum mismatch")
        if not isinstance(state, dict) or set(state) != set(cls._new_state()):
            raise ValueError("Invalid checkpoint fields")
        if type(state["generation"]) is not int or state["generation"] < 0:
            raise ValueError("Invalid checkpoint generation")
        cutoff = state["finalize_before"]
        if cutoff is not None:
            _date_key(cutoff)
        if state["recovery_warning"] is not None and not isinstance(state["recovery_warning"], str):
            raise ValueError("Invalid recovery warning")
        for field in ("days", "late_adjustments"):
            if not isinstance(state[field], dict):
                raise ValueError("Invalid checkpoint dates")
            for date, day in state[field].items():
                _date_key(date)
                if not isinstance(day, dict) or set(day) != set(_empty_day()):
                    raise ValueError("Invalid checkpoint day")
                _nonnegative(day["unattributed_liters"], "unattributed_liters")
                _nonnegative(day["unavailable_seconds"], "unavailable_seconds")
                cls._validate_records(day["valves"])
        if not isinstance(state["exports"], dict):
            raise ValueError("Invalid export intents")
        for date, records in state["exports"].items():
            _date_key(date)
            cls._validate_records(records)
            if cutoff is None or date >= cutoff or date not in state["days"]:
                raise ValueError("Export outside finalized accounting")
            for name, record in records.items():
                total = state["days"][date]["valves"].get(name)
                if (record["seconds"] <= 0 or total is None
                        or record["seconds"] > total["seconds"] or record["liters"] > total["liters"]):
                    raise ValueError("Export exceeds recorded accounting")
        history = state["history"]
        if not isinstance(history, dict) or set(history) != {"length", "sha256", "keys", "duplicate_rows"}:
            raise ValueError("Invalid historical CSV identity")
        for field in ("length", "duplicate_rows"):
            if type(history[field]) is not int or history[field] < 0:
                raise ValueError("Invalid historical CSV count")
        if (not isinstance(history["sha256"], str) or len(history["sha256"]) != 64
                or not isinstance(history["keys"], list)):
            raise ValueError("Invalid historical CSV identity")
        keys = set()
        for key in history["keys"]:
            if not isinstance(key, list) or len(key) != 2 or not isinstance(key[1], str) or not key[1].strip():
                raise ValueError("Invalid historical CSV key")
            _date_key(key[0])
            if tuple(key) in keys:
                raise ValueError("Duplicate historical CSV key")
            keys.add(tuple(key))
        for date, day in state["late_adjustments"].items():
            if date not in state["days"] or (date not in state["exports"] and date not in {k[0] for k in keys}):
                raise ValueError("Late accounting without a frozen date")
            totals = state["days"][date]
            if any(day[field] > totals[field] for field in ("unattributed_liters", "unavailable_seconds")):
                raise ValueError("Late quality exceeds total")
            for name, row in day["valves"].items():
                total = totals["valves"].get(name)
                if total is None or row["seconds"] > total["seconds"] or row["liters"] > total["liters"]:
                    raise ValueError("Late accounting exceeds total")
        return state

    @staticmethod
    def _validate_records(records):
        if not isinstance(records, dict):
            raise ValueError("Invalid checkpoint contributions")
        for name, value in records.items():
            if not isinstance(name, str) or not name.strip():
                raise ValueError("Invalid valve name")
            _record(value)

    @staticmethod
    def _csv_values(record):
        return [
            record["seconds"], round(record["liters"], 2),
            round(_nonnegative(record["liters"] / record["seconds"] * 60, "average flow"), 2),
        ]

    @classmethod
    def _check_csv(cls, raw, rows, state):
        history = state["history"]
        if (len(raw) < history["length"]
                or hashlib.sha256(raw[:history["length"]]).hexdigest() != history["sha256"]):
            raise ValueError("Historical CSV changed or disappeared; refusing to overwrite it")
        historical = {tuple(key) for key in history["keys"]}
        for (date, name), values in rows.items():
            if (date, name) in historical:
                continue
            record = state["exports"].get(date, {}).get(name)
            if record is None or len(values) != 1 or values[0] != cls._csv_values(record):
                raise ValueError("CSV row {}/{} has no matching durable export intent".format(date, name))

    @staticmethod
    def _fsync_directory(directory):
        if os.name == "nt":
            return
        descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _atomic_write(self, path, payload):
        staging = path + ".pending-" + uuid.uuid4().hex
        try:
            with open(staging, "xb") as target:
                target.write(payload)
                target.flush()
                os.fsync(target.fileno())
            os.replace(staging, path)
            self._fsync_directory(self.directory)
        finally:
            try:
                os.unlink(staging)
            except FileNotFoundError:
                pass

    def load(self):
        """Recover accounting only; return whether durable writing is usable.

        Recovery warnings remain latched across later successful checkpoints;
        only transient flush errors are cleared by a successful flush.
        """
        with self._flush_lock:
            with self._lock:
                if self._loaded:
                    return not self._write_blocked
            candidates, errors = [], {}
            for path in (self.state_file, self.backup_file):
                try:
                    with open(path, "rb") as source:
                        state = self._decode_state(source.read())
                    candidates.append((path, state))
                except FileNotFoundError:
                    pass
                except (ValueError, TypeError, KeyError, UnicodeError, OverflowError) as error:
                    errors[path] = (str(error), True)
                except OSError as error:
                    errors[path] = (str(error), False)
            raw, rows, csv_error = b"", {}, None
            try:
                raw, rows = self._read_csv(self.metrics_file)
            except (OSError, ValueError, UnicodeError, csv.Error) as error:
                csv_error = str(error)
            state, blocked, warning = self._new_state(), False, None
            if candidates:
                candidates.sort(key=lambda item: item[1]["generation"], reverse=True)
                chosen_path, state = candidates[0]
                if (len(candidates) == 2
                        and candidates[0][1]["generation"] == candidates[1][1]["generation"]
                        and candidates[0][1] != candidates[1][1]):
                    blocked, warning = True, "Conflicting checkpoints at the same generation"
                if csv_error:
                    blocked, warning = True, "CSV recovery failed: " + csv_error
                else:
                    try:
                        self._check_csv(raw, rows, state)
                    except ValueError as error:
                        blocked, warning = True, str(error)
                if not blocked and (errors or chosen_path == self.backup_file):
                    preserved = []
                    for path, (message, corrupt) in errors.items():
                        if not corrupt:
                            blocked = True
                            preserved.append("{} unreadable: {}".format(path, message))
                            continue
                        destination = path + ".corrupt-" + uuid.uuid4().hex
                        try:
                            os.rename(path, destination)
                            self._fsync_directory(self.directory)
                            preserved.append(destination)
                        except OSError as error:
                            blocked = True
                            preserved.append("{} could not be preserved: {}".format(path, error))
                    warning = "Recovered checkpoint with a persistence limitation; newer accounting may be lost"
                    if preserved:
                        warning += "; preserved evidence: " + "; ".join(preserved)
                if warning:
                    state["recovery_warning"] = warning
            elif errors:
                blocked = True
                warning = "No usable accounting checkpoint; files preserved: " + "; ".join(
                    "{}: {}".format(path, detail[0]) for path, detail in errors.items()
                )
            elif csv_error:
                blocked, warning = True, "CSV recovery failed; file preserved: " + csv_error
            else:
                state["history"] = {
                    "length": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
                    "keys": [list(key) for key in sorted(rows)],
                    "duplicate_rows": sum(len(values) - 1 for values in rows.values()),
                }
                for (date, name), values in rows.items():
                    day = state["days"].setdefault(date, _empty_day())
                    # Legacy duplicates remain unchanged; this view sums their recorded totals.
                    day["valves"][name] = {
                        "seconds": sum(value[0] for value in values),
                        "liters": sum(value[1] for value in values), "complete": True,
                    }
            with self._lock:
                self._state = state
                if warning:
                    self._state["recovery_warning"] = warning
                self._write_blocked = blocked
                self._csv_keys = set(rows)
                self._loaded = True
                self._rebuild_counters()
            if self._state["recovery_warning"]:
                self.logger.error(self._state["recovery_warning"])
            return not blocked

    def _split_interval(self, start_wall, elapsed):
        if not isinstance(start_wall, datetime) or start_wall.utcoffset() is None:
            raise ValueError("start_wall must be an aware configured-local datetime")
        timezone = self._timezone or start_wall.tzinfo
        cursor = start_wall.timestamp()
        remaining = elapsed
        while remaining > 0:
            local = datetime.fromtimestamp(cursor, timezone)
            midnight = datetime.combine(local.date() + timedelta(days=1), WallTime.min)
            if hasattr(timezone, "localize"):
                candidates = [timezone.localize(midnight, is_dst=dst).timestamp() for dst in (True, False)]
            else:
                candidates = [midnight.replace(tzinfo=timezone, fold=fold).timestamp() for fold in (0, 1)]
            # Choose the first real instant on the next date, even at a DST midnight.
            boundary = min(
                candidate for candidate in candidates
                if candidate > cursor and datetime.fromtimestamp(candidate, timezone).date() > local.date()
            )
            duration = min(remaining, boundary - cursor)
            yield local.date().isoformat(), duration / elapsed
            remaining -= duration
            cursor = boundary

    def add_interval(self, start_wall, elapsed_seconds, contributions,
                     unattributed_liters=0.0, unavailable_seconds=0.0):
        """Record controller-attributed values, proportionally split by local date.

        complete=False means liters are only the measured portion, not a full
        measured daily volume. No flow is inferred or distributed between valves.
        Zero-duration/paused contributions do not create an operated-day row.
        """
        elapsed = _nonnegative(elapsed_seconds, "elapsed_seconds")
        unattributed = _nonnegative(unattributed_liters, "unattributed_liters")
        unavailable = _nonnegative(unavailable_seconds, "unavailable_seconds")
        if unavailable > elapsed or (elapsed == 0 and unattributed > 0):
            raise ValueError("Quality accounting exceeds interval duration")
        if not isinstance(contributions, dict):
            raise ValueError("contributions must be a dictionary")
        records = {}
        for name, value in contributions.items():
            if not isinstance(name, str) or not name.strip():
                raise ValueError("Invalid valve name")
            record = _record(value)
            if record["seconds"] > elapsed:
                raise ValueError("Valve duration exceeds elapsed interval")
            if record["seconds"] > 0:
                records[name] = record
        splits = list(self._split_interval(start_wall, elapsed))
        pieces = []
        left = copy.deepcopy(records)
        left_unattributed, left_unavailable = unattributed, unavailable
        for index, (date, fraction) in enumerate(splits):
            last = index == len(splits) - 1
            piece = {
                name: {
                    "seconds": left[name]["seconds"] if last else row["seconds"] * fraction,
                    "liters": left[name]["liters"] if last else row["liters"] * fraction,
                    "complete": row["complete"],
                } for name, row in records.items()
            }
            u = left_unattributed if last else unattributed * fraction
            s = left_unavailable if last else unavailable * fraction
            for name, row in piece.items():
                left[name]["seconds"] -= row["seconds"]
                left[name]["liters"] -= row["liters"]
            left_unattributed -= u
            left_unavailable -= s
            if piece or u or s:
                pieces.append((date, piece, u, s))
        with self._lock:
            if not self._loaded:
                raise RuntimeError("Call load() after startup close-all before recording accounting")
            for date, piece, u, s in pieces:
                day = self._state["days"].setdefault(date, _empty_day())
                late = date in self._state["exports"] or date in self._legacy_dates
                destinations = [day]
                if late:
                    destinations.append(self._state["late_adjustments"].setdefault(date, _empty_day()))
                    self._late_seconds += sum(row["seconds"] for row in piece.values())
                    self._late_liters += sum(row["liters"] for row in piece.values())
                for destination in destinations:
                    for name, row in piece.items():
                        _merge_record(destination["valves"], name, row)
                    destination["unattributed_liters"] += u
                    destination["unavailable_seconds"] += s
                self._unattributed += u
                self._unavailable += s
                self._incomplete.update((date, name) for name, row in piece.items() if not row["complete"])
            if pieces:
                self._revision += 1

    def daily_totals(self, date_or_date_string):
        date = _date_key(date_or_date_string)
        with self._lock:
            return {
                name: dict(row)
                for name, row in self._state["days"].get(date, _empty_day())["valves"].items()
            }

    def finalize_before(self, date):
        date = _date_key(date)
        with self._lock:
            cutoff = self._state["finalize_before"]
            if cutoff is None or date > cutoff:
                self._state["finalize_before"] = date
                self._revision += 1

    def get_health(self):
        monotonic_now = self.clock.monotonic()
        with self._lock:
            errors = [value for value in (self._state["recovery_warning"], self._io_error) if value]
            return {
                "unattributed_liters": self._unattributed,
                "unavailable_seconds": self._unavailable,
                "persistence_error": "; ".join(errors) or None,
                "loaded": self._loaded,
                "recovery_complete": self._loaded and not self._write_blocked and not self._state["recovery_warning"],
                "write_blocked": self._write_blocked,
                "incomplete_rows": len(self._incomplete),
                "late_adjustment_seconds": self._late_seconds,
                "late_adjustment_liters": self._late_liters,
                "export_limited": bool(self._state["late_adjustments"]),
                "export_limitation": (
                    "Late accounting is retained in the sidecar only; frozen CSV dates are incomplete"
                    if self._state["late_adjustments"] else None
                ),
                "historical_duplicate_rows": self._state["history"]["duplicate_rows"],
                "checkpoint_interval_seconds": self.checkpoint_interval_seconds,
                "uncheckpointed_changes": self._revision != self._persisted_revision,
                "last_checkpoint_age_seconds": (
                    None if self._checkpoint_monotonic is None
                    else max(0.0, monotonic_now - self._checkpoint_monotonic)
                ),
            }

    def get_quality(self):
        with self._lock:
            quality = {
                date: {name: row["complete"] for name, row in day["valves"].items()}
                for date, day in self._state["days"].items()
            }
            for date, name in self._csv_keys:
                if self._write_blocked or date in self._state["late_adjustments"]:
                    quality.setdefault(date, {})[name] = False
            for date in self._state["late_adjustments"]:
                for name in quality.get(date, {}):
                    quality[date][name] = False
            return quality

    def load_baselines(self, valves_dict):
        """Populate legacy baseline fields without exposing checkpoint internals.

        Call after load(), outside the actuator lock/deadline thread. This reads
        the store's CSV with recovered quality and the clock's configured-local
        date. Unusable recovery disables baselines without clearing its error.
        """
        with self._lock:
            usable = self._loaded and not self._write_blocked
        if not usable:
            for valve in valves_dict.values():
                valve.baseline_lpm = None
                valve.baseline_trend = None
                valve.baseline_std_dev = None
                valve.baseline_sample_count = 0
            self.logger.error("Baselines unavailable: accounting quality could not be recovered")
            return
        return load_baselines(
            valves_dict, self.logger, metrics_file=self.metrics_file,
            quality=self.get_quality(), now=self.clock.now(),
        )

    def request_flush(self):
        self._requested.set()

    def flush(self):
        """Checkpoint, then export frozen rows; return False and report any failure."""
        with self._flush_lock:
            with self._lock:
                if not self._loaded or self._write_blocked:
                    if not self._loaded:
                        self._io_error = "load() must precede flush()"
                    return False
                cutoff = self._state["finalize_before"]
                if cutoff is not None:
                    for date, day in self._state["days"].items():
                        if date < cutoff and date not in self._state["exports"] and date not in self._legacy_dates:
                            self._state["exports"][date] = {
                                name: dict(row) for name, row in day["valves"].items() if row["seconds"] > 0
                            }
                            self._revision += 1
                self._state["generation"] += 1
                snapshot = copy.deepcopy(self._state)
                revision = self._revision
            try:
                raw, rows = self._read_csv(self.metrics_file)
                self._check_csv(raw, rows, snapshot)
                payload = _canonical_json({
                    "version": 1, "sha256": hashlib.sha256(_canonical_json(snapshot)).hexdigest(),
                    "state": snapshot,
                }) + b"\n"
                os.makedirs(self.directory, exist_ok=True)
                # Quality and export intent must be recoverable before any CSV row is visible.
                self._atomic_write(self.state_file, payload)
                self._atomic_write(self.backup_file, payload)
                output = io.StringIO(newline="")
                writer = csv.writer(output)
                added = set()
                for date, records in sorted(snapshot["exports"].items()):
                    for name, record in sorted(records.items()):
                        if (date, name) not in rows:
                            if not raw and not added:
                                writer.writerow(CSV_FIELDS)
                            writer.writerow([name, date] + self._csv_values(record))
                            added.add((date, name))
                if added:
                    separator = b"\r\n" if raw and not raw.endswith((b"\r", b"\n")) else b""
                    self._atomic_write(self.metrics_file, raw + separator + output.getvalue().encode("utf-8"))
                checkpoint_monotonic = self.clock.monotonic()
                with self._lock:
                    self._csv_keys = set(rows) | added
                    self._persisted_revision = revision
                    self._checkpoint_monotonic = checkpoint_monotonic
                    self._io_error = None
                return True
            except (OSError, ValueError, TypeError, UnicodeError, csv.Error) as error:
                message = "Metrics persistence failed: {}".format(error)
                with self._lock:
                    self._io_error = message
                    if not isinstance(error, OSError):
                        self._write_blocked = True
                self.logger.error(message)
                return False

    def _run_worker(self):
        while not self._stop.is_set():
            self._requested.wait(self.checkpoint_interval_seconds)
            self._requested.clear()
            if not self._stop.is_set():
                self.flush()
        self._shutdown_result = self.flush()

    def start(self):
        with self._worker_lock:
            if self._worker is None or not self._worker.is_alive():
                self._stop.clear()
                self._shutdown_result = False
                self._worker = threading.Thread(target=self._run_worker, name="metrics-persistence", daemon=True)
                self._worker.start()

    def shutdown(self, timeout=5.0):
        """Request a final checkpoint; never wait for disk longer than timeout.

        Returns False on timeout/failure. A timed-out daemon may finish later;
        callers must not close/reuse its directory while it is still working.
        """
        timeout = _nonnegative(timeout, "timeout")
        self.start()
        self._stop.set()
        self._requested.set()
        self._worker.join(timeout)
        if self._worker.is_alive():
            with self._lock:
                self._io_error = "Metrics shutdown timed out; pending accounting may not be durable"
            return False
        return self._shutdown_result


def append_daily_summary(valve_name, date, total_seconds, total_liters):
    """
    Append a daily summary for a valve to the CSV file.
    Only call this if the valve actually operated (total_seconds > 0).
    
    Args:
        valve_name: Name of the valve
        date: Date string in YYYY-MM-DD format
        total_seconds: Total seconds valve was open
        total_liters: Total liters used
    """
    if total_seconds <= 0:
        return  # Don't record days when valve didn't operate
    
    # Calculate average liters per minute
    avg_liters_per_minute = (total_liters / total_seconds) * 60 if total_seconds > 0 else 0
    
    # Create data directory and file with header if it doesn't exist
    os.makedirs(os.path.dirname(METRICS_FILE), exist_ok=True)
    file_exists = os.path.isfile(METRICS_FILE)
    
    with open(METRICS_FILE, 'a', newline='') as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(['valve_name', 'date', 'total_seconds', 'total_liters', 'avg_liters_per_minute'])
        writer.writerow([valve_name, date, total_seconds, round(total_liters, 2), round(avg_liters_per_minute, 2)])


def load_baselines(valves_dict, logger, metrics_file=None, quality=None, now=None):
    """
    Load the last 30 days of data for each valve and calculate baselines.
    Updates valve objects with baseline metrics.
    
    Args:
        valves_dict: Dictionary of valve objects keyed by name
        logger: Logger instance
        metrics_file: Optional explicit CSV path; defaults to the legacy path
        quality: Optional {date_string: {valve_name: complete_bool}} from
            MetricsStore.get_quality(). Missing historic metadata stays eligible.
        now: Optional configured-local datetime, date, or ISO date string
    """
    metrics_file = METRICS_FILE if metrics_file is None else metrics_file
    today = Date.fromisoformat(_date_key(datetime.now() if now is None else now))
    for valve in valves_dict.values():
        valve.baseline_lpm = None
        valve.baseline_trend = None
        valve.baseline_std_dev = None
        valve.baseline_sample_count = 0
    if not os.path.isfile(metrics_file):
        logger.info("No valve metrics file found. Baselines will be calculated after data is collected.")
        return
    
    # Read all data from CSV
    valve_data = defaultdict(list)
    
    try:
        with open(metrics_file, 'r', encoding='utf-8-sig', newline='') as f:
            reader = csv.DictReader(f, strict=True)
            if reader.fieldnames != CSV_FIELDS:
                raise ValueError("Invalid metrics CSV header")
            for number, row in enumerate(reader, 2):
                try:
                    date = _date_key(row['date'])
                    seconds = _nonnegative(float(row['total_seconds']), "total_seconds")
                    liters = _nonnegative(float(row['total_liters']), "total_liters")
                    lpm = _nonnegative(float(row['avg_liters_per_minute']), "avg_liters_per_minute")
                    if None in row or not row['valve_name'].strip() or seconds <= 0:
                        raise ValueError("Missing fields, empty valve name, or nonpositive duration")
                    if date > today.isoformat():
                        raise ValueError("Future-dated row")
                except (ValueError, TypeError, KeyError, AttributeError) as error:
                    logger.warning("Skipping invalid metrics row %s: %s", number, error)
                    continue
                if quality is not None and not quality.get(date, {}).get(row['valve_name'], True):
                    logger.info("Excluding incomplete metrics for %s on %s", row['valve_name'], date)
                    continue
                valve_data[row['valve_name']].append({
                    'date': Date.fromisoformat(date), 'total_seconds': seconds,
                    'total_liters': liters, 'avg_liters_per_minute': lpm,
                })
    except (OSError, ValueError, UnicodeError, csv.Error) as error:
        logger.error("Unable to read valve baselines; CSV was not changed: %s", error)
        return
    
    # Calculate baselines for each valve
    cutoff_date = today - timedelta(days=30)
    
    for valve_name, valve in valves_dict.items():
        if valve_name not in valve_data:
            logger.info(f"No historical data for valve '{valve_name}'")
            valve.baseline_lpm = None
            valve.baseline_trend = None
            valve.baseline_std_dev = None
            valve.baseline_sample_count = 0
            continue
        
        # Filter to last 30 days
        recent_data = [d for d in valve_data[valve_name] if d['date'] > cutoff_date]
        
        if len(recent_data) < 10:
            logger.info(f"Insufficient data for valve '{valve_name}' (only {len(recent_data)} days, need 10+)")
            valve.baseline_lpm = None
            valve.baseline_trend = None
            valve.baseline_std_dev = None
            valve.baseline_sample_count = len(recent_data)
            continue
        
        # Sort by date
        recent_data.sort(key=lambda x: x['date'])
        
        # Extract avg_liters_per_minute values
        lpm_values = [d['avg_liters_per_minute'] for d in recent_data]
        
        # Calculate weighted average (more weight to recent data)
        n = len(lpm_values)
        weighted_sum = 0
        weight_total = 0
        
        for i, lpm in enumerate(lpm_values):
            # Weight: older = 0.5, middle = 1.0, recent = 1.5
            if i < n / 3:
                weight = 0.5
            elif i < 2 * n / 3:
                weight = 1.0
            else:
                weight = 1.5
            
            weighted_sum += lpm * weight
            weight_total += weight
        
        baseline_lpm = weighted_sum / weight_total
        if not math.isfinite(baseline_lpm) or round(baseline_lpm, 2) <= 0:
            valve.baseline_sample_count = len(recent_data)
            logger.warning("No usable positive flow baseline for valve '%s'", valve_name)
            continue
        
        # Calculate standard deviation
        std_dev = statistics.stdev(lpm_values) if len(lpm_values) > 1 else 0
        
        # Calculate trend (linear regression slope) - only if enough samples
        baseline_trend_pct = None
        if len(lpm_values) >= 14:
            # y = mx + b, where x is day index (0 to n-1), y is lpm
            x_values = list(range(len(lpm_values)))
            x_mean = statistics.mean(x_values)
            y_mean = statistics.mean(lpm_values)
            
            numerator = sum((x - x_mean) * (y - y_mean) for x, y in zip(x_values, lpm_values))
            denominator = sum((x - x_mean) ** 2 for x in x_values)
            
            # Slope represents change in lpm per day
            trend = numerator / denominator if denominator != 0 else 0
            
            # Convert to percentage change per 30 days
            baseline_trend_pct = (trend * 30 / baseline_lpm * 100) if baseline_lpm > 0 else 0
        
        # Update valve object
        valve.baseline_lpm = round(baseline_lpm, 2)
        valve.baseline_trend = round(baseline_trend_pct, 2) if baseline_trend_pct is not None else None
        valve.baseline_std_dev = round(std_dev, 2)
        valve.baseline_sample_count = len(recent_data)
        
        trend_text = f"{valve.baseline_trend:+.2f}% per month" if valve.baseline_trend is not None else "N/A (need 14+ samples)"
        logger.info(f"Valve '{valve_name}' baseline: {valve.baseline_lpm} L/min, "
                   f"trend: {trend_text}, "
                   f"std dev: {valve.baseline_std_dev}, "
                   f"samples: {valve.baseline_sample_count}")


def write_daily_summaries(valves_dict, date_str, logger):
    """
    Write daily summaries for all valves that operated today.
    
    Args:
        valves_dict: Dictionary of valve objects keyed by name
        date_str: Date string in YYYY-MM-DD format
        logger: Logger instance
    """
    for valve_name, valve in valves_dict.items():
        if valve.secondsDaily > 0:
            append_daily_summary(valve_name, date_str, valve.secondsDaily, valve.litersDaily)
            logger.info(f"Wrote daily summary for valve '{valve_name}': "
                       f"{valve.secondsDaily}s, {valve.litersDaily:.2f}L")
