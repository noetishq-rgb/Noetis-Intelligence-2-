"""Bounded GitHub Actions task runner. The database owns leases and job state."""
import argparse,os,sys,time
from pathlib import Path
from uuid import UUID
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--kind',choices=['crawl','research','planning'],required=True)
    parser.add_argument('--seconds',type=int,default=240)
    args=parser.parse_args()
    if not 1<=args.seconds<=600:raise SystemExit('Use 1 to 600 seconds')
    if not os.environ.get('MI_SERVICE_USER_ID'):raise SystemExit('A bound service principal is required')
    for key in ['MI_MODEL_INPUT_PAISE_PER_MILLION','MI_MODEL_OUTPUT_PAISE_PER_MILLION','MI_PRICING_AS_OF','MI_RESEARCH_MAX_COST_PAISE','MI_MODEL_MAX_COST_PAISE']:
        if os.environ.get(key)=='':os.environ.pop(key)
    if args.kind in {'research','planning'} and os.environ.get('MI_ALLOW_MODEL_CALLS','false').lower()=='true':
        required=['MI_MODEL_INPUT_PAISE_PER_MILLION','MI_MODEL_OUTPUT_PAISE_PER_MILLION','MI_PRICING_AS_OF','MI_RESEARCH_MAX_COST_PAISE']
        if any(not os.environ.get(k) for k in required):raise SystemExit('Set explicit dated provider prices and a cost cap before enabling model calls')
        if any(int(os.environ[k])<=0 for k in ['MI_MODEL_INPUT_PAISE_PER_MILLION','MI_MODEL_OUTPUT_PAISE_PER_MILLION','MI_RESEARCH_MAX_COST_PAISE']):raise SystemExit('Paid provider prices and cost cap must be positive')
    actor=UUID(os.environ['MI_SERVICE_USER_ID'])
    if args.kind=='crawl':from app.worker import run_once
    elif args.kind=='research':from app.research_worker import run_once
    else:from app.planning_worker import run_once
    end=time.monotonic()+args.seconds
    while time.monotonic()<end:
        if not run_once(actor):break
    print('Bounded worker pass completed.')

if __name__=='__main__':main()
