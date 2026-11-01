"""The provider-switch scripts: the .env editor and the settings they apply.

scripts/vision.sh rewrites the secrets file, so its helper is tested for the
properties that matter there: every other line survives untouched, nothing it
did not set is ever printed, and the result is private. The settings the
script applies are checked against the application's own validation, so the
script and the config cannot drift apart.

Nothing here runs Docker or touches a real .env.
"""

from __future__ import annotations

import importlib.util
import re
import stat
import subprocess
from pathlib import Path

import pytest

from hermes_home.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "vision.sh"
SECRET = "sk-ant-SECRET-SENTINEL-0123456789"


def _helper():
    spec = importlib.util.spec_from_file_location(
        "set_env_keys", REPO_ROOT / "scripts" / "set_env_keys.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


ENV_TEXT = f"""# hermes-home
HOME_ASSISTANT_TOKEN="{SECRET}"
export WEBHOOK_SECRET={SECRET}
VISION_PROVIDER=anthropic
VISION_MODEL=claude-sonnet-5
ANTHROPIC_API_KEY={SECRET}
VISION_PROVIDER=mock
LAN_BIND_IP=
"""


def test_keys_are_replaced_in_place_and_others_kept_verbatim() -> None:
    text, _report = _helper().set_keys(
        ENV_TEXT, {"VISION_PROVIDER": "ollama", "VISION_MODEL": "gemma4:31b"}
    )
    lines = text.splitlines()
    assert lines[0] == "# hermes-home"
    assert lines[1] == f'HOME_ASSISTANT_TOKEN="{SECRET}"'
    assert lines[2] == f"export WEBHOOK_SECRET={SECRET}"
    assert lines[3] == "VISION_PROVIDER=ollama"
    assert lines[4] == "VISION_MODEL=gemma4:31b"
    assert lines[5] == f"ANTHROPIC_API_KEY={SECRET}"
    # The later duplicate would have overridden the first; it is removed.
    assert text.count("VISION_PROVIDER=") == 1
    assert "LAN_BIND_IP=" in lines


def test_missing_keys_are_appended() -> None:
    text, report = _helper().set_keys(ENV_TEXT, {"OLLAMA_THINK": "false"})
    assert text.rstrip().endswith("OLLAMA_THINK=false")
    assert report == ["OLLAMA_THINK=false (added)"]


def test_the_report_never_contains_values_it_did_not_set() -> None:
    _text, report = _helper().set_keys(ENV_TEXT, {"VISION_PROVIDER": "ollama"})
    assert SECRET not in "\n".join(report)


def test_bad_arguments_are_refused() -> None:
    helper = _helper()
    for bad in (["NOEQUALS"], ["1BAD=x"], ["KEY=line\nbreak"]):
        with pytest.raises(ValueError):
            helper.parse_changes(bad)


def test_cli_writes_a_private_file_and_prints_no_secrets(tmp_path: Path) -> None:
    source = tmp_path / ".env"
    source.write_text(ENV_TEXT)
    dest = tmp_path / "env.next"
    result = subprocess.run(
        [
            "python3",
            "-I",
            str(REPO_ROOT / "scripts" / "set_env_keys.py"),
            str(source),
            str(dest),
            "VISION_PROVIDER=ollama",
            "VISION_MODEL=gemma4:31b",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert SECRET not in result.stdout + result.stderr
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600
    assert source.read_text() == ENV_TEXT, "the source is never modified"
    assert f"ANTHROPIC_API_KEY={SECRET}" in dest.read_text()


# --------------------------------------------------------------------------- #
# vision.sh
# --------------------------------------------------------------------------- #


def test_vision_script_parses() -> None:
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


def test_vision_script_without_a_command_prints_usage() -> None:
    result = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 2
    for command in ("status", "local", "anthropic", "--check"):
        assert command in result.stdout


def _script_settings(name: str) -> dict[str, str]:
    text = SCRIPT.read_text()
    block = re.search(rf"^{name}=\((.*?)^\)", text, re.S | re.M)
    assert block, name
    values = dict(re.findall(r'"([A-Z_]+)=([^"]*)"', block.group(1)))
    values["VISION_MODEL"] = values["VISION_MODEL"].replace("$LOCAL_MODEL", "gemma4:31b")
    values["VISION_MODEL"] = values["VISION_MODEL"].replace("$ANTHROPIC_MODEL", "claude-sonnet-5")
    return values


def _settings(values: dict[str, str], **extra) -> Settings:
    return Settings(
        _env_file=None,
        webhook_secret="s",
        home_assistant_token="t",
        **{k.lower(): v for k, v in values.items()},
        **extra,
    )


def test_local_switch_settings_pass_the_application_s_own_checks() -> None:
    settings = _settings(_script_settings("LOCAL_SETTINGS"))
    settings.require_for_runtime()
    assert settings.vision_provider == "ollama"
    assert settings.ingest_lease_seconds > settings.analysis_budget_seconds


def test_anthropic_switch_settings_restore_the_original_timing() -> None:
    values = _script_settings("ANTHROPIC_SETTINGS")
    settings = _settings(values, anthropic_api_key="k")
    settings.require_for_runtime()
    defaults = Settings(_env_file=None)
    # Exactly what Anthropic ran with before local vision existed.
    assert settings.vision_timeout_seconds == defaults.vision_timeout_seconds == 30
    assert settings.ingest_lease_seconds == defaults.ingest_lease_seconds == 120
    assert settings.ingest_lease_seconds > settings.analysis_budget_seconds
