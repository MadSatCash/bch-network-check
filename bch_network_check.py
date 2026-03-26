#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import ssl
import struct
import time
from collections import Counter
from statistics import median
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_TIMEOUT = 10.0
AGE_WARN_THRESHOLD_SECONDS = 45 * 60  # 45 minutos

ELECTRUM_SOURCES: List[Tuple[str, int, bool, str]] = [
    ("bch.event.cash", 50002, True, "Electrum-1"),
    ("electrum.imaginary.cash", 50002, True, "Electrum-2"),
    ("bch.imaginary.cash", 50002, True, "Electrum-3"),
    ("bch.loping.net", 50002, True, "Electrum-4"),
    ("cashnode.bch.ninja", 50002, True, "Electrum-5"),
    ("fulcrum.aglauck.com", 50002, True, "Electrum-6"),
    ("bch.cyberbits.eu", 50002, True, "Electrum-7"),
    ("blackie.c3-soft.com", 50002, True, "Electrum-8"),
    ("bch0.kister.net", 50002, True, "Electrum-9"),
    ("bch.soul-dev.com", 50002, True, "Electrum-10"),
]


def set_console_title(title: str) -> None:
    try:
        if os.name == "nt":
            os.system(f"title {title}")
        else:
            print(f"\33]0;{title}\a", end="", flush=True)
    except Exception:
        pass


def progress(msg: str) -> None:
    print(msg, flush=True)


