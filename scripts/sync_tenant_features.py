from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.core.db import AsyncSessionLocal
from app.services.tenant_feature_service import TenantFeatureService


async def run() -> None:
    async with AsyncSessionLocal() as session:
        async with session.begin():
            inserted = await TenantFeatureService(session).sync_all_tenants_with_registry()
            print(f"tenant_features_inserted={inserted}")


if __name__ == "__main__":
    asyncio.run(run())

