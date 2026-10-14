import logging
from typing import Optional, List, Tuple, Dict
from fastapi import APIRouter, status, HTTPException, Depends, Query

from app.models.schemas import (
    RecommendationCandidate,
    RecommendationRequest,
    RankedProductItem,
    ExcludedProductItem,
    DimensionRecommendationGroup,
    CategoryRecommendationGroup,
    RecommendationResponse,
    RecommendedProductItem,
    validate_category,
)
from app.database import get_db
from app.services.errors import safe_error
from app.models.db_models import Product, Store
from app.services.current_price import CurrentPrice, get_current_prices, pick_cheapest
from sqlalchemy.orm import Session
from app.routers.stores import MAX_SEARCH_RADIUS_KM

logger = logging.getLogger("rakoon_backend.recommendation")

router = APIRouter()

# Dictionary alias untuk pemetaan satuan ke base_unit dan faktor konversi
UNIT_MAPPINGS: Dict[str, Tuple[str, str, float]] = {
    # (Dimension, Base Unit, Multiplier to Base Unit)
    # Volume
    "ml": ("volume", "ml", 1.0),
    "mili": ("volume", "ml", 1.0),
    "milliliter": ("volume", "ml", 1.0),
    "milli liter": ("volume", "ml", 1.0),
    "cc": ("volume", "ml", 1.0),
    "l": ("volume", "ml", 1000.0),
    "liter": ("volume", "ml", 1000.0),
    "litre": ("volume", "ml", 1000.0),
    
    # Weight / Massa
    "g": ("weight", "g", 1.0),
    "gr": ("weight", "g", 1.0),
    "gram": ("weight", "g", 1.0),
    "gramm": ("weight", "g", 1.0),
    "kg": ("weight", "g", 1000.0),
    "kilo": ("weight", "g", 1000.0),
    "kilogram": ("weight", "g", 1000.0),
    
    # Piece / Quantity
    "pcs": ("count", "pcs", 1.0),
    "piece": ("count", "pcs", 1.0),
    "pieces": ("count", "pcs", 1.0),
    "buah": ("count", "pcs", 1.0),
    "biji": ("count", "pcs", 1.0),
    "pack": ("count", "pcs", 1.0),
    "bungkus": ("count", "pcs", 1.0),
}

def normalize_unit_and_dimension(satuan: Optional[str], ukuran: float) -> Optional[Tuple[str, str, float]]:
    """
    Menormalisasi nama satuan dan menghitung ukuran dalam base unit.
    Returns: (dimension, base_unit, normalized_ukuran) atau None jika satuan tidak didukung.
    """
    if not satuan:
        return None
        
    clean_satuan = str(satuan).strip().lower()
    mapping = UNIT_MAPPINGS.get(clean_satuan)
    if not mapping:
        return None
        
    dimension, base_unit, multiplier = mapping
    normalized_ukuran = ukuran * multiplier
    return dimension, base_unit, normalized_ukuran

_CANONICAL_UNIT = {
    ("volume", 1.0): "ml", ("volume", 1000.0): "l",
    ("weight", 1.0): "g", ("weight", 1000.0): "kg",
    ("count", 1.0): "pcs",
}


def canonical_unit(satuan: Optional[str]) -> Optional[str]:
    """Satu ejaan per satuan yang dikenal (gr/gram -> g, liter -> l, ...); satuan lain dikembalikan apa adanya (trim)."""
    if satuan is None or not str(satuan).strip():
        return None
    clean = str(satuan).strip()
    mapping = UNIT_MAPPINGS.get(clean.lower())
    if not mapping:
        return clean
    dimension, _base, multiplier = mapping
    return _CANONICAL_UNIT[(dimension, multiplier)]


def format_unit_price(harga_per_unit: float, base_unit: str) -> str:
    """
    Format harga per unit menjadi teks Rupiah yang rapi (pemisah ribuan titik, desimal koma).
    Contoh: Rp60,00 / ml atau Rp1.234,50 / g
    """
    int_part = int(harga_per_unit)
    dec_part = int(round((harga_per_unit - int_part) * 100))
    if dec_part >= 100:
        int_part += 1
        dec_part = 0
    formatted_int = f"{int_part:,}".replace(",", ".")
    return f"Rp{formatted_int},{dec_part:02d} / {base_unit}"

