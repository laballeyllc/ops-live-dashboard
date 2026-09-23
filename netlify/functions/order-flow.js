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
    // via a wide modifyDate net (a day of buffer on each side, since an
    // order's last modification is essentially always the moment it
    // ships) and then filters precisely by shipDate client-side.
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

    // Hourly breakdown, for the dashboard's hourly chart (single-day
    // mode shows these totals as-is; range mode divides by the number
    // of days to get an average — the frontend's job, not this
    // function's, so this always returns the same shape either way).
    // Orders In uses orderDate directly (a real timestamp). Orders Out
    // has no real ship TIME anywhere in ShipStation's data model
    // (shipDate is date-only, confirmed directly against every real
    // example pulled this project, including externally-fulfilled
    // orders) — modifyDate is the closest available proxy, per Casey's
    // own understanding that an order's last modification is
    // essentially the moment it ships. This is an approximation, and
    // the frontend must label it as such.
    const hourlyIn = new Array(24).fill(0);
    for (const o of ordersIn) {
      const h = hourOf(o.orderDate);
      if (h !== null) hourlyIn[h]++;
    }
    const hourlyOut = new Array(24).fill(0);
    for (const o of ordersOut) {
      const h = hourOf(o.modifyDate);
      if (h !== null) hourlyOut[h]++;
    }
    const dayCount = countDaysInRange(startDate, endDate);

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
        hourlyOut,
      }),
    };
  } catch (err) {
    return { statusCode: 500, headers, body: JSON.stringify({ error: err.message }) };
  }
};
