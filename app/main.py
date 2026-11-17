from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import logging
import hmac
import secrets
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
import psycopg
from psycopg.types.json import Jsonb

from app.api_types import (AcceptExistingInvitation, AcceptInvitation, AcceptedInvitation,
    AuditView, BrandCreated, BrandView, InvitationCreated, JobView, OnboardingView,
    OrganizationView, ProfileConfirm, Questionnaire, StatusView, UserView, WorkflowView)
from app.auth import (actor_transaction, auth_event, current_user, digest, issue_tokens,
    password_hash, require_admin, require_brand, throttle, verify_password)
from app.config import settings
from app.db import check_runtime_role, set_actor, transaction
from app.models import (BrandCreate, BrandProfile, CrawlRequest, FounderActivation, InviteCreate, Login,
    OnboardingInput, OnboardingSave, RefreshRequest, Signup, TokenPair, VersionRequest, WorkflowEvent)
from app.repository import latest_profile, lock_key, payload_hash, persist_profile, transition, workflow_for_brand
from app.services.crawl import CrawlError, validate_url
from app.services.extraction import extract_profile, merge_onboarding
from app.workflow import GateContext, TransitionError

from app.models import BrandProfileV11 as BrandProfile

logger = logging.getLogger("marketing_intelligence")


@asynccontextmanager
async def lifespan(app):
    settings().signing_secret()
    with transaction() as connection:
        check_runtime_role(connection)
        if not connection.execute("SELECT pg_has_role(current_user,'mi_api','MEMBER') AS allowed").fetchone()["allowed"]:
            raise RuntimeError("API requires a mi_api database login")
    yield


app = FastAPI(title="Marketing Intelligence API", version="0.4.1", lifespan=lifespan,
    docs_url=None, redoc_url=None, description="Research backend with Phase 3 provenance checks and explicit human semantic review. Model calls require server configuration. Includes the Brand Brain onboarding adapter. No external marketing execution.")


@app.middleware("http")
async def request_metadata(request: Request, call_next):
    request_id = str(uuid4())
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    logger.info("request_complete request_id=%s method=%s status=%s", request_id, request.method, response.status_code)
    return response


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    # Default validation errors can echo passwords or invitation tokens.
    return JSONResponse(status_code=422, content={"detail": [{"loc": e["loc"], "type": e["type"], "msg": e["msg"]} for e in exc.errors()]})


@app.exception_handler(psycopg.Error)
async def database_error(request, exc):
    code = exc.sqlstate or "connection_error"
    logger.error("database_error sqlstate=%s", code)
    status = 403 if code == "42501" else 409 if code.startswith(("23", "22")) else 503
    return JSONResponse(status_code=status, content={"detail": "Database operation rejected" if status != 503 else "Database temporarily unavailable"})


@app.exception_handler(TransitionError)
async def transition_error(request, exc):
    return JSONResponse(status_code=409, content={"detail": str(exc)})


def view(model, row):
    return model(**{key: row[key] for key in model.model_fields})


def validate_idempotency(key: str):
    if not 8 <= len(key) <= 128 or any(ord(c) < 33 or ord(c) > 126 for c in key):
        raise HTTPException(422, "Idempotency-Key must be 8 to 128 printable ASCII characters")


@app.get("/health", response_model=StatusView, tags=["System"])
def health():
    return StatusView(status="ok")


def create_founder(connection, body, user_id, organization_id):
    connection.execute("INSERT INTO identity.users(id,email,full_name,password_hash) VALUES(%s,%s,%s,%s)",
        (user_id, str(body.email).lower(), body.full_name, password_hash(body.password)))
    connection.execute("INSERT INTO mi.organizations(id,name,owner_id) VALUES(%s,%s,%s)", (organization_id, body.organization_name, user_id))
    connection.execute("INSERT INTO mi.organization_memberships(organization_id,user_id,role) VALUES(%s,%s,'founder')", (organization_id, user_id))
    tokens = issue_tokens(connection, user_id)
    auth_event(connection, "founder_registered", True, user_id)
    return tokens


