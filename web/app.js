/* 互旦 SEO 工作台 —— 前端逻辑。无框架,和 rank-tracker 控制台一个路子。 */
'use strict';

const $ = id => document.getElementById(id);
const CRED_CN = {
  'google_ads.developer_token': '开发者令牌',
  'google_ads.client_id': 'OAuth client_id',
  'google_ads.client_secret': 'OAuth client_secret',
  'google_ads.refresh_token': 'refresh_token',
  'google_ads.login_customer_id': 'MCC 账号 ID',
  'anthropic.api_key': 'Claude API Key',
  'dataforseo.login': 'DataForSEO 账号',
  'dataforseo.password': 'DataForSEO 密码',
  'feishu.app_id': '飞书 app_id',
  'feishu.app_secret': '飞书 app_secret',
};

let poller = null;
let afterJob = null;   // 作业真正跑完之后要做的事
let lastRows = [];
let sortState = { col: null, asc: false };

/* ---------------- 标签页 ---------------- */
document.querySelectorAll('nav button').forEach(b => {
  b.onclick = () => {
    document.querySelectorAll('nav button').forEach(x => x.classList.toggle('active', x === b));
    ['ideas', 'volume', 'sop', 'ranks', 'learn', 'setup'].forEach(t => { $('tab-' + t).hidden = (t !== b.dataset.tab); });
  };
});

/* ---------------- 状态 ---------------- */
async function loadStatus() {
  const s = await fetch('/api/status').then(r => r.json());
  $('ver').textContent = 'v' + s.version;
  const R = s.ready || {};
  $('pills').innerHTML =
    pill('拓词', R.keywords) + pill('skill', R.skills) + pill('排名', R.ranks) +
    `<span>skills ${(s.skills_found || []).length} 个</span>`;
  $('cfgkv').innerHTML =
    Object.keys(CRED_CN).map(k =>
      `<div>${CRED_CN[k]}</div><div>${s.credentials[k]
        ? '<span style="color:var(--ok)">已配置</span>'
        : '<span style="color:var(--dim)">未配置</span>'}</div>`).join('') +
    `<div>配置文件</div><div>${s.config_file || '<span style="color:var(--err)">还没有 config.local.yaml</span>'}</div>` +
    `<div>skills 目录</div><div>${s.skills_dir}</div>`;
  const d = s.defaults || {};
  if (d.min_volume != null) $('i-min').value = d.min_volume;
  if (d.usd_rate != null) { $('s-rate').value = d.usd_rate; }
  return d;
}
const pill = (name, ok) => `<span class="${ok ? 'on' : 'off'}">${name}${ok ? ' 就绪' : ' 未配置'}</span>`;

/* ---------------- 跑作业 ---------------- */
async function run(url, payload, title) {
  showErr('');
  const btns = document.querySelectorAll('button.go, button.ghost');
  btns.forEach(b => b.disabled = true);
  $('outbox').hidden = false;
  $('outtitle').textContent = title;
  $('log').textContent = '';
  $('log').classList.add('show');
  $('meta').hidden = true;
  $('tblwrap').hidden = true;

  try {
    const r = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }).then(x => x.json());
    if (r.error) throw new Error(r.error);
    poll(r.job, 0, btns);
  } catch (e) {
    showErr(e.message);
    btns.forEach(b => b.disabled = false);
  }
}

function poll(id, since, btns) {
  clearTimeout(poller);
  poller = setTimeout(async () => {
    let s;
    try {
      s = await fetch(`/api/job?id=${id}&since=${since}`).then(r => r.json());
    } catch (e) { return poll(id, since, btns); }
    if (s.error) { showErr(s.error); btns.forEach(b => b.disabled = false); return; }

    if (s.lines.length) {
      const el = $('log');
      const stick = el.scrollTop + el.clientHeight >= el.scrollHeight - 10;
      el.textContent += s.lines.join('\n') + '\n';
      if (stick) el.scrollTop = el.scrollHeight;
    }

    if (s.status === 'running') return poll(id, s.total_lines, btns);

    btns.forEach(b => b.disabled = false);
    $('outtitle').textContent = title(s);
    if (s.status === 'error') return showErr(s.error);
    if (s.result && s.result.preview) render(s.result);
    if (afterJob) { const f = afterJob; afterJob = null; f(s); }
  }, 500);
}
const title = s => `${s.name} —— ${s.status === 'done' ? '完成' : '出错'}（${s.elapsed}s）`;

