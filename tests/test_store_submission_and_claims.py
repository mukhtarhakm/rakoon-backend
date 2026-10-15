import os
import io
import sys
import uuid
import unittest
from datetime import datetime, timezone
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.main import app
from app.database import Base, get_db
from app.dependencies import get_current_user, get_current_admin_user, get_current_user_profile
from app.models.db_models import Store, StoreOwner, User, StoreSubmission
from claim_evidence import evidence_for

engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


class TestStoreSubmissionAndClaims(unittest.TestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = TestingSessionLocal()
        app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(app)

        self.user1_id = str(uuid.uuid4())
        self.user2_id = str(uuid.uuid4())
        self.admin_id = str(uuid.uuid4())

        self.user1 = User(id=self.user1_id, email="user1@example.com", nama="Pemohon Satu", role="user")
        self.user2 = User(id=self.user2_id, email="user2@example.com", nama="Pemohon Dua", role="user")
        self.admin = User(id=self.admin_id, email="admin@example.com", nama="Admin Rakoon", role="admin")

        self.store1 = Store(
            id=str(uuid.uuid4()),
            nama="Toko Katalog Existing",
            alamat="Jl. Malioboro No. 10, Yogyakarta",
            lat=-7.7925,
            lng=110.3658
        )
        self.db.add_all([self.user1, self.user2, self.admin, self.store1])
        self.db.commit()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)
        app.dependency_overrides.clear()

    def _login_as(self, user_id: str, role: str = "user"):
        app.dependency_overrides[get_current_user] = lambda: user_id
        app.dependency_overrides[get_current_user_profile] = lambda: {
            "id": user_id,
            "role": role,
            "email": f"{role}@example.com"
        }
        if role == "admin":
            app.dependency_overrides[get_current_admin_user] = lambda: {
                "user_id": user_id,
                "role": "admin"
            }
        else:
            app.dependency_overrides.pop(get_current_admin_user, None)

    def test_claim_store_with_only_store_id_is_rejected_because_evidence_is_mandatory(self):
        self._login_as(self.user1_id)
        res = self.client.post("/ads/claim-store", json={"store_id": str(self.store1.id)})
        self.assertEqual(res.status_code, 422)
        self.assertIsNone(self.db.query(StoreOwner).filter(StoreOwner.user_id == self.user1_id).first())

    def test_claim_store_with_complete_evidence_succeeds(self):
        self._login_as(self.user1_id)
        payload = {"store_id": str(self.store1.id), **evidence_for(self.user1_id)}
        res = self.client.post("/ads/claim-store", json=payload)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertFalse(data["is_claimed"])
        self.assertEqual(data["claim_status"], "pending")
        self.assertEqual(data["store_nama"], "Toko Katalog Existing")

        owner = self.db.query(StoreOwner).filter(StoreOwner.user_id == self.user1_id).first()
        self.assertIsNotNone(owner)
        self.assertEqual(owner.status, "pending")

    def test_claim_store_existing_with_applicant_details(self):
        """Jalur A: Klaim toko yang sudah ada di katalog dengan detail pemohon dan dokumen."""
        self._login_as(self.user1_id)
        evidence = evidence_for(self.user1_id)
        payload = {"store_id": str(self.store1.id), **evidence}
        res = self.client.post("/ads/claim-store", json=payload)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertFalse(data["is_claimed"])  # Belum diverifikasi
        self.assertEqual(data["claim_status"], "pending")
        self.assertEqual(data["applicant_name"], "Budi Santoso")
        self.assertEqual(data["applicant_role"], "Pemilik")
        self.assertEqual(data["document_url"], evidence["document_url"])

        # Cek record di DB: kolom fisik yang ada tersimpan dengan baik
        owner = self.db.query(StoreOwner).filter(StoreOwner.user_id == self.user1_id).first()
        self.assertIsNotNone(owner)
        self.assertEqual(owner.status, "pending")
        self.assertEqual(str(owner.store_id), str(self.store1.id))
        self.assertEqual(owner.applicant_name, "Budi Santoso")
        self.assertEqual(owner.applicant_phone, "081234567890")

    def test_submit_new_store_pending_and_not_in_public_catalog(self):
        """Jalur B: Pengajuan toko baru tanpa field kategori berstatus pending dan TIDAK muncul di katalog publik."""
        self._login_as(self.user1_id)
        payload = {
            "store_name": "Warung Baru Makmur",
            "latitude": -7.79,
            "longitude": 110.37,
            "business_entity_name": "CV Makmur Sejahtera",
            "store_address": "Jl. Kaliurang KM 5 No. 12, Sleman",
            "applicant_name": "Siti Rahma",
            "applicant_phone": "089876543210",
            "applicant_role": "Pengurus Perusahaan",
            **{k: v for k, v in evidence_for(self.user1_id).items() if k in ("document_url", "store_photo_url")},
        }
        res = self.client.post("/ads/submit-new-store", json=payload)
        self.assertEqual(res.status_code, 201)
        sub_data = res.json()
        self.assertEqual(sub_data["status"], "pending")
        self.assertEqual(sub_data["submission_type"], "new_store")
        self.assertEqual(sub_data["store_name"], "Warung Baru Makmur")

        # Verifikasi toko TIDAK ada di tabel stores (katalog publik)
        public_store = self.db.query(Store).filter(Store.nama == "Warung Baru Makmur").first()
        self.assertIsNone(public_store)

        # Pengguna melihat status pengajuannya di get_my_store
        my_store_res = self.client.get("/ads/my-store")
        self.assertEqual(my_store_res.status_code, 200)
        my_store = my_store_res.json()
        self.assertFalse(my_store["is_claimed"])
        self.assertEqual(my_store["claim_status"], "pending")
        self.assertEqual(my_store["store_nama"], "Warung Baru Makmur")

    def test_submit_new_store_duplicate_detection(self):
        """Pengajuan toko baru mencegah duplikasi toko katalog publik dan antrean ganda."""
        self._login_as(self.user1_id)

        # 1. Menolak jika nama & alamat sudah ada di katalog toko
        payload_existing = {
            "store_name": "Toko Katalog Existing",
            "latitude": -7.79,
            "longitude": 110.37,
            "store_address": "Jl. Malioboro No. 10, Yogyakarta",
            "applicant_name": "Andi",
            "applicant_phone": "08123456789",
            "applicant_role": "Pemilik"
        }
        res_dup = self.client.post("/ads/submit-new-store", json=payload_existing)
        self.assertEqual(res_dup.status_code, 409)
        self.assertIn("sudah terdaftar di katalog", res_dup.json()["detail"])

        # 2. Menolak jika user mengajukan toko yang sama berulang kali saat masih pending
        payload_new = {
            "store_name": "Toko Unik Baru",
            "latitude": -7.79,
            "longitude": 110.37,
            "store_address": "Jl. Gejayan No. 20, Sleman",
            "applicant_name": "Andi",
            "applicant_phone": "08123456789",
            "applicant_role": "Pemilik"
        }
        res1 = self.client.post("/ads/submit-new-store", json=payload_new)
        self.assertEqual(res1.status_code, 201)

        res2 = self.client.post("/ads/submit-new-store", json=payload_new)
        self.assertEqual(res2.status_code, 409)
        self.assertIn("Anda sudah memiliki pengajuan aktif", res2.json()["detail"])

    def test_my_submissions_isolation(self):
        """Pengguna hanya dapat melihat pengajuan miliknya sendiri."""
        # User 1 mengajukan klaim toko existing
        self._login_as(self.user1_id)
        self.client.post("/ads/claim-store", json={"store_id": str(self.store1.id), **evidence_for(self.user1_id)})

        # User 2 mengajukan toko baru
        self._login_as(self.user2_id)
        self.client.post("/ads/submit-new-store", json={
            "store_name": "Toko Milik User 2",
            "latitude": -7.79,
            "longitude": 110.37,
            "store_address": "Jl. Palagan KM 8",
            "applicant_name": "Dewi User 2",
            "applicant_phone": "0811223344",
            "applicant_role": "Pemilik"
        })

        # Cek pengajuan User 1: hanya 1 klaim, toko user 2 tidak terlihat
        self._login_as(self.user1_id)
        res1 = self.client.get("/ads/my-submissions")
        self.assertEqual(res1.status_code, 200)
        items1 = res1.json()
        self.assertEqual(len(items1), 1)
        self.assertEqual(items1[0]["store_name"], "Toko Katalog Existing")

        # Cek pengajuan User 2: hanya 1 toko baru
        self._login_as(self.user2_id)
        res2 = self.client.get("/ads/my-submissions")
        self.assertEqual(res2.status_code, 200)
        items2 = res2.json()
        self.assertEqual(len(items2), 1)
        self.assertEqual(items2[0]["store_name"], "Toko Milik User 2")

    def test_admin_flow_verify_and_reject_new_store(self):
        """Admin memeriksa pengajuan toko baru, menyetujui atau menolak dengan alasan."""
        # 1. User 1 mengajukan toko baru
        self._login_as(self.user1_id)
        sub_res = self.client.post("/ads/submit-new-store", json={
            "store_name": "Apotek Sehat Baru",
            "latitude": -7.79,
            "longitude": 110.37,
            "store_address": "Jl. Kusumanegara No. 50, Yogyakarta",
            "store_category": "Apotek & Kesehatan",
            "applicant_name": "Ahmad",
            "applicant_phone": "0855667788",
            "applicant_role": "Apoteker Pengelola"
        })
        sub_id = sub_res.json()["id"]

        # 2. Non-admin mencoba verify -> ditolak 403
        self._login_as(self.user2_id, role="user")
        unauth_verify = self.client.post("/ads/verify-new-store", json={"submission_id": sub_id})
        self.assertEqual(unauth_verify.status_code, 403)

        # 3. Admin melihat antrean di pending-claims
        self._login_as(self.admin_id, role="admin")
        pending_res = self.client.get("/ads/pending-claims")
        self.assertEqual(pending_res.status_code, 200)
        pending_items = pending_res.json()
        self.assertTrue(any(p["submission_id"] == sub_id and p["claim_type"] == "new_store" for p in pending_items))

        # 4. Admin menyetujui pengajuan toko baru
        verify_res = self.client.post("/ads/verify-new-store", json={"submission_id": sub_id})
        self.assertEqual(verify_res.status_code, 200)
        new_store_id = verify_res.json()["store_id"]
        self.assertIsNotNone(new_store_id)

        # Verifikasi toko sekarang aktif di katalog publik tabel stores
        created_store = self.db.query(Store).filter(Store.id == new_store_id).first()
        self.assertIsNotNone(created_store)
        self.assertEqual(created_store.nama, "Apotek Sehat Baru")

        # Verifikasi user1 sekarang menjadi pemilik terverifikasi
        owner = self.db.query(StoreOwner).filter(StoreOwner.user_id == self.user1_id).first()
        self.assertIsNotNone(owner)
        self.assertEqual(owner.status, "verified")
        self.assertEqual(str(owner.store_id), str(new_store_id))

        # 5. User 2 mengajukan toko lain yang kemudian ditolak admin dengan alasan
        self._login_as(self.user2_id)
        sub_res2 = self.client.post("/ads/submit-new-store", json={
            "store_name": "Toko Kurang Bukti",
            "latitude": -7.79,
            "longitude": 110.37,
            "store_address": "Jl. Ringroad Utara No. 99",
            "applicant_name": "Rudi",
            "applicant_phone": "08199988877",
            "applicant_role": "Staf"
        })
        sub_id2 = sub_res2.json()["id"]

        self._login_as(self.admin_id, role="admin")
        reject_res = self.client.post("/ads/reject-new-store", json={
            "submission_id": sub_id2,
            "reason": "Foto papan nama toko buram dan NIB tidak terdaftar."
        })
        self.assertEqual(reject_res.status_code, 200)

        # User 2 melihat alasan penolakan
        self._login_as(self.user2_id)
        my_store_u2 = self.client.get("/ads/my-store").json()
        self.assertEqual(my_store_u2["claim_status"], "rejected")
        self.assertEqual(my_store_u2["rejection_reason"], "Foto papan nama toko buram dan NIB tidak terdaftar.")

    def test_submit_new_store_returns_503_when_table_not_available(self):
        """Submit New Store mengembalikan HTTP 503 jika tabel store_submissions belum tersedia di database."""
        from unittest.mock import patch
        self._login_as(self.user1_id)
        payload = {
            "store_name": "Toko Belum Ada Tabel",
            "latitude": -7.79,
            "longitude": 110.37,
            "store_address": "Jl. Kaliurang KM 10",
            "applicant_name": "Doni",
            "applicant_phone": "0812334455",
            "applicant_role": "Pemilik"
        }
        with patch("app.routers.ads._has_store_submissions_table", return_value=False):
            res = self.client.post("/ads/submit-new-store", json=payload)
            self.assertEqual(res.status_code, 503)
            self.assertIn("belum tersedia karena skema database belum diperbarui", res.json()["detail"])

            # Verifikasi tidak ada file JSON yang dibuat
            from pathlib import Path
            private_storage = Path(__file__).resolve().parent.parent / "private_storage"
            self.assertFalse((private_storage / "submissions").exists())
            self.assertFalse((private_storage / "claim_metadata").exists())

    def test_admin_endpoints_return_503_when_table_not_available(self):
        """Admin verify dan reject new store mengembalikan HTTP 503 jika tabel belum tersedia."""
        from unittest.mock import patch
        self._login_as(self.admin_id, role="admin")
        with patch("app.routers.ads._has_store_submissions_table", return_value=False):
            res_verify = self.client.post("/ads/verify-new-store", json={"submission_id": "dummy-id"})
            self.assertEqual(res_verify.status_code, 503)

            res_reject = self.client.post("/ads/reject-new-store", json={"submission_id": "dummy-id", "reason": "Tolak"})
            self.assertEqual(res_reject.status_code, 503)

    def test_no_json_fallback_created_on_store_claim(self):
        """Klaim toko murni database dan sama sekali tidak membuat fallback file JSON."""
        self._login_as(self.user1_id)
        res = self.client.post("/ads/claim-store", json={
            "store_id": str(self.store1.id),
            **evidence_for(self.user1_id),
        })
        self.assertEqual(res.status_code, 200)

        from pathlib import Path
        private_storage = Path(__file__).resolve().parent.parent / "private_storage"
        self.assertFalse((private_storage / "claim_metadata").exists())
        self.assertFalse((private_storage / "submissions").exists())

    def test_admin_does_not_process_unstored_submissions(self):
        """Admin hanya dapat memproses data yang benar-benar tersimpan di database."""
        self._login_as(self.admin_id, role="admin")
        # ID yang tidak ada di database menghasilkan 404, tidak mencari di file JSON
        fake_id = str(uuid.uuid4())
        res_verify = self.client.post("/ads/verify-new-store", json={"submission_id": fake_id})
        self.assertEqual(res_verify.status_code, 404)
        self.assertIn("tidak ditemukan", res_verify.json()["detail"])

        res_reject = self.client.post("/ads/reject-new-store", json={"submission_id": fake_id, "reason": "Alasan"})
        self.assertEqual(res_reject.status_code, 404)
        self.assertIn("tidak ditemukan", res_reject.json()["detail"])


if __name__ == "__main__":
    unittest.main()
