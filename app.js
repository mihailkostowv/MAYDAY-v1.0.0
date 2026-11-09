
"use strict";

const $ = (id) => document.getElementById(id);
const START_YEAR = 1939;
const CURRENT_YEAR = 2026;
const LIST_PAGE_SIZE = 50;
const startedAt = performance.now();
const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

let viewer;
let markerSource;
let markerBackfillSource;
let events = [];
let filteredEvents = [];
let selectedId = null;
let detailRequest = 0;
let listLimit = LIST_PAGE_SIZE;
let loading = false;
let archiveTotal = 0;
const eventsById = new Map();
const markersById = new Map();
const routeEntities = [];
let routePositions = [];
let selectedRoute;
let markerBuildIndex = 0;
let markerBuildQueue = null;
let markerFirstBatchComplete = false;
let markerBuildGeneration = 0;
let markerBuildScheduled = false;
let markerVisibleIds = new Set();
let searchTimer;
let markerImage = "/static/assets/plane-marker.svg";
let markerImageReady = false;

class SpatialMarkerLayer {
  constructor(scene) {
    this.scene = scene;
    this.regions = new Map();
    this._show = true;
    this.removeListener = scene.preUpdate.addEventListener(() => this.updateVisibility());
  }
  get show() { return this._show; }
  set show(value) { this._show = value; this.updateVisibility(); }
  add(options) {
    const point = Cesium.Cartographic.fromCartesian(options.position);
    const lat = Cesium.Math.toDegrees(point.latitude), lon = Cesium.Math.toDegrees(point.longitude);
    const row = Math.min(5, Math.floor((lat + 90) / 30));
    const col = Math.min(11, Math.floor((lon + 180) / 30));
    const key = row * 12 + col;
    let region = this.regions.get(key);
    if (!region) {
      const latitude = Cesium.Math.toRadians(row * 30 - 75);
      const longitude = Cesium.Math.toRadians(col * 30 - 165);
      region = {
        normal: new Cesium.Cartesian3(Math.cos(latitude) * Math.cos(longitude), Math.cos(latitude) * Math.sin(longitude), Math.sin(latitude)),
        collection: this.scene.primitives.add(new Cesium.BillboardCollection({ scene: this.scene, blendOption: Cesium.BlendOption.TRANSLUCENT }))
      };
      region.collection.show = this._show;
      this.regions.set(key, region);
    }
    return region.collection.add(options);
  }
  updateVisibility() {
    const camera = this.scene.camera.positionWC;
    const distance = Cesium.Cartesian3.magnitude(camera);

    const threshold = Math.cos(Math.acos(Math.min(1, 6356752 / distance)) + Math.PI / 6 + 0.04);
    for (const region of this.regions.values()) {
      region.collection.show = this._show && Cesium.Cartesian3.dot(camera, region.normal) / distance >= threshold;
    }
  }
  removeAll() {
    for (const region of this.regions.values()) this.scene.primitives.remove(region.collection);
    this.regions.clear();
  }
  destroy() { this.removeListener(); this.removeAll(); }
}


function preloadMarkerImage() {
  const image = new Image();
  image.onload = () => {
    markerImage = image;
    markerImageReady = true;
    scheduleMarkerBuild();
    if (viewer) viewer.scene.requestRender();
  };
  image.onerror = () => {
    
    markerImageReady = true;
    scheduleMarkerBuild();
  };
  image.src = markerImage;
}

function clearRouteGraphics() {
  selectedRoute = undefined;
  if (viewer) {
    routeEntities.forEach(entity => viewer.entities.remove(entity));
    viewer.scene.requestRender();
  }
  routeEntities.length = 0;
  routePositions = [];
}

function cameraDuration(range) {
  if (reducedMotion) return 0;
  const height = Math.max(1, viewer.camera.positionCartographic.height);
  return Math.min(3.4, 1.8 + Math.abs(Math.log10(height / Math.max(1, range))) * 0.6);
}

function fitSelectedRoute() {
  if (!viewer || !routePositions.length) return;
  const sphere = Cesium.BoundingSphere.fromPoints(routePositions);
  sphere.radius = Math.max(sphere.radius * 1.35, 12000);
  let range = Math.max(40000, sphere.radius * 3.6);
  if (window.innerWidth > 760 && !$("detailPanel").hidden) {
    const width = Math.max(1, viewer.scene.canvas.clientWidth);
    const cardWidth = $("detailPanel").getBoundingClientRect().width;
    range *= Math.sqrt(width / Math.max(width * 0.4, width - cardWidth));
  }
  viewer.camera.cancelFlight();
  viewer.camera.flyToBoundingSphere(sphere, {
    duration: cameraDuration(range),
    easingFunction: Cesium.EasingFunction.SINUSOIDAL_IN_OUT,
    offset: new Cesium.HeadingPitchRange(0, -Math.PI / 2, range)
  });
}

