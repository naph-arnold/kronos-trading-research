"""Fast, offline checks for the initial research foundation."""

from pathlib import Path
import ast
import tomllib

ROOT = Path(__file__).resolve().parents[1]


def test_project_has_no_live_broker_executor():
    """Phase 1 must not accidentally ship a live trading entrypoint."""
    assert not (ROOT / "trading" / "live_execution.py").exists()


def test_kronos_inference_modules_exist():
    model_init = ROOT / "model" / "__init__.py"
    assert model_init.is_file()
    ast.parse(model_init.read_text(encoding="utf-8"))


def test_workflow_is_present():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "fast-checks:" in workflow
    assert "workflow_dispatch:" in workflow
    assert "full_model_regression" in workflow


def test_project_settings_are_valid_toml():
    settings = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert settings["tool"]["ruff"]["target-version"] == "py310"
