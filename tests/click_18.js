const fs = require('fs'), vm = require('vm');
const code = fs.readFileSync(process.argv[2], 'utf8').match(/<script>([\s\S]*)<\/script>/)[1];
const el = () => ({ innerHTML:'', textContent:'', style:{}, classList:{add(){},remove(){},toggle(){}} });
let handler, calls = [];
const ctx = { document:{ querySelector:() => el(), addEventListener:(t, h) => { handler = h; } },
  fetch: async () => ({ ok:true, json: async () => ({}) }), location:{}, console, URL };
vm.createContext(ctx);
vm.runInContext(code + `;statUser=u=>__c.push(['stat',u]);setBuyer=(u,v)=>__c.push(['buyer',u,v]);resetPass=u=>__c.push(['reset',u]);delUser=u=>__c.push(['del',u]);`, Object.assign(ctx, {__c: calls}));
const NAME = `x'");alert(1)`;
const btn = ds => ({ closest: () => ({ dataset: ds }) });
handler({ target: btn({act:'stat', user:NAME}) });
handler({ target: btn({act:'buyer', user:NAME, val:'true'}) });
handler({ target: btn({act:'buyer', user:'ivan', val:'false'}) });
handler({ target: btn({act:'reset', user:NAME}) });
handler({ target: btn({act:'del', user:NAME}) });
handler({ target: { closest: () => null } });   // клик мимо кнопок
const exp = [['stat',NAME],['buyer',NAME,true],['buyer','ivan',false],['reset',NAME],['del',NAME]];
console.log(JSON.stringify(calls) === JSON.stringify(exp) ? '✓ клики: stat/buyer(true,false)/reset/del вызываются с точным именем' : '✗ ' + JSON.stringify(calls));
