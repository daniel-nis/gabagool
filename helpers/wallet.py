"""
Wallet management for Polymarket CLOB trading.

Handles:
- Private key loading from environment
- EIP-712 structured data signing for orders
- Nonce management
- Balance checking

Usage:
    wallet = Wallet.from_env()  # Loads POLYMARKET_PRIVATE_KEY
    signature = wallet.sign_order(order_data)
"""
import os
import time
from dataclasses import dataclass
from typing import Optional, Dict, Any
from decimal import Decimal
import hashlib
import json

try:
    from eth_account import Account
    from eth_account.messages import encode_typed_data
    from web3 import Web3
    HAS_WEB3 = True
except ImportError:
    HAS_WEB3 = False
    print("Warning: web3/eth-account not installed. Run: pip install web3 eth-account")


# Polymarket CLOB EIP-712 Domain
POLYMARKET_DOMAIN = {
    "name": "Polymarket CTF Exchange",
    "version": "1",
    "chainId": 137,  # Polygon mainnet
}

# EIP-712 Types for Order signing
ORDER_TYPES = {
    "EIP712Domain": [
        {"name": "name", "type": "string"},
        {"name": "version", "type": "string"},
        {"name": "chainId", "type": "uint256"},
    ],
    "Order": [
        {"name": "salt", "type": "uint256"},
        {"name": "maker", "type": "address"},
        {"name": "signer", "type": "address"},
        {"name": "taker", "type": "address"},
        {"name": "tokenId", "type": "uint256"},
        {"name": "makerAmount", "type": "uint256"},
        {"name": "takerAmount", "type": "uint256"},
        {"name": "expiration", "type": "uint256"},
        {"name": "nonce", "type": "uint256"},
        {"name": "feeRateBps", "type": "uint256"},
        {"name": "side", "type": "uint8"},
        {"name": "signatureType", "type": "uint8"},
    ],
}


@dataclass
class WalletConfig:
    """Configuration for wallet."""
    private_key: str
    rpc_url: str = "https://polygon-rpc.com"
    chain_id: int = 137


