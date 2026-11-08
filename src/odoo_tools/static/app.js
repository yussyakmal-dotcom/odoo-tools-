/* Minimal UI. Auth token stays in memory; server session is HttpOnly cookie. */
let csrf = null;
let currentUser = null;
const $ = (id) => document.getElementById(id);
function showMessage(message, good=false) { const el=$('toast'); el.textContent=message; el.className=good?'good':''; }
function clearMessage(){ showMessage(''); }
async function api(path, {method='GET', body=null}={}) {
  const headers = {};
  if (body!==null) headers['Content-Type']='application/json';
  if (method!=='GET' && csrf) headers['X-CSRF-Token']=csrf;
  const response = await fetch('/api/v1'+path, {method,headers,credentials:'same-origin',body:body===null?undefined:JSON.stringify(body)});
  let result; try {result=await response.json();} catch {result={};}
  if (!response.ok) throw new Error(typeof result.detail==='string'?result.detail: 'Request failed ('+response.status+')');
  return result;
}
function text(tag,value){const element=document.createElement(tag);element.textContent=value;return element;}
function switchToLogin(){currentUser=null;csrf=null;$('login-view').hidden=false;$('app-view').hidden=true;}
async function render(){
  const me=await api('/auth/me');csrf=me.csrf_token; currentUser=me.user;
  $('login-view').hidden=true;$('app-view').hidden=false;
  $('identity').textContent=currentUser.display_name+' · '+currentUser.username+(currentUser.is_admin?' · Administrator':'');
  $('admin-area').hidden=!currentUser.is_admin || currentUser.must_change_password;
  $('workspace-form').hidden=!currentUser.is_admin || currentUser.must_change_password;
  $('workspaces').replaceChildren();
  if(!me.workspaces.length) $('workspaces').textContent='Belum ada workspace yang dapat diakses.';
  for(const w of me.workspaces){const row=document.createElement('div');row.className='workspace-item';row.append(text('strong',w.name),text('small',w.slug+' · '+(w.role||'Administrator')));$('workspaces').append(row);}
  await refreshConnectionPanel(me.workspaces);
  if(currentUser.is_admin && !currentUser.must_change_password){const users=await api('/admin/users');$('users').replaceChildren();for(const u of users){const tr=document.createElement('tr');for(const value of [u.username,u.display_name,u.is_admin?'Administrator':'User',u.is_active?'Aktif':'Nonaktif'])tr.append(text('td',value));$('users').append(tr);}}
  if(currentUser.must_change_password) showMessage('Password awal harus diganti sebelum menggunakan fitur lain.');
}
$('login-form').addEventListener('submit',async(e)=>{e.preventDefault();clearMessage();try {const result=await api('/auth/login',{method:'POST',body:{username:$('username').value,password:$('password').value}});csrf=result.csrf_token;$('password').value='';await render();}catch(error){showMessage(error.message);}});
$('logout').addEventListener('click',async()=>{try{await api('/auth/logout',{method:'POST'});}catch(error){showMessage(error.message);}switchToLogin();});
$('password-form').addEventListener('submit',async(e)=>{e.preventDefault();clearMessage();try{const result=await api('/auth/change-password',{method:'POST',body:{current_password:$('old-password').value,new_password:$('new-password').value}});$('password-form').reset();switchToLogin();showMessage(result.message,true);}catch(error){showMessage(error.message);}});
$('workspace-form').addEventListener('submit',async(e)=>{e.preventDefault();clearMessage();try{await api('/workspaces',{method:'POST',body:{name:$('workspace-name').value,slug:$('workspace-slug').value}});$('workspace-form').reset();await render();showMessage('Workspace berhasil dibuat.',true);}catch(error){showMessage(error.message);}});
$('user-form').addEventListener('submit',async(e)=>{e.preventDefault();clearMessage();try{await api('/admin/users',{method:'POST',body:{username:$('user-name').value,display_name:$('user-display').value,password:$('user-password').value,is_admin:$('user-admin').checked}});$('user-form').reset();await render();showMessage('User berhasil dibuat. User perlu mengganti password awal saat login pertama.',true);}catch(error){showMessage(error.message);}});
render().catch(()=>switchToLogin());
