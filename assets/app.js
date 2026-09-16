/* Sports Predictions — shared front end.
 *
 * Pages ship a small HTML shell plus a data file per league; everything below
 * renders from that JSON. The "All" view loads every league and merges them,
 * so one scroll shows the whole day. On a phone the views live in a bottom
 * bar and the sports in a chip row, which keeps the header short.
 */
(function () {
  'use strict';

  var DATA = (window.SP_DATA = window.SP_DATA || {});
  var HISTORY = (window.SP_HISTORY = window.SP_HISTORY || {});
  var LEAGUES = window.SP_LEAGUES || ['mlb', 'nfl', 'nba', 'nhl'];
  var EMOJI = { mlb: '⚾', nfl: '🏈', nba: '🏀', nhl: '🏒', all: '🎯' };
  var LABEL = { mlb: 'MLB', nfl: 'NFL', nba: 'NBA', nhl: 'NHL', all: 'All' };
  var VIEWS = [
    ['today', 'Today', 'Today', '📅'],
    ['upcoming', 'Upcoming', 'Next', '⏭'],
    ['props', 'Player Props', 'Props', '👤'],
    ['results', 'Results', 'Results', '✓'],
    ['model', 'Model', 'Model', '📈']
  ];

  var state = { league: null, view: 'today', filters: {}, filtersOpen: false,
                showCountBadge: false, search: '', loading: {}, liveTimer: null };

  // ── helpers ──────────────────────────────────────────────────────────────
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function pct(v, digits) { return v == null ? '—' : (v * 100).toFixed(digits == null ? 0 : digits) + '%'; }
  function el(id) { return document.getElementById(id); }
  function today() { return new Date().toISOString().slice(0, 10); }
  function addDays(iso, n) { var d = new Date(iso + 'T12:00:00Z'); d.setUTCDate(d.getUTCDate() + n); return d.toISOString().slice(0, 10); }
  function shortDate(iso) {
    var d = new Date(iso + 'T12:00:00Z');
    return isNaN(d) ? iso : d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', timeZone: 'UTC' });
  }
  function fmt(v, spec) {
    if (v == null || v === '') return '—';
    var n = Number(v); if (isNaN(n)) return String(v);
    if (spec === '.3f') return n.toFixed(3);
    if (spec === '+.2f') return (n >= 0 ? '+' : '') + n.toFixed(2);
    if (spec === '+.1f') return (n >= 0 ? '+' : '') + n.toFixed(1);
    if (spec === '.1f') return n.toFixed(1);
    return n.toFixed(2);
  }
  function fmtNum(v) { return v == null ? '—' : (Math.round(v * 100) / 100); }
  function nameSpans(full, short) {
    return '<span class="t-full">' + esc(full) + '</span><span class="t-short">' + esc(short || full) + '</span>';
  }
  function card(k, v, n) {
    return '<div class="card"><div class="k">' + esc(k) + '</div><div class="v">' + v + '</div>' +
      (n ? '<div class="n">' + esc(n) + '</div>' : '') + '</div>';
  }
  function isAll() { return state.league === 'all'; }
  function leaguesInView() { return isAll() ? LEAGUES.filter(function (k) { return DATA[k]; }) : [state.league]; }
  function cur() { return isAll() ? null : (DATA[state.league] || null); }
  function todayOf(d) { return (d && d.today) || today(); }

  function espnFetch(path) {
    var a = 'https://site.api.espn.com/apis/site/v2/sports/' + path;
    var b = 'https://site.web.api.espn.com/apis/site/v2/sports/' + path;
    return fetch(a).then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .catch(function () { return fetch(b).then(function (r) { return r.ok ? r.json() : null; }); });
  }

  // ── data loading ─────────────────────────────────────────────────────────
  function loadScript(key, src, done) {
    if (state.loading[key]) { state.loading[key].push(done); return; }
    state.loading[key] = [done];
    var tag = document.createElement('script');
    tag.src = src; tag.async = true;
    tag.onload = tag.onerror = function () {
      var waiting = state.loading[key] || []; delete state.loading[key];
      waiting.forEach(function (fn) { fn(); });
    };
    document.head.appendChild(tag);
  }
  function load(league, done) {
    if (league === 'all') {
      var pending = LEAGUES.filter(function (k) { return !DATA[k]; });
      if (!pending.length) return done();
      var left = pending.length;
      pending.forEach(function (k) { loadScript(k, 'data/' + k + '.js', function () { if (--left === 0) done(); }); });
      return;
    }
    if (DATA[league]) return done();
    loadScript(league, 'data/' + league + '.js', done);
  }
  function loadHistory(leagues, done) {
    var pending = leagues.filter(function (k) { return !HISTORY[k]; });
    if (!pending.length) return done();
    var left = pending.length;
    pending.forEach(function (k) {
      loadScript(k + ':history', 'data/' + k + '-history.js', function () {
        HISTORY[k] = HISTORY[k] || []; if (--left === 0) done();
      });
    });
  }

  // ── chrome ───────────────────────────────────────────────────────────────
  function renderChrome() {
    var tabs = el('sports');
    if (tabs && !tabs.dataset.built) {
      tabs.innerHTML = ['all'].concat(LEAGUES).map(function (k) {
        return '<button class="sport" role="tab" data-league="' + k + '" aria-selected="false">' +
          '<span class="em">' + EMOJI[k] + '</span>' + LABEL[k] + '</button>';
      }).join('');
      tabs.dataset.built = '1';
    }
    var views = el('views');
    if (views && !views.dataset.built) {
      views.innerHTML = VIEWS.map(function (v) {
        return '<button class="view-tab" role="tab" data-view="' + v[0] + '" aria-selected="false">' +
          '<span class="vi" aria-hidden="true">' + v[3] + '</span>' +
          '<span class="t-full">' + v[1] + '</span><span class="t-short">' + v[2] + '</span></button>';
      }).join('');
      views.dataset.built = '1';
    }
    document.querySelectorAll('.sport').forEach(function (b) {
      b.setAttribute('aria-selected', b.dataset.league === state.league ? 'true' : 'false');
    });
    document.querySelectorAll('.view-tab').forEach(function (b) {
      b.setAttribute('aria-selected', b.dataset.view === state.view ? 'true' : 'false');
    });
    var accent = el('accent-style');
    var d = cur();
    if (accent) accent.textContent = ':root{--accent:' + (d ? d.accent : '#3b82f6') + '}';
    var sub = el('brand-sub');
    if (sub) {
      if (d) {
        var a = d.accuracy || {}, v = a.verified || {};
        var tail = v.total ? ' · ' + pct(v.pct, 1) + ' on ' + v.total + ' verified picks'
          : (a.backtest && a.backtest.pct != null ? ' · ' + pct(a.backtest.pct, 1) + ' in backtest' : '');
        sub.textContent = d.name + ' ' + d.season + tail;
      } else {
        sub.textContent = 'All sports · every game and prop, one scroll';
      }
    }
    document.title = (d ? d.name + ' Predictions' : 'All Sports') + ' · Sports Predictions';
  }

  // ── game rows ────────────────────────────────────────────────────────────
  function teamCell(name, short, rec, cls, right) {
    return '<div class="team' + (right ? ' right' : '') + (cls ? ' ' + cls : '') + '">' +
      '<span class="tname">' + nameSpans(name, short) + '</span>' +
      (rec ? '<span class="trec">' + esc(rec) + '</span>' : '') + '</div>';
  }
  function centerCell(g) {
    if (g.final) {
      return '<div class="center"><span class="score">' + g.away_score + '–' + g.home_score +
        '</span><span class="slabel">FINAL</span></div>';
    }
    return '<div class="center" data-time="1"><span class="gtime">' + esc(g.time || 'TBD') + '</span></div>';
  }
  function countProps(p) {
    var n = 0;
    ['away', 'home'].forEach(function (s) { (p[s] || []).forEach(function (pl) { n += (pl.props || []).length; }); });
    return n;
  }

  function gameRow(g, league) {
    var homePick = g.favored === g.home;
    var awayCls = g.final ? (g.winner === g.away ? 'won' : 'lost') : (homePick ? '' : 'pick');
    var homeCls = g.final ? (g.winner === g.home ? 'won' : 'lost') : (homePick ? 'pick' : '');
    var result = g.correct == null ? '' :
      '<span class="tag ' + (g.correct ? 'ok' : 'no') + '">' + (g.correct ? '✓' : '✗') + '</span>';
    var propCount = g.props ? countProps(g.props) : 0;
    var flag = g.preseason ? '<span class="tag low">PRESEASON</span>'
      : (g.final && !g.counted && state.showCountBadge ? '<span class="tag low">NOT COUNTED</span>' : '');
    var lock = g.locked ? '<span class="lock" title="Locked at first pitch">🔒</span>' : '';
    var lg = isAll() ? '<span class="lg-chip">' + EMOJI[league] + '</span>' : '';

    return '<article class="game' + (g.final ? ' done' : '') + '" data-id="' + esc(g.id) + '" data-league="' + league + '"' +
      ' data-conf="' + g.conf + '" data-date="' + g.date + '"' +
      ' data-result="' + (g.correct == null ? '' : (g.correct ? 'correct' : 'wrong')) + '"' +
      ' data-side="' + (homePick ? 'home' : 'away') + '"' +
      ' data-teams="' + esc((g.away + ' ' + g.home).toLowerCase()) + '">' +
      '<button class="game-head" aria-expanded="false">' +
        '<span class="gdate">' + lg + shortDate(g.date) + '</span>' +
        '<span class="teams">' + teamCell(g.away, g.away_s, g.away_rec, awayCls, false) + centerCell(g) +
          teamCell(g.home, g.home_s, g.home_rec, homeCls, true) + '</span>' +
        '<span class="meta">' +
          '<span class="bar"><i class="a" style="width:' + (g.away_prob * 100).toFixed(1) + '%"></i>' +
          '<i class="h" style="width:' + (g.home_prob * 100).toFixed(1) + '%"></i></span>' +
          '<span class="metarow">' + lock +
            '<span class="pick">' + (homePick ? '→' : '←') + ' <b>' +
              nameSpans(g.favored, homePick ? (g.home_s || g.home) : (g.away_s || g.away)) + '</b> ' + pct(g.pick_prob) + '</span>' +
            '<span class="tag ' + g.conf + '">' + g.conf.toUpperCase() + '</span>' + flag +
            (propCount ? '<span class="tag props">' + propCount + ' PROPS</span>' : '') + result +
          '</span></span>' +
        '<span class="chev" aria-hidden="true">▾</span>' +
      '</button><div class="detail" hidden></div></article>';
  }

  // ── game detail ──────────────────────────────────────────────────────────
  function buildDetail(g, d) {
    var tabs = [['matchup', 'Matchup']];
    var hasProps = g.props && countProps(g.props) > 0;
    if (hasProps) {
      tabs.push(['away', (g.away_s || g.away) + ' props']);
      tabs.push(['home', (g.home_s || g.home) + ' props']);
    }
    var head = '<div class="dtabs" role="tablist">' + tabs.map(function (t, i) {
      return '<button class="dtab" role="tab" data-dtab="' + t[0] + '" aria-selected="' + (i === 0) + '">' + esc(t[1]) + '</button>';
    }).join('') + '</div>';
    var body = '<div class="panel" data-dpanel="matchup">' + matchupPanel(g, d) + '</div>';
    if (hasProps) {
      body += '<div class="panel" data-dpanel="away" hidden>' + playersPanel(g, 'away') + '</div>';
      body += '<div class="panel" data-dpanel="home" hidden>' + playersPanel(g, 'home') + '</div>';
    }
    return head + body;
  }

  function matchupPanel(g, d) {
    var c = g.components || {};
    var out = '<div class="cards">' +
      card('Model pick', nameSpans(g.favored, g.favored === g.home ? (g.home_s || g.home) : (g.away_s || g.away)),
           pct(g.pick_prob, 1) + ' · ' + g.conf + ' confidence' + (g.locked ? ' · locked' : '')) +
      card('Elo', pct(c.elo, 1), 'home win probability') +
      (c.model != null ? card('Learned model', pct(c.model, 1), 'form + rest + Elo') : '') +
      (c.standings != null ? card('Season stats', pct(c.standings, 1), 'standings model') : '') + '</div>';

    if (g.locked) {
      out += '<div class="note">🔒 This forecast was frozen at first pitch' +
        (g.correct != null ? ' and graded ' + (g.correct ? '<b>correct</b>' : '<b>incorrect</b>') : '') + '.</div>';
    }
    if (g.drivers && g.drivers.length) {
      var labels = (d || {}).feature_labels || {};
      var max = Math.max.apply(null, g.drivers.map(function (x) { return Math.abs(x[1]); })) || 1;
      out += '<div><div class="section-title">What is driving this pick</div><div class="drivers">' +
        g.drivers.map(function (x) {
          var w = Math.min(Math.abs(x[1]) / max, 1) * 50;
          var side = x[1] >= 0 ? { full: g.home, short: g.home_s || g.home } : { full: g.away, short: g.away_s || g.away };
          return '<div class="driver"><span class="dname">' + esc(labels[x[0]] || x[0]) +
            ' <span style="color:var(--faint)">favours ' + nameSpans(side.full, side.short) + '</span></span>' +
            '<span class="dbar"><i class="' + (x[1] >= 0 ? 'pos' : 'neg') + '" style="width:' + w.toFixed(1) + '%"></i></span></div>';
        }).join('') + '</div></div>';
    }
    if (g.injuries && (g.injuries.away || g.injuries.home)) {
      var inj = function (side, name, short) {
        var rows = g.injuries[side] || [];
        if (!rows.length) return '';
        return '<div class="inj-team"><div class="inj-head">' + nameSpans(name, short) + '</div>' + rows.map(function (r) {
          return '<div class="inj-row"><span class="tag ' + (r.level === 'out' ? 'no' : 'med') + '">' +
            esc((r.status || r.level || '').toUpperCase()) + '</span> <b>' + esc(r.name) + '</b>' +
            (r.pos ? ' <span style="color:var(--faint)">' + esc(r.pos) + '</span>' : '') +
            (r.detail ? '<span class="inj-detail">' + esc(r.detail) + '</span>' : '') + '</div>';
        }).join('') + '</div>';
      };
      out += '<div><div class="section-title">Availability</div><div class="inj-grid">' +
        inj('away', g.away, g.away_s) + inj('home', g.home, g.home_s) + '</div></div>';
    }
    if (g.starter_edge) {
      out += '<div class="note">Probable starters shift the standings model by ' + (g.starter_edge > 0 ? '+' : '') +
        g.starter_edge.toFixed(2) + ' log-odds, favouring ' +
        esc(g.starter_edge > 0 ? (g.home_s || g.home) : (g.away_s || g.away)) + '.</div>';
    }
    var ctx = g.context || {};
    out += '<div><div class="section-title">Form &amp; situation</div><div class="scroll-x"><table class="grid">' +
      '<thead><tr><th class="num">' + nameSpans(g.away, g.away_s) + '</th><th class="mid">&nbsp;</th><th>' + nameSpans(g.home, g.home_s) + '</th></tr></thead><tbody>' +
      ctxRow('Elo rating', ctx.away_elo, ctx.home_elo, false) +
      ctxRow('Recent record', ctx.away_last10, ctx.home_last10, false) +
      ctxRow('Recent margin', ctx.away_margin10, ctx.home_margin10, false) +
      ctxRow('Scored (recent)', ctx.away_scored10, ctx.home_scored10, false) +
      ctxRow('Allowed (recent)', ctx.away_allowed10, ctx.home_allowed10, true) +
      ctxRow('Road / home win rate', ctx.away_venue == null ? null : pct(ctx.away_venue),
             ctx.home_venue == null ? null : pct(ctx.home_venue), false) +
      ctxRow('Days rest', ctx.away_rest, ctx.home_rest, false) +
      (ctx.h2h ? ctxRow('Head to head', '', ctx.h2h, false) : '') + '</tbody></table></div></div>';
    var defs = (d || {}).stats || [];
    if (g.stats && g.stats.length) {
      var rows = g.stats.map(function (pair, i) {
        var def = defs[i] || {}, a = pair[0], h = pair[1], ac = '', hc = '';
        if (a == null && h == null) return '';
        if (a != null && h != null && a !== h) { var ab = def.lower_better ? a < h : a > h; ac = ab ? 'better' : 'worse'; hc = ab ? 'worse' : 'better'; }
        return '<tr><td class="num ' + ac + '">' + fmt(a, def.fmt) + esc(def.suffix || '') + '</td>' +
          '<td class="mid" style="color:var(--faint)">' + esc(def.label || '') + '</td>' +
          '<td class="' + hc + '">' + fmt(h, def.fmt) + esc(def.suffix || '') + '</td></tr>';
      }).join('');
      if (rows) out += '<div><div class="section-title">Season statistics</div><div class="scroll-x"><table class="grid"><tbody>' + rows + '</tbody></table></div></div>';
    }
    if (g.props && g.props.expected) {
      out += '<div class="note">Projected score: <b>' + nameSpans(g.away, g.away_s) + ' ' + g.props.expected.away +
        '</b> – <b>' + g.props.expected.home + ' ' + nameSpans(g.home, g.home_s) + '</b>.</div>';
    }
    return out;
  }

  function ctxRow(label, a, h, lowerBetter) {
    if ((a == null || a === '') && (h == null || h === '')) return '';
    var ac = '', hc = '', na = parseFloat(a), nh = parseFloat(h);
    if (!isNaN(na) && !isNaN(nh) && na !== nh) { var aw = lowerBetter ? na < nh : na > nh; ac = aw ? 'better' : 'worse'; hc = aw ? 'worse' : 'better'; }
    return '<tr><td class="num ' + ac + '">' + (a == null || a === '' ? '—' : esc(a)) + '</td>' +
      '<td class="mid" style="color:var(--faint)">' + esc(label) + '</td>' +
      '<td class="' + hc + '">' + (h == null || h === '' ? '—' : esc(h)) + '</td></tr>';
  }

  // ── players & props ──────────────────────────────────────────────────────
  function headlineProp(props) {
    var list = props || [];
    if (!list.length) return null;
    var popular = list.filter(function (p) { return (p.rank || 99) <= 3; });
    var pool = popular.length ? popular : list;
    var moved = pool.filter(function (p) { return Math.abs(p.edge || 0) >= 0.15; });
    if (moved.length) return moved.slice().sort(function (a, b) { return Math.abs(b.edge || 0) - Math.abs(a.edge || 0); })[0];
    return pool.slice().sort(function (a, b) { return b.pick_prob - a.pick_prob; })[0];
  }

  function playersPanel(g, side) {
    var players = (g.props && g.props[side]) || [];
    if (!players.length) return '<div class="empty"><span class="icon">👤</span>No player projections for this side.</div>';
    var locked = g.props_locked ? '<div class="note">🔒 Locked at first pitch. The live box score is shown against each line; ' +
      'no line, projection or probability changes once the game starts.</div>' : '';
    return locked + '<div class="players">' + players.map(function (p, i) {
      var best = headlineProp(p.props);
      var hits = (p.props || []).filter(function (x) { return x.hit != null; });
      var record = hits.length ? '<span class="tag ' + (hits.filter(function (x) { return x.hit; }).length >= hits.length / 2 ? 'ok' : 'no') + '">' +
        hits.filter(function (x) { return x.hit; }).length + '/' + hits.length + '</span>' : '';
      var shot = p.headshot ? '<img class="pshot" src="' + esc(p.headshot) + '" alt="" loading="lazy" decoding="async">' : '<span class="pshot" aria-hidden="true"></span>';
      return '<div class="player" data-player="' + i + '" data-athlete="' + esc(p.id || '') + '">' +
        '<button class="player-head" aria-expanded="false">' + shot +
          '<span class="pinfo"><span class="pname">' + esc(p.name) +
            (p.status ? ' <span class="tag med" title="' + esc(p.status_detail || '') + '">' + esc(p.status).toUpperCase() + '</span>' : '') + '</span>' +
          '<span class="pmeta">' + [p.pos, p.role, p.gp ? p.gp + ' GP' : ''].filter(Boolean).map(esc).join(' · ') + '</span></span>' +
          '<span class="pspacer"></span>' + record +
          (best && !hits.length ? '<span class="pbest"><b>' + esc(best.label) + '</b> ' + best.pick.toUpperCase() + ' ' + best.line + '<br>' + pct(best.pick_prob) + '</span>' : '') +
          '<span class="chev" aria-hidden="true">▾</span></button>' +
        '<div class="prop-list" hidden>' + (p.props || []).map(propRow).join('') + '</div></div>';
    }).join('') + '</div>';
  }

  function propRow(p) {
    var lo = p.range ? p.range[0] : p.proj, hi = p.range ? p.range[1] : p.proj;
    var span = Math.max(hi - lo, 1e-6), pad = span * 0.35, min = lo - pad, max = hi + pad;
    var toPct = function (v) { return ((v - min) / (max - min)) * 100; };
    var deltaCls = p.delta > 0.01 ? 'up' : (p.delta < -0.01 ? 'down' : '');
    var deltaTxt = p.delta == null || Math.abs(p.delta) < 0.01 ? '' : (p.delta > 0 ? '+' : '') + p.delta + ' vs season';
    var boxKey = (p.stat || '').replace(/_pg$/, '');
    var outcome = '';
    if (p.push) outcome = '<span class="prop-live low">PUSH ' + p.actual + '</span>';
    else if (p.hit != null) outcome = '<span class="prop-live ' + (p.hit ? 'ok' : 'no') + '">' + (p.hit ? '✓ HIT' : '✗ MISS') + ' · ' + p.actual + '</span>';
    else if (p.played === false) outcome = '<span class="prop-live low">DNP</span>';
    var dot = p.actual != null && !p.push ? '<b class="live-dot" style="left:' + Math.max(0, Math.min(100, toPct(p.actual))).toFixed(1) + '%"></b>' : '';
    return '<div class="prop" data-stat="' + esc(boxKey) + '" data-line="' + p.line + '" data-lo="' + lo + '" data-hi="' + hi + '">' +
      '<span class="prop-name">' + esc(p.label) + (outcome || '<span class="prop-live" hidden></span>') +
        '<span class="prop-sub">season ' + p.season + ' ' + esc(p.unit || '') +
        (deltaTxt ? ' · <span class="delta ' + deltaCls + '">' + deltaTxt + '</span>' : '') + '</span></span>' +
      '<span class="prop-nums"><span class="prop-num"><span class="lbl">Line</span>' + p.line + '</span>' +
        '<span class="prop-num"><span class="lbl">Proj</span>' + p.proj + '</span></span>' +
      '<span class="prop-range" title="Likely range ' + lo + '–' + hi + '">' +
        '<i style="left:' + toPct(lo).toFixed(1) + '%;width:' + (toPct(hi) - toPct(lo)).toFixed(1) + '%"></i>' +
        '<u style="left:' + toPct(p.line).toFixed(1) + '%"></u>' + dot + '</span>' +
      '<span class="prop-pick"><span class="pickdir ' + p.pick + '">' + p.pick.toUpperCase() + '</span>' +
        '<span class="tag ' + p.conf + '">' + pct(p.pick_prob) + '</span></span></div>';
  }

  // ── filtering ────────────────────────────────────────────────────────────
  function allGames(scope) {
    var out = [];
    leaguesInView().forEach(function (k) {
      var d = DATA[k]; if (!d) return;
      var src = scope === 'results' ? d.games.concat(HISTORY[k] || []) : d.games;
      src.forEach(function (g) { out.push({ g: g, league: k, t: todayOf(d) }); });
    });
    return out;
  }
  function findGame(league, id) {
    var d = DATA[league]; if (!d) return null;
    var all = d.games.concat(HISTORY[league] || []);
    for (var i = 0; i < all.length; i++) if (all[i].id === id) return all[i];
    return null;
  }
  function filteredGames(scope) {
    var f = state.filters[scope] || {};
    var q = state.search.trim().toLowerCase();
    var items = allGames(scope).filter(function (x) {
      var g = x.g, t = x.t;
      if (scope === 'today') return g.date === t;
      if (scope === 'upcoming') return g.date > t && !g.final;
      if (scope === 'results') return g.final && !g.preseason;
      return true;
    });
    if (f.conf && f.conf !== 'all') items = items.filter(function (x) { return x.g.conf === f.conf; });
    if (f.side && f.side !== 'all') items = items.filter(function (x) { return (x.g.favored === x.g.home ? 'home' : 'away') === f.side; });
    if (f.result && f.result !== 'all') items = items.filter(function (x) { return (x.g.correct ? 'correct' : 'wrong') === f.result; });
    if (f.days && f.days !== 'all') {
      var n = parseInt(f.days, 10);
      items = items.filter(function (x) {
        var limit = scope === 'results' ? addDays(x.t, -n) : addDays(x.t, n);
        return scope === 'results' ? x.g.date >= limit : x.g.date <= limit;
      });
    }
    if (q) items = items.filter(function (x) { return (x.g.away + ' ' + x.g.home).toLowerCase().indexOf(q) >= 0; });
    items.sort(function (a, b) {
      if (scope === 'results') return a.g.date < b.g.date ? 1 : (a.g.date > b.g.date ? -1 : 0);
      if (a.g.date !== b.g.date) return a.g.date < b.g.date ? -1 : 1;
      return (a.g.time || '') < (b.g.time || '') ? -1 : 1;
    });
    return items;
  }

  var FILTERS = {
    today: [['conf', 'Confidence', [['all', 'All'], ['high', 'High'], ['med', 'Med'], ['low', 'Low']]],
            ['side', 'Favoured', [['all', 'All'], ['home', 'Home'], ['away', 'Away']]]],
    upcoming: [['days', 'Window', [['3', 'Next 3 days'], ['7', 'Next week'], ['all', 'All']]],
               ['conf', 'Confidence', [['all', 'All'], ['high', 'High'], ['med', 'Med'], ['low', 'Low']]]],
    results: [['days', 'Period', [['7', 'Last 7 days'], ['14', 'Last 14'], ['all', 'All']]],
              ['result', 'Result', [['all', 'All'], ['correct', '✓ Correct'], ['wrong', '✗ Wrong']]],
              ['conf', 'Confidence', [['all', 'All'], ['high', 'High'], ['med', 'Med'], ['low', 'Low']]]]
  };
  function defaults(scope) {
    var d = { conf: 'all', side: 'all', result: 'all' };
    if (scope === 'upcoming') d.days = '7';
    if (scope === 'results') d.days = '14';
    return d;
  }
  function activeFilterCount() {
    var f = state.filters[state.view] || {}, base = defaults(state.view), n = 0;
    Object.keys(f).forEach(function (k) { if (f[k] !== (base[k] === undefined ? 'all' : base[k])) n++; });
    if (state.search.trim()) n++;
    return n;
  }
  function filterShell(inner, countLabel) {
    var open = state.filtersOpen ? ' open' : '';
    return '<div class="toolbar' + open + '">' +
      '<button class="filter-toggle" id="filter-toggle" aria-expanded="' + (state.filtersOpen ? 'true' : 'false') + '">' +
        '<span class="ft-icon" aria-hidden="true">☰</span> Filters' +
        (activeFilterCount() ? '<span class="ft-badge">' + activeFilterCount() + '</span>' : '') + '</button>' +
      '<span class="count count-mobile">' + countLabel + '</span>' +
      '<div class="toolbar-body">' + inner + '<span class="count count-wide">' + countLabel + '</span></div></div>';
  }
  function toolbar(scope, count) {
    var f = state.filters[scope] || (state.filters[scope] = defaults(scope));
    var groups = (FILTERS[scope] || []).map(function (grp) {
      return '<div class="fgroup"><span class="flabel">' + grp[1] + '</span>' + grp[2].map(function (o) {
        return '<button class="chip" data-filter="' + grp[0] + '" data-value="' + o[0] + '" aria-pressed="' + (f[grp[0]] === o[0]) + '">' + o[1] + '</button>';
      }).join('') + '</div>';
    }).join('');
    return filterShell(groups + '<div class="fgroup"><input class="search" id="search" type="search" placeholder="Filter by team…" value="' +
      esc(state.search) + '" aria-label="Filter by team"></div>', count + ' game' + (count === 1 ? '' : 's'));
  }

  function gameList(items) {
    if (!items.length) return '<div class="empty"><span class="icon">' + (EMOJI[state.league] || '🏟') + '</span>No games match these filters.</div>';
    if (!isAll()) return '<div class="games">' + items.map(function (x) { return gameRow(x.g, x.league); }).join('') + '</div>';
    // Overview: group by league so each block reads like its own board.
    var out = '';
    LEAGUES.forEach(function (k) {
      var mine = items.filter(function (x) { return x.league === k; });
      if (!mine.length) return;
      out += '<div class="section-title">' + EMOJI[k] + ' ' + LABEL[k] + ' <span class="count-inline">' + mine.length + '</span></div>' +
        '<div class="games">' + mine.map(function (x) { return gameRow(x.g, k); }).join('') + '</div>';
    });
    return out;
  }
  function gamesView(scope) {
    var items = filteredGames(scope);
    return toolbar(scope, items.length) + gameList(items);
  }

  // ── overview strip (All · Today) ─────────────────────────────────────────
  function bestPicks() {
    var picks = allGames('today').filter(function (x) { return x.g.date === x.t && !x.g.final && !x.g.preseason; })
      .sort(function (a, b) { return b.g.pick_prob - a.g.pick_prob; }).slice(0, 6);
    if (!picks.length) return '';
    return '<div class="section-title">Strongest picks today</div><div class="strip">' + picks.map(function (x) {
      var g = x.g, home = g.favored === g.home;
      return '<div class="pick-card" data-jump="' + x.league + '|' + esc(g.id) + '">' +
        '<div class="pc-top"><span class="lg-chip">' + EMOJI[x.league] + '</span><span class="tag ' + g.conf + '">' + pct(g.pick_prob) + '</span></div>' +
        '<div class="pc-team">' + esc(home ? (g.home_s || g.home) : (g.away_s || g.away)) + '</div>' +
        '<div class="pc-sub">' + (home ? 'vs ' : '@ ') + esc(home ? (g.away_s || g.away) : (g.home_s || g.home)) + ' · ' + esc(g.time || 'TBD') + '</div></div>';
    }).join('') + '</div>';
  }

  // ── results ──────────────────────────────────────────────────────────────
  function resultsView() {
    var leagues = leaguesInView();
    var cards = '', total = 0, backfilled = 0;
    leagues.forEach(function (k) {
      var a = (DATA[k] || {}).accuracy || {}, v = a.verified || {}, bt = a.backtest;
      total += v.total || 0; backfilled += a.backfilled || 0;
      var label = isAll() ? EMOJI[k] + ' ' + LABEL[k] : 'Verified record';
      cards += v.total
        ? card(label, pct(v.pct, 1), v.correct + ' of ' + v.total + ' verified picks')
        : card(label, bt && bt.pct != null ? pct(bt.pct, 1) + ' backtest' : '—', 'no verified picks yet');
    });
    var played = filteredGames('results').length;
    if (!total && !backfilled && !played) {
      return '<div class="empty"><span class="icon">' + (EMOJI[state.league] || '🏟') + '</span>No completed games yet. Results appear once games have been played.</div>';
    }
    var head = '<div class="cards">' + cards + '</div>';
    if (!isAll() && !total) {
      head += '<details class="explain"><summary>Why is the record empty?</summary><p>Only games forecast <b>before</b> first pitch count. ' +
        (backfilled ? backfilled + ' completed game' + (backfilled === 1 ? ' was' : 's were') + ' first seen after the final whistle, so ' +
        'their picks were made with standings that already contained the result. ' : '') +
        'The backtest figure is the honest alternative: the model run over history using only what was known at the time.</p></details>';
    }
    var scoped = filteredGames('results');
    state.showCountBadge = scoped.some(function (x) { return x.g.counted; }) && scoped.some(function (x) { return !x.g.counted; });
    var list = gamesView('results');
    return head + '<div class="section-title">Completed games</div>' + list;
  }

  // ── props board ──────────────────────────────────────────────────────────
  function allProps() {
    var out = [];
    leaguesInView().forEach(function (k) { var d = DATA[k]; if (!d) return; (d.games || []).forEach(function (g) {
      if (!g.props) return;
      ['away', 'home'].forEach(function (side) {
        (g.props[side] || []).forEach(function (pl) {
          (pl.props || []).forEach(function (p) {
            out.push({ g: g, side: side, player: pl, prop: p, league: k });
          });
        });
      });
    }); });
    return out;
  }

  function propsView() {
    var d = cur() || {};
    var rows = allProps();
    if (!rows.length) {
      var why = d.props_status === 'unavailable'
        ? 'Player statistics could not be fetched on the last run. The board fills in automatically on the next refresh.'
        : 'No upcoming games with player projections right now.';
      return '<div class="empty"><span class="icon">👤</span>' + esc(why) + '</div>';
    }
    var f = state.filters.props ||
      (state.filters.props = { conf: 'all', pick: 'all', cat: 'all', sort: 'edge' });
    if (!f.sort) f.sort = 'edge';
    var cats = {};
    rows.forEach(function (r) { cats[r.prop.label] = 1; });
    var catOpts = ['<option value="all">All props</option>'].concat(
      Object.keys(cats).sort().map(function (c) {
        return '<option value="' + esc(c) + '"' + (f.cat === c ? ' selected' : '') + '>' + esc(c) + '</option>';
      })).join('');

    var q = state.search.trim().toLowerCase();
    // One row per player and prop: a player on a three-game series would
    // otherwise fill the board with the same read three times over.
    var seen = {};
    var list = rows.filter(function (r) {
      if (f.conf !== 'all' && r.prop.conf !== f.conf) return false;
      if (f.pick !== 'all' && r.prop.pick !== f.pick) return false;
      if (f.cat !== 'all' && r.prop.label !== f.cat) return false;
      if (q && (r.player.name + ' ' + r.g.away + ' ' + r.g.home).toLowerCase().indexOf(q) < 0) return false;
      var key = r.player.id + '|' + r.player.name + '|' + r.prop.key;
      if (seen[key] && seen[key] <= r.g.date) return false;
      seen[key] = r.g.date;
      return true;
    }).sort(function (a, b) {
      if (f.sort === 'conf') return b.prop.pick_prob - a.prop.pick_prob;
      return Math.abs(b.prop.edge || 0) - Math.abs(a.prop.edge || 0);
    }).slice(0, 250);

    var bar = filterShell(
      '<div class="fgroup"><span class="flabel">Confidence</span>' +
        ['all', 'high', 'med', 'low'].map(function (v) {
          return '<button class="chip" data-filter="conf" data-value="' + v + '" aria-pressed="' +
            (f.conf === v) + '">' + (v === 'all' ? 'All' : v.toUpperCase()) + '</button>';
        }).join('') + '</div>' +
      '<div class="fgroup"><span class="flabel">Side</span>' +
        ['all', 'over', 'under'].map(function (v) {
          return '<button class="chip" data-filter="pick" data-value="' + v + '" aria-pressed="' +
            (f.pick === v) + '">' + (v === 'all' ? 'All' : v.toUpperCase()) + '</button>';
        }).join('') + '</div>' +
      '<div class="fgroup"><span class="flabel">Sort</span>' +
        [['edge', 'Model edge'], ['conf', 'Confidence']].map(function (v) {
          return '<button class="chip" data-filter="sort" data-value="' + v[0] + '" aria-pressed="' +
            (f.sort === v[0]) + '">' + v[1] + '</button>';
        }).join('') + '</div>' +
      '<div class="fgroup"><select class="search" id="cat-filter" aria-label="Prop type">' + catOpts + '</select></div>' +
      '<div class="fgroup"><input class="search" id="search" type="search" placeholder="Player or team…" value="' +
        esc(state.search) + '" aria-label="Filter players"></div>',
      list.length + ' props');

    var body = '<div class="scroll-x"><table class="grid"><thead><tr>' +
      '<th>Player</th><th>Prop</th><th class="num">Line</th><th class="num">Proj</th>' +
      '<th class="num">Edge</th><th>Lean</th><th class="num">Prob</th><th>Game</th></tr></thead><tbody>' +
      list.map(function (r) {
        var p = r.prop;
        return '<tr><td><b>' + esc(r.player.name) + '</b><br><span style="color:var(--faint)">' +
            esc(r.player.pos || '') + '</span></td>' +
          '<td>' + (isAll() ? '<span class="lg-chip">' + EMOJI[r.league] + '</span> ' : '') + esc(p.label) + '</td>' +
          '<td class="num">' + p.line + '</td>' +
          '<td class="num">' + p.proj + '</td>' +
          '<td class="num ' + (p.edge > 0.05 ? 'better' : (p.edge < -0.05 ? 'worse' : '')) + '">' +
            (p.edge == null ? '—' : (p.edge > 0 ? '+' : '') + p.edge.toFixed(2) + 'σ') + '</td>' +
          '<td><span class="pickdir ' + p.pick + '">' + p.pick.toUpperCase() + '</span></td>' +
          '<td class="num"><span class="tag ' + p.conf + '">' + pct(p.pick_prob) + '</span></td>' +
          '<td style="color:var(--muted)">' + esc(r.g.away) + ' @ ' + esc(r.g.home) +
            '<br><span style="color:var(--faint)">' + shortDate(r.g.date) + '</span></td></tr>';
      }).join('') + '</tbody></table></div>';

    var note = '<div class="note">Lines are set at each player\'s season baseline and priced against a ' +
      'matchup-adjusted projection, so a lean reflects the model disagreeing with that baseline — these ' +
      'are not sportsbook numbers, so check the real market before acting on anything here. ' +
      '<b>Model edge</b> sorts by how far this matchup moves a player off their own season ' +
      'baseline, in standard deviations — that is the part the model has an opinion about. ' +
      '<b>Confidence</b> sorts by raw probability instead, which favours near-certainties such as ' +
      'an unlikely home run.</div>';
    return note + bar + body;
  }


  // ── model view ───────────────────────────────────────────────────────────
  function modelView() {
    if (isAll()) {
      return '<div class="cards">' + leaguesInView().map(function (k) {
        var m = (DATA[k] || {}).model || {}, v = m.validation || {};
        return card(EMOJI[k] + ' ' + LABEL[k], m.stage === 'trained' ? pct(v.acc, 1) : 'Warming up',
          (m.n_train || 0) + ' games · ' + ((m.ledger || {}).graded || 0) + ' graded forecasts');
      }).join('') + '</div><div class="note">Pick a single sport for its calibration, feature weights and training log.</div>';
    }
    var d = cur();
    var m = d.model || {};
    var v = m.validation || {};
    var led = m.ledger || {};
    var trained = m.stage === 'trained';
    var out = '<div class="cards">' +
      (trained
        ? card('Validated accuracy', pct(v.acc, 1), 'walk-forward, ' + (v.n || 0) + ' games')
        : card('Validated accuracy', 'Warming up', 'needs ' + (m.min_train || 110) + ' games')) +
      (trained
        ? card('Brier score', v.brier != null ? v.brier.toFixed(4) : '—', 'lower is better') +
          card('Log loss', v.logloss != null ? v.logloss.toFixed(4) : '—', 'lower is better')
        : '') +
      card('Training games', m.n_train || 0, (m.archive || 0) + ' in archive') +
      card('Model trust', m.trust != null ? pct(m.trust) : 'scheduled', m.trust_source || '') +
      card('Graded forecasts', led.graded || 0, 'recorded before kickoff') +
      '</div>';

    if (!trained) {
      out += '<div class="note">This league has <b>' + (m.n_train || 0) + '</b> completed games ' +
        'on record — too few for walk-forward validation to mean anything, so no accuracy figure is ' +
        'published yet. Until then predictions lean on the season-standings model, and the archive ' +
        'grows with every refresh.</div>';
    }

    out += '<div class="note"><b>Predictions are frozen at kickoff.</b> The first forecast ' +
      'published for a game is written to a ledger and is what the site shows from then on. ' +
      'Without that, refitting hourly against updated standings lets a finished game drift — ' +
      'and once the result is in the standings, the model can end up naming the winner as the ' +
      'team it favoured all along.</div>';

    out += '<div class="note"><b>How this improves itself.</b> Every run appends completed games to a ' +
      'permanent archive, so the ratings keep a longer memory than the fetch window. Every prediction is ' +
      'written to a ledger <i>before</i> the game starts and graded afterwards, which is the only ' +
      'measurement here that cannot be flattered by hindsight. New parameters are adopted only when they ' +
      'beat the incumbent on validation.</div>';

    if (m.curve && m.curve.length > 3) out += curveSvg(m.curve);


    if (m.importance && m.importance.length) {
      var max = Math.max.apply(null, m.importance.map(function (i) { return i.weight; })) || 1;
      out += '<div><div class="section-title">What the model weighs</div><div class="drivers">' +
        m.importance.map(function (i) {
          return '<div class="driver"><span class="dname">' + esc(i.label) + '</span>' +
            '<span class="dbar"><i class="pos" style="left:0;width:' +
            ((i.weight / max) * 100).toFixed(1) + '%"></i></span></div>';
        }).join('') + '</div></div>';
    }

    if (m.reliability && m.reliability.length) {
      out += '<div><div class="section-title">Calibration</div><div class="scroll-x"><table class="grid">' +
        '<thead><tr><th>Confidence band</th><th class="num">Games</th><th class="num">Predicted</th>' +
        '<th class="num">Actual</th></tr></thead><tbody>' +
        m.reliability.map(function (b) {
          var gap = b.actual != null && b.pred != null ? Math.abs(b.actual - b.pred) : null;
          var cls = gap == null ? '' : (gap < 0.04 ? 'better' : (gap > 0.1 ? 'worse' : ''));
          return '<tr><td>' + pct(b.lo) + '–' + pct(Math.min(b.hi, 1)) + '</td>' +
            '<td class="num">' + b.n + '</td><td class="num">' + pct(b.pred, 1) + '</td>' +
            '<td class="num ' + cls + '">' + pct(b.actual, 1) + '</td></tr>';
        }).join('') + '</tbody></table></div></div>';
    }

    if (led.components && Object.keys(led.components).length) {
      out += '<div><div class="section-title">Component scorecard (pre-game ledger)</div><div class="scroll-x">' +
        '<table class="grid"><thead><tr><th>Source</th><th class="num">Games</th><th class="num">Accuracy</th>' +
        '<th class="num">Log loss</th></tr></thead><tbody>' +
        Object.keys(led.components).map(function (k) {
          var c = led.components[k];
          return '<tr><td>' + esc(k) + '</td><td class="num">' + c.n + '</td>' +
            '<td class="num">' + pct(c.acc, 1) + '</td><td class="num">' + c.logloss + '</td></tr>';
        }).join('') + '</tbody></table></div></div>';
    }

    var pr = d.props_record || {};
    if (pr.total) {
      var bc = pr.by_conf || {};
      out += '<div><div class="section-title">Player prop record (graded against box scores)</div>' +
        '<div class="cards">' +
        card('All props', pct(pr.pct, 1), pr.total + ' graded') +
        card('High confidence', pct(bc.high && bc.high.pct, 1), (bc.high ? bc.high.n : 0) + ' props') +
        card('Medium', pct(bc.med && bc.med.pct, 1), (bc.med ? bc.med.n : 0) + ' props') +
        card('Low', pct(bc.low && bc.low.pct, 1), (bc.low ? bc.low.n : 0) + ' props') +
        '</div>';
      var keys = Object.keys(pr.by_key || {}).sort(function (a, b) {
        return pr.by_key[b].n - pr.by_key[a].n;
      });
      if (keys.length) {
        var tuning = d.props_tuning || {};
        out += '<div class="scroll-x"><table class="grid"><thead><tr><th>Market</th>' +
          '<th class="num">Graded</th><th class="num">Hit rate</th>' +
          '<th class="num">Bias fix</th><th class="num">Spread fix</th></tr></thead><tbody>' +
          keys.map(function (k) {
            var r = pr.by_key[k], t = tuning[k];
            return '<tr><td>' + esc(r.label || k) + '</td><td class="num">' + r.n + '</td>' +
              '<td class="num ' + (r.pct >= 0.55 ? 'better' : (r.pct < 0.48 ? 'worse' : '')) + '">' +
              pct(r.pct, 1) + '</td>' +
              '<td class="num">' + (t ? '×' + t.bias.toFixed(2) : '—') + '</td>' +
              '<td class="num">' + (t ? '×' + t.spread.toFixed(2) : '—') + '</td></tr>';
          }).join('') + '</tbody></table></div>' +
          '<div class="note">Once a market has 30 graded props, the ledger starts correcting its ' +
          'projections: a bias multiplier when players systematically beat or miss the projection, ' +
          'and a spread multiplier when outcomes scatter more or less than the distribution assumed. ' +
          'Both phase in with sample size.</div></div>';
      }
    }

    if (m.runs && m.runs.length) {
      out += '<div><div class="section-title">Recent training runs</div><div class="scroll-x"><table class="grid">' +
        '<thead><tr><th>When</th><th>Adopted</th><th>Why</th></tr></thead><tbody>' +
        m.runs.slice().reverse().map(function (r) {
          return '<tr><td>' + esc((r.at || '').replace('T', ' ').replace('Z', '')) + '</td>' +
            '<td class="' + (r.adopted ? 'better' : 'worse') + '">' + (r.adopted ? 'yes' : 'no') + '</td>' +
            '<td style="color:var(--muted)">' + esc(r.reason || '') + '</td></tr>';
        }).join('') + '</tbody></table></div></div>';
    }

    out += '<div class="note">Elo parameters in use: K ' + fmtNum(m.elo && m.elo.k) +
      ', home advantage ' + fmtNum(m.elo && m.elo.hfa) + ' points, margin weight ' +
      fmtNum(m.elo && m.elo.mov) + ', season regression ' + fmtNum(m.elo && m.elo.regress) +
      '. Generated ' + esc(d.generated) + '.</div>';
    return out;
  }

  function fmtNum(v) { return v == null ? '—' : (Math.round(v * 100) / 100); }

  function curveSvg(points) {
    var w = 640, h = 132, pad = 26;
    var xs = points.length - 1 || 1;
    var vals = points.map(function (p) { return p.cum_acc; });
    var lo = Math.min.apply(null, vals.concat([0.45]));
    var hi = Math.max.apply(null, vals.concat([0.65]));
    var span = (hi - lo) || 0.1;
    var path = points.map(function (p, i) {
      var x = pad + (i / xs) * (w - pad - 8);
      var y = h - pad - ((p.cum_acc - lo) / span) * (h - pad - 12);
      return (i ? 'L' : 'M') + x.toFixed(1) + ' ' + y.toFixed(1);
    }).join(' ');
    var halfY = h - pad - ((0.5 - lo) / span) * (h - pad - 12);
    return '<div><div class="section-title">Cumulative accuracy of graded pre-game picks</div>' +
      '<svg class="curve" viewBox="0 0 ' + w + ' ' + h + '" preserveAspectRatio="none" role="img" ' +
      'aria-label="Cumulative accuracy over time">' +
      '<line class="axis" x1="' + pad + '" y1="' + (h - pad) + '" x2="' + w + '" y2="' + (h - pad) + '"/>' +
      (halfY > 0 && halfY < h ? '<line class="ref" x1="' + pad + '" y1="' + halfY.toFixed(1) +
        '" x2="' + w + '" y2="' + halfY.toFixed(1) + '"/>' : '') +
      '<path class="line" d="' + path + '"/></svg>' +
      '<div class="legend"><span>' + points.length + ' days graded</span>' +
      '<span>latest ' + pct(points[points.length - 1].cum_acc, 1) + '</span>' +
      '<span>dashed line = coin flip</span></div></div>';
  }


  // ── render ───────────────────────────────────────────────────────────────
  function render() {
    renderChrome();
    var root = el('main');
    var ready = isAll() ? LEAGUES.some(function (k) { return DATA[k]; }) : !!cur();
    if (!ready) {
      root.innerHTML = '<div class="games"><div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div></div>';
      return;
    }
    if (state.view === 'results') {
      var need = leaguesInView().filter(function (k) { return !HISTORY[k]; });
      if (need.length) {
        root.innerHTML = '<div class="games"><div class="skeleton"></div><div class="skeleton"></div></div>';
        var want = state.league;
        loadHistory(need, function () { if (state.league === want && state.view === 'results') render(); });
        return;
      }
    }
    var html;
    if (state.view === 'results') html = resultsView();
    else if (state.view === 'props') html = propsView();
    else if (state.view === 'model') html = modelView();
    else if (state.view === 'today' && isAll()) html = bestPicks() + gamesView('today');
    else html = gamesView(state.view);
    root.innerHTML = html;
    if (state.view === 'today') startLive(); else stopLive();
  }

  function go(league, view, replace) {
    state.league = league; state.view = view; state.search = '';
    var hash = '#' + league + '/' + view;
    if (location.hash !== hash) { if (replace) history.replaceState(null, '', hash); else history.pushState(null, '', hash); }
    render();
    load(league, function () { if (state.league === league) render(); });
  }

  // ── events ───────────────────────────────────────────────────────────────
  document.addEventListener('click', function (ev) {
    var sport = ev.target.closest('.sport');
    if (sport) { go(sport.dataset.league, state.view); return; }
    var view = ev.target.closest('.view-tab');
    if (view) { go(state.league, view.dataset.view); return; }
    var jump = ev.target.closest('[data-jump]');
    if (jump) {
      var parts = jump.dataset.jump.split('|');
      var row = document.querySelector('.game[data-id="' + parts[1] + '"]');
      if (row) { row.scrollIntoView({ behavior: 'smooth', block: 'center' }); if (row.dataset.open !== '1') row.querySelector('.game-head').click(); }
      return;
    }
    var toggle = ev.target.closest('.filter-toggle');
    if (toggle) {
      state.filtersOpen = !state.filtersOpen;
      toggle.closest('.toolbar').classList.toggle('open', state.filtersOpen);
      toggle.setAttribute('aria-expanded', String(state.filtersOpen));
      return;
    }
    var chip = ev.target.closest('.chip');
    if (chip) {
      var scope = state.view;
      state.filters[scope] = state.filters[scope] || defaults(scope);
      state.filters[scope][chip.dataset.filter] = chip.dataset.value;
      render(); return;
    }
    var head = ev.target.closest('.game-head');
    if (head) {
      var game = head.closest('.game'), detail = game.querySelector('.detail');
      var open = game.dataset.open === '1';
      if (!open && !detail.dataset.built) {
        var g = findGame(game.dataset.league, game.dataset.id);
        if (g) { detail.innerHTML = buildDetail(g, DATA[game.dataset.league]); detail.dataset.built = '1'; }
      }
      game.dataset.open = open ? '0' : '1';
      detail.hidden = open;
      head.setAttribute('aria-expanded', String(!open));
      if (!open) maybeRefreshBox(game); else clearTimeout(boxTimers[game.dataset.id]);
      return;
    }
    var dtab = ev.target.closest('.dtab');
    if (dtab) {
      var wrap = dtab.closest('.detail');
      wrap.querySelectorAll('.dtab').forEach(function (b) { b.setAttribute('aria-selected', String(b === dtab)); });
      wrap.querySelectorAll('.panel').forEach(function (p) { p.hidden = p.dataset.dpanel !== dtab.dataset.dtab; });
      return;
    }
    var ph = ev.target.closest('.player-head');
    if (ph) {
      var list = ph.nextElementSibling, isOpen = !list.hidden;
      list.hidden = isOpen;
      ph.setAttribute('aria-expanded', String(!isOpen));
      ph.querySelector('.chev').style.transform = isOpen ? '' : 'rotate(180deg)';
    }
  });
  var searchTimer = null;
  document.addEventListener('input', function (ev) {
    if (ev.target.id === 'search') {
      clearTimeout(searchTimer);
      var v = ev.target.value;
      searchTimer = setTimeout(function () {
        state.search = v; render();
        var box = el('search'); if (box) { box.focus(); box.setSelectionRange(v.length, v.length); }
      }, 180);
    }
  });
  document.addEventListener('change', function (ev) {
    if (ev.target.id === 'cat-filter') {
      state.filters.props = state.filters.props || { conf: 'all', pick: 'all', cat: 'all', sort: 'edge' };
      state.filters.props.cat = ev.target.value; render();
    }
  });
  window.addEventListener('popstate', function () { fromHash(true); });

  // ── live scores ──────────────────────────────────────────────────────────
  function startLive() { stopLive(); if (document.querySelector('.game')) poll(); }
  function stopLive() { clearTimeout(state.liveTimer); state.liveTimer = null; }
  function poll() {
    var stamp = new Date().toISOString().slice(0, 10).replace(/-/g, '');
    var leagues = leaguesInView().filter(function (k) { return document.querySelector('.game[data-league="' + k + '"]'); });
    var left = leagues.length, liveTotal = 0;
    if (!left) return;
    leagues.forEach(function (k) {
      espnFetch(DATA[k].espn_path + '/scoreboard?dates=' + stamp)
        .then(function (json) { liveTotal += applyLive(json, k); })
        .catch(function () {})
        .then(function () {
          if (--left === 0) {
            var pill = el('live-pill');
            var clock = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
            if (pill) { pill.textContent = liveTotal ? '● ' + liveTotal + ' live · ' + clock : 'Updated ' + clock; pill.className = 'live-pill' + (liveTotal ? ' on' : ''); }
            state.liveTimer = setTimeout(poll, liveTotal ? 30000 : 300000);
          }
        });
    });
  }
  function applyLive(json, league) {
    var liveCount = 0;
    ((json && json.events) || []).forEach(function (ev) {
      var row = document.querySelector('.game[data-league="' + league + '"][data-id="' + ev.id + '"]');
      if (!row) return;
      var comp = (ev.competitions || [])[0]; if (!comp) return;
      var away = (comp.competitors || []).filter(function (c) { return c.homeAway === 'away'; })[0] || {};
      var home = (comp.competitors || []).filter(function (c) { return c.homeAway === 'home'; })[0] || {};
      var type = ((comp.status || {}).type || {});
      var isLive = type.name === 'STATUS_IN_PROGRESS' || type.name === 'STATUS_HALFTIME';
      var isFinal = type.name === 'STATUS_FINAL' || type.completed;
      var center = row.querySelector('.center'); if (!center) return;
      if (isLive) {
        liveCount++;
        var html = '<span class="score live">' + (away.score || 0) + '–' + (home.score || 0) + '</span><span class="gstatus">● ' + esc(type.shortDetail || 'LIVE') + '</span>';
        if (center.innerHTML !== html) {
          center.innerHTML = html; row.classList.add('live', 'flash');
          setTimeout(function () { row.classList.remove('flash'); }, 700);
          if (row.dataset.open === '1' && !boxTimers[ev.id]) maybeRefreshBox(row);
        }
      } else if (isFinal) {
        var fin = '<span class="score">' + (away.score || 0) + '–' + (home.score || 0) + '</span><span class="slabel">FINAL</span>';
        if (center.innerHTML !== fin) {
          center.innerHTML = fin; row.classList.remove('live');
          gradeRow(row, Number(away.score), Number(home.score));
          if (row.dataset.open === '1') maybeRefreshBox(row);
        }
      } else if (ev.date && center.dataset.time) {
        var t = new Date(ev.date).toLocaleTimeString('en-US', { timeZone: 'America/New_York', hour: 'numeric', minute: '2-digit' });
        var span = center.querySelector('.gtime'); if (span) span.textContent = t + ' ET';
      }
    });
    return liveCount;
  }

  // ── live box score → prop progress ────────────────────────────────────────
  // While a game is in progress and its props are open, each prop row shows
  // the player's actual number so far next to the line it was priced against.
  var BOX = {
    baseball: {
      batting: { ab: 'ab', r: 'runs', h: 'hits', rbi: 'rbi', hr: 'hr', bb: 'bb', k: 'so', sb: 'sb',
                 '2b': 'doubles', '3b': 'triples', tb: 'tb' },
      pitching: { ip: 'ip', h: 'p_h', er: 'p_er', bb: 'p_bb', k: 'p_so' }
    },
    basketball: { '': { min: 'min', pts: 'pts', reb: 'reb', ast: 'ast', stl: 'stl', blk: 'blk',
                        '3pt': 'fg3', to: 'tov' } },
    football: {
      passing: { 'c/att': 'pass_cmp', yds: 'pass_yds', td: 'pass_td', int: 'pass_int' },
      rushing: { car: 'rush_att', yds: 'rush_yds', td: 'rush_td' },
      receiving: { rec: 'rec', yds: 'rec_yds', td: 'rec_td', tgts: 'targets' },
      defensive: { tot: 'tackles', sacks: 'sacks' }
    },
    hockey: {
      skaters: { g: 'goals', a: 'assists', pts: 'points', s: 'sog', sog: 'sog', bs: 'blocks' },
      goalies: { sa: 'shots_against', ga: 'ga', sv: 'saves' }
    }
  };

  function boxValue(raw) {
    var t = String(raw == null ? '' : raw).trim();
    if (!t || t === '--') return null;
    if (t.indexOf(':') > 0) { var mm = t.split(':'); return +mm[0] + (+mm[1] || 0) / 60; }
    if (t.indexOf('/') > 0) t = t.split('/')[0];
    else if (t.indexOf('-') > 0) t = t.split('-')[0];
    var n = parseFloat(t);
    return isNaN(n) ? null : n;
  }

  function parseBox(summary, sport) {
    var table = BOX[sport] || {};
    var out = {};
    (((summary || {}).boxscore || {}).players || []).forEach(function (side) {
      (side.statistics || []).forEach(function (group) {
        var cat = String(group.name || group.type || '').toLowerCase();
        var cols = table[cat];
        if (!cols) {
          if (sport === 'hockey' && cat !== 'goalies') cols = table.skaters;
          else if (sport === 'basketball') cols = table[''];
          else return;
        }
        var labels = (group.labels || group.names || []).map(function (x) { return String(x).toLowerCase(); });
        (group.athletes || []).forEach(function (a) {
          var id = String(((a.athlete || {}).id) || '');
          if (!id) return;
          var st = out[id] = out[id] || {};
          (a.stats || []).forEach(function (raw, i) {
            var key = cols[labels[i]];
            if (!key || st[key] != null) return;
            var v = boxValue(raw);
            if (v != null) st[key] = v;
          });
        });
      });
    });
    Object.keys(out).forEach(function (id) {
      var st = out[id];
      if (sport === 'baseball') {
        if (st.tb == null && st.hits != null) {
          var singles = st.hits - (st.doubles || 0) - (st.triples || 0) - (st.hr || 0);
          st.tb = Math.max(singles, 0) + 2 * (st.doubles || 0) + 3 * (st.triples || 0) + 4 * (st.hr || 0);
        }
        if (st.ip != null) { var w = Math.floor(st.ip); st.outs = w * 3 + Math.round((st.ip - w) * 10); }
      } else if (sport === 'basketball' && st.pts != null) {
        st.pra = st.pts + (st.reb || 0) + (st.ast || 0);
        st.pr = st.pts + (st.reb || 0);
        st.pa = st.pts + (st.ast || 0);
        st.stlblk = (st.stl || 0) + (st.blk || 0);
      } else if (sport === 'football') {
        st.scrim_yds = (st.rush_yds || 0) + (st.rec_yds || 0);
        st.td = (st.rush_td || 0) + (st.rec_td || 0);
      } else if (sport === 'hockey' && st.points == null) {
        st.points = (st.goals || 0) + (st.assists || 0);
      }
    });
    return out;
  }

  var boxTimers = {};

  function refreshBox(gameEl) {
    var d = DATA[gameEl && gameEl.dataset.league];
    if (!d || !gameEl) return;
    var id = gameEl.dataset.id;
    var sport = (d.espn_path || '').split('/')[0];
    espnFetch(d.espn_path + '/summary?event=' + id)
      .then(function (summary) {
        if (!summary) return;
        var box = parseBox(summary, sport);
        var status = (((summary.header || {}).competitions || [])[0] || {}).status || {};
        var final = (status.type || {}).name === 'STATUS_FINAL' || (status.type || {}).completed;
        gameEl.querySelectorAll('.player[data-athlete]').forEach(function (pl) {
          var st = box[pl.dataset.athlete];
          if (!st) return;
          pl.querySelectorAll('.prop[data-stat]').forEach(function (row) {
            var v = st[row.dataset.stat];
            if (v == null) return;
            var chip = row.querySelector('.prop-live');
            var line = parseFloat(row.dataset.line);
            var over = v > line;
            chip.hidden = false;
            chip.className = 'prop-live ' + (final ? (over ? 'ok' : 'no') : 'live');
            chip.textContent = (final ? 'Final: ' : 'Now: ') + (Math.round(v * 10) / 10) +
              (final ? (over ? ' ✓ over' : ' ✓ under') : '');
            var range = row.querySelector('.prop-range');
            if (range) {
              var lo = +row.dataset.lo, hi = +row.dataset.hi;
              var span = Math.max(hi - lo, 1e-6), pad = span * 0.35;
              var min = lo - pad, max = hi + pad;
              var x = Math.max(0, Math.min(100, ((v - min) / (max - min)) * 100));
              var dot = range.querySelector('.live-dot') || document.createElement('b');
              dot.className = 'live-dot';
              dot.style.left = x.toFixed(1) + '%';
              if (!dot.parentNode) range.appendChild(dot);
            }
          });
        });
        if (!final && gameEl.dataset.open === '1') {
          clearTimeout(boxTimers[id]);
          boxTimers[id] = setTimeout(function () { refreshBox(gameEl); }, 30000);
        }
      })
      .catch(function () { /* feed unreachable: leave the static projection */ });
  }

  function maybeRefreshBox(gameEl) {
    var isLive = gameEl.classList.contains('live') || gameEl.dataset.status === 'final';
    var center = gameEl.querySelector('.center');
    var hasScore = center && center.querySelector('.score');
    if ((isLive || hasScore) && gameEl.querySelector('.player[data-athlete]')) refreshBox(gameEl);
  }

  // The pick was frozen before kickoff, so the moment a final score lands we
  // can say whether it was right without waiting for the next rebuild.
  function gradeRow(row, awayScore, homeScore) {
    if (!isFinite(awayScore) || !isFinite(homeScore) || awayScore === homeScore) return;
    var meta = row.querySelector('.metarow');
    if (!meta || meta.querySelector('.tag.ok, .tag.no')) return;
    var winnerSide = awayScore > homeScore ? 'away' : 'home';
    var ok = winnerSide === row.dataset.side;
    var tag = document.createElement('span');
    tag.className = 'tag ' + (ok ? 'ok' : 'no');
    tag.textContent = ok ? '✓' : '✗';
    meta.appendChild(tag);
    row.dataset.result = ok ? 'correct' : 'wrong';
  }

  // ── boot ─────────────────────────────────────────────────────────────────
  function fromHash(replace) {
    var parts = (location.hash || '').replace('#', '').split('/');
    var known = ['all'].concat(LEAGUES);
    var league = known.indexOf(parts[0]) >= 0 ? parts[0] : (window.SP_DEFAULT_LEAGUE || 'all');
    var viewNames = VIEWS.map(function (v) { return v[0]; });
    var view = viewNames.indexOf(parts[1]) >= 0 ? parts[1] : 'today';
    go(league, view, replace !== false);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', function () { fromHash(true); });
  else fromHash(true);
})();
