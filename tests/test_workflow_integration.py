import io
import json
from pathlib import Path

from agent.cli import main
from models.migration_plan import MigrationPhase


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def snapshot(repository: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(repository)): path.read_bytes()
        for path in repository.rglob("*")
        if path.is_file()
    }


def test_workflow_matches_inspect_and_does_not_modify_fixture(
    tmp_path: Path,
) -> None:
    repository = (
        PROJECT_ROOT
        / "examples"
        / "go-gin-example"
    )
    before = snapshot(repository)
    inspect_output = tmp_path / "inspect.json"
    workflow_output = tmp_path / "workflow.json"
    second_workflow_output = tmp_path / "workflow-second.json"

    inspect_exit = main(
        ["inspect", "--repo", str(repository), "--output", str(inspect_output)],
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )
    workflow_exit = main(
        [
            "workflow",
            "inspect",
            "--repo",
            str(repository),
            "--output",
            str(workflow_output),
            "--checkpoint-memory",
        ],
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )
    second_exit = main(
        [
            "workflow",
            "inspect",
            "--repo",
            str(repository),
            "--output",
            str(second_workflow_output),
        ],
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )

    assert inspect_exit == workflow_exit == second_exit == 0
    direct = json.loads(inspect_output.read_text(encoding="utf-8"))
    workflow = json.loads(workflow_output.read_text(encoding="utf-8"))
    second = json.loads(second_workflow_output.read_text(encoding="utf-8"))
    for key in (
        "repository_summary",
        "discovered_gin_usages",
        "exact_migrations",
        "migrations_requiring_review",
        "unsupported_usages",
        "unmatched_usages",
        "ordered_migration_tasks",
    ):
        assert workflow[key] == direct[key]
    assert workflow == second
    assert workflow["final_status"] == "success"
    assert [task["phase"] for task in workflow["ordered_migration_tasks"]] == [
        phase.value for phase in MigrationPhase
    ]
    assert snapshot(repository) == before
