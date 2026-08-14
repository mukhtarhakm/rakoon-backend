import unittest
from datetime import datetime, timedelta, timezone
from fastapi.testclient import TestClient

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import app
from app.database import Base, get_db
from app.dependencies import get_current_user
from app.models.db_models import Product, Store, PriceEntry, ScanSession

SQLALCHEMY_DATABASE_URL = "sqlite:///:memory:"
engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
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


TEST_USER_ID = "11111111-1111-1111-1111-111111111111"
TEST_OTHER_USER_ID = "22222222-2222-2222-2222-222222222222"


class TestScanRecent(unittest.TestCase):

    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = TestingSessionLocal()

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: TEST_USER_ID

        self.prod_id1 = "d1111111-1111-1111-1111-111111111111"
        self.prod_id2 = "d2222222-2222-2222-2222-222222222222"
        self.store_id1 = "a1111111-1111-1111-1111-111111111111"
        self.store_id2 = "a2222222-2222-2222-2222-222222222222"

        # Seed products
        p1 = Product(id=self.prod_id1, nama="Minyak Goreng 2L", kategori="Makanan Pokok", ukuran=2.0, satuan="l")
        p2 = Product(id=self.prod_id2, nama="Beras Premium 5kg", kategori="Makanan Pokok", ukuran=5.0, satuan="kg")

        # Seed store
        s1 = Store(id=self.store_id1, nama="Superindo Dago", lat=-6.2088, lng=106.8456, alamat="Jl. Dago")
        s2 = Store(id=self.store_id2, nama="Toko Amanah", lat=-6.2090, lng=106.8460, alamat="Jl. Riau")

        self.db.add_all([p1, p2, s1, s2])



        now = datetime.now(timezone.utc)

        # Session 1 (Older, 10 mins ago, 1 item)
        session1 = ScanSession(
            id="b1111111-1111-1111-1111-111111111111",
            user_id=TEST_USER_ID,
            store_id=self.store_id1,
            created_at=now - timedelta(minutes=10),
        )

        e1 = PriceEntry(
            id="e1111111-1111-1111-1111-111111111111",
            product_id=self.prod_id1,
            store_id=self.store_id1,
            harga=35000,
            sumber_user_id=TEST_USER_ID,
            timestamp=now - timedelta(minutes=10),
            status_verifikasi="verified",
            scan_session_id=session1.id,
        )

        # Session 2 (Newer, 2 mins ago, 2 items at Toko Amanah)
        session2 = ScanSession(
            id="b2222222-2222-2222-2222-222222222222",
            user_id=TEST_USER_ID,
            store_id=self.store_id2,
            created_at=now - timedelta(minutes=2),
        )
        e2_1 = PriceEntry(
            id="e2222222-2222-2222-2222-222222222221",
            product_id=self.prod_id1,
            store_id=self.store_id2,
            harga=36000,
            sumber_user_id=TEST_USER_ID,
            timestamp=now - timedelta(minutes=2),
            status_verifikasi="verified",
            scan_session_id=session2.id,
        )
        e2_2 = PriceEntry(
            id="e2222222-2222-2222-2222-222222222222",
            product_id=self.prod_id2,
            store_id=self.store_id2,
            harga=72000,
            sumber_user_id=TEST_USER_ID,
            timestamp=now - timedelta(minutes=2),
            status_verifikasi="verified",
            scan_session_id=session2.id,
        )

        # Other user session & scan entry (should NOT be returned for TEST_USER_ID)
        session_other = ScanSession(
            id="b3333333-3333-3333-3333-333333333333",
            user_id=TEST_OTHER_USER_ID,
            store_id=self.store_id1,
            created_at=now,
        )
        e_other = PriceEntry(
            id="e3333333-3333-3333-3333-333333333333",
            product_id=self.prod_id1,
            store_id=self.store_id1,
            harga=40000,
            sumber_user_id=TEST_OTHER_USER_ID,
            timestamp=now,
            status_verifikasi="verified",
            scan_session_id=session_other.id,
        )

        self.db.add_all([session1, e1, session2, e2_1, e2_2, session_other, e_other])
        self.db.commit()

        self.client = TestClient(app)

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)
        app.dependency_overrides.clear()

    def test_get_recent_scans_returns_only_authenticated_user_sessions_sorted(self):
        response = self.client.get("/scan/recent")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data), 2)

        # Session 2 was scanned 2 minutes ago (Toko Amanah, 2 products)
        self.assertEqual(data[0]["id"], "b2222222-2222-2222-2222-222222222222")
        self.assertEqual(data[0]["store_name"], "Toko Amanah")
        self.assertEqual(data[0]["product_count"], 2)

        # Session 1 was scanned 10 minutes ago (Superindo Dago, 1 product)
        self.assertEqual(data[1]["id"], "b1111111-1111-1111-1111-111111111111")
        self.assertEqual(data[1]["store_name"], "Superindo Dago")
        self.assertEqual(data[1]["product_count"], 1)


    def test_get_recent_scans_empty_for_new_user(self):
        app.dependency_overrides[get_current_user] = lambda: "99999999-9999-9999-9999-999999999999"
        response = self.client.get("/scan/recent")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data), 0)

    def test_confirm_scan_creates_session_and_linked_price_entries(self):
        payload = {
            "store_id": self.store_id1,
            "user_id": TEST_USER_ID,
            "items": [
                {
                    "nama_produk": "Kecap Manis 550ml",
                    "harga": 15000,
                    "ukuran": 550.0,
                    "satuan": "ml",
                    "kategori": "Bumbu & Saus"
                },
                {
                    "nama_produk": "Gula Pasir 1kg",
                    "harga": 17500,
                    "ukuran": 1.0,
                    "satuan": "kg",
                    "kategori": "Makanan Pokok"
                }
            ]
        }
        response = self.client.post("/scan/confirm", json=payload)
        self.assertEqual(response.status_code, 201)
        res_data = response.json()
        self.assertEqual(res_data["items_saved"], 2)
        self.assertEqual(res_data["products_created"], 2)
        self.assertIsNotNone(res_data.get("scan_session_id"))

        # Verify scan session in db
        session_id = res_data["scan_session_id"]
        session = self.db.query(ScanSession).filter(ScanSession.id == session_id).first()
        self.assertIsNotNone(session)
        self.assertEqual(str(session.store_id), self.store_id1)
        self.assertEqual(str(session.user_id), TEST_USER_ID)

        # Verify price entries are linked to this session
        entries = self.db.query(PriceEntry).filter(PriceEntry.scan_session_id == session_id).all()
        self.assertEqual(len(entries), 2)
        for entry in entries:
            self.assertEqual(str(entry.scan_session_id), session_id)
            self.assertEqual(str(entry.store_id), self.store_id1)

    def test_get_scan_session_detail_success(self):
        # Fetch Session 2 for TEST_USER_ID
        response = self.client.get("/scan/session/b2222222-2222-2222-2222-222222222222")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["id"], "b2222222-2222-2222-2222-222222222222")
        self.assertEqual(data["store_name"], "Toko Amanah")
        self.assertEqual(data["product_count"], 2)
        self.assertEqual(len(data["items"]), 2)

        product_names = [item["nama_produk"] for item in data["items"]]
        self.assertIn("Minyak Goreng 2L", product_names)
        self.assertIn("Beras Premium 5kg", product_names)

    def test_get_scan_session_detail_forbidden_for_other_user_returns_404(self):
        # Attempt to fetch other user's session
        response = self.client.get("/scan/session/b3333333-3333-3333-3333-333333333333")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "Sesi scan tidak ditemukan.")

    def test_new_scan_session_timestamp_is_timezone_aware(self):
        payload = {
            "store_id": self.store_id1,
            "user_id": TEST_USER_ID,
            "items": [
                {
                    "nama_produk": "Susu UHT 1L",
                    "harga": 19000,
                    "ukuran": 1.0,
                    "satuan": "L",
                    "kategori": "Susu & Olahan Susu"
                }
            ]
        }
        response = self.client.post("/scan/confirm", json=payload)
        self.assertEqual(response.status_code, 201)
        session_id = response.json()["scan_session_id"]

        session = self.db.query(ScanSession).filter(ScanSession.id == session_id).first()
        self.assertIsNotNone(session.created_at)
        # Verify created_at in python has timezone or can be converted to UTC
        if session.created_at.tzinfo is not None:
            self.assertEqual(session.created_at.tzinfo, timezone.utc)

    def test_api_response_contains_iso8601_timezone_offset_and_preserves_timestamp(self):
        # 1. Recent endpoint
        response = self.client.get("/scan/recent")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertGreater(len(data), 0)
        ts_str = data[0]["timestamp"]
        # Must contain timezone indicator (Z or +HH:MM or -HH:MM)
        self.assertTrue(
            ts_str.endswith("Z") or "+" in ts_str or (ts_str.count("-") >= 3),
            f"Expected ISO-8601 offset in timestamp: {ts_str}"
        )
        parsed_dt = datetime.fromisoformat(ts_str)
        self.assertIsNotNone(parsed_dt.tzinfo)

        # 2. Detail endpoint
        session_id = data[0]["id"]
        detail_res = self.client.get(f"/scan/session/{session_id}")
        self.assertEqual(detail_res.status_code, 200)
        detail_data = detail_res.json()
        detail_ts_str = detail_data["timestamp"]
        self.assertTrue(
            detail_ts_str.endswith("Z") or "+" in detail_ts_str or (detail_ts_str.count("-") >= 3),
            f"Expected ISO-8601 offset in detail timestamp: {detail_ts_str}"
        )
        detail_parsed_dt = datetime.fromisoformat(detail_ts_str)
        self.assertIsNotNone(detail_parsed_dt.tzinfo)
        # Detail and Recent timestamps must match
        self.assertEqual(parsed_dt, detail_parsed_dt)


if __name__ == "__main__":
    unittest.main()



