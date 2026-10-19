#!/usr/bin/env python3
"""Run a *served* agentic coding harness (e.g. kimi-code driving Kimi-K3, or
deepseek-harness `dsh` driving DeepSeek-V4-Pro) on a single benchmark task, with
the same completion/resume loop and trajectory format as `run_claude_code.py`.

Why this exists
---------------
`run_claude_code.py` / `run_agent_sdk.py` drive Anthropic-hosted Claude. This
runner drives an *open* agent harness that talks to an **OpenAI-compatible
endpoint** (self-hosted or an API gateway) — the model brain runs behind the
endpoint, the harness + the paper's (often CPU-heavy) experiments run wherever
this script runs. That decoupling is the point: you can host the model on GPU
nodes and run this script (and the paper's compute) on a high-core CPU box, with
the two bridged by an SSH tunnel, so `--base-url` still points at
`http://127.0.0.1:<port>/v1` on both.

The workspace contract is identical to the Claude runner: the workspace already
contains `task_spec.md` (and any `code/` / `data/`); this runner stages
`neurips.sty`, launches the harness with "Read task_spec.md and complete the
task", and resumes until `proposal/report.pdf` is placeholder-free (or a cap).

Harness selection
-----------------
* `--harness kimi` (default): invokes `kimi -m <model> -p <prompt>` and resumes
  via `kimi -r <session_id> -p <prompt>` (session id scraped from stdout).
* `--harness-cmd '<tmpl>'`: any served OpenAI-compatible harness. The template
  is shlex-split; the tokens `{model}` and `{workspace}` are substituted, and the
  single token `{prompt}` is replaced by the full prompt as one argv element
  (so spaces/newlines in the prompt never get re-split).

Endpoint / auth
---------------
The harness reads its own config for the endpoint (kimi-code:
`~/.kimi-code/config.toml`). This runner does not rewrite that config; it only
*pre-flight-checks* that the endpoint is reachable (so a dropped tunnel fails
fast instead of hanging the agent). Point at a different endpoint with
`--base-url`, or skip the check with `--skip-health-check`.

Examples
--------
    # co-located with the model server
    python run_served_harness.py --workspace runs/kimi-k3/paper12 --paper-number 12

    # from a separate CPU box over an SSH tunnel (endpoint still localhost:31000)
    SOLVER_SERVED_BASE_URL=http://127.0.0.1:31000/v1 \
      python run_served_harness.py --workspace runs/kimi-k3/paper12 --paper-number 12
"""
from __future__ import annotations  # PEP-604 `str | None` annotations must stay lazy (some cluster nodes run py<3.10)

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Reuse the Claude runner's completion/resume machinery verbatim so the two
# runners agree on "done" (placeholder-free report.pdf), prompt wording, timeout
# resolution, workspace staging, and trajectory serialization. Importing it does
# not run main() (guarded by __main__); it only sets a few benign env defaults.
import run_claude_code as rcc
from memory_watchdog import use_disk_tmpdir, start_memory_watchdog, pending_notices

HARNESS_BIN = os.environ.get("SOLVER_HARNESS_BIN", os.path.expanduser("~/.kimi-code/bin/kimi"))
SERVED_MODEL = os.environ.get("SOLVER_SERVED_MODEL", "kimik3")
DEFAULT_BASE_URL = os.environ.get("SOLVER_SERVED_BASE_URL", "")  # empty => discover from harness config
# Local SGLang serves ignore the key ("sk-local"); remote API rows (kimi via
# gateway) export SOLVER_SERVED_API_KEY. Used for /models, judge, health check.
API_KEY = os.environ.get("SOLVER_SERVED_API_KEY", "sk-local")
MAX_RESUMES = int(os.environ.get("SOLVER_SERVED_MAX_RESUMES", str(rcc.MAX_RESUMES)))
TRAJECTORY_FILENAME = rcc.TRAJECTORY_FILENAME

# kimi-code prints this on exit; we scrape the session id to resume with context.
_SESSION_RE = re.compile(r"kimi\s+-r\s+(session_[0-9a-fA-F-]+)")


