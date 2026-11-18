// Synthesis of a postgres-like identifier is not used; all ids come from the API.
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const state = {
  observations: [],
  planVersions: [],
  latestPlan: null,
  planDetail: null,
  selectedPlanId: null,
  runs: [],
  currentReport: null,
  fault: { enabled: false, stage: 'summarise' },
};

// --------------------------------------------------------------------- utils

function fmt(v, digits = 2) {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  return Number(v).toFixed(digits);
}

function pValue(v) {
  if (v === null || v === undefined) return '—';
  if (v < 0.0001) return '<0.0001';
  return Number(v).toFixed(4);
}

function signed(v, digits = 2) {
  if (v === null || v === undefined) return '—';
  const n = Number(v);
  return `${n > 0 ? '+' : n < 0 ? '−' : ''}${Math.abs(n).toFixed(digits)}`;
}

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

async function api(path, options = {}) {
  const init = { method: options.method || 'GET', headers: {} };
  if (options.body !== undefined) {
    init.headers['content-type'] = 'application/json';
    init.body = JSON.stringify(options.body);
  }
  const res = await fetch(path, init);
  let payload = null;
  try { payload = await res.json(); } catch { payload = null; }
  if (!res.ok) {
    const detail = payload && payload.detail ? payload.detail : `request failed (${res.status})`;
    const err = new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
    err.status = res.status;
    err.payload = payload;
    throw err;
  }
  return payload;
}

function showError(el, message) {
  if (!message) { el.hidden = true; el.textContent = ''; return; }
  el.hidden = false;
  el.textContent = message;
}

function showBanner(el, message, tone = '') {
  if (!message) { el.hidden = true; return; }
  el.hidden = false;
  el.className = `banner ${tone}`.trim();
  el.textContent = message;
}

let bannerTimer = null;
function flash(message, tone = '') {
  const el = $('#global-banner');
  showBanner(el, message, tone);
  clearTimeout(bannerTimer);
  bannerTimer = setTimeout(() => { el.hidden = true; }, 6000);
}

// ------------------------------------------------------------------- tooltip

const tooltipEl = $('#tooltip');
function bindTooltip(target, html) {
  const show = (event) => {
    tooltipEl.innerHTML = html;
    tooltipEl.classList.add('show');
    positionTip(event);
  };
  const move = (event) => positionTip(event);
  const hide = () => tooltipEl.classList.remove('show');
  target.addEventListener('mouseenter', show);
  target.addEventListener('mousemove', move);
  target.addEventListener('mouseleave', hide);
  target.addEventListener('focus', (event) => show(event));
  target.addEventListener('blur', hide);
  target.setAttribute('tabindex', '0');
}
function positionTip(event) {
  const pad = 14;
  const rect = tooltipEl.getBoundingClientRect();
  let x = (event.clientX ?? 0) + pad;
  let y = (event.clientY ?? 0) + pad;
  if (x + rect.width > window.innerWidth - 8) x = (event.clientX ?? 0) - rect.width - pad;
  if (y + rect.height > window.innerHeight - 8) y = (event.clientY ?? 0) - rect.height - pad;
  tooltipEl.style.left = `${Math.max(8, x)}px`;
  tooltipEl.style.top = `${Math.max(8, y)}px`;
}

// -------------------------------------------------------------------- health

async function loadHealth() {
  const el = $('#health');
  try {
    const data = await api('/api/health');
    el.innerHTML = Object.entries(data).map(([name, value]) => {
      const ok = value && value.ok;
      return `<span class="pill ${ok ? 'ok' : 'bad'}"><span class="dot"></span>${esc(name)}</span>`;
    }).join('');
  } catch {
    el.innerHTML = '<span class="pill bad"><span class="dot"></span>gateway offline</span>';
  }
}

// --------------------------------------------------------------- observations

async function loadObservations() {
  const data = await api('/obs/observations');
  state.observations = data.observations;
  $('#obs-fingerprint').textContent = `feed v${data.version} · ${data.fingerprint.slice(0, 16)}…`;
  renderObsKpis();
  renderObsTable();
}

function currentMeasurement(obs, outcome) {
  const revs = obs.measurements[outcome] || [];
  return revs.length ? revs[revs.length - 1] : null;
}

function renderObsKpis() {
  const obs = state.observations;
  const romReported = obs.filter((o) => currentMeasurement(o, 'rom_week12')?.status === 'reported').length;
  const corrected = obs.filter((o) => {
    const revs = o.measurements.rom_week12 || [];
    return revs.length > 1 && revs[revs.length - 1].status === 'reported';
  }).length;
  const withdrawn = obs.filter((o) => currentMeasurement(o, 'rom_week12')?.status === 'withdrawn').length;
  const arms = obs.reduce((acc, o) => { acc[o.arm] = (acc[o.arm] || 0) + 1; return acc; }, {});
  $('#obs-kpis').innerHTML = [
    ['Registered', obs.length, `${arms.intervention || 0} intervention · ${arms.control || 0} control`],
    ['ROM reported', romReported, `${obs.length - romReported} missing at week 12`],
    ['Correction revisions', corrected, 'history preserved'],
    ['Withdrawn after entry', withdrawn, 'now missing, not zero'],
  ].map(([label, value, sub]) => `
    <div class="stat"><div class="label">${esc(label)}</div>
      <div class="value">${esc(value)}</div><div class="sub">${esc(sub)}</div></div>`).join('');
}

function outcomeCell(obs, outcome) {
  const revs = obs.measurements[outcome] || [];
  if (!revs.length) return '<span class="tag">missing</span>';
  const cur = revs[revs.length - 1];
  if (cur.status === 'withdrawn') return '<span class="tag">withdrawn</span>';
  const corrected = revs.length > 1 ? ' <span class="tag orange">rev ' + cur.revision + '</span>' : '';
  return `<span class="tnum">${fmt(cur.value)}</span>${corrected}`;
}

function historyBlock(obs, outcome) {
  const revs = obs.measurements[outcome] || [];
  if (revs.length < 2) return '<span class="muted">—</span>';
  const rows = revs.map((r) => `<li>rev ${r.revision} · ${r.status === 'reported' ? fmt(r.value) : 'withdrawn'} · ${esc(r.note || 'no note')}</li>`).join('');
  return `<details class="history"><summary>${revs.length} revisions</summary><ul>${rows}</ul></details>`;
}