/* ---------------- 结果表 ---------------- */
function render(res) {
  lastRows = res.preview || [];
  $('cnt').textContent = res.count;
  $('meta').hidden = false;
  $('trunc').textContent = res.truncated ? '（表格只显示前 200 行，完整数据在 CSV 里）' : '';
  // 统计栏按 stats 里实际有什么就显示什么 —— 拓词/SOP/排名/学清单各有各的字段，
  // 硬编码字段名会在别的模块上显示成 undefined。
  const st = res.stats;
  const HIDE = new Set(['币种', '汇率', '市场', '域名', '候选数',
                        '剔除词总数', '保留词总数', '命中剔除词', '误杀数']);
  $('stats').innerHTML = st ? Object.entries(st)
    .filter(([k, v]) => !k.startsWith('_') && !HIDE.has(k) && v !== null && v !== '')
    .map(([k, v]) => `${k} <b>${v}</b>${/召回|误杀/.test(k) ? '%' : ''}`)
    .concat(st['汇率'] ? [`出价按 1 USD = ${st['汇率']} ${st['币种']} 折算`] : [])
    .join('　·　') : '';
  const dl = $('dl');
  if (res.csv) { dl.href = '/api/download?file=' + encodeURIComponent(res.csv); dl.hidden = false; }
  else dl.hidden = true;
  const xl = $('dlx');
  if (res.xlsx) { xl.href = '/api/download?file=' + encodeURIComponent(res.xlsx); xl.hidden = false; }
  else xl.hidden = true;
  if (!lastRows.length) { $('tblwrap').hidden = true; return; }
  sortState = { col: null, asc: false };
  draw(res.columns || Object.keys(lastRows[0]));
  $('tblwrap').hidden = false;
}

function draw(cols) {
  // 表头带币种后缀（页首出价低(CNY)），所以按前缀判定数值列
  const NUM_PREFIX = ['月均搜索量', '竞争指数', '页首出价', '平均CPC',
                      '可行性评分', '全球月搜', '序号'];
  const isNum = c => NUM_PREFIX.some(x => c.startsWith(x)) || c.endsWith('月搜');
  const head = '<thead><tr>' + cols.map(c =>
    `<th data-c="${c}">${c}${sortState.col === c ? (sortState.asc ? ' ▲' : ' ▼') : ''}</th>`).join('') + '</tr></thead>';
  const body = '<tbody>' + lastRows.map(r => '<tr>' + cols.map(c => {
    let v = r[c] == null ? '' : r[c];
    if (c === '高价值' && v) v = `<span class="tag ${v === '极高' ? 'hi' : 'mid'}">${v}</span>`;
    if (c === '近12月') v = `<span style="color:var(--dim);font-size:11.5px">${String(v).slice(0, 34)}…</span>`;
    return `<td>${v}</td>`;
  }).join('') + '</tr>').join('') + '</tbody>';
  $('tbl').innerHTML = head + body;
  $('tbl').querySelectorAll('th').forEach(th => {
    th.onclick = () => {
      const c = th.dataset.c;
      sortState = { col: c, asc: sortState.col === c ? !sortState.asc : false };
      const k = sortState.asc ? 1 : -1;
      lastRows.sort((a, b) => isNum(c)
        ? ((+a[c] || 0) - (+b[c] || 0)) * k
        : String(a[c]).localeCompare(String(b[c]), 'zh') * k);
      draw(cols);
    };
  });
}

function showErr(msg) {
  const e = $('err');
  e.textContent = msg || '';
  e.classList.toggle('show', !!msg);
  if (msg) window.scrollTo({ top: 0, behavior: 'smooth' });
}

