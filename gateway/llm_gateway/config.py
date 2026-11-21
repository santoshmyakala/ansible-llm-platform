"""Gateway configuration: a YAML file rendered by Ansible, validated at start-up."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import yaml

_SHA256 = re.compile(r"[0-9a-f]{64}")
_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,62}")


class ConfigError(ValueError):
    """The configuration file is invalid; the message says which field and why."""


@dataclass(frozen=True)
class Backend:
    name: str
    url: str
    models: tuple[str, ...]
    api_key: str | None = None


@dataclass(frozen=True)
class Team:
    name: str
    key_sha256: str
    requests_per_minute: int
    tokens_per_minute: int
    models: tuple[str, ...] = ("*",)

    def may_use(self, model: str) -> bool:
        return "*" in self.models or model in self.models


@dataclass(frozen=True)
class Settings:
    backends: tuple[Backend, ...]
    teams: tuple[Team, ...]
    connect_timeout: float = 2.0
    read_timeout: float = 120.0
    health_interval: float = 5.0
    unhealthy_after: int = 2
    max_body_bytes: int = 1_048_576
    models: tuple[str, ...] = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "models", tuple(sorted({m for b in self.backends for m in b.models})))


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise ConfigError(msg)


def parse(raw: dict) -> Settings:
    _require(isinstance(raw, dict), "configuration must be a mapping")
    backends, teams = [], []
    for i, b in enumerate(raw.get("backends") or []):
        where = f"backends[{i}]"
        name, url, models = b.get("name", ""), b.get("url", ""), b.get("models") or []
        _require(bool(_NAME.fullmatch(name)), f"{where}.name {name!r} must be lowercase letters, digits, . _ -")
        parsed = urlparse(url)
        _require(parsed.scheme in ("http", "https") and bool(parsed.netloc), f"{where}.url {url!r} must be http(s)://host[:port]")
        _require(bool(models) and all(isinstance(m, str) and m for m in models), f"{where}.models must list at least one model")
        backends.append(Backend(name, url.rstrip("/"), tuple(models), b.get("api_key") or None))
    for i, t in enumerate(raw.get("teams") or []):
        where = f"teams[{i}]"
        name, digest = t.get("name", ""), str(t.get("key_sha256", "")).lower()
        _require(bool(_NAME.fullmatch(name)), f"{where}.name {name!r} must be lowercase letters, digits, . _ -")
        _require(bool(_SHA256.fullmatch(digest)), f"{where}.key_sha256 must be a hex SHA-256 digest, never the key itself")
        rpm, tpm = t.get("requests_per_minute"), t.get("tokens_per_minute")
        _require(isinstance(rpm, int) and rpm > 0, f"{where}.requests_per_minute must be a positive integer")
        _require(isinstance(tpm, int) and tpm > 0, f"{where}.tokens_per_minute must be a positive integer")
        teams.append(Team(name, digest, rpm, tpm, tuple(t.get("models") or ["*"])))
    _require(bool(backends), "at least one backend is required")
    _require(bool(teams), "at least one team is required")
    for kind, items in (("backend", backends), ("team", teams)):
        names = [x.name for x in items]
        _require(len(names) == len(set(names)), f"duplicate {kind} names: {sorted(n for n in set(names) if names.count(n) > 1)}")
    digests = [t.key_sha256 for t in teams]
    _require(len(digests) == len(set(digests)), "two teams share the same API key")
    opts = raw.get("options") or {}
    s = Settings(
        backends=tuple(backends),
        teams=tuple(teams),
        connect_timeout=float(opts.get("connect_timeout", 2.0)),
        read_timeout=float(opts.get("read_timeout", 120.0)),
        health_interval=float(opts.get("health_interval", 5.0)),
        unhealthy_after=int(opts.get("unhealthy_after", 2)),
        max_body_bytes=int(opts.get("max_body_bytes", 1_048_576)),
    )
    for t in s.teams:
        unknown = [m for m in t.models if m != "*" and m not in s.models]
        _require(not unknown, f"team {t.name} allows models no backend serves: {unknown}")
    return s


def load(path: str | Path) -> Settings:
    try:
        raw = yaml.safe_load(Path(path).read_text())
    except (OSError, yaml.YAMLError) as e:
        raise ConfigError(f"cannot read {path}: {e}") from e
    return parse(raw)
