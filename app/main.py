from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings
from app.database import init_db

VERSION = "0.1.0"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    await init_db()

    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    scheduler = AsyncIOScheduler()
    scheduler.start()
    app.state.scheduler = scheduler

    yield

    scheduler.shutdown()


app = FastAPI(title=settings.APP_NAME, version=VERSION, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class APIKeyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        if request.url.path.startswith("/api/"):
            api_key = request.headers.get("X-API-Key")
            if api_key != settings.API_KEY:
                return Response(content='{"detail":"Invalid or missing API key"}', status_code=401, media_type="application/json")
        return await call_next(request)


app.add_middleware(APIKeyMiddleware)

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

from app.routers import dashboard, prospects, searches, campaigns, scraper, eval as eval_router, chat

app.include_router(dashboard.router)
app.include_router(prospects.router)
app.include_router(searches.router)
app.include_router(campaigns.router)
app.include_router(scraper.router)
app.include_router(eval_router.router)
app.include_router(chat.router)


@app.get("/health")
async def health():
    return {"status": "ok", "version": VERSION}
