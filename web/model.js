import {brands,channels,health,opportunities,decisions,strategies,tasks,competitors,readiness,recommendations} from './data.js';
export const REPORT_END='2026-11-16';
export const defaultFilters={period:'28',channel:'all',campaign:'all',audience:'all',geography:'all',device:'all',format:'all',funnel:'all',objective:'all'};
export const filterOptions={campaign:[['all','All campaigns'],['discovery','Everyday discovery'],['intent','High-intent capture'],['lifecycle','Lifecycle nurture']],audience:[['all','All audiences'],['everyday','Everyday choices'],['active','Active lifestyle']],geography:[['all','All geographies'],['metros','Indian metros'],['tier2','Other Indian cities']],device:[['all','All devices'],['mobile','Mobile'],['desktop','Desktop']],format:[['all','All creative formats'],['video','Short video'],['static','Static'],['carousel','Carousel'],['article','Search content'],['email','Email']],funnel:[['all','All funnel stages'],['awareness','Awareness'],['consideration','Consideration'],['conversion','Conversion']],objective:[['all','All objectives'],['sales','Sales'],['leads','Qualified leads']]};
export function initialState(){return {brand:'superyou',mode:'sample',filters:{...defaultFilters},decisions:{},schedule:tasks.map(t=>({...t})),scenario:{budget:200000,allocation:[40,30,20,10],geography:'metros',objective:'sales',capacity:8,target:450},timelineEditable:false,decisionView:{kind:'all',sort:'priority',status:'pending',expanded:false}}}
export const number=v=>new Intl.NumberFormat('en-IN',{maximumFractionDigits:0}).format(v);
export const money=v=>new Intl.NumberFormat('en-IN',{style:'currency',currency:'INR',maximumFractionDigits:0}).format(v);
export function compactMoney(v){return v>=100000?`₹${(v/100000).toFixed(v%100000===0?0:1)}L`:v>=1000?`₹${(v/1000).toFixed(v%1000===0?0:1)}k`:money(v)}
export const rangeMoney=range=>range?range.map(compactMoney).join(' – '):'Not quantified';
export const percentage=(n,p)=>p?((n-p)/p*100):null;
export function dateAt(offset){const d=new Date(REPORT_END+'T12:00:00Z');d.setUTCDate(d.getUTCDate()+offset);return d.toISOString().slice(0,10)}
export function planDate(day){const d=new Date('2026-11-17T12:00:00Z');d.setUTCDate(d.getUTCDate()+day-1);return formatDate(d.toISOString().slice(0,10))}
export function formatDate(value){return new Intl.DateTimeFormat('en-IN',{day:'numeric',month:'short',timeZone:'Asia/Kolkata'}).format(new Date(value+'T12:00:00Z'))}
export function periodLabel(filters){return `${formatDate(dateAt(1-Number(filters.period)))} – ${formatDate(REPORT_END)} 2026`}
export function escapeHtml(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
export function generateRows(brand='superyou'){
 const factor=1+Math.max(0,brands.findIndex(b=>b.id===brand))*.14,out=[];
 for(let day=-111;day<=0;day++)for(let c=0;c<4;c++)for(let a=0;a<2;a++)for(let g=0;g<2;g++)for(let d=0;d<2;d++){
  const cycle=1+Math.sin((day+112)*.31+c)*.12,trend=1+(day+112)*.002,volume=(d===0?1:.46)*(g===0?1:.64)*(a===0?1:.7)*factor;
  const spend=Math.round(([390,270,0,0][c])*volume*cycle),visits=Math.round([105,85,66,33][c]*volume*cycle*trend),leads=Math.max(0,Math.round(visits*[.07,.11,.05,.09][c])),orders=Math.max(0,Math.round(visits*[.031,.048,.024,.043][c]));
  out.push({theme:(day+112+a+g)%5,date:dateAt(day),channel:channels[c].id,campaign:c===0||c===2?'discovery':c===1?'intent':'lifecycle',audience:a?'active':'everyday',geography:g?'tier2':'metros',device:d?'desktop':'mobile',format:c===1?'article':c===3?'email':(day+c+a+112)%3===0?'video':(day+c+a+112)%3===1?'static':'carousel',funnel:c===0?'awareness':c===1?'conversion':'consideration',objective:a?'leads':'sales',spend,visits,leads,orders,revenue:orders*640,impressions:visits*(c===1?14:29),engagements:Math.round(visits*1.6)});
 }return out;
}
const cache=new Map();
export function dataset(brand){if(!cache.has(brand))cache.set(brand,generateRows(brand));return cache.get(brand)}
export function matchesFilters(row,filters){return Object.entries(filters).every(([key,value])=>key==='period'||value==='all'||row[key]===value)}
export function selectRows(rows,filters,previous=false){const days=Number(filters.period),end=dateAt(previous?-days:0),start=dateAt(previous?1-days*2:1-days);return rows.filter(r=>r.date>=start&&r.date<=end&&matchesFilters(r,filters))}
export function summarize(rows){const total={spend:0,visits:0,leads:0,orders:0,revenue:0,impressions:0,engagements:0,paidRevenue:0,paidLeads:0};for(const row of rows){for(const key of ['spend','visits','leads','orders','revenue','impressions','engagements'])total[key]+=row[key];if(['meta','search'].includes(row.channel)){total.paidRevenue+=row.revenue;total.paidLeads+=row.leads}}return {...total,cpl:total.paidLeads?total.spend/total.paidLeads:null,roas:total.spend?total.paidRevenue/total.spend:null,conversion:total.visits?total.orders/total.visits*100:null}}
export function analytics(state){const rows=selectRows(dataset(state.brand),state.filters),previous=selectRows(dataset(state.brand),state.filters,true);return {rows,current:summarize(rows),previous:summarize(previous),trend:[...new Set(rows.map(r=>r.date))].sort().map(date=>({date,...summarize(rows.filter(r=>r.date===date))})),byChannel:channels.map(c=>({...c,...summarize(rows.filter(r=>r.channel===c.id))}))}}
export function activeFilterCount(filters){return Object.entries(filters).filter(([k,v])=>k!=='period'&&v!=='all').length}
export function selectedOpportunities(filters){return opportunities.filter(o=>filters.channel==='all'||o.channel===filters.channel)}
export function recordDecision(state,id,action,reason=''){
 if(state.mode!=='sample')throw new Error('Reviews are available in sample mode only');
 if(!recommendations.some(d=>d.id===id))throw new Error('Unknown decision');
 if(!['approved','rejected','revision_requested','scheduled'].includes(action))throw new Error('Unknown action');
 if(state.decisions[id]?.status==='approved'||state.decisions[id]?.status==='rejected')throw new Error('A final sample decision is already recorded');
 if(['rejected','revision_requested'].includes(action)&&!reason.trim())throw new Error('Add a reason for this decision');
 return {...state,decisions:{...state.decisions,[id]:{status:action,reason:reason.trim(),recordedAt:new Date().toISOString(),sample:true,brand:state.brand}}};
}
export function rescheduleTask(state,id,start){
 if(!state.timelineEditable)throw new Error('Enable sample editing to reschedule');
 const task=state.schedule.find(t=>t.id===id);if(!task)throw new Error('Unknown deliverable');
 if(!Number.isInteger(start)||start<1||start+task.duration-1>30)throw new Error('Keep the deliverable within the 30-day plan');
 const proposed=state.schedule.map(t=>t.id===id?{...t,start}:t);
 for(const t of proposed){const parent=proposed.find(p=>p.id===t.dependency);if(parent&&t.start<parent.start+parent.duration)throw new Error('This change would overlap a dependency. Move the dependent work first.');}
 return {...state,schedule:proposed};
}
export function rebalanceAllocation(values,index,value){const v=Math.min(100,Math.max(0,Math.round(value))),remaining=100-v,others=values.map((x,i)=>i===index?0:x),total=others.reduce((a,b)=>a+b,0);let allocated=0;const result=values.map((x,i)=>{if(i===index)return v;const next=total?Math.floor(others[i]/total*remaining):Math.floor(remaining/3);allocated+=next;return next});const last=[0,1,2,3].filter(i=>i!==index).at(-1);result[last]+=remaining-allocated;return result}
export function estimateScenario(s){const rates=s.objective==='sales'?[620,480,850,400]:[360,280,540,310],geo=s.geography==='metros'?1.15:.9,weighted=s.allocation.reduce((sum,n,i)=>sum+n/100*rates[i],0)*geo,volume=s.budget/Math.max(1,weighted),capacity=Math.min(1,s.capacity/8);const low=Math.floor(volume*.65*capacity),high=Math.floor(volume*1.15*capacity);return {low,high,daily:s.budget/30,costLow:high?s.budget/high:0,costHigh:low?s.budget/low:0,revenueLow:low*640,revenueHigh:high*640,risk:s.budget>300000||s.capacity<5?'High':'Medium',confidence:'Low',targetCoverage:s.target?Math.min(100,Math.round(high/s.target*100)):0}}
export function clientProjection(state){if(state.mode!=='sample')throw new Error('No approved live client projection is available');const a=analytics(state);const safe=rankedRecommendations(state,{client:true,includeAll:true}).map(r=>publicRecommendation(r,state));return {decisionSummary:decisionSummary(state,true),recommendations:safe,brand:brands.find(b=>b.id===state.brand).name,period:periodLabel(state.filters),disclosure:'Illustrative design data. Not observed brand results or a live approval record.',competitors:competitors.map(({id,name,initial,tint,x,y,offer})=>({id,name,initial,tint,x,y,offer})),metrics:{revenue:a.current.revenue,leads:a.current.leads,roas:a.current.roas,orders:a.current.orders},health:health.map(({id,name,score,target,change,lever})=>({id,name,score,target,change,lever})),opportunities:opportunities.filter(o=>o.clientApproved&& (state.filters.channel==='all'||o.channel===state.filters.channel)).map(({id,title,ease,impact,confidence,range,action,evidence,risk})=>({id,title,ease,impact,confidence,range,action,evidence,risk})),strategies:strategies.filter(s=>s.clientApproved).map(({id,name,tag,thesis,allocation,range,weeks,risk,recommended})=>({name,tag,thesis,allocation,range,weeks,risk,recommended:state.selectedStrategy?id===state.selectedStrategy:recommended})),timeline:state.schedule.filter(t=>t.clientApproved).map(({id,name,start,duration,ownerName,status})=>({id,name,start,duration,ownerName,status})),requiredApprovals:rankedRecommendations(state,{client:true}).map(d=>({id:d.id,title:d.title,status:recommendationStatus(state,d),deadline:d.deadline})),kpiTargets:[{label:'Paid cost per lead',target:'Below ₹450'},{label:'Paid return on ad spend',target:'Above 3.0×'}],nextSteps:['Review the sample strategy comparison','Confirm sources and measurement definitions','Agree the first test and review checkpoint']}}
export function csvCell(value){return '"'+String(value??'').replaceAll('"','""')+'"'}

export function readinessProjection(state){const brand=brands.find(b=>b.id===state.brand);return state.brand==='superyou'?{brand:brand.name,asOf:'2026-11-16',status:{...readiness},disclosure:'Actual backend pilot readiness snapshot. No live dossier generated.'}:{brand:brand.name,asOf:'2026-11-16',status:{state:'not_assessed',acceptedSources:null,liveDossiers:null},disclosure:'No confirmed research pilot is recorded for this workspace.'}}

export const DECISION_AS_OF='2026-11-17';
export const decisionKinds={risk:'Urgent risks',opportunity:'High-value opportunities',decision:'Pending decisions',action:'Recommended actions'};
export function recommendation(ref){
 if(ref?.startsWith('decision:'))ref=ref.slice(9);
 if(ref?.startsWith('opportunity:keyword-'))ref='keyword:'+ref.slice(20);
 if(ref?.startsWith('journey:'))ref='journey:welcome';
 return recommendations.find(r=>r.id===ref||r.ref===ref)||null;
}
export function recommendationStatus(state,r){return state.decisions[r.id]?.status||'pending'}
export function priorityScore(r){const days=r.deadline?Math.round((Date.parse(r.deadline+'T12:00:00Z')-Date.parse(DECISION_AS_OF+'T12:00:00Z'))/86400000):null;const urgency=days===null?20:days<=1?100:days<=3?85:days<=7?65:40;return Math.round(r.impact*.45+r.confidence*.2+urgency*.35)}
export function rankedRecommendations(state,{client=false,includeAll=false,kind='all',sort='priority',status='all'}={}){
 const list=recommendations.filter(r=>(includeAll||r.center)&&(!client||r.clientVisible)&&(state.filters.channel==='all'||r.channel==='all'||r.channel===state.filters.channel)&&(kind==='all'||r.category===kind)&&(status==='all'||(status==='pending'?!['approved','rejected'].includes(recommendationStatus(state,r)):recommendationStatus(state,r)===status)));
 return list.sort((a,b)=>{let delta;if(sort==='deadline')delta=(a.deadline?Date.parse(a.deadline):Infinity)-(b.deadline?Date.parse(b.deadline):Infinity);else if(sort==='impact'||sort==='confidence')delta=b[sort]-a[sort];else delta=priorityScore(b)-priorityScore(a);return (Number.isNaN(delta)?0:delta)||priorityScore(b)-priorityScore(a)||a.id.localeCompare(b.id)});
}
export function quickReview(state,ref,action,{client=false}={}){
 const r=recommendation(ref);if(!r||client&&!r.clientVisible)throw new Error('This recommendation is not assigned to the client preview');
 if(!['approved','rejected'].includes(action))throw new Error('Choose approve or reject');
 return recordDecision(state,r.id,action,action==='approved'?'Approved from the sample preview. No live approval or marketing action.':'Rejected from the sample preview. No live workflow was advanced.');
}
export function publicRecommendation(r,state){return {id:r.id,ref:r.ref,category:r.category,title:r.title,summary:r.summary,evidence:r.evidence,sourceId:r.sourceId,confidence:r.confidence,impact:r.impact,priority:priorityScore(r),range:r.range,outcomeBasis:r.outcomeBasis,risk:r.risk,deadline:r.deadline,status:recommendationStatus(state,r)}}
export function decisionSummary(state,client=false){
 const list=rankedRecommendations(state,{client,status:'pending'}),find=kind=>list.find(r=>r.category===kind),pick=r=>r?publicRecommendation(r,state):null;
 const score=Math.round(health.reduce((s,h)=>s+h.score,0)/health.length);
 return {position:{score,target:80,label:score>=80?'Strong foundation':'Foundations need attention',basis:'Workspace-wide illustrative health rubric'},opportunity:pick(find('opportunity')),risk:pick(find('risk')),decision:pick(find('decision')||list[0])};
}
