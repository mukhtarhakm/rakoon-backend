from fastapi import APIRouter, Depends, HTTPException, status
from app.database import _is_production
from app.dependencies import get_current_user, get_current_user_profile
from app.models.schemas import UserProfileResponse

router = APIRouter()


def _not_in_production():
    # Endpoint debug: 404 di produksi (RAKOON_ENV/ENVIRONMENT=production), sebelum autentikasi diperiksa.
    if _is_production():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")


@router.get("/test-protected", dependencies=[Depends(_not_in_production)])
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
