'use strict';

const EXECUTION_MODES = ['shim', 'native'];

function isNativeChrome(c) {
  return !!(c && typeof c === 'object' && c.runtime && typeof c.runtime.id === 'string'
    && c.runtime.id && !c.__nomad);
}

function pf(m, name) {

  if (m.nomad && typeof m.nomad === 'object' && m.nomad[name] !== undefined) {
    return m.nomad[name];
  }

  if (m[`nomad.${name}`] !== undefined) return m[`nomad.${name}`];

  if (m[`nomad_${name}`] !== undefined) return m[`nomad_${name}`];

  return m[`arupa_${name}`];
}

const SURFACE_KINDS = [
  'side_panel', 'toolbar_action', 'context_menu', 'home_panel', 'search_provider',
  'protocol_handler',
  'error_page',
  'profile_card',
  'files',
  'tool_sheet',
];

const PLATFORMS = ['desktop', 'android', 'ios'];

const SURFACE_REQUIRED = {
  side_panel:       ['path'],
  home_panel:       ['path'],
  error_page:       ['path'],
  profile_card:     ['path'],
  files:            ['path'],
  tool_sheet:       ['path'],
  search_provider:  ['search_url'],
  protocol_handler: ['protocol'],

};

const SURFACE_PLATFORMS = {
  protocol_handler: ['desktop'],
  files: ['android', 'ios'],
  tool_sheet: ['android'],

};

const ERROR_KINDS = ['dns', 'connection', 'timeout', 'tls', 'blocked', 'other'];

const RUNTIME_VERSION = '1.0.1';

const ROUTE_SCOPES = ['global', 'tab', 'domain', 'profile'];
const ROUTE_FALLBACKS = ['closed'];

const ROUTE_VERDICT_NAME = {
  0: 'ALLOW',
  1: 'ALLOW_NEEDS_CONSENT',
  2: 'DENY_SCOPE_REQUIRES_SIGNATURE',
  3: 'DENY_NOT_DECLARED',
  4: 'DENY_UNKNOWN',
};

const ENDPOINT_RE = /^(socks5h?|https?):\/\/([A-Za-z0-9.\-]+|\[[0-9A-Fa-f:]+\]):(\d{1,5})$/;

const BYPASS_ALREADY_IMPLICIT = new Set([
  '<local>', '<-loopback>', 'localhost', '127.0.0.1', '[::1]', '::1',
]);

