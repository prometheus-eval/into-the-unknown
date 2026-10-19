#!/usr/bin/env bash
# Launch one solver agent on one task in a fresh workspace.
#
# Usage:
#   bash src/solver/run.sh [--resume] [--cpu-only] <agent> <paper_id> [workspace_root]
#
#   agent           opus-4.8 | opus-5 | opus-5.5 | fable-5 | sol-5.6 | astra-6 | kimi-k3 | deepseek-v4-pro | glm-5.3
#   paper_id        1..15 (the task in tasks/paper<paper_id>/)
#   workspace_root  default: <repo>/runs. The workspace is <workspace_root>/<agent>/paper<paper_id>
#   --resume        relaunch in an existing, non-empty workspace (nothing is re-copied)
#   --cpu-only      hide GPUs (CUDA_VISIBLE_DEVICES=""); tasks whose task_spec provisions a
#                   CPU-only machine were run this way
#
# A fresh workspace is created and the contents of tasks/paper<id>/ (task_spec.md, paper.md,
# paper.pdf, code/, data/, images/, ...) are copied into it. The agent runs with the workspace
# as its working directory; it writes proposal/report.{tex,pdf}, and the runner writes
# ai_scientist_trajectory.json next to it. Console output is also saved to
# <workspace_root>/<agent>/paper<id>_<timestamp>.log.
#
# Every harness uses the same completion ("Ralph Wiggum") loop: an initial attempt plus up to
# 5 resumes, stopping as soon as the placeholder check and the completion judge pass.
#
# Under a job memory limit (a PBS or Slurm job's cgroup) every runner also starts
# memory_watchdog.py: an experiment that outgrows the limit is stopped, and the agent told,
# instead of the scheduler ending the whole run with the agent. SOLVER_MEMORY_WATCHDOG=0 turns it off.
#
# Harness and environment per agent (endpoints and keys come only from the environment):
#   opus-4.8, opus-5, opus-5.5, fable-5   run_agent_sdk.py (pip: claude-agent-sdk)
#       ANTHROPIC_API_KEY, or ANTHROPIC_AUTH_TOKEN [+ ANTHROPIC_BASE_URL] for a gateway.
#       If neither is set, the Claude subscription login (`claude login`) is used.
#       CLAUDE_MODEL overrides the model id (e.g. anthropic/claude-fable-5 on OpenRouter).
#       CLAUDE_CONFIG_DIR selects which logged-in subscription account a run uses.
#   sol-5.6, astra-6            run_codex_sdk.py (pip: openai-codex; astra-6 needs Codex CLI >= 0.153.4)
#       OPENAI_API_KEY [+ OPENAI_BASE_URL], or CODEX_GATEWAY_BASE_URL + CODEX_GATEWAY_API_KEY.
#       If neither is set, the ChatGPT login in $CODEX_HOME (default ~/.codex) is used.
#       CODEX_BIN points the SDK at a newer Codex CLI than the one bundled with openai-codex.
#   kimi-k3                     run_served_harness.py --harness kimi (kimi-code CLI)
#       KIMI_BASE_URL, KIMI_API_KEY [, KIMI_MODEL, KIMI_BIN]
#   deepseek-v4-pro             run_served_harness.py --harness dsh (deepseek-harness CLI)
#       DEEPSEEK_BASE_URL, DEEPSEEK_API_KEY [, DEEPSEEK_MODEL, DSH_BIN]
#   glm-5.3                     run_openhands_sdk.py (pip: openhands-sdk openhands-tools)
#       GLM_BASE_URL, GLM_API_KEY [, GLM_MODEL]
#   *_BASE_URL is any OpenAI-compatible endpoint: a self-hosted server (SGLang/vLLM, key
#   optional) or an API gateway such as https://openrouter.ai/api/v1. The model-id defaults
#   below are the ids we used; gateways may name them differently (e.g. deepseek/deepseek-v4-pro-0813
#   or z-ai/glm-5.3 on OpenRouter). PYTHON selects the interpreter/venv for the runner.

set -euo pipefail

SOLVER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SOLVER_DIR/../.." && pwd)"

usage() { sed -n '2,/^$/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }
die() { echo "[run.sh] $*" >&2; exit 2; }

RESUME=0
CPU_ONLY=0
POS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --resume)   RESUME=1 ;;
    --cpu-only) CPU_ONLY=1 ;;
    -h|--help)  usage; exit 0 ;;
    -*)         die "unknown option: $1" ;;
    *)          POS+=("$1") ;;
  esac
  shift
