"""420 fresh Astra/max evaluations, eight workers, model-level checkpoints.

Reuses the pilot's exact judging protocol and deterministic grader. No automatic
retries, no modification of archive inputs, and no reuse of pilot verdicts.
Each worker runs direction then integrity, so eight workers means at most eight
live judge sessions, not sixteen. Resume is explicit and retains valid stages.

Revision 2 (two ChatGPT subscriptions), active only when manifest.json lists
`accounts`: the eight slots are split across accounts, each a separate CODEX_HOME
login with its own weekly window. A job starts on an account only if its latest
usedPercent plus the projected cost of every stage already in flight there, plus
the new job's two stages, stays under that account's cap. An account at its cap
idles and is re-probed (rateLimits/read, no inference) until its window resets; a
quota refusal requeues the job instead of failing it. Snapshots of a model are
removed once all 60 of its evaluations are complete (their snapshot.json records are
kept). Batches without `accounts` run the original code path unchanged; the judging
protocol, prompt, schema, frozen rubrics and grader are untouched either way.

Revision 3 (`rescope`; active only when manifest.json has view_version 2):
- Scope: 240 evaluations, i.e. repeat_01 of all 105 submissions plus repeats 02-04
  of opus_5, fable_5 and sol_5_6, dispatched in one recorded order. Completed jobs
  outside it are kept as bonus data and are never dispatched again.
- Full-submission view: the snapshot is the solver's archived working directory, not
  just proposal/. Files outside proposal/ are staged as `workdir_outside_proposal/`
  and the prompt then gains one paragraph saying so. Links that stay inside the
  submission are copied as real files; dangling or external links are recorded
  but not staged. Never staged: prior judgments (eval_results*), agent transcripts
  and harness state (ai_scientist_trajectory.json, _runlogs, .dsh, .oh_conversations),
  agent instruction/config files (.claude, .codex, CLAUDE.md, AGENTS.md) and
  environments/caches (.git, .venv*, venv, __pycache__, .uv-cache, node_modules, and
  any directory holding pyvenv.cfg or conda-meta/).
- A submission whose full view equals its old proposal-only snapshot (same files,
  no links, nothing outside proposal/) gets byte-identical inputs and prompt, so its
  completed evaluations stay valid. Started evaluations of any other submission are
  archived under superseded_<stamp>/ in their cell and rerun.
- Snapshots are copied on demand and removed when no running or soon-queued job
  needs them. A recreated snapshot must reproduce the first one's SHA-256 map.

Revision 4 (other agents; credit avoidance):
- `prepare --agents astra-6 [...]` builds a batch of just those agents (src/solver/run.sh directory
  names); without it the batch is the paper's seven. `rescope` then selects all four repeats of each
  of its submissions, judged with the same full-submission view, prompts, rubrics and grader.
- `rescope --slots-per-account N` sets the concurrent judges per account (default 8).
- SOG_CODEX_AVOID_CREDITS=1 at prepare records avoid_credits in the manifest: purchased credits no
  longer lift an account's cap, so the batch waits at the cap instead of spending them.
- On macOS, a view of a submission stored on the same volume as the batch is staged as APFS
  copy-on-write clones (no extra space; each clone's SHA-256 is still checked against the source).
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import judge_protocol as protocol
import judge_common as base

REPO = Path(__file__).resolve().parents[2]
PAPERS = (1, 2, 10, 3, 4, 8, 9, 19, 14, 17, 11, 16, 13, 18, 15)
MODELS = (
    ('opus_4_8', 'Claude-Opus-4.8', 'opus-4.8'),
    ('opus_5', 'Claude-Opus-5', 'opus-5'),
    ('fable_5', 'Claude-Fable-5', 'fable-5'),
    ('sol_5_6', 'GPT-5.6-Sol', 'sol-5.6'),
    ('deepseek', 'DeepSeek-V4-Pro-0813', 'deepseek-v4-pro'),
    ('kimi', 'Kimi-K3', 'kimi-k3'),
    ('glm', 'GLM-5.3', 'glm-5.3'),
    ('opus_5_5', 'Claude-Opus-5.5', 'opus-5.5'),
    ('astra_6', 'GPT-6-Astra', 'astra-6'),
)
# The paper's batch: the first seven agents (105 submissions). `prepare --agents` selects others by their
# src/solver/run.sh directory name; such a batch is rescoped to every repeat of its submissions.
PAPER_AGENTS = tuple(k for k, _, _ in MODELS[:7])
# Opt-in (SOG_CODEX_AVOID_CREDITS=1 at prepare): never run past an account's cap on purchased credits.
AVOID_CREDITS = os.environ.get('SOG_CODEX_AVOID_CREDITS') == '1'
CONCURRENCY = 8
PREFIX = 'codex_judge_'
CODE_FILES = ('run_codex_judge.py', 'judge_common.py', 'judge_protocol.py')
# Revision 2: used only when the manifest lists accounts (see module docstring).
SLOTS_PER_ACCOUNT = 4
# Extra ChatGPT logins, one CODEX_HOME each: SOG_CODEX_HOMES=dir1,dir2 (acct1 = the default login).
ACCOUNTS = tuple([dict(name='acct1', codex_home=None, cap=0.90)] +
                 [dict(name=f'acct{i}', codex_home=h, cap=0.90)
                  for i, h in enumerate(filter(None, os.environ.get('SOG_CODEX_HOMES', '').split(',')), 2)])
STAGE_COST = 0.0075            # weekly-window fraction per rubric stage (68 evals took the window 6% -> 91%)
EXIT_QUOTA = 75                # EX_TEMPFAIL: the job was requeued for quota, not failed
MAX_QUOTA_REQUEUES = 3
PROBE_INTERVAL_S = 600


class QuotaWait(Exception):
    """An account is at its cap or refused a session for usage limits: requeue, do not fail."""


def accounts_of(m):
    return m.get('accounts')


def slots_per_account(m):
    return m.get('slots_per_account', m['concurrency'])


def slot_account(m, slot):
    accts = accounts_of(m)
    return accts[slot // slots_per_account(m)] if accts else None


def use_account(acct):
    """Select the account's Codex login for this process and the app-server it spawns."""
    os.environ.pop('CODEX_HOME', None)
    if acct and acct.get('codex_home'):
        os.environ['CODEX_HOME'] = acct['codex_home']


def windows(limits):
    """(limit id, window key, used fraction, resetsAt) for every reported window."""
    out = []
    by_id = limits.get('rateLimitsByLimitId') or {'codex': limits.get('rateLimits')}
    for lid, r in by_id.items():
        for key in ('primary', 'secondary'):
            w = (r or {}).get(key)
            if w and w.get('usedPercent') is not None:
                out.append((lid, key, w['usedPercent'] / 100, w.get('resetsAt')))
    return out


def has_credits(limits):
    """Purchased credits keep an account serving past its plan window; the service then decides."""
    by_id = limits.get('rateLimitsByLimitId') or {'codex': limits.get('rateLimits')}
    c = ((by_id.get('codex') or {}).get('credits')) or {}
    return bool(c.get('unlimited') or c.get('hasCredits') or float(c.get('balance') or 0) > 0)


def hard_limited(limits, m=None):
    by_id = limits.get('rateLimitsByLimitId') or {'codex': limits.get('rateLimits')}
    if credits_usable(m or {}, limits):                        # plan window spent, credits carry on
        return any(r and r.get('spendControlReached') for r in by_id.values())
    return any(r and (r.get('spendControlReached') or r.get('rateLimitReachedType')) for r in by_id.values())


def usage_path(root, name):
    return root / f'usage_{name}.json'


@contextmanager
def wait_lock(path, timeout=120):
    """Blocking lock for short shared writes; base.lock fails fast and is for exclusive ownership."""
    import fcntl
    with path.open('a+') as f:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.2)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def record_usage(root, name, limits, source):
    with wait_lock(root / f'usage_{name}.lock'):
        write(usage_path(root, name), dict(limits=limits, source=source, utc=utc(), epoch=time.time()))


def read_usage(root, name):
    p = usage_path(root, name)
    return read(p) if p.exists() else None


def fmt_ts(ts):
    return datetime.fromtimestamp(ts).strftime('%a %m-%d %H:%M') if ts else '?'


def credits_usable(m, limits):
    """Purchased credits count only when the batch was not prepared with SOG_CODEX_AVOID_CREDITS=1."""
    return has_credits(limits) and not m.get('avoid_credits')


def account_block(root, acct, remaining_stages, m=None):
    """None if a new two-stage job fits under the account's cap, else the reason it does not."""
    rec = read_usage(root, acct['name'])
    if not rec:
        return 'no usage reading yet'
    if hard_limited(rec['limits'], m):
        return 'service reports a spend/usage limit'
    if credits_usable(m or {}, rec['limits']):
        return None
    for lid, key, used, resets in windows(rec['limits']):
        projected = used + (remaining_stages + 2) * STAGE_COST
        if projected > acct['cap']:
            return (f'{lid}/{key} {used:.0%} + {remaining_stages} stage(s) in flight -> {projected:.0%} '
                    f'> cap {acct["cap"]:.0%}; resets {fmt_ts(resets)}')
    return None


def stage_backstop(acct, limits, m=None):
    """Before each stage: yield (requeue) only when clearly past the account's cap."""
    if hard_limited(limits, m):
        raise QuotaWait(f'{acct["name"]}: service reports a spend/usage limit')
    if credits_usable(m or {}, limits):
        return
    for lid, key, used, _ in windows(limits):
        if round(used, 4) >= round(min(0.99, acct['cap'] + 0.04), 4):   # readings are whole percents
            raise QuotaWait(f'{acct["name"]}: {lid}/{key} at {used:.0%}, past cap {acct["cap"]:.0%}')


def quota_like(exc):
    text = f'{type(exc).__name__} {exc}'.lower()
    return any(k in text for k in ('rate limit', 'rate_limit', 'usage limit', 'quota', 'too many requests', '429'))