function renderObsTable() {
  const site = $('#filter-site').value;
  const arm = $('#filter-arm').value;
  const missing = $('#filter-missing').value;
  const body = $('#obs-table tbody');
  const rows = state.observations.filter((o) => {
    if (site && o.site !== site) return false;
    if (arm && o.arm !== arm) return false;
    if (missing === 'missing_rom' && currentMeasurement(o, 'rom_week12')?.status === 'reported') return false;
    if (missing === 'missing_rom' && !currentMeasurement(o, 'rom_week12')) return true;
    if (missing === 'corrected' && (o.measurements.rom_week12 || []).length < 2) return false;
    if (missing === 'withdrawn' && currentMeasurement(o, 'rom_week12')?.status !== 'withdrawn') return false;
    return true;
  });
  body.innerHTML = rows.map((o) => `
    <tr>
      <td class="mono">${esc(o.subject_code)}</td>
      <td>${esc(o.site)} · ${esc(o.severity_band)}</td>
      <td><span class="tag ${o.arm === 'intervention' ? 'blue' : ''}">${esc(o.arm)}</span></td>
      <td class="num">${fmt(o.baseline_rom, 1)}</td>
      <td>${outcomeCell(o, 'rom_week12')}</td>
      <td>${outcomeCell(o, 'pain_week12')}</td>
      <td>${historyBlock(o, 'rom_week12')}</td>
      <td><button class="btn small ghost" data-record="${esc(o.obs_id)}" data-code="${esc(o.subject_code)}" type="button">Add / correct</button></td>
    </tr>`).join('') || '<tr><td colspan="8"><div class="empty">No participants match this filter.</div></td></tr>';

  $$('[data-record]', body).forEach((btn) => {
    btn.addEventListener('click', () => openRecordDialog(btn.dataset.record, btn.dataset.code));
  });
}

async function openRecordDialog(obsId, code) {
  const outcome = window.prompt(`Outcome for ${code}: type rom_week12 or pain_week12`, 'rom_week12');
  if (!outcome) return;
  if (!['rom_week12', 'pain_week12'].includes(outcome)) { flash('Unknown outcome.', 'bad'); return; }
  const raw = window.prompt(`New value for ${outcome} (leave blank to withdraw)`, '');
  if (raw === null) return;
  try {
    if (raw.trim() === '') {
      const reason = window.prompt('Reason for withdrawing this outcome', 'source verification');
      if (!reason) return;
      await api(`/obs/observations/${obsId}/outcomes/${outcome}/withdraw`, { method: 'POST', body: { reason } });
      flash('Outcome withdrawn; it is now missing, not zero.', 'warn');
    } else {
      const value = Number(raw);
      if (!Number.isFinite(value)) { flash('Value must be a finite number. Use withdraw for missing.', 'bad'); return; }
      const note = window.prompt('Note for this revision', 'clinic remeasurement') || '';
      await api(`/obs/observations/${obsId}/outcomes`, { method: 'POST', body: { outcome, value, note } });
      flash('Outcome recorded as a new revision.', 'good');
    }
    await refreshAll();
  } catch (err) {
    flash(err.message, 'bad');
  }
}

// -------------------------------------------------------------------- pairing

async function loadPlans() {
  const [data, feed] = await Promise.all([api('/pair/plans'), api('/obs/candidates')]);
  state.planVersions = data.versions;
  renderFeedBanner(feed);
  renderPlanVersions();
  const select = $('#an-plan');
  select.innerHTML = data.versions.slice().reverse().map((v) => `
    <option value="${esc(v.plan_version_id)}">v${v.plan_version} · ${esc(v.reason).slice(0, 48)}</option>`).join('');
  if (!data.versions.length) select.innerHTML = '<option value="">no plan yet</option>';
  if (state.selectedPlanId && !data.versions.find((v) => v.plan_version_id === state.selectedPlanId)) {
    state.selectedPlanId = null;
  }
  if (!state.selectedPlanId && data.versions.length) state.selectedPlanId = data.versions[data.versions.length - 1].plan_version_id;
  if (state.selectedPlanId) await loadPlanDetail(state.selectedPlanId);
  else $('#plan-detail').innerHTML = `<div class="empty">No plan yet. Build the first version with the declared policy above.</div>`;
}

function renderFeedBanner(feed) {
  const el = $('#pair-feed-banner');
  const latest = state.planVersions[state.planVersions.length - 1];
  if (!latest) {
    showBanner(el, `Observation feed v${feed.version} · ${feed.candidates.length} participants with covariates only. Build a plan to lock a matching.`, '');
    return;
  }
  if (latest.observation_fingerprint === feed.fingerprint) {
    showBanner(
      el,
      `Covariates are unchanged since plan v${latest.plan_version} was locked. Outcome revisions do not alter the `
      + `pairing; they change what an analysis of each pair finds.`,
      'good',
    );
    return;
  }
  showBanner(
    el,
    `The covariate feed changed after plan v${latest.plan_version} was locked (now ${feed.candidates.length} participants). `
    + `Existing analyses stay bound to the original fingerprint; build a new plan version to pair the current feed.`,
    'warn',
  );
}

function renderPlanVersions() {
  const el = $('#plan-versions');
  if (!state.planVersions.length) {
    el.innerHTML = '<div class="empty">No plan versions.</div>';
    $('#plan-latest-label').textContent = '';
    return;
  }
  el.innerHTML = `<div class="table-wrap"><table><thead><tr>
      <th>Version</th><th>Created</th><th>Reason</th><th class="num">Paired</th><th class="num">Excluded</th><th>Policy</th><th></th>
    </tr></thead><tbody>${state.planVersions.slice().reverse().map((v) => `
      <tr>
        <td><strong>v${v.plan_version}</strong> <span class="tag">locked</span></td>
        <td class="mono">${esc((v.created_at || '').slice(0, 19).replace('T', ' '))}</td>
        <td>${esc(v.reason)}${v.supersedes ? `<br><span class="muted">supersedes ${esc(v.supersedes.slice(-6))}</span>` : ''}</td>
        <td class="num">${v.summary.paired}</td>
        <td class="num">${v.summary.excluded}</td>
        <td class="mono">${esc(v.policy_checksum.slice(0, 10))}…</td>
        <td><button class="btn small ghost" data-plan="${esc(v.plan_version_id)}" type="button">View</button></td>
      </tr>`).join('')}</tbody></table></div>`;
  $$('[data-plan]', el).forEach((btn) => btn.addEventListener('click', async () => {
    state.selectedPlanId = btn.dataset.plan;
    await loadPlanDetail(btn.dataset.plan);
  }));
  const latest = state.planVersions[state.planVersions.length - 1];
  $('#plan-latest-label').textContent = `Latest: v${latest.plan_version}`;
}

