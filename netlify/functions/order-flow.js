// netlify/functions/order-flow.js
//
// On-demand "Orders In / Orders Out" for a given Central-time date
// range — a direct measure of demand vs. performance, week over week.
// Calls ShipStation directly (never touches Supabase) — this is the
// ONE place in the whole project that talks to ShipStation from
// outside the GitHub Actions pipeline, which is why it needs its own
// credentials set directly in Netlify (Project configuration ->
// Environment variables), separate from the GitHub Actions secrets
// used everywhere else.
//
// Scoping, confirmed directly with Casey:
//   - Orders In:  orders whose orderDate falls in the range, Warehouse/
//     Freight Ship-From-Location only (same classification used
//     everywhere else on the dashboard), EXCLUDING orders whose current
//     status is "cancelled".
//   - Orders Out: orders whose own shipDate falls in the range, same
//     Warehouse/Freight scoping — regardless of when the order was
//     originally placed, and regardless of fulfillment method.
//
// CONFIRMED BUG, FIXED (2026-09-22): the first version of this function
// counted Orders Out via the /shipments endpoint, which only covers
// orders shipped through ShipStation's own label system. Checked
// directly against a real 2-week window: 644 of 1415 "shipped" orders
// (46%) had NO matching /shipments record at all — every single one
// checked was externallyFulfilled: true, with its OWN shipDate field
// set directly on the order, never touching /shipments. This wasn't a
// timing artifact (a wider date range never closed the gap) — it was a
// structural blind spot. Fixed by reading shipDate directly off each
// order instead. Since ShipStation's /orders endpoint has no shipDate
// filter, this fetches shipped orders by a wide modifyDate window (an
// order's last modification is essentially always the moment it ships)
// and then filters precisely by each order's own shipDate client-side.
//
// IMPORTANT: this file has been logically verified against real
// ShipStation data pulled via a local diagnostic script, but the
// function ITSELF has not yet been re-tested end-to-end after this
// rewrite — please treat the next real run as a genuine test.

const SHIPSTATION_BASE = "https://ssapi.shipstation.com";
const WAREHOUSE_LOCATION_NAME = "Dripping Springs Warehouse";
const FREIGHT_LOCATION_NAME = "Dripping Springs Freight";

function authHeader() {
  const key = process.env.SHIPSTATION_API_KEY;
  const secret = process.env.SHIPSTATION_API_SECRET;
  if (!key || !secret) {
    throw new Error("SHIPSTATION_API_KEY / SHIPSTATION_API_SECRET are not set in this Netlify site's environment variables.");
  }
  return "Basic " + Buffer.from(`${key}:${secret}`).toString("base64");
}

async function ssGet(path, params, retriesLeft = 3) {
  const url = new URL(SHIPSTATION_BASE + path);
  for (const [k, v] of Object.entries(params || {})) {
    if (v !== undefined && v !== null) url.searchParams.set(k, v);
  }
  const res = await fetch(url.toString(), { headers: { Authorization: authHeader() } });

  if (res.status === 429 && retriesLeft > 0) {
    const retryAfter = parseInt(res.headers.get("Retry-After") || "5", 10);
    await new Promise(r => setTimeout(r, retryAfter * 1000));
    return ssGet(path, params, retriesLeft - 1);
  }
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`ShipStation ${path} failed: ${res.status} ${text}`);
  }
  return res.json();
}

async function getWarehouseIds() {
  const data = await ssGet("/warehouses", {});
  const byName = {};
  for (const w of data) byName[w.warehouseName] = w.warehouseId;

  const warehouseId = byName[WAREHOUSE_LOCATION_NAME];
  const freightId = byName[FREIGHT_LOCATION_NAME];
  if (!warehouseId || !freightId) {
    throw new Error(`Could not find warehouse IDs for "${WAREHOUSE_LOCATION_NAME}" / "${FREIGHT_LOCATION_NAME}" — check these names still match ShipStation's Ship From Location setup.`);
  }
  return { warehouseId, freightId };
}

