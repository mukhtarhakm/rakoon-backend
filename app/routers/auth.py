from fastapi import APIRouter, Depends
from app.dependencies import get_current_user

router = APIRouter()

@router.get("/test-protected")
def test_protected(user_id: str = Depends(get_current_user)):
    """
    Endpoint untuk menguji keabsahan token JWT Auth (Supabase).
    Mengembalikan user_id (UUID) jika token valid.
    Jika token tidak valid, `get_current_user` akan otomatis mengembalikan
    HTTP 401 Unauthorized.
    """
    return {"user_id": user_id}
