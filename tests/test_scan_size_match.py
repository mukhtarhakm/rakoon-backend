import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import app
from app.database import Base, get_db
from app.dependencies import get_current_user
from app.models.db_models import PriceEntry, Product, Store
from app.models.schemas import VerificationStatus
from app.routers import scan as scan_module

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
SEED_USER = "33333333-3333-3333-3333-333333333333"


class TestConfirmSizeAwareMatching(unittest.TestCase):
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

    def _seed(self, nama="Ultra Milk Cokelat", ukuran=1000.0, satuan="ml", prices=(30000, 30500, 31000)):
        product = Product(nama=nama, kategori="Minuman", ukuran=ukuran, satuan=satuan)
        self.db.add(product)
        self.db.commit()
        for harga in prices:
            self.db.add(PriceEntry(
                product_id=product.id, store_id=self.store_id, harga=harga,
                sumber_user_id=SEED_USER, status_verifikasi=VerificationStatus.VERIFIED,
            ))
        self.db.commit()
        return product

    def _confirm(self, nama, harga, ukuran=200.0, satuan="ml", kategori="Minuman"):
        item = {"nama_produk": nama, "harga": harga, "kategori": kategori}
        if ukuran is not None:
            item["ukuran"] = ukuran
        if satuan is not None:
            item["satuan"] = satuan
        return self.client.post("/scan/confirm", json={
            "store_id": self.store_id, "user_id": "diabaikan", "items": [item],
        })

    def _names(self):
        self.db.expire_all()
        return sorted(p.nama for p in self.db.query(Product).all())

    def _entries_of(self, nama):
        self.db.expire_all()
        product = self.db.query(Product).filter(Product.nama == nama).one()
        return self.db.query(PriceEntry).filter(PriceEntry.product_id == product.id).all()

    def test_different_size_goes_to_a_new_variant_and_leaves_the_base_product_untouched(self):
        self._seed()
        res = self._confirm("Ultra Milk Cokelat", 7900, ukuran=200.0, satuan="ml", kategori="Minuman")
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.json()["products_created"], 1)
        self.assertEqual(self._names(), ["Ultra Milk Cokelat", "Ultra Milk Cokelat (200ml)"])

        variant = self.db.query(Product).filter(Product.nama == "Ultra Milk Cokelat (200ml)").one()
        self.assertEqual((variant.ukuran, variant.satuan, variant.kategori), (200.0, "ml", "Minuman"))
        self.assertEqual([e.harga for e in self._entries_of("Ultra Milk Cokelat (200ml)")], [7900])
        self.assertEqual(sorted(e.harga for e in self._entries_of("Ultra Milk Cokelat")), [30000, 30500, 31000])

    def test_second_confirmation_of_the_same_size_reuses_the_variant(self):
        self._seed()
        self._confirm("Ultra Milk Cokelat", 7900, ukuran=200.0, satuan="ml")
        res = self._confirm("ultra milk cokelat", 8000, ukuran=0.2, satuan="l")  # ejaan lain, ukuran sama
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.json()["products_created"], 0)
        self.assertEqual(len(self._names()), 2)
        self.assertEqual(sorted(e.harga for e in self._entries_of("Ultra Milk Cokelat (200ml)")), [7900, 8000])

    def test_same_size_in_another_unit_spelling_uses_the_same_product(self):
        self._seed()  # 1000 ml
        for ukuran, satuan in ((1.0, "l"), (1.0, "liter"), (1000.0, "ml")):
            with self.subTest(ukuran=ukuran, satuan=satuan):
                res = self._confirm("Ultra Milk Cokelat", 30200 + int(ukuran), ukuran=ukuran, satuan=satuan)
                self.assertEqual(res.status_code, 201)
                self.assertEqual(res.json()["products_created"], 0)
        self.assertEqual(self._names(), ["Ultra Milk Cokelat"])

    def test_weight_units_are_normalized_too(self):
        self._seed(nama="Beras Uji", ukuran=1.0, satuan="kg", prices=(15000,))
        self.assertEqual(self._confirm("Beras Uji", 15500, ukuran=1000.0, satuan="gr").json()["products_created"], 0)
        self.assertEqual(self._confirm("Beras Uji", 7900, ukuran=500.0, satuan="gram").json()["products_created"], 1)
        self.assertEqual(self._names(), ["Beras Uji", "Beras Uji (500g)"])

    def test_item_without_size_or_unit_matches_by_name_as_before(self):
        self._seed()
        for ukuran, satuan in ((None, None), (200.0, None), (None, "ml")):
            with self.subTest(ukuran=ukuran, satuan=satuan):
                res = self._confirm("Ultra Milk Cokelat", 30100, ukuran=ukuran, satuan=satuan)
                self.assertEqual(res.status_code, 201)
                self.assertEqual(res.json()["products_created"], 0)
        self.assertEqual(self._names(), ["Ultra Milk Cokelat"])

    def test_existing_product_without_size_matches_by_name_as_before(self):
        self._seed(ukuran=None, satuan=None)
        res = self._confirm("Ultra Milk Cokelat", 30100, ukuran=200.0, satuan="ml")
        self.assertEqual(res.json()["products_created"], 0)
        self.assertEqual(self._names(), ["Ultra Milk Cokelat"])

    def test_unknown_unit_is_not_comparable_and_matches_by_name(self):
        self._seed()
        res = self._confirm("Ultra Milk Cokelat", 30100, ukuran=3.0, satuan="lusin")
        self.assertEqual(res.json()["products_created"], 0)
        self.assertEqual(self._names(), ["Ultra Milk Cokelat"])

    def test_name_that_already_carries_the_matching_suffix_does_not_get_a_second_one(self):
        self._seed(nama="Indomilk Dalgona Coffee (250ml)", ukuran=250.0, satuan="ml", prices=(9000,))
        res = self._confirm("Indomilk Dalgona Coffee (250ml)", 9100, ukuran=250.0, satuan="ml")
        self.assertEqual(res.json()["products_created"], 0)
        self.assertEqual(self._names(), ["Indomilk Dalgona Coffee (250ml)"])

        # sufiks lama tidak cocok dengan ukuran item: varian dibuat dari nama dasar, bukan "(250ml) (200ml)"
        res = self._confirm("Indomilk Dalgona Coffee (250ml)", 7000, ukuran=200.0, satuan="ml")
        self.assertEqual(res.json()["products_created"], 1)
        self.assertEqual(self._names(), ["Indomilk Dalgona Coffee (200ml)", "Indomilk Dalgona Coffee (250ml)"])

    def test_variant_name_uses_plain_numbers_without_trailing_zero(self):
        self._seed(nama="Minyak Uji", ukuran=1.0, satuan="l", prices=(20000,))
        self._confirm("Minyak Uji", 11000, ukuran=0.5, satuan="l")
        self._confirm("Minyak Uji", 52000, ukuran=2.5, satuan="l")
        self.assertEqual(self._names(), ["Minyak Uji", "Minyak Uji (2500ml)", "Minyak Uji (500ml)"])

    def test_variant_lookup_is_case_insensitive_and_literal(self):
        self._seed()
        self.db.add(Product(nama="Ultra Milk Cokelat (200ML)", kategori="Minuman", ukuran=200.0, satuan="ml"))
        self.db.add(Product(nama="Ultra Milk Cokelat (2%0ml)", kategori="Minuman"))
        self.db.commit()
        res = self._confirm("Ultra Milk Cokelat", 7900, ukuran=200.0, satuan="ml")
        self.assertEqual(res.json()["products_created"], 0)
        self.assertEqual(len(self._entries_of("Ultra Milk Cokelat (200ML)")), 1)

    def test_concurrent_confirmation_of_a_new_variant_ends_with_one_product(self):
        self._seed()
        self._confirm("Ultra Milk Cokelat", 7900, ukuran=200.0, satuan="ml")  # varian sudah dibuat pihak lain
        real_lookup = scan_module._find_product_by_name
        seen = set()

        def racing_lookup(db, name):
            if name.endswith("(200ml)") and name not in seen:
                seen.add(name)
                return None  # lookup varian pertama "kalah balapan"
            return real_lookup(db, name)

        with patch("app.routers.scan._find_product_by_name", side_effect=racing_lookup):
            res = self._confirm("Ultra Milk Cokelat", 8100, ukuran=200.0, satuan="ml")
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.json()["products_created"], 0)
        self.assertEqual(self._names(), ["Ultra Milk Cokelat", "Ultra Milk Cokelat (200ml)"])
        self.assertEqual(sorted(e.harga for e in self._entries_of("Ultra Milk Cokelat (200ml)")), [7900, 8100])

    def test_price_moderation_is_per_variant(self):
        self._seed()  # median produk 1000 ml = 30500, pita bawah 9150
        res = self._confirm("Ultra Milk Cokelat", 7900, ukuran=200.0, satuan="ml")
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.json()["items_pending_review"], 0)
        self.assertEqual(self._entries_of("Ultra Milk Cokelat (200ml)")[0].status_verifikasi, VerificationStatus.VERIFIED)

        # varian dengan >= 3 sampel terverifikasi dimoderasi terhadap mediannya sendiri
        variant = self.db.query(Product).filter(Product.nama == "Ultra Milk Cokelat (200ml)").one()
        for harga in (7800, 8000):
            self.db.add(PriceEntry(
                product_id=variant.id, store_id=self.store_id, harga=harga,
                sumber_user_id=SEED_USER, status_verifikasi=VerificationStatus.VERIFIED,
            ))
        self.db.commit()
        res = self._confirm("Ultra Milk Cokelat", 30000, ukuran=200.0, satuan="ml")  # wajar untuk 1000 ml, tidak untuk 200 ml
        self.assertEqual(res.json()["items_pending_review"], 1)

    def test_response_shape_is_unchanged(self):
        self._seed()
        body = self._confirm("Ultra Milk Cokelat", 7900, ukuran=200.0, satuan="ml").json()
        self.assertEqual(
            set(body), {"items_saved", "products_created", "items_pending_review", "message", "scan_session_id"}
        )


if __name__ == "__main__":
    unittest.main()
