#!/usr/bin/env python3
"""
Codex-SDK runner for the benchmark's paper agents (GPT-5.6-Sol).

Why this exists (vs. `codex exec` in run_codex.py):
  `codex exec` is a single self-contained non-interactive turn; resuming starts a
  fresh engine invocation, so any background experiment still running when the
  previous turn ended is already dead. This runner drives ONE PERSISTENT Codex SDK
  thread (openai-codex): the process stays alive across turns, so background
  experiments survive while we (a) judge completeness and (b) inject a "not done,
  here's what's missing" follow-up on the SAME thread. A turn that ends while the
  agent's own experiments are still running waits for them and does not use up an
  attempt (see wait_for_background).

Model/effort: gpt-5.6-sol at reasoning effort `max` (CODEX_MODEL / CODEX_EFFORT).
Auth: reuses the ChatGPT login in $CODEX_HOME/auth.json (default ~/.codex). OPENAI_API_KEY would switch
  to API billing, so we drop it in-process to bill against the subscription.
  Gateway mode (CODEX_GATEWAY_BASE_URL + the key in $CODEX_GATEWAY_KEY_ENV)
  routes calls through an OpenAI-compatible Responses endpoint instead.

Usage:
  python run_codex_sdk.py --workspace <dir containing task_spec.md> --unlimited
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import sys
import threading
import time
from pathlib import Path

# Crash-durable trajectory capture (streams events to disk; saves a canonical
# trajectory on SIGTERM/exception/exit). Lives next to this runner.
from trajectory_stream import streaming_events, install_crash_saver, stream_events  # noqa: E402

# ---- auth + environment (before importing the SDK) ----
# An OPENAI_API_KEY would make the SDK bill against the API instead of the
# ChatGPT-login subscription; drop it so every call uses ~/.codex/auth.json.
os.environ.pop("OPENAI_API_KEY", None)

from openai_codex import Codex, Sandbox, ApprovalMode  # noqa: E402
from openai_codex.client import CodexConfig  # noqa: E402

MODEL = os.environ.get("CODEX_MODEL", "gpt-5.6-sol")
EFFORT = os.environ.get("CODEX_EFFORT", "max")  # low|medium|high|xhigh|max|ultra
MAX_RESUMES = int(os.environ.get("CODEX_MAX_RESUMES", "5"))
# Restart of a run that already started (e.g. a harness update or a node failure): continue its Codex thread with its
# attempt count and elapsed time, so the restart neither grants nor costs attempts. Same account and workspace; the
# events streamed before the restart are kept in the trajectory.
RESUME_THREAD = os.environ.get("CODEX_RESUME_THREAD") or None
RESUME_ATTEMPT = int(os.environ.get("CODEX_RESUME_ATTEMPT", "1"))
PRIOR_ELAPSED_S = float(os.environ.get("CODEX_PRIOR_ELAPSED_S", "0"))
RESTART_NOTE = ("the run was restarted on a new machine (a harness update or an infrastructure failure), so every process you had running "
                "(background tasks, daemons, servers, experiments) was stopped. Check what finished, relaunch what "
                "still has to run, and continue where you left off")

# Gateway mode: when CODEX_GATEWAY_BASE_URL is set, route model calls through a
# custom OpenAI-compatible provider (e.g. a LiteLLM gateway serving
# gpt-5.6-sol) instead of the metered ChatGPT/Codex subscription. The
# codex engine requires wire_api="responses" (chat wire was dropped). The key
# is read by the engine from the env var named in CODEX_GATEWAY_KEY_ENV.
GATEWAY_BASE = os.environ.get("CODEX_GATEWAY_BASE_URL", "")
GATEWAY_KEY_ENV = os.environ.get("CODEX_GATEWAY_KEY_ENV", "CODEX_GATEWAY_API_KEY")
# CODEX_BIN: a Codex CLI newer than the one bundled with openai-codex (new models need it).
CODEX_BIN = os.environ.get("CODEX_BIN", "")
CODEX_HOME = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")

# A subscription usage limit (or sustained throttling) is an infrastructure condition, not a
# model failure: wait for the reset and resume the SAME attempt instead of burning retries.
_LIMIT_MARKERS = ("usage limit", "hit your", "rate limit", "rate_limit", "too many requests", "429",
                  "quota", "try again in")
LIMIT_WAIT_S = int(os.environ.get("CODEX_LIMIT_WAIT_S", "1800"))
MAX_LIMIT_WAITS = int(os.environ.get("CODEX_MAX_LIMIT_WAITS", "400"))  # safety cap (~8 days at 30 min)
# The model being at capacity (overloaded) is also an infrastructure condition: wait and retry the
# SAME turn without using up a retry or an attempt. Any other turn error counts as a retry; a run
# gives up after MAX_RETRIES of them (5, the same for every solver runner).
_CAPACITY_MARKERS = ("at capacity", "overloaded")
CAPACITY_WAIT_S = int(os.environ.get("CODEX_CAPACITY_WAIT_S", "300"))
# A judge that errors or returns no parseable verdict is retried this many times before the
# attempt is marked incomplete -- a judge failure is not the agent's failure.
JUDGE_MAX_ERROR_RETRIES = int(os.environ.get("CODEX_JUDGE_MAX_ERROR_RETRIES", "3"))


def _is_limit(exc) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return any(m in text for m in _LIMIT_MARKERS)


def _is_capacity(exc) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return any(m in text for m in _CAPACITY_MARKERS)


# Opt-in credit guard (CODEX_AVOID_CREDITS=1): a ChatGPT plan with purchased credits never hits a
# usage limit -- past 100% of the plan window it silently bills credits. The guard polls the
# account's limits and freezes the Codex engine (SIGSTOP) while the plan window is at or above
# CODEX_CREDIT_GUARD_PCT and credits remain, then resumes it (SIGCONT) after the window resets.
# The agent's own experiments keep running; only model calls stop. Linux only.
AVOID_CREDITS = os.environ.get("CODEX_AVOID_CREDITS") == "1"
GUARD_PCT = float(os.environ.get("CODEX_CREDIT_GUARD_PCT", "98"))
GUARD_POLL_S = int(os.environ.get("CODEX_CREDIT_GUARD_POLL_S", "300"))


def _plan_state(raw):
    """(highest used % across plan windows, credits remain?) from an account/rateLimits/read reply."""
    used, credits = 0.0, False
    for r in (raw.get("rateLimitsByLimitId") or {"codex": raw.get("rateLimits")}).values():
        for key in ("primary", "secondary"):
            used = max(used, float(((r or {}).get(key) or {}).get("usedPercent") or 0))
        c = (r or {}).get("credits") or {}
        credits = credits or bool(c.get("unlimited") or c.get("hasCredits") or float(c.get("balance") or 0) > 0)
    return used, credits


def _engine_pids():
    """PIDs of this runner's Codex engine processes (direct children whose command mentions codex)."""
    me, out = os.getpid(), []
    for d in os.listdir("/proc") if os.path.isdir("/proc") else []:
        if not d.isdigit():
            continue
        try:
            stat = open(f"/proc/{d}/stat").read()
            ppid = int(stat.rsplit(")", 1)[1].split()[1])
            cmd = open(f"/proc/{d}/cmdline", "rb").read().replace(b"\0", b" ").decode(errors="ignore")
        except (OSError, ValueError, IndexError):
            continue
        if ppid == me and "codex" in cmd:
            out.append(int(d))
    return out


