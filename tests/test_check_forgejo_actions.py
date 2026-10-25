import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest
import requests
from requests.models import Response

from check_forgejo_actions import CheckForgejoActions, ForgejoAPIError, main

SERVER = "forgejo.example.org"
TOKEN = "SUPERSECRET"
NOW = datetime(2026, 10, 25, 15, 15, tzinfo=timezone.utc)
FIXTURES = Path(__file__).parent / "ApiResponses"


def fixture(path: str) -> dict:
    return json.loads((FIXTURES / path).read_text())


def make_check(**kwargs) -> CheckForgejoActions:
    defaults = {"server": SERVER, "token": TOKEN, "owner": "docker", "verbose": True}
    defaults.update(kwargs)
    check = CheckForgejoActions(**defaults)
    check.get_current_time = MagicMock(return_value=NOW)
    return check


def test_main_constructs_and_runs_checker() -> None:
    args = [
        "check_forgejo_actions.py",
        "--server",
        SERVER,
        "--token",
        TOKEN,
        "--owner",
        "docker",
        "--warning-seconds",
        "3600",
        "--critical-seconds",
        "7200",
        "--verbose",
    ]
    with (
        patch("sys.argv", args),
        patch("check_forgejo_actions.CheckForgejoActions") as checker,
    ):
        main()
    checker.assert_called_once_with(SERVER, TOKEN, "docker", 3600, 7200, True)
    checker.return_value.check_actions.assert_called_once_with()


@pytest.mark.parametrize(
    ("server", "expected"),
    [
        (SERVER, f"https://{SERVER}"),
        (f"http://{SERVER}/forgejo/", f"http://{SERVER}/forgejo"),
    ],
)
def test_server_normalization(server: str, expected: str) -> None:
    assert CheckForgejoActions(server, TOKEN).server == expected


@pytest.mark.parametrize(
    ("warning", "critical"), [(0, None), (None, -1), (60, 60), (120, 60)]
)
def test_invalid_thresholds(warning: int | None, critical: int | None) -> None:
    with pytest.raises(ValueError):
        CheckForgejoActions(
            SERVER, TOKEN, warning_seconds=warning, critical_seconds=critical
        )


def test_get_all_repos_paginates() -> None:
    check = make_check()
    page = fixture("user/repos/200.json")
    check.PAGE_SIZE = len(page)
    check.api_call = MagicMock(side_effect=[page, []])
    assert check.get_all_repos() == page
    check.api_call.assert_has_calls(
        [
            call("user/repos", {"page": 1, "limit": len(page)}),
            call("user/repos", {"page": 2, "limit": len(page)}),
        ]
    )


@pytest.mark.parametrize("slug", [None, "", 123])
def test_check_actions_rejects_missing_or_invalid_slug(slug, capsys) -> None:
    check = make_check()
    check.get_all_repos = MagicMock(return_value=[{"full_name": slug}])
    check.get_latest_task = MagicMock()
    with pytest.raises(SystemExit) as exit_info:
        check.check_actions()
    assert exit_info.value.code == 3
    assert "repository response is missing full_name" in capsys.readouterr().out
    check.get_latest_task.assert_not_called()


def task(
    workflow_id: str = "build.yml",
    run_number: int = 12,
    status: str = "success",
    name: str = "Build",
) -> dict:
    return {
        "workflow_id": workflow_id,
        "run_number": run_number,
        "status": status,
        "name": name,
        "created_at": "2026-01-02T10:00:00Z",
    }


def test_get_latest_tasks_empty_response() -> None:
    check = make_check()
    check.api_call = MagicMock(
        return_value=fixture("repos/docker/test-2/actions/tasks/200.json")
    )
    assert check.get_latest_task("docker/test-2") is None


@pytest.mark.parametrize(
    "data",
    [
        [],
        {},
        {"workflow_runs": None},
        {"workflow_runs": [None]},
    ],
)
def test_get_latest_tasks_rejects_malformed_responses(data) -> None:
    check = make_check()
    check.api_call = MagicMock(return_value=data)
    with pytest.raises(ForgejoAPIError):
        check.get_latest_task("docker/test-1")


