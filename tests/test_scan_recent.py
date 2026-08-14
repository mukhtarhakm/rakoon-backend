import unittest
from datetime import datetime, timedelta
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import app
from app.database import Base, get_db
from app.dependencies import get_current_user
from app.models.db_models import Product, Store, PriceEntry

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

app.dependency_overrides[get_db] = override_get_db
app.dependency_overrides[get_current_user] = lambda: TEST_USER_ID


class TestScanRecent(unittest.TestCase):

    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = TestingSessionLocal()

        self.prod_id1 = "d1111111-1111-1111-1111-111111111111"
        self.prod_id2 = "d2222222-2222-2222-2222-222222222222"
        self.store_id1 = "a1111111-1111-1111-1111-111111111111"

        # Seed products
        p1 = Product(id=self.prod_id1, nama="Minyak Goreng 2L", kategori="Makanan Pokok", ukuran=2.0, satuan="l")
        p2 = Product(id=self.prod_id2, nama="Beras Premium 5kg", kategori="Makanan Pokok", ukuran=5.0, satuan="kg")

        # Seed store
        s1 = Store(id=self.store_id1, nama="Superindo Dago", lat=-6.2088, lng=106.8456, alamat="Jl. Dago")

        self.db.add_all([p1, p2, s1])

        now = datetime.utcnow()

        # User 1 scan entries
        e1 = PriceEntry(
            id="e1111111-1111-1111-1111-111111111111",
            product_id=self.prod_id1,
            store_id=self.store_id1,
            harga=35000,
            sumber_user_id=TEST_USER_ID,
            timestamp=now - timedelta(minutes=10),
            status_verifikasi="verified",
        )
        e2 = PriceEntry(
            id="e2222222-2222-2222-2222-222222222222",
            product_id=self.prod_id2,
            store_id=self.store_id1,
            harga=72000,
            sumber_user_id=TEST_USER_ID,
            timestamp=now - timedelta(minutes=2),
            status_verifikasi="verified",
        )

        # Other user scan entry (should NOT be returned for TEST_USER_ID)
        e_other = PriceEntry(
            id="e3333333-3333-3333-3333-333333333333",
            product_id=self.prod_id1,
            store_id=self.store_id1,
            harga=40000,
            sumber_user_id=TEST_OTHER_USER_ID,
            timestamp=now,
            status_verifikasi="verified",
        )

        self.db.add_all([e1, e2, e_other])
        self.db.commit()

        self.client = TestClient(app)

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def test_get_recent_scans_returns_only_authenticated_user_items_sorted(self):
        response = self.client.get("/scan/recent")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data), 2)

        # e2 was scanned 2 minutes ago, e1 was scanned 10 minutes ago
        self.assertEqual(data[0]["nama_produk"], "Beras Premium 5kg")
        self.assertEqual(data[0]["harga"], 72000)
        self.assertEqual(data[0]["store_name"], "Superindo Dago")

        self.assertEqual(data[1]["nama_produk"], "Minyak Goreng 2L")
        self.assertEqual(data[1]["harga"], 35000)

    def test_get_recent_scans_empty_for_new_user(self):
        app.dependency_overrides[get_current_user] = lambda: "99999999-9999-9999-9999-999999999999"
        try:
            response = self.client.get("/scan/recent")
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(len(data), 0)
        finally:
            app.dependency_overrides[get_current_user] = lambda: TEST_USER_ID


if __name__ == "__main__":
    unittest.main()
