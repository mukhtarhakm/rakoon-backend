import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import app
from app.database import Base, get_db
from app.dependencies import get_current_user
from app.models.db_models import (
    AdCampaign, PaymentTransaction, PriceEntry, Product, ScanSession, Store, StoreOwner,
)
from app.models.schemas import MAX_PAGE_SIZE, VerificationStatus

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
V = VerificationStatus.VERIFIED


class SqlCounter:
    def __init__(self):
        self.statements = []

    def __enter__(self):
        event.listen(engine, "before_cursor_execute", self._on)
        return self

    def __exit__(self, *exc):
        event.remove(engine, "before_cursor_execute", self._on)

    def _on(self, conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            self.statements.append(statement)

    def count_on(self, table):
        return sum(1 for s in self.statements if f"FROM {table}" in s or f"JOIN {table}" in s)


class PaginationBase(unittest.TestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = TestingSessionLocal()
        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: USER
        self.client = TestClient(app)
        self.now = datetime.now(timezone.utc)

    def tearDown(self):
        app.dependency_overrides.clear()
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def get_ok(self, url):
        res = self.client.get(url)
        self.assertEqual(res.status_code, 200, f"{url}: {res.text}")
        return res.json()


class TestProductPagination(PaginationBase):
    def setUp(self):
        super().setUp()
        self.store = Store(nama="Toko Halaman", lat=-7.78, lng=110.4)
        self.db.add(self.store)
        self.db.commit()
        for i in range(120):
            product = Product(nama=f"Produk {i:03d}", kategori="Makanan Pokok" if i % 2 else "Minuman", ukuran=1.0, satuan="l")
            self.db.add(product)
            self.db.flush()
            self.db.add(PriceEntry(
                product_id=product.id, store_id=self.store.id, harga=1000 + i, sumber_user_id=USER,
                status_verifikasi=V, timestamp=self.now,
            ))
        self.db.commit()

    def _page(self, path, limit, offset):
        return self.get_ok(f"{path}?limit={limit}&offset={offset}")

    def test_limit_and_offset_are_validated_on_product_lists(self):
        for path in ("/products/", "/products/catalog"):
            for query in (f"limit={MAX_PAGE_SIZE + 1}", "limit=0", "limit=-1", "offset=-1", "limit=abc"):
                with self.subTest(path=path, query=query):
                    self.assertEqual(self.client.get(f"{path}?{query}").status_code, 422)
            self.assertEqual(self.client.get(f"{path}?limit={MAX_PAGE_SIZE}&offset=0").status_code, 200)

    def test_pages_are_contiguous_non_overlapping_and_sorted(self):
        for path in ("/products/", "/products/catalog"):
            with self.subTest(path=path):
                names = []
                for offset in (0, 50, 100):
                    names += [p["nama"] for p in self._page(path, 50, offset)]
                self.assertEqual(len(names), 120)
                self.assertEqual(names, sorted(names))
                self.assertEqual(len(set(names)), 120)

    def test_default_page_sizes_keep_the_current_frontend_behavior(self):
        self.assertEqual(len(self.get_ok("/products/")), 50)
        self.assertEqual(len(self.get_ok("/products/catalog")), MAX_PAGE_SIZE)
        self.assertEqual(len(self.get_ok("/products/catalog?offset=100")), 20)

    def test_catalog_page_carries_the_right_prices(self):
        page = self._page("/products/catalog", 10, 20)
        self.assertEqual([p["nama"] for p in page], [f"Produk {i:03d}" for i in range(20, 30)])
        self.assertEqual([p["harga_terendah"] for p in page], [1000 + i for i in range(20, 30)])

    def test_search_percent_and_underscore_are_literal(self):
        self.db.add(Product(nama="Diskon 100% Asli", kategori="Camilan"))
        self.db.add(Product(nama="Teh_Pucuk", kategori="Minuman"))
        self.db.commit()
        for path in ("/products/", "/products/catalog"):
            with self.subTest(path=path):
                self.assertEqual([p["nama"] for p in self.get_ok(f"{path}?search=%25")], ["Diskon 100% Asli"])
                self.assertEqual([p["nama"] for p in self.get_ok(f"{path}?search=h_P")], ["Teh_Pucuk"])
                self.assertEqual(self.get_ok(f"{path}?search=Produk%20_"), [])

    def test_category_filter_is_an_exact_case_insensitive_match(self):
        for path in ("/products/", "/products/catalog"):
            with self.subTest(path=path):
                self.assertEqual(self.get_ok(f"{path}?category=Makanan&limit=100"), [])
                exact = self.get_ok(f"{path}?category=makanan%20pokok&limit=100")
                self.assertEqual(len(exact), 60)
                self.assertEqual({p["kategori"] for p in exact}, {"Makanan Pokok"})
                self.assertEqual(len(self.get_ok(f"{path}?category=Semua&limit=100")), 100)


class TestStoreProductsPagination(PaginationBase):
    def setUp(self):
        super().setUp()
        self.store = Store(nama="Toko Besar", lat=-7.78, lng=110.4)
        self.db.add(self.store)
        self.db.commit()
        for i in range(120):
            product = Product(nama=f"Barang {i:03d}", kategori="Minuman", ukuran=1.0, satuan="l")
            self.db.add(product)
            self.db.flush()
            self.db.add(PriceEntry(
                product_id=product.id, store_id=self.store.id, harga=2000 + i, sumber_user_id=USER,
                status_verifikasi=V, timestamp=self.now - timedelta(days=100 if i == 0 else 1),
            ))
        self.db.commit()

    def test_limit_offset_bounds(self):
        url = f"/stores/{self.store.id}/products"
        for query in (f"limit={MAX_PAGE_SIZE + 1}", "limit=0", "offset=-1"):
            with self.subTest(query=query):
                self.assertEqual(self.client.get(f"{url}?{query}").status_code, 422)

    def test_default_page_is_the_maximum_and_pages_are_contiguous(self):
        url = f"/stores/{self.store.id}/products"
        first = self.get_ok(url)
        self.assertEqual(len(first), MAX_PAGE_SIZE)
        rest = self.get_ok(f"{url}?offset={MAX_PAGE_SIZE}")
        names = [p["nama"] for p in first + rest]
        self.assertEqual(names, sorted(names))
        self.assertEqual(len(set(names)), 120)

    def test_uses_the_shared_current_price_rule_and_reports_stale(self):
        url = f"/stores/{self.store.id}/products"
        items = {p["nama"]: p for p in self.get_ok(url)}
        self.assertEqual(items["Barang 001"]["harga"], 2001)
        self.assertFalse(items["Barang 001"]["stale"])
        self.assertTrue(items["Barang 000"]["stale"])  # 100 hari lalu

    def test_pending_only_products_are_not_listed_for_the_store(self):
        product = Product(nama="Zzz Pending", kategori="Minuman")
        self.db.add(product)
        self.db.flush()
        self.db.add(PriceEntry(
            product_id=product.id, store_id=self.store.id, harga=1, sumber_user_id=USER,
            status_verifikasi=VerificationStatus.PENDING, timestamp=self.now,
        ))
        self.db.commit()
        names = [p["nama"] for p in self.get_ok(f"/stores/{self.store.id}/products?offset=100&limit=100")]
        self.assertNotIn("Zzz Pending", names)


class TestPriceHistoryPagination(PaginationBase):
    def setUp(self):
        super().setUp()
        self.store = Store(nama="Toko Riwayat", lat=-7.78, lng=110.4)
        self.product = Product(nama="Produk Riwayat", kategori="Minuman", ukuran=1.0, satuan="l")
        self.db.add_all([self.store, self.product])
        self.db.commit()
        for days_ago in range(150):
            self.db.add(PriceEntry(
                product_id=self.product.id, store_id=self.store.id, harga=10000 + days_ago, sumber_user_id=USER,
                status_verifikasi=V, timestamp=self.now - timedelta(days=days_ago, hours=1),
            ))
        self.db.commit()
        self.base = f"/price/products/{self.product.id}/price-history"

    def test_limit_offset_bounds(self):
        for query in (f"limit={MAX_PAGE_SIZE + 1}", "limit=0", "offset=-1"):
            with self.subTest(query=query):
                self.assertEqual(self.client.get(f"{self.base}?{query}").status_code, 422)

    def test_first_page_is_the_newest_entries_in_ascending_order_with_full_total_and_trend(self):
        body = self.get_ok(self.base)
        self.assertEqual(body["total"], 150)
        self.assertEqual(len(body["items"]), MAX_PAGE_SIZE)
        stamps = [i["timestamp"] for i in body["items"]]
        self.assertEqual(stamps, sorted(stamps))
        prices = [i["harga"] for i in body["items"]]
        self.assertEqual(min(prices), 10000)         # entri terbaru (0 hari lalu) ada di halaman pertama
        self.assertEqual(max(prices), 10000 + 99)    # 100 entri terbaru
        self.assertEqual(len(body["trend"]), 150)    # tren dihitung dari seluruh data, bukan satu halaman

    def test_second_page_holds_the_older_entries_without_overlap(self):
        first = {i["id"] for i in self.get_ok(self.base)["items"]}
        second_body = self.get_ok(f"{self.base}?offset=100")
        second = {i["id"] for i in second_body["items"]}
        self.assertEqual(len(second), 50)
        self.assertFalse(first & second)
        self.assertEqual(second_body["total"], 150)
        self.assertEqual(min(i["harga"] for i in second_body["items"]), 10000 + 100)

    def test_legacy_list_endpoint_is_also_bounded_and_paginated(self):
        url = f"/price/product/{self.product.id}"
        self.assertEqual(self.client.get(f"{url}?limit={MAX_PAGE_SIZE + 1}").status_code, 422)
        self.assertEqual(len(self.get_ok(f"{url}?limit=40&offset=10")), 40)
        self.assertEqual(len(self.get_ok(f"{url}?offset=140")), 10)


class TestRecentScansPagination(PaginationBase):
    def setUp(self):
        super().setUp()
        self.store = Store(nama="Toko Scan", lat=-7.78, lng=110.4)
        self.db.add(self.store)
        self.db.commit()
        for minutes_ago in range(12):
            self.db.add(ScanSession(user_id=USER, store_id=self.store.id, created_at=self.now - timedelta(minutes=minutes_ago)))
        self.db.commit()

    def test_limit_bounds_and_offset(self):
        for query in (f"limit={MAX_PAGE_SIZE + 1}", "limit=0", "limit=-3", "offset=-1"):
            with self.subTest(query=query):
                self.assertEqual(self.client.get(f"/scan/recent?{query}").status_code, 422)

    def test_pages_do_not_overlap_and_are_newest_first(self):
        first = self.get_ok("/scan/recent?limit=5&offset=0")
        second = self.get_ok("/scan/recent?limit=5&offset=5")
        third = self.get_ok("/scan/recent?limit=5&offset=10")
        self.assertEqual([len(first), len(second), len(third)], [5, 5, 2])
        ids = [s["id"] for s in first + second + third]
        self.assertEqual(len(set(ids)), 12)
        stamps = [s["timestamp"] for s in first + second + third]
        self.assertEqual(stamps, sorted(stamps, reverse=True))
        self.assertEqual(len(self.get_ok("/scan/recent")), 10)  # default FE tetap 10


class TestNoNPlusOne(PaginationBase):
    def _owner_with_campaigns(self, count, active_paid):
        """Tambah `count` kampanye ke satu toko milik USER (toko dan pemilik dibuat sekali)."""
        store = self.db.query(Store).filter(Store.nama == "Toko Iklan").first()
        if store is None:
            store = Store(nama="Toko Iklan", lat=-7.7829, lng=110.4083)
            self.db.add(store)
            self.db.commit()
            self.db.add(StoreOwner(user_id=USER, store_id=store.id, status="verified", verified_at=self.now))
        existing = self.db.query(AdCampaign).count()
        for i in range(existing, existing + count):
            campaign = AdCampaign(
                store_id=store.id, owner_user_id=USER, title=f"Promo {i}", banner_url="https://example.com/b.jpg",
                duration_days=7, price_paid=30000,  # judul unik per kampanye lewat indeks i
                status="active" if active_paid else "pending_payment",
                payment_status="paid" if active_paid else "unpaid",
                start_at=self.now, expires_at=self.now + timedelta(days=7),
            )
            self.db.add(campaign)
            self.db.flush()
            self.db.add(PaymentTransaction(
                campaign_id=campaign.id, owner_user_id=USER, provider="xendit", external_id=f"INV-{i}-{active_paid}",
                amount=30000, currency="IDR", status="PAID" if active_paid else "PENDING",
                invoice_url="https://x", xendit_invoice_id=f"xinv-{i}",
            ))
        self.db.commit()

    def test_home_banners_loads_transactions_in_one_query_regardless_of_campaign_count(self):
        self._owner_with_campaigns(2, active_paid=True)
        with SqlCounter() as few:
            few_res = self.get_ok("/ads/home-banners?lat=-7.7829&lng=110.4083&radius_km=15")
        self._owner_with_campaigns(8, active_paid=True)
        with SqlCounter() as many:
            many_res = self.get_ok("/ads/home-banners?lat=-7.7829&lng=110.4083&radius_km=15")

        self.assertEqual(len(few_res), 2)
        self.assertEqual(len(many_res), 10)
        self.assertLessEqual(many.count_on("payment_transactions"), 1)
        self.assertEqual(len(many.statements), len(few.statements))

    def test_my_store_query_count_does_not_grow_with_the_number_of_campaigns(self):
        self._owner_with_campaigns(2, active_paid=False)
        with SqlCounter() as few:
            self.get_ok("/ads/my-store")
        self._owner_with_campaigns(8, active_paid=False)
        with SqlCounter() as many:
            body = self.get_ok("/ads/my-store")
        self.assertGreaterEqual(len(body["campaigns"]), 8)
        self.assertEqual(len(many.statements), len(few.statements))
        self.assertEqual(many.count_on("payment_transactions"), 0)


if __name__ == "__main__":
    unittest.main()
