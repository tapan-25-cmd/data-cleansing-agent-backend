from fastapi import APIRouter

from app.services.evaluation_service import load_evaluation_catalog


router = APIRouter(prefix="/evaluations", tags=["evaluations"])


@router.get("/latest")
def latest_evaluation() -> dict:
    return load_evaluation_catalog()
