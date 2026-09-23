/* 社媒客户周报。
 *
 * 分两步，刻意不合成一步：
 *   ① 上传 -> 服务端解析 + 算指标 + 口径体检（不调 AI，几秒出结果）
 *   ② 专员看完识别结果和体检清单，再决定给哪个客户出稿
 * 合成一步的话，一个客户的数据传错了要等 AI 跑完才发现，既慢又费钱。
 */

let socUpload = null;   // { upload_id, clients, issues }

const LEVEL_CN = { block: '必须处理', warn: '注意', info: '口径说明' };

function socLog(text) {
  const el = document.getElementById('soc-log');
  el.textContent = text;
  el.classList.add('show');
}

function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

/* ---------------- 上传 ---------------- */

async function socSend(file) {
  if (!file) return;
  document.getElementById('soc-name').textContent = file.name;
  socLog('正在解析 ' + file.name + ' …');
  document.getElementById('soc-result').hidden = true;
  document.getElementById('soc-out').hidden = true;

  let r;
  try {
    r = await apiFetch('/api/social/upload', {
      method: 'POST',
      headers: { 'X-Filename': encodeURIComponent(file.name) },
      body: file,
    }).then(x => x.json());
  } catch (e) {
    return socLog('上传失败：' + e.message);
  }
  if (r.error) return socLog('解析失败：' + r.error);

  socUpload = r;
  socLog((r.log || []).join('\n'));
  socRenderResult(r);
}

function socRenderResult(r) {
  const box = document.getElementById('soc-clients');
  box.innerHTML = (r.clients || []).map(c => {
    const plats = c.platforms.map(p =>
      '<li>' + esc(p.cn) + '：' + esc(p.start || '—') + ' ~ ' + esc(p.end || '—') +
      '，本期内容 ' + p.posts + ' 篇</li>').join('');
    return '<div class="kv"><b>' + esc(c.client) + '</b><ul>' + plats + '</ul>' +
      '<button class="go" data-client="' + esc(c.client) + '">出这份周报</button></div>';
  }).join('');

  const tb = document.getElementById('soc-issues');
  const rows = (r.issues || []).map(i =>
    '<tr class="lv-' + i.level + '"><td>' + LEVEL_CN[i.level] + '</td><td>' +
    esc(i.scope) + '</td><td>' + esc(i.problem) + '</td><td>' + esc(i.evidence) +
    '</td><td>' + esc(i.advice) + '</td></tr>').join('');
  tb.innerHTML = '<thead><tr><th>级别</th><th>涉及对象</th><th>问题</th>' +
    '<th>具体表现</th><th>处理建议</th></tr></thead><tbody>' + rows + '</tbody>';

  const hint = document.getElementById('soc-feishu-hint');
  const push = document.getElementById('soc-push');
  if (r.feishu_ready) {
    hint.textContent = '飞书已配置，勾选后会自动建文档、加编辑权并挪进指定文件夹。';
    push.disabled = false;
  } else {
    hint.textContent = '飞书未配置（config.local.yaml 里的 feishu.app_id / app_secret 是空的），' +
      '暂时只能导出本地 markdown。';
    push.disabled = true;
    push.checked = false;
  }

  document.getElementById('soc-result').hidden = false;
  box.querySelectorAll('button[data-client]').forEach(b => {
    b.onclick = () => socGenerate(b.dataset.client);
  });
}

/* ---------------- 出稿 ---------------- */

function socGenerate(client) {
  if (!socUpload) return;
  afterJob = (s) => socRenderDoc(s);
  run('/api/social/generate', {
    upload_id: socUpload.upload_id,
    client: client,
    use_ai: document.getElementById('soc-ai').checked,
    push_feishu: document.getElementById('soc-push').checked,
  }, '出周报 · ' + client);
}

function socRenderDoc(s) {
  const res = (s || {}).result;
  if (!res) return;
  const box = document.getElementById('soc-docs');
  const doc = res.doc
    ? '<p>飞书文档：<a href="' + esc(res.doc.url) + '" target="_blank">' +
      esc(res.doc.url) + '</a></p>'
    : '';
  const blockers = (res.blockers || []).length
    ? '<p class="err"><b>发客户前必须处理 ' + res.blockers.length + ' 项：</b></p><ul>' +
      res.blockers.map(b => '<li>' + esc(b.problem) + ' —— ' + esc(b.advice) + '</li>').join('') +
      '</ul>'
    : '<p class="hint">口径体检没有「必须处理」项。</p>';
  const ai = res.ai || {};
  const aiNote = ai.skipped
    ? '<p class="hint">本次未调用 AI，文字段落是待补占位。</p>'
    : '<p class="hint">AI：' + esc(ai.model || '') + '，花费约 $' +
      (ai.cost || 0).toFixed(4) +
      ((ai.fallback || []).length
        ? '；<b class="err">' + ai.fallback.length +
          ' 段未通过数字校验已回退成人工待补（' + ai.fallback.join('、') + '）</b>'
        : '；数字守卫全部通过') + '</p>';

  box.innerHTML =
    '<h3>' + esc(res.client) + '　' + esc(res.period.start) + ' ~ ' + esc(res.period.end) + '</h3>' +
    doc + aiNote + blockers +
    '<p><a class="dl" href="/api/download?file=' + encodeURIComponent(res.file) +
    '">下载 ' + esc(res.file) + '</a></p>' +
    '<details><summary>预览（前 4000 字）</summary><pre>' +
    esc(res.markdown) + '</pre></details>' +
    (box.innerHTML || '');
  document.getElementById('soc-out').hidden = false;
}

/* ---------------- 绑定 ---------------- */

(function () {
  const drop = document.getElementById('soc-drop');
  const input = document.getElementById('soc-file');
  if (!drop) return;

  document.getElementById('soc-pick').onclick = () => input.click();
  input.onchange = () => socSend(input.files[0]);

  ['dragenter', 'dragover'].forEach(ev => drop.addEventListener(ev, e => {
    e.preventDefault();
    drop.classList.add('over');
  }));
  ['dragleave', 'drop'].forEach(ev => drop.addEventListener(ev, e => {
    e.preventDefault();
    drop.classList.remove('over');
  }));
  drop.addEventListener('drop', e => socSend(e.dataTransfer.files[0]));
})();
