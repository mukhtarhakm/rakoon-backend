import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import app
from app.database import Base, get_db
from app.dependencies import get_current_user
from app.models.db_models import PriceEntry, Product, Store
from app.models.schemas import MAX_BUDGET_ITEMS, MAX_BUDGET_RUPIAH, MAX_ITEM_QTY, VerificationStatus

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


class TestBudgetInputHardening(unittest.TestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = TestingSessionLocal()
        app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(app)

        self.store = Store(nama="Toko Anggaran", lat=-7.78, lng=110.4)
        self.milk = Product(nama="Susu Anggaran 1L", kategori="Minuman", ukuran=1000.0, satuan="ml")
        self.rice = Product(nama="Beras Anggaran 5kg", kategori="Makanan Pokok", ukuran=5.0, satuan="kg")
        self.db.add_all([self.store, self.milk, self.rice])
        self.db.commit()
        for product, harga in ((self.milk, 10000), (self.rice, 60000)):
            self.db.add(PriceEntry(
                product_id=product.id, store_id=self.store.id, harga=harga, sumber_user_id=USER,
                status_verifikasi=VerificationStatus.VERIFIED, timestamp=datetime.now(timezone.utc),
            ))
        self.db.commit()

    def tearDown(self):
        app.dependency_overrides.clear()
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def _recommend(self, items, budget=500000):
        return self.client.post("/budget-shopping/recommend", json={"budget": budget, "items": items})

    # --- P8-04: UUID non-kanonik tidak lagi 500 ---

    def test_uppercase_uuid_is_accepted_and_returned_canonical(self):
        canonical = str(self.milk.id)
        for variant in (canonical.upper(), canonical.replace("-", ""), "{" + canonical.upper() + "}", f"urn:uuid:{canonical}"):
            with self.subTest(variant=variant):
                res = self._recommend([{"product_id": variant, "qty": 2}])
                self.assertEqual(res.status_code, 200, res.text)
                body = res.json()
                self.assertEqual(body["recommended_store"]["nama"], "Toko Anggaran")
                self.assertEqual(body["items"][0]["product_id"], canonical)
                self.assertEqual(body["items"][0]["subtotal"], 20000)

    def test_different_spellings_of_the_same_uuid_are_merged_into_one_line(self):
        canonical = str(self.milk.id)
        res = self._recommend([
            {"product_id": canonical, "qty": 1},
            {"product_id": canonical.upper(), "qty": 2},
        ])
        self.assertEqual(res.status_code, 200, res.text)
        items = res.json()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["qty"], 3)

    def test_non_uuid_product_id_is_reported_unavailable_not_500(self):
        res = self._recommend([{"product_id": "bukan-uuid", "qty": 1}])
        self.assertEqual(res.status_code, 200)
        availability = res.json()["product_availabilities"]
        self.assertFalse(availability[0]["is_available"])

    def test_unknown_but_valid_uuid_is_reported_unavailable_with_one_name_lookup(self):
        res = self._recommend([
            {"product_id": str(self.milk.id), "qty": 1},
            {"product_id": "00000000-0000-0000-0000-000000000099", "qty": 1},
        ])
        self.assertEqual(res.status_code, 200)
        by_id = {a["product_id"]: a for a in res.json()["product_availabilities"]}
        self.assertTrue(by_id[str(self.milk.id)]["is_available"])
        self.assertFalse(by_id["00000000-0000-0000-0000-000000000099"]["is_available"])

    # --- P8-05: batas input ---

    def test_too_many_items_are_rejected(self):
        items = [{"product_id": f"00000000-0000-0000-0000-{i:012d}", "qty": 1} for i in range(MAX_BUDGET_ITEMS + 1)]
        self.assertEqual(self._recommend(items).status_code, 422)

    def test_maximum_number_of_items_is_accepted(self):
        items = [{"product_id": f"00000000-0000-0000-0000-{i:012d}", "qty": 1} for i in range(MAX_BUDGET_ITEMS)]
        self.assertEqual(self._recommend(items).status_code, 200)

    def test_quantity_must_be_between_one_and_the_maximum(self):
        pid = str(self.milk.id)
        for qty in (0, -1, MAX_ITEM_QTY + 1, 999999999):
            with self.subTest(qty=qty):
                self.assertEqual(self._recommend([{"product_id": pid, "qty": qty}]).status_code, 422)
        self.assertEqual(self._recommend([{"product_id": pid, "qty": MAX_ITEM_QTY}]).status_code, 200)

    def test_budget_must_be_finite_positive_and_capped(self):
        pid = str(self.milk.id)
        for budget in (0, -5, MAX_BUDGET_RUPIAH + 1):
            with self.subTest(budget=budget):
                self.assertEqual(self._recommend([{"product_id": pid, "qty": 1}], budget=budget).status_code, 422)
        for literal in ("NaN", "Infinity"):
            with self.subTest(literal=literal):
                res = self.client.post(
                    "/budget-shopping/recommend",
                    content=f'{{"budget": {literal}, "items": [{{"product_id": "{pid}", "qty": 1}}]}}',
                    headers={"Content-Type": "application/json"},
                )
                self.assertEqual(res.status_code, 422)


class TestUnitNormalizationOnConfirm(unittest.TestCase):
    """P8-09: satuan yang diketik/dibaca OCR dinormalkan ke satu ejaan sebelum disimpan."""

    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = TestingSessionLocal()
        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: USER
        self.client = TestClient(app)
        self.store = Store(nama="Toko Satuan", lat=-7.78, lng=110.4)
        self.db.add(self.store)
        self.db.commit()

    def tearDown(self):
        app.dependency_overrides.clear()
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def _confirm_unit(self, nama, satuan):
        res = self.client.post("/scan/confirm", json={
            "store_id": str(self.store.id), "user_id": "x",
            "items": [{"nama_produk": nama, "harga": 5000, "ukuran": 100.0, "satuan": satuan, "kategori": "Lainnya"}],
        })
        self.assertEqual(res.status_code, 201, res.text)
        self.db.expire_all()
        return self.db.query(Product).filter(Product.nama == nama).first().satuan

    def test_weight_volume_and_count_aliases_are_stored_in_one_spelling(self):
        cases = {
            "gr": "g", "Gram": "g", " GRAM ": "g", "g": "g", "gramm": "g",
            "kilogram": "kg", "Kilo": "kg", "kg": "kg",
            "ML": "ml", "milliliter": "ml", "cc": "ml",
            "liter": "l", "Litre": "l", "L": "l",
            "buah": "pcs", "Pack": "pcs", "pcs": "pcs",
        }
        for i, (raw, expected) in enumerate(cases.items()):
            with self.subTest(raw=raw):
                self.assertEqual(self._confirm_unit(f"Produk Satuan {i}", raw), expected)

    def test_unknown_unit_is_kept_trimmed_and_missing_unit_stays_empty(self):
        self.assertEqual(self._confirm_unit("Produk Aneh", "  lusin "), "lusin")
        res = self.client.post("/scan/confirm", json={
            "store_id": str(self.store.id), "user_id": "x",
            "items": [{"nama_produk": "Tanpa Satuan", "harga": 5000, "kategori": "Lainnya"}],
        })
        self.assertEqual(res.status_code, 201)
        self.db.expire_all()
        self.assertIsNone(self.db.query(Product).filter(Product.nama == "Tanpa Satuan").first().satuan)


if __name__ == "__main__":
    unittest.main()