def auth_transient(exc):
    """A 401 mid-session: another process (a coupon reset, the user's own Codex) refreshed the
    shared ChatGPT login and this app-server's token went stale. A fresh worker re-reads auth.json."""
    text = f'{type(exc).__name__} {exc}'.lower()
    return '401' in text and 'unauthorized' in text
SKIP_DIRS = {'.git', '.venv', 'venv', '__pycache__'}
read, write, digest, utc = base.read, base.write, base.digest, base.utc
# Revision 3: full-submission view (see module docstring).
OUTSIDE = 'workdir_outside_proposal'
TOP_LEVEL_OMIT = {
    'ai_scientist_trajectory.json': 'agent transcript (protocol excludes it)',
    '_runlogs': 'agent transcript (harness run log)',
    '.dsh': 'agent transcript (harness session state)',
    '.oh_conversations': 'agent transcript (OpenHands conversation)',
    'task_spec.md': 'staged from the frozen task inputs',
    'paper.md': 'staged from the frozen source inputs',
    '.DS_Store': 'Finder metadata',
}
TRANSCRIPTS = ('ai_scientist_trajectory.json', '_runlogs', '.dsh', '.oh_conversations')
AGENT_CONFIG = {'.claude', '.codex', 'CLAUDE.md', 'CLAUDE.local.md', 'AGENTS.md'}
ENV_DIRS = {'.uv-cache', '.uv_cache', 'node_modules'}
REPEATED_MODELS = ('opus_5', 'fable_5', 'sol_5_6')
EVICT_LOOKAHEAD = 16
COPY_ATTEMPTS = 3
COPY_THREADS = 8              # parallel file copies per view (small-file reads over USB are latency-bound)
PREFETCH_THREADS = 2          # views built at once, in the background
PREFETCH_AHEAD = 4            # distinct upcoming submissions whose views are prepared ahead
DISPATCH_WINDOW = 8           # a job may start once its view is ready if it is among the next 8
VIEW_RESERVE = 5 * 2**30
PROMPT_ANCHOR = 'Submitted artifacts are under `proposal/`.'
RESCOPE_SLOTS_PER_ACCOUNT = 8  # Codex is bound by each account's weekly window, not a 5-hour one: more
                               # sessions reach the same weekly cap sooner (each session is ~0.1 GB here)
WORKDIR_NOTE = (
    'This submission also left files in the solver\'s working directory outside `proposal/`. They are staged '
    'read-only under `workdir_outside_proposal/` with their original relative paths: a path `X` relative to the '
    'solver\'s working directory, or `../X` relative to `proposal/`, is `workdir_outside_proposal/X`. They are '
    'part of this submission, not a sibling output directory: they hold the solver\'s own run outputs and working '
    'files, alongside inputs the task supplied (such as `code/`, `data/`, `images/` or `paper.pdf`), which the '
    'solver may have modified or added to. Before judging a required artifact, log or result to be missing, check '
    'both `proposal/` and `workdir_outside_proposal/`. Agent transcripts and environment/cache directories were '
    'not staged.')


def stamp():
    return datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ')


def make_jobs(keys=PAPER_AGENTS, papers=None, repeats=None):
    return [dict(id=f'{key}/P{new}/repeat_{repeat:02d}', source_id=f'{key}/P{new}',
                 model_key=key, model=model, new_paper_id=new, original_paper_id=old,
                 repeat=repeat)
            for key, model, _ in MODELS if key in keys
            for new, old in enumerate(PAPERS, 1) if not papers or new in papers for repeat in (repeats or range(1, 5))]


def batch_papers(m=None):
    """Papers a batch judges: all 15 unless it was prepared with --papers."""
    return list((m or {}).get('papers') or range(1, 16))


def batch_repeats(m=None):
    """Repeats a batch judges: 1-4 unless it was prepared with --repeats (e.g. 2 3 4 to add to an earlier repeat 1)."""
    return list((m or {}).get('repeats_selected') or range(1, 5))


def select_agents(names):
    """Batch agents from src/solver/run.sh directory names (e.g. 'astra-6'); default: the paper's seven."""
    if not names:
        return PAPER_AGENTS
    by_dir = {d: k for k, _, d in MODELS}
    unknown = [n for n in names if n not in by_dir]
    if unknown:
        raise SystemExit(f'Unknown agent(s) {unknown}; choose from {sorted(by_dir)}')
    return tuple(k for k, _, d in MODELS if d in names)


def ignored(name):
    return name in SKIP_DIRS or name.startswith('eval_results') or name.endswith('.pyc') or name.startswith('._')


def inventory(folder):
    """Metadata inventory, without following links or excluding scientific data."""
    result = {}
    for here, dirs, files in os.walk(folder, followlinks=False):
        dirs[:] = sorted(n for n in dirs if not ignored(n))
        for name in sorted(dirs + [n for n in files if not ignored(n)]):
            p = Path(here) / name
            if p.is_symlink():
                raise RuntimeError(f'Artifact link needs review before staging: {p}')
            if p.is_file():
                st = p.stat()
                result[str(p.relative_to(folder))] = dict(size=st.st_size, mtime_ns=st.st_mtime_ns)
    return result


def pause(root, reason):
    base.mounted()
    # A single winner writes this record; parallel workers cannot overwrite it.
    try:
        with (root / 'PAUSE.json').open('x') as f:
            json.dump(dict(reason=reason, utc=utc()), f)
            f.flush()
            os.fsync(f.fileno())
    except FileExistsError:
        pass


def discover_sources(keys=PAPER_AGENTS, papers=None):
    """Every solver workspace <agent>/paper<i>/ under SOG_SUBMISSIONS (src/solver/run.sh's layout)."""
    sources = {}
    for key, model, directory in (m for m in MODELS if m[0] in keys):
        for new, old in enumerate(PAPERS, 1):
            if papers and new not in papers:
                continue
            source = base.SUBMISSIONS / directory / f'paper{new}'
            if not (source / 'proposal').is_dir():
                raise RuntimeError(f'Missing submission: {source}/proposal')
            current = REPO / f'tasks/paper{new}/task_spec.md'
            archived = digest(source / 'task_spec.md') if (source / 'task_spec.md').is_file() else None
            reference = source / 'paper.md'
            if not reference.is_file():
                reference = REPO / f'tasks/paper{new}/paper.md'
            if not reference.is_file():
                raise RuntimeError(f'Missing original-paper text: {reference}')
            # Preserve hashes of task/report/prior judgments, without staging judgments.
            protected = [source / 'task_spec.md']
            protected += [source / 'proposal' / n for n in ('report.tex', 'report.pdf')]
            for d in (source / 'proposal').glob('eval_results*'):
                if d.is_dir():
                    protected.extend(p for p in d.rglob('*') if p.is_file())
            sources[f'{key}/P{new}'] = dict(source=str(source), model=model,
                new_paper_id=new, original_paper_id=old,
                reference=str(reference), reference_sha256=digest(reference),
                archived_task_sha256=archived, current_task_sha256=digest(current),
                task_revised_since_generation=archived != digest(current),
                protected_hashes={str(p): digest(p) for p in protected if p.is_file()})
        print(f'PREFLIGHT {key}: {len(papers) if papers else 15} sources verified', flush=True)
    return sources


def prepare(keys=PAPER_AGENTS, papers=None, repeats=None):
    base.mounted()
    papers = sorted(set(papers)) if papers else None
    if papers and (keys == PAPER_AGENTS or not set(papers) <= set(range(1, 16))):
        raise SystemExit('--papers selects papers 1-15 of a batch of other agents (prepare --agents)')
    repeats = sorted(set(repeats)) if repeats else None
    if repeats and (keys == PAPER_AGENTS or not set(repeats) <= {1, 2, 3, 4}):
        raise SystemExit('--repeats selects repeats 1-4 of a batch of other agents (prepare --agents)')
    # Verify every submission's identity and task provenance before any inference.
    sources = discover_sources(keys, papers)
    n_sub = len(sources)

    base.JUDGE_RUNS.mkdir(parents=True, exist_ok=True)
    root = base.JUDGE_RUNS / f'{PREFIX}{stamp()}'
    root.mkdir(exist_ok=False)
    for name in CODE_FILES:
        shutil.copy2(Path(__file__).parent / name, root / name)
    material_files = []
    for n in range(1, 16):
        material_files += [f'tasks/paper{n}/task_spec.md']
        material_files += [f'src/grader/eval_artifacts/paper{n}/{name}' for name in (*base.KINDS.values(), 'grading.py')]
    for rel in material_files:
        dest = root / 'frozen' / rel.removeprefix('src/grader/')   # frozen/{tasks,eval_artifacts}/paper<i>/
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / rel, dest)
    policies = {}
    for n in range(1, 16):
        folder = root / f'frozen/eval_artifacts/paper{n}'
        policy = base.grader(folder).load_policy()
        policies[f'P{n}'] = dict(revision=policy['rubric_revision'], rubric_sha256=policy['rubric_sha256'])
        inp = root / f'inputs/P{n}'
        inp.mkdir(parents=True)
        for scope, filename in base.KINDS.items():
            rubric = read(folder / filename)
            context = policy['context_fields'] if scope == 'direction' else {}
            write(inp / f'{scope}.schema.json', protocol.schema(rubric, context))
            write(inp / f'{scope}.context_fields.json', context)
            (inp / f'{scope}.prompt.txt').write_text(protocol.prompt(scope, len(protocol.leaves(rubric)), context))
    for sid, source in sources.items():
        inp = root / 'source_inputs' / sid
        inp.mkdir(parents=True)
        shutil.copy2(source['reference'], inp / 'paper.md')
        if digest(inp / 'paper.md') != source['reference_sha256']:
            raise RuntimeError(f'Reference changed during preparation: {sid}')
    fixed = {name: digest(root / name) for name in CODE_FILES}
    for dirname in ('frozen', 'inputs', 'source_inputs'):
        fixed.update({f'{dirname}/{p}': h for p, h in base.hashes(root / dirname).items()})
    jobs = make_jobs(keys, papers, repeats)
    for job in jobs:
        cell = root / job['id']
        cell.mkdir(parents=True)
        (cell / 'verdicts').mkdir()
        write(cell / 'state.json', dict(status='pending', job=job, stages={}, updated_utc=utc()))
    manifest = dict(created_utc=utc(), model=base.MODEL, effort=base.EFFORT,
        total_submissions=n_sub, repeats=len(repeats) if repeats else 4, total_evaluations=len(jobs), total_rubric_sessions=2 * len(jobs),
        concurrency=SLOTS_PER_ACCOUNT * len(ACCOUNTS), slots_per_account=SLOTS_PER_ACCOUNT,
        accounts=[dict(a) for a in ACCOUNTS], stage_cost_estimate=STAGE_COST, cleanup_completed_snapshots=True,
        avoid_credits=AVOID_CREDITS, model_order=list(keys), jobs=jobs, sources=sources,
        fixed_hashes=fixed, policies=policies, prompt_version=protocol.VERSION,
        codex_bin=str(base.CODEX), codex_sha256=digest(base.CODEX), python=sys.executable,
        quota_pause_percent=90, automatic_retries=0, pilot_excluded=True,
        isolation='Separate read-only ephemeral stage/repeat sessions; shared per-submission physical snapshot, prior judgments excluded. Prompt-level read blinding, not OS-level read isolation.',
        scope=f'All {n_sub} submissions ({len(keys)} agents x {len(papers) if papers else 15} papers). '
              + (f'Repeats {", ".join(map(str, repeats))} of each (fresh assessments).' if repeats else 'Four fresh assessments each.'),
        **({'papers': papers} if papers else {}), **({'repeats_selected': repeats} if repeats else {}),
        snapshot_exclusions=sorted(SKIP_DIRS) + ['eval_results*', '*.pyc', '._*'])
    write(root / 'manifest.json', manifest)
    write(root / 'status.json', dict(status='prepared', completed=0, total=len(jobs), updated_utc=utc()))
    print(f'BATCH_DIRECTORY={root}', flush=True)
    return root


