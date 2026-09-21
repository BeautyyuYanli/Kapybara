"""Router-local HTTP errors; validation and upstream failures never echo secret inputs."""

from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import ValidationError

from kapy.agent_plugins import PluginOperationError
from kapy.control.models.types import (
    ModelAlreadyExists,
    ModelDiscoveryError,
)
from kapy.lifecycle import LifecycleError


class ControlRoute(APIRoute):
    """Apply control error mappings without changing the hosting application's handlers."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def handle(request: Request) -> Response:
            try:
                return await handler(request)
            except HTTPException:
                raise
            except (RequestValidationError, ValidationError) as error:
                detail = [
                    {key: item[key] for key in ("loc", "msg", "type")} for item in error.errors()
                ]
                return JSONResponse(status_code=422, content={"detail": detail})
            except LifecycleError:
                return JSONResponse(
                    status_code=409, content={"detail": "Session lifecycle conflict"}
                )
            except PluginOperationError:
                return JSONResponse(
                    status_code=503,
                    content={"detail": "Plugin operation failed; session close can be retried"},
                )
            except ModelAlreadyExists:
                return JSONResponse(status_code=409, content={"detail": "Resource conflict"})
            except LookupError:
                return JSONResponse(status_code=404, content={"detail": "Resource not found"})
            except ValueError:
                return JSONResponse(status_code=422, content={"detail": "Invalid parameters"})
            except ModelDiscoveryError:
                return JSONResponse(status_code=502, content={"detail": "Model discovery failed"})
            except Exception:
                return JSONResponse(status_code=500, content={"detail": "Internal server error"})

        return handle
