"""Bearer-protected, opt-in history routes for the CogniCore REST server."""

import os
import secrets
from tempfile import TemporaryFile

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from starlette.concurrency import run_in_threadpool

from .engine import open_history

UPLOAD_LIMIT = 128 * 1024**2


def _authorize(response: Response, authorization: str = Header("")):
    token = os.environ.get("COGNICORE_HISTORY_API_TOKEN", "")
    if len(token) < 32:
        raise HTTPException(404, "History API is disabled.")
    if not secrets.compare_digest(authorization.encode(), f"Bearer {token}".encode()):
        raise HTTPException(401, "History access denied.", headers={"WWW-Authenticate": "Bearer"})
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(prefix="/history", tags=["history"], dependencies=[Depends(_authorize)])


def _call(action, *args, **kwargs):
    from .sync import schedule

    try:
        result = getattr(open_history(), action)(*args, **kwargs)
        schedule(mutated=action in {"import_stream", "delete"})
        return result
    except KeyError as exc:
        raise HTTPException(404, exc.args[0]) from None
    except (PermissionError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from None


@router.post("/imports")
async def import_history(request: Request, project: str = "", consent: bool = False):
    if not consent:
        raise HTTPException(400, "Explicit consent is required to import conversation history.")
    if request.headers.get("content-type", "").split(";", 1)[0] not in {
        "application/zip", "application/json", "application/octet-stream",
    }:
        raise HTTPException(415, "Supply ZIP or JSON archive bytes.")
    size = 0
    with TemporaryFile(mode="w+b") as source:
        async for chunk in request.stream():
            size += len(chunk)
            if size > UPLOAD_LIMIT:
                raise HTTPException(413, "Archive exceeds the HTTP upload size limit.")
            await run_in_threadpool(source.write, chunk)
        return await run_in_threadpool(_call, "import_stream", source, consent=True, project=project)


@router.get("/imports")
def status():
    return _call("status")


@router.get("/imports/{batch}")
def import_status(batch: str):
    return _call("status", batch)


@router.get("/search")
def search(query: str, project: str = "", kind: str = "", limit: int = 10):
    return _call("search", query, project=project, kind=kind, limit=limit)


@router.get("/timeline")
def timeline(project: str = "", kind: str = "", query: str = "", earliest: bool = False, limit: int = 50):
    return _call("timeline", project=project, kind=kind, query=query, earliest=earliest, limit=limit)


@router.get("/reconstruct")
def reconstruct(query: str, project: str = "", limit: int = 20):
    return _call("reconstruct", query, project=project, limit=limit)


@router.get("/sources/{conversation}")
def inspect(conversation: str, message: str = "", limit: int = 50, offset: int = 0):
    return _call("inspect", conversation, message=message, limit=limit, offset=offset)


@router.get("/experiences/{ref}")
def experience(ref: str):
    return _call("experience", ref)


@router.get("/audit")
def audit(limit: int = 50):
    return _call("audit", limit=limit)


@router.delete("/imports/{batch}")
def delete_batch(batch: str):
    _call("delete", batch=batch)
    return {"deleted": True}


@router.delete("/sources/{conversation}")
def delete_source(conversation: str):
    _call("delete", conversation=conversation)
    return {"deleted": True}