def verify_frozen(root, manifest):
    for rel, expected in manifest['fixed_hashes'].items():
        if digest(root / rel) != expected:
            raise RuntimeError(f'Frozen evaluation input changed: {rel}')
    if digest(Path(manifest['codex_bin'])) != manifest['codex_sha256']:
        raise RuntimeError('Codex binary changed; review before launching more sessions')


def verify_protected(source):
    for name, expected in source['protected_hashes'].items():
        if digest(Path(name)) != expected:
            raise RuntimeError(f'Original protected file changed: {name}')


def snapshot(root, sid, source):
    """Create once before scheduling any of the four repetitions."""
    dest = root / 'snapshots' / sid
    marker = dest / 'snapshot.json'
    if marker.exists():
        return
    if dest.exists():
        raise RuntimeError(f'Partial snapshot needs review: {dest}')
    base.mounted()
    verify_protected(source)
    src = Path(source['source']) / 'proposal'
    inv = inventory(src)
    size = sum(v['size'] for v in inv.values())
    if shutil.disk_usage(root).free < size + 5 * 2**30:
        raise RuntimeError(f'Insufficient SSD space for {sid}: {size / 2**30:.2f} GiB snapshot plus 5 GiB reserve required')
    dest.mkdir(parents=True)
    shutil.copytree(src, dest / 'proposal', ignore=lambda _, names: [n for n in names if ignored(n)])
    if inventory(src) != inv:
        raise RuntimeError(f'Artifacts changed while being copied: {sid}')
    hashes = base.hashes(dest / 'proposal')
    if set(hashes) != set(inv) or any(digest(src / rel) != h for rel, h in hashes.items()):
        raise RuntimeError(f'Artifact snapshot differs from source: {sid}')
    write(marker, dict(source=source['source'], created_utc=utc(), bytes=size,
                       source_inventory=inv, sha256=hashes, snapshot_inventory=inventory(dest / 'proposal')))
    print(f'SNAPSHOT {sid}: {len(hashes)} files, {size / 2**20:.1f} MiB', flush=True)


def verify_snapshot(root, sid):
    dest = root / 'snapshots' / sid
    meta = read(dest / 'snapshot.json')
    if inventory(dest / 'proposal') != meta['snapshot_inventory']:
        raise RuntimeError(f'Frozen artifact metadata changed: {sid}')
    if inventory(Path(meta['source']) / 'proposal') != meta['source_inventory']:
        raise RuntimeError(f'Original artifact metadata changed: {sid}')


def checkpoint_valid(cell, state, scope, spec):
    saved = state['stages'].get(scope, {})
    if saved.get('status') != 'complete':
        return False
    path = cell / f'verdicts/{scope}.json'
    if not path.is_file() or digest(path) != saved['verdict_sha256']:
        raise RuntimeError(f'Completed verdict missing or changed: {path}')
    protocol.validate(read(path), spec)
    return True


def stage_workspace(root, cell, job, scope):
    ws = cell / f'workspace_{scope}'
    ws.mkdir(exist_ok=True)
    n = job['new_paper_id']
    expected = {
        'task_spec.md': root / f'frozen/tasks/paper{n}/task_spec.md',
        'paper.md': root / 'source_inputs' / job['source_id'] / 'paper.md',
        'rubric.json': root / f'frozen/eval_artifacts/paper{n}' / base.KINDS[scope],
        'context_fields.json': root / f'inputs/P{n}/{scope}.context_fields.json',
    }
    for name, src in expected.items():
        dst = ws / name
        if not dst.exists():
            shutil.copy2(src, dst)
        if digest(dst) != digest(src):
            raise RuntimeError(f'Staged input changed: {dst}')
    proposal = root / 'snapshots' / job['source_id'] / 'proposal'
    link = ws / 'proposal'
    if not link.is_symlink():
        link.symlink_to(proposal, target_is_directory=True)
    if link.resolve() != proposal.resolve():
        raise RuntimeError(f'Unexpected artifact link: {link}')
    return ws


def worker(root, job_id, slot):
    base.mounted()
    m = read(root / 'manifest.json')
    job = next(j for j in m['jobs'] if j['id'] == job_id)
    acct = slot_account(m, slot)          # None for single-account batches
    cell = root / job_id
    with base.lock(root / f'slot_{slot}.lock'), base.lock(cell / 'worker.lock'):
        state = read(cell / 'state.json')
        if state['status'] == 'complete':
            return
        if state['status'] != 'pending':
            raise RuntimeError('Started evaluations require explicit resume')
        try:
            v2 = m.get('view_version') == 2
            verify_frozen(root, m)
            verify_protected(m['sources'][job['source_id']])
            (verify_view if v2 else verify_snapshot)(root, job['source_id'])
            state.update(status='running', pid=os.getpid(), started_utc=utc(), updated_utc=utc())
            write(cell / 'state.json', state)
            from openai_codex import Codex, ApprovalMode, Sandbox
            from openai_codex.client import CodexConfig
            from openai_codex._run import _collect_turn_result
            os.environ.pop('OPENAI_API_KEY', None)
            use_account(acct)
            with Codex(CodexConfig(codex_bin=m['codex_bin'])) as codex:
                for scope in base.KINDS:
                    base.mounted()
                    inp = root / f'inputs/P{job["new_paper_id"]}'
                    spec = read(inp / f'{scope}.schema.json')
                    if checkpoint_valid(cell, state, scope, spec):
                        continue
                    if (root / 'PAUSE.json').exists():
                        state.update(status='paused', updated_utc=utc())
                        write(cell / 'state.json', state)
                        return
                    quota = base.limits(codex)
                    write(cell / 'usage_limits.json', quota)
                    if acct:
                        record_usage(root, acct['name'], quota, f'{job_id}/{scope}')
                        stage_backstop(acct, quota, m)
                    elif reason := base.quota_reason(quota):
                        raise RuntimeError(reason)
                    ws = (stage_view_workspace if v2 else stage_workspace)(root, cell, job, scope)
                    prompt_path = prompt_file(root, job, scope) if v2 else inp / f'{scope}.prompt.txt'
                    attempt = cell / 'attempts' / f'{scope}_{stamp()}'
                    attempt.mkdir(parents=True, exist_ok=False)
                    write(attempt / 'sdk_metadata.json', codex.metadata)
                    try:
                        thread = codex.thread_start(model=m['model'],
                            config={'model_reasoning_effort': m['effort'], 'web_search': 'disabled'},
                            cwd=str(ws), sandbox=Sandbox.read_only,
                            approval_mode=ApprovalMode.deny_all, ephemeral=True)
                        turn = thread.turn(prompt_path.read_text(),
                                           output_schema=spec, model=m['model'], effort=m['effort'])
                        state['stages'][scope] = dict(status='running', started_utc=utc(), account=acct and acct['name'],
                            thread_id=thread.id, turn_id=turn.id, attempt=str(attempt.relative_to(root)),
                            prompt=prompt_path.name)
                        state['updated_utc'] = utc()
                        write(cell / 'state.json', state)
                        print(f'START {job_id} {scope}', flush=True)
                        with (attempt / 'events.jsonl').open('x') as f:
                            def events():
                                for event in turn.stream():
                                    f.write(json.dumps(event, default=base.serial, ensure_ascii=False) + '\n')
                                    f.flush()
                                    yield event
                            answer = _collect_turn_result(events(), turn_id=turn.id)
                    except QuotaWait:
                        raise
                    except Exception as exc:
                        if acct and quota_like(exc):
                            raise QuotaWait(f'{acct["name"]}: {scope} refused: {exc}') from exc
                        if acct and auth_transient(exc):
                            raise QuotaWait(f'{acct["name"]}: {scope} stale login token (401), requeued: {str(exc)[:160]}') from exc
                        raise
                    write(attempt / 'turn.json', answer)
                    if not answer.final_response:
                        raise RuntimeError(f'{scope}: no final response')
                    verdict = json.loads(answer.final_response, object_pairs_hook=base.unique)
                    write(attempt / 'raw_verdict.json', verdict)
                    protocol.validate(verdict, spec)
                    write(cell / f'verdicts/{scope}.json', verdict)
                    state['stages'][scope].update(status='complete', duration_ms=answer.duration_ms,
                        usage=answer.usage, completed_utc=utc(),
                        verdict_sha256=digest(cell / f'verdicts/{scope}.json'))
                    state['updated_utc'] = utc()
                    write(cell / 'state.json', state)
                    print(f'COMPLETE {job_id} {scope}', flush=True)
            verify_protected(m['sources'][job['source_id']])
            (verify_view if v2 else verify_snapshot)(root, job['source_id'])
            folder = root / f'frozen/eval_artifacts/paper{job["new_paper_id"]}'
            g = base.grader(folder)
            bundle = g.template()
            for scope in base.KINDS:
                verdict = read(cell / f'verdicts/{scope}.json')
                bundle[scope] = verdict['nodes']
                if scope == 'direction':
                    bundle['context'] = {k: row['value'] for k, row in verdict.get('context', {}).items()}
            write(cell / 'judgment_bundle.json', bundle)
            grade = g.grade_bundle(bundle)
            write(cell / 'grade.json', grade)
            state.update(status='complete', completed_utc=utc(), updated_utc=utc(),
                gates={k: v['status'] for k, v in grade['gates'].items()}, outcome=grade['outcome'],
                protected_files_unchanged=True, artifact_metadata_unchanged=True)
            write(cell / 'state.json', state)
        except QuotaWait as exc:
            n = state.get('quota_requeues', 0) + 1
            for s in state['stages'].values():
                if s['status'] == 'running':
                    s['status'] = 'quota_interrupted'
            if n > MAX_QUOTA_REQUEUES:
                state.update(status='failed', error=f'QuotaWait repeated {n}x: {exc}', updated_utc=utc())
                write(cell / 'state.json', state)
                pause(root, f'{job_id}: {state["error"]}')
                raise RuntimeError(state['error'])
            state.update(status='pending', quota_requeues=n, last_quota_wait=str(exc), updated_utc=utc())
            write(cell / 'state.json', state)
            print(f'REQUEUE {job_id} [{acct["name"]}]: {exc}', flush=True)
            sys.exit(EXIT_QUOTA)
        except BaseException as exc:
            state.update(status='failed', error=f'{type(exc).__name__}: {exc}', updated_utc=utc())
            for s in state['stages'].values():
                if s['status'] == 'running':
                    s['status'] = 'failed'
            if base.SUBMISSIONS.is_dir():
                write(cell / 'state.json', state)
                pause(root, f'{job_id}: {state["error"]}')
            raise