done
[ "${#POS[@]}" -ge 2 ] && [ "${#POS[@]}" -le 3 ] || { usage >&2; exit 2; }

AGENT="${POS[0]}"
ID="${POS[1]#paper}"
WS_ROOT="${POS[2]:-$REPO_ROOT/runs}"

case "$AGENT" in
  opus-4.8|opus-5|opus-5.5|fable-5)  RUNNER=run_agent_sdk.py ;;
  sol-5.6|astra-6)                   RUNNER=run_codex_sdk.py ;;
  kimi-k3)                  RUNNER=run_served_harness.py; NEED_URL=KIMI_BASE_URL ;;
  deepseek-v4-pro)          RUNNER=run_served_harness.py; NEED_URL=DEEPSEEK_BASE_URL ;;
  glm-5.3)                  RUNNER=run_openhands_sdk.py;  NEED_URL=GLM_BASE_URL ;;
  *) die "unknown agent '$AGENT' (opus-4.8|opus-5|opus-5.5|fable-5|sol-5.6|astra-6|kimi-k3|deepseek-v4-pro|glm-5.3)" ;;
esac
if [ -n "${NEED_URL:-}" ] && [ -z "${!NEED_URL:-}" ]; then
  die "set $NEED_URL to the OpenAI-compatible endpoint serving $AGENT"
fi
[[ "$ID" =~ ^[0-9]+$ ]] || die "paper_id must be a number, got '${POS[1]}'"
TASK_DIR="$REPO_ROOT/tasks/paper$ID"
[ -f "$TASK_DIR/task_spec.md" ] || die "no task spec at $TASK_DIR/task_spec.md"

# ---- fresh workspace: copy the task materials in -------------------------------------------
mkdir -p "$WS_ROOT"
WS_ROOT="$(cd "$WS_ROOT" && pwd)"
WS="$WS_ROOT/$AGENT/paper$ID"
if [ -d "$WS" ] && [ -n "$(ls -A "$WS")" ]; then
  [ "$RESUME" -eq 1 ] || die "workspace $WS already exists and is not empty (pass --resume to relaunch in it)"
  echo "[run.sh] relaunching in existing workspace $WS"
else
  mkdir -p "$WS"
  cp -R "$TASK_DIR"/. "$WS"/
  echo "[run.sh] created workspace $WS from $TASK_DIR"
fi

# ---- per-agent harness configuration --------------------------------------------------------
PYTHON="${PYTHON:-python3}"
export CLAUDE_MAX_RESUMES=5 CODEX_MAX_RESUMES=5 SOLVER_SERVED_MAX_RESUMES=5

