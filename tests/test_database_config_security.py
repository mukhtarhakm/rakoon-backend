import os
import sys
import unittest
from unittest.mock import patch
import subprocess

# Ensure rakoon-backend is in sys.path
backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from app.config_security import (
    is_local_or_disposable_url,
    extract_supabase_project_ref,
    validate_migration_target,
)


class TestDatabaseConfigSecurity(unittest.TestCase):
    """Regression test suite for database configuration safety and multi-component migration guards."""

    def test_environment_database_url_not_overwritten_by_dotenv(self):
        """1. DATABASE_URL from environment must not be overwritten by .env."""
        test_url = "sqlite:///disposable_test_env_priority.db"
        
        cmd = [
            sys.executable,
            "-c",
            "import os, sys; "
            "sys.path.insert(0, 'rakoon-backend'); "
            "os.environ['DATABASE_URL'] = '" + test_url + "'; "
            "import app.database; "
            "assert os.environ['DATABASE_URL'] == '" + test_url + "', 'DATABASE_URL was overwritten by .env!'"
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Subprocess failed: {res.stderr}")

    def test_disposable_sqlite_does_not_switch_to_supabase(self):
        """2. Tests using disposable SQLite must not switch to Supabase."""
        test_url = "sqlite:///:memory:"
        cmd = [
            sys.executable,
            "-c",
            "import os, sys; "
            "sys.path.insert(0, 'rakoon-backend'); "
            "os.environ['DATABASE_URL'] = '" + test_url + "'; "
            "from app.database import engine, DATABASE_URL; "
            "assert str(engine.url).startswith('sqlite'), f'Engine switched away from SQLite: {engine.url}'; "
            "assert 'supabase' not in str(engine.url).lower(), 'Engine URL contains Supabase!'"
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Subprocess failed: {res.stderr}")

    def test_empty_database_url_raises_clear_error(self):
        """3. Empty DATABASE_URL produces a clear error."""
        cmd = [
            sys.executable,
            "-c",
            "import os, sys; "
            "sys.path.insert(0, 'rakoon-backend'); "
            "from dotenv import load_dotenv\n"
            "load_dotenv(override=False)\n"
            "os.environ['DATABASE_URL'] = ''\n"
            "import app.database"
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("DATABASE_URL is required", res.stderr)

    def test_import_database_module_does_not_perform_ddl_or_migration(self):
        """4. Import of database module must not perform DDL or migration operations."""
        cmd = [
            sys.executable,
            "-c",
            "import os, sys\n"
            "sys.path.insert(0, 'rakoon-backend'); "
            "os.environ['DATABASE_URL'] = 'sqlite:///:memory:'\n"
            "from sqlalchemy.sql.schema import MetaData\n"
            "def forbidden_create_all(*args, **kwargs):\n"
            "    raise AssertionError('DDL create_all() was invoked during import!')\n"
            "MetaData.create_all = forbidden_create_all\n"
            "import app.database\n"
            "import app.models.db_models\n"
            "print('SUCCESS: Clean import with no DDL')\n"
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"DDL or migration occurred during import: {res.stderr}")
        self.assertIn("SUCCESS: Clean import with no DDL", res.stdout)

    def test_migration_target_validation_always_blocks_remote_in_test_env(self):
        """5. Test environment must ALWAYS block remote migrations even if all authorization flags are present."""
        remote_url = "postgresql://postgres.myproject:secret123@aws-0-ap-southeast-1.pooler.supabase.com:6543/postgres"
        
        test_env_vars = {
            "PYTEST_CURRENT_TEST": "test_case",
            "TESTING": "1",
            "ALLOW_REMOTE_MIGRATION": "true",
            "RAKOON_ENV": "development",
            "EXPECTED_DATABASE_HOST": "aws-0-ap-southeast-1.pooler.supabase.com",
            "EXPECTED_DATABASE_PORT": "6543",
            "EXPECTED_DATABASE_NAME": "postgres",
            "EXPECTED_SUPABASE_PROJECT_REF": "myproject",
        }
        with patch.dict(os.environ, test_env_vars):
            with self.assertRaises(RuntimeError) as ctx:
                validate_migration_target(remote_url)
            self.assertIn("FAIL-CLOSED", str(ctx.exception))
            self.assertIn("Test environment detected", str(ctx.exception))

    def test_render_flag_alone_cannot_bypass_guard(self):
        """6. RENDER=true alone MUST NOT bypass the migration guard."""
        remote_url = "postgresql://postgres.myproject:secret123@aws-0-ap-southeast-1.pooler.supabase.com:6543/postgres"
        
        clean_env = {"RENDER": "true"}
        with patch.dict(os.environ, clean_env, clear=True):
            with self.assertRaises(RuntimeError) as ctx:
                validate_migration_target(remote_url)
            self.assertIn("FAIL-CLOSED", str(ctx.exception))
            self.assertIn("ALLOW_REMOTE_MIGRATION=true", str(ctx.exception))
            self.assertIn("RENDER=true or implicit deployment flags alone do not authorize", str(ctx.exception))

    def test_remote_migration_fails_without_valid_environment(self):
        """7. Remote migration fails if target environment is unset or invalid."""
        remote_url = "postgresql://postgres.myproject:secret123@aws-0-ap-southeast-1.pooler.supabase.com:6543/postgres"
        
        # Case A: Environment unset
        clean_env_no_env = {
            "ALLOW_REMOTE_MIGRATION": "true",
            "EXPECTED_DATABASE_HOST": "aws-0-ap-southeast-1.pooler.supabase.com",
            "EXPECTED_DATABASE_PORT": "6543",
            "EXPECTED_DATABASE_NAME": "postgres",
            "EXPECTED_SUPABASE_PROJECT_REF": "myproject",
        }
        with patch.dict(os.environ, clean_env_no_env, clear=True):
            with self.assertRaises(RuntimeError) as ctx:
                validate_migration_target(remote_url)
            self.assertIn("FAIL-CLOSED", str(ctx.exception))
            self.assertIn("Target environment must be explicitly declared", str(ctx.exception))

        # Case B: Environment invalid (e.g. 'sandbox' instead of development/staging/production)
        clean_env_invalid_env = {
            "ALLOW_REMOTE_MIGRATION": "true",
            "RAKOON_ENV": "sandbox",
            "EXPECTED_DATABASE_HOST": "aws-0-ap-southeast-1.pooler.supabase.com",
            "EXPECTED_DATABASE_PORT": "6543",
            "EXPECTED_DATABASE_NAME": "postgres",
            "EXPECTED_SUPABASE_PROJECT_REF": "myproject",
        }
        with patch.dict(os.environ, clean_env_invalid_env, clear=True):
            with self.assertRaises(RuntimeError) as ctx:
                validate_migration_target(remote_url)
            self.assertIn("FAIL-CLOSED", str(ctx.exception))
            self.assertIn("Target environment must be explicitly declared", str(ctx.exception))

    def test_allow_remote_migration_alone_insufficient_without_identity(self):
        """8. ALLOW_REMOTE_MIGRATION=true alone is insufficient without complete target identity."""
        remote_url = "postgresql://postgres.myproject:secret123@aws-0-ap-southeast-1.pooler.supabase.com:6543/postgres"
        
        # Only ALLOW_REMOTE_MIGRATION and valid RAKOON_ENV, but missing identity config
        clean_env = {
            "ALLOW_REMOTE_MIGRATION": "true",
            "RAKOON_ENV": "development",
        }
        with patch.dict(os.environ, clean_env, clear=True):
            with self.assertRaises(RuntimeError) as ctx:
                validate_migration_target(remote_url)
            self.assertIn("FAIL-CLOSED", str(ctx.exception))
            self.assertIn("Incomplete target database identity configuration", str(ctx.exception))

    def test_two_supabase_projects_sharing_same_pooler_host_isolation(self):
        """9. Two Supabase projects sharing the exact same pooler host and port must NOT authorize each other."""
        pooler_host = "aws-0-ap-southeast-1.pooler.supabase.com"
        port = 6543
        dbname = "postgres"

        url_alpha = f"postgresql://postgres.project_alpha:passA@{pooler_host}:{port}/{dbname}"
        url_beta = f"postgresql://postgres.project_beta:passB@{pooler_host}:{port}/{dbname}"

        alpha_config = {
            "ALLOW_REMOTE_MIGRATION": "true",
            "RAKOON_ENV": "development",
            "EXPECTED_DATABASE_HOST": pooler_host,
            "EXPECTED_DATABASE_PORT": str(port),
            "EXPECTED_DATABASE_NAME": dbname,
            "EXPECTED_SUPABASE_PROJECT_REF": "project_alpha",
        }
        with patch.dict(os.environ, alpha_config, clear=True):
            # 1. Project Alpha URL must pass
            validate_migration_target(url_alpha)

            # 2. Project Beta URL sharing the SAME host/port/dbname must be BLOCKED
            with self.assertRaises(RuntimeError) as ctx:
                validate_migration_target(url_beta)
            self.assertIn("FAIL-CLOSED", str(ctx.exception))
            self.assertIn("Supabase project reference mismatch", str(ctx.exception))
            self.assertIn("Confirmation for one Supabase project does not authorize migration against another", str(ctx.exception))

    def test_database_port_or_name_mismatch_fails_closed(self):
        """10. Port or database name mismatch must halt migration."""
        base_config = {
            "ALLOW_REMOTE_MIGRATION": "true",
            "RAKOON_ENV": "development",
            "EXPECTED_DATABASE_HOST": "aws-0-ap-southeast-1.pooler.supabase.com",
            "EXPECTED_DATABASE_PORT": "6543",
            "EXPECTED_DATABASE_NAME": "postgres",
            "EXPECTED_SUPABASE_PROJECT_REF": "myproject",
        }
        with patch.dict(os.environ, base_config, clear=True):
            # Port mismatch: URL connects on 5432 instead of expected 6543
            url_wrong_port = "postgresql://postgres.myproject:secret@aws-0-ap-southeast-1.pooler.supabase.com:5432/postgres"
            with self.assertRaises(RuntimeError) as ctx:
                validate_migration_target(url_wrong_port)
            self.assertIn("port mismatch", str(ctx.exception).lower())

            # Database name mismatch: URL connects to 'otherdb' instead of 'postgres'
            url_wrong_db = "postgresql://postgres.myproject:secret@aws-0-ap-southeast-1.pooler.supabase.com:6543/otherdb"
            with self.assertRaises(RuntimeError) as ctx:
                validate_migration_target(url_wrong_db)
            self.assertIn("database name mismatch", str(ctx.exception).lower())

    def test_authorized_remote_migration_succeeds_with_full_identity(self):
        """11. Remote migration succeeds when explicit authorization, environment, and full identity match."""
        remote_url = "postgresql://postgres.myproject:secret123@aws-0-ap-southeast-1.pooler.supabase.com:6543/postgres"
        
        valid_config = {
            "ALLOW_REMOTE_MIGRATION": "true",
            "RAKOON_ENV": "development",
            "EXPECTED_DATABASE_HOST": "aws-0-ap-southeast-1.pooler.supabase.com",
            "EXPECTED_DATABASE_PORT": "6543",
            "EXPECTED_DATABASE_NAME": "postgres",
            "EXPECTED_SUPABASE_PROJECT_REF": "myproject",
        }
        with patch.dict(os.environ, valid_config, clear=True):
            # Should validate cleanly without raising RuntimeError
            validate_migration_target(remote_url)

    def test_migration_target_validation_allows_local_and_sqlite(self):
        """12. Local and disposable database targets are always allowed without remote configuration."""
        local_targets = [
            "sqlite:///:memory:",
            "sqlite:///./disposable.db",
            "postgresql://user:pass@localhost:5432/devdb",
            "postgresql://user:pass@127.0.0.1:5432/devdb",
            "postgresql://user:pass@::1:5432/devdb",
        ]
        for url in local_targets:
            self.assertTrue(is_local_or_disposable_url(url), f"Failed for url: {url}")
            validate_migration_target(url)

    def test_credentials_not_exposed_in_validation_error(self):
        """13. Passwords and credentials must never be printed in validation errors or logs."""
        sensitive_password = "SuperSecretPassword999!"
        remote_url = f"postgresql://sensitive_user:{sensitive_password}@remote.example.com:5432/db"
        
        with patch.dict(os.environ, {"PYTEST_CURRENT_TEST": "test_auth_log"}):
            with self.assertRaises(RuntimeError) as ctx:
                validate_migration_target(remote_url)
            err_msg = str(ctx.exception)
            self.assertNotIn(sensitive_password, err_msg)


if __name__ == "__main__":
    unittest.main()