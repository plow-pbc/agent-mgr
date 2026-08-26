import os
import subprocess
from pathlib import Path

from conftest import spawn

ROOT = Path(__file__).resolve().parent.parent
MODEL_PROVIDER = ROOT / "lib" / "model-provider"


def provider(tmp_path, text):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(text)
    return subprocess.run([str(MODEL_PROVIDER), str(cfg)], capture_output=True, text=True)


def test_it_reads_the_provider_from_the_model_block(tmp_path):
    r = provider(tmp_path, "model:\n  default: gpt-5.5\n  provider: openai-codex\n")
    assert r.returncode == 0
    assert r.stdout.strip() == "openai-codex"


def test_a_provider_under_another_block_is_not_mistaken_for_the_model_one(tmp_path):
    """Anchored to model:, not to a two-space indent -- file order must not decide."""
    r = provider(tmp_path, "search:\n  provider: brave\nmodel:\n  provider: openai-codex\n")
    assert r.stdout.strip() == "openai-codex"


def test_a_trailing_inline_comment_is_not_part_of_the_provider_name(tmp_path):
    r = provider(tmp_path, "model:\n  provider: openai-codex  # the device flow one\n")
    assert r.stdout.strip() == "openai-codex"


def test_a_config_with_no_model_provider_fails_rather_than_printing_nothing(tmp_path):
    r = provider(tmp_path, "model:\n  default: gpt-5.5\n")
    assert r.returncode != 0
    assert "model.provider" in r.stderr


def test_a_missing_config_names_the_path_it_could_not_read(tmp_path):
    r = subprocess.run([str(MODEL_PROVIDER), str(tmp_path / "nope.yaml")],
                       capture_output=True, text=True)
    assert r.returncode != 0
    assert "nope.yaml" in r.stderr


def test_reload_refuses_to_run_outside_agent_mgr(tmp_path):
    """It sources common.sh from AGENT_MGR_ROOT; without it the failure must be
    a clear message rather than an unbound-variable trace."""
    # Through conftest's spawn, not subprocess directly: this shells the very
    # script whose reload restarted production, and the seam is what proves the
    # PATH it is handed cannot reach the operator's docker.
    r = spawn([str(ROOT / "lib" / "reload-if-running"), "rowan"],
              {"PATH": os.environ["PATH"]})
    assert r.returncode != 0
    assert "run me through agent-mgr" in r.stderr
