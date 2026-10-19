"""Read and write config.yaml from the dashboard.

Two things matter here beyond plumbing:

* **Comments survive.** config.yaml carries ~70 lines documenting confirmed
  portal behaviour (which selectors are decoys, why a wait is the length it
  is). A PyYAML dump would silently delete all of it, so saves go through
  ruamel's round-trip loader.
* **Secrets never leave the process.** The dashboard has no authentication —
  it is a localhost tool — so passwords, tokens and API keys are sent to the
  browser as a masked placeholder. A field posted back unchanged (still the
  placeholder) is left exactly as it was.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from loguru import logger
from ruamel.yaml import YAML

# Any leaf whose dotted path contains one of these is a secret.
SECRET_HINTS = ("password", "token", "api_key", "imap_pass", "secret", "chat_id")

# Shown in place of a secret. Posting this back means "leave it alone".
MASK = "••••••••"

# Editing these while the bot is mid-run cannot take effect until the process
# restarts, so the UI marks them rather than pretending otherwise.
RESTART_HINTS = ("dashboard.host", "dashboard.port", "dashboard.enabled")

_yaml = YAML()
_yaml.preserve_quotes = True
_yaml.width = 4096          # never re-wrap a long line into something invalid
_yaml.indent(mapping=2, sequence=4, offset=2)


def is_secret(path: str) -> bool:
    low = path.lower()
    return any(hint in low for hint in SECRET_HINTS)


def _is_mask(raw: Any) -> bool:
    """Is this value entirely the placeholder, i.e. 'leave the secret alone'?

    Covers the three shapes the page can post one back in: the bare string, a
    list of them, and a textarea's worth of them separated by newlines.
    """
    if isinstance(raw, (list, tuple)):
        items = [str(v).strip() for v in raw]
    else:
        items = [line.strip() for line in str(raw).splitlines()]
    items = [i for i in items if i]
    return bool(items) and all(i == MASK for i in items)


def flatten(node: Any, prefix: str = "") -> list[dict[str, Any]]:
    """Turn the config tree into an ordered list of editable leaves.

    Lists of scalars stay whole (edited as one multi-line field) because every
    sweepable option -- locations, visa_type, detection phrases -- is one.
    """
    out: list[dict[str, Any]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(value, dict):
                out.extend(flatten(value, path))
            else:
                out.append(_leaf(path, value))
    return out


def _leaf(path: str, value: Any) -> dict[str, Any]:
    secret = is_secret(path)
    if isinstance(value, bool):
        kind = "bool"
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        kind = "number"
    elif isinstance(value, (list, tuple)):
        kind = "list"
    else:
        kind = "text"

    if secret and kind == "list":
        # A masked LIST must still be a list. chat_id is both a secret and,
        # since it started accepting several ids, a list -- so it arrived at
        # the page as kind "list" holding the mask STRING, and the page's
        # value.join() threw. One bad field took the whole Settings view down
        # with it: blank until something was typed in the filter, because a
        # search that excluded this field rendered fine.
        shown: Any = [MASK] * len(value) if value else []
    elif secret:
        shown = MASK if (value not in (None, "")) else ""
    elif kind == "list":
        shown = [str(v) for v in value]
    else:
        shown = value

    return {
        "path": path,
        "kind": kind,
        "value": shown,
        "secret": secret,
        "restart": path in RESTART_HINTS,
    }


def describe(config: dict[str, Any]) -> dict[str, Any]:
    """The whole config, grouped by top-level section, secrets masked.

    Top-level scalars (poll_interval, min_days_ahead, headless, ...) are
    collected into one "general" group instead of each getting its own card
    with a single field in it.
    """
    sections: list[dict[str, Any]] = []
    general: list[dict[str, Any]] = []
    for key, value in config.items():
        if isinstance(value, dict):
            sections.append({"name": key, "fields": flatten(value, key)})
        else:
            general.append(_leaf(key, value))
    if general:
        sections.insert(0, {"name": "general", "fields": general})
    return {"sections": sections, "mask": MASK}


def _coerce(old: Any, new: Any) -> Any:
    """Keep a leaf's original YAML type; the browser sends everything as text."""
    if isinstance(old, bool):
        if isinstance(new, bool):
            return new
        return str(new).strip().lower() in ("1", "true", "yes", "on")
    if isinstance(old, int) and not isinstance(old, bool):
        return int(float(str(new).strip()))
    if isinstance(old, float):
        return float(str(new).strip())
    if isinstance(old, (list, tuple)):
        if isinstance(new, (list, tuple)):
            items = [str(v).strip() for v in new]
        else:
            items = [part.strip() for part in str(new).splitlines()]
        return [i for i in items if i]
    if new is None:
        return ""
    return str(new)


