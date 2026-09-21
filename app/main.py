from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.agents.chat_coordinator import ChatCoordinator
from app.agents.factory import create_inference_provider
from app.api import chat, evaluations, export, jobs, quality, review
from app.config import get_settings
from app.repositories.mongo import MongoRepositories
from app.rules.registry import load_default_registry
from app.services.ai_reading_test_service import AiReadingTestService
from app.services.export_service import ExportService
from app.services.processor import JobProcessor
from app.storage.local import LocalFileStorage


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    registry = load_default_registry()
    repositories = MongoRepositories(settings.mongodb_uri, settings.mongodb_db)
    repositories.ensure_indexes()
    file_storage = LocalFileStorage(settings.local_storage_root, settings.max_upload_bytes)
    app.state.repositories = repositories
    app.state.storage = file_storage
    app.state.registry = registry
    inference_provider = create_inference_provider(settings)
    app.state.ai_reading_test = AiReadingTestService(
        repositories,
        inference_provider,
        registry,
        provider_is_real=settings.ai_provider.strip().lower() != "mock",
        max_concurrency=settings.ai_max_concurrency,
        default_rounding_decimals=settings.default_rounding_decimals,
    )
    app.state.processor = JobProcessor(
        repositories,
        file_storage,
        registry,
        inference_provider,
        settings.default_rounding_decimals,
        settings.ai_max_concurrency,
        settings.pack_size_inference_enabled,
    )
    app.state.exporter = ExportService(repositories, file_storage)
    app.state.chat_coordinator = ChatCoordinator()
    yield
    repositories.close()


settings = get_settings()
app = FastAPI(title="UoM Data Cleansing Agent", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_origin],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(jobs.router, prefix=settings.api_prefix)
app.include_router(review.router, prefix=settings.api_prefix)
app.include_router(export.router, prefix=settings.api_prefix)
app.include_router(chat.router, prefix=settings.api_prefix)
app.include_router(evaluations.router, prefix=settings.api_prefix)
app.include_router(quality.router, prefix=settings.api_prefix)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
