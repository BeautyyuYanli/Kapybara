"""Read an explicit registered model for opt-in live acceptance scripts.

These local administrator scripts already have the control database credential. Keys
stay in the current provider row and the borrowed model connection, never in output.
"""

from contextlib import asynccontextmanager
from uuid import UUID

import httpx2
from psycopg_pool import AsyncConnectionPool

from kapy.agent import create_model_backend
from kapy.gateway.models import Providers, SessionModelConfig
from kapy.gateway.storage import Metadata
from kapy.settings import Settings


@asynccontextmanager
async def selected_model(model_id: str):
    settings = Settings()
    async with (
        AsyncConnectionPool(settings.database_url.get_secret_value(), open=False) as pool,
        httpx2.AsyncClient(trust_env=False) as http,
    ):
        providers = Providers(Metadata(pool, schema=settings.database_schema), http)
        provider, model, window, output = await providers.effective(
            SessionModelConfig(model_id=UUID(model_id))
        )
        backend = create_model_backend(providers.connection(provider), http)
        yield backend, model["name"], window, output