def summarize(root, m, active=None, active_model=None, status=None, reason=None):
    states = [read(root / j['id'] / 'state.json') for j in m['jobs']]
    counts = Counter(s['status'] for s in states)
    for key in m['model_order']:
        subset = [s for s in states if s['job']['model_key'] == key]
        mc = Counter(s['status'] for s in subset)
        summary = dict(model=key, counts=dict(mc), updated_utc=utc(), total=len(subset),
            pass_counts_by_repeat={str(r): {g: sum(s['status'] == 'complete' and s.get('gates', {}).get(g) == 'pass'
                for s in subset if s['job']['repeat'] == r) for g in ('G1', 'G2', 'G3')} for r in batch_repeats(m)})
        # These are independent gate decisions, not cumulative headline pass rates.
        summary['gate_count_definition'] = 'independent G1/G2/G3; cumulative outcomes are retained in each grade.json'
        if mc['complete'] == len(subset):
            per_paper = {}
            for n in batch_papers(m):
                cells = [root / f'{key}/P{n}/repeat_{r:02d}' for r in batch_repeats(m)]
                per_paper[f'P{n}'] = {scope: protocol.agreement([read(c / f'verdicts/{scope}.json') for c in cells]) for scope in base.KINDS}
                per_paper[f'P{n}']['gate_all_four_agree'] = {g: len({read(c / 'state.json')['gates'][g] for c in cells}) == 1 for g in ('G1', 'G2', 'G3')}
            summary['consistency'] = per_paper
        write(root / key / 'checkpoint.json', summary)
    result = dict(status=status or ('complete' if counts['complete'] == len(m['jobs']) else 'running'),
        completed=counts['complete'], total=len(m['jobs']), counts=dict(counts),
        completed_rubric_sessions=sum(stage.get('status') == 'complete' for s in states for stage in s['stages'].values()),
        total_rubric_sessions=2 * len(m['jobs']), active_jobs=active or [], active_model=active_model,
        reason=reason, updated_utc=utc())
    write(root / 'status.json', result)
    return result


def idle_slots(root, count):
    for n in range(count):
        with base.lock(root / f'slot_{n}.lock'):
            pass


def _supervise_single(root):
    base.mounted()
    m = read(root / 'manifest.json')
    with base.lock(root / 'supervisor.lock'):
        idle_slots(root, m['concurrency'])
        verify_frozen(root, m)
        active = {}
        error = None
        try:
            for key in m['model_order']:
                pending = [j for j in m['jobs'] if j['model_key'] == key and read(root / j['id'] / 'state.json')['status'] == 'pending']
                while pending or active:
                    base.mounted()
                    if (root / 'PAUSE.json').exists():
                        error = read(root / 'PAUSE.json')['reason']
                    for slot, (proc, job, log) in list(active.items()):
                        code = proc.poll()
                        if code is None:
                            continue
                        log.close()
                        del active[slot]
                        state = read(root / job['id'] / 'state.json')
                        if code or state['status'] != 'complete':
                            error = state.get('error', f'Worker stopped: {job["id"]}, status={state["status"]}, exit={code}')
                            pause(root, error)
                    while pending and len(active) < m['concurrency'] and not error:
                        if (root / 'PAUSE.json').exists():
                            error = read(root / 'PAUSE.json')['reason']
                            break
                        job = pending.pop(0)
                        snapshot(root, job['source_id'], m['sources'][job['source_id']])
                        slot = next(n for n in range(m['concurrency']) if n not in active)
                        logfile = (root / job['id'] / 'worker.log').open('a')
                        proc = subprocess.Popen([sys.executable, '-B', '-u', str(root / 'run_codex_judge.py'),
                            'worker', '--root', str(root), '--job', job['id'], '--slot', str(slot)],
                            stdin=subprocess.DEVNULL, stdout=logfile, stderr=subprocess.STDOUT)
                        active[slot] = (proc, job, logfile)
                        print(f'LAUNCH pid={proc.pid} {job["id"]}', flush=True)
                    summarize(root, m, [{'pid': p.pid, 'job': j['id']} for p, j, _ in active.values()], key,
                              'pausing' if error and active else 'paused' if error else 'running', error)
                    if error and not active:
                        return
                    if active:
                        time.sleep(15)
                if any(read(root / j['id'] / 'state.json')['status'] != 'complete' for j in m['jobs'] if j['model_key'] == key):
                    raise RuntimeError(f'Incomplete model checkpoint: {key}')
                print(f'MODEL COMPLETE {key}', flush=True)
            summarize(root, m, status='complete')
        except BaseException as exc:
            error = f'Supervisor: {type(exc).__name__}: {exc}'
            if base.SUBMISSIONS.is_dir():
                pause(root, error)
            for proc, _, log in active.values():
                proc.wait()
                log.close()
            if base.SUBMISSIONS.is_dir():
                summarize(root, m, status='paused', reason=error)
            raise


def supervise(root):
    m = read(root / 'manifest.json')
    return _supervise_multi(root) if accounts_of(m) else _supervise_single(root)


def remaining_stages(root, job):
    stages = read(root / job['id'] / 'state.json').get('stages', {})
    return sum(stages.get(s, {}).get('status') != 'complete' for s in base.KINDS)


def cleanup_model_snapshots(root, m, key):
    """Remove a completed model's snapshots (never read again); keep their snapshot.json records."""
    if not m.get('cleanup_completed_snapshots') or key not in m['model_order']:
        return
    if any(read(root / j['id'] / 'state.json')['status'] != 'complete' for j in m['jobs'] if j['model_key'] == key):
        return
    snaps = (root / 'snapshots' / key).resolve()
    if not snaps.is_dir() or snaps.parent != (root / 'snapshots').resolve():
        return
    records = root / 'snapshot_records' / key
    freed = 0
    for d in sorted(snaps.iterdir()):
        if d.is_symlink():
            raise RuntimeError(f'Unexpected link in snapshots: {d}')
        if (d / 'snapshot.json').is_file():
            (records / d.name).mkdir(parents=True, exist_ok=True)
            shutil.copy2(d / 'snapshot.json', records / d.name / 'snapshot.json')
            freed += read(d / 'snapshot.json').get('bytes', 0)
    shutil.rmtree(snaps)
    write(records / 'removed.json', dict(utc=utc(), bytes=freed,
        reason='all 60 evaluations of this model complete; snapshots are never read again'))
    print(f'SNAPSHOTS REMOVED {key}: {freed / 2**30:.2f} GiB', flush=True)


# --------------------------------------------------------------------------- revision 3: scope and full view
def scope_order(m=None):
    """Dispatch order. The paper's batch: its 240 evaluations, repeat_01 of every submission, then the
    extra repeats. A batch of other agents (prepare --agents): all four repeats of each submission,
    paper by paper, so the repeats of one submission share its staged view."""
    def ids(key, repeats, papers=range(1, 16)):
        return [f'{key}/P{n}/repeat_{r:02d}' for n in papers for r in repeats]
    if m is not None and tuple(m['model_order']) != PAPER_AGENTS:
        order = [i for key in m['model_order'] for i in ids(key, batch_repeats(m), batch_papers(m))]
        if set(order) != {j['id'] for j in m['jobs']}:
            raise RuntimeError('Scope order does not cover the batch jobs')
        return order
    order = ids('opus_4_8', (1,)) + ids('opus_5', (1, 2, 3, 4))
    for key in ('fable_5', 'sol_5_6', 'deepseek', 'kimi', 'glm'):
        order += ids(key, (1,))
    for key in ('fable_5', 'sol_5_6'):
        order += ids(key, (2, 3, 4))
    if len(set(order)) != 240 or not set(order) <= {j['id'] for j in make_jobs()}:
        raise RuntimeError('Scope order is not 240 distinct batch jobs')
    return order