def start_credit_guard(codex, note):
    """Background thread: freeze the engine near the plan limit, thaw it after the reset."""
    if not AVOID_CREDITS:
        return None
    state = {"frozen": []}

    def loop():
        while True:
            try:
                used, credits = _plan_state(codex._client._request_raw("account/rateLimits/read", {}))
            except Exception:
                used, credits = None, None
            if used is not None:
                if not state["frozen"] and used >= GUARD_PCT and credits:
                    pids = _engine_pids()
                    for pid in pids:
                        os.kill(pid, signal.SIGSTOP)
                    state["frozen"] = pids
                    note(event="credit_guard_freeze", used_pct=used, pids=pids)
                    _safe_print(f"[guard] plan window at {used:.0f}% with credits available: engine frozen "
                                f"(pids {pids}) until the window resets -- no credits will be spent.")
                elif state["frozen"] and used < GUARD_PCT:
                    for pid in state["frozen"]:
                        try:
                            os.kill(pid, signal.SIGCONT)
                        except OSError:
                            pass
                    note(event="credit_guard_thaw", used_pct=used)
                    _safe_print(f"[guard] plan window back to {used:.0f}%: engine resumed.")
                    state["frozen"] = []
            time.sleep(GUARD_POLL_S)

    _safe_print(f"[guard] credit guard on: the engine is frozen at >= {GUARD_PCT:.0f}% of the plan window "
                f"while credits remain (checked every {GUARD_POLL_S}s).")
    t = threading.Thread(target=loop, name="credit-guard", daemon=True)
    t.start()
    return t


