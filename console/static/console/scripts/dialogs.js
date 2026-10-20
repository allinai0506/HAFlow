/* Menus, keyboard dismissal, modal prompts and persistent error toasts. */
var {
  closeMoreMenu, toggleMoreMenu, closeAllTaskMenus, toggleTaskMenu,
  onTaskRowClick, showConfirmModal, showPromptModal, toast,
  closeToast, openModal, closeModal
} = (function({
  esc, closeLinearSelectMenu, toggleSidebar,
  closeSpaceMenu, closeTaskDrawerMenu, openTaskDrawer,
  closeTaskDrawer
}){

function closeMoreMenu(){const dd=document.getElementById('moreDropdown');if(dd)dd.classList.remove('open')}
function toggleMoreMenu(e){e.stopPropagation();const dd=document.getElementById('moreDropdown');if(dd)dd.classList.toggle('open')}
function closeAllTaskMenus(){document.querySelectorAll('.task-menu.open').forEach(m=>m.classList.remove('open'))}
function toggleTaskMenu(e){e.stopPropagation();const m=e.currentTarget.closest('.task-menu');if(!m)return;const wasOpen=m.classList.contains('open');closeAllTaskMenus();closeMoreMenu();if(!wasOpen)m.classList.add('open')}
function onTaskRowClick(e,tid){if(e.target.closest('.task-menu'))return;if(window.getSelection&&window.getSelection().toString())return;openTaskDrawer(tid)}
document.addEventListener('click',e=>{
  const dd=document.getElementById('moreDropdown');
  if(dd&&!dd.contains(e.target))dd.classList.remove('open');
  if(!e.target.closest('.switcher-wrap'))closeSpaceMenu();
  if(!e.target.closest('.task-menu'))closeAllTaskMenus();
  if(!e.target.closest('.task-drawer-menu-wrap'))closeTaskDrawerMenu();
  if(!e.target.closest('.linear-select'))closeLinearSelectMenu();
});
document.addEventListener('keydown',e=>{
  if((e.metaKey||e.ctrlKey)&&e.key.toLowerCase()==='b'){
    e.preventDefault();
    if(typeof toggleSidebar==='function')toggleSidebar();
    return;
  }
  if(e.key==='Escape'){
    const modal=document.getElementById('modal');
    if(modal&&modal.classList.contains('open')){closeModal();return}
    const dm=document.getElementById('taskDrawerMenu');
    if(dm&&dm.classList.contains('open')){closeTaskDrawerMenu();return}
    const td=document.getElementById('taskDrawer');
    if(td&&!td.hidden){closeTaskDrawer();return}
    closeMoreMenu();
    closeSpaceMenu();
    closeAllTaskMenus();
    closeLinearSelectMenu();
    const d=document.getElementById('deepDrawer');
    if(d&&!d.classList.contains('collapsed')){
      d.classList.add('collapsed');
      const t=document.getElementById('drawerToggleText');
      if(t)t.textContent='▲ 展开抽屉';
    }
  }
});
function showConfirmModal({title,message,confirmText='确认',danger=false,onConfirm}){
  openModal(title,`<div style="line-height:1.6"><div style="font-size:14px;margin-bottom:18px;color:var(--text)">${esc(message)}</div><div style="display:flex;justify-content:flex-end;gap:8px"><button class="btn" onclick="closeModal()">取消</button><button id="modalConfirmBtn" class="btn ${danger?'danger-btn':'primary'}">${esc(confirmText)}</button></div></div>`);
  const btn=document.getElementById('modalConfirmBtn');
  if(btn)btn.onclick=async()=>{closeModal();if(onConfirm)await onConfirm()}
}
function showPromptModal({title,label,defaultValue='',confirmText='确定',onConfirm}){
  openModal(title,`<div class="form"><label for="promptInput">${esc(label)}</label><input id="promptInput" value="${esc(defaultValue)}" autofocus><div style="display:flex;justify-content:flex-end;gap:8px;margin-top:8px"><button class="btn" onclick="closeModal()">取消</button><button id="modalPromptBtn" class="btn primary">${esc(confirmText)}</button></div></div>`);
  const input=document.getElementById('promptInput');
  if(input){input.focus();input.select();input.addEventListener('keydown',e=>{if(e.key==='Enter')document.getElementById('modalPromptBtn')?.click()})};
  const btn=document.getElementById('modalPromptBtn');
  if(btn)btn.onclick=async()=>{const val=input?input.value.trim():'';closeModal();if(onConfirm)await onConfirm(val)}
}
function friendlyErrorChinese(raw){
  const msg=String(raw||'').trim();
  if(!msg)return '操作失败，请重试';
  if(/pre-commit script failed/i.test(msg)||/husky.*failed/i.test(msg)){
    if(/check-node-version-sources/i.test(msg)||/Node 版本/i.test(msg)){
      return '代码提交前门禁失败：检测到 Node 版本声明不一致（或存在临时测试脚本干扰）。\n\n' + msg;
    }
    if(/ArchitectureTest/i.test(msg)||/architecture check failed/i.test(msg)){
      return '代码提交前门禁失败：后端架构规范检查未通过。\n\n' + msg;
    }
    return '代码提交前预检门禁（Git Hooks）拦截：请检查提交内容是否符合仓库门禁约束。\n\n' + msg;
  }
  if(/git_index_lock/i.test(msg)||/Unable to create.*index\.lock/i.test(msg)){
    return 'Git 索引被其他操作锁定中，请稍候几秒后重试。\n\n' + msg;
  }
  if(/merge conflict|conflict/i.test(msg)){
    return '分支合并发生代码冲突，请核对分支基线。\n\n' + msg;
  }
  if(/命令超时|timeout/i.test(msg)){
    return '操作执行超时，可能由于后台进程负载过高或正在编译。\n\n' + msg;
  }
  return msg;
}
let toastTimer=null;
function toast(m,b=false){
  const e=document.getElementById('toast');
  if(!e)return;
  if(toastTimer){clearTimeout(toastTimer);toastTimer=null;}
  const displayMsg=b?friendlyErrorChinese(m):String(m||'');
  e.className='toast show '+(b?'danger-text danger-toast':'good-text');
  e.innerHTML='<div class="toast-msg">'+esc(displayMsg)+'</div>'+(b?'<button class="toast-close" title="点击关闭" onclick="closeToast()">×</button>':'');
  if(b){
    // 错误信息不自动消失，等待用户查看、复制并在点击后再消失
    e.onclick=function(evt){if(evt.target===e)closeToast();};
  }else{
    e.onclick=null;
    toastTimer=setTimeout(closeToast,4200);
  }
}
function closeToast(){
  const e=document.getElementById('toast');
  if(e){e.classList.remove('show');e.className='toast';}
  if(toastTimer){clearTimeout(toastTimer);toastTimer=null;}
}
function openModal(t,h){document.getElementById('modalTitle').textContent=t;document.getElementById('modalBody').innerHTML=h;document.getElementById('modal').classList.add('open')}function closeModal(){document.getElementById('modal').classList.remove('open')}
return {
  closeMoreMenu, toggleMoreMenu, closeAllTaskMenus, toggleTaskMenu,
  onTaskRowClick, showConfirmModal, showPromptModal, toast,
  closeToast, openModal, closeModal
};
})({
  esc, closeLinearSelectMenu:(...args)=>closeLinearSelectMenu(...args), toggleSidebar:(...args)=>toggleSidebar(...args),
  closeSpaceMenu:(...args)=>closeSpaceMenu(...args), closeTaskDrawerMenu:(...args)=>closeTaskDrawerMenu(...args), openTaskDrawer:(...args)=>openTaskDrawer(...args),
  closeTaskDrawer:(...args)=>closeTaskDrawer(...args)
});
