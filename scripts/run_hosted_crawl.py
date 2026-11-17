"""Invoke one Vercel crawl using GitHub OIDC. No database/provider secrets in Actions."""
import json,os,time
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
    url=urlsplit(source)
    if url.scheme!='https' or not url.hostname or not url.hostname.endswith('.actions.githubusercontent.com'):
        raise SystemExit('Unexpected GitHub identity endpoint')
    params=dict(parse_qsl(url.query));params['audience']=AUDIENCE
    request=Request(urlunsplit((url.scheme,url.netloc,url.path,urlencode(params),'')),headers={'Authorization':'Bearer '+credential})
    with urlopen(request,timeout=20) as response:token=json.load(response)['value']
    for attempt in range(6):
        try:
            request=Request(endpoint,data=b'{}',method='POST',headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
            with urlopen(request,timeout=150) as response:result=json.load(response)
            print(json.dumps({'processed':result['processed'],'job_status':(result.get('job') or {}).get('status','idle')}));return
        except HTTPError as error:
            if error.code not in {404,503} or attempt==5:raise SystemExit('Hosted worker returned HTTP '+str(error.code))
            time.sleep(5)
        except Exception:
            raise SystemExit('Hosted worker did not return a confirmed response; a later pass will recover its durable lease')

if __name__=='__main__':main()
