"use strict";
const archiveMode = window.location.pathname.startsWith("/wtf/archive/");
const apiRoot = archiveMode ? "/wtf/api/archive/" : "/wtf/api/";
const pageRoot = archiveMode ? "/wtf/archive/" : "/wtf/";
const $ = (id) => document.getElementById(id);
const esc = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
// Formatters are built once; a refresh formats several hundred values.
const numberFormats = [0, 1, 2, 3].map(
  (digits) =>
    new Intl.NumberFormat(undefined, { maximumFractionDigits: digits }),
);
const fmt = (v, digits = 0) => numberFormats[digits].format(Number(v));
const shortDate = new Intl.DateTimeFormat(undefined, {
  month: "short",
  day: "numeric",
  hour: "numeric",
  minute: "2-digit",
});
const longDate = new Intl.DateTimeFormat(undefined, {
  year: "numeric",
  month: "numeric",
  day: "numeric",
  hour: "numeric",
  minute: "numeric",
  second: "numeric",
});
// A report's time of day, as the browser's toLocaleTimeString() writes it.
const timeOfDay = new Intl.DateTimeFormat(undefined, {
  hour: "numeric",
  minute: "numeric",
  second: "numeric",
});
// Intl formatters throw on an invalid date; one bad timestamp must not blank the feed.
const formatTime = (format, t) => {
  const when = new Date(t * 1000);
  return Number.isNaN(when.getTime()) ? "Unknown time" : format.format(when);
};
const date = (t) => formatTime(shortDate, t);
const fullDate = (t) => formatTime(longDate, t);
const list = (items, render = esc) =>
  `<ul>${items.map((item) => `<li>${render(item)}</li>`).join("")}</ul>`;
const extLink = (url, label) =>
  url ? `<a class="map-link" href="${esc(url)}" target="_blank" rel="noreferrer">${esc(label)} ↗</a>` : "";
const labels = {
  airframes: "Special airframe",
  performance: "Big numbers",
  patterns: "Flight pattern",
  oddities: "Odd data",
  signals: "Transponder signal",
};
let category = "",
  eventsHtml = null,
  spotlightHtml = null;
// The sighting dialog: the sighting it shows, as its id string, while it is
// open, and a generation that each new request or closing advances, so a
// slower earlier reply never replaces what the dialog shows now.
const sightingDialog = { id: null, generation: 0 };
// Where keyboard focus is, as a selector that survives a redraw: the spotlight or
// sighting card that holds the control, and the control's tag and data attributes.
function focusKey(el) {
  const holder = el?.closest?.("#spotlight, #events .event");
  if (!holder?.id || !el.tagName) return null;
  const tag = el.tagName.toLowerCase();
  const attributes = (dataset) =>
    Object.entries(dataset || {})
      .map(([key, value]) => [
        key.replace(/[A-Z]/g, (c) => "-" + c.toLowerCase()),
        String(value).replace(/[^\w-]/g, ""),
      ])
      .map(([key, value]) => `[data-${key}="${value}"]`)
      .join("");
  // A disclosure's summary is known by the section it opens.
  const selector =
    tag === "summary"
      ? `#${holder.id} details${attributes(el.parentElement?.dataset)} > summary`
      : `#${holder.id} ${tag}${attributes(el.dataset)}`;
  return { element: el, selector };
}
const mapUrl = (hex) => document.body.dataset.mapUrl ? document.body.dataset.mapUrl + encodeURIComponent(hex) : null;
const sourceName = (type) =>
  ({
    adsb_icao: "ADS-B",
    adsb_icao_nt: "ADS-B",
    mlat: "MLAT",
    adsr_icao: "ADS-R",
    tisb_icao: "TIS-B",
    adsb_other: "ADS-B (non-ICAO)",
    mode_s: "Mode S",
  })[type] ||
  type ||
  "Not preserved";
