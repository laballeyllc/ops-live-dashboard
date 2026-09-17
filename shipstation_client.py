"""
Thin wrapper around the ShipStation v1 REST API.

Docs: https://www.shipstation.com/docs/api/

Auth: ShipStation uses HTTP Basic Auth with your API Key as the username
and API Secret as the password.
"""
import os
import time
import requests

BASE_URL = "https://ssapi.shipstation.com"


class ShipStationClient:
    def __init__(self, api_key: str = None, api_secret: str = None):
        self.api_key = api_key or os.environ["SHIPSTATION_API_KEY"]
        self.api_secret = api_secret or os.environ["SHIPSTATION_API_SECRET"]
        self.session = requests.Session()
        self.session.auth = (self.api_key, self.api_secret)

    def _get(self, path: str, params: dict = None, retries: int = 3):
        """GET with basic retry/backoff for ShipStation's rate limiting (429s)."""
        url = f"{BASE_URL}{path}"
        for attempt in range(retries):
            resp = self.session.get(url, params=params, timeout=30)
            if resp.status_code == 429:
                # ShipStation sends a Retry-After header (seconds) or X-Rate-Limit-Reset
                wait = int(resp.headers.get("Retry-After", 5))
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        resp.raise_for_status()

    def count_orders(self, order_status: str = None, create_date_start: str = None,
                      create_date_end: str = None, modify_date_start: str = None,
                      modify_date_end: str = None, tag_id: int = None) -> int:
        """
        Returns the total count of orders matching filters, without
        paging through every result (ShipStation returns a `total` field
        alongside the first page).
        Dates should be 'MM/DD/YYYY HH:MM' strings (ShipStation's expected format).
        """
        params = {"pageSize": 1, "page": 1}
        if order_status:
            params["orderStatus"] = order_status
        if create_date_start:
            params["createDateStart"] = create_date_start
        if create_date_end:
            params["createDateEnd"] = create_date_end
        if modify_date_start:
            params["modifyDateStart"] = modify_date_start
        if modify_date_end:
            params["modifyDateEnd"] = modify_date_end
        if tag_id is not None:
            params["tagId"] = tag_id

        data = self._get("/orders", params)
        return data.get("total", 0)

    def list_tags(self):
        """
        Returns all tags defined on the account:
        [{"tagId": 123, "name": "Warehouse", "color": "#..."}, ...]
        """
        return self._get("/accounts/listtags")

    def get_tag_id(self, tag_name: str) -> int:
        """Look up a tag's numeric ID by its display name (case-insensitive)."""
        tags = self.list_tags()
        for tag in tags:
            if tag.get("name", "").strip().lower() == tag_name.strip().lower():
                return tag["tagId"]
        available = ", ".join(t.get("name", "?") for t in tags)
        raise ValueError(f"No ShipStation tag named '{tag_name}' found. Available tags: {available}")

    def count_by_tag(self, tag_id: int, order_status: str = None,
                      create_date_start: str = None, create_date_end: str = None) -> int:
        """
        Counts orders matching a tag, filtering client-side.

        ShipStation's `tagId` query parameter on /orders is not reliably
        honored by every account/API version — some accounts return the
        unfiltered total regardless of tagId. This fetches the matching
        orders (by status/date only) and checks each order's `tagIds` array
        locally, which is slower but correct.
        """
        orders = self.list_orders(
            order_status=order_status,
            create_date_start=create_date_start,
            create_date_end=create_date_end,
        )
        return sum(1 for o in orders if tag_id in (o.get("tagIds") or []))

    def list_warehouses(self):
        """
        Returns all Ship From Locations ("warehouses") on the account:
        [{"warehouseId": 123, "warehouseName": "Dripping Springs Warehouse", ...}, ...]
        """
        return self._get("/warehouses")

    def get_order_by_number(self, order_number) -> dict | None:
        """
        Looks up a single order by its exact order number (this is what we
        use to cross-reference a Finale Order ID against ShipStation, e.g.
        to resolve the Warehouse/Freight location for orders that fulfill
        outside ShipStation and never generate a shipment record there).

        Returns the order dict (with `advancedOptions.warehouseId`) or
        None if ShipStation has no record of that order number at all.
        """
        data = self._get("/orders", {"orderNumber": str(order_number), "pageSize": 1})
        orders = data.get("orders", [])
        return orders[0] if orders else None

    def list_tags(self) -> dict[int, str]:
        """
        Returns {tagId: tagName} for every tag defined in the account.
        This is the real mechanism that determines an order's queue
        (Warehouse, Freight/"Jerry", the drop-ship vendors, holds, etc.)
        — ShipStation's automation rules assign these tags directly, so
        reading them is the correct way to classify an order, rather than
        trying to infer the queue from a field like Ship From Location.
        """
        data = self._get("/accounts/listtags")
        return {t["tagId"]: t["name"] for t in data}

    def list_users(self) -> dict[str, str]:
        """
        Returns {userId: name} for every user on the account. This is how
        we translate an order's assigned userId (a GUID) into a human
        name like "Jerry" or "Warehouse" — confirmed as the actual,
        authoritative signal ShipStation uses to route orders into the
        Freight/Warehouse queues. An order can get assigned this way
        through the tag-based automation rule OR a completely separate
        SKU-based rule that never touches tags at all (e.g. any item SKU
        ending in "55GAL" auto-assigns straight to Jerry) — which is
        exactly what caused a real order (000406235) to be missed under
        the old tag-only classification. Reading the actual assignment
        directly, rather than trying to enumerate every rule that can
        produce it, is the robust fix.
        """
        data = self._get("/users", {"showInactive": "true"})
        return {u["userId"]: (u.get("name") or u.get("userName") or "") for u in data}

    def list_stores(self) -> dict[int, str]:
        """
        Returns {storeId: storeName} for every store/channel connected to
        the account (Magento, Amazon, Walmart, etc.). Used to label which
        sales channel an order came from — order['advancedOptions']
        ['storeId'] is the numeric ID; this translates it to a real name.
        """
        data = self._get("/stores", {"showInactive": "true"})
        return {s["storeId"]: s.get("storeName", "") for s in data}

    def get_warehouse_id(self, warehouse_name: str) -> int:
        """Look up a Ship From Location's numeric ID by its display name (case-insensitive)."""
        warehouses = self.list_warehouses()
        for w in warehouses:
            if w.get("warehouseName", "").strip().lower() == warehouse_name.strip().lower():
                return w["warehouseId"]
        available = ", ".join(w.get("warehouseName", "?") for w in warehouses)
        raise ValueError(f"No ShipStation Ship From Location named '{warehouse_name}' found. Available: {available}")

    def count_by_warehouse(self, warehouse_id: int, order_status: str = None,
                            create_date_start: str = None, create_date_end: str = None) -> int:
        """
        Counts orders whose Ship From Location (advancedOptions.warehouseId)
        matches the given warehouse_id. Filtered client-side for the same
        reliability reason as count_by_tag.
        """
        orders = self.list_orders(
            order_status=order_status,
            create_date_start=create_date_start,
            create_date_end=create_date_end,
        )
        return sum(
            1 for o in orders
            if (o.get("advancedOptions") or {}).get("warehouseId") == warehouse_id
        )

    def count_by_warehouses(self, warehouse_ids, order_status: str = None,
                             create_date_start: str = None, create_date_end: str = None) -> int:
        """Same as count_by_warehouse, but matches ANY of a set of warehouse IDs
        (e.g. combined Warehouse + Freight locations)."""
        ids = set(warehouse_ids)
        orders = self.list_orders(
            order_status=order_status,
            create_date_start=create_date_start,
            create_date_end=create_date_end,
        )
        return sum(
            1 for o in orders
            if (o.get("advancedOptions") or {}).get("warehouseId") in ids
        )

    def list_shipments_by_warehouses(self, warehouse_ids, ship_date_start: str,
                                      ship_date_end: str, include_voided: bool = False):
        """Same as list_shipments, but filtered to shipments whose warehouseId
        matches any of the given IDs (e.g. combined Warehouse + Freight)."""
        ids = set(warehouse_ids)
        shipments = self.list_shipments(ship_date_start, ship_date_end, include_voided)
        return [s for s in shipments if s.get("warehouseId") in ids]

    def list_orders(self, order_status: str = None, create_date_start: str = None,
                     create_date_end: str = None, modify_date_start: str = None,
                     modify_date_end: str = None, page_size: int = 500):
        """
        Returns full order records (paged) rather than just a count — needed
        when we have to filter client-side on a field ShipStation's query
        params don't expose directly (e.g. tag, warehouse, or store, depending
        on how Warehouse vs Freight queues end up being distinguished).

        Prefer modify_date_start/end over create_date when you're trying to
        catch orders that *shipped* recently — an order can be created long
        before it ships (backorders sitting in queue), so filtering on
        create date can pull in a much wider, slower window than intended.
        Modify date moves when the order's status changes (e.g. to shipped),
        so it tracks recent activity much more tightly.
        """
        results = []
        page = 1
        while True:
            params = {"pageSize": page_size, "page": page}
            if order_status:
                params["orderStatus"] = order_status
            if create_date_start:
                params["createDateStart"] = create_date_start
            if create_date_end:
                params["createDateEnd"] = create_date_end
            if modify_date_start:
                params["modifyDateStart"] = modify_date_start
            if modify_date_end:
                params["modifyDateEnd"] = modify_date_end
            data = self._get("/orders", params)
            orders = data.get("orders", [])
            results.extend(orders)
            total_pages = data.get("pages", 1)
            print(f"    page {page}/{total_pages} ({len(results)} orders so far)")
            if page >= total_pages:
                break
            page += 1
        return results

    def list_shipments(self, ship_date_start: str, ship_date_end: str, include_voided: bool = False):
        """
        Returns all shipment records for a date range (paged through).
        Each shipment record includes fields like shipmentId, orderId,
        shipDate, carrierCode, shipmentCost, etc.
        """
        results = []
        page = 1
        while True:
            params = {
                "shipDateStart": ship_date_start,
                "shipDateEnd": ship_date_end,
                "pageSize": 500,
                "page": page,
                "includeShipmentItems": "false",
            }
            data = self._get("/shipments", params)
            shipments = data.get("shipments", [])
            if not include_voided:
                shipments = [s for s in shipments if not s.get("voided")]
            results.extend(shipments)
            if page >= data.get("pages", 1):
                break
            page += 1
        return results
