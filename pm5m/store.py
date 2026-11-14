"""File persistence: settings, secrets (optionally encrypted), paper orders, series, reset/archive."""
import base64
import csv
import json
import os
import shutil
import tempfile
import time
from typing import Any, Dict, List, Optional


def _atomic_write(path: str, text: str, mode: int = 0o644) -> None:
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


CSV_FIELDS = [
    "id", "slug", "side", "price", "shares", "placed_iso", "tau_at_place", "queue_ahead0", "model_p", "p_adj",
    "spot", "B", "sigma_rel", "binance", "basis", "status", "cancel_reason", "filled", "avg_fill", "first_fill_iso",
    "settled", "side_won", "official_side_won", "final_value", "rebate", "pnl",
]


def _iso(ts: Optional[float]) -> str:
    if not ts:
        return ""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def order_to_row(o: dict) -> dict:
    f = o.get("filled") or 0.0
    row = dict(o)
    row["placed_iso"] = _iso(o.get("placed_ts"))
    row["first_fill_iso"] = _iso(o.get("first_fill_ts"))
    row["avg_fill"] = round(o["cost"] / f, 4) if f else ""
    return {k: row.get(k, "") for k in CSV_FIELDS}


def orders_to_csv(orders: List[dict]) -> str:
    import io
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_FIELDS)
    w.writeheader()
    for o in orders:
        w.writerow(order_to_row(o))
    return buf.getvalue()


class SecretBox:
    """Secrets file; encrypted with Fernet(scrypt(passphrase)) when a passphrase is given."""

    def __init__(self, path: str, passphrase: Optional[str] = None):
        self.path = path
        self.passphrase = passphrase or None

    @property
    def encrypted(self) -> bool:
        return bool(self.passphrase)

    def _fernet(self, salt: bytes):
        from cryptography.fernet import Fernet
        from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
        kdf = Scrypt(salt=salt, length=32, n=2 ** 14, r=8, p=1)
        return Fernet(base64.urlsafe_b64encode(kdf.derive(self.passphrase.encode())))

    def save(self, data: Dict[str, Any]) -> None:
        if self.passphrase:
            salt = os.urandom(16)
            token = self._fernet(salt).encrypt(json.dumps(data).encode()).decode()
            doc = {"v": 1, "enc": "fernet-scrypt", "salt": base64.b64encode(salt).decode(), "token": token}
        else:
            doc = {"v": 1, "enc": "none", "data": data}
        _atomic_write(self.path, json.dumps(doc), 0o600)

    def load(self) -> Dict[str, Any]:
        if not os.path.exists(self.path):
            return {}
        with open(self.path, "r", encoding="utf-8") as f:
            doc = json.load(f)
        if doc.get("enc") == "fernet-scrypt":
            if not self.passphrase:
                raise RuntimeError("secrets file is encrypted; set PM5M_SECRET_PASSPHRASE")
            salt = base64.b64decode(doc["salt"])
            return json.loads(self._fernet(salt).decrypt(doc["token"].encode()).decode())
        return doc.get("data", {})

    def clear(self) -> None:
        if os.path.exists(self.path):
            os.remove(self.path)


class DataStore:
    def __init__(self, data_dir: str, passphrase: Optional[str] = None):
        self.dir = data_dir
        os.makedirs(self.dir, exist_ok=True)
        os.makedirs(os.path.join(self.dir, "series"), exist_ok=True)
        self.settings_path = os.path.join(self.dir, "settings.json")
        self.orders_path = os.path.join(self.dir, "paper_orders.json")
        self.events_path = os.path.join(self.dir, "events.jsonl")
        self.secrets = SecretBox(os.path.join(self.dir, "secrets.json"), passphrase)

    # settings
    def load_settings(self) -> dict:
        try:
            with open(self.settings_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def save_settings(self, d: dict) -> None:
        _atomic_write(self.settings_path, json.dumps(d, indent=2, ensure_ascii=False))

    # orders
    def load_orders(self) -> List[dict]:
        try:
            with open(self.orders_path, "r", encoding="utf-8") as f:
                v = json.load(f)
                return v if isinstance(v, list) else []
        except (OSError, ValueError):
            return []

    def save_orders(self, orders: List[dict]) -> None:
        _atomic_write(self.orders_path, json.dumps(orders, ensure_ascii=False))
        _atomic_write(os.path.join(self.dir, "paper_orders.csv"), orders_to_csv(orders))

    def append_event(self, ev: dict) -> None:
        with open(self.events_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")

    # per-window series (raw data for later backtests)
    def save_series(self, slug: str, doc: dict, keep: int = 3000) -> None:
        d = os.path.join(self.dir, "series")
        _atomic_write(os.path.join(d, f"{slug}.json"), json.dumps(doc))
        files = sorted(os.listdir(d))
        for old in files[:-keep] if len(files) > keep else []:
            try:
                os.remove(os.path.join(d, old))
            except OSError:
                pass

    # reset
    def reset_records(self, archive: bool = True, include_series: bool = False) -> Optional[str]:
        """Clear paper records. With archive=True the files are moved to data/archive/<ts>/."""
        targets = [self.orders_path, os.path.join(self.dir, "paper_orders.csv"), self.events_path]
        dest = None
        if archive:
            dest = os.path.join(self.dir, "archive", time.strftime("%Y%m%d_%H%M%S", time.gmtime()))
            os.makedirs(dest, exist_ok=True)
        for p in targets:
            if os.path.exists(p):
                if archive:
                    shutil.move(p, os.path.join(dest, os.path.basename(p)))
                else:
                    os.remove(p)
        if include_series:
            sd = os.path.join(self.dir, "series")
            if archive and os.path.isdir(sd) and os.listdir(sd):
                shutil.move(sd, os.path.join(dest, "series"))
            elif os.path.isdir(sd):
                shutil.rmtree(sd)
            os.makedirs(sd, exist_ok=True)
        return dest
