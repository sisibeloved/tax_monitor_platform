from fastapi import FastAPI

from tax_risk.api.routes.health import router as health_router


def create_app() -> FastAPI:
    """Create the tax risk monitoring API."""

    app = FastAPI(title="Group Income Tax Risk Monitoring Platform")
    app.include_router(health_router)
    return app
