import logging
import uuid
from types import SimpleNamespace
from typing import List, Dict, Any, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.database import get_db
from app.services.errors import safe_error
from app.services.current_price import get_current_prices
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
    """Helper untuk merapikan format Rupiah (pemisah ribuan titik, desimal koma)."""
    int_part = int(amount)
    dec_part = int(round((amount - int_part) * 100))
    if dec_part >= 100:
        int_part += 1
        dec_part = 0
    formatted_int = f"{int_part:,}".replace(",", ".")
    if dec_part > 0:
        return f"Rp{formatted_int},{dec_part:02d}"
    return f"Rp{formatted_int}"

def is_valid_uuid(val: str) -> bool:
    try:
        uuid.UUID(str(val))
        return True
    except (ValueError, TypeError, AttributeError):
        return False

def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    import math
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlon / 2) ** 2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return round(R * c, 2)

def canonical_uuid(val) -> Optional[str]:
    """Bentuk kanonik (huruf kecil, bertanda hubung) dari UUID valid apa pun; None bila bukan UUID."""
    try:
        return str(uuid.UUID(str(val)))
    except (ValueError, TypeError, AttributeError):
        return None

@router.post("/recommend", response_model=BudgetRecommendResponse, status_code=status.HTTP_200_OK)
def recommend_budget_shopping(payload: BudgetRecommendRequest, db: Session = Depends(get_db)):
    """
    Rekomendasi belanja berdasarkan budget & daftar barang (Single-Store Full Match MVP).
    - HANYA merekomendasikan toko yang memiliki 100% seluruh barang yang diminta.
    - HANYA menggunakan PriceEntry yang terverifikasi (status_verifikasi = "verified").
    - Memilih toko dengan total_cost TERKECIL yang masih <= budget (meminimalkan pengeluaran biaya belanja).
    """
    try:
        user_budget = float(payload.budget)
        
        # 1. Peta kuantitas permintaan pengguna per product_id (string key)
        qty_map: Dict[str, int] = {}
        for item in payload.items:
            pid_str = canonical_uuid(item.product_id) or str(item.product_id)
            qty_map[pid_str] = qty_map.get(pid_str, 0) + item.qty

        requested_pids = list(qty_map.keys())
        if not requested_pids:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Daftar barang kebutuhan tidak boleh kosong."
            )

        # Filter valid UUIDs for DB query
        valid_uuid_pids = [pid for pid in requested_pids if canonical_uuid(pid)]
        total_requested_count = len(requested_pids)

        # Jika ada barang yang ID-nya bukan UUID valid, barang tersebut dipastikan tidak ada di DB (bisa membatalkan 100% full match)
        query_results = []
        if valid_uuid_pids:
            # 2. Harga terkini per (produk, toko): terbaru, verified saja, tanpa jendela waktu (stale ditandai)
            current = get_current_prices(db, valid_uuid_pids)
            if current:
                store_rows = {
                    str(s.id): s
                    for s in db.query(Store).filter(Store.id.in_({sid for (_pid, sid) in current})).all()
                }
                product_rows = {
                    str(p.id): p
                    for p in db.query(Product).filter(Product.id.in_({pid for (pid, _sid) in current})).all()
                }
                for (pid, sid), price in sorted(current.items()):
                    store, product = store_rows.get(sid), product_rows.get(pid)
                    if store is None or product is None:
                        continue
                    query_results.append(SimpleNamespace(
                        product_id=pid,
                        store_id=sid,
                        harga=price.harga,
                        nama_produk=product.nama,
                        nama_toko=store.nama,
                        alamat_toko=store.alamat,
                        lat_toko=store.lat,
                        lng_toko=store.lng,
                        tanggal_harga=price.timestamp,
                        stale=price.stale,
                    ))

        # 4. Grouping entri harga berdasarkan store_id
        # stores_data[store_id] = { "store_info": StoreInfoOutput, "items_map": { pid: { nama, harga } } }
        stores_data: Dict[str, Dict[str, Any]] = {}
        for row in query_results:
            sid = str(row.store_id)
            pid = str(row.product_id)

            if sid not in stores_data:
                dist_km = None
                if payload.lat is not None and payload.lng is not None and row.lat_toko is not None and row.lng_toko is not None:
                    dist_km = haversine_distance(payload.lat, payload.lng, float(row.lat_toko), float(row.lng_toko))
                stores_data[sid] = {
                    "store_info": StoreInfoOutput(
                        store_id=sid,
                        nama=row.nama_toko or "Toko Tanpa Nama",
                        alamat=row.alamat_toko,
                        lat=float(row.lat_toko) if row.lat_toko is not None else None,
                        lng=float(row.lng_toko) if row.lng_toko is not None else None,
                        distance_km=dist_km,
                    ),
                    "items_map": {}
                }

            stores_data[sid]["items_map"][pid] = {
                "nama_produk": row.nama_produk or "Produk Tanpa Nama",
                "harga_satuan": float(row.harga),
                "tanggal_harga": row.tanggal_harga,
                "stale": row.stale,
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
                        subtotal=subtotal,
                        tanggal_harga=pinfo["tanggal_harga"],
                        stale=pinfo["stale"],
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
            # Algoritma Pemilihan: Pilih total_cost TERKECIL, break ties dengan jarak (distance_km) lalu nama toko
            within_budget_sorted = sorted(
                within_budget_stores,
                key=lambda x: (
                    x["total_cost"],
                    x["store_info"].distance_km if x["store_info"].distance_km is not None else 999999.0,
                    x["store_info"].nama
                )
            )
            best_store_candidate = within_budget_sorted[0]
            
            store_name = best_store_candidate["store_info"].nama
            cost_str = format_rupiah(best_store_candidate["total_cost"])
            rem_str = format_rupiah(best_store_candidate["remaining_budget"])
            budget_str = format_rupiah(user_budget)
            dist_text = f" (Jarak ~{best_store_candidate['store_info'].distance_km} km)" if best_store_candidate["store_info"].distance_km is not None else ""

            explanation = (
                f"Rekomendasi Utama: {store_name}{dist_text} dapat memenuhi seluruh {total_requested_count} daftar barang "
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
                key=lambda x: (
                    x["total_cost"],
                    x["store_info"].distance_km if x["store_info"].distance_km is not None else 999999.0,
                    x["store_info"].nama
                )
            )
            cheapest_overbudget_store = over_budget_sorted[0]
            store_name = cheapest_overbudget_store["store_info"].nama
            cheapest_cost = cheapest_overbudget_store["total_cost"]
            shortage = cheapest_cost - user_budget
            dist_text = f" (Jarak ~{cheapest_overbudget_store['store_info'].distance_km} km)" if cheapest_overbudget_store["store_info"].distance_km is not None else ""
            
            explanation = (
                f"Budget {format_rupiah(user_budget)} tidak mencukupi untuk membeli seluruh barang kebutuhan di toko mana pun. "
                f"Estimasi total belanja termurah yang memiliki 100% barang Anda adalah {format_rupiah(cheapest_cost)} "
                f"di {store_name}{dist_text} (Kurang {format_rupiah(shortage)}). Kurangi kuantitas barang atau naikkan budget."
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
        missing_pids = [
            pid for pid in requested_pids
            if not any(str(row.product_id) == pid for row in query_results)
        ]
        # Satu query untuk semua nama produk yang tidak punya harga (bukan satu query per produk)
        missing_valid = [pid for pid in missing_pids if canonical_uuid(pid)]
        missing_names = (
            {str(p.id): p.nama for p in db.query(Product).filter(Product.id.in_(missing_valid)).all()}
            if missing_valid else {}
        )
        for pid in requested_pids:
            product_rows = [row for row in query_results if str(row.product_id) == pid]
            if product_rows:
                cheapest_row = min(product_rows, key=lambda x: (x.stale, x.harga, x.store_id))
                prod_name = cheapest_row.nama_produk or "Produk Tanpa Nama"
                availabilities.append(ProductAvailability(
                    product_id=pid,
                    nama_produk=prod_name,
                    is_available=True,
                    harga_terendah=float(cheapest_row.harga),
                    toko_terendah=cheapest_row.nama_toko or "Toko",
                    stale=cheapest_row.stale,
                ))
            else:
                availabilities.append(ProductAvailability(
                    product_id=pid,
                    nama_produk=missing_names.get(pid, "Produk Tidak Dikenal"),
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
        raise safe_error(
            logger,
            e,
            "Terjadi kesalahan saat memproses rekomendasi budget shopping.",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            log_prefix="Error during budget shopping recommendation",
        )