def _limit_wait_s(codex) -> float:
    """Seconds until the earliest exhausted window resets (best effort), else LIMIT_WAIT_S."""
    try:
        raw = codex._client._request_raw("account/rateLimits/read", {})
        resets = []
        for r in (raw.get("rateLimitsByLimitId") or {"codex": raw.get("rateLimits")}).values():
            for key in ("primary", "secondary"):
                w = (r or {}).get(key) or {}
                if (w.get("usedPercent") or 0) >= 100 and w.get("resetsAt"):
                    resets.append(float(w["resetsAt"]))
        if resets:
            return max(60.0, min(resets) - time.time() + 60)
    except Exception:
        pass
    return float(LIMIT_WAIT_S)


# Background-work wait (the Codex counterpart of the Claude runner keeping its session open while
# background tasks run). A Codex turn ends whenever the agent yields, even with the experiments it
# launched still running, and counting that as a failed attempt spends the resume budget on long
# jobs. So when a turn ends with the paper unfinished while the agent's own processes are still
# using CPU, the runner waits for them and then re-engages the agent WITHOUT consuming an attempt.
# The agent's processes are the ones that inherited this run's tag from the engine, plus processes
# started inside the workspace after the engine. Linux only (/proc); elsewhere it never waits.
RUN_TAG_VAR = "AGENT_RUN_TAG"
BG_POLL_S = int(os.environ.get("CODEX_BG_POLL_S", "60"))
BG_IDLE_S = int(os.environ.get("CODEX_BG_IDLE_S", "1800"))         # this long without CPU use = stalled
BG_MIN_CPU_S = float(os.environ.get("CODEX_BG_MIN_CPU_S", "1.0"))  # CPU-seconds per poll that count as busy
_TICK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100


def _stat(pid):
    """Fields of /proc/<pid>/stat after the command name (index 0 = state, 1 = ppid, 11/12 = utime/stime,
    19 = start time)."""
    return open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()


def _ancestors(pid):
    out = set()
    while pid > 1 and pid not in out:
        out.add(pid)
        try:
            pid = int(_stat(pid)[1])
        except (OSError, ValueError, IndexError):
            break
    return out


def _agent_procs(workspace: Path) -> dict:
    """{pid: CPU ticks used so far} for the live processes the agent started (see above)."""
    if not os.path.isdir("/proc"):
        return {}
    engines = _engine_pids()
    skip = _ancestors(os.getpid()) | set(engines)
    born = []
    for p in engines:
        try:
            born.append(int(_stat(p)[19]))
        except (OSError, ValueError, IndexError):
            pass
    engine_start = min(born) if born else None
    tag = f"{RUN_TAG_VAR}={os.environ.get(RUN_TAG_VAR, '')}".encode()
    ws, uid, out = str(workspace), os.getuid(), {}
    for d in os.listdir("/proc"):
        if not d.isdigit() or int(d) in skip:
            continue
        try:
            if os.stat(f"/proc/{d}").st_uid != uid:
                continue
            argv0 = open(f"/proc/{d}/cmdline", "rb").read().split(b"\0", 1)[0]
            if os.path.basename(argv0).startswith(b"codex"):
                continue                                       # the engine's own helpers
            f = _stat(d)
            if f[0] == "Z":
                continue
            mine = tag in open(f"/proc/{d}/environ", "rb").read().split(b"\0")
            if not mine and engine_start is not None and int(f[19]) > engine_start:
                cwd = os.readlink(f"/proc/{d}/cwd")
                mine = cwd == ws or cwd.startswith(ws + os.sep)
            if mine:
                out[int(d)] = int(f[11]) + int(f[12])
        except (OSError, ValueError, IndexError):
            continue
    return out


def _cpu_s(prev: dict, cur: dict) -> float:
    """CPU-seconds the agent's processes used between two _agent_procs() samples."""
    return sum(t - prev.get(p, 0) for p, t in cur.items() if t >= prev.get(p, 0)) / _TICK


