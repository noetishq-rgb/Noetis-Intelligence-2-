"""Invoke one Vercel crawl using GitHub OIDC. No database/provider secrets in Actions."""
import base64,json,os,time
from urllib.parse import urlencode,urlsplit,urlunsplit,parse_qsl
from urllib.request import Request,urlopen
from urllib.error import HTTPError

AUDIENCE='noetis-crawl:prj_rByocViGrd5OJlsCEYPqvchdqrSS'
ENDPOINT='https://noetis-intelligence-2-noetis.vercel.app/internal/crawl'

def main():
    endpoint=os.environ.get('NOETIS_CRAWL_ENDPOINT',ENDPOINT)
    if endpoint!=ENDPOINT:raise SystemExit('Unexpected crawl endpoint')
    source=os.environ.get('ACTIONS_ID_TOKEN_REQUEST_URL');credential=os.environ.get('ACTIONS_ID_TOKEN_REQUEST_TOKEN')
    if not source or not credential:raise SystemExit('Run from the authorized GitHub Actions workflow')
    revision=os.environ.get('GITHUB_SHA')
    if revision:
        # Request an identity only after the exact commit is serving production.
        health=ENDPOINT.rsplit('/internal/crawl',1)[0]+'/health'
        for attempt in range(24):
            try:
                with urlopen(Request(health),timeout=15) as response:
                    if response.headers.get('X-Noetis-Revision')==revision:break
            except Exception:pass
            if attempt==23:raise SystemExit('The expected production revision is not ready; leaving queued work untouched')
            time.sleep(5)
    url=urlsplit(source)
    if url.scheme!='https' or not url.hostname or not url.hostname.endswith('.actions.githubusercontent.com'):
        raise SystemExit('Unexpected GitHub identity endpoint')
    params=dict(parse_qsl(url.query));params['audience']=AUDIENCE
    request=Request(urlunsplit((url.scheme,url.netloc,url.path,urlencode(params),'')),headers={'Authorization':'Bearer '+credential})
    with urlopen(request,timeout=20) as response:token=json.load(response)['value']
    for attempt in range(12):
        try:
            request=Request(endpoint,data=b'{}',method='POST',headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
            with urlopen(request,timeout=150) as response:result=json.load(response)
            print(json.dumps({'processed':result['processed'],'job_status':(result.get('job') or {}).get('status','idle')}));return
        except HTTPError as error:
            # During a Git deployment, the previous static fallback answers POST with 405.
            if error.code not in {404,405,503} or attempt==11:
                try:
                    detail=json.loads(error.read(4096)).get('detail')
                    if isinstance(detail,str):print('Worker response: '+detail[:200])
                except Exception:pass
                if error.code==403:
                    # Public provenance fields only; never log the signed token or credentials.
                    try:
                        segment=token.split('.')[1];claims=json.loads(base64.urlsafe_b64decode(segment+'='*(-len(segment)%4)))
                        fields=['repository','repository_id','ref','workflow_ref','event_name','sub','aud']
                        print(json.dumps({'workflow_identity':{k:str(claims.get(k,''))[:200] for k in fields}}))
                    except Exception:pass
                raise SystemExit('Hosted worker returned HTTP '+str(error.code))
            time.sleep(5)
        except Exception:
            raise SystemExit('Hosted worker did not return a confirmed response; a later pass will recover its durable lease')

if __name__=='__main__':main()
