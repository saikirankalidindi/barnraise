"""The plan (PRD sections 2 and 3.2): models, plan.yaml export, and parsing with line numbers.

plan.yaml is the human-editable form. Dependencies are written per task:
`after` lists completion edges; consuming a contract creates a contract edge from
the contract's producer.
"""

from dataclasses import dataclass
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

KEY_PATTERN = r"^T\d+$"
NAME_PATTERN = r"^[a-z][a-z0-9_.-]*$"

HEADER = (
    "# Barnraise plan. Edit this file, then run /barnraise:plan --sync to validate it.\n"
    "# after: completion dependencies (start once those tasks are merged).\n"
    "# consumes: contract dependencies (start once the producing task publishes the contract).\n"
)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Contract(_Model):
    name: str = Field(pattern=NAME_PATTERN, description="Slug, e.g. tasks-api")
    kind: Literal["openapi", "types", "schema", "cli", "other"]
    description: str = Field(min_length=1, description="What the contract specifies")


class Task(_Model):
    key: str = Field(pattern=KEY_PATTERN, description="T0, T1, ...; T0 is the foundation task")
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1)
    acceptance: list[str] = Field(
        description="Shell commands, run from the repo root, that pass only when the task is done"
    )
    owns: list[str] = Field(description="Repo-relative globs this task may change")
    after: list[str] = Field(default=[], description="Completion dependencies (task keys)")
    produces: list[str] = Field(default=[], description="Contract names this task publishes")
    consumes: list[str] = Field(default=[], description="Contract names this task needs")
    size: Literal["S", "M", "L"] = Field(default="M", description="Relative effort")


class Plan(_Model):
    prompt: str = Field(min_length=1, description="The project prompt this plan implements")
    checks: list[str] = Field(
        description="Project-wide commands that define green, run after every merge"
    )
    setup: list[str] = Field(
        default=[],
        description="Commands that prepare a fresh checkout before checks run, e.g. "
        "'pnpm install' or 'uv sync'",
    )
    contracts: list[Contract] = []
    tasks: list[Task]


Path = tuple[str | int, ...]


@dataclass(frozen=True)
class Issue:
    code: str
    message: str
    path: Path = ()
    line: int | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.path:
            out["at"] = format_path(self.path)
        if self.line is not None:
            out["line"] = self.line
        return out


def format_path(path: Path) -> str:
    out = ""
    for p in path:
        out += f"[{p}]" if isinstance(p, int) else (f".{p}" if out else p)
    return out


def schema_issues(exc: ValidationError) -> list[Issue]:
    issues = []
    for err in exc.errors():
        loc = tuple(err["loc"])
        msg = err["msg"]
        if err["type"] == "string_pattern_mismatch":
            msg = f"{err['input']!r} is not a valid {loc[-1]}"
        elif err["type"] == "extra_forbidden":
            msg = f"unknown field {loc[-1]!r}"
        issues.append(Issue("schema", msg, loc))
    return issues


def parse(data: Any) -> tuple[Plan | None, list[Issue]]:
    try:
        return Plan.model_validate(data), []
    except ValidationError as exc:
        return None, schema_issues(exc)


def to_yaml(plan: Plan) -> str:
    data = plan.model_dump(mode="json")
    for task in data["tasks"]:
        for key in ("after", "produces", "consumes"):
            if not task[key]:
                del task[key]
    if not data["contracts"]:
        del data["contracts"]
    if not data["setup"]:
        del data["setup"]
    body = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100)
    return HEADER + body


def from_yaml(text: str) -> tuple[Plan | None, list[Issue]]:
    """Parse plan.yaml. Every issue carries the line it refers to, where known."""
    try:
        node = yaml.compose(text)
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        line = mark.line + 1 if mark is not None else None
        return None, [Issue("yaml", str(exc).split("\n")[0], (), line)]
    lines = _line_index(node) if node is not None else {}
    plan, issues = parse(data)
    return plan, [with_line(i, lines) for i in issues]


def with_line(issue: Issue, lines: dict[Path, int]) -> Issue:
    path = issue.path
    while path and path not in lines:
        path = path[:-1]
    line = lines.get(path)
    return Issue(issue.code, issue.message, issue.path, line)


def line_index(text: str) -> dict[Path, int]:
    try:
        node = yaml.compose(text)
    except yaml.YAMLError:
        return {}
    return _line_index(node) if node is not None else {}


def _line_index(node: yaml.Node, path: Path = ()) -> dict[Path, int]:
    out: dict[Path, int] = {path: node.start_mark.line + 1}
    if isinstance(node, yaml.MappingNode):
        for k, v in node.value:
            key = k.value
            out[(*path, key)] = k.start_mark.line + 1
            for p, line in _line_index(v, (*path, key)).items():
                out.setdefault(p, line)
    elif isinstance(node, yaml.SequenceNode):
        for i, item in enumerate(node.value):
            out.update(_line_index(item, (*path, i)))
    return out
