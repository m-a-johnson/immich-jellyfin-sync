"""Load and validate config.yaml. Settings only; UI choices live in the state DB."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

CONFIG_PATH = Path(os.environ.get("IJS_CONFIG", "/config/config.yaml"))


@dataclass(frozen=True)
class Service:
    url: str
    api_key: str


@dataclass(frozen=True)
class Paths:
    immich_prefix: str
    jellyfin_prefix: str
    output: Path


@dataclass(frozen=True)
class Config:
    immich: Service
    jellyfin: Service | None
    paths: Paths
    sync_interval_minutes: int


class ConfigError(Exception):
    pass


def _read_key(path: str, name: str) -> str:
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"{name} api_key_file not found: {p}")
    key = p.read_text().strip()
    if not key:
        raise ConfigError(f"{name} api_key_file is empty: {p}")
    return key


def _service(raw: dict | None, name: str, required: bool) -> Service | None:
    if not raw:
        if required:
            raise ConfigError(f"missing '{name}' section")
        return None
    for field in ("url", "api_key_file"):
        if not raw.get(field):
            raise ConfigError(f"{name}.{field} is required")
    return Service(url=raw["url"].rstrip("/"), api_key=_read_key(raw["api_key_file"], name))


def load(path: Path = CONFIG_PATH) -> Config:
    if not path.is_file():
        raise ConfigError(f"config not found: {path} (copy config.example.yaml)")
    raw = yaml.safe_load(path.read_text()) or {}

    p = raw.get("paths") or {}
    for field in ("immich_prefix", "jellyfin_prefix", "output"):
        if not p.get(field):
            raise ConfigError(f"paths.{field} is required")
    output = Path(p["output"])
    if not output.is_dir():
        raise ConfigError(f"paths.output is not a directory: {output}")

    interval = int(raw.get("sync_interval_minutes", 30))
    if interval < 1:
        raise ConfigError("sync_interval_minutes must be >= 1")

    return Config(
        immich=_service(raw.get("immich"), "immich", required=True),
        jellyfin=_service(raw.get("jellyfin"), "jellyfin", required=False),
        paths=Paths(
            immich_prefix=p["immich_prefix"].rstrip("/"),
            jellyfin_prefix=p["jellyfin_prefix"].rstrip("/"),
            output=output,
        ),
        sync_interval_minutes=interval,
    )