def view_excluded(name):
    return ignored(name) or name in ENV_DIRS or name.startswith('.venv') or name in AGENT_CONFIG


def is_environment(folder):
    """A virtualenv or conda environment under any name (e.g. proposal/env with pyvenv.cfg)."""
    return (folder / 'pyvenv.cfg').is_file() or (folder / 'conda-meta').is_dir()


def link_problem(src, target, stack):
    if not target.exists():
        return 'dangling link'
    if target != src and not target.is_relative_to(src):
        return 'link outside the submission'
    if any(s == target or s.is_relative_to(target) for s in stack):
        return 'link cycle'
    parts = target.relative_to(src).parts
    if parts and parts[0] in TRANSCRIPTS or any(view_excluded(p) for p in parts):
        return 'link into content that is not staged'
    return None


def view_plan(source):
    """Files and directories of the full-submission view, with links inside the submission dereferenced."""
    src = Path(source).resolve()
    files, dirs, omitted, excluded = {}, [], [], Counter()

    def walk(real, rel, stack):
        with os.scandir(real) as it:
            entries = sorted(it, key=lambda e: e.name)
        for e in entries:
            r = f'{rel}/{e.name}' if rel else e.name
            if not rel and e.name in TOP_LEVEL_OMIT:
                omitted.append(dict(path=r, reason=TOP_LEVEL_OMIT[e.name]))
                continue
            if view_excluded(e.name):
                excluded[e.name if not e.name.endswith('.pyc') else '*.pyc'] += 1
                continue
            p = Path(e.path)
            if e.is_symlink():
                target = Path(os.path.realpath(p))
                why = link_problem(src, target, stack)
                if why:
                    omitted.append(dict(path=r, reason=why, link=os.readlink(p)))
                    continue
                p = target
            if p.is_dir():
                if is_environment(p):
                    omitted.append(dict(path=r, reason='environment (virtualenv/conda)'))
                    continue
                dirs.append(r)
                walk(p, r, stack + (p,))
            elif p.is_file():
                st = p.stat()
                entry = dict(size=st.st_size, mtime_ns=st.st_mtime_ns)
                real_rel = str(p.relative_to(src))
                if real_rel != r:
                    entry['from'] = real_rel
                files[r] = entry
            else:
                omitted.append(dict(path=r, reason='not a regular file or directory'))

    walk(src, '', (src,))
    if 'proposal' not in dirs:
        raise RuntimeError(f'No proposal/ in {src}')
    return dict(source=str(src), files=files, dirs=dirs, omitted=omitted, excluded_names=dict(excluded),
                outside=any(not r.startswith('proposal/') for r in files) or any(d != 'proposal' and not d.startswith('proposal/') for d in dirs))


def view_dest(rel):
    return rel if rel == 'proposal' or rel.startswith('proposal/') else f'{OUTSIDE}/{rel}'


def reference_path(root, sid):
    return root / 'snapshot_records' / sid / 'view_reference.json'


def v1_record(root, sid):
    """The proposal-only snapshot record, live or kept after the model-level cleanup."""
    for p in (root / 'snapshots' / sid / 'snapshot.json', root / 'snapshot_records' / sid / 'snapshot.json'):
        if p.is_file() and read(p).get('view_version') is None:
            return read(p)
    return None


def v1_equivalent(meta, plan):
    """True when the full view stages exactly the files of the old proposal-only snapshot."""
    if meta is None or plan['outside'] or any('from' in v for v in plan['files'].values()):
        return False
    now = {r[len('proposal/'):]: (v['size'], v['mtime_ns']) for r, v in plan['files'].items()}
    return now == {k: (v['size'], v['mtime_ns']) for k, v in meta['source_inventory'].items()}


def adopt_v1(root, sid, meta, plan):
    """Record that the existing proposal-only snapshot already is this submission's full view."""
    ref = reference_path(root, sid)
    if not ref.exists():
        ref.parent.mkdir(parents=True, exist_ok=True)
        write(ref, dict(created_utc=utc(), adopted_from_v1=True, outside=False, files=plan['files'],
                        dirs=plan['dirs'], omitted=plan['omitted'], excluded_names=plan['excluded_names'],
                        sha256={f'proposal/{k}': h for k, h in meta['sha256'].items()}))
    live = root / 'snapshots' / sid
    if (live / 'snapshot.json').is_file() and not (live / 'view_v2.json').exists():
        write(live / 'view_v2.json', dict(utc=utc(), equivalent_to_v1=True, files=len(plan['files'])))


def plain_inventory(folder):
    """Every file under a staged view, which holds no links or exclusions."""
    result = {}
    for sub in ('proposal', OUTSIDE):
        base_dir = folder / sub
        for here, dirs, files in os.walk(base_dir, followlinks=False):
            dirs.sort()
            for name in sorted(dirs + files):
                p = Path(here) / name
                if p.is_symlink():
                    raise RuntimeError(f'Unexpected link in a staged view: {p}')
                if p.is_file():
                    st = p.stat()
                    result[str(p.relative_to(folder))] = dict(size=st.st_size, mtime_ns=st.st_mtime_ns)
    return result


def copy_hashed(src, dst):
    import hashlib
    h = hashlib.sha256()
    with src.open('rb') as fi, dst.open('xb') as fo:
        for chunk in iter(lambda: fi.read(4 * 2**20), b''):
            h.update(chunk)
            fo.write(chunk)
    shutil.copystat(src, dst)
    return h.hexdigest()


_LIBC = None


def clone_file(src, dst):
    """APFS copy-on-write clone (macOS clonefile(2)): the staged copy shares the source's blocks, so a
    view of a large submission on the same volume takes no extra space. False when unavailable."""
    global _LIBC
    if sys.platform != 'darwin':
        return False
    if _LIBC is None:
        import ctypes, ctypes.util
        _LIBC = ctypes.CDLL(ctypes.util.find_library('c'), use_errno=True)
    if _LIBC.clonefile(os.fsencode(src), os.fsencode(dst), 0) != 0:
        return False
    shutil.copystat(src, dst)
    return True


def remove_snapshot(root, sid, reason):
    """Delete one staged copy (never an original); keep its record."""
    snaps = (root / 'snapshots').resolve()
    dest = (root / 'snapshots' / sid)
    if dest.is_symlink() or dest.resolve().parent.parent != snaps:
        raise RuntimeError(f'Refusing to remove unexpected snapshot path: {dest}')
    records = root / 'snapshot_records' / sid
    records.mkdir(parents=True, exist_ok=True)
    meta = read(dest / 'snapshot.json') if (dest / 'snapshot.json').is_file() else {}
    if meta:
        shutil.copy2(dest / 'snapshot.json', records / f'snapshot.removed_{stamp()}.json')
    shutil.rmtree(dest)
    with (records / 'removals.jsonl').open('a') as f:
        f.write(json.dumps(dict(utc=utc(), reason=reason, bytes=meta.get('bytes'))) + '\n')
    print(f'SNAPSHOT REMOVED {sid}: {reason}', flush=True)


def create_view(root, sid, source, plan=None):
    base.mounted()
    verify_protected(source)
    plan = plan or view_plan(source['source'])
    src = Path(plan['source'])
    ref_path = reference_path(root, sid)
    ref = read(ref_path) if ref_path.exists() else None
    if ref and ref['files'] != plan['files']:
        raise RuntimeError(f'Submission files differ from the first staged view: {sid}')
    size = sum(v['size'] for v in plan['files'].values())
    staging = (root / 'snapshots').resolve() if (root / 'snapshots').exists() else root   # may live on another disk
    # A source on the staging volume is cloned (copy-on-write) on macOS: its view needs no extra space.
    clone = sys.platform == 'darwin' and src.stat().st_dev == staging.stat().st_dev
    if shutil.disk_usage(staging).free < (0 if clone else size) + VIEW_RESERVE:
        raise RuntimeError(f'Insufficient space for {sid}: {size / 2**30:.2f} GiB view plus 5 GiB reserve required')
    partial = root / 'snapshots_partial' / f'{sid.replace("/", "_")}_{stamp()}'
    partial.mkdir(parents=True)
    (partial / 'proposal').mkdir()
    for d in plan['dirs']:
        (partial / view_dest(d)).mkdir(parents=True, exist_ok=True)
    hashes, retried = {}, {}

    def copy_one(item):
        rel, meta = item
        real = src / meta.get('from', rel)
        st = real.stat()
        if (st.st_size, st.st_mtime_ns) != (meta['size'], meta['mtime_ns']):
            raise RuntimeError(f'Artifact changed while being staged: {real}')
        dst = partial / view_dest(rel)
        dst.parent.mkdir(parents=True, exist_ok=True)
        # A USB SSD under heavy parallel reads can return one inconsistent read; retry the
        # copy, and after any mismatch also re-hash the source, so all reads must agree.
        for attempt in range(COPY_ATTEMPTS):
            if clone and clone_file(real, dst):
                h = digest(dst)
                if digest(real) == h:
                    return rel, h, attempt
            else:
                h = copy_hashed(real, dst)
                if digest(dst) == h and (attempt == 0 or digest(real) == h):
                    return rel, h, attempt
            dst.unlink()
            print(f'COPY RETRY {sid}: {rel} (attempt {attempt + 1} disagreed)', flush=True)
        raise RuntimeError(f'Staged copy differs from source after {COPY_ATTEMPTS} attempts: {real}')

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(COPY_THREADS) as pool:
        for rel, h, attempts in pool.map(copy_one, plan['files'].items()):
            hashes[rel] = h
            if attempts:
                retried[rel] = attempts
    if ref and ref['sha256'] != hashes:
        raise RuntimeError(f'Recreated view differs from the first staged view: {sid}')
    if view_plan(source['source'])['files'] != plan['files']:
        raise RuntimeError(f'Artifacts changed while being copied: {sid}')
    inv = plain_inventory(partial)
    if len(inv) != len(plan['files']):
        raise RuntimeError(f'Staged view has {len(inv)} files, plan has {len(plan["files"])}: {sid}')
    write(partial / 'snapshot.json', dict(view_version=2, source=plan['source'], created_utc=utc(), bytes=size,
        outside=plan['outside'], files=plan['files'], dirs=plan['dirs'], omitted=plan['omitted'],
        excluded_names=plan['excluded_names'], sha256=hashes, snapshot_inventory=inv, copy_retries=retried,
        cloned=clone))
    if not ref:
        ref_path.parent.mkdir(parents=True, exist_ok=True)
        write(ref_path, dict(created_utc=utc(), adopted_from_v1=False, outside=plan['outside'], files=plan['files'],
                             dirs=plan['dirs'], omitted=plan['omitted'], excluded_names=plan['excluded_names'],
                             sha256=hashes))
    dest = root / 'snapshots' / sid
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial.rename(dest)
    print(f'VIEW {sid}: {len(hashes)} files, {size / 2**30:.2f} GiB, outside proposal: {plan["outside"]}', flush=True)


