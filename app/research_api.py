import json
from uuid import UUID
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from psycopg.types.json import Jsonb
from app.auth import actor_transaction, current_user, require_brand, require_admin
from app.models import BrandProfile
from app.research_models import (ResearchStart, ResearchAccepted, ResearchRunView, DossierView,
    ResearchDossierV11, ResearchReview, ResearchReviewRecord, ResearchValidationReport, PromptCompile, CompiledPromptView, EvidenceView)
from app.repository import lock_key, payload_hash, latest_profile, workflow_for_brand, transition
from app.services.anthropic_provider import get_model_provider, ProviderError
from app.services.crawl import validate_url, CrawlError
from app.services.research_storage import ensure_template, evidence_rows
from app.services.research_prompt import compile_prompt
from app.services.research_validation import validate_review, evaluate_output, ResearchValidationError
from app.workflow import GateContext
from app.api_types import StatusView
from app.services.research_prompt import FIELDS

from app.models import BrandProfileV11 as BrandProfile

router = APIRouter(prefix="/v1/brands/{brand_id}/research", tags=["Research"])


def gates(workflow, **kwargs):
    return GateContext(calls=workflow["agent_calls"], max_calls=workflow["max_agent_calls"],
        tokens=workflow["tokens_used"] + workflow["tokens_reserved"], token_budget=workflow["token_budget"], **kwargs)


def validation_for(connection, brand_id, *, job_id=None, artifact_id=None):
    clause, value = ('d.job_id=%s', job_id) if job_id else ('o.artifact_id=%s', artifact_id)
    row=connection.execute("""SELECT o.body->'validation' AS report FROM mi.model_dispatches d
        JOIN mi.agent_runs r ON r.prompt_run_id=d.id AND r.brand_id=d.brand_id
        JOIN mi.agent_outputs o ON o.agent_run_id=r.id AND o.brand_id=r.brand_id
        WHERE d.brand_id=%s AND """+clause+" ORDER BY d.created_at DESC,d.id DESC LIMIT 1", (brand_id,value)).fetchone()
    return ResearchValidationReport.model_validate(row['report']) if row and row['report'] else None


