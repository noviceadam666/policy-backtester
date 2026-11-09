"""Matching primitives: shell segments, path candidates and path globs.

A terminal call has only a ``command`` string, so a ``path`` glob needs a defined meaning for it: a
rule's glob hits when any path the command *mentions* matches. ``path_candidates`` pulls those out,
including the forms an upload uses (``curl -F file=@config/vendor-api.yaml``), and resolves relative
ones against ``workdir`` and any ``cd`` before them.

This over-matches on purpose: ``echo .env >> .gitignore`` counts as a mention, and the backtest is
where you find that out and scope it with ``unless_command``. It can't see through indirection: a
shell glob (``cat .en*``), a variable set elsewhere (``cat $SECRET_FILE``) or a parent directory
(``tar c config/``). ``has_unresolved`` reports the first two, so a rule can ask a human instead, and
``could_reach`` drops a wildcard that can't name any of the rule's paths.

``segment_folders`` is the other direction: the folder a segment runs in, for an exemption pinned to
one. It only claims a folder the call itself sets, so it under-claims, and a pinned exemption that
can't be sure of the folder doesn't apply.
"""

from __future__ import annotations

import posixpath
import re
import shlex
from bisect import bisect_right
from collections.abc import Iterable, Iterator
from fnmatch import fnmatchcase
from functools import lru_cache
from operator import itemgetter

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
# Expansions that could produce any path, slashes and all; a plain wildcard stays in its folder.
_ANYTHING = re.compile(r"\$\{?[A-Za-z_]|\{[^}]*,[^}]*\}")
_WILDCARD = re.compile(r"[*?]|\[[^\]]*\]")
# Words that run other shell code, which may change folder in ways one segment can't show.
_RUNS_SHELL_CODE = frozenset({"eval", "source", ".", "exec", "builtin", "command"})
_CHANGES_FOLDER = frozenset({"cd", "pushd", "popd"})


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
    """The segment a match starting at ``offset`` belongs to.

    A pattern like ``(^|[ ;&|])rm`` starts its match on the separator before the command, so an offset on a
    separator belongs to the segment after it, never the one before: an exemption covers only the segment it
    matches. Spans are in order and never overlap, so this bisects; the hook looks one up for every match.
    """
    i = bisect_right(spans, offset, key=itemgetter(0))
    if i and offset < spans[i - 1][1]:
        return spans[i - 1]
    return spans[i] if i < len(spans) else spans[-1]


@lru_cache(maxsize=1024)
def _tokens(segment: str) -> tuple[str, ...]:
    # Cached: every path rule reads every segment, and shlex builds a long word a character at a time.
    try:
        return tuple(shlex.split(segment))
    except ValueError:
        return tuple(t.strip(_QUOTES) for t in segment.split())


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
    return bool(unresolved_pieces(segment))


def unresolved_pieces(segment: str) -> list[str]:
    """The words of a segment the shell would expand, as written, first to last."""
    found = (piece for token in _tokens(segment) for piece in _pieces(token) if _UNRESOLVED.search(_ANY_HOME_VAR.sub("~", piece)))
    return list(dict.fromkeys(found))


# The home folders ``compile_glob`` lets ``~/`` stand for, as components.
_HOMES = (("~",), ("/", "Users", "*"), ("/", "home", "*"), ("/", "root"))


def _glob_parts(glob: str) -> list[tuple[str, ...]]:
    """A path glob as components, one tuple per home folder ``~/`` can be. ``**`` is any number of them."""
    if glob.startswith("~/"):
        return [home + rest for home in _HOMES for rest in _glob_parts(glob[2:])]
    if glob.startswith("**/"):
        return [("**", *rest) for rest in _glob_parts(glob[3:])]
    head = ("/",) if glob.startswith("/") else ()
    return [head + tuple("**" if "**" in part else part for part in glob.split("/") if part)]


