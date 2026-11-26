/* 机会页新版 (opp-next.html): regime 状态条 + 今日机会 (时效 / 归因) + 策略账本 + 反应速度对照。全部 DEMO 虚拟数据 */
'use strict';
renderSide('opp');

var FILTER = Q.get('f') === 'x' || Q.get('f') === 'i' ? Q.get('f') : 'all';
var OPEN_C = Q.get('c');
var XS = TODAY.filter(function (c) { return c.timing === 'x'; });
var IS = TODAY.filter(function (c) { return c.timing === 'i'; });

function mmss(s) { s = Math.max(0, s); var m = Math.floor(s / 60), x = s % 60; return m + ':' + (x < 10 ? '0' : '') + x; }

function cardHTML(c) {
  var s = SBY[c.strat];
  var timing = c.timing === 'x'
    ? '<span class="b st">跨日有效</span>'
    : '<span class="b lim">' + '<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">' + ICON.zap + '</svg>盘中事件</span>';
  var verdict = c.verdict === '下单' ? '<span class="b act ml">下单</span>' : '<span class="b lim ml">不下单</span>';
  var tline = c.timing === 'x'
    ? '<div class="cc-time"><span class="nw">盘后 ' + c.gen + ' 生成</span><span class="nw">可挂限价单</span><span class="nw">有效至 ' + c.until + '</span></div>'
    : '<div class="cc-time"><span class="nw">' + c.gen + ' 触发</span><span class="nw">' + c.event + '</span><span class="nw cd' + (c.left <= 300 ? ' warn' : '') + '" data-left="' + c.left + '" data-valid="' + c.valid * 60 + '">剩 <b class="num">' + mmss(c.left) + '</b></span><span class="nw">限时 ' + c.valid + ' 分钟</span></div>' +
      '<div class="ttl"><span style="width:' + (c.left / (c.valid * 60) * 100).toFixed(1) + '%"' + (c.left <= 300 ? ' class="low"' : '') + '></span></div>';
  var why = c.verdict === '不下单' ? '<div class="cc-why warn">不下单 · ' + c.why + '</div>' : '';
  var ev = c.rg.map(function (n) { return NO[n]; }).join('');
  var src = '<div class="cc-src"><span class="mut nw">来源</span><span class="nw">' + s.name + '</span><span class="mut">·</span><span class="nw">' + FBY[c.fv[0][0]].label + '</span><span class="mut">·</span><span class="nw">' + REGIME.name.slice(0, 2) + ' ' + ev + '</span>' +
    '<button class="btn ghost ml" data-toggle="' + c.id + '" aria-expanded="' + (OPEN_C === c.id) + '">归因' + (OPEN_C === c.id ? ' \u25B4' : ' \u25BE') + '</button></div>';
  return '<article class="cc" id="card-' + c.id + '" data-item="card card-' + c.timing + '" data-card="' + c.id + '">' +
    '<div class="cc-top"><b class="sym">' + c.sym + '</b><span class="fg2 nw">' + SIDE[c.side] + '</span>' + timing + verdict + '</div>' + tline +
    '<div class="kv"><div><span class="k">进场</span><span class="v num">' + c.zone + '</span></div><div><span class="k">止损</span><span class="v num">' + c.stop + '</span></div>' +
    '<div><span class="k">目标</span><span class="v num">' + c.tgt + '</span></div><div><span class="k">期望</span><span class="v num ' + tone(c.exp) + '">' + fR(c.exp) + '</span></div></div>' +
    why + src +
    '<div class="cc-attr' + (OPEN_C === c.id ? ' on' : '') + '" title="' + s.name + '">' + attrHTML(c) + '</div>' +
    '</article>';
}

function group(k, list, name, note) {
  if (!list.length) return '';
  return '<div class="cg" data-cg="' + k + '"' + (FILTER !== 'all' && FILTER !== k ? ' hidden' : '') + '><div class="cg-h"><h3>' + name + '</h3><span class="num fg2">' + list.length + '</span><span class="meta nw">' + note + '</span></div>' +
    '<div class="cards">' + list.map(cardHTML).join('') + '</div></div>';
}

