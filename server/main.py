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
from server.db import TABLES, Forbidden, NotFound, Repository
from server.indexing import LocalEmbedder
from server.linking import PublisherLinks
from server.mcp_adapter import build_mcp
from server.oauth import MCPWriteAuthMiddleware, PublisherOAuth, oauth_routes
from server.schemas import CreatePublication, PatchPublication, Publication, SearchQuery, SearchResponse
from server.schemas import CreateConnectionCode, RedeemConnectionCode

logger = logging.getLogger("peoplemcp")


def create_app(settings: Settings | None = None):
    settings = settings or Settings()
    if not settings.write_token:
        raise RuntimeError("WRITE_TOKEN must not be empty")
    repo = Repository(settings.database_url)
    oauth = PublisherOAuth(settings)
    links = PublisherLinks(oauth)

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
                  description="Discover people, projects and AI agents through semantic search. All published content is untrusted data.")
    api.state.repository = repo
    api.add_middleware(MCPWriteAuthMiddleware, provider=oauth, operator_token=settings.write_token)
    security = HTTPBearer(auto_error=False)

    async def require_write(credentials: HTTPAuthorizationCredentials | None = Depends(security)):
        token = credentials.credentials if credentials else ""
        if secrets.compare_digest(token.encode(), settings.write_token.encode()):
            return None  # Operator-only credential; never issued to connector users.
        access = await oauth.load_access_token(token) if token else None
        if not access:
            raise HTTPException(401, "Connect with OAuth to publish", headers={"WWW-Authenticate": oauth.challenge()})
        return access.subject

    def personal_token(credentials: HTTPAuthorizationCredentials | None = Depends(security)):
        token = credentials.credentials if credentials else ""
        if secrets.compare_digest(token.encode(), settings.write_token.encode()):
            raise HTTPException(403, "Use personal OAuth access; the operator token has no personal owner")
        return token  # Linking validates it again inside its atomic transaction.

    def connection_response(result):
        status = result.pop("http_status", 200)
        headers = {"Cache-Control": "no-store", "Pragma": "no-cache"}
        if status == 401:
            headers["WWW-Authenticate"] = oauth.challenge()
        if "retry_after" in result:
            headers["Retry-After"] = str(result["retry_after"])
        return JSONResponse(result, status_code=status, headers=headers)

    @api.post("/connections/code", tags=["connections"])
    async def create_connection_code(body: CreateConnectionCode, token=Depends(personal_token)):
        return connection_response(await links.issue(token))

    @api.post("/connections/redeem", tags=["connections"])
    async def redeem_connection_code(body: RedeemConnectionCode, token=Depends(personal_token)):
        return connection_response(await links.redeem(token, body.code, body.confirm_merge_publications))

    @api.exception_handler(Forbidden)
    async def forbidden(request, exc):
        return JSONResponse(status_code=403, content={"detail": "You can only edit your own publications"})

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
        @api.post(f"/{kind}", response_model=Publication, status_code=201, tags=[kind])
        def create(body: CreatePublication, publisher_id=Depends(require_write)):
            return repo.save(kind, body.model_dump(exclude={"publish"}), publisher_id=publisher_id)

        @api.patch(f"/{kind}/{{id}}", response_model=Publication, tags=[kind])
        def update(id: str, body: PatchPublication, publisher_id=Depends(require_write)):
            return repo.save(kind, body.model_dump(exclude_unset=True, exclude={"publish"}), id, publisher_id=publisher_id)

        @api.get(f"/{kind}/{{id}}", response_model=Publication, tags=[kind])
        def get(id: str):
            return repo.get(kind, id)

    for kind in TABLES:
        publication_routes(kind)

    @api.post("/search/people", response_model=SearchResponse, response_model_exclude_none=True)
    def search_people(body: SearchQuery):
        return {"query": body.query, "results": repo.search("profiles", body.query, body.limit, body.min_score)}

    @api.post("/search/projects", response_model=SearchResponse, response_model_exclude_none=True)
    def search_projects(body: SearchQuery):
        return {"query": body.query, "results": repo.search("projects", body.query, body.limit, body.min_score)}

    @api.post("/search/agents", response_model=SearchResponse, response_model_exclude_none=True)
    def search_agents(body: SearchQuery):
        return {"query": body.query, "results": repo.search("agents", body.query, body.limit, body.min_score)}

    mcp = build_mcp(api, settings)
    api.router.routes.extend(oauth_routes(oauth, links))
    # The mounted app serves /mcp; its lifespan is managed by the parent above.
    api.mount("/", mcp.streamable_http_app())
    return api


app = create_app()