async function loadPlanDetail(planId) {
  const plan = await api(`/pair/plans/${planId}`);
  state.planDetail = plan;
  renderPlanDetail(plan);
}

function renderPlanDetail(plan) {
  const s = plan.summary;
  const cells = {};
  plan.pairs.forEach((p) => {
    const key = Object.entries(p.cell).map(([k, v]) => `${k}=${v}`).join(' · ');
    (cells[key] = cells[key] || []).push(p);
  });
  const rows = Object.entries(cells).map(([key, pairs]) => ({ key, pairs }));

  const chart = buildGapChart(rows, plan.policy);

  $('#plan-detail').innerHTML = `
    <div class="card">
      <div class="card-head">
        <h3>Version v${plan.plan_version} · match quality by cell</h3>
        <span class="tag orange">plan locked · ${esc(plan.policy_checksum.slice(0, 10))}…</span>
      </div>
      <div class="row" style="gap:20px; margin-bottom:10px; flex-wrap:wrap;">
        <div class="stat"><div class="label">Paired participants</div><div class="value">${s.paired}</div>
          <div class="sub">of ${s.treated} intervention / ${s.controls} control</div></div>
        <div class="stat"><div class="label">Excluded</div><div class="value">${s.excluded}</div>
          <div class="sub">${Object.entries(s.excluded_by_reason).map(([k, v]) => `${v} ${k.replaceAll('_', ' ')}`).join(' · ') || 'none'}</div></div>
        <div class="stat"><div class="label">Largest within-pair gap</div><div class="value">${fmt(s.max_distance, 1)}°</div>
          <div class="sub">caliper ${fmt(plan.policy.caliper, 1)}°</div></div>
        <div class="stat"><div class="label">Ties broken by seed</div><div class="value">${s.ties_broken}</div>
          <div class="sub">exact ties only</div></div>
      </div>
      ${chart}
      <p class="help" style="margin-top:8px">Each dot is one matched pair, placed by how far apart the two
        participants' baseline ROM is. Dots left of the caliper line are inside the declared limit. Hover or focus a
        dot for the pair's values.</p>
    </div>

    <div class="card pad-0">
      <div class="card-head" style="padding:14px 18px 0; margin-bottom:8px;">
        <h3>Exclusions and their basis</h3>
        <span class="help">${plan.exclusions.length} participant${plan.exclusions.length === 1 ? '' : 's'}</span>
      </div>
      <div class="table-wrap"><table><thead><tr>
        <th>Participant</th><th>Arm</th><th>Cell</th><th>Reason</th><th>Basis</th>
      </tr></thead><tbody>
        ${plan.exclusions.map((e) => `
          <tr><td class="mono">${esc(e.obs_id.slice(-8))}</td><td>${esc(e.arm)}</td>
            <td>${esc(Object.entries(e.cell).map(([k, v]) => `${v}`).join(' · '))}</td>
            <td><span class="tag ${e.reason === 'caliper_exceeded' ? 'orange' : ''}">${esc(e.reason.replaceAll('_', ' '))}</span></td>
            <td>${esc(e.detail)}</td></tr>`).join('') || '<tr><td colspan="5"><div class="empty">No exclusions in this plan.</div></td></tr>'}
      </tbody></table></div>
    </div>

    <div class="card pad-0">
      <div class="card-head" style="padding:14px 18px 0; margin-bottom:8px;">
        <h3>Pairs</h3><span class="help">Nearest eligible control within caliper</span>
      </div>
      <div class="table-wrap"><table><thead><tr>
        <th>Pair</th><th>Cell</th><th class="num">Intervention ROM</th><th class="num">Control ROM</th>
        <th class="num">Gap</th><th>Tie</th>
      </tr></thead><tbody>
        ${plan.pairs.map((p) => `
          <tr><td class="mono">${esc(p.pair_id)}</td>
            <td>${esc(Object.values(p.cell).join(' · '))}</td>
            <td class="num">${fmt(p.treated_value, 1)}</td>
            <td class="num">${fmt(p.control_value, 1)}</td>
            <td class="num">${fmt(p.distance, 2)}°</td>
            <td>${p.tie_broken ? `<span class="tag orange">${p.tie_candidates} tied</span>` : '<span class="muted">—</span>'}</td></tr>`).join('')}
      </tbody></table></div>
    </div>`;
  bindChartHover();
}

