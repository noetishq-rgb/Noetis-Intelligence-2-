import {brainSessionInfo,brainWorkspaceName,brainWorkspaceId,navigateBrain,selectBrainWorkspace,renderBrain,signOutBrain} from './brain.js';
import {escapeHtml as e} from './model.js';
import {icon} from './charts.js';
const $=selector=>document.querySelector(selector);
export function renderPortalShell(){
  const state=brainSessionInfo(),guest=!state.user||state.user.requires_password_change;
  document.body.classList.add('portal-mode','brain-mode');
  document.body.classList.toggle('portal-guest',guest);
  document.body.classList.toggle('portal-owner-mode',state.ownerView);
  document.body.classList.remove('presentation-mode');
  const name=state.ownerView?'Owner Console':brainWorkspaceName();
  const links=state.ownerView?[['owner','Client access','lock']]:[['1','Brand setup','book'],['2','Onboarding status','check'],['3','Brand overview','chart'],['4','Intelligence','book'],['5','Review & discuss','message'],['6','Confirmation','check']];
  $('#navigation').innerHTML=`${state.owner?`<div class="nav-group"><p class="nav-label">MANAGEMENT</p><button class="nav-link ${state.ownerView?'active':''}" data-portal-screen="owner">${icon('lock',16)}<span>Owner Console</span></button></div>`:''}<div class="nav-group"><p class="nav-label">BRAND BRAIN</p>${links.filter(([id])=>id!=='owner').map(([id,label,ic])=>`<button class="nav-link ${!state.ownerView&&Number(id)===state.screen?'active':''}" data-portal-screen="${id}" ${Number(id)>1&&!state.session||Number(id)>2&&!state.version?'disabled':''}>${icon(ic,16)}<span>${label}</span></button>`).join('')}</div>`;
  $('#brand-select').innerHTML=state.brands.length?state.brands.map(b=>`<option value="${b.id}" ${b.id===brainWorkspaceId()?'selected':''}>${e(b.name)}</option>`).join(''):'<option>Brand Brain</option>';
  $('#brand-select').disabled=guest||!state.brands.length;
  $('#workspace-logo').textContent=state.ownerView?'N':name.slice(0,1);$('#workspace-logo').style.background='var(--purple-soft)';
  $('#crumb-brand').textContent=name;$('#breadcrumb').textContent=state.ownerView?'Client access':links.find(([id])=>Number(id)===state.screen)?.[1]||'Brand Brain';
  $('.wordmark').href=state.owner?'/owner':'/brain';
  $('.sidebar-note').innerHTML='<span class="note-icon">✧</span><strong>Your brand’s shared intelligence.</strong><p>One private workspace.<br>Progress saved as you go.</p>';
  $('#profile-button').innerHTML=`<span class="avatar">${e(state.user?.full_name?.slice(0,1)||'N')}</span><span>${e(state.user?.full_name||'NOETIS')}<small>${state.owner?'Owner access':'Private client access'}</small></span><span class="profile-menu">↗</span>`;
  $('.preview-label').innerHTML='<span class="preview-dot"></span>Private workspace';
  $('#footer-state').textContent='Private brand workspace · data stored in Supabase';
  $('#view').innerHTML=renderBrain();$('#view').setAttribute('aria-busy',String(state.loading));
  document.title=`${state.ownerView?'Owner Console':guest?'Sign in':name} · NOETIS Brand Brain`;
}
export function installPortalNavigation(){
  document.addEventListener('click',event=>{
    const nav=event.target.closest('[data-portal-screen]');if(nav){navigateBrain(nav.dataset.portalScreen);return}
    if(event.target.closest('#profile-button,#profile-top')){event.preventDefault();event.stopImmediatePropagation();signOutBrain()}
  },true);
  document.addEventListener('keydown',event=>{if((event.metaKey||event.ctrlKey)&&event.key.toLowerCase()==='k'){event.preventDefault();event.stopImmediatePropagation()}},true);
}
