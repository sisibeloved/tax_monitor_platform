from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
def get_health() -> dict[str, str]:
    """Report that the HTTP service is ready."""

    return {"status": "ok", "service": "tax-risk"}
