"""
NewSocial BLE — micro-backend REST
===================================
Associa token BLE temporanei (hex 32 char / 16 byte) ad account Instagram.

Setup rapido
------------
# 1. Crea e attiva un virtualenv (raccomandato)
python -m venv .venv
source .venv/bin/activate          # Linux / macOS
.venv\\Scripts\\activate            # Windows

# 2. Installa le dipendenze
pip install -r requirements.txt

# 3. Avvia il server locale (hot-reload in sviluppo)
uvicorn main:app --reload --host 0.0.0.0 --port 8000

# Documentazione interattiva disponibile su:
#   http://localhost:8000/docs   (Swagger UI)
#   http://localhost:8000/redoc  (ReDoc)
"""

import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Dict

from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, field_validator

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TOKEN_TTL_SECONDS: int = 900          # 15 minutes
TOKEN_HEX_LENGTH: int = 32            # secrets.token_hex(16) → 32 hex chars
INSTAGRAM_RE = re.compile(r"^@?([A-Za-z0-9_.]{1,30})$")

# ---------------------------------------------------------------------------
# In-memory token store
# ---------------------------------------------------------------------------

@dataclass
class TokenRecord:
    instagram: str
    created_at: float
    expires_at: float


class TokenStore:
    """Thread-unsafe in-memory store — suitable for single-process dev only.

    A Redis / database backend should replace this before any production use.
    Expiry is enforced lazily on every read call and proactively on every write
    call (full sweep), keeping memory bounded without a background thread.
    """

    def __init__(self) -> None:
        self._store: Dict[str, TokenRecord] = {}

    # ── Write ────────────────────────────────────────────────────────────────

    def register(self, instagram: str) -> tuple[str, TokenRecord]:
        """Generate a new token for *instagram* and persist it.

        Runs a full expiry sweep before inserting so stale entries are
        collected eagerly on every registration call.
        """
        self._sweep_expired()

        token = secrets.token_hex(16)          # 16 bytes → 32 hex chars
        now = time.time()
        record = TokenRecord(
            instagram=instagram,
            created_at=now,
            expires_at=now + TOKEN_TTL_SECONDS,
        )
        self._store[token] = record
        return token, record

    # ── Read ─────────────────────────────────────────────────────────────────

    def resolve(self, tokens: list[str]) -> Dict[str, dict]:
        """Return only the active (non-expired) records for *tokens*.

        Expired tokens are silently omitted from the result — the caller
        should treat a missing key as "unknown / expired".
        """
        now = time.time()
        results: Dict[str, dict] = {}
        for token in tokens:
            record = self._store.get(token)
            if record is not None and now < record.expires_at:
                results[token] = {"instagram": record.instagram}
        return results

    # ── Maintenance ───────────────────────────────────────────────────────────

    def _sweep_expired(self) -> int:
        """Remove all expired records. Returns the number deleted."""
        now = time.time()
        expired = [t for t, r in self._store.items() if now >= r.expires_at]
        for token in expired:
            del self._store[token]
        return len(expired)

    def __len__(self) -> int:
        return len(self._store)


# Singleton store for the lifetime of the process
_store = TokenStore()

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="NewSocial BLE Backend",
    description=(
        "Associa token BLE temporanei (TTL 15 min) ad account Instagram. "
        "In-memory store — solo per sviluppo locale."
    ),
    version="0.1.0",
)

# CORS — aperto a tutte le origini per lo sviluppo locale.
# Restringere `allow_origins` prima del deploy in produzione.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Pydantic request / response models
# ---------------------------------------------------------------------------

class RegisterRequest(BaseModel):
    instagram: str

    @field_validator("instagram")
    @classmethod
    def validate_instagram(cls, v: str) -> str:
        """Strip leading '@' and validate the username format.

        Rules: 1–30 characters, letters / digits / underscores / dots only.
        """
        match = INSTAGRAM_RE.match(v.strip())
        if not match:
            raise ValueError(
                "Username Instagram non valido. "
                "Usa solo lettere, cifre, underscore e punti (max 30 caratteri)."
            )
        return match.group(1)   # stored without '@'


class RegisterResponse(BaseModel):
    token: str
    expires_in_seconds: int


class ResolveRequest(BaseModel):
    tokens: list[str]

    @field_validator("tokens")
    @classmethod
    def validate_tokens(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("La lista tokens non può essere vuota.")
        if len(v) > 100:
            raise ValueError("Puoi risolvere al massimo 100 token per chiamata.")
        for token in v:
            if len(token) != TOKEN_HEX_LENGTH or not all(
                c in "0123456789abcdefABCDEF" for c in token
            ):
                raise ValueError(
                    f"Token non valido: '{token}'. "
                    f"Deve essere una stringa hex di {TOKEN_HEX_LENGTH} caratteri."
                )
        return [t.lower() for t in v]   # normalise to lowercase


class ResolveResponse(BaseModel):
    results: Dict[str, dict]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post(
    "/beacon/register",
    response_model=RegisterResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Registra un account Instagram e ottieni un token BLE",
    tags=["beacon"],
)
def register(body: RegisterRequest) -> RegisterResponse:
    """Genera un token BLE sicuro (hex 32 char) associato all'username
    Instagram fornito.

    Il token ha una validità di **15 minuti** (900 secondi) e deve essere
    incorporato nel payload dell'advertising BLE del dispositivo.

    - **instagram**: username Instagram (con o senza prefisso `@`)
    """
    token, record = _store.register(body.instagram)
    remaining = int(record.expires_at - time.time())
    return RegisterResponse(token=token, expires_in_seconds=remaining)


@app.post(
    "/beacon/resolve",
    response_model=ResolveResponse,
    summary="Risolvi una lista di token BLE in profili Instagram",
    tags=["beacon"],
)
def resolve(body: ResolveRequest) -> ResolveResponse:
    """Riceve una lista di token BLE rilevati durante la scansione e
    restituisce i soli profili Instagram **attivi** (token non scaduto).

    I token sconosciuti o scaduti vengono **omessi** dalla risposta —
    tratta una chiave assente come "non trovato / scaduto".

    - **tokens**: lista di stringhe hex da 32 caratteri (max 100 per chiamata)
    """
    results = _store.resolve(body.tokens)
    return ResolveResponse(results=results)


# ---------------------------------------------------------------------------
# Health / debug helpers
# ---------------------------------------------------------------------------

@app.get("/health", include_in_schema=False)
def health() -> dict:
    """Liveness probe — usato da load balancer / Docker healthcheck."""
    return {"status": "ok", "active_tokens": len(_store)}


@app.delete(
    "/beacon/purge",
    include_in_schema=False,          # nascosto in produzione
    status_code=status.HTTP_200_OK,
)
def purge() -> dict:
    """Forza la pulizia dei token scaduti. Solo per debug/test."""
    deleted = _store._sweep_expired()
    return {"deleted": deleted, "remaining": len(_store)}
