/* timeline_math.js 单元测试（Node 直接运行：node tests/timeline_math_test.js） */
const assert = require('assert');
const M = require('../public/timeline_math.js');

let n = 0;
function ok(name, cond, extra) {
  n += 1;
  if (!cond) {
    console.error(`[FAIL] #${n} ${name}${extra ? ' | ' + extra : ''}`);
    process.exit(1);
  }
  console.log(`[PASS] #${n} ${name}${extra ? ' | ' + extra : ''}`);
}

// ---- timeToPct / ratioToTime ----
ok('timeToPct 窗口内 50%', M.timeToPct(5, 0, 10) === 50, String(M.timeToPct(5, 0, 10)));
ok('timeToPct 左侧越界钳到 0', M.timeToPct(-3, 0, 10) === 0);
ok('timeToPct 右侧越界钳到 100', M.timeToPct(15, 0, 10) === 100);
ok('timeToPct 零宽窗口返回 0', M.timeToPct(5, 5, 5) === 0);
ok('ratioToTime 0.25 → 2.5', M.ratioToTime(0.25, 0, 10) === 2.5);

// ---- zoomAt：围绕 anchor 的屏幕比例缩放 ----
let [a, b] = M.zoomAt(0, 100, 100, 50, 0.5, 2, 0.05);
ok('放大 2x 居中：窗口 [25,75]', a === 25 && b === 75, `[${a},${b}]`);

[a, b] = M.zoomAt(0, 100, 100, 20, 0.2, 2, 0.05);
ok('放大 2x 固定屏幕 20%：anchor 不动', a === 10 && b === 60 && (20 - a) / (b - a) === 0.2, `[${a},${b}]`);

[a, b] = M.zoomAt(0, 100, 100, 0, 0, 2, 0.05);
ok('贴左边界放大：窗口 [0,50]', a === 0 && b === 50, `[${a},${b}]`);

[a, b] = M.zoomAt(50, 100, 100, 50, 0.5, 0.1, 0.05);
ok('缩小超过全览：钳到 [0,100]', a === 0 && b === 100, `[${a},${b}]`);

[a, b] = M.zoomAt(0, 100, 100, 50, 0.5, 1e9, 0.05);
ok('最小窗口 = minSpan', Math.abs(b - a - 0.05) < 1e-12, `span=${b - a}`);

// ---- panView ----
[a, b] = M.panView(10, 20, 100, 0.5);
ok('平移 +0.5 窗宽', a === 15 && b === 25, `[${a},${b}]`);
[a, b] = M.panView(10, 20, 100, -1);
ok('平移越界钳到起点', a === 0 && b === 10, `[${a},${b}]`);
[a, b] = M.panView(10, 20, 100, 10);
ok('平移越界钳到终点', a === 90 && b === 100, `[${a},${b}]`);

// ---- followAnchor ----
[a, b] = M.followAnchor(10, 20, 100, 15);
ok('播放头在窗口内：不动', a === 10 && b === 20);
[a, b] = M.followAnchor(10, 20, 100, 30);
ok('播放头出界：拉回居中', a === 25 && b === 35, `[${a},${b}]`);

// ---- waveBackground ----
let bg = M.waveBackground(0, 100, 100);
ok('全览：背景 100%', bg.size === '100% 100%' && bg.position === '0% 0%');
bg = M.waveBackground(25, 75, 100);
ok('窗口 [25,75]：size 200% / pos 50%',
   Math.abs(parseFloat(bg.size) - 200) < 1e-6 && Math.abs(parseFloat(bg.position) - 50) < 1e-6,
   `${bg.size} @ ${bg.position}`);
bg = M.waveBackground(0, 25, 100);
ok('窗口 [0,25]：size 400% / pos 0%',
   Math.abs(parseFloat(bg.size) - 400) < 1e-6 && parseFloat(bg.position) === 0,
   `${bg.size} @ ${bg.position}`);
// 映射验证：时间 t 在窗口内应落在屏幕比例 (t-vs)/span；背景偏移公式等价
(function () {
  const d = 100, vs = 30, ve = 70, span = ve - vs;
  const t = 55;
  const screenRatio = (t - vs) / span; // 期望 0.625
  bg = M.waveBackground(vs, ve, d);
  const sizePct = parseFloat(bg.size), posPct = parseFloat(bg.position);
  // CSS 百分比定位下时间 t 的实际屏幕比例
  const W = 1, Wp = W * sizePct / 100, off = posPct / 100 * (W - Wp);
  const actual = (off + t / d * Wp) / W;
  ok('waveBackground 映射与 timeToPct 一致', Math.abs(actual - screenRatio) < 1e-9,
     `actual=${actual} expect=${screenRatio}`);
})();

console.log(`\n===== timeline_math: ${n} 项全部通过 =====`);