@router.post("/runs", response_model=ResearchAccepted, status_code=202)
def start(brand_id: UUID, body: ResearchStart, idempotency_key: str = Header(...),
          user=Depends(current_user), connection=Depends(actor_transaction)):
    brand = require_brand(connection, brand_id, "research:run")
    if not 8 <= len(idempotency_key) <= 128 or not idempotency_key.isascii() or not idempotency_key.isprintable():
        raise HTTPException(422, "Use an 8 to 128 character Idempotency-Key")
    for source in body.sources:
        try:
            validate_url(str(source.url))
        except CrawlError:
            raise HTTPException(422, "A source URL is invalid")
    lock_key(connection, f"research:{brand_id}:{user['id']}:{idempotency_key}")
    serialized = body.model_dump(mode="json", exclude={"brief"} if body.brief is None else set())
    hashed = payload_hash(serialized)
    old = connection.execute("SELECT * FROM mi.idempotency_keys WHERE brand_id=%s AND actor_id=%s AND operation='research' AND key=%s",
        (brand_id, user["id"], idempotency_key)).fetchone()
    if old:
        if old["request_hash"] != hashed:
            raise HTTPException(409, "Idempotency key used with different inputs")
        return ResearchAccepted.model_validate(old["response"])
    try:
        get_model_provider()
    except ProviderError as exc:
        raise HTTPException(503, exc.code)
    workflow = workflow_for_brand(connection, brand_id, lock=True)
    if workflow["version"] != body.expected_workflow_version:
        raise HTTPException(409, "Workflow changed; reload before starting research")
    profile = latest_profile(connection, brand_id)
    confirmed = BrandProfile.model_validate(profile["profile"])
    if profile["artifact_id"] != workflow["approved_profile_id"] or confirmed.approval_status != "confirmed" or any(g.critical for g in confirmed.missing_inputs):
        raise HTTPException(409, "Current confirmed intake without critical gaps is required")
    if workflow["state"] == "ready_for_research":
        event = "queue_research"
    elif workflow["state"] in {"failed", "needs_human_review", "awaiting_inputs"} and workflow["resume_state"] == "research_queued":
        require_admin(connection, brand["organization_id"])
        unknown = connection.execute("SELECT id FROM mi.model_dispatches WHERE brand_id=%s AND workflow_id=%s AND state='outcome_unknown' LIMIT 1", (brand_id, workflow["id"])).fetchone()
        if unknown and not body.acknowledge_uncertain_charge:
            raise HTTPException(409, "A prior charge is uncertain; explicit acknowledgement is required before another run")
        event = "inputs_provided" if workflow["state"] == "awaiting_inputs" else "resume_after_review"
    else:
        raise HTTPException(409, "Research cannot start in this workflow state")
    template = ensure_template(connection, brand, user["id"], body.task_type)
    updated = transition(connection, workflow, event, gates(workflow, human=True, confirmed_profile=True, resume_state="research_queued"),
                         body.expected_workflow_version, last_error=None)
    payload = serialized
    payload.update(profile_artifact_id=str(profile["artifact_id"]), template_id=str(template["id"]))
    job = connection.execute("INSERT INTO mi.jobs(organization_id,brand_id,workflow_id,kind,payload,created_by) VALUES(%s,%s,%s,'research',%s,%s) RETURNING id",
        (brand["organization_id"], brand_id, workflow["id"], Jsonb(payload), user["id"])).fetchone()
    response = ResearchAccepted(brand_id=brand_id, workflow_id=workflow["id"], job_id=job["id"], state=updated["state"])
    connection.execute("INSERT INTO mi.idempotency_keys(organization_id,brand_id,actor_id,operation,key,request_hash,response) VALUES(%s,%s,%s,'research',%s,%s,%s)",
        (brand["organization_id"], brand_id, user["id"], idempotency_key, hashed, Jsonb(response.model_dump(mode="json"))))
    return response


@router.get("/runs/{job_id}", response_model=ResearchRunView)
def run_status(brand_id: UUID, job_id: UUID, connection=Depends(actor_transaction)):
    require_brand(connection, brand_id, "internal:read")
    job = connection.execute("SELECT * FROM mi.jobs WHERE id=%s AND brand_id=%s AND kind='research'", (job_id, brand_id)).fetchone()
    if not job:
        raise HTTPException(404, "Research run not found")
    workflow = workflow_for_brand(connection, brand_id)
    count = connection.execute("SELECT count(*) AS n FROM mi.research_evidence WHERE job_id=%s AND brand_id=%s", (job_id, brand_id)).fetchone()["n"]
    dossier = connection.execute("SELECT id FROM mi.research_dossiers WHERE job_id=%s AND brand_id=%s ORDER BY created_at DESC LIMIT 1", (job_id, brand_id)).fetchone()
    uncertain = connection.execute("SELECT count(*) AS n FROM mi.model_dispatches WHERE brand_id=%s AND workflow_id=%s AND state IN('outcome_unknown','dispatched')", (brand_id,workflow["id"])).fetchone()["n"]
    return ResearchRunView(job_id=job_id, brand_id=brand_id, status=job["status"], attempts=job["attempts"],
        last_error=job["last_error"], source_count=count, model_calls=workflow["agent_calls"],
        tokens_used=workflow["tokens_used"], tokens_reserved=workflow["tokens_reserved"],
        cost_used_paise=workflow["cost_used_paise"], cost_reserved_paise=workflow["cost_reserved_paise"], uncertain_dispatches=uncertain, pricing_complete=workflow["pricing_complete"],
        dossier_id=dossier["id"] if dossier else None, source_limitations=job["source_limitations"],
        latest_validation=validation_for(connection,brand_id,job_id=job_id))