def evaluate_best_value_logic(items: List[RecommendationCandidate]) -> RecommendationResponse:
    """
    Core deterministic business logic untuk menghitung dan menentukan Best Value Recommendation.
    Berbasis pengelompokan Kategori produk dan Dimensi Satuan yang kompatibel.
    Modular dan independen tanpa dependensi ke AI/LLM atau database.
    """
    if not items:
        return RecommendationResponse(
            total_evaluated=0,
            total_valid=0,
            total_excluded=0,
            categories=[],
            excluded_items=[]
        )

    total_evaluated = len(items)
    raw_valid_candidates = []
    excluded_items: List[ExcludedProductItem] = []

    # 1. Validation & Category Normalization Phase
    for idx, item in enumerate(items):
        item_id_str = str(item.product_id) if item.product_id is not None else f"item-{idx + 1}"
        nama = item.nama_produk.strip() if item.nama_produk else None

        if not nama:
            excluded_items.append(ExcludedProductItem(
                product_id=item_id_str,
                nama_produk=None,
                harga=item.harga,
                ukuran=item.ukuran,
                satuan=item.satuan,
                reason="Nama produk tidak boleh kosong."
            ))
            continue

        if item.harga is None or item.harga <= 0:
            excluded_items.append(ExcludedProductItem(
                product_id=item_id_str,
                nama_produk=nama,
                harga=item.harga,
                ukuran=item.ukuran,
                satuan=item.satuan,
                reason="Harga produk tidak valid (harus lebih besar dari 0)."
            ))
            continue

        if item.ukuran is None or item.ukuran <= 0:
            excluded_items.append(ExcludedProductItem(
                product_id=item_id_str,
                nama_produk=nama,
                harga=item.harga,
                ukuran=item.ukuran,
                satuan=item.satuan,
                reason="Ukuran produk tidak valid (harus lebih besar dari 0)."
            ))
            continue

        norm_result = normalize_unit_and_dimension(item.satuan, item.ukuran)
        if not norm_result:
            excluded_items.append(ExcludedProductItem(
                product_id=item_id_str,
                nama_produk=nama,
                harga=item.harga,
                ukuran=item.ukuran,
                satuan=item.satuan,
                reason=f"Satuan '{item.satuan}' tidak didukung untuk perhitungan."
            ))
            continue

        dimension, base_unit, normalized_ukuran = norm_result
        cat_enum = validate_category(item.kategori)
        cat_value = cat_enum.value

        raw_valid_candidates.append({
            "product_id": item_id_str,
            "nama_produk": nama,
            "harga": float(item.harga),
            "ukuran_original": float(item.ukuran),
            "satuan_original": str(item.satuan),
            "kategori": cat_value,
            "dimension": dimension,
            "base_unit": base_unit,
            "normalized_ukuran": float(normalized_ukuran),
            "harga_per_unit": float(item.harga) / float(normalized_ukuran)
        })

    if not raw_valid_candidates:
        return RecommendationResponse(
            total_evaluated=total_evaluated,
            total_valid=0,
            total_excluded=len(excluded_items),
            categories=[],
            excluded_items=excluded_items
        )

    # 2. Group by Category
    category_map: Dict[str, List[dict]] = {}
    for cand in raw_valid_candidates:
        cat = cand["kategori"]
        category_map.setdefault(cat, []).append(cand)

    categories_response: List[CategoryRecommendationGroup] = []
    total_valid_count = 0

    dimension_labels = {
        "volume": "Volume",
        "weight": "Berat",
        "count": "Jumlah",
    }

    # 3. For each Category, Group by Compatible Dimension
    for cat_name, cat_items in category_map.items():
        dim_map: Dict[str, List[dict]] = {}
        for item in cat_items:
            dim = item["dimension"]
            dim_map.setdefault(dim, []).append(item)

        dim_groups_response: List[DimensionRecommendationGroup] = []

        for dim_name, dim_items in dim_map.items():
            # Sort items in dimension group ascending by harga_per_unit
            dim_items.sort(key=lambda x: x["harga_per_unit"])

            base_unit = dim_items[0]["base_unit"]
            dim_label = f"{dimension_labels.get(dim_name, dim_name.capitalize())} ({base_unit})"

            is_comparable = len(dim_items) >= 2
            ranked_items: List[RankedProductItem] = []
            best_unit_price = dim_items[0]["harga_per_unit"]
            best_item_name = dim_items[0]["nama_produk"]

            for rank_idx, cand in enumerate(dim_items, start=1):
                total_valid_count += 1
                unit_label = format_unit_price(cand["harga_per_unit"], cand["base_unit"])

                if is_comparable:
                    is_best = (rank_idx == 1)
                    badge = "BEST VALUE" if is_best else None

                    if is_best:
                        runner_up_price = dim_items[1]["harga_per_unit"]
                        save_vs_runner_up = ((runner_up_price - best_unit_price) / runner_up_price) * 100.0
                        explanation = (
                            f"Pilihan Paling Hemat! Memiliki harga per {cand['base_unit']} terendah ({unit_label}), "
                            f"lebih hemat {save_vs_runner_up:.1f}% dibanding pilihan peringkat #2 ({dim_items[1]['nama_produk']})."
                        )
                    else:
                        diff_vs_best = ((cand["harga_per_unit"] - best_unit_price) / best_unit_price) * 100.0
                        explanation = (
                            f"Peringkat #{rank_idx} ({unit_label}). "
                            f"Lebih mahal {diff_vs_best:.1f}% dibanding {best_item_name} (Best Value)."
                        )
                else:
                    # Single item in dimension group
                    is_best = False
                    badge = None
                    explanation = "Belum ada produk pembanding yang compatible dalam kelompok dimensi ini."

                ranked_items.append(RankedProductItem(
                    product_id=cand["product_id"],
                    nama_produk=cand["nama_produk"],
                    harga=cand["harga"],
                    ukuran_original=cand["ukuran_original"],
                    satuan_original=cand["satuan_original"],
                    normalized_ukuran=cand["normalized_ukuran"],
                    base_unit=cand["base_unit"],
                    harga_per_unit=cand["harga_per_unit"],
                    unit_price_label=unit_label,
                    rank=rank_idx,
                    is_best_value=is_best,
                    badge=badge,
                    explanation=explanation
                ))

            best_value_item = ranked_items[0] if is_comparable else None
            msg = None if is_comparable else "Belum ada produk pembanding yang compatible."

            dim_groups_response.append(DimensionRecommendationGroup(
                dimension=dim_name,
                dimension_label=dim_label,
                base_unit=base_unit,
                is_comparable=is_comparable,
                message=msg,
                best_value=best_value_item,
                ranked_items=ranked_items
            ))

        categories_response.append(CategoryRecommendationGroup(
            kategori=cat_name,
            dimension_groups=dim_groups_response
        ))

    return RecommendationResponse(
        total_evaluated=total_evaluated,
        total_valid=total_valid_count,
        total_excluded=len(excluded_items),
        categories=categories_response,
        excluded_items=excluded_items
    )


