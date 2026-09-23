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

// Treats the timestamp as a plain Central-time string, consistent with
// how the rest of this function already treats ShipStation's date
// fields (see the Central-time note above) — no UTC conversion, just
// reading the date/time components as given. Matches the same
// Mon-Fri 8am-4pm Central production window used elsewhere on the
// Live Queue dashboard for "off-hours order pile-up".
function isWithinWorkingHours(timestampStr) {
  if (!timestampStr) return false;
  const [datePart, timePart] = String(timestampStr).split(/[T ]/);
  if (!datePart || !timePart) return false;
  const d = new Date(datePart + "T00:00:00Z");
  const dayOfWeek = d.getUTCDay(); // 0=Sun, 6=Sat
  if (dayOfWeek === 0 || dayOfWeek === 6) return false;
  const hour = parseInt(timePart.slice(0, 2), 10);
  return hour >= 8 && hour < 16;
}

exports.handler = async (event) => {
  const headers = {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*",
  };

  try {
    const { startDate, endDate, workingHoursOnly: workingHoursOnlyRaw } = event.queryStringParameters || {};
    if (!startDate || !endDate) {
      return { statusCode: 400, headers, body: JSON.stringify({ error: "startDate and endDate are required, as YYYY-MM-DD (Central time calendar dates)." }) };
    }
    const workingHoursOnly = workingHoursOnlyRaw === "true";

    // Central-time calendar day boundaries. ShipStation's date filters
    // are documented as plain, un-suffixed strings interpreted in the
    // account's own configured timezone — confirmed directly earlier in
    // this project that this account's timezone is Central.
    const dayStart = `${startDate} 00:00:00`;
    const dayEnd = `${endDate} 23:59:59`;

    // These three ShipStation calls don't depend on each other's results
    // (only the filtering below needs all three) — running them
    // concurrently instead of one after another cuts this invocation's
    // total latency roughly to whichever single call is slowest, instead
    // of the sum of all three. That matters directly for staying under
    // Netlify's function execution time limit.
    const modifyDateStart = `${shiftDate(startDate, -3)} 00:00:00`;
    const modifyDateEnd = `${shiftDate(endDate, 3)} 23:59:59`;
    const [{ warehouseId, freightId }, placedOrders, recentlyModifiedShipped] = await Promise.all([
      getWarehouseIds(),
      listAllOrders({ orderDateStart: dayStart, orderDateEnd: dayEnd }),
      listAllOrders({ orderStatus: "shipped", modifyDateStart, modifyDateEnd }),
    ]);
    const validWarehouseIds = new Set([warehouseId, freightId]);

    // Orders In: orders PLACED in the window.
    const ordersIn = placedOrders.filter(o => {
      const whId = (o.advancedOptions || {}).warehouseId;
      if (!validWarehouseIds.has(whId) || o.orderStatus === "cancelled") return false;
      if (workingHoursOnly && !isWithinWorkingHours(o.orderDate)) return false;
      return true;
    });

    // Orders Out: orders whose own shipDate falls in the window,
    // regardless of when they were placed or how they were fulfilled.
    // /orders has no shipDate filter, so this fetches shipped orders
    // via a wide modifyDate net (a day of buffer on each side, since an
    // order's last modification is essentially always the moment it
    // ships) and then filters precisely by shipDate client-side.
    const ordersOut = recentlyModifiedShipped.filter(o => {
      const whId = (o.advancedOptions || {}).warehouseId;
      if (!validWarehouseIds.has(whId)) return false;
      const shipDate = (o.shipDate || "").slice(0, 10); // "YYYY-MM-DD"
      if (shipDate < startDate || shipDate > endDate) return false;
      // shipDate is a date-only field per ShipStation's own docs ("regarded
      // strictly as a date") — it has no reliable time-of-day component, so
      // an hour-of-day check against it always fails. modifyDate is what
      // actually carries a real timestamp, and per the note above, an
      // order's last modification is essentially always the moment it ships.
      if (workingHoursOnly && !isWithinWorkingHours(o.modifyDate)) return false;
      return true;
    });

    return {
      statusCode: 200,
      headers,
      body: JSON.stringify({
        startDate,
        endDate,
        workingHoursOnly,
        ordersIn: ordersIn.length,
        ordersOut: ordersOut.length,
        // Diagnostic only, included whenever ?debug=true is passed: a few raw
        // timestamps straight from ShipStation's response, before any of this
        // function's own timezone assumptions are applied, plus what the
        // in/out counts would be with the working-hours filter turned off for
        // comparison. This exists specifically to verify (rather than assume)
        // what timezone ShipStation's orderDate/shipDate fields are actually
        // in, since that assumption is unconfirmed and this function's
        // working-hours filtering depends entirely on it being correct.
        ...(event.queryStringParameters && event.queryStringParameters.debug === 'true' ? {
          debug: {
            sampleOrderDates: placedOrders.slice(0, 5).map(o => o.orderDate),
            sampleShipDates: recentlyModifiedShipped.slice(0, 5).map(o => o.shipDate),
            sampleModifyDates: recentlyModifiedShipped.slice(0, 5).map(o => o.modifyDate),
            ordersInWithoutHourFilter: placedOrders.filter(o => {
              const whId = (o.advancedOptions || {}).warehouseId;
              return validWarehouseIds.has(whId) && o.orderStatus !== "cancelled";
            }).length,
            ordersOutWithoutHourFilter: recentlyModifiedShipped.filter(o => {
              const whId = (o.advancedOptions || {}).warehouseId;
              if (!validWarehouseIds.has(whId)) return false;
              const shipDate = (o.shipDate || "").slice(0, 10);
              return shipDate >= startDate && shipDate <= endDate;
            }).length,
          }
        } : {}),
      }),
    };
  } catch (err) {
    return { statusCode: 500, headers, body: JSON.stringify({ error: err.message }) };
  }
};
