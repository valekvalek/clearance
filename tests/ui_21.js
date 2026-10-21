const fs = require('fs'), vm = require('vm');
const code = fs.readFileSync(process.argv[2], 'utf8').match(/<script>([\s\S]*)<\/script>/)[1];
const pv = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const els = {}; const el = () => ({ innerHTML:'', textContent:'', style:{}, disabled:false,
  classList:{ _h:true, add(c){ if(c==='hide') this._h=true; }, remove(c){ if(c==='hide') this._h=false; }, toggle(){} } });
const ctx = { document:{ querySelector:s => (els[s] ||= el()), addEventListener(){} },
  fetch: async (u) => ({ ok:true, json: async () => u.startsWith('/api/preview') ? pv : {} }),
  location:{}, console, URL, setInterval(){}, clearInterval(){} };
vm.createContext(ctx);
vm.runInContext(code + ';globalThis.__t={showResult, renderWarnings, again:()=>$("#again").onclick()}', ctx);
(async () => {
  await ctx.__t.showResult({rate:''});
  const b = els['#warnBox'];
  console.log('заголовок:', els['#doneText'].textContent, '| цвет:', els['#doneTitle'].style.color);
  console.log('блок виден:', !b.classList._h);
  console.log('блок:', b.innerHTML.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim());
  console.log('строк в таблице:', (els['#tbl'].innerHTML.match(/<tr>/g) || []).length - 1);
  ctx.__t.again();
  console.log('после «Новый файл»: блок скрыт:', b.classList._h, '| заголовок:', els['#doneText'].textContent);
  ctx.__t.renderWarnings(['<img src=x onerror=alert(1)>']);
  console.log('XSS в тексте предупреждения экранирован:', !b.innerHTML.includes('<img'));
})();
