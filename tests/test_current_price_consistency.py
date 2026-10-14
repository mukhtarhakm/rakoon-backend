import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import app
from app.database import Base, get_db
from app.models.db_models import PriceEntry, Product, Store
from app.models.schemas import VerificationStatus

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


USER = "11111111-1111-1111-1111-111111111111"
V, P, R = VerificationStatus.VERIFIED, VerificationStatus.PENDING, VerificationStatus.REJECTED
LAT, LNG = -7.7805, 110.4005


class TestCurrentPriceConsistency(unittest.TestCase):
    """Katalog, compare, recommended-products, dan budget memakai satu aturan harga terkini."""

    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = TestingSessionLocal()
        app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(app)
        self.now = datetime.now(timezone.utc)

        self.store_a = Store(nama="Toko A", lat=-7.7800, lng=110.4000)
        self.store_b = Store(nama="Toko B", lat=-7.7810, lng=110.4010)
        self.milk = Product(nama="Susu Konsistensi 1L", kategori="Minuman", ukuran=1000.0, satuan="ml")
        self.rice = Product(nama="Beras Lama 5kg", kategori="Makanan Pokok", ukuran=5.0, satuan="kg")
        self.pending_only = Product(nama="Hanya Pending 1L", kategori="Minuman", ukuran=1000.0, satuan="ml")
        self.db.add_all([self.store_a, self.store_b, self.milk, self.rice, self.pending_only])
        self.db.commit()

        # Susu: A punya riwayat (9000 lama -> 12000 terbaru verified), ada pending 5000 dan rejected 1000 yang lebih baru.
        self._add(self.milk, self.store_a, 9000, 5)
        self._add(self.milk, self.store_a, 12000, 1)
        self._add(self.milk, self.store_a, 5000, 0, P)
        self._add(self.milk, self.store_a, 1000, 0, R)
        self._add(self.milk, self.store_b, 15000, 2)
        # Beras: hanya satu harga lama (stale) di A.
        self._add(self.rice, self.store_a, 70000, 200)
        # Hanya pending: tidak punya harga terkini.
        self._add(self.pending_only, self.store_b, 3000, 1, P)

    def tearDown(self):
        app.dependency_overrides.clear()
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def _add(self, product, store, harga, days_ago, status=V):
        self.db.add(PriceEntry(
            product_id=product.id, store_id=store.id, harga=harga, sumber_user_id=USER,
            status_verifikasi=status, timestamp=self.now - timedelta(days=days_ago),
        ))
        self.db.commit()

    # --- jalur-jalur ---

    def _catalog(self):
        res = self.client.get("/products/catalog?limit=100")
        self.assertEqual(res.status_code, 200)
        return {item["id"]: item for item in res.json()}

    def _compare(self, product):
        res = self.client.get(f"/price/compare/{product.id}?lat={LAT}&lng={LNG}&radius_km=5")
        self.assertEqual(res.status_code, 200)
        return {row["store_id"]: row for row in res.json()["comparison"]}

    def _recommended(self):
        res = self.client.get("/recommendation/recommended-products?limit=50")
        self.assertEqual(res.status_code, 200)
        return {item["id"]: item for item in res.json()}

    def _budget(self, product):
        res = self.client.post("/budget-shopping/recommend", json={
            "budget": 1000000, "items": [{"product_id": str(product.id), "qty": 1}],
        })
        self.assertEqual(res.status_code, 200)
        return res.json()

    # --- konsistensi harga ---

    def test_all_four_paths_report_the_same_current_price_for_the_same_data(self):
        pid = str(self.milk.id)
        catalog = self._catalog()[pid]
        compare = self._compare(self.milk)
        recommended = self._recommended()[pid]
        budget = self._budget(self.milk)

        self.assertEqual(catalog["harga_terendah"], 12000)
        self.assertEqual(compare[str(self.store_a.id)]["harga_terbaru"], 12000)
        self.assertEqual(compare[str(self.store_b.id)]["harga_terbaru"], 15000)
        self.assertEqual(recommended["harga"], 12000)
        self.assertEqual(budget["items"][0]["harga_satuan"], 12000)
        self.assertEqual(budget["recommended_store"]["nama"], "Toko A")

        # toko dan tanggal harga yang sama di semua jalur
        self.assertEqual(catalog["nama_toko_terendah"], "Toko A")
        self.assertEqual(recommended["nama_toko"], "Toko A")
        price_ts = self.now - timedelta(days=1)
        for stamp in (catalog["updated_at"], recommended["updated_at"], compare[str(self.store_a.id)]["tanggal_update"]):
            parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            self.assertLess(abs((parsed - price_ts).total_seconds()), 5)

    def test_pending_and_rejected_never_become_the_displayed_price(self):
        pid = str(self.milk.id)
        for price in (
            self._catalog()[pid]["harga_terendah"],
            self._recommended()[pid]["harga"],
            self._compare(self.milk)[str(self.store_a.id)]["harga_terbaru"],
        ):
            self.assertNotIn(price, (5000, 1000))

    def test_fresh_prices_are_not_marked_stale_on_any_path(self):
        pid = str(self.milk.id)
        self.assertFalse(self._catalog()[pid]["stale"])
        self.assertFalse(self._recommended()[pid]["stale"])
        self.assertFalse(self._compare(self.milk)[str(self.store_a.id)]["stale"])
        self.assertFalse(self._budget(self.milk)["items"][0]["stale"])

    # --- produk dengan harga lama saja tetap muncul, ditandai stale ---

    def test_product_with_only_an_old_price_still_appears_with_stale_true_everywhere(self):
        pid = str(self.rice.id)
        catalog = self._catalog()[pid]
        self.assertEqual(catalog["harga_terendah"], 70000)
        self.assertTrue(catalog["stale"])

        recommended = self._recommended()[pid]
        self.assertEqual(recommended["harga"], 70000)
        self.assertTrue(recommended["stale"])

        compare = self._compare(self.rice)
        self.assertEqual(compare[str(self.store_a.id)]["harga_terbaru"], 70000)
        self.assertTrue(compare[str(self.store_a.id)]["stale"])

        budget = self._budget(self.rice)
        self.assertEqual(budget["items"][0]["harga_satuan"], 70000)
        self.assertTrue(budget["items"][0]["stale"])
        self.assertIn("tanggal_harga", budget["items"][0])
        self.assertIsNotNone(budget["items"][0]["tanggal_harga"])

    def test_product_with_only_pending_prices_has_no_price_but_stays_in_the_catalog(self):
        pid = str(self.pending_only.id)
        item = self._catalog()[pid]
        self.assertIsNone(item["harga_terendah"])
        self.assertEqual(item["jumlah_toko"], 0)
        self.assertNotIn(pid, self._recommended())

    def test_a_cheaper_stale_price_does_not_beat_a_fresh_one_in_the_catalog(self):
        self._add(self.milk, self.store_b, 8000, 400)  # lebih murah tapi sangat lama di toko lain? B sudah punya 15000 terbaru
        # B: terbaru tetap 15000 (fresh); 8000 hanya riwayat lama -> tidak berpengaruh
        self.assertEqual(self._catalog()[str(self.milk.id)]["harga_terendah"], 12000)

    def test_catalog_prefers_a_fresh_price_over_a_cheaper_stale_one_from_another_store(self):
        store_c = Store(nama="Toko C", lat=-7.7820, lng=110.4020)
        self.db.add(store_c)
        self.db.commit()
        self._add(self.milk, store_c, 6000, 300)  # satu-satunya harga di C, sangat lama
        item = self._catalog()[str(self.milk.id)]
        self.assertEqual(item["harga_terendah"], 12000)
        self.assertFalse(item["stale"])
        self.assertEqual(item["jumlah_toko"], 3)


if __name__ == "__main__":
    unittest.main()