def ensure_view(root, m, sid):
    """Stage the full-submission view once; adopt an equivalent proposal-only snapshot, retire a different one."""
    dest = root / 'snapshots' / sid
    if (dest / 'snapshot.json').is_file():
        meta = read(dest / 'snapshot.json')
        if meta.get('view_version') == 2 or (dest / 'view_v2.json').is_file():
            return
        plan = view_plan(m['sources'][sid]['source'])
        if v1_equivalent(meta, plan):
            adopt_v1(root, sid, meta, plan)
            return
        remove_snapshot(root, sid, 'proposal-only snapshot differs from the full-submission view')
    elif dest.exists():
        raise RuntimeError(f'Partial snapshot needs review: {dest}')
    create_view(root, sid, m['sources'][sid])


def view_has_outside(root, sid):
    return read(reference_path(root, sid))['outside']


def view_dirs(root, sid):
    dest = root / 'snapshots' / sid
    return [dest / 'proposal'] + ([dest / OUTSIDE] if (dest / OUTSIDE).is_dir() else [])


def verify_view(root, sid):
    dest = root / 'snapshots' / sid
    meta = read(dest / 'snapshot.json')
    ref = read(reference_path(root, sid))
    if meta.get('view_version') == 2:
        if plain_inventory(dest) != meta['snapshot_inventory']:
            raise RuntimeError(f'Frozen artifact metadata changed: {sid}')
    elif (dest / 'view_v2.json').is_file():
        if inventory(dest / 'proposal') != meta['snapshot_inventory']:
            raise RuntimeError(f'Frozen artifact metadata changed: {sid}')
    else:
        raise RuntimeError(f'Snapshot is not a full-submission view: {sid}')
    if view_plan(meta['source'])['files'] != ref['files']:
        raise RuntimeError(f'Original artifact metadata changed: {sid}')


def stage_view_workspace(root, cell, job, scope):
    ws = stage_workspace(root, cell, job, scope)
    outside = root / 'snapshots' / job['source_id'] / OUTSIDE
    link = ws / OUTSIDE
    if view_has_outside(root, job['source_id']):
        if not link.is_symlink():
            link.symlink_to(outside, target_is_directory=True)
        if link.resolve() != outside.resolve():
            raise RuntimeError(f'Unexpected artifact link: {link}')
    elif link.exists() or link.is_symlink():
        raise RuntimeError(f'Unexpected staged entry: {link}')
    return ws


def workdir_prompt(text):
    if text.count(PROMPT_ANCHOR) != 1:
        raise RuntimeError('Prompt anchor for the working-directory note not found exactly once')
    return text.replace(PROMPT_ANCHOR, f'{PROMPT_ANCHOR} {WORKDIR_NOTE}')


def prompt_file(root, job, scope):
    inp = root / f'inputs/P{job["new_paper_id"]}'
    return inp / (f'{scope}.prompt_workdir.txt' if view_has_outside(root, job['source_id']) else f'{scope}.prompt.txt')


def evict_views(root, pending, active_sids, building=()):
    """Remove staged copies that no running job and none of the next queued jobs need."""
    keep = set(active_sids) | {j['source_id'] for j in pending[:EVICT_LOOKAHEAD]} | set(building)
    in_progress = tuple(f'{sid.replace("/", "_")}_' for sid in building)
    snaps = root / 'snapshots'
    for model_dir in sorted(p for p in snaps.iterdir() if p.is_dir()) if snaps.is_dir() else []:
        for d in sorted(p for p in model_dir.iterdir() if p.is_dir()):
            sid = f'{model_dir.name}/{d.name}'
            if sid not in keep and (d / 'snapshot.json').is_file():
                remove_snapshot(root, sid, 'no running or soon-queued job needs it')
    for stale in sorted((root / 'snapshots_partial').glob('*')) if (root / 'snapshots_partial').is_dir() else []:
        if stale.name.startswith(in_progress):
            continue                                   # a view the prefetcher is building right now
        if stale.is_dir() and not stale.is_symlink() and stale.parent.resolve() == (root / 'snapshots_partial').resolve():
            shutil.rmtree(stale)
            print(f'PARTIAL VIEW REMOVED {stale.name}', flush=True)


class ViewPrefetcher:
    """Builds the views of upcoming submissions in background threads, so starting an
    evaluation never waits on a copy and several copies proceed at once. A failed build
    is re-raised in the supervisor (which then pauses, as a synchronous failure did)."""

    def __init__(self, root, m, threads=PREFETCH_THREADS):
        from concurrent.futures import ThreadPoolExecutor
        self.root, self.m, self.threads = root, m, threads
        self.pool = ThreadPoolExecutor(threads)
        self.jobs = {}                                   # sid -> future

    def building(self):
        return set(self.jobs)

    def ready(self, sid):
        if sid in self.jobs:
            return False
        dest = self.root / 'snapshots' / sid
        if not (dest / 'snapshot.json').is_file():
            return False
        return read(dest / 'snapshot.json').get('view_version') == 2 or (dest / 'view_v2.json').is_file()

    def request(self, pending):
        sids = []
        for j in pending:
            if j['source_id'] not in sids:
                sids.append(j['source_id'])
            if len(sids) >= PREFETCH_AHEAD:
                break
        for sid in sids:
            if len(self.jobs) >= self.threads:
                break
            if sid not in self.jobs and not self.ready(sid):
                self.jobs[sid] = self.pool.submit(ensure_view, self.root, self.m, sid)

    def poll(self):
        for sid, f in list(self.jobs.items()):
            if f.done():
                del self.jobs[sid]
                f.result()

    def next_ready(self, pending):
        return next((j for j in pending[:DISPATCH_WINDOW] if self.ready(j['source_id'])), None)


def supersede(root, job_id, why):
    """Archive a started evaluation whose inputs changed; the job becomes pending again."""
    cell = root / job_id
    s = read(cell / 'state.json')
    arch = cell / f'superseded_{stamp()}'
    arch.mkdir()
    shutil.copy2(cell / 'state.json', arch / 'state.json')
    for name in ('verdicts', 'judgment_bundle.json', 'grade.json', 'workspace_direction', 'workspace_integrity',
                 'usage_limits.json'):
        if (cell / name).exists() or (cell / name).is_symlink():
            (cell / name).rename(arch / name)
    (cell / 'verdicts').mkdir()
    history = s.get('superseded', []) + [dict(utc=utc(), reason=why, archive=arch.name, previous_status=s['status'])]
    write(cell / 'state.json', dict(status='pending', job=s['job'], stages={}, updated_utc=utc(), superseded=history))


def summarize_selected(root, m, active=None, status=None, reason=None):
    order = m['selection']['order']
    states = {i: read(root / i / 'state.json') for i in order}
    counts = Counter(s['status'] for s in states.values())
    per_model = {}
    for key in m['model_order']:
        ids = [i for i in order if i.startswith(f'{key}/')]
        per_model[key] = dict(selected=len(ids), complete=sum(states[i]['status'] == 'complete' for i in ids),
                              counts=dict(Counter(states[i]['status'] for i in ids)))
    result = dict(status=status or ('complete' if counts['complete'] == len(order) else 'running'),
        completed=counts['complete'], total=len(order), counts=dict(counts), per_model=per_model,
        scope=m['selection']['description'], active_jobs=active or [], reason=reason, updated_utc=utc())
    write(root / 'status.json', result)
    return result