def sha256d(data: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


def format_hash_le_as_be_hex(h: bytes) -> str:
    return h[::-1].hex()


def bits_to_target(bits_hex: str) -> int:
    bits = int(bits_hex, 16)
    exponent = bits >> 24
    mantissa = bits & 0x007FFFFF
    return mantissa * (1 << (8 * (exponent - 3)))


def target_to_difficulty(target: int) -> float:
    max_target = 0x00000000FFFF0000000000000000000000000000000000000000000000000000
    if target <= 0:
        return 0.0
    return max_target / target


def human_seconds(seconds: Optional[float]) -> str:
    if seconds is None:
        return "-"
    s = float(seconds)
    if s < 60:
        return f"{s:.1f}s"
    if s < 3600:
        return f"{s/60:.1f}m"
    if s < 86400:
        return f"{s/3600:.1f}h"
    return f"{s/86400:.1f}d"


def short_hash(h: Optional[str], keep: int = 12) -> str:
    if not h:
        return "-"
    if len(h) <= keep * 2 + 3:
        return h
    return f"{h[:keep]}...{h[-keep:]}"


def json_from_bytes(raw: bytes, source: str) -> Any:
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        raise RuntimeError(f"{source}: respuesta vacía")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        preview = text[:160].replace("\n", " ").replace("\r", " ")
        raise RuntimeError(f"{source}: respuesta no-JSON: {preview!r}")


class ElectrumClient:
    def __init__(self, host: str, port: int, use_ssl: bool, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.host = host
        self.port = port
        self.use_ssl = use_ssl
        self.timeout = timeout
        self._id = 0

    def request(self, method: str, params: Optional[list] = None) -> Any:
        raw_sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        try:
            sock = raw_sock
            if self.use_ssl:
                ctx = ssl.create_default_context()
                sock = ctx.wrap_socket(raw_sock, server_hostname=self.host)
            sock.settimeout(self.timeout)
            f = sock.makefile("rwb")

            self._id += 1
            payload = {
                "jsonrpc": "2.0",
                "id": self._id,
                "method": method,
                "params": params or [],
            }
            f.write((json.dumps(payload) + "\n").encode("utf-8"))
            f.flush()

            line = f.readline()
            if not line:
                raise RuntimeError("cerró la conexión sin responder")

            resp = json_from_bytes(line, f"Electrum {self.host}:{self.port}")
            if isinstance(resp, dict) and resp.get("error"):
                raise RuntimeError(f"error RPC: {resp['error']}")
            if not isinstance(resp, dict):
                raise RuntimeError("respuesta inesperada")
            return resp.get("result")
        finally:
            try:
                raw_sock.close()
            except Exception:
                pass


def parse_header80(header_hex: str) -> Dict[str, Any]:
    header = bytes.fromhex(header_hex)
    if len(header) != 80:
        raise ValueError(f"header inválido: {len(header)} bytes")

    timestamp = struct.unpack("<I", header[68:72])[0]
    bits_hex = header[72:76][::-1].hex()
    block_hash_hex = format_hash_le_as_be_hex(sha256d(header))

    return {
        "time": timestamp,
        "bits_hex": bits_hex,
        "hash_hex": block_hash_hex,
    }


def fetch_tip_from_electrum(client: ElectrumClient) -> Dict[str, Any]:
    result = client.request("blockchain.headers.subscribe", [])
    if not isinstance(result, dict):
        raise RuntimeError("subscribe no devolvió objeto")

    height = int(result["height"])
    header_hex = result.get("hex")
    if not header_hex:
        raise RuntimeError("subscribe no devolvió hex del tip")

    parsed = parse_header80(header_hex)
    target = bits_to_target(parsed["bits_hex"])
    difficulty = target_to_difficulty(target)

    return {
        "height": height,
        "hash_hex": parsed["hash_hex"],
        "time": parsed["time"],
        "bits_hex": parsed["bits_hex"],
        "difficulty": difficulty,
    }


def query_electrum_sources(timeout: float) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for host, port, use_ssl, label in ELECTRUM_SOURCES:
        progress(f"Consultando {label}...")
        row: Dict[str, Any] = {
            "Fuente": label,
            "Host": f"{host}:{port}",
            "Estado": "Sin datos",
            "Altura": "-",
            "Hash": "-",
            "Bits": "-",
            "Dificultad": "-",
            "Edad": "-",
            "Delta": "-",
            "_raw_height": None,
            "_raw_hash": None,
            "_raw_age_seconds": None,
        }
        try:
            client = ElectrumClient(host, port, use_ssl, timeout=timeout)
            tip = fetch_tip_from_electrum(client)
            age_seconds = max(0, time.time() - int(tip["time"]))
            row.update({
                "Estado": "Datos",
                "Altura": str(tip["height"]),
                "Hash": short_hash(tip["hash_hex"]),
                "Bits": tip["bits_hex"],
                "Dificultad": f"{tip['difficulty']:.2f}",
                "Edad": human_seconds(age_seconds),
                "_raw_height": int(tip["height"]),
                "_raw_hash": str(tip["hash_hex"]),
                "_raw_age_seconds": float(age_seconds),
            })
        except Exception as e:
            row["Estado"] = "Error"
            row["Hash"] = str(e)[:28]
        rows.append(row)
    return rows


def apply_consensus_deltas(rows: List[Dict[str, Any]]) -> Tuple[Optional[int], Optional[str], int, int, Optional[float]]:
    pairs = [
        (int(r["_raw_height"]), str(r["_raw_hash"]))
        for r in rows
        if r.get("_raw_height") is not None and r.get("_raw_hash") is not None
    ]
    if not pairs:
        return None, None, 0, 0, None

    pair_counter = Counter(pairs)
    (consensus_height, consensus_hash), consensus_count = pair_counter.most_common(1)[0]
    total_ok = len(pairs)

    consensus_ages: List[float] = []
    for r in rows:
        raw_height = r.get("_raw_height")
        if raw_height is not None:
            r["Delta"] = str(abs(int(raw_height) - int(consensus_height)))
        if (
            r.get("_raw_height") == consensus_height
            and r.get("_raw_hash") == consensus_hash
            and r.get("_raw_age_seconds") is not None
        ):
            consensus_ages.append(float(r["_raw_age_seconds"]))

    median_age = median(consensus_ages) if consensus_ages else None
    return consensus_height, consensus_hash, consensus_count, total_ok, median_age


def compute_status(consensus_count: int, total_ok: int, median_age_seconds: Optional[float]) -> str:
    if total_ok == 0:
        return "WARN"
    if consensus_count <= (total_ok / 2):
        return "WARN"
    if median_age_seconds is None:
        return "WARN"
    if median_age_seconds > AGE_WARN_THRESHOLD_SECONDS:
        return "WARN"
    return "OK"


def build_table(rows: List[Dict[str, Any]], headers: List[str]) -> str:
    widths: Dict[str, int] = {h: len(h) for h in headers}
    for row in rows:
        for h in headers:
            widths[h] = max(widths[h], len(str(row.get(h, ""))))

    def sep() -> str:
        return "+" + "+".join("-" * (widths[h] + 2) for h in headers) + "+"

    def line(values: List[str]) -> str:
        return "| " + " | ".join(values[i].ljust(widths[headers[i]]) for i in range(len(headers))) + " |"

    out = [sep(), line(headers), sep()]
    for row in rows:
        out.append(line([str(row.get(h, "")) for h in headers]))
    out.append(sep())
    return "\n".join(out)


def parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Chequeo de red BCH")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    return p.parse_args(argv)


def main() -> int:
    import sys

    args = parse_args(sys.argv[1:])

    set_console_title("Chequeo de red BCH")
    print("=== CHEQUEO DE RED BCH ===", flush=True)

    try:
        progress("Consultando red...")
        electrum_rows = query_electrum_sources(timeout=args.timeout)
        consensus_height, consensus_hash, consensus_count, total_ok, median_age_seconds = apply_consensus_deltas(electrum_rows)
        status = compute_status(consensus_count, total_ok, median_age_seconds)
    except Exception as e:
        print()
        print("Status: WARN", flush=True)
        print(f"Detalle: {e}", flush=True)
        return 1

    progress("Armando tabla...")
    print()
    print("TABLA A - CONSENSO ELECTRUM", flush=True)
    print(build_table(
        electrum_rows,
        ["Fuente", "Host", "Estado", "Altura", "Hash", "Bits", "Dificultad", "Edad", "Delta"]
    ), flush=True)

    print()
    print(f"Status: {status}", flush=True)
    if consensus_height is not None:
        print(
            f"Consenso Electrum: altura={consensus_height} | hash={short_hash(consensus_hash)} | coincidencias={consensus_count}/{total_ok}",
            flush=True,
        )
        print(
            f"Edad mediana del consenso: {human_seconds(median_age_seconds)} | Tolerance: {human_seconds(AGE_WARN_THRESHOLD_SECONDS)}",
            flush=True,
        )
    else:
        print("Consenso Electrum: -", flush=True)

    return 0 if status == "OK" else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""
Este script verifica rápidamente si la red de Bitcoin Cash (BCH)
parece estar funcionando con normalidad consultando 10 servidores
Electrum públicos. De cada uno obtiene la altura del bloque más
reciente, el hash de ese bloque, los bits de dificultad, la
dificultad calculada y la antigüedad del último bloque. Luego
compara esos datos entre todas las fuentes para detectar si la
mayoría ve la misma cadena, calcula la diferencia de altura de
cada fuente respecto del consenso y determina un estado final:
"OK" si más de la mitad de las fuentes que respondieron coinciden
en altura y hash, y además la antigüedad mediana del bloque
consensuado no supera 45 minutos; en caso contrario marca "WARN".
Finalmente muestra todo en una tabla simple para que se vea
rápido si hay consenso real o si alguna fuente está atrasada,
caída o mostrando algo distinto.
"""