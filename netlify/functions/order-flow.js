// netlify/functions/order-flow.js
//
// On-demand "Orders In / Orders Out" for a given Central-time date
// range. Calls ShipStation directly (never touches Supabase) — this is
// the ONE place in the whole project that talks to ShipStation from
// outside the GitHub Actions pipeline, which is why it needs its own
// credentials set directly in Netlify (Site configuration -> Environment
// variables), separate from the GitHub Actions secrets used everywhere
// else.
//
// Scoping, confirmed directly with Casey:
//   - Orders In:  orders whose orderDate falls in the range, Warehouse/
//     Freight Ship-From-Location only (same classification used
//     everywhere else on the dashboard), EXCLUDING orders whose current
//     status is "cancelled".
//   - Orders Out: shipments whose shipDate falls in the range, same
//     Warehouse/Freight scoping, EXCLUDING voided shipments. De-duped
//     by order number, since a split order can generate more than one
//     shipment record.
//
// IMPORTANT: this file has NOT been tested against a real ShipStation
// account or a real Netlify deployment — I have no live credentials or
// deployment access to verify it myself. Please treat the first real
// run as a genuine test, the same way we've tested everything else in
// this project against real data before trusting it.

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

async function listAllOrders(orderDateStart, orderDateEnd) {
  let page = 1;
  const all = [];
  while (true) {
    const data = await ssGet("/orders", { orderDateStart, orderDateEnd, page, pageSize: 500 });
    all.push(...(data.orders || []));
    if (page >= (data.pages || 1)) break;
    page++;
  }
  return all;
}

async function listAllShipments(shipDateStart, shipDateEnd) {
  let page = 1;
  const all = [];
  while (true) {
    const data = await ssGet("/shipments", {
      shipDateStart, shipDateEnd, page, pageSize: 500, includeShipmentItems: "false",
    });
    all.push(...(data.shipments || []));
    if (page >= (data.pages || 1)) break;
    page++;
  }
  return all;
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
    const orderDateStart = `${startDate} 00:00:00`;
    const orderDateEnd = `${endDate} 23:59:59`;

    const { warehouseId, freightId } = await getWarehouseIds();
    const validWarehouseIds = new Set([warehouseId, freightId]);

    const orders = await listAllOrders(orderDateStart, orderDateEnd);
    const ordersIn = orders.filter(o => {
      const whId = (o.advancedOptions || {}).warehouseId;
      return validWarehouseIds.has(whId) && o.orderStatus !== "cancelled";
    });

    const shipments = await listAllShipments(orderDateStart, orderDateEnd);
    const scopedShipments = shipments.filter(s => validWarehouseIds.has(s.warehouseId) && !s.voided);
    const distinctOrdersOut = new Set(scopedShipments.map(s => s.orderNumber));

    return {
      statusCode: 200,
      headers,
      body: JSON.stringify({
        startDate,
        endDate,
        ordersIn: ordersIn.length,
        ordersOut: distinctOrdersOut.size,
      }),
    };
  } catch (err) {
    return { statusCode: 500, headers, body: JSON.stringify({ error: err.message }) };
  }
};