def _same_component(word: str, glob: str) -> bool:
    """Whether some file name could match both a shell word's component and a rule glob's.

    Shell words use ``*``, ``?`` and ``[...]``; rule globs only ``*`` and ``?``. Two wildcards are
    assumed to meet.
    """
    if "**" in (word, glob):
        return True
    word_wild, glob_wild = bool(_WILDCARD.search(word)), "*" in glob or "?" in glob
    if word_wild and glob_wild:
        return True
    if word_wild:
        return fnmatchcase(glob, word)
    return fnmatchcase(word, glob.replace("[", "[[]")) if glob_wild else word == glob


def _could_meet(word: tuple[str, ...], glob: tuple[str, ...]) -> bool:
    if len(word) + len(glob) > 256:
        # Too deep to walk without running out of stack; saying it could is the safe answer.
        return True

    @lru_cache(maxsize=None)
    def meet(i: int, j: int) -> bool:
        if i == len(word) and j == len(glob):
            return True
        if (i < len(word) and word[i] == "**" and meet(i + 1, j)) or (j < len(glob) and glob[j] == "**" and meet(i, j + 1)):
            return True
        if i == len(word) or j == len(glob) or (word[i] == "**" and glob[j] == "**"):
            return False
        if not _same_component(word[i], glob[j]):
            return False
        return meet(i if word[i] == "**" else i + 1, j if glob[j] == "**" else j + 1)

    return meet(0, 0)


def could_reach(piece: str, globs: Iterable[str], cwd: str | None) -> bool:
    """Whether a word the shell expands could name a path one of ``globs`` matches, as written or resolved.

    Only wildcards are narrowed: ``./cache/*`` names something in ``cache/``, so it can't be
    ``config/vendor-api.yaml``. A variable or a brace list could be anything, and so could ``..``.
    """
    text = _ANY_HOME_VAR.sub("~", piece)
    if _ANYTHING.search(text):
        return True
    written = normalize_path(text)
    for form in {written, _resolve(written, cwd)}:
        parts = form.split("/")
        if ".." in parts:
            return True
        word = (("/",) if form.startswith("/") else ()) + tuple(p for p in parts if p)
        if any(_could_meet(word, parts) for glob in globs for parts in _glob_parts(glob)):
            return True
    return False


def segment_folders(command: str, workdir: str | None) -> dict[tuple[int, int], str | None]:
    """The folder each segment runs in, keyed by its span, as far as the call itself says.

    It starts at the call's ``workdir``. Hermes's terminal keeps the folder between calls, so a call
    that names none runs wherever the last one left it: None here. A plain ``cd DIR`` moves it for
    what follows only when ``&&`` joins them, since after ``;`` or ``||`` the ``cd`` may have failed,
    and in a pipe, a subshell or the background it doesn't last. Anything else that could move it
    (``pushd``, ``eval``, ``source``, a ``cd`` inside a nested shell) leaves it None from there on.
    """
    folder = normalize_path(workdir) if isinstance(workdir, str) and workdir.startswith(("/", "~")) else None
    spans = segment_spans(command)
    breaks = [m.group() for m in _SEGMENT_BREAK.finditer(command)]
    folders: dict[tuple[int, int], str | None] = {}
    for i, span in enumerate(spans):
        folders[span] = folder
        tokens = _tokens(command[span[0]:span[1]])
        if not tokens:
            continue
        before, after = breaks[i - 1] if i else None, breaks[i] if i < len(breaks) else None
        if tokens[0] == "cd" and len(tokens) <= 2 and before in (None, "&&", ";", "\n") and after == "&&":
            target = tokens[1] if len(tokens) == 2 else "~"
            if target == "-" or has_unresolved(target):
                folder = None
            elif target.startswith(("/", "~", "$HOME", "${HOME}")):
                folder = normalize_path(target)
            else:
                folder = _resolve(normalize_path(target), folder) if folder else None
        elif tokens[0] in _RUNS_SHELL_CODE or _CHANGES_FOLDER & set(tokens):
            folder = None
    return folders


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
