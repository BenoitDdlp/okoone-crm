import hashlib
import hmac
import logging
import sys
import time
from contextlib import asynccontextmanager
from typing import AsyncGenerator

# Force all logs to stderr (captured by systemd journalctl)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    stream=sys.stderr,
    force=True,
)
# Make our loggers verbose
for name in ("okoone", "okoone.loop", "okoone.scraper", "okoone.claude"):
    logging.getLogger(name).setLevel(logging.DEBUG)

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
    from app.scheduler.jobs import run_research_loop, LOOP_STATE, set_scraper
    from app.scraper.linkedin import LinkedInScraper
    from app.scraper.rate_limiter import RateLimiter

    # Scraper: lazy-init (browser starts on first search, not at boot)
    rate_limiter = RateLimiter(
        account_created_date=settings.LINKEDIN_ACCOUNT_CREATED,
        max_daily_profiles=settings.LINKEDIN_DAILY_PROFILE_LIMIT,
        max_daily_searches=settings.LINKEDIN_DAILY_SEARCH_LIMIT,
    )
    scraper = LinkedInScraper(
        profile_dir=settings.LINKEDIN_PROFILE_DIR,
        rate_limiter=rate_limiter,
    )
    app.state.scraper = scraper
    app.state.rate_limiter = rate_limiter
    set_scraper(scraper, rate_limiter)

    # Background loop
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        run_research_loop,
        "interval",
        minutes=settings.SCRAPE_INTERVAL_MINUTES,
        id="research_loop",
        replace_existing=True,
        next_run_time=None,
    )
    scheduler.start()
    app.state.scheduler = scheduler
    app.state.loop_state = LOOP_STATE

    yield

    scheduler.shutdown()
    await scraper.stop()


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


from app.routers import dashboard, prospects, searches, campaigns, scraper, eval as eval_router, chat, strategy

app.include_router(dashboard.router)
app.include_router(prospects.router)
app.include_router(searches.router)
app.include_router(campaigns.router)
app.include_router(scraper.router)
app.include_router(eval_router.router)
app.include_router(chat.router)
app.include_router(strategy.router)


@app.get("/health")
async def health():
    return {"status": "ok", "version": VERSION}
