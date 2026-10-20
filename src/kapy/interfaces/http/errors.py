"""Router-local HTTP errors; validation and upstream failures never echo secret inputs."""

from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import ValidationError
from temporalio.service import RPCError

from kapy.control.models.types import (
    ModelAlreadyExists,
    ModelDiscoveryError,
)
from kapy.control.sessions import (
    ActivityIdentityConflict,
    SessionBusy,
    SessionExecutionError,
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
            except SessionExecutionError as error:
                return JSONResponse(
                    status_code={
                        "SessionBusy": 409,
                        "LifecycleError": 409,
                        "SessionAlreadyExists": 409,
                        "OwnershipLost": 409,
                        "NotFound": 404,
                        "OutcomeUnavailable": 404,
                        "InvalidParameters": 422,
                        "UnableToConfirm": 503,
                    }.get(error.category, 500),
                    content={
                        "detail": error.category,
                        "session_id": str(error.session_id),
                        "activity_id": str(error.activity_id),
                        "run_id": error.run_id,
                    },
                )
            except (RequestValidationError, ValidationError) as error:
                detail = [
                    {key: item[key] for key in ("loc", "msg", "type")} for item in error.errors()
                ]
                return JSONResponse(status_code=422, content={"detail": detail})
            except SessionBusy:
                return JSONResponse(status_code=409, content={"detail": "Session busy"})
            except ActivityIdentityConflict as error:
                return JSONResponse(
                    status_code=409,
                    content={
                        "detail": "Activity identity conflict",
                        "session_id": str(error.session_id),
                        "activity_id": str(error.activity_id),
                        "run_id": error.run_id,
                    },
                )
            except RPCError:
                return JSONResponse(
                    status_code=503, content={"detail": "Unable to confirm Activity state"}
                )
            except LifecycleError:
                return JSONResponse(
                    status_code=409, content={"detail": "Session lifecycle conflict"}
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