def test_get_latest_tasks_quotes_path_components() -> None:
    check = make_check()
    check.api_call = MagicMock(return_value={"workflow_runs": []})
    assert check.get_latest_task("a b/c d") is None
    check.api_call.assert_called_once_with(
        "repos/a%20b/c%20d/actions/tasks", {"page": 1, "limit": 1}
    )


def test_get_json_sends_forgejo_auth_and_accepts_subpath() -> None:
    response = Response()
    response.status_code = 200
    response._content = b'{"workflow_runs": [], "total_count": 0}'
    with patch("requests.get", return_value=response) as get:
        data = CheckForgejoActions(f"https://{SERVER}/git", TOKEN).api_call(
            "repos/a/b/actions/tasks"
        )
    assert data == {"workflow_runs": [], "total_count": 0}
    get.assert_called_once_with(
        f"https://{SERVER}/git/api/v1/repos/a/b/actions/tasks",
        headers={"Authorization": f"token {TOKEN}", "Accept": "application/json"},
        params=None,
        timeout=15,
    )


@pytest.mark.parametrize("status_code", [401, 404, 500])
def test_get_json_rejects_http_errors(status_code: int) -> None:
    response = Response()
    response.status_code = status_code
    with patch("requests.get", return_value=response), pytest.raises(ForgejoAPIError):
        make_check().api_call("user/repos")


def test_get_json_rejects_network_and_malformed_responses() -> None:
    with (
        patch("requests.get", side_effect=requests.ConnectionError("offline")),
        pytest.raises(ForgejoAPIError),
    ):
        make_check().api_call("user/repos")

    response = Response()
    response.status_code = 200
    response._content = b"not json"
    with patch("requests.get", return_value=response), pytest.raises(ForgejoAPIError):
        make_check().api_call("user/repos")


@pytest.mark.parametrize(
    ("run_status", "expected_status"),
    [
        ("success", "OK"),
        ("cancelled", "OK"),
        ("skipped", "OK"),
        ("failure", "CRITICAL"),
        ("failed", "UNKNOWN"),
    ],
)
def test_evaluate_completed_run_statuses(run_status: str, expected_status: str) -> None:
    check = make_check()
    run = fixture("repos/docker/test-1/actions/tasks/200.json")["workflow_runs"][0]
    run["status"] = run_status
    status, message = check.evaluate_run("docker/test-1", run)
    assert status == expected_status
    assert "docker/test-1: build-push-image" in message


def test_evaluate_run_matches_real_tasks_response() -> None:
    check = make_check()
    check.api_call = MagicMock(
        return_value=fixture("repos/docker/test-1/actions/tasks/200.json")
    )
    run = check.get_latest_task("docker/test-1")
    assert check.evaluate_run("docker/test-1", run) == (
        "OK",
        "docker/test-1: build-push-image success",
    )


@pytest.mark.parametrize("run", [{}, {"status": "completed"}])
def test_run_without_recognized_status_is_unknown(run: dict) -> None:
    assert make_check().evaluate_run("docker/test-1", run)[0] == "UNKNOWN"


@pytest.mark.parametrize(
    ("status", "age_hours", "expected"),
    [
        ("queued", 1, "OK"),
        ("waiting", 2, "WARNING"),
        ("pending", 4, "CRITICAL"),
        ("requested", 2, "WARNING"),
        ("in_progress", 4, "CRITICAL"),
        ("success", 1, "OK"),
        ("success", 999, "OK"),
    ],
)
def test_active_run_thresholds(status: str, age_hours: int, expected: str) -> None:
    check = make_check(warning_seconds=3600, critical_seconds=10800)
    run = fixture("repos/docker/test-1/actions/tasks/200.json")["workflow_runs"][0]
    run["status"] = status
    run["created_at"] = f"2026-10-25T{15 - age_hours:02d}:15:00Z"
    assert check.evaluate_run("docker/test-1", run)[0] == expected


def test_active_run_without_thresholds_is_ok() -> None:
    check = make_check()
    run = fixture("repos/docker/test-1/actions/tasks/200.json")["workflow_runs"][0]
    assert check.evaluate_run("docker/test-1", run)[0] == "OK"


