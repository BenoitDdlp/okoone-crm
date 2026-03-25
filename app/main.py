import hashlib
import hmac
import time
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, Form, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings
from app.database import init_db

VERSION = "0.1.0"

OPEN_PATHS = {"/health", "/login", "/static"}


def _sign_session(ts: str) -> str:
    return hmac.new(settings.SESSION_SECRET.encode(), ts.encode(), hashlib.sha256).hexdigest()[:32]


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


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path

        # API routes use X-API-Key header
        if path.startswith("/api/"):
            api_key = request.headers.get("X-API-Key")
            # Also accept session cookie for htmx calls from dashboard
            session = request.cookies.get("crm_session", "")
            if api_key != settings.API_KEY and not _verify_session(session):
                return Response(content='{"detail":"Unauthorized"}', status_code=401, media_type="application/json")
            return await call_next(request)

        # Open paths (health, login, static)
        if any(path.startswith(p) for p in OPEN_PATHS):
            return await call_next(request)

        # Dashboard routes require session cookie
        session = request.cookies.get("crm_session", "")
        if not _verify_session(session):
            return RedirectResponse(url="/login", status_code=302)

        return await call_next(request)


def _verify_session(cookie: str) -> bool:
    if not cookie or ":" not in cookie:
        return False
    ts, sig = cookie.rsplit(":", 1)
    try:
        created = int(ts)
    except ValueError:
        return False
    # Sessions expire after 30 days
    if time.time() - created > 30 * 86400:
        return False
    return hmac.compare_digest(sig, _sign_session(ts))


app.add_middleware(AuthMiddleware)

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = ""):
    return templates.TemplateResponse(request, "login.html", {"error": error})


@app.post("/login")
async def login_submit(password: str = Form(...)):
    if password != settings.DASHBOARD_PASSWORD:
        return RedirectResponse(url="/login?error=1", status_code=302)
    ts = str(int(time.time()))
    sig = _sign_session(ts)
    response = RedirectResponse(url="/prospects", status_code=302)
    response.set_cookie("crm_session", f"{ts}:{sig}", max_age=30 * 86400, httponly=True, samesite="lax")
    return response


@app.get("/logout")
async def logout():
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie("crm_session")
    return response


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
