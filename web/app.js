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
let lastRows = [];
let sortState = { col: null, asc: false };

/* ---------------- 标签页 ---------------- */
document.querySelectorAll('nav button').forEach(b => {
  b.onclick = () => {
    document.querySelectorAll('nav button').forEach(x => x.classList.toggle('active', x === b));
    ['ideas', 'volume', 'setup'].forEach(t => { $('tab-' + t).hidden = (t !== b.dataset.tab); });
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
  if (d.geo) { $('i-geo').value = d.geo; $('v-geo').value = d.geo; }
  if (d.lang) { $('i-lang').value = d.lang; $('v-lang').value = d.lang; }
  if (d.min_volume != null) $('i-min').value = d.min_volume;
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
  }, 500);
}
const title = s => `${s.name} —— ${s.status === 'done' ? '完成' : '出错'}（${s.elapsed}s）`;

/* ---------------- 结果表 ---------------- */
function render(res) {
  lastRows = res.preview || [];
  $('cnt').textContent = res.count;
  $('meta').hidden = false;
  $('trunc').textContent = res.truncated ? '（表格只显示前 200 行，完整数据在 CSV 里）' : '';
  const dl = $('dl');
  if (res.csv) { dl.href = '/api/download?file=' + encodeURIComponent(res.csv); dl.hidden = false; }
  else dl.hidden = true;
  if (!lastRows.length) { $('tblwrap').hidden = true; return; }
  sortState = { col: null, asc: false };
  draw(res.columns || Object.keys(lastRows[0]));
  $('tblwrap').hidden = false;
}

function draw(cols) {
  const num = new Set(['月均搜索量', '竞争指数', '页首出价低', '页首出价高', '平均CPC', '可行性评分']);
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
      lastRows.sort((a, b) => num.has(c)
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

/* ---------------- 按钮 ---------------- */
const geos = v => v.split(/[,，\s]+/).map(s => s.trim()).filter(Boolean);

$('run-ideas').onclick = () => run('/api/keywords/ideas', {
  seeds: $('seeds').value,
  url: $('url').value,
  geos: geos($('i-geo').value),
  lang: $('i-lang').value.trim() || 'en',
  min_volume: +$('i-min').value || 0,
}, '拓词中…');

$('run-volume').onclick = () => run('/api/keywords/volume', {
  keywords: $('words').value,
  geos: geos($('v-geo').value),
  lang: $('v-lang').value.trim() || 'en',
}, '取数中…');

$('run-check').onclick = () => run('/api/keywords/check', {}, '自检中…');

loadStatus();