def rescope(root, m, reason, installed):
    """Apply revision 3 to an idle batch: prompts, equivalence decisions, superseding and the selection."""
    jobs = {j['id']: j for j in m['jobs']}
    order = scope_order(m)
    paper_batch = tuple(m['model_order']) == PAPER_AGENTS
    for n in range(1, 16):
        for scope in base.KINDS:
            inp = root / f'inputs/P{n}'
            out = inp / f'{scope}.prompt_workdir.txt'
            text = workdir_prompt((inp / f'{scope}.prompt.txt').read_text())
            if out.exists() and out.read_text() != text:
                raise RuntimeError(f'Existing working-directory prompt differs: {out}')
            out.write_text(text)
            m['fixed_hashes'][f'inputs/P{n}/{scope}.prompt_workdir.txt'] = digest(out)

    def started(i):
        s = read(root / i / 'state.json')
        return s['status'] != 'pending' or bool(s.get('stages'))

    equivalent, changed, superseded = [], [], []
    for sid in sorted({jobs[i]['source_id'] for i in order if started(i)}):
        plan = view_plan(m['sources'][sid]['source'])
        meta = v1_record(root, sid)
        if v1_equivalent(meta, plan):
            adopt_v1(root, sid, meta, plan)
            equivalent.append(sid)
            continue
        changed.append(sid)
        for i in order:
            if jobs[i]['source_id'] == sid and started(i):
                supersede(root, i, 'full-submission view differs from the proposal-only snapshot it was judged on')
                superseded.append(i)
        if (root / 'snapshots' / sid / 'snapshot.json').is_file():
            remove_snapshot(root, sid, 'proposal-only snapshot differs from the full-submission view')
    bonus = [j['id'] for j in m['jobs'] if j['id'] not in set(order)
             and read(root / j['id'] / 'state.json')['status'] == 'complete']
    m['view_version'] = 2
    m['selection'] = dict(order=order, total=len(order), bonus_complete=bonus, superseded=superseded,
        equivalent_sources=equivalent, changed_sources=changed, utc=utc(),
        description=('repeat_01 of all 105 submissions, plus repeats 02-04 of '
                     f'{", ".join(REPEATED_MODELS)} (240 evaluations)' if paper_batch else
                     f'repeats 01-04 of all {len(order) // 4} submissions of {", ".join(m["model_order"])} '
                     f'({len(order)} evaluations)'))
    m['view_policy'] = dict(outside_dir=OUTSIDE, top_level_omitted=TOP_LEVEL_OMIT, agent_config=sorted(AGENT_CONFIG),
        env_dirs=sorted(ENV_DIRS | SKIP_DIRS) + ['.venv*'], other_exclusions=['eval_results*', '*.pyc', '._*'],
        links='inside the submission: copied as real files; dangling, external, cyclic or into unstaged content: recorded, not staged',
        prompt_note=WORKDIR_NOTE, prompt_anchor=PROMPT_ANCHOR,
        equivalence='identical files to the proposal-only snapshot, no links, nothing outside proposal/ -> same inputs and prompt')
    m.setdefault('code_revisions', []).append(dict(utc=utc(), revision=3, installed=installed, reason=reason,
        judging_inputs_changed=bool(changed), superseded=superseded, changed_sources=changed,
        equivalent_sources=equivalent, bonus_complete=len(bonus)))
    write(root / 'manifest.json', m)
    print(f'RESCOPED: {len(order)} selected; {len(equivalent)} started sources unchanged, '
          f'{len(changed)} changed -> {len(superseded)} evaluation(s) superseded; {len(bonus)} bonus', flush=True)
    return m


def probe(root, name):
    """Free quota reading (rateLimits/read, no inference) for one account."""
    m = read(root / 'manifest.json')
    acct = next(a for a in accounts_of(m) if a['name'] == name)
    use_account(acct)
    os.environ.pop('OPENAI_API_KEY', None)
    from openai_codex import Codex
    from openai_codex.client import CodexConfig
    with Codex(CodexConfig(codex_bin=m['codex_bin'])) as c:
        limits = base.limits(c)
    record_usage(root, name, limits, 'probe')
    return limits


def run_probe(root, name):
    out = subprocess.run([sys.executable, '-B', str(root / 'run_codex_judge.py'), 'probe', '--root', str(root),
                          '--account', name], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=300)
    if out.returncode:
        print(f'PROBE FAILED {name}: {out.stderr[-500:]}', flush=True)


def needs_probe(root, acct, blocked):
    rec = read_usage(root, acct['name'])
    if rec is None:
        return True
    age = time.time() - rec.get('epoch', 0)
    if any(r and r <= time.time() for _, _, _, r in windows(rec['limits'])):
        return age > 60
    return bool(blocked) and age > PROBE_INTERVAL_S


def write_accounts_status(root, m, active, blocks):
    rows = []
    for a in accounts_of(m):
        rec = read_usage(root, a['name']) or {}
        rows.append(dict(account=a['name'], cap=a['cap'], reading_utc=rec.get('utc'),
            windows=[dict(limit=lid, window=k, used=u, resets=fmt_ts(r)) for lid, k, u, r in windows(rec.get('limits') or {})],
            in_flight=sum(1 for v in active.values() if v[3] == a['name']), blocked=blocks.get(a['name'])))
    write(root / 'accounts_status.json', dict(updated_utc=utc(), accounts=rows))