def test_unknown_status_and_bad_timestamp_are_unknown() -> None:
    check = make_check(warning_seconds=60)
    run = fixture("repos/docker/test-1/actions/tasks/200.json")["workflow_runs"][0]
    run["status"] = "surprising"
    assert check.evaluate_run("docker/test-1", run)[0] == "UNKNOWN"
    run["status"] = "queued"
    run["created_at"] = "yesterday"
    assert check.evaluate_run("docker/test-1", run)[0] == "UNKNOWN"


def test_check_actions_reports_healthy_workflow_and_repository_counts(capsys) -> None:
    check = make_check(warning_seconds=3600)
    check.api_call = MagicMock(
        side_effect=[
            fixture("user/repos/200.json"),
            fixture("repos/docker/test-1/actions/tasks/200.json"),
            fixture("repos/docker/test-1/actions/tasks/200.json"),
        ]
    )

    with pytest.raises(SystemExit) as exit_info:
        check.check_actions()
    assert capsys.readouterr().out == (
        "OK - 2 action run(s) healthy across 2 repositories\n"
    )
    assert exit_info.value.code == 0


def test_check_actions_ignores_workflows_and_repositories_without_tasks(capsys) -> None:
    check = make_check(warning_seconds=3600)
    check.api_call = MagicMock(
        side_effect=[
            fixture("user/repos/200.json"),
            fixture("repos/docker/test-1/actions/tasks/200.json"),
            fixture("repos/docker/test-2/actions/tasks/200.json"),
        ]
    )
    with pytest.raises(SystemExit) as exit_info:
        check.check_actions()
    assert capsys.readouterr().out == (
        "OK - 1 action run(s) healthy across 2 repositories\n"
    )
    assert exit_info.value.code == 0


def test_check_actions_task_with_failure(capsys) -> None:
    check = make_check(warning_seconds=3600)
    check.api_call = MagicMock(
        side_effect=[
            fixture("user/repos/201.json"),
            fixture("repos/docker/test-1/actions/tasks/200.json"),
            fixture("repos/docker/test-2/actions/tasks/200.json"),
            fixture("repos/docker/test-3/actions/tasks/200.json"),
        ]
    )
    with pytest.raises(SystemExit) as exit_info:
        check.check_actions()
    assert capsys.readouterr().out == (
        "CRITICAL - docker/test-3: build-push-image failed\n"
    )
    assert exit_info.value.code == 2

def test_check_actions_task_with_multiple_failures(capsys) -> None:
    check = make_check(warning_seconds=3600)
    check.api_call = MagicMock(
        side_effect=[
            fixture("user/repos/201.json"),
            fixture("repos/docker/test-1/actions/tasks/200.json"),
            fixture("repos/docker/test-3/actions/tasks/200.json"),
            fixture("repos/docker/test-3/actions/tasks/200.json"),
        ]
    )
    with pytest.raises(SystemExit) as exit_info:
        check.check_actions()
    assert capsys.readouterr().out == (
        "CRITICAL - docker/test-2: build-push-image failed; docker/test-3: build-push-image failed\n"
    )
    assert exit_info.value.code == 2


def test_check_actions_task_with_running_task(capsys) -> None:
    check = make_check(warning_seconds=3600)
    check.api_call = MagicMock(
        side_effect=[
            fixture("user/repos/202.json"),
            fixture("repos/docker/test-1/actions/tasks/200.json"),
            fixture("repos/docker/test-2/actions/tasks/200.json"),
            fixture("repos/docker/test-4/actions/tasks/200.json"),
        ]
    )
    with pytest.raises(SystemExit) as exit_info:
        check.check_actions()
    assert capsys.readouterr().out == (
        "OK - 2 action run(s) healthy across 3 repositories\n"
    )
    assert exit_info.value.code == 0


def test_check_actions_task_with_running_task_overdue(capsys) -> None:
    check = make_check(warning_seconds=3600)
    running = fixture("repos/docker/test-4/actions/tasks/200.json")
    running["workflow_runs"][0]["created_at"] = "2026-10-23T10:00:00Z"
    check.api_call = MagicMock(
        side_effect=[
            fixture("user/repos/202.json"),
            fixture("repos/docker/test-1/actions/tasks/200.json"),
            fixture("repos/docker/test-2/actions/tasks/200.json"),
            running,
        ]
    )
    with pytest.raises(SystemExit) as exit_info:
        check.check_actions()
    assert capsys.readouterr().out == (
        "WARNING - docker/test-4: build-push-image running for 2 days\n"
    )
    assert exit_info.value.code == 1

