"""Atomic admission and conservative settlement. Unknown charges retain reservations."""
import math
from datetime import date
from uuid import uuid4
from psycopg.types.json import Jsonb
from app.config import settings
from app.repository import payload_hash


class BudgetError(RuntimeError):
    pass


def cost_paise(input_tokens, output_tokens, input_rate, output_rate):
    if input_rate is None or output_rate is None:
        return None
    return (input_tokens * input_rate + output_tokens * output_rate + 999999) // 1_000_000


def budget_context(connection):
    connection.execute("SELECT set_config('app.workflow_event','research_budget',true)")


def reserve(connection, job, workflow, profile_row, request, input_count, provider, model, actor):
    config = settings()
    if input_count < 1 or input_count > request.maximum_input_tokens:
        raise BudgetError('research_context_exceeds_limit')
    padding = max(256, math.ceil(input_count * .1))
    reserved_tokens = input_count + padding + request.maximum_output_tokens
    if config.research_pilot_brand_id == job['brand_id']:
        # Dollar admission is independent of optional INR reporting rates. The
        # workflow lock held by the caller serializes this with dispatch writes.
        from app.research_pilot import admit_pilot
        admit_pilot(connection, job, request, input_count + padding, model)
    rates = (config.model_input_paise_per_million, config.model_output_paise_per_million)
    price_date = date.fromisoformat(config.pricing_as_of) if config.pricing_as_of else None
    priced = all(rate is not None for rate in rates) and price_date is not None
    cost_cap = min([c for c in [config.model_max_cost_paise,config.research_max_cost_paise] if c is not None],default=None)
    if cost_cap and not priced:
        raise BudgetError('pricing_required_for_cost_cap')
    estimated_cost = cost_paise(input_count + padding, request.maximum_output_tokens, *rates) if priced else None
    if cost_cap and workflow['cost_used_paise'] + workflow['cost_reserved_paise'] + estimated_cost > cost_cap:
        raise BudgetError('research_cost_cap_reached')
    budget_context(connection)
    updated = connection.execute("""UPDATE mi.workflow_instances SET agent_calls=agent_calls+1,tokens_reserved=tokens_reserved+%s,
        cost_reserved_paise=cost_reserved_paise+%s,pricing_complete=pricing_complete AND %s
        WHERE id=%s AND brand_id=%s AND NOT budget_breached AND agent_calls<max_agent_calls
        AND tokens_used+tokens_reserved+%s<=token_budget RETURNING id""",
        (reserved_tokens, estimated_cost or 0, priced, workflow['id'], job['brand_id'], reserved_tokens)).fetchone()
    if not updated:
        raise BudgetError('workflow_model_budget_exhausted')
    dispatch_id = uuid4()
    data = request.model_dump(mode='json')
    row = connection.execute("""INSERT INTO mi.model_dispatches(id,organization_id,brand_id,workflow_id,job_id,lease_id,
        template_id,profile_artifact_id,state,provider,model,request,request_sha256,output_schema,input_token_estimate,
        maximum_output_tokens,reserved_tokens,reserved_cost_paise,pricing_as_of,input_rate_paise_per_million,output_rate_paise_per_million,created_by)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'reserved',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
        (dispatch_id, job['organization_id'], job['brand_id'], workflow['id'], job['id'], job['lease_id'],
         job['payload']['template_id'], profile_row['artifact_id'], provider, model, Jsonb(data), payload_hash(data),
         request.output_schema_name, input_count, request.maximum_output_tokens, reserved_tokens, estimated_cost or 0,
         price_date, rates[0] if priced else None, rates[1] if priced else None, actor)).fetchone()
    return row


def settle(connection, dispatch, result=None, *, uncertain=False, error=None, latency_ms=0):
    current = connection.execute('SELECT * FROM mi.model_dispatches WHERE id=%s AND brand_id=%s FOR UPDATE',
        (dispatch['id'], dispatch['brand_id'])).fetchone()
    if current['state'] in {'settled', 'rejected'} or (current['state']=='outcome_unknown' and result is None):
        return current
    if uncertain:
        return connection.execute("UPDATE mi.model_dispatches SET state='outcome_unknown',error_code=%s,latency_ms=%s,finished_at=now() WHERE id=%s AND brand_id=%s RETURNING *",
            (error, latency_ms, current['id'], current['brand_id'])).fetchone()
    used = result.input_tokens + result.output_tokens if result else 0
    cost = cost_paise(result.input_tokens, result.output_tokens, current['input_rate_paise_per_million'], current['output_rate_paise_per_million']) if result else None
    budget_context(connection)
    breach = result is not None and (used > current['reserved_tokens'] or result.output_tokens > current['maximum_output_tokens'])
    connection.execute("""UPDATE mi.workflow_instances SET tokens_reserved=tokens_reserved-%s,tokens_used=tokens_used+%s,
        cost_reserved_paise=cost_reserved_paise-%s,cost_used_paise=cost_used_paise+%s,
        budget_breached=budget_breached OR %s OR tokens_used+%s>token_budget
        WHERE id=%s AND brand_id=%s""",
        (current['reserved_tokens'], used, current['reserved_cost_paise'], cost or 0, breach, used, current['workflow_id'], current['brand_id']))
    return connection.execute("""UPDATE mi.model_dispatches SET state=%s,input_tokens=%s,output_tokens=%s,cost_paise=%s,
        latency_ms=%s,error_code=%s,finished_at=now() WHERE id=%s AND brand_id=%s RETURNING *""",
        ('settled' if result else 'rejected', result.input_tokens if result else None, result.output_tokens if result else None,
         cost, latency_ms, error, current['id'], current['brand_id'])).fetchone()