@router.post("/evaluate", response_model=RecommendationResponse, status_code=status.HTTP_200_OK)
def evaluate_recommendation(payload: RecommendationRequest):
    """
    Evaluasi nilai ekonomi terbaik (Best Value Recommendation) dari sekumpulan kandidat produk.
    Perhitungan dilakukan secara deterministik berdasarkan harga per unit (harga / ukuran ter-normalisasi).
    """
    try:
        return evaluate_best_value_logic(payload.items)
    except Exception as e:
        raise safe_error(
            logger,
            e,
            "Terjadi kesalahan saat memproses rekomendasi.",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            log_prefix="Error during recommendation evaluation",
        )


RECOMMENDED_MAX_LIMIT = 50


@router.get("/recommended-products", response_model=List[RecommendedProductItem], status_code=status.HTTP_200_OK)
@router.get("/recommended", response_model=List[RecommendedProductItem], status_code=status.HTTP_200_OK)
def get_recommended_products(
    lat: Optional[float] = Query(None, ge=-90, le=90, description="Latitude pengguna"),
    lng: Optional[float] = Query(None, ge=-180, le=180, description="Longitude pengguna"),
    radius_km: float = Query(1.0, gt=0, le=MAX_SEARCH_RADIUS_KM, description="Radius pencarian dalam kilometer"),
    limit: int = Query(10, ge=1, le=RECOMMENDED_MAX_LIMIT, description="Jumlah maksimum produk"),
    db: Session = Depends(get_db),
):
    """
    Mengambil produk-produk rekomendasi terkompetitif/worth-it di toko-toko sekitar dari database.
    Perhitungan mengutamakan produk dengan selisih harga terendah & lokasi terdekat dari database riil.
    """
    results: List[RecommendedProductItem] = []

    try:
        from app.routers.stores import haversine_distance
        from datetime import datetime, timezone

        # 1. Map all stores and compute GPS distances if user coordinates (lat, lng) are provided
        store_map: Dict[str, Store] = {}
        store_distance_map: Dict[str, float] = {}

        all_stores = db.query(Store).all()
        for st in all_stores:
            s_id = str(st.id)
            store_map[s_id] = st
            if lat is not None and lng is not None and st.lat is not None and st.lng is not None:
                dist = haversine_distance(lat, lng, st.lat, st.lng)
                store_distance_map[s_id] = dist

        # 2. Produk + harga terkini per (produk, toko): terbaru, verified saja, stale > 90 hari (aturan bersama)
        products = db.query(Product).all()
        prices_by_product: Dict[str, List[CurrentPrice]] = {}
        for (pid, _sid), price in get_current_prices(db).items():
            prices_by_product.setdefault(pid, []).append(price)

        candidates = []
        for prod in products:
            prices = prices_by_product.get(str(prod.id), [])
            if not prices:
                continue

            if lat is not None and lng is not None and store_distance_map:
                inside_radius = [
                    p for p in prices
                    if p.store_id in store_distance_map and store_distance_map[p.store_id] <= radius_km
                ]
                if inside_radius:
                    best = min(inside_radius, key=lambda p: (p.stale, p.harga, store_distance_map[p.store_id], p.store_id))
                else:
                    # Tidak ada toko dalam radius: ambil toko terdekat yang punya harga
                    best = min(prices, key=lambda p: (store_distance_map.get(p.store_id, 999.0), p.stale, p.harga, p.store_id))
            else:
                best = pick_cheapest(prices)
            best_dist = store_distance_map.get(best.store_id)

            store = store_map.get(best.store_id)
            store_name = store.nama if store else "Toko Terdekat"

            # Round distance to 2 decimal places (e.g. 1.04 or 1.25 km) to preserve accuracy
            exact_dist_km = round(best_dist, 2) if best_dist is not None else None
            is_inside = 1 if (best_dist is not None and best_dist <= radius_km) else 0

            candidates.append({
                "item": RecommendedProductItem(
                    id=str(prod.id),
                    nama=prod.nama,
                    kategori=prod.kategori or "General",
                    harga=float(best.harga),
                    ukuran=prod.ukuran,
                    satuan=prod.satuan,
                    nama_toko=store_name,
                    jarak_km=exact_dist_km,
                    updated_at=best.timestamp.isoformat(),
                    stale=best.stale,
                    foto_url=getattr(prod, "foto_url", None),
                ),
                "inside": is_inside,
                "stale": best.stale,
                "harga": float(best.harga),
                "jarak": best_dist if best_dist is not None else 999.0,
                "id": str(prod.id),
            })

        # Dalam radius dulu, harga segar sebelum stale, lalu termurah dan terdekat (urutan deterministik)
        candidates.sort(key=lambda x: (-x["inside"], x["stale"], x["harga"], x["jarak"], x["id"]))
        results = [c["item"] for c in candidates]

    except Exception as e:
        logger.error(f"Failed to query DB for recommended products: {type(e).__name__}: {e}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Gagal memuat rekomendasi produk. Coba lagi nanti."
        )

    return results[:limit]

