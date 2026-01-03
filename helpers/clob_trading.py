"""
Polymarket CLOB Trading API Client.

Handles:
- Order submission (POST /orders)
- Order cancellation (DELETE /orders/{id})
- Order status queries (GET /orders)
- Trade history

Usage:
    from helpers.wallet import Wallet
    from helpers.clob_trading import CLOBClient

    wallet = Wallet.from_env()
    client = CLOBClient(wallet)

    # Place a buy order
    order = await client.place_order(
        token_id="0x...",
        side="BUY",
        size=30,
        price=0.45,
    )

    # Cancel order
    await client.cancel_order(order["id"])
"""
import asyncio
import time
from dataclasses import dataclass
from typing import Optional, Dict, Any, List
from enum import Enum
import json

try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False
    import requests

try:
    from .wallet import Wallet
except ImportError:
    from wallet import Wallet


# Polymarket CLOB API endpoints
CLOB_API_BASE = "https://clob.polymarket.com"


class OrderStatus(Enum):
    """Order status on CLOB."""
    LIVE = "LIVE"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    PENDING = "PENDING"


class OrderSide(Enum):
    """Order side."""
    BUY = "BUY"
    SELL = "SELL"


@dataclass
class Order:
    """Represents an order on the CLOB."""
    id: str
    token_id: str
    side: OrderSide
    size: float
    price: float
    filled_size: float
    status: OrderStatus
    created_at: int
    expiration: int

    @classmethod
    def from_api(cls, data: dict) -> "Order":
        """Create Order from API response."""
        return cls(
            id=data.get("id", ""),
            token_id=data.get("asset_id", data.get("tokenId", "")),
            side=OrderSide(data.get("side", "BUY")),
            size=float(data.get("original_size", data.get("size", 0))),
            price=float(data.get("price", 0)),
            filled_size=float(data.get("size_matched", data.get("filled", 0))),
            status=OrderStatus(data.get("status", "PENDING")),
            created_at=int(data.get("created_at", 0)),
            expiration=int(data.get("expiration", 0)),
        )


