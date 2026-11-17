"""Projection/review adapter. No second crawler, LLM, queue, or invented findings."""
from copy import deepcopy
from datetime import datetime, timezone
from uuid import UUID, uuid4
from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from psycopg.types.json import Jsonb
from app.brain_models import BrainDocument, CHAPTERS, Finding
from app.repository import latest_profile, payload_hash


def finding(field,text,provenance='unverified',confidence='unknown',sources=None,critical=False,unknown=False):
    return Finding(id=uuid4(),field=field,original_text=text,text=text,provenance=provenance,
        confidence=confidence,source_ids=sources or [],critical=critical,unknown=unknown).model_dump(mode='json')


def synthesise(connection,brand,session,actor):
    profile_row=latest_profile(connection,brand['id'])
    profile=profile_row['profile'] if profile_row else {}
    draft=session['draft'];out={key:[] for key in CHAPTERS};sources={}
    for source in profile.get('sources',[]):sources[source['source_id']]=source
    def observed(chapter,key,label,critical=False):
        value=profile.get(key)
        if value:
            claim=value.get('evidence',{});out[chapter].append(finding(key,value['value'],
                'founder_provided' if any(sources.get(str(sid),{}).get('publisher') in ['Founder supplied intake','Client onboarding response'] for sid in claim.get('source_ids',[])) else 'publicly_observed',
                'medium',claim.get('source_ids',[]),critical))
    observed('business','business_description','Business description',True)
    observed('business','business_model','Business model')
    for p in profile.get('products_services',[]):
        value=p['name'];out['business'].append(finding('product',value['value'],'publicly_observed','medium',value['evidence']['source_ids']))
    observed('audience','audience_description','Audience',True)
    observed('positioning','positioning','Positioning',True)
    out['business'].append(finding('category',draft['category']+(' / '+draft['niche'] if draft.get('niche') else ''),'founder_provided','unknown'))
    for objective in draft['objectives']:
        text=draft.get('other_objective') or 'Other objective — details not supplied' if objective=='Other' else objective
        out['objectives'].append(finding('objective',text,'founder_provided','unknown'))
    for link in draft['online_presence']:
        out['presence'].append(finding('presence_url',str(link['url']),'founder_provided','unknown'))
    # Import only successfully validated worker output into an UNCONFIRMED review draft. Canonical research approval remains separate.
    dossier=connection.execute("""SELECT d.* FROM mi.research_dossiers d JOIN mi.jobs j ON j.id=d.job_id AND j.brand_id=d.brand_id
        WHERE d.brand_id=%s AND j.status='succeeded' AND d.profile_artifact_id=%s ORDER BY d.created_at DESC LIMIT 1""",(brand['id'],profile_row['artifact_id'] if profile_row else None)).fetchone()
    if dossier:
        body=dossier['body']
        for source in body['sources']:sources[source['source_id']]=source
        for chapter,claims in [('business',body['brand_findings']),('market',body['category_market']),('presence',body['social_listening'])]:
            for claim in claims[:12]:
                confidence='high' if claim['confidence']>=.8 else 'medium' if claim['confidence']>=.5 else 'low' if claim['confidence']>0 else 'unknown'
                out[chapter].append(finding('research',claim['statement'],'publicly_observed' if claim['classification']=='fact' else 'ai_inferred' if claim['classification']=='inference' else 'unverified',confidence,claim['source_ids']))
        for segment in body['audiences'][:5]:
            for claim in segment['evidence'][:4]:out['audience'].append(finding('audience_evidence',claim['statement'],'ai_inferred','high' if claim['confidence']>=.8 else 'medium' if claim['confidence']>=.5 else 'low' if claim['confidence']>0 else 'unknown',claim['source_ids']))
    for key in CHAPTERS:
        if not out[key]:out[key].append(finding('missing_'+key,'Not established by captured evidence. Provide verified information or mark this section unknown.',critical=key in {'business','audience','positioning'},unknown=True))
    document=BrainDocument(brand_id=brand['id'],brand_name=draft['brand_name'],chapters=[{'key':key,'title':title,'summary':out[key][0]['text'],'findings':out[key]} for key,title in CHAPTERS.items()],
        sources=list(sources.values()),warnings=['Website and founder statements are not independent verification.','Unresearched market/audience/positioning remains explicitly unknown.','Strategy readiness is governed separately by existing confirmed-intake and approved-research gates.'])
    version=connection.execute('SELECT coalesce(max(version),0)+1 AS n FROM mi.brand_brain_versions WHERE brand_id=%s',(brand['id'],)).fetchone()['n']
    body=document.model_dump(mode='json')
    return connection.execute('''INSERT INTO mi.brand_brain_versions(organization_id,brand_id,version,session_id,profile_artifact_id,dossier_id,body,content_sha256,created_by)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *''',(brand['organization_id'],brand['id'],version,session['id'],profile_row['artifact_id'] if profile_row else None,dossier['id'] if dossier else None,Jsonb(body),payload_hash(body),actor)).fetchone()


