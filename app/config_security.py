import os
import urllib.parse
from typing import Optional, Set


def is_local_or_disposable_url(url: str) -> bool:
    """Returns True if the target URL points to a local or disposable database."""
    if not url:
        return True
    if url.startswith("sqlite"):
        return True
    try:
        parsed = urllib.parse.urlparse(url)
        hostname = (parsed.hostname or "").lower()
        return hostname in ("localhost", "127.0.0.1", "::1", "")
    except Exception:
        return False


def extract_supabase_project_ref(parsed_url: urllib.parse.ParseResult) -> Optional[str]:
    """Safely extracts Supabase project reference from parsed URL without manual string splitting of the URL.

    Handles both pooler URLs (where user is e.g. postgres.<project_ref>)
    and direct connection URLs (where host is e.g. db.<project_ref>.supabase.co).
    """
    # 1. From pooler username: e.g. postgres.<project_ref>
    username = parsed_url.username or ""
    if "." in username:
        return username.split(".")[-1].strip().lower()

    # 2. From direct hostname: e.g. db.<project_ref>.supabase.co
    hostname = (parsed_url.hostname or "").lower()
    if hostname.endswith(".supabase.co"):
        parts = hostname.split(".")
        if len(parts) >= 3 and parts[0] == "db":
            return parts[1].strip().lower()

    return None