@app.post("/v1/auth/register", response_model=TokenPair, status_code=201, tags=["Authentication"])
def register(body: Signup, request: Request):
    if not settings().allow_signup:
        raise HTTPException(404, "Registration unavailable")
    user_id, organization_id = uuid4(), uuid4()
    with transaction(user_id) as connection:
        tokens = create_founder(connection, body, user_id, organization_id)
    return tokens


@app.post("/v1/auth/activate-founder", response_model=TokenPair, status_code=201, tags=["Authentication"])
def activate_founder(body: FounderActivation, request: Request):
    config = settings()
    if not config.bootstrap_email or not config.bootstrap_token_sha256 or not config.bootstrap_expires_at:
        raise HTTPException(404, "Founder activation unavailable")
    now = datetime.now(timezone.utc)
    expires = config.bootstrap_expires_at
    valid = (expires.tzinfo is not None and expires > now and
        str(body.email).lower() == config.bootstrap_email.lower() and
        hmac.compare_digest(digest(body.token), config.bootstrap_token_sha256.get_secret_value()))
    actor, organization_id = uuid4(), uuid4()
    failure, tokens = None, None
    with transaction(actor) as connection:
        keys = throttle(connection, "founder-activation:" + str(body.email).lower(), request)
        if not keys:
            failure = (429, "Try activation again later")
        elif not valid:
            failure = (400, "Activation link is invalid or expired")
            for key in keys:connection.execute("SELECT identity.record_attempt(%s,false)", (key,))
            auth_event(connection, "founder_activation_rejected", False, subject=str(body.email))
        else:
            lock_key(connection, "founder-activation:" + config.bootstrap_email.lower())
            existing = connection.execute("SELECT id FROM identity.login_lookup(%s)", (str(body.email).lower(),)).fetchone()
            if existing:
                failure = (409, "This founder account is already activated; sign in instead")
            else:
                signup = Signup(email=body.email, full_name=body.full_name, password=body.password, organization_name=config.bootstrap_organization)
                tokens = create_founder(connection, signup, actor, organization_id)
    if failure:raise HTTPException(*failure)
    return tokens


@app.post("/v1/auth/token", response_model=TokenPair, tags=["Authentication"])
def login(body: Login, request: Request):
    tokens, throttled = None, False
    address = str(body.email).lower()
    with transaction() as connection:
        keys = throttle(connection, address, request)
        if not keys:
            throttled = True
            auth_event(connection, "login_rate_limited", False, subject=address)
        else:
            row = connection.execute("SELECT * FROM identity.login_lookup(%s)", (address,)).fetchone()
            valid = verify_password(row["password_hash"] if row else None, body.password)
            valid = valid and not row["disabled"]
            for key in keys:
                connection.execute("SELECT identity.record_attempt(%s,%s)", (key, valid))
            if valid:
                tokens = issue_tokens(connection, row["id"])
            auth_event(connection, "login", bool(tokens), row["id"] if row else None, address)
    if throttled:
        raise HTTPException(429, "Try again later", headers={"Retry-After": "900"})
    if tokens is None:
        raise HTTPException(401, "Invalid email or password")
    return tokens


