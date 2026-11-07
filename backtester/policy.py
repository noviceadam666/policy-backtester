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

from .matchers import compile_glob, normalize_path

ACTIONS = ("block", "approve", "allow")
_RULE_FIELDS = frozenset({
    "id", "tool", "profile", "command", "unless_command", "unless", "path", "args", "action", "message", "if_unresolved",
})
_UNLESS_FIELDS = frozenset({"command", "workdir"})


class PolicyError(ValueError):
    pass


@dataclass(frozen=True)
class Exemption:
    """One ``unless`` entry: segments its regex matches are exempt, from ``workdir`` only when it names one."""

    command: re.Pattern[str]
    workdir: str | None = None


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
    args: tuple[tuple[str, re.Pattern[str]], ...] = ()
    if_unresolved: str | None = None
    unless: tuple[Exemption, ...] = ()
    paths: tuple[str, ...] = ()

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
    return _compile(pattern, rule_id, field)


def _compile(pattern: object, rule_id: str, field: str) -> re.Pattern[str]:
    if not isinstance(pattern, str):
        raise PolicyError(f"rule {rule_id}: '{field}' must be a regex string")
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise PolicyError(f"rule {rule_id}: bad '{field}' regex: {exc}") from exc


def _args(raw: dict, rule_id: str) -> tuple[tuple[str, re.Pattern[str]], ...]:
    if "args" not in raw:
        return ()
    value = raw["args"]
    if not isinstance(value, dict) or not value or not all(isinstance(k, str) and k for k in value):
        raise PolicyError(f"rule {rule_id}: 'args' must map argument names to regexes")
    return tuple((name, _compile(pattern, rule_id, f"args.{name}")) for name, pattern in value.items())


def _unless(raw: dict, rule_id: str) -> tuple[Exemption, ...]:
    if "unless" not in raw:
        return ()
    value = raw["unless"]
    if not isinstance(value, list) or not value:
        raise PolicyError(f"rule {rule_id}: 'unless' must be a list of exemptions, each a 'command' regex and an optional 'workdir'")
    entries = []
    for n, entry in enumerate(value, 1):
        where = f"unless[{n}]"
        if not isinstance(entry, dict) or "command" not in entry:
            raise PolicyError(f"rule {rule_id}: '{where}' needs a 'command' regex")
        extra = set(entry) - _UNLESS_FIELDS
        if extra:
            raise PolicyError(f"rule {rule_id}: unknown field(s) in '{where}': {', '.join(sorted(extra))}")
        workdir = entry.get("workdir")
        if workdir is not None and not (isinstance(workdir, str) and workdir.startswith(("/", "~"))):
            raise PolicyError(f"rule {rule_id}: '{where}.workdir' must be an absolute folder")
        entries.append(Exemption(_compile(entry["command"], rule_id, f"{where}.command"), normalize_path(workdir) if workdir else None))
    return tuple(entries)


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
    if_unresolved = raw.get("if_unresolved")
    if if_unresolved is not None:
        if if_unresolved not in ("block", "approve"):
            raise PolicyError(f"rule {rule_id}: 'if_unresolved' must be block or approve")
        if "path" not in raw:
            raise PolicyError(f"rule {rule_id}: 'if_unresolved' only means something on a rule with a 'path'")
        if not raw.get("message"):
            raise PolicyError(f"rule {rule_id}: 'if_unresolved' needs a 'message' to show the agent")
    paths = tuple(_string_list(raw["path"], rule_id, "path")) if "path" in raw else ()
    return Rule(
        id=rule_id,
        tools=frozenset(_string_list(raw["tool"], rule_id, "tool")),
        action=action,
        message=str(raw.get("message") or ""),
        profiles=frozenset(_string_list(raw["profile"], rule_id, "profile")) if "profile" in raw else None,
        command=_regex(raw, rule_id, "command"),
        unless_command=_regex(raw, rule_id, "unless_command"),
        globs=tuple(compile_glob(g) for g in paths),
        args=_args(raw, rule_id),
        if_unresolved=if_unresolved,
        unless=_unless(raw, rule_id),
        paths=paths,
    )