# ---------------------------------------------------------------------------
# LLM completion judge (parity with run_agent_sdk.py / run_codex_sdk.py):
# a fresh, tool-less session of the SAME served model judges its own paper.
# The cheap placeholder regex (rcc._completion_status) stays as the fast-fail
# pre-filter; the judge only runs once that passes.
# ---------------------------------------------------------------------------
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


def _read_report_sources(workspace: Path, cap: int = 60000) -> str:
    """report.tex plus every \\input-ed .tex, concatenated and capped (verbatim
    from run_agent_sdk.py, reusing rcc's resolver so 'sources' means the same)."""
    tex = workspace / "proposal" / "report.tex"
    if not tex.exists():
        return "(proposal/report.tex missing)"
    parts = []
    for src in rcc._resolve_tex_sources(tex):
        try:
            parts.append(f"% ==== {src.name} ====\n" + src.read_text(errors="ignore"))
        except OSError:
            pass
    blob = "\n\n".join(parts)
    return blob[:cap] + ("\n...[truncated]" if len(blob) > cap else "")


def _served_model_id(base_url: str, fallback: str) -> str:
    """The harness config uses an alias (e.g. 'dsv4'); the endpoint wants its
    real served id (e.g. 'deepseek-ai/DeepSeek-V4-Pro'). Ask /models.
    On multi-model gateways (OpenRouter) the first catalog entry is NOT ours:
    set SOLVER_SERVED_MODEL_ID to pin the exact id and skip the probe."""
    pinned = os.environ.get("SOLVER_SERVED_MODEL_ID", "")
    if pinned:
        return pinned
    try:
        req = urllib.request.Request(base_url.rstrip("/") + "/models",
                                     headers={"Authorization": f"Bearer {API_KEY}"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        return (data.get("data") or [{}])[0].get("id") or fallback
    except Exception:
        return fallback


# --- completion-judge HTTP budget -------------------------------------------
# The judge may emit up to max_tokens (24576) of reasoning+answer. On a
# self-hosted serve at ~13 tok/s that is ~30 min of generation, which used to
# exceed the hardcoded 1800 s socket timeout and score the paper INCOMPLETE
# (a self-hosted GLM-5.3 run once lost several attempts that way). Overridable per lane.
JUDGE_HTTP_TIMEOUT = int(os.environ.get("SOLVER_JUDGE_TIMEOUT_S", "5400"))
JUDGE_TRIES = int(os.environ.get("SOLVER_JUDGE_TRIES", "3"))


def judge_completion(workspace: Path, base_url: str, model_alias: str):
    """Independent same-model judge over the served OpenAI-compatible endpoint.
    Returns (complete: bool, issues: list[str]). Judge failure is treated as
    INCOMPLETE-unknown, matching the SDK runners."""
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
    payload = json.dumps({
        "model": _served_model_id(base_url, model_alias),
        "messages": [
            {"role": "system", "content": "You are a meticulous, skeptical completeness "
                                          "reviewer. Output only the requested JSON."},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 24576,
    }).encode()
    text = ""
    last_err = None
    for _try in range(JUDGE_TRIES):
        try:
            req = urllib.request.Request(
                base_url.rstrip("/") + "/chat/completions", data=payload,
                headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=JUDGE_HTTP_TIMEOUT) as r:
                resp = json.loads(r.read().decode("utf-8", "replace"))
            msg = (resp.get("choices") or [{}])[0].get("message", {})
            text = (msg.get("content") or "") or (msg.get("reasoning_content") or "")
            break
        except Exception as e:  # judge failure must not crash the run
            last_err = e
            rcc._safe_print(f"[judge] try {_try + 1}/{JUDGE_TRIES} failed after "
                            f"up to {JUDGE_HTTP_TIMEOUT}s: {e!r}")
            time.sleep(20)
    if not text:
        rcc._safe_print(f"[judge] error: {last_err!r} -- treating as INCOMPLETE-unknown")
        return False, [f"completion judge failed to run ({last_err!r}); re-verify manually"]
    v = _extract_judge_json(text)
    if v is None:
        rcc._safe_print(f"[judge] no parseable JSON in reply: {text[:200]!r}")
        return False, ["completion judge returned malformed JSON; re-verify the paper is complete"]
    complete = bool(v.get("complete"))
    issues = [str(x) for x in (v.get("blocking_issues") or [])]
    return complete, issues


def _extract_judge_json(text: str):
    """Robustly pull the {complete, blocking_issues} object out of a reasoning
    model's reply: reasoning models (GLM-5.3 especially) wrap the verdict in
    prose/thinking that may itself contain braces, which broke the old greedy
    `\\{.*\\}` extraction. Try, in order: fenced ```json blocks, then every
    balanced {...} span scanned right-to-left, preferring ones that mention
    "complete"."""
    candidates = []
    for m in re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL):
        candidates.append(m.group(1))
    starts = [i for i, c in enumerate(text) if c == "{"]
    for s in reversed(starts):
        depth = 0
        for e in range(s, len(text)):
            if text[e] == "{":
                depth += 1
            elif text[e] == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(text[s:e + 1])
                    break
    # prefer candidates that look like the verdict object
    candidates.sort(key=lambda c: ('"complete"' not in c, len(c)))
    for c in candidates:
        try:
            v = json.loads(c)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(v, dict) and "complete" in v:
            return v
    return None


def resume_prompt(reason: str) -> str:
    """Reason-interpolating resume nudge, verbatim from run_agent_sdk.py (the
    claude/gpt rounds), so judge blocking_issues reach the agent."""
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


# ---------------------------------------------------------------------------
# Endpoint discovery + pre-flight health check
# ---------------------------------------------------------------------------
def discover_base_url(harness: str) -> str:
    """Best-effort: read the served endpoint from the harness's own config so the
    pre-flight check tests the *same* URL the harness will use. kimi-code stores
    `[providers.<name>].base_url` in ~/.kimi-code/config.toml."""
    if DEFAULT_BASE_URL:
        return DEFAULT_BASE_URL
    if harness != "kimi":
        return ""
    cfg = Path.home() / ".kimi-code" / "config.toml"
    if not cfg.exists():
        return ""
    try:
        import tomllib
        data = tomllib.loads(cfg.read_text())
        providers = data.get("providers", {})
        # prefer the provider backing the default_model, else the first one
        default_model = data.get("default_model")
        model_provider = (data.get("models", {}).get(default_model, {}) or {}).get("provider")
        prov = providers.get(model_provider) or (next(iter(providers.values()), {}) if providers else {})
        return prov.get("base_url", "") if isinstance(prov, dict) else ""
    except Exception:
        # tomllib missing (<3.11) or malformed config: fall back to a line scan.
        m = re.search(r'base_url\s*=\s*"([^"]+)"', cfg.read_text())
        return m.group(1) if m else ""


def health_check(base_url: str, timeout: float = 10.0) -> tuple[bool, str]:
    """GET <base_url>/models. Returns (ok, detail). A served OpenAI-compatible
    endpoint answers /models even without a real key; a dropped tunnel refuses
    the connection, which is exactly what we want to catch before launching."""
    if not base_url:
        return False, "no base_url discovered (pass --base-url or --skip-health-check)"
    url = base_url.rstrip("/") + "/models"
    try:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {API_KEY}"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(4096).decode("utf-8", "replace")
            return True, f"HTTP {r.status} from {url}: {body[:200]}"
    except Exception as e:
        return False, f"{type(e).__name__} contacting {url}: {e}"


# ---------------------------------------------------------------------------
# Harness invocation
# ---------------------------------------------------------------------------
def build_harness_argv(harness: str, harness_cmd: str, *, model: str, workspace: Path,
                       prompt: str, resume_session: str | None) -> list[str]:
    """Return the argv to launch the served harness for one attempt."""
    if harness_cmd:
        argv: list[str] = []
        for tok in shlex.split(harness_cmd):
            if tok == "{prompt}":
                argv.append(prompt)               # keep the prompt a single arg
            else:
                argv.append(tok.replace("{model}", model).replace("{workspace}", str(workspace)))
        return argv
    if harness == "qwen":
        # qwen-code (gemini-cli fork): headless one-shot with full autonomy.
        # Endpoint/model come from OPENAI_BASE_URL / OPENAI_API_KEY / OPENAI_MODEL env.
        if resume_session:
            return [HARNESS_BIN, "--resume", resume_session, "-p", prompt, "--approval-mode", "yolo"]
        return [HARNESS_BIN, "-p", prompt, "--approval-mode", "yolo"]
    if harness == "dsh":
        # deepseek-harness: headless profile; endpoint via DEEPSEEK_BASE_URL /
        # DEEPSEEK_API_KEY env; model/endpoint/bash-timeout pinned by the patch
        # overlay in $SOLVER_DSH_PATCH (the default model alias 'deepseek-v4-flash'
        # doesn't exist on our serve). Resume: fresh session each attempt
        # (workspace state + the reason-carrying resume prompt provide continuity).
        argv = [HARNESS_BIN, "--profile", "headless"]
        patch = os.environ.get("SOLVER_DSH_PATCH", "")
        if patch:
            argv += ["--patch", patch]
        return argv + [prompt]
    # built-in kimi-code
    if resume_session:
        return [HARNESS_BIN, "-r", resume_session, "-p", prompt]
    return [HARNESS_BIN, "-m", model, "-p", prompt]


def discover_session(harness: str, captured: str) -> str | None:
    """Best-effort session id for contextful resume, per harness."""
    if harness == "kimi":
        m = _SESSION_RE.search(captured)
        return m.group(1) if m else None
    if harness == "qwen":
        # newest session dir under $QWEN_HOME/sessions/<project>/<session-id>/
        root = Path(os.environ.get("QWEN_HOME", os.path.expanduser("~/.qwen"))) / "sessions"
        try:
            cands = [d for p in root.iterdir() if p.is_dir() for d in p.iterdir()]
            newest = max(cands, key=lambda d: d.stat().st_mtime)
            return newest.stem
        except (OSError, ValueError):
            return None
    return None  # dsh: fresh-session resumes


def run_harness(argv, cwd, timeout, trajectory_events):
    """Popen the served harness, stream+record stdout lines, kill the process
    group on timeout. Unlike the Claude runner, a served CLI's stdout is not
    stream-json, so every line is recorded raw. Returns
    (timed_out, return_code, captured_text)."""
    proc = subprocess.Popen(
        argv, cwd=cwd, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        start_new_session=True, text=True, bufsize=1,
    )
    timed_out = threading.Event()

    def _on_timeout():
        timed_out.set()
        rcc._kill_process_group(proc.pid)

    timer = threading.Timer(timeout, _on_timeout)
    timer.start()
    t0 = time.monotonic()
    captured: list[str] = []
    try:
        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            captured.append(line)
            trajectory_events.append({
                "t_offset": round(time.monotonic() - t0, 3),
                "parsed": None,
                "raw_line": line,
            })
            rcc._safe_print(line)
        proc.wait()
        if timed_out.is_set():
            print(f"\n[run] Timeout after {timeout}s; killed process group.", flush=True)
    finally:
        timer.cancel()
        rcc._kill_process_group(proc.pid)
    return timed_out.is_set(), proc.returncode, "\n".join(captured)


def main():
    ap = argparse.ArgumentParser(
        description="Run a served agentic harness (kimi-code / any OpenAI-compatible harness) on one benchmark task.")
    ap.add_argument("--workspace", required=True,
                    help="Paper directory containing task_spec.md (the agent runs here).")
    ap.add_argument("--paper-number", default=None, help="Paper id, for labeling only.")
    ap.add_argument("--harness", default=os.environ.get("SOLVER_HARNESS", "kimi"),
                    choices=["kimi", "qwen", "dsh", "custom"],
                    help="Built-in 'kimi' (kimi-code), 'qwen' (qwen-code), 'dsh' "
                         "(deepseek-harness), or 'custom' with --harness-cmd. "
                         "Default from env SOLVER_HARNESS, else 'kimi'.")
    ap.add_argument("--harness-cmd", default=None,
                    help="Template for a custom served harness; tokens {model} {workspace} {prompt}.")
    ap.add_argument("--model", default=SERVED_MODEL,
                    help=f"Served model id the harness should use (default {SERVED_MODEL}).")
    ap.add_argument("--base-url", default=None,
                    help="OpenAI-compatible endpoint for the pre-flight health check "
                         "(default: env SOLVER_SERVED_BASE_URL, else discovered from the harness config).")
    ap.add_argument("--skip-health-check", action="store_true",
                    help="Skip the endpoint reachability pre-flight (use if the harness reaches it differently).")
    ap.add_argument("--max-resumes", type=int, default=MAX_RESUMES,
                    help=f"Resume-until-complete cap (default {MAX_RESUMES}; 0 disables the loop).")
    time_group = ap.add_mutually_exclusive_group()
    time_group.add_argument("--hours", type=int, default=None,
                            help="Tell the agent it has N hours and cap the subprocess at N hours.")
    time_group.add_argument("--unlimited", action="store_true",
                            help="Tell the agent it has unlimited time; subprocess effectively uncapped.")
    args = ap.parse_args()

    if args.hours is not None and args.hours <= 0:
        sys.exit("[run] --hours must be a positive integer")
    if args.harness == "custom" and not args.harness_cmd:
        sys.exit("[run] --harness custom requires --harness-cmd '<template with {prompt}>'")

    # Pre-flight: harness binary present.
    if args.harness in ("kimi", "qwen", "dsh") and not (Path(HARNESS_BIN).exists() or _which(HARNESS_BIN)):
        sys.exit(f"[run] harness binary not found: {HARNESS_BIN} (set SOLVER_HARNESS_BIN)")

    # Pre-flight: endpoint reachable (a dropped tunnel should fail fast).
    base_url = args.base_url or discover_base_url(args.harness)
    if not args.skip_health_check:
        ok, detail = health_check(base_url)
        if not ok:
            sys.exit(f"[run] served endpoint pre-flight FAILED: {detail}\n"
                     f"      Is the model served and the tunnel up? "
                     f"Re-run with --skip-health-check to bypass.")
        print(f"[run] endpoint OK: {detail}", flush=True)

    workspace = rcc.setup_workspace(args.workspace)
    prompt = rcc.build_prompt(workspace, hours=args.hours, unlimited=args.unlimited)
    timeout = rcc.resolve_timeout(hours=args.hours, unlimited=args.unlimited)
    session_id = str(uuid.uuid4())  # our own run id; the harness may mint its own

    if args.unlimited:
        time_label = "unlimited (agent told)"
    elif args.hours is not None:
        time_label = f"{args.hours}h (agent told)"
    else:
        time_label = f"{timeout}s safety-net (agent not told)"

    print(f"[run] Harness:   {args.harness} ({args.harness_cmd or HARNESS_BIN})", flush=True)
    print(f"[run] Model:     {args.model} (served)", flush=True)
    print(f"[run] Endpoint:  {base_url or '(harness-configured)'}", flush=True)
    print(f"[run] Workspace: {workspace}", flush=True)
    print(f"[run] Paper:     {args.paper_number}", flush=True)
    print(f"[run] Time:      {time_label}", flush=True)
    print(f"[run] Timeout:   {timeout}s ({rcc._format_elapsed(timeout)})", flush=True)

    trajectory_events: list = []
    start_wall = time.monotonic()
    start_iso = datetime.now(timezone.utc).isoformat()
    deadline = start_wall + timeout
    timed_out = False
    return_code = None
    attempt = 0
    complete = False
    reason = ""
    judge_verdict = None  # last same-model judge verdict, recorded in metadata
    harness_session = None  # scraped from harness stdout, used to resume with context
    # The agent's experiments share the job's memory limit with the runner and the harness: keep them from ending the run.
    use_disk_tmpdir()
    start_memory_watchdog(workspace, on_event=lambda kind, info: trajectory_events.append({
        "t_offset": round(time.monotonic() - start_wall, 3),
        "parsed": {"type": "harness", "event": "memory_" + kind, **info}, "raw_line": None}))
    try:
        while True:
            attempt += 1
            remaining = max(1, int(deadline - time.monotonic()))
            this_prompt = (prompt if attempt == 1 else resume_prompt(reason)) + pending_notices()
            argv = build_harness_argv(
                args.harness, args.harness_cmd, model=args.model, workspace=workspace,
                prompt=this_prompt, resume_session=(harness_session if attempt > 1 else None),
            )
            trajectory_events.append({
                "t_offset": round(time.monotonic() - start_wall, 3),
                "parsed": {"type": "harness", "event": "attempt_start", "attempt": attempt,
                           "resume": attempt > 1, "harness_session": harness_session,
                           "reason": (reason if attempt > 1 else None)},
                "raw_line": None,
            })
            if attempt == 1:
                print(f"\n[run] Attempt {attempt} (initial)", flush=True)
            else:
                print(f"\n[run] Attempt {attempt} (resume{' '+harness_session if harness_session else ' fresh'}) "
                      f"— previous stop was incomplete: {reason}", flush=True)

            to, return_code, captured = run_harness(
                argv, cwd=workspace, timeout=remaining, trajectory_events=trajectory_events)
            timed_out = timed_out or to

            sid = discover_session(args.harness, captured)
            if sid:
                harness_session = sid

            complete, reason = rcc._completion_status(workspace)
            if complete and base_url:
                # Cheap check passed -> same-model LLM judge (parity with the
                # claude/gpt rounds; each model judges its own paper).
                print(f"[run] Placeholder check passed; asking the {args.model} judge...", flush=True)
                jc, issues = judge_completion(workspace, base_url, args.model)
                judge_verdict = {"complete": jc, "issues": issues}
                trajectory_events.append({
                    "t_offset": round(time.monotonic() - start_wall, 3),
                    "parsed": {"type": "harness", "event": "judge_verdict",
                               "attempt": attempt, **judge_verdict},
                    "raw_line": None,
                })
                if not jc:
                    complete = False
                    reason = "; ".join(issues) if issues else "judge flagged the paper incomplete"
            print(f"[run] After attempt {attempt}: "
                  f"{'COMPLETE' if complete else 'INCOMPLETE -- ' + reason}", flush=True)

            if complete or timed_out:
                break
            if attempt >= 1 + args.max_resumes:
                print(f"[run] Reached resume cap ({args.max_resumes}); accepting the current "
                      f"(incomplete) submission.", flush=True)
                break
            if time.monotonic() >= deadline:
                print("[run] Time budget exhausted; accepting the current submission.", flush=True)
                break
    finally:
        end_wall = time.monotonic()
        end_iso = datetime.now(timezone.utc).isoformat()
        elapsed_seconds = end_wall - start_wall
        metadata = {
            "runner": "run_served_harness.py",
            "session_id": session_id,
            "harness": args.harness,
            "harness_cmd": args.harness_cmd,
            "harness_bin": HARNESS_BIN,
            "harness_session": harness_session,
            "model": args.model,
            "served": True,
            "base_url": base_url,
            "paper_number": args.paper_number,
            "workspace": str(workspace),
            "timeout_seconds": timeout,
            "time_budget_hours": args.hours,
            "time_budget_unlimited": args.unlimited,
            "attempts": attempt,
            "max_resumes": args.max_resumes,
            "completed": complete,
            "completion_reason": reason,
            "judge_verdict": judge_verdict,
            "timed_out": timed_out,
            "return_code": return_code,
            "start_utc": start_iso,
            "end_utc": end_iso,
            "elapsed_seconds": round(elapsed_seconds, 3),
            "elapsed_human": rcc._format_elapsed(elapsed_seconds),
            "event_count": len(trajectory_events),
        }
        traj_path = rcc.save_trajectory(workspace, metadata, trajectory_events)
        print(f"\n[run] Elapsed:    {rcc._format_elapsed(elapsed_seconds)} "
              f"({elapsed_seconds:.1f}s) over {attempt} attempt(s)", flush=True)
        if complete:
            print("[run] Status:     COMPLETE (report.pdf present, no placeholders, "
                  "same-model judge approved)", flush=True)
        elif timed_out:
            print(f"[run] Status:     TIMED OUT, still incomplete ({reason})", flush=True)
        else:
            print(f"[run] Status:     stopped INCOMPLETE ({reason})", flush=True)
        print(f"[run] Trajectory: {traj_path} ({len(trajectory_events)} events)", flush=True)


def _which(name: str) -> bool:
    from shutil import which
    return which(name) is not None


if __name__ == "__main__":
    main()