function drawRoute(event, route, focus) {
  clearRouteGraphics();
  const available = route && route.route_kind === "reconstructed" && hasCoordinates(route.departure) && hasCoordinates(event)
    && safeSource(route.source_url) && safeSource(route.departure.source_url);
  showHeading(event, available ? route : null);
  selectedRoute = available ? route : null;
  const selectedMarker = markersById.get(String(event.id));
  if (selectedMarker) selectedMarker.billboard.alignedAxis = markerAxis(event, available ? route : null);
  $("routeBadge").hidden = !available;
  $("fitRoute").hidden = !available || !viewer;
  $("routeEvidence").hidden = !available;
  $("routeEvidence").open = false;
  const source = available ? safeSource(route.source_url) : null;
  $("routeSource").hidden = !source;
  if (source) $("routeSource").href = source;
  else $("routeSource").removeAttribute("href");
  const originSource = available ? safeSource(route.departure.source_url) : null;
  $("routeOriginSource").hidden = !originSource;
  if (originSource) $("routeOriginSource").href = originSource;
  else $("routeOriginSource").removeAttribute("href");
  if (!available) {
    $("routeDescription").textContent = "No route available";
    $("routeNote").textContent = "";
    return false;
  }
  $("routeDescription").textContent = (route.departure_label || "Departure") + ": " + route.departure.name + " → Event location";
  $("routeNote").textContent = "Approximate connection. Exact flight track unavailable.";
  $("routeEvidenceNote").textContent = (route.note || "")
    + (route.destination_name ? " Intended destination: " + route.destination_name + "." : "");
  if (!viewer) return true;
  const start = Cesium.Cartesian3.fromDegrees(route.departure.longitude, route.departure.latitude, 30);
  const end = Cesium.Cartesian3.fromDegrees(event.longitude, event.latitude, 30);
  routePositions = [start, end];
  routeEntities.push(viewer.entities.add({
    name: "Approximate route to event location",
    polyline: {
      positions: routePositions, width: 2.5,
      arcType: Cesium.ArcType.GEODESIC,
      material: Cesium.Color.fromCssColorString("#28e0d38c")
    }
  }));
  routeEntities.push(viewer.entities.add({
    position: start,
    point: { pixelSize: 8, color: Cesium.Color.fromCssColorString("#ffffff99"), outlineColor: Cesium.Color.fromCssColorString("#28e0d399"), outlineWidth: 2 },
    label: {
      text: route.departure.name, font: '12px "Mayday Archivo", sans-serif',
      fillColor: Cesium.Color.WHITE, showBackground: true,
      backgroundColor: Cesium.Color.fromCssColorString("#191d22e8"),
      backgroundPadding: new Cesium.Cartesian2(7, 5),
      pixelOffset: new Cesium.Cartesian2(0, -22),
      verticalOrigin: Cesium.VerticalOrigin.BOTTOM
    }
  }));
  if (focus) fitSelectedRoute();
  viewer.scene.requestRender();
  return true;
}

function showEventRoutes(event, focus = false) {
  const routes = Array.isArray(event.routes) ? event.routes : [];
  $("routeChoice").replaceChildren();
  routes.forEach((route, index) => {
    const option = document.createElement("option");
    option.value = String(index);
    option.textContent = route.aircraft || route.departure?.name || "Source route " + (index + 1);
    $("routeChoice").append(option);
  });
  $("routeChoice").hidden = routes.length <= 1;
  $("routeChoiceLabel").hidden = routes.length <= 1;
  return drawRoute(event, routes[0], focus);
}


function hasCoordinates(event) {
  if (!event || typeof event !== "object") return false;
  return typeof event.latitude === "number" && Number.isFinite(event.latitude)
    && typeof event.longitude === "number" && Number.isFinite(event.longitude)
    && event.latitude >= -90 && event.latitude <= 90
    && event.longitude >= -180 && event.longitude <= 180;
}

