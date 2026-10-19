#!/usr/bin/env python3
"""Run the OpenHands software-agent-sdk (LocalConversation, no docker) against a
served OpenAI-compatible endpoint on one benchmark task, with the same
completion/resume/judge loop and prompts as the other runners.

Used for GLM-5.3 (per-model harnesses: kimi-code for Kimi, deepseek-harness for
DeepSeek, OpenHands SDK for GLM).

Contract parity:
* workspace staging, initial prompt, timeout resolution: run_claude_code (rcc)
* completion gate: rcc._completion_status (placeholder regex over resolved tex)
  THEN the same-model LLM judge from run_served_harness (each model judges its
  own paper via its own serve endpoint)
* resume: same persisted OpenHands conversation (full context) re-engaged with
  the reason-interpolating resume prompt; cap SOLVER_SERVED_MAX_RESUMES (default 5)
* trajectory: ai_scientist_trajectory.json via rcc.save_trajectory

Env: SOLVER_SERVED_BASE_URL (required), SOLVER_SERVED_MODEL (served model id, e.g.
zai-org/GLM-5.3), SOLVER_SERVED_API_KEY, SOLVER_SERVED_MAX_RESUMES.
Continuing an interrupted run: SOLVER_SERVED_FIRST_ATTEMPT=<n> (and optionally
SOLVER_SERVED_PRIOR_TRAJECTORY=<recovered trajectory json>).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import run_claude_code as rcc
import run_served_harness as rsh  # judge_completion + resume_prompt
from memory_watchdog import use_disk_tmpdir, start_memory_watchdog, pending_notices

from pydantic import SecretStr
from openhands.sdk import LLM, Agent, Conversation, Tool, LLMSummarizingCondenser
from openhands.tools.terminal import TerminalTool
from openhands.tools.file_editor import FileEditorTool

BASE_URL = os.environ.get("SOLVER_SERVED_BASE_URL", "")
SERVED_MODEL = os.environ.get("SOLVER_SERVED_MODEL", "zai-org/GLM-5.3")
MAX_RESUMES = int(os.environ.get("SOLVER_SERVED_MAX_RESUMES", str(rcc.MAX_RESUMES)))
# Continuing a cell whose job was killed mid-attempt (batch-job walltime): number attempts from
# where it stopped and tell the agent it was interrupted, instead of re-sending the initial
# task prompt into the resumed conversation. Default 1 = original behaviour.
FIRST_ATTEMPT = int(os.environ.get("SOLVER_SERVED_FIRST_ATTEMPT", "1"))
PRIOR_TRAJECTORY = os.environ.get("SOLVER_SERVED_PRIOR_TRAJECTORY")  # recovered file: its runner markers are kept
CONTINUE_MSG = ("The run was interrupted by the cluster's job time limit and has been restarted in this "
                "same conversation. Continue exactly where you left off with the request above.")


def main():
    ap = argparse.ArgumentParser(description="OpenHands SDK runner for one benchmark task (served endpoint).")
    ap.add_argument("--workspace", required=True,
                    help="Paper directory containing task_spec.md (the agent runs here).")
    ap.add_argument("--paper-number", default=None)
    ap.add_argument("--model", default=SERVED_MODEL)
    ap.add_argument("--base-url", default=BASE_URL)
    ap.add_argument("--max-resumes", type=int, default=MAX_RESUMES)
    tg = ap.add_mutually_exclusive_group()
    tg.add_argument("--hours", type=int, default=None)
    tg.add_argument("--unlimited", action="store_true")
    args = ap.parse_args()

    if not args.base_url:
        sys.exit("[run] SOLVER_SERVED_BASE_URL / --base-url required")

    workspace = rcc.setup_workspace(args.workspace)
    prompt = rcc.build_prompt(workspace, hours=args.hours, unlimited=args.unlimited)
    timeout = rcc.resolve_timeout(hours=args.hours, unlimited=args.unlimited)
    session_id = str(uuid.uuid4())
    use_disk_tmpdir()   # before the agent's terminal starts, so its commands inherit it

    print(f"[run] Harness:   openhands-sdk (LocalConversation)", flush=True)
    print(f"[run] Model:     {args.model} (served)", flush=True)
    print(f"[run] Endpoint:  {args.base_url}", flush=True)
    print(f"[run] Workspace: {workspace}", flush=True)
    print(f"[run] Paper:     {args.paper_number}", flush=True)
    print(f"[run] Timeout:   {timeout}s ({rcc._format_elapsed(timeout)})", flush=True)

    llm = LLM(model=f"openai/{args.model}", api_key=SecretStr(os.environ.get("SOLVER_SERVED_API_KEY","sk-local")),
              base_url=args.base_url, timeout=1800, num_retries=5)
    condenser = LLMSummarizingCondenser(
        llm=llm.model_copy(update={"usage_id": "condenser"}), max_size=200, keep_first=3
    )
    agent = Agent(llm=llm, tools=[Tool(name=TerminalTool.name), Tool(name=FileEditorTool.name)], condenser=condenser)

    trajectory_events: list = []
    start_wall = time.monotonic()
    start_iso = datetime.now(timezone.utc).isoformat()
    deadline = start_wall + timeout

    def on_event(event):
        try:
            d = event.model_dump(mode="json")
        except Exception:
            d = {"repr": repr(event)[:2000]}
        line = str(d)[:4000]
        trajectory_events.append({
            "t_offset": round(time.monotonic() - start_wall, 3),
            "parsed": {"type": "openhands", "event": event.__class__.__name__},
            "raw_line": line,
        })
        rcc._safe_print(f"[{event.__class__.__name__}] {line[:300]}")

    # Deterministic per-workspace id: a relaunched process resumes the SAME
    # persisted conversation (continuing event indices) instead of opening a
    # fresh one — required for a complete native trajectory across restarts.
    conv_id = uuid.uuid5(uuid.NAMESPACE_URL, str(workspace.resolve()))
    persist_dir = str(workspace / ".oh_conversations")
    conversation = Conversation(agent=agent, workspace=str(workspace),
                                persistence_dir=persist_dir, conversation_id=conv_id,
                                callbacks=[on_event], max_iteration_per_run=1000000)

    ev_dirs = [Path(persist_dir) / conv_id.hex / "events", Path(persist_dir) / str(conv_id) / "events"]
    prior_markers = []
    if FIRST_ATTEMPT > 1:
        if not any(any(d.glob("*.json")) for d in ev_dirs if d.is_dir()):
            sys.exit(f"[run] SOLVER_SERVED_FIRST_ATTEMPT={FIRST_ATTEMPT} but no persisted conversation to continue")
        if PRIOR_TRAJECTORY:
            prior_markers = [e for e in json.loads(Path(PRIOR_TRAJECTORY).read_text())["events"]
                             if isinstance(e, dict) and e.get("runner_marker")]
    attempt = FIRST_ATTEMPT - 1
    complete = False
    reason = ""
    judge_verdict = None
    timed_out = False
    # The agent's experiments share the job's memory limit with the runner and the harness: keep them from ending the run.
    start_memory_watchdog(workspace, on_event=lambda kind, info: trajectory_events.append({
        "t_offset": round(time.monotonic() - start_wall, 3),
        "parsed": {"type": "harness", "event": "memory_" + kind, **info}, "raw_line": None}))
    try:
        while True:
            attempt += 1
            continuing = FIRST_ATTEMPT > 1 and attempt == FIRST_ATTEMPT
            this_prompt = (CONTINUE_MSG if continuing else (prompt if attempt == 1 else rsh.resume_prompt(reason))) \
                + pending_notices()
            trajectory_events.append({
                "t_offset": round(time.monotonic() - start_wall, 3),
                "parsed": {"type": "harness", "event": "attempt_start", "attempt": attempt,
                           "resume": attempt > 1, "reason": (reason if attempt > 1 else None),
                           "continued_after_interruption": continuing},
                "raw_line": None,
            })
            print(f"\n[run] Attempt {attempt} " +
                  ("(continued after interruption)" if continuing else "(initial)" if attempt == 1
                   else f"(resume same conversation) — previous stop was incomplete: {reason}"),
                  flush=True)
            conversation.send_message(this_prompt)
            try:
                conversation.run()
            except Exception as e:
                print(f"[run] conversation.run() raised {e!r}; checking completion anyway", flush=True)

            complete, reason = rcc._completion_status(workspace)
            if complete:
                print(f"[run] Placeholder check passed; asking the {args.model} judge...", flush=True)
                jc, issues = rsh.judge_completion(workspace, args.base_url, args.model)
                judge_verdict = {"complete": jc, "issues": issues}
                trajectory_events.append({
                    "t_offset": round(time.monotonic() - start_wall, 3),
                    "parsed": {"type": "harness", "event": "judge_verdict", "attempt": attempt, **judge_verdict},
                    "raw_line": None,
                })
                if not jc:
                    complete = False
                    reason = "; ".join(issues) if issues else "judge flagged the paper incomplete"
            print(f"[run] After attempt {attempt}: "
                  f"{'COMPLETE' if complete else 'INCOMPLETE -- ' + reason}", flush=True)

            if complete:
                break
            if time.monotonic() >= deadline:
                timed_out = True
                print("[run] Time budget exhausted; accepting the current submission.", flush=True)
                break
            if attempt >= 1 + args.max_resumes:
                print(f"[run] Reached resume cap ({args.max_resumes}); accepting the current "
                      f"(incomplete) submission.", flush=True)
                break
    finally:
        elapsed_seconds = time.monotonic() - start_wall
        # Serialize the FULL persisted conversation history (authoritative
        # OpenHands event store), not just events observed by this process.
        # A reservation kill mid-run loses in-process events; the persistence
        # dir survives and the resumed conversation appends to it, so dumping
        # it at save time yields a complete native trajectory for the cell.
        persisted_events = []
        try:
            # The SDK names the directory by the hex id; older code looked for str(conv_id).
            ev_dir = next((d for d in ev_dirs if d.is_dir()), ev_dirs[0])
            for f in sorted(ev_dir.glob("*.json")):
                try:
                    persisted_events.append(json.loads(f.read_text(errors="ignore")))
                except (OSError, ValueError):
                    persisted_events.append({"unreadable_event_file": f.name})
        except OSError:
            pass
        if persisted_events:
            # runner-level markers (attempt boundaries, judge verdicts) kept alongside
            trajectory_events = persisted_events + prior_markers + [
                {"runner_marker": True, **e} for e in trajectory_events
                if not isinstance(e, dict) or e.get("type") != "oh_event"
            ]
        metadata = {
            "runner": "run_openhands_sdk.py",
            "session_id": session_id,
            "harness": "openhands-sdk",
            "conversation_id": str(conv_id),
            "model": args.model,
            "served": True,
            "base_url": args.base_url,
            "paper_number": args.paper_number,
            "workspace": str(workspace),
            "timeout_seconds": timeout,
            "time_budget_hours": args.hours,
            "time_budget_unlimited": args.unlimited,
            "attempts": attempt,
            "first_attempt_this_process": FIRST_ATTEMPT,
            "continuation_message": CONTINUE_MSG if FIRST_ATTEMPT > 1 else None,
            "prior_trajectory": PRIOR_TRAJECTORY,
            "max_resumes": args.max_resumes,
            "completed": complete,
            "completion_reason": reason,
            "judge_verdict": judge_verdict,
            "timed_out": timed_out,
            "return_code": 0,
            "start_utc": start_iso,
            "end_utc": datetime.now(timezone.utc).isoformat(),
            "elapsed_seconds": round(elapsed_seconds, 3),
            "elapsed_human": rcc._format_elapsed(elapsed_seconds),
            "event_count": len(trajectory_events),
        }
        traj_path = rcc.save_trajectory(workspace, metadata, trajectory_events)
        print(f"\n[run] Elapsed:    {rcc._format_elapsed(elapsed_seconds)} over {attempt} attempt(s)", flush=True)
        if complete:
            print("[run] Status:     COMPLETE (report.pdf present, no placeholders, "
                  "same-model judge approved)", flush=True)
        else:
            print(f"[run] Status:     stopped INCOMPLETE ({reason})", flush=True)
        print(f"[run] Trajectory: {traj_path} ({len(trajectory_events)} events)", flush=True)


if __name__ == "__main__":
    main()
