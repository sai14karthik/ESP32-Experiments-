"""Load serial port config."""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIGS = ROOT / "configs"


def load_ports() -> dict:
    base = CONFIGS / "ports.toml"
    with base.open("rb") as f:
        cfg = tomllib.load(f)
    local = CONFIGS / "ports.local.toml"
    if local.exists():
        with local.open("rb") as f:
            override = tomllib.load(f)
        for k, v in override.items():
            if isinstance(v, dict) and k in cfg and isinstance(cfg[k], dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    return cfg


def resolve_device(role: str = "rx") -> str:
    cfg = load_ports()
    devices = cfg.get("devices", {})
    roles = cfg.get("roles", {})
    key = roles.get(role, role)
    if key in devices:
        return devices[key]
    if role in devices:
        return devices[role]
    raise KeyError(f"No port for role={role!r} (devices={devices}, roles={roles})")
