"""HTML for the mail assistant page (tabs: today digest + AI composer)."""


ASSISTANT_PAGE_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>邮件助手 · 今日摘要</title>
<style>
* { box-sizing: border-box; }
body { margin: 0; font-family: "Segoe UI", "Microsoft YaHei", system-ui, sans-serif; background: #eef1f5; color: #1f2430; }
.topbar { background: #171e29; color: #fff; padding: 12px 22px; }
.topbar .inner { max-width: 1080px; margin: 0 auto; display: flex; justify-content: space-between; align-items: center; gap: 12px; }
.brand { display: flex; align-items: center; gap: 10px; font-size: 17px; font-weight: 600; }
.brand .dot { width: 9px; height: 9px; border-radius: 50%; background: #34d399; }
.tabs { display: flex; gap: 4px; background: rgba(255,255,255,0.08); border-radius: 8px; padding: 4px; }
.tab { border: 0; background: transparent; color: #cbd2d9; padding: 7px 18px; border-radius: 6px; font-size: 14px; cursor: pointer; }
.tab.active { background: #ffffff; color: #171e29; font-weight: 600; }
.actions { display: flex; gap: 10px; align-items: center; }
.topbar .actions button { font: inherit; font-size: 13px; padding: 6px 14px; border-radius: 6px; border: 1px solid rgba(255,255,255,0.28); background: rgba(255,255,255,0.08); color: #e5e9ee; cursor: pointer; }
.topbar .actions button:hover { background: rgba(255,255,255,0.16); }
.topbar .actions button:disabled { opacity: 0.55; cursor: wait; }
.update-hint { font-size: 12px; color: #cbd2d9; }
.wrap { max-width: 1080px; margin: 18px auto 40px; padding: 0 16px; }
.tab-panel { display: none; }
.tab-panel.active { display: block; }
#digest-frame { width: 100%; height: calc(100vh - 190px); min-height: 480px; border: 1px solid #e2e6ea; border-radius: 8px; background: #fff; }
.digest-bar { display: flex; justify-content: space-between; align-items: center; gap: 10px; margin-bottom: 10px; font-size: 13px; color: #52606d; flex-wrap: wrap; }
.digest-bar a { color: #2563eb; }
.tiles { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 18px; }
.tile { background: #fff; border: 1px solid #e2e6ea; border-radius: 8px; padding: 12px 16px; }
.tile .name { font-size: 12px; color: #6b7686; }
.tile .count { font-size: 24px; font-weight: 600; margin-top: 2px; }
.tile .sub { font-size: 12px; color: #8792a2; }
.tile.off .count { color: #b3261e; font-size: 16px; }
.grid { display: grid; grid-template-columns: 5fr 7fr; gap: 16px; align-items: start; }
.card { background: #fff; border: 1px solid #e2e6ea; border-radius: 8px; padding: 18px 20px; }
.card h2 { margin: 0 0 4px; font-size: 16px; display: flex; align-items: center; gap: 8px; }
.step { display: inline-flex; width: 22px; height: 22px; border-radius: 50%; background: #2563eb; color: #fff; font-size: 13px; align-items: center; justify-content: center; }
.hint { font-size: 12px; color: #8792a2; margin: 0 0 12px; }
label { display: block; font-size: 13px; color: #52606d; margin: 12px 0 4px; }
textarea, input, select { width: 100%; font: inherit; font-size: 14px; padding: 9px 11px; border: 1px solid #cfd6dd; border-radius: 6px; background: #fff; color: #1f2430; }
textarea:focus, input:focus, select:focus { outline: 2px solid #bcd0f7; border-color: #2563eb; }
textarea { resize: vertical; }
#instruction { min-height: 96px; }
#draft-body { min-height: 220px; line-height: 1.75; }
.examples { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
.examples span { font-size: 12px; color: #334e68; background: #eef2f6; border: 1px solid #dbe1e8; border-radius: 4px; padding: 3px 9px; cursor: pointer; }
.examples span:hover { background: #e3eaf1; }
.mailbox-block { margin-top: 16px; padding-top: 12px; border-top: 1px dashed #e2e6ea; }
.notice { font-size: 13px; color: #92400e; background: #fef3c7; border: 1px solid #fde68a; border-radius: 6px; padding: 8px 10px; margin: 10px 0 0; }
.actions { display: flex; gap: 10px; margin-top: 16px; flex-wrap: wrap; }
button.act { font: inherit; font-size: 14px; padding: 9px 20px; border-radius: 6px; border: 1px solid #cfd6dd; background: #f6f8fa; cursor: pointer; }
button.act.primary { background: #2563eb; border-color: #2563eb; color: #fff; }
button.act.primary:hover { background: #1d4ed8; }
button.act.send { background: #15803d; border-color: #15803d; color: #fff; }
button.act.send:hover { background: #166534; }
button:disabled { opacity: 0.55; cursor: wait; }
#status { max-width: 1080px; margin: 0 auto 30px; padding: 0 16px; font-size: 13px; color: #52606d; min-height: 18px; }
.mailbox-block { margin-top: 16px; }
@media (max-width: 860px) { .grid { grid-template-columns: 1fr; } .tiles { grid-template-columns: repeat(2, 1fr); } }
</style>
</head>
<body>
<div class="topbar"><div class="inner">
  <div class="brand"><span class="dot"></span>邮件助手</div>
  <nav class="tabs">
    <button class="tab active" data-tab="digest">今日摘要</button>
    <button class="tab" data-tab="ai">AI 写邮件</button>
  </nav>
  <div class="actions">
    <span class="update-hint" id="refresh-status"></span>
    <button id="refresh">刷新摘要</button>
  </div>
</div></div>
<div class="wrap">
  <section id="tab-digest" class="tab-panel active">
    <div class="digest-bar">
      <span>下方为最近一次生成的摘要页面，点击上方"刷新摘要"获取最新邮件。</span>
      <a href="/digest" target="_blank">在新窗口打开完整摘要 →</a>
    </div>
    <iframe id="digest-frame" src="/digest" title="今日摘要"></iframe>
  </section>
  <section id="tab-ai" class="tab-panel">
    <div class="tiles" id="tiles"></div>
    <div class="grid">
      <section class="card">
        <h2><span class="step">1</span>描述你想发的邮件</h2>
        <p class="hint">写给谁、关于什么事、什么语气，AI 会据此写出完整草稿。</p>
        <label>你的指令</label>
        <textarea id="instruction" placeholder="例如：给王老师写一封邮件，询问下周一下午三点是否方便面谈实习报告的修改意见，语气礼貌"></textarea>
        <div class="examples">
          <span data-fill="给课程负责人写一封简短邮件，说明实习报告初稿已完成，询问本周内能否给出反馈">实习报告反馈</span>
          <span data-fill="写一封请假邮件，因身体不适申请明天请假一天，语气诚恳">请假申请</span>
          <span data-fill="写一封邮件预约学校图书馆讨论间，本周四下午两点，时长两小时">预约讨论间</span>
        </div>
        <button class="act primary" id="generate" style="margin-top: 14px; width: 100%;">生成草稿</button>
        <div class="mailbox-block">
          <label>保存 / 发送使用的邮箱</label>
          <select id="mailbox">
            <option value="master_mail">巴黎萨克雷邮箱 (Outlook)</option>
            <option value="bachelor_mail">传媒大学本科邮箱</option>
            <option value="qq_mail">QQ 邮箱（仅存草稿）</option>
          </select>
          <p id="mailbox-notice" class="notice" style="display: none;"></p>
        </div>
      </section>
      <section class="card">
        <h2><span class="step">2</span>草稿预览</h2>
        <p class="hint">生成后可直接修改，再保存或发送。</p>
        <label>收件人</label>
        <input id="to" placeholder="recipient@example.com">
        <label>主题</label>
        <input id="subject">
        <label>正文</label>
        <textarea id="draft-body"></textarea>
        <div class="actions">
          <button class="act" id="copy">复制正文</button>
          <button class="act" id="save">保存到草稿箱</button>
          <button class="act send" id="send">保存并准备发送</button>
          <button class="act send" id="confirm-send" style="display:none;">确认发送已保存草稿</button>
        </div>
      </section>
    </div>
  </section>
</div>
<script>
const $ = (id) => document.getElementById(id);
function setStatus(text) { $('status').textContent = text; }
document.querySelectorAll('.tab').forEach((tab) => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach((t) => t.classList.remove('active'));
    document.querySelectorAll('.tab-panel').forEach((p) => p.classList.remove('active'));
    tab.classList.add('active');
    document.getElementById('tab-' + tab.dataset.tab).classList.add('active');
  });
});
async function api(path, payload) {
  const response = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload || {}),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok || data.error) { throw new Error(data.error || ('HTTP ' + response.status)); }
  return data;
}
document.getElementById('generate').addEventListener('click', async () => {
  const button = $('generate');
  button.disabled = true; setStatus('AI 正在写草稿…');
  try {
    const data = await api('/api/ai-draft', { instruction: $('instruction').value });
    $('to').value = data.to || '';
    $('subject').value = data.subject || '';
    $('draft-body').value = data.body || '';
    setStatus('草稿已生成，可直接修改后保存或发送。');
  } catch (error) { setStatus('生成失败：' + error.message); }
  button.disabled = false;
});
document.getElementById('save').addEventListener('click', async () => {
  $('save').disabled = true; setStatus('正在保存草稿…');
  try {
    const data = await api('/api/save-draft', {
      mailbox_id: $('mailbox').value,
      to: $('to').value,
      subject: $('subject').value,
      body: $('draft-body').value,
    });
    setStatus(data.detail || '已保存');
  } catch (error) { setStatus('保存失败：' + error.message); }
  $('save').disabled = false;
});
let stagedPendingId = '';
let stagedMailboxId = '';
function resetStagedDraft(message) {
  stagedPendingId = '';
  stagedMailboxId = '';
  const confirmButton = document.getElementById('confirm-send');
  confirmButton.style.display = 'none';
  confirmButton.disabled = false;
  if (message) setStatus(message);
}
document.getElementById('send').addEventListener('click', async () => {
  const select = $('mailbox');
  if (select.value === 'qq_mail') { setStatus('QQ 邮箱按安全规则不支持发送，只能保存草稿。'); return; }
  resetStagedDraft();
  $('send').disabled = true; setStatus('正在保存待确认草稿…');
  try {
    const data = await api('/api/stage-draft', {
      mailbox_id: select.value,
      to: $('to').value,
      subject: $('subject').value,
      body: $('draft-body').value,
    });
    stagedPendingId = data.pending_id;
    stagedMailboxId = select.value;
    $('confirm-send').style.display = '';
    $('confirm-send').disabled = false;
    setStatus(data.detail || '草稿已保存，请点击确认发送。');
  } catch (error) {
    setStatus('准备失败：' + error.message);
    $('send').disabled = false;
  }
});
document.getElementById('confirm-send').addEventListener('click', async () => {
  const select = $('mailbox');
  if (!stagedPendingId || stagedMailboxId !== select.value) {
    resetStagedDraft('请重新保存待发送草稿。');
    $('send').disabled = false;
    return;
  }
  $('confirm-send').disabled = true; setStatus('正在发送已保存草稿…');
  try {
    const data = await api('/api/send-mail', { pending_id: stagedPendingId });
    resetStagedDraft();
    $('send').disabled = false;
    setStatus(data.detail || '已发送');
  } catch (error) {
    resetStagedDraft('发送失败：' + error.message + ' 如草稿仍在，请重新保存并发送。');
    $('send').disabled = false;
  }
});
document.getElementById('copy').addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText($('draft-body').value);
    setStatus('正文已复制到剪贴板，可粘贴到 Outlook 等邮箱中发送。');
  } catch (error) { setStatus('复制失败，请手动选择正文复制。'); }
});
document.getElementById('refresh').addEventListener('click', async () => {
  $('refresh').disabled = true;
  $('refresh-status').textContent = '后台刷新中…';
  await fetch('/api/refresh', { method: 'POST' });
  const timer = setInterval(async () => {
    const state = await (await fetch('/api/refresh-status')).json();
    if (!state.running) { clearInterval(timer); location.reload(); }
  }, 2000);
});
window.addEventListener('load', async () => {
  try {
    const state = await (await fetch('/api/refresh-status')).json();
    if (state.running) {
      $('refresh-status').textContent = '后台刷新中…';
    } else if (state.last_finished) {
      $('refresh-status').textContent =
        '最近更新：' + state.last_finished.replace('T', ' ').slice(0, 16);
    }
  } catch (error) { /* status is optional */ }
});
const mailboxSelect = document.getElementById('mailbox');
const noticeBox = document.getElementById('mailbox-notice');
function refreshMailboxUi() {
  const isMaster = mailboxSelect.value === 'master_mail';
  noticeBox.textContent = isMaster
    ? '巴黎萨克雷（Outlook）：学校要求管理员批准自动写权限，暂无法自动存草稿或发送。可生成草稿后点击"复制正文"，到 Outlook 手动粘贴发送。'
    : '';
  noticeBox.style.display = isMaster ? 'block' : 'none';
  document.getElementById('save').disabled = isMaster;
  document.getElementById('send').disabled = isMaster || mailboxSelect.value === 'qq_mail';
  resetStagedDraft();
}
mailboxSelect.addEventListener('change', refreshMailboxUi);
refreshMailboxUi();
['to', 'subject', 'draft-body'].forEach((id) => {
  document.getElementById(id).addEventListener('input', () => {
    if (stagedPendingId) resetStagedDraft('草稿字段已修改，请重新保存并准备发送。');
  });
});
const EXAMPLES = [
  '给课程负责人写一封简短邮件，说明实习报告初稿已完成，询问本周内能否给出反馈',
  '写一封请假邮件，因身体不适申请明天请假一天，语气诚恳',
  '写一封邮件预约学校图书馆讨论间，本周四下午两点，时长两小时',
];
const examplesBox = document.querySelector('.examples');
for (const [index, text] of EXAMPLES.entries()) {
  const chip = document.createElement('span');
  chip.textContent = (index + 1) + '. ' + text.slice(0, 14) + '…';
  chip.title = text;
  chip.addEventListener('click', () => { document.getElementById('instruction').value = text; });
  examplesBox.appendChild(chip);
}
async function loadStats() {
  try {
    const stats = await (await fetch('/api/stats')).json();
    const names = { qq_mail: 'QQ 邮箱', bachelor_mail: '传媒大学', master_mail: '巴黎萨克雷' };
    const tiles = document.getElementById('tiles');
    tiles.innerHTML = '';
    for (const box of stats.mailboxes) {
      const tile = document.createElement('div');
      tile.className = 'tile' + (box.ok ? '' : ' off');
      const count = box.ok ? (box.count + ' 封') : '失败';
      tile.innerHTML = '<div class="name">' + box.name + '</div>' +
        '<div class="count">' + count + '</div>' +
        '<div class="sub">过去 24 小时</div>';
      tiles.appendChild(tile);
    }
  } catch (error) { /* stats are optional */ }
}
loadStats();
</script>
</body>
</html>
"""
