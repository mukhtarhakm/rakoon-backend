import os
import sys
import unittest
import uuid
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import app
from app.database import Base, get_db
from app.models.db_models import PriceEntry, Product, Store
from app.routers.stores import haversine_distance

engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

USER = (-7.7829, 110.4083)
NEAR = (-7.7700, 110.4000)  # toko murah
FAR = (-7.9000, 110.5000)   # toko mahal
ENDPOINTS = {
    "catalog": "/products/catalog",
    "recommended": "/recommendation/recommended-products",
}


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


class TestProductDistance(unittest.TestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = TestingSessionLocal()
        app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(app)
        self.user = str(uuid.uuid4())
        self.now = datetime.now(timezone.utc)

    def tearDown(self):
        app.dependency_overrides.clear()
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def _store(self, nama, coords):
        s = Store(nama=nama, lat=coords[0] if coords else None, lng=coords[1] if coords else None)
        self.db.add(s)
        self.db.commit()
        return s

    def _product(self, nama):
        p = Product(nama=nama, kategori="Minuman", ukuran=1.0, satuan="l")
        self.db.add(p)
        self.db.commit()
        return p

    def _price(self, product, store, harga, days_ago=1, status="verified"):
        self.db.add(PriceEntry(
            product_id=product.id, store_id=store.id, harga=harga, sumber_user_id=self.user,
            timestamp=self.now - timedelta(days=days_ago), status_verifikasi=status,
        ))
        self.db.commit()

    def _get(self, name, query=""):
        return self.client.get(f"{ENDPOINTS[name]}?{query}" if query else ENDPOINTS[name])

    def _loc(self):
        return f"lat={USER[0]}&lng={USER[1]}"

    def _seed_two_stores(self):
        near, far = self._store("Dekat", NEAR), self._store("Jauh", FAR)
        prod = self._product("Susu")
        self._price(prod, near, 10000)
        self._price(prod, far, 20000)
        return prod, near, far

    def test_without_coordinates_jarak_km_is_null_and_old_fields_unchanged(self):
        self._seed_two_stores()
        for name in ENDPOINTS:
            with self.subTest(endpoint=name):
                res = self._get(name)
                self.assertEqual(res.status_code, 200)
                item = res.json()[0]
                self.assertIsNone(item["jarak_km"])
                self.assertEqual(item["harga_terendah" if name == "catalog" else "harga"], 10000)
                self.assertEqual(item["nama_toko_terendah" if name == "catalog" else "nama_toko"], "Dekat")

    def test_distance_is_to_the_store_with_the_lowest_current_price(self):
        self._seed_two_stores()
        expected = round(haversine_distance(USER[0], USER[1], NEAR[0], NEAR[1]), 2)
        self.assertGreater(expected, 0)
        for name in ENDPOINTS:
            with self.subTest(endpoint=name):
                item = self._get(name, self._loc() + "&radius_km=50").json()[0]
                self.assertEqual(item["jarak_km"], expected)

    def test_cheapest_source_follows_current_price_rules(self):
        near, far = self._store("Dekat", NEAR), self._store("Jauh", FAR)
        prod = self._product("Teh")
        self._price(prod, near, 5000, status="pending")        # pending: bukan sumber
        self._price(prod, near, 4000, days_ago=200)             # stale: kalah dari harga segar
        self._price(prod, far, 20000)
        expected = round(haversine_distance(USER[0], USER[1], FAR[0], FAR[1]), 2)
        item = self._get("catalog", self._loc()).json()[0]
        self.assertEqual(item["nama_toko_terendah"], "Jauh")
        self.assertEqual(item["jarak_km"], expected)

    def test_store_without_coordinates_gives_null(self):
        prod = self._product("Kopi")
        self._price(prod, self._store("Tanpa Koordinat", None), 9000)
        for name in ENDPOINTS:
            with self.subTest(endpoint=name):
                self.assertIsNone(self._get(name, self._loc()).json()[0]["jarak_km"])

    def test_product_without_price_gives_null_in_catalog(self):
        self._product("Kosong")
        item = self._get("catalog", self._loc()).json()[0]
        self.assertIsNone(item["harga_terendah"])
        self.assertIsNone(item["jarak_km"])

    def test_recommended_distance_matches_radius_filter_distance(self):
        _, near, _ = self._seed_two_stores()
        dist = haversine_distance(USER[0], USER[1], NEAR[0], NEAR[1])
        inside = self._get("recommended", self._loc() + f"&radius_km={dist + 0.5}").json()[0]
        self.assertEqual(inside["jarak_km"], round(dist, 2))
        self.assertLessEqual(inside["jarak_km"], dist + 0.5)

    def test_only_one_coordinate_is_422(self):
        for name in ENDPOINTS:
            for query in (f"lat={USER[0]}", f"lng={USER[1]}"):
                with self.subTest(endpoint=name, query=query):
                    self.assertEqual(self._get(name, query).status_code, 422)

    def test_out_of_range_and_non_finite_coordinates_are_422(self):
        for name in ENDPOINTS:
            for query in ("lat=91&lng=110", "lat=-91&lng=110", "lat=0&lng=181", "lat=0&lng=-181",
                          "lat=nan&lng=110", "lat=0&lng=inf", "lat=-inf&lng=0"):
                with self.subTest(endpoint=name, query=query):
                    res = self._get(name, query)
                    self.assertEqual(res.status_code, 422)
                    self.assertNotIn("input", res.text)

    def test_query_count_does_not_grow_with_items(self):
        def count_queries(n_products):
            Base.metadata.drop_all(bind=engine)
            Base.metadata.create_all(bind=engine)
            near, far = self._store("Dekat", NEAR), self._store("Jauh", FAR)
            for i in range(n_products):
                p = self._product(f"Produk {i}")
                self._price(p, near, 10000 + i)
                self._price(p, far, 20000 + i)
            statements = []

            def on_exec(conn, cursor, statement, *a):
                statements.append(statement)

            event.listen(engine, "before_cursor_execute", on_exec)
            try:
                self.assertEqual(self._get("catalog", self._loc()).status_code, 200)
            finally:
                event.remove(engine, "before_cursor_execute", on_exec)
            return len(statements)

        self.assertEqual(count_queries(2), count_queries(12))


if __name__ == "__main__":
    unittest.main()
