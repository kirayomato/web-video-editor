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
    viewStart: 0, // 时间线可见窗口 [viewStart, viewEnd]；0,0 = 全览
    viewEnd: 0,
    art: null,
    activeOp: 'trim',
    pendingFile: null,
    tasks: {},
    cursor: 0, // 播放头位置：未加载播放器时由时间线点击/方向键驱动
    scrubPos: null, // 拖动时间线时的预览位置；非 null 时光标显示此值，不写播放器
    playerEnabled: false, // 是否加载浏览器播放器；默认 false = 仅用 ffmpeg 读元数据
    audioOnly: false, // 当前播放器是否使用纯音频流
    audioReady: false, // 纯音频缓存是否就绪，决定切仅听声能否直接设 src
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

  // 把 HTMLMediaElement.error（MediaError）翻译成可读中文
  function describeMediaError(me) {
    if (!me) return '';
    const codeMap = {
      1: 'MEDIA_ERR_ABORTED：播放被中止（可能被脚本或用户中断）',
      2: 'MEDIA_ERR_NETWORK：网络/请求中断（Range 请求或服务端异常，导致数据不完整）',
      3: 'MEDIA_ERR_DECODE：媒体数据解码失败（文件损坏，或浏览器不支持该编码/容器）',
      4: 'MEDIA_ERR_SRC_NOT_SUPPORTED：源不支持 / 文件找不到 / MIME 不正确 / 服务器拒绝',
    };
    const base = codeMap[me.code] || `MEDIA_ERR(未知 code=${me.code})`;
    return me.message ? `${base} — ${me.message}` : base;
  }

  // 统一上报播放错误：从 <video> 取真正的 MediaError，并带上正在播放的 URL
  function reportPlaybackError(art, playedUrl, retry) {
    const v = art && art.video;
    let detail;
    const me = v && v.error; // 真正的错误藏在 video 元素上，事件对象本身没有 message
    if (me) {
      detail = describeMediaError(me);
    } else if (typeof playedUrl === 'string' && playedUrl.includes('/api/media')) {
      detail = '媒体加载失败（事件未携带 MediaError，可能是连接被拒或响应非媒体）';
    } else {
      detail = '未知播放错误';
    }
    const url = (v && (v.currentSrc || v.src)) || playedUrl || '';
    const retryInfo = (typeof retry === 'number' && retry > 0) ? `（已自动重连 ${retry} 次仍失败）` : '';
    err(`播放出错${retryInfo}：${detail}｜URL: ${url}`);
    // 额外探测：直接 HEAD 请求媒体地址，暴露 404/403/500 等 HTTP 错误（路径问题最常见）
    if (url) {
      fetch(url, { method: 'HEAD' }).then((r) => {
        if (!r.ok) err(`媒体地址返回 HTTP ${r.status} ${r.statusText}｜${url}`);
      }).catch(() => { /* 网络层错误已通过 MediaError 体现，无需重复 */ });
    }
  }

  async function api(url, options) {
    const res = await fetch(url, options);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    return data;
  }

  // 清洗粘贴路径：去空白/BOM/成对引号（Windows「复制为路径」自带引号）
  function cleanPath(s) {
    s = (s || '').replace(/^\uFEFF/, '').trim();
    while (s.length >= 2 && s[0] === s[s.length - 1] && '"\'“”‘’«»'.includes(s[0])) {
      s = s.slice(1, -1).trim();
    }
    return s.replace(/^["'“”‘’«»]+|["'“”‘’«»]+$/g, '');
  }

  // ---------------- 加载文件 ----------------
  let prepareToken = 0;
  let prepareCancelled = false;
  const PREPARE_INTERVAL_MS = 800;
  const PREPARE_TIMEOUT_MS = 10 * 60 * 1000;
  const PREPARE_MAX_POLLS = Math.ceil(PREPARE_TIMEOUT_MS / PREPARE_INTERVAL_MS);

  function showPrepareUI(on) {
    $('prepareBox').hidden = !on;
    $('cancelPrepareBtn').disabled = false;
    if (on) setPrepareProgress(0);
  }

  function setPrepareProgress(pct) {
    const v = Math.max(0, Math.min(100, Math.round(pct)));
    $('prepareProgress').value = v;
    $('preparePercent').textContent = v + '%';
    $('playerModeHint').textContent = `正在准备纯音频… ${v}%`;
  }

  // 短请求轮询后台抽音轨状态；返回 null 表示本次准备已作废，调用方应丢弃结果
  async function prepareAudio(path, token, restart = false) {
    const url = '/api/prepare?path=' + encodeURIComponent(path);
    const firstUrl = restart ? url + '&restart=1' : url;
    for (let i = 0; i < PREPARE_MAX_POLLS; i++) {
      if (prepareCancelled || token !== prepareToken) return null;
      let res;
      try {
        res = await api(i === 0 ? firstUrl : url);
      } catch (e) {
        if (prepareCancelled || token !== prepareToken) return null;
        status('音频准备请求失败，未加载播放器：' + e.message, 'warn');
        return false;
      }
      if (prepareCancelled || token !== prepareToken) return null;
      if (res.ready) { setPrepareProgress(100); return true; }
      if (res.cancelled) {
        status('音轨准备已终止，未加载播放器；点「仅听声」可重新准备', 'warn');
        return false;
      }
      if (res.error) {
        status('音轨抽取失败，未加载播放器；点「仅听声」可重试：' + res.error, 'warn');
        return false;
      }
      setPrepareProgress((res.progress || 0) * 100);
      if (i === 0) status('正在准备纯音频，完成后自动加载播放器…');
      await new Promise((resolve) => setTimeout(resolve, PREPARE_INTERVAL_MS));
    }
    try {
      await api('/api/prepare/cancel?path=' + encodeURIComponent(path), { method: 'POST' });
    } catch (_) { /* 终止失败时状态栏仍给出提示，不阻断后续重试 */ }
    status(`音轨准备超时（${PREPARE_TIMEOUT_MS / 60000} 分钟），已终止后台抽取；点「仅听声」可重试`, 'warn');
    return false;
  }

  async function prepareAndPlay(path, keepVideo, restart, token) {
    prepareCancelled = false;
    showPlayerPlaceholder();
    showPrepareUI(true);
    const ready = await prepareAudio(path, token, restart);
    if (prepareCancelled || token !== prepareToken || ready === null) return;
    showPrepareUI(false);
    if (ready !== true) {
      $('playerModeHint').textContent = '纯音频未就绪，未加载播放器；可点「仅听声」重试';
      return;
    }
    state.audioReady = true;
    loadPlayer(!keepVideo);
    // 音轨缓存就绪后才生成波形，波形直接吃缓存，不再扫完整源视频
    loadTimelineAssets(path, state.duration);
  }

  async function loadFromPath(path, name) {
    path = cleanPath(path);
    if (!path) { err('请提供视频文件路径'); return; }
    const token = ++prepareToken;
    state.audioReady = false;
    // 关键：先记下路径——即使 ffprobe / 浏览器播放失败，也要保证后续 ffmpeg 剪辑可用
    state.path = path;
    state.name = name || path.split(/[\\/]/).pop() || 'video';
    state.inPoint = 0;
    state.outPoint = 0;
    state.viewStart = 0;
    state.viewEnd = 0;
    state.cursor = 0;
    state.scrubPos = null;
    $('pathInput').value = path;
    $('downloadSourceBtn').disabled = false;

    status('探测媒体信息…');
    let hasAudio = false;
    try {
      const info = await api('/api/probe?path=' + encodeURIComponent(path));
      hasAudio = !!info.audio;
      state.duration = info.duration || 0;
      state.fps = (info.video && info.video.fps) || 30;
      state.outPoint = state.duration;
      state.viewEnd = state.duration;
      state.name = name || info.name;
      renderMediaInfo(info);
      status(`已载入：${info.name}（${fmtTime(info.duration)}）`, 'ok');
    } catch (e) {
      // 读取/探测失败：不阻塞剪辑。ffmpeg 通常比浏览器/ffprobe 更宽容，仍可直接处理
      state.duration = 0;
      state.fps = 30;
      state.outPoint = 0;
      state.viewEnd = 0;
      renderMediaInfo({ name: state.name, path, duration: 0 });
      status(`⚠ 无法读取媒体信息（${e.message}），但仍可用 ffmpeg 剪辑；请在右侧手动填写入/出点区间。`, 'warn');
    }

    syncSelectionUI();
    // 读取失败时跳过，避免覆盖上面的警告
    if (state.duration <= 0) { showPlayerPlaceholder(); return; }

    // 视频模式是用户显式选的，换文件时保持；其余情况默认只听声
    const keepVideo = state.playerEnabled && !state.audioOnly;
    if (!hasAudio) {
      showPrepareUI(false);
      loadPlayer(false);
      loadTimelineAssets(path, state.duration);
      return;
    }
    // 载入即用户显式意图：restart=1 清掉上一轮失败/已终止的终态槽位重新抽，
    // 正在跑的任务不受影响（服务端 restart 仅清理已结束的槽位）
    prepareAndPlay(path, keepVideo, true, token);
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
      resetWave();
      if (assets.wave) {
        try {
          await loadWaveEnvelope(assets.wave);
        } catch (e) {
          status('波形解码失败：' + e.message + '，时间线仍可点击跳转', 'error');
        }
      } else if (assets.reason === 'no_audio') {
        status('文件无音轨，未生成波形，时间线仍可点击跳转');
      } else if (assets.reason === 'failed') {
        status('波形生成失败：' + (assets.error || '未知错误') + '，时间线仍可点击跳转', 'error');
      } else {
        status('未生成波形，时间线仍可点击跳转');
      }
      applyWaveView();
    } catch (e) {
      resetWave();
      applyWaveView();
      status('时间线素材生成失败：' + e.message, 'error');
    }
  }

  // ---------------- 播放器模式（按需加载） ----------------
  let placeholderHTML = '';

  function updatePlayerBar() {
    $('loadPlayerBtn').textContent = state.playerEnabled ? '卸载播放器' : '加载播放器';
    $('audioOnlyBtn').textContent = state.audioOnly ? '恢复画面' : '仅听声';
    $('playerModeHint').textContent = state.audioOnly
      ? '仅听声模式：播放纯音频流，不下载视频轨'
      : state.playerEnabled
        ? '播放器已加载；点击「卸载播放器」可回到仅元数据模式'
        : '仅用 ffmpeg 读取元数据，未加载播放器（可正常剪辑）';
  }

  // 仅元数据模式：销毁播放器、显示占位。占位节点从 HTML 缓存一次，之后复用。
  function showPlayerPlaceholder() {
    const container = $('artplayer');
    const node = $('playerPlaceholder');
    if (node && !placeholderHTML) placeholderHTML = node.outerHTML;
    if (state.art) {
      try { state.art.destroy(); } catch (_) { /* ignore */ }
      state.art = null;
    }
    container.innerHTML = placeholderHTML || '<div class="player-placeholder"></div>';
    state.playerEnabled = false;
    state.audioOnly = false;
    updatePlayerBar();
  }

  function loadPlayer(audioOnly) {
    if (!state.path) return err('请先载入视频文件');
    if (typeof Artplayer === 'undefined') {
      return err('ArtPlayer 未加载，请检查 public/vendor/artplayer.js');
    }
    try {
      initPlayer(state.path, audioOnly);
    } catch (e) {
      showPlayerPlaceholder();
      return err('播放器加载失败：' + e.message);
    }
    if (!state.art) { showPlayerPlaceholder(); return; }
    state.playerEnabled = true;
    state.audioOnly = !!audioOnly;
    updatePlayerBar();
  }

  function togglePlayer() {
    if (state.playerEnabled) {
      showPlayerPlaceholder();
      status('已卸载播放器，回到仅元数据模式', 'ok');
    } else {
      ++prepareToken;
      showPrepareUI(false);
      loadPlayer(false);
    }
  }

  function toggleAudioOnly() {
    if (!$('prepareBox').hidden) return;
    if (state.audioOnly) {
      ++prepareToken;
      showPrepareUI(false);
      loadPlayer(false);
      return;
    }
    if (state.audioReady) { loadPlayer(true); return; }
    if (!state.path) return err('请先载入视频文件');
    prepareAndPlay(state.path, false, true, ++prepareToken);
  }

  // ---------------- 播放器 ----------------
  function initPlayer(path, audioOnly) {
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
    const url = '/api/media?path=' + encodeURIComponent(path)
      + (audioOnly ? '&audio=1' : '');
    const art = new Artplayer({
      container: '#artplayer',
      url,
      lang: 'zh-cn',
      theme: '#378add',
      autoplay: false,
      playbackRate: true,
      // 纯音频资源没有 videoWidth/videoHeight，比例与画中画选项需关闭
      aspectRatio: !audioOnly,
      fullscreen: true,
      fullscreenWeb: true,
      pip: !audioOnly,
      setting: true,
      hotkey: false,
      lock: true,
      fastForward: true,
    });
    state.art = art;

    art.on('video:timeupdate', () => {
      // 拖动预览期间光标由 scrubPos 决定，不被播放位置覆盖
      if (state.scrubPos === null) state.cursor = art.currentTime || 0;
      updateTimeUI();
      ensurePlayheadVisible(); // 缩放后播放头离开可见窗口时把窗口拉回
      $('frameTime').textContent = fmtTime(currentTime());
    });
    art.on('video:loadedmetadata', () => updateTimeUI());
    art.on('error', (e, retry) => reportPlaybackError(art, url, retry));
    art.on('ready', () => updateTimeUI());
  }

  function currentTime() {
    // 拖动预览优先：光标显示的是用户正在拖的位置，播放器实际位置还没动
    if (state.scrubPos !== null) return state.scrubPos;
    // 加载了播放器时随真实播放位置；否则用独立光标（时间线点击/方向键驱动）
    return (state.art && Number.isFinite(state.art.currentTime)) ? state.art.currentTime : state.cursor;
  }

  function seek(t) {
    const v = Math.max(0, Math.min(state.duration || t, t));
    state.cursor = v; // 播放头位置：无论有无播放器都更新
    // ArtPlayer 5.x：seek 是 setter 属性（art.seek = 秒数），不是方法，调用形式会抛 TypeError
    if (state.art) state.art.seek = v;
    updateTimeUI();
    ensurePlayheadVisible();
  }

  // 拖动时间线：只移动光标，不写播放器。
  // 每次写 video.currentTime 都会让浏览器中止旧 Range 请求并重发，
  // 拖一次就是几十个请求；松手时统一跳一次即可。
  function scrubTo(t) {
    const v = Math.max(0, Math.min(state.duration || t, t));
    state.scrubPos = v;
    state.cursor = v;
    updateTimeUI();
    ensurePlayheadVisible();
  }

  function endScrub() {
    if (state.scrubPos === null) return;
    const v = state.scrubPos;
    state.scrubPos = null;
    seek(v); // 真正跳转：整段拖动只产生一次媒体请求
  }

  function frameStep(dir) {
    const step = 1 / (state.fps || 30);
    seek(currentTime() + dir * step);
    if (state.art) state.art.pause();
  }

  // ---------------- 时间线 ----------------
  const M = window.TimelineMath;

  // 当前可见窗口（未缩放时 = 整段时长）
  function view() {
    const d = state.duration || 0;
    if (!(d > 0)) return [0, 0];
    if (!(state.viewEnd > state.viewStart)) return [0, d];
    return [state.viewStart, state.viewEnd];
  }

  function pctOf(t) {
    const [vs, ve] = view();
    return M.timeToPct(t, vs, ve);
  }

  // 波形包络：PNG 只解码一次，按列记下 alpha 上下沿，绘制时按可见窗口映射成
  // 硬边竖条。横向像素由服务端按时长给定，前端只做列映射，不缩放位图。
  const wave = { top: null, bottom: null, cols: 0, rows: 0 };

  function resetWave() {
    wave.top = null;
    wave.bottom = null;
    wave.cols = 0;
    wave.rows = 0;
  }

  function loadWaveEnvelope(url) {
    return new Promise((resolve, reject) => {
      const img = new Image();
      img.onload = () => {
        try {
          extractEnvelope(img);
          resolve();
        } catch (e) {
          reject(e);
        }
      };
      img.onerror = () => reject(new Error('波形图加载失败'));
      img.src = url;
    });
  }

  // 逐列取笔画上下沿；无笔画的静音列落在中线，退化成一条细线
  function extractEnvelope(img) {
    const w = img.naturalWidth;
    const h = img.naturalHeight;
    const off = document.createElement('canvas');
    off.width = w;
    off.height = h;
    const ctx = off.getContext('2d', { willReadFrequently: true });
    ctx.drawImage(img, 0, 0);
    const px = ctx.getImageData(0, 0, w, h).data;
    const top = new Uint16Array(w);
    const bottom = new Uint16Array(w);
    const mid = h >> 1;
    for (let x = 0; x < w; x++) {
      let t = -1;
      let b = -1;
      for (let y = 0; y < h; y++) {
        if (px[(y * w + x) * 4 + 3] > 24) {
          if (t < 0) t = y;
          b = y;
        }
      }
      top[x] = t < 0 ? mid : t;
      bottom[x] = b < 0 ? mid : b;
    }
    wave.top = top;
    wave.bottom = bottom;
    wave.cols = w;
    wave.rows = h;
  }

  // 刻度步长取让相邻标签至少隔开 80px 的整齐值，缩放/平移后刻度密度自适应
  const RULER_STEPS = [0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1800, 3600];

  function rulerLabel(t, step) {
    if (step < 0.1) return t.toFixed(2) + 's';
    if (step < 1) return t.toFixed(1) + 's';
    const s = Math.round(t);
    return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
  }

  function drawRuler(ctx, pw, ph, rulerH, vs, ve, dpr) {
    const span = ve - vs;
    const widthCss = pw / dpr;
    const step = RULER_STEPS.find((s) => s / span * widthCss >= 80) || RULER_STEPS[RULER_STEPS.length - 1];
    const hair = Math.max(1, Math.round(dpr));
    ctx.fillStyle = '#f1f4f8';
    ctx.fillRect(0, 0, pw, rulerH);
    ctx.fillStyle = '#e2e5ea';
    ctx.fillRect(0, rulerH - hair, pw, hair);

    ctx.font = `${11 * dpr}px "Segoe UI", system-ui, sans-serif`;
    ctx.textBaseline = 'middle';
    ctx.textAlign = 'left';
    ctx.lineWidth = hair;
    for (let t = Math.ceil(vs / step - 1e-6) * step; t <= ve + 1e-6; t += step) {
      const x = Math.round((t - vs) / span * pw) + 0.5;
      if (x < 0 || x > pw) continue;
      ctx.strokeStyle = '#eef1f5';
      ctx.beginPath();
      ctx.moveTo(x, rulerH);
      ctx.lineTo(x, ph);
      ctx.stroke();
      ctx.strokeStyle = '#aab6c4';
      ctx.beginPath();
      ctx.moveTo(x, rulerH - 6 * dpr);
      ctx.lineTo(x, rulerH);
      ctx.stroke();
      ctx.fillStyle = '#8892a0';
      ctx.fillText(rulerLabel(t, step), x + 4 * dpr, rulerH / 2);
    }
    ctx.strokeStyle = '#d8dee7';
    const midY = Math.round((rulerH + ph) / 2) + 0.5;
    ctx.beginPath();
    ctx.moveTo(0, midY);
    ctx.lineTo(pw, midY);
    ctx.stroke();
  }

  // 每 2 个 CSS 像素一根竖条，条间留缝；缩小时取覆盖列的上下最值（不丢峰值），
  // 放大时同一列重复铺开（不插值）。外层为峰值包络，内层按中线收缩模拟能量层。
  function drawWave() {
    const canvas = $('tlWaveCanvas');
    if (!canvas) return;
    const box = canvas.parentElement.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    const pw = Math.max(1, Math.round(box.width * dpr));
    const ph = Math.max(1, Math.round(box.height * dpr));
    if (canvas.width !== pw || canvas.height !== ph) {
      canvas.width = pw;
      canvas.height = ph;
    }
    const ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, pw, ph);
    if (!(state.duration > 0)) return;

    const [vs, ve] = view();
    const span = ve - vs;
    if (!(span > 0)) return;

    const rulerH = Math.round(20 * dpr);
    drawRuler(ctx, pw, ph, rulerH, vs, ve, dpr);
    if (!wave.cols) return;

    const waveH = ph - rulerH;
    const midRow = wave.rows / 2;
    const colAtLeft = vs / state.duration * wave.cols;
    const colsPerPx = span / state.duration * wave.cols / pw;
    const stride = Math.max(2, Math.round(2 * dpr));
    const barW = Math.max(1, stride - Math.max(1, Math.round(0.6 * dpr)));
    for (let x = 0; x < pw; x += stride) {
      const c0 = Math.floor(colAtLeft + x * colsPerPx);
      const c1 = Math.max(c0 + 1, Math.floor(colAtLeft + (x + stride) * colsPerPx));
      let t = wave.rows;
      let b = -1;
      for (let c = c0; c < c1; c++) {
        if (c < 0 || c >= wave.cols) continue;
        if (wave.top[c] < t) t = wave.top[c];
        if (wave.bottom[c] > b) b = wave.bottom[c];
      }
      if (b < 0) continue;
      const y = rulerH + t / wave.rows * waveH;
      const hh = Math.max(1, rulerH + (b + 1) / wave.rows * waveH - y);
      ctx.fillStyle = '#185FA5';
      ctx.fillRect(x, y, barW, hh);
      const iy = rulerH + (midRow + (t - midRow) * 0.45) / wave.rows * waveH;
      const ih = Math.max(1, rulerH + (midRow + (b - midRow) * 0.45 + 1) / wave.rows * waveH - iy);
      ctx.fillStyle = '#0C447C';
      ctx.fillRect(x, iy, barW, ih);
    }
  }

  function applyWaveView() {
    drawWave();
    updateViewInfo();
  }

  function updateViewInfo() {
    const [vs, ve] = view();
    const d = state.duration || 0;
    const el = $('viewInfo');
    if (!el) return;
    if (!(d > 0)) { el.textContent = ''; return; }
    const full = ve - vs >= d - 1e-6;
    const zoom = (d / Math.max(ve - vs, 1e-6));
    el.textContent = full
      ? `视图 全览（${fmtTime(d)}）`
      : `视图 ${fmtTime(vs)} ~ ${fmtTime(ve)}（窗口 ${fmtTime(ve - vs)}，${zoom.toFixed(1)}x）`;
  }

  function setView(vs, ve) {
    state.viewStart = vs;
    state.viewEnd = ve;
    applyWaveView();
    syncSelectionUI();
  }

  // 以当前播放位置为中心缩放：可见窗口始终围绕播放位置展开/收缩（贴边时钳制到 [0, duration]）
  function zoomTimeline(factor) {
    const d = state.duration;
    if (!d) return;
    const [vs, ve] = view();
    const anchor = currentTime();
    const minSpan = Math.max(1 / (state.fps || 30), 0.05); // 至少一帧，且不小于 50ms
    const [ns, ne] = M.zoomAt(vs, ve, d, anchor, 0.5, factor, minSpan);
    setView(ns, ne);
  }

  // 横向平移（Shift+滚轮 / 触控板横向滑动）；frac 为窗口宽度比例
  function panTimeline(frac) {
    const d = state.duration;
    if (!d) return;
    const [vs, ve] = view();
    const [ns, ne] = M.panView(vs, ve, d, frac);
    setView(ns, ne);
  }

  function resetView() {
    state.viewStart = 0;
    state.viewEnd = state.duration || 0;
    applyWaveView();
    syncSelectionUI();
  }

  // 播放/跳转导致播放头离开可见窗口时，把窗口拉回（播放头居中）
  function ensurePlayheadVisible() {
    const d = state.duration;
    if (!d) return;
    const [vs, ve] = view();
    const [ns, ne] = M.followAnchor(vs, ve, d, currentTime());
    if (ns !== vs || ne !== ve) setView(ns, ne);
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
    if (state.duration) {
      $('rangeInfo').textContent = `区间 ${fmtTime(state.inPoint)} ~ ${fmtTime(state.outPoint)}（时长 ${fmtTime(len)}）`;
    } else if (state.outPoint > state.inPoint) {
      $('rangeInfo').textContent = `区间 ${fmtTime(state.inPoint)} ~ ${fmtTime(state.outPoint)}（时长未知，已手动填写）`;
    } else {
      $('rangeInfo').textContent = '时长未知：请在右侧入/出点输入框手动填写区间';
    }
    updateTimeUI();
  }

  function updateTimeUI() {
    const t = currentTime();
    $('timeInfo').textContent = `${fmtTime(t)} / ${fmtTime(state.duration)}`;
    $('tlHead').style.left = pctOf(t) + '%';
    $('frameTime').textContent = fmtTime(t);
  }

  // 时长未知（读取失败）时返回 Infinity，入/出点不再被 0 时长夹死，仍可手动填写区间
  function _maxTime() { return state.duration > 0 ? state.duration : Infinity; }

  function setIn(t) {
    t = Number(t) || 0;
    const hi = _maxTime();
    state.inPoint = Math.max(0, Math.min(t, hi));
    // 入点顶到出点时，自动把出点外推（受已知时长上限约束；时长未知时为无穷大）
    if (state.outPoint <= state.inPoint) {
      state.outPoint = Math.min(hi, state.inPoint + 0.05);
    }
    syncSelectionUI();
    updateKeyframeHint();
  }

  function setOut(t) {
    t = Number(t) || 0;
    state.outPoint = Math.min(_maxTime(), Math.max(t, state.inPoint + 0.05));
    syncSelectionUI();
    updateKeyframeHint();
  }

  // 用播放头/光标标记入/出点：加载了播放器时取真实位置，否则取独立光标定位
  function markFromPlayhead(which) {
    if (!state.path) return err('请先载入视频文件');
    if (which === 'in') setIn(currentTime());
    else setOut(currentTime());
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
      const ratio = M.clamp((clientX - rect.left) / rect.width, 0, 1);
      const [vs, ve] = view();
      return M.ratioToTime(ratio, vs, ve);
    }

    tl.addEventListener('mousedown', (e) => {
      if (!state.duration) return;
      const target = e.target;
      if (target.id === 'handleIn') dragging = 'in';
      else if (target.id === 'handleOut') dragging = 'out';
      else {
        dragging = 'scrub';
        // 只移动光标；真正跳转推迟到 mouseup，避免连续拖动产生请求风暴
        scrubTo(timeAt(e.clientX));
      }
      e.preventDefault();
    });

    window.addEventListener('mousemove', (e) => {
      if (!dragging) return;
      const t = timeAt(e.clientX);
      if (dragging === 'scrub') scrubTo(t);
      else if (dragging === 'in') setIn(t);
      else if (dragging === 'out') setOut(t);
    });

    window.addEventListener('mouseup', () => {
      if (dragging === 'scrub') endScrub();
      dragging = null;
    });
    // 拖到窗口外松手时收尾，否则预览位置会一直卡住
    document.addEventListener('mouseleave', () => {
      if (dragging === 'scrub') endScrub();
      dragging = null;
    });

    // 滚轮缩放：以当前播放位置为中心（播放位置在窗口外则拉回居中）
    // Shift+滚轮 / 触控板横向滑动：平移视图
    tl.addEventListener('wheel', (e) => {
      if (!state.duration) return;
      e.preventDefault();
      const dx = e.deltaX || 0;
      const dy = e.deltaY || 0;
      const horizontal = e.shiftKey || Math.abs(dx) > Math.abs(dy);
      if (horizontal) {
        const delta = (e.shiftKey && Math.abs(dx) <= Math.abs(dy)) ? dy : dx;
        panTimeline(M.clamp(delta * 0.002, -0.5, 0.5));
      } else {
        // 向上滚（deltaY < 0）放大；指数映射对触控板/滚轮都平滑
        const factor = M.clamp(Math.exp(-dy * 0.0025), 0.5, 2);
        zoomTimeline(factor);
      }
    }, { passive: false });

    // 双击时间线空白处：恢复全览
    tl.addEventListener('dblclick', (e) => {
      if (!state.duration) return;
      if (e.target.id === 'handleIn' || e.target.id === 'handleOut') return;
      resetView();
    });

    // 容器尺寸变化后重设画布分辨率并重绘
    window.addEventListener('resize', () => applyWaveView());
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
        const paths = $('concatPaths').value.split(/\r?\n/).map(cleanPath).filter(Boolean);
        return {
          type: 'concat',
          paths,
          mode: $('concatMode').value,
          crf: Number($('concatCrf').value),
        };
      }
      case 'audio': {
        const v = $('audioAction').value; // extract | remove
        if (v === 'remove') return { type: 'audio', path: state.path, action: 'remove' };
        return { type: 'audio', path: state.path, action: 'extract' };
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
          ${t.note ? `<p class="hint">ⓘ ${t.note}</p>` : ''}
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

  function dayLabel(d) { return `${d} 天前`; }

  async function cleanupOld() {
    const days = Number($('cleanupDays').value);
    if (!Number.isFinite(days) || days < 1) {
      err('请输入大于 0 的天数（1~365，0 表示不清理）');
      return;
    }
    const info = $('cleanupInfo');
    info.textContent = '清理中…';
    try {
      const res = await api('/api/cleanup', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ days }),
      });
      if (res.deleted > 0) {
        info.textContent = `已清理 ${res.deleted} 个文件，释放 ${fmtBytes(res.bytes)}`;
        status(`已清理 ${res.deleted} 个${dayLabel(days)}产物，释放 ${fmtBytes(res.bytes)}`, 'ok');
      } else {
        info.textContent = `${dayLabel(days)}的产物无需清理`;
        status(`${dayLabel(days)}的产物无需清理`);
      }
    } catch (e) {
      info.textContent = '';
      err('清理失败：' + e.message);
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
        case 'i': case 'I': markFromPlayhead('in'); break;
        case 'o': case 'O': markFromPlayhead('out'); break;
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

  // ---------------- 本地文件浏览器 ----------------
  const FS_LAST_DIR_KEY = 'wve.fsLastDir';
  const FS_SORT_KEY = 'wve.fsSort';
  const FS_SORT_KEYS = ['name', 'size', 'mtime'];
  const fsSort = (() => {
    try {
      const s = JSON.parse(localStorage.getItem(FS_SORT_KEY));
      if (s && FS_SORT_KEYS.includes(s.key) && (s.dir === 'asc' || s.dir === 'desc')) return s;
    } catch (e) {}
    return { key: 'name', dir: 'asc' };
  })();
  let fsLastData = null;

  function fsInitialDir() {
    const saved = localStorage.getItem(FS_LAST_DIR_KEY);
    if (saved) return saved;
    if (state.path) {
      const i = Math.max(state.path.lastIndexOf('\\'), state.path.lastIndexOf('/'));
      if (i > 0) return state.path.slice(0, i);
    }
    return null; // 由后端决定：home 优先，回落磁盘根
  }

  function fsParentOf(p) {
    const i = Math.max(p.lastIndexOf('\\'), p.lastIndexOf('/'));
    if (i <= 0) return null;
    // Windows 盘符根：D:\
    const head = p.slice(0, i);
    if (/^[A-Za-z]:$/.test(head)) return head + '\\';
    return head || null;
  }

  async function fsNavigate(path) {
    const list = $('fsList');
    list.innerHTML = '<p class="fs-empty">载入中…</p>';
    try {
      if (path == null) {
        const d = await api('/api/fs/roots');
        path = d.home || (d.roots && d.roots[0]) || null;
        if (!path) { list.innerHTML = '<p class="fs-empty">没有可用的目录</p>'; return; }
      }
      const all = $('fsFilter').value === 'all' ? '&all=1' : '';
      const data = await api(`/api/fs/list?path=${encodeURIComponent(path)}${all}`);
      localStorage.setItem(FS_LAST_DIR_KEY, data.path);
      $('fsPathInput').value = data.path;
      renderFsList(data);
    } catch (e) {
      list.innerHTML = `<p class="fs-empty">无法打开：${e.message}</p>`;
    }
  }

  function fsCompare(a, b) {
    if (a.isDir !== b.isDir) return a.isDir ? -1 : 1; // 文件夹恒在前
    let r = 0;
    if (fsSort.key === 'size') r = a.size - b.size;
    else if (fsSort.key === 'mtime') r = a.mtime - b.mtime;
    else r = a.name.localeCompare(b.name, 'zh', { numeric: true, sensitivity: 'base' });
    return fsSort.dir === 'asc' ? r : -r;
  }

  function renderFsSortHead() {
    $('fsHead').querySelectorAll('[data-sort]').forEach((th) => {
      const active = th.dataset.sort === fsSort.key;
      th.classList.toggle('active', active);
      th.querySelector('.fs-sort-ind').textContent = active ? (fsSort.dir === 'asc' ? '▲' : '▼') : '';
    });
  }

  function renderFsList(data) {
    fsLastData = data;
    renderFsSortHead();
    const list = $('fsList');
    const up = $('fsUpBtn');
    up.disabled = !data.parent;
    up.onclick = () => data.parent && fsNavigate(data.parent);

    const entries = data.entries.slice().sort(fsCompare);
    if (!entries.length) {
      list.innerHTML = '<p class="fs-empty">此目录没有可显示的内容（可在上方切换「全部文件」）</p>';
      return;
    }
    list.innerHTML = entries.map((e) => `
      <div class="fs-row" data-path="${encodeURIComponent(e.path)}" data-dir="${e.isDir ? 1 : 0}"
           title="${e.isDir ? '单击进入' : '单击载入播放'}">
        <span class="fs-icon">${e.isDir ? '📁' : (e.media ? '🎬' : '📄')}</span>
        <span class="fs-name ${e.isDir ? 'dir' : ''}">${e.name.replace(/[<>&]/g, (c) => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;' }[c]))}</span>
        <span class="fs-size">${e.isDir ? '' : fmtBytes(e.size)}</span>
        <span class="fs-mtime">${new Date(e.mtime * 1000).toLocaleString()}</span>
      </div>`).join('');

    list.querySelectorAll('.fs-row').forEach((row) => {
      row.addEventListener('click', () => {
        const p = decodeURIComponent(row.dataset.path);
        if (row.dataset.dir === '1') fsNavigate(p);
        else {
          closeFsBrowser();
          loadFromPath(p);
        }
      });
    });
  }

  function openFsBrowser() {
    $('fsModal').hidden = false;
    const start = fsInitialDir();
    if (!$('fsRoots').childElementCount) {
      api('/api/fs/roots').then((d) => {
        $('fsRoots').innerHTML = (d.roots || []).map((r) =>
          `<button class="btn small" data-root="${encodeURIComponent(r)}">${r.replace(/[\\/]$/, '')}</button>`
        ).join('');
        $('fsRoots').querySelectorAll('[data-root]').forEach((b) => {
          b.addEventListener('click', () => fsNavigate(decodeURIComponent(b.dataset.root)));
        });
      }).catch(() => {});
    }
    fsNavigate(start);
  }

  function closeFsBrowser() {
    $('fsModal').hidden = true;
  }

  function bindFsBrowser() {
    $('browseBtn').addEventListener('click', openFsBrowser);
    $('fsCloseBtn').addEventListener('click', closeFsBrowser);
    $('fsModal').addEventListener('mousedown', (e) => {
      if (e.target === $('fsModal')) closeFsBrowser();
    });
    $('fsGoBtn').addEventListener('click', () => {
      const p = cleanPath($('fsPathInput').value);
      if (p) fsNavigate(p);
    });
    $('fsPathInput').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') $('fsGoBtn').click();
    });
    $('fsFilter').addEventListener('change', () => {
      const cur = cleanPath($('fsPathInput').value);
      if (cur) fsNavigate(cur);
    });
    $('fsHead').addEventListener('click', (e) => {
      const th = e.target.closest('[data-sort]');
      if (!th) return;
      const key = th.dataset.sort;
      if (fsSort.key === key) fsSort.dir = fsSort.dir === 'asc' ? 'desc' : 'asc';
      else { fsSort.key = key; fsSort.dir = 'asc'; }
      localStorage.setItem(FS_SORT_KEY, JSON.stringify(fsSort));
      if (fsLastData) renderFsList(fsLastData);
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && !$('fsModal').hidden) closeFsBrowser();
    });
  }

  // ---------------- 初始化 ----------------
  function bindUI() {
    $('fileInput').addEventListener('change', (e) => handleFile(e.target.files[0]));
    bindFsBrowser();
    $('loadPathBtn').addEventListener('click', () => {
      const p = cleanPath($('pathInput').value);
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

    $('markInBtn').addEventListener('click', () => markFromPlayhead('in'));
    $('markOutBtn').addEventListener('click', () => markFromPlayhead('out'));
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
    $('cleanupBtn').addEventListener('click', cleanupOld);
    $('loadPlayerBtn').addEventListener('click', togglePlayer);
    $('audioOnlyBtn').addEventListener('click', toggleAudioOnly);
    $('cancelPrepareBtn').addEventListener('click', async () => {
      if ($('prepareBox').hidden || !state.path) return;
      prepareCancelled = true;
      $('cancelPrepareBtn').disabled = true;
      try {
        await api('/api/prepare/cancel?path=' + encodeURIComponent(state.path), { method: 'POST' });
        status('已终止音轨准备，未加载播放器；点「仅听声」可重新准备', 'warn');
      } catch (e) {
        status('终止音轨准备失败：' + e.message, 'error');
      }
      showPrepareUI(false);
      $('playerModeHint').textContent = '音轨准备已终止，未加载播放器';
    });

    bindTimeline();
    bindHotkeys();
  }

  async function init() {
    bindUI();
    showPlayerPlaceholder(); // 初始仅元数据模式，并缓存占位节点供卸载时复用
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
