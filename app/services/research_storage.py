import hashlib
from uuid import UUID, uuid4
from psycopg.types.json import Jsonb
from app.models import Citation
from app.repository import payload_hash
from app.services.research_prompt import POLICY, FIELDS, TEMPLATE_VERSION
from app.config import settings


def evidence_rows(connection, brand_id, *, job_id=None, ids=None):
    clause, value = ("e.job_id=%s", job_id) if job_id else ("e.id=ANY(%s::uuid[])", ids)
    rows = connection.execute("""SELECT e.*,s.url,s.publisher,s.published_at,s.accessed_at,s.geographic_scope,
        s.applicability_to_india,s.access_limitations FROM mi.research_evidence e JOIN mi.research_sources s
        ON s.id=e.source_id AND s.brand_id=e.brand_id WHERE e.brand_id=%s AND """ + clause + " ORDER BY e.created_at,e.id", (brand_id, value)).fetchall()
    for row in rows:
        row["citation"] = Citation(source_id=row["source_id"], url=row["url"], publisher=row["publisher"],
            published_at=row["published_at"], accessed_at=row["accessed_at"], geographic_scope=row["geographic_scope"],
            applicability_to_india=row["applicability_to_india"], access_limitations=row["access_limitations"]).model_dump(mode="json")
    return rows


def persist_evidence(connection, job, source):
    source_id = uuid4()
    connection.execute("""INSERT INTO mi.research_sources(id,organization_id,brand_id,url,publisher,published_at,accessed_at,
        geographic_scope,applicability_to_india,access_limitations,content_sha256,provenance)
        VALUES(%s,%s,%s,%s,%s,%s,now(),%s,%s,%s,%s,%s)""",
        (source_id, job["organization_id"], job["brand_id"], source.url, source.publisher, source.published_at,
         source.seed.geographic_scope, source.seed.applicability_to_india, Jsonb(source.limitations), source.content_sha256, source.seed.kind))
    connection.execute("""INSERT INTO mi.research_evidence(organization_id,brand_id,job_id,source_id,area,kind,
        extracted_text,extracted_sha256,confidence_cap) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (job["organization_id"], job["brand_id"], job["id"], source_id, source.seed.area, source.seed.kind,
         source.text, hashlib.sha256(source.text.encode()).hexdigest(), source.confidence_cap))


def ensure_template(connection, brand, actor, task_type):
    row = connection.execute("SELECT * FROM mi.prompt_templates WHERE brand_id=%s AND task_type=%s AND version=%s",
                            (brand["id"], task_type, TEMPLATE_VERSION)).fetchone()
    if row:
        return row
    config = settings()
    return connection.execute("""INSERT INTO mi.prompt_templates(organization_id,brand_id,task_type,version,context_fields,
        template,output_schema,max_input_tokens,max_output_tokens,created_by)
        VALUES(%s,%s,%s,%s,%s,%s,'research-dossier-v1.1',%s,%s,%s) RETURNING *""",
        (brand["organization_id"], brand["id"], task_type, TEMPLATE_VERSION, Jsonb(FIELDS[task_type]), POLICY,
         config.research_max_input_tokens, config.research_max_output_tokens, actor)).fetchone()


def persist_dossier(connection, job, profile_row, dossier, actor):
    version = connection.execute("SELECT coalesce(max(version),0)+1 AS n FROM mi.artifacts WHERE brand_id=%s AND kind='research_dossier'",
                                 (job["brand_id"],)).fetchone()["n"]
    artifact_id, dossier_id = uuid4(), uuid4()
    body = dossier.model_dump(mode="json")
    connection.execute("""INSERT INTO mi.artifacts(id,organization_id,brand_id,kind,version,logical_key,schema_version,
        created_by,content_sha256) VALUES(%s,%s,%s,'research_dossier',%s,%s,'1.1',%s,%s)""",
        (artifact_id, job["organization_id"], job["brand_id"], version, job["brand_id"], actor, payload_hash(body)))
    connection.execute("INSERT INTO mi.research_dossiers(id,organization_id,brand_id,artifact_id,profile_artifact_id,body,job_id) VALUES(%s,%s,%s,%s,%s,%s,%s)",
        (dossier_id, job["organization_id"], job["brand_id"], artifact_id, profile_row["artifact_id"], Jsonb(body), job["id"]))
    for competitor in dossier.competitors:
        connection.execute("INSERT INTO mi.competitor_profiles(organization_id,brand_id,dossier_id,body) VALUES(%s,%s,%s,%s)",
            (job["organization_id"], job["brand_id"], dossier_id, Jsonb(competitor.model_dump(mode="json"))))
    for audience in dossier.audiences:
        connection.execute("INSERT INTO mi.audience_segments(organization_id,brand_id,dossier_id,body) VALUES(%s,%s,%s,%s)",
            (job["organization_id"], job["brand_id"], dossier_id, Jsonb(audience.model_dump(mode="json"))))
    return artifact_id, dossier_id
