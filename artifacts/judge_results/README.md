# Judge results

`gpt-6-astra/` holds every official judgment by the GPT-6-Astra judge (reasoning effort `max`, Codex SDK),
judged following `src/grader/run_codex_judge.py` under the paper's setting: the separated-rubrics evidence
protocol and the full-submission view (`proposal/` plus the solver's working directory).

```
gpt-6-astra/
  index.json                                 leaderboard, agents, papers, and one summary row per judgment
  <agent>/paper<i>/consensus.json            the judgment the leaderboard grades (see below)
  <agent>/paper<i>/files.json                the files of the submission the judge saw, and which cited paths exist
  <agent>/paper<i>/repeat_<r>/
    integrity_check_rubric.json              the judge's verdict for src/grader/eval_artifacts/paper<i>/integrity_check_rubric.json
    direction_specific_rubric.json           the judge's verdict for src/grader/eval_artifacts/paper<i>/direction_specific_rubric.json
    grade.json                               gates G1-G3 and every node's status, from src/grader/eval_artifacts/paper<i>/grading.py
```

- `<agent>` uses the solver directory names of `src/solver/run.sh` (`opus-4.8`, `opus-5`, `fable-5`, `opus-5.5`,
  `sol-5.6`, `astra-6`, `deepseek-v4-pro`, `kimi-k3`, `glm-5.3`).
- Verdict files are the judge's output, unmodified: for each rubric node its `value`, `confidence`, `reason`,
  `evidence`, and adjudication flags; the direction verdict also records the task `context` it established.
- `grade.json` is recomputed with this repository's graders. Node `status` is one of `pass`, `fail`,
  `undetermined`, `not_applicable` or `diagnostic`; `gate` is the gate the node counts toward.
- Repeats: four independent judgments of every submission (nine agents x 15 papers, 540 in total).

- `consensus.json` combines a submission's judgments node by node: a grade counts only when more than half of the
  judgments gave it (3 of 4), otherwise the node has no grade and counts as failed. It is graded with the same
  grader and reproduces the paper's main table, whose almost grading passes a gate when at least 90% of its nodes pass. Per node it lists the votes and `repeat`, the judgment whose
  reasoning matches the majority grade; per gate, how many counted nodes failed.
- `files.json` lists the full-submission view the judge read (`proposal/` and the solver's working directory as
  `workdir_outside_proposal/`; transcripts and environment folders were not shown and are listed under `omitted` and
  `excluded`). Large folders are summarised by file count and size, but every file or folder cited in the evidence of
  any judgment is listed; `cites` records whether each cited path exists (`f` file, `d` folder, `g` pattern, `x` not found).
- `index.json` also gives each submission's run time (`hours`, the wall-clock time of the agent's run) and each agent's
  reasoning effort and median run time.
- `grading_policy/paper<i>.json` lists, for every node of a paper, its gate, its role (`required`, `conditional`
  and `composite_member` nodes count toward a gate; `optional` and `diagnostic` ones do not) and the outcome of each
  rubric class. It is read from `src/grader/eval_artifacts/paper<i>/grading.py` (`load_policy`).

Browse them with the viewer: `python viewer/serve.py`.
