'use strict';
/* Into the Unknown leaderboard: a static site that reads artifacts/ and src/grader/eval_artifacts/ of this repository.
   Serve the repository root (python viewer/serve.py, or GitHub Pages from the root) and open viewer/. All paths are relative. */
(() => {
  const ROOT = '..';
  const DATA = `${ROOT}/artifacts/judge_results/gpt-6-astra`;
  const POLICY = `${ROOT}/artifacts/judge_results/grading_policy`;
  const RUBRICS = `${ROOT}/src/grader/eval_artifacts`;

  const ICON = {
    integrity: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3l7 3v5c0 4.5-3 8.3-7 10-4-1.7-7-5.5-7-10V6l7-3z" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/><path d="M9 12l2 2 4-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    direction: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="8.5" fill="none" stroke="currentColor" stroke-width="2"/><circle cx="12" cy="12" r="4.5" fill="none" stroke="currentColor" stroke-width="2"/><circle cx="12" cy="12" r="1.3" fill="currentColor"/></svg>',
    search: '<svg width="14" height="14" viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="7" fill="none" stroke="currentColor" stroke-width="2"/><path d="m20 20-3.5-3.5" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>',
    folder: '<svg class="fi" viewBox="0 0 24 24" aria-hidden="true"><path d="M3 6.5A1.5 1.5 0 0 1 4.5 5h4.6l2 2.2h8.4A1.5 1.5 0 0 1 21 8.7v9.8a1.5 1.5 0 0 1-1.5 1.5h-15A1.5 1.5 0 0 1 3 18.5z" fill="currentColor" opacity=".9"/></svg>',
    file: '<svg class="fi" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 3h8l5 5v12a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/><path d="M14 3v5h5" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/></svg>',
    missing: '<svg class="fi" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 3h8l5 5v12a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-dasharray="3 2.4" stroke-linejoin="round"/><path d="M9.5 11.5l5 5m0-5l-5 5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>',
  };
  const RUB = { integrity: 'Integrity check rubric', direction: 'Direction-specific rubric' };
  const RK = ['integrity', 'direction'];
  const GATES = ['G1', 'G2', 'G3'];
  const SUB = { G1: 'G₁', G2: 'G₂', G3: 'G₃' };
  const GATE_NAME = { G1: 'Honesty', G2: 'Completeness', G3: "Scientist's bar" };
  const ROOTS = [
    { gate: 'G1', cls: 'integrity', sub: 'Integrity check rubric' },
    { gate: 'G2', cls: 'direction', sub: 'Direction-specific rubric · required deliverables' },
    { gate: 'G3', cls: 'direction', sub: 'Direction-specific rubric · success conditions' },
  ];
  const GATING = new Set(['required', 'conditional', 'composite_member']);  // roles that can fail a gate
  const KIND = {
    pass: { badge: '✓ PASSED', icon: '✓' }, fail: { badge: '✕ FAILED', icon: '✕' },
    na: { badge: 'NOT APPLICABLE', icon: '–' }, diag: { badge: 'DIAGNOSTIC', icon: '◆' }, opt: { badge: 'OPTIONAL', icon: '' },
  };

  const app = document.getElementById('app');
  const hero = document.getElementById('hero');
  const state = {
    index: null, cache: new Map(), view: null, home: 'leaderboard',
    an: { mode: 'strict', gate: 'all' },
    sub: null, sel: null,
    tree: { failOnly: false, q: '', closed: new Set(), rootClosed: new Set() },
    side: 'nodes', files: null, dirDomain: 'all', pp: { agent: null, paper: 1 },
  };

  // ---------------------------------------------------------------- utilities
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const humanize = (s) => String(s ?? '').replace(/_/g, ' ');
  const pad = (r) => String(r).padStart(2, '0');
  const fmtValue = (v) => (v == null ? 'no answer' : Array.isArray(v) ? v.join(', ') : typeof v === 'object' ? JSON.stringify(v) : String(v));
  const pct = (v) => `${v.toFixed(1)}%`;
  // A path the judge cites inside the submission view: same expression as build_viewer_files.py (CITE_RE / norm_cite).
  const CITE_RE = /(?:proposal|workdir_outside_proposal)\/(?:\{[^{}\n]{0,120}\}|[^\s,;:`'"()\[\]<>{}])*/g;
  const normCite = (p) => p.replace(/[./]+$/, '').replace('proposal/../', 'workdir_outside_proposal/');
  const fmtHours = (h) => (h == null ? '—' : h < 1 ? `${Math.round(h * 60)} min` : `${h.toFixed(1)} h`);
  const fmtCount = (n) => Number(n).toLocaleString('en-US');
  const fmtBytes = (b) => {
    const u = ['B', 'KB', 'MB', 'GB', 'TB'];
    let x = b; let i = 0;
    while (x >= 1024 && i < u.length - 1) { x /= 1024; i += 1; }
    return `${x < 10 && i ? x.toFixed(1) : Math.round(x)} ${u[i]}`;
  };
  const rubBadge = (k) => `<span class="rub ${k}">${ICON[k]}${RUB[k]}</span>`;

  async function getJSON(url) {
    if (!state.cache.has(url)) {
      // no-cache: always revalidate with the server (cheap 304s), so an updated export shows up without a hard reload
      state.cache.set(url, fetch(url, { cache: 'no-cache' }).then((r) => {
        if (!r.ok) throw new Error(`${r.status} ${r.statusText} — ${url}`);
        return r.json();
      }));
    }
    try { return await state.cache.get(url); } catch (e) { state.cache.delete(url); throw e; }
  }

  function initTheme() {
    const root = document.documentElement;
    let saved = null;
    try { saved = localStorage.getItem('sog-viewer-theme'); } catch (_) { /* storage unavailable */ }
    const forced = new URLSearchParams(location.search).get('theme');
    root.dataset.theme = (forced === 'light' || forced === 'dark') ? forced
      : saved || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
    document.getElementById('theme-toggle').addEventListener('click', () => {
      root.dataset.theme = root.dataset.theme === 'dark' ? 'light' : 'dark';
      try { localStorage.setItem('sog-viewer-theme', root.dataset.theme); } catch (_) { /* ignore */ }
    });
  }

  const subOf = (dir, paper) => state.index.leaderboard.submissions.find((s) => s.model === dir && s.paper === paper);

  // ---------------------------------------------------------------- home: leaderboard, analysis, research directions
  const EFFORT_TIP = {
    max: 'Reasoning effort pinned to max (src/solver/run.sh), as recorded in every run',
    default: "No reasoning-effort setting in this harness: the served model's default, with thinking on",
  };
  const effortCell = (m) => `<span class="effort ${esc(m.reasoning_effort)}" title="${esc(EFFORT_TIP[m.reasoning_effort] || '')}">${esc(m.reasoning_effort || '—')}</span>`;
  const TIME_TIP = 'Median over the 15 papers of the wall-clock time of the agent\'s run (all attempts, including waiting and experiments)';
  const timeCell = (a) => `<span class="time" title="${esc(TIME_TIP)}">${fmtHours(a?.median_hours)}</span>`;
  const TABS = [['leaderboard', 'Leaderboard', '#/'], ['submit', "Submit Your Agent's Paper", '#/submit'], ['analysis', 'Analysis', '#/analysis'],
    ['directions', 'Research directions', '#/directions'], ['papers', 'AI-written papers', '#/papers']];

  function renderHome(tab) {
    const first = state.view === null || state.view.startsWith('sub:');
    state.view = `home:${tab}`;
    if (tab === 'leaderboard' || tab === 'analysis') state.home = tab;
    state.sub = null;
    hero.hidden = false;
    if (first) window.scrollTo(0, 0);
    app.innerHTML = `
      <div class="board ${tab}">
        <div class="board-head">
          <nav class="tabs" aria-label="Views">
            ${TABS.map(([k, l, h]) => `<a href="${h}" aria-current="${tab === k ? 'page' : 'false'}">${l}</a>`).join('')}
          </nav>
          ${tab === 'directions' || tab === 'papers' || tab === 'submit' ? '' : '<div class="board-meta" title="Paper-level grades combine the judge\'s repeated judgments of a submission node by node (3 of 4 must agree)">GPT-6-Astra judge (majority@4)</div>'}
        </div>
        <div id="board"></div>
      </div>`;
    if (tab === 'analysis') renderAnalysis(); else if (tab === 'directions') renderDirections(); else if (tab === 'papers') renderPapers();
    else if (tab === 'submit') renderSubmit(); else renderLeaderboard();
  }

  function renderLeaderboard() {
    const { models, papers, leaderboard: L } = state.index;
    const n = papers.length;
    const thr = Math.round(100 * L.threshold);
    const count = (dir, mode) => L.submissions.filter((s) => s.model === dir && GATES.every((g) => s[mode][g])).length;
    const rows = models.map((m) => {
      const a = L.agents.find((x) => x.model === m.dir);
      return a && { m, a, strict: count(m.dir, 'strict'), almost: count(m.dir, 'almost'), node: (a.R_integrity + a.R_direction) / 2 };
    }).filter(Boolean).sort((x, y) => y.strict - x.strict || y.almost - x.almost || y.node - x.node);
    const score = (v) => `<div class="score"><div class="v"><b>${pct(100 * v / n)}</b><span class="of">${v}/${n}</span></div>
      <span class="bar"><span style="width:${100 * v / n}%"></span></span></div>`;
    document.getElementById('board').innerHTML = `
      <section class="card lb-card"><div class="lb-wrap"><table class="lb">
        <thead><tr>
          <th>Model</th><th class="h-col">Agent harness</th><th class="h-col">Reasoning effort</th>
          <th class="num" title="A paper passes when each gate (honesty, completeness, scientist's bar) has at least ${thr}% of its nodes passing">Almost<span class="sub">(#papers passing<br>≥${thr}% of nodes)</span></th>
          <th class="num main" title="A paper passes when every node of all three gates passes (our main metric)">Strict<span class="sub">(#papers passing<br>100% of nodes)</span></th>
          <th class="num" title="${esc(TIME_TIP)}">Median time<span class="sub">per paper</span></th>
        </tr></thead>
        <tbody>${rows.map((r) => `<tr data-go="#/s/${encodeURIComponent(r.m.dir)}/1" tabindex="0" title="Browse ${esc(r.m.label)}'s papers">
          <td><span class="model">${esc(r.m.label)}</span></td>
          <td class="h-col"><span class="harness">${esc(r.m.harness)}</span></td>
          <td class="h-col">${effortCell(r.m)}</td>
          <td class="num">${score(r.almost)}</td>
          <td class="num main">${score(r.strict)}</td>
          <td class="num">${timeCell(r.a)}</td></tr>`).join('')}
        </tbody></table></div></section>`;
    document.querySelectorAll('#board [data-go]').forEach((tr) => {
      tr.addEventListener('click', () => { location.hash = tr.dataset.go; });
      tr.addEventListener('keydown', (e) => { if (e.key === 'Enter') location.hash = tr.dataset.go; });
    });
  }

  // Effort vs pass rates (paper Table 2): figures/effort_rubric_associations/association_table.csv, 135 submissions.
  // [label, integrity [D, CI low, CI high, p_adj, significant], direction [...]]
  const ASSOC = [
    ['Execution time (within paper)', [-0.313, -0.446, -0.167, '0.0014', true], [-0.324, -0.454, -0.191, '&lt; 8 × 10<sup>−4</sup>', true]],
    ['Execution time (within agent)', [-0.023, -0.201, 0.181, '1.000', false], [-0.056, -0.261, 0.136, '1.000', false]],
    ['# Steps (within paper)', [-0.143, -0.219, -0.063, '0.0024', true], [-0.009, -0.072, 0.065, '1.000', false]],
    ['# Steps (within agent)', [0.016, -0.151, 0.183, '1.000', false], [0.008, -0.169, 0.194, '1.000', false]],
  ];
  const num = (v) => (v < 0 ? `−${(-v).toFixed(3)}` : v.toFixed(3));
  const figure = (file, w, h, alt, caption) => `<figure class="an-fig"><a href="images/${file}" target="_blank" rel="noopener" title="Open full size">
    <img src="images/${file}" width="${w}" height="${h}" alt="${esc(alt)}" loading="lazy" decoding="async"></a><figcaption>${caption}</figcaption></figure>`;

  function renderAnalysis() {
    const { models, papers, leaderboard: L } = state.index;
    const thr = Math.round(100 * L.threshold);
    const agents = models.map((m) => ({ m, a: L.agents.find((x) => x.model === m.dir) })).filter((r) => r.a);
    const PAPER_GATES = [...GATES, 'all'];
    const cols = [
      ['R_integrity', (a) => a.R_integrity], ['R_direction', (a) => a.R_direction],
      ...PAPER_GATES.flatMap((g) => [[`almost_${g}`, (a) => a.almost[g]], [`strict_${g}`, (a) => a.strict[g]]]),
    ];
    const best = Object.fromEntries(cols.map(([k, f]) => [k, Math.max(...agents.map((r) => f(r.a)))]));
    const gateHead = { G1: '% of honest papers (G₁)', G2: '% of complete papers (G₂)', G3: "% of papers that passed the scientist's bar (G₃)",
      all: '% of valid papers (G₁ ∩ G₂ ∩ G₃)' };
    const { mode, gate } = state.an;
    const sel = gate === 'all' ? GATES : [gate];
    const passed = (x) => (x.counted ? 100 * (x.counted - x.failed) / x.counted : 0);   // share of the rubric's nodes passed
    const cell = (m, p) => {
      const s = subOf(m.dir, p.id);
      if (!s) return '<td></td>';
      const pass = sel.every((g) => s[mode][g]);
      const I = passed(s.rubrics.integrity);
      const D = passed(s.rubrics.direction);
      const lines = [
        `Integrity check rubric: ${s.rubrics.integrity.counted - s.rubrics.integrity.failed} of ${s.rubrics.integrity.counted} nodes passed`,
        `Direction-specific rubric: ${s.rubrics.direction.counted - s.rubrics.direction.failed} of ${s.rubrics.direction.counted} nodes passed`,
        ...GATES.map((g) => `${SUB[g]} ${GATE_NAME[g].toLowerCase()}: strict ${s.strict[g] ? 'pass' : 'fail'}, almost ${s.almost[g] ? 'pass' : 'fail'}`),
        `Run time: ${fmtHours(s.hours)}`,
      ];
      const tip = `${m.label} · Paper ${p.id}: ${p.title}\n${pass ? 'Passes' : 'Fails'} ${mode === 'strict' ? 'strict (100%)' : `almost (≥${thr}%)`}`
        + ` ${gate === 'all' ? 'G₁∩G₂∩G₃ (valid)' : SUB[gate]}\n${lines.join('\n')}\n${s.judgments > 1 ? `Per-node majority of ${s.judgments} judgments` : '1 judgment'} · click to browse the nodes`;
      const line = (k, v) => `<span class="pl"><i class="${k}">${k === 'int' ? 'I' : 'D'}</i><b>${Math.round(v)}%</b></span><span class="mb ${k}"><span style="width:${v}%"></span></span>`;
      return `<td><a class="pc ${pass ? 'pass' : 'fail'}" href="#/s/${encodeURIComponent(m.dir)}/${p.id}" title="${esc(tip)}"
        aria-label="${esc(`Paper ${p.id}: ${pass ? 'passed' : 'failed'}; ${Math.round(I)}% of integrity and ${Math.round(D)}% of direction nodes passed`)}">${line('int', I)}${line('dir', D)}</a></td>`;
    };
    document.getElementById('board').innerHTML = `
      <section class="card an-card">
        <div class="an-head"><h2>Pass rates</h2></div>
        <div class="wrap-x"><table class="rates">
          <thead>
            <tr><th class="l" rowspan="3">Model</th><th class="l" rowspan="3">Agent harness</th><th class="l" rowspan="3">Reasoning<br>effort</th>
              <th class="grp gap" colspan="2">Node-level grading</th><th class="grp gap" colspan="8">Paper-level grading <span class="faint">(almost: ≥${thr}% of nodes pass · strict: 100%)</span></th>
              <th class="gap" rowspan="3" title="${esc(TIME_TIP)}">Median time<br>per paper</th></tr>
            <tr><th rowspan="2" class="gap">Integrity check<br>rubric pass rate</th><th rowspan="2">Direction-specific<br>rubric pass rate</th>
              ${PAPER_GATES.map((g) => `<th colspan="2" class="gap gh ${g === 'all' ? 'main' : ''}">${esc(gateHead[g])}</th>`).join('')}</tr>
            <tr>${PAPER_GATES.map((g) => `<th class="cond gap">Almost</th><th class="cond ${g === 'all' ? 'main' : ''}">Strict</th>`).join('')}</tr>
          </thead>
          <tbody>${agents.map(({ m, a }) => `<tr><td class="l"><span class="model">${esc(m.label)}</span></td>
            <td class="l"><span class="harness">${esc(m.harness)}</span></td><td class="l">${effortCell(m)}</td>
            ${cols.map(([k, f], i) => {
              const v = f(a);
              return `<td class="${v > 0 && v === best[k] ? 'best' : ''}${i % 2 === 0 ? ' gap' : ''}${k === 'strict_all' ? ' main' : ''}">${pct(v)}</td>`;
            }).join('')}<td class="gap">${timeCell(a)}</td></tr>`).join('')}
          </tbody></table></div>
      </section>
      <section class="card an-card">
        <div class="an-head"><h2>Per paper</h2>
          <div class="segmented" role="group" aria-label="Grading">
            ${[['strict', 'Strict (100%)'], ['almost', `Almost (≥${thr}%)`]].map(([k, l]) => `<button type="button" data-mode="${k}" aria-pressed="${mode === k}">${l}</button>`).join('')}
          </div>
          <div class="segmented" role="group" aria-label="Gate">
            ${[['all', 'G₁ ∩ G₂ ∩ G₃ Valid'], ['G1', 'G₁ Honest'], ['G2', 'G₂ Complete'], ['G3', "G₃ Scientist's bar"]].map(([k, l]) => `<button type="button" data-gate="${k}" aria-pressed="${gate === k}">${l}</button>`).join('')}
          </div>
          <div class="legend"><span><i class="sq pass"></i>passed</span><span><i class="sq fail"></i>failed</span>
            <span>bars: % of nodes passed in the <b class="int">I</b>ntegrity check and <b class="dir">D</b>irection-specific rubrics</span></div>
        </div>
        <div class="wrap-x"><table class="matrix">
          <thead>
            <tr><th class="l" rowspan="2">Model</th><th class="grp" colspan="${papers.length}">Papers</th><th class="tot-h" rowspan="2"># Papers that<br>passed</th>
              <th class="tot-h" rowspan="2" title="${esc(TIME_TIP)}">Median time<br>per paper</th></tr>
            <tr>${papers.map((p) => `<th class="p" title="${esc(`Paper ${p.id}: ${p.title}\nFollow-up: ${p.direction}`)}">P${p.id}</th>`).join('')}</tr>
          </thead>
          <tbody>${agents.map(({ m, a }) => {
            const passed = papers.filter((p) => { const s = subOf(m.dir, p.id); return s && sel.every((g) => s[mode][g]); }).length;
            return `<tr><td class="l"><span class="model">${esc(m.label)}</span></td>${papers.map((p) => cell(m, p)).join('')}<td class="tot">${passed}/${papers.length}</td>
              <td class="tot time-c">${timeCell(a)}</td></tr>`;
          }).join('')}</tbody>
        </table></div>
      </section>
      <section class="card an-card">
        <div class="an-head"><h2>Why agents fail</h2></div>
        <div class="prose">
          <p>Across all 135 submissions, 114 fail the integrity check rubric and every one fails the direction-specific rubric. The most
            frequent integrity failures are a mismatch between the paper and its code or protocol (90 submissions), reported numbers that
            contradict the evidence (85), and false claims of completed work (68). On the direction-specific rubric, most submissions lack
            required files or records (128), leave method or protocol requirements unmet (120), or give an inadequate analysis or
            explanation (104).</p>
          ${figure('failure_analysis1.png', 1886, 784, 'Heatmaps counting, for each agent, the submissions that fail each category of the two rubrics',
            "Number of each agent's 15 submissions that fail each category of the integrity check rubric (left) and the direction-specific rubric (right), from the judge's verdicts. Categories overlap; the bottom rows count the submissions that fail each rubric.")}
          <p>Reading the agents' trajectories, we identified three behaviors that keep them from writing an honest, complete paper that
            meets the scientist's bar:</p>
          <ul class="behaviors">
            <li><b>Lenient self-verification.</b> Agents treat limited checks as evidence that broader task requirements are satisfied, or
              accept results as satisfactory despite contradictory evidence. They are not strict enough when checking their own work and
              whether a task is complete.</li>
            <li><b>Insufficient backtracking.</b> Agents recognize an unmet requirement but, instead of trying to fix it, document the
              shortfall or proceed with a suboptimal solution. The issue is not a negative scientific finding itself, but the decision to
              stop pursuing a specified objective.</li>
            <li><b>Self-directed scope reduction.</b> Agents replace required work with fewer experiments, an easier comparison, or a
              narrower objective without authorization. Such changes can be reasonable in research, but they mean agents cannot
              autonomously carry out a specified direction.</li>
          </ul>
          ${figure('failure_analysis2.png', 1894, 1622, 'Annotated excerpts from a Claude-Fable-5 trajectory and a GPT-5.6-Sol trajectory',
            'Excerpts from two trajectories. Claude-Fable-5 writes its own completion checker and accepts its output (lenient self-verification); GPT-5.6-Sol acknowledges that its method misses a requirement and writes the paper around it (insufficient backtracking).')}
        </div>
      </section>
      <section class="card an-card">
        <div class="an-head"><h2>Does more effort help?</h2></div>
        <div class="prose">
          <p>No. Comparing different agents on the same paper, longer runs go with lower pass rates on both rubrics, and more steps with
            lower integrity pass rates. Comparing papers attempted by the same agent, neither execution time nor the number of steps is
            associated with pass rates: no agent does better on the papers it spends longer on.</p>
          ${figure('time_analysis.png', 1900, 784, 'Dot plots of the number of steps and the execution time of every submission, by agent',
            "Number of recorded steps (left) and execution time in hours (right) for each agent, ordered by median execution time. Each point is one submission; ticks mark medians, bars span the interquartile range, and each agent's median is listed on the right. Both axes are logarithmic.")}
          <div class="wrap-x"><table class="rates assoc">
            <thead>
              <tr><th class="l" rowspan="2">Comparison axis</th><th class="grp gap" colspan="2">Integrity check rubric pass rate</th>
                <th class="grp gap" colspan="2">Direction-specific rubric pass rate</th></tr>
              <tr><th class="gap">Mean Somers' <i>D</i> [95% CI]</th><th><i>p</i><sub>adj</sub></th>
                <th class="gap">Mean Somers' <i>D</i> [95% CI]</th><th><i>p</i><sub>adj</sub></th></tr>
            </thead>
            <tbody>${ASSOC.map(([label, ...cells]) => `<tr><td class="l">${label}</td>${cells.map(([d, lo, hi, p, sig]) =>
              `<td class="gap">${num(d)} [${num(lo)}, ${num(hi)}]</td><td class="${sig ? 'best' : ''}">${p}</td>`).join('')}</tr>`).join('')}</tbody>
          </table></div>
          <p class="tnote">Pass rates are the fractions of rubric nodes passed. Comparisons pair different agents on the same paper (within
            paper) or different papers attempted by the same agent (within agent). We report mean Somers' <i>D</i> with pointwise 95%
            confidence intervals and approximate <i>p</i>-values from 20,000 paper-level bootstrap resamples; <i>p</i><sub>adj</sub> is the
            <i>p</i>-value after Holm correction across all eight comparisons, and bold marks <i>p</i><sub>adj</sub> &lt; 0.05. Negative
            <i>D</i> indicates lower pass rates with greater effort. Harnesses record steps at different granularities, so within-paper step
            comparisons are descriptive.</p>
        </div>
      </section>`;
    document.querySelectorAll('#board [data-mode]').forEach((b) => b.addEventListener('click', () => { state.an.mode = b.dataset.mode; renderAnalysis(); }));
    document.querySelectorAll('#board [data-gate]').forEach((b) => b.addEventListener('click', () => { state.an.gate = b.dataset.gate; renderAnalysis(); }));
  }

  // ---------------------------------------------------------------- submit a paper
  // A static site cannot send mail: the form posts to FormSubmit (formsubmit.co), which emails it, PDF attached, to SUBMIT_TO.
  const SUBMIT_TO = 'into.the.unknown.grader@gmail.com';
  const MAX_PDF = 10 * 1024 * 1024;   // FormSubmit's limit for all attachments of one submission
  function renderSubmit() {
    const { papers } = state.index;
    const back = `${location.origin}${location.pathname}?submitted=1#/submit`;
    const field = (label, control, opts = {}) => `<label class="f ${opts.full ? 'full' : ''}"><span>${label}${opts.optional ? '' : ' <i>*</i>'}</span>
      ${control}${opts.hint ? `<small>${opts.hint}</small>` : ''}</label>`;
    document.getElementById('board').innerHTML = `
      <section class="card sub-card">
        ${state.submitted ? '<div class="sub-done" role="status"><b>Thank you, your submission was sent.</b> We will email the results to the address you gave.</div>' : ''}
        <h2>Submit your agent's paper</h2>
        <p class="sub-lede">Send us the follow-up paper your agent wrote for one of the 15 research directions (P1–P15), one paper per
          submission. Upon request, we will evaluate the artifacts and send the results back to you.</p>
        <div class="sub-req"><div class="lab">Required</div><ol>
          <li><b>The paper PDF</b> your agent wrote (up to 10 MB).</li>
          <li><b>A link to the agent's workspace</b>, where the code, logs and results it produced are stored, rather than the files
            themselves. We recommend a Google Drive folder or a GitHub repository that anyone with the link can view.</li></ol></div>
        <form class="sub-form" id="submit-form" action="https://formsubmit.co/${SUBMIT_TO}" method="POST" enctype="multipart/form-data" novalidate>
          <input type="hidden" name="_subject" value="Into the Unknown submission">
          <input type="hidden" name="_template" value="table">
          <input type="hidden" name="_next" value="${esc(back)}">
          <input type="hidden" name="_autoresponse" value="Thank you for submitting to Into the Unknown. We received your agent's paper and workspace link, and will email you the results.">
          <input type="text" name="_honey" class="hp" tabindex="-1" autocomplete="off" aria-hidden="true">
          ${field('Research direction', `<select name="paper" required><option value="">Choose a paper (P1–P15)…</option>
            ${papers.map((p) => `<option value="${esc(`P${p.id}: ${p.title}`)}">P${p.id}: ${esc(p.title)}</option>`).join('')}</select>`, { full: true })}
          ${field('Agent (model and harness)', '<input name="agent" required placeholder="e.g. Claude-Opus-5.5 with claude-agent-sdk">')}
          ${field('Your name', '<input name="name" required autocomplete="name">')}
          ${field('Email for the results', '<input type="email" name="email" required autocomplete="email">')}
          ${field('Affiliation', '<input name="affiliation" autocomplete="organization">', { optional: true })}
          ${field('Paper PDF', '<input type="file" name="paper_pdf" accept="application/pdf,.pdf" required>', { full: true, hint: 'One PDF file, up to 10 MB.' })}
          ${field('Workspace link', '<input type="url" name="workspace_link" required placeholder="https://drive.google.com/… or https://github.com/…">',
            { full: true, hint: "A Google Drive folder or GitHub repository with the agent's workspace (code, logs, results)." })}
          ${field('Notes', '<textarea name="notes" rows="3" placeholder="Anything we should know, e.g. run time, compute or settings"></textarea>', { full: true, optional: true })}
          <label class="chk full"><input type="checkbox" name="link_viewable" value="yes" required> The workspace link is viewable by anyone with the link.</label>
          <div class="sub-actions full"><button type="submit" class="nav-btn primary">Submit paper</button><span class="sub-err" id="sub-err" role="alert"></span></div>
        </form>
        <p class="fineprint">Submissions are emailed to ${SUBMIT_TO} through <a href="https://formsubmit.co" target="_blank" rel="noopener">FormSubmit</a>,
          which may ask you to complete a CAPTCHA and sends you a confirmation copy.</p>
      </section>`;
    const form = document.getElementById('submit-form');
    const err = document.getElementById('sub-err');
    const el = (n) => form.elements.namedItem(n);
    form.addEventListener('submit', (e) => {
      const fail = (msg, x) => { e.preventDefault(); err.textContent = msg; x.focus(); };
      for (const x of form.querySelectorAll('[required]')) {
        if (!x.checkValidity()) {
          return fail(x.type === 'checkbox' ? 'Please confirm that the workspace link is viewable.'
            : `Please fill in “${x.closest('label').querySelector('span').textContent.replace('*', '').trim()}”${x.type === 'email' || x.type === 'url' ? ' correctly' : ''}.`, x);
        }
      }
      const pdf = el('paper_pdf').files[0];
      if (!/\.pdf$/i.test(pdf.name) && pdf.type !== 'application/pdf') return fail('The paper must be a PDF file.', el('paper_pdf'));
      if (pdf.size > MAX_PDF) return fail(`The PDF is ${fmtBytes(pdf.size)}; the limit is 10 MB.`, el('paper_pdf'));
      if (!/^https?:\/\/\S+\.\S+/i.test(el('workspace_link').value.trim())) return fail('The workspace link must be a web address (https://…).', el('workspace_link'));
      err.textContent = '';
      el('_subject').value = `Into the Unknown submission: ${el('paper').value.split(':')[0]} · ${el('agent').value.trim()}`;
      const btn = form.querySelector('button[type=submit]');
      btn.disabled = true;
      btn.textContent = 'Sending…';
      // coming back with the browser's Back button restores this page from cache: re-enable the button
      window.addEventListener('pageshow', () => { btn.disabled = false; btn.textContent = 'Submit paper'; }, { once: true });
    });
  }

  // Original paper links: arXiv / DOI pages, or a file of this repository (relative to its root).
  const paperUrl = (u) => (/^https?:/.test(u) ? u : `${ROOT}/${u}`);
  const paperSource = (u) => (u.includes('arxiv.org') ? 'arXiv preprint' : u.includes('researchsquare.com') ? 'Research Square preprint'
    : u.includes('doi.org') ? 'DOI publisher page' : 'PDF in this repository (not publicly released)');
  const paperSourceShort = (u) => paperSource(u).replace(/ (preprint|publisher page|in this repository.*)$/, '');   // arXiv, Research Square, DOI, PDF
  function renderDirections() {
    const { models, papers } = state.index;
    const domains = [...new Set(papers.map((p) => p.domain))].sort();
    const want = state.dirDomain || 'all';
    const shown = papers.filter((p) => want === 'all' || p.domain === want);
    const card = (p) => `<article class="dcard" id="paper-${p.id}">
        <div class="dc-top"><span class="pid">P${p.id}</span><span class="dom">${esc(p.domain)}</span>
          <span class="dc-meta">${esc(p.venue)}<span class="sep">·</span><span title="Citations as of September 14, 2026">${p.citations} citation${p.citations === 1 ? '' : 's'}</span>${p.lab ? `<span class="sep">·</span><span title="${esc(p.lab_name || p.lab)}">${esc(p.lab)}</span>` : ''}</span></div>
        <h3 class="dc-title">${p.url ? `<a href="${esc(paperUrl(p.url))}" target="_blank" rel="noopener" title="${esc(paperSource(p.url))}">${esc(p.title)}<span class="ext">${esc(paperSourceShort(p.url))} ↗</span></a>` : esc(p.title)}</h3>
        <div class="followup"><div class="lab">Follow-up direction proposed by the domain scientist</div><p>${esc(p.direction)}</p></div>
      </article>`;
    const groups = [['A', 'Group A', 'published 2024 or earlier'], ['B', 'Group B', 'submitted 2026']]
      .map(([g, name, note]) => [name, note, shown.filter((p) => p.group === g)]).filter(([, , ps]) => ps.length);
    document.getElementById('board').innerHTML = `
      <div class="dfilter" role="group" aria-label="Domain">
        ${[['all', `All papers <span>${papers.length}</span>`], ...domains.map((d) => [d, `${esc(d)} <span>${papers.filter((p) => p.domain === d).length}</span>`])]
          .map(([k, l]) => `<button type="button" data-domain="${esc(k)}" aria-pressed="${want === k}">${l}</button>`).join('')}
      </div>
      ${groups.map(([name, note, ps]) => `<section class="dgroup"><h2>${name} <span>${note}</span></h2><div class="dgrid">${ps.map(card).join('')}</div></section>`).join('')}`;
    document.querySelectorAll('#board [data-domain]').forEach((b) => b.addEventListener('click', () => { state.dirDomain = b.dataset.domain; renderDirections(); }));
  }

  function renderPapers() {
    const { models, papers } = state.index;
    const p = papers.find((x) => x.id === state.pp.paper) || papers[0];
    const avail = models.filter((m) => subOf(m.dir, p.id));
    const L = state.index.leaderboard;
    const top = [...L.agents].sort((x, y) => y.strict.all - x.strict.all || y.almost.all - x.almost.all)[0]?.model;
    const m = avail.find((x) => x.dir === (state.pp.agent || top)) || avail[0];
    const s = subOf(m.dir, p.id);
    const href = (dir, id) => `#/papers/${encodeURIComponent(dir)}/${id}`;
    const valid = (mode) => GATES.every((g) => s[mode][g]);
    const grade = (mode, label) => `<span class="tag ${valid(mode) ? 'ok' : 'no'}" title="${label}: the paper ${valid(mode) ? 'passes' : 'fails'} G₁∩G₂∩G₃">${label} ${valid(mode) ? '✓' : '✕'}</span>`;
    document.getElementById('board').innerHTML = `
      <div class="pp">
        <nav class="card pp-list" aria-label="Papers">
          ${papers.map((x) => `<a class="pp-item ${x.id === p.id ? 'on' : ''}" href="${href(m.dir, x.id)}" title="${esc(x.title)}"><span class="pid">P${x.id}</span><span class="t">${esc(x.title)}</span></a>`).join('')}
        </nav>
        <section class="card pp-main">
          <div class="pp-agents" role="group" aria-label="Agent">
            ${avail.map((x) => { const sx = subOf(x.dir, p.id); return `<a class="mchip ${x.dir === m.dir ? 'on' : ''} ${sx.pdf ? '' : 'none'}" href="${href(x.dir, p.id)}"
              title="${esc(sx.pdf ? `${x.label}'s paper for P${p.id}` : `${x.label} did not submit a paper for P${p.id}`)}">${esc(x.label)}</a>`; }).join('')}
          </div>
          <div class="pp-title"><span class="pid">P${p.id}</span><span>${esc(p.title)}</span></div>
          <div class="followup"><div class="lab">Follow-up direction proposed by the domain scientist</div><p>${esc(p.direction)}</p></div>
          <div class="pp-bar">
            <span class="pp-who">Written by <b>${esc(m.label)}</b></span>
            <span class="tag" title="Wall-clock time of the agent's run">run time ${fmtHours(s.hours)}</span>
            ${grade('almost', `Valid (almost, ≥${Math.round(100 * state.index.leaderboard.threshold)}%)`)}${grade('strict', 'Valid (strict)')}
            ${s.pdf?.redacted ? `<span class="tag warn" title="${esc(`Changed from the submitted PDF: ${s.pdf.redacted}`)}">author details redacted</span>` : ''}
            <span class="pp-actions">
              ${s.pdf ? `<a class="nav-btn" href="${ROOT}/${esc(s.pdf.path)}" target="_blank" rel="noopener">Open PDF ↗</a>` : ''}
              <a class="nav-btn" href="#/s/${encodeURIComponent(m.dir)}/${p.id}">Judge's verdicts →</a>
            </span>
          </div>
          ${s.pdf ? `<iframe class="pdf" src="${ROOT}/${esc(s.pdf.path)}#view=FitH" title="${esc(`${m.label}'s paper for P${p.id}`)}"></iframe>`
            : `<div class="pp-empty">${esc(m.label)} did not submit a paper for P${p.id}: its submission has no <code>proposal/report.pdf</code>.</div>`}
        </section>
      </div>`;
  }

  // ---------------------------------------------------------------- submission browser: data
  function classesOf(leaf) {
    if (Array.isArray(leaf.tiers)) return leaf.tiers.map((t) => ({ name: t.name, description: t.description, criteria: t.criteria }));
    if (Array.isArray(leaf.values)) return leaf.values.map((v) => (typeof v === 'string' ? { name: v } : v));
    if (leaf.values && typeof leaf.values === 'object') {
      return Object.entries(leaf.values).map(([name, v]) => ({ name, description: typeof v === 'string' ? v : v?.description, criteria: v?.criteria }));
    }
    return [];
  }
  function shortLabel(id, clusterKey) {
    const parts = String(id).split('.').filter((p) => !['rh', 'integrity'].includes(p) && p !== clusterKey);
    return humanize((parts.length ? parts : String(id).split('.').slice(-1)).join(' · '));
  }
  function kindOf(info) {
    if (info.status === 'not_applicable' || info.applicable === false) return 'na';
    if (info.role === 'diagnostic' || info.status === 'diagnostic') return 'diag';
    if (!GATING.has(info.role)) return 'opt';
    return info.status === 'pass' ? 'pass' : 'fail';  // undetermined (judge could not decide) counts as failed
  }

  async function renderSubmission(dir, paper, judgment, focusKey) {
    const { models, papers } = state.index;
    const model = models.find((m) => m.dir === dir);
    const p = papers.find((x) => x.id === paper);
    const s = model && p && subOf(dir, paper);
    if (!s) {
      app.innerHTML = '<div class="card error"><h2>Submission not found</h2><a class="muted" href="#/">← back to the leaderboard</a></div>';
      return;
    }
    const sameSubmission = state.sub && state.sub.dir === dir && state.sub.paper === paper;
    const key = `sub:${dir}|${paper}|${judgment}`;
    state.view = key;
    hero.hidden = true;
    if (!sameSubmission) app.innerHTML = '<div class="loading"><span class="spinner"></span>Loading verdicts…</div>';
    const dirUrl = `${DATA}/${encodeURIComponent(dir)}/paper${paper}`;
    const [rubI, rubD, policy, cons] = await Promise.all([
      getJSON(`${RUBRICS}/paper${paper}/integrity_check_rubric.json`),
      getJSON(`${RUBRICS}/paper${paper}/direction_specific_rubric.json`),
      getJSON(`${POLICY}/paper${paper}.json`), getJSON(`${dirUrl}/consensus.json`),
    ]);
    const files = await getJSON(`${dirUrl}/files.json`).catch(() => null);
    const reps = cons.judgments;
    const j = judgment !== 'm' && reps.includes(judgment) && reps.length > 1 ? judgment : 'm';
    const want = j === 'm' ? reps : [j];
    const loaded = await Promise.all(want.map((r) => Promise.all([
      getJSON(`${dirUrl}/repeat_${pad(r)}/integrity_check_rubric.json`), getJSON(`${dirUrl}/repeat_${pad(r)}/direction_specific_rubric.json`),
      j === 'm' ? null : getJSON(`${dirUrl}/repeat_${pad(r)}/grade.json`)])));
    if (state.view !== key) return;
    const verdicts = Object.fromEntries(want.map((r, i) => [r, { integrity: loaded[i][0], direction: loaded[i][1] }]));
    const statusSrc = j === 'm' ? cons.nodes : loaded[0][2].nodes;
    const rubric = { integrity: rubI, direction: rubD };

    // Group the nodes by the gate they count toward, then by rubric section.
    const all = [];
    for (const k of RK) {
      for (const [ck, c] of Object.entries(rubric[k].clusters || {})) {
        for (const leaf of c.leaves || []) {
          const nk = `${k}:${leaf.id}`;
          const info = statusSrc[nk] || {};
          const pol = policy.nodes[k]?.[leaf.id] || {};
          all.push({ key: nk, src: k, leaf, ck, secTitle: c.title || humanize(ck), label: shortLabel(leaf.id, ck), info, pol,
            gate: info.gate || pol.gate || 'other', assigned: info.assigned_gate || pol.gate, kind: kindOf(info),
            value: info.value ?? null, votes: j === 'm' ? info.votes : null,
            // the judgment whose reasoning is shown; when all judgments gave no grade, the first one
            repeat: j !== 'm' ? j : info.repeat ?? ((info.votes || []).length === 1 ? reps[0] : null) });
        }
      }
    }
    const roots = [...ROOTS, ...[...new Set(all.map((n) => n.gate))].filter((g) => !GATES.includes(g)).map((g) => ({ gate: g, cls: 'direction', sub: 'Other nodes' }))]
      .map((r) => {
        const secs = new Map();
        for (const n of all.filter((x) => x.gate === r.gate)) {
          const sk = `${r.gate}:${n.src}:${n.ck}`;
          if (!secs.has(sk)) secs.set(sk, { key: sk, title: n.secTitle, nodes: [] });
          secs.get(sk).nodes.push(n);
          n.sec = secs.get(sk);
          n.root = r;
        }
        return { ...r, sections: [...secs.values()] };
      }).filter((r) => r.sections.length);
    const nodes = roots.flatMap((r) => r.sections.flatMap((x) => x.nodes));
    if (!sameSubmission) {
      state.tree.closed = new Set(roots.flatMap((r) => r.sections.filter((x) => !x.nodes.some((n) => n.kind === 'fail')).map((x) => x.key)));
      state.tree.rootClosed = new Set();
      state.files = { open: new Set(['proposal']), q: '', citedOnly: false, sel: new Set(), showFor: new Set(), autoFor: null };
      window.scrollTo(0, 0);
    }
    const keepSel = state.sel && nodes.some((n) => n.key === state.sel) && sameSubmission ? state.sel : null;
    state.sel = (focusKey && nodes.some((n) => n.key === focusKey) && focusKey) || keepSel
      || (nodes.find((n) => n.kind === 'fail') || nodes[0])?.key;
    state.sub = { dir, paper, j, reps, model, p, s, roots, nodes, verdicts, cons, files };
    state.sub.citeIndex = buildCiteIndex();
    const focus = nodes.find((n) => n.key === state.sel);
    if (focus) { state.tree.closed.delete(focus.sec.key); state.tree.rootClosed.delete(focus.root.gate); }
    submissionShell();
    renderSide();
    renderNode();
  }

  const subHash = (S, key, j = S.j) => `#/s/${encodeURIComponent(S.dir)}/${S.paper}/${j}${key ? `/${encodeURIComponent(key)}` : ''}`;

  // ---------------------------------------------------------------- submission browser: layout
  function submissionShell() {
    const S = state.sub;
    const { models, papers } = state.index;
    const withPaper = models.filter((m) => subOf(m.dir, S.paper));
    const ids = papers.map((x) => x.id);
    const i = ids.indexOf(S.paper);
    const prev = ids[i - 1];
    const next = ids[i + 1];
    const P = S.p;
    const meta = [
      `<span title="${P.group === 'A' ? 'Published 2024 or earlier' : 'Submitted 2026'}">Group ${esc(P.group)}</span>`,
      esc(P.domain), esc(P.venue),
      `<span title="Citations as of September 14, 2026">${P.citations} citation${P.citations === 1 ? '' : 's'}</span>`,
      P.lab ? `<span title="${esc(P.lab_name || P.lab)}">${esc(P.lab)}</span>` : '',
    ].filter(Boolean).join('<span class="sep">·</span>');
    app.innerHTML = `
      <section class="card paper-card">
        <div class="paper-bar">
          <nav class="crumbs"><a href="${state.home === 'analysis' ? '#/analysis' : '#/'}">${state.home === 'analysis' ? 'Analysis' : 'Leaderboard'}</a><span>›</span>
            <span>${esc(S.model.label)}</span><span>›</span><span>P${S.paper}</span>
            <span class="tag" title="Wall-clock time of the agent's run (all attempts, including waiting and experiments)">run time ${fmtHours(S.s.hours)}</span>
            ${S.s.pdf ? `<a class="tag link" href="#/papers/${encodeURIComponent(S.dir)}/${S.paper}">Read the paper</a>` : '<span class="tag" title="No proposal/report.pdf">no paper submitted</span>'}</nav>
          <div class="controls">
            <select class="select" id="agent-select" aria-label="Agent">
              ${withPaper.map((m) => `<option value="${esc(m.dir)}" ${m.dir === S.dir ? 'selected' : ''}>${esc(m.label)}</option>`).join('')}
            </select>
            ${S.reps.length > 1 ? `<select class="select" id="rep-select" aria-label="Judgment">
              <option value="m" ${S.j === 'm' ? 'selected' : ''}>Majority of ${S.reps.length} judgments</option>
              ${S.reps.map((r) => `<option value="${r}" ${r === S.j ? 'selected' : ''}>Judgment ${r} of ${S.reps.length}</option>`).join('')}
            </select>` : '<span class="tag" title="This submission was judged once">1 judgment</span>'}
            <button class="nav-btn" id="prev-paper" ${prev ? '' : 'disabled'} title="Previous paper">${prev ? `← P${prev}` : '← Prev'}</button>
            <button class="nav-btn" id="next-paper" ${next ? '' : 'disabled'} title="Next paper">${next ? `P${next} →` : 'Next →'}</button>
          </div>
        </div>
        <div class="pmeta"><span class="pid">P${S.paper}</span>${meta}</div>
        <div class="ptitle">${esc(P.title)}</div>
        <div class="followup"><div class="lab">Follow-up direction proposed by the domain scientist</div><p>${esc(P.direction)}</p></div>
      </section>
      <div class="browser">
        <aside class="card tree-pane">
          <div class="side-tabs" role="tablist" aria-label="Sidebar">
            <button type="button" role="tab" data-side="nodes">Rubric nodes</button>
            <button type="button" role="tab" data-side="files">Files${S.files ? ` <span>${fmtCount(S.files.files)}</span>` : ''}</button>
          </div>
          <div class="tree-tools" id="side-tools"></div>
          <div class="tree" id="tree" role="tree"></div>
          <div class="side-foot" id="side-foot"></div>
        </aside>
        <section class="card detail-pane" id="detail"></section>
      </div>`;
    const go = (dir, paper, j) => { location.hash = `#/s/${encodeURIComponent(dir)}/${paper}/${j}`; };
    document.getElementById('agent-select').addEventListener('change', (e) => go(e.target.value, S.paper, 'm'));
    document.getElementById('rep-select')?.addEventListener('change', (e) => { location.hash = subHash(S, state.sel, e.target.value); });
    document.getElementById('prev-paper').addEventListener('click', () => { if (prev) go(S.dir, prev, S.j); });
    document.getElementById('next-paper').addEventListener('click', () => { if (next) go(S.dir, next, S.j); });
    app.querySelectorAll('[data-side]').forEach((b) => b.addEventListener('click', () => { state.side = b.dataset.side; renderSide(); }));
  }

  function renderSide() {
    app.querySelectorAll('[data-side]').forEach((b) => b.setAttribute('aria-selected', String(b.dataset.side === state.side)));
    renderSideTools();
    renderTree();
    scrollTreeToSelection();
  }

  function renderSideTools() {
    const S = state.sub;
    const files = state.side === 'files';
    const box = document.getElementById('side-tools');
    const only = files ? [['0', 'All files', !state.files.citedOnly], ['1', 'Cited files', state.files.citedOnly]]
      : [['0', 'All nodes', !state.tree.failOnly], ['1', 'Failures only', state.tree.failOnly]];
    box.innerHTML = `
      <div class="row"><label class="search">${ICON.search}<input id="q" type="search" placeholder="${files ? 'Search files…' : 'Search nodes, grades, reasoning…'}"
        value="${esc(files ? state.files.q : state.tree.q)}" aria-label="Search"></label></div>
      <div class="row">
        <div class="segmented" role="group" aria-label="Show">${only.map(([k, l, on]) => `<button type="button" data-only="${k}" aria-pressed="${on}">${l}</button>`).join('')}</div>
        <span style="margin-left:auto"></span>
        <button class="text-btn" id="expand" type="button">Expand</button><button class="text-btn" id="collapse" type="button">Collapse</button>
      </div>
      ${files && S.files ? `<div class="row fsum">${fmtCount(S.files.files)} files · ${fmtBytes(S.files.bytes)}, as the judge saw them</div>` : ''}`;
    const q = document.getElementById('q');
    q.addEventListener('input', () => { if (files) state.files.q = q.value; else state.tree.q = q.value; renderTree(); });
    box.querySelectorAll('[data-only]').forEach((b) => b.addEventListener('click', () => {
      if (files) state.files.citedOnly = b.dataset.only === '1'; else state.tree.failOnly = b.dataset.only === '1';
      box.querySelectorAll('[data-only]').forEach((x) => x.setAttribute('aria-pressed', String(x === b)));
      renderTree();
    }));
    document.getElementById('expand').addEventListener('click', () => {
      if (files) walkFiles(S.files?.tree || [], '', (path, e) => { if (!Array.isArray(e) && Array.isArray(e.k)) state.files.open.add(path); });
      else { state.tree.closed.clear(); state.tree.rootClosed.clear(); }
      renderTree();
    });
    document.getElementById('collapse').addEventListener('click', () => {
      if (files) state.files.open = new Set();
      else S.roots.forEach((r) => r.sections.forEach((x) => state.tree.closed.add(x.key)));
      renderTree();
    });
  }

  const verdictOf = (n) => (n.repeat != null ? state.sub.verdicts[n.repeat]?.[n.src]?.nodes?.[n.leaf.id] : null) || null;
  const evidenceOf = (v) => (Array.isArray(v?.evidence) ? v.evidence : v?.evidence ? [v.evidence] : []).map(String);

  // ---------------------------------------------------------------- submission browser: cited files
  function citeTargets(p) {
    const c = state.sub?.files?.cites?.[p];
    if (!c) return [];
    if (c[0] === 'f') return [c[2] || p];
    if (c[0] === 'd') return [p];
    if (c[0] === 'g') return c[2];
    return [];
  }
  const nodeTargets = (n) => [...new Set(evidenceOf(verdictOf(n)).flatMap((e) => [...e.matchAll(CITE_RE)].flatMap((m) => citeTargets(normCite(m[0])))))];

  function buildCiteIndex() {
    const idx = new Map();
    for (const n of state.sub.nodes) {
      for (const t of nodeTargets(n)) {
        if (!idx.has(t)) idx.set(t, []);
        idx.get(t).push(n);
      }
    }
    return idx;
  }

  function citeChip(raw) {
    const text = raw.replace(/[./]+$/, '');
    const tail = raw.slice(text.length);
    const p = normCite(raw);
    const c = state.sub?.files?.cites?.[p];
    if (!c) return `<code>${esc(text)}</code>${esc(tail)}`;
    let cls; let tip;
    if (c[0] === 'f') { cls = 'file'; tip = `In the submission · ${fmtBytes(c[1])}${c[2] && c[2] !== p ? ` · a field of ${c[2]}` : ''}`; }
    else if (c[0] === 'd') { cls = 'dir'; tip = `Folder in the submission · ${fmtCount(c[1])} files · ${fmtBytes(c[2])}`; }
    else if (c[0] === 'g' && c[1]) { cls = 'glob'; tip = `Pattern · ${fmtCount(c[1])} matching ${c[1] === 1 ? 'entry' : 'entries'} in the submission`; }
    else { cls = 'missing'; tip = c[0] === 'g' ? 'Pattern · nothing in the submission matches it' : "Not found among the submission's files"; }
    const icon = cls === 'dir' ? ICON.folder : cls === 'missing' ? ICON.missing : ICON.file;
    const targets = citeTargets(p);
    return (targets.length
      ? `<button type="button" class="fchip ${cls}" data-reveal="${esc(JSON.stringify(targets))}" title="${esc(`${tip} · click to show it in Files`)}">${icon}${esc(text)}</button>`
      : `<span class="fchip ${cls}" title="${esc(tip)}">${icon}${esc(text)}</span>`) + esc(tail);
  }
  function citeHtml(text) {
    const t = String(text ?? '');
    let out = '';
    let last = 0;
    for (const m of t.matchAll(CITE_RE)) {
      out += esc(t.slice(last, m.index)) + citeChip(m[0]);
      last = m.index + m[0].length;
    }
    return out + esc(t.slice(last));
  }

  function walkFiles(entries, base, fn) {
    for (const e of entries) {
      if (e.more) continue;
      const path = base ? `${base}/${Array.isArray(e) ? e[0] : e.n}` : (Array.isArray(e) ? e[0] : e.n);
      fn(path, e);
      if (!Array.isArray(e) && Array.isArray(e.k)) walkFiles(e.k, path, fn);
    }
  }
  const openAncestors = (path) => { const parts = path.split('/'); for (let i = 1; i < parts.length; i += 1) state.files.open.add(parts.slice(0, i).join('/')); };

  function revealFiles(targets) {
    const st = state.files;
    st.sel = new Set(targets);
    targets.forEach(openAncestors);
    st.q = '';
    st.citedOnly = false;
    state.side = 'files';
    renderSide();
  }

  function renderFiles() {
    const S = state.sub;
    const st = state.files;
    const tree = document.getElementById('tree');
    const foot = document.getElementById('side-foot');
    if (!S.files) { tree.innerHTML = '<div class="t-empty">No file snapshot for this submission.</div>'; foot.innerHTML = ''; return; }
    const selNode = S.nodes.find((n) => n.key === state.sel);
    const hits = new Set(selNode ? nodeTargets(selNode) : []);
    if (st.autoFor !== state.sel) { st.autoFor = state.sel; hits.forEach(openAncestors); }
    const q = st.q.trim().toLowerCase();
    let show = null;
    if (q || st.citedOnly) {
      show = new Set();
      walkFiles(S.files.tree, '', (path) => {
        if ((!q || path.toLowerCase().includes(q)) && (!st.citedOnly || S.citeIndex.has(path))) {
          const parts = path.split('/');
          for (let i = 1; i <= parts.length; i += 1) show.add(parts.slice(0, i).join('/'));
        }
      });
    }
    const rows = [];
    const count = (cites) => {
      if (!cites.length) return '';
      const f = cites.filter((n) => n.kind === 'fail').length;
      const p = cites.filter((n) => n.kind === 'pass').length;
      const o = cites.length - f - p;
      return `<span class="cnt" title="Cited as evidence by ${cites.length} node${cites.length === 1 ? '' : 's'}: click to list them">${f ? `<span class="s-fail">✕ ${f}</span>` : ''}${p ? `<span class="s-pass">✓ ${p}</span>` : ''}${o ? `<span class="s-na">${o}</span>` : ''}</span>`;
    };
    const emit = (entries, base, d) => {
      for (const e of entries) {
        if (e.more) {
          const [fc, fb, dc, dfc, dfb] = e.more;
          if (!show && (fc || dc)) {
            rows.push(`<div class="f-row more" style="--d:${d}"><span class="chev"></span><span class="lbl">${[fc ? `${fmtCount(fc)} more file${fc === 1 ? '' : 's'} (${fmtBytes(fb)})` : '',
              dc ? `${fmtCount(dc)} more folder${dc === 1 ? '' : 's'} (${fmtCount(dfc)} files, ${fmtBytes(dfb)})` : ''].filter(Boolean).join(' · ')} not listed</span></div>`);
          }
          continue;
        }
        const isFile = Array.isArray(e);
        const name = isFile ? e[0] : e.n;
        const path = base ? `${base}/${name}` : name;
        if (show && !show.has(path)) continue;
        const cites = S.citeIndex.get(path) || [];
        const cls = `${hits.has(path) ? 'hit' : ''} ${st.sel.has(path) ? 'picked' : ''} ${cites.length ? 'cited' : ''}`;
        if (isFile) {
          rows.push(`<div class="f-row file ${cls}" data-fpath="${esc(path)}" style="--d:${d}" title="${esc(path)}"><span class="chev"></span>${ICON.file}
            <span class="lbl">${esc(name)}</span><span class="fmeta">${fmtBytes(e[1])}</span>${count(cites)}</div>`);
        } else {
          const expandable = Array.isArray(e.k);
          const open = expandable && (show ? true : st.open.has(path));
          rows.push(`<div class="f-row dir ${cls} ${open ? '' : 'collapsed'} ${expandable ? '' : 'flat'}" data-fdir="${esc(path)}" style="--d:${d}"
            title="${esc(expandable ? path : `${path}: too many files to list individually`)}"><span class="chev">${expandable ? '▾' : ''}</span>${ICON.folder}
            <span class="lbl">${esc(name)}</span><span class="fmeta">${fmtCount(e.c)} file${e.c === 1 ? '' : 's'} · ${fmtBytes(e.b)}</span>${count(cites)}</div>`);
          if (st.showFor.has(path) && cites.length) rows.push(citeRows(cites, d + 1));
          if (open) emit(e.k, path, d + 1);
          continue;
        }
        if (st.showFor.has(path) && cites.length) rows.push(citeRows(cites, d + 1));
      }
    };
    const citeRows = (cites, d) => cites.map((n) => `<div class="f-cite ${n.key === state.sel ? 'selected' : ''}" data-cnode="${esc(n.key)}" style="--d:${d}" title="${esc(n.leaf.question || n.leaf.id)}">
      <span class="ico ${n.kind}">${n.kind === 'opt' ? (n.info.status === 'pass' ? '✓' : '✕') : KIND[n.kind].icon}</span><span class="lbl">${esc(n.label)}</span>
      <span class="mini">${SUB[n.gate] || ''}</span></div>`).join('');
    emit(S.files.tree, '', 0);
    tree.innerHTML = rows.join('') || '<div class="t-empty">No matching files</div>';
    tree.querySelectorAll('[data-fdir]').forEach((row) => row.addEventListener('click', (ev) => {
      const path = row.dataset.fdir;
      if (ev.target.closest('.cnt')) { if (st.showFor.has(path)) st.showFor.delete(path); else st.showFor.add(path); }
      else if (!row.classList.contains('flat')) { if (st.open.has(path)) st.open.delete(path); else st.open.add(path); }
      renderFiles();
    }));
    tree.querySelectorAll('[data-fpath]').forEach((row) => row.addEventListener('click', () => {
      const path = row.dataset.fpath;
      if (st.showFor.has(path)) st.showFor.delete(path); else st.showFor.add(path);
      renderFiles();
    }));
    tree.querySelectorAll('[data-cnode]').forEach((row) => row.addEventListener('click', () => select(row.dataset.cnode, false)));
    const om = S.files.omitted || [];
    const ex = Object.entries(S.files.excluded || {});
    foot.innerHTML = om.length || ex.length ? `<b>Not shown to the judge:</b> ${om.slice(0, 6).map((o) => `${esc(o.path)} <span class="faint">(${esc(o.reason)})</span>`).join(', ')}${om.length > 6 ? `, and ${om.length - 6} more` : ''}${
      ex.length ? `${om.length ? '; ' : ''}environment and cache folders ${ex.slice(0, 6).map(([k, c]) => `${esc(k)} ×${c}`).join(', ')}${ex.length > 6 ? ', …' : ''}` : ''}.` : '';
  }

  // ---------------------------------------------------------------- submission browser: rubric nodes
  function visibleNode(n) {
    const t = state.tree;
    if (t.failOnly && n.kind !== 'fail') return false;
    if (t.q) {
      const hay = `${n.leaf.id} ${n.label} ${n.leaf.question || ''} ${fmtValue(n.value)} ${verdictOf(n)?.reason || ''}`.toLowerCase();
      if (!hay.includes(t.q.toLowerCase())) return false;
    }
    return true;
  }

  function renderTree() {
    if (state.side === 'files') { renderFiles(); return; }
    document.getElementById('side-foot').innerHTML = '';
    const S = state.sub;
    const t = state.tree;
    const count = (ns) => {
      const f = ns.filter((n) => n.kind === 'fail').length;
      const p = ns.filter((n) => n.kind === 'pass').length;
      return `<span class="cnt">${f ? `<span class="s-fail" title="${f} failed">✕ ${f}</span>` : ''}${p ? `<span class="s-pass" title="${p} passed">✓ ${p}</span>` : ''}</span>`;
    };
    const mini = (n) => {
      if (n.kind === 'opt') return '<span class="mini" title="Optional: not counted toward the gate">optional</span>';
      if (n.kind === 'na') return '<span class="mini" title="Does not apply to this submission">N/A</span>';
      if (n.kind === 'diag') return '<span class="mini" title="Diagnostic: recorded, not scored">diagnostic</span>';
      if (n.assigned && n.assigned !== n.gate) return `<span class="mini" title="${esc(`${GATE_NAME[n.assigned]} node counted under ${GATE_NAME[n.gate] || n.gate}: the grade it received means the item is missing`)}">from ${SUB[n.assigned] || n.assigned}</span>`;
      return '';
    };
    const html = S.roots.map((r) => {
      const ns = r.sections.flatMap((x) => x.nodes);
      const secs = r.sections.map((x) => {
        const vis = x.nodes.filter(visibleNode);
        if (!vis.length) return '';
        const closed = !t.q && t.closed.has(x.key);
        return `<div class="t-sec ${closed ? 'closed' : ''}">
          <div class="t-row ${closed ? 'collapsed' : ''}" data-sec="${esc(x.key)}"><span class="chev">▾</span><span class="lbl">${esc(x.title)}</span>${count(x.nodes)}</div>
          <div class="t-kids">${vis.map((n) => `<div class="t-row t-leaf ${n.kind} ${n.key === state.sel ? 'selected' : ''}" data-node="${esc(n.key)}" role="treeitem"
            title="${esc(n.leaf.question || n.leaf.id)}"><span class="ico ${n.kind}">${n.kind === 'opt' ? (n.info.status === 'pass' ? '✓' : '✕') : KIND[n.kind].icon}</span>
            <span class="lbl">${esc(n.label)}</span>${mini(n)}</div>`).join('')}</div></div>`;
      }).join('');
      const rclosed = t.rootClosed.has(r.gate);
      return `<div class="t-root ${rclosed ? 'closed' : ''}">
        <div class="t-row ${r.cls} ${rclosed ? 'collapsed' : ''}" data-root="${esc(r.gate)}"><span class="chev">▾</span>${ICON[r.cls]}
          <span class="root-lbl"><b>${esc(GATE_NAME[r.gate] || r.gate)}<span class="g">${SUB[r.gate] || ''}</span></b><small>${esc(r.sub)}</small></span>${count(ns)}</div>
        <div class="t-secs">${secs || '<div class="t-empty">No matching nodes</div>'}</div></div>`;
    }).join('');
    const tree = document.getElementById('tree');
    tree.innerHTML = html;
    tree.querySelectorAll('[data-root]').forEach((row) => row.addEventListener('click', () => {
      const k = row.dataset.root;
      if (t.rootClosed.has(k)) t.rootClosed.delete(k); else t.rootClosed.add(k);
      renderTree();
    }));
    tree.querySelectorAll('[data-sec]').forEach((row) => row.addEventListener('click', () => {
      const k = row.dataset.sec;
      if (t.closed.has(k)) t.closed.delete(k); else t.closed.add(k);
      renderTree();
    }));
    tree.querySelectorAll('[data-node]').forEach((row) => row.addEventListener('click', () => select(row.dataset.node, false)));
  }

  function scrollTreeToSelection() {  // scroll only the tree pane, never the page
    const tree = document.getElementById('tree');
    const el = state.side === 'files'
      ? tree?.querySelector('.f-row.picked') || tree?.querySelector('.f-row.hit')
      : tree?.querySelector(`[data-node="${CSS.escape(state.sel || '')}"]`);
    if (!el) return;
    const t = tree.getBoundingClientRect();
    const r = el.getBoundingClientRect();
    if (r.top < t.top) tree.scrollTop -= t.top - r.top + 8;
    else if (r.bottom > t.bottom) tree.scrollTop += r.bottom - t.bottom + 8;
  }

  function select(key, reveal) {
    const S = state.sub;
    const n = S.nodes.find((x) => x.key === key);
    if (!n) return;
    state.sel = key;
    if (state.side === 'files') {
      state.files.sel = new Set();
      renderFiles();
    } else {
      if (reveal) {
        let changed = state.tree.closed.delete(n.sec.key);
        changed = state.tree.rootClosed.delete(n.root.gate) || changed;
        if (!visibleNode(n)) {
          state.tree.failOnly = false; state.tree.q = ''; changed = true;
          renderSideTools();
        }
        if (changed) renderTree();
      }
      document.querySelectorAll('#tree .t-leaf.selected').forEach((el) => el.classList.remove('selected'));
      document.querySelector(`#tree [data-node="${CSS.escape(key)}"]`)?.classList.add('selected');
    }
    scrollTreeToSelection();
    renderNode();
    history.replaceState(null, '', subHash(S, key));
  }

  function renderNode() {
    const S = state.sub;
    const n = S.nodes.find((x) => x.key === state.sel);
    const pane = document.getElementById('detail');
    if (!n) { pane.innerHTML = '<p class="muted">Select a node in the tree.</p>'; return; }
    const v = verdictOf(n);
    const total = S.reps.length;
    const majority = S.j === 'm' && total > 1;
    const classes = classesOf(n.leaf);
    const outcomes = n.pol.outcomes || {};
    const value = n.value == null ? null : String(n.value);
    const votes = new Map((n.votes || []).map((x) => [x.value == null ? null : String(x.value), x.count]));
    const gotKind = n.kind === 'opt' ? (n.info.status === 'pass' ? 'pass' : 'fail') : n.kind;
    const outcomeOfValue = value != null ? outcomes[value] : null;
    const split = majority && value == null && (n.votes || []).length > 1;

    const notes = [];
    if (n.kind === 'fail' && value == null) {
      notes.push(split
        ? `No grade received a majority of the ${total} judgments (${[...votes].map(([k, c]) => `${esc(k ?? 'no answer')} ×${c}`).join(', ')}), so this node counts as failed.`
        : majority ? `None of the ${total} judgments gave a grade for this node (each asked for adjudication), so it counts as failed.`
          : 'The judge gave no grade for this node (it asked for adjudication), so it counts as failed.');
    } else if (n.kind === 'fail' && n.info.status === 'undetermined' && n.info.applicable == null) {
      notes.push("The judge could not decide whether this node applies to this submission, so it counts as failed.");
    } else if (n.kind === 'fail' && outcomeOfValue?.status === 'undetermined') {
      notes.push('The judge found the evidence insufficient to decide, so this node counts as failed.');
    }
    if (n.kind === 'na') notes.push('This node does not apply to this submission (its condition does not hold), so it is not scored.');
    if (n.kind === 'diag') notes.push('Diagnostic node: recorded for analysis, not scored.');
    if (n.kind === 'opt') notes.push(`Optional node: recorded but not counted toward ${SUB[n.gate] || n.gate}.`);
    if (n.assigned && n.assigned !== n.gate && n.kind === 'fail') {
      notes.push(`This ${GATE_NAME[n.assigned]?.toLowerCase() || n.assigned} node counts toward ${SUB[n.gate] || n.gate} (${GATE_NAME[n.gate]?.toLowerCase() || n.gate}) here: the grade it received means the item is missing.`);
    }

    const known = new Set(classes.map((c) => String(c.name)));
    const list = classes.map((c) => {
      const got = value != null && String(c.name) === value;
      const cnt = votes.get(String(c.name));
      return `<li class="${got ? `got ${gotKind}` : ''}"><span class="radio"></span>
        <div class="c-top"><span class="c-name">${esc(c.name)}</span>${majority && cnt ? `<span class="c-votes">${cnt} of ${total}</span>` : ''}</div>
        ${c.description ? `<div class="c-desc">${esc(c.description)}</div>` : ''}
        ${c.criteria ? `<div class="c-crit">${esc(c.criteria)}</div>` : ''}</li>`;
    }).join('');
    const unknown = value != null && !known.has(value)
      ? `<li class="got ${gotKind}"><span class="radio"></span><div class="c-top"><span class="c-name">${esc(value)}</span></div></li>` : '';

    let reasoning;
    if (v) {
      const from = majority ? `<span class="from">judgment ${n.repeat} of ${total}, which gave the majority grade</span>` : '';
      reasoning = `<h3>Judge's reasoning ${from}</h3><p class="reason">${citeHtml(v.reason || 'No reasoning recorded.')}</p>`;
    } else if (split) {
      reasoning = `<h3>Judge's reasoning</h3><p class="muted" style="margin:0">The judgments disagreed. Open one to read its reasoning:</p>
        <div class="pick">${S.reps.map((r) => `<a class="nav-btn" href="${subHash(S, n.key, r)}">Judgment ${r}</a>`).join('')}</div>`;
    } else {
      reasoning = '<h3>Judge\'s reasoning</h3><p class="muted" style="margin:0">No reasoning recorded.</p>';
    }

    const evid = evidenceOf(v);
    const cited = [...new Set(evid.flatMap((e) => [...e.matchAll(CITE_RE)].map((m) => normCite(m[0]))))];
    const missing = cited.filter((p) => { const c = S.files?.cites?.[p]; return c && (c[0] === 'x' || (c[0] === 'g' && !c[1])); }).length;
    const evidence = evid.length ? `<div class="d-sec"><h3>Evidence <span class="from">${cited.length} cited path${cited.length === 1 ? '' : 's'}${
      missing ? ` · <span class="miss">${missing} not found in the submission</span>` : ''}${S.files && cited.length ? ' · click a path to show it among the submission\'s files' : ''}</span></h3>
      <ul class="evidence">${evid.map((e) => `<li>${citeHtml(e)}</li>`).join('')}</ul></div>` : '';

    const failures = S.nodes.filter((x) => x.kind === 'fail');
    const fi = failures.findIndex((x) => x.key === n.key);
    const idx = S.nodes.indexOf(n);
    const after = S.nodes.slice(idx + 1).find((x) => x.kind === 'fail') || null;
    const before = S.nodes.slice(0, idx).reverse().find((x) => x.kind === 'fail') || null;
    const role = n.kind === 'opt' ? 'optional · not counted' : n.kind === 'diag' ? 'diagnostic · not scored' : humanize(n.info.role || '');
    pane.innerHTML = `
      <div class="d-top"><span class="status s-${n.kind === 'pass' || n.kind === 'fail' ? n.kind : ''} ${n.kind}">${KIND[n.kind].badge}</span>${rubBadge(n.src)}
        <span class="tag">${SUB[n.gate] || esc(n.gate)} · ${esc(GATE_NAME[n.gate] || '')}</span>
        ${role ? `<span class="tag">${esc(role)}</span>` : ''}
        ${n.kind === 'fail' && n.info.status === 'undetermined' ? `<span class="tag warn">${split ? 'judgments split' : 'judge couldn\'t decide'} → counted as failed</span>` : ''}</div>
      <div class="d-path">${esc(RUB[n.src])} › ${esc(n.sec.title)}</div>
      <h2>${esc(n.leaf.question || n.leaf.id)}</h2>
      <div class="d-id">${esc(n.leaf.id)}</div>
      <div class="d-sec"><h3>Grades</h3>
        ${notes.map((x) => `<div class="note ${n.kind === 'fail' ? 'bad' : ''}">${x}</div>`).join('')}
        <ul class="classes">${list}${unknown}</ul></div>
      <div class="d-sec">${reasoning}</div>
      ${evidence}
      <div class="d-nav">
        <button class="nav-btn" id="prev-fail" ${before ? '' : 'disabled'}>← Previous failure</button>
        <span class="pos">${failures.length ? (fi >= 0 ? `Failure ${fi + 1} of ${failures.length}` : `${failures.length} failures`) : 'No failures'}
          · <kbd>j</kbd>/<kbd>k</kbd> next/previous failure · <kbd>↑</kbd>/<kbd>↓</kbd> move</span>
        <button class="nav-btn" id="next-fail" ${after ? '' : 'disabled'}>Next failure →</button>
      </div>`;
    document.getElementById('prev-fail').addEventListener('click', () => before && select(before.key, true));
    document.getElementById('next-fail').addEventListener('click', () => after && select(after.key, true));
    pane.querySelectorAll('[data-reveal]').forEach((b) => b.addEventListener('click', () => revealFiles(JSON.parse(b.dataset.reveal))));
  }

  function stepFailure(dir) {
    const S = state.sub;
    const i = S.nodes.findIndex((x) => x.key === state.sel);
    const seq = dir > 0 ? S.nodes.slice(i + 1) : S.nodes.slice(0, Math.max(i, 0)).reverse();
    const n = seq.find((x) => x.kind === 'fail');
    if (n) select(n.key, true);
  }
  function stepVisible(dir) {
    if (state.side === 'files') {
      const S = state.sub;
      const n = S.nodes[S.nodes.findIndex((x) => x.key === state.sel) + dir];
      if (n) select(n.key, false);
      return;
    }
    const rows = [...document.querySelectorAll('#tree [data-node]')];
    const i = rows.findIndex((r) => r.dataset.node === state.sel);
    const r = rows[i + dir];
    if (r) select(r.dataset.node, false);
  }

  // ---------------------------------------------------------------- routing
  async function route() {
    const parts = location.hash.replace(/^#\/?/, '').split('/').filter(Boolean);
    try {
      if (parts[0] === 's' && parts.length >= 3) {
        const j = parts[3] && /^\d+$/.test(parts[3]) ? Number(parts[3]) : 'm';
        await renderSubmission(decodeURIComponent(parts[1]), Number(parts[2]), j,
          parts[4] ? decodeURIComponent(parts.slice(4).join('/')) : null);
      } else {
        if (parts[0] === 'papers') state.pp = { agent: parts[1] ? decodeURIComponent(parts[1]) : state.pp.agent, paper: Number(parts[2]) || state.pp.paper };
        renderHome(['submit', 'analysis', 'directions', 'papers'].includes(parts[0]) ? parts[0] : 'leaderboard');
      }
    } catch (e) { showError(e); }
  }

  function showError(e) {
    const local = location.protocol === 'file:';
    hero.hidden = true;
    app.innerHTML = `<div class="card error"><h2>Could not load the judgments</h2>
      <p class="muted">${esc(e.message || e)}</p>
      ${local ? '<p>Browsers block reading local files from a page opened directly. From the repository root run <code>python viewer/serve.py</code>.</p>' : ''}</div>`;
  }

  document.addEventListener('keydown', (e) => {
    const active = document.activeElement;
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(active?.tagName || '');
    if (e.key === 'Escape' && active?.id === 'q') { state.tree.q = ''; active.value = ''; renderTree(); active.blur(); return; }
    if (typing || e.metaKey || e.ctrlKey || e.altKey || !state.sub) return;
    if (e.key === '/') { e.preventDefault(); document.getElementById('q')?.focus(); }
    else if (e.key === 'j') stepFailure(1);
    else if (e.key === 'k') stepFailure(-1);
    else if (e.key === 'ArrowDown') { e.preventDefault(); stepVisible(1); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); stepVisible(-1); }
  });

  async function main() {
    initTheme();
    const qs = new URLSearchParams(location.search);
    if (qs.get('side') === 'files') state.side = 'files';
    if (qs.has('submitted')) {   // back from FormSubmit after a submission
      state.submitted = true;
      qs.delete('submitted');
      history.replaceState(null, '', `${location.pathname}${qs.toString() ? `?${qs}` : ''}#/submit`);
    }
    try {
      state.index = await getJSON(`${DATA}/index.json`);
      if (!state.index.leaderboard?.submissions?.[0]?.nodes) throw new Error('index.json lacks the viewer data; re-run the export.');
    } catch (e) { showError(e); return; }
    window.addEventListener('hashchange', route);
    route();
  }
  main();
})();
