import logging
from typing import Optional, List, Tuple, Dict
from fastapi import APIRouter, status, HTTPException

from app.models.schemas import (
    RecommendationCandidate,
    RecommendationRequest,
    RankedProductItem,
    ExcludedProductItem,
    RecommendationResponse,
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
    Modular dan independen tanpa dependensi ke AI/LLM atau database.
    """
    if not items:
        return RecommendationResponse(
            total_evaluated=0,
            total_valid=0,
            total_excluded=0,
            best_value=None,
            ranked_items=[],
            excluded_items=[]
        )

    total_evaluated = len(items)
    raw_valid_candidates = []
    excluded_items: List[ExcludedProductItem] = []

    # 1. Validation Phase
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
        raw_valid_candidates.append({
            "product_id": item_id_str,
            "nama_produk": nama,
            "harga": float(item.harga),
            "ukuran_original": float(item.ukuran),
            "satuan_original": str(item.satuan),
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
            best_value=None,
            ranked_items=[],
            excluded_items=excluded_items
        )

    # 2. Comparability Dimension Check
    # Cari dimensi terbanyak (misal mayoritas volume 'ml')
    dimension_counts: Dict[str, int] = {}
    for cand in raw_valid_candidates:
        dim = cand["dimension"]
        dimension_counts[dim] = dimension_counts.get(dim, 0) + 1

    dominant_dimension = max(dimension_counts.items(), key=lambda x: x[1])[0]

    filtered_valid_candidates = []
    for cand in raw_valid_candidates:
        if cand["dimension"] == dominant_dimension:
            filtered_valid_candidates.append(cand)
        else:
            excluded_items.append(ExcludedProductItem(
                product_id=cand["product_id"],
                nama_produk=cand["nama_produk"],
                harga=cand["harga"],
                ukuran=cand["ukuran_original"],
                satuan=cand["satuan_original"],
                reason=f"Dimensi satuan '{cand['satuan_original']}' tidak dapat dibandingkan dengan produk lain berdimensi '{dominant_dimension}'."
            ))

    if not filtered_valid_candidates:
        return RecommendationResponse(
            total_evaluated=total_evaluated,
            total_valid=0,
            total_excluded=len(excluded_items),
            best_value=None,
            ranked_items=[],
            excluded_items=excluded_items
        )

    # 3. Ranking Phase (Ascending order of harga_per_unit)
    filtered_valid_candidates.sort(key=lambda x: x["harga_per_unit"])

    best_item = filtered_valid_candidates[0]
    best_unit_price = best_item["harga_per_unit"]

    # Hitung rata-rata harga per unit untuk pembanding persentase kehematan
    avg_unit_price = sum(c["harga_per_unit"] for c in filtered_valid_candidates) / len(filtered_valid_candidates)

    ranked_items: List[RankedProductItem] = []
    for rank_idx, cand in enumerate(filtered_valid_candidates, start=1):
        is_best = (rank_idx == 1)
        badge = "BEST VALUE" if is_best else None
        unit_label = format_unit_price(cand["harga_per_unit"], cand["base_unit"])

        # Generasi penjelasan transparan & explainable
        if is_best:
            if len(filtered_valid_candidates) > 1:
                runner_up_price = filtered_valid_candidates[1]["harga_per_unit"]
                save_vs_runner_up = ((runner_up_price - best_unit_price) / runner_up_price) * 100.0
                explanation = (
                    f"Pilihan Paling Hemat! Memiliki harga per {cand['base_unit']} terendah ({unit_label}), "
                    f"lebih hemat {save_vs_runner_up:.1f}% dibanding pilihan peringkat #2 ({filtered_valid_candidates[1]['nama_produk']})."
                )
            else:
                explanation = (
                    f"Pilihan Utama! Memiliki harga per {cand['base_unit']} sebesar {unit_label}."
                )
        else:
            diff_vs_best = ((cand["harga_per_unit"] - best_unit_price) / best_unit_price) * 100.0
            explanation = (
                f"Peringkat #{rank_idx} ({unit_label}). "
                f"Lebih mahal {diff_vs_best:.1f}% dibanding {best_item['nama_produk']} (Best Value)."
            )

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

    return RecommendationResponse(
        total_evaluated=total_evaluated,
        total_valid=len(ranked_items),
        total_excluded=len(excluded_items),
        best_value=ranked_items[0] if ranked_items else None,
        ranked_items=ranked_items,
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
