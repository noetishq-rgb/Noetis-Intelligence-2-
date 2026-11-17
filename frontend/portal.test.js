import test from 'node:test';
import assert from 'node:assert/strict';
import {accessScreen,ownerScreen,passwordScreen} from '../web/access-views.js';
import {createBrainApi} from '../web/brain-api.js';

test('client sign-in comes before brand inputs and owner setup asks only for access details',()=>{
  const login=accessScreen({available:true});assert.match(login,/Sign in to Brand Brain/);assert.doesNotMatch(login,/name="brand_name"|name="category"|SAMPLE DATA/);
  const owner=ownerScreen();assert.match(owner,/Create brand login/);assert.match(owner,/name="email"/);assert.doesNotMatch(owner,/name="category"|name="objectives"|name="website"/);
  assert.match(passwordScreen(),/autocomplete="new-password"/);
});
test('owner directory escapes client content and shows reset, suspension and saved onboarding state',()=>{
  const html=ownerScreen({accounts:[{id:'a',brand_id:'b',brand_name:'<img src=x>',email:'client@example.com',full_name:'A & B',status:'active',requires_password_change:false,onboarding_status:'submitted'}]});
  assert.match(html,/&lt;img src=x&gt;/);assert.doesNotMatch(html,/<img src=x>/);assert.match(html,/data-owner-action="suspend"/);assert.match(html,/data-owner-action="reset_password"/);assert.match(html,/Onboarding: submitted/);
});
test('a new page restores with a same-origin cookie request without browser token storage',async()=>{
  const calls=[];const api=createBrainApi(async(path,options)=>{calls.push({path,options});return {ok:true,status:200,json:async()=>path==='/v1/auth/session'?{access_token:'restored',refresh_token:'rotated'}:{id:'client'}}});
  assert.equal((await api.restore()).id,'client');assert.equal(calls[0].path,'/v1/auth/session');assert.equal(calls[0].options.credentials,'same-origin');assert.equal(calls[0].options.headers['X-NOETIS-Session'],'1');assert.equal(calls[0].options.body,undefined);assert.equal(calls[1].options.headers.Authorization,'Bearer restored');
});
test('simultaneous expired requests share refresh rotation',async()=>{
  let refreshes=0;const api=createBrainApi(async(path,options)=>{
    if(path==='/v1/auth/token')return {ok:true,status:200,json:async()=>({access_token:'old',refresh_token:'first'})};
    if(path==='/v1/auth/refresh'){refreshes++;await new Promise(resolve=>setImmediate(resolve));return {ok:true,status:200,json:async()=>({access_token:'new',refresh_token:'second'})}}
    const ok=options.headers.Authorization==='Bearer new'||path==='/v1/me';return {ok,status:ok?200:401,json:async()=>ok?{id:'u'}:{detail:'Expired'}};
  });await api.login('client@example.com','fixture-password');await Promise.all([api.request('/v1/brands'),api.request('/v1/organizations')]);assert.equal(refreshes,1);
});

async function restoredFlow(kind){
  const originals=Object.fromEntries(['document','location','history','fetch'].map(k=>[k,globalThis[k]]));
  const calls=[],listeners={};const root={addEventListener:(key,listener)=>{listeners[key]=listener}};
  globalThis.document={documentElement:{dataset:{environment:'live-pilot'}},querySelector:selector=>selector==='#view'?root:null};
  globalThis.location={hash:'',pathname:'/login',origin:'https://app.example'};
  globalThis.history={replaceState(_a,_b,path){globalThis.location.pathname=path}};
  globalThis.fetch=async(path)=>{
    calls.push(path);let status=200,data={};
    if(path==='/health')data={status:'ok'};
    else if(path==='/v1/auth/session')data={access_token:'restored',refresh_token:'restored-refresh'};
    else if(path==='/v1/me')data={id:'user',full_name:'Contact',managed_client:kind==='client',requires_password_change:false};
    else if(path==='/v1/brands?limit=100')data=kind==='owner'?[]:[{id:'assigned',name:'Assigned brand',website_url:null}];
    else if(path==='/v1/organizations')data=[{id:'org',role:kind==='owner'?'founder':'client'}];
    else if(path==='/v1/organizations/org/clients')data=[];
    else if(path.endsWith('/sessions/current'))data={id:'session',status:'draft',version:1,draft:{brand_name:'Assigned brand',online_presence:[],category:'',niche:'',objectives:[],other_objective:''}};
    else if(path.endsWith('/overview')){status=404;data={detail:'No findings'}}
    else throw new Error('Unexpected API '+path);
    return {ok:status===200,status,json:async()=>data};
  };
  try{
    const module=await import('../web/brain.js?flow='+kind);module.initializeBrain(()=>{});
    for(let i=0;i<12;i++)await new Promise(resolve=>setImmediate(resolve));
    return {state:module.brainSessionInfo(),html:module.renderBrain(),path:location.pathname,calls};
  }finally{for(const [k,v]of Object.entries(originals)){if(v===undefined)delete globalThis[k];else globalThis[k]=v}}
}
test('returning client opens only the assigned saved Brand Brain',async()=>{
  const result=await restoredFlow('client');assert.equal(result.path,'/brain');assert.equal(result.state.brands[0].id,'assigned');assert.match(result.html,/value="Assigned brand"/);assert.doesNotMatch(result.html,/Create a new brand|Create brand login|SuperYou|SAMPLE DATA/);assert.equal(result.calls.filter(p=>p.includes('/clients')).length,0);
});
test('returning owner goes directly to client management without brand onboarding',async()=>{
  const result=await restoredFlow('owner');assert.equal(result.path,'/owner');assert.equal(result.state.ownerView,true);assert.match(result.html,/Create brand login/);assert.doesNotMatch(result.html,/id="bb-setup"/);assert.ok(!result.calls.some(p=>p.includes('/brain/')));
});
