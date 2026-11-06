"""The plan (PRD sections 2 and 3.2): models, validation of their shape, plan.yaml export,
and parsing with line numbers.

plan.yaml is the human-editable form. Dependencies are written per task:
`after` lists completion edges; consuming a contract creates a contract edge from
the contract's producer.

Standard library only (plus vendored pure-Python PyYAML), so the plugin runs from its
own readable source with no package installs.
"""

import json
import re
from dataclasses import dataclass, field
from typing import Any, ClassVar

from barnraise._vendor import yaml

KEY_PATTERN = r"^T\d+$"
NAME_PATTERN = r"^[a-z][a-z0-9_.-]*$"

HEADER = (
    "# Barnraise plan. Edit this file, then run /barnraise:plan --sync to validate it.\n"
    "# after: completion dependencies (start once those tasks are merged).\n"
    "# consumes: contract dependencies (start once the producing task publishes the contract).\n"
)

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


class PlanError(ValueError):
    """The data does not have the shape of a plan."""

    def __init__(self, issues: list[Issue]) -> None:
        super().__init__("; ".join(f"{format_path(i.path)}: {i.message}" for i in issues))
        self.issues = issues


# --- Field specs: one source for validation and for the tool's JSON schema ----------


@dataclass(frozen=True)
class Spec:
    name: str
    kind: str  # text | texts | choice | models
    description: str
    required: bool = True
    pattern: str | None = None
    min_len: int = 0
    max_len: int | None = None
    choices: tuple[str, ...] = ()
    model: Any = None  # the model class, for kind == "models"
    default: Any = None


class _Model:
    SPECS: ClassVar[tuple[Spec, ...]] = ()

    @classmethod
    def json_schema(cls) -> dict[str, Any]:
        props: dict[str, Any] = {}
        for s in cls.SPECS:
            if s.kind == "text":
                p: dict[str, Any] = {"type": "string"}
                if s.pattern:
                    p["pattern"] = s.pattern
                if s.min_len:
                    p["minLength"] = s.min_len
                if s.max_len:
                    p["maxLength"] = s.max_len
            elif s.kind == "texts":
                p = {"type": "array", "items": {"type": "string"}}
            elif s.kind == "choice":
                p = {"type": "string", "enum": list(s.choices)}
            else:
                p = {"type": "array", "items": s.model.json_schema()}
            p["description"] = s.description
            if not s.required:
                p["default"] = s.default
            props[s.name] = p
        return {
            "type": "object",
            "properties": props,
            "required": [s.name for s in cls.SPECS if s.required],
            "additionalProperties": False,
        }

    @classmethod
    def _read(cls, data: Any, loc: Path, issues: list[Issue]) -> dict[str, Any] | None:
        """Check `data` against SPECS, collecting issues. Returns constructor arguments."""
        if not isinstance(data, dict):
            issues.append(Issue("schema", "should be an object", loc))
            return None
        out: dict[str, Any] = {}
        known = {s.name for s in cls.SPECS}
        for key in data:
            if key not in known:
                issues.append(Issue("schema", f"unknown field {key!r}", (*loc, key)))
        for s in cls.SPECS:
            here = (*loc, s.name)
            if s.name not in data:
                if s.required:
                    issues.append(Issue("schema", "field required", here))
                else:
                    out[s.name] = list(s.default) if isinstance(s.default, list) else s.default
                continue
            value = data[s.name]
            if s.kind == "text":
                if _text_ok(value, s, here, issues):
                    out[s.name] = value
            elif s.kind == "choice":
                if value in s.choices:
                    out[s.name] = value
                else:
                    issues.append(
                        Issue(
                            "schema",
                            f"{value!r} is not a valid {s.name} (one of {', '.join(s.choices)})",
                            here,
                        )
                    )
            elif s.kind == "texts":
                if not isinstance(value, list):
                    issues.append(Issue("schema", "should be a list of text", here))
                    continue
                bad = [i for i, v in enumerate(value) if not isinstance(v, str)]
                for i in bad:
                    issues.append(Issue("schema", "should be text", (*here, i)))
                if not bad:
                    out[s.name] = list(value)
            else:
                if not isinstance(value, list):
                    issues.append(Issue("schema", "should be a list", here))
                    continue
                items = [s.model.build(v, (*here, i), issues) for i, v in enumerate(value)]
                if all(item is not None for item in items):
                    out[s.name] = items
        return out

    @classmethod
    def build(cls, data: Any, loc: Path, issues: list[Issue]) -> Any:
        before = len(issues)
        args = cls._read(data, loc, issues)
        if args is None or len(issues) > before:
            return None
        return cls(**args)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for s in self.SPECS:
            value = getattr(self, s.name)
            out[s.name] = [v.to_dict() for v in value] if s.kind == "models" else value
        return out


