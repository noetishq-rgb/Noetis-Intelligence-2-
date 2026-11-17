import json,sys
from pathlib import Path
root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root))
from app.models import PromptRun
uid={'type':'string','format':'uuid'}
ref=lambda name:{'$ref':'../schemas/'+name+'.schema.json'}
closed=lambda props,required=None:{'type':'object','additionalProperties':False,'properties':props,'required':required if required is not None else list(props)}
tasks=PromptRun.model_json_schema()['properties']['task_type']['enum']
research_tasks={'brand_research','competitor_research','audience_research','market_research','social_listening','content_gap','strategy','strategy_critique','thirty_day_plan','creative_brief'}
tasks=[task for task in tasks if task not in research_tasks]
filters=closed({'geography':{'const':'IN'},'channels':{'type':'array','items':{'enum':['meta_ads','meta_organic','google_seo','google_search','instagram','linkedin','youtube','facebook','email']}},'languages':{'type':'array','minItems':1,'items':{'type':'string'}},'topic':{'type':['string','null']},'date_start':{'type':['string','null'],'format':'date'},'date_end':{'type':['string','null'],'format':'date'}})
artifact_ref=closed({'artifact_id':uid,'version':{'type':'integer','minimum':1},'sha256':{'type':'string','pattern':'^[a-f0-9]{64}$'}})
start=closed({'expected_workflow_version':{'type':'integer','minimum':1},'inputs':{'type':'array','items':artifact_ref},'filters':filters})
accepted=closed({'brand_id':uid,'workflow_id':uid,'run_id':uid,'state':{'type':'string'},'output_schema':{'type':'string'}})
decision=closed({'artifact':artifact_ref,'decision':{'enum':['approved','revision_requested','rejected']},'reason':{'type':'string','minLength':1},'selected_option_id':{'anyOf':[uid,{'type':'null'}]},'approved_kpi_names':{'type':'array','items':{'type':'string'}}})
paths={}
brand_parameter={'name':'brand_id','in':'path','required':True,'schema':uid}
idempotency={'name':'Idempotency-Key','in':'header','required':True,'schema':{'type':'string','minLength':8,'maxLength':128}}
def operation(method,path,summary,body=None,response=None,parameters=None,status='200'):
    parameters=[brand_parameter]+(parameters or [])
    op={'summary':summary,'x-implementation-status':'planned-not-implemented','security':[{'BearerAuth':[]}], 'parameters':parameters,'responses':{status:{'description':'Planned successful response','content':{'application/json':{'schema':response or accepted}}},'401':{'description':'Authentication required'},'404':{'description':'Absent or inaccessible resource'},'409':{'description':'Stale version, missing inputs or approval gate'},'422':{'description':'Invalid structured input'}}}
    if body is not None:op['requestBody']={'required':True,'content':{'application/json':{'schema':body}}}
    paths.setdefault(path,{})[method]=op
for task in tasks:
    operation('post',f'/v1/brands/{{brand_id}}/workflows/{task}/runs','Queue '+task+' with validated compact context',start,accepted,[idempotency],'202')
operation('get','/v1/brands/{brand_id}/runs/{run_id}','Read a staff-only run record',response=ref('prompt-run'),parameters=[{'name':'run_id','in':'path','required':True,'schema':uid}])
operation('post','/v1/brands/{brand_id}/approval-requests','Create a stored approval request without sending a message',closed({'artifact':artifact_ref,'assigned_to':uid,'workflow_id':uid}),ref('approval-record'))
operation('get','/v1/brands/{brand_id}/approvals/{approval_id}','Read only an approval assigned to the caller or staff',response=ref('approval-record'),parameters=[{'name':'approval_id','in':'path','required':True,'schema':uid}])
operation('post','/v1/brands/{brand_id}/approvals/{approval_id}/decision','Record the assigned human decision for the exact version/hash',decision,ref('approval-record'),[{'name':'approval_id','in':'path','required':True,'schema':uid}])
operation('post','/v1/brands/{brand_id}/exports','Queue a private report or presentation file',closed({'artifact':artifact_ref,'audience':{'enum':['internal','client']},'format':{'enum':['pdf','pptx','docx']},'language':{'type':'string'}}),accepted,[idempotency],'202')
operation('get','/v1/brands/{brand_id}/deliverables/{artifact_id}','Read an approved client projection, never raw internal output',response=closed({'artifact':artifact_ref,'kind':{'enum':['research_summary','strategy_options','plan','creative','report','presentation']},'title':{'type':'string'},'executive_summary':{'type':'string'},'evidence_links':{'type':'array','items':{'type':'string','format':'uri'}},'next_steps':{'type':'array','items':{'type':'string'}},'approval_requests':{'type':'array','items':uid}}),parameters=[{'name':'artifact_id','in':'path','required':True,'schema':uid}])
operation('post','/v1/brands/{brand_id}/assets/upload-intents','Create a private brand-bound upload with size and type limits',closed({'filename':{'type':'string'},'content_type':{'type':'string'},'byte_size':{'type':'integer','minimum':1,'maximum':52428800},'sha256':{'type':'string','pattern':'^[a-f0-9]{64}$'}}),closed({'asset_id':uid,'upload_url':{'type':'string','format':'uri'},'expires_in_seconds':{'const':60}}))
operation('post','/v1/brands/{brand_id}/assets/{asset_id}/complete','Verify object ownership, digest and malware scan',closed({'sha256':{'type':'string','pattern':'^[a-f0-9]{64}$'}}),closed({'asset_id':uid,'scan_status':{'enum':['pending','clean','rejected']}}),[{'name':'asset_id','in':'path','required':True,'schema':uid}])
operation('post','/v1/brands/{brand_id}/prompts/compile','Compile an allowlisted template with selected versioned context',closed({'task_type':{'enum':tasks},'inputs':{'type':'array','items':artifact_ref},'filters':filters}),closed({'template_id':uid,'template_version':{'type':'integer'},'compiled_prompt':{'type':'string'},'prompt_sha256':{'type':'string'},'output_schema_name':{'type':'string'},'estimated_input_tokens':{'type':'integer'},'maximum_output_tokens':{'type':'integer'}}))
spec={'openapi':'3.1.0','info':{'title':'Marketing Intelligence planned workflow contracts','version':'0.4.0','description':'SPECIFICATIONS ONLY. These endpoints are not implemented. The implemented research and planning APIs are in openapi-phase4.json. Generic client delivery, private account integrations and external execution remain deferred.'},'paths':paths,'components':{'securitySchemes':{'BearerAuth':{'type':'http','scheme':'bearer','bearerFormat':'JWT'}}}}
(root/'docs/openapi-planned.json').write_text(json.dumps(spec,indent=2)+'\n')
print('Exported planned contracts for',len(tasks),'workflow task types.')
