"""Vercel ASGI entrypoint: one origin for static UI, authenticated API and readiness."""
from pathlib import Path
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from app.main import app
from app.db import check_runtime_role, transaction

# Vercel routes all requests to this ASGI application. Keep validation on API requests
# even on platforms where ASGI lifespan delivery differs from a persistent Uvicorn server.
@app.middleware('http')
async def deployment_role_guard(request,call_next):
    if request.url.path.startswith('/v1/') or request.url.path=='/health':
        from fastapi.responses import JSONResponse
        try:
            with transaction() as c:
                check_runtime_role(c)
                if not c.execute("SELECT pg_has_role(current_user,'mi_api','MEMBER') AS ok").fetchone()['ok']:
                    raise RuntimeError('API runtime role required')
        except Exception:
            return JSONResponse(status_code=503,content={'detail':'Application database configuration is unavailable or unsafe'})
    return await call_next(request)

@app.get('/login',include_in_schema=False)
@app.get('/owner',include_in_schema=False)
@app.get('/brain',include_in_schema=False)
def portal_page():
    return FileResponse(Path(__file__).resolve().parent/'web'/'index.html',headers={'Cache-Control':'no-store'})

app.mount('/',StaticFiles(directory=Path(__file__).resolve().parent/'web',html=True),name='dashboard')
