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
    ['record', 'Track Record', 'Record', '🏆']
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
  function today() {
    try {
      // en-CA formats as YYYY-MM-DD; the leagues run on US Eastern time.
      return new Date().toLocaleDateString('en-CA', { timeZone: 'America/New_York' });
    } catch (e) { return new Date().toISOString().slice(0, 10); }
  }
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
  // What the optimiser has been doing, in words: the settings in use, how
  // many alternatives it has tried, and the last change it kept.
  function selfTuning(d, tuning) {
    var m = d.model || {}, st = m.settings || {}, o = m.optimizer || {};
    var out = '<div class="section-title">How the model tunes itself</div><div class="cards">';
    var lastMoves = o.last_change && o.last_change.moves ? o.last_change.moves.map(function (x) { return x.move; }).join(', ') : '';
    out += card('Settings tried', o.tried || 0, 'over ' + (o.runs || 0) + ' hourly checks');
    out += card('Changes kept', o.runs_with_a_find || 0,
      o.last_change ? 'last: ' + esc((o.last_change.at || '').slice(0, 10)) + ' · ' + esc(lastMoves) : 'none yet');
    out += card('Random probes', o.probes || 0, (o.probe_wins || 0) + ' beat the current settings');
    out += '</div>';
    if (st.form_window) {
      out += '<p style="color:var(--muted)">Game picks currently weigh a team\'s last <b>' + st.form_window + '</b> games, each one back counting ' +
        '<b>' + Math.round((st.form_decay || 1) * 100) + '%</b> as much as the one after it, using ' +
        '<b>' + ((st.signals || []).length) + '</b> signals: ' + esc((st.signals || []).join(', ')) + '. ' +
        'Every hour the site tries nearby settings (a shorter or longer memory, one signal more or fewer, a faster or slower ' +
        'rating) plus one random combination, replays the season with each, and keeps a change only when it would have ' +
        'predicted past games better.</p>';
    }
    var cal = tuning._calibration, mk = tuning._market;
    var bits = [];
    if (cal) bits.push('Prop probabilities are recalibrated on ' + cal.n + ' graded props (held-out error ' + cal.holdout_before.toFixed(3) + ' → ' + cal.holdout_after.toFixed(3) + ').');
    else bits.push('Prop probabilities will be recalibrated once 150 props with outcomes are on file and the recalibration proves itself on held-out results.');
    if (mk) bits.push('Our own number now gets half the weight against the sportsbook once a player has ' + mk.k + ' games (learned from ' + mk.n + ' graded props; the default was ' + mk.default + ').');
    out += '<p style="color:var(--muted)">' + bits.join(' ') + '</p>';
    return out;
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
  function todayOf(d) { return today() || (d && d.today); }

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
  var RECORDS = {};
  function loadRecords(leagues, done) {
    var pending = leagues.filter(function (k) { return !RECORDS[k]; });
    if (!pending.length) return done();
    var left = pending.length;
    pending.forEach(function (k) {
      loadScript(k + ':records', 'data/' + k + '-records.js', function () {
        RECORDS[k] = (window.SP_RECORDS || {})[k] || { teams: {}, players: {} }; if (--left === 0) done();
      });
    });
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
          '<span class="em">' + EMOJI[k] + '</span>' + LABEL[k] + '<span class="sc" id="sc-' + k + '"></span></button>';
      }).join('');
      tabs.dataset.built = '1';
    }
    // Today's game count on each sport, so the eye goes where the action is.
    var t = today(), total = 0;
    LEAGUES.forEach(function (k) {
      var n = ((DATA[k] || {}).games || []).filter(function (g) { return g.date === t && !g.preseason; }).length;
      total += n;
      var badge = el('sc-' + k); if (badge) badge.textContent = n ? String(n) : '';
    });
    var allBadge = el('sc-all'); if (allBadge) allBadge.textContent = total ? String(total) : '';
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
    if (accent) accent.textContent = ':root{--accent:' + (d ? d.accent : '#7c83ff') + '}';
    var sub = el('brand-sub');
    if (sub) {
      if (d) {
        var a = d.accuracy || {}, v = a.verified || {};
        var tail = v.total ? ' · ' + v.correct + ' of ' + v.total + ' game picks correct' : '';
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
  function countProps(p, pendingToo) {
    var n = 0;
    ['away', 'home'].forEach(function (s) { (p[s] || []).forEach(function (pl) {
      (pl.props || []).forEach(function (x) { if (pendingToo || !x.pending) n++; });
    }); });
    return n;
  }
  function propTally(g) {
    // Graded props for this game: from the ledger tally (any age) or the board.
    if (g.props_tally && g.props_tally.n) return g.props_tally;
    if (!g.props) return null;
    var t = { n: 0, hit: 0, push: 0 };
    ['away', 'home'].forEach(function (s) { (g.props[s] || []).forEach(function (pl) {
      (pl.props || []).forEach(function (x) {
        if (x.push) t.push++;
        else if (x.hit != null) { t.n++; if (x.hit) t.hit++; }
      });
    }); });
    return t.n || t.push ? t : null;
  }

  function gameRow(g, league) {
    var homePick = g.favored === g.home;
    var awayCls = g.final ? (g.winner === g.away ? 'won' : 'lost') : (homePick ? '' : 'pick');
    var homeCls = g.final ? (g.winner === g.home ? 'won' : 'lost') : (homePick ? 'pick' : '');
    var result = g.correct == null ? '' :
      '<span class="tag ' + (g.correct ? 'ok' : 'no') + '" title="Our pick was ' + (g.correct ? 'correct' : 'incorrect') + '">' +
        (g.correct ? '✓ CORRECT' : '✗ INCORRECT') + '</span>';
    var propCount = g.props ? countProps(g.props) : 0;
    var pendingCount = g.props ? countProps(g.props, true) - propCount : 0;
    var tally = propTally(g);
    var propTag = tally
      ? '<span class="tag ' + (tally.hit >= tally.n / 2 ? 'ok' : 'no') + '" title="Player props graded correct">PROPS ' + tally.hit + '/' + tally.n + '</span>'
      : (propCount ? '<span class="tag props">' + propCount + ' PROPS</span>'
        : (pendingCount ? '<span class="tag low" title="Waiting on sportsbook lines">PROPS · NO LINES YET</span>' : ''));
    var flag = g.preseason ? '<span class="tag low">PRESEASON</span>'
      : (g.replay ? '<span class="tag low" title="Replayed: this pick was generated after the fact from the model as it stood before kickoff, using only earlier results">REPLAY</span>' : '')
      || (g.preseason ? '<span class="tag low">PRESEASON</span>'
      : (g.final && !g.counted && state.showCountBadge ? '<span class="tag low" title="This pick was made after the game had started, so it does not count toward the record">LATE PICK</span>' : ''));
    var lock = g.locked ? '<span class="lock" title="Locked at first pitch">🔒</span>' : '';
    var confirming = g.provisional ? '<span class="tag low" title="Graded on this page from the live feed the moment the game ended; the site\'s next refresh writes the same result to the record">CONFIRMING</span>' : '';
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
            '<span class="tag ' + g.conf + '" title="' + confWord(g.conf) + ' confidence">' + g.conf.toUpperCase() + '</span>' + flag +
            propTag + result + confirming +
          '</span></span>' +
        '<span class="chev" aria-hidden="true">▾</span>' +
      '</button><div class="detail" hidden></div></article>';
  }

  // ── game detail ──────────────────────────────────────────────────────────
  function buildDetail(g, d) {
    var tabs = [['matchup', 'Matchup']];
    var hasProps = g.props && countProps(g.props, true) > 0;
    if (hasProps) {
      tabs.push(['away', (g.away_s || g.away) + ' props']);
      tabs.push(['home', (g.home_s || g.home) + ' props']);
    }
    var head = '<div class="dtabs" role="tablist">' + tabs.map(function (t, i) {
      return '<button class="dtab" role="tab" data-dtab="' + t[0] + '" aria-selected="' + (i === 0) + '">' + esc(t[1]) + '</button>';
    }).join('') + '</div>';
    var body = '<div class="panel" data-dpanel="matchup">' + matchupPanel(g, d) + '</div>';
    if (hasProps) {
      body += '<div class="panel" data-dpanel="away" hidden>' + playersPanel(g, 'away', d.league) + '</div>';
      body += '<div class="panel" data-dpanel="home" hidden>' + playersPanel(g, 'home', d.league) + '</div>';
    }
    return head + body;
  }

  function matchupPanel(g, d) {
    var c = g.components || {};
    var out = '<div class="cards">' +
      card('Our pick', nameSpans(g.favored, g.favored === g.home ? (g.home_s || g.home) : (g.away_s || g.away)),
           pct(g.pick_prob, 1) + ' chance · ' + confWord(g.conf) + ' confidence' + (g.locked ? ' · locked' : '')) +
      card('Team strength', pct(c.elo, 1), 'home win chance by ratings') +
      (c.model != null ? card('Recent form', pct(c.model, 1), 'home win chance by form and rest') : '') +
      (c.standings != null ? card('Standings', pct(c.standings, 1), 'home win chance by season record') : '') + '</div>';

    if (g.replay) {
      out += '<div class="note">↺ <b>Replayed pick.</b> This game finished before the site was watching, so the pick was generated afterwards ' +
        'from the model exactly as it stood before kickoff: last season\'s results and ratings only, nothing from after the game.' +
        (g.correct != null ? ' It was <b>' + (g.correct ? 'correct' : 'incorrect') + '</b>.' : '') + '</div>';
    } else if (g.locked) {
      out += '<div class="note">🔒 This pick was locked when the game started' +
        (g.correct != null ? ' and it was <b>' + (g.correct ? 'correct' : 'incorrect') + '</b>' : '') + '.</div>';
    }
    if (g.drivers && g.drivers.length) {
      var labels = (d || {}).feature_labels || {};
      var max = Math.max.apply(null, g.drivers.map(function (x) { return Math.abs(x[1]); })) || 1;
      out += '<div><div class="section-title">Why this pick</div><div class="drivers">' +
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
      out += '<div class="note">The probable starting pitchers tilt this game toward ' +
        esc(g.starter_edge > 0 ? (g.home_s || g.home) : (g.away_s || g.away)) + '.</div>';
    }
    var ctx = g.context || {};
    out += '<div><div class="section-title">Form &amp; situation</div><div class="scroll-x"><table class="grid">' +
      '<thead><tr><th class="num">' + nameSpans(g.away, g.away_s) + '</th><th class="mid">&nbsp;</th><th>' + nameSpans(g.home, g.home_s) + '</th></tr></thead><tbody>' +
      ctxRow('Strength rating', ctx.away_elo, ctx.home_elo, false) +
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
    var list = (props || []).filter(function (p) { return !p.pending; });
    if (!list.length) return null;
    var popular = list.filter(function (p) { return (p.rank || 99) <= 3; });
    var pool = popular.length ? popular : list;
    var moved = pool.filter(function (p) { return edgeScore(p) >= 1.5; });
    if (moved.length) return moved.slice().sort(function (a, b) { return edgeScore(b) - edgeScore(a); })[0];
    return pool.slice().sort(function (a, b) { return b.pick_prob - a.pick_prob; })[0];
  }

  function playersPanel(g, side, league) {
    var players = (g.props && g.props[side]) || [];
    if (!players.length) return '<div class="empty"><span class="icon">👤</span>No player projections for this side.</div>';
    var locked = g.props_locked ? '<div class="note">🔒 Locked at first pitch. Each prop is checked against the box score and marked ' +
      '<b>correct</b> or <b>incorrect</b>; no line, projection or probability changes once the game starts.</div>' : '';
    var blanks = 0;
    players.forEach(function (p) { (p.props || []).forEach(function (x) { if (x.pending) blanks++; }); });
    if (blanks && !g.props_locked) {
      locked += '<div class="note">' + blanks + ' prop' + (blanks === 1 ? '' : 's') + ' still waiting on a sportsbook line. ' +
        'The projection is ours; the line, lean and probability fill in automatically once a book posts one.</div>';
    }
    return locked + '<div class="players">' + players.map(function (p, i) {
      var best = headlineProp(p.props);
      var hits = (p.props || []).filter(function (x) { return x.hit != null; });
      var nHit = hits.filter(function (x) { return x.hit; }).length;
      var record = hits.length ? '<span class="tag ' + (nHit >= hits.length / 2 ? 'ok' : 'no') + '" title="Props predicted correctly">' +
        nHit + ' of ' + hits.length + ' correct</span>' : '';
      var shot = p.headshot ? '<img class="pshot" src="' + esc(p.headshot) + '" alt="" loading="lazy" decoding="async">' : '<span class="pshot" aria-hidden="true"></span>';
      return '<div class="player" data-player="' + i + '" data-athlete="' + esc(p.id || '') + '">' +
        '<button class="player-head" aria-expanded="false">' + shot +
          '<span class="pinfo"><span class="pname">' + esc(p.name) +
            (p.status ? ' <span class="tag med" title="' + esc(p.status_detail || '') + '">' + esc(p.status).toUpperCase() + '</span>' : '') + '</span>' +
          '<span class="pmeta">' + [p.pos, p.role, p.gp ? p.gp + ' GP' : ''].filter(Boolean).map(esc).join(' · ') + '</span></span>' +
          '<span class="pspacer"></span><span class="tag live-tag plive" hidden></span>' + record +
          (best && !hits.length ? '<span class="pbest"><b>' + esc(best.label) + '</b> ' + best.pick.toUpperCase() + ' ' + best.line + '<br>' + pct(best.pick_prob) + '</span>' : '') +
          '<span class="chev" aria-hidden="true">▾</span></button>' +
        '<div class="prop-list" hidden>' + (p.props || []).map(function (x) { return propRow(x, { league: league, game: g.id, athlete: p.id }); }).join('') + '</div></div>';
    }).join('') + '</div>';
  }

  // How strong a lean is. Against a book price it is our probability minus
  // the book's implied probability, in points; without one, the distance the
  // matchup moved the player off his baseline, scaled to compare.
  function edgeScore(p) {
    if (p.edge_pts != null) return Math.abs(p.edge_pts);
    if (p.line_source === 'book') return 0;   // a one-sided alternate line: nothing to disagree with
    return Math.abs(p.edge || 0) * 10;
  }
  function edgeText(p) {
    if (p.edge_pts != null) return (p.edge_pts > 0 ? '+' : '') + p.edge_pts.toFixed(1) + ' pts';
    if (p.edge == null) return '—';
    return Math.abs(p.edge) < 0.05 ? 'about usual' : (p.edge > 0 ? 'above usual' : 'below usual');
  }
  function edgeClass(p) {
    var v = p.edge_pts != null ? p.edge_pts : (p.edge || 0) * 10;
    return v > 2 ? 'better' : (v < -2 ? 'worse' : '');
  }

  function lineSource(p) {
    if (p.line_source === 'book') {
      var who = p.book || 'book';
      return '<span class="src book" title="Sportsbook consensus line' + (p.books > 1 ? ' (median of ' + p.books + ' books)' : '') + '">' + esc(who) + '</span>';
    }
    if (p.line_source === 'model') return '<span class="src model" title="No sportsbook line was available, so this line is based on the player\'s season average">our line</span>';
    return '';
  }

  function propOutcome(p) {
    var unit = p.unit ? ' ' + esc(p.unit.toLowerCase()) : '';
    var vs = p.line != null ? ' (line ' + p.line + ')' : '';
    if (p.push) return '<span class="prop-live low">PUSH · had ' + p.actual + unit + vs + '</span>';
    if (p.hit != null) return '<span class="prop-live ' + (p.hit ? 'ok' : 'no') + '">' +
      (p.hit ? '✓ CORRECT' : '✗ INCORRECT') + ' · had ' + p.actual + unit + vs + '</span>';
    if (p.played === false) return '<span class="prop-live low">DID NOT PLAY</span>';
    return '';
  }

  function propRow(p, ctx) {
    var liveAttr = ctx ? ' data-live="' + esc(ctx.league + '|' + ctx.game + '|' + ctx.athlete + '|' + p.key) + '"' : '';
    var lo = p.range ? p.range[0] : p.proj, hi = p.range ? p.range[1] : p.proj;
    var span = Math.max(hi - lo, 1e-6), pad = span * 0.35, min = lo - pad, max = hi + pad;
    var toPct = function (v) { return ((v - min) / (max - min)) * 100; };
    var deltaCls = p.delta > 0.01 ? 'up' : (p.delta < -0.01 ? 'down' : '');
    var deltaTxt = p.delta == null || Math.abs(p.delta) < 0.01 ? '' : (p.delta > 0 ? '+' : '') + p.delta + ' vs season';
    var boxKey = (p.stat || '').replace(/_pg$/, '');
    var sub = '<span class="prop-sub">' + (p.season_prev ? 'last season ' : 'season ') + p.season + ' ' + esc(p.unit || '') +
        (deltaTxt ? ' · <span class="delta ' + deltaCls + '">' + deltaTxt + '</span>' : '') + '</span>';
    if (p.pending) {
      return '<div class="prop pending" data-stat="' + esc(boxKey) + '">' +
        '<span class="prop-name">' + esc(p.label) + '<span class="prop-live" hidden></span>' + sub + '</span>' +
        '<span class="prop-nums"><span class="prop-num"><span class="lbl">Book line</span><span class="blank" title="No sportsbook line posted yet">—</span></span>' +
          '<span class="prop-num"><span class="lbl">Our number</span>' + p.proj + '</span></span>' +
        '<span class="prop-range" title="Likely range ' + lo + '–' + hi + '">' +
          '<i style="left:' + toPct(lo).toFixed(1) + '%;width:' + (toPct(hi) - toPct(lo)).toFixed(1) + '%"></i></span>' +
        '<span class="prop-pick"><span class="tag low">NO LINE YET</span></span></div>';
    }
    var outcome = propOutcome(p);
    var dot = p.actual != null && !p.push ? '<b class="live-dot" style="left:' + Math.max(0, Math.min(100, toPct(p.actual))).toFixed(1) + '%"></b>' : '';
    return '<div class="prop' + (p.hit != null ? (p.hit ? ' right' : ' wrong') : '') + '" data-stat="' + esc(boxKey) + '" data-line="' + p.line +
      '" data-pick="' + p.pick + '" data-lo="' + lo + '" data-hi="' + hi + '"' + liveAttr + '>' +
      '<span class="prop-name">' + esc(p.label) + outcome + sub + '</span>' +
      '<span class="prop-nums"><span class="prop-num"><span class="lbl">' + (p.line_source === 'book' ? 'Book line' : 'Line') + '</span>' + p.line + lineSource(p) + '</span>' +
        '<span class="prop-num"><span class="lbl">Our number</span>' + p.proj + '</span></span>' +
      '<span class="prop-range" title="Likely range ' + lo + '–' + hi + '">' +
        '<i style="left:' + toPct(lo).toFixed(1) + '%;width:' + (toPct(hi) - toPct(lo)).toFixed(1) + '%"></i>' +
        '<u style="left:' + toPct(p.line).toFixed(1) + '%"></u>' + dot + '</span>' +
      '<span class="prop-pick"><span class="pickdir ' + p.pick + '">' + p.pick.toUpperCase() + '</span>' +
        '<span class="tag ' + p.conf + '">' + pct(p.pick_prob) + '</span></span>' +
      (outcome ? '' : '<span class="prop-progress-slot"></span>') + '</div>';
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
  function propDefaults() {
    return { conf: 'all', pick: 'all', cat: 'all', sort: 'edge', when: 'all', team: 'all', game: 'all', pos: 'all', edge: 'all', status: 'all' };
  }
  function activeFilterCount() {
    var f = state.filters[state.view] || {}, base = state.view === 'props' ? propDefaults() : defaults(state.view), n = 0;
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
    if (!items.length) return '<div class="empty"><span class="icon">' + (EMOJI[state.league] || '🏟') + '</span>' + emptyReason() + '</div>';
    if (!isAll()) return '<div class="games">' + items.map(function (x) { return gameRow(x.g, x.league); }).join('') + '</div>';
    // Overview: group by league so each block reads like its own board.
    var out = '';
    LEAGUES.forEach(function (k) {
      var mine = items.filter(function (x) { return x.league === k; });
      if (!mine.length) return;
      out += '<div class="section-title" id="sec-' + k + '">' + EMOJI[k] + ' ' + LABEL[k] + ' <span class="count-inline">' + mine.length + '</span></div>' +
        '<div class="games">' + mine.map(function (x) { return gameRow(x.g, k); }).join('') + '</div>';
    });
    return out;
  }
  function emptyReason() {
    if (isAll()) return 'No games match these filters.';
    var d = cur() || {}, games = d.games || [], t = today();
    if (!games.length) return 'No ' + esc(d.name || '') + ' games are on the schedule yet. Picks appear as soon as the league posts games.';
    var next = games.filter(function (g) { return g.date > t && !g.final; }).sort(function (a, b) { return a.date.localeCompare(b.date); })[0];
    if (state.view === 'today' && next) {
      return 'No ' + esc(d.name || '') + ' games today. Next up: ' + shortDate(next.date) + (next.preseason ? ' (preseason)' : '') +
        ' · <a href="#' + state.league + '/upcoming">see upcoming</a>';
    }
    return 'No games match these filters.';
  }
  function gamesView(scope) {
    var items = filteredGames(scope);
    return toolbar(scope, items.length) + gameList(items);
  }

  // ── overview strip (All · Today) ─────────────────────────────────────────
  function staleNotice() {
    var newest = null;
    leaguesInView().forEach(function (k) {
      var g = (DATA[k] || {}).generated; if (g && (!newest || g > newest)) newest = g;
    });
    if (!newest) return '';
    var age = (Date.now() - new Date(newest).getTime()) / 3600000;
    if (!(age > 3)) return '';
    return '<div class="note warn">⚠ These numbers were last refreshed ' + Math.round(age) + ' hours ago. ' +
      'The site normally refreshes every half hour; this refresh is running late.</div>';
  }

  function yesterdayRecap() {
    var y = addDays(today(), -1);
    var gc = 0, gn = 0, pc = 0, pn = 0;
    allGames('results').forEach(function (x) {
      var g = x.g;
      if (g.date !== y || !g.final) return;
      if (g.counted && g.correct != null) { gn++; gc += g.correct ? 1 : 0; }
      var t = propTally(g);
      if (t) { pn += t.n; pc += t.hit; }
    });
    if (!gn && !pn) return '';
    return '<div class="section-title">Yesterday</div><div class="cards">' +
      (gn ? card('Game picks', pct(gc / gn, 1), gc + ' of ' + gn + ' correct') : '') +
      (pn ? card('Player props', pct(pc / pn, 1), pc + ' of ' + pn + ' correct') : '') +
      card('Full record', '<a href="#' + state.league + '/record">Track Record →</a>', 'by confidence, team and player') +
      '</div>';
  }

  function topProps() {
    var t = today();
    var rows = [], seen = {};
    allGames('today').forEach(function (x) {
      var g = x.g;
      if (g.final || g.preseason || !g.props || g.props_locked) return;
      if (g.date !== t && g.date !== addDays(t, 1)) return;
      ['away', 'home'].forEach(function (side) {
        (g.props[side] || []).forEach(function (pl) {
          (pl.props || []).forEach(function (p) {
            // Only well-founded plays make the strip: a real sample behind the
            // player and more than one book behind the line.
            if (p.pending || p.edge_pts == null || p.edge_pts < 3) return;
            if ((pl.gp || 0) < 10 || (p.books || 0) < 2) return;
            rows.push({ g: g, league: x.league, pl: pl, p: p });
          });
        });
      });
    });
    rows.sort(function (a, b) { return b.p.edge_pts - a.p.edge_pts || b.p.pick_prob - a.p.pick_prob; });
    var picks = [];
    rows.forEach(function (r) { var k = r.league + r.pl.id; if (!seen[k] && picks.length < 6) { seen[k] = 1; picks.push(r); } });
    if (!picks.length) return '';
    return '<div class="section-title">Prop plays we like most</div><div class="strip">' + picks.map(function (r) {
      var g = r.g, p = r.p;
      return '<div class="pick-card" data-jump="' + r.league + '|' + esc(g.id) + '">' +
        '<div class="pc-top"><span class="lg-chip">' + EMOJI[r.league] + '</span><span class="tag ' + p.conf + '">' + pct(p.pick_prob) + '</span></div>' +
        '<div class="pc-team">' + esc(r.pl.short || r.pl.name) + '</div>' +
        '<div class="pc-sub">' + esc(p.label) + ' <b>' + p.pick.toUpperCase() + ' ' + p.line + '</b> · +' + p.edge_pts.toFixed(0) + ' pts vs book</div>' +
        '<div class="pc-sub">' + esc(g.away_s || g.away) + ' @ ' + esc(g.home_s || g.home) + ' · ' + esc(g.time || 'TBD') + '</div></div>';
    }).join('') + '</div><div class="lookup-sub" style="margin:-2px 0 10px">Edge is how much likelier we think the pick is than the sportsbook\'s price implies.</div>';
  }

  function jumpBar(scope) {
    var items = filteredGames(scope), counts = {};
    items.forEach(function (x) { counts[x.league] = (counts[x.league] || 0) + 1; });
    var ks = LEAGUES.filter(function (k) { return counts[k]; });
    if (ks.length < 2) return '';
    return '<div class="jump">' + ks.map(function (k) {
      return '<button class="chip" data-scroll="sec-' + k + '"><span class="lg-chip">' + EMOJI[k] + '</span><b>' + LABEL[k] + '</b><span class="n">' + counts[k] + '</span></button>';
    }).join('') + '</div>';
  }

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
      var label = isAll() ? EMOJI[k] + ' ' + LABEL[k] + ' game picks' : 'Game picks';
      cards += v.total
        ? card(label, pct(v.pct, 1), v.correct + ' of ' + v.total + ' correct')
        : card(label, '—', 'no games graded yet');
    });
    var pn = 0, ph = 0;
    leagues.forEach(function (k) { var pr = (DATA[k] || {}).props_record || {}; pn += pr.total || 0; ph += pr.hit || 0; });
    if (pn) cards += card('Player props', pct(ph / pn, 1), ph + ' of ' + pn + ' correct');
    var played = filteredGames('results').length;
    if (!total && !backfilled && !played) {
      return '<div class="empty"><span class="icon">' + (EMOJI[state.league] || '🏟') + '</span>No completed games yet. Results appear once games have been played.</div>';
    }
    var head = '<div class="cards">' + cards + '</div>';
    if (!isAll() && !total) {
      head += '<details class="explain"><summary>Why is the record empty?</summary><p>Only picks made <b>before</b> a game starts count. ' +
        (backfilled ? backfilled + ' finished game' + (backfilled === 1 ? ' was' : 's were') + ' added to the site after the fact, ' +
        'so there was no pick to grade. ' : '') +
        'The record fills in as games are played.</p></details>';
    }
    var scoped = filteredGames('results');
    state.showCountBadge = scoped.some(function (x) { return x.g.counted; }) && scoped.some(function (x) { return !x.g.counted; });
    var list = gamesView('results');
    return head + '<div class="note">See the <a href="#' + state.league + '/record">Track Record</a> page for the full breakdown by confidence, sport and prop type.</div>' +
      '<div class="section-title">Completed games</div>' + list;
  }

  // ── props board ──────────────────────────────────────────────────────────
  function allProps() {
    var out = [];
    leaguesInView().forEach(function (k) { var d = DATA[k]; if (!d) return; (d.games || []).forEach(function (g) {
      if (!g.props) return;
      ['away', 'home'].forEach(function (side) {
        (g.props[side] || []).forEach(function (pl) {
          (pl.props || []).forEach(function (p) {
            if (p.pending) return;
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
      var ls = d.lines_status || {};
      var why = d.props_status === 'unavailable'
        ? 'Player statistics could not be fetched on the last run. The board fills in automatically on the next refresh.'
        : (ls.mode === 'book' && !ls.games
          ? 'Waiting on sportsbook lines: the books have not posted player markets for the next games yet. Props fill in automatically once they do.'
          : 'No upcoming games with player projections right now.');
      return '<div class="empty"><span class="icon">👤</span>' + esc(why) + '</div>';
    }
    var f = state.filters.props || (state.filters.props = propDefaults());
    Object.keys(propDefaults()).forEach(function (k) { if (f[k] === undefined) f[k] = propDefaults()[k]; });
    var t = today(), tomorrow = addDays(t, 1);
    var selectOpts = function (name, label, values, current, fmt) {
      return '<select class="search" data-select-filter="' + name + '" aria-label="' + esc(label) + '">' +
        '<option value="all">' + esc(label) + '</option>' + values.map(function (v) {
          return '<option value="' + esc(v[0]) + '"' + (current === v[0] ? ' selected' : '') + '>' + esc(v[1]) + '</option>';
        }).join('') + '</select>';
    };
    var cats = {}, teams = {}, games = {}, groups = {};
    rows.forEach(function (r) {
      cats[r.prop.label] = 1; teams[r.g.away] = 1; teams[r.g.home] = 1;
      games[r.g.id] = r.g; if (r.player.group) groups[r.player.group] = 1;
    });
    var gameList = Object.keys(games).map(function (id) { return games[id]; })
      .sort(function (a, b) { return (a.date + (a.time || '')).localeCompare(b.date + (b.time || '')); });
    var GROUP_LABEL = { pitcher: 'Pitchers', batter: 'Hitters', qb: 'Quarterbacks', rb: 'Running backs', wr: 'Receivers & tight ends', goalie: 'Goalies', skater: 'Skaters' };
    var statusOf = function (r) {
      if (r.prop.hit != null || r.prop.push || r.prop.played === false) return 'graded';
      return r.g.props_locked ? 'live' : 'upcoming';
    };

    var q = state.search.trim().toLowerCase();
    // One row per player and prop: a player on a three-game series would
    // otherwise fill the board with the same read three times over, unless
    // a date or game is chosen.
    var seen = {}, dedupe = f.when === 'all' && f.game === 'all';
    var list = rows.filter(function (r) {
      if (f.conf !== 'all' && r.prop.conf !== f.conf) return false;
      if (f.pick !== 'all' && r.prop.pick !== f.pick) return false;
      if (f.cat !== 'all' && r.prop.label !== f.cat) return false;
      if (f.when === 'today' && r.g.date !== t) return false;
      if (f.when === 'tomorrow' && r.g.date !== tomorrow) return false;
      if (f.team !== 'all' && r.g.away !== f.team && r.g.home !== f.team) return false;
      if (f.game !== 'all' && r.g.id !== f.game) return false;
      if (f.pos !== 'all' && r.player.group !== f.pos) return false;
      if (f.edge !== 'all' && !(r.prop.edge_pts != null && r.prop.edge_pts >= +f.edge)) return false;
      if (f.status !== 'all' && statusOf(r) !== f.status) return false;
      if (q && (r.player.name + ' ' + r.g.away + ' ' + r.g.home).toLowerCase().indexOf(q) < 0) return false;
      if (dedupe) {
        var key = r.player.id + '|' + r.player.name + '|' + r.prop.key;
        if (seen[key] && seen[key] <= r.g.date) return false;
        seen[key] = r.g.date;
      }
      return true;
    }).sort(function (a, b) {
      if (f.sort === 'conf') return b.prop.pick_prob - a.prop.pick_prob;
      return edgeScore(b.prop) - edgeScore(a.prop);
    });
    var totalMatched = list.length;
    list = list.slice(0, 250);

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
      '<div class="fgroup"><span class="flabel">When</span>' +
        [['all', 'All'], ['today', 'Today'], ['tomorrow', 'Tomorrow']].map(function (v) {
          return '<button class="chip" data-filter="when" data-value="' + v[0] + '" aria-pressed="' + (f.when === v[0]) + '">' + v[1] + '</button>';
        }).join('') + '</div>' +
      '<div class="fgroup"><span class="flabel">Status</span>' +
        [['all', 'All'], ['upcoming', 'Upcoming'], ['live', 'In progress'], ['graded', 'Graded']].map(function (v) {
          return '<button class="chip" data-filter="status" data-value="' + v[0] + '" aria-pressed="' + (f.status === v[0]) + '">' + v[1] + '</button>';
        }).join('') + '</div>' +
      '<div class="fgroup"><span class="flabel">Edge</span>' +
        [['all', 'Any'], ['3', '3+ pts'], ['5', '5+ pts'], ['10', '10+ pts']].map(function (v) {
          return '<button class="chip" data-filter="edge" data-value="' + v[0] + '" aria-pressed="' + (f.edge === v[0]) + '">' + v[1] + '</button>';
        }).join('') + '</div>' +
      '<div class="fgroup"><span class="flabel">Sort</span>' +
        [['edge', 'Edge vs book'], ['conf', 'Confidence']].map(function (v) {
          return '<button class="chip" data-filter="sort" data-value="' + v[0] + '" aria-pressed="' +
            (f.sort === v[0]) + '">' + v[1] + '</button>';
        }).join('') + '</div>' +
      '<div class="fgroup">' +
        selectOpts('cat', 'All props', Object.keys(cats).sort().map(function (c) { return [c, c]; }), f.cat) +
        selectOpts('pos', 'All positions', Object.keys(groups).sort().map(function (g) { return [g, GROUP_LABEL[g] || g]; }), f.pos) +
        selectOpts('team', 'All teams', Object.keys(teams).sort().map(function (x) { return [x, x]; }), f.team) +
        selectOpts('game', 'All games', gameList.map(function (g) { return [g.id, shortDate(g.date) + ' · ' + (g.away_s || g.away) + ' @ ' + (g.home_s || g.home)]; }), f.game) +
      '</div>' +
      '<div class="fgroup"><input class="search" id="search" type="search" placeholder="Player or team…" value="' +
        esc(state.search) + '" aria-label="Filter players">' +
        (activeFilterCount() ? '<button class="chip" data-clear-filters>✕ Clear filters</button>' : '') + '</div>',
      (totalMatched > list.length ? 'top ' + list.length + ' of ' + totalMatched : totalMatched) + ' props');

    // A result column once anything on the board is graded or in progress:
    // graded rows show the verdict, live rows show the number so far.
    var graded = list.some(function (r) { return r.prop.hit != null || r.prop.push || r.prop.played === false || r.g.props_locked; });
    var body = '<div class="scroll-x"><table class="grid"><thead><tr>' +
      '<th>Player</th><th>Prop</th><th class="num">Book line</th><th class="num">Our number</th>' +
      '<th class="num">Edge</th><th>Our pick</th><th class="num">Chance</th>' + (graded ? '<th>Result</th>' : '') + '<th>Game</th></tr></thead><tbody>' +
      list.map(function (r) {
        var p = r.prop;
        var res = p.push ? '<span class="tag low">PUSH · had ' + p.actual + '</span>'
          : (p.hit != null ? '<span class="tag ' + (p.hit ? 'ok' : 'no') + '">' + (p.hit ? '✓ correct' : '✗ wrong') + ' · had ' + p.actual + '</span>'
          : (p.played === false ? '<span class="tag low">DID NOT PLAY</span>' : '<span class="prop-progress-slot"></span>'));
        return '<tr data-live="' + esc(r.league + '|' + r.g.id + '|' + r.player.id + '|' + p.key) + '"><td><b>' + esc(r.player.name) + '</b><br><span style="color:var(--faint)">' +
            esc(r.player.pos || '') + '</span></td>' +
          '<td>' + (isAll() ? '<span class="lg-chip">' + EMOJI[r.league] + '</span> ' : '') + esc(p.label) + '</td>' +
          '<td class="num">' + p.line + '<br>' + lineSource(p) + '</td>' +
          '<td class="num">' + p.proj + '</td>' +
          '<td class="num ' + edgeClass(p) + '"' + (p.book_p != null ? ' title="The book\'s own price says ' + pct(p.pick === 'over' ? p.book_p : 1 - p.book_p) + ' for this side"' : '') + '>' +
            edgeText(p) + '</td>' +
          '<td><span class="pickdir ' + p.pick + '">' + p.pick.toUpperCase() + '</span></td>' +
          '<td class="num"><span class="tag ' + p.conf + '">' + pct(p.pick_prob) + '</span></td>' +
          (graded ? '<td>' + res + '</td>' : '') +
          '<td style="color:var(--muted)">' + esc(r.g.away) + ' @ ' + esc(r.g.home) +
            '<br><span style="color:var(--faint)">' + shortDate(r.g.date) + '</span></td></tr>';
      }).join('') + '</tbody></table></div>';

    var bookMode = leaguesInView().some(function (k) { return ((DATA[k] || {}).lines_status || {}).mode === 'book'; });
    var note = bookMode
      ? '<div class="note"><b>Book line</b> is the sportsbooks\' number. <b>Our number</b> is what this site expects ' +
        'the player to do tonight, based on his season, last season, tonight\'s opponent and the injury report. ' +
        '<b>Our pick</b> is the side our number lands on, <b>Chance</b> is how likely we think that side is, and ' +
        '<b>Edge</b> is how much likelier we think it is than the book does, in percentage points. ' +
        'A prop with no book line yet stays blank until a book posts one.</div>'
      : '<div class="note">No sportsbook lines are connected, so the <b>Line</b> shown is based on each player\'s ' +
        'season average and <b>Our number</b> is what we expect tonight given the matchup.</div>';
    return note + bar + body;
  }


  // ── model view ───────────────────────────────────────────────────────────
  function confWord(c) { return c === 'high' ? 'high' : (c === 'med' ? 'medium' : 'low'); }

  // Sum a daily series over the last N days.
  function windowOf(curve, days) {
    var cutoff = new Date(Date.now() - days * 86400000).toISOString().slice(0, 10);
    var c = 0, n = 0;
    (curve || []).forEach(function (p) { if (p.date >= cutoff) { c += p.correct; n += p.n; } });
    return { correct: c, n: n, pct: n ? c / n : null };
  }
  function recordCard(label, correct, n, note) {
    return card(label, n ? pct(correct / n, 1) : '—', n ? correct + ' of ' + n + ' correct' + (note ? ' · ' + note : '') : (note || 'nothing graded yet'));
  }
  function tierRows(tiers, unit) {
    return '<div class="scroll-x"><table class="grid"><thead><tr><th>When we said</th><th class="num">' + unit + '</th>' +
      '<th class="num">Correct</th><th class="num">Hit rate</th></tr></thead><tbody>' +
      [['high', 'High confidence'], ['med', 'Medium confidence'], ['low', 'Low confidence']].map(function (t) {
        var b = tiers[t[0]] || {}; var n = b.n || 0, ok = b.ok != null ? b.ok : (b.hit || 0);
        return '<tr><td>' + t[1] + '</td><td class="num">' + n + '</td><td class="num">' + ok + '</td>' +
          '<td class="num ' + (n && ok / n >= 0.55 ? 'better' : '') + '">' + (n ? pct(ok / n, 1) : '—') + '</td></tr>';
      }).join('') + '</tbody></table></div>';
  }

  // ── team / player lookup ─────────────────────────────────────────────────
  function lookupEntries() {
    var out = [];
    leaguesInView().forEach(function (k) {
      var r = RECORDS[k] || {};
      Object.keys(r.teams || {}).forEach(function (name) {
        var t = r.teams[name];
        if (t.n || t.props_n) out.push({ league: k, kind: 'team', id: name, name: name, sub: LABEL[k] });
      });
      Object.keys(r.players || {}).forEach(function (id) {
        var p = r.players[id];
        if (p.n) out.push({ league: k, kind: 'player', id: id, name: p.name, sub: (p.team ? p.team + ' · ' : '') + LABEL[k] });
      });
    });
    return out;
  }
  function lookupBox() {
    var q = state.lookupQuery || '';
    var hits = [];
    if (q.trim().length >= 2) {
      var needle = q.trim().toLowerCase();
      hits = lookupEntries().filter(function (e) { return e.name.toLowerCase().indexOf(needle) >= 0; })
        .sort(function (a, b) { return (a.kind === b.kind ? 0 : (a.kind === 'team' ? -1 : 1)) || a.name.localeCompare(b.name); })
        .slice(0, 12);
    }
    return '<div class="lookup"><input class="search" id="lookup" type="search" placeholder="Look up a team or player (e.g. Yankees, Aaron Judge)…" ' +
      'value="' + esc(q) + '" aria-label="Look up a team or player" autocomplete="off">' +
      (hits.length ? '<div class="lookup-results">' + hits.map(function (e) {
        return '<button class="lookup-item" data-lookup="' + e.league + '|' + e.kind + '|' + esc(e.id) + '">' +
          '<span>' + EMOJI[e.league] + '</span><b>' + esc(e.name) + '</b><span style="color:var(--faint)">' + esc(e.sub) + '</span>' +
          '<span class="kind">' + e.kind + '</span></button>';
      }).join('') + '</div>' : (q.trim().length >= 2 ? '<div class="lookup-results"><div class="lookup-item" style="color:var(--faint)">No graded picks for that name yet.</div></div>' : '')) +
      '</div>';
  }
  function rate(ok, n) { return n ? pct(ok / n, 1) : '—'; }
  function rateCls(ok, n) { return n >= 5 ? (ok / n >= 0.55 ? 'better' : (ok / n < 0.45 ? 'worse' : '')) : ''; }
  function byKeyTable(byKey, title) {
    var keys = Object.keys(byKey || {}).sort(function (a, b) { return byKey[b].n - byKey[a].n; });
    if (!keys.length) return '';
    return '<div class="section-title">' + esc(title) + '</div><div class="scroll-x"><table class="grid"><thead><tr>' +
      '<th>Prop</th><th class="num">Graded</th><th class="num">Correct</th><th class="num">Hit rate</th></tr></thead><tbody>' +
      keys.map(function (k) {
        var r = byKey[k];
        return '<tr><td>' + esc(r.label || k) + '</td><td class="num">' + r.n + '</td><td class="num">' + r.hit + '</td>' +
          '<td class="num ' + rateCls(r.hit, r.n) + '">' + rate(r.hit, r.n) + '</td></tr>';
      }).join('') + '</tbody></table></div>';
  }
  function lookupPanel() {
    var sel = state.lookup;
    if (!sel) return '';
    var r = RECORDS[sel.league] || {};
    var head = function (title, sub) {
      return '<div class="lookup-head"><span style="font-size:20px">' + EMOJI[sel.league] + '</span><div><h2>' + esc(title) + '</h2>' +
        '<div class="lookup-sub">' + esc(sub) + '</div></div><button class="close" data-lookup-close aria-label="Close">×</button></div>';
    };
    if (sel.kind === 'team') {
      var t = (r.teams || {})[sel.id];
      if (!t) return '';
      var out = '<div class="lookup-panel">' + head(t.name, LABEL[sel.league] + ' · track record') +
        '<div class="section-title">Game picks in ' + esc(t.name) + ' games</div><div class="cards">' +
        card('All their games', rate(t.ok, t.n), t.ok + ' of ' + t.n + ' picks correct') +
        card('When we picked them', rate(t.picked_ok, t.picked), t.picked ? t.picked_ok + ' of ' + t.picked + ' correct' : 'never yet') +
        card('When we picked against them', rate(t.faded_ok, t.faded), t.faded ? t.faded_ok + ' of ' + t.faded + ' correct' : 'never yet') +
        card('Their players\' props', rate(t.props_hit, t.props_n), t.props_n ? t.props_hit + ' of ' + t.props_n + ' correct' : 'none graded yet') + '</div>';
      if (t.recent && t.recent.length) {
        out += '<div class="section-title">Recent games</div><div class="scroll-x"><table class="grid"><thead><tr>' +
          '<th>Date</th><th>Game</th><th>Our pick</th><th>Score</th><th>Result</th></tr></thead><tbody>' +
          t.recent.map(function (x) {
            return '<tr><td>' + shortDate(x.date) + '</td><td>' + (x.home ? esc(x.opp) + ' @ ' + esc(t.name) : esc(t.name) + ' @ ' + esc(x.opp)) + '</td>' +
              '<td>' + (x.picked ? esc(t.name) : esc(x.opp)) + '</td><td>' + esc(x.score) + '</td>' +
              '<td><span class="tag ' + (x.ok ? 'ok' : 'no') + '">' + (x.ok ? '✓ correct' : '✗ wrong') + '</span></td></tr>';
          }).join('') + '</tbody></table></div>';
      }
      out += byKeyTable(t.props_by_key, 'Their players\' props by type');
      return out + '</div>';
    }
    var p = (r.players || {})[sel.id];
    if (!p) return '';
    var out2 = '<div class="lookup-panel">' + head(p.name, (p.team ? p.team + ' · ' : '') + LABEL[sel.league] + ' · prop track record') +
      '<div class="cards">' + card('All props', rate(p.hit, p.n), p.hit + ' of ' + p.n + ' correct') + '</div>' +
      byKeyTable(p.by_key, 'By prop type');
    if (p.recent && p.recent.length) {
      out2 += '<div class="section-title">Recent props</div><div class="scroll-x"><table class="grid"><thead><tr>' +
        '<th>Date</th><th>Opponent</th><th>Prop</th><th class="num">Line</th><th>Our pick</th><th class="num">Actual</th><th>Result</th></tr></thead><tbody>' +
        p.recent.map(function (x) {
          return '<tr><td>' + shortDate(x.date) + '</td><td>' + esc(x.opp || '') + '</td><td>' + esc(x.label) + '</td>' +
            '<td class="num">' + (x.line == null ? '—' : x.line) + '</td><td>' + esc((x.pick || '').toUpperCase()) + '</td>' +
            '<td class="num">' + (x.actual == null ? '—' : x.actual) + '</td>' +
            '<td><span class="tag ' + (x.hit ? 'ok' : 'no') + '">' + (x.hit ? '✓ correct' : '✗ wrong') + '</span></td></tr>';
        }).join('') + '</tbody></table></div>';
    }
    return out2 + '</div>';
  }

  function pickStreak() {
    var games = allGames('results').filter(function (x) { return x.g.final && x.g.counted && x.g.correct != null; })
      .sort(function (a, b) { return (a.g.date + (a.g.time || '')).localeCompare(b.g.date + (b.g.time || '')); });
    var n = 0, ok = null, best = 0, run = 0;
    games.forEach(function (x) {
      if (x.g.correct) { run++; if (run > best) best = run; } else run = 0;
    });
    for (var i = games.length - 1; i >= 0; i--) {
      var c = games[i].g.correct;
      if (ok == null) ok = c;
      if (c !== ok) break;
      n++;
    }
    return { n: n, ok: ok, best: best };
  }

  function replayNote() {
    var n = 0;
    allGames('results').forEach(function (x) { if (x.g.replay && x.g.final && x.g.counted) n++; });
    return n ? ' <b>' + n + '</b> pick' + (n === 1 ? '' : 's') + ' marked ↺ were replayed: games that finished before the site was ' +
      'watching, predicted afterwards by the model as it stood before kickoff, using only earlier results.' : '';
  }

  function recordView() {
    var leagues = leaguesInView();
    var out = lookupBox() + lookupPanel() + '<div class="note"><b>How this page works.</b> Every pick is locked the moment a game starts and graded ' +
      'when it ends. A game pick is correct when the team we chose wins. A player prop is correct when the player\'s ' +
      'final number lands on the side we picked; landing exactly on the line is a push and is not counted. ' +
      'Picks made after a game had already started never count. 50% is a coin flip.' + replayNote() + '</div>';

    // ── games ──
    var gC = 0, gN = 0, gTiers = { high: { n: 0, ok: 0 }, med: { n: 0, ok: 0 }, low: { n: 0, ok: 0 } }, gCurve = [];
    var perLeague = '';
    leagues.forEach(function (k) {
      var d = DATA[k] || {}, v = (d.accuracy || {}).verified || {}, b = v.buckets || {};
      gC += v.correct || 0; gN += v.total || 0;
      ['high', 'med', 'low'].forEach(function (t) { gTiers[t].n += (b[t] || {}).n || 0; gTiers[t].ok += (b[t] || {}).ok || 0; });
      gCurve = gCurve.concat(((d.model || {}).curve) || []);
      if (isAll()) perLeague += recordCard(EMOJI[k] + ' ' + LABEL[k], v.correct || 0, v.total || 0);
    });
    var w7 = windowOf(gCurve, 7), w30 = windowOf(gCurve, 30);
    var streak = pickStreak();
    out += '<div class="section-title">Game picks</div><div class="cards">' +
      recordCard('All time', gC, gN) + recordCard('Last 7 days', w7.correct, w7.n) + recordCard('Last 30 days', w30.correct, w30.n) +
      (streak.n ? card('Current streak', streak.n + ' ' + (streak.ok ? 'right' : 'wrong'), 'in a row · best run ' + streak.best + ' right') : '') +
      '</div>' + (perLeague ? '<div class="cards">' + perLeague + '</div>' : '') +
      (gN ? tierRows(gTiers, 'Games') : '');
    if (!isAll()) {
      var m = (cur() || {}).model || {};
      if (m.curve && m.curve.length > 3) out += curveSvg(m.curve, 'Game picks over time');
    }
    var teamRows = [];
    leagues.forEach(function (k) {
      var ts = (RECORDS[k] || {}).teams || {};
      Object.keys(ts).forEach(function (name) { if (ts[name].n) teamRows.push({ league: k, t: ts[name] }); });
    });
    if (teamRows.length) {
      teamRows.sort(function (a, b) { return b.t.n - a.t.n || a.t.name.localeCompare(b.t.name); });
      out += '<div><div class="section-title">By team</div><div class="scroll-x"><table class="grid"><thead><tr>' +
        '<th>Team</th><th class="num">Games</th><th class="num">Correct</th><th class="num">Hit rate</th>' +
        '<th class="num">Picked them</th><th class="num">Picked against</th></tr></thead><tbody>' +
        teamRows.map(function (x) {
          var t = x.t;
          return '<tr><td><button class="linkish" data-lookup="' + x.league + '|team|' + esc(t.name) + '">' + (isAll() ? EMOJI[x.league] + ' ' : '') + esc(t.name) + '</button></td>' +
            '<td class="num">' + t.n + '</td><td class="num">' + t.ok + '</td><td class="num ' + rateCls(t.ok, t.n) + '">' + rate(t.ok, t.n) + '</td>' +
            '<td class="num">' + (t.picked ? t.picked_ok + ' of ' + t.picked : '—') + '</td>' +
            '<td class="num">' + (t.faded ? t.faded_ok + ' of ' + t.faded : '—') + '</td></tr>';
        }).join('') + '</tbody></table></div><div class="lookup-sub" style="margin-top:6px">Tap a team for its full record, or search a player above.</div></div>';
    }

    // ── props ──
    var pC = 0, pN = 0, pTiers = { high: { n: 0, hit: 0 }, med: { n: 0, hit: 0 }, low: { n: 0, hit: 0 } }, pCurve = [], perLeagueP = '';
    var byType = {};
    leagues.forEach(function (k) {
      var pr = (DATA[k] || {}).props_record || {}, bc = pr.by_conf || {};
      pC += pr.hit || 0; pN += pr.total || 0;
      ['high', 'med', 'low'].forEach(function (t) { pTiers[t].n += (bc[t] || {}).n || 0; pTiers[t].hit += (bc[t] || {}).hit || 0; });
      pCurve = pCurve.concat(pr.curve || []);
      Object.keys(pr.by_key || {}).forEach(function (key) {
        var r = pr.by_key[key], name = LABEL[k] + ' · ' + (r.label || key);
        var slot = byType[name] || (byType[name] = { n: 0, hit: 0, league: k });
        slot.n += r.n; slot.hit += r.hit;
      });
      if (isAll()) perLeagueP += recordCard(EMOJI[k] + ' ' + LABEL[k], pr.hit || 0, pr.total || 0);
    });
    var p7 = windowOf(pCurve, 7), p30 = windowOf(pCurve, 30);
    out += '<div class="section-title">Player props</div><div class="cards">' +
      recordCard('All time', pC, pN) + recordCard('Last 7 days', p7.correct, p7.n) + recordCard('Last 30 days', p30.correct, p30.n) +
      '</div>' + (perLeagueP ? '<div class="cards">' + perLeagueP + '</div>' : '') +
      (pN ? tierRows(pTiers, 'Props') : '');
    var types = Object.keys(byType).sort(function (a, b) { return byType[b].n - byType[a].n; });
    if (types.length) {
      out += '<div><div class="section-title">By prop type</div><div class="scroll-x"><table class="grid"><thead><tr>' +
        '<th>Prop</th><th class="num">Graded</th><th class="num">Correct</th><th class="num">Hit rate</th></tr></thead><tbody>' +
        types.map(function (t) {
          var r = byType[t];
          return '<tr><td>' + (isAll() ? esc(t) : esc(t.split(' · ').slice(1).join(' · '))) + '</td><td class="num">' + r.n + '</td>' +
            '<td class="num">' + r.hit + '</td><td class="num ' + (r.n >= 10 && r.hit / r.n >= 0.55 ? 'better' : (r.n >= 10 && r.hit / r.n < 0.48 ? 'worse' : '')) + '">' +
            pct(r.hit / r.n, 1) + '</td></tr>';
        }).join('') + '</tbody></table></div></div>';
    }
    if (!isAll()) {
      var prc = ((cur() || {}).props_record || {}).curve || [];
      if (prc.length > 3) out += curveSvg(prc, 'Player props over time');
    }
    var playerRows = [];
    leagues.forEach(function (k) {
      var ps = (RECORDS[k] || {}).players || {};
      Object.keys(ps).forEach(function (id) { if (ps[id].n >= 3) playerRows.push({ league: k, p: ps[id] }); });
    });
    var best = playerRows.filter(function (x) { return x.p.n >= 5; })
      .sort(function (a, b) { return (b.p.hit / b.p.n) - (a.p.hit / a.p.n) || b.p.n - a.p.n; }).slice(0, 10);
    if (best.length) {
      out += '<div><div class="section-title">Players we call best (5+ props)</div><div class="strip">' + best.map(function (x) {
        return '<div class="pick-card" data-lookup="' + x.league + '|player|' + esc(x.p.id) + '">' +
          '<div class="pc-top"><span class="lg-chip">' + EMOJI[x.league] + '</span><span class="tag ' + (x.p.hit / x.p.n >= 0.6 ? 'high' : 'med') + '">' + pct(x.p.hit / x.p.n) + '</span></div>' +
          '<div class="pc-team">' + esc(x.p.name) + '</div><div class="pc-sub">' + x.p.hit + ' of ' + x.p.n + ' props · ' + esc(x.p.team || '') + '</div></div>';
      }).join('') + '</div></div>';
    }
    if (playerRows.length) {
      playerRows.sort(function (a, b) { return b.p.n - a.p.n || a.p.name.localeCompare(b.p.name); });
      out += '<div><div class="section-title">By player (most graded first)</div><div class="scroll-x"><table class="grid"><thead><tr>' +
        '<th>Player</th><th>Team</th><th class="num">Props</th><th class="num">Correct</th><th class="num">Hit rate</th></tr></thead><tbody>' +
        playerRows.slice(0, 40).map(function (x) {
          var p = x.p;
          return '<tr><td><button class="linkish" data-lookup="' + x.league + '|player|' + esc(p.id) + '">' + (isAll() ? EMOJI[x.league] + ' ' : '') + esc(p.name) + '</button></td>' +
            '<td style="color:var(--muted)">' + esc(p.team || '') + '</td><td class="num">' + p.n + '</td><td class="num">' + p.hit + '</td>' +
            '<td class="num ' + rateCls(p.hit, p.n) + '">' + rate(p.hit, p.n) + '</td></tr>';
        }).join('') + '</tbody></table></div><div class="lookup-sub" style="margin-top:6px">Showing players with at least three graded props. Search above for anyone else.</div></div>';
    }
    if (!gN && !pN) out += '<div class="empty"><span class="icon">🏆</span>Nothing has been graded yet. The record starts with the first finished game.</div>';

    // ── the technical part, tucked away ──
    if (!isAll()) out += technicalDetails(cur());
    return out;
  }

  function technicalDetails(d) {
    var m = d.model || {}, v = m.validation || {}, led = m.ledger || {}, trained = m.stage === 'trained';
    var out = '<details class="explain"><summary>Under the hood (for the curious)</summary><div class="row-gap" style="padding-bottom:12px">' +
      '<p>Game picks blend three views of each matchup: a strength rating that updates after every game, a recent-form model ' +
      'that also weighs rest and home/road splits, and the season standings. Each hour the site refits on every game it has ' +
      'seen and keeps the new settings only if they would have predicted past games better. Player props start from a ' +
      'player\'s season and last-season rates, adjust for the opponent, expected score, home/away and injuries, and are ' +
      'checked against the sportsbook line. Every graded prop feeds back a correction for its prop type.</p>' +
      '<div class="cards">' +
      (trained ? card('Historical test accuracy', pct(v.acc, 1), 'model replayed over ' + (v.n || 0) + ' past games')
               : card('Historical test accuracy', 'Warming up', 'needs ' + (m.min_train || 110) + ' games')) +
      card('Games learned from', m.n_train || 0, (m.archive || 0) + ' in the archive') +
      card('Picks graded', led.graded || 0, 'made before game time') +
      '</div>';
    if (m.reliability && m.reliability.length) {
      out += '<div class="section-title">Does a 70% pick win 70% of the time?</div><div class="scroll-x"><table class="grid">' +
        '<thead><tr><th>We said</th><th class="num">Games</th><th class="num">Average said</th><th class="num">Actually won</th></tr></thead><tbody>' +
        m.reliability.map(function (b) {
          var gap = b.actual != null && b.pred != null ? Math.abs(b.actual - b.pred) : null;
          var cls = gap == null ? '' : (gap < 0.04 ? 'better' : (gap > 0.1 ? 'worse' : ''));
          return '<tr><td>' + pct(b.lo) + '–' + pct(Math.min(b.hi, 1)) + '</td><td class="num">' + b.n + '</td>' +
            '<td class="num">' + pct(b.pred, 1) + '</td><td class="num ' + cls + '">' + pct(b.actual, 1) + '</td></tr>';
        }).join('') + '</tbody></table></div>';
    }
    if (m.importance && m.importance.length) {
      var max = Math.max.apply(null, m.importance.map(function (i) { return i.weight; })) || 1;
      out += '<div class="section-title">What matters most in game picks</div><div class="drivers">' +
        m.importance.map(function (i) {
          return '<div class="driver"><span class="dname">' + esc(i.label) + '</span><span class="dbar"><i class="pos" style="left:0;width:' +
            ((i.weight / max) * 100).toFixed(1) + '%"></i></span></div>';
        }).join('') + '</div>';
    }
    var tuning = d.props_tuning || {};
    var keys = Object.keys(tuning).filter(function (k) { return k.charAt(0) !== '_'; });
    out += selfTuning(d, tuning);
    if (keys.length) {
      out += '<div class="section-title">Prop corrections learned so far</div><div class="scroll-x"><table class="grid">' +
        '<thead><tr><th>Prop</th><th class="num">Graded</th><th class="num">Our numbers scaled by</th></tr></thead><tbody>' +
        keys.map(function (k) {
          var t = tuning[k], label = ((d.props_record || {}).by_key || {})[k];
          return '<tr><td>' + esc(label ? label.label : k) + '</td><td class="num">' + t.n + '</td><td class="num">×' + t.bias.toFixed(2) + '</td></tr>';
        }).join('') + '</tbody></table></div>' +
        '<p style="color:var(--muted)">Once a prop type has 30 graded results, its projections are nudged up or down by how far players ' +
        'actually landed from them. The nudge grows with the sample and is capped.</p>';
    }
    if (m.runs && m.runs.length) {
      out += '<div class="section-title">Recent refits</div><div class="scroll-x"><table class="grid">' +
        '<thead><tr><th>When</th><th>Kept new settings?</th><th>Why</th></tr></thead><tbody>' +
        m.runs.slice().reverse().slice(0, 6).map(function (r) {
          return '<tr><td>' + esc((r.at || '').replace('T', ' ').replace('Z', '')) + '</td>' +
            '<td class="' + (r.adopted ? 'better' : 'worse') + '">' + (r.adopted ? 'yes' : 'no') + '</td>' +
            '<td style="color:var(--muted)">' + esc(r.reason || '') + '</td></tr>';
        }).join('') + '</tbody></table></div>';
    }
    out += '<p style="color:var(--faint)">Data refreshed ' + esc(d.generated) + '.</p></div></details>';
    return out;
  }

  function fmtNum(v) { return v == null ? '—' : (Math.round(v * 100) / 100); }

  function curveSvg(points, title) {
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
    return '<div><div class="section-title">' + esc(title || 'Accuracy over time') + '</div>' +
      '<svg class="curve" viewBox="0 0 ' + w + ' ' + h + '" preserveAspectRatio="none" role="img" ' +
      'aria-label="Cumulative accuracy over time">' +
      '<line class="axis" x1="' + pad + '" y1="' + (h - pad) + '" x2="' + w + '" y2="' + (h - pad) + '"/>' +
      (halfY > 0 && halfY < h ? '<line class="ref" x1="' + pad + '" y1="' + halfY.toFixed(1) +
        '" x2="' + w + '" y2="' + halfY.toFixed(1) + '"/>' : '') +
      '<path class="line" d="' + path + '"/></svg>' +
      '<div class="legend"><span>' + points.length + ' days graded</span>' +
      '<span>running hit rate ' + pct(points[points.length - 1].cum_acc, 1) + '</span>' +
      '<span>dashed line = coin flip (50%)</span></div></div>';
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
    if (state.view === 'record' || state.view === 'model') {
      var needR = leaguesInView().filter(function (k) { return !RECORDS[k]; });
      if (needR.length) {
        root.innerHTML = '<div class="games"><div class="skeleton"></div><div class="skeleton"></div></div>';
        var wantR = state.league;
        loadRecords(needR, function () { if (state.league === wantR && (state.view === 'record' || state.view === 'model')) render(); });
        return;
      }
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
    else if (state.view === 'record' || state.view === 'model') html = recordView();
    else if (state.view === 'today') html = staleNotice() + yesterdayRecap() + (isAll() ? bestPicks() : '') + topProps() + (isAll() ? jumpBar('today') : '') + gamesView('today');
    else html = gamesView(state.view);
    root.innerHTML = html;
    startLive();
  }

  function go(league, view, replace) {
    state.league = league; state.view = view; state.search = '';
    var hash = '#' + league + '/' + view;
    if (view === 'record' && state.lookup) hash += '/' + state.lookup.league + ':' + state.lookup.kind + '/' + encodeURIComponent(state.lookup.id);
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
    var scrollTo = ev.target.closest('[data-scroll]');
    if (scrollTo) { var sec = el(scrollTo.dataset.scroll); if (sec) sec.scrollIntoView({ behavior: 'smooth', block: 'start' }); return; }
    if (ev.target.closest('.totop')) { window.scrollTo({ top: 0, behavior: 'smooth' }); return; }
    var pick = ev.target.closest('[data-lookup]');
    if (pick) {
      var lp = pick.dataset.lookup.split('|');
      state.lookup = { league: lp[0], kind: lp[1], id: lp.slice(2).join('|') };
      state.lookupQuery = '';
      state.view = 'record';
      if (state.league !== 'all') state.league = lp[0];
      history.replaceState(null, '', '#' + state.league + '/record/' + lp[0] + ':' + lp[1] + '/' + encodeURIComponent(state.lookup.id));
      render();
      var panel = document.querySelector('.lookup-panel');
      if (panel) panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
      return;
    }
    if (ev.target.closest('[data-lookup-close]')) { state.lookup = null; history.replaceState(null, '', '#' + state.league + '/record'); render(); return; }
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
    if (ev.target.closest('[data-clear-filters]')) {
      state.filters[state.view] = state.view === 'props' ? propDefaults() : defaults(state.view);
      state.search = ''; render(); return;
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
      if (!open) patchLive();
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
      if (!isOpen) patchLive();
    }
  });
  var searchTimer = null;
  document.addEventListener('input', function (ev) {
    if (ev.target.id === 'lookup') {
      state.lookupQuery = ev.target.value;
      // Redraw only the search box so typing keeps its focus.
      var box = ev.target.closest('.lookup');
      if (box) {
        var v = ev.target.value, caret = ev.target.selectionStart;
        box.outerHTML = lookupBox();
        var again = el('lookup'); if (again) { again.focus(); again.setSelectionRange(caret, caret); }
      }
      return;
    }
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
    var name = ev.target.dataset && ev.target.dataset.selectFilter;
    if (name) {
      state.filters.props = state.filters.props || propDefaults();
      state.filters.props[name] = ev.target.value; render();
    }
  });
  window.addEventListener('popstate', function () { fromHash(true); });

  // ── live layer ───────────────────────────────────────────────────────────
  // The site is rebuilt every half hour, but a game moves faster than that.
  // While games are on, the page asks ESPN's public scoreboard and box score
  // feeds itself, every half minute, and:
  //   - shows the score and clock on each game row,
  //   - shows every chosen prop's progress against its line (the number so
  //     far, how much is still needed, whether it has already cleared),
  //   - the moment a game goes final, grades the game pick and every prop
  //     from the final box score and moves the game to Results, marked
  //     "confirming" until the next rebuild writes the same result down.
  // Nothing here changes a pick: the line, the lean and the probability were
  // frozen at kickoff and only the outcome is filled in.
  var LIVE = { games: {}, box: {}, boxAt: {}, timer: null, settled: {}, reloadTimer: null };

  function liveKey(league, id) { return league + ':' + id; }

  function leaguesToWatch() {
    var t = today(), y = addDays(t, -1);
    return LEAGUES.filter(function (k) {
      var d = DATA[k]; if (!d) return false;
      return (d.games || []).some(function (g) {
        if (g.date !== t && g.date !== y) return false;
        if (!g.final) return true;                  // scheduled or in progress
        return !!(g.props && countProps(g.props) > 0 && !propTally(g));   // finished, props not yet graded
      });
    });
  }

  function startLive() {
    stopLive();
    if (document.hidden) return;
    poll();
  }
  function stopLive() { clearTimeout(LIVE.timer); LIVE.timer = null; }

  function poll() {
    var leagues = leaguesToWatch();
    var t = today(), y = addDays(t, -1);
    var range = y.replace(/-/g, '') + '-' + t.replace(/-/g, '');
    var left = leagues.length, liveTotal = 0;
    var finish = function () {
      var pill = el('live-pill');
      var clock = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
      if (pill) {
        pill.textContent = liveTotal ? '● ' + liveTotal + ' live · ' + clock : 'Updated ' + clock;
        pill.className = 'live-pill' + (liveTotal ? ' on' : '');
      }
      patchLive();
      LIVE.timer = setTimeout(poll, liveTotal ? 30000 : 180000);
    };
    if (!left) { finish(); return; }
    leagues.forEach(function (k) {
      espnFetch(DATA[k].espn_path + '/scoreboard?dates=' + range)
        .then(function (json) { return ingestScoreboard(json, k); })
        .then(function (n) { liveTotal += n || 0; })
        .catch(function () {})
        .then(function () { if (--left === 0) finish(); });
    });
  }

  // Scoreboard → LIVE.games, then fetch box scores for the games that need one.
  function ingestScoreboard(json, league) {
    var liveCount = 0, wants = [];
    ((json && json.events) || []).forEach(function (ev) {
      var g = findGame(league, ev.id); if (!g) return;
      var comp = (ev.competitions || [])[0]; if (!comp) return;
      var away = (comp.competitors || []).filter(function (c) { return c.homeAway === 'away'; })[0] || {};
      var home = (comp.competitors || []).filter(function (c) { return c.homeAway === 'home'; })[0] || {};
      var type = ((comp.status || {}).type || {});
      var name = type.name || '';
      var st = (name === 'STATUS_FINAL' || type.completed) ? 'final'
        : (['STATUS_IN_PROGRESS', 'STATUS_HALFTIME', 'STATUS_END_PERIOD', 'STATUS_DELAYED', 'STATUS_RAIN_DELAY'].indexOf(name) >= 0 ? 'live' : 'pre');
      var rec = LIVE.games[liveKey(league, ev.id)] = {
        state: st, detail: type.shortDetail || type.detail || '', start: ev.date || '',
        away: Number(away.score), home: Number(home.score), at: Date.now()
      };
      if (st === 'live') liveCount++;
      var hasProps = g.props && countProps(g.props, true) > 0;
      if (st === 'live' && hasProps) wants.push({ g: g, league: league, every: 30000 });
      else if (st === 'final' && !g.final && !LIVE.settled[liveKey(league, ev.id)]) wants.push({ g: g, league: league, every: 0 });
      else if (st === 'final' && g.final && hasProps && !propTally(g) && !LIVE.settled[liveKey(league, ev.id)]) wants.push({ g: g, league: league, every: 0 });
      if (st === 'final' && !g.final && !hasProps) settleGame(league, g, rec, {});
    });
    var chain = Promise.resolve();
    wants.forEach(function (w) {
      var k = liveKey(w.league, w.g.id);
      if (w.every && LIVE.boxAt[k] && Date.now() - LIVE.boxAt[k] < w.every - 2000) return;
      chain = chain.then(function () { return fetchBox(w.league, w.g); });
    });
    return chain.then(function () { return liveCount; });
  }

  function fetchBox(league, g) {
    var d = DATA[league]; if (!d) return Promise.resolve();
    var sport = (d.espn_path || '').split('/')[0];
    var k = liveKey(league, g.id);
    return espnFetch(d.espn_path + '/summary?event=' + g.id).then(function (summary) {
      if (!summary) return;
      var box = parseBox(summary, sport);
      LIVE.box[k] = box; LIVE.boxAt[k] = Date.now();
      var status = (((summary.header || {}).competitions || [])[0] || {}).status || {};
      var final = (status.type || {}).name === 'STATUS_FINAL' || (status.type || {}).completed;
      var rec = LIVE.games[k];
      if (final && rec && !LIVE.settled[k] && Object.keys(box).length) settleGame(league, g, rec, box);
    }).catch(function () { /* feed unreachable: the static board stands */ });
  }

  // ── box score parsing (mirrors the server's column map) ──────────────────
  var BOX = {
    baseball: {
      batting: { ab: 'ab', r: 'runs', h: 'hits', rbi: 'rbi', hr: 'hr', bb: 'bb', k: 'so', so: 'so', sb: 'sb',
                 '2b': 'doubles', '3b': 'triples', tb: 'tb', atbats: 'ab', runs: 'runs', hits: 'hits', rbis: 'rbi',
                 homeruns: 'hr', walks: 'bb', strikeouts: 'so', stolenbases: 'sb', doubles: 'doubles', triples: 'triples', totalbases: 'tb' },
      pitching: { ip: 'ip', h: 'p_h', r: 'p_r', er: 'p_er', bb: 'p_bb', k: 'p_so', so: 'p_so', hr: 'p_hr', pc: 'pitches',
                  inningspitched: 'ip', hits: 'p_h', earnedruns: 'p_er', walks: 'p_bb', strikeouts: 'p_so', pitches: 'pitches' }
    },
    basketball: { '': { min: 'min', pts: 'pts', reb: 'reb', ast: 'ast', stl: 'stl', blk: 'blk', to: 'tov', '3pt': 'fg3', fg: 'fgm', ft: 'ftm',
                        oreb: 'oreb', dreb: 'dreb', pf: 'pf', minutes: 'min', points: 'pts', rebounds: 'reb', assists: 'ast', steals: 'stl',
                        blocks: 'blk', turnovers: 'tov', threepointfieldgoalsmade: 'fg3',
                        threepointfieldgoalsmadethreepointfieldgoalsattempted: 'fg3', fieldgoalsmadefieldgoalsattempted: 'fgm' } },
    football: {
      passing: { catt: 'pass_cmp', yds: 'pass_yds', td: 'pass_td', int: 'pass_int', completionsattempts: 'pass_cmp',
                 completionspassingattempts: 'pass_cmp', passingyards: 'pass_yds', passingtouchdowns: 'pass_td', interceptions: 'pass_int', sacks: 'sacked' },
      rushing: { car: 'rush_att', yds: 'rush_yds', td: 'rush_td', long: 'rush_long', rushingattempts: 'rush_att', rushingyards: 'rush_yds', rushingtouchdowns: 'rush_td' },
      receiving: { rec: 'rec', yds: 'rec_yds', td: 'rec_td', tgts: 'targets', long: 'rec_long', receptions: 'rec', receivingyards: 'rec_yds',
                   receivingtouchdowns: 'rec_td', receivingtargets: 'targets' },
      defensive: { tot: 'tackles', solo: 'solo', sacks: 'sacks', tfl: 'tfl', totaltackles: 'tackles' },
      kicking: { fg: 'fgm', xp: 'xpm', pts: 'kick_pts' }
    },
    hockey: {
      skaters: { g: 'goals', a: 'assists', pts: 'points', s: 'sog', sog: 'sog', bs: 'blocks', hits: 'hits', toi: 'toi', goals: 'goals',
                 assists: 'assists', points: 'points', shotsongoal: 'sog', shots: 'sog', blockedshots: 'blocks', timeonice: 'toi' },
      goalies: { sa: 'shots_against', ga: 'ga', sv: 'saves', svpct: 'sv_pct', toi: 'toi', shotsagainst: 'shots_against', goalsagainst: 'ga', saves: 'saves', savepct: 'sv_pct' }
    }
  };
  function normKey(s) { return String(s == null ? '' : s).toLowerCase().replace(/[^a-z0-9]/g, ''); }

  function boxValue(raw) {
    var t = String(raw == null ? '' : raw).trim();
    if (!t || t === '--' || t === '-') return null;
    if (t.indexOf(':') > 0) { var mm = t.split(':'); return +mm[0] + (+mm[1] || 0) / 60; }
    if (t.indexOf('-') > 0) t = t.split('-')[0];
    if (t.indexOf('/') > 0) t = t.split('/')[0];
    var n = parseFloat(t);
    return isNaN(n) ? null : n;
  }

  // {athleteId: {played: bool, stats: {...}}}
  function parseBox(summary, sport) {
    var table = BOX[sport] || {};
    var out = {};
    (((summary || {}).boxscore || {}).players || []).forEach(function (side) {
      (side.statistics || []).forEach(function (group) {
        var cat = normKey(group.name || group.type || '');
        var cols = table[cat];
        if (!cols) {
          if (sport === 'hockey' && cat !== 'goalies' && cat !== 'goalie') cols = table.skaters;
          else if (sport === 'basketball') cols = table[''];
          else return;
        }
        var labels = (group.labels || group.names || []).map(normKey);
        var names = (group.names || []).map(normKey);
        (group.athletes || []).forEach(function (a) {
          var id = String(((a.athlete || {}).id) || '');
          if (!id) return;
          var values = a.stats || [];
          var here = !(a.didNotPlay || a.active === false);
          if (!here && values.some(function (v) { var x = boxValue(v); return x != null && x !== 0; })) here = true;
          var rec = out[id] = out[id] || { played: false, stats: {} };
          rec.played = rec.played || here;
          if (!here) return;
          values.forEach(function (raw, i) {
            var key = (i < names.length ? cols[names[i]] : null) || (i < labels.length ? cols[labels[i]] : null);
            if (!key || rec.stats[key] != null) return;
            var v = boxValue(raw);
            if (v != null) rec.stats[key] = v;
          });
        });
      });
    });
    Object.keys(out).forEach(function (id) {
      var st = out[id].stats;
      if (sport === 'baseball') {
        if (st.tb == null && st.hits != null) {
          var singles = st.hits - (st.doubles || 0) - (st.triples || 0) - (st.hr || 0);
          st.tb = Math.max(singles, 0) + 2 * (st.doubles || 0) + 3 * (st.triples || 0) + 4 * (st.hr || 0);
        }
        if (st.ip != null) { var w = Math.floor(st.ip); st.outs = w * 3 + Math.round((st.ip - w) * 10); }
      } else if (sport === 'basketball') {
        if (st.pts != null) { st.pra = st.pts + (st.reb || 0) + (st.ast || 0); st.pr = st.pts + (st.reb || 0); st.pa = st.pts + (st.ast || 0); }
        st.stlblk = (st.stl || 0) + (st.blk || 0);
      } else if (sport === 'football') {
        st.scrim_yds = (st.rush_yds || 0) + (st.rec_yds || 0);
        st.td = (st.rush_td || 0) + (st.rec_td || 0);
      } else if (sport === 'hockey' && st.points == null && (st.goals != null || st.assists != null)) {
        st.points = (st.goals || 0) + (st.assists || 0);
      }
    });
    return out;
  }

  // ── progress against the line ────────────────────────────────────────────
  function fmtStat(v) { return Math.round(v * 10) / 10; }
  function needFor(line, v) {
    // Over 4.5 needs 5; over 250 (a whole-number line) needs 251.
    var target = Number.isInteger(line) ? line + 1 : Math.ceil(line);
    return fmtStat(Math.max(0, target - v));
  }
  function spareFor(line, v) {
    var ceiling = Number.isInteger(line) ? line - 1 : Math.floor(line);
    return fmtStat(Math.max(0, ceiling - v));
  }
  // What the number so far means for this pick, in words and a class.
  function progressState(p, v, final) {
    var line = p.line, over = p.pick === 'over';
    var above = v > line + 1e-9, push = Math.abs(v - line) < 1e-9;
    if (final) {
      if (push) return { cls: 'push', text: 'PUSH · landed on the line' };
      var ok = above === over;
      return { cls: ok ? 'win' : 'lose', text: ok ? '✓ CORRECT' : '✗ INCORRECT' };
    }
    if (over) {
      if (above) return { cls: 'win', text: '✓ cleared the line' };
      return { cls: 'wait', text: 'needs ' + needFor(line, v) + ' more' };
    }
    if (above) return { cls: 'lose', text: '✗ went over the line' };
    if (push) return { cls: 'wait', text: 'on the line' };
    return { cls: 'lead', text: spareFor(line, v) + ' to spare' };
  }
  function progressHtml(p, v, final, played) {
    if (played === false) return '<span class="prop-progress push"><span class="ptext">DID NOT PLAY</span></span>';
    if (v == null) return '';
    var s = progressState(p, v, final);
    var line = p.line;
    var scale = Math.max(line * 1.6, v * 1.08, line + 1, 1e-6);
    var unit = p.unit ? ' ' + esc(String(p.unit).toLowerCase()) : '';
    return '<span class="prop-progress ' + s.cls + (final ? '' : ' live') + '">' +
      '<span class="pbar"><i style="width:' + Math.min(100, (v / scale) * 100).toFixed(1) + '%"></i>' +
      '<u style="left:' + Math.min(100, (line / scale) * 100).toFixed(1) + '%"></u></span>' +
      '<span class="ptext"><b>' + fmtStat(v) + '</b> of ' + line + unit + ' · ' + s.text + '</span></span>';
  }

  // Everything on screen that shows a prop carries data-live="league|game|athlete|key".
  function propByLive(attr) {
    var parts = (attr || '').split('|');
    var g = findGame(parts[0], parts[1]); if (!g || !g.props) return null;
    var out = null;
    ['away', 'home'].forEach(function (side) { (g.props[side] || []).forEach(function (pl) {
      if (String(pl.id) !== parts[2]) return;
      (pl.props || []).forEach(function (p) { if (p.key === parts[3]) out = { g: g, player: pl, prop: p, league: parts[0] }; });
    }); });
    return out;
  }

  // Paint the live state onto whatever is on screen, without re-rendering.
  function patchLive() {
    document.querySelectorAll('.game[data-id]').forEach(function (row) {
      var k = liveKey(row.dataset.league, row.dataset.id), live = LIVE.games[k];
      var center = row.querySelector('.center'); if (!live || !center) return;
      if (live.state === 'live') {
        var html = '<span class="score live">' + (live.away || 0) + '–' + (live.home || 0) + '</span><span class="gstatus">● ' + esc(live.detail || 'LIVE') + '</span>';
        if (center.innerHTML !== html) {
          center.innerHTML = html; row.classList.add('live', 'flash');
          setTimeout(function () { row.classList.remove('flash'); }, 700);
        }
        var g = findGame(row.dataset.league, row.dataset.id), tag = row.querySelector('.tag.props');
        if (g && tag) tag.textContent = '● ' + liveTallyText(row.dataset.league, g);
      } else if (live.state === 'final' && !row.classList.contains('done')) {
        var fin = '<span class="score">' + (live.away || 0) + '–' + (live.home || 0) + '</span><span class="slabel">FINAL</span>';
        if (center.innerHTML !== fin) { center.innerHTML = fin; row.classList.remove('live'); row.classList.add('done'); }
      } else if (live.state === 'pre' && live.start && center.dataset.time) {
        var t = new Date(live.start).toLocaleTimeString('en-US', { timeZone: 'America/New_York', hour: 'numeric', minute: '2-digit' });
        var span = center.querySelector('.gtime'); if (span) span.textContent = t + ' ET';
      }
    });
    document.querySelectorAll('[data-live]').forEach(function (node) {
      var hit = propByLive(node.dataset.live); if (!hit) return;
      var p = hit.prop;
      if (p.hit != null || p.push || p.played === false) return;       // graded: the row already says so
      var k = liveKey(hit.league, hit.g.id), live = LIVE.games[k], box = LIVE.box[k];
      if (!live || live.state === 'pre' || !box) return;
      var entry = box[String(hit.player.id)];
      var v = entry ? entry.stats[(p.stat || '').replace(/_pg$/, '')] : null;
      var slot = node.querySelector('.prop-progress-slot'); if (!slot) return;
      var html = entry ? progressHtml(p, v == null && entry.played ? 0 : v, false, entry.played) : '';
      if (slot.innerHTML !== html) slot.innerHTML = html;
    });
    document.querySelectorAll('.player[data-athlete]').forEach(function (pl) {
      var game = pl.closest('.game'); if (!game) return;
      var k = liveKey(game.dataset.league, game.dataset.id), live = LIVE.games[k], box = LIVE.box[k];
      var chip = pl.querySelector('.plive'); if (!chip) return;
      if (!live || live.state !== 'live' || !box) { chip.hidden = true; return; }
      var g = findGame(game.dataset.league, game.dataset.id); if (!g) return;
      var t = liveTally(game.dataset.league, g, String(pl.dataset.athlete));
      chip.hidden = !t.n;
      if (t.n) chip.textContent = '● ' + t.good + ' of ' + t.n + ' on track';
    });
  }

  // How the chosen props stand right now: cleared or on track versus not.
  function liveTally(league, g, athleteId) {
    var k = liveKey(league, g.id), box = LIVE.box[k] || {};
    var t = { n: 0, good: 0 };
    ['away', 'home'].forEach(function (side) { (g.props[side] || []).forEach(function (pl) {
      if (athleteId && String(pl.id) !== athleteId) return;
      var entry = box[String(pl.id)]; if (!entry || !entry.played) return;
      (pl.props || []).forEach(function (p) {
        if (p.pending || p.line == null) return;
        var v = entry.stats[(p.stat || '').replace(/_pg$/, '')]; if (v == null) v = 0;
        t.n++;
        var s = progressState(p, v, false);
        if (s.cls === 'win' || s.cls === 'lead') t.good++;
      });
    }); });
    return t;
  }
  function liveTallyText(league, g) {
    var t = liveTally(league, g);
    return t.n ? 'PROPS ' + t.good + '/' + t.n + ' ON TRACK' : 'PROPS LIVE';
  }

  // ── settling a finished game on the page ────────────────────────────────
  // Mirrors the server's grading so the Results tab can show the game the
  // moment it ends. The next rebuild writes the same verdict to the ledger.
  function settleGame(league, g, live, box) {
    var k = liveKey(league, g.id);
    if (LIVE.settled[k]) return;
    LIVE.settled[k] = true;
    var changed = false;
    if (!g.final && isFinite(live.away) && isFinite(live.home)) {
      g.final = true; g.away_score = live.away; g.home_score = live.home;
      g.winner = live.away > live.home ? g.away : (live.home > live.away ? g.home : '');
      g.correct = g.winner ? (g.favored === g.winner) : null;
      g.locked = true; g.provisional = true;
      changed = true;
    }
    if (g.props && box && Object.keys(box).length) {
      ['away', 'home'].forEach(function (side) { (g.props[side] || []).forEach(function (pl) {
        (pl.props || []).forEach(function (p) {
          if (p.pending || p.line == null || p.hit != null || p.push) return;
          var entry = box[String(pl.id)];
          if (!entry || !entry.played) { p.played = false; p.actual = null; changed = true; return; }
          var v = entry.stats[(p.stat || '').replace(/_pg$/, '')];
          if (v == null) return;
          p.actual = Math.round(v * 100) / 100; p.played = true;
          if (Math.abs(v - p.line) < 1e-9) { p.push = true; p.hit = null; }
          else { p.push = false; p.hit = (v > p.line) === (p.pick === 'over'); }
          changed = true;
        });
      }); });
      g.props_locked = true; g.provisional = true;
    }
    if (changed) rerenderKeepingPlace();
  }

  // Re-render the current view but keep what the reader had open and where
  // they were on the page.
  function rerenderKeepingPlace() {
    var openGames = [], openPlayers = [];
    document.querySelectorAll('.game[data-open="1"]').forEach(function (r) {
      openGames.push(r.dataset.league + '|' + r.dataset.id);
      r.querySelectorAll('.player').forEach(function (pl) {
        var list = pl.querySelector('.prop-list');
        if (list && !list.hidden) openPlayers.push(r.dataset.league + '|' + r.dataset.id + '|' + pl.dataset.athlete);
      });
      var tab = r.querySelector('.dtab[aria-selected="true"]');
      if (tab) openGames[openGames.length - 1] += '|' + tab.dataset.dtab;
    });
    var y = window.scrollY;
    render();
    openGames.forEach(function (key) {
      var parts = key.split('|');
      var row = document.querySelector('.game[data-league="' + parts[0] + '"][data-id="' + parts[1] + '"]');
      if (!row) return;
      row.querySelector('.game-head').click();
      if (parts[2]) { var tab = row.querySelector('.dtab[data-dtab="' + parts[2] + '"]'); if (tab) tab.click(); }
      row.querySelectorAll('.player').forEach(function (pl) {
        if (openPlayers.indexOf(parts[0] + '|' + parts[1] + '|' + pl.dataset.athlete) >= 0) pl.querySelector('.player-head').click();
      });
    });
    window.scrollTo(0, y);
  }

  // ── fresh data without a reload ─────────────────────────────────────────
  // Every ten minutes, re-fetch each league's data file; when the site has
  // been rebuilt since, swap it in (keeping the page where it was). Games the
  // page settled itself are confirmed by the rebuilt file.
  function reloadData() {
    var loaded = LEAGUES.filter(function (k) { return DATA[k]; });
    if (!loaded.length || document.hidden) { scheduleReload(); return; }
    var stamp = Date.now(), left = loaded.length, before = {};
    loaded.forEach(function (k) { before[k] = DATA[k].generated; });
    loaded.forEach(function (k) {
      loadScript(k + ':reload:' + stamp, 'data/' + k + '.js?v=' + stamp, function () {
        if (--left === 0) {
          var changed = loaded.some(function (x) { return DATA[x] && DATA[x].generated !== before[x]; });
          if (changed) {
            LIVE.settled = {};
            loaded.forEach(function (x) { delete HISTORY[x]; delete RECORDS[x]; });
            rerenderKeepingPlace();
          }
          scheduleReload();
        }
      });
    });
  }
  function scheduleReload() { clearTimeout(LIVE.reloadTimer); LIVE.reloadTimer = setTimeout(reloadData, 600000); }
  document.addEventListener('visibilitychange', function () {
    if (document.hidden) { stopLive(); return; }
    startLive(); reloadData();
  });
  scheduleReload();

  // ── boot ─────────────────────────────────────────────────────────────────
  function fromHash(replace) {
    var parts = (location.hash || '').replace('#', '').split('/');
    var known = ['all'].concat(LEAGUES);
    var league = known.indexOf(parts[0]) >= 0 ? parts[0] : (window.SP_DEFAULT_LEAGUE || 'all');
    var viewNames = VIEWS.map(function (v) { return v[0]; });
    var view = viewNames.indexOf(parts[1]) >= 0 ? parts[1] : 'today';
    // #mlb/record/mlb:player/33192 opens that player's page directly.
    state.lookup = null;
    if (view === 'record' && parts[2] && parts[3]) {
      var lk = parts[2].split(':');
      if (lk.length === 2 && LEAGUES.indexOf(lk[0]) >= 0 && (lk[1] === 'team' || lk[1] === 'player')) {
        state.lookup = { league: lk[0], kind: lk[1], id: decodeURIComponent(parts.slice(3).join('/')) };
      }
    }
    go(league, view, replace !== false);
  }
  (function () {
    var btn = document.createElement('button');
    btn.className = 'totop'; btn.setAttribute('aria-label', 'Back to top'); btn.textContent = '↑';
    document.body.appendChild(btn);
    var ticking = false;
    window.addEventListener('scroll', function () {
      if (ticking) return; ticking = true;
      requestAnimationFrame(function () { btn.classList.toggle('show', window.scrollY > 700); ticking = false; });
    }, { passive: true });
  })();
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', function () { fromHash(true); });
  else fromHash(true);
})();
