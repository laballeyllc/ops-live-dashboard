// =====================================================================
// ops-logic.js: shared Lab Alley queue logic
// Loaded by Ops Live (index.html) and the wall displays (/tv/*.html).
// Change queue, shipping-list, production-list or order-age rules
// HERE ONLY, and every screen picks it up.
//
// Expects the page to declare:  let currentStockLevels = [];
// (Ops Live and the TV runtime both do.)
// =====================================================================

function orderAgeDays(orderDateStr) {
  if (!orderDateStr) return null;
  const orderDate = new Date(orderDateStr.slice(0, 10) + "T00:00:00Z");
  if (isNaN(orderDate)) return null;
  const today = new Date();
  const todayUTC = new Date(Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), today.getUTCDate()));
  return Math.floor((todayUTC - orderDate) / 86400000);
}

function buildSummary(rows) {
  // Scope EVERYTHING on this page to Core Queue = Warehouse/Freight/Both,
  // consistently. An order can land in live_queue (real Finale product
  // data exists) while carrying some other tag entirely — a hold like
  // "Pending Approval," or nothing at all — meaning core_queue comes out
  // blank ("Other"). That order was never really "in the Warehouse/
  // Freight queue" the way ShipStation's own tabs define it, so it must
  // not count toward "in queue," category/chem percentages, or —
  // critically — the oldest/average age figures. This is exactly what
  // caused the dashboard to report a 483-day-old "oldest" order when the
  // real oldest order actually sitting in Warehouse or Freight was only
  // 48 days old: the old code was including an "Other"-tagged order in
  // that calculation.
  //
  // ALSO explicitly excludes shipment_status === "on_hold", regardless
  // of core_queue. This is a second, separate real bug that already
  // happened once: order 220199 was on_hold, tagged "Austin Warehouse"
  // (a real Warehouse core_queue value), and its 225-day age leaked into
  // "oldest in queue" before this exclusion existed. on_hold orders are
  // now pulled again (the new Held/Compliance panel needs them), which
  // means this exclusion is the ONLY thing standing between that bug
  // recurring — do not remove it without re-testing against a mock
  // on_hold + Warehouse-tagged order the way we did when this was fixed.
  const scopedRows = rows.filter(r =>
    ["Warehouse", "Freight", "Both"].includes(r.core_queue) && r.shipment_status !== "on_hold"
  );

  // Group product lines by order — core_queue/order_date/tags are
  // order-level (same across every line of a given order); category/chem
  // are line-level and a single order can span more than one value.
  const linesByOrder = new Map();
  for (const row of scopedRows) {
    if (!linesByOrder.has(row.order_id)) linesByOrder.set(row.order_id, []);
    linesByOrder.get(row.order_id).push(row);
  }
  const totalOrders = linesByOrder.size;

  const coreQueueCounts = {};
  let oldestAge = 0;
  let oldestOrderId = null;
  let labRoomOrders = 0;
  const agesByQueue = { Warehouse: [], Freight: [], Both: [] };

  for (const [oid, lines] of linesByOrder) {
    const first = lines[0];
    const cq = first.core_queue || "Other";
    coreQueueCounts[cq] = (coreQueueCounts[cq] || 0) + 1;
    const age = orderAgeDays(first.order_date);
    if (age !== null) {
      if (age > oldestAge) {
        oldestAge = age;
        oldestOrderId = oid;
      }
      if (agesByQueue[cq]) agesByQueue[cq].push(age);
    }
    // An order "requires a lab room" if ANY of its lines do — picking
    // the whole order still means handling that line's requirement.
    if (lines.some(l => (l.lab_room || "").trim().toLowerCase() === "yes")) {
      labRoomOrders++;
    }
  }

  function average(arr) {
    return arr.length ? arr.reduce((s, n) => s + n, 0) / arr.length : null;
  }

  // "% of orders containing this value" (confirmed as the wanted
  // definition): an order counts toward EVERY distinct value it has for
  // this field across its lines, not just one — so percentages across
  // values can sum past 100%. That's deliberate, not a bug — see the
  // note rendered next to each breakdown's heading. The same raw counts
  // are reused for the pie charts above, just normalized differently
  // there (by total touches, so the pie correctly sums to 100%) — see
  // renderPieChart().
  function orderPercentagesFor(field) {
    const valueOrderCounts = {};
    for (const [oid, lines] of linesByOrder) {
      const distinctValues = new Set(
        lines.map(l => (l[field] || "").trim()).filter(v => v)
      );
      if (distinctValues.size === 0) {
        // Every line on this order left this field blank in Finale —
        // shown explicitly rather than silently dropped, so percentages
        // for a normally single-valued field (like Downpack Product)
        // honestly account for every order instead of quietly not
        // summing to 100%.
        distinctValues.add("(not set)");
      }
      for (const value of distinctValues) {
        valueOrderCounts[value] = (valueOrderCounts[value] || 0) + 1;
      }
    }
    return Object.entries(valueOrderCounts)
      .map(([label, count]) => [label, count, totalOrders ? (count / totalOrders * 100) : 0])
      .sort((a, b) => b[2] - a[2]);
  }

  return {
    total_orders: totalOrders,
    core_queue_counts: coreQueueCounts,
    oldest_age_days: oldestAge,
    oldest_order_id: oldestOrderId,
    avg_age_warehouse: average(agesByQueue.Warehouse),
    avg_age_freight: average(agesByQueue.Freight),
    category_pcts: orderPercentagesFor("category"),
    chem_pcts: orderPercentagesFor("chem"),  // Chem Tech level lives in the `chem` field, not `chem_tech`
    downpack_pcts: orderPercentagesFor("downpack"),
    hazmat_pcts: orderPercentagesFor("hazmat"),
    lab_room_pct: totalOrders ? (labRoomOrders / totalOrders * 100) : 0,
    lab_room_orders: labRoomOrders,
    unique_skus: new Set(scopedRows.map(r => (r.product_id || "").trim()).filter(v => v)).size,
  };
}

