from fastapi import FastAPI
from app.routers import price

app = FastAPI(
    title="Rakoon Backend",
    description="Backend API untuk Rakoon - AI Computer Vision untuk Belanja Cerdas di Supermarket",
    version="1.0.0"
)

# Include routers
app.include_router(price.router, prefix="/price", tags=["price"])

@app.get("/")
def health_check():
    """
    Health check endpoint untuk memastikan server berjalan dengan baik.
    """
    return {
        "status": "ok",
        "app": "Rakoon Backend",
        "version": "1.0.0"
    }
