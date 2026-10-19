#!/usr/bin/env python3
"""Keep a run's memory below its job limit, so the agent's own experiments cannot take the agent down with them.

WHY THIS EXISTS
---------------
On a batch system the runner, the agent harness and every process the agent starts share one memory budget: under
PBS, the job's memory cgroup, capped at the job's memory request. When the memory that cannot be reclaimed -- the
processes' own memory, files in RAM-backed directories (tmpfs), and file pages written faster than they reach the
disk -- fills it, the kernel's out-of-memory killer fires inside the job and the scheduler ends the whole job, runner
and agent included, in the middle of the research. (Cgroup v1 has no per-cgroup writeback, so a run writing a few
hundred GB, such as container images, can get there with dirty page cache alone.) That is a failure of the evaluation
harness, not of the agent, so the runner prevents it:

  1. From SOLVER_MEMORY_DROP_AT of the limit (default 0.80, counting page cache), the large files the job has open,
     memory-mapped or wrote (workspace, node-local scratch, model and container caches) are flushed to disk and their
     cached pages released (fdatasync + posix_fadvise(DONTNEED)). Nothing is deleted; data is read again if needed.
  2. Only if the memory that cannot be reclaimed still exceeds SOLVER_MEMORY_KILL_AT of the limit (default 0.93) is a
     process ended: the process the agent launched that has the most resident memory, with its children. Never the
     runner, the agent harness, or anything the job script started next to the runner (a model server, for example).
     A notice is written to that process's stderr, where the agent will look when it sees it die (as with an
     out-of-memory kill), and is added to the agent's next prompt.

It also moves temporary files off a RAM-backed TMPDIR (on many HPC nodes /var/tmp is tmpfs) onto node-local disk
($PBS_LOCALDIR, or SOLVER_SCRATCH_DIR): files there are memory that cannot be released, only deleted.

USAGE (in a runner)
-------------------
    from memory_watchdog import use_disk_tmpdir, start_memory_watchdog, pending_notices

    use_disk_tmpdir()                                    # before the agent harness is started
    start_memory_watchdog(workspace, on_event=log)       # log(kind, info) records into the trajectory
    ...
    agent.send(prompt + pending_notices())               # tells the agent what the watchdog did, if anything

Settings (environment): SOLVER_MEMORY_WATCHDOG=0 turns it off. SOLVER_MEMORY_LIMIT_GB sets the limit where the job
has none (or a lower one); SOLVER_MEMORY_DROP_AT / SOLVER_MEMORY_KILL_AT are fractions of it; SOLVER_MEMORY_POLL_S is
the check interval (default 10); SOLVER_MEMORY_CACHE_DIRS adds directories (colon-separated) whose cache may be
dropped. Linux only; without a job memory limit (and no SOLVER_MEMORY_LIMIT_GB) it does nothing, since the kernel's
own out-of-memory handling then ends the largest process rather than the whole run.
"""
from __future__ import annotations

import os
import signal
import stat
import sys
import threading
import time
from pathlib import Path

GIB = 1 << 30
MIN_FILE = 8 << 20            # files smaller than this are not worth an fadvise
MIN_VICTIM = 1 * GIB          # a process smaller than this is not worth stopping
SCAN_BUDGET_S = 90            # time budget of one cache-drop pass
SKIP_DIRS = {'.git', 'node_modules', '__pycache__', '.venv', 'venv'}
HARNESS_NAMES = ('claude', 'codex', 'kimi', 'kimi-code', 'qwen', 'qwen-code', 'dsh', 'openhands', 'tmux')
_NOTICES: list[str] = []
_LOCK = threading.Lock()


def _env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return float(default)


def _gib(n):
    return round(n / GIB, 1)


# ------------------------------------------------------------------ memory accounting (cgroup v1 / v2)
def _cgroup_dir():
    """(version, directory) of this process's memory cgroup, or (None, None)."""
    try:
        lines = Path('/proc/self/cgroup').read_text().splitlines()
    except OSError:
        return None, None
    for line in lines:                                   # v1: "4:memory:/pbs_jobs.service/jobid/123.pbs1"
        _hid, ctrls, path = line.split(':', 2)
        if 'memory' in ctrls.split(','):
            d = Path('/sys/fs/cgroup/memory') / path.lstrip('/')
            if (d / 'memory.usage_in_bytes').exists():
                return 1, d
    for line in lines:                                   # v2: "0::/system.slice/..."
        hid, _ctrls, path = line.split(':', 2)
        if hid == '0':
            d = Path('/sys/fs/cgroup') / path.lstrip('/')
            if (d / 'memory.current').exists():
                return 2, d
    return None, None