/* ---------------- 词表文件上传 ----------------
   解析放在服务端：GKP 网页版导出的 CSV 是 UTF-16 + Tab 分隔，
   Excel 另存可能是 GBK，还有 xlsx —— 这些浏览器里都读不了。
   `full=true` 的字段（客户原始词）保留整行，其余只取第一列。   */
const ACCEPT = '.txt,.csv,.tsv,.xlsx,.xlsm';

function attachUploader(ta) {
  const full = ta.dataset.full === '1';
  const label = ta.closest('div,section')?.querySelector('label') ||
                ta.previousElementSibling;
  const note = document.createElement('span');
  note.className = 'upnote';
  note.hidden = true;
  ta.insertAdjacentElement('afterend', note);

  const input = document.createElement('input');
  input.type = 'file';
  input.accept = ACCEPT;
  input.hidden = true;
  document.body.appendChild(input);

  const link = document.createElement('span');
  link.className = 'up';
  link.textContent = '⬆ 上传文件';
  link.title = '支持 txt / csv / tsv / xlsx，也可以直接把文件拖进输入框';
  if (label) label.appendChild(link);

  async function send(file) {
    if (!file) return;
    link.classList.add('busy');
    link.textContent = '解析中…';
    note.hidden = true;
    try {
      const buf = await file.arrayBuffer();
      const r = await fetch('/api/parse-file', {
        method: 'POST',
        headers: { 'X-Filename': encodeURIComponent(file.name) },
        body: buf,
      }).then(x => x.json());
      if (r.error) throw new Error(r.error);
      const lines = r.rows.map(row => full ? row.join('\t') : (row[0] || ''))
                          .filter(Boolean);
      // 去重但保留顺序
      const seen = new Set();
      const uniq = lines.filter(l => {
        const k = l.toLowerCase();
        if (seen.has(k)) return false;
        seen.add(k); return true;
      });
      ta.value = uniq.join('\n');
      note.className = 'upnote';
      note.textContent = `已读入 ${file.name}：${uniq.length} 行` +
        (uniq.length < lines.length ? `（去重 ${lines.length - uniq.length}）` : '') +
        `　·　识别为 ${r.note}`;
      note.hidden = false;
    } catch (e) {
      note.className = 'upnote bad';
      note.textContent = '读取失败：' + e.message;
      note.hidden = false;
    } finally {
      link.classList.remove('busy');
      link.textContent = '⬆ 上传文件';
      input.value = '';
    }
  }

  // 模板下载 —— 放在上传按钮左边。模板里的说明在单独的工作表，
  // 所以填完可以原样传回来，不会把说明当成关键词导进去。
  if (ta.dataset.tpl && label) {
    const tpl = document.createElement("span");
    tpl.className = "up";
    tpl.style.marginRight = "12px";
    tpl.textContent = "⬇ 模板";
    tpl.title = "下载这一栏的 xlsx 模板（含填写说明）";
    tpl.onclick = () => { location.href = "/api/template?kind=" + ta.dataset.tpl; };
    label.appendChild(tpl);
  }

  link.onclick = () => input.click();
  input.onchange = () => send(input.files[0]);

  ['dragenter', 'dragover'].forEach(ev => ta.addEventListener(ev, e => {
    e.preventDefault(); ta.classList.add('drop');
  }));
  ['dragleave', 'drop'].forEach(ev => ta.addEventListener(ev, e => {
    e.preventDefault(); ta.classList.remove('drop');
  }));
  ta.addEventListener('drop', e => send(e.dataTransfer.files[0]));
}

/* ---------------- 地区 / 语言选择器 ----------------
   地区可加多个（GKP 会把多地区的量合并统计）。
   语言默认跟着地区走 —— 选了德国就自动带出德语，省得去记小语种代码；
   一旦手动改过语言就不再自动覆盖，并给一个「跟随地区」的退回入口。   */
