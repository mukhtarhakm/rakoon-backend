import os
import hmac
import logging
from typing import Dict, Any, Optional
import httpx

logger = logging.getLogger("rakoon_backend.xendit")

XENDIT_INVOICE_API_URL = "https://api.xendit.co/v2/invoices"


class XenditConfigError(Exception):
    """Raised when Xendit configuration is invalid or missing."""
    pass


class XenditAPIError(Exception):
    """Raised when communication with Xendit API fails."""
    pass


def get_xendit_config() -> Dict[str, Any]:
    """
    Validates and retrieves Xendit Sandbox configuration.
    Strictly enforces sandbox environment; rejects live keys or production mode.
    """
    secret_key = os.getenv("XENDIT_SECRET_KEY", "").strip()
    webhook_token = os.getenv("XENDIT_WEBHOOK_TOKEN", "").strip()
    environment = os.getenv("XENDIT_ENVIRONMENT", "sandbox").strip().lower()

    if not secret_key:
        raise XenditConfigError("XENDIT_SECRET_KEY is required but not configured.")

    # Strictly reject non-sandbox environments in this stage
    if environment != "sandbox":
        raise XenditConfigError(f"Xendit environment must be 'sandbox', got: {environment}")

    # Enforce sandbox development key prefix
    if not secret_key.startswith("xnd_development_"):
        raise XenditConfigError("XENDIT_SECRET_KEY must be a valid Xendit sandbox development key (prefix 'xnd_development_').")

    return {
        "secret_key": secret_key,
        "webhook_token": webhook_token,
        "environment": environment,
        "success_redirect_url": os.getenv("XENDIT_SUCCESS_REDIRECT_URL"),
        "failure_redirect_url": os.getenv("XENDIT_FAILURE_REDIRECT_URL"),
    }


def is_xendit_sandbox_configured() -> bool:
    """Checks if Xendit sandbox is safely and completely configured."""
    try:
        get_xendit_config()
        return True
    except XenditConfigError:
        return False


def verify_webhook_token(token: Optional[str]) -> bool:
    """
    Validates incoming webhook callback token using constant-time comparison.
    """
    if not token or not isinstance(token, str):
        return False

    expected_token = os.getenv("XENDIT_WEBHOOK_TOKEN", "").strip()
    if not expected_token:
        logger.warning("XENDIT_WEBHOOK_TOKEN is not configured; rejecting webhook.")
        return False

    return hmac.compare_digest(token, expected_token)


def create_invoice(
    external_id: str,
    amount: int,
    description: str,
    invoice_duration_seconds: int = 86400,
    client: Optional[httpx.Client] = None,
) -> Dict[str, Any]:
    """
    Creates a Xendit Sandbox invoice.
    Uses HTTP Basic Auth with secret_key as username and empty password.
    """
    config = get_xendit_config()
    secret_key = config["secret_key"]

    payload: Dict[str, Any] = {
        "external_id": external_id,
        "amount": int(amount),
        "description": description,
        "invoice_duration": invoice_duration_seconds,
        "currency": "IDR",
    }

    if config["success_redirect_url"]:
        payload["success_redirect_url"] = config["success_redirect_url"]
    if config["failure_redirect_url"]:
        payload["failure_redirect_url"] = config["failure_redirect_url"]

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    try:
        if client is not None:
            response = client.post(
                XENDIT_INVOICE_API_URL,
                json=payload,
                headers=headers,
                auth=(secret_key, ""),
                timeout=10.0,
            )
        else:
            with httpx.Client(timeout=10.0) as http_client:
                response = http_client.post(
                    XENDIT_INVOICE_API_URL,
                    json=payload,
                    headers=headers,
                    auth=(secret_key, ""),
                )

        if response.status_code not in (200, 201):
            logger.error(f"Xendit API returned status {response.status_code}")
            raise XenditAPIError(f"Xendit API returned status code {response.status_code}")

        data = response.json()
        if "id" not in data or "invoice_url" not in data:
            raise XenditAPIError("Invalid response structure from Xendit API")

        return data

    except httpx.RequestError as e:
        logger.error(f"Network error communicating with Xendit: {type(e).__name__}")
        raise XenditAPIError("Failed to reach Xendit payment gateway.") from e
