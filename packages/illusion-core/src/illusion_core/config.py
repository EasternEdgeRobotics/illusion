"""Config loading shared by every illusion service.

Each service declares the fields it actually needs. Missing ones are reported
together, by name, at startup -- rather than one per run, or as a confusing
failure at first use (an empty Discord token surfaces as a login error, an
empty font path as a PIL traceback halfway through a print job).
"""

from pathlib import Path

import yaml


class ConfigError(Exception):
    """The config file is missing, unreadable, or missing required values."""


_MISSING = object()


def get(config, dotted, default=None):
    """Read a dotted path like 'illusion.discord.token', or default."""
    node = config

    for key in dotted.split("."):
        if not isinstance(node, dict) or key not in node:
            return default

        node = node[key]

    return node


def _is_empty(config, dotted):
    value = get(config, dotted, _MISSING)

    return value is _MISSING or value is None or value == ""


def require(config, dotted_paths, source="config"):
    """Raise naming every required field that is absent or blank."""
    missing = [dotted for dotted in dotted_paths if _is_empty(config, dotted)]

    if missing:
        raise ConfigError(
            f"{source} is missing required values:\n"
            + "\n".join(f"  {dotted}" for dotted in missing)
        )


def load(path, required=()):
    path = Path(path)

    if not path.is_file():
        raise ConfigError(
            f"{path} not found. Copy config.example.yaml to {path.name} and fill it in."
        )

    try:
        with path.open("r") as file:
            config = yaml.safe_load(file) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"{path} is not valid YAML: {e}") from e

    require(config, required, source=str(path))

    return config
