import math
import uuid
import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.db_models import Store
from pydantic import BaseModel, Field

# Setup logger
logger = logging.getLogger("rakoon_backend.stores")

router = APIRouter()

# Schema definitions
class NearbyStoreItem(BaseModel):
    store_id: str = Field(..., description="ID dari database kita")
    nama: str = Field(..., description="Nama toko")
    lat: float = Field(..., description="Latitude toko")
    lng: float = Field(..., description="Longitude toko")
    jarak_km: float = Field(..., description="Jarak dari titik user dalam km")

class NearbyStoresResponse(BaseModel):
    stores: List[NearbyStoreItem] = Field(default_factory=list, description="Daftar toko terdekat")
    message: Optional[str] = Field(None, description="Pesan tambahan jika data terbatas")

class StoreCreate(BaseModel):
    nama: str = Field(..., description="Nama toko")
    lat: float = Field(..., description="Latitude toko")
    lng: float = Field(..., description="Longitude toko")
    alamat: Optional[str] = Field(None, description="Alamat toko (opsional)")

class StoreCreateResponse(BaseModel):
    store_id: str = Field(..., description="ID toko yang baru dibuat")


# Helpers
def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Menghitung jarak antara dua titik koordinat (latitude, longitude)
    dalam kilometer menggunakan formula Haversine.
    """
    R = 6371.0  # Radius bumi dalam km
    
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    
    return R * c

def get_candidate_stores(db: Session, lat: float, lng: float, radius_km: float) -> List[Store]:
    """
    Mendapatkan daftar toko kandidat dari database lokal dalam bounding box.
    Menambahkan buffer 1 km untuk memastikan batas terluar tercakup.
    """
    buffer_km = radius_km + 1.0
    lat_delta = buffer_km / 111.0
    
    cos_lat = math.cos(math.radians(lat))
    # Hindari division by zero jika sangat dekat dengan kutub
    lng_delta = buffer_km / (111.0 * abs(cos_lat)) if abs(cos_lat) > 0.001 else buffer_km / 111.0
    
    return db.query(Store).filter(
        Store.lat.between(lat - lat_delta, lat + lat_delta),
        Store.lng.between(lng - lng_delta, lng + lng_delta)
    ).all()


@router.get("/nearby", response_model=NearbyStoresResponse, status_code=status.HTTP_200_OK)
async def get_nearby_stores(
    lat: float = Query(..., description="Latitude koordinat pengguna"),
    lng: float = Query(..., description="Longitude koordinat pengguna"),
    radius_km: float = Query(5.0, description="Radius pencarian dalam kilometer"),
    db: Session = Depends(get_db)
):
    """
    Mencari toko/supermarket terdekat dalam radius tertentu dari koordinat lat/lng
    dengan melakukan query langsung ke database lokal.
    """
    # Ambil toko kandidat dari database lokal
    db_stores = get_candidate_stores(db, lat, lng, radius_km)
    
    results = []
    for store in db_stores:
        dist = haversine_distance(lat, lng, store.lat, store.lng)
        if dist <= radius_km:
            results.append(
                NearbyStoreItem(
                    store_id=str(store.id),
                    nama=store.nama,
                    lat=store.lat,
                    lng=store.lng,
                    jarak_km=dist
                )
            )
            
    # Urutkan hasil berdasarkan jarak terdekat
    results.sort(key=lambda x: x.jarak_km)
    
    # Cek jika data toko kurang dari 2
    message = None
    if len(results) < 2:
        message = "Data toko di sekitar masih terbatas"
        
    return NearbyStoresResponse(
        stores=results,
        message=message
    )

@router.post("/", response_model=StoreCreateResponse, status_code=status.HTTP_201_CREATED)
def create_store(
    store_data: StoreCreate,
    db: Session = Depends(get_db)
):
    """
    Menambahkan toko baru secara manual ke database.
    Validasi nama toko tidak boleh kosong, dan lat/lng harus dalam rentang koordinat bumi yang valid.
    """
    # Validasi nama tidak boleh kosong
    if not store_data.nama or not store_data.nama.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Nama toko tidak boleh kosong."
        )
        
    # Validasi lat (-90 to 90)
    if not (-90.0 <= store_data.lat <= 90.0):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Latitude harus berada dalam rentang -90 hingga 90."
        )
        
    # Validasi lng (-180 to 180)
    if not (-180.0 <= store_data.lng <= 180.0):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Longitude harus berada dalam rentang -180 hingga 180."
        )
        
    # Buat store_id baru
    store_id = str(uuid.uuid4())
    
    # Buat instance model database Store
    new_store = Store(
        id=store_id,
        nama=store_data.nama.strip(),
        alamat=store_data.alamat.strip() if store_data.alamat else None,
        lat=store_data.lat,
        lng=store_data.lng
    )
    
    try:
        db.add(new_store)
        db.commit()
        db.refresh(new_store)
        return StoreCreateResponse(store_id=new_store.id)
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to create store manually: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Gagal menyimpan data toko ke database: {str(e)}"
        )

