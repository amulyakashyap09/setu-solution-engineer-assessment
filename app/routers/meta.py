"""Health check and merchant lookup - small but needed for a live demo."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends

from .. import queries
from ..config import settings
from ..db import get_db
from ..schemas import HealthResponse

router = APIRouter(tags=["meta"])


@router.get("/health", response_model=HealthResponse, summary="Liveness and row counts")
def health(conn: sqlite3.Connection = Depends(get_db)) -> HealthResponse:
    counts = conn.execute(
        """SELECT (SELECT COUNT(*) FROM events)       AS events,
                  (SELECT COUNT(*) FROM transactions) AS transactions,
                  (SELECT COUNT(*) FROM merchants)    AS merchants"""
    ).fetchone()
    return HealthResponse(
        status="ok",
        version=settings.version,
        database=settings.database_path,
        events=counts["events"],
        transactions=counts["transactions"],
        merchants=counts["merchants"],
    )


@router.get("/merchants", summary="List merchants with transaction volume")
def merchants(conn: sqlite3.Connection = Depends(get_db)) -> dict:
    items = queries.list_merchants(conn)
    return {"total": len(items), "items": items}
