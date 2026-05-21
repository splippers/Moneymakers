(() => {
  const LS_DONE = 'moneymakers_survey_done_v2';
  const NAV = document.getElementById('nav');
  const barFill = document.getElementById('barFill');
  const barStat = document.getElementById('barStat');
  const progressLabel = document.getElementById('progressLabel');
  const repoMeta = document.getElementById('repoMeta');
  const saveErr = document.getElementById('saveErr');
  const toast = document.getElementById('toast');

  /** @type {{ projects_root?: string; repos?: any[]} | null } */
  let payload = null;
  /** @type {number} */
  let idx = 0;

  function setupSectionNav() {
    const buttons = document.querySelectorAll('.section-nav button');
    buttons.forEach((btn) => {
      btn.addEventListener('click', () => {
        const section = btn.dataset.section;
        if (!section) return;
        buttons.forEach((b) => b.classList.remove('active'));
        btn.classList.add('active');
        document.querySelectorAll('.survey-section').forEach((s) => {
          s.style.display = 'none';
        });
        const target = document.getElementById('section-' + section);
        if (target) target.style.display = 'block';
      });
    });
  }

  function doneMap() {
    try {
      return JSON.parse(localStorage.getItem(LS_DONE) || '{}');
    } catch {
      return {};
    }
  }

  function setDone(name, v) {
    const m = doneMap();
    if (v) m[name] = { at: new Date().toISOString() };
    else delete m[name];
    localStorage.setItem(LS_DONE, JSON.stringify(m));
  }

  function showToast(msg) {
    toast.textContent = msg;
    toast.classList.add('show');
    setTimeout(() => toast.classList.remove('show'), 2200);
  }

  function $(id) {
    const el = document.getElementById(id);
    if (!el) throw new Error('missing #' + id);
    return el;
  }

  function currentRepo() {
    const r = payload && payload.repos;
    if (!r || !r.length) return null;
    return r[idx];
  }

  function parseIntSafe(v, fallback = null) {
    if (v == null || v === '') return fallback;
    const n = Number(v);
    if (Number.isFinite(n)) return n;
    return fallback;
  }

  function fillForm(r) {
    $('prototype_goal').value = r.prototype_goal ?? '';
    $('market_tag').value = (r.market_tag || 'INTERNAL_TOOL').toUpperCase();
    $('importance').value = String(r.importance ?? 3);
    $('status').value = r.status ?? 'active';
    $('manual_notes').value = r.manual_notes ?? '';
    $('monetization_notes').value = r.monetization_notes ?? '';
    $('manual_money_low').value = r.manual_money_low != null ? String(r.manual_money_low) : '';
    $('manual_money_high').value = r.manual_money_high != null ? String(r.manual_money_high) : '';
    $('manual_value_override').value =
      r.manual_value_override != null && r.manual_value_override !== '' ? String(r.manual_value_override) : '';
    $('hide_meta').checked = !!r.hidden;

    $('lore_demand_estimate').value = r.lore_demand_estimate ?? '';
    $('lore_pain_level').value = r.lore_pain_level != null ? String(r.lore_pain_level) : '';
    $('lore_mvp_readiness').value = r.lore_mvp_readiness != null ? String(r.lore_mvp_readiness) : '';
    $('lore_ideal_acv').value = r.lore_ideal_acv != null ? String(r.lore_ideal_acv) : '';
    $('lore_conversion_pct').value = r.lore_conversion_pct != null ? String(r.lore_conversion_pct) : '';

    $('lore_story').value = r.lore_story ?? '';
    $('lore_tech_stack').value = r.lore_tech_stack ?? '';
    $('lore_blockers').value = r.lore_blockers ?? '';
    $('lore_prior_attempts').value = r.lore_prior_attempts ?? '';
    $('lore_ideal_customer').value = r.lore_ideal_customer ?? '';
    $('lore_competitive_edge').value = r.lore_competitive_edge ?? '';
    $('lore_next_steps').value = r.lore_next_steps ?? '';

    $('lore_waitlist_count').value = r.lore_waitlist_count != null ? String(r.lore_waitlist_count) : '';
    $('lore_loi_value').value = r.lore_loi_value != null ? String(r.lore_loi_value) : '';
    $('lore_beta_users').value = r.lore_beta_users != null ? String(r.lore_beta_users) : '';
  }

  function readFormBody() {
    const loRaw = $('manual_money_low').value.trim();
    const hiRaw = $('manual_money_high').value.trim();
    let manual_money_low = null;
    let manual_money_high = null;
    if (loRaw && hiRaw) {
      manual_money_low = parseIntSafe(loRaw, 0);
      manual_money_high = parseIntSafe(hiRaw, 0);
    }

    return {
      prototype_goal: $('prototype_goal').value.trimEnd(),
      market_tag: $('market_tag').value,
      importance: parseInt($('importance').value, 10),
      status: $('status').value,
      manual_notes: $('manual_notes').value,
      monetization_notes: $('monetization_notes').value,
      manual_money_low,
      manual_money_high,
      manual_value_override: parseIntSafe($('manual_value_override').value.trim(), null),
      hidden: $('hide_meta').checked,

      lore_demand_estimate: $('lore_demand_estimate').value.trimEnd(),
      lore_pain_level: parseIntSafe($('lore_pain_level').value, null),
      lore_mvp_readiness: parseIntSafe($('lore_mvp_readiness').value, null),
      lore_ideal_acv: parseIntSafe($('lore_ideal_acv').value, null),
      lore_conversion_pct: parseIntSafe($('lore_conversion_pct').value, null),

      lore_story: $('lore_story').value.trimEnd(),
      lore_tech_stack: $('lore_tech_stack').value.trimEnd(),
      lore_blockers: $('lore_blockers').value.trimEnd(),
      lore_prior_attempts: $('lore_prior_attempts').value.trimEnd(),
      lore_ideal_customer: $('lore_ideal_customer').value.trimEnd(),
      lore_competitive_edge: $('lore_competitive_edge').value.trimEnd(),
      lore_next_steps: $('lore_next_steps').value.trimEnd(),

      lore_waitlist_count: parseIntSafe($('lore_waitlist_count').value, null),
      lore_loi_value: parseIntSafe($('lore_loi_value').value, null),
      lore_beta_users: parseIntSafe($('lore_beta_users').value, null),
    };
  }

  async function saveCurrent() {
    saveErr.textContent = '';
    const name = currentRepo()?.name;
    if (!name) throw new Error('No repo loaded');
    const body = readFormBody();
    const res = await fetch('/api/project/' + encodeURIComponent(name), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const j = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(j.error || 'Save failed');

    const r = payload.repos.find((x) => x.name === name);
    if (r) Object.assign(r, body);
    return name;
  }

  function repoMetaHtml(r, root) {
    const sc = ((r.scores && r.scores.value) ?? '—') + ' · gtm ' + (r.gtm_readiness ?? r.total_score ?? '—');
    return (
      '<strong style="font-size:1.05rem">' +
      escapeHtml(r.name) +
      '</strong> <code>' +
      escapeHtml(r.path || '') +
      '</code> ' +
      '<span class="pill">value ' +
      escapeHtml(String(sc)) +
      '</span> ' +
      '<span class="pill">' +
      escapeHtml(r.scoring_confidence || '—') +
      '</span>' +
      (root ? ' <span class="pill">root ' + escapeHtml(root) + '</span>' : '')
    );
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function renderNav() {
    const r = payload.repos;
    const dm = doneMap();
    NAV.innerHTML = '';
    r.forEach((repo, i) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'pick' + (dm[repo.name] ? ' done' : '');
      b.textContent = repo.name;
      b.dataset.active = i === idx ? '1' : '0';
      b.addEventListener('click', () => {
        idx = i;
        renderAll();
      });
      NAV.appendChild(b);
    });
  }

  function progress() {
    const total = payload.repos.length;
    const dm = doneMap();
    let c = 0;
    for (const r of payload.repos) if (dm[r.name]) c++;
    const pct = total ? Math.round((c / total) * 100) : 0;
    barFill.style.width = pct + '%';
    barStat.textContent = 'Marked complete locally: ' + c + ' / ' + total + ' (browser memory only)';
    progressLabel.textContent = 'Repo ' + (idx + 1) + ' / ' + total;
  }

  function renderAll() {
    renderNav();
    const r = currentRepo();
    progress();
    repoMeta.innerHTML = r ? repoMetaHtml(r, payload.projects_root || '') : '<em>No projects</em>';
    if (r) fillForm(r);
  }

  async function bootstrap() {
    setupSectionNav();
    const res = await fetch('/api/projects');
    payload = await res.json();
    if (!payload.repos || !payload.repos.length) {
      progressLabel.textContent = 'Empty index';
      repoMeta.innerHTML =
        '<p>Run <code>python projectscan.py</code> once, then reopen this page.</p>';
      return;
    }
    payload.repos.sort((a, b) => String(a.name).localeCompare(b.name));
    idx = Math.min(Math.max(0, idx), payload.repos.length - 1);
    renderAll();
  }

  $('saveStay').addEventListener('click', async () => {
    try {
      await saveCurrent();
      showToast('Saved');
      renderNav();
      progress();
    } catch (e) {
      saveErr.textContent = String(e.message || e);
    }
  });

  $('saveNext').addEventListener('click', async () => {
    try {
      await saveCurrent();
      showToast('Saved');
      idx = Math.min(payload.repos.length - 1, idx + 1);
      renderAll();
    } catch (e) {
      saveErr.textContent = String(e.message || e);
    }
  });

  $('markDone').addEventListener('click', () => {
    const r = currentRepo();
    if (!r) return;
    const dm = doneMap();
    const was = !!dm[r.name];
    setDone(r.name, !was);
    showToast(was ? 'Unmarked' : 'Marked complete');
    renderAll();
  });

  $('exportJson').addEventListener('click', () => {
    if (!payload?.repos?.length) return;
    const out = {};
    for (const r of payload.repos)
      out[r.name] = {
        prototype_goal: r.prototype_goal || '',
        market_tag: r.market_tag || '',
        importance: r.importance,
        status: r.status,
        manual_notes: r.manual_notes || '',
        monetization_notes: r.monetization_notes || '',
        manual_money_low: r.manual_money_low,
        manual_money_high: r.manual_money_high,
        manual_value_override: r.manual_value_override,
        hidden: !!r.hidden,
        lore_demand_estimate: r.lore_demand_estimate,
        lore_pain_level: r.lore_pain_level,
        lore_mvp_readiness: r.lore_mvp_readiness,
        lore_ideal_acv: r.lore_ideal_acv,
        lore_conversion_pct: r.lore_conversion_pct,
        lore_story: r.lore_story,
        lore_tech_stack: r.lore_tech_stack,
        lore_blockers: r.lore_blockers,
        lore_prior_attempts: r.lore_prior_attempts,
        lore_ideal_customer: r.lore_ideal_customer,
        lore_competitive_edge: r.lore_competitive_edge,
        lore_next_steps: r.lore_next_steps,
        lore_waitlist_count: r.lore_waitlist_count,
        lore_loi_value: r.lore_loi_value,
        lore_beta_users: r.lore_beta_users,
      };
    const blob = new Blob([JSON.stringify(out, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'moneymakers_survey_export.json';
    a.click();
    URL.revokeObjectURL(url);
  });

  bootstrap().catch((e) => {
    saveErr.textContent = 'Cannot load projects: ' + String(e.message || e);
  });
})();