function buildHeldSummary(rows) {
  // Every on_hold order, regardless of core_queue — deliberately NOT
  // scoped to Warehouse/Freight/Both like buildSummary() is, since a
  // held order needs visibility no matter which team it would otherwise
  // belong to. This is a genuinely separate question from "how's the
  // Warehouse/Freight queue doing" — these orders can't be worked at all
  // right now, by anyone, until whatever's holding them gets resolved.
  const heldRows = rows.filter(r => r.shipment_status === "on_hold");
  const linesByOrder = new Map();
  for (const row of heldRows) {
    if (!linesByOrder.has(row.order_id)) linesByOrder.set(row.order_id, []);
    linesByOrder.get(row.order_id).push(row);
  }

  const totalHeld = linesByOrder.size;
  let oldestHeldAge = 0;
  let oldestHeldOrderId = null;
  const reasonCounts = {};

  for (const [oid, lines] of linesByOrder) {
    const first = lines[0];
    const age = orderAgeDays(first.order_date);
    if (age !== null && age > oldestHeldAge) {
      oldestHeldAge = age;
      oldestHeldOrderId = oid;
    }
    // An order's Tags field can list more than one reason (e.g. the real
    // 220199 example: "Austin Warehouse, Roshan") — count it under every
    // distinct tag it carries, same "% of orders touching X" logic used
    // elsewhere on this page, rather than picking just one.
    const tags = (first.tags || "").split(",").map(t => t.trim()).filter(t => t);
    const reasons = tags.length ? tags : ["(untagged)"];
    for (const reason of new Set(reasons)) {
      reasonCounts[reason] = (reasonCounts[reason] || 0) + 1;
    }
  }

  const reasonBreakdown = Object.entries(reasonCounts)
    .map(([label, count]) => [label, count, totalHeld ? (count / totalHeld * 100) : 0])
    .sort((a, b) => b[1] - a[1]);

  return {
    total_held: totalHeld,
    oldest_held_age_days: oldestHeldAge,
    oldest_held_order_id: oldestHeldOrderId,
    reason_breakdown: reasonBreakdown,
  };
}

// ---------------------------------------------------------------------
// Order timestamp math: converts ShipStation's naive order timestamp
// (wall-clock time in US Pacific -- confirmed 2026-09-23 against a real
// order: the v1 API ignores the account's configured timezone) into a
// true UTC instant, correctly handling PST (-8) vs PDT (-7) depending on
// the date. Order AGE is then plain elapsed time from that instant (see
// orderAgeHours below) -- weekends count, matching ShipStation's Age.
//
// Both functions are unit-tested in isolation (DST transition edges,
// weekend-exclusion boundaries) before being wired into anything below
// -- see the project notes for the test cases if this ever needs
// revisiting.
function chicagoPartsForInstant(dateMs) {
  const fmt = new Intl.DateTimeFormat('en-US', {
    timeZone: 'America/Chicago',
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
    hour12: false, weekday: 'short'
  });
  const parts = {};
  for (const p of fmt.formatToParts(new Date(dateMs))) parts[p.type] = p.value;
  return parts;
}

function pacificPartsForInstant(dateMs) {
  // Used ONLY for parsing ShipStation's own raw timestamp strings (see
  // parseShipStationNaive below) — NOT for any display or business-hours
  // logic, which correctly stays Central (the warehouse's real
  // location). CONFIRMED BUG, FIXED (2026-09-23): ShipStation's v1 API
  // returns every timestamp in fixed US Pacific time regardless of the
  // account's configured timezone — verified directly against a real
  // order (raw createDate 15:12:58 matched ShipStation's own UI showing
  // 17:12 Central, an exact 2-hour offset, confirmed to the second).
  // Before this fix, every order's raw timestamp was incorrectly parsed
  // AS IF it were already Central time, which silently overstated every
  // order's age by up to 2 hours — pushing orders past the 24h/48h SLA
  // thresholds earlier than they truly should have, project-wide.
  const fmt = new Intl.DateTimeFormat('en-US', {
    timeZone: 'America/Los_Angeles',
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
    hour12: false, weekday: 'short'
  });
  const parts = {};
  for (const p of fmt.formatToParts(new Date(dateMs))) parts[p.type] = p.value;
  return parts;
}

