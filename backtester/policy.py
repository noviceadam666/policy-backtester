"""Policy files: load, validate strictly, precompile, hash.

A typo in a guardrail must fail loudly. An ignored ``unless_comand`` would silently change what a
rule does, so unknown fields are errors.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from .matchers import compile_glob

ACTIONS = ("block", "approve", "allow")
_RULE_FIELDS = frozenset({"id", "tool", "profile", "command", "unless_command", "path", "action", "message"})


class PolicyError(ValueError):
    pass


@dataclass(frozen=True)
class Rule:
    id: str
    tools: frozenset[str]
    action: str
    message: str
    profiles: frozenset[str] | None = None
    command: re.Pattern[str] | None = None
    unless_command: re.Pattern[str] | None = None
    globs: tuple[re.Pattern[str], ...] = ()

    def applies_to(self, tool: str, profile: str | None) -> bool:
        if tool not in self.tools and "*" not in self.tools:
            return False
        return self.profiles is None or profile in self.profiles


@dataclass(frozen=True)
class Policy:
    rules: tuple[Rule, ...]
    hash: str
    source: str = "<memory>"


def load_policy(path: Path) -> Policy:
    try:
        # ruamel.yaml, not PyYAML: it's the parser Hermes core ships, and it rejects duplicate keys.
        data = YAML(typ="safe", pure=True).load(Path(path).read_text())
    except OSError as exc:
        raise PolicyError(f"can't read policy {path}: {exc}") from exc
    except YAMLError as exc:
        raise PolicyError(f"{path} isn't valid YAML: {exc}") from exc
    return parse_policy(data, source=str(path))


def parse_policy(data: object, source: str = "<memory>") -> Policy:
    if not isinstance(data, dict) or not isinstance(data.get("rules"), list):
        raise PolicyError(f"{source}: a policy is a mapping with a 'rules' list")
    extra = set(data) - {"rules"}
    if extra:
        raise PolicyError(f"{source}: unknown top-level field(s): {', '.join(sorted(extra))}")
    rules = [_parse_rule(raw, source) for raw in data["rules"]]
    ids = [r.id for r in rules]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise PolicyError(f"{source}: duplicate rule id(s): {', '.join(duplicates)}")
    digest = hashlib.sha256(json.dumps(data["rules"], sort_keys=True).encode()).hexdigest()
    return Policy(tuple(rules), digest, source)


def _string_list(value: object, rule_id: str, field: str) -> list[str]:
    values = [value] if isinstance(value, str) else value
    if not isinstance(values, list) or not values or not all(isinstance(v, str) and v for v in values):
        raise PolicyError(f"rule {rule_id}: '{field}' must be a string or a list of strings")
    return values


def _regex(raw: dict, rule_id: str, field: str) -> re.Pattern[str] | None:
    pattern = raw.get(field)
    if pattern is None:
        return None
    if not isinstance(pattern, str):
        raise PolicyError(f"rule {rule_id}: '{field}' must be a regex string")
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise PolicyError(f"rule {rule_id}: bad '{field}' regex: {exc}") from exc


def _parse_rule(raw: object, source: str) -> Rule:
    if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not raw["id"]:
        raise PolicyError(f"{source}: every rule needs a string 'id'")
    rule_id = raw["id"]
    extra = set(raw) - _RULE_FIELDS
    if extra:
        raise PolicyError(f"rule {rule_id}: unknown field(s): {', '.join(sorted(extra))}")
    if "tool" not in raw:
        raise PolicyError(f"rule {rule_id}: 'tool' is required")
    action = raw.get("action")
    if action not in ACTIONS:
        raise PolicyError(f"rule {rule_id}: 'action' must be one of {', '.join(ACTIONS)}")
    if action != "allow" and not raw.get("message"):
        raise PolicyError(f"rule {rule_id}: '{action}' needs a 'message' to show the agent")
    return Rule(
        id=rule_id,
        tools=frozenset(_string_list(raw["tool"], rule_id, "tool")),
        action=action,
        message=str(raw.get("message") or ""),
        profiles=frozenset(_string_list(raw["profile"], rule_id, "profile")) if "profile" in raw else None,
        command=_regex(raw, rule_id, "command"),
        unless_command=_regex(raw, rule_id, "unless_command"),
        globs=tuple(compile_glob(g) for g in _string_list(raw["path"], rule_id, "path")) if "path" in raw else (),
    )
