globalThis.window = globalThis; globalThis.sessionStorage = { _m: {}, getItem(k){return this._m[k]??null}, setItem(k,v){this._m[k]=String(v)}, removeItem(k){delete this._m[k]}, key(i){return Object.keys(this._m)[i]}, get length(){return Object.keys(this._m).length} };
globalThis.localStorage = { _m: {}, getItem(k){return this._m[k]??null}, setItem(k,v){this._m[k]=String(v)}, removeItem(k){delete this._m[k]}, key(i){return Object.keys(this._m)[i]}, get length(){return Object.keys(this._m).length} };
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
const src = readFileSync(fileURLToPath(new URL('./nomad-plugin-runtime.js', import.meta.url)), 'utf8');
const { PluginInstance } = await import('data:text/javascript;base64,' + Buffer.from(src).toString('base64'));
let pass = 0, fail = 0;
const ok = (name, c) => { c ? pass++ : fail++; console.log((c ? 'PASS ' : 'FAIL ') + name); };
const man = { manifest_version: 3, name: 'probe', version: '1', permissions: ['tabs', 'storage'], content_scripts: [{ matches: ['https://a.com/*'], js: ['c.js'] }] };
const sent = [];
const host = { sendTabMessage: async (id, tab, msg, o) => { sent.push({ id, tab, msg, o }); return { echo: msg }; }, queryActiveTab: async () => null };
const p = new PluginInstance(man, 'https://x/', host);
const c = p.makeChrome('background');
ok('runtime version 1.0.1', c.__nomad.runtimeVersion === '1.0.1');
ok('tabs.sendMessage 经 host.sendTabMessage 转发', JSON.stringify(await c.tabs.sendMessage(3, 'hi', { frameId: 2, documentId: 'd1' })) === '{"echo":"hi"}' && sent[0].o.frameId === 2 && sent[0].o.documentId === 'd1');
let cbErr = null, cbVal = 'x';
await new Promise((r) => new PluginInstance(man, 'https://x/', {}).makeChrome('bg').tabs.sendMessage(1, 'm', (v) => { cbVal = v; r(); }));
const c2 = new PluginInstance(man, 'https://x/', {}).makeChrome('bg');
await new Promise((r) => c2.tabs.sendMessage(1, 'm', (v) => { cbErr = c2.runtime.lastError; cbVal = v; r(); }));
ok('宿主没接通道: 回调里 runtime.lastError 有值, 结果 undefined', cbErr && /sendTabMessage/.test(cbErr.message) && cbVal === undefined);
ok('回调结束后 lastError 清空', c2.runtime.lastError === undefined);
let rejected = false; try { await c2.tabs.sendMessage(1, 'm'); } catch (e) { rejected = true; }
ok('Promise 形式如实 reject', rejected);
ok('tabs.query 空活动标签返回 []', JSON.stringify(await c.tabs.query({})) === '[]');
p.events.onMessage.addListener(() => { throw new Error('boom'); });
const real = { runtime: { id: 'abcdefghijklmnop' } };
ok('原生 chrome 原样交回', p.makeChrome('bg', real) === real);
let threw = false; try { new PluginInstance(man, 'https://x/', { executionMode: 'native' }).makeChrome('bg'); } catch (e) { threw = true; }
ok('native 模式缺真 chrome 时拒绝', threw);
threw = false; try { await new PluginInstance({ ...man, background: { service_worker: 'sw.js' } }, 'https://x/', { executionMode: 'native' }).startBackground(); } catch (e) { threw = /拒绝再启动/.test(e.message); }
ok('native 模式 startBackground 拒绝启动 shim', threw);
const probs = new PluginInstance(man, 'https://x/', {}).hostCapabilityProblems();
ok('宿主能力诊断 HOST_CAPABILITY_MISSING', probs.length === 3 && probs.every((x) => x.code === 'HOST_CAPABILITY_MISSING'));
console.log(`\n${pass} PASS / ${fail} FAIL`);
process.exit(fail ? 1 : 0);
