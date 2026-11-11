"""Consistency between code, configuration files and documentation; secret scan."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from src.config import PROJECT_ROOT

TEXT_SUFFIXES = {".py", ".md", ".yml", ".yaml", ".toml", ".sql", ".txt", ".example", ".json"}
SECRET_PATTERNS = [
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS access key id
    re.compile(r"aws_secret_access_key\s*=\s*\S{20,}", re.IGNORECASE),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"ghp_[A-Za-z0-9]{30,}"),  # GitHub token
]


def read(relative: str) -> str:
    return (PROJECT_ROOT / relative).read_text(encoding="utf-8")


def project_text_files() -> list[Path]:
    ignored = {".venv", ".git", "data", "logs", ".pytest_cache", ".ruff_cache", "__pycache__"}
    files = []
    for path in PROJECT_ROOT.rglob("*"):
        relative_parts = path.relative_to(PROJECT_ROOT).parts
        is_text = path.suffix in TEXT_SUFFIXES or path.name in {"Makefile", "Dockerfile"}
        if path.is_file() and is_text and not ignored.intersection(relative_parts):
            files.append(path)
    return files


def env_example_keys() -> set[str]:
    keys = set()
    for line in read(".env.example").splitlines():
        match = re.match(r"^([A-Z][A-Z0-9_]*)=", line)
        if match:
            keys.add(match.group(1))
    return keys


def test_every_environment_variable_read_by_the_code_is_documented() -> None:
    used = set(re.findall(r'_get_\w+\(\s*env,\s*"([A-Z0-9_]+)"', read("src/config.py")))
    used |= set(re.findall(r'env\.get\("([A-Z0-9_]+)"', read("src/config.py")))
    optional_paths = {"DATA_DIR", "SQL_DIR", "LOG_DIR", "AWS_DEFAULT_REGION"}
    assert used - optional_paths <= env_example_keys()


def test_env_example_contains_no_real_secret() -> None:
    content = read(".env.example")
    assert not any(pattern.search(content) for pattern in SECRET_PATTERNS)
    assert "AWS_ACCESS_KEY_ID" not in content and "AWS_SECRET_ACCESS_KEY" not in content
    assert "POSTGRES_PASSWORD=change_me_locally" in content
    assert "S3_ENABLED=false" in content and "LOAD_TO_POSTGRES=false" in content


def test_repository_contains_no_secret() -> None:
    offenders = [
        f"{path.relative_to(PROJECT_ROOT)}: {pattern.pattern}"
        for path in project_text_files()
        for pattern in SECRET_PATTERNS
        if pattern.search(path.read_text(encoding="utf-8", errors="ignore"))
    ]
    assert offenders == []


def test_gitignore_protects_secrets_and_generated_data() -> None:
    lines = read(".gitignore").splitlines()
    for expected in (".env", "data/*", "!data/README.md", "logs/", ".venv/", "*.pbix"):
        assert expected in lines
    assert "!.env.example" in lines


def test_docker_compose_has_healthcheck_volume_and_ordering() -> None:
    compose = yaml.safe_load(read("docker-compose.yml"))
    db, app = compose["services"]["db"], compose["services"]["app"]
    assert "healthcheck" in db and "pg_isready" in " ".join(db["healthcheck"]["test"])
    assert any(str(v).startswith("pgdata:") for v in db["volumes"])
    assert "pgdata" in compose["volumes"]
    assert app["depends_on"]["db"]["condition"] == "service_healthy"
    assert db["ports"][0].startswith("127.0.0.1:")  # not exposed on the network
    assert ":?" in db["environment"]["POSTGRES_PASSWORD"]  # password is mandatory, no default


def test_compose_environment_matches_application_settings() -> None:
    compose = yaml.safe_load(read("docker-compose.yml"))
    app_env = set(compose["services"]["app"]["environment"])
    config_source = read("src/config.py")
    for variable in app_env:
        assert f'"{variable}"' in config_source, f"{variable} is not read by src/config.py"


def test_dockerfile_runs_as_non_root_with_pinned_base() -> None:
    dockerfile = read("Dockerfile")
    assert re.search(r"^FROM python:3\.\d+-slim", dockerfile, re.MULTILINE)
    assert re.search(r"^USER app", dockerfile, re.MULTILINE)


def test_ci_workflow_runs_lint_and_tests_on_push_and_pull_request() -> None:
    workflow = yaml.safe_load(read(".github/workflows/ci.yml"))
    triggers = workflow.get(True, workflow.get("on"))  # PyYAML parses the key `on` as True
    assert "push" in triggers and "pull_request" in triggers
    steps = " ".join(str(step.get("run", "")) for step in workflow["jobs"]["quality"]["steps"])
    for command in (
        "pip install -r requirements-dev.txt",
        "ruff check",
        "ruff format --check",
        "pytest",
    ):
        assert command in steps


def test_sql_files_define_every_view_used_by_reconciliation() -> None:
    from src.load import _RECONCILIATION_CHECKS

    analytics = read("sql/analytics.sql")
    for _, view, _ in _RECONCILIATION_CHECKS:
        assert f"ecommerce.{view} AS" in analytics
    assert "PRIMARY KEY" in read("sql/schema.sql")


def makefile_targets() -> set[str]:
    return set(re.findall(r"^([a-zA-Z_-]+):", read("Makefile"), re.MULTILINE))


@pytest.mark.parametrize(
    "document", ["README.md", "docs/workflow.md", "docs/architecture.md", "dashboard/powerbi.md"]
)
def test_make_targets_cited_in_documentation_exist(document: str) -> None:
    cited = set(re.findall(r"(?:^|`|&& )\s*make ([a-z][a-z-]+)", read(document), re.MULTILINE))
    assert cited <= makefile_targets(), (
        f"unknown targets in {document}: {cited - makefile_targets()}"
    )


def test_documented_cli_flags_exist() -> None:
    from src.main import build_parser

    flags = {
        action.option_strings[0] for action in build_parser()._actions if action.option_strings
    }
    cited = set(re.findall(r"(--(?:regenerate|skip-db|load-db|s3))\b", read("README.md")))
    assert cited <= flags


def test_readme_contains_valid_looking_mermaid_and_required_sections() -> None:
    readme = read("README.md")
    assert "```mermaid\nflowchart LR" in readme
    for heading in (
        "Security and secrets",
        "Known limitations",
        "Roadmap",
        "Author",
        "Dashboard guide",
    ):
        assert f"## {heading}" in readme
    assert (
        "YOUR_GITHUB_USERNAME" in readme and "YOUR_LINKEDIN_PROFILE" in readme
    )  # placeholders only
