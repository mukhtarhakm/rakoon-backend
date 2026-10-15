"""Test helper: valid claim evidence (paths bound to a user) without touching Supabase."""
from app.services.storage_upload import new_claim_path


def evidence_for(user_id: str) -> dict:
    return {
        "applicant_name": "Budi Santoso",
        "applicant_phone": "081234567890",
        "applicant_role": "Pemilik",
        "document_url": new_claim_path("documents", ".pdf", user_id),
        "store_photo_url": new_claim_path("photos", ".jpg", user_id),
    }
