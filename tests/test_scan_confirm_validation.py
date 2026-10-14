import os
import sys
import unittest
import uuid
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import app
from app.database import Base, get_db
from app.dependencies import get_current_user
from app.models.db_models import PriceEntry, Product, ScanSession, Store
from app.models.schemas import MAX_CONFIRM_ITEMS, MAX_PRICE_RUPIAH, MAX_PRODUCT_NAME_LENGTH

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


USER_ID = "11111111-1111-1111-1111-111111111111"


class TestScanConfirmValidation(unittest.TestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = TestingSessionLocal()
        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: USER_ID
        self.client = TestClient(app)
        self.store = Store(nama="Toko Uji", lat=-7.75, lng=110.4)
        self.db.add(self.store)
        self.db.commit()
        self.store_id = str(self.store.id)

    def tearDown(self):
        app.dependency_overrides.clear()
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def _confirm(self, items, store_id=None):
        return self.client.post("/scan/confirm", json={
            "store_id": store_id if store_id is not None else self.store_id,
            "user_id": "diabaikan",
            "items": items,
        })

    def _item(self, nama="Susu Uji 1L", harga=15000, **extra):
        item = {"nama_produk": nama, "harga": harga, "ukuran": 1000.0, "satuan": "ml", "kategori": "Minuman"}
        item.update(extra)
        return item

    def _counts(self):
        self.db.expire_all()
        return (
            self.db.query(ScanSession).count(),
            self.db.query(Product).count(),
            self.db.query(PriceEntry).count(),
        )

    # --- harga ---

    def test_price_must_be_positive_and_below_the_cap(self):
        for harga in (0, -1, -50000, MAX_PRICE_RUPIAH + 1, 999999999):
            with self.subTest(harga=harga):
                self.assertEqual(self._confirm([self._item(harga=harga)]).status_code, 422)
        self.assertEqual(self._counts(), (0, 0, 0))

    def test_price_at_the_boundaries_is_accepted(self):
        self.assertEqual(self._confirm([self._item("Murah", harga=1)]).status_code, 201)
        self.assertEqual(self._confirm([self._item("Mahal", harga=MAX_PRICE_RUPIAH)]).status_code, 201)

    # --- jumlah item dan nama ---

    def test_empty_items_are_rejected_without_creating_a_session(self):
        self.assertEqual(self._confirm([]).status_code, 422)
        self.assertEqual(self._counts(), (0, 0, 0))

    def test_too_many_items_are_rejected_and_the_maximum_is_accepted(self):
        too_many = [self._item(f"Produk {i}") for i in range(MAX_CONFIRM_ITEMS + 1)]
        self.assertEqual(self._confirm(too_many).status_code, 422)
        self.assertEqual(self._counts(), (0, 0, 0))

        maximum = [self._item(f"Produk {i}") for i in range(MAX_CONFIRM_ITEMS)]
        res = self._confirm(maximum)
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.json()["items_saved"], MAX_CONFIRM_ITEMS)

    def test_product_name_must_be_non_blank_and_not_too_long(self):
        for nama in ("", "   ", "x" * (MAX_PRODUCT_NAME_LENGTH + 1)):
            with self.subTest(nama_len=len(nama)):
                self.assertEqual(self._confirm([self._item(nama)]).status_code, 422)
        self.assertEqual(self._confirm([self._item("x" * MAX_PRODUCT_NAME_LENGTH)]).status_code, 201)

    # --- toko ---

    def test_unknown_store_returns_404_and_writes_nothing(self):
        for store_id in (str(uuid.uuid4()), "bukan-uuid", 123):
            with self.subTest(store_id=store_id):
                res = self._confirm([self._item()], store_id=store_id)
                self.assertEqual(res.status_code, 404)
        self.assertEqual(self._counts(), (0, 0, 0))

    # --- pencocokan nama tanpa wildcard ---

    def test_percent_and_underscore_in_input_are_literal_not_wildcards(self):
        self.db.add_all([Product(nama="Biskuit Cokelat", kategori="Camilan"), Product(nama="Tehx Pucuk", kategori="Minuman")])
        self.db.commit()

        res = self._confirm([self._item("Biskuit%"), self._item("Teh_Pucuk"), self._item("%")])
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.json()["products_created"], 3)
        names = {p.nama for p in self.db.query(Product).all()}
        self.assertTrue({"Biskuit%", "Teh_Pucuk", "%"} <= names)

    def test_case_insensitive_exact_name_still_reuses_existing_product(self):
        self.db.add(Product(nama="Susu UHT Cokelat", kategori="Minuman"))
        self.db.commit()
        res = self._confirm([self._item("  susu uht COKELAT  ")])
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.json()["products_created"], 0)
        self.assertEqual(self.db.query(Product).count(), 1)

    # --- balapan nama produk ---

    def test_concurrent_creation_of_same_product_name_reuses_existing_instead_of_500(self):
        import app.routers.scan as scan_module
        self.db.add(Product(nama="Roti Tawar", kategori="Makanan Pokok"))
        self.db.commit()

        real_lookup = scan_module._find_product_by_name
        calls = []

        def racing_lookup(db, name):
            calls.append(name)
            return None if len(calls) == 1 else real_lookup(db, name)  # lookup pertama "kalah balapan"

        with patch("app.routers.scan._find_product_by_name", side_effect=racing_lookup):
            res = self._confirm([self._item("Roti Tawar", harga=14000)])

        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.json()["products_created"], 0)
        self.assertEqual(res.json()["items_saved"], 1)
        self.assertEqual(self._counts(), (1, 1, 1))

    def test_unexpected_database_error_still_returns_500_without_leaking_details(self):
        with patch("app.routers.scan._find_product_by_name", side_effect=RuntimeError("secret-internal-detail")):
            res = self._confirm([self._item()])
        self.assertEqual(res.status_code, 500)
        self.assertNotIn("secret-internal-detail", res.text)
        self.assertEqual(self._counts(), (0, 0, 0))


if __name__ == "__main__":
    unittest.main()