def wait_for_background(workspace: Path, deadline: float, note):
    """Block while the agent's own processes keep using CPU after its turn ended. Returns how the
    wait ended, or None when none of the agent's processes was doing any work."""
    prev = _agent_procs(workspace)
    if not prev:
        return None
    time.sleep(BG_POLL_S)
    cur = _agent_procs(workspace)
    if _cpu_s(prev, cur) < BG_MIN_CPU_S:
        return None                      # only idle leftovers (a shell, an idle server)
    t0 = last_busy = time.monotonic()
    note(event="background_wait", n_procs=len(cur))
    _safe_print(f"[run] turn ended while {len(cur)} of the agent's processes are still running; "
                f"waiting for them -- NOT consuming an attempt.")
    while True:
        if time.monotonic() >= deadline:
            how = "were still running at the deadline"
            break
        time.sleep(BG_POLL_S)
        prev, cur = cur, _agent_procs(workspace)
        if not cur:
            how = "finished"
            break
        if _cpu_s(prev, cur) >= BG_MIN_CPU_S:
            last_busy = time.monotonic()
        elif time.monotonic() - last_busy >= BG_IDLE_S:
            how = f"stopped using CPU {BG_IDLE_S // 60} min ago and may be stalled"
            break
    waited = int(time.monotonic() - t0)
    note(event="background_wait_end", how=how, waited_s=waited)
    _safe_print(f"[run] background processes {how} (waited {waited}s); re-engaging the agent.")
    return how


def _cfg(effort):
    """Base thread config; adds the custom provider when in gateway mode."""
    c = {"model_reasoning_effort": effort}
    if GATEWAY_BASE:
        c["model_provider"] = "gateway"
        c["model_providers"] = {"gateway": {"name": "Gateway", "base_url": GATEWAY_BASE,
                                            "env_key": GATEWAY_KEY_ENV, "wire_api": "responses"}}
    return c
GOAL_FILE = Path(__file__).resolve().parent / "goal.md"
UNLIMITED_TIMEOUT = 100 * 24 * 3600
INITIAL_PROMPT = "Read task_spec.md and complete the task it describes."


def _safe_print(s: str) -> None:
    try:
        print(s, flush=True)
    except BlockingIOError:
        try:
            fd = sys.stdout.fileno()
            fl = os.get_blocking(fd)
            os.set_blocking(fd, True)
            try:
                print(s, flush=True)
            finally:
                os.set_blocking(fd, fl)
        except Exception:
            pass


# ======================================================================
# Cheap placeholder pre-filter (fast-fail before the LLM judge) — same
# deterministic checks the Claude-SDK runner uses; runner-agnostic.
# ======================================================================
_PLACEHOLDER_RE = re.compile(
    r"\[\s*(?:PEND(?:ING)?|TODO|TBD|PLACEHOLDER|XXX|\?\?\?|FIXME|FILL[ _]?IN|RESULTS?)\s*\]"
    r"|runs?\s+pending"
    r"|\\(?:todo|pending|placeholder)\b"
    r"|\bto\s?be\s?(?:filled|determined|computed|run|added|reported)\b"
    r"|\bX{3,}\b"
    r"|\bplaceholder\b"
    r"|\bsmoke[-\s]?test\b"
    r"|\b(?:TBD|TODO|FIXME)\b"
    r"|\\newcommand\*?\s*\{\\[A-Za-z@]+\}\s*(?:\[\d+\])?\s*\{\s*(?:-{1,2}|\?{1,3}|n/?a|nan)\s*\}",
    re.IGNORECASE,
)
_TEX_INPUT_RE = re.compile(r"\\(?:input|include|subfile)\*?\s*\{([^}]+)\}")


def _strip_tex_comments(text: str) -> str:
    return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("%"))


def _resolve_tex_sources(report_tex: Path, _max_files: int = 200):
    base = report_tex.parent
    seen, order, queue = set(), [], [report_tex]
    while queue and len(order) < _max_files:
        cur = queue.pop(0)
        if not cur.exists():
            continue
        try:
            rp = cur.resolve()
        except OSError:
            continue
        if rp in seen:
            continue
        seen.add(rp)
        order.append(cur)
        try:
            body = _strip_tex_comments(cur.read_text(errors="ignore"))
        except OSError:
            continue
        for ref in _TEX_INPUT_RE.findall(body):
            cand = base / ref.strip()
            if cand.suffix != ".tex":
                cand = cand.with_suffix(".tex")
            queue.append(cand)
    return order


