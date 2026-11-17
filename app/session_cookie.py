"""Browser session continuity without putting bearer secrets in browser storage."""
from fastapi import HTTPException, Request, Response
from app.config import settings

COOKIE = "__Secure-noetis_session"


def set_session_cookie(response: Response, tokens):
    response.set_cookie(COOKIE, tokens.refresh_token, max_age=settings().refresh_days*86400,
                        secure=True, httponly=True, samesite="lax", path="/v1/auth")


def clear_session_cookie(response: Response):
    response.delete_cookie(COOKIE, path="/v1/auth", secure=True, httponly=True, samesite="lax")


def require_same_origin(request: Request):
    from urllib.parse import urlsplit
    origin=request.headers.get('origin')
    if request.headers.get('x-noetis-session')!='1' or request.headers.get('sec-fetch-site')=='cross-site':
        raise HTTPException(403,'Use the application sign-in page')
    if origin and urlsplit(origin).netloc!=request.headers.get('host'):
        raise HTTPException(403,'Use the application sign-in page')
