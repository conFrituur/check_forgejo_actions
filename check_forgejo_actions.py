#!/usr/bin/env python3

import argparse
import logging
from datetime import datetime, timezone
from typing import NoReturn
from urllib.parse import quote, urlparse

import requests


class ForgejoAPIError(RuntimeError):
    """Raised when Forgejo cannot provide a usable API response."""


class CheckForgejoActions:
    PAGE_SIZE = 200
    REQUEST_TIMEOUT = 15
    ACTIVE_STATUSES = {"queued", "waiting", "pending", "requested", "in_progress"}
    OK_CONCLUSIONS = {"success", "cancelled", "skipped"}
    SEVERITY = {"OK": 0, "UNKNOWN": 1, "WARNING": 2, "CRITICAL": 3}

    def __init__(
        self,
        server: str,
        token: str,
        owner: str = "",
        warning_seconds: int | None = None,
        critical_seconds: int | None = None,
        verbose: bool = False,
    ) -> None:
        self.server = self.normalize_server(server)
        self.token = token
        self.owner = owner
        self.warning_seconds = warning_seconds
        self.critical_seconds = critical_seconds
        self.validate_thresholds()

        self.log = logging.getLogger(__name__)
        if not self.log.handlers:
            self.log.addHandler(logging.StreamHandler())
        self.log.setLevel(logging.DEBUG if verbose else logging.CRITICAL)

    @staticmethod
    def normalize_server(server: str) -> str:
        server = server.strip()
        if "://" not in server:
            server = f"https://{server}"
        parsed = urlparse(server)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("server must be a hostname or an HTTP(S) base URL")
        if parsed.query or parsed.fragment:
            raise ValueError("server URL must not contain a query string or fragment")
        return server.rstrip("/")

    def validate_thresholds(self) -> None:
        for name, value in (
            ("warning-seconds", self.warning_seconds),
            ("critical-seconds", self.critical_seconds),
        ):
            if value is not None and value <= 0:
                raise ValueError(f"{name} must be greater than zero")
        if (
            self.warning_seconds is not None
            and self.critical_seconds is not None
            and self.warning_seconds >= self.critical_seconds
        ):
            raise ValueError("warning-seconds must be lower than critical-seconds")

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"token {self.token}", "Accept": "application/json"}

    def api_call(self, path: str, params: dict[str, int] | None = None) -> object:
        url = f"{self.server}/api/v1/{path.lstrip('/')}"
        try:
            response = requests.get(
                url,
                headers=self.headers,
                params=params,
                timeout=self.REQUEST_TIMEOUT,
            )
        except requests.RequestException as error:
            raise ForgejoAPIError(f"Forgejo API request failed for {path}: {error}") from error

        if response.status_code != 200:
            raise ForgejoAPIError(
                f"Forgejo API {path} returned HTTP {response.status_code}"
            )
        try:
            data = response.json()
        except ValueError as error:
            raise ForgejoAPIError(
                f"Forgejo API {path} did not return valid JSON"
            ) from error
        self.log.debug("Forgejo API %s response: %r", path, data)
        return data

    def get_all_repos(self) -> list[dict]:
        repos: list[dict] = []
        page = 1
        while True:
            data = self.api_call(
                "user/repos", {"page": page, "limit": self.PAGE_SIZE}
            )
            if not isinstance(data, list) or not all(isinstance(repo, dict) for repo in data):
                raise ForgejoAPIError("Forgejo API user/repos returned an unexpected schema")
            repos.extend(data)
            if len(data) < self.PAGE_SIZE:
                return repos
            page += 1

    def get_workflows(self, owner: str, repo: str) -> list[dict]:
        workflows: list[dict] = []
        page = 1
        path = f"repos/{quote(owner, safe='')}/{quote(repo, safe='')}/actions/workflows"
        while True:
            data = self.api_call(path, {"page": page, "limit": self.PAGE_SIZE})
            if not isinstance(data, dict) or not isinstance(data.get("workflows"), list):
                raise ForgejoAPIError(f"Forgejo API {path} returned an unexpected schema")
            entries = data["workflows"]
            if not all(isinstance(workflow, dict) for workflow in entries):
                raise ForgejoAPIError(f"Forgejo API {path} returned an unexpected schema")
            workflows.extend(entries)
            total = data.get("total_count")
            if len(entries) < self.PAGE_SIZE or (
                isinstance(total, int) and len(workflows) >= total
            ):
                return workflows
            page += 1

    def get_latest_run(self, owner: str, repo: str, workflow_id: str) -> dict | None:
        path = (
            f"repos/{quote(owner, safe='')}/{quote(repo, safe='')}/actions/"
            f"workflows/{quote(str(workflow_id), safe='')}/runs"
        )
        data = self.api_call(path, {"page": 1, "limit": 1})
        if not isinstance(data, dict) or not isinstance(data.get("workflow_runs"), list):
            raise ForgejoAPIError(f"Forgejo API {path} returned an unexpected schema")
        runs = data["workflow_runs"]
        if not runs:
            return None
        if not isinstance(runs[0], dict):
            raise ForgejoAPIError(f"Forgejo API {path} returned an unexpected schema")
        return runs[0]

    @staticmethod
    def get_current_time() -> datetime:
        return datetime.now(timezone.utc)

    def run_age_seconds(self, run: dict) -> int:
        created_at = run.get("created_at")
        if not isinstance(created_at, str):
            raise ValueError("missing created_at")
        try:
            created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("invalid created_at") from error
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return max(0, int((self.get_current_time() - created).total_seconds()))

    @staticmethod
    def duration(seconds: int) -> str:
        if seconds < 60:
            return f"{seconds}s"
        if seconds < 3600:
            return f"{seconds // 60}m"
        if seconds < 86400:
            return f"{seconds // 3600}h"
        return f"{seconds // 86400}d"

    def evaluate_run(self, slug: str, workflow: dict, run: dict) -> tuple[str, str]:
        workflow_name = workflow.get("name") or workflow.get("id") or "unknown workflow"
        run_number = run.get("run_number", "?")
        label = f"{slug}: {workflow_name} (run #{run_number})"
        status = run.get("status")
        conclusion = run.get("conclusion")

        if status == "completed":
            if conclusion == "failure":
                return "CRITICAL", f"{label} failed"
            if conclusion in self.OK_CONCLUSIONS:
                return "OK", f"{label} {conclusion}"
            return "UNKNOWN", f"{label} has unknown conclusion {conclusion!r}"

        if status in self.ACTIVE_STATUSES:
            try:
                age = self.run_age_seconds(run)
            except ValueError as error:
                return "UNKNOWN", f"{label} has {error}"
            detail = f"{label} {status} for {self.duration(age)}"
            if self.critical_seconds is not None and age > self.critical_seconds:
                return "CRITICAL", detail
            if self.warning_seconds is not None and age > self.warning_seconds:
                return "WARNING", detail
            return "OK", detail

        return "UNKNOWN", f"{label} has unknown status {status!r}"

    def check_actions(self) -> None:
        try:
            repos = self.get_all_repos()
        except ForgejoAPIError as error:
            self.nagios_exit("UNKNOWN", str(error))

        results: list[tuple[str, str]] = []
        checked_repos: set[str] = set()
        for repo in repos:
            try:
                owner_data = repo.get("owner")
                owner = owner_data.get("login") if isinstance(owner_data, dict) else None
                name = repo.get("name")
                if not isinstance(owner, str) or not isinstance(name, str):
                    raise ForgejoAPIError("repository response is missing owner.login or name")
                if self.owner and owner != self.owner:
                    continue
                slug = repo.get("full_name") or f"{owner}/{name}"
                workflows = self.get_workflows(owner, name)
                active_workflows = [w for w in workflows if w.get("state") == "active"]
                for workflow in active_workflows:
                    workflow_id = workflow.get("id")
                    if workflow_id is None:
                        results.append(("UNKNOWN", f"{slug}: workflow is missing id"))
                        continue
                    run = self.get_latest_run(owner, name, str(workflow_id))
                    if run is None:
                        continue
                    checked_repos.add(str(slug))
                    results.append(self.evaluate_run(str(slug), workflow, run))
            except ForgejoAPIError as error:
                slug = repo.get("full_name", repo.get("name", "unknown repository"))
                results.append(("UNKNOWN", f"{slug}: {error}"))

        if not results:
            self.nagios_exit("OK", "No Forgejo Actions runs found")

        status = max(results, key=lambda result: self.SEVERITY[result[0]])[0]
        affected = [message for level, message in results if level == status]
        if status == "OK":
            workflow_count = len(results)
            self.nagios_exit(
                "OK",
                f"{workflow_count} latest action run(s) healthy across "
                f"{len(checked_repos)} repository/repositories",
            )
        self.nagios_exit(status, "; ".join(affected))

    @staticmethod
    def nagios_exit(status: str, message: str) -> NoReturn:
        codes = {"OK": 0, "WARNING": 1, "CRITICAL": 2, "UNKNOWN": 3}
        print(f"{status} - {message}")
        raise SystemExit(codes[status])


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Check Forgejo workflows for failed or stuck actions"
    )
    required = parser.add_argument_group("required arguments")
    required.add_argument(
        "--server",
        "-s",
        metavar="<FORGEJO_SERVER>",
        required=True,
        help="Forgejo hostname or HTTP(S) base URL",
    )
    required.add_argument(
        "--token",
        "-t",
        metavar="<FORGEJO_TOKEN>",
        required=True,
        help="Forgejo API token",
    )
    parser.add_argument(
        "--owner", "-o", default="", help="only check repositories owned by this user or organization"
    )
    parser.add_argument(
        "--warning-seconds", "-w", type=int, help="warn when an active run is older than this"
    )
    parser.add_argument(
        "--critical-seconds", "-c", type=int, help="fail when an active run is older than this"
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    try:
        check = CheckForgejoActions(
            args.server,
            args.token,
            args.owner,
            args.warning_seconds,
            args.critical_seconds,
            args.verbose,
        )
    except ValueError as error:
        parser.error(str(error))
    check.check_actions()


if __name__ == "__main__":  # pragma: no cover
    main()
