#!/usr/bin/env bash
# Bootstrap a clean Docker-capable box (GitHub Codespace or a CPU VM) to run the
# full o11y-bench investigation category (11 tasks) with Phoebe on Theodosia,
# Kimi K2.6 (FSM) and the raw-tools base.
#
# Prereqs on the box: Docker daemon running, git, curl, Python 3.12+.
# Pass the two keys in the environment before running:
#   TOGETHER_API_KEY=...  ANTHROPIC_API_KEY=...  bash bench_bootstrap.sh
# On a 32GB+ box, set NCONCURRENT=8 for ~1.5-1.8x faster wall-clock.
# Run it detached so an ssh drop cannot kill it:
#   nohup env TOGETHER_API_KEY=... ANTHROPIC_API_KEY=... NCONCURRENT=8 \
#     bash bench_bootstrap.sh > ~/bench_run.log 2>&1 &
set -euo pipefail

WORK="${WORK:-$HOME/bench}"
NCONCURRENT="${NCONCURRENT:-2}"   # 2 for a 16GB box; 6-8 on a 32GB+ box
mkdir -p "$WORK" && cd "$WORK"

# 1) Tooling: uv + mise
command -v uv   >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
command -v mise >/dev/null || curl -fsSL https://mise.run | sh
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
eval "$(mise activate bash)" 2>/dev/null || true

# 2) Repos: the harness (public), phoebe (the agent), theodosia from PyPI
[ -d o11y-bench ] || git clone --depth 1 https://github.com/grafana/o11y-bench
[ -d phoebe ]     || git clone --depth 1 https://github.com/msradam/phoebe

cd o11y-bench
mise trust -y 2>/dev/null || true
mise install -y 2>/dev/null || true
uv sync
# make phoebe + theodosia importable by the harness venv
uv pip install -e ../phoebe theodosia

# 3) Sanity: docker + the agent import
docker info >/dev/null 2>&1 || { echo "FATAL: docker daemon not reachable"; exit 1; }
uv run --no-sync python -c "import phoebe.harbor.agent as a; print('phoebe agent import OK:', a.PhoebeAgent.name())"

# 4) The full investigation category (11 tasks), Pass^3
TASKS=(cache-incident-blast-radius cache-refresh-lag-handoff cache-rollout-trigger-check \
       dependency-outage-false-lead deployment-blast-radius-check incident-triage \
       payment-error-blast-radius payments-path-root-cause retry-backlog-incident \
       service-degradation-rca slow-path-hotspot-correlation)
TASK_FLAGS=(); for t in "${TASKS[@]}"; do TASK_FLAGS+=(--task-name "$t"); done

echo "================ KIMI FSM (Phoebe + Theodosia) full investigation $(date) ================"
OPENAI_API_BASE=https://api.together.xyz/v1 OPENAI_API_KEY="$TOGETHER_API_KEY" \
mise run bench:job -- --model openai/moonshotai/Kimi-K2.6 \
  --agent-import-path phoebe.harbor:PhoebeAgent --reasoning-effort off \
  --n-attempts 3 --n-concurrent "$NCONCURRENT" --job-name kimi-inv-full-fsm "${TASK_FLAGS[@]}"

echo "================ KIMI base (raw tools) full investigation $(date) ================"
OPENAI_API_BASE=https://api.together.xyz/v1 OPENAI_API_KEY="$TOGETHER_API_KEY" \
mise run bench:job -- --model openai/moonshotai/Kimi-K2.6 \
  --agent-import-path agents.o11y_agent:O11yBenchAgent --reasoning-effort off \
  --n-attempts 3 --n-concurrent "$NCONCURRENT" --job-name kimi-inv-full-base "${TASK_FLAGS[@]}"

echo "================ DONE $(date) ================"
# Headline mean per arm. Pass^3 (the leaderboard metric) is computed separately
# from the per-trial rewards under jobs/<name>/*/verifier/reward.txt.
python3 - <<'PY'
import json, glob, os, collections
for j in ("kimi-inv-full-fsm", "kimi-inv-full-base"):
    try:
        d = json.load(open(f"jobs/{j}/result.json"))
        v = list(d["stats"]["evals"].values())[0]
        per = collections.defaultdict(list)
        for tr in glob.glob(f"jobs/{j}/*/verifier/reward.txt"):
            task = os.path.basename(os.path.dirname(os.path.dirname(tr))).rsplit("__", 1)[0]
            per[task].append(float(open(tr).read().strip()))
        p3 = sum(1 for r in per.values() if len(r) == 3 and all(x == 1.0 for x in r))
        print(f"{j}: mean {round(v['metrics'][0]['mean'],4)}  Pass^3 {p3}/{len(per)}  ({v['n_trials']} trials, {v['n_errors']} errors)")
    except Exception as e:
        print(j, "no result:", e)
PY