const DOMAIN_RE = /^(\*\.|\.)?([A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?\.)+[A-Za-z]{2,}$/;

function validateRouteSpec(spec) {
  const s = spec || {};
  if (!ROUTE_SCOPES.includes(s.scope)) {
    throw new Error(`[nomad.route] scope 必须是 ${ROUTE_SCOPES.join(' / ')} —— 收到 ${JSON.stringify(s.scope)}`);
  }
  const fallback = s.fallback === undefined ? 'closed' : s.fallback;
  if (!ROUTE_FALLBACKS.includes(fallback)) {
    throw new Error(`[nomad.route] fallback 只能是 ${ROUTE_FALLBACKS.join(' / ')} —— 收到 ${JSON.stringify(s.fallback)}`);
  }
  if (typeof s.endpoint !== 'string' || !ENDPOINT_RE.test(s.endpoint)) {
    throw new Error(`[nomad.route] endpoint 形状不对(要 socks5://host:port, 端口必填) —— 收到 ${JSON.stringify(s.endpoint)}`);
  }
  const port = Number(ENDPOINT_RE.exec(s.endpoint)[3]);
  if (!(port >= 1 && port <= 65535)) {
    throw new Error(`[nomad.route] endpoint 端口越界: ${port}`);
  }
  const out = { scope: s.scope, endpoint: s.endpoint, fallback };

  if (s.scope === 'tab') {
    if (!Number.isInteger(s.tabId)) {
      throw new Error('[nomad.route] scope=tab 必须给整数 tabId');
    }
    out.tabId = s.tabId;
  } else if (s.scope === 'profile') {
    if (typeof s.profileId !== 'string' || !s.profileId.trim()) {
      throw new Error('[nomad.route] scope=profile 必须给非空 profileId');
    }
    out.profileId = s.profileId.trim();
  } else if (s.scope === 'domain') {
    const list = Array.isArray(s.domains) ? s.domains : null;
    if (!list || !list.length) {
      throw new Error('[nomad.route] scope=domain 必须给非空 domains 数组');
    }
    const bad = list.filter((d) => typeof d !== 'string' || !DOMAIN_RE.test(d.trim()));
    if (bad.length) {

      throw new Error(`[nomad.route] domains 里有 ${bad.length} 条格式非法 ⇒ **整份配置作废**(fail-closed): ${JSON.stringify(bad.slice(0, 3))}`);
    }
    const set = new Set();
    list.forEach((d) => {
      const one = d.trim().replace(/^\./, '');
      set.add(one);

      if (!one.startsWith('*.')) set.add(`*.${one}`);
    });
    out.domains = [...set];
  }
  return out;
}

function notSupported(api) {
  return () => Promise.reject(new Error(`[nomad] 宿主未提供能力: ${api}`));
}

function bindEvent(ev) {
  return {
    addListener: (fn) => ev.addListener(fn),
    removeListener: (fn) => ev.removeListener(fn),
    hasListener: (fn) => ev.hasListener(fn),
  };
}

function assertPerm(manifest, perm, api) {
  const declared = new Set((manifest && manifest.permissions) || []);
  if (!declared.has(perm)) {
    throw new Error(`[nomad] ${api} 需要在清单 permissions 里声明 "${perm}" —— 当前未声明, 拒绝调用`);
  }
}

function assertAnyPerm(manifest, perms, api) {
  const declared = new Set((manifest && manifest.permissions) || []);
  if (!perms.some((x) => declared.has(x))) {
    throw new Error(`[nomad] ${api} 需要在清单 permissions 里声明 `
      + `${perms.map((x) => `"${x}"`).join(' 或 ')} —— 当前都没有, 拒绝调用`);
  }
}

const VIA_CHROME_PROXY = Symbol('nomad.route: 调用来自 chrome.proxy 转译层');

function assertRoutePerm(manifest, api, via) {
  if (via === VIA_CHROME_PROXY) {
    assertAnyPerm(manifest, ['nomad.route', 'proxy'], api);
    return;
  }
  assertPerm(manifest, 'nomad.route', api);
}

function installChromeProxyShim(chromeObj, nomadObj, pluginId) {
  if (!chromeObj || !nomadObj || !nomadObj.route) return;
  if (chromeObj.proxy) return;

  const onError = new EventSource('proxy.onProxyError');

  nomadObj.route.onError.addListener((e) => {
    onError.emit({ fatal: (e && e.trafficNow) === 'blocked', error: (e && e.kind) || 'other',
                   details: (e && e.message) || '' });
  });

  const oneServer = (s) => {
    const scheme = (s.scheme || 'http').toLowerCase();
    const port = s.port || (scheme === 'socks5' || scheme === 'socks' ? 1080 : 8080);
    return `${scheme === 'socks' ? 'socks5' : scheme}://${s.host}:${port}`;
  };

  const toEndpoint = (value) => {
    const v = value || {};
    if (v.mode === 'direct' || v.mode === 'system' || v.mode === 'auto_detect') return null;
    if (v.mode === 'pac_script') {

      throw new Error('[chrome.proxy] 暂不支持 pac_script（按请求逐条判定，对外文档标为规划中）'
        + ' —— 请改用 nomad.route 的 domain 作用域');
    }
    if (v.mode !== 'fixed_servers') {
      throw new Error(`[chrome.proxy] 不认识的 mode: ${JSON.stringify(v.mode)}`);
    }
    const r = v.rules || {};

    const bypass = Array.isArray(r.bypassList) ? r.bypassList : [];
    const realBypass = bypass.filter(
      (b) => !BYPASS_ALREADY_IMPLICIT.has(String(b || '').trim().toLowerCase()));
    if (realBypass.length) {
      throw new Error('[chrome.proxy] rules.bypassList 表达的是"这些域名不走代理", '
        + `而转译后的 global 作用域是**全量走代理** —— 内核无法表达这份排除名单, `
        + `所以**整份配置作废**而不是悄悄丢掉它(丢掉的后果是 ${JSON.stringify(realBypass.slice(0, 3))} `
        + '这些流量照样进了你的代理)。要分流请改用 nomad.route 的 domain 作用域, '
        + '它是**白名单**语义: 列出要走代理的域名, 其余直连。');
    }

    const servers = [r.singleProxy, r.proxyForHttp, r.proxyForHttps, r.proxyForFtp,
                     r.fallbackProxy].filter((x) => x && x.host);
    if (!servers.length) throw new Error('[chrome.proxy] rules 里没有可用的代理服务器');
    const distinct = [...new Set(servers.map(oneServer))];
    if (distinct.length > 1) {
      throw new Error(`[chrome.proxy] rules 里给了 ${distinct.length} 个不同的出口 `
        + `(${JSON.stringify(distinct)}) —— 转译后的 global 作用域只有一条出口, `
        + '按协议分流表达不了。**整份配置作废**而不是替你挑一个(挑错的那部分流量会走'
        + '你没预期的出口)。请只给一个出口, 或改用 nomad.route。');
    }
    return distinct[0];
  };

  chromeObj.proxy = {
    settings: {
      async set(details) {
        const endpoint = toEndpoint(details && details.value);
        if (endpoint === null) {

          return nomadObj.route.clear({ scope: 'global' }, VIA_CHROME_PROXY);
        }

        return nomadObj.route.set({ scope: 'global', endpoint }, VIA_CHROME_PROXY);
      },
      async get() {
        const cur = await nomadObj.route.get({ scope: 'global' }, VIA_CHROME_PROXY);
        const mine = !!(cur && cur.pluginId === pluginId);
        return {
          value: cur && cur.endpoint
            ? { mode: 'fixed_servers', rules: { singleProxy: parseEndpoint(cur.endpoint) } }
            : { mode: 'direct' },
          levelOfControl: !cur || !cur.endpoint
            ? 'controllable_by_this_extension'
            : (mine ? 'controlled_by_this_extension' : 'controlled_by_other_extensions'),
        };
      },
      async clear() { return nomadObj.route.clear({ scope: 'global' }, VIA_CHROME_PROXY); },
    },
    onProxyError: bindEvent(onError),
  };
}

function parseEndpoint(endpoint) {
  const m = ENDPOINT_RE.exec(endpoint || '');
  if (!m) return null;
  return { scheme: m[1], host: m[2], port: Number(m[3]) };
}

async function checkRouteWithKernel(host, pluginId, scope) {
  const guard = host && host.guard;
  if (!guard || typeof guard.checkRoute !== 'function') {
    return {
      allowed: false, unchecked: true, verdict: null,
      verdictName: '未校验',
      reason: '宿主没提供 host.guard.checkRoute ⇒ 按拒处理（设出口不许在未经内核判定时放行）',
    };
  }
  try {
    const v = await guard.checkRoute(pluginId, scope);
    const code = typeof v === 'number' ? v : (v && typeof v.verdict === 'number' ? v.verdict : 4);
    return {
      allowed: code <= 1,
      needsConsent: code === 1,
      unchecked: false,
      verdict: code,
      verdictName: ROUTE_VERDICT_NAME[code] || String(code),
      reason: (v && v.reason) || null,
    };
  } catch (e) {

    return {
      allowed: false, unchecked: false, verdict: null,
      verdictName: `护栏不可达(${e && e.message})`,
      reason: '问不到判定，按拒处理',
    };
  }
}

class PluginStorageArea {
  constructor(pluginId, kind) {
    this._prefix = `nomad.plugin.${pluginId}.${kind}.`;
    this._backing = kind === 'session' ? window.sessionStorage : window.localStorage;
    this._listeners = [];
  }
  _key(k) { return this._prefix + k; }

  async get(keys) {
    const out = {};
    const pick = (k, dflt) => {
      const raw = this._backing.getItem(this._key(k));
      if (raw === null) { if (dflt !== undefined) out[k] = dflt; return; }
      try { out[k] = JSON.parse(raw); } catch { out[k] = raw; }
    };
    if (keys === null || keys === undefined) {
      for (let i = 0; i < this._backing.length; i++) {
        const full = this._backing.key(i);
        if (full && full.startsWith(this._prefix)) pick(full.slice(this._prefix.length));
      }
    } else if (typeof keys === 'string') { pick(keys); }
    else if (Array.isArray(keys)) { keys.forEach((k) => pick(k)); }
    else if (typeof keys === 'object') { Object.entries(keys).forEach(([k, d]) => pick(k, d)); }
    return out;
  }

  async set(obj) {
    const changes = {};
    for (const [k, v] of Object.entries(obj || {})) {
      const oldRaw = this._backing.getItem(this._key(k));
      this._backing.setItem(this._key(k), JSON.stringify(v));
      changes[k] = { newValue: v, oldValue: oldRaw === null ? undefined : safeParse(oldRaw) };
    }
    this._emit(changes);
  }

  async remove(keys) {
    const list = Array.isArray(keys) ? keys : [keys];
    const changes = {};
    list.forEach((k) => {
      const oldRaw = this._backing.getItem(this._key(k));
      this._backing.removeItem(this._key(k));
      changes[k] = { oldValue: oldRaw === null ? undefined : safeParse(oldRaw) };
    });
    this._emit(changes);
  }

  async clear() { this.wipe(); }

  wipe() {
    const doomed = [];
    for (let i = 0; i < this._backing.length; i++) {
      const full = this._backing.key(i);
      if (full && full.startsWith(this._prefix)) doomed.push(full);
    }
    doomed.forEach((k) => this._backing.removeItem(k));
  }

  onChanged_addListener(fn) { this._listeners.push(fn); }
  _emit(changes) { this._listeners.forEach((fn) => { try { fn(changes); } catch (e) { console.error(e); } }); }
}

function safeParse(s) { try { return JSON.parse(s); } catch { return s; } }

class EventSource {
  constructor(name) { this._name = name; this._fns = []; }
  addListener(fn) { if (typeof fn === 'function') this._fns.push(fn); }
  removeListener(fn) { this._fns = this._fns.filter((f) => f !== fn); }
  hasListener(fn) { return this._fns.includes(fn); }
  get listenerCount() { return this._fns.length; }

  emit(...args) {
    this._fns.forEach((fn) => { try { fn(...args); } catch (e) { console.error(`[${this._name}]`, e); } });
  }

  emitWithResponse(message, sender) {
    return new Promise((resolve) => {
      let settled = false;
      let willRespondAsync = false;
      const sendResponse = (resp) => { if (!settled) { settled = true; resolve(resp); } };
      for (const fn of this._fns) {
        try {
          const ret = fn(message, sender, sendResponse);
          if (ret === true) willRespondAsync = true;
          else if (ret && typeof ret.then === 'function') {
            willRespondAsync = true;
            ret.then(sendResponse).catch(() => sendResponse(undefined));
          }
        } catch (e) { console.error(`[${this._name}]`, e); }
      }
      if (!willRespondAsync && !settled) { settled = true; resolve(undefined); }
    });
  }
}

const NOMAD_PRIVILEGED_COMMANDS = Object.freeze(['route', 'transport', 'identity']);

const EXT_NAME_RE = /^[a-z][a-zA-Z0-9]{1,31}$/;

const _extRegistry = new Map();

let _kernelCommandCache = null;

function nomadKernelCommands() {
  if (_kernelCommandCache) return _kernelCommandCache;
  const probe = new PluginInstance(
    { manifest_version: 3, name: '__kernel_probe__', version: '0.0.0' },
    'about:blank', {});
  _kernelCommandCache = Object.freeze(
    Object.keys(probe._makeKernelNomad('background')).sort());
  return _kernelCommandCache;
}

function registerNomadCommand(name, impl, opts) {
  const o = opts || {};
  const n = typeof name === 'string' ? name : '';

  if (!EXT_NAME_RE.test(n)) {
    throw new Error(`[nomad.ext] 指令名不合法: ${JSON.stringify(name)} —— `
      + '要小写字母开头的驼峰、2–32 字符(例: reader / quickNote)');
  }

  if (NOMAD_PRIVILEGED_COMMANDS.includes(n)) {
    throw new Error(`[nomad.ext] "${n}" 是特权轨(T4)指令, **不许从 JS 扩展** —— `
      + '出口路由 / 传输 / 身份必须由内核执行并经签名 ABI 判定; '
      + `当前特权名单: ${NOMAD_PRIVILEGED_COMMANDS.join(' / ')}`);
  }

  if (nomadKernelCommands().includes(n)) {
    throw new Error(`[nomad.ext] "${n}" 是内核指令, JS 扩展不许覆盖它 —— `
      + '内核指令优先是硬规则(否则特权指令能被 JS 顶掉)。'
      + '要扩展它的行为请换一个名字。');
  }

  const okImpl = typeof impl === 'function'
    || (impl && typeof impl === 'object' && !Array.isArray(impl));
  if (!okImpl) {
    throw new Error(`[nomad.ext] "${n}" 的实现必须是对象或函数 —— `
      + `收到 ${impl === null ? 'null' : typeof impl}`);
  }

  if (_extRegistry.has(n) && o.replace !== true) {
    const prev = _extRegistry.get(n);
    throw new Error(`[nomad.ext] "${n}" 已由 ${prev.owner || '(未署名)'} 注册过 —— `
      + '确实要换掉请显式传 { replace: true }');
  }

  if (o.requiresPermission !== undefined && typeof o.requiresPermission !== 'string') {
    throw new Error(`[nomad.ext] "${n}" 的 requiresPermission 要么不填, 要么是权限名字符串`);
  }

  _extRegistry.set(n, {
    name: n,
    impl,
    owner: typeof o.owner === 'string' ? o.owner : null,
    since: typeof o.since === 'string' ? o.since : null,
    requiresPermission: o.requiresPermission || null,
  });
  return { name: n, unregister: () => unregisterNomadCommand(n) };
}

function unregisterNomadCommand(name) {
  return _extRegistry.delete(String(name));
}

function listNomadCommands() {
  return [..._extRegistry.values()].map((e) => ({
    name: e.name,
    owner: e.owner,
    since: e.since,
    requiresPermission: e.requiresPermission,
    kind: typeof e.impl === 'function' ? 'function' : 'namespace',
  }));
}

function _resetNomadCommands() { _extRegistry.clear(); }

function _guardExtImpl(entry, instance) {
  if (!entry.requiresPermission) return entry.impl;
  const perm = entry.requiresPermission;
  const wrap = (fn, path) => function (...args) {
    assertPerm(instance.manifest, perm, path);
    return fn.apply(this, args);
  };
  if (typeof entry.impl === 'function') return wrap(entry.impl, `nomad.${entry.name}`);
  const out = {};
  for (const k of Object.keys(entry.impl)) {
    const v = entry.impl[k];
    out[k] = typeof v === 'function' ? wrap(v, `nomad.${entry.name}.${k}`) : v;
  }
  return out;
}

class PluginInstance {

  constructor(manifest, baseUrl, host) {
    this.manifest = manifest;
    this.id = pf(manifest, 'plugin_id') || slugify(manifest.name);
    this.baseUrl = baseUrl.endsWith('/') ? baseUrl : baseUrl + '/';
    this.host = host || {};
    this.enabled = false;
    this.executionMode = EXECUTION_MODES.includes(this.host.executionMode)
      ? this.host.executionMode : 'shim';

    this.storage = { local: new PluginStorageArea(this.id, 'local'), session: new PluginStorageArea(this.id, 'session') };
    this.events = {
      onInstalled: new EventSource('runtime.onInstalled'),
      onMessage: new EventSource('runtime.onMessage'),

      routeError: new EventSource('nomad.route.onError'),
      actionClicked: new EventSource('action.onClicked'),
      menuClicked: new EventSource('contextMenus.onClicked'),
    };
    this._menus = [];
    this._panelPath = (manifest.side_panel && manifest.side_panel.default_path) || null;
    this._panelBehavior = { openPanelOnActionClick: false };
    this._bgFrame = null;

    this.platform = (this.host.platform && PLATFORMS.includes(this.host.platform))
      ? this.host.platform : null;

    const sp = this.platform ? surfacesForPlatform(manifest, this.platform)
                             : { active: normalizeSurfaces(manifest), dropped: [] };
    this._surfaces = sp.active;
    this._droppedSurfaces = sp.dropped;
    this._skills = Array.isArray(pf(manifest, 'skills')) ? pf(manifest, 'skills') : [];
  }

  url(path) { return new URL(path, this.baseUrl).href; }

  surfaces() { return this._surfaces; }

  droppedSurfaces() { return this._droppedSurfaces; }
  skills() { return this._skills; }

  _makeKernelNomad(context) {
    const self = this;
    return {
      version: RUNTIME_VERSION,
      context,
      pluginId: self.id,

      ext: {

        list() { return listNomadCommands(); },

        has(name) { return _extRegistry.has(String(name)); },

        kernelCommands() { return nomadKernelCommands().slice(); },
      },

      skills: {

        ui: {

          openPanel(opts) { return { action: 'open_panel', route: (opts && opts.route) || null }; },

          openTab(url) { return { action: 'open_tab', url }; },

          confirm(text, then) { return { action: 'confirm', text, then }; },
        },

        onInvoke(handler) {
          self.events.onMessage.addListener((msg, sender, sendResponse) => {

            if (!msg || (msg.type !== 'NOMAD_SKILL' && msg.type !== 'ARUPA_SKILL')) return undefined;
            const r = handler(msg.skill, msg.args || {});
            if (r && typeof r.then === 'function') { r.then(sendResponse); return true; }
            sendResponse(r);
            return true;
          });
        },

        declared() { return self._skills.slice(); },
      },

      surfaces: {

        declared() { return normalizeSurfaces(self.manifest).map((x) => ({ ...x })); },

        async active() {
          if (!self.host.queryActiveSurfaces) return null;
          return self.host.queryActiveSurfaces(self.id);
        },
      },

      entitlement: {

        async report(state) {
          if (!self.host.reportEntitlement) return notSupported('entitlement.report')();
          const s = state || {};
          return self.host.reportEntitlement(self.id, {
            status: ['active', 'expired', 'none'].includes(s.status) ? s.status : 'none',
            planName: s.planName || null,
            expiresAt: typeof s.expiresAt === 'number' ? s.expiresAt : null,
            quota: s.quota && typeof s.quota === 'object' ? s.quota : null,
            note: s.note || null,
          });
        },

        async current() {
          if (!self.host.queryEntitlement) return null;
          return self.host.queryEntitlement(self.id);
        },
      },

      route: {

        async set(spec, via) {
          assertRoutePerm(self.manifest, 'route.set', via);
          const resolved = validateRouteSpec(spec);
          const v = await checkRouteWithKernel(self.host, self.id, resolved.scope);
          if (!v.allowed) {
            throw new Error(`[nomad.route] scope=${resolved.scope} 被拒(${v.verdictName})`
              + (v.reason ? ` —— ${v.reason}` : '')
              + (v.verdict === 2 ? ' ｜ 未签名的插件只能用 scope=global，'
                                 + '这里**不会**替你降级成 global(那会让你以为分流生效了)' : ''));
          }
          if (v.needsConsent) {

            if (typeof self.host.requestRouteConsent !== 'function') {
              throw new Error('[nomad.route] 内核判定"需用户授权"，而宿主没接 host.requestRouteConsent'
                + ' ⇒ 按拒处理（不许跳过用户授权直接改出口）');
            }
            const ok = await self.host.requestRouteConsent(self.id, resolved);
            if (!ok || ok.granted !== true) {
              throw new Error('[nomad.route] 用户没有同意本插件更改网络出口');
            }
          }
          if (typeof self.host.setTransportRoute !== 'function') return notSupported('route.set')();
          return self.host.setTransportRoute(self.id, resolved);
        },

        async get(query, via) {
          assertRoutePerm(self.manifest, 'route.get', via);
          if (typeof self.host.queryTransportRoute !== 'function') return null;
          return self.host.queryTransportRoute(self.id, query || { scope: 'global' });
        },

        async clear(query, via) {
          assertRoutePerm(self.manifest, 'route.clear', via);
          if (typeof self.host.clearTransportRoute !== 'function') return notSupported('route.clear')();
          return self.host.clearTransportRoute(self.id, query || { scope: 'global' });
        },

        async touch(spec) {
          assertPerm(self.manifest, 'nomad.route', 'route.touch');
          if (typeof self.host.touchTransportRoute !== 'function') return notSupported('route.touch')();
          const s = spec || {};
          if (typeof s.endpoint !== 'string' || !ENDPOINT_RE.test(s.endpoint)) {
            throw new Error(`[nomad.route] touch 要给 endpoint(socks5://host:port) —— 收到 ${JSON.stringify(s.endpoint)}`);
          }
          return self.host.touchTransportRoute(self.id, { endpoint: s.endpoint });
        },

        onError: bindEvent(self.events.routeError),
      },

      errorPage: {

        async context() {
          if (context !== 'error_page') return null;
          if (!self.host.errorContext) return null;
          const c = await self.host.errorContext(self.id);
          if (!c) return null;
          return {
            url: c.url,
            errorKind: ERROR_KINDS.includes(c.errorKind) ? c.errorKind : 'other',
          };
        },

        async retry(opts) {
          if (!self.host.errorRetry) return notSupported('errorPage.retry')();
          return self.host.errorRetry(self.id, { withAcceleration: !!(opts && opts.withAcceleration) });
        },
      },
    };
  }

  makeNomad(context) {
    const nomad = this._makeKernelNomad(context);
    for (const entry of _extRegistry.values()) {
      if (Object.prototype.hasOwnProperty.call(nomad, entry.name)) {
        console.error(`[nomad.ext] "${entry.name}" 与内核指令同名, 已忽略这条 JS 扩展`
          + ' —— 内核指令优先(注册闸本该拦下它, 走到这里说明注册表被绕过了)');
        continue;
      }
      nomad[entry.name] = _guardExtImpl(entry, this);
    }
    return nomad;
  }

  makeChrome(context, nativeChrome) {
    const self = this;
    const log = (lvl, ...a) => (self.host.log ? self.host.log(lvl, self.id, ...a) : console[lvl === 'error' ? 'error' : 'log'](`[${self.id}]`, ...a));

    if (isNativeChrome(nativeChrome)) return nativeChrome;
    if (self.executionMode === 'native') {
      throw new Error(`[nomad] ${self.id}: executionMode=native 但拿不到真 Chromium chrome 对象, 拒绝退化成 shim`);
    }

    const needPerm = (perm, api) => assertPerm(self.manifest, perm, api);

    const needAnyPerm = (perms, api) => assertAnyPerm(self.manifest, perms, api);

    const respond = (p, cb) => {
      p.then((v) => cb(v), (err) => {
        chromeObj.runtime.lastError = { message: String((err && err.message) || err) };
        try { cb(undefined); } finally { chromeObj.runtime.lastError = undefined; }
      });
      return undefined;
    };

    const chromeObj = {
      __nomad: { runtimeVersion: RUNTIME_VERSION, context, pluginId: self.id },
      __arupa: { runtimeVersion: RUNTIME_VERSION, context, pluginId: self.id },

      runtime: {
        id: self.id,
        getURL: (p) => self.url(p),
        getManifest: () => JSON.parse(JSON.stringify(self.manifest)),
        onInstalled: bindEvent(self.events.onInstalled),
        onMessage: bindEvent(self.events.onMessage),

        sendMessage: (msg, cb) => {
          const p = self.events.onMessage.emitWithResponse(msg, { id: self.id, context });
          if (typeof cb === 'function') return respond(p, cb);
          return p;
        },
        lastError: undefined,
      },

      storage: {
        local: bindStorage(self.storage.local, 'storage.local'),
        session: bindStorage(self.storage.session, 'storage.session'),
        onChanged: { addListener: (fn) => { needPerm('storage', 'storage.onChanged'); self.storage.local.onChanged_addListener(fn); self.storage.session.onChanged_addListener(fn); } },
      },

      sidePanel: {
        open: async (opts) => {
          needPerm('sidePanel', 'sidePanel.open');
          if (!self.host.openPanel) return notSupported('sidePanel.open')();
          if (!self._panelPath) throw new Error('[nomad] manifest 未声明 side_panel.default_path');
          return self.host.openPanel(self.id, self.url(self._panelPath), opts || {});
        },
        setOptions: async (opts) => {
          needPerm('sidePanel', 'sidePanel.setOptions');
          if (opts && opts.path) self._panelPath = opts.path;
          if (self.host.setPanelOptions) self.host.setPanelOptions(self.id, opts || {});
        },
        setPanelBehavior: async (b) => { Object.assign(self._panelBehavior, b || {}); },
      },

      contextMenus: {
        create: (item, cb) => {
          needPerm('contextMenus', 'contextMenus.create');
          self._menus.push({ ...item, pluginId: self.id });
          if (self.host.setContextMenus) self.host.setContextMenus(self._menus.slice());
          if (cb) cb();
          return item.id;
        },
        removeAll: (cb) => {
          needPerm('contextMenus', 'contextMenus.removeAll');
          self._menus = [];
          if (self.host.setContextMenus) self.host.setContextMenus([]);
          if (cb) cb();
        },
        onClicked: bindEvent(self.events.menuClicked),
      },

      action: {
        onClicked: bindEvent(self.events.actionClicked),
        setBadgeText: async (o) => self.host.setActionBadge && self.host.setActionBadge(self.id, { text: o && o.text }),
        setTitle: async (o) => self.host.setActionBadge && self.host.setActionBadge(self.id, { title: o && o.title }),
      },

      tabs: {

        create: async (opts) => {
          needPerm('tabs', 'tabs.create');
          if (!self.host.openTab) return notSupported('tabs.create')();
          const url = (opts && opts.url) || 'about:blank';
          if (url !== 'about:blank' && !/^https?:\/\//i.test(String(url))) {
            return Promise.reject(new Error(
              `[nomad] tabs.create 只允许 http/https —— 拒绝: ${url}(不得打开产品内部页)`));
          }
          return self.host.openTab(url, opts || {});
        },

        query: async (queryInfo) => {
          needAnyPerm(['tabs', 'activeTab'], 'tabs.query');
          if (queryInfo && Object.keys(queryInfo).length) {
            log('warn', 'tabs.query 的过滤条件被忽略 —— 本运行时只返回当前活动标签(防画像)');
          }
          const tab = self.host.queryActiveTab ? await self.host.queryActiveTab() : null;
          return tab ? [tab] : [];
        },

        sendMessage: (tabId, msg, optsOrCb, maybeCb) => {
          const cb = typeof optsOrCb === 'function' ? optsOrCb : maybeCb;
          const opts = (optsOrCb && typeof optsOrCb === 'object') ? optsOrCb : {};
          let p;
          try {
            needAnyPerm(['tabs', 'activeTab'], 'tabs.sendMessage');
            if (!Number.isInteger(tabId) || tabId < 0) {
              throw new Error('[nomad] tabs.sendMessage: tabId 必须是非负整数');
            }
            if (!self.host.sendTabMessage) {
              throw new Error('[nomad] 宿主未提供能力: tabs.sendMessage(host.sendTabMessage)');
            }
            const o = {};
            if (Number.isInteger(opts.frameId)) o.frameId = opts.frameId;
            if (typeof opts.documentId === 'string' && opts.documentId) o.documentId = opts.documentId;
            p = Promise.resolve(self.host.sendTabMessage(self.id, tabId, msg, o));
          } catch (e) {
            p = Promise.reject(e);
          }
          if (typeof cb === 'function') return respond(p, cb);
          return p;
        },
      },
    };
    return chromeObj;

    function bindStorage(area, api) {
      const g = (fn) => (...a) => { needPerm('storage', api); return fn(...a); };
      return { get: g((k) => area.get(k)), set: g((o) => area.set(o)),
               remove: g((k) => area.remove(k)), clear: g(() => area.clear()) };
    }
  }

  async startBackground() {
    const swPath = this.manifest.background && this.manifest.background.service_worker;
    if (!swPath) return;

    if (this.executionMode === 'native' || isNativeChrome(typeof chrome !== 'undefined' ? chrome : null)) {
      throw new Error(`[nomad] ${this.id}: 原生 MV3 环境(executionMode=${this.executionMode}), 拒绝再启动 shim background`);
    }
    const code = await (await fetch(this.url(swPath))).text();

    const frame = document.createElement('iframe');
    frame.style.cssText = 'position:absolute;width:0;height:0;border:0;opacity:0;pointer-events:none';
    frame.setAttribute('aria-hidden', 'true');
    document.body.appendChild(frame);
    this._bgFrame = frame;

    const w = frame.contentWindow;
    w.chrome = this.makeChrome('background');
    w.browser = w.chrome;
    w.nomad = this.makeNomad('background');

    installChromeProxyShim(w.chrome, w.nomad, this.id);

    w.__nomadBase = this.baseUrl;
    try {
      w.eval(code);
    } catch (e) {
      console.error(`[${this.id}] background 执行失败:`, e);
      throw e;
    }
  }

  hostCapabilities() {
    const m = this.manifest;
    const perms = [].concat(m.permissions || [], m.optional_permissions || []);
    const need = [];
    if (perms.includes('tabs') || perms.includes('activeTab')) {
      need.push(['openTab', 'tabs.create'], ['sendTabMessage', 'tabs.sendMessage']);
    }
    if (perms.includes('sidePanel') || m.side_panel) need.push(['openPanel', 'sidePanel']);
    if ((m.content_scripts || []).length) need.push(['injectContentScript', 'content_scripts']);
    return need.map(([cap, by]) => ({ capability: cap, needed_by: by, present: typeof this.host[cap] === 'function' }));
  }

  hostCapabilityProblems() {
    return this.hostCapabilities().filter((c) => !c.present).map((c) => {
      const msg = `HOST_CAPABILITY_MISSING: 宿主没有提供 ${c.capability}(${c.needed_by} 需要)`;
      if (this.host.log) this.host.log('warn', this.id, msg);
      return { level: 'warn', code: 'HOST_CAPABILITY_MISSING', capability: c.capability, msg };
    });
  }

  fireInstalled(reason = 'install') { this.events.onInstalled.emit({ reason }); }

  async clickAction(tab) {
    this.events.actionClicked.emit(tab || { id: 1 });

    if (this._panelBehavior.openPanelOnActionClick && this._panelPath && this.host.openPanel) {
      await this.host.openPanel(this.id, this.url(this._panelPath), { tabId: (tab && tab.id) || 1 });
    }
  }

  clickMenu(info, tab) { this.events.menuClicked.emit(info, tab || { id: 1 }); }

  fireRouteError(err) {
    const e = err || {};
    this.events.routeError.emit({
      kind: ERROR_KINDS.includes(e.kind) ? e.kind : 'other',
      scope: ROUTE_SCOPES.includes(e.scope) ? e.scope : null,
      endpoint: typeof e.endpoint === 'string' ? e.endpoint : null,
      message: typeof e.message === 'string' ? e.message : null,

      trafficNow: ['blocked', 'central', 'direct'].includes(e.trafficNow) ? e.trafficNow : null,
    });
  }

  unload() {
    this.storage.local.wipe();
    this.storage.session.wipe();
    this._menus = [];
    if (this.host.setContextMenus) this.host.setContextMenus([]);
    if (this._bgFrame && this._bgFrame.parentNode) this._bgFrame.parentNode.removeChild(this._bgFrame);
    this._bgFrame = null;
    this.enabled = false;
  }
}

class NomadPluginRuntime {
  constructor(host) { this.host = host || {}; this.plugins = new Map(); }

  async load(baseUrl) {
    let base = baseUrl.endsWith('/') ? baseUrl : baseUrl + '/';

    if (typeof location !== 'undefined') base = new URL(base, location.href).href;
    const res = await fetch(base + 'manifest.json');
    if (!res.ok) throw new Error(`读不到 manifest.json (${res.status})`);
    const manifest = await res.json();
    const problems = validateManifest(manifest);

    problems.push(...platformProblems(manifest, this.host.platform).problems);

    const verdicts = await checkPermissionsWithKernel(this.host, manifest.permissions || []);
    verdicts.forEach((v) => {
      if (v.denied) {
        problems.push({
          level: 'error',
          msg: `权限 "${v.permission}" 被内核护栏拒绝(${v.verdictName})`,
          effect: '不加载 —— 这是安全模型的硬线, 不是可配置项',
        });
      } else if (v.unchecked) {
        problems.push({
          level: 'warn',
          msg: `权限 "${v.permission}" **未经内核校验**(宿主没提供 host.guard)`,
          effect: '风险分级没有生效 —— 别把它当成"通过了"',
        });
      }
    });

    const inst = new PluginInstance(manifest, base, this.host);
    inst.problems = problems;
    inst.permissionVerdicts = verdicts;

    inst.blocked = problems.some((p) => p.level === 'error');
    this.plugins.set(inst.id, inst);
    return inst;
  }

  async enable(pluginId) {
    const p = this.plugins.get(pluginId);
    if (!p) throw new Error(`未加载的插件: ${pluginId}`);

    if (p.blocked) {
      const why = (p.problems || []).filter((x) => x.level === 'error').map((x) => x.msg).join('; ');
      throw new Error(`[nomad] 拒绝启用 ${pluginId}: ${why}`);
    }
    p.hostCapabilityIssues = p.hostCapabilityProblems();
    await p.startBackground();
    p.fireInstalled('install');
    if (this.host.registerSkills && p.skills().length) this.host.registerSkills(p.id, p.skills());

    p.contentScripts = await applyContentScripts(p, this.host);
    p.enabled = true;
    return p;
  }

  get(pluginId) { return this.plugins.get(pluginId); }
  list() { return [...this.plugins.values()]; }
  unload(pluginId) { const p = this.plugins.get(pluginId); if (p) { p.unload(); this.plugins.delete(pluginId); } }
}

const PERM_VERDICT_NAME = {
  0: 'ALLOW_MAPPED',
  1: 'ALLOW_LOW_RISK',
  2: 'DENY_PRIVILEGED',
  3: 'DENY_FINGERPRINT_RISK',
  4: 'DENY_UNKNOWN',

  5: 'VENDOR_NAMESPACE',
};

async function checkPermissionsWithKernel(host, permissions) {
  const guard = host && host.guard;
  const out = [];
  for (const permission of permissions) {
    if (!guard || typeof guard.mapPermission !== 'function') {
      out.push({ permission, unchecked: true, denied: false, verdict: null, verdictName: '未校验' });
      continue;
    }
    try {

      const v = await guard.mapPermission(permission);
      const code = typeof v === 'number' ? v : (v && typeof v.verdict === 'number' ? v.verdict : 4);
      out.push({
        permission,
        verdict: code,
        verdictName: PERM_VERDICT_NAME[code] || String(code),

        denied: code >= 2 && code !== 5,
        vendorNamespace: code === 5,
        unchecked: false,
      });
    } catch (e) {

      out.push({
        permission, verdict: null, verdictName: `护栏不可达(${e && e.message})`,
        denied: true, unchecked: false,
      });
    }
  }
  return out;
}

async function applyContentScripts(inst, host) {
  const scripts = (inst.manifest.content_scripts || []);
  if (!scripts.length) return { applied: 0, skipped: 0, reason: '' };
  if (!host || typeof host.injectContentScript !== 'function') {
    return {
      applied: 0, skipped: scripts.length,
      reason: '宿主没有接 injectContentScript(内核隔离世界通道) —— **刻意不降级到主世界注入**',
    };
  }
  let applied = 0;
  for (const s of scripts) {
    await host.injectContentScript(inst.id, {
      matches: s.matches || [],
      js: (s.js || []).map((p) => inst.url(p)),
      css: (s.css || []).map((p) => inst.url(p)),
      runAt: s.run_at || 'document_idle',

      isolated: true,
    });
    applied++;
  }
  return { applied, skipped: 0, reason: '' };
}

function validateManifest(m) {
  const out = [];
  if (m.manifest_version !== 3) out.push({ level: 'error', msg: `manifest_version 必须是 3(当前 ${m.manifest_version})`, effect: '不加载' });
  if (!m.name) out.push({ level: 'error', msg: '缺 name', effect: '不加载' });
  if (!m.version) out.push({ level: 'warn', msg: '缺 version', effect: '无法做升级判断' });

  const perms = m.permissions || [];
  const SUPPORTED = ['storage', 'sidePanel', 'contextMenus', 'tabs', 'activeTab', 'scripting'];
  perms.filter((p) => !SUPPORTED.includes(p)).forEach((p) =>
    out.push({ level: 'warn', msg: `权限 "${p}" 本运行时未实现`, effect: '相关 API 调用会如实报错(不会静默失败)' }));

  if (m.side_panel && !m.side_panel.default_path) out.push({ level: 'error', msg: 'side_panel 缺 default_path', effect: '面板打不开' });
  if (m.background && !m.background.service_worker) out.push({ level: 'warn', msg: 'background 缺 service_worker', effect: '后台逻辑不会运行' });

  const NESTED_KEYS = ['plugin_id', 'entry', 'surfaces', 'skills', 'compat', 'transport'];
  NESTED_KEYS.forEach((k) => {
    const nested = m.nomad && typeof m.nomad === 'object' && m.nomad[k] !== undefined;
    if (nested) return;
    if (m[`nomad.${k}`] !== undefined) {

      out.push({ level: 'warn',
                 msg: `"nomad.${k}" 是**手册示例里的旧形态**(带点的顶层键), 现行写法是嵌套块 nomad.${k}`,
                 effect: '本运行时已兼容读取(2026-09-04 起), 但请改成 "nomad": { "' + k + '": ... } —— '
                       + '手册那几处示例待产品仓订正' });
    } else if (m[`nomad_${k}`] !== undefined) {
      out.push({ level: 'warn', msg: `nomad_${k} 已改为 nomad.${k}(标准 v2.1 嵌套写法)`,
                 effect: '当前仍可用, 后续版本移除 —— 请写成 "nomad": { "' + k + '": ... }' });
    } else if (m[`arupa_${k}`] !== undefined) {
      out.push({ level: 'warn', msg: `arupa_${k} 已改为 nomad.${k}(经 v1.2 的 nomad_${k} 两次更名)`,
                 effect: '当前仍可用, 后续版本移除 —— arupa 是内核命名, 且现行是嵌套写法' });
    }
  });

  (pf(m, 'skills') || []).forEach((s, i) => {
    if (!s.name) out.push({ level: 'error', msg: `nomad_skills[${i}] 缺 name`, effect: 'AI 无法调用该技能' });
    if (!s.description) out.push({ level: 'warn', msg: `nomad_skills[${i}] 缺 description`, effect: 'AI 不知道何时该用它' });

    if (s.parameters !== undefined
        && (typeof s.parameters !== 'object' || s.parameters === null || Array.isArray(s.parameters))) {
      out.push({ level: 'error', msg: `nomad_skills[${i}].parameters 必须是对象(JSON Schema)`,
                 effect: 'AI 解析不出参数, 技能被调用时拿不到任何入参' });
    }
  });

  const surfaces = pf(m, 'surfaces');
  if (surfaces !== undefined) {
    if (!Array.isArray(surfaces)) {
      out.push({ level: 'error', msg: 'nomad_surfaces 必须是数组(标准 v1.2 · D2)',
                 effect: '整块位置声明被忽略, 插件一个位置都占不到' });
    } else {
      surfaces.forEach((s, i) => {
        const at = `nomad_surfaces[${i}]`;
        if (!s || typeof s !== 'object') {
          out.push({ level: 'error', msg: `${at} 不是对象`, effect: '该条被丢弃' });
          return;
        }
        if (!SURFACE_KINDS.includes(s.kind)) {
          out.push({ level: 'error', msg: `${at} 位置 "${s.kind}" 不是标准位置`,
                     effect: `被丢弃, 插件在这个位置上不会出现。标准位置: ${SURFACE_KINDS.join(' / ')}` });
          return;
        }
        (SURFACE_REQUIRED[s.kind] || []).forEach((k) => {
          if (!s[k]) {
            out.push({ level: 'error', msg: `${at}(${s.kind}) 缺 ${k}`,
                       effect: '位置会被占住但打不开 —— 用户看到空白或无反应' });
          }
        });

        if (s.platforms !== undefined) {
          if (!Array.isArray(s.platforms)) {
            out.push({ level: 'error', msg: `${at}.platforms 必须是数组`, effect: '跨端声明失效' });
          } else {
            const bad = s.platforms.filter((x) => !PLATFORMS.includes(x));
            if (bad.length) {
              out.push({ level: 'error', msg: `${at}.platforms 有不认识的平台 [${bad.join(', ')}]`,
                         effect: `跨端声明失效。合法值: ${PLATFORMS.join(' / ')}` });
            }
            const allowed = SURFACE_PLATFORMS[s.kind];
            if (allowed && s.platforms.length && !s.platforms.some((x) => allowed.includes(x))) {
              out.push({ level: 'error', msg: `${at} 位置 "${s.kind}" 只支持 [${allowed.join(', ')}], 但声明给了 [${s.platforms.join(', ')}]`,
                         effect: '该位置在声明的平台上永远不会生效' });
            }
          }
        }
      });
    }
  }

  const compat = pf(m, 'compat');
  if (compat !== undefined) {
    if (!compat || typeof compat !== 'object' || Array.isArray(compat)) {
      out.push({ level: 'error', msg: 'nomad_compat 必须是对象', effect: '跨端降级声明失效' });
    } else {
      const MODES = ['skip', 'warn', 'reject'];
      if (compat.on_missing_surface !== undefined && !MODES.includes(compat.on_missing_surface)) {
        out.push({ level: 'error', msg: `nomad_compat.on_missing_surface 只能是 ${MODES.join(' / ')}`,
                   effect: '端上遇到不支持的位置时按默认(skip)处理, 与你的预期可能相反' });
      }
      if (compat.platforms !== undefined) {
        if (!Array.isArray(compat.platforms)) {
          out.push({ level: 'error', msg: 'nomad_compat.platforms 必须是数组', effect: '平台限定失效' });
        } else {
          const bad = compat.platforms.filter((x) => !PLATFORMS.includes(x));
          if (bad.length) out.push({ level: 'error', msg: `nomad_compat.platforms 有不认识的平台 [${bad.join(', ')}]`,
                                     effect: `平台限定失效。合法值: ${PLATFORMS.join(' / ')}` });
        }
      }
    }
  }

  const transport = pf(m, 'transport');
  if (transport !== undefined) {
    if (!transport || typeof transport !== 'object' || Array.isArray(transport)) {
      out.push({ level: 'error', msg: 'nomad_transport 必须是对象', effect: '加速类声明整块失效' });
    } else {
      const hosts = transport.managed_hosts;
      if (!Array.isArray(hosts) || hosts.length === 0) {
        out.push({ level: 'error', msg: '🔴 nomad_transport.managed_hosts 为空',
                   effect: '**全部流量直连** —— 界面显示"已连接"但没有任何流量走加速线路(泄漏级)' });
      } else {
        const wild = hosts.filter((h) => typeof h === 'string' && /^\*+(\.\*+)*$/.test(h.trim()));
        if (wild.length) {
          out.push({ level: 'error', msg: `🔴 nomad_transport.managed_hosts 含通配符 [${wild.join(', ')}] = 全量接管`,
                     effect: '插件接管用户的全部流量 —— 必须逐条声明域名, 不接受通配' });
        }
        const notStr = hosts.filter((h) => typeof h !== 'string' || !h.trim());
        if (notStr.length) {
          out.push({ level: 'error', msg: 'nomad_transport.managed_hosts 含空项或非字符串',
                     effect: '该条被忽略, 对应域名不会被接管' });
        }
      }
      if (transport.requires_signature !== true) {
        out.push({ level: 'error', msg: 'nomad_transport.requires_signature 必须显式为 true',
                   effect: '加速类插件不经签名即可加载 —— 任何人都能替换掉它' });
      }
    }
  }

  if (m.nomad && m.nomad.provides_page) {
    out.push({ level: 'warn', msg: 'nomad.provides_page 是 iOS 旧写法, 已改为 nomad_surfaces 数组',
               effect: '当前仍可用(运行时会映射), 但后续版本移除 —— 请改成标准写法' });
  }
  return out;
}

function normalizeSurfaces(m, platform) {
  const all = normalizeSurfacesAll(m);
  return platform ? splitSurfacesByPlatform(all, platform).active : all;
}

function splitSurfacesByPlatform(list, platform) {
  const active = [];
  const dropped = [];
  for (const s of list) {
    const supported = SURFACE_PLATFORMS[s.kind];
    if (supported && !supported.includes(platform)) {
      dropped.push({ ...s, reason: 'platform_unsupported',
                     why: `位置 "${s.kind}" 只存在于 ${supported.join(' / ')}` });
      continue;
    }
    if (Array.isArray(s.platforms) && s.platforms.length && !s.platforms.includes(platform)) {
      dropped.push({ ...s, reason: 'not_declared_here',
                     why: `插件只把 "${s.kind}" 声明给了 ${s.platforms.join(' / ')}` });
      continue;
    }
    active.push(s);
  }
  return { active, dropped };
}

function surfacesForPlatform(m, platform) {
  return splitSurfacesByPlatform(normalizeSurfacesAll(m), platform);
}

function platformProblems(m, platform) {
  if (!platform) {
    return { problems: [{ level: 'warn', msg: '宿主没有声明 platform',
      effect: '平台限定(nomad.compat.platforms)与位置按端过滤**都没有生效** —— '
              + '别把"装上了"读成"这个包支持本端"' }], dropped: [] };
  }
  const problems = [];
  const compat = pf(m, 'compat') || {};

  if (Array.isArray(compat.platforms) && compat.platforms.length
      && !compat.platforms.includes(platform)) {
    problems.push({ level: 'error',
      msg: `本插件只声明支持 ${compat.platforms.join(' / ')}, 当前端是 ${platform}`,
      effect: '不安装 —— 这是插件作者的跨端声明, 不是本端的缺陷' });
  }

  const { dropped } = surfacesForPlatform(m, platform);
  const mode = compat.on_missing_surface || 'skip';
  if (dropped.length) {
    const detail = dropped.map((d) => `${d.kind}(${d.why})`).join('; ');
    if (mode === 'reject') {
      problems.push({ level: 'error', msg: `声明了本端拿不到的位置: ${detail}`,
        effect: '按 on_missing_surface="reject" 整包拒装 —— 插件作者声明了"离开这个位置就没意义"' });
    } else if (mode === 'warn') {
      problems.push({ level: 'warn', msg: `本端拿不到这些位置: ${detail}`,
        effect: '按 on_missing_surface="warn" 跳过这些位置并在插件管理界面标注, 其余照常运行' });
    }

  }
  return { problems, dropped };
}

function normalizeSurfacesAll(m) {
  const out = [];
  if (m.side_panel && m.side_panel.default_path) out.push({ kind: 'side_panel', path: m.side_panel.default_path });
  if (m.action) out.push({ kind: 'toolbar_action', title: m.action.default_title || m.name, icon: pickIcon(m.action.default_icon || m.icons) });
  if ((m.permissions || []).includes('contextMenus')) out.push({ kind: 'context_menu', dynamic: true });

  const surfaces = pf(m, 'surfaces');
  if (Array.isArray(surfaces)) {
    for (const s of surfaces) if (s && SURFACE_KINDS.includes(s.kind)) out.push({ ...s });
  }

  const IOS_SLOT = { sidebar: 'side_panel', newtab: 'home_panel', toolbar: 'toolbar_action', files: 'files' };
  const legacy = m.nomad && m.nomad.provides_page;
  if (legacy && IOS_SLOT[legacy.slot]) {
    const kind = IOS_SLOT[legacy.slot];
    if (!out.some((x) => x.kind === kind)) out.push({ kind, path: legacy.entry, _from: 'ios_legacy' });
  }
  return out;
}

function pickIcon(icons) {
  if (!icons) return null;
  if (typeof icons === 'string') return icons;
  return icons['48'] || icons['128'] || icons['16'] || Object.values(icons)[0] || null;
}

function slugify(s) {
  return String(s || 'plugin').trim().toLowerCase().replace(/[^\w一-龥-]+/g, '-').replace(/^-+|-+$/g, '') || 'plugin';
}

export { NomadPluginRuntime, PluginInstance, validateManifest, normalizeSurfaces,
         surfacesForPlatform, platformProblems, pf as nomadField,
         SURFACE_KINDS, SURFACE_PLATFORMS, PLATFORMS, RUNTIME_VERSION,

         validateRouteSpec, checkRouteWithKernel, installChromeProxyShim,
         ROUTE_SCOPES, ROUTE_FALLBACKS, ROUTE_VERDICT_NAME,

         checkPermissionsWithKernel, PERM_VERDICT_NAME,

         registerNomadCommand, unregisterNomadCommand, listNomadCommands,
         nomadKernelCommands, NOMAD_PRIVILEGED_COMMANDS, _resetNomadCommands,

         _extRegistry as _extRegistryUnsafe };
if (typeof window !== 'undefined') {
  window.NomadPluginRuntime = NomadPluginRuntime;
  window.__nomadPluginRuntimeVersion = RUNTIME_VERSION;

  window.registerNomadCommand = registerNomadCommand;
  window.unregisterNomadCommand = unregisterNomadCommand;
  window.listNomadCommands = listNomadCommands;
}