class Wallet:
    """
    Wallet for Polymarket CLOB trading.

    Handles key management, order signing, and balance checking.
    """

    def __init__(self, config: WalletConfig):
        if not HAS_WEB3:
            raise ImportError("web3 and eth-account required. Run: pip install web3 eth-account")

        self.config = config
        self._account = Account.from_key(config.private_key)
        self._web3 = Web3(Web3.HTTPProvider(config.rpc_url))
        self._nonce = 0
        self._nonce_timestamp = 0

    @classmethod
    def from_env(cls, key_name: str = "POLYMARKET_PRIVATE_KEY") -> "Wallet":
        """
        Load wallet from environment variable.

        Args:
            key_name: Environment variable name containing private key

        Returns:
            Wallet instance

        Raises:
            ValueError: If environment variable not set
        """
        private_key = os.environ.get(key_name)
        if not private_key:
            raise ValueError(f"Environment variable {key_name} not set")

        # Handle keys with or without 0x prefix
        if not private_key.startswith("0x"):
            private_key = "0x" + private_key

        rpc_url = os.environ.get("POLYGON_RPC_URL", "https://polygon-rpc.com")

        config = WalletConfig(
            private_key=private_key,
            rpc_url=rpc_url,
        )
        return cls(config)

    @property
    def address(self) -> str:
        """Get wallet address (checksummed)."""
        return self._account.address

    @property
    def address_lower(self) -> str:
        """Get wallet address (lowercase, for API calls)."""
        return self._account.address.lower()

    def get_balance(self, token_address: Optional[str] = None) -> Decimal:
        """
        Get wallet balance.

        Args:
            token_address: ERC20 token address, or None for MATIC

        Returns:
            Balance in token units (not wei)
        """
        if token_address is None:
            # Native MATIC balance
            balance_wei = self._web3.eth.get_balance(self.address)
            return Decimal(balance_wei) / Decimal(10**18)
        else:
            # ERC20 token balance
            # Minimal ABI for balanceOf
            erc20_abi = [
                {
                    "constant": True,
                    "inputs": [{"name": "_owner", "type": "address"}],
                    "name": "balanceOf",
                    "outputs": [{"name": "balance", "type": "uint256"}],
                    "type": "function",
                }
            ]
            contract = self._web3.eth.contract(
                address=Web3.to_checksum_address(token_address),
                abi=erc20_abi,
            )
            balance = contract.functions.balanceOf(self.address).call()
            return Decimal(balance) / Decimal(10**6)  # USDC has 6 decimals

    def get_usdc_balance(self) -> Decimal:
        """Get USDC balance on Polygon."""
        USDC_POLYGON = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
        return self.get_balance(USDC_POLYGON)

    def generate_salt(self) -> int:
        """Generate unique salt for order."""
        return int(time.time() * 1000000) + int.from_bytes(os.urandom(4), "big")

    def get_next_nonce(self) -> int:
        """
        Get next nonce for order.

        Nonces must be unique and increasing per address.
        """
        current_time = int(time.time())
        if current_time > self._nonce_timestamp:
            self._nonce_timestamp = current_time
            self._nonce = 0
        else:
            self._nonce += 1

        # Combine timestamp and counter for unique nonce
        return (self._nonce_timestamp * 1000) + self._nonce

    def sign_order(
        self,
        token_id: str,
        side: str,  # "BUY" or "SELL"
        size: float,  # Number of shares
        price: float,  # Price per share (0-1)
        expiration: Optional[int] = None,
        fee_rate_bps: int = 0,
    ) -> Dict[str, Any]:
        """
        Sign an order for Polymarket CLOB.

        Args:
            token_id: Outcome token ID (YES or NO token)
            side: "BUY" or "SELL"
            size: Number of shares to trade
            price: Price per share (0.01 to 0.99)
            expiration: Unix timestamp when order expires (default: 1 day)
            fee_rate_bps: Fee rate in basis points (default: 0)

        Returns:
            Signed order dict ready for API submission
        """
        if expiration is None:
            expiration = int(time.time()) + 86400  # 1 day from now

        # Convert to contract units
        # Price is in cents (0-100), size is in shares
        # makerAmount = what you're giving (USDC for BUY, shares for SELL)
        # takerAmount = what you're getting (shares for BUY, USDC for SELL)

        size_units = int(size * 10**6)  # 6 decimal places
        price_units = int(price * 10**6)  # 6 decimal places

        if side.upper() == "BUY":
            # Buying: give USDC, get shares
            maker_amount = int(size * price * 10**6)  # USDC to pay
            taker_amount = size_units  # Shares to receive
            side_int = 0
        else:
            # Selling: give shares, get USDC
            maker_amount = size_units  # Shares to give
            taker_amount = int(size * price * 10**6)  # USDC to receive
            side_int = 1

        salt = self.generate_salt()
        nonce = self.get_next_nonce()

        # Build order struct
        order = {
            "salt": salt,
            "maker": self.address,
            "signer": self.address,
            "taker": "0x0000000000000000000000000000000000000000",
            "tokenId": int(token_id) if isinstance(token_id, str) and token_id.startswith("0x") else int(token_id, 16) if isinstance(token_id, str) else token_id,
            "makerAmount": maker_amount,
            "takerAmount": taker_amount,
            "expiration": expiration,
            "nonce": nonce,
            "feeRateBps": fee_rate_bps,
            "side": side_int,
            "signatureType": 0,  # EOA signature
        }

        # Build EIP-712 typed data
        typed_data = {
            "types": ORDER_TYPES,
            "primaryType": "Order",
            "domain": POLYMARKET_DOMAIN,
            "message": order,
        }

        # Sign the typed data
        signable = encode_typed_data(full_message=typed_data)
        signed = self._account.sign_message(signable)

        # Return order with signature
        return {
            "order": order,
            "signature": signed.signature.hex(),
            "orderType": "GTC",  # Good till cancelled
            # API-friendly format
            "salt": str(salt),
            "maker": self.address_lower,
            "signer": self.address_lower,
            "taker": "0x0000000000000000000000000000000000000000",
            "tokenID": str(order["tokenId"]),
            "makerAmount": str(maker_amount),
            "takerAmount": str(taker_amount),
            "expiration": str(expiration),
            "nonce": str(nonce),
            "feeRateBps": str(fee_rate_bps),
            "side": "BUY" if side_int == 0 else "SELL",
            "signatureType": 0,
        }

    def sign_cancel_order(self, order_id: str) -> Dict[str, Any]:
        """
        Sign a cancel order request.

        Args:
            order_id: Order ID to cancel

        Returns:
            Signed cancel request
        """
        timestamp = int(time.time())
        message = f"Cancel order {order_id} at {timestamp}"

        # Sign raw message
        from eth_account.messages import encode_defunct
        signable = encode_defunct(text=message)
        signed = self._account.sign_message(signable)

        return {
            "orderID": order_id,
            "timestamp": timestamp,
            "signature": signed.signature.hex(),
        }

    def sign_api_request(self, method: str, path: str, body: Optional[dict] = None) -> Dict[str, str]:
        """
        Sign an API request for authentication.

        Args:
            method: HTTP method (GET, POST, DELETE)
            path: API path
            body: Request body (for POST)

        Returns:
            Headers dict with authentication
        """
        timestamp = int(time.time())

        # Build message to sign
        body_str = json.dumps(body, separators=(",", ":")) if body else ""
        message = f"{timestamp}{method.upper()}{path}{body_str}"

        # Sign
        from eth_account.messages import encode_defunct
        signable = encode_defunct(text=message)
        signed = self._account.sign_message(signable)

        return {
            "POLY_ADDRESS": self.address_lower,
            "POLY_SIGNATURE": signed.signature.hex(),
            "POLY_TIMESTAMP": str(timestamp),
            "POLY_NONCE": str(self.get_next_nonce()),
        }


def check_wallet_setup() -> bool:
    """
    Check if wallet is properly configured.

    Returns:
        True if wallet can be loaded, False otherwise
    """
    if not HAS_WEB3:
        print("❌ web3/eth-account not installed")
        print("   Run: pip install web3 eth-account")
        return False

    private_key = os.environ.get("POLYMARKET_PRIVATE_KEY")
    if not private_key:
        print("❌ POLYMARKET_PRIVATE_KEY not set")
        print("   Export your wallet private key:")
        print("   export POLYMARKET_PRIVATE_KEY=0x...")
        return False

    try:
        wallet = Wallet.from_env()
        print(f"✅ Wallet loaded: {wallet.address}")

        balance = wallet.get_balance()
        print(f"   MATIC balance: {balance:.4f}")

        try:
            usdc = wallet.get_usdc_balance()
            print(f"   USDC balance: ${usdc:.2f}")
        except Exception as e:
            print(f"   USDC balance: (unable to check: {e})")

        return True
    except Exception as e:
        print(f"❌ Wallet error: {e}")
        return False


if __name__ == "__main__":
    print("Polymarket Wallet Setup Check\n")
    check_wallet_setup()
