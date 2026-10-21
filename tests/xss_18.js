const fs = require('fs'), vm = require('vm');
const html = fs.readFileSync(process.argv[2], 'utf8');
const code = html.match(/<script>([\s\S]*)<\/script>/)[1];
const els = {};
const el = () => ({ innerHTML:'', textContent:'', value:'', style:{}, disabled:false, checked:false,
  classList:{add(){},remove(){},toggle(){}}, appendChild(c){ this.innerHTML += c.innerHTML; } });
const EVIL = `x'");alert(1);//<img src=x onerror=alert(2)>`;
const data = {
  '/api/me': {username:EVIL, role:'admin', is_buyer:true},
  '/api/admin/users': {users:[{username:EVIL, role:'user', groups:[]}, {username:`a"b`, role:'user', groups:['Закупщик']}]},
  '/api/admin/activity': {users:[{username:EVIL, role:`<b>r`, groups:[], runs:1, results:1, last_run:0}]},
  '/api/preview/J': {preview:[
    {name_ru:EVIL, url:'javascript:alert(3)', photo:'javascript:alert(4)', qty:1, unit:'шт', total_usd:1,
     src:{china_url:'data:text/html,<script>alert(5)</script>', supplier:EVIL}},
    {name_ru:'ok', url:'https://ex.com/a"onmouseover="alert(6)', photo:'/api/img/abc123/0', qty:2, unit:'шт', total_usd:2, src:{china_url:'https://s.1688.com/x?q=1'}},
    {name_ru:'ok2', url:' JavaScript:alert(7)', photo:'//evil.com/x.png', qty:1, unit:'', total_usd:0}]},
};
const ctx = {
  document:{ querySelector:s => (els[s] ||= el()), addEventListener(){}, createElement:() => el() },
  fetch: async (u) => ({ ok:true, json: async () => data[u] ?? {} }),
  location:{}, setInterval(){}, clearInterval(){}, alert(){}, confirm:()=>false, prompt:()=>null,
  console, URL, encodeURIComponent, FormData:class{append(){}},
};
vm.createContext(ctx);
vm.runInContext(code + `;globalThis.__t={loadUsers, showResult, statUser, setJob:v=>{jobId=v}, btnStats:()=>$('#btnStats').onclick()}`, ctx);
(async () => {
  const t = ctx.__t;
  await new Promise(r => setTimeout(r, 10));        // loadMe()
  await t.loadUsers(); await t.btnStats(); t.setJob('J'); await t.showResult({rate:''});
  const out = ['#who', '#userList', '#statsBody', '#tbl'].map(s => els[s].innerHTML).join('\n');
  fs.writeFileSync(process.argv[3], out);
})();
