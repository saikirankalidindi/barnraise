from typing import Any

from barnraise.validate import analyse, completion_reachability, validate
from tests.conftest import plan, sample


def codes(**overrides: Any) -> list[str]:
    return [i.code for i in validate(plan(**overrides))]


def messages(**overrides: Any) -> list[str]:
    return [i.message for i in validate(plan(**overrides))]


def tasks(**changes: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = sample()["tasks"]
    for t in out:
        t.update(changes.get(t["key"], {}))
    return out


def test_sample_is_valid() -> None:
    assert validate(plan()) == []


def test_cycle_is_named() -> None:
    issues = validate(plan(tasks=tasks(T0={"after": ["T3"]})))
    cycle = [i for i in issues if i.code == "cycle"]
    assert len(cycle) == 1
    assert cycle[0].message == "dependency cycle: T0 -> T1 -> T2 -> T3 -> T0"


def test_unproduced_contract() -> None:
    issues = validate(plan(tasks=tasks(T2={"consumes": ["tasks-api", "auth-api"]})))
    assert [(i.code, i.message, i.path) for i in issues] == [
        (
            "unproduced_contract",
            "T2 consumes auth-api, but no task produces it",
            ("tasks", 2, "consumes", 1),
        )
    ]


def test_declared_contract_without_producer() -> None:
    assert "contract tasks-api is declared but no task produces it" in messages(
        tasks=tasks(T1={"produces": []}, T2={"consumes": []})
    )


def test_overlapping_owned_paths_of_parallel_tasks() -> None:
    issues = validate(plan(tasks=tasks(T2={"owns": ["src/**"]})))
    assert [i.code for i in issues] == ["owned_path_overlap", "owned_path_overlap"]
    assert issues[0].message.startswith("T0 owns 'src/db/**' and T2 owns 'src/**'; they can run in")


def test_serialised_tasks_may_share_paths() -> None:
    # T3 runs after T1 is merged, so it may touch T1's files.
    assert validate(plan(tasks=tasks(T3={"owns": ["e2e/**", "src/api/fixtures.ts"]}))) == []


def test_contract_consumers_still_run_in_parallel() -> None:
    serial = completion_reachability(plan())
    assert serial["T0"] == set()  # T1 and T2 only wait for contracts
    assert serial["T1"] == {"T3"} and serial["T2"] == {"T3"}


def test_other_rules() -> None:
    assert codes(checks=[]) == ["no_checks"]
    assert codes(tasks=tasks(T1={"acceptance": [" "]})) == ["no_acceptance"]
    assert codes(tasks=tasks(T1={"after": ["T9"]})) == ["unknown_task"]
    assert codes(tasks=tasks(T1={"after": ["T1"]})) == ["self_dependency"]
    assert codes(tasks=tasks(T1={"owns": []})) == ["owns_nothing"]
    assert codes(tasks=tasks(T1={"owns": ["../other/**"]})) == ["path_outside_repo"]
    # Owning a shared file is wrong twice: only T0 may, and T0 already does.
    shared = codes(tasks=tasks(T1={"owns": ["src/api/**", "package.json"]}))
    assert shared == ["shared_file", "owned_path_overlap"]
    two = codes(tasks=tasks(T0={"produces": ["db-schema", "tasks-api"]}))
    assert two == ["multiple_producers"]
    extra = {**sample()["tasks"][3], "after": [], "owns": ["docs/**"]}
    assert codes(tasks=[*tasks(), extra]) == ["duplicate_key"]
    assert codes(tasks=tasks(T1={"consumes": ["db-schema", "tasks-api"]})) == ["self_contract"]


def test_analysis() -> None:
    a = analyse(plan())
    # T0 (S=1) runs 0-1 and publishes at 0.5; T1 (M=2) runs 0.5-2.5 and publishes at
    # 1.5; T2 (L=3) runs 1.5-4.5; T3 (S=1) runs 4.5-5.5. At most two overlap.
    assert a["critical_path"] == ["T0", "T1", "T2", "T3"]
    assert a["critical_path_length"] == 5.5
    assert a["total_effort"] == 7.0
    assert a["peak_parallel"] == 2
    assert a["parallel_pairs"] == 4  # all pairs except T1-T3 and T2-T3
