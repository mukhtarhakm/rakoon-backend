import logging
from typing import Optional
from fastapi import HTTPException, status


def safe_error(
    logger: logging.Logger,
    exc: Exception,
    public_message: str = "Terjadi kesalahan pada server.",
    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR,
    log_prefix: Optional[str] = None
) -> HTTPException:
    """
    Mencatat error internal secara lengkap ke logger server (termasuk traceback),
    lalu mengembalikan HTTPException dengan pesan publik generik yang aman tanpa
    membocorkan teks exception asli (seperti potongan SQL, traceback, atau detail teknis).
    """
    if isinstance(exc, HTTPException):
        return exc

    prefix = f"{log_prefix}: " if log_prefix else ""
    logger.error(f"{prefix}{str(exc)}", exc_info=True)
    return HTTPException(status_code=status_code, detail=public_message)
