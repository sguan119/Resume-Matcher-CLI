"""Health check and status endpoints."""

from fastapi import APIRouter

from app.config import settings
from app.database import db
from app.llm_dispatch import check_llm_health, get_llm_config
from app.schemas import HealthResponse, StatusResponse

router = APIRouter(tags=["Health"])


@router.get("/health", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    """Basic health check endpoint."""
    llm_status = await check_llm_health()

    return HealthResponse(
        status="healthy" if llm_status["healthy"] else "degraded",
        llm=llm_status,
    )


@router.get("/status", response_model=StatusResponse)
async def get_status() -> StatusResponse:
    """Get comprehensive application status.

    Returns:
        - LLM configuration status
        - Master resume existence
        - Database statistics
    """
    config = get_llm_config()
    llm_status = await check_llm_health(config)
    db_stats = db.get_stats()

    # Codex CLI uses ChatGPT subscription auth, no API key needed
    is_codex = getattr(settings, "llm_backend", "litellm") == "codex_cli"
    llm_configured = is_codex or bool(config.api_key) or config.provider == "ollama"

    return StatusResponse(
        status="ready" if llm_status["healthy"] and db_stats["has_master_resume"] else "setup_required",
        llm_configured=llm_configured,
        llm_healthy=llm_status["healthy"],
        has_master_resume=db_stats["has_master_resume"],
        database_stats=db_stats,
    )
