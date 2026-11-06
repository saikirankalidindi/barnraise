"""Plan validation (PRD B-9, B-10) and analysis (critical path, parallelism).

Edges: `after` gives completion edges (dependent starts once the producer is merged);
`consumes` gives contract edges (dependent starts once the producer publishes). Two
tasks can run at the same time unless one reaches the other through completion edges.
"""

from collections import defaultdict
from itertools import combinations
from typing import Any

from barnraise import globs
from barnraise.plan import Issue, Plan

# Files every task tends to touch; only T0 may own them (B-10).
SHARED_FILES = (
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "pnpm-workspace.yaml",
    "yarn.lock",
    "bun.lock",
    "bun.lockb",
    "pyproject.toml",
    "uv.lock",
    "poetry.lock",
    "requirements.txt",
    "Cargo.toml",
    "Cargo.lock",
    "go.mod",
    "go.sum",
    "Gemfile",
    "Gemfile.lock",
    "composer.json",
    "composer.lock",
)
FOUNDATION = "T0"
SIZE_WEIGHT = {"S": 1.0, "M": 2.0, "L": 3.0}
# Estimate for when a producer publishes its contract, as a share of its duration.
PUBLISH_AT = 0.5


def edges(plan: Plan) -> list[tuple[str, str, str]]:
    """(from, to, type) for every dependency, from known tasks and contracts only."""
    keys = {t.key for t in plan.tasks}
    producer = {c: t.key for t in plan.tasks for c in t.produces}
    out = []
    for t in plan.tasks:
        out += [(d, t.key, "completion") for d in t.after if d in keys and d != t.key]
        for c in t.consumes:
            p = producer.get(c)
            if p is not None and p != t.key:
                out.append((p, t.key, "contract"))
    return out


def validate(plan: Plan) -> list[Issue]:
    issues: list[Issue] = []
    index = {t.key: i for i, t in enumerate(plan.tasks)}

    if not plan.tasks:
        issues.append(Issue("no_tasks", "the plan has no tasks", ("tasks",)))
    if not [c for c in plan.checks if c.strip()]:
        issues.append(Issue("no_checks", "the plan needs at least one project check", ("checks",)))

    # Keys and completion dependencies.
    seen: dict[str, int] = {}
    for i, t in enumerate(plan.tasks):
        if t.key in seen:
            issues.append(
                Issue("duplicate_key", f"{t.key} is used by two tasks", ("tasks", i, "key"))
            )
        seen.setdefault(t.key, i)
        for j, d in enumerate(t.after):
            if d == t.key:
                issues.append(
                    Issue("self_dependency", f"{t.key} depends on itself", ("tasks", i, "after", j))
                )
            elif d not in index:
                issues.append(
                    Issue(
                        "unknown_task",
                        f"{t.key} depends on {d}, which is not in the plan",
                        ("tasks", i, "after", j),
                    )
                )
        if not [a for a in t.acceptance if a.strip()]:
            issues.append(
                Issue(
                    "no_acceptance",
                    f"{t.key} has no acceptance checks",
                    ("tasks", i, "acceptance"),
                )
            )

    issues += _contract_issues(plan)
    cycle = find_cycle(plan)
    if cycle:
        first = plan.tasks[index[cycle[0]]] if cycle[0] in index else None
        issues.append(
            Issue(
                "cycle",
                "dependency cycle: " + " -> ".join([*cycle, cycle[0]]),
                ("tasks", index[cycle[0]]) if first else (),
            )
        )
    issues += _path_issues(plan, has_cycle=bool(cycle))
    return issues


def _contract_issues(plan: Plan) -> list[Issue]:
    issues = []
    declared: dict[str, int] = {}
    for i, contract in enumerate(plan.contracts):
        if contract.name in declared:
            issues.append(
                Issue(
                    "duplicate_contract",
                    f"contract {contract.name} is declared twice",
                    ("contracts", i, "name"),
                )
            )
        declared.setdefault(contract.name, i)
    producers: dict[str, list[str]] = defaultdict(list)
    for i, t in enumerate(plan.tasks):
        for j, c in enumerate(t.produces):
            producers[c].append(t.key)
            if c not in declared:
                issues.append(
                    Issue(
                        "undeclared_contract",
                        f"{t.key} produces {c}, which is not in contracts",
                        ("tasks", i, "produces", j),
                    )
                )
    for name, keys in producers.items():
        if len(keys) > 1:
            issues.append(
                Issue(
                    "multiple_producers",
                    f"contract {name} is produced by {', '.join(keys)}; "
                    "exactly one task may produce it",
                    ("contracts", declared[name]) if name in declared else (),
                )
            )
    for name, i in declared.items():
        if name not in producers:
            issues.append(
                Issue(
                    "unproduced_contract",
                    f"contract {name} is declared but no task produces it",
                    ("contracts", i),
                )
            )
    for i, t in enumerate(plan.tasks):
        for j, c in enumerate(t.consumes):
            if c in t.produces:
                issues.append(
                    Issue(
                        "self_contract",
                        f"{t.key} consumes {c}, which it produces itself",
                        ("tasks", i, "consumes", j),
                    )
                )
            elif c not in producers:
                issues.append(
                    Issue(
                        "unproduced_contract",
                        f"{t.key} consumes {c}, but no task produces it",
                        ("tasks", i, "consumes", j),
                    )
                )
    return issues