function parseShipStationNaive(naiveStr) {
  // Parses a raw timestamp string exactly as ShipStation's API returns
  // it (e.g. order_datetime) — these are ALWAYS Pacific time, regardless
  // of account settings (see pacificPartsForInstant for how this was
  // confirmed). Returns the correct real-world instant; everything
  // downstream (business-hours math, the production shift window, etc.)
  // correctly converts that instant to Central time from there, since
  // that's where the warehouse actually is.
  const m = (naiveStr || "").match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})/);
  if (!m) return null;
  const wantY = +m[1], wantMo = +m[2], wantD = +m[3], wantH = +m[4], wantMi = +m[5], wantS = +m[6];
  const wantMs = Date.UTC(wantY, wantMo - 1, wantD, wantH, wantMi, wantS);

  let guessMs = wantMs;
  // 3 iterations always converges, even right at a DST transition edge
  // (each iteration's error is at most one offset-change, i.e. 1 hour).
  for (let i = 0; i < 3; i++) {
    const got = pacificPartsForInstant(guessMs);
    const gotMs = Date.UTC(+got.year, +got.month - 1, +got.day, +got.hour, +got.minute, +got.second);
    const errorMs = wantMs - gotMs;
    if (errorMs === 0) break;
    guessMs += errorMs;
  }
  return new Date(guessMs);
}

function chicagoMidnightMs(dateMs, dayOffset) {
  const parts = chicagoPartsForInstant(dateMs);
  const guessMs = Date.UTC(+parts.year, +parts.month - 1, +parts.day + dayOffset, 0, 0, 0);
  let g = guessMs;
  for (let i = 0; i < 3; i++) {
    const got = chicagoPartsForInstant(g);
    const gotMs = Date.UTC(+got.year, +got.month - 1, +got.day, +got.hour, +got.minute, +got.second);
    const err = guessMs - gotMs;
    if (err === 0) break;
    g += err;
  }
  return g;
}

// --- Production shift window: Mon-Fri 8am-4pm Central, confirmed
// directly. Used both for the Build Health "production hours" average
// (site tab: Queue Breakdown) and the off-hours order pile-up tracking
// below (site tab: Historical Analysis).
const SHIFT_START_HOUR = 8;

const SHIFT_END_HOUR = 16;

function chicagoTimeOnDate(dayMs, hour) {
  // Same iterative-correction technique as chicagoMidnightMs, just for
  // an arbitrary hour on the same Chicago calendar day as dayMs rather
  // than always midnight.
  const parts = chicagoPartsForInstant(dayMs);
  const guessMs = Date.UTC(+parts.year, +parts.month - 1, +parts.day, hour, 0, 0);
  let g = guessMs;
  for (let i = 0; i < 3; i++) {
    const got = chicagoPartsForInstant(g);
    const gotMs = Date.UTC(+got.year, +got.month - 1, +got.day, +got.hour, +got.minute, +got.second);
    const err = guessMs - gotMs;
    if (err === 0) break;
    g += err;
  }
  return g;
}

function isProductionWeekday(weekday) {
  return weekday !== "Sat" && weekday !== "Sun";
}

function isProductionHours(ms) {
  const parts = chicagoPartsForInstant(ms);
  if (!isProductionWeekday(parts.weekday)) return false;
  const hour = +parts.hour;
  return hour >= SHIFT_START_HOUR && hour < SHIFT_END_HOUR;
}

function mostRecentShiftEnd(nowMs) {
  // Walks backward day by day (Chicago calendar) to find the most
  // recent Mon-Fri 4pm at or before now.
  let cursor = chicagoMidnightMs(nowMs, 0);
  for (let i = 0; i < 14; i++) {
    const parts = chicagoPartsForInstant(cursor);
    if (isProductionWeekday(parts.weekday)) {
      const shiftEnd = chicagoTimeOnDate(cursor, SHIFT_END_HOUR);
      if (shiftEnd <= nowMs) return shiftEnd;
    }
    cursor = chicagoMidnightMs(cursor, -1);
  }
  return null;
}

function nextShiftStartAfter(afterMs) {
  // Walks forward day by day to find the next Mon-Fri 8am strictly
  // after afterMs.
  let cursor = chicagoMidnightMs(afterMs, 0);
  for (let i = 0; i < 14; i++) {
    const parts = chicagoPartsForInstant(cursor);
    if (isProductionWeekday(parts.weekday)) {
      const shiftStart = chicagoTimeOnDate(cursor, SHIFT_START_HOUR);
      if (shiftStart > afterMs) return shiftStart;
    }
    cursor = chicagoMidnightMs(cursor, 1);
  }
  return null;
}

function currentOffHoursWindow() {
  // The off-hours order pile-up window: starts growing the moment a
  // shift ends, freezes the instant the next shift starts (so it reads
  // as "this many piled up while we were out" for the whole following
  // shift), then resets to zero the next time a shift ends — confirmed
  // directly as the intended "reset each shift" behavior.
  const nowMs = Date.now();
  const start = mostRecentShiftEnd(nowMs);
  const nextStart = nextShiftStartAfter(start);
  const end = Math.min(nowMs, nextStart);
  return { start, end, isFrozen: nowMs >= nextStart, gapType: offHoursGapType(start, nextStart) };
}

