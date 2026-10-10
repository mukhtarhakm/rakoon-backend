import os
import sys
import unittest
from unittest.mock import patch
import subprocess
import yaml
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

# Ensure rakoon-backend is in sys.path
backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from app.main import app
from app.database import check_database_readiness


class TestHealthAndDeploymentReadiness(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_liveness_check_returns_200_without_touching_database(self):
        """1. Liveness probe returns 200 OK without requiring database connection."""
        with patch("app.routers.health.check_database_readiness", return_value=False):
            res_live = self.client.get("/health/live")
            self.assertEqual(res_live.status_code, 200)
            data_live = res_live.json()
            self.assertEqual(data_live["status"], "ok")
            self.assertEqual(data_live["app"], "Rakoon Backend")

            # Check /live alias
            res_alias = self.client.get("/live")
            self.assertEqual(res_alias.status_code, 200)

            # Check root /
            res_root = self.client.get("/")
            self.assertEqual(res_root.status_code, 200)
            self.assertEqual(res_root.json()["status"], "ok")

    def test_readiness_check_returns_200_when_database_is_connected(self):
        """2. Readiness probe returns 200 OK when database SELECT 1 succeeds."""
        with patch("app.routers.health.check_database_readiness", return_value=True):
            res_ready = self.client.get("/health/ready")
            self.assertEqual(res_ready.status_code, 200)
            data_ready = res_ready.json()
            self.assertEqual(data_ready["status"], "ready")
            self.assertEqual(data_ready["database"], "connected")

            # Check /ready alias
            res_alias = self.client.get("/ready")
            self.assertEqual(res_alias.status_code, 200)

            # Check main /health endpoint used by Render
            res_health = self.client.get("/health")
            self.assertEqual(res_health.status_code, 200)
            data_health = res_health.json()
            self.assertEqual(data_health["status"], "ok")
            self.assertEqual(data_health["database"], "connected")

    def test_readiness_check_returns_503_when_database_is_inaccessible(self):
        """3. Readiness probe returns HTTP 503 when database cannot be reached."""
        with patch("app.routers.health.check_database_readiness", return_value=False):
            res_ready = self.client.get("/health/ready")
            self.assertEqual(res_ready.status_code, 503)
            data_ready = res_ready.json()
            self.assertEqual(data_ready["status"], "unready")
            self.assertEqual(data_ready["database"], "disconnected")
            self.assertIn("detail", data_ready)

            # /ready alias must also return 503
            res_alias = self.client.get("/ready")
            self.assertEqual(res_alias.status_code, 503)

            # Main /health used by Render must fail with 503
            res_health = self.client.get("/health")
            self.assertEqual(res_health.status_code, 503)
            data_health = res_health.json()
            self.assertEqual(data_health["status"], "unready")
            self.assertEqual(data_health["database"], "disconnected")

    def test_database_query_failure_is_not_disguised_as_healthy(self):
        """4. Query/connection failure in check_database_readiness is not masked as healthy."""
        with patch("app.database.engine.connect", side_effect=OperationalError("Connection refused", {}, None)):
            is_ready = check_database_readiness()
            self.assertFalse(is_ready, "Readiness check must return False upon database query failure!")

            # Endpoint must return 503, never 200
            res = self.client.get("/health/ready")
            self.assertEqual(res.status_code, 503)
            self.assertNotEqual(res.json().get("status"), "ready")

    def test_error_response_does_not_leak_internal_credentials_or_details(self):
        """5. Health error response does not expose database credentials, URLs, or internal exceptions."""
        sensitive_string = "postgresql://my_secret_user:SuperSecretPassword123@db.supabase.com:6543/proddb"
        with patch("app.database.engine.connect", side_effect=OperationalError(f"Failed to connect to {sensitive_string}", {}, None)):
            res_ready = self.client.get("/health/ready")
            self.assertEqual(res_ready.status_code, 503)
            body_ready = res_ready.text
            self.assertNotIn("SuperSecretPassword123", body_ready)
            self.assertNotIn("my_secret_user", body_ready)
            self.assertNotIn("db.supabase.com", body_ready)
            self.assertNotIn("OperationalError", body_ready)

            res_health = self.client.get("/health")
            self.assertEqual(res_health.status_code, 503)
            body_health = res_health.text
            self.assertNotIn("SuperSecretPassword123", body_health)
            self.assertNotIn("my_secret_user", body_health)
            self.assertNotIn("db.supabase.com", body_health)
            self.assertNotIn("OperationalError", body_health)

    def test_head_methods_supported_on_health_endpoints(self):
        """HEAD requests return proper status code without breaking."""
        with patch("app.routers.health.check_database_readiness", return_value=True):
            self.assertEqual(self.client.head("/health/live").status_code, 200)
            self.assertEqual(self.client.head("/health/ready").status_code, 200)
            self.assertEqual(self.client.head("/health").status_code, 200)

        with patch("app.routers.health.check_database_readiness", return_value=False):
            self.assertEqual(self.client.head("/health/ready").status_code, 503)
            self.assertEqual(self.client.head("/health").status_code, 503)

    def test_render_yaml_configuration_validity_and_migration_sequencing(self):
        """6. render.yaml configures migration execution before Uvicorn and uses /health readiness path."""
        render_yaml_path = os.path.join(backend_dir, "render.yaml")
        self.assertTrue(os.path.isfile(render_yaml_path), "render.yaml not found!")

        with open(render_yaml_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        services = data.get("services", [])
        self.assertTrue(len(services) > 0, "No services defined in render.yaml")
        web_service = services[0]

        # Verify service type and health check path
        self.assertEqual(web_service.get("type"), "web")
        self.assertEqual(web_service.get("healthCheckPath"), "/health")

        # Verify startCommand sequences migration before uvicorn
        start_cmd = web_service.get("startCommand", "")
        self.assertIn("alembic upgrade head", start_cmd, "startCommand must execute alembic upgrade head")
        self.assertIn("uvicorn app.main:app", start_cmd, "startCommand must start uvicorn")
        self.assertIn("&&", start_cmd, "startCommand must use && to ensure migration failure halts startup")

        # Verify alembic appears before uvicorn in startCommand
        alembic_idx = start_cmd.find("alembic upgrade head")
        uvicorn_idx = start_cmd.find("uvicorn")
        self.assertLess(alembic_idx, uvicorn_idx, "alembic upgrade head must be executed before uvicorn")

        # Verify migration guard environment variables are declared
        env_vars = {item["key"]: item for item in web_service.get("envVars", [])}
        required_guard_keys = [
            "ALLOW_REMOTE_MIGRATION",
            "RAKOON_ENV",
            "EXPECTED_DATABASE_HOST",
            "EXPECTED_DATABASE_PORT",
            "EXPECTED_DATABASE_NAME",
            "EXPECTED_SUPABASE_PROJECT_REF",
        ]
        for key in required_guard_keys:
            self.assertIn(key, env_vars, f"Migration guard variable {key} must be declared in render.yaml envVars")

    def test_failed_migration_halts_startup_sequence(self):
        """7. If the migration step fails, the sequential command halts immediately without executing uvicorn."""
        cmd = "python -c \"import sys; sys.exit(42)\" && python -c \"print('STARTED_UVICORN')\""
        proc = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 42, "Shell command must preserve failing exit code")
        self.assertNotIn("STARTED_UVICORN", proc.stdout, "Uvicorn must NOT start if migration fails!")


if __name__ == "__main__":
    unittest.main()