case "$AGENT" in
  opus-4.8|opus-5|opus-5.5|fable-5)
    case "$AGENT" in
      opus-4.8) DEFAULT_MODEL=claude-opus-4-8 ;;
      opus-5)   DEFAULT_MODEL=claude-opus-5 ;;
      opus-5.5) DEFAULT_MODEL=claude-opus-5-5 ;;
      fable-5)  DEFAULT_MODEL=claude-fable-5 ;;
    esac
    export CLAUDE_MODEL="${CLAUDE_MODEL:-$DEFAULT_MODEL}" CLAUDE_EFFORT=max   # effort pinned as in our runs
    # An API key / gateway token switches the runner from subscription OAuth to that credential.
    if [ -n "${ANTHROPIC_API_KEY:-}${ANTHROPIC_AUTH_TOKEN:-}" ]; then
      export SOLVER_ANTHROPIC_BYOK=1
    fi
    ;;

  sol-5.6|astra-6)
    case "$AGENT" in
      sol-5.6) DEFAULT_MODEL=gpt-5.6-sol ;;
      astra-6) DEFAULT_MODEL=gpt-6-astra ;;
    esac
    export CODEX_MODEL="${CODEX_MODEL:-$DEFAULT_MODEL}" CODEX_EFFORT=max   # effort pinned as in our runs
    # An API key / gateway switches the runner from the ChatGPT login to a Responses endpoint.
    if [ -n "${CODEX_GATEWAY_BASE_URL:-}${OPENAI_API_KEY:-}" ]; then
      export CODEX_GATEWAY_BASE_URL="${CODEX_GATEWAY_BASE_URL:-${OPENAI_BASE_URL:-https://api.openai.com/v1}}"
      export CODEX_GATEWAY_API_KEY="${CODEX_GATEWAY_API_KEY:-${OPENAI_API_KEY:-}}"
    fi
    ;;

  kimi-k3)
    KIMI_MODEL="${KIMI_MODEL:-moonshotai/kimi-k3}"
    KIMI_BIN="${KIMI_BIN:-$(command -v kimi || echo "$HOME/.kimi-code/bin/kimi")}"
    # kimi-code reads its endpoint from ~/.kimi-code/config.toml, so each run gets its own
    # HOME (next to, not inside, the workspace) holding that config.
    KHOME="$WS_ROOT/$AGENT/.home-paper$ID"
    mkdir -p "$KHOME/.kimi-code"
    chmod 700 "$KHOME"
    case "$KIMI_BIN" in */.kimi-code/bin/kimi) ln -sfn "$(dirname "$KIMI_BIN")" "$KHOME/.kimi-code/bin" ;; esac
    ( umask 077; cat > "$KHOME/.kimi-code/config.toml" <<EOF
default_model = "kimik3"

[providers.kimik3]
base_url = "$KIMI_BASE_URL"
api_key = "${KIMI_API_KEY:-sk-local}"

[models.kimik3]
provider = "kimik3"
protocol = "openai"
id = "$KIMI_MODEL"
name = "$KIMI_MODEL"
max_context_size = 1048576
max_output_tokens = 131072
EOF
    )
    export TEXMFHOME="${TEXMFHOME:-$HOME/texmf}"   # keep the user's LaTeX tree visible
    export HOME="$KHOME"
    export SOLVER_HARNESS=kimi SOLVER_HARNESS_BIN="$KIMI_BIN" SOLVER_SERVED_MODEL=kimik3
    export SOLVER_SERVED_MODEL_ID="$KIMI_MODEL" SOLVER_SERVED_BASE_URL="$KIMI_BASE_URL"
    export SOLVER_SERVED_API_KEY="${KIMI_API_KEY:-sk-local}"
    ;;

  deepseek-v4-pro)
    DEEPSEEK_MODEL="${DEEPSEEK_MODEL:-deepseek-ai/DeepSeek-V4-Pro-0813}"
    export DEEPSEEK_BASE_URL DEEPSEEK_API_KEY="${DEEPSEEK_API_KEY:-sk-local}"
    export DSH_PERMISSION_MODE=danger-full-access DSH_HOME="$WS/.dsh"
    mkdir -p "$DSH_HOME"
    sed -e '/^#/d' -e "s|__DEEPSEEK_BASE_URL__|$DEEPSEEK_BASE_URL|g" -e "s|__DEEPSEEK_MODEL__|$DEEPSEEK_MODEL|g" \
      "$SOLVER_DIR/dsh_patch.yml" > "$DSH_HOME/patch.yml"
    export SOLVER_HARNESS=dsh SOLVER_HARNESS_BIN="${DSH_BIN:-$(command -v dsh || echo dsh)}"
    export SOLVER_DSH_PATCH="$DSH_HOME/patch.yml"
    export SOLVER_SERVED_MODEL="$DEEPSEEK_MODEL" SOLVER_SERVED_MODEL_ID="$DEEPSEEK_MODEL"
    export SOLVER_SERVED_BASE_URL="$DEEPSEEK_BASE_URL" SOLVER_SERVED_API_KEY="$DEEPSEEK_API_KEY"
    ;;

  glm-5.3)
    export SOLVER_SERVED_MODEL="${GLM_MODEL:-zai-org/GLM-5.3}"
    export SOLVER_SERVED_MODEL_ID="$SOLVER_SERVED_MODEL" SOLVER_SERVED_BASE_URL="$GLM_BASE_URL"
    export SOLVER_SERVED_API_KEY="${GLM_API_KEY:-sk-local}"
    ;;
esac

if [ "$CPU_ONLY" -eq 1 ]; then
  export CUDA_VISIBLE_DEVICES=""
fi

# ---- launch ---------------------------------------------------------------------------------
LOG="$WS_ROOT/$AGENT/paper${ID}_$(date +%Y%m%d_%H%M%S).log"
echo "[run.sh] agent=$AGENT paper=$ID runner=$RUNNER"
echo "[run.sh] workspace=$WS"
echo "[run.sh] log=$LOG"
set +e
"$PYTHON" "$SOLVER_DIR/$RUNNER" --workspace "$WS" --unlimited 2>&1 | tee -a "$LOG"
exit "${PIPESTATUS[0]}"