function offHoursGapType(start, nextStart) {
  // Tags a gap as "weekend" if it spans more than ~24h (i.e. crossed a
  // weekend), otherwise "overnight" — used by the Nights/Weekends/
  // Combined historical filter.
  const hours = (nextStart - start) / 3600000;
  return hours > 24 ? "weekend" : "overnight";
}

// Order age = real elapsed time since the order's ShipStation Order Date,
// weekends and nights INCLUDED -- exactly what ShipStation's own "Age"
// column shows, because that is the number the rest of the company sees
// and uses. CHANGED 2026-10-06: this used to be "business hours" with
// weekends excluded (the 48h clock paused Sat/Sun), which made every
// threshold here (24h at-risk, 48h overdue, Build %) disagree with what
// everyone else was reading in ShipStation.
function orderAgeHours(orderDatetimeStr) {
  const orderMs = parseShipStationNaive(orderDatetimeStr);
  if (!orderMs) return 0;
  const ms = Date.now() - orderMs.getTime();
  return ms > 0 ? ms / 3600000 : 0;
}

function formatAge(hours) {
  // "2d 18h" once past a full day, otherwise just "Xh" — reads faster
  // than a raw hour count once orders start aging past 24h. Rounds the
  // TOTAL first, then decomposes — avoids an edge case where rounding
  // the remainder alone could produce something like "1d 24h" instead
  // of the correct "2d 0h".
  const rounded = Math.round(hours);
  if (rounded < 24) return `${rounded}h`;
  const days = Math.floor(rounded / 24);
  const remHours = rounded % 24;
  return `${days}d ${remHours}h`;
}

const AT_RISK_HOURS = 24;

const OVERDUE_HOURS = 48;

// ---------------------------------------------------------------------
// "Attack the Queue": two chemical-centric lists (not order-centric),
// per the actual real prioritization logic in Lab Alley's own build
// scheduler (Sandbox_Build_Script.xlsx) -- channel committment (Amazon/
// Walmart) takes priority over raw volume, and "needs Downpack" is a
// distinct, separate concern from "how much of this do we need."

function skuFamily(productId) {
  // Heuristic: group by everything before the LAST hyphen, so e.g.
  // "EAPMB200-4LCS" and "EAPMB200-1LCS" (same base chemical, different
  // pack size) cluster into family "EAPMB200". Confirmed as the right
  // grouping rule for the Production List / Stock Builds clustering
  // (any other SKU sharing the same parent chemical should move up
  // together, so the downpacker handles a whole bulk container's worth
  // of SKUs in one pass).
  const idx = productId.lastIndexOf("-");
  return idx > 0 ? productId.slice(0, idx) : productId;
}

function clusterByFamily(sortedList, skuKeyFn) {
  // Re-groups an ALREADY priority-sorted list so every SKU sharing a
  // parent chemical sits together, at the position of whichever member
  // of that family ranked highest in the original sort — i.e. process
  // items in priority order, and the first time a family is seen sets
  // where the whole family lands; nothing about the individual items'
  // own priority changes, just where clusters of them sit.
  const familyOrder = [];
  const familyGroups = new Map();
  for (const item of sortedList) {
    const fam = skuFamily(skuKeyFn(item));
    if (!familyGroups.has(fam)) {
      familyGroups.set(fam, []);
      familyOrder.push(fam);
    }
    familyGroups.get(fam).push(item);
  }
  const result = [];
  for (const fam of familyOrder) result.push(...familyGroups.get(fam));
  return result;
}

function stockByProduct() {
  const map = new Map();
  for (const s of currentStockLevels) {
    let lots = {};
    try { lots = JSON.parse(s.lots || "{}"); } catch (e) { lots = {}; }
    let lotSublocations = {};
    try { lotSublocations = JSON.parse(s.lot_sublocations || "{}"); } catch (e) { lotSublocations = {}; }
    map.set(s.product_id, {
      usableStock: parseFloat(s.usable_stock) || 0,
      reorderPointMax: s.reorder_point_max != null ? parseFloat(s.reorder_point_max) : null,
      unitsBackorder: parseFloat(s.units_backorder) || 0,
      lots,
      lotSublocations,
      isBuilt: s.is_built === true,
    });
  }
  return map;
}

function pickLocationsFor(sourceLots, stock) {
  // Converts a claim's sourceLots (which lot(s) were actually consumed,
  // possibly across the SKU and its case/singles pair) into real
  // pick-location text: sublocation + quantity, for every sublocation
  // that lot's stock actually sits in — not a synthetic "take exactly
  // N from bin A" apportionment (we don't track consumption at that
  // granularity), but the real physical breakdown, so the picker can
  // see where to go and grab from whichever bin is convenient.
  if (!sourceLots || sourceLots.length === 0) return [];
  const locations = [];
  for (const { sku, lotId } of sourceLots) {
    const s = stock.get(sku);
    const subMap = s && s.lotSublocations ? s.lotSublocations[lotId] : null;
    if (subMap) {
      for (const [sublocation, qty] of Object.entries(subMap)) {
        locations.push({ sublocation, qty, lotId });
      }
    } else {
      locations.push({ sublocation: "(sublocation unknown)", qty: null, lotId });
    }
  }
  return locations;
}

