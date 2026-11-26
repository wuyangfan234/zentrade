/* 因子页新版 (factor-next.html): 因子 → 槽位 → 卡 闭环 + 在用 vs 挑战者同尺对比 + 准入 / 淘汰理由 + 与机会页共用 regime 条。全部 DEMO 虚拟数据 */
'use strict';
renderSide('factor');

var EXPAND = Q.get('x') === 'all' ? SLOTS.map(function (s) { return s.id; }) : (Q.get('x') ? Q.get('x').split(',') : ['board']);
var SLOT_ZH = { board: '机会板', pick: '选股', score: '打分' };
var TIER_CELL = { champ: '在用', ch: '挑战', out: '淘汰' };

function members(slot) { return STRATS.filter(function (s) { return s.slot === slot; }); }
function slotCards(slot) { return CARDS.filter(function (c) { return cardSlots(c).indexOf(slot) >= 0; }); }
function mean(a) { return a.reduce(function (x, y) { return x + y; }, 0) / a.length; }
/* 因子在各槽位里的角色: 取该槽位里引用它的成员的最高层 (在用 > 挑战 > 淘汰) */
function roleIn(fid, slot) {
  var order = ['champ', 'ch', 'out'], best = null;
  members(slot).forEach(function (s) {
    if (s.f.indexOf(fid) >= 0 && (best === null || order.indexOf(s.tier) < order.indexOf(best))) best = s.tier;
  });
  return best;
}
function miniBars(w) {
  return '<span class="mb" title="近 4 周 每周超额">' + w.map(function (v) {
    var h = Math.max(2, Math.round(Math.abs(v) / 0.2 * 16));
    return '<i class="' + (v < 0 ? 'neg' : '') + '" style="height:' + h + 'px"></i>';
  }).join('') + '</span>';
}

function summary() {
  var all = STRATS.filter(function (s) { return s.slot !== 'board-short'; });
  var cnt = function (t) { return all.filter(function (s) { return s.tier === t; }).length; };
  var used = FACTORS.filter(function (f) { return SLOTS.some(function (sl) { return roleIn(f.id, sl.id) === 'champ'; }); }).length;
  var cards = CARDS.length;
  return '<div class="kpis">' +
    '<div class="kpi"><span class="k">因子</span><span class="v num"><span data-count="fac">' + FACTORS.length + '</span></span><span class="s">在用引用 ' + used + '</span></div>' +
    '<div class="kpi"><span class="k">槽位</span><span class="v num"><span data-count="slot">' + SLOTS.length + '</span></span><span class="s">出卡 · 选股 · 打分</span></div>' +
    '<div class="kpi"><span class="k">在用</span><span class="v num" data-count="mem-champ">' + cnt('champ') + '</span><span class="s">挑战者 <span data-count="mem-ch">' + cnt('ch') + '</span> · 已淘汰 <span data-count="mem-out">' + cnt('out') + '</span></span></div>' +
    '<div class="kpi"><span class="k">近期出卡</span><span class="v num">' + cards + '</span><span class="s">今日 ' + TODAY.length + ' · 已结算 ' + (cards - TODAY.length) + '</span></div>' +
    '</div>';
}