@app.post("/v1/auth/refresh", response_model=TokenPair, tags=["Authentication"])
def refresh(body: RefreshRequest):
    tokens = None
    with transaction() as connection:
        session = connection.execute("SELECT * FROM identity.refresh_lookup(%s)", (digest(body.refresh_token),)).fetchone()
        if session:
            set_actor(connection, session["user_id"])
            user = connection.execute("SELECT id,disabled,is_service FROM identity.users WHERE id=%s", (session["user_id"],)).fetchone()
            if session["consumed_at"] or session["revoked_at"]:
                # Commit revocation even though the HTTP response will be 401.
                connection.execute("UPDATE identity.sessions SET revoked_at=now() WHERE family_id=%s", (session["family_id"],))
                auth_event(connection, "refresh_replay_family_revoked", False, session["user_id"], session_id=session["id"])
            elif session["expires_at"] > datetime.now(timezone.utc) and user and not user["disabled"] and not user["is_service"]:
                connection.execute("UPDATE identity.sessions SET consumed_at=now() WHERE id=%s", (session["id"],))
                tokens = issue_tokens(connection, session["user_id"], session["family_id"])
                auth_event(connection, "refresh_rotated", True, session["user_id"], session_id=session["id"])
    if tokens is None:
        raise HTTPException(401, "Invalid or expired refresh token")
    return tokens


@app.post("/v1/auth/logout", response_model=StatusView, tags=["Authentication"])
def logout(user=Depends(current_user), connection=Depends(actor_transaction)):
    connection.execute("UPDATE identity.sessions SET revoked_at=now() WHERE family_id=(SELECT family_id FROM identity.sessions WHERE id=%s)", (user["session_id"],))
    auth_event(connection, "logout", True, user["id"], session_id=user["session_id"])
    return StatusView(status="logged_out")


@app.get("/v1/me", response_model=UserView, tags=["Authentication"])
def me(user=Depends(current_user)):
    return view(UserView, user)


@app.get("/v1/organizations", response_model=list[OrganizationView], tags=["Workspaces"])
def organizations(user=Depends(current_user), connection=Depends(actor_transaction)):
    rows = connection.execute("SELECT o.id,o.name,o.currency,m.role FROM mi.organizations o JOIN mi.organization_memberships m ON m.organization_id=o.id WHERE m.user_id=%s ORDER BY o.id", (user["id"],)).fetchall()
    return [view(OrganizationView, row) for row in rows]