def version_row(connection,brand_id,version_id=None):
    row=connection.execute('SELECT * FROM mi.brand_brain_versions WHERE brand_id=%s'+(' AND id=%s' if version_id else '')+' ORDER BY version DESC LIMIT 1',(brand_id,version_id) if version_id else (brand_id,)).fetchone()
    if not row:raise HTTPException(404,'Brand Brain version not found')
    return row


def project(connection,row):
    document=deepcopy(row['body']);reviews=connection.execute('SELECT * FROM mi.brand_brain_reviews WHERE brand_id=%s AND version_id=%s ORDER BY created_at,id',(row['brand_id'],row['id'])).fetchall()
    by_id={str(f['id']):f for ch in document['chapters'] for f in ch['findings']}
    for f in by_id.values():f.update(review_sequence=0,review_status='not_started',history=[])
    for review in reviews:
        f=by_id.get(str(review['finding_id']))
        if not f:continue
        action=review['action'];f['history'].append(jsonable_encoder({k:review[k] for k in ['id','action','text','reason','actor_name','created_by','created_at','sequence']}))
        f['review_sequence']=review['sequence']
        f['review_status']={'accept':'approved','edit':'in_review','reject':'changes_requested','request_revision':'changes_requested','mark_unknown':'needs_attention'}[action]
        if action=='edit':f.update(text=review['text'],provenance='founder_provided',confidence='unknown',unknown=False)
        elif action=='mark_unknown':f.update(unknown=True,provenance='unverified',confidence='unknown')
        elif action=='accept':
            # Human confirmation is an annotation, never a change to original_text/evidence.
            f['provenance']='founder_confirmed'
    for ch in document['chapters']:
        statuses=[f['review_status'] for f in ch['findings']]
        ch['review_status']='approved' if all(s=='approved' for s in statuses) else 'changes_requested' if 'changes_requested' in statuses else 'needs_attention' if any(f['unknown'] for f in ch['findings']) else 'in_review' if any(s!='not_started' for s in statuses) else 'not_started'
        ch['source_count']=len({sid for f in ch['findings'] for sid in f['source_ids']})
        ch['confidence']='unknown' if any(f['unknown'] for f in ch['findings']) else min((f['confidence'] for f in ch['findings']),key=lambda c:['unknown','low','medium','high'].index(c),default='unknown')
        ch['summary']=ch['findings'][0]['text']
    return {'id':row['id'],'version':row['version'],'brand_id':row['brand_id'],'created_at':row['created_at'],'document':document,'review_sequence':len(reviews),'content_sha256':payload_hash(document)}


def threads(connection,brand_id,version_id):
    rows=connection.execute('SELECT * FROM mi.brand_brain_threads WHERE brand_id=%s AND version_id=%s ORDER BY created_at',(brand_id,version_id)).fetchall()
    for row in rows:
        row['comments']=connection.execute('SELECT * FROM mi.brand_brain_comments WHERE brand_id=%s AND thread_id=%s ORDER BY created_at,id',(brand_id,row['id'])).fetchall()
        events=connection.execute('SELECT * FROM mi.brand_brain_thread_events WHERE brand_id=%s AND thread_id=%s ORDER BY created_at,id',(brand_id,row['id'])).fetchall()
        row['events']=events;row['resolved']=bool(events and events[-1]['action']=='resolve');row['last_event']=events[-1]['id'] if events else None
    return rows


def quality(connection,row):
    view=project(connection,row);chs=view['document']['chapters'];comments=threads(connection,row['brand_id'],row['id'])
    findings=[f for ch in chs for f in ch['findings']]
    view['quality']={'approved_chapters':[ch['key'] for ch in chs if ch['review_status']=='approved'],'attention_chapters':[ch['key'] for ch in chs if ch['review_status']!='approved'],
        'unresolved_comments':sum(not t['resolved'] for t in comments),'critical_missing':[f['id'] for f in findings if f['critical'] and f['unknown']],
        'completion_percent':round(sum(f['review_status']=='approved' for f in findings)/len(findings)*100) if findings else 0}
    view['threads']=comments
    return view
