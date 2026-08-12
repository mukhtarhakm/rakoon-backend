import unittest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from pydantic import BaseModel, ValidationError

from app.main import app
from app.database import Base, get_db
from app.models.db_models import Product, Store, PriceEntry
from app.models.schemas import ProductCategory, ProductCategoryType, ConfirmItem, ScanResultItem

# In-memory SQLite for testing
SQLALCHEMY_DATABASE_URL = "sqlite:///:memory:"
engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


# Dummy Pydantic model to test validation
class CategoryTestModel(BaseModel):
    kategori: ProductCategoryType

class TestScanCategories(unittest.TestCase):

    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = TestingSessionLocal()
        self.original_get_db = app.dependency_overrides.get(get_db)
        app.dependency_overrides[get_db] = lambda: self.db
        from app.dependencies import get_current_user
        app.dependency_overrides[get_current_user] = lambda: "b5b07384-d113-4956-b51c-43f11075d655"
        self.client = TestClient(app)

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)
        from app.dependencies import get_current_user
        app.dependency_overrides.pop(get_current_user, None)
        if self.original_get_db is not None:
            app.dependency_overrides[get_db] = self.original_get_db
        else:
            app.dependency_overrides.pop(get_db, None)

    def test_category_enum_and_validation(self):
        # 1. Valid authoritative category name
        m1 = CategoryTestModel(kategori="Makanan Pokok")
        self.assertEqual(m1.kategori, ProductCategory.MAKANAN_POKOK)
        self.assertEqual(m1.kategori.value, "Makanan Pokok")

        # 2. Case-insensitive values
        m2 = CategoryTestModel(kategori="makanan instan")
        self.assertEqual(m2.kategori, ProductCategory.MAKANAN_INSTAN)
        
        m3 = CategoryTestModel(kategori="Susu & Olahan")
        self.assertEqual(m3.kategori, ProductCategory.SUSU_OLAHAN)

        # 3. Case-insensitive Enum member names
        m4 = CategoryTestModel(kategori="MAKANAN_POKOK")
        self.assertEqual(m4.kategori, ProductCategory.MAKANAN_POKOK)

        m5 = CategoryTestModel(kategori="susu_olahan")
        self.assertEqual(m5.kategori, ProductCategory.SUSU_OLAHAN)

        # 4. Unknown/Arbitrary category falls back to "Lainnya"
        m6 = CategoryTestModel(kategori="Instant Food")
        self.assertEqual(m6.kategori, ProductCategory.LAINNYA)

        m7 = CategoryTestModel(kategori="Arbitrary Category")
        self.assertEqual(m7.kategori, ProductCategory.LAINNYA)

        m8 = CategoryTestModel(kategori="")
        self.assertEqual(m8.kategori, ProductCategory.LAINNYA)

    def test_confirm_scan_saves_category(self):
        # 1. Seed store
        store_id = "a5b07384-d113-4956-b51c-43f11075d654"
        user_id = "b5b07384-d113-4956-b51c-43f11075d655"
        
        store = Store(id=store_id, nama="Indomaret Sudirman")
        
        # 2. Seed an existing product with "General" category
        existing_id = "c5b07384-d113-4956-b51c-43f11075d654"
        existing_product = Product(
            id=existing_id, 
            nama="Ultra Milk Cokelat 1L", 
            kategori="General",
            ukuran=1000.0,
            satuan="ml"
        )
        
        # 3. Seed another existing product with specific category ("Minuman")
        specific_id = "c5b07384-d113-4956-b51c-43f11075d655"
        specific_product = Product(
            id=specific_id,
            nama="Teh Botol Sosro 450ml",
            kategori="Minuman",
            ukuran=450.0,
            satuan="ml"
        )

        self.db.add_all([store, existing_product, specific_product])
        self.db.commit()

        # 4. Call /scan/confirm with:
        # - existing product with "General" category -> should update category to "Susu & Olahan"
        # - existing product with "Minuman" category -> should keep "Minuman" (not overwritten silently)
        # - new product with category "Makanan Instan" -> should create new product with "Makanan Instan"
        payload = {
            "store_id": store_id,
            "user_id": user_id,
            "items": [
                {
                    "nama_produk": "Ultra Milk Cokelat 1L",
                    "harga": 20000,
                    "ukuran": 1000.0,
                    "satuan": "ml",
                    "kategori": "Susu & Olahan"
                },
                {
                    "nama_produk": "Teh Botol Sosro 450ml",
                    "harga": 6500,
                    "ukuran": 450.0,
                    "satuan": "ml",
                    "kategori": "Camilan" # Attempt to change to Camilan, should not overwrite silently
                },
                {
                    "nama_produk": "Indomie Goreng 85g",
                    "harga": 3500,
                    "ukuran": 85.0,
                    "satuan": "g",
                    "kategori": "Makanan Instan"
                }
            ]
        }

        response = self.client.post("/scan/confirm", json=payload)
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertEqual(data["items_saved"], 3)
        self.assertEqual(data["products_created"], 1)

        # Re-fetch from DB
        db = TestingSessionLocal()
        
        # - Ultra Milk Cokelat 1L should now have category "Susu & Olahan"
        prod_updated = db.query(Product).filter(Product.id == existing_id).first()
        self.assertEqual(prod_updated.kategori, "Susu & Olahan")
        
        # - Teh Botol Sosro 450ml should still have category "Minuman" (no silent overwrite)
        prod_kept = db.query(Product).filter(Product.id == specific_id).first()
        self.assertEqual(prod_kept.kategori, "Minuman")

        # - Indomie Goreng 85g should have category "Makanan Instan"
        prod_new = db.query(Product).filter(Product.nama == "Indomie Goreng 85g").first()
        self.assertIsNotNone(prod_new)
        self.assertEqual(prod_new.kategori, "Makanan Instan")
        
        db.close()

if __name__ == "__main__":
    unittest.main()
