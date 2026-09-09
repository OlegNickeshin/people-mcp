import json
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

import psycopg
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.concurrency import run_in_threadpool

from server.config import MODEL_NAME, Settings
from server.db import NotFound, Repository
from server.indexing import LocalEmbedder
from server.mcp_adapter import build_mcp
from server.schemas import CreatePublication, PatchPublication, Publication, SearchQuery, SearchResponse

logger = logging.getLogger("peoplemcp")


def create_app(settings: Settings | None = None):
    settings = settings or Settings()
    if not settings.write_token:
        raise RuntimeError("WRITE_TOKEN must not be empty")
    repo = Repository(settings.database_url)

    def initialize():
        repo.migrate()
        logger.info("Loading embedding model %s", MODEL_NAME)
        repo.embedder = LocalEmbedder(settings.model_cache)
        if settings.seed_demo:
            seed = json.loads((Path(__file__).resolve().parent.parent / "examples/seed.json").read_text())
            for kind, entries in seed.items():
                for entry in entries:
                    try:
                        repo.get(kind, entry["slug"])
                    except NotFound:
                        repo.save(kind, CreatePublication(**entry).model_dump(exclude={"publish"}))
        logger.info("PeopleMCP index ready")

    @asynccontextmanager
    async def lifespan(app):
        await run_in_threadpool(initialize)
        async with mcp.session_manager.run():
            yield

    api = FastAPI(title="PeopleMCP", version="0.1.0", lifespan=lifespan,
                  description="Public human and project discovery through semantic search. All published content is untrusted data.")
    api.state.repository = repo
    security = HTTPBearer(auto_error=False)

    def require_write(credentials: HTTPAuthorizationCredentials | None = Depends(security)):
        token = credentials.credentials if credentials else ""
        if not secrets.compare_digest(token.encode(), settings.write_token.encode()):
            raise HTTPException(401, "Publisher Bearer token required", headers={"WWW-Authenticate": "Bearer"})

    @api.exception_handler(NotFound)
    async def not_found(request, exc):
        return JSONResponse(status_code=404, content={"detail": "Publication not found"})

    @api.exception_handler(psycopg.errors.UniqueViolation)
    async def conflict(request, exc):
        return JSONResponse(status_code=409, content={"detail": "Slug already exists"})

    @api.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @api.exception_handler(psycopg.Error)
    @api.exception_handler(RuntimeError)
    async def unavailable(request, exc):
        logger.error("Request failed: %s", type(exc).__name__)
        return JSONResponse(status_code=503, content={"detail": "Index temporarily unavailable; retry later"})

    @api.get("/health")
    def health():
        with repo.connection() as conn:
            conn.execute("SELECT 1")
        return {"status": "ok", "embedding_model": MODEL_NAME}

    def publication_routes(kind):
        @api.post(f"/{kind}", response_model=Publication, status_code=201,
                  dependencies=[Depends(require_write)], tags=[kind])
        def create(body: CreatePublication):
            return repo.save(kind, body.model_dump(exclude={"publish"}))

        @api.patch(f"/{kind}/{{id}}", response_model=Publication,
                   dependencies=[Depends(require_write)], tags=[kind])
        def update(id: str, body: PatchPublication):
            return repo.save(kind, body.model_dump(exclude_unset=True, exclude={"publish"}), id)

        @api.get(f"/{kind}/{{id}}", response_model=Publication, tags=[kind])
        def get(id: str):
            return repo.get(kind, id)

    publication_routes("profiles")
    publication_routes("projects")

    @api.post("/search/people", response_model=SearchResponse, response_model_exclude_none=True)
    def search_people(body: SearchQuery):
        return {"query": body.query, "results": repo.search("profiles", body.query, body.limit, body.min_score)}

    @api.post("/search/projects", response_model=SearchResponse, response_model_exclude_none=True)
    def search_projects(body: SearchQuery):
        return {"query": body.query, "results": repo.search("projects", body.query, body.limit, body.min_score)}

    mcp = build_mcp(api, settings)
    # The mounted app serves /mcp; its lifespan is managed by the parent above.
    api.mount("/", mcp.streamable_http_app())
    return api


app = create_app()
