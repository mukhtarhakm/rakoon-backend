import logging
from fastapi import APIRouter, status
from fastapi.responses import JSONResponse
from app.database import check_database_readiness

logger = logging.getLogger("rakoon_backend.health")

router = APIRouter()

APP_NAME = "Rakoon Backend"
APP_VERSION = "1.0.0"


@router.api_route("/health/live", methods=["GET", "HEAD"], status_code=status.HTTP_200_OK)
@router.api_route("/live", methods=["GET", "HEAD"], status_code=status.HTTP_200_OK)
def liveness_check():
    """
    Liveness probe: verifies that the application process is running and responding.
    Does not depend on external services or database connectivity.
    """
    return {
        "status": "ok",
        "app": APP_NAME,
        "version": APP_VERSION,
    }


@router.api_route("/health/ready", methods=["GET", "HEAD"], status_code=status.HTTP_200_OK)
@router.api_route("/ready", methods=["GET", "HEAD"], status_code=status.HTTP_200_OK)
def readiness_check():
    """
    Readiness probe: verifies that the application is ready to accept traffic.
    Checks database availability using a lightweight 'SELECT 1' query.
    Returns HTTP 200 if database is reachable, HTTP 503 otherwise.
    Does not leak connection details, credentials, or internal exceptions.
    """
    if check_database_readiness():
        return {
            "status": "ready",
            "app": APP_NAME,
            "database": "connected",
        }
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={
            "status": "unready",
            "app": APP_NAME,
            "database": "disconnected",
            "detail": "Database connection is not ready",
        },
    )


@router.api_route("/health", methods=["GET", "HEAD"], status_code=status.HTTP_200_OK)
def health_check():
    """
    Main health probe (used by Render healthCheckPath and monitoring tools).
    Evaluates database readiness while preserving backward-compatible JSON structure.
    Returns HTTP 200 when database is ready, HTTP 503 when database is unready.
    """
    if check_database_readiness():
        return {
            "status": "ok",
            "app": APP_NAME,
            "version": APP_VERSION,
            "database": "connected",
        }
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={
            "status": "unready",
            "app": APP_NAME,
            "version": APP_VERSION,
            "database": "disconnected",
            "detail": "Database connection is not ready",
        },
    )
