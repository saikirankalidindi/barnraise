"""Owned-path globs (PRD B-9): syntax checks and a conservative overlap test.

Globs are repo-relative, `/`-separated, and use `*`, `?`, `[...]` within a segment and
`**` for any number of segments. Two globs overlap when some path could match both.
The test may report an overlap that cannot really happen (it is conservative inside a
segment), never the reverse.
"""

from fnmatch import fnmatchcase
from functools import cache

WILDCARDS = frozenset("*?[")


def outside_repo(glob: str) -> str | None:
    """Why a glob points outside the repository, or None."""
    if glob.startswith(("/", "~")) or (len(glob) > 1 and glob[1] == ":"):
        return "must be relative to the repository root"
    if ".." in glob.split("/"):
        return "must not contain '..'"
    if not glob.strip() or glob.strip("/") == "":
        return "must not be empty"
    return None


def normalise(glob: str) -> tuple[str, ...]:
    parts = [p for p in glob.strip().strip("/").split("/") if p not in ("", ".")]
    # A trailing directory name owns everything under it: `src/api` == `src/api/**`.
    if parts and not any(c in WILDCARDS for c in parts[-1]) and "." not in parts[-1]:
        parts.append("**")
    return tuple(parts)


def _literal(seg: str) -> bool:
    return not any(c in WILDCARDS for c in seg)


def _segments_overlap(a: str, b: str) -> bool:
    if _literal(a) and _literal(b):
        return a == b
    if _literal(a):
        return fnmatchcase(a, b)
    if _literal(b):
        return fnmatchcase(b, a)

    # Both have wildcards: compare the fixed text before the first and after the last one.
    def prefix(s: str) -> str:
        return s[: min((s.index(c) for c in WILDCARDS if c in s), default=len(s))]

    def suffix(s: str) -> str:
        last = max((s.rindex(c) for c in "*?]" if c in s), default=-1)
        return s[last + 1 :]

    pa, pb, sa, sb = prefix(a), prefix(b), suffix(a), suffix(b)
    return (pa.startswith(pb) or pb.startswith(pa)) and (sa.endswith(sb) or sb.endswith(sa))


def overlap(a: str, b: str) -> bool:
    return _overlap(normalise(a), normalise(b))


@cache
def _overlap(a: tuple[str, ...], b: tuple[str, ...]) -> bool:
    if not a or not b:
        # An empty pattern matches only the end of a path; `**` may match zero segments.
        return all(s == "**" for s in a) and all(s == "**" for s in b)
    if a[0] == "**":
        return _overlap(a[1:], b) or _overlap(a, b[1:])
    if b[0] == "**":
        return _overlap(a, b[1:]) or _overlap(a[1:], b)
    return _segments_overlap(a[0], b[0]) and _overlap(a[1:], b[1:])
