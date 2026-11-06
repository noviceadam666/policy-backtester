"""Matching primitives: shell segments, path candidates and path globs.

A terminal call has only a ``command`` string, so a ``path`` glob needs a defined meaning for it: a
rule's glob hits when any path the command *mentions* matches. ``path_candidates`` pulls those out,
including the forms an upload uses (``curl -F file=@config/vendor-api.yaml``), and resolves relative
ones against ``workdir`` and any ``cd`` before them.

This over-matches on purpose: ``echo .env >> .gitignore`` counts as a mention, and the backtest is
where you find that out and scope it with ``unless_command``. It can't see through indirection: a
shell glob (``cat .en*``), a variable set elsewhere (``cat $SECRET_FILE``) or a parent directory
(``tar c config/``). ``has_unresolved`` reports the first two, so a rule can ask a human instead.
"""

from __future__ import annotations

import posixpath
import re
import shlex
from collections.abc import Iterable, Iterator

# Deliberately ignores quoting. Splitting inside a quoted string only makes a segment smaller, so
# ``unless_command`` is less likely to match and the rule more likely to fire: the safe direction.
# It also catches ``bash -c "rm -rf ./cache/x; rm -rf /"``.
_SEGMENT_BREAK = re.compile(r"&&|\|\||[;&|\n()`]")
_TOKEN_SPLIT = re.compile(r"[=@:,<>]")
_SHORT_OPTION = re.compile(r"^-[A-Za-z].")
_HOME_VAR = re.compile(r"^\$\{?HOME\}?(?=/|$)")
_HOME_PREFIX = r"(?:~|/Users/[^/]+|/home/[^/]+|/root)/"
_QUOTES = "'\"`"
# What the shell expands before the command sees it: variables other than HOME, which path matching
# already resolves, wildcards and brace lists. A lone ``[`` is the test builtin.
_UNRESOLVED = re.compile(r"\$\{?[A-Za-z_]|[*?]|\[[^\]]*\]|\{[^}]*,[^}]*\}")
_ANY_HOME_VAR = re.compile(r"\$\{?HOME\}?(?![A-Za-z0-9_])")


def segment_spans(command: str) -> list[tuple[int, int]]:
    """Start and end offsets of each simple command in a shell string."""
    spans = []
    start = 0
    for match in _SEGMENT_BREAK.finditer(command):
        spans.append((start, match.start()))
        start = match.end()
    spans.append((start, len(command)))
    return spans


def span_at(spans: list[tuple[int, int]], offset: int) -> tuple[int, int]:
    for span in spans:
        if span[0] <= offset <= span[1]:
            return span
    return spans[-1]


def _tokens(segment: str) -> list[str]:
    try:
        return shlex.split(segment)
    except ValueError:
        return [t.strip(_QUOTES) for t in segment.split()]


def _pieces(token: str) -> Iterator[str]:
    """Every part of a token that could be a path: ``file=@a/b;type=x`` holds ``a/b``."""
    for word in token.split():
        word = word.strip(_QUOTES)
        if "://" in word:
            if word.startswith("file://"):
                yield word[len("file://"):]
            continue
        yield word
        yield from _TOKEN_SPLIT.split(word)
        if _SHORT_OPTION.match(word):
            yield word[2:]


def normalize_path(path: str) -> str:
    path = _HOME_VAR.sub("~", path.strip().strip(_QUOTES))
    return posixpath.normpath(path) if path else ""


def _resolve(path: str, cwd: str | None) -> str:
    if cwd is None or path.startswith(("/", "~")):
        return path
    return posixpath.normpath(posixpath.join(cwd, path))


def segment_candidates(segment: str, cwd: str | None) -> tuple[set[str], str | None]:
    """The paths one segment mentions, as written and resolved, and the working directory after it."""
    tokens = _tokens(segment)
    if len(tokens) >= 2 and tokens[0] == "cd":
        cwd = _resolve(normalize_path(tokens[1]), cwd)
    found: set[str] = set()
    for token in tokens:
        for piece in _pieces(token):
            path = normalize_path(piece)
            if path in ("", ".") or path.startswith("-"):
                continue
            found.add(path)
            found.add(_resolve(path, cwd))
    return found, cwd


def has_unresolved(segment: str) -> bool:
    """Whether a segment holds something the shell expands, so its real paths can't be known here."""
    return any(_UNRESOLVED.search(_ANY_HOME_VAR.sub("~", piece)) for token in _tokens(segment) for piece in _pieces(token))


def path_candidates(command: str, workdir: str | None = None) -> set[str]:
    found: set[str] = set()
    cwd = normalize_path(workdir) if workdir else None
    for start, end in segment_spans(command):
        paths, cwd = segment_candidates(command[start:end], cwd)
        found |= paths
    return found


def compile_glob(glob: str) -> re.Pattern[str]:
    """Compile a path glob. ``**/`` is zero or more directories, ``*`` stays within one.

    ``~/x`` also matches the same path under an absolute home directory, and ``x/**`` matches
    ``x`` itself as well as everything beneath it.
    """
    out = ""
    i = 0
    if glob.startswith("~/"):
        out = _HOME_PREFIX
        i = 2
    elif glob.startswith("**/"):
        out = "/?(?:[^/]+/)*"
        i = 3
    while i < len(glob):
        if glob.startswith("/**", i) and i + 3 == len(glob):
            out += "(?:/.*)?"
            i += 3
        elif glob.startswith("**/", i):
            out += "(?:[^/]+/)*"
            i += 3
        elif glob.startswith("**", i):
            out += ".*"
            i += 2
        elif glob[i] == "*":
            out += "[^/]*"
            i += 1
        elif glob[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(glob[i])
            i += 1
    return re.compile(out + r"\Z")


def any_glob_matches(globs: Iterable[re.Pattern[str]], paths: Iterable[str]) -> bool:
    return any(glob.match(path) for path in paths for glob in globs)
