"""Schémas de données. `Extraction` est le JSON forcé en sortie de l'extracteur."""
from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

DocType = Literal[
    "CNI", "PASSEPORT", "PERMIS", "CARTE_CMU", "CARTE_CONSULAIRE",
    "EXTRAIT_NAISSANCE", "ATTESTATION", "AUTRE", "ILLISIBLE",
]
Face = Literal["RECTO", "VERSO", "UNIQUE", "INCONNUE"]

_EMPTY = {"", "NULL", "NONE", "N/A", "NA", "ND", "-", "--", "?", "INCONNU", "ILLISIBLE"}


def _clean(v: Any) -> str | None:
    if v is None:
        return None
    s = re.sub(r"\s+", " ", str(v)).strip()
    return None if s.upper() in _EMPTY else s


class Confiance(BaseModel):
    nom: float | None = None
    prenoms: float | None = None
    numero_piece: float | None = None

    @field_validator("*", mode="before")
    @classmethod
    def _clip(cls, v: Any) -> float | None:
        if v is None:
            return None
        try:
            return max(0.0, min(1.0, float(v)))
        except (TypeError, ValueError):
            return None


class Extraction(BaseModel):
    """Sortie normalisée de l'extraction d'UNE image (une face de pièce)."""

    lisible: bool = True
    type_piece: DocType = "AUTRE"
    face: Face = "INCONNUE"
    nom: str | None = None
    prenoms: str | None = None
    numero_piece: str | None = None
    date_naissance: str | None = None
    confiance: Confiance = Field(default_factory=Confiance)
    remarques: str = ""
    # métadonnées techniques
    backend: str = ""
    erreur: str | None = None
    mrz: dict | None = None

    @field_validator("nom", "prenoms", "numero_piece", "date_naissance", mode="before")
    @classmethod
    def _strings(cls, v: Any) -> str | None:
        return _clean(v)

    @field_validator("type_piece", mode="before")
    @classmethod
    def _type(cls, v: Any) -> str:
        v = (str(v) if v else "AUTRE").upper().strip().replace(" ", "_")
        allowed = set(DocType.__args__)  # type: ignore[attr-defined]
        return v if v in allowed else "AUTRE"

    @field_validator("face", mode="before")
    @classmethod
    def _face(cls, v: Any) -> str:
        v = (str(v) if v else "INCONNUE").upper().strip()
        return v if v in set(Face.__args__) else "INCONNUE"  # type: ignore[attr-defined]

    def conf(self, field: str) -> float:
        c = getattr(self.confiance, field, None)
        return 0.0 if c is None else c

    @property
    def n_fields(self) -> int:
        return sum(1 for x in (self.nom, self.prenoms, self.numero_piece) if x)
