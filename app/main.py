from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.routers import price, scan, stores, products

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

# Include routers
app.include_router(price.router, prefix="/price", tags=["price"])
app.include_router(scan.router, prefix="/scan", tags=["scan"])
app.include_router(stores.router, prefix="/stores", tags=["stores"])
app.include_router(products.router, prefix="/products", tags=["products"])

@app.get("/")
@app.get("/health")
def health_check():
    """
    Health check endpoint untuk memastikan server berjalan dengan baik.
    """
    return {
        "status": "ok",
        "app": "Rakoon Backend",
        "version": "1.0.0"
    }