def dossier_view(connection, brand_id, dossier_id):
    row = connection.execute("""SELECT d.*,a.version,a.content_sha256 FROM mi.research_dossiers d
        JOIN mi.artifacts a ON a.id=d.artifact_id AND a.brand_id=d.brand_id WHERE d.id=%s AND d.brand_id=%s""",
        (dossier_id, brand_id)).fetchone()
    if not row:
        raise HTTPException(404, "Research dossier not found")
    review = connection.execute("SELECT * FROM mi.research_reviews WHERE dossier_id=%s AND brand_id=%s ORDER BY created_at DESC LIMIT 1",
        (dossier_id, brand_id)).fetchone()
    return row, DossierView(id=row["id"], artifact_id=row["artifact_id"], artifact_version=row["version"],
        artifact_sha256=row["content_sha256"], profile_artifact_id=row["profile_artifact_id"],
        content=ResearchDossierV11.model_validate(row["body"]), review_status=review["decision"] if review else "pending",
        review_record=ResearchReviewRecord(id=review["id"],reviewer_id=review["reviewer_id"],reviewed_at=review["created_at"],
            decision=review["decision"],reason=review["reason"],gap_resolutions=review["resolutions"]["gap_resolutions"],
            conflict_resolutions=review["resolutions"]["conflict_resolutions"],
            semantic_review=review["resolutions"].get("semantic_review")) if review else None,
        semantic_review_status=(review['decision'] if review['decision']=='revision_requested' or review['resolutions'].get('semantic_review') else 'historical_unassessed') if review else 'pending',
        validation_report=ResearchValidationReport.model_validate(review['resolutions']['validation_report']) if review and review['resolutions'].get('validation_report') else validation_for(connection,brand_id,artifact_id=row['artifact_id']))


@router.get("/dossiers", response_model=list[DossierView])
def list_dossiers(brand_id: UUID, limit: int = Query(20, ge=1, le=50), connection=Depends(actor_transaction)):
    require_brand(connection, brand_id, "internal:read")
    rows = connection.execute("SELECT id FROM mi.research_dossiers WHERE brand_id=%s ORDER BY created_at DESC LIMIT %s", (brand_id, limit)).fetchall()
    return [dossier_view(connection, brand_id, row["id"])[1] for row in rows]


@router.get("/dossiers/{dossier_id}", response_model=DossierView)
def get_dossier(brand_id: UUID, dossier_id: UUID, connection=Depends(actor_transaction)):
    require_brand(connection, brand_id, "internal:read")
    return dossier_view(connection, brand_id, dossier_id)[1]