function params() {
  return new URLSearchParams({
    days: $("period").value,
    sort: $("sort").value,
    view: $("view").value,
    category,
    q: $("search").value,
  });
}
function valueHtml(e) {
  if (e.value == null) return "";
  // A saved range is a 50-nm band; show it as the records panel does.
  if (e.unit === "nm band" || (e.kind === "distant" && e.unit === "nm"))
    return `${rangeBand(e.value)}<small>nm</small>`;
  return `${fmt(e.value, ["kn", "nm", "turns"].includes(e.unit) ? 1 : 0)}<small>${esc(e.unit)}</small>`;
}
function aircraftHtml(a) {
  return `<span class="registration">${esc(a.r || a.flight || a.hex)}</span>${a.t ? `<span class="typecode">${esc(a.t)}</span>` : ""}${a.flight && a.flight !== a.r ? `<span>${esc(a.flight)}</span>` : ""}<span class="hex">${esc(a.hex?.toUpperCase())}</span>`;
}
const finite = (value) => typeof value === "number" && Number.isFinite(value);
const clock = (t) => (finite(t) ? formatTime(timeOfDay, t) : "Unknown time");
// Each saved altitude field names its reference; the chart and table say which one.
const ALTITUDE_FIELDS = [
  ["alt_baro", "pressure"],
  ["alt_geom", "geometric"],
  ["archive_alt", "archive"],
];
// Saved context samples have two shapes: archive rows ({timestamp, archive_alt})
// and live reports ({ts, alt_baro, alt_geom}). Both are read here, once.
function contextSample(s) {
  const time = finite(s.timestamp) ? s.timestamp : s.ts;
  const [field, reference] =
    ALTITUDE_FIELDS.find(([key]) => finite(s[key]) || s[key] === "ground") ||
    [];
  return {
    time: finite(time) ? time : null,
    gs: finite(s.gs) ? s.gs : null,
    altitude: field ? s[field] : null,
    reference: field ? reference : null,
    squawk: s.squawk,
    source: s.source,
  };
}
// Every saved altitude with its reference; a geometric reading alone is still a reading.
function altitudeText(raw) {
  const readings = ALTITUDE_FIELDS.filter(
    ([field]) => finite(raw[field]) || raw[field] === "ground",
  ).map(([field, reference]) =>
    raw[field] === "ground" ? "Ground" : `${fmt(raw[field])} ft (${reference})`,
  );
  return readings.length ? [...new Set(readings)].join(" · ") : "Not preserved";
}
function contextHtml(e) {
  const raw = e.evidence.context_samples;
  if (!raw?.length) return "";
  const samples = raw.map(contextSample);
  const series = {
    gs: samples.filter((s) => s.time !== null && s.gs !== null),
    altitude: samples.filter((s) => s.time !== null && finite(s.altitude)),
  };
  // The kind names the reading under review when it can; otherwise, or when
  // that reading was not saved, the chart shows whichever measurement is there.
  const preferred = /alt|climb|descent|vertical|oscillat/.test(e.kind || "")
    ? "altitude"
    : "gs";
  const other = preferred === "gs" ? "altitude" : "gs";
  const field = series[preferred].length > 1 ? preferred : other;
  const values = series[field];
  const references = [
    ...new Set(values.map((s) => s.reference).filter(Boolean)),
  ];
  const label =
    field === "gs"
      ? "ground speeds"
      : `${references.length === 1 ? references[0] : "mixed-reference"} altitudes`;
  const unit = field === "gs" ? "kn" : "ft";
  let chart = "";
  if (values.length > 1) {
    const low = Math.min(...values.map((s) => s[field])),
      high = Math.max(...values.map((s) => s[field]));
    const start = values[0].time,
      end = values.at(-1).time;
    const pts = values
      .map(
        (s) =>
          `${(34 + (530 * (s.time - start)) / Math.max(1, end - start)).toFixed(1)},${(110 - (82 * (s[field] - low)) / Math.max(1, high - low)).toFixed(1)}`,
      )
      .join(" ");
    chart = `<svg viewBox="0 0 600 146" role="img" aria-label="Surrounding reported ${label}"><path d="M34 25H564M34 110H564" stroke="#34414b" stroke-dasharray="3 4"/><polyline points="${pts}" fill="none" stroke="#ff9a83" stroke-width="2.5"/><text x="34" y="17" fill="#9caebc" font-size="10">${esc(fmt(high, 1))} ${unit}</text><text x="34" y="128" fill="#9caebc" font-size="10">${esc(fmt(low, 1))} ${unit}</text><text x="565" y="141" text-anchor="end" fill="#7d919f" font-size="10">${esc(date(start))} – ${esc(clock(end))}</text></svg>`;
  }
  const altitudeCell = (s) =>
    s.altitude === null
      ? "—"
      : s.altitude === "ground"
        ? "Ground"
        : `${fmt(s.altitude)} <small>${esc(s.reference)}</small>`;
  return `<section class="sample-context"><h4>Before and after the strange reading</h4>${chart}<details><summary>See all ${samples.length} surrounding saved samples</summary><div class="sample-table"><table><thead><tr><th>Local time</th><th>Speed / kn</th><th>Altitude / ft (reference)</th><th>Squawk</th><th>Source</th></tr></thead><tbody>${samples.map((s) => `<tr><td>${esc(clock(s.time))}</td><td>${s.gs === null ? "—" : fmt(s.gs, 1)}</td><td>${altitudeCell(s)}</td><td>${esc(s.squawk || "—")}</td><td>${esc(sourceName(s.source))}</td></tr>`).join("")}</tbody></table></div></details></section>`;
}
function noteHtml(e) {
  const n = e.note;
  if (!n)
    return `<div class="ai-note pending"><div class="eyebrow">FIELD NOTES</div><p>No field note has been saved for this sighting. The rule explanation and original evidence are available now.</p></div>`;
  return `<section class="ai-note"><div class="eyebrow">AI FIELD NOTES <span>· ${esc(date(n.created_at))}</span></div><p class="note-summary">${esc(n.summary)}</p><h4>Why this is interesting</h4><p>${esc(n.why_interesting)}</p><h4>What the data actually says</h4>${list(n.known_facts)}<h4>What could explain it</h4>${list(n.possible_explanations)}<h4>What we can’t conclude</h4><p>${esc(n.cannot_conclude)}</p><h4>Worth checking next</h4>${list(n.next_checks)}<div class="source-note">Written from saved evidence using ${esc(n.model)}. Notes explain the evidence saved at analysis time; later observations may differ. Explanations remain hypotheses unless the observations establish them.</div></section>`;
}
function storyHtml(e) {
  const s = e.story;
  if (!s?.headline) return "";
  // Draft links come from the saved evidence packet; show only ordinary web links.
  const links = (s.links || []).filter((l) => /^https:\/\//.test(l.url));
  return `<section class="ai-note story-draft"><div class="eyebrow">UNPUBLISHED STORY DRAFT <span>· ${esc(date(s.created_at))}</span></div><h4 class="story-headline">${esc(s.headline)}</h4><p class="note-summary">${esc(s.standfirst)}</p>${(s.paragraphs || []).map((p) => `<p>${esc(p)}</p>`).join("")}${s.caveat ? `<h4>Record limits</h4><p>${esc(s.caveat)}</p>` : ""}${links.length ? `<h4>Further reading</h4>${list(links, (l) => extLink(l.url, l.label))}` : ""}<div class="source-note">Drafted with ${esc(s.model)} from the story evidence saved at that time. It has not been published; check it against the evidence before using it.</div></section>`;
}
// The journal saves range only as a 50-nm band; older responses are banded here too.
const rangeBand = (value) => {
  const low = Math.floor(Number(value) / 50) * 50;
  return `${fmt(low)}–${fmt(low + 50)}`;
};
const tagText = (tag) => String(tag).replaceAll("_", " ");
function annotationsHtml(e) {
  const blocks = [e.evidence, e.evidence.maneuver, e.evidence.pattern];
  const collect = (key) => [
    ...new Set(
      blocks.flatMap((block) =>
        Array.isArray(block?.[key])
          ? block[key].filter((item) => typeof item === "string")
          : [],
      ),
    ),
  ];
  const context = collect("context_tags"),
    flags = collect("quality_flags");
  const notes = [];
  if (context.length)
    notes.push(`Context: ${context.map(tagText).join(", ")}.`);
  if (flags.length)
    notes.push(`Review flags: ${flags.map(tagText).join(", ")}.`);
  // A turn or loiter saved before a circle or survey grid verified that covers it.
  const absorbed = e.evidence.absorbed_by;
  if (absorbed && Number.isInteger(absorbed.event_id))
    notes.push(
      `Part of the ${tagText(absorbed.kind)} saved as sighting ${absorbed.event_id}.`,
    );
  return notes.length
    ? `<p class="evidence-extra">${esc(notes.join(" "))}</p>`
    : "";
}
function participantsHtml(e) {
  const others = e.evidence.participants;
  if (!Array.isArray(others) || !others.length) return "";
  const measured = (m) =>
    Object.entries(m || {})
      .filter(([, value]) => finite(value))
      .map(([key, value]) => `${tagText(key)} ${fmt(value, 2)}`)
      .join(", ") || "—";
  return `<div class="sample-table"><table><caption>Other aircraft</caption><thead><tr><th>Role</th><th>Aircraft</th><th>Type</th><th>Source</th><th>Measurements</th></tr></thead><tbody>${others.map((p) => `<tr><td>${esc(p.role)}</td><td>${esc(p.registration || p.callsign || String(p.hex || "").toUpperCase())}</td><td>${esc(p.type || "—")}</td><td>${esc(sourceName(p.source))}</td><td>${esc(measured(p.measurements))}</td></tr>`).join("")}</tbody></table></div>`;
}
function reasonsHtml(e) {
  const reasons = e.encounter?.reasons || [];
  if (reasons.length < 2) return "";
  return `<details data-section="reasons"><summary>${reasons.length} reasons in this encounter</summary><ul>${reasons.map((r) => `<li><button type="button" class="text-button note-open" data-event="${r.id}">${esc(r.title)}</button></li>`).join("")}</ul><p class="source-note">Grouped observations can describe different moments. An encounter is not a confirmed flight.</p></details>`;
}
function reviewHtml(e) {
  if (archiveMode) return "";
  const choices = {
    keep: "Worth keeping",
    routine: "Routine repeat",
    bad_data: "Bad data",
    clear: "Clear review",
  };
  return `<div class="feedback"><span>Rate this reason:</span>${Object.entries(
    choices,
  )
    .map(
      ([value, label]) =>
        `<button type="button" class="text-button review-save" data-event="${e.id}" data-verdict="${value}" aria-pressed="${e.selection?.verdict === value}">${label}</button>`,
    )
    .join(
      "",
    )}</div>${e.selection?.reasons?.length ? `<p class="source-note">${e.selection.reasons.map(esc).join(". ")}</p>` : ""}`;
}
function flightawareHtml(context) {
  if (!context || !["matched", "candidate"].includes(context.state)) return "";
  const flight = context.flight;
  const airport = (value) =>
    value?.code_icao ||
    value?.code_iata ||
    value?.code_lid ||
    "Airport unavailable";
  const rows = [
    ["From", airport(flight.origin)],
    ["To", airport(flight.destination)],
    ["Reported status", flight.status || "Unavailable"],
    ["Retrieved", fullDate(context.retrieved_at)],
  ];
  if (flight.actual_off)
    rows.push([
      "Reported departure",
      new Date(flight.actual_off).toLocaleString(),
    ]);
  if (flight.actual_on)
    rows.push([
      "Reported arrival",
      new Date(flight.actual_on).toLocaleString(),
    ]);
  else if (flight.estimated_on)
    rows.push([
      "Estimated arrival",
      new Date(flight.estimated_on).toLocaleString(),
    ]);
  return `<details data-section="flightaware"><summary>${context.review_required ? "Possible FlightAware match" : "FlightAware context"} · ${esc(airport(flight.origin))} to ${esc(airport(flight.destination))}</summary><div class="evidence-grid">${rows.map(([label, value]) => `<div><span>${esc(label)}</span><strong>${esc(value)}</strong></div>`).join("")}</div>${flight.diverted === true ? '<p class="source-note">FlightAware marked this flight as diverted.</p>' : ""}<p class="source-note">${context.review_required ? "Callsign and time match only; aircraft registration is unconfirmed. " : ""}${esc(context.caveat)}</p>${extLink(context.source_url, "View on FlightAware")}</details>`;
}
function ownershipHtml(e) {
  const fleet = e.training_context;
  const owner = e.flightaware_owner;
  if (!fleet && !owner) return "";
  const listing = fleet
    ? `<p>${esc(fleet.organization)} listed this aircraft in its ${esc(fleet.association)} fleet. Checked ${esc(fleet.checked_on)}.</p><p class="source-note">${esc(fleet.caveat)}</p>${extLink(fleet.source_url, "Fleet listing")}`
    : "";
  const reportedOwner = owner
    ? `<p>FlightAware registered owner: ${esc(owner.name || "Unavailable")}. Retrieved ${esc(fullDate(owner.retrieved_at))}.</p>${owner.training_hint ? "<p>The owner name suggests a club or school; operator research is still needed.</p>" : ""}<p class="source-note">${esc(owner.caveat)}</p>`
    : "";
  return `<details data-section="ownership"><summary>School, club and owner context</summary>${listing}${reportedOwner}</details>`;
}
function card(e, expanded = false) {
  const a = e.aircraft;
  const raw = e.evidence.aircraft;
  const story = expanded ? storyHtml(e) : "";
  const info = [
    ["Source", sourceName(a.type)],
    ["Observations", fmt(e.hits)],
    ["Evidence from", fullDate(e.evidence.observed_at)],
    ["Ground speed", raw.gs == null ? "Not preserved" : fmt(raw.gs, 1) + " kn"],
    ["Altitude", altitudeText(raw)],
    ["Squawk", raw.squawk || "Not preserved"],
  ];
  return `<article class="event ${esc(e.category)}" id="sighting-${e.id}"><div class="event-top"><span class="tag">${esc(labels[e.category] || e.category)}</span><time datetime="${new Date(e.last_seen * 1000).toISOString()}" title="${esc(fullDate(e.last_seen))}">${esc(date(e.last_seen))}</time></div><div class="event-head"><h3>${esc(e.title)}</h3>${e.value != null ? `<div class="event-value">${valueHtml(e)}</div>` : ""}</div><div class="aircraft-meta">${aircraftHtml(a)}</div><p class="why">${esc(e.why)}</p>${expanded ? "" : e.draft_headline ? `<p class="note-preview"><span>✎</span> Story draft: ${esc(e.draft_headline)}</p>` : e.note ? `<p class="note-preview"><span>✳</span> ${esc(e.note.summary)}</p>` : ""}${reasonsHtml(e)}<div class="event-bottom"><span class="confidence">${esc(e.confidence)} <span>·</span> ${fmt(e.hits)} observations</span><button type="button" class="map-link note-open" data-event="${e.id}">${expanded ? "Permalink ↗" : e.draft_headline ? "Read story draft ↗" : e.note ? "Read field notes ↗" : "Open sighting ↗"}</button></div><details data-section="evidence" ${expanded ? "open" : ""}><summary>Evidence &amp; interpretation</summary><p>${esc(e.explanation)}</p><div class="evidence-grid">${info.map(([k, v]) => `<div><span>${esc(k)}</span><strong>${esc(v)}</strong></div>`).join("")}</div>${e.evidence.previous_position ? `<p class="evidence-extra">Prior position: ${esc(JSON.stringify(e.evidence.previous_position))}. New position: ${esc(raw.lat)}, ${esc(raw.lon)}.</p>` : ""}${annotationsHtml(e)}${expanded ? participantsHtml(e) : ""}<p class="source-note">${esc(e.origin)} · First observed ${esc(fullDate(e.first_seen))}${e.evidence.archive ? " · Archive " + esc(e.evidence.archive) : ""}</p>${extLink(mapUrl(e.hex), "Find this address on the map")}</details>${flightawareHtml(e.flightaware)}${ownershipHtml(e)}${reviewHtml(e)}${expanded ? contextHtml(e) + story + (e.note || !story ? noteHtml(e) : "") : ""}</article>`;
}
function render(data) {
  $("status").classList.toggle("stale", !data.healthy);
  $("status").innerHTML =
    `<i></i><span>${archiveMode ? "Read-only archive" : data.healthy ? "Live · " + fmt(data.live_aircraft) + " aircraft" : "Saved history · feed stale"}</span>`;
  $("notice").hidden = data.healthy;
  $("notice").textContent =
    data.error ||
    "The receiver has not supplied fresh data recently. Your saved sightings are still available.";
  $("stat-finds").textContent = fmt(data.event_count_24h);
  $("stat-airframes").textContent = fmt(data.categories_24h.airframes || 0);
  $("stat-oddities").textContent = fmt(data.categories_24h.oddities || 0);
  $("stat-unique").textContent = fmt(data.unique_ids);
  const featured = data.events[0];
  const spotlight = featured
    ? `<div class="eyebrow">↗ CAUGHT BY YOUR RECEIVER</div><div class="feature-label">${esc(featured.title)}</div>${featured.value == null ? `<h2>${esc(featured.aircraft.desc || featured.aircraft.r || featured.aircraft.t || featured.hex)}</h2>` : `<div class="feature-number">${valueHtml(featured)}</div>`}<div class="feature-meta">${esc(featured.aircraft.r || featured.hex)} · ${esc(featured.aircraft.t || sourceName(featured.aircraft.type))} · ${esc(date(featured.evidence.observed_at))}</div><p class="feature-note">${esc(featured.note?.summary || featured.why)}</p><button type="button" class="feature-link note-open" data-event="${featured.id}">The story behind the number →</button>`
    : `<div class="eyebrow">KEEPING AN EYE ON YOUR SKY</div><h2>Curiosity stays on.</h2><p class="feature-note">No sightings match these filters yet. New discoveries appear automatically, with the evidence that triggered them.</p>`;
  const html = data.events.length
    ? data.events.map((e) => card(e)).join("")
    : `<div class="empty"><span class="empty-icon">⌖</span><h3>Nothing tripped these rules.</h3><p>${category === "airframes" ? "Military, vintage and special-airframe sightings will land here. Local rarity is learning from your traffic." : "Try a longer time period or another category. The detector keeps watching every ten seconds."}</p></div>`;
  // Unchanged markup keeps its DOM, so focus and open sections survive a refresh;
  // a redraw puts focus back on the same control of the same sighting.
  const focused = focusKey(document.activeElement);
  if (spotlight !== spotlightHtml)
    $("spotlight").innerHTML = spotlightHtml = spotlight;
  if (html !== eventsHtml) {
    const opened = [
      ...document.querySelectorAll("#events .event details[open]"),
    ].map((el) => [el.closest(".event").id, el.dataset.section]);
    $("events").innerHTML = eventsHtml = html;
    for (const [id, section] of opened) {
      const el = document
        .getElementById(id)
        ?.querySelector(`details[data-section="${section}"]`);
      if (el) el.open = true;
    }
  }
  if (focused && document.activeElement !== focused.element)
    document.querySelector?.(focused.selector)?.focus?.();
  $("result-caption").textContent =
    `${fmt(data.matching_events)} matching ${["all", "drafts"].includes($("view").value) ? "observations" : "encounters"}${data.matching_events > data.events.length ? " · showing the first " + data.events.length + "; narrow your search or export all" : ""}`;
  $("export").href = apiRoot + "export.csv?" + params();
  $("json-export").href = apiRoot + "export.json?" + params();
  const r = data.rarity;
  $("rarity-status").textContent = r.ready
    ? "Local rarity detection is active"
    : "Building a trustworthy baseline";
  $("rarity-progress").style.width =
    Math.min(100, 100 * Math.min(r.days / 7, r.hours / 24, r.total / 250)) +
    "%";
  $("rarity-context").textContent =
    `${r.days}/7 observation days · ${fmt(r.hours, 1)}/24 hours · ${fmt(r.total)} unique ICAO IDs. ${r.ready ? "Comparing the last 30 days." : "Frequency is shown now; rarity labels wait for enough coverage."}`;
  $("types").innerHTML =
    data.types
      .map(
        (t) =>
          `<div class="type-row"><div><strong>${esc(t.type)}</strong>${t.description ? `<small>${esc(t.description)}</small>` : ""}</div><div class="type-count"><b>${fmt(t.count)}</b><small>${fmt(t.share, 2)}% of IDs</small></div></div>`,
      )
      .join("") ||
    '<p class="muted">Waiting for aircraft type information.</p>';
  $("records").innerHTML = [
    ["speed", "Highest speed report · may be an outlier", "kn"],
    ["altitude", "Highest altitude report", "ft"],
    ["range", "Farthest map position band", "nm"],
  ]
    .map(([key, label, unit]) => {
      const r = data.records[key];
      const value = !r
        ? "—"
        : key === "range"
          ? rangeBand(r.value)
          : fmt(r.value, key === "altitude" ? 0 : 1);
      return `<div class="record"><span class="record-label">${label}</span><strong>${value} <small>${unit}</small></strong>${r && mapUrl(r.hex) ? `<a href="${esc(mapUrl(r.hex))}" target="_blank" rel="noreferrer">${esc(r.registration || r.hex)} · ${esc(r.typecode || "Type unavailable")} ↗</a>` : ""}</div>`;
    })
    .join("");
  $("updated").textContent = archiveMode ? `Archive captured ${fullDate(data.captured_at)}` : data.last_success
    ? `Updated ${clock(data.last_success)} · auto-refresh 15s`
    : "Awaiting first receiver snapshot · auto-refresh 15s";
  $("ai-status").textContent = archiveMode
    ? `${fmt(data.ai?.drafts || 0)} saved drafts · ${fmt(data.ai?.notes || 0)} field notes`
    : "Factual observations and cached enrichment";
  $("ai-next").textContent = archiveMode
    ? `Read-only archive captured ${fullDate(data.captured_at)}`
    : "Paid writing runs in the publisher";
  $("archive-label").textContent = archiveMode
    ? `Read-only archive · ${fullDate(data.captured_at)}. Date filters use this capture time.`
    : "Live factual journal";
  $("flightaware-status").textContent = data.flightaware?.status || "Disabled";
  $("flightaware-budget").textContent = data.flightaware?.enabled
    ? `$${fmt(data.flightaware.monthly_reserved_usd, 3)} reserved of $${fmt(data.flightaware.monthly_budget_usd, 2)} this month. FlightAware account credits and other usage are separate.`
    : "Flight context uses cached, attributed data from FlightAware.";
  if (data.archive_scan)
    $("archive-context").textContent =
      `${fmt(data.archive_scan.files || 0)} replay files searched · ${fmt(data.archive_scan.days || 0)} days of saved history`;
}
// Polling: the next automatic poll is scheduled when the current request has
// finished, so a slow receiver never has several polls racing. A filter change
// still refreshes at once and aborts the request it replaces. A request that
// neither answers nor fails within STALL_MS is aborted as well and counts as
// failed, so polling resumes after a hung connection.
const POLL_MS = 15000,
  STALL_MS = 45000;
let summaryRequest = null,
  pollTimer = 0;
function schedule() {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(poll, POLL_MS);
}
function poll() {
  if (summaryRequest) return;
  if (document.hidden) schedule();
  else refresh();
}
// The signal a summary request passes to fetch(): aborted when a newer request
// replaces it, or after STALL_MS. AbortSignal.any() and AbortSignal.timeout()
// need Safari 17.4, Chrome 116, or Firefox 124; without them a timer aborts the
// request's own controller and marks the request stalled, so a stall still
// counts as a failure rather than a replacement. clear() stops that timer.
function requestSignal(controller) {
  if (
    typeof AbortSignal.any === "function" &&
    typeof AbortSignal.timeout === "function"
  ) {
    return {
      signal: AbortSignal.any([
        controller.signal,
        AbortSignal.timeout(STALL_MS),
      ]),
      stalled: false,
      clear() {},
    };
  }
  const request = { signal: controller.signal, stalled: false };
  const timer = setTimeout(() => {
    request.stalled = true;
    controller.abort();
  }, STALL_MS);
  request.clear = () => clearTimeout(timer);
  return request;
}
async function refresh() {
  summaryRequest?.abort();
  const controller = (summaryRequest = new AbortController());
  const request = requestSignal(controller);
  try {
    const response = await fetch(apiRoot + "summary?" + params(), {
      cache: "no-store",
      signal: request.signal,
    });
    if (!response.ok) throw new Error("Server unavailable");
    const data = await response.json();
    if (!controller.signal.aborted) render(data);
  } catch {
    // A newer request replaced this one and reports for both.
    if (controller.signal.aborted && !request.stalled) return;
    $("status").classList.add("stale");
    $("status").innerHTML = "<i></i><span>Connection interrupted</span>";
    $("notice").hidden = false;
    $("notice").textContent =
      "The dashboard connection is interrupted. Displayed sightings are saved data; check your connection to the private viewer.";
  } finally {
    request.clear();
    if (summaryRequest === controller) {
      summaryRequest = null;
      schedule();
    }
  }
}
// The dialog's own status line: review results stay with the sighting they concern.
function noteStatus(text) {
  const region = $("note-status");
  if (!region) return;
  region.textContent = text || "";
  region.hidden = !text;
}
async function openNote(id) {
  const generation = ++sightingDialog.generation;
  try {
    const response = await fetch(apiRoot + "event/" + encodeURIComponent(id));
    if (!response.ok) throw new Error("Not found");
    const e = await response.json();
    if (generation !== sightingDialog.generation) return;
    $("note-content").innerHTML = card(e, true);
    $("note-permalink").href = pageRoot + "?sighting=" + e.id;
    sightingDialog.id = String(e.id);
    noteStatus("");
    // Reopening after a saved review must not call showModal() on an open dialog.
    if (!$("note-dialog").open) $("note-dialog").showModal();
  } catch {
    if (generation !== sightingDialog.generation) return;
    $("notice").hidden = false;
    $("notice").textContent = "That saved sighting could not be loaded.";
  }
}
// Saves a review; the dialog is refreshed only while it still shows that sighting,
// so a slow reply never replaces a sighting the reader has moved on to.
async function saveReview(review) {
  if (archiveMode) return;
  const target = String(review.dataset.event);
  const inDialog = Boolean(review.closest?.("#note-dialog"));
  review.disabled = true;
  try {
    const response = await fetch(apiRoot + "feedback", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        event_id: Number(target),
        verdict: review.dataset.verdict,
      }),
    });
    if (!response.ok) throw new Error("Review unavailable");
    const shown = $("note-dialog").open && sightingDialog.id === target;
    if (shown) noteStatus("");
    await Promise.all([shown ? openNote(target) : null, refresh()]);
  } catch {
    const message = "The review could not be saved. Please try again.";
    if (inDialog && $("note-dialog").open && sightingDialog.id === target) {
      noteStatus(message);
    } else {
      $("notice").hidden = false;
      $("notice").textContent = message;
    }
  } finally {
    review.disabled = false;
  }
}
for (const button of document.querySelectorAll("[data-category]"))
  button.addEventListener("click", () => {
    category = button.dataset.category;
    for (const b of document.querySelectorAll("[data-category]")) {
      b.classList.toggle("active", b === button);
      b.setAttribute("aria-pressed", b === button ? "true" : "false");
    }
    refresh();
  });
