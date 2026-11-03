#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")/.."
export PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"
if [ -d "$PWD/.research-cache/node-v22.15.0-linux-x64/bin" ]; then
  export PATH="$PWD/.research-cache/node-v22.15.0-linux-x64/bin:$PATH"
fi
if [ -n "${AGENTLAB_PYTHON:-}" ]; then
  agentlab_python="$AGENTLAB_PYTHON"
elif [ -x "$PWD/.venv/bin/python" ]; then
  agentlab_python="$PWD/.venv/bin/python"
else
  agentlab_python=python3
fi
exec "$agentlab_python" -m agentlab_app.cli visual serve --profile profiles/basicagent-viz.yaml