function eventCategory(event = {}) {
  const value = String(event.category || "").trim().toLowerCase().replace(/[\s-]+/g, "_");
  return ["accident", "combat_loss", "terrorist_attack", "incident"].includes(value) ? value : "other";
}

function categoryLabel(event) {
  return { accident: "Accident", combat_loss: "Combat / shootdown", incident: "Incident", terrorist_attack: "Terrorist attack", other: "Unclassified" }[eventCategory(event)];
}

function eventOutcome(event) {
  return ["fatal", "nonfatal"].includes(event.fatality_status) ? event.fatality_status : "unknown";
}

function deathLabel(event) {
  return Number.isInteger(event.fatalities) && event.fatalities >= 0
    ? event.fatalities.toLocaleString() + (event.fatalities === 1 ? " reported death" : " reported deaths")
    : "Death toll unknown";
}

function showEventSummary(event) {
  $("detailFatalities").textContent = deathLabel(event);
  $("detailFatalityNote").textContent = [event.fatalities_scope, event.fatality_note].filter(Boolean).join(". ");
  $("detailSummary").textContent = event.summary || "A detailed account is not yet available for this event.";
  $("detailSummaryNote").textContent = event.summary_note || "";
  const countUrl = safeSource(event.fatalities_source_url);
  $("detailFatalitySource").hidden = !countUrl;
  if (countUrl) $("detailFatalitySource").href = countUrl;
  else $("detailFatalitySource").removeAttribute("href");
  const url = safeSource(event.summary_source_url || event.fatalities_source_url || event.source_url);
  $("detailSummarySource").hidden = !url;
  if (url) $("detailSummarySource").href = url;
  else $("detailSummarySource").removeAttribute("href");
}

function eventYear(event) {
  const match = String(event.event_date || "").match(/^(\d{4})(?:-|$)/);
  return match ? Number(match[1]) : null;
}

function formatDate(value) {
  if (!value) return "Date not recorded";
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return String(value);
  const date = new Date(value + "T00:00:00Z");
  if (Number.isNaN(date.getTime()) || date.toISOString().slice(0, 10) !== value) return String(value);
  return date.toLocaleDateString("en-GB", {
    day: "numeric", month: "short", year: "numeric", timeZone: "UTC"
  });
}


function safeSource(value) {
  try {
    const url = new URL(value);
    return ["https:", "http:"].includes(url.protocol) ? url.href : null;
  } catch { return null; }
}

async function fetchJSON(path, cache = "no-store") {
  const response = await fetch(path, { cache, signal: AbortSignal.timeout(15000) });
  if (!response.ok) {
    let message = "Request failed (HTTP " + response.status + ").";
    try {
      const body = await response.json();
      if (typeof body.detail === "string") message = body.detail;
    } catch { }
    throw new Error(message);
  }
  return response.json();
}

