from pathlib import Path

from barnraise import server
from barnraise.plan import Plan
from tests.conftest import plan, sample


def test_tools_need_init(repo: Path) -> None:
    assert server.status()["initialized"] is False
    assert server.propose_plan(plan())["errors"][0]["code"] == "not_initialized"
    assert server.init_project(checks=[])["ok"] is True
    assert server.status()["initialized"] is True


def test_propose_then_edit_and_sync(repo: Path) -> None:
    server.init_project(checks=["pnpm test"])
    bad = server.propose_plan(Plan.from_dict(sample(checks=[])))
    assert bad["ok"] is False and bad["errors"][0]["code"] == "no_checks"
    assert server.get_plan()["ok"] is False  # nothing stored

    good = server.propose_plan(plan())
    assert good["ok"] is True and good["version"] == 1
    assert good["analysis"]["critical_path"] == ["T0", "T1", "T2", "T3"]
    plan_file = repo / ".barnraise" / "plan.yaml"
    assert server.status()["plan"]["unsynced_edits"] is False

    # Unchanged file: sync is a no-op.
    assert server.sync_plan() == {"ok": True, "changed": False, "version": 1}

    # A valid edit becomes version 2; the user's comments are kept.
    text = plan_file.read_text().replace("title: Tasks UI", "title: Tasks web UI  # renamed")
    plan_file.write_text(text)
    assert server.status()["plan"]["unsynced_edits"] is True
    synced = server.sync_plan()
    assert synced["changed"] is True and synced["version"] == 2
    assert server.get_plan()["yaml"].count("Tasks web UI") == 1
    assert plan_file.read_text() == text

    # An invalid edit is rejected with its line, and nothing is stored.
    plan_file.write_text(text.replace("- T2\n", "- T7\n"))
    rejected = server.sync_plan()
    assert rejected["ok"] is False
    [err] = rejected["errors"]
    assert err["message"] == "T3 depends on T7, which is not in the plan"
    assert plan_file.read_text().splitlines()[err["line"] - 1].strip() == "- T7"
    assert server.get_plan()["version"] == 2


def test_propose_keeps_unsynced_edits_as_backup(repo: Path) -> None:
    server.init_project(checks=[])
    server.propose_plan(plan())
    plan_file = repo / ".barnraise" / "plan.yaml"
    plan_file.write_text(plan_file.read_text() + "# my notes\n")
    out = server.propose_plan(plan())
    assert "unsynced edits" in out["note"]
    assert (repo / ".barnraise" / "plan.yaml.bak").read_text().endswith("# my notes\n")


async def test_tools_are_registered() -> None:
    names = {t.name for t in await server.mcp.list_tools()}
    assert names == {
        "status", "init_project", "propose_plan", "get_plan", "sync_plan", "approve_plan",
        "start_run", "next_actions", "wait_for_change", "answer_blocker", "control",
        "claim_task", "publish_contract", "read_contract", "post_message", "read_messages",
        "report_blocker", "complete_task",
    }  # fmt: skip
