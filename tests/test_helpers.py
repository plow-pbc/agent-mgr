import sys

import pytest
from conftest import ROOT

sys.path.insert(0, str(ROOT))

from agent_mgr.commands import model_provider
from agent_mgr.errors import AgentMgrError


def provider(tmp_path, text):
    config = tmp_path / "config.yaml"
    config.write_text(text)
    return model_provider(config)


def test_it_reads_the_provider_from_the_model_block(tmp_path):
    assert (
        provider(tmp_path, "model:\n  default: gpt-5.5\n  provider: openai-codex\n")
        == "openai-codex"
    )


def test_a_provider_under_another_block_is_not_mistaken_for_the_model_one(tmp_path):
    assert (
        provider(tmp_path, "search:\n  provider: brave\nmodel:\n  provider: openai-codex\n")
        == "openai-codex"
    )


def test_a_trailing_inline_comment_is_not_part_of_the_provider_name(tmp_path):
    assert provider(tmp_path, "model:\n  provider: openai-codex  # device flow\n") == "openai-codex"


def test_a_config_with_no_model_provider_fails(tmp_path):
    with pytest.raises(AgentMgrError, match="model.provider"):
        provider(tmp_path, "model:\n  default: gpt-5.5\n")
