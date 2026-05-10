"""FastAPI app factory.

Importable as `src.api.main:app` for `uvicorn` and Cloud Run.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.api.routes import router
from src.core.config import get_settings
from src.core.logging import configure_logging, get_logger


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001
    configure_logging()
    logger = get_logger(__name__)
    settings = get_settings()
    logger.info(
        "app.startup",
        env=settings.env,
        gcp_project=settings.gcp_project_id,
        dataset=settings.bq_dataset,
        model=settings.llm_model,
    )
    yield
    logger.info("app.shutdown")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="BI Copilot",
        description="Multi-agent conversational BI over BigQuery.",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.api_cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router)
    return app


app = create_app()
