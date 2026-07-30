import io
import json
from pathlib import Path

from agent.cli import main
from models.migration_plan import MigrationPhase


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def repository_snapshot(repository: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(repository)): path.read_bytes()
        for path in repository.rglob("*")
        if path.is_file()
    }


def test_inspect_fixture_is_deterministic_and_read_only(tmp_path: Path) -> None:
    repository = (
        PROJECT_ROOT
        / "go-analyzer"
        / "analyzer"
        / "testdata"
        / "gin-positive"
    )
    before = repository_snapshot(repository)
    first_output = tmp_path / "first.json"
    second_output = tmp_path / "second.json"

    first_exit = main(
        ["inspect", "--repo", str(repository), "--output", str(first_output)],
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )
    second_exit = main(
        ["inspect", "--repo", str(repository), "--output", str(second_output)],
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )

    assert first_exit == second_exit == 0
    first = json.loads(first_output.read_text(encoding="utf-8"))
    second = json.loads(second_output.read_text(encoding="utf-8"))
    assert first == second
    assert len(first["discovered_gin_usages"]) > 0
    classified = sum(
        len(first[key])
        for key in (
            "exact_migrations",
            "migrations_requiring_review",
            "unsupported_usages",
            "unmatched_usages",
        )
    )
    api_usages = [
        usage
        for usage in first["discovered_gin_usages"]
        if usage["kind"] != "gin_import"
    ]
    assert classified == len(api_usages)
    assert not any(
        item["usage"]["kind"] == "gin_import"
        for item in first["unmatched_usages"]
    )
    assert first["unmatched_usages"] == []
    primary_ids = [
        usage_id
        for task in first["ordered_migration_tasks"]
        if task["task_kind"] == "primary"
        for usage_id in task["direct_usage_ids"]
    ]
    assert len(primary_ids) == len(set(primary_ids))
    assert set(primary_ids) == {usage["usage_id"] for usage in api_usages}
    assert first["ordered_migration_tasks"][0]["direct_usage_ids"] == []
    assert [task["phase"] for task in first["ordered_migration_tasks"]] == [
        phase.value for phase in MigrationPhase
    ]
    assert repository_snapshot(repository) == before
