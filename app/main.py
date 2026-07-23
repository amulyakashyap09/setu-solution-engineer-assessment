"""FastAPI application factory."""

from __future__ import annotations

import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from pydantic import ValidationError

from .config import settings
from .db import connect, init_db
from .errors import register_error_handlers
from .ingest import ingest_events
from .routers import events, meta, reconciliation, transactions
from .schemas import EventIn

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("recon")

DESCRIPTION = """
Backend service for ingesting payment lifecycle events and reconciling
the payment leg against the settlement leg.

**Design in one paragraph.** `events` is an append-only log keyed on
`event_id`, which makes ingestion idempotent at the database level.
`transactions` is a projection of that log, maintained on write, storing
the first observed timestamp of each lifecycle event rather than a
mutable status. Status is a generated column derived from those
timestamps, so it can never drift from the event history and can still be
indexed. Payment state and settlement state are tracked as two
independent axes - that is what makes "settled but failed" representable
instead of overwritten.
"""


def _seed_if_empty() -> None:
    """Load the sample file on first boot so a fresh deployment is
    immediately explorable. Idempotent by construction: if the events
    table already has rows we skip, and even if we did not, every event
    would be absorbed as a duplicate."""
    if not settings.seed_on_startup:
        return

    source = Path(settings.seed_file)
    if not source.exists():
        logger.info("no seed file at %s; starting empty", source)
        return

    conn = connect()
    try:
        existing = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
        if existing:
            logger.info("database already holds %d events; skipping seed", existing)
            return

        records = json.loads(source.read_text())
        if isinstance(records, dict):
            records = records.get("events", [])

        events, rejected = [], 0
        for record in records:
            try:
                events.append(EventIn.model_validate(record))
            except ValidationError:
                rejected += 1

        for start in range(0, len(events), 2000):
            ingest_events(conn, events[start:start + 2000])

        logger.info("seeded %d events from %s (%d rejected)",
                    len(events), source.name, rejected)
    except Exception:
        # A failed seed must not stop the service from starting.
        logger.exception("seeding failed; continuing with an empty database")
    finally:
        conn.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    logger.info("database ready at %s", settings.database_path)
    _seed_if_empty()
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        version=settings.version,
        description=DESCRIPTION,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    register_error_handlers(app)

    @app.middleware("http")
    async def add_timing_header(request: Request, call_next):
        started = time.perf_counter()
        response = await call_next(request)
        elapsed_ms = (time.perf_counter() - started) * 1000
        response.headers["X-Response-Time-Ms"] = f"{elapsed_ms:.2f}"
        return response

    app.include_router(meta.router)
    app.include_router(events.router)
    app.include_router(transactions.router)
    app.include_router(reconciliation.router)

    @app.get("/", include_in_schema=False)
    def root() -> dict:
        return {
            "service": settings.app_name,
            "version": settings.version,
            "docs": "/docs",
            "endpoints": [
                "POST /events",
                "GET /events",
                "GET /transactions",
                "GET /transactions/{transaction_id}",
                "GET /reconciliation/summary",
                "GET /reconciliation/discrepancies",
                "GET /merchants",
                "GET /health",
            ],
        }

    return app


app = create_app()
