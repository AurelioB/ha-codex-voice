const esc = (value) =>
  String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");

class CodexVoicePanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._entries = [];
    this._entryId = null;
    this._status = null;
    this._busy = false;
    this._error = null;
  }

  set hass(value) {
    const first = !this._hass;
    this._hass = value;
    if (first) this._loadEntries();
  }

  set panel(value) {
    this._panel = value;
  }

  connectedCallback() {
    this._render();
  }

  async _call(type, data = {}) {
    if (!this._hass) throw new Error("Home Assistant is not connected");
    return this._hass.callWS({ type, ...data });
  }

  async _loadEntries() {
    this._busy = true;
    this._render();
    try {
      this._entries = await this._call("codex_voice/identity/entries");
      if (!this._entryId && this._entries.length) {
        this._entryId = this._entries[0].entry_id;
      }
      await this._refresh();
    } catch (error) {
      this._error = error.message || String(error);
    } finally {
      this._busy = false;
      this._render();
    }
  }

  async _refresh() {
    if (!this._entryId) return;
    this._error = null;
    try {
      this._status = await this._call("codex_voice/identity/status", {
        entry_id: this._entryId,
      });
    } catch (error) {
      this._status = null;
      this._error = error.message || String(error);
    }
  }

  async _run(operation) {
    if (this._busy) return;
    this._busy = true;
    this._error = null;
    this._render();
    try {
      await operation();
      await this._refresh();
    } catch (error) {
      this._error = error.message || String(error);
    } finally {
      this._busy = false;
      this._render();
    }
  }

  _personOptions(selected = "") {
    const people = this._status?.people || [];
    return [
      '<option value="">Not linked to a Person</option>',
      ...people.map(
        (person) =>
          `<option value="${esc(person.entity_id)}" ${person.entity_id === selected ? "selected" : ""}>${esc(person.name)} (${esc(person.entity_id)})</option>`,
      ),
    ].join("");
  }

  _userOptions(selected = "") {
    const users = this._status?.users || [];
    return [
      '<option value="">Not linked to a user</option>',
      ...users.map(
        (user) =>
          `<option value="${esc(user.id)}" ${user.id === selected ? "selected" : ""}>${esc(user.name)}${user.is_owner ? " (owner)" : ""}</option>`,
      ),
    ].join("");
  }

  _renderEnrollment() {
    const enrollment = (this._status?.enrollments || [])[0];
    if (enrollment) {
      const percent = Math.min(
        100,
        Math.round((enrollment.sample_count / enrollment.required_samples) * 100),
      );
      return `
        <section class="card important">
          <div class="section-title"><h2>Enrollment in progress</h2><span class="pill">${esc(enrollment.sample_count)} / ${esc(enrollment.required_samples)}</span></div>
          <p><strong>${esc(enrollment.display_name)}</strong> · <code>${esc(enrollment.speaker_id)}</code></p>
          <div class="progress"><span style="width:${percent}%"></span></div>
          <p>Use the speaker in ${esc(Math.max(0, enrollment.required_samples - enrollment.sample_count))} more separate sessions. After each wake, speak naturally for at least three seconds. Vary distance and wording.</p>
          <p class="muted">The worker keeps embeddings and audio hashes only. Raw enrollment audio is not retained.</p>
          <div class="actions">
            <button data-action="refresh">Refresh</button>
            <button class="primary" data-action="complete" data-id="${esc(enrollment.speaker_id)}" ${enrollment.ready ? "" : "disabled"}>Build profile</button>
            <button class="danger" data-action="cancel" data-id="${esc(enrollment.speaker_id)}">Cancel</button>
          </div>
        </section>`;
    }
    return `
      <section class="card">
        <h2>Enroll a speaker</h2>
        <p>Create a private voice profile and optionally link it to a Home Assistant Person and user. Explicit consent is required.</p>
        <form id="enroll-form" class="grid-form">
          <label>Profile ID<input name="speaker_id" required pattern="[A-Za-z0-9][A-Za-z0-9_.-]{0,63}" placeholder="aurelio"></label>
          <label>Display name<input name="display_name" required maxlength="128" placeholder="Aurelio"></label>
          <label>Home Assistant Person<select name="ha_person_id">${this._personOptions()}</select></label>
          <label>Home Assistant user<select name="ha_user_id">${this._userOptions()}</select></label>
          <label class="consent"><input name="consent" type="checkbox" required> This person explicitly consents to creating and storing a local voice embedding.</label>
          <div class="actions"><button class="primary" type="submit">Start enrollment</button></div>
        </form>
      </section>`;
  }

  _renderProfiles() {
    const profiles = this._status?.profiles || [];
    if (!profiles.length) {
      return '<section class="card"><h2>Voice profiles</h2><p class="muted">No completed profiles yet.</p></section>';
    }
    return `
      <section class="card">
        <h2>Voice profiles</h2>
        <p>New profiles remain disabled until you run held-out tests and activate them.</p>
        <div class="profile-list">
          ${profiles
            .map(
              (profile) => `
              <article class="profile" data-profile="${esc(profile.speaker_id)}">
                <div class="profile-heading">
                  <div><strong>${esc(profile.display_name)}</strong><br><code>${esc(profile.speaker_id)}</code></div>
                  <span class="pill ${profile.enabled ? "good" : ""}">${profile.enabled ? "Active" : "Inactive"}</span>
                </div>
                <div class="grid-form compact">
                  <label>Display name<input data-field="display_name" value="${esc(profile.display_name)}" maxlength="128"></label>
                  <label>Person<select data-field="ha_person_id">${this._personOptions(profile.ha_person_id || "")}</select></label>
                  <label>User<select data-field="ha_user_id">${this._userOptions(profile.ha_user_id || "")}</select></label>
                  <label class="toggle"><input data-field="enabled" type="checkbox" ${profile.enabled ? "checked" : ""}> Use for live identification</label>
                </div>
                <p class="muted">${esc(profile.chunks)} enrollment embeddings. Identity is personalization only, never authentication.</p>
                <div class="actions">
                  <button data-action="save-profile" data-id="${esc(profile.speaker_id)}">Save links</button>
                  <button data-action="test" data-id="${esc(profile.speaker_id)}">Test next wake as this person</button>
                  <button class="danger" data-action="delete-profile" data-id="${esc(profile.speaker_id)}">Delete</button>
                </div>
              </article>`,
            )
            .join("")}
        </div>
        <div class="actions"><button data-action="test-unknown">Test next wake as an unknown speaker</button></div>
      </section>`;
  }

  _renderTest() {
    const test = this._status?.last_test;
    const armed = this._status?.test_armed;
    if (!test && !armed) return "";
    return `
      <section class="card ${test?.passed ? "success" : armed ? "important" : "warning"}">
        <h2>Held-out identity test</h2>
        ${
          armed
            ? "<p>The next five-second post-wake sample will be used only for validation. It will not personalize that session.</p>"
            : `<p><strong>${test.passed ? "Passed" : "Did not pass"}</strong> · expected ${esc(test.expected_speaker_id || "unknown")}, observed ${esc(test.observed_speaker_id || "unknown")}</p><p class="muted">Score ${esc(test.score)}, margin ${esc(test.margin)}</p>`
        }
      </section>`;
  }

  _renderSettings() {
    const settings = this._status?.settings || {};
    return `
      <section class="card">
        <div class="section-title"><h2>Assistant settings</h2><a href="${esc(this._status?.integration_url || "/config/integrations")}">Conversation, language, voice and Home Assistant tools</a></div>
        <p>Identity thresholds apply immediately and persist in the private worker directory. Raise them to prefer “unknown” over a false match.</p>
        <form id="settings-form" class="grid-form two">
          <label>Match threshold<input name="match_threshold" type="number" min="-1" max="1" step="0.01" value="${esc(settings.match_threshold ?? 0.55)}"></label>
          <label>Separation margin<input name="margin_threshold" type="number" min="0" max="2" step="0.01" value="${esc(settings.margin_threshold ?? 0.08)}"></label>
          <div class="actions"><button type="submit">Save thresholds</button></div>
        </form>
      </section>`;
  }

  _render() {
    if (!this.shadowRoot) return;
    const entryOptions = this._entries
      .map(
        (entry) =>
          `<option value="${esc(entry.entry_id)}" ${entry.entry_id === this._entryId ? "selected" : ""}>${esc(entry.title)}</option>`,
      )
      .join("");
    this.shadowRoot.innerHTML = `
      <style>
        :host { display:block; min-height:100%; background:var(--primary-background-color); color:var(--primary-text-color); font-family:var(--paper-font-body1_-_font-family, sans-serif); }
        main { max-width:1050px; margin:0 auto; padding:24px 16px 64px; }
        header { display:flex; align-items:center; justify-content:space-between; gap:16px; margin-bottom:20px; }
        h1 { font-size:28px; margin:0; } h2 { font-size:19px; margin:0 0 12px; }
        p { line-height:1.5; } a { color:var(--primary-color); }
        .card { background:var(--card-background-color); border-radius:12px; box-shadow:var(--ha-card-box-shadow); padding:20px; margin:16px 0; }
        .important { border-left:4px solid var(--warning-color,#ff9800); } .success { border-left:4px solid var(--success-color,#43a047); } .warning { border-left:4px solid var(--error-color,#db4437); }
        .toolbar,.actions,.section-title,.profile-heading { display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
        .section-title,.profile-heading { justify-content:space-between; }
        .grid-form { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:14px; }
        .grid-form.two { grid-template-columns:repeat(2,minmax(0,260px)); }
        .grid-form.compact { margin-top:14px; }
        label { display:flex; flex-direction:column; gap:6px; font-size:13px; color:var(--secondary-text-color); }
        label.consent,label.toggle { grid-column:1/-1; flex-direction:row; align-items:flex-start; color:var(--primary-text-color); }
        input,select,button { font:inherit; }
        input,select { box-sizing:border-box; width:100%; color:var(--primary-text-color); background:var(--card-background-color); border:1px solid var(--divider-color); border-radius:8px; padding:11px; }
        input[type=checkbox] { width:auto; margin-top:2px; }
        button { border:0; border-radius:8px; padding:10px 14px; background:var(--secondary-background-color); color:var(--primary-text-color); cursor:pointer; }
        button.primary { background:var(--primary-color); color:var(--text-primary-color,#fff); } button.danger { color:var(--error-color,#db4437); }
        button:disabled { opacity:.45; cursor:not-allowed; }
        .actions { grid-column:1/-1; margin-top:8px; }
        .muted { color:var(--secondary-text-color); font-size:13px; }
        .pill { border-radius:999px; padding:5px 9px; background:var(--secondary-background-color); font-size:12px; } .pill.good { color:var(--success-color,#43a047); }
        .progress { height:8px; border-radius:999px; overflow:hidden; background:var(--secondary-background-color); } .progress span { display:block; height:100%; background:var(--primary-color); }
        .profile { padding:16px 0; border-top:1px solid var(--divider-color); } .profile:first-child { border-top:0; }
        .error { background:var(--error-color,#db4437); color:white; padding:12px 16px; border-radius:8px; margin:12px 0; }
        .spinner { opacity:.7; }
        @media (max-width:700px) { .grid-form,.grid-form.two { grid-template-columns:1fr; } header { align-items:flex-start; flex-direction:column; } }
      </style>
      <main>
        <header>
          <div><h1>Codex Voice</h1><div class="muted">Local speaker profiles and assistant configuration</div></div>
          <div class="toolbar">
            ${this._entries.length > 1 ? `<select id="entry-select">${entryOptions}</select>` : ""}
            <button data-action="refresh" ${this._busy ? "disabled" : ""}>${this._busy ? "Working…" : "Refresh"}</button>
          </div>
        </header>
        ${this._error ? `<div class="error">${esc(this._error)}</div>` : ""}
        ${!this._entries.length ? '<section class="card"><p>Add and load the Codex Voice integration first.</p></section>' : ""}
        ${this._status ? `${this._renderEnrollment()}${this._renderTest()}${this._renderProfiles()}${this._renderSettings()}` : this._entries.length ? '<section class="card spinner"><p>Loading identity worker…</p></section>' : ""}
      </main>`;
    this._bind();
  }

  _bind() {
    const root = this.shadowRoot;
    root.querySelector("#entry-select")?.addEventListener("change", (event) => {
      this._entryId = event.target.value;
      this._run(async () => this._refresh());
    });
    root.querySelector("#enroll-form")?.addEventListener("submit", (event) => {
      event.preventDefault();
      const form = new FormData(event.target);
      this._run(() =>
        this._call("codex_voice/identity/enrollment/start", {
          entry_id: this._entryId,
          speaker_id: form.get("speaker_id"),
          display_name: form.get("display_name"),
          ha_person_id: form.get("ha_person_id") || null,
          ha_user_id: form.get("ha_user_id") || null,
          consent: form.get("consent") === "on",
        }),
      );
    });
    root.querySelector("#settings-form")?.addEventListener("submit", (event) => {
      event.preventDefault();
      const form = new FormData(event.target);
      this._run(() =>
        this._call("codex_voice/identity/settings/update", {
          entry_id: this._entryId,
          match_threshold: Number(form.get("match_threshold")),
          margin_threshold: Number(form.get("margin_threshold")),
        }),
      );
    });
    root.querySelectorAll("[data-action]").forEach((button) => {
      button.addEventListener("click", () => this._handleAction(button));
    });
  }

  _handleAction(button) {
    const action = button.dataset.action;
    const id = button.dataset.id;
    if (action === "refresh") return this._run(async () => this._refresh());
    if (action === "complete")
      return this._run(() => this._call("codex_voice/identity/enrollment/complete", { entry_id: this._entryId, speaker_id: id }));
    if (action === "cancel" && confirm("Cancel this enrollment and delete its embeddings?"))
      return this._run(() => this._call("codex_voice/identity/enrollment/cancel", { entry_id: this._entryId, speaker_id: id }));
    if (action === "delete-profile" && confirm("Permanently delete this local voice profile?"))
      return this._run(() => this._call("codex_voice/identity/profile/delete", { entry_id: this._entryId, speaker_id: id }));
    if (action === "test")
      return this._run(() => this._call("codex_voice/identity/test/arm", { entry_id: this._entryId, expected_speaker_id: id }));
    if (action === "test-unknown")
      return this._run(() => this._call("codex_voice/identity/test/arm", { entry_id: this._entryId, expected_speaker_id: null }));
    if (action === "save-profile") {
      const card = button.closest("[data-profile]");
      const field = (name) => card.querySelector(`[data-field=${name}]`);
      return this._run(() =>
        this._call("codex_voice/identity/profile/update", {
          entry_id: this._entryId,
          speaker_id: id,
          display_name: field("display_name").value,
          ha_person_id: field("ha_person_id").value || null,
          ha_user_id: field("ha_user_id").value || null,
          enabled: field("enabled").checked,
        }),
      );
    }
  }
}

customElements.define("codex-voice-panel", CodexVoicePanel);