class Targeting {
  constructor(prefix, opts) {
    this.sel = $(prefix + '-geo');
    this.lang = $(prefix + '-lang');
    this.chips = $(prefix + '-chips');
    this.note = $(prefix + '-note');
    this.picked = [];
    this.manual = false;
    this.name = {};

    this.sel.innerHTML = '<option value="">＋ 添加地区…</option>' +
      opts.countries.map(g => `<optgroup label="${g.group}">` +
        g.items.map(c => {
          this.name[c.code] = c.name;
          return `<option value="${c.code}" data-lang="${c.lang}">${c.name} (${c.code})</option>`;
        }).join('') + '</optgroup>').join('');

    this.lang.innerHTML = opts.languages
      .map(l => `<option value="${l.code}">${l.name}</option>`).join('');

    this.sel.onchange = () => {
      const code = this.sel.value;
      this.sel.selectedIndex = 0;
      if (code) this.add(code);
    };
    this.lang.onchange = () => { this.manual = true; this.drawNote(); };
  }

  add(code) {
    if (this.picked.includes(code)) return;
    this.picked.push(code);
    if (!this.manual) this.syncLang();
    this.draw();
  }

  remove(code) {
    this.picked = this.picked.filter(c => c !== code);
    if (!this.manual) this.syncLang();
    this.draw();
  }

  /* 语言跟「最后选中的那个地区」走。
     刚点了德国就该给德语 —— 若跟第一个地区走，预填的美国会把德语压住，
     表现就是「选了德国语言还是 en」，很反直觉。 */
  syncLang() {
    const last = this.picked[this.picked.length - 1];
    if (!last) return;
    const opt = this.sel.querySelector(`option[value="${last}"]`);
    if (opt) this.lang.value = opt.dataset.lang;
  }

  draw() {
    this.chips.innerHTML = this.picked.map(c =>
      `<span class="chip"><b>${this.name[c] || c}</b> ${c}<i data-x="${c}" title="移除">×</i></span>`).join('');
    this.chips.querySelectorAll('i[data-x]').forEach(x => {
      x.onclick = () => this.remove(x.dataset.x);
    });
    this.drawNote();
  }

  drawNote() {
    if (!this.note) return;
    this.note.innerHTML = this.manual
      ? '· 已手动指定 <a data-reset>跟随地区</a>'
      : (this.picked.length ? '· 已按地区自动带出' : '');
    const a = this.note.querySelector('[data-reset]');
    if (a) a.onclick = () => { this.manual = false; this.syncLang(); this.drawNote(); };
  }

  get value() { return this.picked.slice(); }
}

let T = {};

/* ---------------- 按钮 ---------------- */
/* 拓词方式三选一。以前种子词和网址两个框并列，标签写「填了网址就不用种子词」，
   但后端其实是两个都跑 —— 说明和行为对不上，所以改成显式三选一。 */
let ideaMode = 'seed';
document.querySelectorAll('#i-mode button').forEach(b => {
  b.onclick = () => {
    ideaMode = b.dataset.m;
    document.querySelectorAll('#i-mode button').forEach(x => x.classList.toggle('on', x === b));
    ['seed', 'site', 'page'].forEach(m => { $('i-box-' + m).hidden = (m !== ideaMode); });
  };
});

$('run-ideas').onclick = () => {
  const payload = {
    geos: T.i.value,
    lang: $('i-lang').value,
    min_volume: +$('i-min').value || 0,
    usd: $('i-usd').checked,
  };
  if (ideaMode === 'seed') {
    payload.seeds = $('seeds').value;
    if (!payload.seeds.trim()) return showErr('种子词是空的。');
  } else {
    const el = ideaMode === 'site' ? $('i-url-site') : $('i-url-page');
    payload.url = el.value.trim();
    payload.site = (ideaMode === 'site');      // 整站 vs 单页 —— 以前这个参数前端从来没传过
    if (!payload.url) return showErr('网址是空的。');
  }
  run('/api/keywords/ideas', payload, '拓词中…');
};

$('run-volume').onclick = () => run('/api/keywords/volume', {
  keywords: $('words').value,
  geos: T.v.value,
  lang: $('v-lang').value,
  usd: $('v-usd').checked,
}, '取数中…');

