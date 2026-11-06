from barnraise import plan as planmod
from barnraise.validate import validate
from tests.conftest import plan


def test_round_trip() -> None:
    p = plan()
    text = planmod.to_yaml(p)
    assert text.startswith("# Barnraise plan.")
    assert "after: []" not in text  # empty lists are omitted
    parsed, issues = planmod.from_yaml(text)
    assert issues == [] and parsed == p


def test_schema_errors_carry_line_numbers() -> None:
    text = (
        planmod.to_yaml(plan())
        .replace("key: T2", "key: task-two")
        .replace("size: L", "size: XL\n  colour: red")
    )
    parsed, issues = planmod.from_yaml(text)
    assert parsed is None
    lines = text.splitlines()
    found = {i.message: i.line or 0 for i in issues}
    assert lines[found["'task-two' is not a valid key"] - 1].strip() == "- key: task-two"
    assert lines[found["unknown field 'colour'"] - 1].strip() == "colour: red"


def test_semantic_errors_map_to_lines() -> None:
    text = planmod.to_yaml(plan()).replace("- tasks-api\n  size: L", "- auth-api\n  size: L")
    parsed, issues = planmod.from_yaml(text)
    assert parsed is not None and issues == []
    index = planmod.line_index(text)
    [issue] = [
        planmod.with_line(i, index)
        for i in validate(parsed)
        if i.code == "unproduced_contract" and "auth" in i.message
    ]
    assert text.splitlines()[issue.line - 1].strip() == "- auth-api"  # type: ignore[operator]


def test_yaml_syntax_error() -> None:
    _, issues = planmod.from_yaml("prompt: x\ntasks: [\n")
    assert issues[0].code == "yaml" and issues[0].line is not None