def _path_issues(plan: Plan, *, has_cycle: bool) -> list[Issue]:
    issues = []
    for i, t in enumerate(plan.tasks):
        if not t.owns:
            issues.append(Issue("owns_nothing", f"{t.key} owns no paths", ("tasks", i, "owns")))
        for j, g in enumerate(t.owns):
            if reason := globs.outside_repo(g):
                issues.append(
                    Issue(
                        "path_outside_repo",
                        f"{t.key} owns {g!r}: paths {reason}",
                        ("tasks", i, "owns", j),
                    )
                )
            elif t.key != FOUNDATION:
                shared = [f for f in SHARED_FILES if globs.overlap(g, f)]
                if shared:
                    issues.append(
                        Issue(
                            "shared_file",
                            f"{t.key} owns {g!r}, which covers {', '.join(shared)}; only "
                            f"{FOUNDATION} may own shared files (send it a notice instead)",
                            ("tasks", i, "owns", j),
                        )
                    )
    if has_cycle:
        return issues  # "can run in parallel" is undefined until the cycle is fixed
    serial = completion_reachability(plan)
    for (_, a), (j, b) in combinations(enumerate(plan.tasks), 2):
        if b.key in serial[a.key] or a.key in serial[b.key]:
            continue
        for ga in a.owns:
            for gb in b.owns:
                if (
                    not globs.outside_repo(ga)
                    and not globs.outside_repo(gb)
                    and globs.overlap(ga, gb)
                ):
                    issues.append(
                        Issue(
                            "owned_path_overlap",
                            f"{a.key} owns {ga!r} and {b.key} owns {gb!r}; they can run in "
                            "parallel, so their owned paths must not overlap (or add an 'after' "
                            "dependency between them)",
                            ("tasks", j, "owns", b.owns.index(gb)),
                        )
                    )
    return issues


def find_cycle(plan: Plan) -> list[str] | None:
    graph: dict[str, list[str]] = defaultdict(list)
    for a, b, _ in edges(plan):
        graph[a].append(b)
    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(n: str) -> list[str] | None:
        state[n] = 1
        stack.append(n)
        for m in sorted(graph[n]):
            if state.get(m) == 1:
                return stack[stack.index(m) :]
            if m not in state and (found := visit(m)):
                return found
        stack.pop()
        state[n] = 2
        return None

    for t in plan.tasks:
        if t.key not in state and (found := visit(t.key)):
            return found
    return None


def completion_reachability(plan: Plan) -> dict[str, set[str]]:
    """For each task A, the tasks that cannot start until A is merged.

    That is everything reachable from A's completion successors, by any edges. A path
    that leaves A through a contract edge does not serialise: the consumer starts once A
    publishes, so A may still be running when the rest of that path runs.
    """
    succ: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for a, b, kind in edges(plan):
        succ[a].append((b, kind))
    out: dict[str, set[str]] = {}
    for t in plan.tasks:
        reach: set[str] = set()
        todo = [b for b, kind in succ[t.key] if kind == "completion"]
        while todo:
            n = todo.pop()
            if n in reach:
                continue
            reach.add(n)
            todo += [b for b, _ in succ[n]]
        out[t.key] = reach
    return out


def analyse(plan: Plan) -> dict[str, Any]:
    """Critical path and parallelism, for valid (acyclic) plans."""
    weight = {t.key: SIZE_WEIGHT[t.size] for t in plan.tasks}
    preds: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for a, b, kind in edges(plan):
        preds[b].append((a, kind))
    start: dict[str, float] = {}
    via: dict[str, str | None] = {}

    def earliest(k: str) -> float:
        if k not in start:
            best, best_from = 0.0, None
            for p, kind in preds[k]:
                ready = earliest(p) + weight[p] * (1.0 if kind == "completion" else PUBLISH_AT)
                if ready > best:
                    best, best_from = ready, p
            start[k], via[k] = best, best_from
        return start[k]

    for t in plan.tasks:
        earliest(t.key)
    finish = {k: start[k] + weight[k] for k in start}
    end = max(finish, key=lambda k: (finish[k], k)) if finish else None
    path: list[str] = []
    node = end
    while node is not None:
        path.append(node)
        node = via[node]
    path.reverse()

    serial = completion_reachability(plan)
    parallel_pairs = [
        (a.key, b.key)
        for a, b in combinations(plan.tasks, 2)
        if b.key not in serial[a.key] and a.key not in serial[b.key]
    ]
    # Peak concurrency in the estimated schedule (unlimited agents).
    points = sorted([(start[k], 1) for k in start] + [(finish[k], -1) for k in finish])
    peak = cur = 0
    for _, d in points:
        cur += d
        peak = max(peak, cur)
    total = sum(weight.values())
    span = finish[end] if end else 0.0
    return {
        "tasks": len(plan.tasks),
        "critical_path": path,
        "critical_path_length": span,
        "total_effort": total,
        "max_speedup": round(total / span, 2) if span else 1.0,
        "peak_parallel": peak,
        "parallel_pairs": len(parallel_pairs),
        "edges": [{"from": a, "to": b, "type": k} for a, b, k in edges(plan)],
    }