function factorMatrix() {
  var rows = FACTORS.map(function (f) {
    var st = fState(f);
    var cells = SLOTS.map(function (sl) {
      var r = roleIn(f.id, sl.id);
      return '<td class="rc">' + (r ? '<span class="role ' + r + '">' + TIER_CELL[r] + '</span>' : '<span class="mut">\u2014</span>') + '</td>';
    }).join('');
    return '<tr data-item="fac"><td class="nw"><b>' + f.label + '</b></td><td class="nw fg2">偏' + f.dir + '</td>' +
      '<td><span class="perf">' + miniBars(f.w) + '<b class="num ' + tone(f.w[3]) + '">' + fR(f.w[3]) + '</b></span></td>' +
      '<td class="r num ' + tone(mean(f.w)) + '">' + fR(mean(f.w)) + '</td><td class="nw ' + st[1] + '">' + st[0] + '</td>' + cells + '</tr>';
  }).join('');
  return '<section class="card sec" data-sec="matrix"><div class="card-h"><h2>因子 \u2192 槽位</h2><span class="meta nw">每周超额 = 因子前三分之一 比 门内随便挑 每笔多出的 R</span></div>' +
    '<div class="mhint meta">表内左右滑动 \u2192</div><div class="tw"><table><thead><tr><th>因子</th><th>方向</th><th>近 4 周 · 本周</th><th class="r">4 周均值</th><th>状态</th>' +
    SLOTS.map(function (s) { return '<th class="rc">' + SLOT_ZH[s.id] + '</th>'; }).join('') + '</tr></thead><tbody>' + rows + '</tbody></table></div>' +
    '</section>';
}

function cmpTable(sl) {
  var ms = members(sl.id), body = '';
  ['champ', 'ch', 'out'].forEach(function (t) {
    var g = ms.filter(function (s) { return s.tier === t; });
    body += '<tr class="grp"><td colspan="7">' + TIER[t] + ' <span data-count="m-' + sl.id + '-' + t + '">' + g.length + '</span></td></tr>';
    if (!g.length) body += '<tr class="empty"><td colspan="7">' + (t === 'ch' ? '0 候选 · 下次周度 11-28 收盘后' : '无') + '</td></tr>';
    g.forEach(function (s) {
      body += '<tr data-item="m-' + sl.id + '-' + t + ' mem-' + t + '"' + (t === 'out' ? ' class="dim"' : '') + '><td><b class="nw">' + s.name + '</b><span class="fl">' +
        s.f.map(function (id) { return FBY[id].label; }).join(' · ') + '</span></td>' +
        '<td class="r num ' + (t === 'out' ? '' : tone(s.ex)) + '"><b>' + fR(s.ex) + '</b></td><td>' + ciBar(s) + '</td><td class="r num">' + s.n + '</td>' +
        '<td class="num nw">' + s.last + '</td><td class="why"><span class="wtag ' + t + '">' + (t === 'out' ? '换下' : t === 'ch' ? '待定' : '留用') + '</span>' + s.why + '</td></tr>';
    });
  });
  return '<div class="mhint meta in">表内左右滑动 \u2192</div><div class="tw"><table class="cmp"><thead><tr><th>' + sl.use + '</th><th class="r">每笔超额</th><th>95% 区间 ' + ciScale() + '</th><th class="r">样本</th><th>最近结算</th><th>准入 / 淘汰理由</th></tr></thead><tbody>' +
    body + '</tbody></table></div>';
}

function cardList(sl) {
  var cs = slotCards(sl.id);
  if (!cs.length) return '<div class="empty-line mut">近期 0 张卡 · 在用成员下次出卡后显示</div>';
  return '<div class="clist">' + cs.map(function (c) {
    var s = SBY[c.strat];
    var st = c.timing === 'x' ? '<span class="b st">跨日有效</span>' : c.timing === 'i' ? '<span class="b lim">盘中事件</span>'
      : '<b class="num ' + tone(c.res) + '">' + fR(c.res) + '</b>';
    var when = c.timing ? (c.timing === 'x' ? c.gen.slice(0, 5) : '11-25') : c.settled;
    var tail = sl.id === 'score' ? '打分第 ' + c.rank : c.timing ? '今日' : c.exit;
    return '<div class="crow" data-item="cards-' + sl.id + '" role="button" tabindex="0" data-open="' + c.id + '"><b class="sym num">' + c.sym + '</b>' +
      '<span class="cmid"><span class="nw">' + s.name + '</span><span class="nw mut">' + when + ' · ' + tail + '</span></span>' +
      '<span class="cst">' + st + '</span><button class="btn ghost" data-open="' + c.id + '">归因</button></div>';
  }).join('') + '</div>';
}