function cardsSection() {
  var seg = [['all', '全部', TODAY.length, 'card'], ['x', '跨日有效', XS.length, 'card-x'], ['i', '盘中事件', IS.length, 'card-i']].map(function (x) {
    return '<button class="' + (FILTER === x[0] ? 'on' : '') + '" data-f="' + x[0] + '">' + x[1] + ' <span data-count="' + x[3] + '">' + x[2] + '</span></button>';
  }).join('');
  var shown = FILTER === 'all' ? TODAY : TODAY.filter(function (c) { return c.timing === FILTER; });
  var empties = '';
  if (XS.length === 0 && FILTER !== 'i') empties += '<div class="empty" data-empty="x"><span class="b st">跨日有效 0</span><span class="mut nw">下次盘后生成 11-25 16:40 ET</span></div>';
  if (IS.length === 0 && FILTER !== 'x') empties += '<div class="empty" data-empty="i"><span class="b lim">盘中事件 0</span><span class="mut nw">事件触发即出</span></div>';
  return '<section class="card sec" data-sec="cards"><div class="card-h"><h2>今日机会</h2><span class="num fg2" data-shown>' + shown.length + '</span>' +
    '<div class="seg" data-seg>' + seg + '</div>' +
    '<span class="ml meta nw">每张卡可展开归因</span></div>' +
    '<div class="card-b">' + group('x', XS, '跨日有效', '盘后生成 · 可挂限价单 · 次日 09:45 ET 起成交') + group('i', IS, '盘中事件', '刚触发 · 限时有效 · 过时自动作废') + empties + '</div></section>';
}

function ledgerSection() {
  var rows = STRATS.filter(function (s) { return s.slot === 'board' || s.slot === 'board-short'; });
  var body = '';
  ['champ', 'ch', 'out'].forEach(function (t) {
    var g = rows.filter(function (s) { return s.tier === t; });
    body += '<tr class="grp" data-grp="' + t + '"><td colspan="9">' + TIER[t] + ' <span data-count="led-' + t + '">' + g.length + '</span></td></tr>';
    if (!g.length) body += '<tr class="empty"><td colspan="9">' + (t === 'ch' ? '本周无新挑战者 · 下次周度 11-28 收盘后' : '无') + '</td></tr>';
    g.forEach(function (s) {
      var op = t === 'champ' ? '<button class="btn" title="原型 · 不生效">淘汰</button>' : t === 'ch' ? '<button class="btn" title="原型 · 不生效">晋级</button>' : '<button class="btn" title="原型 · 不生效">复活</button>';
      body += '<tr data-item="led led-' + t + '"' + (t === 'out' ? ' class="dim"' : '') + '><td class="nw"><b>' + s.name + '</b></td><td class="nw fg2">' + SIDE[s.side] + '</td><td class="nw">' + s.kind + '</td>' +
        '<td class="r num ' + (t === 'out' ? '' : tone(s.ex)) + '"><b>' + fR(s.ex) + '</b></td><td>' + ciBar(s) + '</td><td class="r num">' + s.n + '</td><td class="num nw">' + s.last + '</td>' +
        '<td class="why">' + s.why + '</td><td>' + op + '</td></tr>';
    });
  });
  return '<section class="card sec" data-sec="ledger"><div class="card-h"><h2>策略账本</h2>' +
    '<span class="chipline">' + ['champ', 'ch', 'out'].map(function (t) { return '<span class="b lim">' + TIER[t] + ' <span data-count="led-' + t + '">' + rows.filter(function (s) { return s.tier === t; }).length + '</span></span>'; }).join('') + '</span>' +
    '<span class="meta ml nw">滚动 60 交易日 · 比门内随便挑 每笔多出的 R · 在用条数不固定</span></div>' +
    '<div class="tw"><table><thead><tr><th>策略</th><th>方向</th><th>来源</th><th class="r">滚动超额</th><th>95% 区间 ' + ciScale() + '</th><th class="r">样本</th><th>最近结算</th><th>理由</th><th>操作</th></tr></thead><tbody>' +
    body + '</tbody></table></div><div class="mhint meta">表内左右滑动</div></section>';
}