def apply_changes(
    config: dict[str, Any], changes: dict[str, Any]
) -> tuple[list[str], list[str]]:
    """Write edits into the live config dict. Returns (applied, rejected).

    Mutates in place rather than rebuilding, because the Monitor, Auth and
    Notifier all hold a reference to this same dict — replacing it would leave
    them pointed at the old one.
    """
    applied: list[str] = []
    rejected: list[str] = []

    for path, raw in changes.items():
        parts = str(path).split(".")
        node: Any = config
        try:
            for part in parts[:-1]:
                node = node[part]
            leaf = parts[-1]
            if not isinstance(node, dict) or leaf not in node:
                rejected.append(f"{path} (no such setting)")
                continue
            # An untouched secret comes back as the mask; never store that.
            # A masked LIST comes back as lines of masks, or as one multi-line
            # string of them, and str() on either never equals MASK -- so the
            # mask itself would have been written over the real chat ids.
            if is_secret(path) and _is_mask(raw):
                continue
            node[leaf] = _coerce(node[leaf], raw)
            applied.append(path)
        except Exception as exc:
            rejected.append(f"{path} ({exc})")

    return applied, rejected


def save(config: dict[str, Any], path: Path) -> None:
    """Persist the edited config, keeping comments and the previous file.

    Writes to a temp file next to the target and replaces atomically, so an
    interrupted save cannot leave the bot with a half-written config.
    """
    path = Path(path)
    with path.open("r", encoding="utf-8") as handle:
        document = _yaml.load(handle)

    _merge(document, config)

    backup = path.with_suffix(path.suffix + ".bak")
    try:
        shutil.copy2(path, backup)
    except Exception as exc:
        logger.warning(f"config: could not write {backup.name}: {exc}")

    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        _yaml.dump(document, handle)
    os.replace(tmp, path)
    logger.info(f"config: saved {path} (backup at {backup.name})")


def _merge(document: Any, source: dict[str, Any]) -> None:
    """Copy values from the plain dict into the comment-carrying document."""
    for key, value in source.items():
        if key not in document:
            document[key] = value
            continue
        if isinstance(value, dict) and isinstance(document[key], dict):
            _merge(document[key], value)
        elif isinstance(value, (list, tuple)):
            _merge_list(document, key, list(value))
        else:
            document[key] = value


def _merge_list(document: Any, key: str, new: list[Any]) -> None:
    """Update a YAML sequence without losing the comment block after it.

    ruamel attaches a free-standing comment block to the LAST ITEM of the
    sequence that precedes it, not to the key it visually introduces. So
    rewriting bls.locations deleted the 23-line block documenting visa_type,
    and rewriting available_phrases deleted the one above payment_phrases --
    30 comment lines gone on the first dashboard save.

    Two defences: never touch a list that did not change, and when one does,
    carry its trailing comment over to the new last item.
    """
    target = document[key]
    if not isinstance(target, list):
        document[key] = new
        return

    if [str(x) for x in target] == [str(x) for x in new]:
        return  # unchanged — leave the node, and its comments, alone

    trailing = None
    ca_items = getattr(getattr(target, "ca", None), "items", None)
    if ca_items and target:
        trailing = ca_items.get(len(target) - 1)

    target[:] = new

    if trailing is not None and target:
        try:
            target.ca.items[len(target) - 1] = trailing
        except Exception as exc:  # comment rescue must never break the save
            logger.debug(f"config: could not re-attach comment on {key}: {exc}")
