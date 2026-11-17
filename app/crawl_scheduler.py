"""One bounded capture invocation, authorized by an owner or exact GitHub workflow."""
from functools import lru_cache
from uuid import UUID
import jwt
import logging
from fastapi import APIRouter, Depends, HTTPException, Request
from app.auth import current_user,require_admin,require_brand
from app.config import settings
from app.db import transaction,check_runtime_role

router=APIRouter(tags=['Website capture'])
ISSUER='https://token.actions.githubusercontent.com'
AUDIENCE='noetis-crawl:prj_rByocViGrd5OJlsCEYPqvchdqrSS'
REPOSITORY='noetishq-rgb/Noetis-Intelligence-2-'
WORKFLOW=REPOSITORY+'/.github/workflows/workers.yml@refs/heads/main'


@lru_cache
def github_keys():
    return jwt.PyJWKClient(ISSUER+'/.well-known/jwks',cache_keys=True,lifespan=300,timeout=8)


def verify_scheduler(request:Request):
    value=request.headers.get('authorization','')
    if not value.startswith('Bearer ') or len(value)>16000:raise HTTPException(401,'Workflow identity required')
    token=value[7:]
    try:
        key=github_keys().get_signing_key_from_jwt(token).key
        claims=jwt.decode(token,key,algorithms=['RS256'],issuer=ISSUER,audience=AUDIENCE,
                          options={'require':['exp','iat','nbf','iss','aud','sub','repository','repository_id','ref','workflow_ref','event_name']},leeway=10)
    except (jwt.PyJWTError,ValueError,TypeError):raise HTTPException(401,'Invalid workflow identity')
    expected={'repository':REPOSITORY,'repository_id':'1460555916','ref':'refs/heads/main',
              'workflow_ref':WORKFLOW,'sub':'repo:'+REPOSITORY+':ref:refs/heads/main'}
    mismatched=[k for k,v in expected.items() if claims.get(k)!=v]
    if claims['event_name'] not in {'schedule','workflow_dispatch','push'}:mismatched.append('event_name')
    if mismatched:
        logging.getLogger(__name__).warning('scheduler_identity_rejected fields=%s',','.join(mismatched))
        raise HTTPException(403,'Workflow is not authorized')
    return claims


def run_bound_capture():
    config=settings()
    if not config.crawl_scheduler_enabled or not config.crawl_worker_database_url or not config.crawl_service_user_id or not config.crawl_brand_id:
        raise HTTPException(503,'Website capture worker is not configured')
    url=config.crawl_worker_database_url.get_secret_value();actor=config.crawl_service_user_id
    from app.worker import run_once,validate_service
    with transaction(actor,url) as c:
        check_runtime_role(c);validate_service(c,actor)
        brands=c.execute('SELECT id FROM mi.brand_workspaces').fetchall()
        if {r['id'] for r in brands}!={config.crawl_brand_id}:
            raise HTTPException(503,'Website worker brand binding is unsafe')
    # No owner transaction remains open while the separate service writes audit entries.
    had_work=run_once(actor,database_url=url)
    with transaction(actor,url) as c:
        row=c.execute("SELECT id,status,last_error FROM mi.jobs WHERE brand_id=%s AND kind='website_crawl' ORDER BY created_at DESC LIMIT 1",(config.crawl_brand_id,)).fetchone()
    return {'processed':had_work,'brand_id':config.crawl_brand_id,'job':row}


@router.post('/internal/crawl',dependencies=[Depends(verify_scheduler)],include_in_schema=False)
def scheduled_capture():
    return run_bound_capture()


@router.post('/v1/brands/{brand_id}/brain/capture/run')
def owner_capture(brand_id:UUID,user=Depends(current_user)):
    with transaction(user['id']) as c:
        brand=require_brand(c,brand_id,'crawl:run');require_admin(c,brand['organization_id'])
    if settings().crawl_brand_id!=brand_id:raise HTTPException(503,'This brand’s capture worker is not configured yet')
    return run_bound_capture()
