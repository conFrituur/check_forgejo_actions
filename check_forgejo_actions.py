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
    ACTIVE_STATUSES = {
        "queued",
        "waiting",
        "pending",
        "requested",
        "in_progress",
        "running",
        "blocked",
    }
    OK_STATUSES = {"success", "cancelled", "skipped"}
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

    def check_actions(self) -> None:
        try:
            repos = self.get_all_repos()
        except ForgejoAPIError as error:
            self.nagios_exit("UNKNOWN", str(error))

        self.log.debug("Discovered %d accessible repositories", len(repos))
        results: list[tuple[str, str]] = []
        checked_repos: int = 0
        for repo in repos:
            try:
                slug = repo.get("full_name")
                if not isinstance(slug, str) or not slug:
                    raise ForgejoAPIError("repository response is missing full_name")
                if repo.get("has_actions") is False:
                    self.log.debug("Skipping repository %s: actions are disabled", slug)
                    continue
                self.log.debug("Checking repository %s", slug)
                latest = self.get_latest_task(slug)
                checked_repos += 1
                if latest is not None:
                    results.append(self.evaluate_run(slug, latest))
            except ForgejoAPIError as error:
                slug = repo.get("full_name", repo.get("name", "unknown repository"))
                self.log.debug("Repository %s could not be checked: %s", slug, error)
                results.append(("UNKNOWN", f"{slug}: {error}"))

        if not results:
            self.nagios_exit("UNKNOWN", "Could not find any actions to check")

        status = max(results, key=lambda result: self.SEVERITY[result[0]])[0]
        affected = [message for level, message in results if level == status]
        counts = {
            level: sum(result_level == level for result_level, _ in results)
            for level in self.SEVERITY
        }
        self.log.debug(
            "Aggregated results: runs=%d counts=%s",
            len(results),
            counts,
        )
        if status == "OK":
            self.nagios_exit(
                "OK",
                f"{len(results)} action run(s) healthy across "
                f"{checked_repos} repositories",
            )
        self.nagios_exit(status, "; ".join(affected))

    def get_all_repos(self) -> list[dict]:
        repos: list[dict] = []
        page = 1
        while True:
            data = self.api_call("user/repos", {"page": page, "limit": self.PAGE_SIZE})
            if not isinstance(data, list) or not all(
                isinstance(repo, dict) for repo in data
            ):
                raise ForgejoAPIError(
                    "Forgejo API user/repos returned an unexpected schema"
                )
            repos.extend(data)
            self.log.debug(
                "Repository page %d: received=%d collected=%d",
                page,
                len(data),
                len(repos),
            )
            if len(data) < self.PAGE_SIZE:
                return repos
            page += 1

    def get_latest_task(self, slug: str) -> dict | None:
        """Fetch the latest action task for a repository."""
        path = f"repos/{quote(slug, safe='/')}/actions/tasks"
        data = self.api_call(path, {"page": 1, "limit": 1})

        if not isinstance(data, dict) or not isinstance(
            data.get("workflow_runs"), list
        ):
            raise ForgejoAPIError(f"Forgejo API {path} returned an unexpected schema")

        tasks = data["workflow_runs"]

        if not tasks:
            self.log.debug("%s has no tasks", slug)
            return None
        task = tasks[0]
        if not isinstance(task, dict):
            raise ForgejoAPIError(f"Forgejo API {path} returned an unexpected schema")
        return task

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"token {self.token}", "Accept": "application/json"}

    def api_call(self, path: str, params: dict[str, int] | None = None) -> object:
        url = f"{self.server}/api/v1/{path.lstrip('/')}"
        self.log.debug("API request: GET %s params=%s", url, params or {})
        try:
            response = requests.get(
                url,
                headers=self.headers,
                params=params,
                timeout=self.REQUEST_TIMEOUT,
            )
        except requests.RequestException as error:
            self.log.debug("API request failed: path=%s error=%s", path, error)
            raise ForgejoAPIError(
                f"Forgejo API request failed for {path}: {error}"
            ) from error

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
            return f"{seconds} secs"
        if seconds < 3600:
            return f"{seconds // 60} mins"
        if seconds < 86400:
            return f"{seconds // 3600} hours"
        return f"{seconds // 86400} days"

    def evaluate_run(self, slug: str, run: dict) -> tuple[str, str]:
        """Evaluate a Forgejo task's status, which also contains its final outcome."""
        run_name = run.get("name") or run.get("id") or "unknown run"
        label = f"{slug}: {run_name}"
        status = run.get("status")
        self.log.debug(
            "Evaluating %s: status=%s created_at=%s",
            label,
            status,
            run.get("created_at"),
        )

        if status in self.OK_STATUSES:
            return "OK", f"{label} {status}"
        if status == "failure":
            return "CRITICAL", f"{label} failed"

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
        "--owner",
        "-o",
        default="",
        help="only check repositories owned by this user or organization",
    )
    parser.add_argument(
        "--warning-seconds",
        "-w",
        type=int,
        help="warn when an pending/active run is older than this (stuck)",
    )
    parser.add_argument(
        "--critical-seconds",
        "-c",
        type=int,
        help="fail when an pending/active run is older than this (stuck)",
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
