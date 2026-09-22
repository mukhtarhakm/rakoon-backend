"""
Tests for GET /price/compare/{product_id}
Phase 3.3.2 — verifies store_id, lat, lng fields, N+1 elimination, deterministic sort,
unavailable-price store behavior, and existing price ranking semantics.
"""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.main import app
from app.database import Base, get_db
from app.models.db_models import Store, Product, PriceEntry

# ---------------------------------------------------------------------------
# In-memory test database
# ---------------------------------------------------------------------------
SQLALCHEMY_DATABASE_URL = "sqlite:///./test_price_compare.db"
engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = override_get_db


# ---------------------------------------------------------------------------
# Fixtures helpers
# ---------------------------------------------------------------------------
def _make_store(db, name: str, lat: float, lng: float) -> Store:
    store = Store(nama=name, lat=lat, lng=lng, alamat=None)
    db.add(store)
    db.commit()
    db.refresh(store)
    return store


def _make_product(db, name: str = "Indomie Goreng") -> Product:
    product = Product(nama=name, kategori="Makanan Instan")
    db.add(product)
    db.commit()
    db.refresh(product)
    return product


def _make_price(db, product_id, store_id, harga: int, status: str = "verified",
                dt: datetime = None) -> PriceEntry:
    entry = PriceEntry(
        product_id=product_id,
        store_id=store_id,
        harga=harga,
        sumber_user_id="00000000-0000-0000-0000-000000000001",
        timestamp=dt or datetime.now(timezone.utc),
        status_verifikasi=status,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


# ---------------------------------------------------------------------------
# Test class
# ---------------------------------------------------------------------------
class TestPriceCompare(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        Base.metadata.create_all(bind=engine)
        cls.client = TestClient(app)

        from app.dependencies import get_current_user
        app.dependency_overrides[get_current_user] = lambda: "00000000-0000-0000-0000-000000000001"

    @classmethod
    def tearDownClass(cls):
        from app.dependencies import get_current_user
        app.dependency_overrides.pop(get_current_user, None)
        Base.metadata.drop_all(bind=engine)
        engine.dispose()
        if os.path.exists("./test_price_compare.db"):
            os.remove("./test_price_compare.db")

    def setUp(self):
        from app.dependencies import get_current_user
        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: "00000000-0000-0000-0000-000000000001"
        db = TestingSessionLocal()
        db.query(PriceEntry).delete()
        db.query(Product).delete()
        db.query(Store).delete()
        db.commit()
        db.close()


    # -----------------------------------------------------------------------
    # 1. Response contains store_id
    # -----------------------------------------------------------------------
    def test_compare_item_contains_store_id(self):
        db = TestingSessionLocal()
        product = _make_product(db)
        store = _make_store(db, "Indomaret Pusat", lat=-6.2001, lng=106.8001)
        _make_price(db, product.id, store.id, harga=18000)
        product_id = str(product.id)
        store_id = str(store.id)
        db.close()

        resp = self.client.get(
            f"/price/compare/{product_id}",
            params={"lat": -6.2, "lng": 106.8, "radius_km": 5.0},
        )
        self.assertEqual(resp.status_code, 200)
        items = resp.json()["comparison"]
        self.assertGreater(len(items), 0)
        self.assertIn("store_id", items[0])
        self.assertEqual(items[0]["store_id"], store_id)

    # -----------------------------------------------------------------------
    # 2. Response contains lat and lng
    # -----------------------------------------------------------------------
    def test_compare_item_contains_lat_lng(self):
        db = TestingSessionLocal()
        product = _make_product(db)
        store = _make_store(db, "Alfamart Maju", lat=-6.2002, lng=106.8002)
        _make_price(db, product.id, store.id, harga=19000)
        product_id = str(product.id)
        db.close()

        resp = self.client.get(
            f"/price/compare/{product_id}",
            params={"lat": -6.2, "lng": 106.8, "radius_km": 5.0},
        )
        self.assertEqual(resp.status_code, 200)
        item = resp.json()["comparison"][0]
        self.assertIn("lat", item)
        self.assertIn("lng", item)
        self.assertAlmostEqual(item["lat"], -6.2002, places=4)
        self.assertAlmostEqual(item["lng"], 106.8002, places=4)

    # -----------------------------------------------------------------------
    # 3. Coordinates map to correct store
    # -----------------------------------------------------------------------
    def test_coordinates_map_to_correct_store(self):
        db = TestingSessionLocal()
        product = _make_product(db)
        store_a = _make_store(db, "Toko A", lat=-6.201, lng=106.801)
        store_b = _make_store(db, "Toko B", lat=-6.202, lng=106.802)
        _make_price(db, product.id, store_a.id, harga=15000)
        _make_price(db, product.id, store_b.id, harga=20000)
        product_id = str(product.id)
        store_a_id = str(store_a.id)
        store_b_id = str(store_b.id)
        db.close()

        resp = self.client.get(
            f"/price/compare/{product_id}",
            params={"lat": -6.2, "lng": 106.8, "radius_km": 5.0},
        )
        self.assertEqual(resp.status_code, 200)
        items = resp.json()["comparison"]
        by_id = {item["store_id"]: item for item in items}

        self.assertAlmostEqual(by_id[store_a_id]["lat"], -6.201, places=3)
        self.assertAlmostEqual(by_id[store_b_id]["lat"], -6.202, places=3)

    # -----------------------------------------------------------------------
    # 4. N+1 regression — total DB queries <= 5 for 5 stores
    # -----------------------------------------------------------------------
    def test_no_n_plus_1_query(self):
        """Ensure the endpoint issues at most 5 DB statements for 5 stores (not 5+1 per store)."""
        db = TestingSessionLocal()
        product = _make_product(db)
        for i in range(5):
            store = _make_store(db, f"Toko {i}", lat=-6.200 + i * 0.001, lng=106.800 + i * 0.001)
            _make_price(db, product.id, store.id, harga=10000 + i * 1000)
        product_id = str(product.id)
        db.close()

        query_count = []

        @event.listens_for(engine, "before_cursor_execute")
        def count_queries(conn, cursor, statement, parameters, context, executemany):
            query_count.append(statement)

        try:
            resp = self.client.get(
                f"/price/compare/{product_id}",
                params={"lat": -6.2, "lng": 106.8, "radius_km": 5.0},
            )
        finally:
            event.remove(engine, "before_cursor_execute", count_queries)

        self.assertEqual(resp.status_code, 200)
        # N+1 would be 5+3=8+. Batch query: <=5 total (with framework overhead)
        self.assertLessEqual(len(query_count), 5,
            f"Too many queries ({len(query_count)}); N+1 pattern likely present.\n"
            f"Queries executed:\n" + "\n".join(q[:60] for q in query_count))

    # -----------------------------------------------------------------------
    # 5. Price ranking semantics preserved (cheapest first)
    # -----------------------------------------------------------------------
    def test_cheapest_store_is_first(self):
        db = TestingSessionLocal()
        product = _make_product(db)
        store_cheap = _make_store(db, "Toko Murah", lat=-6.201, lng=106.801)
        store_exp = _make_store(db, "Toko Mahal", lat=-6.202, lng=106.802)
        _make_price(db, product.id, store_cheap.id, harga=5000)
        _make_price(db, product.id, store_exp.id, harga=15000)
        product_id = str(product.id)
        db.close()

        resp = self.client.get(
            f"/price/compare/{product_id}",
            params={"lat": -6.2, "lng": 106.8, "radius_km": 5.0},
        )
        self.assertEqual(resp.status_code, 200)
        items = resp.json()["comparison"]
        priced = [i for i in items if i["harga_terbaru"] is not None]
        prices = [i["harga_terbaru"] for i in priced]
        self.assertEqual(prices, sorted(prices))

    # -----------------------------------------------------------------------
    # 6. Unavailable price store still has coordinates
    # -----------------------------------------------------------------------
    def test_unavailable_price_store_has_coordinates(self):
        db = TestingSessionLocal()
        product = _make_product(db)
        store_nodata = _make_store(db, "Toko Kosong", lat=-6.203, lng=106.803)
        product_id = str(product.id)
        store_nodata_id = str(store_nodata.id)
        db.close()

        resp = self.client.get(
            f"/price/compare/{product_id}",
            params={"lat": -6.2, "lng": 106.8, "radius_km": 5.0},
        )
        self.assertEqual(resp.status_code, 200)
        items = resp.json()["comparison"]
        nodata = next((i for i in items if i["store_id"] == store_nodata_id), None)
        self.assertIsNotNone(nodata, "Store with no price should still appear in comparison list")
        self.assertIsNone(nodata["harga_terbaru"])
        self.assertAlmostEqual(nodata["lat"], -6.203, places=3)
        self.assertAlmostEqual(nodata["lng"], 106.803, places=3)
        self.assertIsNotNone(nodata["pesan"])

    # -----------------------------------------------------------------------
    # 7. Deterministic sort: same price -> jarak_km ASC
    # -----------------------------------------------------------------------
    def test_deterministic_sort_on_price_tie(self):
        db = TestingSessionLocal()
        product = _make_product(db)
        store_close = _make_store(db, "Toko Dekat", lat=-6.2001, lng=106.8001)
        store_far = _make_store(db, "Toko Jauh", lat=-6.210, lng=106.810)
        _make_price(db, product.id, store_close.id, harga=12000)
        _make_price(db, product.id, store_far.id, harga=12000)
        product_id = str(product.id)
        db.close()

        resp = self.client.get(
            f"/price/compare/{product_id}",
            params={"lat": -6.2, "lng": 106.8, "radius_km": 5.0},
        )
        self.assertEqual(resp.status_code, 200)
        items = resp.json()["comparison"]
        priced = [i for i in items if i["harga_terbaru"] is not None]
        self.assertGreaterEqual(len(priced), 2)
        self.assertLess(priced[0]["jarak_km"], priced[1]["jarak_km"])

    # -----------------------------------------------------------------------
    # 8. Rejected price entries are ignored
    # -----------------------------------------------------------------------
    def test_rejected_price_ignored(self):
        db = TestingSessionLocal()
        product = _make_product(db)
        store = _make_store(db, "Toko Rejected", lat=-6.201, lng=106.801)
        _make_price(db, product.id, store.id, harga=9000, status="rejected")
        product_id = str(product.id)
        store_id = str(store.id)
        db.close()

        resp = self.client.get(
            f"/price/compare/{product_id}",
            params={"lat": -6.2, "lng": 106.8, "radius_km": 5.0},
        )
        self.assertEqual(resp.status_code, 200)
        items = resp.json()["comparison"]
        store_item = next((i for i in items if i["store_id"] == store_id), None)
        self.assertIsNotNone(store_item)
        self.assertIsNone(store_item["harga_terbaru"])

    # -----------------------------------------------------------------------
    # 9. Latest price wins when multiple entries exist for same store
    # -----------------------------------------------------------------------
    def test_latest_price_wins(self):
        from datetime import timezone
        db = TestingSessionLocal()
        product = _make_product(db)
        store = _make_store(db, "Toko Harga", lat=-6.201, lng=106.801)
        old_dt = datetime.now(timezone.utc) - timedelta(days=2)
        new_dt = datetime.now(timezone.utc)
        _make_price(db, product.id, store.id, harga=10000, dt=old_dt.replace(tzinfo=None))
        _make_price(db, product.id, store.id, harga=8000, dt=new_dt.replace(tzinfo=None))
        product_id = str(product.id)
        store_id = str(store.id)
        db.close()

        resp = self.client.get(
            f"/price/compare/{product_id}",
            params={"lat": -6.2, "lng": 106.8, "radius_km": 5.0},
        )
        self.assertEqual(resp.status_code, 200)
        items = resp.json()["comparison"]
        item = next(i for i in items if i["store_id"] == store_id)
        self.assertEqual(item["harga_terbaru"], 8000)

    # -----------------------------------------------------------------------
    # 10. None prices appear at end of list
    # -----------------------------------------------------------------------
    def test_none_prices_at_end(self):
        db = TestingSessionLocal()
        product = _make_product(db)
        store_has_price = _make_store(db, "Punya Harga", lat=-6.201, lng=106.801)
        store_no_price = _make_store(db, "Tanpa Harga", lat=-6.202, lng=106.802)
        _make_price(db, product.id, store_has_price.id, harga=25000)
        product_id = str(product.id)
        db.close()

        resp = self.client.get(
            f"/price/compare/{product_id}",
            params={"lat": -6.2, "lng": 106.8, "radius_km": 5.0},
        )
        self.assertEqual(resp.status_code, 200)
        items = resp.json()["comparison"]
        has_price = [i for i in items if i["harga_terbaru"] is not None]
        no_price = [i for i in items if i["harga_terbaru"] is None]
        if has_price and no_price:
            priced_indices = [items.index(i) for i in has_price]
            no_price_indices = [items.index(i) for i in no_price]
            self.assertLess(max(priced_indices), min(no_price_indices))


if __name__ == "__main__":
    unittest.main()
