#!/usr/bin/env python3
"""
Agent-SDK runner for the benchmark's paper agents (Claude models).

Why this exists (vs. the `claude -p` + `--resume` runner):
  `claude -p` is a single self-contained turn; `--resume` starts a BRAND NEW
  process that reloads the transcript, so any background experiment still running
  when the previous turn ended is already dead. This runner instead drives ONE
  PERSISTENT Claude Agent SDK session: the process stays alive across turns, so
  background experiments survive while we (a) judge completeness and (b) inject a
  "not done, here's what's missing" follow-up message in the SAME session.

Auth: subscription OAuth (CLAUDE_CODE_OAUTH_TOKEN / ~/.claude claudeAiOauth).
  ANTHROPIC_API_KEY silently wins over OAuth in the SDK auth precedence, so we
  drop it in-process. No --bare (which would require an API key).
  With SOLVER_ANTHROPIC_BYOK=1 the API credential (ANTHROPIC_API_KEY or
  ANTHROPIC_AUTH_TOKEN, optionally with ANTHROPIC_BASE_URL) is used instead.

Usage:
  python run_agent_sdk.py --workspace <dir containing task_spec.md> --unlimited
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import sys
import time
import uuid
from pathlib import Path

# Crash-durable trajectory capture (streams events to disk; saves a canonical
# trajectory on SIGTERM/exception/exit). Lives next to this runner.
from trajectory_stream import streaming_events, install_crash_saver  # noqa: E402
from memory_watchdog import use_disk_tmpdir, start_memory_watchdog, pending_notices  # noqa: E402

# ---- auth + environment (must run before importing the SDK) ----
# Two auth modes:
#   (1) subscription OAuth (default): ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN
#       outrank the OAuth token, so we DROP them → every call bills the
#       subscription, not API credits.
#   (2) BYOK / gateway (SOLVER_ANTHROPIC_BYOK=1, e.g. an API key, or a gateway
#       such as OpenRouter anthropic/*): the key/token IS the credential — KEEP
#       it and forward ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN / ANTHROPIC_API_KEY
#       to the SDK subprocess (see build_options).
BYOK = os.environ.get("SOLVER_ANTHROPIC_BYOK") == "1"
if not BYOK:
    os.environ.pop("ANTHROPIC_API_KEY", None)
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
os.environ.setdefault("IS_SANDBOX", "1")
# Ban Claude Code auto-memory: every run starts clean (no cross-run/cross-paper
# leakage of prior solutions). Hard-set so the environment can't turn it back on.
os.environ["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
# Wait (unlimited) for background subagents/workflows before the session closes.
os.environ.setdefault("CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS", "0")
# The SDK's async-subagent stall watchdog defaults to ~10 min of no stream events,
# which would kill a legitimately long, quiet experiment. Raise it so the host-side
# idle ceiling (CLAUDE_IDLE_CEILING_S, default 6h) is what actually catches hangs.
os.environ.setdefault("CLAUDE_ASYNC_AGENT_STALL_TIMEOUT_MS", str(6 * 3600 * 1000))

from claude_agent_sdk import (  # noqa: E402
    ClaudeSDKClient,
    ClaudeAgentOptions,
    query,
    AssistantMessage,
    UserMessage,
    SystemMessage,
    ResultMessage,
    RateLimitEvent,
    TERMINAL_TASK_STATUSES,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
    ToolResultBlock,
)

MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-4-8")
EFFORT = os.environ.get("CLAUDE_EFFORT", "max")
MAX_RESUMES = int(os.environ.get("CLAUDE_MAX_RESUMES", "5"))
JUDGE_MAX_LIMIT_WAITS = int(os.environ.get("CLAUDE_JUDGE_MAX_LIMIT_WAITS", "400"))  # ~8 days at 30 min
# A judge that errors or returns no parseable verdict is retried this many times before the
# attempt is marked incomplete -- a judge failure is not the agent's failure.
JUDGE_MAX_ERROR_RETRIES = int(os.environ.get("CLAUDE_JUDGE_MAX_ERROR_RETRIES", "3"))
# An overloaded model API (Anthropic 529, "at capacity") or a usage/rate limit is an infrastructure
# condition: wait and continue the SAME attempt without using up a reconnect. Any other session
# error counts as a reconnect; a run gives up after MAX_RECONNECTS of them (5, the same for every
# solver runner).
CAPACITY_MARKERS = ("overloaded", "at capacity")
EXC_LIMIT_MARKERS = ("session limit", "hit your", "usage limit", "rate limit", "rate_limit", "429")
CAPACITY_WAIT_S = int(os.environ.get("CLAUDE_CAPACITY_WAIT_S", "300"))
MAX_INFRA_WAITS = int(os.environ.get("CLAUDE_MAX_INFRA_WAITS", "400"))
GOAL_FILE = Path(__file__).resolve().parent / "goal.md"
UNLIMITED_TIMEOUT = 100 * 24 * 3600  # ~unlimited

INITIAL_PROMPT = "Read task_spec.md and complete the task it describes."
# A turn that ends with a scheduled wakeup (the ScheduleWakeup tool) is the agent waiting, not finishing: the runner
# sleeps until the wakeup is due and re-engages the SAME session without using an attempt (like a background task).
WAKE_MIN_S, WAKE_MAX_S = 60, 3600   # the tool's own clamp
# Restart of a run that already started (e.g. a harness update or a node failure): continue its Claude session with
# its attempt count and elapsed time, so the restart neither grants nor costs attempts. Same account and workspace.
RESUME_SESSION = os.environ.get("CLAUDE_RESUME_SESSION") or None
RESUME_ATTEMPT = int(os.environ.get("CLAUDE_RESUME_ATTEMPT", "1"))
PRIOR_ELAPSED_S = float(os.environ.get("CLAUDE_PRIOR_ELAPSED_S", "0"))
RESTART_NOTE = ("the run was restarted on a new machine (a harness update or an infrastructure failure), so every process you had running "
                "(background tasks, daemons, servers, experiments) was stopped. Check what finished, relaunch what "
                "still has to run, and continue where you left off")


def wakeup_prompt(prompt: str) -> str:
    prompt = (prompt or "").strip()
    return ("Your scheduled wakeup is due." + (f" Your note for it: {prompt}" if prompt else "")
            + " Continue the task in THIS session.")


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
# Cheap placeholder pre-filter (fast-fail before the LLM judge)
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
    """Fast, deterministic pre-check. Returns (incomplete: bool, reason: str)."""
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
# LLM completion judge (a fresh, independent session)
# ======================================================================
JUDGE_INSTRUCTIONS = (
    "You are a strict COMPLETENESS checker for an autonomous science agent's paper "
    "submission. You are given the task specification and the current LaTeX sources "
    "(report.tex and every file it \\input-s, including results-macro files). Decide "
    "whether the submission is COMPLETE, meaning: every deliverable and section the "
    "spec requires is present; every required metric/table/number is filled with a "
    "REAL value (not '--', 'XXX', blank, 'N/A', 'TBD', or any placeholder); no results "
    "macro is unfilled; figures referenced in the text exist as real content; and the "
    "paper reads as a finished result rather than a skeleton. Do NOT judge scientific "
    "correctness or quality -- only completeness. Reply with ONLY a JSON object on a "
    "single line: {\"complete\": true|false, \"blocking_issues\": [\"...\", ...]}. "
    "blocking_issues must be specific and actionable (name the missing section, the "
    "unfilled table/metric, the absent figure). If complete, use an empty list."
)


async def judge_completion(workspace: Path):
    """Independent LLM judge. Returns (complete: bool, issues: list[str])."""
    spec = ""
    sp = workspace / "task_spec.md"
    if sp.exists():
        spec = sp.read_text(errors="ignore")[:60000]
    report = _read_report_sources(workspace)
    prompt = (
        f"{JUDGE_INSTRUCTIONS}\n\n===== TASK SPECIFICATION =====\n{spec}\n\n"
        f"===== CURRENT REPORT SOURCES =====\n{report}\n\n"
        "Return the JSON verdict now."
    )
    opts = ClaudeAgentOptions(
        model=MODEL,
        permission_mode="bypassPermissions",
        # Everything the judge needs is in the prompt. tools=[] removes the tools entirely
        # (allowed_tools=[] alone does not under bypassPermissions): a judge that wandered the
        # workspace with tools once ran out of turns and crashed.
        tools=[],
        allowed_tools=[],
        max_turns=8,  # was 1: a reasoning model needs >1 turn to think + emit the JSON verdict;
                      # max_turns=1 errored "Reached maximum number of turns (1)" every time, so the
                      # judge never ran and complete papers were false-marked incomplete (endless resumes).
        system_prompt="You are a meticulous, skeptical completeness reviewer. Output only the requested JSON.",
    )
    # A subscription limit is an infra condition: wait for the reset and judge again
    # instead of marking the attempt incomplete. Any other judge failure (an error, no
    # verdict, malformed JSON) is retried a few times before it counts against the attempt.
    limit_markers = ("session limit", "hit your", "usage limit")
    limit_waits = failures = 0
    while True:
        text, err = "", None
        try:
            async for msg in query(prompt=prompt, options=opts):
                if isinstance(msg, AssistantMessage):
                    for b in msg.content:
                        if isinstance(b, TextBlock):
                            text += b.text
                elif isinstance(msg, ResultMessage):
                    if getattr(msg, "result", None):
                        text = text or msg.result
        except Exception as e:  # judge failure must not crash the run
            err = e
        probe = (f"{err!r} " if err else "") + (text if not re.search(r"\{.*\}", text, re.DOTALL) else "")
        if any(k in probe.lower() for k in limit_markers) and limit_waits < JUDGE_MAX_LIMIT_WAITS:
            limit_waits += 1
            _safe_print("[judge] subscription limit; sleeping 1800s, then judging again "
                        "-- NOT consuming an attempt.")
            await asyncio.sleep(1800)
            continue
        if any(k in probe.lower() for k in CAPACITY_MARKERS) and limit_waits < JUDGE_MAX_LIMIT_WAITS:
            limit_waits += 1
            _safe_print(f"[judge] model API overloaded; sleeping {CAPACITY_WAIT_S}s, then judging again "
                        "-- NOT consuming an attempt.")
            await asyncio.sleep(CAPACITY_WAIT_S)
            continue
        v, problem = None, None
        if err is not None:
            problem = f"completion judge failed to run ({err!r})"
        else:
            m = re.search(r"\{.*\}", text, re.DOTALL)
            if not m:
                problem = f"completion judge returned no verdict ({text[:200]!r})"
            else:
                try:
                    v = json.loads(m.group(0))
                except json.JSONDecodeError:
                    problem = "completion judge returned malformed JSON"
        if problem is None:
            break
        if failures < JUDGE_MAX_ERROR_RETRIES:
            failures += 1
            _safe_print(f"[judge] {problem}; judging again ({failures}/{JUDGE_MAX_ERROR_RETRIES}) "
                        f"-- NOT consuming an attempt.")
            await asyncio.sleep(60)
            continue
        _safe_print(f"[judge] {problem} after {failures} retries -- treating as INCOMPLETE-unknown")
        return False, [f"{problem}; re-verify the paper is complete"]
    complete = bool(v.get("complete"))
    issues = [str(x) for x in (v.get("blocking_issues") or [])]
    return complete, issues


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
# Trajectory capture + live logging
# ======================================================================
def _short(v, n=300):
    s = v if isinstance(v, str) else json.dumps(v, default=str)
    s = s.replace("\n", " | ")
    return s if len(s) <= n else s[:n] + "..."


def _block_to_dict(b):
    if isinstance(b, TextBlock):
        return {"type": "text", "text": b.text}
    if isinstance(b, ThinkingBlock):
        return {"type": "thinking", "thinking": b.thinking}
    if isinstance(b, ToolUseBlock):
        return {"type": "tool_use", "id": b.id, "name": b.name, "input": b.input}
    if isinstance(b, ToolResultBlock):
        return {"type": "tool_result", "tool_use_id": b.tool_use_id,
                "is_error": bool(b.is_error), "content": b.content}
    return {"type": "other", "repr": str(b)[:500]}


def record(events, start, msg):
    t = round(time.monotonic() - start, 3)
    if isinstance(msg, AssistantMessage):
        ev = {"kind": "assistant", "model": msg.model,
              "stop_reason": msg.stop_reason,
              "content": [_block_to_dict(b) for b in msg.content]}
        for b in msg.content:
            if isinstance(b, ThinkingBlock):
                _safe_print(f"[{t}] 🧠 {_short(b.thinking, 160)}")
            elif isinstance(b, TextBlock) and b.text.strip():
                _safe_print(f"[{t}] 💬 {_short(b.text, 200)}")
            elif isinstance(b, ToolUseBlock):
                _safe_print(f"[{t}] → {b.name}: {_short(b.input, 160)}")
    elif isinstance(msg, UserMessage):
        content = msg.content if isinstance(msg.content, list) else [msg.content]
        ev = {"kind": "user", "content": [_block_to_dict(b) for b in content if not isinstance(b, str)]}
        for b in content:
            if isinstance(b, ToolResultBlock):
                mark = "✗" if b.is_error else "✓"
                _safe_print(f"[{t}]   {mark} {_short(b.content, 160)}")
    elif isinstance(msg, SystemMessage):
        ev = {"kind": "system", "subtype": msg.subtype, "data": msg.data}
        if msg.subtype == "init":
            _safe_print(f"[{t}] [init] session={str(msg.data.get('session_id',''))[:8]}")
    elif isinstance(msg, ResultMessage):
        ev = {"kind": "result", "subtype": msg.subtype, "is_error": msg.is_error,
              "num_turns": msg.num_turns, "total_cost_usd": msg.total_cost_usd}
        _safe_print(f"[{t}] [result] turn done: is_error={msg.is_error} num_turns={msg.num_turns}")
    else:
        ev = {"kind": "other", "repr": str(msg)[:500]}
    events.append({"t_offset": t, "event": ev})


# ======================================================================
# Workspace setup
# ======================================================================
def setup_workspace(workspace: Path):
    if not workspace.is_dir():
        sys.exit(f"[run] workspace is not a directory: {workspace}")
    if not (workspace / "task_spec.md").exists():
        sys.exit(f"[run] missing task_spec.md in {workspace}")
    # stage neurips.sty next to the report if the repo provides one
    src_sty = Path(__file__).resolve().parent / "neurips.sty"
    ws_sty = workspace / "neurips.sty"
    if src_sty.exists() and not ws_sty.exists():
        shutil.copy2(src_sty, ws_sty)


def check_auth():
    if BYOK:
        if not (os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY")):
            sys.exit("[run] SOLVER_ANTHROPIC_BYOK=1 but neither ANTHROPIC_API_KEY nor "
                     "ANTHROPIC_AUTH_TOKEN is set.")
        return
    if os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("[run] ANTHROPIC_API_KEY is set; it would shadow the subscription OAuth token.")
    creds = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / ".credentials.json"
    if not (os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or creds.exists()):
        sys.exit("[run] no OAuth token found (run `claude setup-token` or `claude` login).")


# ======================================================================
# Main persistent-session loop
# ======================================================================
async def run(args):
    workspace = Path(args.workspace).resolve()
    setup_workspace(workspace)
    check_auth()

    timeout = UNLIMITED_TIMEOUT if args.unlimited else (args.hours * 3600 if args.hours else UNLIMITED_TIMEOUT)
    start = time.monotonic()
    deadline = start + timeout

    goal_present = GOAL_FILE.exists()

    def build_options(resume_sid):
        ea = {}
        if goal_present:
            ea["append-system-prompt-file"] = str(GOAL_FILE)
        sub_env = {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}
        if BYOK:
            # The token was popped in subscription mode; in BYOK mode it IS the
            # credential — forward it (and the base URL) so the SDK subprocess
            # actually sends an Authorization header to the custom endpoint.
            # Only non-empty values are forwarded (an empty base URL would
            # override the default endpoint).
            for k in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"):
                if os.environ.get(k):
                    sub_env[k] = os.environ[k]
        return ClaudeAgentOptions(
            model=MODEL,
            effort=EFFORT,
            permission_mode="bypassPermissions",
            cwd=str(workspace),
            system_prompt={"type": "preset", "preset": "claude_code"},
            extra_args=ea,
            env=sub_env,
            max_buffer_size=64 * 1024 * 1024,
            resume=resume_sid,
        )

    # Host-side safeguards the SDK does not enforce itself:
    #  IDLE_CEILING  -- if a background task goes silent this long, treat it as hung.
    #  MAX_RECONNECTS -- reconnect+resume this many times across rate-limit/API errors.
    IDLE_CEILING = int(os.environ.get("CLAUDE_IDLE_CEILING_S", str(6 * 3600)))
    MAX_RECONNECTS = int(os.environ.get("CLAUDE_MAX_RECONNECTS", "5"))
    POLL = int(os.environ.get("CLAUDE_POLL_S", "900"))  # how often to wake to re-check deadline/hang

    events = streaming_events(workspace, {
        "runner": "agent-sdk", "model": MODEL, "effort": EFFORT,
        "workspace": str(workspace), "max_resumes": MAX_RESUMES,
    })
    install_crash_saver(events)   # a batch-job walltime kill / crash still leaves a trajectory
    # The agent's experiments share the job's memory limit with the runner and the harness: keep them from ending the run.
    use_disk_tmpdir()
    start_memory_watchdog(workspace, on_event=lambda kind, info: events.append(
        {"t_offset": round(time.monotonic() - start, 3), "event": {"kind": "harness", "event": "memory_" + kind, **info}}))
    complete = False
    reason = RESTART_NOTE if RESUME_SESSION else ""
    attempt = RESUME_ATTEMPT if RESUME_SESSION else 1
    session_id = RESUME_SESSION
    timed_out = False
    active_bg = {}       # task_id -> description of CURRENTLY-RUNNING background tasks
    rate_reset_at = 0.0  # unix epoch of the next rate-limit reset, if known
    reconnects = 0
    limit_waits = 0  # subscription session-limit stalls absorbed WITHOUT consuming attempts
    infra_waits = 0  # overloaded-API / limit errors absorbed WITHOUT consuming reconnects or attempts
    wake_waits = 0   # turns that ended on a scheduled wakeup, waited out WITHOUT consuming an attempt
    wake_at, wake_note, waiting_wake = None, "", False
    # Resuming an interrupted session can first return an empty result (0 turns) before the agent has done anything:
    # that is the harness reconnecting, not the agent ending a turn. Re-send the prompt instead of judging it.
    last_prompt, agent_active, empty_resends = None, False, 0

    async def send(client, prompt):
        nonlocal last_prompt, agent_active
        prompt += pending_notices()   # what the memory watchdog did since the last prompt, if anything
        last_prompt, agent_active = prompt, False
        await client.query(prompt)

    def note(**kw):
        events.append({"t_offset": round(time.monotonic() - start, 3),
                       "event": {"kind": "harness", **kw}})
        live = {k: kw[k] for k in ("attempt", "reason") if k in kw}
        if live:
            events.note_meta(**live)

    if RESUME_SESSION:
        note(attempt=attempt, resume=True, reason="runner restarted", restarted_session=RESUME_SESSION,
             prior_elapsed_s=PRIOR_ELAPSED_S)
        _safe_print(f"\n[run] === Attempt {attempt} (continued after a runner restart; session {RESUME_SESSION[:8]}) ===")
    else:
        note(attempt=1, resume=False, reason=None)
        _safe_print("\n[run] === Attempt 1 (initial) ===")

    # Outer loop reconnects+resumes across transient/rate-limit errors so a mid-run
    # 5-hour-limit hit doesn't lose the whole paper. Inner loop drains one session.
    while not complete and not timed_out:
        fresh = session_id is None
        try:
            async with ClaudeSDKClient(options=build_options(session_id)) as client:
                active_bg = {}  # a fresh process has no live background tasks
                if fresh:
                    await send(client, INITIAL_PROMPT)
                else:
                    _safe_print(f"[run] {'restarted' if reason == RESTART_NOTE else 'reconnected'} "
                                f"(resume {session_id[:8]}); nudging to continue.")
                    await send(client, resume_prompt("(reconnected after an interruption) "
                                                     + (reason or "continue where you left off")))
                last_activity = time.monotonic()
                # receive_messages() keeps the session open ACROSS turns: when the
                # agent ends a turn with a background experiment still running, the
                # SDK auto-re-invokes it on completion, so we finalize only when the
                # agent is idle AND no background task is running.
                it = client.receive_messages().__aiter__()
                pending = None
                while True:
                    if time.monotonic() >= deadline:
                        timed_out = True
                        _safe_print("[run] deadline reached; stopping.")
                        break
                    remaining = deadline - time.monotonic()
                    # Keep ONE outstanding receive as a task and poll it with
                    # asyncio.wait, which (unlike asyncio.wait_for) does NOT cancel
                    # the task on timeout. Cancelling it would break the async
                    # generator and end the stream -- finalizing prematurely while a
                    # long background experiment is still running (the paper-1 bug).
                    if pending is None:
                        pending = asyncio.ensure_future(it.__anext__())
                    tmo = min(remaining, POLL)
                    if waiting_wake:
                        tmo = min(tmo, max(1.0, wake_at - time.time()))
                    done, _ = await asyncio.wait({pending}, timeout=tmo)
                    if not done:
                        if waiting_wake and time.time() >= wake_at:
                            waiting_wake, wake_at = False, None
                            note(event="scheduled_wakeup_due", n=wake_waits)
                            _safe_print("[run] scheduled wakeup due; re-engaging the agent in the same attempt.")
                            await send(client, wakeup_prompt(wake_note))
                            last_activity = time.monotonic()
                            continue
                        # quiet stretch; if a background task has been silent past the
                        # ceiling, treat it as hung: stop it and re-engage the agent.
                        if active_bg and (time.monotonic() - last_activity) > IDLE_CEILING:
                            stuck = list(active_bg.keys())
                            _safe_print(f"[run] background task(s) {stuck} silent > {IDLE_CEILING}s; "
                                        f"stopping as hung and re-engaging the agent.")
                            for tid in stuck:
                                try:
                                    await client.stop_task(tid)
                                except Exception as e:
                                    _safe_print(f"[run]   stop_task({tid}) failed: {e!r}")
                            active_bg = {}
                            note(event="hung_task_stopped", tasks=stuck)
                            await send(client, resume_prompt(
                                "A background task stalled with no activity for a long time and was "
                                "stopped. Finalize with the results you already have, or re-run that "
                                "experiment; either way log it honestly in proposal/attempts_log.md."))
                            last_activity = time.monotonic()
                        continue
                    try:
                        msg = pending.result()
                    except StopAsyncIteration:
                        pending = None
                        _safe_print("[run] session stream ended.")
                        break
                    pending = None
                    last_activity = time.monotonic()

                    if isinstance(msg, RateLimitEvent):
                        info = getattr(msg, "rate_limit_info", None)
                        ra = getattr(info, "resets_at", None) if info else None
                        if ra:
                            rate_reset_at = float(ra)
                    if isinstance(msg, SystemMessage):
                        if msg.subtype == "init":
                            session_id = (msg.data or {}).get("session_id", session_id)
                            events.note_meta(session_id=session_id)
                        elif msg.subtype == "background_tasks_changed":
                            active_bg = {t.get("task_id"): t.get("description")
                                         for t in ((msg.data or {}).get("tasks") or [])}
                        elif msg.subtype in ("task_notification", "task_updated"):
                            d = msg.data or {}
                            tid = d.get("task_id")
                            st = d.get("status") or (d.get("patch") or {}).get("status")
                            if tid and st in TERMINAL_TASK_STATUSES:
                                active_bg.pop(tid, None)
                        elif msg.subtype == "thinking_tokens":
                            continue  # pure token-estimate noise; don't record

                    record(events, start, msg)

                    if isinstance(msg, AssistantMessage):
                        agent_active, empty_resends = True, 0
                        if waiting_wake:   # the agent is active again before the wakeup was due
                            waiting_wake, wake_at = False, None
                        for b in (msg.content or []):
                            if isinstance(b, ToolUseBlock) and b.name == "ScheduleWakeup":
                                inp = b.input if isinstance(b.input, dict) else {}
                                if inp.get("stop"):
                                    wake_at = None
                                else:
                                    try:
                                        d = float(inp.get("delaySeconds") or 0)
                                    except (TypeError, ValueError):
                                        d = 0.0
                                    wake_at = time.time() + min(max(d, WAKE_MIN_S), WAKE_MAX_S)
                                    wake_note = str(inp.get("prompt") or "")

                    if not isinstance(msg, ResultMessage):
                        continue

                    # --- a turn ended ---
                    if not agent_active and (getattr(msg, "num_turns", 1) or 0) == 0 and last_prompt and empty_resends < 3:
                        empty_resends += 1
                        note(event="empty_result_resend", n=empty_resends)
                        _safe_print(f"[run] empty result (0 turns) before any agent activity; re-sending the prompt "
                                    f"(#{empty_resends}) -- NOT consuming an attempt.")
                        await send(client, last_prompt)
                        continue
                    # FAIRNESS GUARD: a subscription session-limit is an infra
                    # condition, not a model failure. Sleep to the reset and
                    # re-engage the SAME attempt; never burn wiggum budget on it.
                    _err_text = str(getattr(msg, "result", "") or "").lower()
                    # NOTE: the limit can arrive as a SUCCESS-subtype turn whose
                    # entire reply is the limit banner -- match on text alone.
                    if ("session limit" in _err_text or "hit your" in _err_text
                            or "usage limit" in _err_text):
                        limit_waits += 1
                        wait = 1800.0
                        if rate_reset_at and rate_reset_at > time.time():
                            wait = min(rate_reset_at - time.time() + 60, 3 * 3600)
                        note(event="session_limit_wait", n=limit_waits, wait_s=int(wait))
                        _safe_print(f"[run] session limit (wait #{limit_waits}); sleeping "
                                    f"{int(wait)}s -- NOT consuming an attempt.")
                        await asyncio.sleep(wait)
                        await send(client, resume_prompt(
                            "(session limit reset) continue exactly where you left off"))
                        continue
                    # Same for a turn that ended on an overloaded model API.
                    if (getattr(msg, "is_error", False) and any(m in _err_text for m in CAPACITY_MARKERS)
                            and infra_waits < MAX_INFRA_WAITS):
                        infra_waits += 1
                        note(event="capacity_wait", n=infra_waits, wait_s=CAPACITY_WAIT_S)
                        _safe_print(f"[run] model API overloaded (wait #{infra_waits}); sleeping "
                                    f"{CAPACITY_WAIT_S}s -- NOT consuming an attempt.")
                        await asyncio.sleep(CAPACITY_WAIT_S)
                        await send(client, resume_prompt(
                            "(the model API was overloaded) continue exactly where you left off"))
                        continue
                    if msg.subtype and msg.subtype != "success":
                        _safe_print(f"[run] turn ended non-success (subtype={msg.subtype}).")
                    if active_bg:
                        _safe_print(f"[run] turn ended, {len(active_bg)} background task(s) still running "
                                    f"({', '.join(str(v) for v in list(active_bg.values())[:3])}); "
                                    f"waiting for auto-re-invoke, NOT finalizing.")
                        continue
                    if wake_at is not None:
                        waiting_wake = True
                        wake_waits += 1
                        delay = max(0, int(wake_at - time.time()))
                        note(event="scheduled_wakeup_wait", n=wake_waits, wait_s=delay)
                        _safe_print(f"[run] turn ended with a scheduled wakeup in {delay}s; "
                                    f"waiting for it, NOT finalizing.")
                        continue

                    inc, why = cheap_incomplete(workspace)
                    if inc:
                        reason = why
                        _safe_print(f"[run] attempt {attempt}: INCOMPLETE (pre-check) -- {why}")
                    else:
                        _safe_print(f"[run] attempt {attempt}: pre-check clean; running completion judge...")
                        jc, issues = await judge_completion(workspace)
                        if not jc and issues and any(
                                ("session limit" in str(i).lower() or "hit your" in str(i).lower())
                                for i in issues):
                            limit_waits += 1
                            _safe_print(f"[run] judge hit session limit (wait #{limit_waits}); "
                                        "sleeping 1800s and retrying judge -- NOT consuming an attempt.")
                            await asyncio.sleep(1800)
                            jc, issues = await judge_completion(workspace)
                        if jc:
                            complete = True
                            reason = "complete"
                            events.note_meta(completed=True, completion_reason="complete")
                            _safe_print("[run] judge: COMPLETE")
                            break
                        reason = "; ".join(issues) if issues else "judge flagged the paper incomplete"
                        _safe_print(f"[run] judge: INCOMPLETE -- {_short(reason, 240)}")

                    if attempt >= 1 + MAX_RESUMES:
                        _safe_print(f"[run] reached resume cap ({MAX_RESUMES}); accepting current state.")
                        break
                    if time.monotonic() >= deadline:
                        timed_out = True
                        break
                    attempt += 1
                    note(attempt=attempt, resume=True, reason=reason)
                    _safe_print(f"\n[run] === Attempt {attempt} (resume: {_short(reason, 120)}) ===")
                    await send(client, resume_prompt(reason))
                if pending is not None and not pending.done():
                    pending.cancel()  # cancel a dangling receive only when leaving the session
            # inner session ended cleanly (complete / cap / deadline / stream-end)
            break
        except asyncio.CancelledError:
            raise
        except Exception as e:
            etext = f"{type(e).__name__} {e}".lower()
            overloaded = any(m in etext for m in CAPACITY_MARKERS)
            limited = any(m in etext for m in EXC_LIMIT_MARKERS)
            now_epoch = time.time()
            if (overloaded or limited) and infra_waits < MAX_INFRA_WAITS and time.monotonic() < deadline:
                infra_waits += 1
                if limited and rate_reset_at and rate_reset_at > now_epoch:
                    wait = min(rate_reset_at - now_epoch + 15, 3 * 3600)
                else:
                    wait = CAPACITY_WAIT_S if overloaded else 1800
                kind = "model API overloaded" if overloaded else "usage/rate limit"
                note(event="infra_wait", kind=kind, error=repr(e)[:300], n=infra_waits, wait_s=int(wait))
                _safe_print(f"[run] {kind} (wait #{infra_waits}): {e!r}; sleeping {int(wait)}s "
                            f"-- NOT consuming a reconnect or an attempt.")
            else:
                reconnects += 1
                note(event="session_error", error=repr(e)[:300], reconnect=reconnects)
                _safe_print(f"[run] session error ({reconnects}/{MAX_RECONNECTS}): {e!r}")
                if session_id is None or reconnects > MAX_RECONNECTS or time.monotonic() >= deadline:
                    _safe_print("[run] not reconnecting (no-session/cap/deadline); finalizing with current state.")
                    break
                if rate_reset_at and rate_reset_at > now_epoch:
                    wait = min(rate_reset_at - now_epoch + 15, 3 * 3600)
                    _safe_print(f"[run] rate-limited; waiting {int(wait)}s for reset, then resuming {session_id[:8]}.")
                else:
                    wait = min(30 * reconnects, 900)
                    _safe_print(f"[run] backing off {wait}s, then resuming {session_id[:8]}.")
            try:
                await asyncio.sleep(wait)
            except asyncio.CancelledError:
                raise
            # loop back: reconnect with resume=session_id

    elapsed = time.monotonic() - start + PRIOR_ELAPSED_S   # a restarted run counts its time before the restart
    metadata = {
        "model": MODEL,
        "effort": EFFORT,
        "runner": "agent-sdk",
        "session_id": session_id,
        "attempts": attempt,
        "max_resumes": MAX_RESUMES,
        "reconnects": reconnects,
        "infra_waits": infra_waits,
        "wakeup_waits": wake_waits,
        "restarted_session": RESUME_SESSION,
        "prior_elapsed_seconds": PRIOR_ELAPSED_S or None,
        "completed": complete,
        "completion_reason": reason,
        "timed_out": timed_out,
        "elapsed_seconds": round(elapsed, 1),
    }
    out = events.finalize(metadata)   # idempotent; the crash saver may already have written it
    _safe_print(f"\n[run] Trajectory: {out} ({len(events)} events)")
    _safe_print(f"[run] Status: {'COMPLETE' if complete else ('TIMED OUT' if timed_out else 'INCOMPLETE (cap)')} "
                f"in {elapsed:.0f}s over {attempt} attempt(s)")
    return 0 if complete else 1


def main():
    ap = argparse.ArgumentParser(description="Claude Agent SDK runner for one benchmark task.")
    ap.add_argument("--workspace", required=True,
                    help="Paper directory containing task_spec.md (the agent runs here).")
    ap.add_argument("--unlimited", action="store_true")
    ap.add_argument("--hours", type=float, default=None)
    args = ap.parse_args()
    try:
        rc = asyncio.run(run(args))
    except KeyboardInterrupt:
        rc = 130
    sys.exit(rc)


if __name__ == "__main__":
    main()
