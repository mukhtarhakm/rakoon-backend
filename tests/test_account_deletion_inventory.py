"""Penjaga runbook hapus akun (HAPUS_AKUN.md di root proyek).

Gagal bila model punya kolom id pengguna yang belum tercatat di COVERED_IDENTITY_COLUMNS,
supaya kolom baru tidak lolos dari prosedur penghapusan.
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

RUNBOOK = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "HAPUS_AKUN.md")

# (tabel, kolom) yang ditangani HAPUS_AKUN.md. Tambah entri di sini DAN di runbook.
COVERED_IDENTITY_COLUMNS = {
    ("scan_sessions", "user_id"),
    ("price_entries", "sumber_user_id"),
    ("store_owners", "user_id"),
    ("store_owners", "reviewed_by"),
    ("store_submissions", "user_id"),
    ("store_submissions", "reviewed_by"),
    ("ad_campaigns", "owner_user_id"),
    ("payment_transactions", "owner_user_id"),
}

IDENTITY_SUFFIX = re.compile(r"user_id$")
IDENTITY_NAMES = {"reviewed_by"}


def model_identity_columns():
    from app.database import Base
    from app.models import db_models  # noqa: F401  (mendaftarkan semua tabel ke Base.metadata)

    return {
        (table.name, column.name)
        for table in Base.metadata.tables.values()
        for column in table.columns
        if IDENTITY_SUFFIX.search(column.name) or column.name in IDENTITY_NAMES
    }


class TestAccountDeletionInventory(unittest.TestCase):
    def test_every_identity_column_is_covered_by_the_runbook(self):
        missing = sorted(model_identity_columns() - COVERED_IDENTITY_COLUMNS)
        self.assertEqual(
            missing,
            [],
            f"Kolom identitas baru belum tercakup penghapusan akun: {missing}. "
            "Perbarui HAPUS_AKUN.md (dry-run, penghapusan, verifikasi) lalu tambahkan kolomnya "
            "ke COVERED_IDENTITY_COLUMNS di test ini.",
        )

    def test_covered_list_has_no_stale_entries(self):
        stale = sorted(COVERED_IDENTITY_COLUMNS - model_identity_columns())
        self.assertEqual(
            stale,
            [],
            f"Entri tidak ada lagi di model: {stale}. Hapus dari COVERED_IDENTITY_COLUMNS dan dari HAPUS_AKUN.md.",
        )

    @unittest.skipUnless(os.path.exists(RUNBOOK), "HAPUS_AKUN.md ada di root proyek, bukan di repo backend")
    def test_runbook_mentions_every_covered_column(self):
        with open(RUNBOOK, encoding="utf-8") as f:
            text = f.read()
        absent = sorted(f"{t}.{c}" for t, c in COVERED_IDENTITY_COLUMNS if f"{t}.{c}" not in text)
        self.assertEqual(absent, [], f"HAPUS_AKUN.md tidak menyebut: {absent}")


if __name__ == "__main__":
    unittest.main()