@router.post("/dossiers/{dossier_id}/review", response_model=DossierView)
def review_dossier(brand_id: UUID, dossier_id: UUID, body: ResearchReview, user=Depends(current_user), connection=Depends(actor_transaction)):
    brand = require_brand(connection, brand_id, "internal:read")
    require_admin(connection, brand["organization_id"])
    workflow = workflow_for_brand(connection, brand_id, lock=True)
    row, view = dossier_view(connection, brand_id, dossier_id)
    newest = connection.execute("SELECT id FROM mi.research_dossiers WHERE brand_id=%s ORDER BY created_at DESC LIMIT 1", (brand_id,)).fetchone()
    if workflow["state"] != "awaiting_research_confirmation" or workflow["version"] != body.expected_workflow_version:
        raise HTTPException(409, "Research review is stale or unavailable")
    if newest["id"] != dossier_id or workflow["approved_profile_id"] != row["profile_artifact_id"] or body.artifact_sha256 != row["content_sha256"]:
        raise HTTPException(409, "Review must target the current dossier and profile")
    profile=latest_profile(connection,brand_id)
    if profile['artifact_id']!=row['profile_artifact_id'] or profile['status']!='confirmed':
        raise HTTPException(409, "Review must target the latest confirmed profile")
    resolutions=body.model_dump(mode='json')
    if body.decision=='approved':
        if not row['job_id']:
            raise HTTPException(409, {'code':'review_evidence_unavailable','message':'Captured evidence is required; request a new research run'})
        evidence=evidence_rows(connection,brand_id,job_id=row['job_id'])
        dispatch=connection.execute("SELECT request FROM mi.model_dispatches WHERE brand_id=%s AND job_id=%s ORDER BY created_at DESC LIMIT 1",(brand_id,row['job_id'])).fetchone()
        if not evidence or not dispatch:
            raise HTTPException(409, {'code':'review_evidence_unavailable','message':'Captured evidence is required; request a new research run'})
        validated,report=evaluate_output(json.dumps(row['body']),brand_id,profile['version'],evidence,json.loads(dispatch['request']['context_json'])['research_as_of'])
        if not validated:
            raise HTTPException(409, {'code':'review_provenance_failed','issue_codes':report.issue_codes})
        resolutions['validation_report']=report.model_dump(mode='json')
    try:
        validate_review(view.content, body)
    except ResearchValidationError as exc:
        raise HTTPException(409, {'code':exc.code,'message':str(exc)})
    connection.execute("""INSERT INTO mi.research_reviews(organization_id,brand_id,dossier_id,artifact_id,artifact_sha256,
        reviewer_id,decision,reason,resolutions) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (brand["organization_id"], brand_id, dossier_id, row["artifact_id"], row["content_sha256"], user["id"], body.decision, body.reason, Jsonb(resolutions)))
    if body.decision == "approved":
        transition(connection, workflow, "confirm_research", gates(workflow, human=True, confirmed_profile=True, validated_dossier=True),
                   body.expected_workflow_version, approved_research_id=row["artifact_id"])
    else:
        transition(connection, workflow, "request_research_revision", gates(workflow, human=True),
                   body.expected_workflow_version, last_error=None)
    return dossier_view(connection, brand_id, dossier_id)[1]


@router.post("/prompts/compile", response_model=CompiledPromptView)
def compile_preview(brand_id: UUID, body: PromptCompile, connection=Depends(actor_transaction)):
    require_brand(connection, brand_id, "internal:read")
    rows = evidence_rows(connection, brand_id, ids=body.evidence_ids)
    if {row["id"] for row in rows} != set(body.evidence_ids):
        raise HTTPException(404, "Evidence snapshot not found")
    profile = latest_profile(connection, brand_id)
    request = compile_prompt(profile, rows, body.areas, body.task_type)
    return CompiledPromptView(template_version=request.template_version, system=request.system, context_json=request.context_json,
        output_schema_json=request.output_schema_json, prompt_sha256=payload_hash(request.model_dump(mode="json")),
        references=request.context, estimated_input_tokens=(len(request.system) + len(request.context_json) + len(request.output_schema_json) + 3)//4)


@router.get("/runs/{job_id}/sources",response_model=list[EvidenceView])
def list_sources(brand_id: UUID,job_id: UUID,connection=Depends(actor_transaction)):
    require_brand(connection,brand_id,"internal:read")
    job=connection.execute("SELECT id FROM mi.jobs WHERE id=%s AND brand_id=%s AND kind='research'",(job_id,brand_id)).fetchone()
    if not job:
        raise HTTPException(404,"Research run not found")
    return [EvidenceView(id=row["id"],source=row["citation"],area=row["area"],kind=row["kind"],
        excerpt=row["extracted_text"][:2400],captured_snapshot_sha256=row["extracted_sha256"],confidence_cap=row["confidence_cap"])
        for row in evidence_rows(connection,brand_id,job_id=job_id)]


@router.post("/templates/initialize",response_model=StatusView)
def initialize_templates(brand_id: UUID,user=Depends(current_user),connection=Depends(actor_transaction)):
    brand=require_brand(connection,brand_id,"internal:read")
    require_admin(connection,brand["organization_id"])
    lock_key(connection,"research-templates:"+str(brand_id))
    for task_type in FIELDS:
        ensure_template(connection,brand,user["id"],task_type)
    return StatusView(status="research_templates_ready")
