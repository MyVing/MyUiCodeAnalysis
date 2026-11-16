"""健康检查。"""

from datetime import UTC, datetime

from fastapi import APIRouter

from app.api.deps import SettingsDep
from app.schemas import HealthStatus

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthStatus)
def health(settings: SettingsDep) -> HealthStatus:
    return HealthStatus(
        name=settings.name,
        version=settings.version,
        server_time=datetime.now(UTC),
    )
