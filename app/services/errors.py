import logging
import traceback
from typing import Optional
from fastapi import HTTPException, status
from sqlalchemy.exc import SQLAlchemyError


def exc_summary(exc: BaseException) -> str:
    """
    Teks exception yang aman untuk log. Pesan SQLAlchemy memuat nilai parameter query
    (nama, telepon, id pengguna), jadi untuk SQLAlchemyError hanya nama tipenya yang dicatat.
    """
    if isinstance(exc, SQLAlchemyError):
        orig = getattr(exc, "orig", None)
        return f"{type(exc).__name__}({type(orig).__name__})" if orig is not None else type(exc).__name__
    return str(exc)


def safe_error(
    logger: logging.Logger,
    exc: Exception,
    public_message: str = "Terjadi kesalahan pada server.",
    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR,
    log_prefix: Optional[str] = None
) -> HTTPException:
    """
    Mencatat error internal ke logger server, lalu mengembalikan HTTPException dengan pesan
    publik generik yang aman tanpa membocorkan teks exception asli (seperti potongan SQL,
    traceback, atau detail teknis). Untuk SQLAlchemyError, log hanya memuat tipe dan lokasi
    kode (tanpa pesan/traceback, karena itu memuat nilai parameter query).
    """
    if isinstance(exc, HTTPException):
        return exc

    prefix = f"{log_prefix}: " if log_prefix else ""
    if isinstance(exc, SQLAlchemyError):
        frames = traceback.extract_tb(exc.__traceback__)
        where = f" di {frames[-1].filename}:{frames[-1].lineno}" if frames else ""
        logger.error(f"{prefix}{exc_summary(exc)}{where}")
    else:
        logger.error(f"{prefix}{str(exc)}", exc_info=True)
    return HTTPException(status_code=status_code, detail=public_message)
