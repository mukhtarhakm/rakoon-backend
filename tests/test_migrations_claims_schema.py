"""Migrations M1-M3 (store submission coordinates, claim audit + active-claim uniqueness,
price_entries.store_id FK + created_store_id index). SQLite only: never touches a real database."""
import os
import sys
import tempfile
import unittest
import uuid
from contextlib import contextmanager
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OLD_HEAD = "f8a9b0c1d2e3"
M1, M2, M3 = "a9b0c1d2e3f4", "b0c1d2e3f4a5", "c1d2e3f4a5b6"


def _config() -> Config:
    cfg = Config()  # no ini file: avoids fileConfig() resetting logging for other tests
    cfg.set_main_option("script_location", os.path.join(ROOT, "migrations"))
    return cfg


@contextmanager
def _sqlite_db():
    with tempfile.TemporaryDirectory() as tmp:
        url = "sqlite:///" + os.path.join(tmp, "mig.db").replace("\\", "/")
        with mock.patch.dict(os.environ, {"DATABASE_URL": url}):
            engine = create_engine(url)
            try:
                yield url, engine
            finally:
                engine.dispose()


def _uid() -> str:
    return str(uuid.uuid4())


# M1b/M5 (drop store_category, drop users) are covered in test_migrations_drop_legacy.py.
class TestMigrationChain(unittest.TestCase):
    def test_single_head_chained_from_the_current_head(self):
        script = ScriptDirectory.from_config(_config())
        self.assertIn(M3, [r.revision for r in script.walk_revisions()])
        self.assertEqual(script.get_revision(M1).down_revision, OLD_HEAD)
        self.assertEqual(script.get_revision(M2).down_revision, M1)
        self.assertEqual(script.get_revision(M3).down_revision, M2)


class TestUpgradeOnCleanSqlite(unittest.TestCase):
    def test_upgrade_head_creates_the_new_schema(self):
        with _sqlite_db() as (_, engine):
            command.upgrade(_config(), "head")
            insp = inspect(engine)

            sub_cols = {c["name"]: c for c in insp.get_columns("store_submissions")}
            self.assertIn("latitude", sub_cols)
            self.assertIn("longitude", sub_cols)
            self.assertTrue(sub_cols["latitude"]["nullable"])
            self.assertTrue(sub_cols["longitude"]["nullable"])

            owner_cols = {c["name"] for c in insp.get_columns("store_owners")}
            self.assertTrue({"reviewed_by", "reviewed_at"} <= owner_cols)

            fks = insp.get_foreign_keys("price_entries")
            self.assertTrue(any(fk["constrained_columns"] == ["store_id"] and fk["referred_table"] == "stores" for fk in fks))
            sub_idx = {i["name"] for i in insp.get_indexes("store_submissions")}
            self.assertIn("ix_store_submissions_created_store_id", sub_idx)

    def test_models_and_migrations_agree_on_the_touched_tables(self):
        from app.models.db_models import StoreOwner, StoreSubmission

        with _sqlite_db() as (_, engine):
            command.upgrade(_config(), "head")
            insp = inspect(engine)
            for model in (StoreOwner, StoreSubmission):
                db_cols = {c["name"] for c in insp.get_columns(model.__tablename__)}
                self.assertEqual({c.name for c in model.__table__.columns} - db_cols, set(), model.__tablename__)

    def test_only_one_active_claim_per_user_and_store(self):
        with _sqlite_db() as (_, engine):
            command.upgrade(_config(), "head")
            user, store = _uid(), _uid()
            insert = text(
                "INSERT INTO store_owners (id, user_id, store_id, status, created_at) "
                "VALUES (:id, :u, :s, :st, '2026-10-01 00:00:00')"
            )
            with engine.begin() as conn:
                conn.execute(text("INSERT INTO stores (id, nama) VALUES (:id, 'Toko')"), {"id": store})
                conn.execute(insert, {"id": _uid(), "u": user, "s": store, "st": "rejected"})
                conn.execute(insert, {"id": _uid(), "u": user, "s": store, "st": "rejected"})  # history may repeat
                conn.execute(insert, {"id": _uid(), "u": user, "s": store, "st": "pending"})
            with self.assertRaises(IntegrityError):
                with engine.begin() as conn:
                    conn.execute(insert, {"id": _uid(), "u": user, "s": store, "st": "pending"})
            with self.assertRaises(IntegrityError):
                with engine.begin() as conn:
                    conn.execute(insert, {"id": _uid(), "u": user, "s": store, "st": "verified"})


class TestIdempotentAndReversible(unittest.TestCase):
    def test_upgrade_tolerates_objects_that_already_exist(self):
        """Production already has the FK (outside Alembic); a partially applied DB must not break."""
        with _sqlite_db() as (_, engine):
            command.upgrade(_config(), OLD_HEAD)
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE store_owners ADD COLUMN reviewed_by VARCHAR"))
                conn.execute(text("ALTER TABLE store_submissions ADD COLUMN latitude FLOAT"))
                conn.execute(text("CREATE INDEX ix_store_submissions_created_store_id ON store_submissions (created_store_id)"))
            command.upgrade(_config(), "head")
            insp = inspect(engine)
            self.assertIn("reviewed_at", {c["name"] for c in insp.get_columns("store_owners")})
            self.assertIn("longitude", {c["name"] for c in insp.get_columns("store_submissions")})

    def test_downgrade_returns_to_the_previous_head(self):
        with _sqlite_db() as (_, engine):
            command.upgrade(_config(), "head")
            command.downgrade(_config(), OLD_HEAD)
            insp = inspect(engine)
            self.assertNotIn("reviewed_by", {c["name"] for c in insp.get_columns("store_owners")})
            self.assertNotIn("latitude", {c["name"] for c in insp.get_columns("store_submissions")})
            self.assertNotIn(
                "uq_store_owners_active_user_store", {i["name"] for i in insp.get_indexes("store_owners")}
            )


if __name__ == "__main__":
    unittest.main()