async function loadEvents() {
  if (loading) return;
  loading = true;
  $("refreshData").disabled = true;
  $("dataStatus").textContent = "Loading your archive…";
  $("statusDot").className = "status-dot";
  try {
    const index = await fetchJSON("/event-index", "no-cache");
    if (index.version !== 1 || !Array.isArray(index.events) || !Array.isArray(index.columns)) throw new Error("Unexpected event index. Restart the updated API.");
    const stats = index.stats;
    if (!Number.isInteger(stats.total) || stats.total < 0) throw new Error("Unexpected /stats response.");
    $("archiveTotal").textContent = stats.total.toLocaleString();
    $("coverageSummary").textContent = (stats.sources || []).map(source => source.source_name + ": " + Number(source.records).toLocaleString()).join(" · ") + ". Coverage is incomplete. Source coordinates may be approximate; overlapping sources may contain duplicate events.";
    $("archiveMapped").textContent = Number(stats.mapped || 0).toLocaleString();
    const loaded = index.events.map(row => {
      const event = Object.fromEntries(index.columns.map((column, i) => [column, row[i]]));
      event._search = [event.name, event.country, event.aircraft].join(" ").toLocaleLowerCase();
      event._year = eventYear(event);
      event._category = eventCategory(event);
      event._outcome = eventOutcome(event);
      return event;
    });
    if (loaded.length !== stats.total) throw new Error("The event index is incomplete. Refresh the archive to retry.");
    ++markerBuildGeneration;
    markerBuildScheduled = false;
    markerBuildIndex = 0;
    markerBuildQueue = null;
    markerFirstBatchComplete = false;
    if (markerSource) markerSource.removeAll();
    if (markerBackfillSource) {
      markerBackfillSource.removeAll();
      markerBackfillSource.show = false;
    }
    markersById.clear();
    events = loaded;
    archiveTotal = stats.total;
    eventsById.clear();
    events.forEach((event) => eventsById.set(String(event.id), event));
    $("archiveTotal").textContent = stats.total.toLocaleString();
    $("coverageSummary").textContent = (stats.sources || []).map(source => source.source_name + ": " + Number(source.records).toLocaleString()).join(" · ") + ". Coverage is incomplete. Source coordinates may be approximate; overlapping sources may contain duplicate events.";
    $("archiveMapped").textContent = Number(stats.mapped || 0).toLocaleString();
    $("statusDot").className = "status-dot ready";
    $("dataStatus").textContent = events.length < archiveTotal
      ? events.length.toLocaleString() + " of " + archiveTotal.toLocaleString() + " loaded. Filters apply to loaded records."
      : "All " + events.length.toLocaleString() + " archive records loaded · " + new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    applyFilters();
    console.debug("MAYDAY: index ready in " + Math.round(performance.now() - startedAt) + "ms; " + events.length + " records.");
    if (selectedId && eventsById.has(selectedId)) selectEvent(selectedId, false, false);
  } catch (error) {
    $("statusDot").className = "status-dot error";
    $("dataStatus").textContent = error.message || "Could not connect to the archive.";
    if (!events.length) {
      $("resultCount").textContent = "Archive unavailable";
      $("eventList").replaceChildren();
      const message = document.createElement("p");
      message.className = "empty-state";
      message.textContent = "The archive could not be loaded. Check the message below, then use the refresh button.";
      $("eventList").append(message);
    }
  } finally {
    loading = false;
    $("refreshData").disabled = false;
  }
}

function applyFilters() {
  clearTimeout(searchTimer);
  const span = $("timeSpan").value;
  const [from, to] = span === "all" ? [START_YEAR, CURRENT_YEAR] : span.split("-").map(Number);
  const valid = Number.isInteger(from) && Number.isInteger(to)
    && from >= START_YEAR && to <= CURRENT_YEAR && from <= to;
  $("filterError").hidden = valid;
  if (!valid) {
    $("filterError").textContent = "Choose a time span from " + START_YEAR + " to " + CURRENT_YEAR + ".";
    return;
  }
  const query = $("search").value.trim().toLocaleLowerCase();
  const category = $("category").value;
  const outcome = $("outcome").value;
  const fullDateRange = from === START_YEAR && to === CURRENT_YEAR;
  filteredEvents = events.filter((event) => {
    const year = event._year !== undefined ? event._year : eventYear(event);
    const dateMatches = year === null ? fullDateRange : year >= from && year <= to;
    const searchText = event._search ?? [event.name, event.country, event.aircraft].join(" ").toLocaleLowerCase();
    return dateMatches && searchText.includes(query)
      && (category === "all" || (event._category ?? eventCategory(event)) === category)
      && (outcome === "all" || (event._outcome ?? eventOutcome(event)) === outcome);
  });
  listLimit = LIST_PAGE_SIZE;
  if (selectedId !== null && !filteredEvents.some((event) => String(event.id) === selectedId)) closeDetails();
  renderList();
  renderMarkers();
}

function renderList() {
  $("resultCount").textContent = filteredEvents.length.toLocaleString() + " matching "
    + (filteredEvents.length === 1 ? "record" : "records");
  $("eventList").replaceChildren();
  const fragment = document.createDocumentFragment();
  for (const event of filteredEvents.slice(0, listLimit)) {
    const button = document.createElement("button");
    button.className = "event-card";
    button.type = "button";
    button.dataset.eventId = String(event.id);
    button.setAttribute("aria-pressed", String(String(event.id) === selectedId));
    const top = document.createElement("span");
    top.className = "event-card-top";
    top.textContent = formatDate(event.event_date) + "  /  " + categoryLabel(event)
      + " · " + ({fatal: "Fatal", nonfatal: "Nonfatal", unknown: "Outcome unknown"}[eventOutcome(event)]);
    const title = document.createElement("span");
    title.className = "event-card-title";
    title.textContent = event.name || "Unnamed event";
    const meta = document.createElement("span");
    meta.className = "event-card-meta";
    meta.textContent = (event.country || "Country not recorded") + " · "
      + (event.aircraft || "Aircraft not recorded")
      + (hasCoordinates(event) ? "" : " · Location unavailable");
    const deaths = document.createElement("span");
    deaths.className = "event-card-deaths " + eventOutcome(event);
    deaths.textContent = deathLabel(event);
    button.append(top, title, meta, deaths);
    button.addEventListener("click", () => selectEvent(String(event.id), true));
    fragment.append(button);
  }
  if (!filteredEvents.length) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.textContent = events.length
      ? "No records match these filters. Try another search or reset the filters."
      : "Your archive is empty. Add a sourced event, then refresh the archive.";
    fragment.append(empty);
  }
  $("eventList").append(fragment);
  $("moreResults").hidden = filteredEvents.length <= listLimit;
}