def test_check_actions_task_with_running_task_overdue_critical(capsys) -> None:
    check = make_check(warning_seconds=3600, critical_seconds=7200)
    running = fixture("repos/docker/test-4/actions/tasks/200.json")
    running["workflow_runs"][0]["created_at"] = "2026-10-23T10:00:00Z"
    check.api_call = MagicMock(
        side_effect=[
            fixture("user/repos/202.json"),
            fixture("repos/docker/test-1/actions/tasks/200.json"),
            fixture("repos/docker/test-2/actions/tasks/200.json"),
            running,
        ]
    )
    with pytest.raises(SystemExit) as exit_info:
        check.check_actions()
    assert capsys.readouterr().out == (
        "CRITICAL - docker/test-4: build-push-image running for 2 days\n"
    )
    assert exit_info.value.code == 2

def test_check_actions_task_with_running_task_overdue_and_a_failure(capsys) -> None:
    check = make_check(warning_seconds=3600)
    running = fixture("repos/docker/test-4/actions/tasks/200.json")
    running["workflow_runs"][0]["created_at"] = "2026-10-23T10:00:00Z"
    check.api_call = MagicMock(
        side_effect=[
            fixture("user/repos/202.json"),
            fixture("repos/docker/test-1/actions/tasks/200.json"),
            fixture("repos/docker/test-3/actions/tasks/200.json"),
            running,
        ]
    )
    with pytest.raises(SystemExit) as exit_info:
        check.check_actions()
    assert capsys.readouterr().out == (
        "CRITICAL - docker/test-2: build-push-image failed\n"
    )
    assert exit_info.value.code == 2


def test_check_actions_without_tasks_is_unknown(capsys) -> None:
    check = make_check()
    check.get_all_repos = MagicMock(
        return_value=[
            {"full_name": "docker/empty"},
        ]
    )
    check.get_latest_task = MagicMock(return_value=None)
    with pytest.raises(SystemExit) as exit_info:
        check.check_actions()
    assert capsys.readouterr().out == "UNKNOWN - Could not find any actions to check\n"
    assert exit_info.value.code == 3


@pytest.mark.parametrize(
    ("status", "code"),
    [
        ("success", 0),
        ("failure", 2),
        ("running", 1),
        ("unknown", 3),
    ],
)
def test_check_actions_reads_tasks_endpoint(status: str, code: int, capsys) -> None:
    check = make_check(warning_seconds=3600)
    response = Response()
    response.status_code = 200
    response._content = json.dumps(
        {"workflow_runs": [task(status=status)], "total_count": 1}
    ).encode()
    check.get_all_repos = MagicMock(
        return_value=[
            {"full_name": "docker/test-1"},
        ]
    )
    with patch("requests.get", return_value=response) as get:
        with pytest.raises(SystemExit) as exit_info:
            check.check_actions()
    assert exit_info.value.code == code
    assert get.call_args.args == (
        f"https://{SERVER}/api/v1/repos/docker/test-1/actions/tasks",
    )
    assert capsys.readouterr().out


def test_repository_api_failure_is_unknown(capsys) -> None:
    check = make_check()
    check.get_all_repos = MagicMock(return_value=[fixture("user/repos/200.json")[0]])
    check.get_latest_task = MagicMock(side_effect=ForgejoAPIError("offline"))
    with pytest.raises(SystemExit) as exit_info:
        check.check_actions()
    assert exit_info.value.code == 3
    assert "offline" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("status", "code"), [("OK", 0), ("WARNING", 1), ("CRITICAL", 2), ("UNKNOWN", 3)]
)
def test_nagios_exit(status: str, code: int, capsys) -> None:
    with pytest.raises(SystemExit) as exit_info:
        CheckForgejoActions.nagios_exit(status, "Test")
    assert exit_info.value.code == code
    assert capsys.readouterr().out == f"{status} - Test\n"
