"""Polymarket CLOB authentication helpers.

L1: EIP-712 ClobAuth signature (private key) -> create/derive API credentials.
L2: HMAC-SHA256 over timestamp+method+path+body (API secret) -> authenticated requests.
Orders are NOT created by this module; the private key is only used for L1.
"""
import base64
import hashlib
import hmac

CLOB_AUTH_MESSAGE = "This message attests that I control the given wallet"
CHAIN_ID = 137


def l1_typed_data(address: str, timestamp, nonce: int = 0, chain_id: int = CHAIN_ID) -> dict:
    return {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
            ],
            "ClobAuth": [
                {"name": "address", "type": "address"},
                {"name": "timestamp", "type": "string"},
                {"name": "nonce", "type": "uint256"},
                {"name": "message", "type": "string"},
            ],
        },
        "primaryType": "ClobAuth",
        "domain": {"name": "ClobAuthDomain", "version": "1", "chainId": chain_id},
        "message": {"address": address, "timestamp": str(timestamp), "nonce": nonce,
                    "message": CLOB_AUTH_MESSAGE},
    }


def address_from_key(private_key: str) -> str:
    from eth_account import Account
    return Account.from_key(private_key).address


def sign_l1(private_key: str, timestamp, nonce: int = 0):
    """Returns (address, signature_hex)."""
    from eth_account import Account
    acct = Account.from_key(private_key)
    full = l1_typed_data(acct.address, timestamp, nonce)
    try:
        from eth_account.messages import encode_typed_data
        signable = encode_typed_data(full_message=full)
    except ImportError:  # older eth-account
        from eth_account.messages import encode_structured_data
        signable = encode_structured_data(full)
    sig = acct.sign_message(signable).signature.hex()
    if not sig.startswith("0x"):
        sig = "0x" + sig
    return acct.address, sig


def l1_headers(private_key: str, timestamp, nonce: int = 0) -> dict:
    addr, sig = sign_l1(private_key, timestamp, nonce)
    return {"POLY_ADDRESS": addr, "POLY_SIGNATURE": sig, "POLY_TIMESTAMP": str(timestamp),
            "POLY_NONCE": str(nonce)}


def l2_signature(secret: str, timestamp, method: str, path: str, body: str = "") -> str:
    s = secret + "=" * (-len(secret) % 4)
    key = base64.urlsafe_b64decode(s)
    msg = f"{timestamp}{method.upper()}{path}{body}"
    return base64.urlsafe_b64encode(hmac.new(key, msg.encode(), hashlib.sha256).digest()).decode()


def l2_headers(address: str, creds: dict, timestamp, method: str, path: str, body: str = "") -> dict:
    """creds: {'apiKey','secret','passphrase'}. `path` must exclude the query string."""
    return {
        "POLY_ADDRESS": address,
        "POLY_SIGNATURE": l2_signature(creds["secret"], timestamp, method, path, body),
        "POLY_TIMESTAMP": str(timestamp),
        "POLY_API_KEY": creds["apiKey"],
        "POLY_PASSPHRASE": creds["passphrase"],
    }