def _read_int(path):
    try:
        v = Path(path).read_text().strip()
        return None if v == 'max' else int(v)
    except (OSError, ValueError):
        return None


def _read_stat(path):
    out = {}
    try:
        for line in Path(path).read_text().splitlines():
            k, v = line.split()
            out[k] = int(v)
    except (OSError, ValueError):
        pass
    return out


def _mem_total():
    try:
        for line in Path('/proc/meminfo').read_text().splitlines():
            if line.startswith('MemTotal:'):
                return int(line.split()[1]) * 1024
    except OSError:
        pass
    return None


class Accounting:
    """The job's memory cgroup: its limit (the job's memory request) and what is using it."""

    def __init__(self):
        self.version, self.dir = _cgroup_dir()
        total = _mem_total()
        limit = None
        if self.version == 1:
            limit = _read_int(self.dir / 'memory.limit_in_bytes')
        elif self.version == 2:
            limit = _read_int(self.dir / 'memory.max')
        if limit is not None and total and limit >= total:
            limit = None                                 # unlimited cgroup: no job limit
        override = _env_float('SOLVER_MEMORY_LIMIT_GB', 0)
        if override > 0:
            limit = min(limit or int(override * GIB), int(override * GIB))
        self.limit = limit if self.version else None     # usage is only known from a cgroup

    def sample(self):
        """dict(usage, cache, anon, shmem, dirty, pinned) in bytes; pinned is what reclaim cannot free quickly."""
        st = _read_stat(self.dir / 'memory.stat')
        if self.version == 1:
            g = lambda k: st.get('total_' + k, st.get(k, 0))
            s = dict(usage=_read_int(self.dir / 'memory.usage_in_bytes') or 0, cache=g('cache'), anon=g('rss'),
                     shmem=g('shmem'), dirty=g('dirty') + g('writeback'))
        else:
            s = dict(usage=_read_int(self.dir / 'memory.current') or 0, cache=st.get('file', 0),
                     anon=st.get('anon', 0), shmem=st.get('shmem', 0),
                     dirty=st.get('file_dirty', 0) + st.get('file_writeback', 0))
        s['pinned'] = s['anon'] + s['shmem'] + s['dirty']
        return s

    def pids(self):
        """Every process in the job's cgroup (sub-cgroups included)."""
        pids = set()
        for root, _dirs, files in os.walk(self.dir):
            if 'cgroup.procs' in files:
                try:
                    pids.update(int(x) for x in Path(root, 'cgroup.procs').read_text().split())
                except (OSError, ValueError):
                    pass
        return pids


# ------------------------------------------------------------------ processes
def _proc(pid):
    """dict(ppid, rss, uid, argv) of a live process, or None."""
    try:
        status = Path(f'/proc/{pid}/status').read_text()
        argv = Path(f'/proc/{pid}/cmdline').read_bytes().decode(errors='replace').split('\0')
    except OSError:
        return None
    info = dict(ppid=0, rss=0, uid=-1, argv=[a for a in argv if a])
    for line in status.splitlines():
        if line.startswith('PPid:'):
            info['ppid'] = int(line.split()[1])
        elif line.startswith('VmRSS:'):
            info['rss'] = int(line.split()[1]) * 1024
        elif line.startswith('Uid:'):
            info['uid'] = int(line.split()[1])
    return info


def _chain(pid, table):
    """pid's ancestors (nearest first), following table, then /proc for processes outside it."""
    out, seen = [], {pid}
    info = table.get(pid) or _proc(pid)
    while info and info['ppid'] > 1 and info['ppid'] not in seen:
        p = info['ppid']
        out.append(p)
        seen.add(p)
        info = table.get(p) or _proc(p)
    return out


def _is_harness(info):
    """The agent harness itself (claude, codex, kimi-code, ...), possibly behind an interpreter such as node."""
    for a in info['argv'][:2]:
        base = os.path.basename(a).lower()
        if base.endswith(('.js', '.mjs', '.cjs', '.py')):
            base = base.rsplit('.', 1)[0]
        if base in HARNESS_NAMES or any(f'/{n}/' in a.lower() or f'@{n}' in a.lower() for n in ('claude-code', 'codex')):
            return True
    return False


