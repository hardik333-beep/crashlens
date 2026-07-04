"""Health endpoint.

Returns overall status plus per-dependency reachability booleans. The status is
always "ok" when the process is serving requests; the booleans tell an operator
whether the database and Redis are reachable right now.
"""

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.health import check_database, check_redis

router = APIRouter()


class HealthResponse(BaseModel):
    status: str
    database: bool
    redis: bool


@router.get("/health", response_model=HealthResponse, tags=["ops"])
async def health(request: Request) -> HealthResponse:
    settings = request.app.state.settings
    database_ok = await check_database(settings.database_url)
    redis_ok = await check_redis(settings.redis_url)
    return HealthResponse(status="ok", database=database_ok, redis=redis_ok)
