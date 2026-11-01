/* 网页视频剪辑器 — 前端逻辑（无构建，原生 JS + ArtPlayer） */
(function () {
  'use strict';

  // ---------------- 状态 ----------------
  const state = {
    path: null,
    name: null,
    duration: 0,
    fps: 30,
    inPoint: 0,
    outPoint: 0,
    art: null,
    activeOp: 'trim',
    pendingFile: null,
    tasks: {},
  };

  const $ = (id) => document.getElementById(id);

  // ---------------- 工具 ----------------
  function fmtTime(s) {
    if (!Number.isFinite(s)) s = 0;
    const sign = s < 0 ? '-' : '';
    s = Math.abs(s);
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = s % 60;
    const mm = String(m).padStart(2, '0');
    const ss = sec.toFixed(2).padStart(5, '0');
    return h > 0 ? `${sign}${h}:${mm}:${ss}` : `${sign}${mm}:${ss}`;
  }

  function fmtBytes(n) {
    if (!Number.isFinite(n)) return '-';
    const units = ['B', 'KB', 'MB', 'GB'];
    let i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return `${n.toFixed(i ? 1 : 0)} ${units[i]}`;
  }

  function status(msg, cls) {
    const el = $('statusBar');
    el.textContent = msg;
    el.className = 'statusbar' + (cls ? ' ' + cls : '');
  }

  function err(msg) { status(msg, 'error'); }

  async function api(url, options) {
    const res = await fetch(url, options);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    return data;
  }

  // ---------------- 加载文件 ----------------
  async function loadFromPath(path, name) {
    status('探测媒体信息…');
    try {
      const info = await api('/api/probe?path=' + encodeURIComponent(path));
      state.path = info.path;
      state.name = name || info.name;
      state.duration = info.duration || 0;
      state.fps = (info.video && info.video.fps) || 30;
      state.inPoint = 0;
      state.outPoint = state.duration;
      $('pathInput').value = info.path;
      renderMediaInfo(info);
      initPlayer(info.path);
      syncSelectionUI();
      $('downloadSourceBtn').disabled = false;
      status(`已载入：${info.name}（${fmtTime(info.duration)}）`, 'ok');
      loadTimelineAssets(info.path, info.duration);
    } catch (e) {
      err('载入失败：' + e.message);
    }
  }

  function renderMediaInfo(info) {
    const rows = [
      ['文件名', info.name],
      ['路径', info.path],
      ['时长', fmtTime(info.duration)],
      ['大小', fmtBytes(info.size)],
      ['容器', info.format_name || '-'],
    ];
    if (info.video) {
      rows.push(['视频', `${info.video.codec_name} ${info.video.width}×${info.video.height} @ ${(info.video.fps || 0).toFixed(2)}fps`]);
    }
    if (info.audio) {
      rows.push(['音频', `${info.audio.codec_name} ${info.audio.channels || ''}ch ${(info.audio.sample_rate / 1000 || 0).toFixed(1)}kHz`]);
    } else {
      rows.push(['音频', '无音轨']);
    }
    $('mediaInfo').innerHTML =
      '<table><tbody>' +
      rows.map(([k, v]) => `<tr><td>${k}</td><td>${v == null ? '-' : String(v)}</td></tr>`).join('') +
      '</tbody></table>';
  }

  async function loadTimelineAssets(path, duration) {
    try {
      const assets = await api(`/api/timeline?path=${encodeURIComponent(path)}&duration=${duration || 0}`);
      const thumbs = $('tlThumbs');
      thumbs.innerHTML = '';
      if (assets.sprite) {
        for (let i = 0; i < assets.cols; i++) {
          const cell = document.createElement('div');
          cell.className = 'tl-thumb';
          cell.style.backgroundImage = `url("${assets.sprite}")`;
          cell.style.backgroundPositionX = `${(i / (assets.cols - 1)) * 100}%`;
          thumbs.appendChild(cell);
        }
      }
      const wave = $('tlWave');
      wave.style.backgroundImage = assets.wave ? `url("${assets.wave}")` : 'none';
    } catch (e) {
      status('时间线素材生成失败：' + e.message, 'error');
    }
  }

  // ---------------- 播放器 ----------------
  function initPlayer(path) {
    const container = $('artplayer');
    if (state.art) {
      try { state.art.destroy(); } catch (_) { /* ignore */ }
      state.art = null;
    }
    container.innerHTML = '';
    if (typeof Artplayer === 'undefined') {
      err('ArtPlayer 未加载，请检查 public/vendor/artplayer.js');
      return;
    }
    const url = '/api/media?path=' + encodeURIComponent(path);
    const art = new Artplayer({
      container: '#artplayer',
      url,
      lang: 'zh-cn',
      theme: '#378add',
      autoplay: false,
      playbackRate: true,
      aspectRatio: true,
      fullscreen: true,
      fullscreenWeb: true,
      pip: true,
      setting: true,
      hotkey: false,
      lock: true,
      fastForward: true,
    });
    state.art = art;

    art.on('video:timeupdate', () => {
      updateTimeUI();
      const t = art.currentTime || 0;
      $('frameTime').textContent = fmtTime(t);
    });
    art.on('video:loadedmetadata', () => updateTimeUI());
    art.on('error', (e) => err('播放出错：' + (e && e.message ? e.message : JSON.stringify(e))));
    art.on('ready', () => updateTimeUI());
  }

  function currentTime() {
    return (state.art && Number.isFinite(state.art.currentTime)) ? state.art.currentTime : 0;
  }

  function seek(t) {
    if (!state.art) return;
    const v = Math.max(0, Math.min(state.duration || t, t));
    state.art.seek(v);
    updateTimeUI();
  }

  function frameStep(dir) {
    const step = 1 / (state.fps || 30);
    seek(currentTime() + dir * step);
    if (state.art) state.art.pause();
  }

  // ---------------- 时间线 ----------------
  function pctOf(t) {
    if (!state.duration) return 0;
    return Math.max(0, Math.min(1, t / state.duration)) * 100;
  }

  function syncSelectionUI() {
    const a = pctOf(state.inPoint);
    const b = pctOf(state.outPoint);
    const sel = $('tlSelection');
    sel.style.left = a + '%';
    sel.style.width = Math.max(0, b - a) + '%';
    $('handleIn').style.left = a + '%';
    $('handleOut').style.left = b + '%';
    $('inTime').value = state.inPoint.toFixed(2);
    $('outTime').value = state.outPoint.toFixed(2);
    const len = Math.max(0, state.outPoint - state.inPoint);
    $('rangeInfo').textContent = state.duration
      ? `区间 ${fmtTime(state.inPoint)} ~ ${fmtTime(state.outPoint)}（时长 ${fmtTime(len)}）`
      : '未选择区间';
    updateTimeUI();
  }

  function updateTimeUI() {
    const t = currentTime();
    $('timeInfo').textContent = `${fmtTime(t)} / ${fmtTime(state.duration)}`;
    $('tlHead').style.left = pctOf(t) + '%';
    $('frameTime').textContent = fmtTime(t);
  }

  function setIn(t) {
    state.inPoint = Math.max(0, Math.min(t, state.outPoint - 0.05));
    if (state.outPoint <= state.inPoint) state.outPoint = Math.min(state.duration, state.inPoint + 0.05);
    syncSelectionUI();
    updateKeyframeHint();
  }

  function setOut(t) {
    state.outPoint = Math.min(state.duration, Math.max(t, state.inPoint + 0.05));
    syncSelectionUI();
    updateKeyframeHint();
  }

  let kfTimer = null;
  function updateKeyframeHint() {
    if (!state.path || !state.duration) return;
    clearTimeout(kfTimer);
    kfTimer = setTimeout(async () => {
      try {
        const data = await api(`/api/keyframes?path=${encodeURIComponent(state.path)}&at=${state.inPoint}&window=4`);
        const frames = data.frames || [];
        const aligned = frames.filter((f) => f.time <= state.inPoint + 1e-6).pop();
        if (aligned) {
          $('keyframeInfo').textContent =
            `无损切点对齐 ≈ ${fmtTime(aligned.time)}（偏移 ${(state.inPoint - aligned.time).toFixed(2)}s）`;
        } else {
          $('keyframeInfo').textContent = '';
        }
      } catch (_) {
        $('keyframeInfo').textContent = '';
      }
    }, 400);
  }

  function bindTimeline() {
    const tl = $('timeline');
    let dragging = null; // 'scrub' | 'in' | 'out'

    function timeAt(clientX) {
      const rect = tl.getBoundingClientRect();
      const ratio = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
      return ratio * (state.duration || 0);
    }

    tl.addEventListener('mousedown', (e) => {
      if (!state.duration) return;
      const target = e.target;
      if (target.id === 'handleIn') dragging = 'in';
      else if (target.id === 'handleOut') dragging = 'out';
      else {
        dragging = 'scrub';
        seek(timeAt(e.clientX));
      }
      e.preventDefault();
    });

    window.addEventListener('mousemove', (e) => {
      if (!dragging) return;
      const t = timeAt(e.clientX);
      if (dragging === 'scrub') seek(t);
      else if (dragging === 'in') setIn(t);
      else if (dragging === 'out') setOut(t);
    });

    window.addEventListener('mouseup', () => { dragging = null; });
  }

  // ---------------- 操作面板 ----------------
  function switchOp(op) {
    state.activeOp = op;
    document.querySelectorAll('.tab').forEach((b) => b.classList.toggle('active', b.dataset.op === op));
    document.querySelectorAll('.op-form').forEach((f) => f.classList.toggle('active', f.dataset.op === op));
    $('cmdPreview').hidden = true;
  }

  function buildTaskPayload() {
    const op = state.activeOp;
    if (!state.path && op !== 'concat') throw new Error('请先载入视频文件');
    switch (op) {
      case 'trim': {
        if (state.outPoint <= state.inPoint) throw new Error('出点必须大于入点');
        return {
          type: 'trim',
          path: state.path,
          start: state.inPoint,
          end: state.outPoint,
          mode: $('trimMode').value,
          crf: Number($('trimCrf').value),
          preset: $('trimPreset').value,
        };
      }
      case 'cut-middle': {
        return {
          type: 'cut-middle',
          path: state.path,
          in: state.inPoint,
          out: state.outPoint,
          duration: state.duration,
          mode: $('cutMode').value,
          crf: Number($('cutCrf').value),
          preset: $('cutPreset').value,
        };
      }
      case 'concat': {
        const paths = $('concatPaths').value.split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
        return {
          type: 'concat',
          paths,
          mode: $('concatMode').value,
          crf: Number($('concatCrf').value),
        };
      }
      case 'audio': {
        const v = $('audioAction').value; // extract_m4a | extract_mp3 | remove
        if (v === 'remove') return { type: 'audio', path: state.path, action: 'remove' };
        return { type: 'audio', path: state.path, action: 'extract', format: v.endsWith('mp3') ? 'mp3' : 'm4a' };
      }
      case 'frame': {
        return { type: 'frame', path: state.path, time: currentTime(), format: $('frameFormat').value };
      }
      case 'transcode': {
        return {
          type: 'transcode',
          path: state.path,
          format: $('tcFormat').value,
          crf: Number($('tcCrf').value),
          preset: $('tcPreset').value,
          scale: $('tcScale').value ? Number($('tcScale').value) : null,
        };
      }
      default:
        throw new Error('未知操作');
    }
  }

  async function previewCommand() {
    try {
      const payload = buildTaskPayload();
      payload.dryRun = true;
      const res = await api('/api/tasks', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const el = $('cmdPreview');
      el.hidden = false;
      el.textContent = res.command + (res.outName ? `\n\n→ 输出: ${res.outName}` : '');
      status('命令预览已生成（未执行）');
    } catch (e) {
      err('预览失败：' + e.message);
    }
  }

  async function executeTask() {
    try {
      const payload = buildTaskPayload();
      const res = await api('/api/tasks', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      status(`任务已创建：${res.label}`);
      state.tasks[res.id] = res;
      renderTasks();
      watchTask(res.id);
    } catch (e) {
      err('执行失败：' + e.message);
    }
  }

  // ---------------- 任务列表 ----------------
  function renderTasks() {
    const ids = Object.keys(state.tasks).sort((a, b) => (state.tasks[b].createdAt || 0) - (state.tasks[a].createdAt || 0));
    const box = $('taskList');
    if (!ids.length) {
      box.innerHTML = '<p class="hint">暂无任务</p>';
      return;
    }
    box.innerHTML = ids.map((id) => {
      const t = state.tasks[id];
      const pct = Math.round((t.progress || 0) * 100);
      const badgeCls = t.status === 'running' ? 'running' : t.status === 'done' ? 'done' : t.status === 'error' ? 'error' : '';
      const statusText = { queued: '排队中', running: '执行中', done: '完成', error: '失败' }[t.status] || t.status;
      const canCancel = t.status === 'running' || t.status === 'queued';
      return `
        <div class="task-item" data-id="${id}">
          <div class="task-head">
            <span class="task-label">${t.label || id}</span>
            <span class="badge ${badgeCls}">${statusText}</span>
            <div class="progress"><i style="width:${pct}%"></i></div>
            <div class="task-actions">
              ${t.status === 'done' && t.outPath
                ? `<a class="btn small" href="/api/download?path=${encodeURIComponent(t.outPath)}&name=${encodeURIComponent(t.outName || 'output')}">下载</a>`
                : ''}
              ${canCancel ? `<button class="btn small" data-cancel="${id}">取消</button>` : ''}
            </div>
          </div>
          ${t.error ? `<p class="task-error">${t.error}</p>` : ''}
          <details class="task-cmd"><summary>ffmpeg 命令</summary><code>${t.command || ''}</code></details>
        </div>`;
    }).join('');

    box.querySelectorAll('[data-cancel]').forEach((btn) => {
      btn.addEventListener('click', async () => {
        try {
          await api(`/api/tasks/${btn.dataset.cancel}/cancel`, { method: 'POST' });
          status('已请求取消');
        } catch (e) {
          err('取消失败：' + e.message);
        }
      });
    });
  }

  function watchTask(id) {
    if (!window.EventSource) return;
    const es = new EventSource(`/api/tasks/${id}/events`);
    es.onmessage = (ev) => {
      let data;
      try { data = JSON.parse(ev.data); } catch (_) { return; }
      if (data.id) {
        state.tasks[data.id] = Object.assign({}, state.tasks[data.id], data);
        renderTasks();
      }
      if (data.type === 'status' && (data.status === 'done' || data.status === 'error')) {
        es.close();
        if (data.status === 'done') status(`任务完成：${data.outName}（已可下载）`, 'ok');
        else err('任务失败：' + (data.error || '未知错误'));
      }
    };
    es.onerror = () => es.close();
  }

  async function refreshTasks() {
    try {
      const data = await api('/api/tasks');
      for (const t of data.tasks || []) {
        state.tasks[t.id] = t;
        if (t.status === 'running' || t.status === 'queued') watchTask(t.id);
      }
      renderTasks();
    } catch (e) {
      err('刷新任务失败：' + e.message);
    }
  }

  // ---------------- 上传 ----------------
  function uploadFile(file) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open('POST', '/api/upload?name=' + encodeURIComponent(file.name));
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) status(`上传中 ${Math.round((e.loaded / e.total) * 100)}%…`);
      };
      xhr.onload = () => {
        try {
          const data = JSON.parse(xhr.responseText);
          if (xhr.status >= 200 && xhr.status < 300) resolve(data);
          else reject(new Error(data.error || '上传失败'));
        } catch (_) {
          reject(new Error('上传响应解析失败'));
        }
      };
      xhr.onerror = () => reject(new Error('上传失败'));
      xhr.send(file);
    });
  }

  async function handleFile(file) {
    if (!file) return;
    state.pendingFile = file;
    try {
      status(`上传 ${file.name} …`);
      const data = await uploadFile(file);
      await loadFromPath(data.path, file.name);
    } catch (e) {
      err(e.message);
    }
  }

  // ---------------- 快捷键 ----------------
  function bindHotkeys() {
    document.addEventListener('keydown', (e) => {
      const tag = (e.target.tagName || '').toLowerCase();
      if (tag === 'input' || tag === 'textarea' || tag === 'select' || e.target.isContentEditable) return;
      switch (e.key) {
        case ' ':
          e.preventDefault();
          if (!state.art) return;
          if (state.art.playing) state.art.pause();
          else state.art.play();
          break;
        case 'i': case 'I': if (state.path) setIn(currentTime()); break;
        case 'o': case 'O': if (state.path) setOut(currentTime()); break;
        case 'ArrowLeft':
          e.preventDefault();
          if (state.path) e.shiftKey ? seek(currentTime() - 1) : frameStep(-1);
          break;
        case 'ArrowRight':
          e.preventDefault();
          if (state.path) e.shiftKey ? seek(currentTime() + 1) : frameStep(1);
          break;
        case 'Home': if (state.path) seek(state.inPoint); break;
        case 'End': if (state.path) seek(state.outPoint); break;
        default: break;
      }
    });
  }

  // ---------------- 初始化 ----------------
  function bindUI() {
    $('fileInput').addEventListener('change', (e) => handleFile(e.target.files[0]));
    $('loadPathBtn').addEventListener('click', () => {
      const p = $('pathInput').value.trim();
      if (!p) return err('请输入文件路径');
      loadFromPath(p);
    });
    $('pathInput').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') $('loadPathBtn').click();
    });
    $('downloadSourceBtn').addEventListener('click', () => {
      if (!state.path) return;
      const a = document.createElement('a');
      a.href = `/api/download?path=${encodeURIComponent(state.path)}&name=${encodeURIComponent(state.name || 'video')}`;
      a.click();
    });

    // 拖拽文件
    document.addEventListener('dragover', (e) => e.preventDefault());
    document.addEventListener('drop', (e) => {
      e.preventDefault();
      const f = e.dataTransfer.files && e.dataTransfer.files[0];
      if (f) handleFile(f);
    });

    $('markInBtn').addEventListener('click', () => setIn(currentTime()));
    $('markOutBtn').addEventListener('click', () => setOut(currentTime()));
    $('inTime').addEventListener('change', (e) => setIn(Number(e.target.value)));
    $('outTime').addEventListener('change', (e) => setOut(Number(e.target.value)));

    $('opTabs').addEventListener('click', (e) => {
      const btn = e.target.closest('.tab');
      if (btn) switchOp(btn.dataset.op);
    });

    $('trimMode').addEventListener('change', () => {
      $('trimEncodeFields').style.opacity = $('trimMode').value === 'precise' ? '1' : '0.4';
    });
    $('trimEncodeFields').style.opacity = '0.4';

    $('addCurrentToConcat').addEventListener('click', () => {
      if (!state.path) return err('尚未载入文件');
      const ta = $('concatPaths');
      const lines = ta.value.split(/\r?\n/).filter(Boolean);
      lines.push(state.path);
      ta.value = lines.join('\n');
    });

    $('previewCmdBtn').addEventListener('click', previewCommand);
    $('runBtn').addEventListener('click', executeTask);
    $('refreshTasksBtn').addEventListener('click', refreshTasks);

    bindTimeline();
    bindHotkeys();
  }

  async function init() {
    bindUI();
    try {
      const h = await api('/api/health');
      $('healthInfo').textContent = h.version || 'ffmpeg 已就绪';
    } catch (e) {
      $('healthInfo').textContent = '后端不可用';
      err('无法连接本地服务：' + e.message);
    }
    refreshTasks();
  }

  document.addEventListener('DOMContentLoaded', init);
})();
