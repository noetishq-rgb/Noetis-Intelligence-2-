"""Invoke one Vercel crawl using GitHub OIDC. No database/provider secrets in Actions."""
import json,os,time
from uuid import UUID
from urllib.parse import urlencode,urlsplit,urlunsplit,parse_qsl
from urllib.request import Request,urlopen
from urllib.error import HTTPError

AUDIENCE='noetis-crawl:prj_rByocViGrd5OJlsCEYPqvchdqrSS'
ENDPOINT='https://noetis-intelligence-2-noetis.vercel.app/internal/crawl'

def main():
    endpoint=os.environ.get('NOETIS_CRAWL_ENDPOINT',ENDPOINT)
    if endpoint!=ENDPOINT:raise SystemExit('Unexpected crawl endpoint')
    action=os.environ.get('NOETIS_RESEARCH_PILOT_ACTION')
    if action and (action not in {'status','run'} or os.environ.get('GITHUB_EVENT_NAME')!='workflow_dispatch'):
        raise SystemExit('Research pilot requires an explicit workflow dispatch')
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
    headers = {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}
    if action:
        target = endpoint.replace('/internal/crawl', '/internal/research-pilot')
        request = Request(target, data=json.dumps({'action': action}).encode(), headers=headers)
        with urlopen(request, timeout=150) as response:
            print(json.dumps(json.load(response)))
        return

    for attempt in range(12):
        try:
            with urlopen(Request(endpoint + '/brands', headers=headers), timeout=20) as response:
                brands = [UUID(item) for item in json.load(response)]
            break
        except HTTPError as error:
            if error.code not in {404,405,503} or attempt == 11:
                raise SystemExit('Capture discovery returned HTTP ' + str(error.code))
            time.sleep(5)
    if not brands:
        raise SystemExit('No hosted capture workers configured')
    failures = 0
    for brand in brands:
        target = endpoint + '?brand_id=' + str(brand)
        try:
            with urlopen(Request(target, data=b'{}', headers=headers), timeout=150) as response:
                result = json.load(response)
            job = result.get('job') or {}
            print(json.dumps({'processed': result['processed'], 'job_status': job.get('status', 'idle')}))
        except HTTPError as error:
            print('Hosted capture returned HTTP', error.code)
            failures += 1
        except (OSError, ValueError):
            print('Capture response unavailable; the durable job lease controls recovery')
            failures += 1
    if failures:
        raise SystemExit(f'{failures} hosted capture invocation(s) need attention')


if __name__=='__main__':main()