function buildGapChart(rows, policy) {
  if (!rows.length) return '<div class="empty">This plan has no complete pairs.</div>';
  const width = 820;
  const margin = { top: 34, right: 128, bottom: 34, left: 178 };
  const innerW = width - margin.left - margin.right;
  const laneH = 40;
  const height = margin.top + margin.bottom + rows.length * laneH;
  const maxGap = Math.max(policy.caliper, ...rows.flatMap((r) => r.pairs.map((p) => p.distance)));
  const domainMax = Math.max(maxGap * 1.06, policy.caliper * 1.08);
  const x = (v) => margin.left + (v / domainMax) * innerW;
  const caliperX = x(policy.caliper);

  let svg = `<div class="chart"><svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Within-pair baseline ROM gap by cell">`;
  const ticks = niceTicks(0, domainMax, 5).filter((t) => Math.abs(t - policy.caliper) > domainMax * 0.045);
  ticks.forEach((t) => {
    svg += `<line class="gridline" x1="${x(t).toFixed(1)}" x2="${x(t).toFixed(1)}" y1="${margin.top - 12}" y2="${height - margin.bottom + 8}"/>`;
    svg += `<text class="tick" x="${x(t).toFixed(1)}" y="${margin.top - 18}" text-anchor="middle">${fmt(t, 0)}°</text>`;
  });
  svg += `<line class="zero-line" x1="${caliperX.toFixed(1)}" x2="${caliperX.toFixed(1)}" y1="${margin.top - 12}" y2="${height - margin.bottom + 8}"/>`;
  svg += `<text class="tick" x="${caliperX.toFixed(1)}" y="${margin.top - 18}" text-anchor="middle" style="font-weight:500">caliper ${fmt(policy.caliper, 1)}°</text>`;

  rows.forEach((row, i) => {
    const laneTop = margin.top + i * laneH;
    const y = laneTop + laneH / 2;
    svg += `<text class="row-label" x="${margin.left - 12}" y="${y + 4}" text-anchor="end">${esc(row.key)}</text>`;
    svg += `<line class="gridline" x1="${margin.left}" x2="${margin.left + innerW}" y1="${laneTop + laneH}" y2="${laneTop + laneH}"/>`;
    row.pairs.forEach((p) => {
      const px = x(p.distance);
      const offsets = [-9, 0, 9, -4.5, 4.5, -6.8, 6.8];
      const py = y + offsets[hashCode(p.pair_id) % offsets.length];
      const near = p.distance >= policy.caliper - 1e-9;
      svg += `<circle cx="${px.toFixed(1)}" cy="${py.toFixed(1)}" r="4.5" fill="${near ? 'var(--series-2)' : 'var(--series-1)'}" stroke="var(--surface)" stroke-width="2"/>`;
      const tip = `<div class="t-title">${esc(p.pair_id)} · ${esc(Object.values(p.cell).join(' · '))}</div>
        <div class="t-row">Within-pair baseline gap: <strong>${fmt(p.distance, 2)}°</strong></div>
        <div class="t-row">Intervention baseline: <strong>${fmt(p.treated_value, 1)}°</strong> · control: <strong>${fmt(p.control_value, 1)}°</strong></div>
        <div class="t-row">Policy caliper: ${fmt(policy.caliper, 1)}°</div>
        ${p.tie_broken ? `<div class="t-row">Exact tie among ${p.tie_candidates} candidates; seed order applied</div>` : ''}`;
      svg += `<circle class="hit" data-tip="${esc(tip)}" cx="${px.toFixed(1)}" cy="${py.toFixed(1)}" r="11"/>`;
    });
  });
  svg += '</svg></div>';
  svg += `<div class="legend">
      <span class="legend-item"><span class="swatch" style="background:var(--series-1)"></span>One matched pair</span>
      <span class="legend-item"><span class="swatch" style="background:var(--series-2)"></span>Gap at the caliper limit</span>
      <span class="legend-item"><span class="swatch line" style="background:var(--ink-2)"></span>Pre-declared caliper (${fmt(policy.caliper, 1)}°)</span>
    </div>`;
  return svg;
}

function hashCode(text) {
  let h = 0;
  for (let i = 0; i < text.length; i += 1) h = (h * 31 + text.charCodeAt(i)) | 0;
  return Math.abs(h);
}

function clamp(v) { return Math.max(0, Math.min(1, v)); }

function bindChartHover() {
  $$('[data-tip]').forEach((el) => bindTooltip(el, el.dataset.tip));
}

// ------------------------------------------------------------------- analysis

async function loadRuns() {
  const data = await api('/report/analyses');
  state.runs = data.runs;
  renderRuns();
  const options = data.runs.filter((r) => r.status === 'complete');
  ['#cmp-a', '#cmp-b'].forEach((sel, i) => {
    const el = $(sel);
    const prev = el.value;
    el.innerHTML = options.map((r) => `<option value="${esc(r.run_id)}">${esc(r.outcome)} · ${esc(r.run_id.slice(-6))} · v? </option>`).join('');
    if (!options.length) el.innerHTML = '<option value="">no completed runs</option>';
    if (prev && options.find((r) => r.run_id === prev)) el.value = prev;
    else if (options.length) el.value = options[Math.max(0, options.length - 1 - i)].run_id;
  });
}

function renderRuns() {
  const el = $('#runs-table');
  if (!state.runs.length) { el.innerHTML = '<div class="empty">No runs yet.</div>'; return; }
  el.innerHTML = `<div class="table-wrap"><table><thead><tr>
      <th>Run</th><th>Status</th><th>Outcome</th><th>Input hash</th><th>Created</th><th></th>
    </tr></thead><tbody>${state.runs.slice().reverse().map((r) => {
      const resumable = ['failed', 'running', 'pending'].includes(r.status);
      return `<tr>
        <td class="mono">${esc(r.run_id)}</td>
        <td>${statusTag(r.status)}</td>
        <td>${esc(r.outcome)}</td>
        <td class="mono muted">${r.input_hash ? esc(r.input_hash.slice(0, 12)) + '…' : '—'}</td>
        <td class="mono">${esc((r.created_at || '').slice(0, 19).replace('T', ' '))}</td>
        <td class="row" style="gap:6px">
          <button class="btn small ghost" data-open="${esc(r.run_id)}" type="button">Open</button>
          ${resumable ? `<button class="btn small" data-resume="${esc(r.run_id)}" type="button">Resume</button>` : ''}
        </td></tr>`;
    }).join('')}</tbody></table></div>`;
  $$('[data-open]', el).forEach((b) => b.addEventListener('click', () => openRun(b.dataset.open)));
  $$('[data-resume]', el).forEach((b) => b.addEventListener('click', () => resumeRun(b.dataset.resume)));
}

function statusTag(status) {
  const cls = status === 'complete' ? 'blue' : status === 'failed' ? 'orange' : '';
  const label = { complete: 'complete', failed: 'interrupted', running: 'running', pending: 'pending', stale: 'inputs changed' }[status] || status;
  return `<span class="tag ${cls}">${esc(label)}</span>`;
}

async function openRun(runId) {
  const run = await api(`/report/analyses/${runId}`);
  if (run.report) {
    state.currentReport = run.report;
    renderReport(run.report, run);
  } else {
    $('#analysis-result').innerHTML = `<div class="banner warn">Run ${esc(runId)} is ${esc(run.status)}
      ${run.error ? `— ${esc(run.error)}` : ''}.
      ${['failed', 'running', 'pending'].includes(run.status) ? `<button class="linkish" data-resume="${esc(runId)}" type="button">Resume from checkpoints</button>` : ''}</div>`;
    const b = $('[data-resume]', $('#analysis-result'));
    if (b) b.addEventListener('click', () => resumeRun(runId));
  }
}

async function resumeRun(runId) {
  try {
    const run = await api(`/report/analyses/${runId}/resume`, { method: 'POST', body: { note: 'resume from console' } });
    flash(`Run resumed: ${(run.report?.resumability?.reused_stages || []).length} checkpoint(s) reused.`, 'good');
    await loadRuns();
    if (run.report) renderReport(run.report, run);
    await loadHealth();
  } catch (err) {
    flash(err.message, 'bad');
  }
}

