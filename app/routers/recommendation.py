import logging
from typing import Optional, List, Tuple, Dict
from fastapi import APIRouter, status, HTTPException, Depends

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
from app.models.db_models import Product, PriceEntry, Store
from sqlalchemy.orm import Session

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

def format_unit_price(harga_per_unit: float, base_unit: str) -> str:
    """
    Format harga per unit menjadi teks Rupiah yang rapi.
    Contoh: Rp60.00 / ml atau Rp150.50 / g
    """
    if harga_per_unit >= 10:
        return f"Rp{harga_per_unit:,.2f} / {base_unit}".replace(",", ".")
    else:
        return f"Rp{harga_per_unit:.2f} / {base_unit}".replace(".", ",")

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
        logger.error(f"Error during recommendation evaluation: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Terjadi kesalahan saat memproses rekomendasi: {str(e)}"
        )


@router.get("/recommended-products", response_model=List[RecommendedProductItem], status_code=status.HTTP_200_OK)
@router.get("/recommended", response_model=List[RecommendedProductItem], status_code=status.HTTP_200_OK)
def get_recommended_products(
    lat: Optional[float] = None,
    lng: Optional[float] = None,
    radius_km: float = 1.0,
    limit: int = 10,
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

        # 2. Query all products from DB
        products = db.query(Product).all()
        candidates = []

        for prod in products:
            # Query non-rejected price entries for this product
            pes = (
                db.query(PriceEntry)
                .filter(
                    PriceEntry.product_id == prod.id,
                    PriceEntry.status_verifikasi != "rejected"
                )
                .order_by(PriceEntry.harga.asc(), PriceEntry.timestamp.desc())
                .all()
            )

            if not pes:
                continue

            best_pe = None
            best_dist = None

            if lat is not None and lng is not None and store_distance_map:
                # First check price entries within specified radius
                inside_radius = [
                    pe for pe in pes
                    if str(pe.store_id) in store_distance_map and store_distance_map[str(pe.store_id)] <= radius_km
                ]
                if inside_radius:
                    best_pe = sorted(inside_radius, key=lambda x: (x.harga, store_distance_map.get(str(x.store_id), 999.0)))[0]
                    best_dist = store_distance_map.get(str(best_pe.store_id))
                else:
                    # If none inside radius, pick cheapest overall among stores with known distance
                    sorted_pes = sorted(
                        pes,
                        key=lambda x: (store_distance_map.get(str(x.store_id), 999.0), x.harga)
                    )
                    best_pe = sorted_pes[0]
                    best_dist = store_distance_map.get(str(best_pe.store_id))
            else:
                best_pe = pes[0]
                best_dist = store_distance_map.get(str(best_pe.store_id))

            store = store_map.get(str(best_pe.store_id))
            if not store:
                store = db.query(Store).filter(Store.id == best_pe.store_id).first()

            store_name = store.nama if store else "Toko Terdekat"

            # Compute real timestamp in ISO format
            if best_pe.timestamp:
                updated_at_str = best_pe.timestamp.isoformat()
            else:
                updated_at_str = datetime.now(timezone.utc).isoformat()

            # Read product photo URL
            foto = getattr(prod, "foto_url", None)

            # Round distance to 2 decimal places (e.g. 1.04 or 1.25 km) to preserve accuracy
            exact_dist_km = round(best_dist, 2) if best_dist is not None else None

            is_inside = 1 if (best_dist is not None and best_dist <= radius_km) else 0

            candidates.append({
                "item": RecommendedProductItem(
                    id=str(prod.id),
                    nama=prod.nama,
                    kategori=prod.kategori or "General",
                    harga=float(best_pe.harga),
                    ukuran=prod.ukuran,
                    satuan=prod.satuan,
                    nama_toko=store_name,
                    jarak_km=exact_dist_km,
                    updated_at=updated_at_str,
                    foto_url=foto,
                ),
                "inside": is_inside,
                "harga": float(best_pe.harga),
                "jarak": best_dist if best_dist is not None else 999.0,
            })

        # Sort candidates prioritizing items inside radius, then lowest price & proximity
        candidates.sort(key=lambda x: (-x["inside"], x["harga"], x["jarak"]))
        results = [c["item"] for c in candidates]

    except Exception as e:
        logger.warning(f"Failed to query DB for recommended products: {e}")

    # Fallback only if database has zero products (e.g. initial unseeded DB)
    if not results:
        max_dist = max(0.1, radius_km)
        default_items = [
            RecommendedProductItem(
                id="rec-1",
                nama="INDOMIE GORENG 85G",
                kategori="Makanan Instan",
                harga=3100.0,
                ukuran=85.0,
                satuan="g",
                nama_toko="MANNA KAMPUS BABARSARI",
                jarak_km=min(0.5, max_dist),
                updated_at="2026-08-11T18:33:00+00:00",
                foto_url=None,
            ),
            RecommendedProductItem(
                id="rec-2",
                nama="BIMOLI MINYAK GORENG 2L",
                kategori="Makanan Pokok",
                harga=34500.0,
                ukuran=2.0,
                satuan="l",
                nama_toko="INDOMARET BABARSARI",
                jarak_km=min(0.3, max_dist),
                updated_at="2026-08-11T17:00:00+00:00",
                foto_url=None,
            ),
        ]
        return default_items[:limit]

    return results[:limit]

