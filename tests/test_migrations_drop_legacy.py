"""Migrations M1b (drop store_submissions.store_category) and M5 (drop users).
SQLite only: never touches a real database."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

from test_migrations_claims_schema import _config, _sqlite_db

M3 = "c1d2e3f4a5b6"
M1B, M5 = "d2e3f4a5b6c7", "e3f4a5b6c7d8"


class TestChain(unittest.TestCase):
    def test_single_head_chained_from_the_previous_head(self):
        script = ScriptDirectory.from_config(_config())
        self.assertEqual(script.get_heads(), [M5])
        self.assertEqual(script.get_revision(M1B).down_revision, M3)
        self.assertEqual(script.get_revision(M5).down_revision, M1B)


class TestModels(unittest.TestCase):
    def test_models_no_longer_define_users_or_store_category(self):
        from app.database import Base
        from app.models import db_models

        self.assertNotIn("users", Base.metadata.tables)
        self.assertFalse(hasattr(db_models, "User"))
        self.assertNotIn("store_category", db_models.StoreSubmission.__table__.columns)


class TestUpgradeDowngrade(unittest.TestCase):
    def test_upgrade_head_drops_users_and_store_category(self):
        with _sqlite_db() as (_, engine):
            command.upgrade(_config(), "head")
            insp = inspect(engine)
            self.assertNotIn("users", insp.get_table_names())
            self.assertNotIn("store_category", {c["name"] for c in insp.get_columns("store_submissions")})

    def test_downgrade_restores_empty_schema_and_upgrade_works_again(self):
        with _sqlite_db() as (_, engine):
            command.upgrade(_config(), "head")
            command.downgrade(_config(), M3)
            insp = inspect(engine)
            self.assertIn("users", insp.get_table_names())
            users = {c["name"]: c for c in insp.get_columns("users")}
            self.assertEqual(set(users), {"id", "nama", "email", "reputasi_score", "role"})
            self.assertFalse(users["role"]["nullable"])
            idx = {i["name"]: i for i in insp.get_indexes("users")}
            self.assertTrue(idx["ix_users_email"]["unique"])
            self.assertIn("ix_users_id", idx)
            self.assertIn("store_category", {c["name"] for c in insp.get_columns("store_submissions")})
            with engine.connect() as conn:
                self.assertEqual(conn.execute(text("SELECT count(*) FROM users")).scalar(), 0)

            command.upgrade(_config(), "head")
            self.assertNotIn("users", inspect(engine).get_table_names())

    def test_upgrade_tolerates_objects_already_dropped(self):
        with _sqlite_db() as (_, engine):
            command.upgrade(_config(), M3)
            with engine.begin() as conn:
                conn.execute(text("DROP TABLE users"))
                conn.execute(text("ALTER TABLE store_submissions DROP COLUMN store_category"))
            command.upgrade(_config(), "head")
            self.assertNotIn("users", inspect(engine).get_table_names())

    def test_upgrade_discards_existing_store_category_values_and_keeps_other_columns(self):
        with _sqlite_db() as (_, engine):
            command.upgrade(_config(), M3)
            with engine.begin() as conn:
                conn.execute(text(
                    "INSERT INTO store_submissions (id, user_id, store_name, store_address, store_category, "
                    "applicant_name, applicant_phone, applicant_role, status, created_at) VALUES "
                    "('s1', 'u1', 'Toko', 'Jl', 'Apotek', 'A', '08', 'Pemilik', 'pending', '2026-10-01 00:00:00')"
                ))
            command.upgrade(_config(), "head")
            with engine.connect() as conn:
                row = conn.execute(text("SELECT store_name, applicant_name FROM store_submissions")).one()
            self.assertEqual(tuple(row), ("Toko", "A"))


if __name__ == "__main__":
    unittest.main()
