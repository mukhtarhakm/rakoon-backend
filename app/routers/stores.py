import math
import uuid
import re
import logging
from typing import List, Optional
from difflib import SequenceMatcher

import httpx
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
    source: str = Field(..., description="Sumber data ('osm' atau 'local_fallback')")
    stores: List[NearbyStoreItem] = Field(default_factory=list, description="Daftar toko terdekat")
    message: Optional[str] = Field(None, description="Pesan tambahan jika data terbatas")


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

def normalize_name(name: str) -> str:
    """
    Menormalisasi nama toko untuk perbandingan string.
    Mengubah ke lowercase dan membuang karakter non-alphanumeric.
    """
    if not name:
        return ""
    return re.sub(r'[^a-z0-9]', '', name.lower())

def name_similarity(n1: str, n2: str) -> float:
    """
    Menghitung kemiripan nama toko menggunakan SequenceMatcher.
    """
    return SequenceMatcher(None, normalize_name(n1), normalize_name(n2)).ratio()

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
    Mencari toko/supermarket terdekat dalam radius tertentu dari koordinat lat/lng.
    Query ke Overpass API (OSM) dan simpan toko baru secara otomatis ke database jika belum terdaftar.
    Jika Overpass API gagal/timeout, lakukan fallback ke database lokal.
    """
    # 1. Inisialisasi status sumber data dan elemen OSM
    source = "osm"
    elements = []
    
    # 2. Coba query ke Overpass API
    try:
        overpass_url = "https://overpass-api.de/api/interpreter"
        # Convert radius_km ke meter
        radius_meters = radius_km * 1000.0
        
        # Overpass QL query untuk mencari supermarket dan convenience store
        query = f"""
        [out:json][timeout:10];
        (
          node["shop"~"supermarket|convenience"](around:{radius_meters},{lat},{lng});
          way["shop"~"supermarket|convenience"](around:{radius_meters},{lat},{lng});
          relation["shop"~"supermarket|convenience"](around:{radius_meters},{lat},{lng});
        );
        out center;
        """
        
        headers = {
            "User-Agent": "RakoonNearbyComparison/1.0 (contact: developer@rakoon.app)"
        }
        async with httpx.AsyncClient() as client:
            response = await client.post(overpass_url, data={"data": query}, headers=headers, timeout=10.0)
            
        if response.status_code == 200:
            elements = response.json().get("elements", [])
        else:
            logger.error(f"Overpass API returned status code {response.status_code}: {response.text}")
            source = "local_fallback"
            
    except Exception as e:
        logger.error(f"Overpass API call failed/timed out: {str(e)}")
        source = "local_fallback"
        
    # 3. Proses data berdasarkan sumber (OSM vs local_fallback)
    results = []
    
    if source == "local_fallback":
        # Ambil toko kandidat dari database lokal dan filter dengan Haversine
        db_stores = get_candidate_stores(db, lat, lng, radius_km)
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
    else:
        # source == "osm": Proses hasil dari Overpass API
        # Ambil toko kandidat dari database lokal untuk deduplikasi
        candidates = get_candidate_stores(db, lat, lng, radius_km)
        
        for el in elements:
            # Ekstrak koordinat (node memiliki lat/lon secara langsung, way/relation memiliki center)
            if el.get("type") == "node":
                item_lat = el.get("lat")
                item_lng = el.get("lon")
            else:
                center = el.get("center", {})
                item_lat = center.get("lat")
                item_lng = center.get("lon")
                
            if item_lat is None or item_lng is None:
                continue
                
            # Hitung jarak dari posisi user
            dist = haversine_distance(lat, lng, item_lat, item_lng)
            if dist > radius_km:
                continue
                
            # Ekstrak data nama dan alamat dari tags
            tags = el.get("tags", {})
            osm_name = tags.get("name")
            if not osm_name:
                shop_type = tags.get("shop", "kelontong")
                osm_name = f"Toko {shop_type.capitalize()}"
                
            # Buat alamat gabungan dari OSM tags
            addr_parts = []
            for tag_key in ["addr:street", "addr:housenumber", "addr:city"]:
                val = tags.get(tag_key)
                if val:
                    addr_parts.append(val)
            osm_alamat = ", ".join(addr_parts) if addr_parts else None
            
            # Cek kecocokan di database lokal (jarak <= 40m dan similarity nama >= 0.7)
            # Threshold 40m dipilih agar hanya menggabungkan entri duplikat untuk toko fisik yang sama,
            # serta menghindari penggabungan salah antara cabang minimarket (seperti Indomaret/Alfamart) 
            # yang letaknya berdekatan di jalan yang sama.
            matched_store = None
            for candidate in candidates:
                d = haversine_distance(item_lat, item_lng, candidate.lat, candidate.lng)
                if d <= 0.04:  # 40 meter
                    sim = name_similarity(osm_name, candidate.nama)
                    if sim >= 0.7:
                        matched_store = candidate
                        break
                        
            if matched_store:
                store_id = matched_store.id
            else:
                # Store belum terdaftar, buat baru di database
                store_id = str(uuid.uuid4())
                new_store = Store(
                    id=store_id,
                    nama=osm_name,
                    alamat=osm_alamat,
                    lat=item_lat,
                    lng=item_lng
                )
                try:
                    db.add(new_store)
                    db.commit()
                    db.refresh(new_store)
                    # Tambahkan ke candidates agar tidak terduplikasi pada loop data berikutnya
                    candidates.append(new_store)
                except Exception as ex:
                    db.rollback()
                    logger.error(f"Failed to auto-create store from OSM: {str(ex)}")
                    # Skip jika gagal menyimpan ke database (agar user tidak terganggu error 500)
                    continue
                    
            results.append(
                NearbyStoreItem(
                    store_id=str(store_id),
                    nama=osm_name,
                    lat=item_lat,
                    lng=item_lng,
                    jarak_km=dist
                )
            )
            
    # 4. Urutkan hasil berdasarkan jarak terdekat
    results.sort(key=lambda x: x.jarak_km)
    
    # 5. Cek jika data toko kurang dari 2
    message = None
    if len(results) < 2:
        message = "Data toko di sekitar masih terbatas"
        
    return NearbyStoresResponse(
        source=source,
        stores=results,
        message=message
    )
