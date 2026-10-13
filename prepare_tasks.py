#!/usr/bin/env python3
"""Stage the input materials (code/ and data/) for the Into the Unknown tasks.

For each task P1..P15 this script populates

    tasks/paper<N>/code/   third-party source code handed to the agent
    tasks/paper<N>/data/   datasets handed to the agent

as listed under "Available resources" in tasks/paper<N>/task_spec.md.
paper.md, paper.pdf and images/ are produced separately and are not touched.

Usage
    python prepare_tasks.py                  # all 15 tasks
    python prepare_tasks.py --papers 1 5 9   # a subset (task ids 1..15)
    python prepare_tasks.py --dry-run        # print the plan; no network, no writes
    python prepare_tasks.py --force          # re-fetch items that already exist

Requirements: Python >= 3.8, git on PATH, network access, and about 20 GB of
free disk for all tasks (largest: P1 ~6.3 GB, P11 ~3.6 GB, P3 ~2.9 GB).

Reproducibility
  * Every git repository is pinned to the commit the benchmark agents received
    (a shallow fetch of exactly that commit, checked out on the same branch
    name). If that commit can no longer be fetched, the branch tip is used and
    the item is reported as WARN.
  * Every downloaded file is checked against the size and MD5 of the copy the
    agents received. A mismatch is reported as WARN and the file is kept.
  * Existing items are skipped, so the script can simply be re-run to resume;
    interrupted downloads continue from their ``.part`` file. --force re-fetches.

Author-provided files (P15 only)
  Three DFT reference tarballs and one ACE potential used by P15 were supplied
  directly by the original paper's authors and have no public download URL.
  They ship xz-compressed in this repository's assets/ directory, which is used
  by default; they are decompressed and checked against the agents' MD5.
  --assets-dir DIR (or SOG_ASSETS_DIR) points elsewhere.

Exit status: 0 = everything staged; 1 = at least one item failed;
3 = no failures, but manual steps are still pending.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import lzma
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_ASSETS_DIR = REPO_ROOT / "assets"   # the P15 author-provided files, xz-compressed
DEFAULT_TASKS_DIR = REPO_ROOT / "tasks"
ASSETS_ENV = "SOG_ASSETS_DIR"
USER_AGENT = "Mozilla/5.0 (compatible; into-the-unknown-prepare/1.0)"
HTTP_TIMEOUT = 120          # seconds per blocking socket operation
HTTP_RETRIES = 4
GIT_TIMEOUT = 3600          # seconds per git command (OMatG / QuCF / dakota are large)
PROGRESS_EVERY = 30         # seconds between progress lines for long downloads

OK, SKIP, WARN, MANUAL, FAIL, PLAN = "OK", "SKIP", "WARN", "MANUAL", "FAIL", "PLAN"
_SEVERITY = {SKIP: 0, OK: 0, PLAN: 0, WARN: 1, MANUAL: 2, FAIL: 3}


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #

def log(msg: str = "") -> None:
    print(msg, flush=True)


def human(n: Optional[int]) -> str:
    if n is None:
        return "?"
    if n < 1024:
        return f"{n} B"
    size = n / 1024
    for unit in ("KiB", "MiB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GiB"


def md5_of(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify(path: Path, size: Optional[int], md5: Optional[str]) -> Optional[str]:
    """Return None if `path` matches the expected size/MD5, else a message."""
    actual = path.stat().st_size
    if size is not None and actual != size:
        return f"{path.name}: size {actual} differs from the {size} bytes agents received"
    if md5 is not None:
        got = md5_of(path)
        if got != md5:
            return f"{path.name}: md5 {got} differs from the {md5} agents received"
    return None


def remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def _copy_stream(src, dst, done: int, total: Optional[int], label: str) -> None:
    last = time.monotonic()
    while True:
        chunk = src.read(1 << 20)
        if not chunk:
            break
        dst.write(chunk)
        done += len(chunk)
        now = time.monotonic()
        if now - last >= PROGRESS_EVERY:
            last = now
            log(f"      ... {label}: {human(done)} / {human(total)}")


def http_download(url: str, dest: Path, expected_size: Optional[int] = None) -> None:
    """Stream `url` into `dest` via `dest.part`, resuming a previous partial file.

    Raises RuntimeError when all attempts fail. The final name is always the
    one given by the caller: some hosts (figshare) put the real file name only
    on the redirect response, which urllib does not expose.
    """
    part = dest.with_name(dest.name + ".part")
    dest.parent.mkdir(parents=True, exist_ok=True)
    last_error = "no attempt made"
    for attempt in range(1, HTTP_RETRIES + 1):
        wait = 10 * attempt
        have = part.stat().st_size if part.exists() else 0
        headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
        if have:
            headers["Range"] = f"bytes={have}-"
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as resp:
                if have and getattr(resp, "status", 200) != 206:
                    have = 0  # server ignored the Range header: start over
                length = resp.headers.get("Content-Length")
                total = have + int(length) if length and length.isdigit() else None
                with part.open("ab" if have else "wb") as out:
                    _copy_stream(resp, out, have, total or expected_size, dest.name)
            if total is not None and part.stat().st_size != total:
                raise RuntimeError(f"connection closed early at {part.stat().st_size} of {total} bytes")
            part.replace(dest)
            return
        except urllib.error.HTTPError as exc:
            if exc.code == 416 and have and expected_size == have:
                part.replace(dest)  # the partial file was already complete
                return
            if exc.code == 416:
                part.unlink()
            last_error = f"HTTP {exc.code} from {url}"
            if exc.code in (404, 410):
                break
            # 403/429 are also used for throttling (e.g. OSF), so they are retried.
            retry_after = (exc.headers or {}).get("Retry-After", "")
            if retry_after.isdigit():
                wait = min(int(retry_after), 300)
        except Exception as exc:  # noqa: BLE001 - network errors of all kinds
            last_error = f"{type(exc).__name__}: {exc}"
        if attempt < HTTP_RETRIES:
            log(f"      retry {attempt}/{HTTP_RETRIES - 1} in {wait}s after error: {last_error}")
            time.sleep(wait)
    raise RuntimeError(last_error)


class GitError(RuntimeError):
    pass


def git(args: List[str], cwd: Optional[Path] = None, check: bool = True) -> subprocess.CompletedProcess:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    try:
        result = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True,
                                text=True, timeout=GIT_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise GitError(f"git {' '.join(args[:3])} timed out after {GIT_TIMEOUT}s")
    if check and result.returncode != 0:
        lines = (result.stderr or result.stdout or "").strip().splitlines()
        raise GitError(f"git {' '.join(args[:3])} failed: {lines[-1] if lines else '(no output)'}")
    return result


def git_head(repo: Path) -> Optional[str]:
    if not (repo / ".git").exists():
        return None
    result = git(["-C", str(repo), "rev-parse", "HEAD"], check=False)
    return result.stdout.strip() if result.returncode == 0 else None


# --------------------------------------------------------------------------- #
# Item types                                                                   #
# --------------------------------------------------------------------------- #

@dataclass
class Result:
    status: str
    message: str
    instructions: str = ""


@dataclass
class Ctx:
    force: bool
    dry_run: bool
    assets_dir: Optional[Path]
    task_id: int = 0


@dataclass
class Remote:
    """One downloadable file, with the size/MD5 of the copy agents received."""
    url: str
    name: str
    size: Optional[int] = None
    md5: Optional[str] = None


@dataclass
class GitRepo:
    """A git repository pinned to the exact commit the agents received."""
    url: str
    commit: str
    branch: str                  # branch the agents' checkout was on; also the fallback
    dest: str                    # relative to the task directory
    recursive: bool = False      # initialise submodules

    def label(self) -> str:
        return f"git {self.url} @ {self.commit[:12]} -> {self.dest}/"

    def run(self, task_dir: Path, ctx: Ctx) -> Result:
        target = task_dir / self.dest
        if target.exists() and not ctx.force:
            head = git_head(target) if not ctx.dry_run else None
            if head and head != self.commit:
                return Result(WARN, f"already present but at {head[:12]}, not the pinned "
                                    f"{self.commit[:12]} (re-run with --force to replace)")
            return Result(SKIP, "already present")
        extra = ", with submodules" if self.recursive else ""
        if ctx.dry_run:
            verb = "would delete and re-fetch" if target.exists() else "would fetch"
            return Result(PLAN, f"{verb} commit {self.commit[:12]} (branch {self.branch}{extra})")
        if target.exists():
            remove(target)
        tmp = target.with_name(target.name + ".partial")
        if tmp.exists():
            remove(tmp)
        tmp.parent.mkdir(parents=True, exist_ok=True)
        status, note = OK, f"checked out {self.commit[:12]} on branch {self.branch}{extra}"
        try:
            git(["-c", f"init.defaultBranch={self.branch}", "init", "-q", str(tmp)])
            git(["-C", str(tmp), "remote", "add", "origin", self.url])
            fetched = git(["-C", str(tmp), "fetch", "-q", "--depth", "1", "origin", self.commit],
                          check=False).returncode == 0
            if fetched:
                git(["-C", str(tmp), "checkout", "-q", "-B", self.branch, "FETCH_HEAD"])
                git(["-C", str(tmp), "update-ref", f"refs/remotes/origin/{self.branch}", self.commit])
            else:
                remove(tmp)
                git(["clone", "-q", "--depth", "1", "--branch", self.branch, self.url, str(tmp)])
                head = git_head(tmp) or "?"
                if head != self.commit:
                    status = WARN
                    note = (f"pinned commit {self.commit[:12]} is not fetchable; using the "
                            f"current {self.branch} tip {head[:12]} instead")
            if self.recursive:
                git(["-C", str(tmp), "submodule", "--quiet", "update", "--init", "--recursive"])
            tmp.rename(target)
        except GitError as exc:
            if tmp.exists():
                remove(tmp)
            return Result(FAIL, str(exc))
        return Result(status, note)


@dataclass
class Download:
    """Plain HTTP(S) files saved under `dest` with explicit names."""
    dest: str
    files: List[Remote]
    extract: Dict[str, str] = field(default_factory=dict)  # zip name -> top-level entry it creates

    def label(self) -> str:
        return f"download {len(self.files)} file(s) -> {self.dest}/"

    def run(self, task_dir: Path, ctx: Ctx) -> Result:
        root = task_dir / self.dest
        # A file only appears under its final name once complete (see http_download).
        todo = [f for f in self.files if ctx.force or not (root / f.name).exists()]
        unpack = [z for z, out in self.extract.items() if ctx.force or not (root / out).exists()]
        if not todo and not unpack:
            return Result(SKIP, "already present")
        if ctx.dry_run:
            parts = [f"{f.name} ({human(f.size)})" for f in todo]
            parts += [f"unzip {z} -> {self.extract[z]}/" for z in unpack]
            return Result(PLAN, "would get " + ", ".join(parts))
        problems: List[str] = []
        for f in todo:
            path = root / f.name
            if ctx.force:
                for stale in (path, path.with_name(path.name + ".part")):
                    if stale.exists():
                        stale.unlink()
            log(f"      GET {f.url}")
            try:
                http_download(f.url, path, f.size)
            except RuntimeError as exc:
                return Result(FAIL, f"{f.name}: {exc}")
            mismatch = verify(path, f.size, f.md5)
            if mismatch:
                problems.append(mismatch)
        for zname in unpack:
            out = root / self.extract[zname]
            if out.exists():
                remove(out)
            try:
                with zipfile.ZipFile(root / zname) as zf:
                    zf.extractall(root)
            except (zipfile.BadZipFile, OSError) as exc:
                return Result(FAIL, f"could not unzip {zname}: {exc}")
        done = []
        if todo:
            done.append("downloaded " + ", ".join(f.name for f in todo))
        if unpack:
            done.append("unpacked " + ", ".join(unpack))
        if problems:
            return Result(WARN, "; ".join(done + problems))
        message = "; ".join(done)
        if todo:
            message += " (checksums verified)"
        return Result(OK, message)


@dataclass
class CopyFromRepo:
    """Copy files/directories out of an already-fetched repository into `dest`."""
    dest: str
    repo_dest: str               # e.g. "code/FitSNAP" (a GitRepo of the same task)
    paths: List[str]             # relative to the repository root

    def label(self) -> str:
        return f"copy {len(self.paths)} path(s) from {self.repo_dest}/ -> {self.dest}/"

    def run(self, task_dir: Path, ctx: Ctx) -> Result:
        root = task_dir / self.dest
        pairs = [(task_dir / self.repo_dest / rel, root / Path(rel).name) for rel in self.paths]
        todo = [(src, out) for src, out in pairs if ctx.force or not out.exists()]
        if not todo:
            return Result(SKIP, "already present")
        if ctx.dry_run:
            return Result(PLAN, "would copy " + ", ".join(src.name for src, _ in todo))
        missing = [str(src.relative_to(task_dir)) for src, _ in todo if not src.exists()]
        if missing:
            return Result(FAIL, "not found (was the repository fetched?): " + ", ".join(missing))
        root.mkdir(parents=True, exist_ok=True)
        for src, out in todo:
            if out.exists():
                remove(out)
            if src.is_dir():
                shutil.copytree(src, out)
            else:
                shutil.copy2(src, out)
        return Result(OK, "copied " + ", ".join(src.name for src, _ in todo))


@dataclass
class OsfProject:
    """All files of a public OSF project storage, mirrored under `dest`."""
    dest: str
    node: str
    n_files: int                 # what the agents received
    n_bytes: int

    def label(self) -> str:
        return f"OSF project {self.node} ({self.n_files} files) -> {self.dest}/"

    def _listing(self) -> List[Tuple[str, int, Optional[str], str]]:
        entries: List[Tuple[str, int, Optional[str], str]] = []
        pending = [(f"https://api.osf.io/v2/nodes/{self.node}/files/osfstorage/", "")]
        while pending:
            url, prefix = pending.pop()
            while url:
                request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as resp:
                    page = json.load(resp)
                for entry in page["data"]:
                    attrs = entry["attributes"]
                    name = attrs["name"]
                    if name in ("", ".", "..") or "/" in name or "\\" in name:
                        raise RuntimeError(f"refusing unsafe OSF file name {name!r}")
                    if attrs["kind"] == "folder":
                        href = entry["relationships"]["files"]["links"]["related"]["href"]
                        pending.append((href, f"{prefix}{name}/"))
                    else:
                        md5 = ((attrs.get("extra") or {}).get("hashes") or {}).get("md5")
                        entries.append((prefix + name, int(attrs["size"]), md5,
                                        entry["links"]["download"]))
                url = page["links"].get("next")
        return entries

    def run(self, task_dir: Path, ctx: Ctx) -> Result:
        root = task_dir / self.dest
        if root.exists() and any(root.iterdir()) and not ctx.force:
            return Result(SKIP, "already present")
        if ctx.dry_run:
            return Result(PLAN, f"would mirror https://osf.io/{self.node}/ "
                                f"({self.n_files} files, {human(self.n_bytes)})")
        tmp = root.with_name(root.name + ".partial")
        try:
            listing = self._listing()
        except Exception as exc:  # noqa: BLE001
            return Result(FAIL, f"could not list OSF project {self.node}: {exc}")
        problems: List[str] = []
        for rel, size, md5, url in listing:
            path = tmp / rel
            if path.exists() and path.stat().st_size == size:
                continue  # resuming an interrupted mirror
            try:
                http_download(url, path, size)
            except RuntimeError as exc:
                return Result(FAIL, f"{rel}: {exc}")
            mismatch = verify(path, size, md5)
            if mismatch:
                problems.append(mismatch)
        if root.exists():
            remove(root)
        tmp.rename(root)
        total = sum(size for _, size, _, _ in listing)
        if (len(listing), total) != (self.n_files, self.n_bytes):
            problems.append(f"got {len(listing)} files / {total} bytes, agents received "
                            f"{self.n_files} files / {self.n_bytes} bytes")
        if problems:
            return Result(WARN, "; ".join(problems))
        return Result(OK, f"{len(listing)} files, {human(total)} (verified)")


@dataclass
class Embedded:
    """Small curated files shipped inside this script (see the end of the file)."""
    dest: str
    group: str
    files: Dict[str, str]        # file name -> expected MD5

    def label(self) -> str:
        return f"write {len(self.files)} bundled file(s) -> {self.dest}/"

    def run(self, task_dir: Path, ctx: Ctx) -> Result:
        root = task_dir / self.dest
        todo = [n for n in self.files if ctx.force or not (root / n).exists()]
        if not todo:
            return Result(SKIP, "already present")
        if ctx.dry_run:
            return Result(PLAN, "would write " + ", ".join(todo))
        root.mkdir(parents=True, exist_ok=True)
        for name in todo:
            key = f"{self.group}/{name}"
            text = _EMBEDDED[key]
            if key in _EMBEDDED_CRLF:
                text = text.replace("\n", "\r\n")
            data = text.encode("utf-8")
            if hashlib.md5(data).hexdigest() != self.files[name]:
                return Result(FAIL, f"internal error: bundled {key} does not match its checksum")
            (root / name).write_bytes(data)
        return Result(OK, "wrote " + ", ".join(todo))


@dataclass
class Manual:
    """Files with no public download URL; copied (or xz-decompressed) from the assets directory."""
    dest: str
    files: List[Remote]          # `url` is unused (kept empty)
    instructions: str

    def label(self) -> str:
        return f"manual: {len(self.files)} author-provided file(s) -> {self.dest}/"

    def _candidates(self, ctx: Ctx, name: str) -> List[Path]:
        if ctx.assets_dir is None:
            return []
        base = ctx.assets_dir
        paths = [base / f"paper{ctx.task_id}" / self.dest / name,
                 base / self.dest / name,
                 base / Path(self.dest).name / name,
                 base / name]
        return [q for p in paths for q in (p, p.with_name(p.name + ".xz"))]

    def run(self, task_dir: Path, ctx: Ctx) -> Result:
        root = task_dir / self.dest
        copies: List[Tuple[Remote, Path]] = []
        missing: List[Remote] = []
        for f in self.files:
            present = (root / f.name).exists()
            if present and not ctx.force:
                continue
            source = next((p for p in self._candidates(ctx, f.name) if p.is_file()), None)
            if source is not None:
                copies.append((f, source))
            elif not present:
                missing.append(f)  # with --force an existing copy is kept if no source exists
        if not copies and not missing:
            return Result(SKIP, "already present")
        if ctx.dry_run and not missing:
            return Result(PLAN, "would copy " + ", ".join(f.name for f, _ in copies)
                          + f" from {ctx.assets_dir}")
        problems: List[str] = []
        if not ctx.dry_run:
            root.mkdir(parents=True, exist_ok=True)
            for f, source in copies:
                if source.name == f.name + ".xz":
                    with lzma.open(source) as fi, (root / f.name).open("wb") as fo:
                        shutil.copyfileobj(fi, fo, 4 * 2**20)
                else:
                    shutil.copy2(source, root / f.name)
                mismatch = verify(root / f.name, f.size, f.md5)
                if mismatch:
                    problems.append(mismatch)
        if missing:
            where = (f"not found under --assets-dir {ctx.assets_dir}" if ctx.assets_dir
                     else f"no --assets-dir given (or ${ASSETS_ENV} unset)")
            listing = "\n".join(f"      {self.dest}/{f.name}  ({f.size} bytes, md5 {f.md5})"
                                for f in missing)
            text = (f"P{ctx.task_id} {self.dest}/: {len(missing)} file(s) {where}:\n{listing}\n"
                    f"    {self.instructions}")
            return Result(MANUAL, f"{len(missing)} file(s) pending: "
                                  + ", ".join(f.name for f in missing), text)
        if problems:
            return Result(WARN, "; ".join(problems))
        return Result(OK, "copied " + ", ".join(f.name for f, _ in copies))


@dataclass
class Task:
    id: int
    short: str
    title: str
    items: list


# --------------------------------------------------------------------------- #
# Task registry (P1..P15)                                                      #
# --------------------------------------------------------------------------- #

FIGSHARE = "https://ndownloader.figshare.com/files/"
SAIDI = ("https://raw.githubusercontent.com/saidigroup/23-Single-Element-DNPs/"
         "8b3ba91386c3d607901769b05b8f01d8c3202757/Training_Data/")
FITSNAP = GitRepo("https://github.com/FitSNAP/FitSNAP",
                  "1a9b0507fa2ba292a6497ec363707944c1cb89cd", "master", "code/FitSNAP")
DAKOTA = GitRepo("https://github.com/snl-dakota/dakota",
                 "d24ec38f6342790967acc65dfc2b02ebf59155ba", "devel", "code/dakota")
GPCAM = GitRepo("https://github.com/lbl-camera/gpCAM",
                "4acdc3942b98cd285d8e4e845ccafc202a99cb0e", "master", "code/gpCAM")
LANL_COLOR_GRAY = ["Gray_Experiment/data/gray_complete_data_release.csv",
                   "Gray_Experiment/data/data_abstract.pdf",
                   "In-Person_Replication/crowdsourced_clean_data.csv"]
P15_AUTHOR_NOTE = (
    "These files were supplied directly by the authors of the original paper "
    "(Wood, Koknat & Thompson) and have no public download URL. They ship in this "
    "repository's assets/paper15/ (xz-compressed); if that directory is missing, put the "
    "files in a directory (layout DIR/paper15/<dest>/<file>[.xz] or flat DIR/<file>) and "
    f"re-run with --assets-dir DIR (or {ASSETS_ENV}=DIR).")


TASKS: List[Task] = [
    Task(1, "HIP-NN", "Hierarchical modeling of molecular energies using a deep neural network", [
        GitRepo("https://github.com/lanl/hippynn",
                "7166f9106a14a5b5dbbd136e4a158a816bcb3675", "development", "code/hippynn"),
        # QM9 (Ramakrishnan et al. 2014, figshare collection 978904).
        Download("data/qm9", [
            Remote(FIGSHARE + "3195389", "dsgdb9nsd.xyz.tar.bz2", 86144227, "ad1ebd51ee7f5b3a6e32e974e5d54012"),
            Remote(FIGSHARE + "3195392", "readme.txt", 5198, "c6581a03f673746528c57acfc6b79679"),
            Remote(FIGSHARE + "3195395", "atomref.txt", 964, "2d30b2df8329d8fd805c0a4d158a0a0f"),
            Remote(FIGSHARE + "3195398", "dsC7O2H10nsd.xyz.tar.bz2", 4149920, "c750c9f3832e6f63ad64bfb3bd12a045"),
            Remote(FIGSHARE + "3195401", "validation.txt", 10863, "7f356bccd797af35e4e54b783ac7d79d"),
            Remote(FIGSHARE + "3195404", "uncharacterized.txt", 486752, "a361887bacb427b8a0ce7903d92a53b4"),
        ]),
        # Revised MD17 (Christensen & von Lilienfeld 2020), figshare article 12672038 version 1.
        Download("data/rmd17", [
            Remote(FIGSHARE + "23950364", "rmd17.tar.bz2", 1063047052, "b6bd1c0afbd6912e899d95e8f52999bf"),
        ]),
        # ANI-1x (Smith et al. 2020), figshare article 10047041 version 1.
        Download("data/ani1x", [
            Remote(FIGSHARE + "18112775", "ani1x-release.h5", 5590846027, "98090dd6679106da861f52bed825ffb7"),
        ]),
    ]),
    Task(2, "QuadraticSNAP", "Extending the accuracy of the SNAP interatomic potential form", [
        FITSNAP,
        DAKOTA,
        GitRepo("https://github.com/libAtoms/QUIP",
                "056de31d64b60487e05823d7797f80023ea1dbe5", "public", "code/QUIP", recursive=True),
        GitRepo("https://github.com/FERMat-ML/OMatG",
                "fcb9ba2c2cfd70505b0f142a5b3c44944d78e7f0", "main", "code/OMatG"),
        # Tantalum training set and trained quadratic SNAP, both shipped as FitSNAP examples.
        CopyFromRepo("data/ta_dft_training_jsons", "code/FitSNAP", [
            "examples/Ta_Linear_JCP2014/JSON",
            "examples/Ta_Linear_JCP2014/Ta-example.in",
            "examples/Ta_Linear_JCP2014/README.md",
        ]),
        CopyFromRepo("data/ta_quadratic_trained", "code/FitSNAP", ["examples/Ta_Quadratic_JCP2018"]),
        # Transfer materials (Andolina & Saidi 2023, GPL-3.0), DeePMD format.
        Download("data/mo_dft_training", [
            Remote(SAIDI + "Mo/iter_all_Mo.tar.xz", "iter_all_Mo.tar.xz", 12209860, "212a0d251f21145d9ee8d7905872a3ce"),
            Remote(SAIDI + "Mo/input.json", "input.json", 14767, "d54f3e3730ec0247ca0b0d08505ad705"),
            Remote(SAIDI + "Mo/lcurve.out", "lcurve.out", 112650, "1599157779996d0f3cfb7d58fd2fd55c"),
            Remote(SAIDI + "Mo/out.json", "out.json", 19153, "799a3830f1bae64ee7094a80564b404b"),
        ]),
        Download("data/nb_dft_training", [
            Remote(SAIDI + "Nb/iter_al_Nb.tar.xz", "iter_al_Nb.tar.xz", 16186104, "9d99ac5b2a6ba529b97cea50d62ab142"),
            Remote(SAIDI + "Nb/input.json", "input.json", 17942, "221ef87430e59354e4e39d375986ebaf"),
            Remote(SAIDI + "Nb/lcurve.out", "lcurve.out", 112650, "a6399525e85b6fabd4f5a765cc780ce4"),
            Remote(SAIDI + "Nb/out.json", "out.json", 23096, "7bdef3527eee41594248c69af41f886e"),
        ]),
        Download("data/re_dft_training", [
            Remote(SAIDI + "Re/iter0_Re.tar.xz", "iter0_Re.tar.xz", 12346748, "c3c0e91b07d6dfd7e88899d74058d569"),
            Remote(SAIDI + "Re/iter1_2_Re.tar.xz", "iter1_2_Re.tar.xz", 18292408, "17ed62ae210ecb79247a940e68d469ce"),
            Remote(SAIDI + "Re/input.json", "input.json", 18585, "03027961b4faec4508d64314c790932b"),
            Remote(SAIDI + "Re/lcurve.out", "lcurve.out", 112650, "7d4499d1110afbfda651de585f50ef2c"),
            Remote(SAIDI + "Re/out.json", "out.json", 24155, "940a7d58043ef1c827cf8786c0bbd465"),
        ]),
    ]),
    Task(3, "CDL-SPORCO", "Convolutional Dictionary Learning: A Comparative Review and New Algorithms", [
        GitRepo("https://github.com/bwohlberg/sporco",
                "52c20b554c01844023a651fd7c140d931cee80fc", "master", "code/sporco"),
        # MIRFLICKR-25K (Huiskes & Lew 2008); kept zipped, as the task spec describes.
        Download("data/mirflickr25k", [
            Remote("https://press.liacs.nl/mirflickr/mirflickr25k.v3b/mirflickr25k.zip",
                   "mirflickr25k.zip", 3069184257, "a23d0a8564ee84cda5622a6c2f947785"),
            Remote("https://press.liacs.nl/mirflickr/mirflickr25k.v3b/mirflickr25k_annotations_v080.zip",
                   "mirflickr25k_annotations_v080.zip", 207220, "aa0138c79892b1b670184d142380948f"),
        ]),
    ]),
    Task(4, "Kriging-SMART", "A Kriging-based approach to autonomous experimentation with "
                             "applications to X-ray scattering", [
        GPCAM,
        GitRepo("https://github.com/bluesky/bluesky",
                "5e1423146939ced25413caea4058369bb2537390", "main", "code/bluesky"),
        GitRepo("https://github.com/CFN-softbio/SciAnalysis",
                "225df368e0e0def8ebfa5ae8c7649e05e0d64250", "master", "code/SciAnalysis"),
        # data/ stays empty (synthetic test functions only).
    ]),
    Task(5, "AVQDS", "Adaptive variational quantum dynamics simulations", [
        # Authors' figshare deposit (article 14920074, GPL-3.0+): avqds.zip, unpacked in place.
        Download("code/AVQDS", [
            Remote(FIGSHARE + "28728894", "avqds.zip", 332919904, "bd0b84a2f31806ad82c7deac4d10be4e"),
        ], extract={"avqds.zip": "avdynamics-avqds"}),
        # data/ stays empty (Hamiltonians are specified in the task).
    ]),
    Task(6, "LatticeQED", "Simulations of relativistic-quantum plasmas using real-time lattice "
                          "scalar QED", []),  # no public code or data: code/ and data/ stay empty
    Task(7, "ColorPNAS", "The non-Riemannian nature of perceptual color space", [
        GitRepo("https://github.com/lanl/color",
                "099491a0737248fffed75524cf01c66289bf9e3d", "main", "code/color"),
        # Gray-axis 2AFC data (BSD-3) from the authors' repository.
        CopyFromRepo("data/gray", "code/color", LANL_COLOR_GRAY),
        # Wiebel, Aguilar & Maertens (2017) MLDS lightness data, OSF project 58jz3 (CC BY 4.0).
        OsfProject("data/gray/wiebel_mlds", "58jz3", n_files=113, n_bytes=3343365),
        # CIE 2006 2-deg LMS cone fundamentals and CIE 1931 2-deg CMFs (cvrl.org terms apply).
        Download("data/cone_fundamentals", [
            Remote("http://www.cvrl.org/database/data/cones/linss2_10e_1.csv",
                   "linss2_10e_1.csv", 17930, "23fdce9023f311990e9a77b04a504dfb"),
            Remote("http://www.cvrl.org/database/data/cmfs/ciexyz31_1.csv",
                   "ciexyz31_1.csv", 23548, "6dfc8143bff1e445b2555a6a7df2df22"),
        ]),
        # Helm (1964) / Ekman (1954) dissimilarities (extracted from R package smacof, GPL-3)
        # and curated stimulus colorimetry with provenance notes; bundled below.
        Embedded("data/chromatic", "chromatic", {
            "SOURCE_ekman.md": "4d1e3ef5a96d33c5c1221ae24b8cd8c7",
            "SOURCE_helm.md": "7636ea1ed6525f5ea9ace161e074289d",
            "ekman_dissimilarities.csv": "045845283cc7d8a1f01b750d8d9e1ef4",
            "ekman_stimuli_cie.csv": "9342d879e72a9d3bdc4998607c027102",
            "helm_dissimilarities_long.csv": "134f80e7e32537257920fd3c11cb11d6",
            "helm_dissimilarities_normals_avg.csv": "72d551e85c2b53e256fbbb576944d131",
            "helm_stimuli_cie.csv": "a1eef85643a7314588dcf4c8a930078c",
        }),
        # Legacy duplicate of the gray-axis files; present in the workspace the agents received.
        CopyFromRepo("data/observer_responses", "code/color", LANL_COLOR_GRAY),
    ]),
    Task(8, "P2INN", "Parameterized Physics-informed Neural Networks for Parameterized PDEs", [
        GitRepo("https://github.com/WooJin-Cho/Parameterized-Physics-informed-Neural-Networks",
                "c142193e0372fce1eeb98358158c4bd696464d6a", "main", "code/P2INN"),
        # data/ stays empty (PDE data are generated on the fly).
    ]),
    Task(9, "LCHS", "An efficient explicit implementation of a near-optimal quantum algorithm "
                    "for simulating linear dissipative differential equations", [
        GitRepo("https://github.com/QuCF/QuCF",
                "6c6f957beb56b6753bb6a5b3e733e5a883ac7bfd", "main", "code/QuCF", recursive=True),
        GitRepo("https://github.com/QuCF/QuCF.git",
                "0ab44f442f83e9183f34c6707f07d53a56e839f3", "OPT-LCHS", "code/QuCF-OPT-LCHS",
                recursive=True),
        GitRepo("https://github.com/QuCF/QuCF.wiki.git",
                "0e25c609f0afcf6bb4673ea03434188ce6b90d90", "master", "code/QuCF.wiki"),
        CopyFromRepo("data/ade_opt_simulations", "code/QuCF-OPT-LCHS", ["simulations/LCHS/ADE-OPT"]),
    ]),
    Task(10, "BerryAVQC", "Efficient Berry Phase Calculation via Adaptive Variational Quantum "
                          "Computing Approach", [
        # code/ stays empty. Authors' data deposit (figshare article 29410139), kept zipped.
        Download("data/figshare_data", [
            Remote(FIGSHARE + "55656839", "AVQS-Berry.zip", 354988550, "d2fb80526a7c15ef975b67fa9c1ed7a0"),
        ]),
    ]),
    Task(11, "PHL", "Projected Hessian Learning: Fast Curvature Supervision for Accurate "
                    "Machine-Learning Interatomic Potentials", [
        GitRepo("https://github.com/Austinrg14/PHL",
                "13721f446646295823f5b6837af2d3e05442dc21", "main", "code/PHL"),
        # OpenREACT-CHON-EFH, figshare article 29189858 version 1 (CC BY 4.0). Version 1 holds
        # two copies of each file under the same name, so each is prefixed with its file id.
        Download("data/openreact_chon_efh", [
            Remote(FIGSHARE + "54948347", "54948347_molecules-IRC.h5", 454849726, "46a23e9ee940a6ec64569d66f5d4d977"),
            Remote(FIGSHARE + "54948350", "54948350_molecules-RTP.h5", 638951022, "4d34660019b4236947c96c5d649af517"),
            Remote(FIGSHARE + "54948353", "54948353_molecules-RTP.h5", 638951022, "4d34660019b4236947c96c5d649af517"),
            Remote(FIGSHARE + "54948356", "54948356_molecules-NMS.h5", 811695384, "1af1f28889df510bb0adf0bebdd71bc1"),
            Remote(FIGSHARE + "54948359", "54948359_molecules-IRC.h5", 454849726, "46a23e9ee940a6ec64569d66f5d4d977"),
            Remote(FIGSHARE + "54948362", "54948362_molecules-NMS.h5", 811695384, "1af1f28889df510bb0adf0bebdd71bc1"),
        ]),
    ]),
    Task(12, "gp2Scale", "gp2Scale: Compactly-Supported Non-Stationary Kernels and Distributed "
                         "Computing for Exact Gaussian Processes on 10 Million Data Points", [
        GPCAM,
        GitRepo("https://github.com/lbl-camera/fvGP",
                "74e524f4f1b58799ab36d0b2d7e5d462dd8ba964", "master", "code/fvGP"),
        # California Housing (Pace & Barry 1997): the canonical cal_housing.tgz, figshare mirror.
        Download("data/california_housing", [
            Remote(FIGSHARE + "5976036", "cal_housing.tgz", 441963, "130d0eececf165046ec4dc621d121d80"),
        ]),
        # MNIST, CVDF mirror (byte-identical to the original distribution).
        Download("data/mnist", [
            Remote("https://storage.googleapis.com/cvdf-datasets/mnist/train-images-idx3-ubyte.gz",
                   "train-images-idx3-ubyte.gz", 9912422, "f68b3c2dcbeaaa9fbdd348bbdeb94873"),
            Remote("https://storage.googleapis.com/cvdf-datasets/mnist/train-labels-idx1-ubyte.gz",
                   "train-labels-idx1-ubyte.gz", 28881, "d53e105ee54ea40749a09fcbcd1e9432"),
            Remote("https://storage.googleapis.com/cvdf-datasets/mnist/t10k-images-idx3-ubyte.gz",
                   "t10k-images-idx3-ubyte.gz", 1648877, "9fb629c4189551a2d022fa330f9573f3"),
            Remote("https://storage.googleapis.com/cvdf-datasets/mnist/t10k-labels-idx1-ubyte.gz",
                   "t10k-labels-idx1-ubyte.gz", 4542, "ec29112dd5afa0611ce80d1b7f02629c"),
        ]),
    ]),
    Task(13, "Magnetized-CBET", "Particle-in-cell simulations of laser crossbeam energy transfer "
                                "via magnetized ion-acoustic wave", [
        # EPOCH needs its SDF submodule to build, hence recursive=True.
        GitRepo("https://github.com/Warwick-Plasma/epoch",
                "f294c484f76dff0777d5cc0d50b38506a2b049ff", "main", "code/epoch", recursive=True),
        GitRepo("https://gitlab.com/seanYuanSHI/magnetized-cross-beam-energy-transfer",
                "60a3812d1a1e8e2569a2df934d55d20f51145537", "main", "code/magnetized_cbet_matlab"),
        # Authors' processed PIC outputs, Zenodo record 16498564 (CC BY 4.0), kept zipped.
        Download("data/pic_simulations", [
            Remote("https://zenodo.org/records/16498564/files/PIC_processed_data.zip?download=1",
                   "PIC_processed_data.zip", 88634, "95eed8e92609d84240c6dcf18cc6eaa4"),
        ]),
    ]),
    Task(14, "QuantumPlasmaWave", "Simulating plasma wave propagation on a superconducting "
                                  "quantum chip", []),  # code/ and data/ stay empty
    Task(15, "LayeredIPs", "Beyond Classical Molecular Dynamics with Layered Interatomic Potentials", [
        FITSNAP,
        GitRepo("https://github.com/ICAMS/lammps-user-pace",
                "99aa6e685cce24c24f81ce35b241d1b480d1ad05", "main", "code/lammps-user-pace"),
        DAKOTA,
        # Marinica et al. 2013 EAM tungsten potential (NIST Interatomic Potentials Repository).
        Download("data/marinica_eam_w", [
            Remote("https://www.ctcms.nist.gov/potentials/Download/"
                   "2013--Marinica-M-C-Ventelon-L-Gilbert-M-R-et-al--W-4/1/w_eam4.fs",
                   "w_eam4.fs", 9300293, "57aa1517b76e48f1a30eeb25dacf2f0a"),
        ]),
        # Trained ACE potentials. The ground-state potential and the example LAMMPS input are
        # public supplementary files of the preprint (Research Square rs-9077430 v1).
        Download("data/ace_potentials", [
            Remote("https://assets-eu.researchsquare.com/files/rs-9077430/v1/4ebe134f6e6882c11936afaf.txt",
                   "in.ACE_Sommerfeld_PKA", 4869, "16dbc36c01b150faf8769e3c2b0baf29"),
            Remote("https://assets-eu.researchsquare.com/files/rs-9077430/v1/de3876a6961269032c8b46b1.txt",
                   "W_pot_T0.0.yace", 108254, "9728a96bc6c36862db34c23fa77a5f0f"),
        ]),
        Embedded("data/ace_potentials", "ace_potentials", {
            "W_pot_T0.0.mod": "94f123c19b2099c05bb993dd0804081d",
            "W_pot_T0.5.mod": "6d67946b10789f20aac8f3b18b00d9c7",
        }),
        Manual("data/ace_potentials", [
            Remote("", "W_pot_T0.5.yace", 109001, "aed7b132bcd7384ce69c0194ac901d20"),
        ], P15_AUTHOR_NOTE + " (The preprint's public attachment WpotT0.5.txt holds the "
                             "excited-state model as FitSNAP ACE coefficients, not this .yace file.)"),
        # Author-provided VASP DFT reference sets at three electronic temperatures.
        Embedded("data/dft_reference", "dft_reference", {
            "README.md": "a71c89639ffd2999223209d6e05b294e",
        }),
        Manual("data/dft_reference", [
            Remote("", "W-VASP_T0.001eV_JSON.tar", 53780480, "ce68b6c48fe82cbc90725d1e2b79ecf0"),
            Remote("", "W-VASP_T0.1eV_JSON.tar", 11714560, "dbdd121e1a0d03bc5f2b92985415e859"),
            Remote("", "W-VASP_T0.5eV_JSON.tar", 55388160, "1e8c7b53ca67e3882cca9015b3d10ecc"),
        ], P15_AUTHOR_NOTE),
    ]),
]


# --------------------------------------------------------------------------- #
# Driver                                                                       #
# --------------------------------------------------------------------------- #

def prepare_task(task: Task, tasks_dir: Path, ctx: Ctx) -> List[Result]:
    task_dir = tasks_dir / f"paper{task.id}"
    ctx.task_id = task.id
    log(f"\n[P{task.id}] {task.short} - {task.title}")
    if not ctx.dry_run:
        (task_dir / "code").mkdir(parents=True, exist_ok=True)
        (task_dir / "data").mkdir(parents=True, exist_ok=True)
    if not task.items:
        log("  nothing to fetch (code/ and data/ are intentionally empty)")
    results: List[Result] = []
    for item in task.items:
        log(f"  {item.label()}")
        try:
            result = item.run(task_dir, ctx)
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # noqa: BLE001 - report and continue with the next item
            result = Result(FAIL, f"unexpected {type(exc).__name__}: {exc}")
        log(f"    {result.status}: {result.message}")
        results.append(result)
    return results


def overall(results: List[Result]) -> str:
    if not results:
        return OK
    worst = max(results, key=lambda r: _SEVERITY[r.status]).status
    if worst in (SKIP, OK, PLAN):
        return PLAN if any(r.status == PLAN for r in results) else OK
    return worst


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Download the code/ and data/ inputs of the Into the Unknown tasks.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Reproducibility" + __doc__.split("Reproducibility", 1)[1],
    )
    parser.add_argument("--papers", type=int, nargs="+", metavar="N",
                        help="task ids to prepare (1..15; default: all)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print what would be done; no network access, nothing written")
    parser.add_argument("--force", action="store_true",
                        help="re-fetch items that already exist (author-provided files are "
                             "only replaced when a new copy is available)")
    parser.add_argument("--assets-dir", type=Path, default=None,
                        help=f"directory holding the author-provided files "
                             f"(default: ${ASSETS_ENV}, else the repository's assets/)")
    parser.add_argument("--tasks-dir", type=Path, default=DEFAULT_TASKS_DIR,
                        help="where the tasks/paper<N>/ directories live (default: ./tasks)")
    args = parser.parse_args()

    known = {t.id for t in TASKS}
    wanted = sorted(set(args.papers)) if args.papers else sorted(known)
    unknown = [n for n in wanted if n not in known]
    if unknown:
        parser.error(f"unknown task id(s) {unknown}; valid ids are 1..{max(known)}")
    selected = [t for t in TASKS if t.id in wanted]

    assets = args.assets_dir or (Path(os.environ[ASSETS_ENV]) if os.environ.get(ASSETS_ENV) else None)
    if assets is None and DEFAULT_ASSETS_DIR.is_dir():
        assets = DEFAULT_ASSETS_DIR
    if assets is not None:
        assets = assets.expanduser().resolve()
        if not assets.is_dir():
            parser.error(f"assets directory {assets} does not exist")
    tasks_dir = args.tasks_dir.expanduser().resolve()
    needs_git = any(isinstance(i, GitRepo) for t in selected for i in t.items)
    if needs_git and not args.dry_run and shutil.which("git") is None:
        log("ERROR: git is not on PATH (needed to fetch the pinned code repositories).")
        return 1

    ctx = Ctx(force=args.force, dry_run=args.dry_run, assets_dir=assets)
    log(f"Tasks directory: {tasks_dir}")
    log(f"Tasks: {', '.join(f'P{t.id}' for t in selected)}"
        + ("   [dry run: nothing is downloaded or written]" if args.dry_run else ""))

    summary: List[Tuple[Task, List[Result]]] = []
    try:
        for task in selected:
            summary.append((task, prepare_task(task, tasks_dir, ctx)))
    except KeyboardInterrupt:
        log("\nInterrupted. Re-run the same command to resume.")
        return 130

    log("\n" + "=" * 78)
    log(f"{'task':<5} {'name':<18} {'status':<7} items")
    log("-" * 78)
    for task, results in summary:
        counts: Dict[str, int] = {}
        for r in results:
            counts[r.status] = counts.get(r.status, 0) + 1
        detail = ", ".join(f"{n} {s.lower()}" for s, n in sorted(counts.items())) or "nothing to fetch"
        log(f"P{task.id:<4} {task.short:<18} {overall(results):<7} {detail}")
    log("=" * 78)

    warnings = [(t, r) for t, rs in summary for r in rs if r.status == WARN]
    manual = [(t, r) for t, rs in summary for r in rs if r.status == MANUAL]
    failed = [(t, r) for t, rs in summary for r in rs if r.status == FAIL]
    for title, rows in (("Warnings", warnings), ("Failures", failed)):
        if rows:
            log(f"\n{title}:")
            for task, r in rows:
                log(f"  P{task.id}: {r.message}")
    if manual:
        log("\nManual steps required:")
        for _, r in manual:
            log("  " + r.instructions)
    if args.dry_run:
        return 0
    if failed:
        return 1
    return 3 if manual else 0


# --------------------------------------------------------------------------- #
# Bundled small files (byte-exact copies of what the agents received)          #
# --------------------------------------------------------------------------- #
# chromatic/*       P7: Helm (1964) and Ekman (1954) dissimilarities extracted
#                   from the R package smacof (GPL-3), plus stimulus colorimetry
#                   transcribed from the original publications.
# ace_potentials/*  P15: LAMMPS pair_style includes generated by FitSNAP.
# dft_reference/*   P15: description of the author-provided DFT tarballs.

_EMBEDDED: Dict[str, str] = {}

# These files use CRLF line endings in the original; they are stored with LF here
# and converted on write so the output is byte-identical (checked via MD5).
_EMBEDDED_CRLF = {
    "chromatic/ekman_dissimilarities.csv",
    "chromatic/ekman_stimuli_cie.csv",
    "chromatic/helm_dissimilarities_long.csv",
    "chromatic/helm_dissimilarities_normals_avg.csv",
}

_EMBEDDED["chromatic/SOURCE_ekman.md"] = """\
# Ekman color data — provenance and colorimetry