const headingEvidence = new Map([
  ["First Air Flight 6560|2011-08-20", {
    degrees: 348, reference: "true", reviewed: true,
    source: "https://www.tsb.gc.ca/sites/default/files/rapports-reports/aviation/A11H0002/eng/a11h0002.pdf",
    note: "Investigator estimate at 16:40:35 UTC on final approach, about 77 seconds before impact. Not the heading at impact. TSB A11H0002, section 1.11.2.4, Figure 3 and Table 8."
  }]
]);

function documentedHeading(event) {
  const evidence = headingEvidence.get(event.name + "|" + event.event_date);
  return evidence?.reviewed === true && evidence.reference === "true"
    && Number.isFinite(evidence.degrees) && evidence.degrees >= 0 && evidence.degrees < 360
    && safeSource(evidence.source) && evidence.note ? evidence : null;
}

function routeBearing(event, route) {
  const value = route === undefined ? event.route_bearing : route?.arrival_bearing;
  return typeof value === "number" && Number.isFinite(value) && value >= 0 && value < 360 ? value : null;
}

function markerAxis(event, route) {
  const evidence = documentedHeading(event);
  const degrees = evidence ? evidence.degrees : routeBearing(event, route);
  const north = northAt(event.latitude, event.longitude);
  if (degrees === null) return north;
  
  const lon = Cesium.Math.toRadians(event.longitude);
  const h = Cesium.Math.toRadians(degrees);
  return new Cesium.Cartesian3(
    north.x * Math.cos(h) - Math.sin(lon) * Math.sin(h),
    north.y * Math.cos(h) + Math.cos(lon) * Math.sin(h),
    north.z * Math.cos(h)
  );
}

function showHeading(event, route) {
  const evidence = documentedHeading(event);
  const bearing = routeBearing(event, route);
  const source = evidence?.source || (bearing !== null ? safeSource(route?.source_url) : null);
  $("detailHeading").textContent = evidence ? evidence.degrees + "° true (investigator estimate)"
    : bearing !== null ? Math.round(bearing) % 360 + "° true (route estimate)" : "Unknown";
  $("detailHeadingNote").textContent = evidence ? evidence.note
    : bearing !== null ? "Icon aligned with the approximate route's arrival direction. This is not a verified aircraft heading before impact."
    : "Neutral icon orientation. No unambiguous route direction or verified aircraft heading is available.";
  $("detailHeadingSource").hidden = !source;
  $("detailHeadingSource").textContent = evidence ? "Heading evidence ↗" : "Route source ↗";
  if (source) $("detailHeadingSource").href = source;
  else $("detailHeadingSource").removeAttribute("href");
}


function northAt(latitude, longitude) {
  const lat = Cesium.Math.toRadians(latitude);
  const lon = Cesium.Math.toRadians(longitude);
  return new Cesium.Cartesian3(-Math.sin(lat) * Math.cos(lon), -Math.sin(lat) * Math.sin(lon), Math.cos(lat));
}

function renderMarkers() {
  const mapped = filteredEvents.filter(hasCoordinates);
  const unmapped = filteredEvents.length - mapped.length;
  $("mapCount").textContent = mapped.length.toLocaleString() + " mapped · " + unmapped.toLocaleString() + " without coordinates";
  if (!viewer || !markerSource) return;
  markerVisibleIds = new Set(mapped.map(event => String(event.id)));
  for (const [id, marker] of markersById) {
    const visible = markerVisibleIds.has(id);
    if (marker.billboard.show !== visible) marker.billboard.show = visible;
  }
  scheduleMarkerBuild();
  viewer.scene.requestRender();
}