function renderReport(report, run) {
  const cc = report.analysis.complete_case;
  const sens = report.analysis.sensitivity || [];
  const bounded = sens.find((s) => s.scenario === 'bounded_worst_best');
  const tipped = sens.find((s) => s.scenario === 'tipped_to_null');
  const mi = sens.find((s) => s.scenario === 'imputed_mi');
  const unit = report.outcome_spec.unit;
  const excluded = report.plan.exclusions.length;

  const layerChart = buildLayeredChart(cc, bounded, unit);
  const resumability = (run && run.resumability) || report.resumability || { reused_stages: [] };

  $('#analysis-result').innerHTML = `
    <div class="card">
      <div class="card-head">
        <h3>${esc(report.outcome_spec.label)} · favourable difference (intervention − control)</h3>
        <span class="row" style="gap:6px">
          <span class="tag blue">${esc(report.outcome)}</span>
          <span class="tag">plan v${report.plan.plan_version}</span>
          ${resumability.reused_stages.length ? `<span class="tag orange">resumed · ${resumability.reused_stages.length} stages reused</span>` : ''}
        </span>
      </div>

      <div class="hero">
        <div class="value tnum">${signed(cc.mean_difference)}</div>
        <div class="unit">${esc(unit)} per pair · complete-case estimate</div>
        <div class="ci">${(cc.ci_level * 100).toFixed(0)}% CI ${signed(cc.ci_low)} to ${signed(cc.ci_high)}
          · n = ${cc.n} complete pairs${cc.p_value !== null ? ` · p ${pValue(cc.p_value)}` : ''}</div>
      </div>

      ${layerChart}

      <div class="kpi-row" style="margin-top:14px">
        <div class="stat"><div class="label">Pairs in plan</div><div class="value">${cc.counts.pairs_in_plan}</div>
          <div class="sub">${excluded} plan-level exclusion${excluded === 1 ? '' : 's'}</div></div>
        <div class="stat"><div class="label">Complete pairs</div><div class="value">${cc.n}</div>
          <div class="sub">${(report.coverage.coverage * 100).toFixed(0)}% of plan</div></div>
        <div class="stat"><div class="label">Pairs excluded for missing outcome</div><div class="value">${cc.counts.excluded_pairs}</div>
          <div class="sub">listed below, never zero-filled</div></div>
        <div class="stat"><div class="label">Sensitivity analyses</div><div class="value">${sens.length}</div>
          <div class="sub">assumption-bound, reported separately</div></div>
      </div>
    </div>

    <div class="card">
      <div class="card-head"><h3>Complete-case analysis</h3><span class="tag">no missing-data assumption</span></div>
      <div class="table-wrap"><table><tbody>
        <tr><th>Estimate</th><td class="num">${signed(cc.mean_difference)} ${esc(unit)}</td>
          <th>Standard error</th><td class="num">${fmt(cc.se)}</td></tr>
        <tr><th>${(cc.ci_level * 100).toFixed(0)}% CI</th><td class="num">${signed(cc.ci_low)} to ${signed(cc.ci_high)}</td>
          <th>t · df</th><td class="num">${fmt(cc.t, 3)} · ${cc.df ?? '—'}</td></tr>
        <tr><th>Pairs (n)</th><td class="num">${cc.n}</td>
          <th>p-value</th><td class="num">${pValue(cc.p_value)}</td></tr>
      </tbody></table></div>
      <p class="help" style="margin-top:10px">Scope: ${cc.n} fully observed pairs out of ${cc.counts.pairs_in_plan}
        in the plan. This estimate is valid only for that subset; it makes no claim about the ${cc.counts.excluded_pairs}
        pairs with a missing week-12 value.</p>
    </div>

    <div class="card">
      <div class="card-head"><h3>Sensitivity under stated assumptions</h3><span class="tag orange">not pooled with complete-case</span></div>
      ${sens.length ? `
        <div class="table-wrap"><table><thead><tr>
          <th>Scenario</th><th>Assumption</th><th class="num">Mean difference</th><th>Interval / model</th><th>Type</th>
        </tr></thead><tbody>
          ${bounded ? `<tr>
            <td><strong>${esc(bounded.label)}</strong></td>
            <td>${esc(bounded.detail)}</td>
            <td class="num">${signed(bounded.observed_mean_difference)} (observed)</td>
            <td>bound ${signed(bounded.interval_low)} to ${signed(bounded.interval_high)}${bounded.crosses_null ? ' · crosses null' : ''}</td>
            <td><span class="tag">bound, not CI</span></td></tr>` : ''}
          ${tipped ? `<tr>
            <td><strong>${esc(tipped.label)}</strong></td>
            <td>${esc(tipped.detail)}</td>
            <td class="num">${signed(tipped.mean_difference)}</td>
            <td>${tipped.imputed_pairs} missing pairs at zero</td>
            <td><span class="tag">extreme case</span></td></tr>` : ''}
          ${mi && mi.combined ? `<tr>
            <td><strong>${esc(mi.label)}</strong></td>
            <td>${esc(mi.detail)}</td>
            <td class="num">${signed(mi.combined.mean_difference)}</td>
            <td>${(mi.combined.ci_level * 100).toFixed(0)}% CI ${signed(mi.combined.ci_low)} to ${signed(mi.combined.ci_high)} · m=${mi.imputations} · seed ${mi.seed}</td>
            <td><span class="tag">model estimate</span></td></tr>` :
            mi ? `<tr><td><strong>${esc(mi.label)}</strong></td><td>${esc(mi.reason)}</td><td class="num">—</td><td>not run</td><td><span class="tag">unavailable</span></td></tr>` : ''}
        </tbody></table></div>
        <p class="help" style="margin-top:10px">These scenarios answer “what would the result be if the missing
          contrasts were …”. None of them replaces the complete-case estimate, and the bound is not a confidence
          interval.</p>
      ` : `<div class="empty">Every pair has both outcomes, so no sensitivity analysis was required.</div>`}
    </div>

    <div class="card pad-0">
      <div class="card-head" style="padding:14px 18px 0; margin-bottom:8px;">
        <h3>Pairs excluded from the complete-case analysis</h3>
        <button class="btn small ghost" id="dl-json" type="button">Download JSON</button>
      </div>
      <div class="table-wrap"><table><thead><tr>
        <th>Pair</th><th>Cell</th><th>Missing side(s)</th><th>Reason</th><th>Evidenced by</th>
      </tr></thead><tbody>
        ${cc.excluded.length ? cc.excluded.map((e) => `
          <tr><td class="mono">${esc(e.pair_id)}</td>
            <td>${esc(Object.values(e.cell).join(' · '))}</td>
            <td>${e.missing_sides.map((m) => esc(m.arm)).join(', ')}</td>
            <td><span class="tag">${esc(e.reason.replaceAll('_', ' '))}</span></td>
            <td class="mono muted">${e.missing_sides.map((m) => esc(m.obs_id)).join(', ')}</td></tr>`).join('')
          : '<tr><td colspan="5"><div class="empty">No pair excluded: every pair had both outcomes reported.</div></td></tr>'}
      </tbody></table></div>
    </div>

    <div class="card">
      <div class="card-head"><h3>Provenance</h3><span class="tag">hashed inputs</span></div>
      <div class="table-wrap"><table><tbody>
        <tr><th>Plan</th><td class="mono">v${report.plan.plan_version} · ${esc(report.plan.plan_version_id)}</td>
          <th>Plan checksum</th><td class="mono">${esc(report.plan.plan_checksum.slice(0, 20))}…</td></tr>
        <tr><th>Policy</th><td class="mono">${esc(report.plan.policy_checksum.slice(0, 20))}…</td>
          <th>Observation fingerprint</th><td class="mono">${esc(report.plan.observation_fingerprint.slice(0, 20))}…</td></tr>
        <tr><th>Analysis input hash</th><td class="mono">${esc(report.provenance.report_input_hash.slice(0, 32))}…</td>
          <th>Observation feed version</th><td class="num">${report.provenance.observation_version}</td></tr>
        <tr><th>Matching uses outcomes</th><td>${report.plan.policy ? 'no' : '—'}</td>
          <th>Missing-data handling</th><td>exclude + declared sensitivity</td></tr>
      </tbody></table></div>
      <ul class="secondary" style="font-size:12.5px; margin:12px 0 0; padding-left:18px; display:flex; flex-direction:column; gap:4px;">
        ${report.warnings.map((w) => `<li>${esc(w)}</li>`).join('')}
      </ul>
    </div>

    <div class="card">
      <div class="card-head"><h3>Table view</h3><span class="help">same numbers as the chart</span></div>
      <div class="table-wrap"><table><thead><tr>
        <th>Pair</th><th>Cell</th><th class="num">Intervention</th><th class="num">Control</th><th class="num">Favourable difference</th>
      </tr></thead><tbody>
        ${cc.pairs.map((p) => `<tr><td class="mono">${esc(p.pair_id)}</td>
          <td>${esc(Object.values(p.cell).join(' · '))}</td>
          <td class="num">${fmt(p.treated_value)}</td><td class="num">${fmt(p.control_value)}</td>
          <td class="num">${signed(p.favourable_difference)}</td></tr>`).join('')}
      </tbody></table></div>
    </div>`;

  $('#dl-json').addEventListener('click', () => {
    const blob = new Blob([JSON.stringify(report, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `analysis-${report.outcome}.json`;
    a.click();
    URL.revokeObjectURL(url);
  });
  bindChartHover();
}

function buildLayeredChart(cc, bounded, unit) {
  // Two layers on one shared x scale: per-pair differences (dots) and, beneath,
  // the complete-case CI plus the sensitivity bound. Never a dual axis.
  const diffs = cc.pairs.map((p) => p.favourable_difference);
  const lo0 = Math.min(...diffs, cc.ci_low ?? Infinity, bounded ? bounded.interval_low : Infinity);
  const hi0 = Math.max(...diffs, cc.ci_high ?? -Infinity, bounded ? bounded.interval_high : -Infinity);
  const span = Math.max(hi0 - lo0, 1);
  const lo = lo0 - span * 0.08;
  const hi = hi0 + span * 0.08;
  const width = 860;
  const laneH = 58;
  const margin = { top: 28, right: 24, bottom: 30, left: 208 };
  const innerW = width - margin.left - margin.right;
  const lanes = [];
  if (bounded) lanes.push({ key: 'sensitivity', label: 'Sensitivity bound', y: 0 });
  lanes.push({ key: 'ci', label: 'Complete-case CI', y: 1 });
  lanes.push({ key: 'pairs', label: 'Pair differences', y: 2 });
  const height = margin.top + margin.bottom + lanes.length * laneH;
  const x = (v) => margin.left + ((v - lo) / (hi - lo)) * innerW;
  const laneY = (i) => margin.top + i * laneH;
  const zeroX = x(0);

  let svg = `<div class="chart" style="margin-top:16px"><svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Pairwise differences with complete-case interval and sensitivity bound">`;
  // axis + zero
  const tickVals = niceTicks(lo, hi, 5);
  tickVals.forEach((t) => {
    svg += `<line class="gridline" x1="${x(t).toFixed(1)}" x2="${x(t).toFixed(1)}" y1="${margin.top - 8}" y2="${height - margin.bottom + 4}" ${Math.abs(t) < 1e-9 ? 'opacity="0"' : ''}/>`;
    svg += `<text class="tick" x="${x(t).toFixed(1)}" y="${margin.top - 12}" text-anchor="middle">${signed(t, 0)}</text>`;
  });
  svg += `<line class="zero-line" x1="${zeroX}" x2="${zeroX}" y1="${margin.top - 8}" y2="${height - margin.bottom + 4}"/>`;
  svg += `<text class="tick" x="${zeroX}" y="${height - margin.bottom + 18}" text-anchor="middle">no difference</text>`;

  lanes.forEach((lane, i) => {
    const y = laneY(i) + laneH / 2;
    svg += `<text class="row-label" x="${margin.left - 12}" y="${y + 4}" text-anchor="end">${esc(lane.label)}</text>`;
    if (lane.key === 'ci' && cc.ci_low !== null) {
      svg += `<line x1="${x(cc.ci_low).toFixed(1)}" x2="${x(cc.ci_high).toFixed(1)}" y1="${y}" y2="${y}" stroke="var(--series-1)" stroke-width="8" stroke-linecap="round" opacity="0.9"/>`;
      svg += `<circle cx="${x(cc.mean_difference).toFixed(1)}" cy="${y}" r="5" fill="var(--surface)" stroke="var(--series-1)" stroke-width="2.5"/>`;
      svg += `<text class="value-label" x="${x(cc.mean_difference).toFixed(1)}" y="${y - 13}" text-anchor="middle">${signed(cc.mean_difference)}</text>`;
      const tip = `<div class="t-title">Complete-case estimate</div>
        <div class="t-row">Mean difference: <strong>${signed(cc.mean_difference)} ${esc(unit)}</strong></div>
        <div class="t-row">${(cc.ci_level * 100).toFixed(0)}% CI: <strong>${signed(cc.ci_low)} to ${signed(cc.ci_high)}</strong></div>
        <div class="t-row">n = ${cc.n} pairs with both outcomes</div>`;
      svg += `<rect class="hit" data-tip="${esc(tip)}" x="${x(cc.ci_low).toFixed(1)}" y="${y - 11}" width="${Math.max(8, x(cc.ci_high) - x(cc.ci_low)).toFixed(1)}" height="22"/>`;
    }
    if (lane.key === 'sensitivity' && bounded) {
      const crossed = bounded.crosses_null;
      svg += `<line x1="${x(bounded.interval_low).toFixed(1)}" x2="${x(bounded.interval_high).toFixed(1)}" y1="${y}" y2="${y}" stroke="var(--series-2)" stroke-width="7" stroke-linecap="round" opacity="0.28"/>`;
      [bounded.interval_low, bounded.interval_high].forEach((v) => {
        svg += `<circle cx="${x(v).toFixed(1)}" cy="${y}" r="4" fill="var(--series-2)" stroke="var(--surface)" stroke-width="2"/>`;
      });
      const tip = `<div class="t-title">Bounded sensitivity</div>
        <div class="t-row">Assumption: <strong>${esc(bounded.detail)}</strong></div>
        <div class="t-row">Possible mean difference: <strong>${signed(bounded.interval_low)} to ${signed(bounded.interval_high)} ${esc(unit)}</strong></div>
        <div class="t-row">${bounded.imputed_pairs} missing pairs · ${crossed ? 'interval crosses no difference' : 'does not cross no difference'}</div>
        <div class="t-row">This is a bound, not a confidence interval.</div>`;
      svg += `<rect class="hit" data-tip="${esc(tip)}" x="${x(bounded.interval_low).toFixed(1)}" y="${y - 11}" width="${Math.max(8, x(bounded.interval_high) - x(bounded.interval_low)).toFixed(1)}" height="22"/>`;
    }
    if (lane.key === 'pairs') {
      cc.pairs.forEach((p) => {
        const px = x(p.favourable_difference);
        const jitter = ((hashCode(p.pair_id) % 11) - 5) * 2.1;
        const py = y + jitter;
        svg += `<circle cx="${px.toFixed(1)}" cy="${py.toFixed(1)}" r="4.5" fill="var(--series-1)" stroke="var(--surface)" stroke-width="2"/>`;
        const tip = `<div class="t-title">${esc(p.pair_id)} · ${esc(Object.values(p.cell).join(' · '))}</div>
          <div class="t-row">Intervention: <strong>${fmt(p.treated_value)}</strong> · Control: <strong>${fmt(p.control_value)}</strong></div>
          <div class="t-row">Favourable difference: <strong>${signed(p.favourable_difference)} ${esc(unit)}</strong></div>
          <div class="t-row">Baseline gap: ${fmt(p.distance, 2)}°</div>`;
        svg += `<circle class="hit" data-tip="${esc(tip)}" cx="${px.toFixed(1)}" cy="${py.toFixed(1)}" r="11"/>`;
      });
      svg += `<line class="axis-line" x1="${margin.left}" x2="${margin.left + innerW}" y1="${y + laneH / 2 - 8}" y2="${y + laneH / 2 - 8}" opacity="0"/>`;
    }
  });
  svg += '</svg></div>';
  svg += `<div class="legend">
      <span class="legend-item"><span class="swatch" style="background:var(--series-1)"></span>Complete-case estimate and ${(cc.ci_level * 100).toFixed(0)}% CI · pair differences</span>
      ${bounded ? `<span class="legend-item"><span class="swatch range" style="background:var(--series-2); opacity:.45"></span>Sensitivity bound under stated assumption</span>` : ''}
    </div>`;
  return svg;
}

function niceTicks(lo, hi, count) {
  const span = hi - lo;
  if (!(span > 0)) return [0];
  const step0 = span / count;
  const mag = Math.pow(10, Math.floor(Math.log10(step0)));
  const norm = step0 / mag;
  const step = (norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 7 ? 5 : 10) * mag;
  const start = Math.ceil(lo / step) * step;
  const ticks = [];
  for (let v = start; v <= hi + 1e-9; v += step) ticks.push(Math.abs(v) < 1e-9 ? 0 : v);
  return ticks;
}

// -------------------------------------------------------------------- compare

async function compareRuns() {
  const a = $('#cmp-a').value;
  const b = $('#cmp-b').value;
  const el = $('#compare-result');
  if (!a || !b) { el.innerHTML = '<div class="empty">Pick two completed runs.</div>'; return; }
  if (a === b) { el.innerHTML = '<div class="empty">Pick two different runs.</div>'; return; }
  try {
    const [ra, rb] = await Promise.all([api(`/report/analyses/${a}`), api(`/report/analyses/${b}`)]);
    if (!ra.report || !rb.report) { el.innerHTML = '<div class="empty">Both runs must be complete.</div>'; return; }
    const A = ra.report.analysis.complete_case;
    const B = rb.report.analysis.complete_case;
    const pa = new Map(A.pairs.map((p) => [p.pair_id, p]));
    const pb = new Map(B.pairs.map((p) => [p.pair_id, p]));
    const changedPairs = [...pa.keys()].filter((k) => pb.has(k) && Math.abs(pa.get(k).favourable_difference - pb.get(k).favourable_difference) > 1e-9);
    const onlyA = [...pa.keys()].filter((k) => !pb.has(k));
    const onlyB = [...pb.keys()].filter((k) => !pa.has(k));
    const delta = B.mean_difference - A.mean_difference;
    const sameInputs = ra.report.provenance.report_input_hash === rb.report.provenance.report_input_hash;

    el.innerHTML = `
      <div class="banner ${sameInputs ? '' : 'warn'}" style="margin-bottom:10px">
        ${sameInputs ? 'Identical inputs: the two runs are the same analysis.' :
          `Different inputs. ${changedPairs.length} shared pair(s) changed value, ${onlyA.length} pair(s) only in A, ${onlyB.length} only in B.`}
      </div>
      <div class="table-wrap"><table><thead><tr>
        <th>Quantity</th><th class="num">Run A</th><th class="num">Run B</th><th class="num">Change</th>
      </tr></thead><tbody>
        <tr><th>Mean difference</th><td class="num">${signed(A.mean_difference)}</td><td class="num">${signed(B.mean_difference)}</td>
          <td class="num">${sameInputs ? '—' : signed(delta)}</td></tr>
        <tr><th>Complete pairs</th><td class="num">${A.n}</td><td class="num">${B.n}</td><td class="num">${B.n - A.n > 0 ? '+' : ''}${B.n - A.n}</td></tr>
        <tr><th>Pairs excluded (missing)</th><td class="num">${A.counts.excluded_pairs}</td><td class="num">${B.counts.excluded_pairs}</td>
          <td class="num">${B.counts.excluded_pairs - A.counts.excluded_pairs}</td></tr>
        <tr><th>Plan version</th><td>v${ra.report.plan.plan_version}</td><td>v${rb.report.plan.plan_version}</td><td>—</td></tr>
        <tr><th>95% CI width</th><td class="num">${signed(A.ci_high - A.ci_low)}</td><td class="num">${signed(B.ci_high - B.ci_low)}</td>
          <td class="num">${signed((B.ci_high - B.ci_low) - (A.ci_high - A.ci_low))}</td></tr>
      </tbody></table></div>
      ${changedPairs.length ? `<details class="history" open><summary>${changedPairs.length} changed pair value(s)</summary>
        <ul>${changedPairs.slice(0, 12).map((k) => `<li>${esc(k)}: ${signed(pa.get(k).favourable_difference)} → ${signed(pb.get(k).favourable_difference)}</li>`).join('')}</ul></details>` : ''}`;
  } catch (err) {
    el.innerHTML = `<div class="banner bad">${esc(err.message)}</div>`;
  }
}

// --------------------------------------------------------------------- wiring

async function refreshAll() {
  await Promise.all([loadHealth(), loadObservations(), loadPlans()]);
  await loadRuns();
}

$('#register-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = new FormData(event.target);
  const body = {
    subject_code: String(form.get('subject_code')).trim(),
    site: form.get('site'),
    severity_band: form.get('severity_band'),
    arm: form.get('arm'),
    baseline_rom: Number(form.get('baseline_rom')),
  };
  try {
    await api('/obs/observations', { method: 'POST', body });
    showError($('#register-error'), '');
    event.target.reset();
    flash(`Registered ${body.subject_code}.`, 'good');
    await refreshAll();
  } catch (err) {
    showError($('#register-error'), err.message);
  }
});

$('#plan-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = new FormData(event.target);
  const matchFields = form.getAll('match_fields');
  if (!matchFields.length) { showError($('#plan-error'), 'Select at least one exact-match cell.'); return; }
  const body = {
    policy: {
      match_fields: matchFields,
      balance_field: 'baseline_rom',
      caliper: Number(form.get('caliper')),
      tie_break_seed: Number(form.get('tie_break_seed')),
    },
    reason: String(form.get('reason')).trim(),
  };
  try {
    const plan = await api('/pair/plans', { method: 'POST', body });
    showError($('#plan-error'), '');
    state.selectedPlanId = plan.plan_version_id;
    flash(`Plan v${plan.plan_version} locked: ${plan.summary.paired} pairs.`, 'good');
    await loadPlans();
  } catch (err) {
    showError($('#plan-error'), err.message);
  }
});

$('#analysis-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const planId = $('#an-plan').value;
  if (!planId) { showError($('#analysis-error'), 'Build a plan version first.'); return; }
  const low = $('#an-bound-low').value;
  const high = $('#an-bound-high').value;
  const body = {
    plan_version_id: planId,
    outcome: $('#an-outcome').value,
    alpha: Number($('#an-alpha').value),
    mi_seed: Number($('#an-mi-seed').value),
    mi_draws: Number($('#an-mi-draws').value),
    missing_bounds: (low !== '' && high !== '') ? { low: Number(low), high: Number(high) } : null,
  };
  try {
    const run = await api('/report/analyses', { method: 'POST', body });
    showError($('#analysis-error'), '');
    flash('Analysis complete; inputs hashed and recorded.', 'good');
    await loadRuns();
    renderReport(run.report, run);
  } catch (err) {
    showError($('#analysis-error'), err.message);
    await loadRuns();
  }
});

