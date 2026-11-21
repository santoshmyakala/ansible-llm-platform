import re

import pytest

from llm_gateway.config import ConfigError, parse

from .conftest import config


def test_valid_configuration_lists_every_model():
    s = parse(config())
    assert s.models == ("coder", "qwen-small")
    assert [b.name for b in s.backends] == ["node-a", "node-b"]


@pytest.mark.parametrize("mutate, message", [
    (lambda c: c["teams"][0].update(key_sha256="rk-plaintext-key"), "never the key itself"),
    (lambda c: c["teams"].append(dict(c["teams"][0])), "duplicate team names"),
    (lambda c: c["teams"][1].update(key_sha256=c["teams"][0]["key_sha256"]), "share the same API key"),
    (lambda c: c["backends"][0].update(url="node-a:8080"), "must be http(s)"),
    (lambda c: c["backends"][0].update(models=[]), "at least one model"),
    (lambda c: c["teams"][1].update(models=["missing-model"]), "no backend serves"),
    (lambda c: c["teams"][0].update(requests_per_minute=0), "positive integer"),
    (lambda c: c.update(backends=[]), "at least one backend"),
])
def test_invalid_configuration_is_rejected_with_a_reason(mutate, message):
    raw = config()
    mutate(raw)
    with pytest.raises(ConfigError, match=re.escape(message)):
        parse(raw)
