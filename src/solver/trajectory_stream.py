#!/usr/bin/env python3
"""Crash-durable trajectory capture for the paper-agent runners.

WHY THIS EXISTS
---------------
`run_agent_sdk.py` (Claude) and `run_codex_sdk.py` (Codex) used to accumulate
their events in a plain list and write `ai_scientist_trajectory.json` only
*after* the run loop returned -- with no try/finally. So any of these lost the
entire trajectory of a multi-day run:

  * a batch-scheduler walltime kill (SIGTERM then SIGKILL),
  * a job deletion,
  * an unhandled exception in the loop,
  * a hang that had to be killed by hand.

Several week-long runs lost theirs that way, recoverable afterwards only by
replaying the SDKs' own transcripts.

WHAT THIS PROVIDES
------------------
`StreamingEvents` is a drop-in replacement for `events = []`: every append is
*also* appended to `<workspace>/ai_scientist_trajectory.jsonl` and flushed, so
the transcript is on disk as it happens. `install_crash_saver()` turns
SIGTERM/SIGHUP/SIGINT, interpreter exit and unhandled exceptions into a
canonical `ai_scientist_trajectory.json` write, so a killed run still leaves
the file the grading pipeline expects.

SIGKILL cannot be caught. For that case the streamed `.jsonl` is the backstop:
`trajectory_from_stream()` (also runnable as `python trajectory_stream.py <ws>`)
rebuilds the canonical JSON from it.

USAGE (in a runner)
-------------------
    from trajectory_stream import streaming_events, install_crash_saver

    events = streaming_events(workspace, {"runner": "agent-sdk", "model": MODEL})
    install_crash_saver(events)
    ...
    events.note_meta(attempt=attempt, completion_reason=reason)   # on state change
    ...
    out = events.finalize(metadata)                               # instead of write_text
"""
from __future__ import annotations

import atexit
import json
import os
import signal
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

TRAJECTORY_FILENAME = "ai_scientist_trajectory.json"
TRAJECTORY_STREAM_FILENAME = "ai_scientist_trajectory.jsonl"
_FSYNC_EVERY = 50


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def save_trajectory(workspace, metadata: dict, events: list) -> Path:
    """Write `<workspace>/ai_scientist_trajectory.json` (same contract as the
    runners' previous inline write: never raise, coerce unserializable values)."""
    path = Path(workspace) / TRAJECTORY_FILENAME
    payload = {"metadata": metadata, "events": events}
    try:
        path.write_text(json.dumps(payload, ensure_ascii=False, default=str))
    except (OSError, TypeError, ValueError) as e:
        try:
            path.write_text(json.dumps(payload, ensure_ascii=False, default=repr))
            print(f"[run] Note: trajectory written with repr-coercion fallback: {e}", flush=True)
        except OSError as e2:
            print(f"[run] ERROR: could not write trajectory: {e2!r}", flush=True)
    return path


class StreamingEvents(list):
    """Trajectory event list that is simultaneously an append-only JSONL file."""

    def __init__(self, workspace, meta=None):
        super().__init__()
        self.workspace = Path(workspace)
        self.meta = dict(meta or {})
        self.meta.setdefault("start_utc", _utcnow())
        self.finalized = False
        self.stream_path = self.workspace / TRAJECTORY_STREAM_FILENAME
        self._since_sync = 0
        self._fh = None
        self._lock = threading.RLock()   # the memory watchdog appends from its own thread
        try:
            self._fh = open(self.stream_path, "a", buffering=1, encoding="utf-8")
            self._write({"__meta__": self.meta, "opened_utc": _utcnow()})
        except OSError as e:
            print(f"[run] Note: trajectory streaming disabled ({e}); the trajectory "
                  f"will only be written when the run ends.", flush=True)
            self._fh = None

    # --- internals ----------------------------------------------------
    def _write(self, obj) -> None:
        if self._fh is None:
            return
        try:
            self._fh.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")
            self._since_sync += 1
            if self._since_sync >= _FSYNC_EVERY:
                self._fh.flush()
                os.fsync(self._fh.fileno())
                self._since_sync = 0
        except (OSError, TypeError, ValueError):
            pass  # logging must never break the run

    # --- list surface: every mutation is streamed ---------------------
    def append(self, ev) -> None:
        with self._lock:
            super().append(ev)
            self._write(ev)

    def extend(self, evs) -> None:
        evs = list(evs)
        with self._lock:
            super().extend(evs)
            for ev in evs:
                self._write(ev)

    def __iadd__(self, evs):
        self.extend(evs)
        return self

    # --- metadata + finalization --------------------------------------
    def note_meta(self, **kw) -> None:
        """Record current run state, so a crash-written trajectory is accurate."""
        with self._lock:
            self.meta.update(kw)
            self._write({"__meta_update__": kw, "at_utc": _utcnow()})

    def finalize(self, metadata=None, status="clean") -> Path:
        """Write the canonical trajectory JSON. Idempotent."""
        if self.finalized:
            return self.workspace / TRAJECTORY_FILENAME
        md = dict(self.meta)
        md.update(metadata or {})
        md.setdefault("event_count", len(self))
        md["end_utc"] = _utcnow()
        if status != "clean":
            md["crash_saved"] = status
            md.setdefault("completed", False)
            md.setdefault("completion_reason",
                          f"run ended before a verdict ({status}); trajectory saved by the crash handler")
        path = save_trajectory(self.workspace, md, list(self))
        self.finalized = True
        self._write({"__finalized__": status, "at_utc": _utcnow(),
                     "events": len(self), "path": str(path)})
        if self._fh is not None:
            try:
                self._fh.flush()
                os.fsync(self._fh.fileno())
            except OSError:
                pass
        return path


