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
NOW = datetime(2026, 1, 2, 12, 0, tzinfo=timezone.utc)
FIXTURES = Path(__file__).parent / "ApiResponses"


def fixture(path: str) -> object:
    return json.loads((FIXTURES / path).read_text())


def make_check(**kwargs) -> CheckForgejoActions:
    defaults = {"server": SERVER, "token": TOKEN, "owner": "docker"}
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
    with patch("sys.argv", args), patch(
        "check_forgejo_actions.CheckForgejoActions"
    ) as checker:
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
        CheckForgejoActions(SERVER, TOKEN, warning_seconds=warning, critical_seconds=critical)


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


def test_get_workflows_and_latest_run_use_forgejo_wrappers() -> None:
    check = make_check()
    workflows = fixture("repos/docker/test-1/actions/workflows/200.json")
    runs = fixture("repos/docker/test-1/actions/workflows/build.yml/runs/200.json")
    check.api_call = MagicMock(side_effect=[workflows, runs])
    assert check.get_workflows("docker", "test-1") == workflows["workflows"]
    assert check.get_latest_run("docker", "test-1", "build.yml") == runs["workflow_runs"][0]


def test_get_json_sends_forgejo_auth_and_accepts_subpath() -> None:
    response = Response()
    response.status_code = 200
    response._content = b'{"workflows": [], "total_count": 0}'
    with patch("requests.get", return_value=response) as get:
        data = CheckForgejoActions(f"https://{SERVER}/git", TOKEN).api_call("repos/a/b/actions/workflows")
    assert data == {"workflows": [], "total_count": 0}
    get.assert_called_once_with(
        f"https://{SERVER}/git/api/v1/repos/a/b/actions/workflows",
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
    with patch("requests.get", side_effect=requests.ConnectionError("offline")), pytest.raises(
        ForgejoAPIError
    ):
        make_check().api_call("user/repos")

    response = Response()
    response.status_code = 200
    response._content = b"not json"
    with patch("requests.get", return_value=response), pytest.raises(ForgejoAPIError):
        make_check().api_call("user/repos")


@pytest.mark.parametrize(
    ("fixture_name", "expected_status"),
    [
        ("success.json", "OK"),
        ("cancelled.json", "OK"),
        ("skipped.json", "OK"),
        ("failure.json", "CRITICAL"),
    ],
)
def test_completed_run_statuses(fixture_name: str, expected_status: str) -> None:
    check = make_check()
    workflow = {"id": "build.yml", "name": "Build", "state": "active"}
    run = fixture(f"runs/{fixture_name}")
    status, message = check.evaluate_run("docker/test-1", workflow, run)
    assert status == expected_status
    assert "docker/test-1: Build (run #12)" in message


@pytest.mark.parametrize(
    ("status", "age_hours", "expected"),
    [
        ("queued", 1, "OK"),
        ("waiting", 2, "WARNING"),
        ("pending", 4, "CRITICAL"),
        ("requested", 2, "WARNING"),
        ("in_progress", 4, "CRITICAL"),
    ],
)
def test_active_run_thresholds(status: str, age_hours: int, expected: str) -> None:
    check = make_check(warning_seconds=3600, critical_seconds=10800)
    workflow = {"id": "build.yml", "name": "Build", "state": "active"}
    run = fixture("runs/active.json")
    run["status"] = status
    run["created_at"] = f"2026-01-02T{12 - age_hours:02d}:00:00Z"
    assert check.evaluate_run("docker/test-1", workflow, run)[0] == expected


def test_active_run_without_thresholds_is_ok() -> None:
    check = make_check()
    run = fixture("runs/active.json")
    assert check.evaluate_run("docker/test-1", {"name": "Build"}, run)[0] == "OK"


def test_unknown_status_and_bad_timestamp_are_unknown() -> None:
    check = make_check(warning_seconds=60)
    workflow = {"name": "Build"}
    run = fixture("runs/active.json")
    run["status"] = "surprising"
    assert check.evaluate_run("docker/test-1", workflow, run)[0] == "UNKNOWN"
    run["status"] = "queued"
    run["created_at"] = "yesterday"
    assert check.evaluate_run("docker/test-1", workflow, run)[0] == "UNKNOWN"


def test_check_actions_filters_owner_and_disabled_workflows() -> None:
    check = make_check()
    repos = fixture("user/repos/200.json")
    workflows = fixture("repos/docker/test-1/actions/workflows/200.json")["workflows"]
    run = fixture("runs/success.json")
    check.get_all_repos = MagicMock(return_value=repos)
    check.get_workflows = MagicMock(return_value=workflows)
    check.get_latest_run = MagicMock(return_value=run)

    with pytest.raises(SystemExit) as exit_info:
        check.check_actions()

    assert exit_info.value.code == 0
    check.get_workflows.assert_called_once_with("docker", "test-1")
    check.get_latest_run.assert_called_once_with("docker", "test-1", "build.yml")


def test_check_actions_aggregates_with_critical_precedence(capsys) -> None:
    check = make_check(warning_seconds=3600)
    check.get_all_repos = MagicMock(
        return_value=[
            {"name": "one", "full_name": "docker/one", "owner": {"login": "docker"}},
            {"name": "two", "full_name": "docker/two", "owner": {"login": "docker"}},
        ]
    )
    check.get_workflows = MagicMock(
        return_value=[{"id": "build.yml", "name": "Build", "state": "active"}]
    )
    check.get_latest_run = MagicMock(
        side_effect=[fixture("runs/active.json"), fixture("runs/failure.json")]
    )

    with pytest.raises(SystemExit) as exit_info:
        check.check_actions()

    assert exit_info.value.code == 2
    assert capsys.readouterr().out.startswith("CRITICAL - docker/two: Build")


def test_check_actions_skips_repositories_and_workflows_without_runs(capsys) -> None:
    check = make_check(owner="")
    check.get_all_repos = MagicMock(return_value=fixture("user/repos/200.json"))
    check.get_workflows = MagicMock(return_value=[])
    with pytest.raises(SystemExit) as exit_info:
        check.check_actions()
    assert exit_info.value.code == 0
    assert capsys.readouterr().out == "OK - No Forgejo Actions runs found\n"


def test_repository_api_failure_is_unknown(capsys) -> None:
    check = make_check()
    check.get_all_repos = MagicMock(return_value=[fixture("user/repos/200.json")[0]])
    check.get_workflows = MagicMock(side_effect=ForgejoAPIError("offline"))
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