async function listAllOrders(params) {
  let page = 1;
  const all = [];
  while (true) {
    const data = await ssGet("/orders", { ...params, page, pageSize: 500 });
    all.push(...(data.orders || []));
    if (page >= (data.pages || 1)) break;
    page++;
  }
  return all;
}

function shiftDate(dateStr, days) {
  const d = new Date(dateStr + "T00:00:00Z");
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
}

function partsForInstant(dateMs, timeZone) {
  const fmt = new Intl.DateTimeFormat("en-US", {
    timeZone,
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit",
    hour12: false,
  });
  const parts = {};
  for (const p of fmt.formatToParts(new Date(dateMs))) parts[p.type] = p.value;
  return parts;
}

function parseShipStationNaive(naiveStr) {
  // Parses a raw timestamp string exactly as ShipStation's API returns
  // it — these are ALWAYS Pacific time, regardless of account settings.
  // CONFIRMED BUG, FIXED (2026-09-23): verified directly against a real
  // order (raw createDate 15:12:58 matched ShipStation's own UI showing
  // 17:12 Central, an exact 2-hour offset, confirmed to the second).
  // This function was originally missing entirely here — hourOf() used
  // to read the raw hour digits directly, assuming (wrongly) that the
  // string was already Central time. Mirrors parseShipStationNaive in
  // site/index.html and parse_shipstation_naive in ops_common.py.
  const m = (naiveStr || "").match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})/);
  if (!m) return null;
  const wantY = +m[1], wantMo = +m[2], wantD = +m[3], wantH = +m[4], wantMi = +m[5], wantS = +m[6];
  const wantMs = Date.UTC(wantY, wantMo - 1, wantD, wantH, wantMi, wantS);

  let guessMs = wantMs;
  for (let i = 0; i < 3; i++) {
    const got = partsForInstant(guessMs, "America/Los_Angeles");
    const gotMs = Date.UTC(+got.year, +got.month - 1, +got.day, +got.hour, +got.minute, +got.second);
    const errorMs = wantMs - gotMs;
    if (errorMs === 0) break;
    guessMs += errorMs;
  }
  return guessMs;
}

function hourOf(dateTimeStr) {
  // The hour to bucket by is the real CENTRAL hour (where the warehouse
  // actually is, same as everywhere else on the dashboard) — parse the
  // raw Pacific string to the correct instant, then read its Central
  // hour, rather than reading the raw (Pacific) hour digits directly.
  const instantMs = parseShipStationNaive(dateTimeStr);
  if (instantMs === null) return null;
  const centralParts = partsForInstant(instantMs, "America/Chicago");
  const hour = parseInt(centralParts.hour, 10);
  return Number.isNaN(hour) ? null : hour;
}

function countDaysInRange(startDate, endDate) {
  const start = new Date(startDate + "T00:00:00Z");
  const end = new Date(endDate + "T00:00:00Z");
  return Math.round((end - start) / (24 * 3600 * 1000)) + 1;
}