def cheap_incomplete(workspace: Path):
    pdf = workspace / "proposal" / "report.pdf"
    tex = workspace / "proposal" / "report.tex"
    if not pdf.exists() or pdf.stat().st_size == 0:
        return True, "proposal/report.pdf is missing or empty (the paper did not compile)"
    if not tex.exists():
        return True, "proposal/report.tex is missing"
    from collections import Counter
    counts = Counter()
    for src in _resolve_tex_sources(tex):
        body = _strip_tex_comments(src.read_text(errors="ignore"))
        for h in _PLACEHOLDER_RE.findall(body):
            counts[h.strip().replace("\n", " ")] += 1
    if counts:
        summary = ", ".join(f"'{t}' x{n}" for t, n in counts.most_common(6))
        return True, f"placeholder/pending entries remain ({summary})"
    return False, ""


def _read_report_sources(workspace: Path, cap: int = 60000) -> str:
    tex = workspace / "proposal" / "report.tex"
    if not tex.exists():
        return "(proposal/report.tex missing)"
    parts = []
    for src in _resolve_tex_sources(tex):
        try:
            parts.append(f"% ==== {src.name} ====\n" + src.read_text(errors="ignore"))
        except OSError:
            pass
    blob = "\n\n".join(parts)
    return blob[:cap] + ("\n...[truncated]" if len(blob) > cap else "")


# ======================================================================
# LLM completion judge — a fresh, ephemeral read-only Codex thread that
# returns a strict JSON verdict via output_schema.
# ======================================================================
JUDGE_INSTRUCTIONS = (
    "You are a strict COMPLETENESS checker for an autonomous science agent's paper "
    "submission. Given the task spec and the current LaTeX sources (report.tex and "
    "every file it \\input-s, incl. results-macro files), decide whether the submission "
    "is COMPLETE: every required deliverable/section present; every required "
    "metric/table/number filled with a REAL value (not '--','XXX',blank,'N/A','TBD' or "
    "any placeholder); no results macro unfilled; referenced figures exist; the paper "
    "reads as finished, not a skeleton. Judge ONLY completeness, not correctness/quality."
)
_JUDGE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,  # required=false by OpenAI structured-output
    "properties": {
        "complete": {"type": "boolean"},
        "blocking_issues": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["complete", "blocking_issues"],
}


def judge_completion(codex: Codex, workspace: Path):
    spec = ""
    sp = workspace / "task_spec.md"
    if sp.exists():
        spec = sp.read_text(errors="ignore")[:60000]
    report = _read_report_sources(workspace)
    prompt = (
        f"{JUDGE_INSTRUCTIONS}\n\n===== TASK SPECIFICATION =====\n{spec}\n\n"
        f"===== CURRENT REPORT SOURCES =====\n{report}\n\n"
        "Return the JSON verdict {complete, blocking_issues} now. blocking_issues must "
        "be specific (name the missing section, unfilled table/metric, absent figure); "
        "empty list if complete."
    )
    # A usage limit waits for the reset; any other judge failure (an error, no verdict,
    # malformed JSON) is retried a few times before it counts against the attempt.
    limit_waits = failures = 0
    while True:
        v, problem = None, None
        try:
            jt = codex.thread_start(
                model=MODEL,
                config=_cfg("high"),
                cwd=str(workspace),
                sandbox=Sandbox.read_only,
                approval_mode=ApprovalMode.auto_review,
                ephemeral=True,
            )
            res = jt.run(prompt, output_schema=_JUDGE_SCHEMA)
            text = getattr(res, "final_response", "") or ""
            m = re.search(r"\{.*\}", text, re.DOTALL)
            if not m:
                problem = f"completion judge returned no verdict ({text[:200]!r})"
            else:
                try:
                    v = json.loads(m.group(0))
                except json.JSONDecodeError:
                    problem = "completion judge returned malformed JSON"
        except Exception as e:
            if (_is_limit(e) or _is_capacity(e)) and limit_waits < MAX_LIMIT_WAITS:
                limit_waits += 1
                wait = _limit_wait_s(codex) if _is_limit(e) else CAPACITY_WAIT_S
                _safe_print(f"[judge] {'usage limit' if _is_limit(e) else 'model at capacity'}: {e!r}; "
                            f"sleeping {int(wait)}s, then judging again -- NOT consuming an attempt.")
                time.sleep(wait)
                continue
            problem = f"completion judge failed to run ({e!r})"
        if problem is None:
            break
        if failures < JUDGE_MAX_ERROR_RETRIES:
            failures += 1
            _safe_print(f"[judge] {problem}; judging again ({failures}/{JUDGE_MAX_ERROR_RETRIES}) "
                        f"-- NOT consuming an attempt.")
            time.sleep(60)
            continue
        _safe_print(f"[judge] {problem} after {failures} retries -- treating as INCOMPLETE-unknown")
        return False, [f"{problem}; re-verify the paper is complete"]
    return bool(v.get("complete")), [str(x) for x in (v.get("blocking_issues") or [])]


