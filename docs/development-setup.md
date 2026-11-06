# Kronos research development setup

## Environment

Use Python **3.11** (supported by the project's Python 3.10+ requirement). Isolate the virtual environment:

```bash
python -m venv .venv
# Linux/macOS:
source .venv/bin/activate
# Windows PowerShell:
# .venv\\Scripts\\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt -r requirements-dev.txt
python -m pytest -q tests/test_project_smoke.py
```

### Important reproducibility notes

The upstream `requirements.txt` is **not a full lock file**: some dependencies (notably PyTorch) are unconstrained or have only lower bounds. The development tools are version pinned. Before relying on benchmark results, record the exact installed environment (`python -m pip freeze > environment-freeze.txt`) alongside dataset checksums, model revisions, OS and hardware. Do not assume identical results across different PyTorch/CPU/GPU versions.

## Automated checks

The GitHub Action runs on pushes and pull requests. Fast tests never fetch pretrained model weights. A manual **Run workflow** with **full_model_regression** enabled runs the existing upstream numerical regression suite. Those tests require network access to Hugging Face and can consume substantial CPU time and memory.

The dependency audit is enforced in CI. If upstream dependencies have a security finding, investigate and resolve or explicitly document a scoped exception before merging; do not silently ignore vulnerabilities.

## Safety

Research and paper trading only. No real-money broker integration or production trade execution is part of this phase. Never commit tokens, broker API keys, passwords, account identifiers, model checkpoint artifacts, or raw private account data. Local secrets belong in environment variables (or a secret manager), not in GitHub commits.

## Next milestones

Validated OHLCV ingestion, reproducible walk-forward backtesting, baseline comparison, risk controls, and an auditable paper-trading engine. Live trading stays disabled pending independent risk and performance review.
