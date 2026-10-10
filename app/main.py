from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.routers import price, scan, stores, products, recommendation, budget_shopping, auth, ads, health

app = FastAPI(
    title="Rakoon Backend",
    description="Backend API untuk Rakoon - AI Computer Vision untuk Belanja Cerdas di Supermarket",
    version="1.0.0"
)

# Konfigurasi CORS agar frontend (Flutter/Web) dapat mengakses API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Mengizinkan semua origin untuk development
    allow_credentials=False,  # Set False jika menggunakan wildcard ("*")
    allow_methods=["*"],  # Mengizinkan semua HTTP methods (GET, POST, dll)
    allow_headers=["*"],  # Mengizinkan semua HTTP headers
)

from fastapi.staticfiles import StaticFiles
from pathlib import Path

# Setup static files directory (product images, etc.)
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
UPLOAD_DIR = STATIC_DIR / "uploads" / "products"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Include routers
app.include_router(price.router, prefix="/price", tags=["price"])
app.include_router(scan.router, prefix="/scan", tags=["scan"])
app.include_router(stores.router, prefix="/stores", tags=["stores"])
app.include_router(products.router, prefix="/products", tags=["products"])
app.include_router(recommendation.router, prefix="/recommendation", tags=["recommendation"])
app.include_router(budget_shopping.router, prefix="/budget-shopping", tags=["budget-shopping"])
app.include_router(auth.router, prefix="/auth", tags=["auth"])
app.include_router(ads.router, prefix="/ads", tags=["ads"])
app.include_router(health.router, tags=["health"])

@app.api_route("/", methods=["GET", "HEAD"])
def root():
    """Root endpoint returning application liveness info."""
    return {
        "status": "ok",
        "app": "Rakoon Backend",
        "version": "1.0.0",
    }