$('run-sop').onclick = () => run('/api/keywords/sop', {
  seeds: $('s-seeds').value,
  sites: $('s-sites').value,
  customer: $('s-customer').value,
  mixed: $('s-mixed').value,
  exclude: $('s-exclude').value,
  geos: T.s.value,
  lang: $('s-lang').value,
  min_volume: +$('s-min').value || 0,
  usd_rate: +$('s-rate').value || 0,
}, '生成 SOP 总表中…');


/* ---------------- 清单库 ---------------- */
async function loadLib() {
  const box = $('lib');
  if (!box) return;
  const { lists } = await fetch('/api/lists').then(r => r.json());
  if (!lists || !lists.length) {
    box.innerHTML = '<span class="hint">还没有学过的清单。用上面的功能学一份。</span>';
    return;
  }
  box.innerHTML = '<div class="tblwrap"><table><thead><tr><th>名称</th><th>模式数</th><th>召回</th><th>误杀</th><th>学于</th><th>备注</th><th></th></tr></thead><tbody>' +
    lists.map(l => `<tr><td><b>${l.name}</b></td><td>${l.patterns}</td>` +
      `<td>${l.recall}%</td><td>${l.false_kill}%</td><td>${l.saved_at}</td>` +
      `<td style="white-space:normal">${l.note || ''}</td>` +
      `<td><span class="up" style="float:none" data-use="${l.file}">用到剔除清单 →</span></td></tr>`).join('') +
    '</tbody></table></div>';
  box.querySelectorAll('[data-use]').forEach(el => {
    el.onclick = async () => {
      const d = await fetch('/api/lists?load=' + encodeURIComponent(el.dataset.use)).then(r => r.json());
      if (d.error) return showErr(d.error);
      $('s-exclude').value = (d.patterns || []).join(String.fromCharCode(10));
      document.querySelector('nav button[data-tab="sop"]').click();
      window.scrollTo({ top: 0, behavior: 'smooth' });
    };
  });
}

$('run-learn').onclick = () => {
  afterJob = () => loadLib();        // 学完再刷新库,不能用 run() 的 then(那时作业还没跑完)
  run('/api/keywords/learn', {
    cut: $('l-cut').value,
    keep: $('l-keep').value,
    min_support: +$('l-sup').value || 8,
    allow_false_kill: +$('l-fk').value || 0,
    save_as: $('l-name').value,
  }, '学习中…');
};

/* ---------------- 排名监控 ---------------- */
let rankUnit = { standard: 0.0015, live: 0.005 };

function rankCost() {
  const n = $('r-kw').value.split(String.fromCharCode(10)).filter(x => x.trim()).length;
  const pages = +$('r-depth').value || 3;
  const mode = $('r-mode').value;
  const base = mode === 'standard' ? 0.0006 : 0.002;
  const unit = base * (1 + 0.75 * (pages - 1));
  $('r-cost').textContent = n ? `$${(unit * n).toFixed(4)}　(${n} 词 × $${unit.toFixed(4)})` : '—';
}

async function loadProjects(opts) {
  const sel = $('r-proj');
  if (!sel) return;
  const d = await fetch('/api/ranks/projects').then(r => r.json());
  rankUnit = d.unit || rankUnit;
  window.__projects = d.projects || [];
  sel.innerHTML = '<option value="">（手填域名）</option>' +
    window.__projects.map((p, i) => `<option value="${i}">${p.name}　${p.host}　${p.keywords.length} 词</option>`).join('');
  // 地区/语言下拉复用 /api/options 的数据
  $('r-gl').innerHTML = opts.countries.map(g => `<optgroup label="${g.group}">` +
    g.items.map(c => `<option value="${c.code}">${c.name} (${c.code})</option>`).join('') + '</optgroup>').join('');
  $('r-hl').innerHTML = opts.languages.map(l => `<option value="${l.code}">${l.name}</option>`).join('');
  $('r-gl').value = 'US'; $('r-hl').value = 'en';
  sel.onchange = () => {
    const p = window.__projects[+sel.value];
    if (!p) return;
    $('r-domain').value = p.domain;
    $('r-kw').value = (p.keywords || []).join(String.fromCharCode(10));
    $('r-gl').value = (p.gl || 'US').toUpperCase();
    $('r-hl').value = p.hl || 'en';
    $('r-dev').value = p.device || 'desktop';
    $('r-depth').value = String(p.depth || 3);
    $('r-feishu').value = p.feishu_url || '';
    $('r-field').value = p.feishu_rank_field || '';
    rankCost();
  };
  ['r-kw', 'r-depth', 'r-mode'].forEach(id => {
    $(id).addEventListener('input', rankCost);
    $(id).addEventListener('change', rankCost);
  });
  rankCost();
}