$('#cmp-run').addEventListener('click', compareRuns);

$('#seed-demo').addEventListener('click', async () => {
  if (!window.confirm('Load the bundled synthetic dataset into the observation service? Already-present subject codes are skipped; outcome entry is idempotent.')) return;
  try {
    const result = await api('/api/demo/seed', { method: 'POST' });
    flash(`Loaded ${result.observations} synthetic participants; ${result.missing_rom} ROM values missing.`, 'good');
    await refreshAll();
  } catch (err) {
    flash(err.message, 'bad');
  }
});

$('#theme-toggle').addEventListener('click', () => {
  const root = document.documentElement;
  const dark = root.dataset.theme === 'dark' || (!root.dataset.theme && matchMedia('(prefers-color-scheme: dark)').matches);
  root.dataset.theme = dark ? 'light' : 'dark';
});

['#filter-site', '#filter-arm', '#filter-missing'].forEach((sel) => {
  $(sel).addEventListener('change', renderObsTable);
});

async function loadFault() {
  try {
    state.fault = await api('/report/faults');
    $('#fault-status').textContent = state.fault.enabled ? `Interruption armed at ${state.fault.stage}` : 'No interruption armed';
    $('#fault-toggle').classList.toggle('primary', state.fault.enabled);
  } catch { /* the gateway may not be up yet */ }
}

$('#fault-toggle').addEventListener('click', async () => {
  try {
    const stage = state.fault.stage || 'summarise';
    const next = !state.fault.enabled;
    state.fault = await api('/report/faults', { method: 'POST', body: { stage, enabled: next } });
    await loadFault();
    flash(next ? `Next analysis will be interrupted at ${stage}.` : 'Interruption disarmed.', next ? 'warn' : '');
  } catch (err) { flash(err.message, 'bad'); }
});

(async function boot() {
  await refreshAll();
  await loadFault();
  setInterval(loadHealth, 15000);
})();