def resume_prompt(reason: str) -> str:
    reason = (reason or "").strip()
    detail = f" Specifically: {reason}." if reason else ""
    return (
        "Your paper is not finished yet." + detail + " Continue in THIS session: any "
        "experiments you launched are still running -- poll them to completion and fold "
        "their real results in; fill every remaining value; make sure proposal/report.pdf "
        "recompiles with no placeholder, pending, XXX, or '--' entries anywhere (including "
        "\\input-ed section and results-macro files). If a specific experiment genuinely "
        "cannot be completed, remove its placeholder and log the attempt honestly in "
        "proposal/attempts_log.md. Do not stop until it is done."
    )


# ======================================================================
# Trajectory capture + live per-turn logging
# ======================================================================
def _short(v, n=300):
    s = v if isinstance(v, str) else json.dumps(v, default=str)
    s = s.replace("\n", " | ")
    return s if len(s) <= n else s[:n] + "..."


def log_turn(events, start, attempt, res):
    t = round(time.monotonic() - start, 3)
    items = getattr(res, "items", None) or []
    for it in items:
        d = it if isinstance(it, dict) else getattr(it, "__dict__", {}) or {"repr": str(it)[:400]}
        kind = d.get("type") or d.get("item_type") or type(it).__name__
        events.append({"t_offset": t, "attempt": attempt, "item": d})
        # human-readable one-liners for common item kinds
        blob = _short(d, 180)
        if any(k in str(kind).lower() for k in ("reason", "think")):
            _safe_print(f"[{t}] 🧠 {blob}")
        elif any(k in str(kind).lower() for k in ("command", "tool", "exec", "shell", "patch", "file")):
            _safe_print(f"[{t}] → {kind}: {blob}")
        elif "message" in str(kind).lower() or "text" in str(kind).lower():
            _safe_print(f"[{t}] 💬 {blob}")
    _safe_print(f"[{t}] [turn {attempt}] status={getattr(res,'status',None)} "
                f"err={getattr(res,'error',None)} items={len(items)}")


# ======================================================================
# Workspace setup
# ======================================================================
def setup_workspace(workspace: Path):
    if not workspace.is_dir():
        sys.exit(f"[run] workspace is not a directory: {workspace}")
    if not (workspace / "task_spec.md").exists():
        sys.exit(f"[run] missing task_spec.md in {workspace}")
    src_sty = Path(__file__).resolve().parent / "neurips.sty"
    ws_sty = workspace / "neurips.sty"
    if src_sty.exists() and not ws_sty.exists():
        shutil.copy2(src_sty, ws_sty)


def check_auth():
    if GATEWAY_BASE:
        # Gateway mode: the engine authenticates with the key in $GATEWAY_KEY_ENV.
        if not os.environ.get(GATEWAY_KEY_ENV):
            sys.exit(f"[run] CODEX_GATEWAY_BASE_URL is set but ${GATEWAY_KEY_ENV} is empty.")
        return
    if os.environ.get("OPENAI_API_KEY"):
        sys.exit("[run] OPENAI_API_KEY is set; it would bill API credits instead of the ChatGPT subscription.")
    if not (CODEX_HOME / "auth.json").exists():
        sys.exit(f"[run] no {CODEX_HOME}/auth.json found (run `codex login` / the ChatGPT login once).")