function rankPayload(extra) {
  return Object.assign({
    domain: $('r-domain').value,
    keywords: $('r-kw').value,
    gl: $('r-gl').value, hl: $('r-hl').value,
    device: $('r-dev').value, depth: +$('r-depth').value || 3,
    mode: $('r-mode').value,
    writeback: $('r-wb').checked, push: $('r-push').checked,
  }, extra || {});
}

$('run-ranks').onclick = () => {
  const txt = $('r-cost').textContent;
  if (!confirm(`这一轮会真实扣费，预估 ${txt}。确定开始吗？`)) return;
  run('/api/ranks/check', rankPayload(), '排名检查中…');
};

$('run-ranks-failed').onclick = () => {
  if (!confirm('只补查从来没查成功过的词，会产生少量费用。继续吗？')) return;
  run('/api/ranks/check', rankPayload({ only_failed: true }), '补查出错词…');
};


$('run-sync').onclick = () => {
  afterJob = (s) => {                        // 同步完把词填进输入框
    const kws = (s.result || {}).keywords;
    if (kws && kws.length) { $('r-kw').value = kws.join(String.fromCharCode(10)); rankCost(); }
  };
  run('/api/ranks/sync', { domain: $('r-domain').value, url: $('r-feishu').value },
      '同步飞书词库…');
};

$('run-wb').onclick = () => {
  if (!confirm('会把最近一轮的排名写进飞书表的排名列，覆盖原值。继续吗？')) return;
  run('/api/ranks/writeback', { domain: $('r-domain').value,
      url: $('r-feishu').value, field: $('r-field').value }, '写回飞书…');
};
$('run-ranks-view').onclick = async () => {
  showErr('');
  const d = await fetch('/api/ranks/overview?domain=' + encodeURIComponent($('r-domain').value)).then(r => r.json());
  if (d.error) return showErr(d.error);
  if (!d.rows.length) return showErr('这个域名还没有任何检查记录。');
  $('outbox').hidden = false;
  $('outtitle').textContent = `最近一轮 —— ${d.domain}`;
  $('log').classList.remove('show');
  render({ count: d.rows.length, columns: ['关键词', '排名', '上轮', '变化', 'URL', '轮次'],
           preview: d.rows.slice(0, 300), csv: null, truncated: d.rows.length > 300,
           stats: { 轮次: (d.cost || {}).run_date, 上轮花费: '$' + ((d.cost || {}).cost || 0), 待补查: d.failed } });
};
$('run-check').onclick = () => run('/api/keywords/check', {}, '自检中…');

/* ---------------- 启动 ---------------- */
(async () => {
  const [d, opts] = await Promise.all([
    loadStatus(),
    fetch('/api/options').then(r => r.json()),
  ]);
  document.querySelectorAll('textarea').forEach(attachUploader);
  T.i = new Targeting('i', opts);
  T.v = new Targeting('v', opts);
  T.s = new Targeting('s', opts);
  loadLib();
  loadProjects(opts);
  const init = (d.geo || 'US').toUpperCase();
  [T.i, T.v, T.s].forEach(t => {
    t.add(init);
    if (d.lang) { t.lang.value = d.lang; t.manual = false; t.syncLang(); }
    t.draw();
  });
})();