function scheduleMarkerBuild() {
  if (!viewer || !markerSource || !markerImageReady || markerBuildScheduled || !events.length) return;
  if (markerBuildQueue === null) {
   
    const camera = viewer.camera.positionCartographic;
    const latitude = camera?.latitude ?? -10 * Math.PI / 180;
    const longitude = camera?.longitude ?? 120 * Math.PI / 180;
    const sinLat = Math.sin(latitude), cosLat = Math.cos(latitude);
    const center = [], edge = [], far = [];
    for (const event of events) {
      if (!hasCoordinates(event)) continue;
      const lat = event.latitude * Math.PI / 180, lon = event.longitude * Math.PI / 180;
      const facing = sinLat * Math.sin(lat) + cosLat * Math.cos(lat) * Math.cos(lon - longitude);
      (facing > 0.5 ? center : facing > 0 ? edge : far).push(event);
    }
    markerBuildQueue = center.concat(edge, far);
  }
  if (markerBuildIndex >= markerBuildQueue.length) return;
  markerBuildScheduled = true;
  const generation = markerBuildGeneration;
  requestAnimationFrame(() => {
    if (generation !== markerBuildGeneration) return;
    markerBuildScheduled = false;
    const batchStarted = performance.now();
    let added = 0;
    const firstBatch = !markerFirstBatchComplete;
    const collection = !firstBatch && markerBackfillSource ? markerBackfillSource : markerSource;
    
    while (markerBuildIndex < markerBuildQueue.length && added < 1000 && performance.now() - batchStarted < 8) {
      const event = markerBuildQueue[markerBuildIndex++];
      const id = String(event.id);
      if (markersById.has(id)) continue;
      const billboard = collection.add({
        id, position: Cesium.Cartesian3.fromDegrees(event.longitude, event.latitude, 10),
        image: markerImage, alignedAxis: markerAxis(event, String(event.id) === selectedId ? selectedRoute : undefined),
        width: 16, height: 16, show: markerVisibleIds.has(id),
        scale: id === selectedId ? 1.35 : 1,
        verticalOrigin: Cesium.VerticalOrigin.CENTER,
        horizontalOrigin: Cesium.HorizontalOrigin.CENTER
      });
      markersById.set(id, { billboard });
      added++;
    }
    if (added && firstBatch) {
      markerFirstBatchComplete = true;
      console.debug("MAYDAY: first plane batch prepared in " + Math.round(performance.now() - startedAt) + "ms.");
      viewer.scene.requestRender();
    }
    if (markerBuildIndex < markerBuildQueue.length) scheduleMarkerBuild();
    else {
      
      if (markerBackfillSource) markerBackfillSource.show = true;
      viewer.scene.requestRender();
      console.debug("MAYDAY: all " + markersById.size + " plane markers prepared in " + Math.round(performance.now() - startedAt) + "ms.");
    }
  });
}

