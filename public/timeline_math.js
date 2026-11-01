/* 时间线视图窗口纯函数 —— 浏览器（window.TimelineMath）与 Node 单测（module.exports）共用 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.TimelineMath = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  function clamp(v, lo, hi) {
    return Math.max(lo, Math.min(hi, v));
  }

  // 时间 → 时间线百分比 0~100（超出可见窗口的部分裁剪到两端）
  function timeToPct(t, viewStart, viewEnd) {
    var span = viewEnd - viewStart;
    if (!(span > 0)) return 0;
    return clamp((t - viewStart) / span, 0, 1) * 100;
  }

  // 屏幕比例 0~1 → 时间
  function ratioToTime(r, viewStart, viewEnd) {
    return viewStart + clamp(r, 0, 1) * (viewEnd - viewStart);
  }

  // 以 anchor 为固定点缩放：保持 anchor 的屏幕比例 anchorRatio 不变（factor > 1 放大，窗口变窄）
  // 返回 [viewStart, viewEnd]；窗口钳制在 [0, duration]
  function zoomAt(viewStart, viewEnd, duration, anchor, anchorRatio, factor, minSpan) {
    if (!(duration > 0)) return [0, 0];
    var span = viewEnd - viewStart;
    if (!(span > 0)) span = duration;
    var newSpan = clamp(span / factor, Math.min(minSpan, duration), duration);
    var r = clamp(anchorRatio, 0, 1);
    var vs = clamp(anchor - r * newSpan, 0, Math.max(0, duration - newSpan));
    return [vs, vs + newSpan];
  }

  // 横向平移：frac 为窗口宽度的比例（正 = 看向更晚的时间），窗口钳制在 [0, duration]
  function panView(viewStart, viewEnd, duration, frac) {
    var span = viewEnd - viewStart;
    if (!(span > 0) || !(duration > 0)) return [viewStart, viewEnd];
    var vs = clamp(viewStart + span * frac, 0, Math.max(0, duration - span));
    return [vs, vs + span];
  }

  // 播放头跑出可见窗口时拉回（播放头居中）；在窗口内则不动
  function followAnchor(viewStart, viewEnd, duration, anchor) {
    var span = viewEnd - viewStart;
    if (!(span > 0) || !(duration > 0)) return [viewStart, viewEnd];
    if (anchor >= viewStart && anchor <= viewEnd) return [viewStart, viewEnd];
    return zoomAt(viewStart, viewEnd, duration, anchor, 0.5, 1, span);
  }

  // 波形背景铺放：整幅波形图代表 duration，窗口只显示 [viewStart, viewEnd]
  // size: 背景放大（%），position: 水平偏移（%，CSS background-position 百分比语义）
  function waveBackground(viewStart, viewEnd, duration) {
    var span = viewEnd - viewStart;
    if (!(duration > 0) || !(span > 0) || span >= duration) {
      return { size: '100% 100%', position: '0% 0%' };
    }
    return {
      size: (duration / span * 100).toFixed(4) + '% 100%',
      position: (viewStart / (duration - span) * 100).toFixed(4) + '% 0%',
    };
  }

  return {
    clamp: clamp,
    timeToPct: timeToPct,
    ratioToTime: ratioToTime,
    zoomAt: zoomAt,
    panView: panView,
    followAnchor: followAnchor,
    waveBackground: waveBackground,
  };
});
