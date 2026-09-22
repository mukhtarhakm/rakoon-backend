from fastapi import APIRouter, Depends
from app.dependencies import get_current_user, get_current_user_profile
from app.models.schemas import UserProfileResponse

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

@router.get("/me", response_model=UserProfileResponse)
def get_my_profile(profile: dict = Depends(get_current_user_profile)):
    """
    Endpoint untuk mendapatkan informasi profil pengguna yang sedang login,
    termasuk status peran ('admin' atau 'user').
    """
    return profile