function lotSortKey(lotId) {
  // Two real lot-numbering schemes, confirmed against real data
  // (2026-09-18): purchased goods use "PONUMBER/X.Y" (smaller PO number
  // = older, since POs are issued sequentially); in-house-made goods use
  // "U-MMDDYY-D" (the embedded production date orders them). Returns
  // [formatType, sortValue] so the two schemes never get inappropriately
  // cross-compared against each other within the same product.
  const poMatch = lotId.match(/^(\d+)\//);
  if (poMatch) return [0, parseInt(poMatch[1], 10)];
  const dateMatch = lotId.match(/^U-(\d{2})(\d{2})(\d{2})-/);
  if (dateMatch) {
    const [, mm, dd, yy] = dateMatch;
    return [1, parseInt(`20${yy}${mm}${dd}`, 10)]; // YYYYMMDD, sortable as a plain number
  }
  return [2, 0]; // unrecognized format — sorts after both known types, never silently mismatched
}

function mergedLotsFor(sku, stock) {
  // Combines a SKU's own lot data with its case/single pair's lot data,
  // converted into the SAME unit as `sku` — but ONLY merges quantities
  // under a matching lot ID between the two. Confirmed against a real
  // example: CANLCMS-4L (singles) and CANLCMS-4LCS (cases) both had
  // stock under the SAME lot "5002362/1.2" (5 singles + 518 cases) —
  // physically the same lot, just currently split across two packaging
  // forms, so it's correct to treat that as one combined pool. A
  // DIFFERENT lot ID appearing only in the pair SKU is a genuinely
  // different physical lot and must never get merged in just because
  // it's the same chemical.
  const direct = stock.get(sku);
  const directLots = direct ? direct.lots : {};
  const pairSku = pairSkuFor(sku);
  const pair = stock.get(pairSku);
  const pairLots = pair ? pair.lots : {};

  const merged = { ...directLots };
  for (const [lotId, qty] of Object.entries(pairLots)) {
    const converted = isCaseSku(sku) ? Math.floor(qty / 4) : qty * 4;
    merged[lotId] = (merged[lotId] || 0) + converted;
  }
  return merged;
}

function sameLotCheck(sku, needed, stock) {
  // Whether this order line can be filled from a SINGLE lot (Casey's
  // stated preference — ship cases of the same lot together), and if
  // so, which lot to prefer (oldest first, real FIFO). Spans the
  // case/singles pair (see mergedLotsFor) — confirmed correct against
  // a real product where the exact same lot had stock recorded under
  // both packaging forms simultaneously.
  const lots = mergedLotsFor(sku, stock);
  const qualifying = Object.entries(lots).filter(([lotId, qty]) => qty >= needed);
  if (qualifying.length === 0) return { available: false, preferredLot: null };
  qualifying.sort((a, b) => {
    const [ta, va] = lotSortKey(a[0]);
    const [tb, vb] = lotSortKey(b[0]);
    if (ta !== tb) return ta - tb;
    return va - vb;
  });
  return { available: true, preferredLot: qualifying[0][0] };
}

function isCaseSku(sku) {
  // All Lab Alley case SKUs end in "CS", with nothing ever following it
  // — confirmed directly. The leading number is chemical concentration
  // (irrelevant here); the suffix before "CS" (e.g. "4L", "1L") is size.
  return /CS$/.test(sku);
}

function pairSkuFor(sku) {
  // Case -> single: strip trailing "CS" (same size, no case suffix).
  // Single -> case: append "CS". 1 case = 4 singles, universally.
  return isCaseSku(sku) ? sku.slice(0, -2) : sku + "CS";
}

function resolveSkuAvailability(sku, stock) {
  // We build cases from singles and break cases into singles, so a
  // shortage in the SKU actually ordered can still be covered by its
  // case/single counterpart. Effective availability = direct stock of
  // the ordered SKU + whatever its pair converts to. Shipping-only per
  // Casey's call — Production List build recommendations are untouched.
  const direct = stock.get(sku);
  const directUsable = direct ? direct.usableStock : 0;
  const pair = stock.get(pairSkuFor(sku));
  const pairUsable = pair ? pair.usableStock : 0;
  return isCaseSku(sku)
    ? directUsable + Math.floor(pairUsable / 4)   // singles converting up to cases
    : directUsable + (pairUsable * 4);             // cases converting down to singles
}

function buildWorkingStock() {
  // Deep, mutable copy of stock, split per SKU into {lots: {lotId: qty},
  // untracked: qty} — untracked covers the ~7.6% of usable-location
  // stock that genuinely has no lot ID (confirmed against real data:
  // exclusively packaging/supplies, never actual order-able chemicals).
  // Deriving usableStock as lots+untracked (rather than storing it
  // separately) means the aggregate and per-lot pictures can never
  // drift out of sync as we deplete them below.
  const stock = stockByProduct();
  const working = new Map();
  for (const [sku, s] of stock) {
    const lotsCopy = { ...s.lots };
    const lotsSum = Object.values(lotsCopy).reduce((a, b) => a + b, 0);
    working.set(sku, {
      lots: lotsCopy,
      untracked: Math.max(0, s.usableStock - lotsSum),
      reorderPointMax: s.reorderPointMax,
      unitsBackorder: s.unitsBackorder,
    });
  }
  return working;
}

function entryTotal(entry) {
  if (!entry) return 0;
  return Object.values(entry.lots).reduce((a, b) => a + b, 0) + entry.untracked;
}

function sortedLotIds(lots) {
  return Object.keys(lots).sort((a, b) => {
    const [ta, va] = lotSortKey(a);
    const [tb, vb] = lotSortKey(b);
    return ta !== tb ? ta - tb : va - vb;
  });
}

function depleteFrom(entry, qty) {
  // Removes qty units from an entry's lots (FIFO order) then untracked,
  // mutating it in place. Caller guarantees qty <= entryTotal(entry).
  // Returns which lots were actually touched and how much was taken
  // from each — used to show real pick locations (which sublocation(s)
  // to physically go to), added alongside the original depletion
  // behavior without changing it.
  let remaining = qty;
  const touched = [];
  for (const lotId of sortedLotIds(entry.lots)) {
    if (remaining <= 0) break;
    const take = Math.min(remaining, entry.lots[lotId]);
    if (take > 0) {
      entry.lots[lotId] -= take;
      touched.push({ lotId, taken: take });
    }
    remaining -= take;
  }
  if (remaining > 0) {
    const take = Math.min(remaining, entry.untracked);
    entry.untracked -= take;
    remaining -= take;
  }
  return touched;
}

function claimStock(sku, needed, working) {
  // Checks availability AND, if satisfiable, actually depletes the
  // MUTABLE working stock — this is what fixes the real bug Casey
  // found: 4 separate orders each needing 1 unit of a SKU with only 2
  // in stock were ALL showing Pickable, because every order was
  // checked against the same static, undiminished figure. Orders are
  // now processed in allocation-priority order (see buildShippingList)
  // and each one claims its share before the next is checked.
  const pairSku = pairSkuFor(sku);
  const direct = working.get(sku) || { lots: {}, untracked: 0 };
  const pair = working.get(pairSku) || { lots: {}, untracked: 0 };

  const directTotal = entryTotal(direct);
  const pairTotal = entryTotal(pair);
  const pairConverted = isCaseSku(sku) ? Math.floor(pairTotal / 4) : pairTotal * 4;
  const totalUsable = directTotal + pairConverted;

  // Same-lot check against CURRENT (already-depleted-by-earlier-orders)
  // lot quantities — per Casey's confirmation, a later order competing
  // for the same lot must see it correctly reduced, not the original.
  const mergedLots = { ...direct.lots };
  for (const [lotId, qty] of Object.entries(pair.lots)) {
    const converted = isCaseSku(sku) ? Math.floor(qty / 4) : qty * 4;
    mergedLots[lotId] = (mergedLots[lotId] || 0) + converted;
  }
  const qualifying = sortedLotIds(mergedLots).filter(lotId => mergedLots[lotId] >= needed);
  const sameLot = qualifying.length > 0;
  const preferredLot = sameLot ? qualifying[0] : null;

  if (totalUsable < needed) {
    // Blocked — deplete NOTHING, so this unfulfillable need doesn't
    // wrongly reduce stock that a different, smaller order could
    // actually use.
    return { usable: totalUsable, isBlocked: true, sameLot, preferredLot, sourceLots: [] };
  }

  // Satisfiable. Consumption order confirmed directly: prefer the
  // direct/native form first, only reach into the case/singles pair
  // once the direct form alone can't cover it (don't break open a case
  // if loose singles already suffice, and vice versa).
  const fromDirect = Math.min(needed, directTotal);
  const touchedDirect = depleteFrom(direct, fromDirect);
  const stillNeeded = needed - fromDirect;

  let touchedPair = [];
  if (stillNeeded > 0) {
    const pairUnitsNeeded = isCaseSku(sku)
      ? stillNeeded * 4                // sku=case, pair=singles
      : Math.ceil(stillNeeded / 4);    // sku=single, pair=cases
    touchedPair = depleteFrom(pair, Math.min(pairUnitsNeeded, pairTotal));
  }

  working.set(sku, direct);
  working.set(pairSku, pair);
  // sourceLots: which lots were ACTUALLY consumed for this claim (not
  // just which lot was preferred) — this is what powers the pick-
  // location column, since it reflects reality even in the (rare,
  // sameLot=false) case where a need had to be split across lots or
  // across the case/singles pair.
  const sourceLots = [
    ...touchedDirect.map(t => ({ sku, ...t })),
    ...touchedPair.map(t => ({ sku: pairSku, ...t })),
  ];
  return { usable: totalUsable, isBlocked: false, sameLot, preferredLot, sourceLots };
}

function buildShippingList(rows) {
  // Same scope as the rest of Attack the Queue: Warehouse/Freight/Both,
  // held orders excluded (nothing here is workable for a held order
  // regardless of stock).
  const scopedRows = rows.filter(r =>
    ["Warehouse", "Freight", "Both"].includes(r.core_queue) && r.shipment_status !== "on_hold"
  );
  const linesByOrder = new Map();
  for (const row of scopedRows) {
    if (!linesByOrder.has(row.order_id)) linesByOrder.set(row.order_id, []);
    linesByOrder.get(row.order_id).push(row);
  }
  const working = buildWorkingStock();

  // Process orders in ALLOCATION priority order — channel priority,
  // then FIFO — so whichever order has the strongest claim gets first
  // right to limited stock, confirmed directly with Casey as the
  // correct priority for WHO gets stock (separate from how the final
  // list is displayed, sorted again below).
  const orderEntries = Array.from(linesByOrder.entries()).map(([oid, lines]) => {
    const first = lines[0];
    const oidUpper = oid.toUpperCase();
    return {
      oid, lines, first,
      isChannelPriority: oidUpper.startsWith("AMZN") || oidUpper.startsWith("WMT"),
      orderDatetime: first.order_datetime || "",
    };
  });
  orderEntries.sort((a, b) => {
    if (a.isChannelPriority !== b.isChannelPriority) return a.isChannelPriority ? -1 : 1;
    return a.orderDatetime.localeCompare(b.orderDatetime);
  });

  const results = [];
  for (const { oid, lines, first, isChannelPriority, orderDatetime } of orderEntries) {
    // Real per-SKU quantity needed, from ShipStation's own item data
    // (captured in ss_items). Falls back to "assume at least 1" for
    // rows pulled before this field existed, rather than silently
    // treating an unknown quantity as zero risk.
    let ssItems = [];
    try { ssItems = JSON.parse(first.ss_items || "[]"); } catch (e) { ssItems = []; }
    const qtyBySku = new Map();
    for (const item of ssItems) {
      if (item.sku) qtyBySku.set(item.sku, (qtyBySku.get(item.sku) || 0) + (item.qty || 0));
    }

    // Real distinct SKUs for this order. CRITICAL FIX: when Finale
    // collapsed a split/backordered order into a single "Multiple
    // products" row, that row gives us NO real per-SKU detail — the
    // OLD code simply filtered it out, leaving zero SKUs to check,
    // which meant zero blockers were ever found and the order defaulted
    // to "Pickable" regardless of whether the real items were actually
    // in stock. Now falls back to ss_items (ShipStation's own real item
    // list) for exactly this case, same fallback already used for the
    // "(not set)" drilldown elsewhere on this page.
    let distinctSkus = [...new Set(
      lines.map(l => (l.product_id || "").trim()).filter(v => v && v !== "Multiple products")
    )];
    const hasCollapsedRow = lines.some(l => (l.product_id || "").trim() === "Multiple products");
    if (hasCollapsedRow && ssItems.length) {
      const realSkus = ssItems.map(item => (item.sku || "").trim()).filter(v => v);
      distinctSkus = [...new Set([...distinctSkus, ...realSkus])];
    }

    const items = [];
    const blockers = [];
    if (distinctSkus.length === 0) {
      // Genuinely no way to know what's in this order (collapsed row,
      // no ss_items data either) — never default to "Pickable" when we
      // can't actually confirm that.
      blockers.push({ sku: "(unknown — no item data)", needed: "?", usable: "?" });
    } else {
      for (const sku of distinctSkus) {
        const needed = qtyBySku.get(sku) || 1;
        const claim = claimStock(sku, needed, working);
        items.push({ sku, needed, usable: claim.usable, isBlocked: claim.isBlocked, sameLot: claim.sameLot, preferredLot: claim.preferredLot, sourceLots: claim.sourceLots });
        if (claim.isBlocked) blockers.push({ sku, needed, usable: claim.usable });
      }
    }

    results.push({
      orderId: oid,
      isPickable: blockers.length === 0,
      items,
      blockers,
      isChannelPriority,
      hours: orderAgeHours(orderDatetime),
      orderDatetime,
    });
  }

  // Pickable orders first (nothing blocking them), then channel
  // priority, then FIFO (oldest first) — exactly "priority source and
  // FIFO" as specified.
  results.sort((a, b) => {
    if (a.isPickable !== b.isPickable) return a.isPickable ? -1 : 1;
    if (a.isChannelPriority !== b.isChannelPriority) return a.isChannelPriority ? -1 : 1;
    return a.orderDatetime.localeCompare(b.orderDatetime);
  });
  return results;
}

function buildProductionList(shippingList) {
  // Which chemicals are actually blocking a live order right now,
  // cross-referenced from the shipping list above.
  const blockedByProduct = new Map();
  for (const order of shippingList) {
    for (const b of order.blockers) {
      if (typeof b.needed !== "number") continue; // "(unknown)" placeholder — can't attribute to a real product
      if (!blockedByProduct.has(b.sku)) {
        blockedByProduct.set(b.sku, { orderCount: 0, maxHours: 0, hasChannelPriority: false, neededTotal: 0 });
      }
      const entry = blockedByProduct.get(b.sku);
      entry.orderCount += 1;
      entry.maxHours = Math.max(entry.maxHours, order.hours);
      if (order.isChannelPriority) entry.hasChannelPriority = true;
      entry.neededTotal += Math.max(0, b.needed - b.usable);
    }
  }

  const stock = stockByProduct();
  const results = [];
  for (const [sku, entry] of blockedByProduct) {
    // Only chemicals ACTUALLY blocking a live order right now belong
    // here — pure reorder-point deficits with zero current demand moved
    // to the separate "Stock builds" list instead, so this stays a
    // purely actionable, urgent list.
    const s = stock.get(sku) || { usableStock: 0, reorderPointMax: null, unitsBackorder: 0 };

    // Build UP TO Reorder Point Max, not just enough for current
    // demand — the explicit rule: demand of 5 but a reorder max of 10
    // means building 10, to get ahead of near-future orders too.
    let buildQty;
    if (s.reorderPointMax != null) {
      buildQty = Math.max(0, s.reorderPointMax - s.usableStock);
    } else {
      buildQty = Math.max(s.unitsBackorder, entry.neededTotal);
    }
    if (buildQty <= 0) continue;

    results.push({
      sku,
      buildQty,
      usableStock: s.usableStock,
      reorderPointMax: s.reorderPointMax,
      ordersBlocked: entry.orderCount,
      maxHours: entry.maxHours,
      isChannelPriority: entry.hasChannelPriority,
    });
  }

  // Channel priority first, then how many live orders it's blocking,
  // then the oldest affected order's age — balances "fulfill the most
  // orders quickly" against "don't let aging orders sit."
  results.sort((a, b) => {
    if (a.isChannelPriority !== b.isChannelPriority) return a.isChannelPriority ? -1 : 1;
    if (a.ordersBlocked !== b.ordersBlocked) return b.ordersBlocked - a.ordersBlocked;
    return b.maxHours - a.maxHours;
  });
  return clusterByFamily(results, p => p.sku);
}

function buildStockBuilds(shippingList) {
  // Proactive restocking targets: below Reorder Point Max, but NOT
  // currently blocking any live order and no open backorder either —
  // i.e. nothing urgent is demanding it yet, but it's trending toward
  // running out. A Reorder Point Max existing at all implies it's a
  // real, tracked seller worth staying ahead of.
  const blockedSkus = new Set();
  for (const order of shippingList) {
    for (const b of order.blockers) {
      if (typeof b.needed === "number") blockedSkus.add(b.sku);
    }
  }
  const stock = stockByProduct();
  const results = [];
  for (const [sku, s] of stock) {
    if (!s.isBuilt) continue; // purchased SKUs need buying, not building (confirmed bug, 2026-10-01)
    if (s.reorderPointMax == null) continue;
    if (s.unitsBackorder > 0) continue;
    if (blockedSkus.has(sku)) continue;
    const deficit = s.reorderPointMax - s.usableStock;
    if (deficit <= 0) continue;
    const pctOfMax = s.reorderPointMax > 0 ? (s.usableStock / s.reorderPointMax * 100) : 0;
    results.push({ sku, deficit, usableStock: s.usableStock, reorderPointMax: s.reorderPointMax, pctOfMax });
  }
  // Lowest % of Reorder Point Max first — the ones closest to actually
  // running out, not just the ones with the biggest raw unit gap
  // (a high-volume item can have a huge deficit while still being
  // comfortably stocked percentage-wise; this ranks true urgency).
  results.sort((a, b) => a.pctOfMax - b.pctOfMax);
  return clusterByFamily(results, p => p.sku);
}

function formatPickLocations(locations) {
  if (!locations || locations.length === 0) return "—";
  return locations.map(l => l.qty != null ? `${l.sublocation} (${l.qty})` : l.sublocation).join(", ");
}

// ---------------------------------------------------------------------
// Daily Briefing — rule-based (not AI-generated): deterministic, so the
// same underlying queue data always produces the same read. Built
// entirely from the same buildShippingList/buildProductionList/
// buildStockBuilds output already powering the rest of Attack the
// Queue, so it can't drift from what those lists actually show.

function pluralize(n, singular, plural) {
  return n === 1 ? singular : (plural || singular + "s");
}

function nameList(items, max, formatter) {
  const named = items.slice(0, max).map(formatter);
  const remainder = items.length - named.length;
  return named.join(", ") + (remainder > 0 ? `, and ${remainder} more` : "");
}

// ---------------------------------------------------------------------
// Historical Analysis tab

function countOffHoursOrders(rows, window) {
  // Orders currently in the actionable queue whose order_datetime falls
  // within the given off-hours window — the "piled up while nobody was
  // working" count.
  const scopedRows = rows.filter(r =>
    ["Warehouse", "Freight", "Both"].includes(r.core_queue) && r.shipment_status !== "on_hold"
  );
  const linesByOrder = new Map();
  for (const row of scopedRows) {
    if (!linesByOrder.has(row.order_id)) linesByOrder.set(row.order_id, []);
    linesByOrder.get(row.order_id).push(row);
  }
  let count = 0;
  for (const [oid, lines] of linesByOrder) {
    const dt = parseShipStationNaive(lines[0].order_datetime);
    if (dt && dt.getTime() >= window.start && dt.getTime() <= window.end) count++;
  }
  return count;
}