var SPEED = [
  { k: '状态切换识别', old: 52, now: 11, u: '分钟' },
  { k: '盘中事件出卡', old: 38, now: 6, u: '分钟' },
  { k: '失效策略下架', old: 20, now: 6, u: '交易日' },
  { k: '新策略上岗', old: 28, now: 9, u: '交易日' }
];
function speedSection() {
  var cut = SPEED.reduce(function (a, r) { return a + (1 - r.now / r.old); }, 0) / SPEED.length;
  var mx = {}; SPEED.forEach(function (r) { mx[r.u] = Math.max(mx[r.u] || 0, r.old); });
  var rows = SPEED.map(function (r) {
    return '<div class="sp-row" data-item="speed"><div class="sp-k"><span class="nw">' + r.k + '</span><span class="nw meta">' + r.u + '</span></div>' +
      '<div class="sp-bars"><div class="sp-b"><span class="lab mut nw">现网</span><span class="bar old" style="width:' + (r.old / mx[r.u] * 100).toFixed(1) + '%"></span><b class="num nw">' + r.old + '</b></div>' +
      '<div class="sp-b"><span class="lab acc nw">新</span><span class="bar new" style="width:' + (r.now / mx[r.u] * 100).toFixed(1) + '%"></span><b class="num nw acc">' + r.now + '</b></div></div></div>';
  }).join('');
  return '<section class="card" data-sec="speed"><div class="card-h"><h2>反应速度</h2><span class="b demo">DEMO</span><span class="meta ml nw">平均滞后 · 近 20 次</span></div>' +
    '<div class="card-b"><div class="sp-big"><span class="num acc" data-cut>' + MINUS + Math.round(cut * 100) + '%</span><span class="meta nw">滞后缩短 · <span data-count="speed">' + SPEED.length + '</span> 项均值</span></div>' + rows +
    '<div class="meta sp-foot">现网 = 整点重判 · 固定 3+3 · 周度换策略</div></div></section>';
}

function render() {
  document.getElementById('app').innerHTML =
    '<div class="head"><h1>机会</h1><span class="meta nw">11-25 周三 · 10:24 ET · 盘中</span><span class="b demo">DEMO · 虚拟数据</span>' +
    '<span class="meta ml nw">原型 · 不接后端</span></div>' +
    '<div class="top2">' + regimeBar() + speedSection() + '</div>' +
    cardsSection() + ledgerSection();
}
render();

document.addEventListener('click', function (e) {
  var t = e.target.closest('[data-toggle]');
  if (t) {
    var box = document.querySelector('#card-' + t.dataset.toggle + ' .cc-attr');
    var on = box.classList.toggle('on');
    t.setAttribute('aria-expanded', on); t.textContent = '归因' + (on ? ' \u25B4' : ' \u25BE');
    return;
  }
  var f = e.target.closest('[data-f]');
  if (f) {
    var u = new URL(location.href); if (f.dataset.f === 'all') u.searchParams.delete('f'); else u.searchParams.set('f', f.dataset.f);
    u.searchParams.delete('c'); location.href = u.toString();
  }
});
/* 盘中事件倒计时: 每秒减一, 归零标「已失效」 */
setInterval(function () {
  document.querySelectorAll('.cd[data-left]').forEach(function (el) {
    var left = Math.max(0, +el.dataset.left - 1), valid = +el.dataset.valid;
    el.dataset.left = left;
    el.querySelector('b').textContent = left ? mmss(left) : '已失效';
    el.classList.toggle('warn', left <= 300);
    var bar = el.closest('.cc').querySelector('.ttl span');
    bar.style.width = (left / valid * 100).toFixed(1) + '%'; bar.classList.toggle('low', left <= 300);
  });
}, 1000);
if (OPEN_C && document.getElementById('card-' + OPEN_C)) document.getElementById('card-' + OPEN_C).scrollIntoView({ block: 'center' });