function slotCard(sl) {
  var ms = members(sl.id), on = EXPAND.indexOf(sl.id) >= 0;
  var cnt = function (t) { return ms.filter(function (s) { return s.tier === t; }).length; };
  var champF = [];
  ms.filter(function (s) { return s.tier === 'champ'; }).forEach(function (s) { s.f.forEach(function (id) { if (champF.indexOf(id) < 0) champF.push(id); }); });
  var fchips = champF.map(function (id) {
    var f = FBY[id], st = fState(f);
    return '<span class="fchip" data-item="ref-' + sl.id + '"><b>' + f.label + '</b><span class="num ' + tone(f.w[3]) + '">' + fR(f.w[3]) + '</span><span class="' + st[1] + '">' + st[0] + '</span></span>';
  }).join('');
  var ncards = slotCards(sl.id).length;
  return '<section class="card sec slot' + (on ? ' on' : '') + '" data-item="slot" data-slot="' + sl.id + '">' +
    '<button class="slot-h" data-slot-toggle="' + sl.id + '" aria-expanded="' + on + '"><span class="chev">' + (on ? '\u25BE' : '\u25B8') + '</span><h2>' + sl.name + '</h2>' +
    ['champ', 'ch', 'out'].map(function (t) { return '<span class="b lim">' + TIER[t] + ' <span data-count="m-' + sl.id + '-' + t + '">' + cnt(t) + '</span></span>'; }).join('') + '<span class="b st">近期卡 <span data-count="cards-' + sl.id + '">' + ncards + '</span></span>' +
    '<span class="meta ml nw">尺子 · ' + sl.ruler + '</span></button>' +
    '<div class="slot-b">' +
    '<div class="blk"><h3>引用因子 <span class="num fg2" data-count="ref-' + sl.id + '">' + champF.length + '</span><span class="meta"> · 本周超额 · 状态</span></h3><div class="chipline">' + fchips + '</div></div>' +
    '<div class="blk"><h3>在用 vs 挑战者 · 同尺</h3>' + cmpTable(sl) + '</div>' +
    '<div class="blk"><h3>最近产出的卡 <span class="num fg2" data-count="cards-' + sl.id + '">' + ncards + '</span><span class="meta"> · 点开看归因</span></h3>' + cardList(sl) + '</div>' +
    '</div></section>';
}

function render() {
  var tabs = ['按用途', '因子库', '周度发现', '检验'].map(function (t, i) { return '<button class="' + (i === 0 ? 'on' : '') + '">' + t + '</button>'; }).join('');
  document.getElementById('app').innerHTML =
    '<div class="head"><h1>因子</h1><span class="meta nw">11-25 周三 · 10:24 ET</span><span class="b demo">DEMO · 虚拟数据</span><span class="seg ml">' + tabs + '</span></div>' +
    regimeBar() + '<div class="same meta"><span class="nw">与机会页同一状态条</span><span class="nw">指纹 ' + REGIME.fp + '</span><a class="nw" href="opp-next.html">去机会页对照</a></div>' +
    summary() + factorMatrix() + SLOTS.map(slotCard).join('');
}
render();

document.addEventListener('click', function (e) {
  var t = e.target.closest('[data-slot-toggle]');
  if (t) {
    var sec = t.closest('.slot'), on = sec.classList.toggle('on');
    t.setAttribute('aria-expanded', on); t.querySelector('.chev').textContent = on ? '\u25BE' : '\u25B8';
    return;
  }
  var o = e.target.closest('[data-open]');
  if (o) openDrawer(o.dataset.open);
});
document.addEventListener('keydown', function (e) {
  if (e.key === 'Enter' && e.target.matches && e.target.matches('.crow')) openDrawer(e.target.dataset.open);
});
if (Q.get('open')) openDrawer(Q.get('open'));
