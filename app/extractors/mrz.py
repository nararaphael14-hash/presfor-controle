"""Lecture et validation de zones MRZ (OACI 9303) : TD1 (CNI, 3x30), TD2 (2x36), TD3 (passeport, 2x44)."""
from __future__ import annotations

import re

_W = (7, 3, 1)


def _val(c: str) -> int:
    if c.isdigit():
        return int(c)
    if "A" <= c <= "Z":
        return ord(c) - 55
    return 0  # '<'


def check_digit(field: str) -> int:
    return sum(_val(c) * _W[i % 3] for i, c in enumerate(field)) % 10


def _ok(field: str, digit: str) -> bool:
    return digit.isdigit() and check_digit(field) == int(digit)


def _names(s: str) -> tuple[str, str]:
    sur, _, giv = s.partition("<<")
    clean = lambda x: re.sub(r"\s+", " ", x.replace("<", " ")).strip()
    return clean(sur), clean(giv)


def clean_line(s: str) -> str:
    s = s.upper().replace(" ", "").replace("«", "<").replace("‹", "<").replace("〈", "<").replace("≪", "<")
    return re.sub(r"[^A-Z0-9<]", "", s)


def _fit(line: str, n: int) -> str | None:
    if abs(len(line) - n) > 3 or len(line) < n - 3:
        return None
    return line[:n].ljust(n, "<")


def candidate_lines(text_lines: list[str]) -> list[str]:
    out = []
    for raw in text_lines:
        c = clean_line(raw)
        if len(c) >= 26 and c.count("<") >= 3:
            out.append(c)
    return out


def parse_mrz(text_lines: list[str]) -> dict | None:
    """Retourne {format, numero, nom, prenoms, date_naissance, valid, checks} ou None."""
    lines = candidate_lines(text_lines)
    # TD3
    for i in range(len(lines) - 1):
        a, b = _fit(lines[i], 44), _fit(lines[i + 1], 44)
        if a and b and a[0] == "P":
            sur, giv = _names(a[5:])
            checks = {
                "numero": _ok(b[0:9], b[9]),
                "naissance": _ok(b[13:19], b[19]),
                "expiration": _ok(b[21:27], b[27]),
                "global": _ok(b[0:10] + b[13:20] + b[21:43], b[43]),
            }
            return _result("TD3", b[0:9].replace("<", ""), sur, giv, b[13:19], checks)
    # TD1
    for i in range(len(lines) - 2):
        a, b, c = (_fit(lines[i + k], 30) for k in range(3))
        if a and b and c and a[0] in "ICA":
            sur, giv = _names(c)
            checks = {
                "numero": _ok(a[5:14], a[14]),
                "naissance": _ok(b[0:6], b[6]),
                "expiration": _ok(b[8:14], b[14]),
                "global": _ok(a[5:30] + b[0:7] + b[8:15] + b[18:29], b[29]),
            }
            return _result("TD1", a[5:14].replace("<", ""), sur, giv, b[0:6], checks)
    # TD2
    for i in range(len(lines) - 1):
        a, b = _fit(lines[i], 36), _fit(lines[i + 1], 36)
        if a and b and a[0] in "IAC":
            sur, giv = _names(a[5:])
            checks = {
                "numero": _ok(b[0:9], b[9]),
                "naissance": _ok(b[13:19], b[19]),
                "expiration": _ok(b[21:27], b[27]),
                "global": _ok(b[0:10] + b[13:20] + b[21:35], b[35]),
            }
            return _result("TD2", b[0:9].replace("<", ""), sur, giv, b[13:19], checks)
    return None


def _result(fmt: str, number: str, sur: str, giv: str, dob: str, checks: dict) -> dict:
    return {
        "format": fmt,
        "numero": number,
        "nom": sur,
        "prenoms": giv,
        "date_naissance_aammjj": dob,
        "checks": checks,
        "valid": all(checks.values()),
        "n_valid": sum(checks.values()),
    }