async function selectEvent(id, moveCamera, scrollToPanel = true) {
  const event = eventsById.get(id);
  if (!event) return;
  const previousMarker = markersById.get(selectedId);
  if (previousMarker) {
    previousMarker.billboard.scale = 1;
    previousMarker.billboard.alignedAxis = markerAxis(eventsById.get(selectedId));
  }
  selectedId = id;
  const request = ++detailRequest;
  if (viewer) viewer.camera.cancelFlight();
  clearRouteGraphics();
  $("detailCategory").textContent = categoryLabel(event) + " / RECORD " + event.id;
  $("detailName").textContent = event.name || "Unnamed event";
  $("detailDate").textContent = formatDate(event.event_date);
  $("detailCountry").textContent = event.country || "Not recorded";
  $("detailAircraft").textContent = event.aircraft || "Not recorded";
  showHeading(event);
  $("detailCoordinates").textContent = hasCoordinates(event)
    ? event.latitude.toFixed(4) + ", " + event.longitude.toFixed(4) : "Unknown";
  $("detailNote").textContent = hasCoordinates(event)
    ? "Position from the archive record. Check the source for the location's precision and context."
    : "This event remains in the archive. A marker will appear once usable coordinates are recorded.";
  const url = safeSource(event.source_url);
  $("detailSource").hidden = !url;
  if (url) $("detailSource").href = url;
  else $("detailSource").removeAttribute("href");
  showEventSummary(event);
  const needsDetail = event.summary === undefined || event.routes === undefined;
  if (needsDetail) {
    $("detailSummary").textContent = "Loading the sourced account…";
    $("detailFatalityNote").textContent = "Loading count scope and source notes…";
    $("detailSummarySource").hidden = true;
    $("routeDescription").textContent = "Loading route information…";
    $("routeNote").textContent = "";
    for (const name of ["routeBadge", "fitRoute", "routeSource", "routeChoice", "routeChoiceLabel", "routeEvidence"]) $(name).hidden = true;
  }
  $("detailPanel").hidden = false;
  document.querySelectorAll(".event-card").forEach((button) => {
    button.setAttribute("aria-pressed", String(button.dataset.eventId === id));
  });
  const selectedMarker = markersById.get(id);
  if (selectedMarker) selectedMarker.billboard.scale = 1.35;
  const focusEvent = () => {
    const hasRoute = showEventRoutes(event, scrollToPanel);
    if (!hasRoute && viewer && hasCoordinates(event) && moveCamera) {
      viewer.camera.flyTo({
        destination: Cesium.Cartesian3.fromDegrees(event.longitude, event.latitude, 350000),
        duration: cameraDuration(350000),
        easingFunction: Cesium.EasingFunction.SINUSOIDAL_IN_OUT
      });
    }
  };
  if (!needsDetail) {
    focusEvent();
  }
  if (viewer) viewer.scene.requestRender();
  if (scrollToPanel && window.innerWidth <= 760) $("detailPanel").scrollIntoView({ block: "center", behavior: reducedMotion ? "instant" : "smooth" });
  if (needsDetail) {
    try {
      const detail = await fetchJSON("/events/" + encodeURIComponent(id));
      if (request !== detailRequest || selectedId !== id) return;
      Object.assign(event, detail);
      const originalSource = safeSource(event.source_url);
      $("detailSource").hidden = !originalSource;
      if (originalSource) $("detailSource").href = originalSource;
      else $("detailSource").removeAttribute("href");
      showEventSummary(event);
      focusEvent();
    } catch (error) {
      if (request !== detailRequest || selectedId !== id) return;
      $("detailSummary").textContent = "Could not load this account. Select the event again to retry.";
      $("detailFatalityNote").textContent = "Count details could not be loaded.";
      $("detailSummaryNote").textContent = error.message;
      $("routeDescription").textContent = "Route information could not be loaded.";
      $("routeNote").textContent = "Select this record again to retry.";
    }
  }
}

function closeDetails() {
  ++detailRequest;
  clearRouteGraphics();
  if (viewer) viewer.camera.cancelFlight();
  const selectedMarker = markersById.get(selectedId);
  if (selectedMarker) {
    selectedMarker.billboard.scale = 1;
    selectedMarker.billboard.alignedAxis = markerAxis(eventsById.get(selectedId));
  }
  selectedId = null;
  $("detailPanel").hidden = true;
  document.querySelectorAll(".event-card").forEach((button) => button.setAttribute("aria-pressed", "false"));
  if (viewer) viewer.scene.requestRender();
}

function homeView(animate = true) {
  if (!viewer) return;
  const destination = Cesium.Cartesian3.fromDegrees(120, -10, 19000000);
  if (animate) viewer.camera.flyTo({ destination, duration: reducedMotion ? 0 : 1.5 });
  else viewer.camera.setView({ destination });
}