for (const id of ["period", "sort", "view"])
  $(id).addEventListener("change", refresh);
let searchTimer;
$("search").addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(refresh, 250);
});
for (const id of ["how-button", "rules-button"])
  $(id).addEventListener("click", () => $("how-dialog").showModal());
$("close-dialog").addEventListener("click", () => $("how-dialog").close());
$("close-note").addEventListener("click", () => $("note-dialog").close());
$("note-dialog").addEventListener("close", () => {
  sightingDialog.generation += 1;
  sightingDialog.id = null;
  noteStatus("");
});
for (const id of ["how-dialog", "note-dialog"])
  $(id).addEventListener("click", (e) => {
    if (e.target === $(id)) $(id).close();
  });
document.addEventListener("click", (e) => {
  const review = e.target.closest(".review-save");
  if (review) {
    saveReview(review);
    return;
  }
  const button = e.target.closest(".note-open");
  if (button) {
    if ($("note-dialog").open) {
      window.location.href = pageRoot + "?sighting=" + button.dataset.event;
    } else openNote(button.dataset.event);
  }
});
refresh();
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) poll();
});
const sighting = new URLSearchParams(location.search).get("sighting");
if (sighting && /^\d+$/.test(sighting)) openNote(sighting);

if (!archiveMode) document.querySelector('option[value="drafts"]')?.remove();
