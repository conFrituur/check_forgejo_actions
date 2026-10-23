# Repository Guidelines

## Project Structure & Module Organization

The implementation is intentionally small: `check_forgejo_actions.py` contains the CLI, Forgejo API client, status evaluation, and Nagios-compatible exit handling. Project metadata and dependencies live in `pyproject.toml`; `uv.lock` pins reproducible versions. Tests are in `tests/test_check_forgejo_actions.py`, with sanitized API payloads under `tests/ApiResponses/`, arranged to mirror API routes. GitHub Actions workflows in `.github/workflows/` run the same lint and test checks expected locally.

## Build, Test, and Development Commands

- `uv sync --locked --all-extras --dev` creates or updates the environment from the lockfile, including pytest, coverage, and Ruff.
- `uv run check_forgejo_actions.py --help` verifies the CLI and lists supported arguments.
- `uv run ruff check` runs the repository's Python lint checks.
- `uv run pytest` executes the full unit test suite.
- `uv run pytest --cov=check_forgejo_actions` runs tests with the coverage measurement used in CI.

There is no separate build step; the executable Python script is the deployable artifact. Use Python 3.12 or newer.

## Coding Style & Naming Conventions

Follow standard Python conventions: four-space indentation, `snake_case` for functions and variables, `PascalCase` for classes, and uppercase names for module constants. Preserve type annotations on public methods and helpers. Keep CLI parsing in `main()` and API/status behavior in `CheckForgejoActions`. Run Ruff before submitting changes; avoid unrelated formatting churn.

## Testing Guidelines

Pytest is the test framework. Name tests `test_<behavior>` and place reusable JSON responses in the route-matching `tests/ApiResponses/` tree (for example, `user/repos/401.json`). Mock network calls, time, command-line arguments, and `nagios_exit` so tests remain deterministic and offline. Add coverage for success, warning, critical, unknown, and malformed/API-error paths affected by a change. No fixed coverage threshold is configured, but new behavior should not reduce meaningful coverage.

## Commit & Pull Request Guidelines

Recent commits use short, imperative summaries such as `Skip non 'active' repo's in check`; keep each commit focused and describe the observable change. Pull requests should explain the motivation and behavior, note any Icinga or CLI impact, and link relevant issues. Include sample plugin output when status messages change. Ensure Ruff and the coverage test command pass before requesting review.

## Security & Configuration

Never commit Forgejo tokens or live API responses containing secrets. Pass credentials through CLI/configuration mechanisms and mark `forgejo_token` as a protected Icinga custom variable, as described in `README.md`.
