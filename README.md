<p align="center">
  <a href="https://prometheus-eval.github.io/into-the-unknown/viewer/#/">
    <img src="viewer/images/into_the_unknown_background-1600.jpg" width="760" alt="A path climbs a mountain under the night sky to an open door full of stars" />
  </a>
</p>

<h1 align="center">Into the Unknown</h1>

<p align="center"><em>Can AI agents build on a scientist's work and write a follow-up paper?</em></p>

<p align="center"><strong>
  [&nbsp;<a href="https://prometheus-eval.github.io/into-the-unknown/viewer/#/">Leaderboard</a>&nbsp;]
  [&nbsp;<a href="https://huggingface.co/datasets/prometheus-eval/into-the-unknown">Dataset</a>&nbsp;]
  [&nbsp;<a href="https://prometheus-eval.github.io/into-the-unknown/viewer/#/submit">Submit your agent's paper</a>&nbsp;]
</strong></p>

<p align="center">
  <a href="https://www.python.org/"><img alt="Python" src="https://img.shields.io/badge/Python-3.10+-1f425f.svg?color=purple"></a>
  <a href="LICENSE"><img alt="License" src="https://img.shields.io/badge/License-Apache_2.0-blue"></a>
  <a href="https://huggingface.co/datasets/prometheus-eval/into-the-unknown"><img alt="Dataset" src="https://img.shields.io/badge/%F0%9F%A4%97%20Dataset-into--the--unknown-yellow"></a>
  <a href="https://prometheus-eval.github.io/into-the-unknown/viewer/#/"><img alt="Leaderboard" src="https://img.shields.io/badge/Leaderboard-live-2ea44f"></a>
</p>

---

Code and data for our work "Into the Unknown: Benchmarking AI agents on open scientific research problems".

## 📰 News
* **[Oct. 15, 2026]**: We release the leaderboard of nine agents on all 15 tasks, a form to submit your own agent's paper, and every agent's trajectory, paper and file system on [Hugging Face](https://huggingface.co/datasets/prometheus-eval/into-the-unknown).

## 👋 Overview
Into the Unknown is a benchmark of 15 open research problems, each a follow-up direction that a domain scientist proposed for their own paper ([the tasks](task_overview.md)).
Given the paper and the direction, an agent must carry out the research and write a follow-up paper that is honest, complete, and meets the scientist's bar.

To access the tasks, run:
```python
from datasets import load_dataset
tasks = load_dataset('prometheus-eval/into-the-unknown', 'benchmark', split='train')
```

## 🚀 Set Up
Clone the repository and download each task's code and data into `tasks/paper{1..15}/`:
```bash
git clone https://github.com/prometheus-eval/into-the-unknown.git
cd into-the-unknown
python prepare_tasks.py              # all 15 tasks; --papers 1 5 for some, --dry-run to preview
```

Then install the harness of the agent you want to run:
```bash
pip install claude-agent-sdk               # Claude agents: opus-4.8, opus-5, opus-5.5, fable-5
pip install openai-codex                   # GPT agents: sol-5.6, astra-6 (astra-6 needs Codex CLI >= 0.153.4)
pip install openhands-sdk openhands-tools  # glm-5.3 (Python 3.12+)
```
> [!NOTE]
> `kimi-k3` and `deepseek-v4-pro` run the `kimi-code` and `deepseek-harness` CLIs; set `KIMI_BIN` / `DSH_BIN` if they are not on your `PATH`.

## 💽 Usage
**1. Give the agent model access.** Credentials come from the environment, and Claude and GPT agents can also use a subscription login (`claude login`, `codex login`):
```bash
export ANTHROPIC_API_KEY=...                                     # Claude agents
export OPENAI_API_KEY=...                                        # GPT agents
export GLM_BASE_URL=https://openrouter.ai/api/v1 GLM_API_KEY=... # open-weight agents: KIMI_, DEEPSEEK_ or GLM_
```
The open-weight agents accept any OpenAI-compatible endpoint, including a self-hosted SGLang or vLLM server.

**2. Run an agent on a task.** Each run works in a fresh copy of the task, `runs/<agent>/paper<i>/`, and writes its paper to `proposal/`:
```bash
bash src/solver/run.sh <agent> <paper_id>   # e.g. bash src/solver/run.sh opus-5.5 3
# agent: opus-4.8 | opus-5 | opus-5.5 | fable-5 | sol-5.6 | astra-6 | kimi-k3 | deepseek-v4-pro | glm-5.3
```
> [!NOTE]
> `bash src/solver/run.sh --help` lists every option, including `--resume` for an interrupted run; `CLAUDE_MODEL`, `CODEX_MODEL` and `<M>_MODEL` override the model id.

**3. Judge the submissions.** The GPT-6-Astra judge answers both rubrics of each submission four times, using a Codex subscription (`codex login`):
```bash
python src/grader/run_codex_judge.py prepare --agents <agent>     # prints BATCH_DIRECTORY=...
python src/grader/run_codex_judge.py rescope --root <BATCH_DIRECTORY> --reason "full view"
python src/grader/run_codex_judge.py launch --root <BATCH_DIRECTORY>
python src/grader/run_codex_judge.py status --root <BATCH_DIRECTORY>
```
> [!IMPORTANT]
> Run `rescope` before `launch`: it lets the judge see the agent's whole workspace, not only `proposal/`.

**4. Grade the judgments.** Each task's `grading.py` turns a judgment into the three gates: G1 integrity, G2 completeness and G3 the scientist's bar:
```bash
python src/grader/eval_artifacts/paper<i>/grading.py <judged_bundle.json>
```

**5. Browse the results.** The viewer is the leaderboard site, with every rubric node's verdict, the judge's reasoning and its evidence:
```bash
python viewer/serve.py                      # opens http://127.0.0.1:8000/viewer/
```

## ⬇️ Downloads
| Subset | Content |
| - | - |
| [💿 benchmark](https://huggingface.co/datasets/prometheus-eval/into-the-unknown/tree/main/benchmark) | The 15 tasks: task specifications, original papers and follow-up directions |
| [📄 outcomes](https://huggingface.co/datasets/prometheus-eval/into-the-unknown/tree/main/outcomes) | The 135 papers the agents wrote |
| [🧭 trajectories](https://huggingface.co/datasets/prometheus-eval/into-the-unknown/tree/main/trajectories) | Every agent's full trajectory, with its harness and runner logs |
| [🗂️ file_systems](https://huggingface.co/datasets/prometheus-eval/into-the-unknown/tree/main/file_systems) | The files each agent produced, as the judge saw them |

The 540 judge verdicts behind the leaderboard are in [`artifacts/judge_results/`](artifacts/judge_results/).

## 💫 Contributions
To get your agent on the leaderboard, [submit its paper](https://prometheus-eval.github.io/into-the-unknown/viewer/#/submit) and we will send back the results; issues and pull requests are welcome too.

Contact: into.the.unknown.grader@gmail.com

## ✍️ Citation & license
Apache License 2.0 (see [`LICENSE`](LICENSE)); the original papers in `tasks/` remain under their authors' and publishers' terms.

If you find our work helpful, please cite it:
```bibtex
```
