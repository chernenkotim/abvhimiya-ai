'use strict';
const $ = id => document.getElementById(id);
function render(container, data) {
  container.replaceChildren();
  for(const item of data){
    const box=document.createElement('div');box.className='entry';
    const txt=document.createElement('pre');txt.textContent=JSON.stringify(item,null,2);box.append(txt);container.append(box);
  }
}
$('load').addEventListener('click',async()=>{
  $('error').textContent='';
  const token=$('token').value.trim();if(!token){$('error').textContent='Введіть ADMIN_TOKEN';return;}
  try{
    const fetchAdmin=async path=>{const r=await fetch(path,{headers:{Authorization:'Bearer '+token}});if(!r.ok)throw Error(`Помилка ${r.status}`);return r.json();};
    const [leads,logs]=await Promise.all([fetchAdmin('/admin/leads'),fetchAdmin('/admin/logs')]);
    render($('leads'),leads.leads);render($('logs'),logs.logs);
  }catch(e){$('error').textContent=e.message;}
});
$('clear').addEventListener('click',()=>{$('token').value='';$('leads').replaceChildren();$('logs').replaceChildren();});