def _candidates(acct, root_pid):
    """Processes the agent launched, by pid: the runner's descendants, plus processes in the job that were detached
    from it (reparented to init). Excluded: the runner, its ancestors, its direct children (the harness, the
    completion judge), harness binaries, and whatever the job script started beside the runner (a model server)."""
    me = os.getuid()
    table = {}
    for pid in acct.pids():
        info = _proc(pid)
        if info and info['uid'] == me:
            table[pid] = info
    runner_chain = [root_pid] + _chain(root_pid, table)
    job_side = {p for p in runner_chain[1:] if p in table}   # the job script and its shells, not the scheduler
    out = {}
    for pid, info in table.items():
        if pid in runner_chain or info['ppid'] == root_pid or _is_harness(info):
            continue
        chain = _chain(pid, table)
        if root_pid in chain or not (job_side & set(chain)):
            out[pid] = info
    return out


def _notify_process(pid, text):
    """Append text to the process's stderr (its log file, or the pipe its caller reads), like the kernel's 'Killed'."""
    try:
        fd = os.open(f'/proc/{pid}/fd/2', os.O_WRONLY | os.O_APPEND | os.O_NONBLOCK)
    except OSError:
        return
    try:
        mode = os.fstat(fd).st_mode
        if stat.S_ISREG(mode) or stat.S_ISFIFO(mode) or os.isatty(fd):
            os.write(fd, ('\n' + text + '\n').encode())
    except OSError:
        pass
    finally:
        os.close(fd)


def stop_largest(acct, root_pid, message):
    """End the agent-launched process with the most resident memory, with its children. Returns what it ended."""
    cand = _candidates(acct, root_pid)
    if not cand:
        return None
    victim = max(cand, key=lambda p: cand[p]['rss'])
    if cand[victim]['rss'] < MIN_VICTIM:
        return None
    children = {}
    for pid, info in cand.items():
        children.setdefault(info['ppid'], []).append(pid)
    tree, stack = [], [victim]
    while stack:
        p = stack.pop()
        tree.append(p)
        stack.extend(children.get(p, []))
    rss = sum(cand[p]['rss'] for p in tree)
    found = dict(pid=victim, processes=len(tree), rss_gib=_gib(rss), cmd=' '.join(cand[victim]['argv'])[:300])
    _notify_process(victim, message(found))
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for p in reversed(tree):
            try:
                os.kill(p, sig)
            except OSError:
                pass
        if sig == signal.SIGTERM:
            for _ in range(20):                          # up to 10 s to exit cleanly
                if not any(Path(f'/proc/{p}').exists() for p in tree):
                    break
                time.sleep(0.5)
    return found


# ------------------------------------------------------------------ page cache
def _mounts():
    out = []
    try:
        for line in Path('/proc/mounts').read_text().splitlines():
            parts = line.split()
            out.append((parts[1].replace('\\040', ' '), parts[2]))
    except OSError:
        pass
    return sorted(out, key=lambda m: len(m[0]), reverse=True)


def _fs_type(path, mounts):
    p = str(path)
    for mnt, fs in mounts:
        if p == mnt or p.startswith(mnt.rstrip('/') + '/'):
            return fs
    return ''


def _is_ram(path, mounts=None):
    return _fs_type(Path(path).resolve(), mounts or _mounts()) in ('tmpfs', 'ramfs')


