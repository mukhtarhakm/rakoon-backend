import os
import time
import uuid
import logging
import jwt
import httpx
from typing import Dict, Any
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jwt.algorithms import ECAlgorithm
from dotenv import load_dotenv

load_dotenv(override=True)

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

class JwksCache:
    """
    Simple in-memory cache for JWKS keys to optimize authentication performance.
    """
    def __init__(self, ttl_seconds: int = 300):
        self.ttl = ttl_seconds
        self.keys: Dict[str, Dict[str, Any]] = {}
        self.last_fetched: float = 0.0

    def get_jwk(self, kid: str, jwks_url: str) -> Dict[str, Any]:
        now = time.time()
        # Return cached key if it exists and cache is not expired
        if kid in self.keys and (now - self.last_fetched) < self.ttl:
            return self.keys[kid]

        # Refresh cache
        self._refresh(jwks_url)
        
        if kid in self.keys:
            return self.keys[kid]
            
        raise KeyError(f"Key ID {kid} not found in JWKS.")

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
            logger.error(f"Error fetching JWKS from Supabase: {e}")
            # Retain old keys on transient network failure to avoid downtime

jwks_cache = JwksCache()

def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)) -> str:
    """
    FastAPI dependency to validate a user's Supabase JWT access token.
    Fetches the public key dynamically from the JWKS endpoint, validates claims,
    and returns the subject user_id (UUID).
    
    If validation fails, raises an HTTP 401 Unauthorized exception.
    """
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated"
        )
        
    token = credentials.credentials
    
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
            public_key = ECAlgorithm.from_jwk(jwk)
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
            
        return user_id

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