## What this is
Human color-dissimilarity data for 14 spectral (monochromatic) colors, from:
> G. Ekman, "Dimensions of color vision," J. Psychol. 38, 467-474 (1954).

Two parts:

1. **Dissimilarities (the human responses).** A 14x14 similarity matrix over the
   14 spectral colors, ~31 subjects. Source of truth: the `smacof` R package
   (`smacof::ekman`, GPL-3), which redistributes it machine-readably (its
   row/column labels are the wavelengths below). Convert similarity `s` to
   dissimilarity as `1 - s` (the standard treatment).

2. **Stimulus colorimetry (`ekman_stimuli_cie.csv`).** Since Ekman's stimuli are
   *monochromatic lights*, they carry no CIE table of their own — their
   colorimetry is fixed by wavelength. We therefore compute each stimulus's
   chromaticity as the **CIE 1931 2-degree spectral-locus point** at its
   wavelength, from the CIE 1931 2-deg color-matching functions
   (`ciexyz31_1.csv`, cvrl.org): `x = xbar/(xbar+ybar+zbar)`, `y = ybar/(...)`.

## Stimuli
The 14 wavelengths (nm), the canonical Ekman set (= `smacof::ekman` labels):
434, 445, 465, 472, 490, 504, 537, 555, 584, 600, 610, 628, 651, 674.

