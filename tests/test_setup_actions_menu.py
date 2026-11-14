import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "tools/setup_runtime.sh"


def choose(tmp_path, *, stdin="", **values):
    env = {**os.environ, "ACTION": "", "MODE": "setup", "ST_METHOD": "compose-image",
           "ST_ROLE": "bot", "ST_REF": "v1.0.0", "DETECTED_STATE": "installed", "DETECTED_CURRENT": "0",
           "UNATTENDED": "0", "DRY_RUN": "0", "SHIP_PACK": "", "CONFIG_SOURCE": "", "HELPER": "no"}
    env.update(values)
    script = ('set -euo pipefail\n'
              'source "$1"\n'
              'warn() { printf "WARNING: %s\\n" "$*" >&2; }\n'
              'die() { printf "ERROR: %s\\n" "$*" >&2; exit 1; }\n'
              'confirm() { printf "unexpected confirmation\\n" >&2; exit 9; }\n'
              'select_action\n'
              'printf "SELECTED=%s MODE=%s\\n" "$ACTION" "$MODE"\n')
    before = set(tmp_path.iterdir())
    result = subprocess.run(["bash", "-c", script, "setup-actions", str(RUNTIME)], env=env, check=False,
                            cwd=tmp_path, input=stdin, text=True, capture_output=True, timeout=5)
    assert set(tmp_path.iterdir()) == before
    return result


@pytest.mark.parametrize("answer,action", [("1", "keep"), ("2", "upgrade"), ("3", "migrate"),
                                           ("4", "repair"), ("5", "reinstall"), ("keep", "keep"),
                                           ("upgrade", "upgrade"), ("migrate", "migrate"),
                                           ("repair", "repair"), ("reinstall", "reinstall")])
def test_each_existing_install_action_can_be_selected_without_confirmation_or_writes(tmp_path, answer, action):
    result = choose(tmp_path, stdin=answer + "\n")
    assert result.returncode == 0, result.stderr
    mode = "upgrade" if action in {"upgrade", "repair", "reinstall"} else "setup"
    assert f"SELECTED={action} MODE={mode}" in result.stdout
    assert "1) Keep unchanged" in result.stdout and "5) Reinstall" in result.stdout


@pytest.mark.parametrize("stdin", ["", "\n"])
def test_eof_or_empty_answer_keeps_the_install_unchanged(tmp_path, stdin):
    result = choose(tmp_path, stdin=stdin)
    assert result.returncode == 0 and "SELECTED=keep MODE=setup" in result.stdout
    assert "ERROR" not in result.stderr


def test_invalid_choice_can_be_corrected_without_mutation(tmp_path):
    result = choose(tmp_path, stdin="invalid\n2\n")
    assert result.returncode == 0 and "SELECTED=upgrade MODE=upgrade" in result.stdout
    assert result.stderr.count("Choose 1-5") == 2
    assert "WARNING: choose keep" in result.stderr


@pytest.mark.parametrize("values,action", [
    ({"ST_METHOD": "", "DETECTED_STATE": "not-installed"}, "install"),
    ({"ST_METHOD": "", "DETECTED_STATE": ""}, "install"),
    ({"MODE": "upgrade"}, "upgrade"),
    ({"ACTION": "repair"}, "repair"),
    ({"ACTION": "keep", "MODE": "upgrade"}, "keep"),
    ({"UNATTENDED": "1", "DETECTED_CURRENT": "1"}, "keep"),
    ({"UNATTENDED": "1"}, "upgrade"),
    ({"UNATTENDED": "1", "DETECTED_STATE": "partial", "ST_METHOD": ""}, "repair"),
    ({"DRY_RUN": "1", "DETECTED_CURRENT": "1"}, "keep"),
    ({"DRY_RUN": "1"}, "upgrade"),
    ({"DRY_RUN": "1", "DETECTED_STATE": "partial"}, "repair"),
    ({"UNATTENDED": "1", "DETECTED_CURRENT": "1", "SHIP_PACK": "archive"}, "upgrade"),
    ({"UNATTENDED": "1", "DETECTED_CURRENT": "1", "CONFIG_SOURCE": "config-dir"}, "upgrade"),
    ({"UNATTENDED": "1", "DETECTED_CURRENT": "1", "HELPER": "yes"}, "upgrade"),
])
def test_explicit_fresh_and_noninteractive_choices_need_no_menu(tmp_path, values, action):
    result = choose(tmp_path, **values)
    assert result.returncode == 0, result.stderr
    assert f"SELECTED={action} " in result.stdout
    assert "Actions:" not in result.stdout


@pytest.mark.parametrize("action", ["upgrade", "migrate", "repair", "reinstall"])
def test_existing_only_actions_refuse_a_fresh_install(tmp_path, action):
    result = choose(tmp_path, ACTION=action, ST_METHOD="", DETECTED_STATE="not-installed")
    assert result.returncode == 1
    assert f"{action} needs an existing install; nothing changed" in result.stderr


@pytest.mark.parametrize("action,message", [("install", "install needs a new install"),
                                           ("unknown", "unknown setup action")])
def test_invalid_actions_fail_closed(tmp_path, action, message):
    result = choose(tmp_path, ACTION=action)
    assert result.returncode == 1 and message in result.stderr


def test_keep_can_be_explicitly_selected_on_a_fresh_install(tmp_path):
    result = choose(tmp_path, ACTION="keep", ST_METHOD="", DETECTED_STATE="not-installed")
    assert result.returncode == 0 and "SELECTED=keep " in result.stdout