def _supervise_multi(root):
    base.mounted()
    m = read(root / 'manifest.json')
    accts, per = accounts_of(m), slots_per_account(m)
    total = per * len(accts)
    with base.lock(root / 'supervisor.lock'):
        idle_slots(root, total)
        verify_frozen(root, m)
        selected = m.get('view_version') == 2
        if selected:
            by_id = {j['id']: j for j in m['jobs']}
            groups = [('selection', [by_id[i] for i in m['selection']['order']])]
            views = ViewPrefetcher(root, m)
        else:
            for key in m['model_order']:
                cleanup_model_snapshots(root, m, key)
            groups = [(key, [j for j in m['jobs'] if j['model_key'] == key]) for key in m['model_order']]
        active = {}                                        # slot -> (proc, job, logfile, account name)
        error = None
        try:
            for key, group in groups:
                pending = [j for j in group if read(root / j['id'] / 'state.json')['status'] == 'pending']
                while pending or active:
                    base.mounted()
                    if (root / 'PAUSE.json').exists():
                        error = read(root / 'PAUSE.json')['reason']
                    for slot, (proc, job, log, acc) in list(active.items()):
                        code = proc.poll()
                        if code is None:
                            continue
                        log.close()
                        del active[slot]
                        state = read(root / job['id'] / 'state.json')
                        if code == EXIT_QUOTA and state['status'] == 'pending':
                            pending.insert(0, job)
                            print(f'REQUEUED {job["id"]} [{acc}]', flush=True)
                        elif code or state['status'] != 'complete':
                            error = state.get('error', f'Worker stopped: {job["id"]}, status={state["status"]}, exit={code}')
                            pause(root, error)
                    blocks = {}
                    if not error:
                        for a in accts:
                            inflight = [v[1] for v in active.values() if v[3] == a['name']]
                            load = sum(remaining_stages(root, j) for j in inflight)
                            if needs_probe(root, a, account_block(root, a, load, m)):
                                run_probe(root, a['name'])
                            blocks[a['name']] = account_block(root, a, load, m)
                        if selected:
                            views.poll()
                            views.request(pending)
                            evict_views(root, pending, [v[1]['source_id'] for v in active.values()], views.building())
                        for slot in range(total):
                            if slot in active:
                                continue
                            if not pending or (root / 'PAUSE.json').exists():
                                break
                            a = accts[slot // per]
                            inflight = [v[1] for v in active.values() if v[3] == a['name']]
                            if account_block(root, a, sum(remaining_stages(root, j) for j in inflight), m):
                                continue
                            if selected:
                                job = views.next_ready(pending)
                                if job is None:
                                    break
                                pending.remove(job)
                            else:
                                job = pending[0]
                                snapshot(root, job['source_id'], m['sources'][job['source_id']])
                                pending.pop(0)
                            logfile = (root / job['id'] / 'worker.log').open('a')
                            proc = subprocess.Popen([sys.executable, '-B', '-u', str(root / 'run_codex_judge.py'),
                                'worker', '--root', str(root), '--job', job['id'], '--slot', str(slot)],
                                stdin=subprocess.DEVNULL, stdout=logfile, stderr=subprocess.STDOUT)
                            active[slot] = (proc, job, logfile, a['name'])
                            print(f'LAUNCH pid={proc.pid} {job["id"]} [{a["name"]} slot {slot}]', flush=True)
                    write_accounts_status(root, m, active, blocks)
                    waiting = bool(pending) and not active and not error
                    reason = error or ('; '.join(f'{k}: {v}' for k, v in blocks.items() if v) if waiting else None)
                    if waiting and selected and not reason and views.building():
                        reason = f'building views: {", ".join(sorted(views.building()))}'
                    now = [{'pid': p.pid, 'job': j['id'], 'account': acc} for p, j, _, acc in active.values()]
                    phase = 'pausing' if error and active else 'paused' if error else 'waiting_quota' if waiting else 'running'
                    if selected:
                        summarize_selected(root, m, now, phase, reason)
                    else:
                        summarize(root, m, now, key, phase, reason)
                    if error and not active:
                        return
                    time.sleep(15 if active or (selected and views.building()) else 60)
                if any(read(root / j['id'] / 'state.json')['status'] != 'complete' for j in group):
                    raise RuntimeError(f'Incomplete model checkpoint: {key}')
                print(f'MODEL COMPLETE {key}', flush=True)
                if selected:
                    evict_views(root, [], [])
                else:
                    cleanup_model_snapshots(root, m, key)
            (summarize_selected if selected else summarize)(root, m, status='complete')
        except BaseException as exc:
            error = f'Supervisor: {type(exc).__name__}: {exc}'
            if base.SUBMISSIONS.is_dir():
                pause(root, error)
            for proc, _, log, _ in active.values():
                proc.wait()
                log.close()
            if base.SUBMISSIONS.is_dir():
                (summarize_selected if selected else summarize)(root, m, status='paused', reason=error)
            raise


def revise(root, reason):
    """Install this file into an idle batch as a recorded code revision; judging inputs are untouched."""
    base.mounted()
    with base.lock(root / 'launch.lock'), base.lock(root / 'supervisor.lock'):
        m = read(root / 'manifest.json')
        idle_slots(root, m['concurrency'])
        running = [j['id'] for j in m['jobs'] if read(root / j['id'] / 'state.json')['status'] == 'running']
        if running:
            raise RuntimeError(f'Jobs still marked running: {running[:4]}')
        src = Path(__file__).resolve()
        if src == (root / 'run_codex_judge.py').resolve():
            raise RuntimeError('Run revise from the repository copy, not the frozen one')
        old_sha, new_sha = digest(root / 'run_codex_judge.py'), digest(src)
        (root / 'code_history').mkdir(exist_ok=True)
        shutil.copy2(root / 'run_codex_judge.py', root / 'code_history' / f'run_astra_batch.{old_sha[:12]}.py')
        shutil.copy2(src, root / 'run_codex_judge.py')
        if digest(root / 'run_codex_judge.py') != new_sha:
            raise RuntimeError('Revision copy mismatch')
        m['fixed_hashes']['run_codex_judge.py'] = new_sha
        m['accounts'] = [dict(a) for a in ACCOUNTS]
        m['slots_per_account'] = SLOTS_PER_ACCOUNT
        m['concurrency'] = SLOTS_PER_ACCOUNT * len(ACCOUNTS)
        m['stage_cost_estimate'] = STAGE_COST
        m['cleanup_completed_snapshots'] = True
        m.setdefault('code_revisions', []).append(dict(utc=utc(), file='run_codex_judge.py', old_sha256=old_sha,
            new_sha256=new_sha, reason=reason, judging_inputs_changed=False,
            accounts=[a['name'] for a in ACCOUNTS], slots=m['concurrency']))
        write(root / 'manifest.json', m)
        print(f'REVISED run_codex_judge.py {old_sha[:12]} -> {new_sha[:12]}; accounts '
              f'{[a["name"] for a in ACCOUNTS]}, {m["concurrency"]} slots', flush=True)


def install_code(root, m, names):
    """Copy repository code files into a batch, keeping the replaced versions in code_history/."""
    out = {}
    (root / 'code_history').mkdir(exist_ok=True)
    for name in names:
        src = Path(__file__).resolve().parent / name
        if src == (root / name).resolve():
            raise RuntimeError('Run from the repository copy, not the frozen one')
        old, new = digest(root / name), digest(src)
        if old != new:
            shutil.copy2(root / name, root / 'code_history' / f'{Path(name).stem}.{old[:12]}.py')
            shutil.copy2(src, root / name)
            if digest(root / name) != new:
                raise RuntimeError(f'Revision copy mismatch: {name}')
        m['fixed_hashes'][name] = new
        out[name] = dict(old_sha256=old, new_sha256=new)
    return out


def worker_alive(cell):
    try:
        with base.lock(cell / 'worker.lock'):
            return False
    except BlockingIOError:
        return True


def update_code(root, m, reason, names):
    """Install repository code fixes into an idle batch; judging inputs and settings are untouched."""
    # A state left at 'running' by a killed process (e.g. the laptop shut down) is stale:
    # only a live worker holds its cell's worker.lock (flock is released on process exit).
    live = [j['id'] for j in m['jobs'] if read(root / j['id'] / 'state.json')['status'] == 'running'
            and worker_alive(root / j['id'])]
    if live:
        raise RuntimeError(f'Workers still running: {live[:4]}')
    installed = install_code(root, m, names)
    m.setdefault('code_revisions', []).append(dict(utc=utc(), installed=installed, reason=reason,
                                                   judging_inputs_changed=False))
    write(root / 'manifest.json', m)
    for name, h in installed.items():
        print(f'UPDATED {name} {h["old_sha256"][:12]} -> {h["new_sha256"][:12]}', flush=True)


def set_caps(root, cap, reason):
    """Change every account's usage cap on an idle batch (a scheduling margin, not a judging input)."""
    base.mounted()
    with base.lock(root / 'launch.lock'), base.lock(root / 'supervisor.lock'):
        m = read(root / 'manifest.json')
        idle_slots(root, m['concurrency'])
        old = {a['name']: a['cap'] for a in accounts_of(m)}
        for a in m['accounts']:
            a['cap'] = cap
        m.setdefault('settings_history', []).append(dict(utc=utc(), caps=[old, cap], reason=reason))
        write(root / 'manifest.json', m)
        print(f'CAPS {old} -> {cap}', flush=True)


def update_code_batch(root, reason):
    base.mounted()
    with base.lock(root / 'launch.lock'), base.lock(root / 'supervisor.lock'):
        m = read(root / 'manifest.json')
        idle_slots(root, m['concurrency'])
        update_code(root, m, reason, ['run_codex_judge.py'])


def rescope_batch(root, reason, slots=None):
    base.mounted()
    with base.lock(root / 'launch.lock'), base.lock(root / 'supervisor.lock'):
        m = read(root / 'manifest.json')
        idle_slots(root, m['concurrency'])
        running = [j['id'] for j in m['jobs'] if read(root / j['id'] / 'state.json')['status'] == 'running']
        if running:
            raise RuntimeError(f'Jobs still marked running: {running[:4]}')
        if m.get('view_version') == 2:
            raise RuntimeError('Batch already rescoped')
        if accounts_of(m):
            m['slots_per_account'] = slots or RESCOPE_SLOTS_PER_ACCOUNT
            m['concurrency'] = m['slots_per_account'] * len(accounts_of(m))
        rescope(root, m, reason, install_code(root, m, ['run_codex_judge.py']))


def launch(root, resume=False):
    base.mounted()
    with base.lock(root / 'launch.lock'), base.lock(root / 'supervisor.lock'):
        m = read(root / 'manifest.json')
        idle_slots(root, m['concurrency'])
        verify_frozen(root, m)
        if (root / 'launch.json').exists() and not resume:
            raise RuntimeError('Already launched; use explicit resume to retain checkpoints')
        if accounts_of(m):
            for a in accounts_of(m):
                run_probe(root, a['name'])
            readings = {a['name']: read_usage(root, a['name']) for a in accounts_of(m)}
            if not any(readings.values()):
                raise RuntimeError('No account returned a usage reading; check the logins')
            write(root / f'preflight_usage_limits_{stamp()}.json', readings)
        else:
            from openai_codex import Codex
            from openai_codex.client import CodexConfig
            os.environ.pop('OPENAI_API_KEY', None)
            with Codex(CodexConfig(codex_bin=m['codex_bin'])) as c:
                q = base.limits(c)
                write(root / 'preflight_usage_limits.json', q)
                if reason := base.quota_reason(q):
                    raise RuntimeError(reason)
        if resume:
            for j in m['jobs']:
                cell = root / j['id']
                s = read(cell / 'state.json')
                if s['status'] not in ('pending', 'complete'):
                    history = cell / 'state_history'
                    history.mkdir(exist_ok=True)
                    write(history / f'{stamp()}.json', s)
                    s.update(status='pending', updated_utc=utc())
                    s.pop('error', None)
                    write(cell / 'state.json', s)
            if (root / 'PAUSE.json').exists():
                (root / 'PAUSE.json').rename(root / f'pause_history_{stamp()}.json')
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
        with (root / 'supervisor.log').open('a') as logfile:
            proc = subprocess.Popen([sys.executable, '-B', '-u', str(root / 'run_codex_judge.py'),
                'supervise', '--root', str(root)], stdin=subprocess.DEVNULL,
                stdout=logfile, stderr=subprocess.STDOUT, start_new_session=True, env=env)
        record = dict(pid=proc.pid, utc=utc(), resume=resume, concurrency=m['concurrency'])
        write(root / 'launch.json', record)
        (root / 'launch_history').mkdir(exist_ok=True)
        write(root / 'launch_history' / f'{stamp()}.json', record)
        if Path('/usr/bin/caffeinate').exists():
            subprocess.Popen(['/usr/bin/caffeinate', '-i', '-w', str(proc.pid)], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        print(f'BATCH_PID={proc.pid}\nBATCH_DIRECTORY={root}', flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('command', choices=('prepare', 'launch', 'resume', 'supervise', 'worker', 'status', 'pause',
                                        'probe', 'revise', 'rescope', 'update-code', 'set-caps'))
    ap.add_argument('--root', type=Path)
    ap.add_argument('--job')
    ap.add_argument('--slot', type=int)
    ap.add_argument('--account')
    ap.add_argument('--reason')
    ap.add_argument('--cap', type=float)
    ap.add_argument('--agents', nargs='+', help='prepare: agents to judge, by src/solver/run.sh directory name '
                    '(e.g. astra-6); default: the paper\'s seven')
    ap.add_argument('--papers', type=int, nargs='+', help='prepare (with --agents): judge only these papers '
                    '(e.g. the finished ones); default: all 15')
    ap.add_argument('--repeats', type=int, nargs='+', help='prepare (with --agents): judge only these repeats, e.g. 2 3 4 '
                    'to add three judgments to an earlier batch\'s repeat 1; default: 1-4')
    ap.add_argument('--slots-per-account', type=int, help=f'rescope: concurrent judges per account '
                    f'(default {RESCOPE_SLOTS_PER_ACCOUNT})')
    args = ap.parse_args()
    if args.command == 'prepare':
        prepare(select_agents(args.agents), args.papers, args.repeats)
        return
    base.mounted()
    if not args.root:
        ap.error('--root is required')
    root = args.root.resolve()
    if not root.is_relative_to((base.JUDGE_RUNS).resolve()) or not root.name.startswith(PREFIX):
        ap.error('Root must be a prepared revised-rubric batch, not a legacy run')
    if args.command == 'supervise':
        time.sleep(1)  # Let the launcher release its supervisor lock.
        supervise(root)
    elif args.command in ('launch', 'resume'):
        launch(root, args.command == 'resume')
    elif args.command == 'worker':
        if args.job is None or args.slot is None or not 0 <= args.slot < read(root / 'manifest.json')['concurrency']:
            ap.error('--job and a valid --slot are required')
        worker(root, args.job, args.slot)
    elif args.command == 'probe':
        if not args.account:
            ap.error('--account is required')
        print(json.dumps(probe(root, args.account), default=base.serial))
    elif args.command == 'set-caps':
        if not args.reason or args.cap is None or not 0 < args.cap <= 1:
            ap.error('--cap (0-1] and --reason are required')
        set_caps(root, args.cap, args.reason)
    elif args.command in ('revise', 'rescope', 'update-code'):
        if not args.reason:
            ap.error('--reason is required')
        if args.command == 'rescope':
            rescope_batch(root, args.reason, args.slots_per_account)
        else:
            dict(revise=revise).get(args.command, update_code_batch)(root, args.reason)
    elif args.command == 'pause':
        pause(root, 'Manual pause; in-flight stages finish and checkpoint')
    else:
        print(json.dumps(read(root / 'status.json'), indent=2))


if __name__ == '__main__':
    main()