- These are **saturated spectral colors on the gamut boundary**, spanning the
  full hue circle. (This complements Helm, whose 10 tiles are *moderate-chroma*
  Munsell surface colors at Value 6.0 — so the two chromatic sets sample the
  chromatic plane at two different saturation levels.)
- **Equal brightness.** Ekman's stimuli were presented at equal brightness, so
  the vendored luminance factor `Y` is set to a common nominal level
  (`Y = 31.70`, matching Helm's Value-6 level). The absolute level is arbitrary
  and is handled by the per-dataset observation model / LMS normalization; what
  matters is that `Y` is equal across the 14, as in the experiment.

## Fields in `ekman_stimuli_cie.csv`
`stimulus_nm` (wavelength) | `x`,`y` (CIE 1931 spectral-locus chromaticity) |
`Y_luminance_factor` (nominal, equal across stimuli).

## Notes for cone-space placement
Being monochromatic, each stimulus's cone excitation is also directly the CIE
2006 LMS cone fundamentals evaluated at its wavelength; the vendored `x,y,Y`
(spectral-locus) representation is provided so the pipeline is uniform with Helm
(stimuli specified in CIE coordinates, converted to LMS via the provided
fundamentals). These are self-luminous aperture colors, so a surface-color
CIELAB step is not physically natural; convert `xyY -> XYZ -> LMS` directly and
state the choice.

## Vendored files
- `ekman_stimuli_cie.csv` — the 14 spectral-locus chromaticities (above).
- `ekman_dissimilarities.csv` — the 14x14 dissimilarity matrix, extracted from
  `smacof::ekman` (the GitHub CRAN mirror `data/ekman.rda`) with the Python
  `rdata` reader (no R needed) and converted from similarity to dissimilarity as
  `d = 1 - s`. Sanity-checked: adjacent wavelengths are near (d(434,445)=0.14),
  far ones distant (d(434,674)=0.84). Labels are the ascending wavelengths above.

## Licensing
Wavelengths and CIE spectral-locus chromaticities are non-copyrightable physical
facts; the CMFs are from cvrl.org (cvrl's citation terms apply). The dissimilarity
matrix is redistributed openly via `smacof` (GPL-3). Cite Ekman (1954).
"""

_EMBEDDED["chromatic/SOURCE_helm.md"] = """\
# Helm color data — provenance and colorimetry

## What this is
Human color-dissimilarity data for 10 chromatic surface colors, from Helm's
multidimensional ratio-scaling studies. Two parts:

1. **Dissimilarities (the human responses).** Interpoint-distance estimates for
   all color pairs, per subject (10 color-normal, 4 color-deficient), from
   Helm (1959/1964). Source of truth for this file: the `smacof` R package
   (`smacof::helm`, GPL-3), which redistributes these matrices machine-readably.
   (They are also reproduced in Borg & Groenen 2005, Table 21.1.) Keyed by the
   10 tile labels A, C, E, G, I, K, M, O, Q, S.

2. **Stimulus colorimetry (`helm_stimuli_cie.csv`).** CIE chromaticity + Munsell
   for each of the 10 tiles, transcribed from **Helm (1964), Table I**:
   > C. E. Helm, "Multidimensional Ratio Scaling Analysis of Perceived Color
   > Relations," J. Opt. Soc. Am. 54(2):256-262 (1964), Table I ("Tristimulus
   > values, chromaticity coordinates, and Munsell designations of the stimuli").

## Stimulus facts (Helm 1964, sec. 4a)
- The tiles are hexagonal OSA-Committee uniform-color-scale chips (alternate
  chips from the set of 20 used in Helm's earlier successive-intervals study),
  painted matte, "approximately equally spaced in hue for daylight illumination."
- All 10 are at **Munsell Value 6.0** -> a near-isoluminant hue circle
  (luminance factor ~= 31-33%). This is the isoluminant-hue-loop geometry the
  task's identifiability analysis assumes.
- Chromaticities are specified under **CIE Illuminant C, 1931 2-degree observer**
  (the Munsell/OSA convention of that era). Use Illuminant C as the reference
  white for any CIELAB/XYZ -> LMS conversion.

## Fields in `helm_stimuli_cie.csv`
`tile` (A,C,...,S) | `munsell_hue`,`munsell_value`,`munsell_chroma` | `x`,`y`
(CIE 1931 chromaticity) | `Y_luminance_factor` (percent; = the paper's
tristimulus Y divided by 100).

The chromaticity coordinates `x`,`y` are the authoritative colorimetry and are
sufficient (with `Y`) to place each tile in cone space; the Munsell columns are
supplementary.

## Transcription caveats (recorded honestly)
- **x, y are authoritative.** Cross-check: x = X/(X+Y+Z) computed from the
  paper's reported tristimulus reproduces the reported x,y *exactly* for tiles
  G, I, K and closely for E. For tiles A and M the reported tristimulus X and Z
  digits are internally inconsistent with their reported x,y (transcription/OCR
  error in those two rows), so the raw X,Z tristimulus are **not** vendored;
  the 4-decimal chromaticities x,y are used instead. The Y (luminance) column is
  internally consistent for all 10 tiles and is retained.
- **Tile C's Munsell hue** printed as "10.0N" in our copy, which is inconsistent
  with its saturated yellowish chromaticity (x=0.3749, y=0.3835) — an evident
  transcription artifact. It is left blank; the CIE x,y are used, so there is no
  downstream impact.

## Vendored files
- `helm_stimuli_cie.csv` — the 10 tiles' colorimetry (above).
- `helm_dissimilarities_long.csv` — per-subject dissimilarities (long format:
  subject, tile_i, tile_j, dissimilarity) for all 16 subjects (N1-N10 color-normal,
  incl. the N6a/N6b replication; CD1-CD4 color-deficient), extracted from
  `smacof::helm` (`data/helm.rda`) with the Python `rdata` reader (no R needed).
  Validated: subject N1 = [6.8, 12.5, 13.8, ...] matches Helm 1964 / Borg & Groenen
  Table 21.1 (s1: AC, AE, AG, ...). This long format is authoritative.
- `helm_dissimilarities_normals_avg.csv` — convenience 10x10 mean over the
  color-normal subjects (aggregate as you prefer from the long file).

## Licensing
These are factual physical measurements of color chips (not copyrightable) and
are cited to Helm (1964). The dissimilarity matrices are redistributed openly via
`smacof` (GPL-3). Nothing paywalled is reproduced here beyond the ~10 rows of
factual colorimetry, cited to their source.
"""

_EMBEDDED["chromatic/ekman_dissimilarities.csv"] = """\
wavelength_nm,434,445,465,472,490,504,537,555,584,600,610,628,651,674
434,0.0,0.14,0.58,0.58,0.82,0.94,0.93,0.96,0.98,0.93,0.91,0.88,0.87,0.84
445,0.14,0.0,0.5,0.56,0.78,0.91,0.93,0.93,0.98,0.96,0.93,0.89,0.87,0.86
465,0.58,0.5,0.0,0.19,0.53,0.83,0.9,0.92,0.98,0.99,0.98,0.99,0.95,0.97
472,0.58,0.56,0.19,0.0,0.46,0.75,0.9,0.91,0.98,0.99,1.0,0.99,0.98,0.96
490,0.82,0.78,0.53,0.46,0.0,0.39,0.69,0.74,0.93,0.98,0.98,0.99,0.98,1.0
504,0.94,0.91,0.83,0.75,0.39,0.0,0.38,0.55,0.86,0.92,0.98,0.98,0.98,0.99
537,0.93,0.93,0.9,0.9,0.69,0.38,0.0,0.27,0.78,0.86,0.95,0.98,0.98,1.0
555,0.96,0.93,0.92,0.91,0.74,0.55,0.27,0.0,0.67,0.81,0.96,0.97,0.98,0.98
584,0.98,0.98,0.98,0.98,0.93,0.86,0.78,0.67,0.0,0.42,0.63,0.73,0.8,0.77
600,0.93,0.96,0.99,0.99,0.98,0.92,0.86,0.81,0.42,0.0,0.26,0.5,0.59,0.72
610,0.91,0.93,0.98,1.0,0.98,0.98,0.95,0.96,0.63,0.26,0.0,0.24,0.38,0.45
628,0.88,0.89,0.99,0.99,0.99,0.98,0.98,0.97,0.73,0.5,0.24,0.0,0.15,0.32
651,0.87,0.87,0.95,0.98,0.98,0.98,0.98,0.98,0.8,0.59,0.38,0.15,0.0,0.24
674,0.84,0.86,0.97,0.96,1.0,0.99,1.0,0.98,0.77,0.72,0.45,0.32,0.24,0.0
"""

_EMBEDDED["chromatic/ekman_stimuli_cie.csv"] = """\
stimulus_nm,x,y,Y_luminance_factor
434,0.1673,0.0082,31.7
445,0.1611,0.0138,31.7
465,0.1355,0.0399,31.7
472,0.1187,0.0678,31.7
490,0.0454,0.295,31.7
504,0.0036,0.633,31.7
537,0.2077,0.7711,31.7
555,0.3374,0.6588,31.7
584,0.5385,0.4607,31.7
600,0.627,0.3725,31.7
610,0.6658,0.334,31.7
628,0.7052,0.2948,31.7
651,0.7265,0.2735,31.7
674,0.7326,0.2674,31.7
"""

_EMBEDDED["chromatic/helm_dissimilarities_normals_avg.csv"] = """\
tile,A,C,E,G,I,K,M,O,Q,S
A,0.0,6.46,10.38,11.9,12.52,12.01,10.8,8.73,5.65,3.85
C,6.46,0.0,5.85,9.82,11.43,12.04,12.61,12.1,9.74,8.04
E,10.38,5.85,0.0,5.86,8.17,10.0,11.65,12.72,12.3,11.48
G,11.9,9.82,5.86,0.0,3.83,5.87,8.63,10.58,11.6,11.91
I,12.52,11.43,8.17,3.83,0.0,4.05,6.93,9.31,11.38,11.71
K,12.01,12.04,10.0,5.87,4.05,0.0,4.54,7.47,10.04,11.06
M,10.8,12.61,11.65,8.63,6.93,4.54,0.0,5.04,8.4,9.55
O,8.73,12.1,12.72,10.58,9.31,7.47,5.04,0.0,4.97,6.56
Q,5.65,9.74,12.3,11.6,11.38,10.04,8.4,4.97,0.0,3.58
S,3.85,8.04,11.48,11.91,11.71,11.06,9.55,6.56,3.58,0.0
"""

_EMBEDDED["chromatic/helm_stimuli_cie.csv"] = """\
tile,munsell_hue,munsell_value,munsell_chroma,x,y,Y_luminance_factor
A,1.3RP,6.0,6.0,0.3533,0.2780,31.69
C,,6.0,6.0,0.3749,0.3835,31.65
E,2.5Y,6.0,3.5,0.3809,0.3798,31.28
G,3.5GY,6.0,4.0,0.3577,0.4050,31.74
I,9.3GY,6.0,4.8,0.3216,0.3992,33.20
K,8.8G,6.0,4.4,0.2799,0.3519,31.46
M,1.3B,6.0,5.6,0.2553,0.3191,31.66
O,1.8PB,6.0,5.6,0.2553,0.2652,31.66
Q,10.0P,6.0,6.8,0.2734,0.2550,31.55
S,5.7P,6.0,7.2,0.2956,0.2589,31.89
"""

_EMBEDDED["ace_potentials/W_pot_T0.0.mod"] = """\
# This file was generated by FitSNAP.
# Hash: 074c6a3e6728f27c5974760fdaa38b44

pair_style hybrid/overlay zbl 3.927000 4.488000 pace product
pair_coeff 1 1 zbl 74 74
pair_coeff * * pace W_pot.yace W"""

_EMBEDDED["ace_potentials/W_pot_T0.5.mod"] = """\
# This file was generated by FitSNAP.
# Hash: 2250404c30d165d5d0190746ef4f6eee

pair_style hybrid/overlay zbl 3.927000 4.488000 pace product
pair_coeff 1 1 zbl 74 74
pair_coeff * * pace W_pot.yace W"""

_EMBEDDED["dft_reference/README.md"] = """\
# DFT reference sets (author-provided, Wood et al.)

Author-provided VASP DFT training/reference data for the layered-Sommerfeld
accuracy axis, at three electronic (Fermi-smearing) temperatures. These are the
**ground-truth energies and forces** the blended potential is compared against.
No DFT engine is provided in the workspace and no new DFT runs are permitted; the
accuracy comparison uses these provided sets only.

## Contents

Three tarballs, one per electronic temperature Te. Each unpacks to a set of
category directories (`BCC_ForceLib_*`, `EOS_Data`, `gamma_surface`, `md_bulk`,
`vacancy`, `surface`, `dislocation_quadrupole`, ...) of per-configuration JSON
files, plus a `NotConverged/` directory (see below).

| Tarball | Te | Converged configs | Role |
|---|---|---|---|
| `W-VASP_T0.001eV_JSON.tar` | 0.001 eV | 9467 | Ground-state endpoint; the DFT that `W_pot_T0.0.yace` (U_gs) was fit to. |
| `W-VASP_T0.1eV_JSON.tar` | 0.1 eV | 2444 | **Intermediate reference — the accuracy-axis ground truth** (the Supplemental Fig. 18 set). |
| `W-VASP_T0.5eV_JSON.tar` | 0.5 eV | 9445 | Hot endpoint; the DFT that `W_pot_T0.5.yace` (U_ht) was fit to. |

The Te = 0.1 eV set is **deliberately smaller** than the two endpoints: the
author paused resubmission of those jobs early once the focus moved to a
two-temperature potential. Treat 0.1 eV as a sparser reference; do not expect the
same configuration coverage as the endpoints.

## Two important usage rules

1. **Exclude `NotConverged/`.** Each tarball contains a `NotConverged/` folder
   (and, for Te = 0.5 eV, a `NotConverged.dat` listing). Those DFT calculations
   did not converge and their energies/forces are unreliable — **do not use any
   configuration under `NotConverged/` in the accuracy comparison.** Excluded
   counts: 15 (0.001 eV), 78 (0.1 eV), 258 (0.5 eV).

2. **Reference only — do not refit the provided potentials.** The two provided
   ACE potentials in `../ace_potentials/` are held fixed. The 0.001 eV and 0.5 eV
   sets here are the exact DFT those potentials were trained on; they are provided
   so you can (a) verify U_gs / U_ht reproduce their own training DFT (the
   lambda -> 0 and lambda -> 1 anchors) and (b) anchor the lambda-versus-Te
   calibration. They are **not** an invitation to retrain, re-fit, or replace
   U_gs / U_ht -- that is a held-fixed violation.

## Format

FitSNAP-readable per-configuration JSON (`Dataset` schema):

- `Energy` -- total DFT energy, electronvolt (`EnergyStyle: electronvolt`).
- `Forces` -- per-atom forces, eV/Angstrom (`ForcesStyle: electronvoltperangstrom`),
  one 3-vector per atom.
- `Positions`, `Lattice` (Angstrom), `AtomTypes` (chemical symbol, all `W`),
  `NumAtoms`, and `Stress` (kB).
- `Smearing` -- the electronic-smearing width recorded in the config, which
  identifies the set: `0.0` for the ground-state endpoint, `0.1` for the
  intermediate reference, `0.5` for the hot endpoint. Note the ground-state set
  is recorded as `Smearing = 0.0` (VASP practical-zero smearing, matching the
  `W_pot_T0.0.yace` potential name), not `0.001`; if you filter configs by the
  `Smearing` field for that set, expect `0.0`.

Each JSON is one configuration. To use: extract the tarball(s), walk the category
directories (skipping `NotConverged/`), and read `Dataset.Data[0]` for each
configuration's energy/forces.

## Role in the task

The accuracy axis of direction C compares the blended potential
`U_tot = lambda*U_ht + (1-lambda)*U_gs` against these DFT references. The
**Te = 0.1 eV set is the held-out intermediate** (the potentials were fit at
0.001 and 0.5 eV, not 0.1 eV), so it is the temperature at which the accuracy
improvement over the paper's sigmoid must be demonstrated -- reproducing the
Supplemental Fig. 18 finding (energy-optimal blend fraction lambda ~ 0.2,
force-optimal lambda ~ 0.35). The two endpoint sets anchor the lambda-versus-Te
calibration (lambda -> 0 at 0.001 eV, lambda -> 1 at 0.5 eV).
"""

_EMBEDDED["chromatic/helm_dissimilarities_long.csv"] = """\
subject,tile_i,tile_j,dissimilarity
N1,A,C,6.8
N1,A,E,12.5
N1,A,G,13.8
N1,A,I,14.2
N1,A,K,12.5
N1,A,M,11.0
N1,A,O,8.6
N1,A,Q,5.5
N1,A,S,3.5
N1,C,E,5.4
N1,C,G,8.3
N1,C,I,10.4
N1,C,K,11.6
N1,C,M,13.8
N1,C,O,14.3
N1,C,Q,11.8
N1,C,S,8.9
N1,E,G,5.2
N1,E,I,7.2
N1,E,K,9.5
N1,E,M,11.3
N1,E,O,13.5
N1,E,Q,14.6
N1,E,S,14.1
N1,G,I,3.7
N1,G,K,5.9
N1,G,M,10.1
N1,G,O,11.1
N1,G,Q,12.3
N1,G,S,12.5
N1,I,K,4.2
N1,I,M,6.9
N1,I,O,10.2
N1,I,Q,12.1
N1,I,S,11.2
N1,K,M,4.3
N1,K,O,6.8
N1,K,Q,9.9
N1,K,S,10.7
N1,M,O,4.8
N1,M,Q,7.4
N1,M,S,8.7
N1,O,Q,4.5
N1,O,S,6.1
N1,Q,S,3.6
N2,A,C,5.9
N2,A,E,11.1
N2,A,G,18.8
N2,A,I,17.3
N2,A,K,16.6
N2,A,M,16.5
N2,A,O,8.3
N2,A,Q,5.7
N2,A,S,4.2
N2,C,E,4.9
N2,C,G,10.6
N2,C,I,14.3
N2,C,K,16.6
N2,C,M,17.3
N2,C,O,14.5
N2,C,Q,9.5
N2,C,S,7.3
N2,E,G,4.8
N2,E,I,8.3
N2,E,K,13.2
N2,E,M,14.6
N2,E,O,16.1
N2,E,Q,14.0
N2,E,S,13.8
N2,G,I,3.6
N2,G,K,5.3
N2,G,M,8.2
N2,G,O,14.5
N2,G,Q,17.0
N2,G,S,17.3
N2,I,K,3.5
N2,I,M,6.8
N2,I,O,11.0
N2,I,Q,15.8
N2,I,S,15.8
N2,K,M,3.8
N2,K,O,7.4
N2,K,Q,13.8
N2,K,S,15.1
N2,M,O,5.7
N2,M,Q,10.9
N2,M,S,13.9
N2,O,Q,5.0
N2,O,S,6.0
N2,Q,S,3.5
N3,A,C,7.1
N3,A,E,10.2
N3,A,G,11.1
N3,A,I,12.5
N3,A,K,11.8
N3,A,M,9.9
N3,A,O,8.6
N3,A,Q,4.3
N3,A,S,2.9
N3,C,E,5.7
N3,C,G,11.5
N3,C,I,10.7
N3,C,K,11.8
N3,C,M,11.2
N3,C,O,12.5
N3,C,Q,9.2
N3,C,S,8.2
N3,E,G,6.7
N3,E,I,8.9
N3,E,K,9.4
N3,E,M,11.3
N3,E,O,12.5
N3,E,Q,11.9
N3,E,S,10.5
N3,G,I,3.7
N3,G,K,5.9
N3,G,M,10.3
N3,G,O,11.6
N3,G,Q,10.9
N3,G,S,11.5
N3,I,K,3.6
N3,I,M,8.2
N3,I,O,9.8
N3,I,Q,11.3
N3,I,S,11.1
N3,K,M,5.1
N3,K,O,8.1
N3,K,Q,10.2
N3,K,S,10.6
N3,M,O,4.9
N3,M,Q,8.7
N3,M,S,9.7
N3,O,Q,6.3
N3,O,S,7.5
N3,Q,S,3.0
N4,A,C,7.5
N4,A,E,10.3
N4,A,G,10.7
N4,A,I,11.6
N4,A,K,10.6
N4,A,M,9.7
N4,A,O,8.4
N4,A,Q,5.8
N4,A,S,3.6
N4,C,E,6.9
N4,C,G,8.5
N4,C,I,10.7
N4,C,K,11.1
N4,C,M,12.2
N4,C,O,10.8
N4,C,Q,9.9
N4,C,S,8.0
N4,E,G,4.9
N4,E,I,6.6
N4,E,K,8.7
N4,E,M,10.6
N4,E,O,11.7
N4,E,Q,11.1
N4,E,S,12.0
N4,G,I,3.5
N4,G,K,6.3
N4,G,M,7.8
N4,G,O,10.4
N4,G,Q,11.6
N4,G,S,11.3
N4,I,K,4.1
N4,I,M,6.5
N4,I,O,8.6
N4,I,Q,10.0
N4,I,S,10.8
N4,K,M,5.0
N4,K,O,7.4
N4,K,Q,9.1
N4,K,S,10.7
N4,M,O,5.9
N4,M,Q,8.7
N4,M,S,9.6
N4,O,Q,5.6
N4,O,S,6.7
N4,Q,S,3.5
N5,A,C,6.6
N5,A,E,10.5
N5,A,G,10.2
N5,A,I,9.6
N5,A,K,10.8
N5,A,M,9.7
N5,A,O,8.5
N5,A,Q,4.9
N5,A,S,3.5
N5,C,E,5.5
N5,C,G,9.6
N5,C,I,9.3
N5,C,K,9.9
N5,C,M,11.7
N5,C,O,11.6
N5,C,Q,10.3
N5,C,S,8.0
N5,E,G,7.2
N5,E,I,8.3
N5,E,K,9.3
N5,E,M,11.3
N5,E,O,11.9
N5,E,Q,11.8
N5,E,S,11.5
N5,G,I,4.7
N5,G,K,6.2
N5,G,M,8.9
N5,G,O,10.3
N5,G,Q,11.6
N5,G,S,10.2
N5,I,K,3.3
N5,I,M,6.3
N5,I,O,9.1
N5,I,Q,11.1
N5,I,S,10.4
N5,K,M,4.2
N5,K,O,8.9
N5,K,Q,9.4
N5,K,S,10.6
N5,M,O,6.6
N5,M,Q,8.9
N5,M,S,9.2
N5,O,Q,5.8
N5,O,S,7.3
N5,Q,S,2.9
N6a,A,C,5.2
N6a,A,E,9.4
N6a,A,G,11.4
N6a,A,I,13.3
N6a,A,K,12.0
N6a,A,M,12.3
N6a,A,O,10.6
N6a,A,Q,4.9
N6a,A,S,3.5
N6a,C,E,6.2
N6a,C,G,11.2
N6a,C,I,13.5
N6a,C,K,12.9
N6a,C,M,12.0
N6a,C,O,11.5
N6a,C,Q,8.2
N6a,C,S,6.3
N6a,E,G,5.6
N6a,E,I,8.2
N6a,E,K,9.6
N6a,E,M,12.7
N6a,E,O,13.7
N6a,E,Q,13.4
N6a,E,S,11.7
N6a,G,I,4.0
N6a,G,K,5.8
N6a,G,M,6.8
N6a,G,O,9.3
N6a,G,Q,10.5
N6a,G,S,12.2
N6a,I,K,3.8
N6a,I,M,5.4
N6a,I,O,7.9
N6a,I,Q,9.9
N6a,I,S,13.2
N6a,K,M,3.6
N6a,K,O,5.6
N6a,K,Q,9.0
N6a,K,S,10.4
N6a,M,O,4.2
N6a,M,Q,8.2
N6a,M,S,9.8
N6a,O,Q,5.1
N6a,O,S,6.8
N6a,Q,S,3.8
N6b,A,C,5.8
N6b,A,E,10.5
N6b,A,G,13.4
N6b,A,I,14.0
N6b,A,K,13.2
N6b,A,M,11.7
N6b,A,O,10.2
N6b,A,Q,6.4
N6b,A,S,3.5
N6b,C,E,4.9
N6b,C,G,12.2
N6b,C,I,14.8
N6b,C,K,14.6
N6b,C,M,14.1
N6b,C,O,13.4
N6b,C,Q,9.7
N6b,C,S,7.9
N6b,E,G,4.6
N6b,E,I,8.3
N6b,E,K,10.7
N6b,E,M,12.8
N6b,E,O,14.1
N6b,E,Q,12.9
N6b,E,S,10.9
N6b,G,I,3.5
N6b,G,K,4.7
N6b,G,M,8.8
N6b,G,O,11.0
N6b,G,Q,11.8
N6b,G,S,11.7
N6b,I,K,3.6
N6b,I,M,6.9
N6b,I,O,9.4
N6b,I,Q,12.4
N6b,I,S,13.7
N6b,K,M,4.1
N6b,K,O,6.9
N6b,K,Q,10.6
N6b,K,S,12.2
N6b,M,O,4.1
N6b,M,Q,10.0
N6b,M,S,11.1
N6b,O,Q,4.1
N6b,O,S,6.9
N6b,Q,S,3.4
N7,A,C,6.2
N7,A,E,10.8
N7,A,G,9.9
N7,A,I,11.1
N7,A,K,10.3
N7,A,M,8.8
N7,A,O,7.6
N7,A,Q,5.8
N7,A,S,3.0
N7,C,E,7.5
N7,C,G,8.9
N7,C,I,10.7
N7,C,K,10.8
N7,C,M,10.6
N7,C,O,10.4
N7,C,Q,9.0
N7,C,S,7.5
N7,E,G,6.3
N7,E,I,8.7
N7,E,K,9.6
N7,E,M,10.1
N7,E,O,10.8
N7,E,Q,11.7
N7,E,S,9.4
N7,G,I,3.9
N7,G,K,6.8
N7,G,M,9.4
N7,G,O,9.7
N7,G,Q,10.4
N7,G,S,9.7
N7,I,K,5.0
N7,I,M,8.3
N7,I,O,9.0
N7,I,Q,10.9
N7,I,S,9.6
N7,K,M,4.3
N7,K,O,7.3
N7,K,Q,9.0
N7,K,S,8.8
N7,M,O,4.9
N7,M,Q,7.2
N7,M,S,7.6
N7,O,Q,4.7
N7,O,S,5.6
N7,Q,S,3.5
N8,A,C,7.5
N8,A,E,9.1
N8,A,G,10.2
N8,A,I,12.1
N8,A,K,12.5
N8,A,M,9.7
N8,A,O,9.8
N8,A,Q,8.3
N8,A,S,6.7
N8,C,E,4.4
N8,C,G,7.9
N8,C,I,10.4
N8,C,K,11.2
N8,C,M,12.6
N8,C,O,11.4
N8,C,Q,11.3
N8,C,S,10.4
N8,E,G,5.7
N8,E,I,8.3
N8,E,K,10.2
N8,E,M,11.3
N8,E,O,12.2
N8,E,Q,11.9
N8,E,S,10.7
N8,G,I,3.9
N8,G,K,6.5
N8,G,M,8.7
N8,G,O,10.3
N8,G,Q,10.7
N8,G,S,12.6
N8,I,K,4.6
N8,I,M,7.8
N8,I,O,9.9
N8,I,Q,11.2
N8,I,S,11.6
N8,K,M,6.3
N8,K,O,9.6
N8,K,Q,10.6
N8,K,S,11.6
N8,M,O,4.8
N8,M,Q,6.8
N8,M,S,9.1
N8,O,Q,4.6
N8,O,S,7.4
N8,Q,S,5.2
N9,A,C,6.0
N9,A,E,9.4
N9,A,G,9.5
N9,A,I,9.5
N9,A,K,9.8
N9,A,M,8.7
N9,A,O,6.7
N9,A,Q,4.9
N9,A,S,4.1
N9,C,E,7.1
N9,C,G,9.5
N9,C,I,9.5
N9,C,K,9.9
N9,C,M,10.6
N9,C,O,10.6
N9,C,Q,8.5
N9,C,S,7.9
N9,E,G,7.6
N9,E,I,8.9
N9,E,K,9.8
N9,E,M,10.5
N9,E,O,10.7
N9,E,Q,9.7
N9,E,S,10.2
N9,G,I,3.8
N9,G,K,5.3
N9,G,M,7.3
N9,G,O,7.6
N9,G,Q,9.2
N9,G,S,10.1
N9,I,K,4.8
N9,I,M,6.2
N9,I,O,8.2
N9,I,Q,9.1
N9,I,S,9.7
N9,K,M,4.7
N9,K,O,6.7
N9,K,Q,8.8
N9,K,S,9.9
N9,M,O,4.5
N9,M,Q,7.2
N9,M,S,6.8
N9,O,Q,4.0
N9,O,S,5.3
N9,Q,S,3.4
N10,A,C,9.2
N10,A,E,10.8
N10,A,G,9.7
N10,A,I,10.1
N10,A,K,10.3
N10,A,M,9.7
N10,A,O,9.0
N10,A,Q,6.6
N10,A,S,4.6
N10,C,E,5.5
N10,C,G,8.2
N10,C,I,9.4
N10,C,K,10.1
N10,C,M,10.5
N10,C,O,10.8
N10,C,Q,11.2
N10,C,S,10.5
N10,E,G,4.6
N10,E,I,6.7
N10,E,K,9.8
N10,E,M,11.3
N10,E,O,11.9
N10,E,Q,11.5
N10,E,S,10.2
N10,G,I,3.7
N10,G,K,6.6
N10,G,M,8.7
N10,G,O,10.6
N10,G,Q,10.0
N10,G,S,7.7
N10,I,K,4.0
N10,I,M,7.5
N10,I,O,9.9
N10,I,Q,10.9
N10,I,S,10.6
N10,K,M,5.4
N10,K,O,9.3
N10,K,Q,9.9
N10,K,S,9.7
N10,M,O,5.6
N10,M,Q,8.2
N10,M,S,9.7
N10,O,Q,5.3
N10,O,S,6.3
N10,Q,S,3.4
CD1,A,C,11.5
CD1,A,E,13.1
CD1,A,G,12.6
CD1,A,I,10.6
CD1,A,K,10.6
CD1,A,M,10.8
CD1,A,O,7.3
CD1,A,Q,5.4
CD1,A,S,5.0
CD1,C,E,6.0
CD1,C,G,7.9
CD1,C,I,8.4
CD1,C,K,9.4
CD1,C,M,10.2
CD1,C,O,11.3
CD1,C,Q,11.5
CD1,C,S,11.5
CD1,E,G,6.2
CD1,E,I,8.4
CD1,E,K,9.9
CD1,E,M,10.3
CD1,E,O,12.7
CD1,E,Q,12.9
CD1,E,S,10.7
CD1,G,I,5.2
CD1,G,K,6.5
CD1,G,M,8.8
CD1,G,O,11.2
CD1,G,Q,11.7
CD1,G,S,10.2
CD1,I,K,4.1
CD1,I,M,7.0
CD1,I,O,10.4
CD1,I,Q,10.8
CD1,I,S,10.6
CD1,K,M,6.4
CD1,K,O,9.9
CD1,K,Q,9.4
CD1,K,S,10.1
CD1,M,O,4.2
CD1,M,Q,8.4
CD1,M,S,8.1
CD1,O,Q,4.5
CD1,O,S,6.4
CD1,Q,S,3.0
CD2a,A,C,9.3
CD2a,A,E,10.7
CD2a,A,G,10.7
CD2a,A,I,11.9
CD2a,A,K,11.0
CD2a,A,M,9.8
CD2a,A,O,8.9
CD2a,A,Q,8.9
CD2a,A,S,5.1
CD2a,C,E,6.5
CD2a,C,G,8.0
CD2a,C,I,8.2
CD2a,C,K,8.9
CD2a,C,M,9.3
CD2a,C,O,10.7
CD2a,C,Q,10.1
CD2a,C,S,9.6
CD2a,E,G,4.4
CD2a,E,I,7.0
CD2a,E,K,10.8
CD2a,E,M,10.4
CD2a,E,O,11.8
CD2a,E,Q,11.6
CD2a,E,S,10.2
CD2a,G,I,4.6
CD2a,G,K,9.6
CD2a,G,M,10.8
CD2a,G,O,11.9
CD2a,G,Q,11.3
CD2a,G,S,10.9
CD2a,I,K,5.8
CD2a,I,M,8.0
CD2a,I,O,10.5
CD2a,I,Q,10.4
CD2a,I,S,10.7
CD2a,K,M,7.7
CD2a,K,O,9.6
CD2a,K,Q,10.6
CD2a,K,S,10.7
CD2a,M,O,7.4
CD2a,M,Q,9.0
CD2a,M,S,8.7
CD2a,O,Q,4.5
CD2a,O,S,7.0
CD2a,Q,S,4.5
CD2b,A,C,9.0
CD2b,A,E,10.0
CD2b,A,G,10.4
CD2b,A,I,10.0
CD2b,A,K,9.3
CD2b,A,M,8.6
CD2b,A,O,8.8
CD2b,A,Q,7.5
CD2b,A,S,5.8
CD2b,C,E,6.9
CD2b,C,G,8.9
CD2b,C,I,8.4
CD2b,C,K,8.3
CD2b,C,M,9.7
CD2b,C,O,11.1
CD2b,C,Q,10.6
CD2b,C,S,10.3
CD2b,E,G,6.0
CD2b,E,I,6.8
CD2b,E,K,8.2
CD2b,E,M,10.9
CD2b,E,O,11.6
CD2b,E,Q,9.6
CD2b,E,S,10.5
CD2b,G,I,4.2
CD2b,G,K,7.3
CD2b,G,M,10.1
CD2b,G,O,10.2
CD2b,G,Q,10.6
CD2b,G,S,10.3
CD2b,I,K,5.2
CD2b,I,M,7.6
CD2b,I,O,9.2
CD2b,I,Q,10.3
CD2b,I,S,10.3
CD2b,K,M,6.4
CD2b,K,O,9.5
CD2b,K,Q,10.0
CD2b,K,S,9.6
CD2b,M,O,7.0
CD2b,M,Q,7.9
CD2b,M,S,8.7
CD2b,O,Q,4.8
CD2b,O,S,6.7
CD2b,Q,S,4.3
CD3,A,C,10.4
CD3,A,E,12.4
CD3,A,G,12.8
CD3,A,I,13.7
CD3,A,K,11.8
CD3,A,M,4.3
CD3,A,O,4.0
CD3,A,Q,5.5
CD3,A,S,4.1
CD3,C,E,8.1
CD3,C,G,10.8
CD3,C,I,10.4
CD3,C,K,4.6
CD3,C,M,9.6
CD3,C,O,12.3
CD3,C,Q,14.2
CD3,C,S,13.0
CD3,E,G,3.5
CD3,E,I,4.3
CD3,E,K,7.9
CD3,E,M,13.0
CD3,E,O,13.8
CD3,E,Q,14.8
CD3,E,S,13.9
CD3,G,I,3.5
CD3,G,K,9.0
CD3,G,M,12.3
CD3,G,O,12.3
CD3,G,Q,12.9
CD3,G,S,14.5
CD3,I,K,7.0
CD3,I,M,13.1
CD3,I,O,13.1
CD3,I,Q,13.6
CD3,I,S,14.1
CD3,K,M,9.9
CD3,K,O,11.3
CD3,K,Q,13.6
CD3,K,S,12.3
CD3,M,O,3.9
CD3,M,Q,5.3
CD3,M,S,6.4
CD3,O,Q,4.7
CD3,O,S,3.2
CD3,Q,S,2.4
CD4,A,C,9.9
CD4,A,E,13.2
CD4,A,G,12.3
CD4,A,I,11.1
CD4,A,K,8.7
CD4,A,M,5.6
CD4,A,O,7.4
CD4,A,Q,6.4
CD4,A,S,5.8
CD4,C,E,7.3
CD4,C,G,7.9
CD4,C,I,6.9
CD4,C,K,6.8
CD4,C,M,9.9
CD4,C,O,13.1
CD4,C,Q,12.7
CD4,C,S,12.1
CD4,E,G,4.5
CD4,E,I,5.3
CD4,E,K,9.7
CD4,E,M,11.5
CD4,E,O,13.7
CD4,E,Q,14.1
CD4,E,S,13.4
CD4,G,I,5.3
CD4,G,K,8.6
CD4,G,M,12.5
CD4,G,O,13.4
CD4,G,Q,14.1
CD4,G,S,13.1
CD4,I,K,6.9
CD4,I,M,9.0
CD4,I,O,12.2
CD4,I,Q,12.5
CD4,I,S,13.4
CD4,K,M,6.7
CD4,K,O,9.7
CD4,K,Q,11.3
CD4,K,S,9.9
CD4,M,O,5.5
CD4,M,Q,7.4
CD4,M,S,5.4
CD4,O,Q,4.2
CD4,O,S,4.0
CD4,Q,S,4.3
"""


if __name__ == "__main__":
    sys.exit(main())
