// Bearer tokens stay in memory; a Secure HttpOnly cookie restores the session.
export function createBrainApi(fetcher=globalThis.fetch){
  let access=null,refresh=null,renewal=null;
  const adopt=tokens=>{access=tokens.access_token;refresh=tokens.refresh_token};
  async function renew(){
    if(!renewal)renewal=(async()=>{
      const path=refresh?'/v1/auth/refresh':'/v1/auth/session';
      const response=await fetcher(path,{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-NOETIS-Session':'1'},body:refresh?JSON.stringify({refresh_token:refresh}):undefined});
      if(!response.ok){access=null;refresh=null;return false}
      adopt(await response.json());return true;
    })().finally(()=>{renewal=null});
    return renewal;
  }
  async function request(path,method='GET',body=null,extra={}){
    if(!path.startsWith('/v1/'))throw new Error('Only application API paths are allowed');
    const send=()=>fetcher(path,{method,credentials:'same-origin',headers:{Accept:'application/json',...(body?{'Content-Type':'application/json'}:{}),...(access?{Authorization:'Bearer '+access}:{}),...extra},body:body?JSON.stringify(body):undefined});
    const sentAccess=access;
    let r;try{r=await send()}catch{throw new Error('The application backend is unavailable. Your saved workspace remains in the database.')}
    if(r.status===401&&access&&!path.startsWith('/v1/auth/')){if(access!==sentAccess||await renew())r=await send()}
    let data;try{data=await r.json()}catch{throw new Error('The application API is not connected to this preview. Research has not started.')}
    if(!r.ok){const detail=data.detail;const message=typeof detail==='string'?detail:Array.isArray(detail)?detail.map(e=>e.msg).join('; '):detail?.message||'The request could not be completed';const error=new Error(message);error.status=r.status;throw error}
    return data;
  }
  return {request,
    async available(){try{const r=await fetcher('/health',{method:'GET',credentials:'same-origin',headers:{Accept:'application/json'}});return r.ok&&(await r.json()).status==='ok'}catch{return false}},
    async restore(){try{if(!await renew())return null;return await request('/v1/me')}catch(error){if(error.status===401)return null;throw error}},
    async activate(email,full_name,password,token){adopt(await request('/v1/auth/activate-founder','POST',{email,full_name,password,token}));return request('/v1/me')},
    async login(email,password){adopt(await request('/v1/auth/token','POST',{email,password}));return request('/v1/me')},
    async changePassword(current_password,new_password){adopt(await request('/v1/auth/password','POST',{current_password,new_password}));return request('/v1/me')},
    async logout(){try{await request('/v1/auth/logout','POST')}finally{access=null;refresh=null;await fetcher('/v1/auth/session',{method:'DELETE',credentials:'same-origin',headers:{'X-NOETIS-Session':'1'}}).catch(()=>{})}},
    hasSession:()=>!!access
  };
}
