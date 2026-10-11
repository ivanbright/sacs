"""FastAPI application factory for SACS."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import __version__
from .config import get_settings
from .db import get_sessionmaker
from .events import background_dispatcher
from .scheduler import bell_loop

from .routers import auth as auth_router
from .routers import academics as academics_router
from .routers import bell as bell_router
from .routers import invitations as invitations_router
from .routers import platform as platform_router
from .routers import realtime as realtime_router
from .routers import requests as requests_router
from .routers import schools as schools_router
from .routers import timetable as timetable_router


def cors_urls() -> list[str]:
    settings = get_settings()
    origins = list(settings.cors_origins)
    if settings.env in ("demo", "dev"):
        origins = ["*"]
    return origins


def create_app() -> FastAPI:
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        session_factory = get_sessionmaker()
        tasks = [
            bell_loop(session_factory),
            background_dispatcher(session_factory),
        ]
        for task in tasks:
            app.state.tasks.append(asyncio.create_task(task))
        yield
        for task in app.state.tasks:
            task.cancel()
        await asyncio.gather(*app.state.tasks, return_exceptions=True)

    app = FastAPI(
        title="SACS - Smart Academic Coordination System",
        version=__version__,
        description="Multi-tenant academic coordination platform: one shared backend, "
        "isolated school tenants, real-time delivery, and a software bell scheduler.",
        lifespan=lifespan,
    )
    app.state.tasks = []

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_urls(),
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(invitations_router.router)
    app.include_router(auth_router.router)
    app.include_router(platform_router.router)
    app.include_router(schools_router.router)
    app.include_router(academics_router.router)
    app.include_router(timetable_router.router)
    app.include_router(requests_router.router)
    app.include_router(bell_router.router)
    app.include_router(bell_router.test_router)
    app.include_router(realtime_router.router)

    @app.get("/api/health", tags=["system"])
    def public_health() -> dict:
        return {"status": "ok", "service": "sacs", "version": __version__}

    @app.get("/", include_in_schema=False)
    def root() -> dict:
        return {
            "service": "sacs",
            "version": __version__,
            "docs": "/docs",
            "health": "/api/health",
            "note": "No browser frontend is shipped; the SACS CLI is the interface.",
        }

    return app


__all__ = ["create_app"]