def _text_ok(value: Any, s: Spec, here: Path, issues: list[Issue]) -> bool:
    if not isinstance(value, str):
        issues.append(Issue("schema", "should be text", here))
        return False
    if s.pattern and not re.match(s.pattern, value):
        issues.append(Issue("schema", f"{value!r} is not a valid {s.name}", here))
        return False
    if len(value) < s.min_len:
        issues.append(Issue("schema", "should not be empty", here))
        return False
    if s.max_len is not None and len(value) > s.max_len:
        issues.append(Issue("schema", f"should be at most {s.max_len} characters", here))
        return False
    return True


# --- The models ---------------------------------------------------------------------


@dataclass
class Contract(_Model):
    name: str
    kind: str
    description: str

    SPECS: ClassVar[tuple[Spec, ...]] = (
        Spec("name", "text", "Slug, e.g. tasks-api", pattern=NAME_PATTERN),
        Spec(
            "kind",
            "choice",
            "What kind of interface",
            choices=("openapi", "types", "schema", "cli", "other"),
        ),
        Spec("description", "text", "What the contract specifies", min_len=1),
    )


@dataclass
class Task(_Model):
    key: str
    title: str
    description: str
    acceptance: list[str]
    owns: list[str]
    after: list[str] = field(default_factory=list)
    produces: list[str] = field(default_factory=list)
    consumes: list[str] = field(default_factory=list)
    size: str = "M"

    SPECS: ClassVar[tuple[Spec, ...]] = (
        Spec("key", "text", "T0, T1, ...; T0 is the foundation task", pattern=KEY_PATTERN),
        Spec("title", "text", "Short title", min_len=1, max_len=120),
        Spec("description", "text", "The worker's brief: what to build and how", min_len=1),
        Spec(
            "acceptance",
            "texts",
            "Shell commands, run from the repo root, that pass only when the task is done",
        ),
        Spec("owns", "texts", "Repo-relative globs this task may change"),
        Spec("after", "texts", "Completion dependencies (task keys)", required=False, default=[]),
        Spec("produces", "texts", "Contract names this task publishes", required=False, default=[]),
        Spec("consumes", "texts", "Contract names this task needs", required=False, default=[]),
        Spec(
            "size",
            "choice",
            "Relative effort",
            required=False,
            choices=("S", "M", "L"),
            default="M",
        ),
    )


@dataclass
class Plan(_Model):
    prompt: str
    checks: list[str]
    tasks: list[Task]
    setup: list[str] = field(default_factory=list)
    contracts: list[Contract] = field(default_factory=list)

    SPECS: ClassVar[tuple[Spec, ...]] = (
        Spec("prompt", "text", "The project prompt this plan implements", min_len=1),
        Spec("checks", "texts", "Project-wide commands that define green, run after every merge"),
        Spec(
            "setup",
            "texts",
            "Commands that prepare a fresh checkout before checks run, e.g. 'pnpm install' or 'uv sync'",
            required=False,
            default=[],
        ),
        Spec(
            "contracts",
            "models",
            "Interfaces tasks publish",
            required=False,
            model=Contract,
            default=[],
        ),
        Spec("tasks", "models", "The tasks", model=Task),
    )

    @classmethod
    def from_dict(cls, data: Any) -> "Plan":
        """Build a plan, raising PlanError with every shape problem found."""
        plan, issues = parse(data)
        if plan is None:
            raise PlanError(issues)
        return plan

    @classmethod
    def from_json(cls, text: str) -> "Plan":
        return cls.from_dict(json.loads(text))

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True)


def parse(data: Any) -> tuple[Plan | None, list[Issue]]:
    issues: list[Issue] = []
    plan = Plan.build(data, (), issues)
    return (plan, []) if plan is not None and not issues else (None, issues)


# --- plan.yaml ----------------------------------------------------------------------


def to_yaml(plan: Plan) -> str:
    data = plan.to_dict()
    ordered: dict[str, Any] = {"prompt": data["prompt"], "checks": data["checks"]}
    if data["setup"]:
        ordered["setup"] = data["setup"]
    if data["contracts"]:
        ordered["contracts"] = data["contracts"]
    tasks = []
    for task in data["tasks"]:
        tasks.append(
            {k: v for k, v in task.items() if v or k not in ("after", "produces", "consumes")}
        )
    ordered["tasks"] = tasks
    body = str(yaml.safe_dump(ordered, sort_keys=False, allow_unicode=True, width=100))
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


def _line_index(node: Any, path: Path = ()) -> dict[Path, int]:
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