def validate_migration_target(url: str) -> None:
    """Validates the migration target database before online execution.

    Enforces strict, multi-component fail-closed security:
    1. Test environments (pytest, test runner) are ALWAYS blocked from running migrations
       against remote databases, regardless of any authorization flags.
    2. Remote migrations require EXPLICIT authorization via ALLOW_REMOTE_MIGRATION=true.
       Platform flags like RENDER=true or ENVIRONMENT alone are never sufficient.
    3. Target environment must be explicitly declared as one of ('development', 'staging', 'production').
    4. Full target database identity must be explicitly configured and validated:
       - Hostname (EXPECTED_DATABASE_HOST)
       - Port (EXPECTED_DATABASE_PORT)
       - Database Name (EXPECTED_DATABASE_NAME)
       - User or Supabase Project Reference (EXPECTED_DATABASE_USER or EXPECTED_SUPABASE_PROJECT_REF)
       If any required component is missing or does not match the parsed URL, the migration
       is rejected immediately before any connection or mutation occurs.
    """
    if is_local_or_disposable_url(url):
        return

    # Use safe standard URL parser
    try:
        parsed = urllib.parse.urlparse(url)
        actual_host = (parsed.hostname or "").strip().lower()
        actual_port = parsed.port or 5432
        actual_dbname = (parsed.path.lstrip("/").split("?")[0]).strip().lower()
        actual_user = (parsed.username or "").strip().lower()
        actual_project_ref = extract_supabase_project_ref(parsed)
    except Exception:
        raise RuntimeError("FAIL-CLOSED: Unable to parse target database URL safely.")

    # 1. Fail-closed: Test runner isolation (strictly blocked)
    is_test_env = (
        os.getenv("PYTEST_CURRENT_TEST") is not None
        or os.getenv("TESTING", "").lower() in ("1", "true", "yes")
        or os.getenv("RAKOON_ENV", "").lower() in ("test", "testing")
    )
    if is_test_env:
        raise RuntimeError(
            f"FAIL-CLOSED: Migration execution blocked. Test environment detected, "
            f"but migration target is a remote database ({actual_host}). Migration tests must use "
            f"an isolated local or disposable database."
        )

    # 2. Fail-closed: Explicit remote migration authorization
    allow_flag = os.getenv("ALLOW_REMOTE_MIGRATION", "").lower() in ("1", "true", "yes")
    confirm_flag = os.getenv("CONFIRM_REMOTE_MIGRATION", "").lower() in ("1", "true", "yes")
    if not (allow_flag or confirm_flag):
        raise RuntimeError(
            f"FAIL-CLOSED: Target database is remote ({actual_host}). Remote migrations require "
            f"explicit authorization via ALLOW_REMOTE_MIGRATION=true. "
            f"Note: RENDER=true or implicit deployment flags alone do not authorize remote migrations."
        )

    # 3. Fail-closed: Target environment validation
    target_env = (
        os.getenv("RAKOON_ENV")
        or os.getenv("ENVIRONMENT")
        or os.getenv("APP_ENV")
        or ""
    ).strip().lower()

    valid_envs = ("development", "staging", "production")
    if not target_env or target_env not in valid_envs:
        raise RuntimeError(
            f"FAIL-CLOSED: Target environment must be explicitly declared as one of "
            f"{valid_envs} (e.g. RAKOON_ENV=development|staging|production) to authorize remote migrations. "
            f"Current value: '{target_env or 'UNSET'}'."
        )

    # 4. Fail-closed: Verify that expected target identity configuration is complete
    expected_host = (os.getenv("EXPECTED_DATABASE_HOST") or "").strip().lower()
    expected_port_raw = (os.getenv("EXPECTED_DATABASE_PORT") or "").strip()
    expected_dbname = (os.getenv("EXPECTED_DATABASE_NAME") or "").strip().lower()
    expected_user = (os.getenv("EXPECTED_DATABASE_USER") or "").strip().lower()
    expected_project_ref = (
        os.getenv("EXPECTED_SUPABASE_PROJECT_REF")
        or os.getenv("EXPECTED_PROJECT_REF")
        or ""
    ).strip().lower()

    if not expected_host:
        raise RuntimeError(
            "FAIL-CLOSED: Incomplete target database identity configuration. "
            "EXPECTED_DATABASE_HOST must be explicitly specified."
        )

    if not expected_port_raw:
        raise RuntimeError(
            "FAIL-CLOSED: Incomplete target database identity configuration. "
            "EXPECTED_DATABASE_PORT must be explicitly specified."
        )

    try:
        expected_port = int(expected_port_raw)
    except ValueError:
        raise RuntimeError("FAIL-CLOSED: EXPECTED_DATABASE_PORT must be a valid integer port.")

    if not expected_dbname:
        raise RuntimeError(
            "FAIL-CLOSED: Incomplete target database identity configuration. "
            "EXPECTED_DATABASE_NAME must be explicitly specified."
        )

    if not expected_user and not expected_project_ref:
        raise RuntimeError(
            "FAIL-CLOSED: Incomplete target database identity configuration. "
            "Either EXPECTED_DATABASE_USER or EXPECTED_SUPABASE_PROJECT_REF must be explicitly specified "
            "to verify the identity of the target database."
        )

    # 5. Fail-closed: Validate all target identity components
    # 5a. Host validation
    if actual_host != expected_host:
        raise RuntimeError(
            f"FAIL-CLOSED: Target database host mismatch. Expected '{expected_host}', got '{actual_host}'. "
            "Confirmation for one host does not authorize migration against another remote target."
        )

    # Optional allowlist check if ALLOWED_MIGRATION_HOSTS is additionally configured
    raw_allowed_hosts = os.getenv("ALLOWED_MIGRATION_HOSTS", "").strip()
    if raw_allowed_hosts:
        allowed_hosts: Set[str] = {h.strip().lower() for h in raw_allowed_hosts.split(",") if h.strip()}
        if actual_host not in allowed_hosts:
            raise RuntimeError(
                f"FAIL-CLOSED: Target database host '{actual_host}' does not match the configured allowlist "
                f"{sorted(list(allowed_hosts))}."
            )

    # 5b. Port validation
    if actual_port != expected_port:
        raise RuntimeError(
            f"FAIL-CLOSED: Target database port mismatch. Expected {expected_port}, got {actual_port}."
        )

    # 5c. Database name validation
    if actual_dbname != expected_dbname:
        raise RuntimeError(
            f"FAIL-CLOSED: Target database name mismatch. Expected '{expected_dbname}', got '{actual_dbname}'."
        )

    # 5d. Username validation (if configured)
    if expected_user and actual_user != expected_user:
        raise RuntimeError(
            f"FAIL-CLOSED: Target database username mismatch. Confirmation for one database user "
            f"does not authorize migration against another remote target."
        )

    # 5e. Supabase Project Reference validation (if configured)
    if expected_project_ref:
        if not actual_project_ref or actual_project_ref != expected_project_ref:
            raise RuntimeError(
                f"FAIL-CLOSED: Target database Supabase project reference mismatch. "
                f"Expected project ref '{expected_project_ref}'. Confirmation for one Supabase project "
                f"does not authorize migration against another project sharing the same pooler host."
            )