import logging
import uuid
from typing import List, Dict, Any, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.database import get_db
from app.models.db_models import Product, PriceEntry, Store
from app.models.schemas import (
    BudgetRecommendRequest,
    BudgetRecommendResponse,
    BudgetItemResult,
    StoreInfoOutput,
    VerificationStatus,
    ProductAvailability,
    AlternativeStoreOutput,
)

logger = logging.getLogger("rakoon_backend.budget_shopping")

router = APIRouter()

def format_rupiah(amount: float) -> str:
    """Helper untuk merapikan format Rupiah."""
    return f"Rp{amount:,.2f}".replace(",", ".")

def is_valid_uuid(val: str) -> bool:
    try:
        uuid.UUID(str(val))
        return True
    except (ValueError, TypeError, AttributeError):
        return False

@router.post("/recommend", response_model=BudgetRecommendResponse, status_code=status.HTTP_200_OK)
def recommend_budget_shopping(payload: BudgetRecommendRequest, db: Session = Depends(get_db)):
    """
    Rekomendasi belanja berdasarkan budget & daftar barang (Single-Store Full Match MVP).
    - HANYA merekomendasikan toko yang memiliki 100% seluruh barang yang diminta.
    - HANYA menggunakan PriceEntry yang terverifikasi (status_verifikasi = "verified").
    - Memilih toko dengan total_cost TERBESAR yang masih <= budget (memaksimalkan pemanfaatan budget).
    """
    try:
        user_budget = float(payload.budget)
        
        # 1. Peta kuantitas permintaan pengguna per product_id (string key)
        qty_map: Dict[str, int] = {}
        for item in payload.items:
            pid_str = str(item.product_id)
            qty_map[pid_str] = qty_map.get(pid_str, 0) + item.qty

        requested_pids = list(qty_map.keys())
        if not requested_pids:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Daftar barang kebutuhan tidak boleh kosong."
            )

        # Filter valid UUIDs for DB query
        valid_uuid_pids = [pid for pid in requested_pids if is_valid_uuid(pid)]
        total_requested_count = len(requested_pids)

        # Jika ada barang yang ID-nya bukan UUID valid, barang tersebut dipastikan tidak ada di DB (bisa membatalkan 100% full match)
        query_results = []
        if valid_uuid_pids:
            # 2. Subquery untuk mengambil timestamp terbaru per (product_id, store_id) - HANYA status_verifikasi == "verified"
            subquery = (
                db.query(
                    PriceEntry.product_id.label("pid"),
                    PriceEntry.store_id.label("sid"),
                    func.max(PriceEntry.timestamp).label("max_ts")
                )
                .filter(
                    PriceEntry.product_id.in_(valid_uuid_pids),
                    func.lower(PriceEntry.status_verifikasi) == VerificationStatus.VERIFIED.value
                )
                .group_by(PriceEntry.product_id, PriceEntry.store_id)
                .subquery()
            )

            # 3. Query harga terbaru yang verified beserta info Product dan Store
            query_results = (
                db.query(
                    PriceEntry.product_id,
                    PriceEntry.store_id,
                    PriceEntry.harga,
                    Product.nama.label("nama_produk"),
                    Store.nama.label("nama_toko"),
                    Store.alamat.label("alamat_toko"),
                    Store.lat.label("lat_toko"),
                    Store.lng.label("lng_toko")
                )
                .join(
                    subquery,
                    (PriceEntry.product_id == subquery.c.pid) &
                    (PriceEntry.store_id == subquery.c.sid) &
                    (PriceEntry.timestamp == subquery.c.max_ts)
                )
                .join(Product, PriceEntry.product_id == Product.id)
                .join(Store, PriceEntry.store_id == Store.id)
                .filter(func.lower(PriceEntry.status_verifikasi) == VerificationStatus.VERIFIED.value)
                .all()
            )

        # 4. Grouping entri harga berdasarkan store_id
        # stores_data[store_id] = { "store_info": StoreInfoOutput, "items_map": { pid: { nama, harga } } }
        stores_data: Dict[str, Dict[str, Any]] = {}
        for row in query_results:
            sid = str(row.store_id)
            pid = str(row.product_id)

            if sid not in stores_data:
                stores_data[sid] = {
                    "store_info": StoreInfoOutput(
                        store_id=sid,
                        nama=row.nama_toko or "Toko Tanpa Nama",
                        alamat=row.alamat_toko,
                        lat=float(row.lat_toko) if row.lat_toko is not None else None,
                        lng=float(row.lng_toko) if row.lng_toko is not None else None,
                    ),
                    "items_map": {}
                }

            stores_data[sid]["items_map"][pid] = {
                "nama_produk": row.nama_produk or "Produk Tanpa Nama",
                "harga_satuan": float(row.harga)
            }

        # 5. Evaluasi Toko (Full Match & Cost Calculation)
        total_requested_count = len(requested_pids)
        all_full_match_stores = []

        for sid, sdata in stores_data.items():
            items_map = sdata["items_map"]
            
            # Persyaratan Mutlak MVP: FULL MATCH ONLY (100% ketersediaan barang)
            if len(items_map) == total_requested_count:
                total_cost = 0.0
                item_results: List[BudgetItemResult] = []

                for pid in requested_pids:
                    pinfo = items_map[pid]
                    q = qty_map[pid]
                    unit_p = pinfo["harga_satuan"]
                    subtotal = unit_p * q
                    total_cost += subtotal

                    item_results.append(BudgetItemResult(
                        product_id=pid,
                        nama_produk=pinfo["nama_produk"],
                        qty=q,
                        harga_satuan=unit_p,
                        subtotal=subtotal
                    ))

                all_full_match_stores.append({
                    "store_info": sdata["store_info"],
                    "total_cost": total_cost,
                    "remaining_budget": user_budget - total_cost,
                    "item_results": item_results
                })

        # 6. Skenario Penentuan Rekomendasi
        # Skenario A: Ada toko Full Match yang valid <= budget
        within_budget_stores = [s for s in all_full_match_stores if s["total_cost"] <= user_budget]
        if within_budget_stores:
            # Algoritma Pemilihan: Pilih total_cost TERKECIL, break ties dengan store name secara alfabetis
            within_budget_sorted = sorted(
                within_budget_stores,
                key=lambda x: (x["total_cost"], x["store_info"].nama)
            )
            best_store_candidate = within_budget_sorted[0]
            
            store_name = best_store_candidate["store_info"].nama
            cost_str = format_rupiah(best_store_candidate["total_cost"])
            rem_str = format_rupiah(best_store_candidate["remaining_budget"])
            budget_str = format_rupiah(user_budget)

            explanation = (
                f"Rekomendasi Utama: {store_name} dapat memenuhi seluruh {total_requested_count} daftar barang "
                f"kebutuhan Anda (100% Full Match) dengan total belanja termurah {cost_str}. "
                f"Sisa budget Anda adalah {rem_str}."
            )

            # Build store alternatives from remaining within-budget stores
            alternatives = []
            for alt in within_budget_sorted[1:3]:
                alternatives.append(AlternativeStoreOutput(
                    store_info=alt["store_info"],
                    total_cost=alt["total_cost"],
                    remaining_budget=alt["remaining_budget"],
                    is_full_match=True,
                    matched_products_count=total_requested_count,
                    items=alt["item_results"]
                ))

            return BudgetRecommendResponse(
                budget=user_budget,
                total_cost=best_store_candidate["total_cost"],
                remaining_budget=best_store_candidate["remaining_budget"],
                is_full_match=True,
                recommended_store=best_store_candidate["store_info"],
                items=best_store_candidate["item_results"],
                explanation=explanation,
                store_alternatives=alternatives
            )

        # Skenario B: Ada toko Full Match tetapi SEMUA total_cost > budget
        if all_full_match_stores:
            over_budget_sorted = sorted(
                all_full_match_stores,
                key=lambda x: (x["total_cost"], x["store_info"].nama)
            )
            cheapest_overbudget_store = over_budget_sorted[0]
            store_name = cheapest_overbudget_store["store_info"].nama
            cheapest_cost = cheapest_overbudget_store["total_cost"]
            shortage = cheapest_cost - user_budget
            
            explanation = (
                f"Budget {format_rupiah(user_budget)} tidak mencukupi untuk membeli seluruh barang kebutuhan di toko mana pun. "
                f"Estimasi total belanja termurah yang memiliki 100% barang Anda adalah {format_rupiah(cheapest_cost)} "
                f"di {store_name} (Kurang {format_rupiah(shortage)}). Kurangi kuantitas barang atau naikkan budget."
            )

            # Alternatives for over-budget stores
            alternatives = []
            for alt in over_budget_sorted[1:3]:
                alternatives.append(AlternativeStoreOutput(
                    store_info=alt["store_info"],
                    total_cost=alt["total_cost"],
                    remaining_budget=alt["remaining_budget"],
                    is_full_match=True,
                    matched_products_count=total_requested_count,
                    items=alt["item_results"]
                ))

            return BudgetRecommendResponse(
                budget=user_budget,
                total_cost=cheapest_cost,
                remaining_budget=user_budget - cheapest_cost,
                is_full_match=True,
                recommended_store=cheapest_overbudget_store["store_info"],
                items=cheapest_overbudget_store["item_results"],
                explanation=explanation,
                store_alternatives=alternatives
            )

        # Skenario C: Tidak ada toko yang memuat 100% barang di database
        availabilities = []
        for pid in requested_pids:
            product_rows = [row for row in query_results if str(row.product_id) == pid]
            if product_rows:
                cheapest_row = min(product_rows, key=lambda x: x.harga)
                prod_name = cheapest_row.nama_produk or "Produk Tanpa Nama"
                availabilities.append(ProductAvailability(
                    product_id=pid,
                    nama_produk=prod_name,
                    is_available=True,
                    harga_terendah=float(cheapest_row.harga),
                    toko_terendah=cheapest_row.nama_toko or "Toko"
                ))
            else:
                prod_db = db.query(Product).filter(Product.id == pid).first()
                prod_name = prod_db.nama if prod_db else "Produk Tidak Dikenal"
                availabilities.append(ProductAvailability(
                    product_id=pid,
                    nama_produk=prod_name,
                    is_available=False,
                    harga_terendah=None,
                    toko_terendah=None
                ))

        explanation = (
            f"Tidak ditemukan toko di database yang menjual seluruh ({total_requested_count}) daftar barang "
            f"kebutuhan Anda sekaligus secara lengkap."
        )

        return BudgetRecommendResponse(
            budget=user_budget,
            total_cost=0.0,
            remaining_budget=user_budget,
            is_full_match=False,
            recommended_store=None,
            items=[],
            explanation=explanation,
            product_availabilities=availabilities
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error during budget shopping recommendation: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Terjadi kesalahan saat memproses rekomendasi budget shopping: {str(e)}"
        )
