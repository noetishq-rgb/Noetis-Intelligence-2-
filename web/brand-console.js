import {escapeHtml as e} from './model.js';

export function newConsole(){
  return {enabled:false,history:[],documents:[],filter:'',question:'',busy:false,error:''};
}

export function searchDocuments(state){
  const needle=state.filter.trim().toLowerCase();
  return state.documents.map(doc=>{
    const index=doc.text.toLowerCase().indexOf(needle);
    if(index<0&&!doc.name.toLowerCase().includes(needle))return '';
    const start=Math.max(0,index-50);
    return `<article><strong>${e(doc.name)}</strong><p>${e(doc.text.slice(start,start+200))}</p></article>`;
  }).join('')||'<p>No matching documents.</p>';
}

export function consoleView(state){
  return `<section class="portal-card agency-console"><h2>Brand console</h2>
  <p>Ask Claude about your brand and the documents you attach. This conversation lasts until you leave the workspace or reload.</p>
  <label>Attach documents (.txt or .md)<input type="file" multiple accept=".txt,.md" id="console-files" ${state.busy?'disabled':''}></label>
  ${state.documents.length?`<label>Search documents<input id="console-find" value="${e(state.filter)}"></label><div id="console-matches">${searchDocuments(state)}</div>`:''}
  <div aria-live="polite">${state.history.map(m=>`<article class="console-turn"><strong>${m.role==='user'?'You':'Claude'}</strong><p>${e(m.text)}</p></article>`).join('')}</div>
  ${state.enabled?'':'<p>Claude replies are awaiting agency activation. You can attach and search documents now.</p>'}
  <form id="console-ask"><label>Your question<textarea name="question" maxlength="1500" required>${e(state.question)}</textarea></label>
  <p>Sending shares the question, recent conversation and attached text with Claude.</p>
  <button class="button primary" ${state.busy||!state.enabled?'disabled':''}>${state.busy?'Waiting for Claude…':'Send'}</button></form>
  ${state.error?`<p role="alert">${e(state.error)}</p>`:''}</section>`;
}
