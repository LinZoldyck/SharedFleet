import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import config
from .db import init_db
from .routers import admin, auth, billing, bookings, fines, fleet, home, messaging, portal
from .security import LoginRequired, same_origin
from .services import scheduler
from .web import redirect, render

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    if config.SECRET_KEY == "dev-insecure-change-me":
        logging.getLogger("sharedfleet").warning("SECRET_KEY par défaut : définissez-la dans .env avant toute mise en service.")
    task = None if config.DISABLE_SCHEDULER else asyncio.create_task(scheduler.run())
    yield
    if task:
        task.cancel()


app = FastAPI(title="SharedFleet", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def csrf_guard(request: Request, call_next):
    # API de l'extension Chrome : appelée depuis une autre origine, authentifiée par jeton (jamais par cookie)
    if request.url.path.startswith("/api/ext/"):
        cors = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "Authorization, Content-Type",
                "Access-Control-Allow-Methods": "GET, POST, OPTIONS"}
        if request.method == "OPTIONS":
            return PlainTextResponse("", status_code=204, headers=cors)
        response = await call_next(request)
        response.headers.update(cors)
        return response
    if not same_origin(request):
        return PlainTextResponse("Requête refusée (origine inattendue).", status_code=403)
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response


# Ajouté après csrf_guard : la session est donc ouverte avant lui (ordre inverse d'ajout)
app.add_middleware(SessionMiddleware, secret_key=config.SECRET_KEY, session_cookie="sharedfleet",
                   max_age=14 * 86400, same_site="lax", https_only=config.COOKIE_SECURE)
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")


@app.exception_handler(LoginRequired)
async def login_required(request: Request, _):
    return redirect("/connexion")


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    if exc.status_code == 303 and exc.headers:
        return redirect(exc.headers["Location"])
    if request.url.path.startswith(("/api/", "/webhooks/")):
        return PlainTextResponse(str(exc.detail), status_code=exc.status_code)
    return render(request, "error.html", status_code=exc.status_code, code=exc.status_code, detail=exc.detail)


@app.get("/sante", include_in_schema=False)
def health():
    return {"status": "ok"}


for module in (auth, home, bookings, fleet, messaging, fines, billing, admin, portal):
    app.include_router(module.router)
