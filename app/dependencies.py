import os
import time
import uuid
import logging
import jwt
import httpx
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Any, Optional
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from dotenv import load_dotenv

load_dotenv(override=False)

logger = logging.getLogger("rakoon_backend")

# Fail-fast check on startup for core credentials
SUPABASE_URL = os.getenv("SUPABASE_URL")
if not SUPABASE_URL:
    raise RuntimeError("FATAL STARTUP ERROR: SUPABASE_URL environment variable is not configured.")

# Construct JWKS configurations
base_url = SUPABASE_URL.rstrip("/")
JWKS_URL = f"{base_url}/auth/v1/.well-known/jwks.json"
EXPECTED_ISSUER = f"{base_url}/auth/v1"

# Define security dependency (auto_error=False to customize error formatting)
security = HTTPBearer(auto_error=False)

class JwksUnavailableError(Exception):
    """JWKS tidak bisa diambil dan tidak ada kunci layak pakai di cache (dipetakan ke HTTP 503)."""


class JwksCache:
    """
    Cache JWKS in-memory. Kunci lama boleh dipakai saat refresh gagal, tetapi hanya sampai
    max_stale_seconds. Fetch sinkron dibatasi (min_refetch_seconds) setelah kegagalan dan untuk
    kid tak dikenal, supaya request anonim dengan kid acak tidak memaksa panggilan HTTP berulang.
    Batas: kunci baru hasil rotasi baru dikenali setelah jeda min_refetch_seconds dari fetch terakhir.
    """
    def __init__(self, ttl_seconds: int = 300, max_stale_seconds: int = 3600, min_refetch_seconds: int = 30):
        self.ttl = ttl_seconds
        self.max_stale = max_stale_seconds
        self.min_refetch = min_refetch_seconds
        self.keys: Dict[str, Dict[str, Any]] = {}
        self.last_fetched: float = 0.0
        self.last_failure: float = 0.0

    def get_jwk(self, kid: str, jwks_url: str) -> Dict[str, Any]:
        now = time.time()
        cached = self.keys.get(kid)
        # Return cached key if it exists and cache is not expired
        if cached is not None and (now - self.last_fetched) < self.ttl:
            return cached

        if self._may_fetch(now, kid_known=cached is not None):
            self._refresh(jwks_url)
            now = time.time()
            cached = self.keys.get(kid)

        if cached is not None:
            if (now - self.last_fetched) < self.max_stale:
                return cached
            raise JwksUnavailableError("Cached JWKS key exceeded the maximum allowed age.")
        if not self.keys:
            raise JwksUnavailableError("JWKS is unavailable and the cache is empty.")
        raise KeyError(f"Key ID {kid} not found in JWKS.")

    def _may_fetch(self, now: float, kid_known: bool) -> bool:
        if (now - self.last_failure) < self.min_refetch:
            return False
        if not kid_known and self.keys and (now - self.last_fetched) < self.min_refetch:
            return False
        return True

    def _refresh(self, jwks_url: str):
        try:
            logger.info(f"Fetching Supabase JWKS from {jwks_url}")
            response = httpx.get(jwks_url, timeout=5.0)
            response.raise_for_status()
            data = response.json()

            new_keys = {}
            for key in data.get("keys", []):
                key_id = key.get("kid")
                if key_id:
                    new_keys[key_id] = key

            self.keys = new_keys
            self.last_fetched = time.time()
        except Exception as e:
            self.last_failure = time.time()
            logger.error(f"Error fetching JWKS from Supabase: {type(e).__name__}")
            # Retain old keys on transient network failure (bounded by max_stale in get_jwk)

jwks_cache = JwksCache()

