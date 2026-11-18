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
TRUSTED_SUBJECTS={
    'repo:'+REPOSITORY+':ref:refs/heads/main',
    'repo:noetishq-rgb@344326924/Noetis-Intelligence-2-@1460555916:ref:refs/heads/main',
}


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
    expected={'repository':REPOSITORY,'repository_id':'1460555916','ref':'refs/heads/main','workflow_ref':WORKFLOW}
    mismatched=[k for k,v in expected.items() if claims.get(k)!=v]
    if claims.get('sub') not in TRUSTED_SUBJECTS:mismatched.append('sub')
    if claims['event_name'] not in {'schedule','workflow_dispatch','push'}:mismatched.append('event_name')
    if mismatched:
        logging.getLogger(__name__).warning('scheduler_identity_rejected fields=%s',','.join(mismatched))
        raise HTTPException(403,'Workflow is not authorized')
    return claims


def capture_binding(brand_id):
    config = settings()
    if not config.crawl_scheduler_enabled:
        return None
    if brand_id == config.crawl_brand_id and config.crawl_service_user_id and config.crawl_worker_database_url:
        return config.crawl_service_user_id, config.crawl_worker_database_url
    extra = config.crawl_bindings.get(brand_id)
    return (extra.service_user_id, extra.database_url) if extra else None


def run_bound_capture(brand_id=None):
    config=settings()
    brand_id = brand_id or config.crawl_brand_id
    binding = capture_binding(brand_id)
    if not binding:
        raise HTTPException(503, 'The agency needs to configure website capture for this brand')
    actor, secret = binding
    url = secret.get_secret_value()
    from app.worker import run_once,validate_service
    with transaction(actor,url) as c:
        check_runtime_role(c);validate_service(c,actor)
        brands=c.execute('SELECT id FROM mi.brand_workspaces').fetchall()
        if {r['id'] for r in brands} != {brand_id}:
            raise HTTPException(503,'Website worker brand binding is unsafe')
        if not c.execute("SELECT mi.has_brand_scope(%s,'crawl:run') AS allowed", (brand_id,)).fetchone()['allowed']:
            raise HTTPException(503, 'Website worker lacks capture scope')
    # No owner transaction remains open while the separate service writes audit entries.
    had_work=run_once(actor,database_url=url)
    with transaction(actor,url) as c:
        row=c.execute("SELECT id,status,last_error FROM mi.jobs WHERE brand_id=%s AND kind='website_crawl' ORDER BY created_at DESC LIMIT 1",(brand_id,)).fetchone()
    return {'processed':had_work,'brand_id':brand_id,'job':row}


@router.get('/internal/crawl/brands', dependencies=[Depends(verify_scheduler)], include_in_schema=False)
def scheduled_brands():
    config = settings()
    brands = set(config.crawl_bindings)
    if config.crawl_brand_id:
        brands.add(config.crawl_brand_id)
    return sorted(str(brand) for brand in brands if capture_binding(brand))


@router.post('/internal/crawl',dependencies=[Depends(verify_scheduler)],include_in_schema=False)
def scheduled_capture(brand_id: UUID | None = None):
    return run_bound_capture(brand_id) if brand_id else run_bound_capture()


@router.post('/v1/brands/{brand_id}/brain/capture/run')
def owner_capture(brand_id:UUID,user=Depends(current_user)):
    with transaction(user['id']) as c:
        brand=require_brand(c,brand_id,'crawl:run');require_admin(c,brand['organization_id'])
    return run_bound_capture(brand_id)