class CLOBClient:
    """
    Async client for Polymarket CLOB API.

    Handles order placement, cancellation, and queries.
    """

    def __init__(
        self,
        wallet: Wallet,
        base_url: str = CLOB_API_BASE,
        timeout: float = 30.0,
        dry_run: bool = False,
    ):
        """
        Initialize CLOB client.

        Args:
            wallet: Wallet instance for signing
            base_url: CLOB API base URL
            timeout: Request timeout in seconds
            dry_run: If True, sign but don't submit orders
        """
        self.wallet = wallet
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.dry_run = dry_run
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create async HTTP client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
                headers={"Content-Type": "application/json"},
            )
        return self._client

    async def close(self):
        """Close the HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    def _get_auth_headers(self, method: str, path: str, body: Optional[dict] = None) -> Dict[str, str]:
        """Get authentication headers for request."""
        return self.wallet.sign_api_request(method, path, body)

    async def place_order(
        self,
        token_id: str,
        side: str,
        size: float,
        price: float,
        expiration: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Place a limit order on the CLOB.

        Args:
            token_id: Token ID to trade (YES or NO outcome token)
            side: "BUY" or "SELL"
            size: Number of shares
            price: Price per share (0.01 to 0.99)
            expiration: Unix timestamp when order expires

        Returns:
            Order response dict with order ID

        Raises:
            CLOBError: If order placement fails
        """
        # Sign the order
        signed_order = self.wallet.sign_order(
            token_id=token_id,
            side=side,
            size=size,
            price=price,
            expiration=expiration,
        )

        if self.dry_run:
            print(f"[DRY RUN] Would place order: {side} {size} @ {price}")
            print(f"          Token: {token_id}")
            print(f"          Signature: {signed_order['signature'][:20]}...")
            return {
                "id": f"dry_run_{int(time.time() * 1000)}",
                "status": "DRY_RUN",
                "order": signed_order,
            }

        # Submit to CLOB
        path = "/order"
        body = {
            "order": {
                "salt": signed_order["salt"],
                "maker": signed_order["maker"],
                "signer": signed_order["signer"],
                "taker": signed_order["taker"],
                "tokenId": signed_order["tokenID"],
                "makerAmount": signed_order["makerAmount"],
                "takerAmount": signed_order["takerAmount"],
                "expiration": signed_order["expiration"],
                "nonce": signed_order["nonce"],
                "feeRateBps": signed_order["feeRateBps"],
                "side": 0 if side.upper() == "BUY" else 1,
                "signatureType": 0,
            },
            "signature": signed_order["signature"],
            "owner": signed_order["maker"],
            "orderType": "GTC",
        }

        headers = self._get_auth_headers("POST", path, body)
        client = await self._get_client()

        try:
            response = await client.post(path, json=body, headers=headers)
            response.raise_for_status()
            result = response.json()

            return {
                "id": result.get("orderID", result.get("id", "")),
                "status": result.get("status", "PENDING"),
                "response": result,
                "order": signed_order,
            }
        except httpx.HTTPStatusError as e:
            error_body = e.response.text if e.response else "No response"
            raise CLOBError(f"Order placement failed: {e.response.status_code} - {error_body}")
        except Exception as e:
            raise CLOBError(f"Order placement error: {e}")

    async def cancel_order(self, order_id: str) -> Dict[str, Any]:
        """
        Cancel an open order.

        Args:
            order_id: Order ID to cancel

        Returns:
            Cancellation response

        Raises:
            CLOBError: If cancellation fails
        """
        if self.dry_run:
            print(f"[DRY RUN] Would cancel order: {order_id}")
            return {"status": "DRY_RUN_CANCELLED", "orderID": order_id}

        # Sign cancellation
        signed_cancel = self.wallet.sign_cancel_order(order_id)

        path = f"/order/{order_id}"
        headers = self._get_auth_headers("DELETE", path)
        client = await self._get_client()

        try:
            response = await client.delete(
                path,
                headers=headers,
                params={
                    "signature": signed_cancel["signature"],
                    "timestamp": signed_cancel["timestamp"],
                },
            )
            response.raise_for_status()
            return {"status": "CANCELLED", "orderID": order_id}
        except httpx.HTTPStatusError as e:
            error_body = e.response.text if e.response else "No response"
            raise CLOBError(f"Cancel failed: {e.response.status_code} - {error_body}")
        except Exception as e:
            raise CLOBError(f"Cancel error: {e}")

    async def cancel_all_orders(self, market_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Cancel all open orders.

        Args:
            market_id: Optional - only cancel orders for this market

        Returns:
            Cancellation response
        """
        if self.dry_run:
            print(f"[DRY RUN] Would cancel all orders" + (f" for {market_id}" if market_id else ""))
            return {"status": "DRY_RUN_CANCELLED_ALL"}

        path = "/orders"
        params = {}
        if market_id:
            params["market"] = market_id

        headers = self._get_auth_headers("DELETE", path)
        client = await self._get_client()

        try:
            response = await client.delete(path, headers=headers, params=params)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            raise CLOBError(f"Cancel all error: {e}")

    async def get_order(self, order_id: str) -> Optional[Order]:
        """
        Get order status.

        Args:
            order_id: Order ID to query

        Returns:
            Order object or None if not found
        """
        path = f"/order/{order_id}"
        headers = self._get_auth_headers("GET", path)
        client = await self._get_client()

        try:
            response = await client.get(path, headers=headers)
            if response.status_code == 404:
                return None
            response.raise_for_status()
            return Order.from_api(response.json())
        except Exception as e:
            raise CLOBError(f"Get order error: {e}")

    async def get_open_orders(self, market_id: Optional[str] = None) -> List[Order]:
        """
        Get all open orders.

        Args:
            market_id: Optional - filter by market

        Returns:
            List of Order objects
        """
        path = "/orders"
        params = {"status": "LIVE"}
        if market_id:
            params["market"] = market_id

        headers = self._get_auth_headers("GET", path)
        client = await self._get_client()

        try:
            response = await client.get(path, headers=headers, params=params)
            response.raise_for_status()
            data = response.json()
            orders = data.get("orders", data) if isinstance(data, dict) else data
            return [Order.from_api(o) for o in orders]
        except Exception as e:
            raise CLOBError(f"Get orders error: {e}")

    async def get_trades(
        self,
        market_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """
        Get trade history.

        Args:
            market_id: Optional - filter by market
            limit: Max number of trades to return

        Returns:
            List of trade dicts
        """
        path = "/trades"
        params = {"limit": limit}
        if market_id:
            params["market"] = market_id

        headers = self._get_auth_headers("GET", path)
        client = await self._get_client()

        try:
            response = await client.get(path, headers=headers, params=params)
            response.raise_for_status()
            data = response.json()
            return data.get("trades", data) if isinstance(data, dict) else data
        except Exception as e:
            raise CLOBError(f"Get trades error: {e}")

    async def wait_for_fill(
        self,
        order_id: str,
        timeout: float = 60.0,
        poll_interval: float = 1.0,
    ) -> Order:
        """
        Wait for an order to be filled.

        Args:
            order_id: Order ID to wait for
            timeout: Max time to wait in seconds
            poll_interval: Time between status checks

        Returns:
            Final Order object

        Raises:
            CLOBError: If timeout or order cancelled/expired
        """
        start_time = time.time()

        while (time.time() - start_time) < timeout:
            order = await self.get_order(order_id)

            if order is None:
                raise CLOBError(f"Order {order_id} not found")

            if order.status == OrderStatus.FILLED:
                return order

            if order.status in (OrderStatus.CANCELLED, OrderStatus.EXPIRED):
                raise CLOBError(f"Order {order_id} {order.status.value}")

            await asyncio.sleep(poll_interval)

        raise CLOBError(f"Timeout waiting for order {order_id}")


class CLOBError(Exception):
    """Error from CLOB API."""
    pass


# Synchronous wrapper for non-async code
class CLOBClientSync:
    """
    Synchronous wrapper for CLOBClient.

    For use in non-async contexts.
    """

    def __init__(self, wallet: Wallet, **kwargs):
        self._async_client = CLOBClient(wallet, **kwargs)

    def _run(self, coro):
        """Run coroutine in event loop."""
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)

    def place_order(self, **kwargs) -> Dict[str, Any]:
        return self._run(self._async_client.place_order(**kwargs))

    def cancel_order(self, order_id: str) -> Dict[str, Any]:
        return self._run(self._async_client.cancel_order(order_id))

    def get_order(self, order_id: str) -> Optional[Order]:
        return self._run(self._async_client.get_order(order_id))

    def get_open_orders(self, **kwargs) -> List[Order]:
        return self._run(self._async_client.get_open_orders(**kwargs))


if __name__ == "__main__":
    # Test the client
    import os

    print("CLOB Client Test\n")

    if not os.environ.get("POLYMARKET_PRIVATE_KEY"):
        print("Set POLYMARKET_PRIVATE_KEY to test")
        exit(1)

    async def test():
        wallet = Wallet.from_env()
        print(f"Wallet: {wallet.address}")

        # Dry run mode
        client = CLOBClient(wallet, dry_run=True)

        # Test order signing
        result = await client.place_order(
            token_id="12345",
            side="BUY",
            size=10,
            price=0.45,
        )
        print(f"\nDry run order: {result['id']}")

        await client.close()

    asyncio.run(test())