def verify_access_token(token: str) -> Dict[str, Any]:
    """
    Memverifikasi JWT access token Supabase (ES256 via JWKS) dan mengembalikan klaim yang sudah
    terverifikasi tanda tangan, exp, aud, iss, dan sub (UUID). Gagal -> HTTP 401.
    """
    try:
        # 1. Retrieve unverified header to check alg and kid
        header = jwt.get_unverified_header(token)
        alg = header.get("alg")
        kid = header.get("kid")

        # 2. Strict algorithm validation to prevent algorithm confusion attacks
        if alg != "ES256":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Not authenticated"
            )

        if not kid:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Not authenticated"
            )

        # 3. Retrieve JWK matching kid (from cache or refresh)
        try:
            jwk = jwks_cache.get_jwk(kid, JWKS_URL)
        except KeyError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Not authenticated"
            )

        # 4. Validate JWK type
        if jwk.get("kty") != "EC" or jwk.get("crv") != "P-256":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Not authenticated"
            )

        # 5. Construct Elliptic Curve public key
        try:
            public_key = jwt.PyJWK.from_dict(jwk).key
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Not authenticated"
            )

        # 6. Verify signature and claims (exp, aud, iss)
        payload = jwt.decode(
            token,
            key=public_key,
            algorithms=["ES256"],
            audience="authenticated",
            issuer=EXPECTED_ISSUER,
            leeway=60,
            options={
                "verify_aud": True,
                "verify_iss": True,
                "require": ["exp", "sub"]
            }
        )

        user_id = payload.get("sub")
        if not user_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Not authenticated"
            )

        # 7. Validate that subject is a valid UUID
        try:
            uuid.UUID(user_id)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Not authenticated"
            )

        return payload

    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated"
        )
    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated"
        )
    except HTTPException:
        raise
    except JwksUnavailableError as e:
        logger.error(f"Authentication unavailable: {e}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication service temporarily unavailable"
        )
    except Exception as e:
        # InvalidKeyError, PyJWKError, TypeError pada klaim (mis. iat null), dll: token tak bisa dipercaya -> 401, bukan 500.
        logger.warning(f"Token verification failed unexpectedly ({type(e).__name__}).")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated"
        )


def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)) -> str:
    """
    FastAPI dependency to validate a user's Supabase JWT access token.
    Returns the subject user_id (UUID); raises HTTP 401 Unauthorized if validation fails.
    """
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated"
        )
    return verify_access_token(credentials.credentials)["sub"]


def get_admin_emails() -> set[str]:
    raw = os.getenv("ADMIN_EMAILS", "")
    return {email.strip().lower() for email in raw.split(",") if email.strip()}


# Hasil positif Admin API dicache singkat agar tiap request admin tidak memukul Supabase.
# Negatif/gagal tidak dicache. Batas: perubahan konfirmasi email baru terlihat setelah TTL.
ADMIN_CONFIRM_TTL_SECONDS = 60.0
ADMIN_API_TIMEOUT_SECONDS = 3.0
_admin_confirm_cache: Dict[str, float] = {}


