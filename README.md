# into-the-unknown
Official code repository for "Into the Unknown: Benchmarking AI agents on open scientific research problems"

The 15 tasks (original papers and their scientist-proposed follow-up directions) are listed in [task_overview.md](task_overview.md).

```
tasks/paper{1..15}/                       task_spec.md, paper.pdf, paper.md, images/  (+ code/, data/ after step 1)
src/solver/                               agent harnesses + run.sh
src/grader/                               Codex judge and Claude Code judge
src/grader/eval_artifacts/paper{1..15}/   direction_specific_rubric.json, integrity_check_rubric.json, grading.py
artifacts/judge_results/                  the paper's judgments (GPT-6-Astra judge) of every agent submission
artifacts/ai_written_papers/              the papers the agents wrote
viewer/                                   website for browsing the leaderboard, verdicts and papers
assets/paper15/                           author-provided P15 files (xz), unpacked by prepare_tasks.py
```

## 1. Prepare the task materials

```bash
python prepare_tasks.py                  # download each task's code/ and data/ into tasks/paper{i}/
python prepare_tasks.py --papers 1 5     # only some tasks; --dry-run shows the plan
python ocr_paper.py --overwrite          # optional: rebuild paper.md (+ images/) from paper.pdf (needs MISTRAL_API_KEY)
```

## 2. Run an agent

```bash
bash src/solver/run.sh <agent> <paper_id>
# agent: opus-4.8 | opus-5 | opus-5.5 | fable-5 | sol-5.6 | astra-6 | kimi-k3 | deepseek-v4-pro | glm-5.3
```

Each run gets a fresh workspace `runs/<agent>/paper<i>/` (a copy of `tasks/paper<i>/`); the agent writes its submission to `proposal/` and is resumed up to 5 times until the paper is complete.

Model access is chosen by environment variables set before `run.sh`:

| agent | subscription | API / gateway | self-hosted |
|---|---|---|---|
| `opus-4.8`, `opus-5`, `opus-5.5`, `fable-5` | default (`claude login`); `CLAUDE_CONFIG_DIR` picks the account | `ANTHROPIC_API_KEY`, or `ANTHROPIC_AUTH_TOKEN` + `ANTHROPIC_BASE_URL` | — |
| `sol-5.6`, `astra-6` | default (`codex login`); `CODEX_HOME` picks the account | `OPENAI_API_KEY` (+ `OPENAI_BASE_URL`), or `CODEX_GATEWAY_BASE_URL` + `CODEX_GATEWAY_API_KEY` | — |
| `kimi-k3`, `deepseek-v4-pro`, `glm-5.3` | — | `<M>_BASE_URL` + `<M>_API_KEY` (e.g. OpenRouter) | `<M>_BASE_URL` of your OpenAI-compatible server (SGLang, vLLM) |

`<M>` is `KIMI`, `DEEPSEEK` or `GLM`; `CLAUDE_MODEL`, `CODEX_MODEL` and `<M>_MODEL` override the model id (e.g. a gateway's name for it). `astra-6` needs Codex CLI ≥ 0.153.4: install a recent `openai-codex`, or point `CODEX_BIN` at a newer `codex`. A subscription usage limit pauses a run until the limit resets; it never uses up an attempt. Neither does a turn that ends while the agent's own experiments are still running, or one that ends on a wakeup the agent scheduled for itself: the runners wait for them first. A Claude run can be restarted where it stopped (e.g. after a node failure) with `CLAUDE_RESUME_SESSION=<session id>`, `CLAUDE_RESUME_ATTEMPT=<attempt>` and `CLAUDE_PRIOR_ELAPSED_S=<seconds>` on the same account and workspace; it continues the same session with its attempt count and run time. A completion judge that fails to return a verdict is retried before it counts against an attempt. An overloaded model ("at capacity") also just pauses the run; any other API or connection error is retried at most 5 times (the same for every runner) before the run ends with what it has. With `CODEX_AVOID_CREDITS=1`, a Codex run on a plan that also holds purchased credits pauses at 98% of the plan window instead of spending them.

## 3. Judge the submissions

```bash
python src/grader/run_codex_judge.py prepare                  # prints BATCH_DIRECTORY=...
python src/grader/run_codex_judge.py launch --root <BATCH_DIRECTORY>
python src/grader/run_codex_judge.py status --root <BATCH_DIRECTORY>
```

`src/grader/run_claude_code_judge.py` takes the same commands (`prepare`, `launch`, `status`, `pause`, `resume`). Submissions are read from `runs/` (override with `SOG_SUBMISSIONS`), batches are written to `judge_runs/` (`SOG_JUDGE_RUNS`). Running `rescope --root <BATCH_DIRECTORY> --reason paper` before `launch` uses the paper's setting: the judge also sees the solver files outside `proposal/`, and the scope is repeat 1 for every submission plus repeats 2-4 for three agents (240 evaluations). To judge other agents, `prepare --agents astra-6` (directory names under `runs/`) builds a batch of just those agents, which `rescope` then sets to all four repeats of each submission; `rescope --slots-per-account N` sets the concurrent judges per account; `prepare --agents <agent> --papers 1 2 ...` judges only some of its papers (e.g. while the rest are still running), and `--repeats 2 3 4` adds three judgments to an earlier batch's repeat 1. With `SOG_CODEX_AVOID_CREDITS=1` at `prepare`, a Codex batch waits at each account's cap instead of spending purchased credits.

Both judges run on subscriptions (API keys in the environment are ignored) and pace themselves against the plan's usage windows:

| judge | login | more accounts (parallel sessions) |
|---|---|---|
| Codex | `codex login` (ChatGPT); `CODEX_BIN` selects the CLI | `SOG_CODEX_HOMES=dir1,dir2`, each logged in with `CODEX_HOME=<dir> codex login` |
| Claude Code | `claude login` | `SOG_CLAUDE_CONFIG_DIRS=dir1,dir2`, each logged in with `CLAUDE_CONFIG_DIR=<dir> claude login` |

## 4. Analyze where agents fail and check their grades

```bash
python src/grader/eval_artifacts/paper<i>/grading.py <judged_bundle.json>   # G1 integrity, G2 completeness, G3 scientist's bar
python src/grader/eval_artifacts/paper<i>/grading.py --template             # empty bundle to fill in
```

## 5. Browse the judge's verdicts

`artifacts/judge_results/gpt-6-astra/` holds all 540 GPT-6-Astra judgments of the paper's results (four for each of the 135 submissions) (see `artifacts/judge_results/README.md`). The viewer shows the leaderboard (almost and strict $G_1 \cap G_2 \cap G_3$, median run time), an analysis page with every pass rate and a per-paper grid, the 15 research directions, the papers the agents wrote (`artifacts/ai_written_papers/`), and, for each submission, every rubric node grouped by the gate it counts toward, with the grade it received, the judge's reasoning and evidence, and the submission's files with the ones the judge cited:

```bash
python viewer/serve.py        # opens http://127.0.0.1:8000/viewer/
```

The viewer is a static site that reads the repository's files, so GitHub Pages can host it as is. With
Settings → Pages → Source set to **GitHub Actions**, `.github/workflows/pages.yml` publishes the repository on every push
to `main` (or deploy from the branch with folder `/ (root)` instead). The site opens at `https://<user>.github.io/<repo>/`,
where `index.html` forwards to `viewer/`.
