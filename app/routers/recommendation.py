import logging
from typing import Optional, List, Tuple, Dict
from fastapi import APIRouter, status, HTTPException

from app.models.schemas import (
    RecommendationCandidate,
    RecommendationRequest,
    RankedProductItem,
    ExcludedProductItem,
    DimensionRecommendationGroup,
    CategoryRecommendationGroup,
    RecommendationResponse,
    validate_category,
)

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