def _email_confirmed_via_admin_api(user_id: str, email: str) -> bool:
    """
    True hanya bila Supabase Admin API menyatakan email_confirmed_at terisi untuk user ini.
    Fail-closed: kunci service kosong, timeout, non-200, atau respons tak terduga = False.
    """
    now = time.time()
    cache_key = f"{user_id}:{email}"
    if _admin_confirm_cache.get(cache_key, 0.0) > now:
        return True

    service_key = os.getenv("SUPABASE_SERVICE_KEY", "").strip()
    if not service_key:
        logger.warning("SUPABASE_SERVICE_KEY is not configured; cannot confirm admin email (fail-closed).")
        return False

    try:
        response = httpx.get(
            f"{base_url}/auth/v1/admin/users/{user_id}",
            headers={"apikey": service_key, "Authorization": f"Bearer {service_key}"},
            timeout=ADMIN_API_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        data = response.json()
    except Exception as e:
        logger.warning(f"Supabase Admin API check failed ({type(e).__name__}); treating user as non-admin.")
        return False

    if not isinstance(data, dict) or not data.get("email_confirmed_at"):
        return False
    api_email = data.get("email")
    if isinstance(api_email, str) and api_email.strip().lower() != email:
        return False

    for key in [k for k, expiry in _admin_confirm_cache.items() if expiry <= now]:
        del _admin_confirm_cache[key]
    _admin_confirm_cache[cache_key] = now + ADMIN_CONFIRM_TTL_SECONDS
    return True


def _is_confirmed_admin(user_id: str, token_email: Optional[str]) -> bool:
    if not token_email:
        return False
    email = token_email.strip().lower()
    if email not in get_admin_emails():
        return False
    return _email_confirmed_via_admin_api(user_id, email)


IDENTITY_TTL_SECONDS = 300.0
IDENTITY_MAX_LOOKUPS = 50
IDENTITY_WORKERS = 5
_identity_cache: Dict[str, tuple] = {}


def _fetch_identity(user_id: str, service_key: str) -> Optional[Dict[str, Optional[str]]]:
    try:
        response = httpx.get(
            f"{base_url}/auth/v1/admin/users/{user_id}",
            headers={"apikey": service_key, "Authorization": f"Bearer {service_key}"},
            timeout=ADMIN_API_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        data = response.json()
    except Exception as e:
        logger.warning(f"Supabase Admin API identity lookup failed ({type(e).__name__}).")
        return None
    if not isinstance(data, dict):
        return None
    meta = data.get("user_metadata") if isinstance(data.get("user_metadata"), dict) else {}
    nama = meta.get("name") or meta.get("full_name") or meta.get("display_name")
    email = data.get("email")
    return {
        "nama": nama if isinstance(nama, str) else None,
        "email": email if isinstance(email, str) else None,
    }


def get_user_identities(user_ids) -> Dict[str, Dict[str, Optional[str]]]:
    """
    Nama/email akun lewat Supabase Admin API, untuk tampilan admin. Best-effort: id yang gagal
    dicari tidak ada di hasil (bukan error). Id unik, hanya UUID valid, maks IDENTITY_MAX_LOOKUPS
    panggilan per pemanggilan (sisanya dilewati), paralel terbatas IDENTITY_WORKERS, hasil
    positif dicache IDENTITY_TTL_SECONDS.
    """
    now = time.time()
    result: Dict[str, Dict[str, Optional[str]]] = {}
    todo = []
    for raw in dict.fromkeys(str(u) for u in user_ids):
        try:
            uid = str(uuid.UUID(raw))
        except ValueError:
            continue
        cached = _identity_cache.get(uid)
        if cached and cached[0] > now:
            result[raw] = cached[1]
        else:
            todo.append((raw, uid))
    service_key = os.getenv("SUPABASE_SERVICE_KEY", "").strip()
    todo = todo[:IDENTITY_MAX_LOOKUPS]
    if not todo or not service_key:
        return result

    with ThreadPoolExecutor(max_workers=IDENTITY_WORKERS) as pool:
        fetched = list(pool.map(lambda t: _fetch_identity(t[1], service_key), todo))
    for key in [k for k, (expiry, _) in _identity_cache.items() if expiry <= now]:
        del _identity_cache[key]
    for (raw, uid), identity in zip(todo, fetched):
        if identity is not None:
            _identity_cache[uid] = (now + IDENTITY_TTL_SECONDS, identity)
            result[raw] = identity
    return result


def get_current_user_profile(
    user_id: str = Depends(get_current_user),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> Dict[str, Any]:
    """
    Mengembalikan data profil pengguna aktif (user_id, email, nama, role).
    Opsi B: role 'admin' HANYA bila email pada token yang terverifikasi ada di ADMIN_EMAILS
    (tanpa beda huruf besar/kecil dan spasi) DAN email_confirmed_at terisi menurut Supabase Admin API.
    users.role, app_metadata.role, dan klaim role TIDAK dipakai. Nama/email hanya dari klaim token.
    """
    claims: Dict[str, Any] = {}
    if credentials and credentials.credentials:
        claims = verify_access_token(credentials.credentials)

    token_email = claims.get("email") if isinstance(claims.get("email"), str) else None
    user_meta = claims.get("user_metadata")
    if not isinstance(user_meta, dict):
        user_meta = {}
    nama = user_meta.get("name") or user_meta.get("full_name") or user_meta.get("display_name")
    if not isinstance(nama, str):
        nama = None

    email = token_email

    return {
        "user_id": user_id,
        "email": email,
        "nama": nama,
        "role": "admin" if _is_confirmed_admin(user_id, token_email) else "user"
    }


def get_current_admin_user(
    profile: Dict[str, Any] = Depends(get_current_user_profile)
) -> Dict[str, Any]:
    """
    FastAPI dependency untuk memastikan pengguna yang terotentikasi memiliki peran 'admin'.
    Jika bukan admin, melempar HTTP 403 Forbidden.
    """
    if profile.get("role") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Akses ditolak: Hanya admin yang dapat melakukan aksi ini."
        )
    return profile