@app.post("/v1/organizations/{organization_id}/invitations", response_model=InvitationCreated, status_code=201, tags=["Membership"])
def invite(organization_id: UUID, body: InviteCreate, user=Depends(current_user), connection=Depends(actor_transaction)):
    require_admin(connection, organization_id)
    brand = require_brand(connection, body.brand_id, "brand:read")
    if brand["organization_id"] != organization_id:
        raise HTTPException(404, "Brand workspace not found")
    permitted = {"brand:read", "internal:read", "onboarding:read", "onboarding:write", "crawl:run", "profile:write", "artifact:write", "workflow:run", "assets:write", "research:run"}
    if not set(body.scopes) <= permitted:
        raise HTTPException(422, "Unknown workflow scope")
    if body.role == "client" and body.scopes:
        raise HTTPException(422, "Client permissions are fixed")
    token, invitation_id = secrets.token_urlsafe(48), uuid4()
    expires = datetime.now(timezone.utc) + timedelta(hours=48)
    connection.execute("INSERT INTO identity.invites(id,organization_id,brand_id,email,role,scopes,token_hash,expires_at,invited_by) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (invitation_id, organization_id, body.brand_id, str(body.email).lower(), body.role, body.scopes, digest(token), expires, user["id"]))
    auth_event(connection, "invitation_created", True, user["id"], subject=str(body.email))
    return InvitationCreated(invitation_id=invitation_id, invitation_token=token, expires_at=expires)


@app.post("/v1/auth/accept-invitation", response_model=TokenPair, status_code=201, tags=["Membership"])
def accept_invitation(body: AcceptInvitation):
    user_id = uuid4()
    with transaction(user_id) as connection:
        invitation = connection.execute("SELECT * FROM identity.invite_lookup(%s)", (digest(body.invitation_token),)).fetchone()
        if not invitation or invitation["accepted_at"] or invitation["expires_at"] <= datetime.now(timezone.utc):
            raise HTTPException(400, "Invalid or expired invitation")
        existing = connection.execute("SELECT * FROM identity.login_lookup(%s)", (invitation["email"],)).fetchone()
        if existing:
            raise HTTPException(409, "Sign in to accept this invitation with your existing account")
        hashed = password_hash(body.password)
        connection.execute("INSERT INTO identity.users(id,email,full_name,password_hash) VALUES(%s,%s,%s,%s)", (user_id, invitation["email"], body.full_name, hashed))
        connection.execute("SELECT identity.accept_invite(%s)", (digest(body.invitation_token),))
        tokens = issue_tokens(connection, user_id)
        auth_event(connection, "invitation_accepted", True, user_id)
    return tokens


@app.post("/v1/me/accept-invitation", response_model=AcceptedInvitation, tags=["Membership"])
def accept_existing_invitation(body: AcceptExistingInvitation, user=Depends(current_user), connection=Depends(actor_transaction)):
    row = connection.execute("SELECT identity.accept_invite(%s) AS brand_id", (digest(body.invitation_token),)).fetchone()
    auth_event(connection, "invitation_accepted", True, user["id"])
    return AcceptedInvitation(brand_id=row["brand_id"])


def initial_job_response(connection, brand):
    row = connection.execute("SELECT id,workflow_id FROM mi.jobs WHERE brand_id=%s ORDER BY created_at,id LIMIT 1", (brand["id"],)).fetchone()
    return BrandCreated(brand=view(BrandView, brand), workflow_id=row["workflow_id"], crawl_job_id=row["id"])


@app.post("/v1/organizations/{organization_id}/brands", response_model=BrandCreated, status_code=201, tags=["Workspaces"])
def create_brand(organization_id: UUID, body: BrandCreate, idempotency_key: str = Header(...), user=Depends(current_user), connection=Depends(actor_transaction)):
    require_admin(connection, organization_id)
    validate_idempotency(idempotency_key)
    try:
        validate_url(str(body.website_url or body.online_presence[0].url))
        for link in body.online_presence:
            validate_url(str(link.url))
    except CrawlError:
        raise HTTPException(422, "Use a public HTTP or HTTPS website URL without credentials or nonstandard ports")
    data = body.model_dump(mode="json")
    request_hash = payload_hash(data)
    lock_key(connection, f"brand-create:{organization_id}:{user['id']}:{idempotency_key}")
    existing = connection.execute("SELECT * FROM mi.brand_workspaces WHERE organization_id=%s AND created_by=%s AND creation_key=%s", (organization_id, user["id"], idempotency_key)).fetchone()
    if existing:
        if existing["creation_hash"] != request_hash:
            raise HTTPException(409, "Idempotency key already used with different inputs")
        return initial_job_response(connection, existing)
    brand_id, workflow_id, job_id, input_source_id = uuid4(), uuid4(), uuid4(), uuid4()
    brand = connection.execute("INSERT INTO mi.brand_workspaces(id,organization_id,name,website_url,intake,created_by,creation_key,creation_hash) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
        (brand_id, organization_id, body.name, str(body.website_url) if body.website_url else None, Jsonb(data), user["id"], idempotency_key, request_hash)).fetchone()
    connection.execute("INSERT INTO mi.research_sources(id,organization_id,brand_id,url,publisher,accessed_at,geographic_scope,provenance) VALUES(%s,%s,%s,%s,'Founder supplied intake',now(),'unknown','client_input')", (input_source_id, organization_id, brand_id, str(body.website_url or body.online_presence[0].url)))
    persist_profile(connection, brand, extract_profile(brand_id, 1, data, [], input_source_id), user["id"])
    workflow = connection.execute("INSERT INTO mi.workflow_instances(id,organization_id,brand_id,created_by) VALUES(%s,%s,%s,%s) RETURNING *", (workflow_id, organization_id, brand_id, user["id"])).fetchone()
    transition(connection, workflow, "queue_crawl", GateContext(human=True))
    connection.execute("INSERT INTO mi.jobs(id,organization_id,brand_id,workflow_id,kind,payload,created_by) VALUES(%s,%s,%s,%s,'website_crawl',%s,%s)",
        (job_id, organization_id, brand_id, workflow_id, Jsonb({"url": str(body.website_url or body.online_presence[0].url), "max_pages": 5, "input_source_id": str(input_source_id)}), user["id"]))
    from app.services.research_storage import ensure_template
    from app.services.research_prompt import FIELDS
    for task_type in FIELDS:
        ensure_template(connection, brand, user["id"], task_type)
    return BrandCreated(brand=view(BrandView, brand), workflow_id=workflow_id, crawl_job_id=job_id)


@app.get("/v1/brands", response_model=list[BrandView], tags=["Workspaces"])
def brands(after: UUID | None = None, limit: int = Query(50, ge=1, le=100), connection=Depends(actor_transaction)):
    rows = connection.execute("SELECT * FROM mi.brand_workspaces WHERE (%s::uuid IS NULL OR id>%s) ORDER BY id LIMIT %s", (after, after, limit)).fetchall()
    return [view(BrandView, row) for row in rows]


@app.get("/v1/brands/{brand_id}", response_model=BrandView, tags=["Workspaces"])
def brand_detail(brand_id: UUID, connection=Depends(actor_transaction)):
    return view(BrandView, require_brand(connection, brand_id, "brand:read"))


@app.get("/v1/brands/{brand_id}/profile", response_model=BrandProfile, tags=["Intake"])
def profile(brand_id: UUID, connection=Depends(actor_transaction)):
    require_brand(connection, brand_id, "internal:read")
    return BrandProfile.model_validate(latest_profile(connection, brand_id)["profile"])


@app.get("/v1/brands/{brand_id}/onboarding/questionnaire", response_model=Questionnaire, tags=["Intake"])
def questionnaire(brand_id: UUID, connection=Depends(actor_transaction)):
    require_brand(connection, brand_id, "onboarding:read")
    result = connection.execute("SELECT mi.questionnaire(%s) AS questionnaire", (brand_id,)).fetchone()["questionnaire"]
    if result is None:
        raise HTTPException(404, "Questionnaire not available")
    return Questionnaire.model_validate(result)


def onboarding_view(brand_id, actor_id, row=None):
    if row is None:
        return OnboardingView(id=None, brand_id=brand_id, respondent_id=actor_id, version=0, status="empty", answers=OnboardingInput(), submitted_at=None)
    return view(OnboardingView, row)


def own_onboarding(connection, brand_id, actor_id):
    return connection.execute("SELECT * FROM mi.onboarding_responses WHERE brand_id=%s AND respondent_id=%s ORDER BY version DESC LIMIT 1", (brand_id, actor_id)).fetchone()


@app.get("/v1/brands/{brand_id}/onboarding", response_model=OnboardingView, tags=["Intake"])
def get_onboarding(brand_id: UUID, user=Depends(current_user), connection=Depends(actor_transaction)):
    require_brand(connection, brand_id, "onboarding:read")
    return onboarding_view(brand_id, user["id"], own_onboarding(connection, brand_id, user["id"]))


@app.put("/v1/brands/{brand_id}/onboarding", response_model=OnboardingView, tags=["Intake"])
def save_onboarding(brand_id: UUID, body: OnboardingSave, user=Depends(current_user), connection=Depends(actor_transaction)):
    brand = require_brand(connection, brand_id, "onboarding:write")
    lock_key(connection, f"onboarding:{brand_id}:{user['id']}")
    if not connection.execute("SELECT mi.intake_open(%s) AS allowed", (brand_id,)).fetchone()["allowed"]:
        raise HTTPException(409, "Onboarding is closed; request a new intake cycle")
    previous = own_onboarding(connection, brand_id, user["id"])
    version = previous["version"] if previous else 0
    if version != body.expected_version:
        raise HTTPException(409, "Onboarding changed; reload before saving")
    if body.answers.existing_asset_ids:
        found = connection.execute("SELECT id FROM mi.creative_assets WHERE brand_id=%s AND id=ANY(%s::uuid[])", (brand_id, body.answers.existing_asset_ids)).fetchall()
        if {r["id"] for r in found} != set(body.answers.existing_asset_ids):
            raise HTTPException(422, "Asset reference is unavailable in this workspace")
    row = connection.execute("INSERT INTO mi.onboarding_responses(organization_id,brand_id,respondent_id,version,status,answers) VALUES(%s,%s,%s,%s,'draft',%s) RETURNING *",
        (brand["organization_id"], brand_id, user["id"], version + 1, Jsonb(body.answers.model_dump(mode="json")))).fetchone()
    return onboarding_view(brand_id, user["id"], row)


@app.post("/v1/brands/{brand_id}/onboarding/submit", response_model=OnboardingView, tags=["Intake"])
def submit_onboarding(brand_id: UUID, body: VersionRequest, user=Depends(current_user), connection=Depends(actor_transaction)):
    brand = require_brand(connection, brand_id, "onboarding:write")
    lock_key(connection, f"onboarding:{brand_id}:{user['id']}")
    previous = own_onboarding(connection, brand_id, user["id"])
    if not previous or previous["version"] != body.expected_version or previous["status"] != "draft":
        raise HTTPException(409, "A current saved draft is required")
    row = connection.execute("INSERT INTO mi.onboarding_responses(organization_id,brand_id,respondent_id,version,status,answers,submitted_at) VALUES(%s,%s,%s,%s,'submitted',%s,now()) RETURNING *",
        (brand["organization_id"], brand_id, user["id"], previous["version"] + 1, Jsonb(previous["answers"]))).fetchone()
    connection.execute("SELECT mi.submit_intake(%s)", (brand_id,))
    return onboarding_view(brand_id, user["id"], row)


@app.get("/v1/brands/{brand_id}/onboarding/submissions", response_model=list[OnboardingView], tags=["Intake"])
def submissions(brand_id: UUID, connection=Depends(actor_transaction)):
    require_brand(connection, brand_id, "internal:read")
    rows = connection.execute("SELECT * FROM mi.onboarding_responses WHERE brand_id=%s AND status='submitted' ORDER BY created_at DESC LIMIT 100", (brand_id,)).fetchall()
    return [view(OnboardingView, row) for row in rows]


@app.post("/v1/brands/{brand_id}/profile/confirm", response_model=BrandProfile, tags=["Intake"])
def confirm_profile(brand_id: UUID, body: ProfileConfirm, user=Depends(current_user), connection=Depends(actor_transaction)):
    brand = require_brand(connection, brand_id, "internal:read")
    require_admin(connection, brand["organization_id"])
    lock_key(connection, f"profile:{brand_id}")
    workflow = workflow_for_brand(connection, brand_id, lock=True)
    if workflow["state"] != "awaiting_profile_confirmation":
        raise HTTPException(409, "Onboarding must be submitted before confirmation")
    latest = latest_profile(connection, brand_id)
    if latest["version"] != body.expected_profile_version:
        raise HTTPException(409, "Profile changed; reload before confirmation")
    response = connection.execute("SELECT * FROM mi.onboarding_responses WHERE id=%s AND brand_id=%s AND status='submitted'", (body.onboarding_response_id, brand_id)).fetchone()
    if not response:
        raise HTTPException(404, "Submitted onboarding not found")
    most_recent = own_onboarding(connection, brand_id, response["respondent_id"])
    if most_recent["id"] != response["id"]:
        raise HTTPException(409, "A newer onboarding revision exists")
    input_source = uuid4()
    profile = merge_onboarding(BrandProfile.model_validate(latest["profile"]), response["answers"], input_source)
    if any(g.critical for g in profile.missing_inputs):
        raise HTTPException(409, "Critical inputs are missing; answer the remaining onboarding questions")
    profile = profile.model_copy(update={"version": latest["version"] + 1, "approval_status": "confirmed"})
    connection.execute("INSERT INTO mi.research_sources(id,organization_id,brand_id,url,publisher,accessed_at,geographic_scope,provenance) VALUES(%s,%s,%s,%s,'Client onboarding response',now(),'unknown','client_input')", (input_source, brand["organization_id"], brand_id, brand["website_url"] or brand["intake"]["online_presence"][0]["url"]))
    artifact_id = persist_profile(connection, brand, profile, user["id"])
    approval_id = connection.execute("INSERT INTO mi.approvals(organization_id,brand_id,artifact_id,artifact_version,assigned_to,decision,artifact_sha256,requested_by,workflow_id) VALUES(%s,%s,%s,%s,%s,'pending',%s,%s,%s) RETURNING id",
        (brand["organization_id"], brand_id, artifact_id, profile.version, user["id"], payload_hash(profile.model_dump(mode="json")), user["id"], workflow["id"])).fetchone()["id"]
    connection.execute("UPDATE mi.approvals SET decision='approved',decided_by=%s,decided_at=now(),reason='Founder confirmed selected onboarding and brand profile' WHERE id=%s AND brand_id=%s", (user["id"], approval_id, brand_id))
    transition(connection, workflow, "confirm_profile", GateContext(human=True, confirmed_profile=True), approved_profile_id=artifact_id)
    return profile


@app.post("/v1/brands/{brand_id}/crawls", response_model=JobView, status_code=202, tags=["Crawling"])
def start_crawl(brand_id: UUID, body: CrawlRequest, idempotency_key: str = Header(...), user=Depends(current_user), connection=Depends(actor_transaction)):
    brand = require_brand(connection, brand_id, "crawl:run")
    validate_idempotency(idempotency_key)
    lock_key(connection, f"crawl:{brand_id}:{user['id']}:{idempotency_key}")
    previous = connection.execute("SELECT * FROM mi.idempotency_keys WHERE brand_id=%s AND actor_id=%s AND operation='crawl' AND key=%s", (brand_id, user["id"], idempotency_key)).fetchone()
    request_hash = payload_hash(body.model_dump())
    if previous:
        if previous["request_hash"] != request_hash:
            raise HTTPException(409, "Idempotency key used with different inputs")
        return JobView.model_validate(previous["response"])
    workflow = workflow_for_brand(connection, brand_id, lock=True)
    transition(connection, workflow, "queue_crawl", GateContext(human=brand["actor_role"] in {"founder", "admin", "team"}))
    source = connection.execute("SELECT id FROM mi.research_sources WHERE brand_id=%s AND provenance='client_input' ORDER BY created_at LIMIT 1", (brand_id,)).fetchone()
    row = connection.execute("INSERT INTO mi.jobs(organization_id,brand_id,workflow_id,kind,payload,created_by) VALUES(%s,%s,%s,'website_crawl',%s,%s) RETURNING *", (brand["organization_id"], brand_id, workflow["id"], Jsonb({"url": brand["website_url"] or brand["intake"]["online_presence"][0]["url"], "max_pages": body.max_pages, "input_source_id": str(source["id"])}), user["id"])).fetchone()
    result = view(JobView, row)
    connection.execute("INSERT INTO mi.idempotency_keys(organization_id,brand_id,actor_id,operation,key,request_hash,response) VALUES(%s,%s,%s,'crawl',%s,%s,%s)", (brand["organization_id"], brand_id, user["id"], idempotency_key, request_hash, Jsonb(result.model_dump(mode="json"))))
    return result


@app.get("/v1/brands/{brand_id}/jobs/{job_id}", response_model=JobView, tags=["Crawling"])
def get_job(brand_id: UUID, job_id: UUID, connection=Depends(actor_transaction)):
    require_brand(connection, brand_id, "internal:read")
    row = connection.execute("SELECT * FROM mi.jobs WHERE id=%s AND brand_id=%s", (job_id, brand_id)).fetchone()
    if not row:
        raise HTTPException(404, "Job not found")
    return view(JobView, row)


@app.get("/v1/brands/{brand_id}/workflow", response_model=WorkflowView, tags=["Workflow"])
def get_workflow(brand_id: UUID, connection=Depends(actor_transaction)):
    require_brand(connection, brand_id, "internal:read")
    return view(WorkflowView, workflow_for_brand(connection, brand_id))


@app.post("/v1/brands/{brand_id}/workflow/events", response_model=WorkflowView, tags=["Workflow"])
def workflow_event(brand_id: UUID, body: WorkflowEvent, connection=Depends(actor_transaction)):
    brand = require_brand(connection, brand_id, "internal:read")
    require_admin(connection, brand["organization_id"])
    workflow = workflow_for_brand(connection, brand_id, lock=True)
    if body.event == "retry":
        previous_kind = connection.execute("SELECT kind FROM mi.jobs WHERE brand_id=%s AND workflow_id=%s ORDER BY created_at DESC LIMIT 1", (brand_id, workflow["id"])).fetchone()
        if previous_kind and previous_kind["kind"] != "website_crawl":
            raise HTTPException(409, "Use the research run endpoint to resume research")
        if workflow["state"] not in {"crawl_failed", "needs_human_review"}:
            raise HTTPException(409, "Only a failed Phase 1 crawl can be retried")
        target = "retry" if workflow["state"] == "crawl_failed" else "resume_after_review"
        result = transition(connection, workflow, target, GateContext(human=True, resume_state="crawl_queued"), body.expected_version)
        previous = connection.execute("SELECT * FROM mi.jobs WHERE brand_id=%s AND workflow_id=%s ORDER BY created_at DESC LIMIT 1 FOR UPDATE", (brand_id, workflow["id"])).fetchone()
        if previous["status"] not in {"failed", "cancelled"}:
            raise HTTPException(409, "The previous crawl must be finished before retrying")
        connection.execute("INSERT INTO mi.jobs(organization_id,brand_id,workflow_id,kind,payload,created_by) VALUES(%s,%s,%s,'website_crawl',%s,%s)", (brand["organization_id"], brand_id, workflow["id"], Jsonb(previous["payload"]), brand["created_by"]))
    else:
        result = transition(connection, workflow, "cancel", GateContext(human=True), body.expected_version)
        connection.execute("UPDATE mi.jobs SET status='cancelled',lease_id=NULL,lease_expires_at=NULL WHERE brand_id=%s AND status IN('queued','running','retry')", (brand_id,))
    return view(WorkflowView, result)


@app.get("/v1/brands/{brand_id}/audit", response_model=list[AuditView], tags=["Audit"])
def audit(brand_id: UUID, limit: int = Query(50, ge=1, le=100), connection=Depends(actor_transaction)):
    brand = require_brand(connection, brand_id, "brand:read")
    require_admin(connection, brand["organization_id"])
    rows = connection.execute("SELECT * FROM mi.audit_logs WHERE brand_id=%s ORDER BY occurred_at DESC LIMIT %s", (brand_id, limit)).fetchall()
    return [view(AuditView, row) for row in rows]


from app.research_api import router as research_router
app.include_router(research_router)

from app.planning_api import router as planning_router
app.include_router(planning_router)

from app.brain_api import router as brain_router
app.include_router(brain_router)

# Optional same-origin frontend, using existing API sessions; never serves source/config directories.
if settings().ui_directory:
    from fastapi.staticfiles import StaticFiles
    app.mount('/app', StaticFiles(directory=settings().ui_directory, html=True), name='noetis_ui')
