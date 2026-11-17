import hashlib
import json
from uuid import UUID, uuid4

from fastapi import HTTPException
from psycopg.types.json import Jsonb

from app.models import BrandProfile
from app.workflow import GateContext, RESUME, next_state


from app.models import BrandProfileV11 as BrandProfile

def lock_key(connection, key: str):
    connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (key,))


def payload_hash(data: dict) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def persist_profile(connection, brand, profile: BrandProfile, actor_id: UUID) -> UUID:
    # All writes validate the canonical model, not only its JSON representation.
    profile = BrandProfile.model_validate(profile.model_dump())
    artifact_id = uuid4()
    content_hash = payload_hash(profile.model_dump(mode="json"))
    connection.execute("INSERT INTO mi.artifacts(id,organization_id,brand_id,kind,version,logical_key,created_by,content_sha256,schema_version) VALUES(%s,%s,%s,'brand_profile',%s,%s,%s,%s,%s)",
        (artifact_id, brand["organization_id"], brand["id"], profile.version, brand["id"], actor_id, content_hash, profile.schema_version))
    connection.execute("INSERT INTO mi.brand_profiles(organization_id,brand_id,artifact_id,version,profile,status,created_by) VALUES(%s,%s,%s,%s,%s,%s,%s)",
        (brand["organization_id"], brand["id"], artifact_id, profile.version, Jsonb(profile.model_dump(mode="json")), profile.approval_status, actor_id))
    return artifact_id


def latest_profile(connection, brand_id):
    return connection.execute("SELECT * FROM mi.brand_profiles WHERE brand_id=%s ORDER BY version DESC LIMIT 1", (brand_id,)).fetchone()


def workflow_for_brand(connection, brand_id, lock=False):
    return connection.execute("SELECT * FROM mi.workflow_instances WHERE brand_id=%s ORDER BY created_at DESC LIMIT 1" + (" FOR UPDATE" if lock else ""), (brand_id,)).fetchone()


def transition(connection, workflow, event: str, gate: GateContext, expected_version: int | None = None, **updates):
    target = next_state(workflow["state"], event, gate)
    expected = expected_version if expected_version is not None else workflow["version"]
    connection.execute("SELECT set_config('app.workflow_event',%s,true)", (event,))
    allowed = {"approved_profile_id", "approved_strategy_id", "approved_research_id", "selected_strategy_approval_id", "approved_plan_id", "approved_creative_id", "last_error", "validation_failures", "resume_state"}
    if set(updates) - allowed:
        raise ValueError("unsupported workflow update")
    if event in {"validation_failed", "run_failed", "request_inputs", "provider_outcome_unknown", "budget_exhausted"}:
        updates["resume_state"] = RESUME.get(workflow["state"])
    if event == "validation_failed":
        updates["validation_failures"] = min(2, workflow["validation_failures"] + 1)
    if event in {"crawl_succeeded", "research_succeeded", "resume_after_review", "inputs_provided", "strategy_succeeded", "critique_succeeded", "plan_succeeded", "creative_succeeded", "request_strategy_revision", "request_plan_revision", "request_creative_revision"}:
        updates["validation_failures"] = 0
    columns = ["state=%s", "version=version+1"] + [key + "=%s" for key in updates]
    values = [target, *updates.values(), workflow["id"], workflow["brand_id"], expected]
    updated = connection.execute("UPDATE mi.workflow_instances SET " + ",".join(columns) + " WHERE id=%s AND brand_id=%s AND version=%s RETURNING *", values).fetchone()
    if not updated:
        raise HTTPException(409, "Workflow changed; reload before retrying")
    return updated