def streaming_events(workspace, meta=None) -> StreamingEvents:
    """Factory, so runner call sites stay one line."""
    return StreamingEvents(workspace, meta)


def install_crash_saver(events: StreamingEvents, quiet: bool = False) -> StreamingEvents:
    """Save a canonical trajectory on signal, exit or unhandled exception."""

    def _save(status: str) -> None:
        try:
            p = events.finalize(status=status)
            if not quiet:
                print(f"\n[run] trajectory saved on {status}: {p} ({len(events)} events)", flush=True)
        except Exception as e:  # noqa: BLE001 - last-ditch path must not raise
            print(f"[run] could not save trajectory on {status}: {e!r}", flush=True)

    def _handler(signum, _frame):
        try:
            name = signal.Signals(signum).name
        except Exception:  # noqa: BLE001
            name = str(signum)
        print(f"\n[run] {name} received; finalizing trajectory before exit.", flush=True)
        _save(f"signal:{name}")
        os._exit(143 if signum == getattr(signal, "SIGTERM", -1) else 130)

    for sig_name in ("SIGTERM", "SIGHUP", "SIGINT"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):
            pass  # e.g. not the main thread

    atexit.register(lambda: None if events.finalized else _save("exit"))

    _prev_hook = sys.excepthook

    def _hook(exc_type, exc, tb):
        _save(f"exception:{exc_type.__name__}")
        _prev_hook(exc_type, exc, tb)

    sys.excepthook = _hook
    return events


# ======================================================================
# Backstop: rebuild the canonical JSON from a streamed .jsonl (SIGKILL case)
# ======================================================================
def stream_events(workspace) -> list:
    """The events already in a workspace's streamed .jsonl (a restarted run keeps them in its trajectory)."""
    stream = Path(workspace) / TRAJECTORY_STREAM_FILENAME
    events = []
    if stream.exists():
        for line in stream.open(errors="ignore"):
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if not (isinstance(d, dict) and ({"__meta__", "__meta_update__", "__finalized__"} & d.keys())):
                events.append(d)
    return events


def trajectory_from_stream(workspace, out=None, force=False) -> Path | None:
    """Rebuild ai_scientist_trajectory.json from ai_scientist_trajectory.jsonl."""
    ws = Path(workspace)
    stream = ws / TRAJECTORY_STREAM_FILENAME
    target = Path(out) if out else ws / TRAJECTORY_FILENAME
    if not stream.exists():
        print(f"[recover] no {stream}", flush=True)
        return None
    if target.exists() and not force:
        print(f"[recover] {target} already exists (use force=True to overwrite)", flush=True)
        return None

    meta: dict = {}
    events: list = []
    finalized = None
    bad = 0
    for line in stream.open(errors="ignore"):
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            bad += 1
            continue
        if isinstance(d, dict) and "__meta__" in d:
            meta.update(d["__meta__"] or {})
        elif isinstance(d, dict) and "__meta_update__" in d:
            meta.update(d["__meta_update__"] or {})
        elif isinstance(d, dict) and "__finalized__" in d:
            finalized = d
        else:
            events.append(d)

    meta.update({
        "event_count": len(events),
        "recovered": True,
        "recovery": {
            "written_utc": _utcnow(),
            "source": str(stream),
            "why": "the runner was killed without writing the canonical trajectory "
                   "(SIGKILL / node loss); rebuilt from the streamed JSONL",
            "unparsable_lines": bad,
            "stream_reported_finalized": finalized,
        },
    })
    meta.setdefault("completed", False)
    path = save_trajectory(ws, meta, events)
    print(f"[recover] wrote {path}: {len(events)} events "
          f"({bad} unparsable lines skipped)", flush=True)
    return path


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("workspace")
    ap.add_argument("--out", default=None)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    trajectory_from_stream(a.workspace, a.out, a.force)