# ======================================================================
# Main persistent-thread loop
# ======================================================================
def run(args) -> int:
    workspace = Path(args.workspace).resolve()
    setup_workspace(workspace)
    check_auth()

    timeout = UNLIMITED_TIMEOUT if args.unlimited else (args.hours * 3600 if args.hours else UNLIMITED_TIMEOUT)
    start = time.monotonic()
    deadline = start + timeout

    goal_text = GOAL_FILE.read_text(errors="ignore") if GOAL_FILE.exists() else None
    MAX_RETRIES = int(os.environ.get("CODEX_MAX_RETRIES", "5"))

    prior = stream_events(workspace) if RESUME_THREAD else []
    events = streaming_events(workspace, {
        "runner": "codex-sdk", "model": MODEL, "effort": EFFORT,
        "workspace": str(workspace), "max_resumes": MAX_RESUMES,
    })
    list.extend(events, prior)    # already in the .jsonl: kept in the trajectory, not streamed again
    install_crash_saver(events)   # a batch-job walltime kill / crash still leaves a trajectory
    complete = False
    reason = RESTART_NOTE if RESUME_THREAD else ""
    attempt = RESUME_ATTEMPT if RESUME_THREAD else 1
    thread_id = RESUME_THREAD
    timed_out = False
    retries = 0
    limit_waits = 0
    bg_waits = 0

    def note(**kw):
        events.append({"t_offset": round(time.monotonic() - start, 3), "harness": kw})

    # Tag the engine's environment so every command the agent runs (background jobs included) carries it.
    os.environ[RUN_TAG_VAR] = f"{os.getpid()}-{time.time_ns()}"
    with Codex(CodexConfig(codex_bin=CODEX_BIN) if CODEX_BIN else None) as codex:
        start_credit_guard(codex, note)
        thread = None
        if not RESUME_THREAD:
            # Start one persistent thread; full_access + auto_review == unattended
            # autonomous execution (write files, run experiments) with no approval stalls.
            thread = codex.thread_start(
                model=MODEL,
                config=_cfg(EFFORT),
                cwd=str(workspace),
                sandbox=Sandbox.full_access,
                approval_mode=ApprovalMode.auto_review,
                developer_instructions=goal_text,
            )
            thread_id = getattr(thread, "id", None)
            _safe_print(f"\n[run] === Attempt 1 (initial) === thread={str(thread_id)[:8]} "
                        f"model={MODEL} effort={EFFORT}")
        events.note_meta(thread_id=thread_id, attempt=attempt)
        prompt = INITIAL_PROMPT

        def infra_wait(e):
            """Seconds to wait for a usage limit or a model at capacity (NOT a retry), else None."""
            nonlocal limit_waits
            if limit_waits >= MAX_LIMIT_WAITS or not (_is_limit(e) or _is_capacity(e)):
                return None
            limit_waits += 1
            kind = "usage limit" if _is_limit(e) else "model at capacity"
            wait = min(_limit_wait_s(codex) if _is_limit(e) else CAPACITY_WAIT_S,
                       max(60.0, deadline - time.monotonic()))
            events.append({"t_offset": round(time.monotonic() - start, 3),
                           "harness": {"event": kind.replace(" ", "_") + "_wait", "error": repr(e)[:300],
                                       "n": limit_waits, "wait_s": int(wait)}})
            _safe_print(f"[run] {kind} (wait #{limit_waits}): {e!r}; sleeping {int(wait)}s "
                        f"-- NOT consuming a retry or an attempt.")
            return wait

        def count_retry(e, what):
            """Count one retry for a genuine error; False once the cap or the deadline is reached."""
            nonlocal retries
            retries += 1
            events.append({"t_offset": round(time.monotonic() - start, 3),
                           "harness": {"event": "turn_error", "error": repr(e)[:300], "retry": retries}})
            _safe_print(f"[run] {what} ({retries}/{MAX_RETRIES}): {e!r}")
            if retries > MAX_RETRIES or time.monotonic() >= deadline or thread_id is None:
                _safe_print("[run] not retrying (cap/deadline/no-thread); finalizing with current state.")
                return False
            wait = min(30 * retries, 900)
            _safe_print(f"[run] backing off {wait}s, then resuming thread {str(thread_id)[:8]}.")
            time.sleep(wait)
            return True

        def resume_thread():
            """Re-attach to the thread after an interruption; None when giving up."""
            while True:
                try:
                    return codex.thread_resume(
                        thread_id, model=MODEL, config=_cfg(EFFORT),
                        cwd=str(workspace), sandbox=Sandbox.full_access,
                        approval_mode=ApprovalMode.auto_review, developer_instructions=goal_text)
                except Exception as e2:
                    wait = infra_wait(e2)
                    if wait is not None:
                        time.sleep(wait)
                    elif not count_retry(e2, "resume failed"):
                        return None

        if RESUME_THREAD:
            note(attempt=attempt, resume=True, reason="runner restarted", restarted_thread=RESUME_THREAD,
                 prior_elapsed_s=PRIOR_ELAPSED_S, prior_events=len(prior))
            _safe_print(f"\n[run] === Attempt {attempt} (continued after a runner restart; thread {RESUME_THREAD[:8]}, "
                        f"{len(prior)} earlier events kept) ===")
            thread = resume_thread()
            prompt = resume_prompt(RESTART_NOTE)

        while thread is not None and not complete and not timed_out:
            # --- run one turn on the persistent thread (with resume-on-error) ---
            try:
                res = thread.run(prompt)
            except Exception as e:
                wait = infra_wait(e)
                if wait is not None:
                    time.sleep(wait)
                elif not count_retry(e, "turn error"):
                    break
                thread = resume_thread()
                if thread is None:
                    break
                prompt = resume_prompt("(resumed after an interruption) " + (reason or "continue"))
                continue

            log_turn(events, start, attempt, res)

            if time.monotonic() >= deadline:
                timed_out = True
                _safe_print("[run] deadline reached; stopping.")
                break

            # --- turn ended: decide complete / resume ---
            inc, why = cheap_incomplete(workspace)
            if inc:
                reason = why
                _safe_print(f"[run] attempt {attempt}: INCOMPLETE (pre-check) -- {why}")
            else:
                _safe_print(f"[run] attempt {attempt}: pre-check clean; running completion judge...")
                jc, issues = judge_completion(codex, workspace)
                if jc:
                    complete = True
                    reason = "complete"
                    events.note_meta(completed=True, completion_reason="complete")
                    _safe_print("[run] judge: COMPLETE")
                    break
                reason = "; ".join(issues) if issues else "judge flagged the paper incomplete"
                _safe_print(f"[run] judge: INCOMPLETE -- {_short(reason, 240)}")

            # The agent yielded while its own experiments are still running: wait for them, then
            # re-engage it on the SAME attempt (the Claude runner gets this from the SDK).
            how = wait_for_background(workspace, deadline, note)
            if how and time.monotonic() < deadline:
                bg_waits += 1
                prompt = f"Your background processes {how}. " + resume_prompt(reason)
                continue

            if attempt >= 1 + MAX_RESUMES:
                _safe_print(f"[run] reached resume cap ({MAX_RESUMES}); accepting current state.")
                break
            if time.monotonic() >= deadline:
                timed_out = True
                break
            attempt += 1
            events.note_meta(attempt=attempt, completion_reason=reason)
            _safe_print(f"\n[run] === Attempt {attempt} (resume: {_short(reason, 120)}) ===")
            prompt = resume_prompt(reason)

    elapsed = time.monotonic() - start + PRIOR_ELAPSED_S   # a restarted run counts its time before the restart
    metadata = {
        "model": MODEL, "effort": EFFORT, "runner": "codex-sdk",
        "thread_id": thread_id, "attempts": attempt, "max_resumes": MAX_RESUMES,
        "retries": retries, "infra_waits": limit_waits, "background_waits": bg_waits, "completed": complete, "completion_reason": reason,
        "timed_out": timed_out, "elapsed_seconds": round(elapsed, 1),
        "restarted_thread": RESUME_THREAD, "prior_elapsed_seconds": PRIOR_ELAPSED_S or None,
    }
    out = events.finalize(metadata)   # idempotent; the crash saver may already have written it
    _safe_print(f"\n[run] Trajectory: {out} ({len(events)} events)")
    _safe_print(f"[run] Status: {'COMPLETE' if complete else ('TIMED OUT' if timed_out else 'INCOMPLETE (cap)')} "
                f"in {elapsed:.0f}s over {attempt} attempt(s)")
    return 0 if complete else 1


def main():
    ap = argparse.ArgumentParser(description="Codex SDK runner for one benchmark task.")
    ap.add_argument("--workspace", required=True,
                    help="Paper directory containing task_spec.md (the agent runs here).")
    ap.add_argument("--unlimited", action="store_true")
    ap.add_argument("--hours", type=float, default=None)
    args = ap.parse_args()
    try:
        rc = run(args)
    except KeyboardInterrupt:
        rc = 130
    sys.exit(rc)


if __name__ == "__main__":
    main()