exports.handler = async (event) => {
  const headers = {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*",
  };

  try {
    const { startDate, endDate } = event.queryStringParameters || {};
    if (!startDate || !endDate) {
      return { statusCode: 400, headers, body: JSON.stringify({ error: "startDate and endDate are required, as YYYY-MM-DD (Central time calendar dates)." }) };
    }

    // Central-time calendar day boundaries. ShipStation's date filters
    // are documented as plain, un-suffixed strings interpreted in the
    // account's own configured timezone — confirmed directly earlier in
    // this project that this account's timezone is Central.
    const dayStart = `${startDate} 00:00:00`;
    const dayEnd = `${endDate} 23:59:59`;

    const { warehouseId, freightId } = await getWarehouseIds();
    const validWarehouseIds = new Set([warehouseId, freightId]);

    // Orders In: orders PLACED in the window.
    const placedOrders = await listAllOrders({ orderDateStart: dayStart, orderDateEnd: dayEnd });
    const ordersIn = placedOrders.filter(o => {
      const whId = (o.advancedOptions || {}).warehouseId;
      return validWarehouseIds.has(whId) && o.orderStatus !== "cancelled";
    });

    // Orders Out: orders whose own shipDate falls in the window,
    // regardless of when they were placed or how they were fulfilled.
    // /orders has no shipDate filter, so this fetches shipped orders
    // via a wide modifyDate net (3 days of buffer on each side, since
    // an order's last modification is USUALLY close to when it ships —
    // see the hourly-bucketing note below for the real exception to
    // that) and then filters precisely by shipDate client-side.
    const modifyDateStart = `${shiftDate(startDate, -3)} 00:00:00`;
    const modifyDateEnd = `${shiftDate(endDate, 3)} 23:59:59`;
    const recentlyModifiedShipped = await listAllOrders({
      orderStatus: "shipped",
      modifyDateStart,
      modifyDateEnd,
    });
    const ordersOut = recentlyModifiedShipped.filter(o => {
      const whId = (o.advancedOptions || {}).warehouseId;
      if (!validWarehouseIds.has(whId)) return false;
      const shipDate = (o.shipDate || "").slice(0, 10); // "YYYY-MM-DD"
      return shipDate >= startDate && shipDate <= endDate;
    });

    // Hourly breakdown (Orders In only — see below for why Out is
    // excluded). Orders In uses orderDate directly, a real timestamp,
    // reliable at any granularity.
    const hourlyIn = new Array(24).fill(0);
    for (const o of ordersIn) {
      const h = hourOf(o.orderDate);
      if (h !== null) hourlyIn[h]++;
    }

    // CONFIRMED, FIXED, THEN CONFIRMED UNFIXABLE (2026-09-24): Orders
    // Out has no real ship TIME anywhere in ShipStation's data model —
    // shipDate is date-only, confirmed against every real example
    // pulled this project. modifyDate looked like the closest
    // available proxy, and an initial fix filtered out modifyDate
    // values far from shipDate (a real batch-job artifact: 5 real
    // orders shared the exact same modifyDate despite shipDates
    // spread across 3+ months). That fix was insufficient — a second,
    // full-day measurement showed 71.3% of ALL orders on a real day
    // share a near-identical modifyDate with at least other order,
    // CONSISTENTLY throughout the 8am-4pm shift itself (55-79% every
    // hour), not just at the edges. This means some automated process
    // (a sync, tracking poller, or similar) touches modifyDate on a
    // continuous ~5-minute cadence all day, indistinguishable from real
    // shipping activity — there is no reliable way to separate the two
    // for any individual order. Hourly Orders Out is therefore not
    // provided at all; the frontend shows an honest explanation instead
    // of a misleading chart.
    //
    // Daily granularity is a completely different story: shipDate alone
    // (already date-only, already reliable) is exactly the precision
    // needed for a day-by-day breakdown, no modifyDate involved at all.
    const dailyIn = {};
    for (const o of ordersIn) {
      const d = (o.orderDate || "").slice(0, 10);
      if (d) dailyIn[d] = (dailyIn[d] || 0) + 1;
    }
    const dailyOut = {};
    for (const o of ordersOut) {
      const d = (o.shipDate || "").slice(0, 10);
      if (d) dailyOut[d] = (dailyOut[d] || 0) + 1;
    }
    const dayCount = countDaysInRange(startDate, endDate);
    const dailyLabels = [];
    for (let i = 0; i < dayCount; i++) dailyLabels.push(shiftDate(startDate, i));

    return {
      statusCode: 200,
      headers,
      body: JSON.stringify({
        startDate,
        endDate,
        dayCount,
        ordersIn: ordersIn.length,
        ordersOut: ordersOut.length,
        hourlyIn,
        dailyLabels,
        dailyIn: dailyLabels.map(d => dailyIn[d] || 0),
        dailyOut: dailyLabels.map(d => dailyOut[d] || 0),
      }),
    };
  } catch (err) {
    return { statusCode: 500, headers, body: JSON.stringify({ error: err.message }) };
  }
};
