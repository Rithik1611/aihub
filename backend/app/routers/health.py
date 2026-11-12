from fastapi import APIRouter
from pydantic import BaseModel

from app.config import get_settings

router = APIRouter(tags=["health"])


class Health(BaseModel):
    status: str
    app: str


@router.get("/health", response_model=Health)
def health() -> Health:
    return Health(status="ok", app=get_settings().app_name)