def _drop_file(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return False
    try:
        try:
            os.fdatasync(fd)               # dirty pages must reach the disk before they can be dropped
        except OSError:
            pass
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        return True
    except (OSError, AttributeError):
        return False
    finally:
        os.close(fd)


def _cache_roots(workspace):
    home = Path.home()
    roots = [workspace, os.environ.get('PBS_LOCALDIR'), os.environ.get('SOLVER_SCRATCH_DIR'),
             os.environ.get('TMPDIR'), os.environ.get('HF_HOME'), os.environ.get('HF_HUB_CACHE'),
             home / '.cache' / 'huggingface', os.environ.get('SINGULARITY_CACHEDIR'),
             os.environ.get('APPTAINER_CACHEDIR'), home / '.singularity' / 'cache', home / '.apptainer' / 'cache']
    roots += os.environ.get('SOLVER_MEMORY_CACHE_DIRS', '').split(':')
    out, seen = [], set()
    for r in roots:
        if r and Path(r).is_dir():
            rp = Path(r).resolve()
            if rp not in seen:
                seen.add(rp)
                out.append(rp)
    return out


def _open_files(pids):
    """Regular files the job's processes have open or memory-mapped (model weights, files being written)."""
    files = set()
    for pid in pids:
        try:
            for fd in os.listdir(f'/proc/{pid}/fd'):
                try:
                    files.add(os.readlink(f'/proc/{pid}/fd/{fd}'))
                except OSError:
                    pass
        except OSError:
            pass
        try:
            for line in Path(f'/proc/{pid}/maps').read_text().splitlines():
                parts = line.split(None, 5)
                if len(parts) == 6 and parts[5].startswith('/'):
                    files.add(parts[5])
        except OSError:
            pass
    return sorted(f for f in files if f.startswith('/') and not f.startswith(('/proc/', '/sys/', '/dev/')))


def drop_cache(workspace, pids, budget_s=SCAN_BUDGET_S):
    """Release the cached pages of the job's large files. Returns (files advised, GiB of file size covered)."""
    t0 = time.monotonic()
    mounts = _mounts()
    done, n, size = set(), 0, 0

    def advise(path):
        nonlocal n, size
        try:
            st = os.stat(path)
        except OSError:
            return
        if st.st_size < MIN_FILE or not stat.S_ISREG(st.st_mode) or (st.st_dev, st.st_ino) in done:
            return
        done.add((st.st_dev, st.st_ino))
        if _fs_type(path, mounts) not in ('tmpfs', 'ramfs') and _drop_file(path):
            n, size = n + 1, size + st.st_size

    for f in _open_files(pids):
        advise(f)
    roots = _cache_roots(workspace)
    for i, root in enumerate(roots):          # each tree gets a share of the remaining budget
        share = (budget_s - (time.monotonic() - t0)) / (len(roots) - i)
        until = time.monotonic() + max(share, 0)
        for dirpath, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            if time.monotonic() > until:
                break
            for name in files:
                advise(os.path.join(dirpath, name))
    return n, _gib(size)


# ------------------------------------------------------------------ the watchdog
class MemoryWatchdog(threading.Thread):
    def __init__(self, workspace, on_event=None, root_pid=None):
        super().__init__(name='memory-watchdog', daemon=True)
        self.workspace = Path(workspace).resolve()
        self.on_event = on_event
        self.root_pid = root_pid or os.getpid()
        self.acct = Accounting()
        self.drop_at = _env_float('SOLVER_MEMORY_DROP_AT', 0.80)
        self.kill_at = _env_float('SOLVER_MEMORY_KILL_AT', 0.93)
        self.poll = max(1.0, _env_float('SOLVER_MEMORY_POLL_S', 10))
        self._last_drop = 0.0
        self._drop_every = 120.0
        self._last_ram_note = 0.0

    def _event(self, kind, **info):
        print(f'[watchdog] {kind}: ' + ', '.join(f'{k}={v}' for k, v in info.items()), flush=True)
        if self.on_event:
            try:
                self.on_event(kind, info)
            except Exception:
                pass

    def _notice(self, text):
        print(f'[watchdog] notice for the agent: {text}', flush=True)
        with _LOCK:
            _NOTICES.append(text)

    def check(self):
        limit = self.acct.limit
        s = self.acct.sample()
        if s['usage'] < self.drop_at * limit:
            return
        urgent = s['pinned'] >= self.kill_at * limit
        droppable = s['cache'] - s['shmem']
        if droppable > 0.02 * limit and (urgent or time.monotonic() - self._last_drop > self._drop_every):
            self._last_drop = time.monotonic()
            before, t0 = s, time.monotonic()
            files, gib = drop_cache(self.workspace, self.acct.pids(), budget_s=20 if urgent else SCAN_BUDGET_S)
            s = self.acct.sample()
            freed = before['usage'] - s['usage']
            # a pass that frees little is not repeated soon (the cache is elsewhere, or in use)
            self._drop_every = 120.0 if freed > 0.02 * limit else min(self._drop_every * 2, 1800.0)
            self._event('cache_dropped', files=files, file_gib=gib, seconds=round(time.monotonic() - t0, 1),
                        usage_gib_before=_gib(before['usage']),
                        usage_gib_after=_gib(s['usage']), limit_gib=_gib(limit), anon_gib=_gib(s['anon']),
                        ram_files_gib=_gib(s['shmem']), dirty_gib=_gib(s['dirty']))
        if s['shmem'] > 0.25 * limit and time.monotonic() - self._last_ram_note > 3600:
            self._last_ram_note = time.monotonic()
            self._event('ram_files_notice', ram_files_gib=_gib(s['shmem']), limit_gib=_gib(limit))
            self._notice(f"Note from the run's memory watchdog: {_gib(s['shmem'])} GiB of the job's "
                         f"{_gib(limit)} GiB memory limit is taken by files in RAM-backed directories (tmpfs, e.g. "
                         f"/dev/shm or a RAM-backed /tmp); that memory is only freed when the files are deleted. "
                         f"Delete what you no longer need and keep large files on disk.")
        if s['pinned'] < self.kill_at * limit:
            return
        usage = s['pinned']

        def message(found):
            kids = found['processes'] - 1
            return (f"[memory watchdog] This process (PID {found['pid']}, {found['rss_gib']} GiB resident"
                    f"{f', with {kids} child processes' if kids else ''}) was stopped because the job was about to "
                    f"exceed its memory limit ({_gib(usage)} of {_gib(limit)} GiB in use), which would have ended "
                    f"the whole run. Treat this as an out-of-memory failure of this process and adapt it (smaller "
                    f"batches, fewer parallel workers, streaming instead of loading everything).")

        found = stop_largest(self.acct, self.root_pid, message)
        after = self.acct.sample()
        if not found:
            self._event('over_limit_nothing_to_stop', pinned_gib=_gib(usage), limit_gib=_gib(limit),
                        anon_gib=_gib(s['anon']), ram_files_gib=_gib(s['shmem']), dirty_gib=_gib(s['dirty']))
            return
        self._event('process_stopped', pinned_gib_before=_gib(usage), pinned_gib_after=_gib(after['pinned']),
                    limit_gib=_gib(limit), **found)
        self._notice(f"Note from the run's memory watchdog: it stopped one of your processes because the job was "
                     f"about to exceed its memory limit ({_gib(usage)} of {_gib(limit)} GiB in use, which would "
                     f"have ended the whole run): PID {found['pid']} ({found['rss_gib']} GiB resident, "
                     f"{found['processes']} process(es)): {found['cmd'][:200]}. Treat it as an out-of-memory failure "
                     f"of that process and adapt (smaller batches, fewer parallel workers, streaming).")

    def run(self):
        while True:
            try:
                self.check()
            except Exception as e:                     # the watchdog must never take the runner down
                print(f'[watchdog] check failed: {type(e).__name__}: {e}', flush=True)
            time.sleep(self.poll)


def start_memory_watchdog(workspace, on_event=None):
    """Start the watchdog thread. Returns it, or None where it does not apply (see the module docstring)."""
    if os.environ.get('SOLVER_MEMORY_WATCHDOG', '1') == '0' or not sys.platform.startswith('linux'):
        return None
    wd = MemoryWatchdog(workspace, on_event=on_event)
    if not wd.acct.limit:
        print('[watchdog] no job memory limit found; memory watchdog off', flush=True)
        return None
    print(f'[watchdog] memory watchdog on: limit {_gib(wd.acct.limit)} GiB (cgroup v{wd.acct.version}); drops page '
          f'cache from {wd.drop_at:.0%} and stops the largest agent-launched process when unreclaimable memory '
          f'reaches {wd.kill_at:.0%}', flush=True)
    wd.start()
    return wd


def pending_notices() -> str:
    """What the watchdog told the agent since the last prompt, to append to it (empty if nothing)."""
    with _LOCK:
        notes = list(_NOTICES)
        _NOTICES.clear()
    return ''.join('\n\n' + n for n in notes)


def use_disk_tmpdir():
    """Point TMPDIR (and the container tools' temp dirs) at node-local disk if the current TMPDIR is RAM-backed."""
    cur = os.environ.get('TMPDIR', '/tmp')
    scratch = os.environ.get('SOLVER_SCRATCH_DIR') or os.environ.get('PBS_LOCALDIR')
    if (os.environ.get('SOLVER_MEMORY_WATCHDOG', '1') == '0' or not sys.platform.startswith('linux')
            or not scratch or not Path(scratch).is_dir()):
        return None
    mounts = _mounts()
    if not _is_ram(cur, mounts) or _is_ram(scratch, mounts):
        return None
    new = Path(scratch) / 'tmp'
    new.mkdir(parents=True, exist_ok=True)
    for var in ('TMPDIR', 'TMP', 'TEMP', 'SINGULARITY_TMPDIR', 'APPTAINER_TMPDIR'):
        os.environ[var] = str(new)
    print(f'[watchdog] TMPDIR {cur} is RAM-backed; temporary files now go to {new} (node-local disk)', flush=True)
    return new