async function createGlobe() {
  try {
    if (!window.Cesium) throw new Error("The globe library could not load. Check your internet connection and reload the page.");
   
    Cesium.CreditDisplay.cesiumCredit = new Cesium.Credit("", false);
    
    viewer = new Cesium.Viewer("globe", {
      baseLayer: false, baseLayerPicker: false, geocoder: false,
      animation: false, timeline: false, homeButton: false,
      sceneModePicker: false, navigationHelpButton: false,
      fullscreenButton: false, infoBox: false, selectionIndicator: false,
      requestRenderMode: true, maximumRenderTimeChange: Infinity,
      shouldAnimate: false, scene3DOnly: true,
      contextOptions: { webgl: { powerPreference: "high-performance" } }
    });
    viewer.resolutionScale = Math.min(window.devicePixelRatio || 1, 2);
    viewer.scene.globe.maximumScreenSpaceError = 0.75;
    if (viewer.scene.msaaSupported) viewer.scene.msaaSamples = 4;
    viewer.scene.backgroundColor = Cesium.Color.fromCssColorString("#0b0f16");
    viewer.scene.globe.baseColor = Cesium.Color.fromCssColorString("#173b53");
    viewer.scene.globe.enableLighting = false;
  
    viewer.scene.globe.depthTestAgainstTerrain = false;
    viewer.scene.globe.showGroundAtmosphere = true;
    viewer.scene.skyBox.show = false;
    viewer.scene.skyAtmosphere.show = true;
    viewer.scene.sun.show = false;
    viewer.scene.moon.show = false;
    viewer.scene.screenSpaceCameraController.minimumZoomDistance = 300;
    viewer.scene.screenSpaceCameraController.maximumZoomDistance = 45000000;
    viewer.scene.canvas.setAttribute("aria-label", "Earth globe. Use the event list to select a record.");
   
    markerSource = new SpatialMarkerLayer(viewer.scene);
    markerBackfillSource = new SpatialMarkerLayer(viewer.scene);
    markerBackfillSource.show = false;
    homeView(false);
    $("mapMessage").hidden = true;
    renderMarkers();
    viewer.screenSpaceEventHandler.setInputAction((click) => {
      const picked = viewer.scene.pick(click.position);
      if (Cesium.defined(picked) && typeof picked.id === "string" && eventsById.has(picked.id)) {
        selectEvent(picked.id, false);
      }
    }, Cesium.ScreenSpaceEventType.LEFT_CLICK);
    const provider = await Cesium.ArcGisMapServerImageryProvider.fromUrl(
      "https://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer",
      { enablePickFeatures: false }
    );
    const layer = viewer.imageryLayers.addImageryProvider(provider);
   
    layer.brightness = 1;
    layer.gamma = 1;
    layer.saturation = 1;
    layer.contrast = 1;
    provider.errorEvent.addEventListener(() => {
      $("mapNotice").hidden = false;
      $("mapNotice").textContent = "Some map imagery could not load. Check your internet connection.";
    });
    viewer.scene.requestRender();
  } catch (error) {
    if (viewer) {
      $("mapNotice").hidden = false;
      $("mapNotice").textContent = "Basemap unavailable. Event locations are still shown on the globe.";
    } else {
      $("mapMessage").hidden = false;
      $("mapMessage").textContent = error.message || "The globe could not start. Try a browser with WebGL support.";
    }
    console.error("MAYDAY globe:", error);
  }
}


$("filters").addEventListener("submit", (event) => event.preventDefault());
$("search").addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(applyFilters, 120);
});
$("timeSpan").addEventListener("change", applyFilters);
$("category").addEventListener("change", applyFilters);
$("outcome").addEventListener("change", applyFilters);
$("resetFilters").addEventListener("click", () => {
  $("search").value = ""; $("category").value = "all";
  $("timeSpan").value = "all"; $("outcome").value = "all";
  applyFilters();
});
$("moreResults").addEventListener("click", () => { listLimit += LIST_PAGE_SIZE; renderList(); });
$("refreshData").addEventListener("click", loadEvents);
$("closeDetails").addEventListener("click", closeDetails);
$("fitRoute").addEventListener("click", fitSelectedRoute);
$("routeChoice").addEventListener("change", () => {
  const event = eventsById.get(selectedId);
  if (event) drawRoute(event, event.routes?.[Number($("routeChoice").value)], true);
});
$("homeView").addEventListener("click", () => homeView());
$("zoomIn").addEventListener("click", () => {
  if (viewer) { viewer.camera.zoomIn(viewer.camera.positionCartographic.height * 0.35); viewer.scene.requestRender(); }
});
$("zoomOut").addEventListener("click", () => {
  if (viewer) { viewer.camera.zoomOut(viewer.camera.positionCartographic.height * 0.35); viewer.scene.requestRender(); }
});
$("aboutButton").addEventListener("click", () => $("aboutDialog").showModal());
$("closeAbout").addEventListener("click", () => $("aboutDialog").close());
$("aboutDialog").addEventListener("click", (event) => {
  if (event.target === $("aboutDialog")) {
    const bounds = $("aboutDialog").getBoundingClientRect();
    if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) $("aboutDialog").close();
  }
});
window.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !$("aboutDialog").open) closeDetails();
});


preloadMarkerImage();
loadEvents();
if (window.Cesium) createGlobe();
else {
  $("cesiumScript").addEventListener("load", createGlobe, { once: true });
  $("cesiumScript").addEventListener("error", () => {
    $("mapMessage").hidden = false;
    $("mapMessage").textContent = "The globe library could not load. Check your connection and reload.";
  }, { once: true });
}
