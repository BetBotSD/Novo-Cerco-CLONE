# -*- coding: utf-8 -*-
"""
pinnacle_cerco_client.py
------------------------
Cliente HTTP enxuto para o CERCO, falando com a API Pinnacle via RapidAPI.

Responsabilidades principais:
- Encapsular as chamadas aos endpoints `/markets` e `/special-markets`
  do KIT da Pinnacle disponível no RapidAPI.
- Não aplicar nenhuma lógica de janela de tempo (Δh) ou filtragem de eventos;
  isso é papel do CercoEngine.

Update 2025-11-21:
- Chunking por TAMANHO de string do parâmetro league_ids (limite RapidAPI ~254 chars).
- Variáveis de ambiente:
    PINNACLE_LEAGUE_IDS_MAX_CHARS (default 240)
    PINNACLE_LEAGUE_BATCH_SIZE    (default 60)  # cap opcional por quantidade

Update 2025-11-22 (alinhamento snapshot completo + robustez RapidAPI):
- Retry/backoff em 429/5xx (env PINNACLE_HTTP_RETRIES, PINNACLE_HTTP_BACKOFF_SEC).
- Headers Accept JSON.
- _get_json mais tolerante a payloads não-dict.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence

import httpx

# ---------------------------------------------------------------------------
# CONFIG BÁSICA (pode vir direto de env ou ser sobrescrita via construtor)
# ---------------------------------------------------------------------------

RAPIDAPI_KEY: str = os.getenv("RAPIDAPI_KEY", "")
RAPIDAPI_HOST: str = os.getenv("RAPIDAPI_HOST", "pinnacle-odds.p.rapidapi.com")

# Base URL do KIT Pinnacle no RapidAPI; pode ser sobrescrita via PINNACLE_BASE_URL
PINNACLE_BASE_URL: str = os.getenv(
    "PINNACLE_BASE_URL",
    f"https://{RAPIDAPI_HOST}/kit/v1",
)

HTTP_TIMEOUT_SEC: float = float(os.getenv("PINNACLE_HTTP_TIMEOUT_SEC", "10.0"))

# Limite de caracteres para a string "league_ids=1,2,3,..."
PINNACLE_LEAGUE_IDS_MAX_CHARS: int = int(
    os.getenv("PINNACLE_LEAGUE_IDS_MAX_CHARS", "240")
)

# Cap por quantidade (secundário).
PINNACLE_LEAGUE_BATCH_SIZE: int = int(
    os.getenv("PINNACLE_LEAGUE_BATCH_SIZE", "60")
)

PINNACLE_HTTP_RETRIES: int = int(os.getenv("PINNACLE_HTTP_RETRIES", "2"))
PINNACLE_HTTP_BACKOFF_SEC: float = float(os.getenv("PINNACLE_HTTP_BACKOFF_SEC", "1.5"))

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    try:
        return int(raw) if raw is not None and raw != "" else default
    except Exception:
        return default


def _normalize_league_ids(league_ids: Iterable[int | str]) -> str:
    """Converte uma lista de ids em string '1834,1234,5678'."""
    return ",".join(str(x) for x in league_ids)


def _try_get_list(payload: Dict[str, Any], *keys: str) -> List[Any]:
    """Tenta obter uma lista em payload sob qualquer uma das chaves."""
    for k in keys:
        v = payload.get(k)
        if isinstance(v, list):
            return v
    return []


def _chunk_league_ids_by_len(
    league_ids: Sequence[int | str],
    *,
    max_chars: int,
    max_items: int,
) -> List[List[int]]:
    """
    Quebra league_ids em chunks que respeitam:
    - max_chars no STRING "a,b,c"
    - max_items por chunk (cap secundário)
    """
    chunks: List[List[int]] = []
    cur: List[int] = []
    cur_len = 0

    for x in league_ids:
        sx = str(x)
        add_len = len(sx) + (1 if cur else 0)  # vírgula se já existe item

        if cur and (cur_len + add_len > max_chars or len(cur) >= max_items):
            chunks.append(cur)
            cur = []
            cur_len = 0
            add_len = len(sx)

        cur.append(int(x))
        cur_len += add_len

    if cur:
        chunks.append(cur)

    return chunks


# ---------------------------------------------------------------------------
# CLIENT
# ---------------------------------------------------------------------------

class PinnacleCercoClient:
    """Client fino para acessar `/markets` e `/special-markets` via RapidAPI."""

    def __init__(
        self,
        rapidapi_key: Optional[str] = None,
        rapidapi_host: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout_sec: float = HTTP_TIMEOUT_SEC,
        *,
        league_ids_max_chars: int = PINNACLE_LEAGUE_IDS_MAX_CHARS,
        league_batch_size: int = PINNACLE_LEAGUE_BATCH_SIZE,
        http_retries: int = PINNACLE_HTTP_RETRIES,
        http_backoff_sec: float = PINNACLE_HTTP_BACKOFF_SEC,
    ) -> None:
        self.rapidapi_key = rapidapi_key or RAPIDAPI_KEY
        self.rapidapi_host = rapidapi_host or RAPIDAPI_HOST

        if not self.rapidapi_key:
            raise RuntimeError(
                "PinnacleCercoClient: RAPIDAPI_KEY não configurada. "
                "Defina a env RAPIDAPI_KEY ou passe rapidapi_key no construtor."
            )

        self.base_url = base_url or PINNACLE_BASE_URL
        self.timeout_sec = timeout_sec

        self.league_ids_max_chars = league_ids_max_chars
        self.league_batch_size = league_batch_size

        self.http_retries = max(0, int(http_retries))
        self.http_backoff_sec = max(0.1, float(http_backoff_sec))

        headers = {
            "x-rapidapi-key": self.rapidapi_key,
            "x-rapidapi-host": self.rapidapi_host,
            "accept": "application/json",
        }

        self._client = httpx.Client(
            base_url=self.base_url,
            headers=headers,
            timeout=self.timeout_sec,
        )

        logger.debug(
            "PinnacleCercoClient iniciado | base_url=%s timeout=%.2f "
            "max_chars=%d max_items=%d retries=%d backoff=%.2fs",
            self.base_url,
            self.timeout_sec,
            self.league_ids_max_chars,
            self.league_batch_size,
            self.http_retries,
            self.http_backoff_sec,
        )

    # ------------------------------------------------------------------
    # Context manager / lifecycle
    # ------------------------------------------------------------------

    def __enter__(self) -> "PinnacleCercoClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        if getattr(self, "_client", None) is not None:
            self._client.close()
            self._client = None  # type: ignore[assignment]

    # ------------------------------------------------------------------
    # HTTP baixo nível
    # ------------------------------------------------------------------

    def _get_json(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Executa um GET com retry/backoff e retorna resp.json()."""
        if not path.startswith("/"):
            path = "/" + path

        url_for_log = f"{self.base_url}{path}"

        last_err: Optional[Exception] = None

        for attempt in range(self.http_retries + 1):
            if attempt > 0:
                sleep_s = self.http_backoff_sec * (2 ** (attempt - 1))
                logger.warning("Retry %d/%d | sleeping %.2fs | %s", attempt, self.http_retries, sleep_s, url_for_log)
                time.sleep(sleep_s)

            try:
                logger.info("HTTP GET %s | params=%s", url_for_log, params)
                resp = self._client.get(path, params=params)
                logger.info("→ status=%s", resp.status_code)

                # Retry em throttling/erro temporário
                if resp.status_code in (429, 500, 502, 503, 504):
                    try:
                        body = resp.text
                    except Exception:
                        body = ""
                    raise httpx.HTTPStatusError(
                        f"status={resp.status_code}",
                        request=resp.request,
                        response=resp,
                    )

                resp.raise_for_status()
                data = resp.json()
                if not isinstance(data, dict):
                    logger.warning("Payload não-dict recebido (%s). Envelopando como dict.", type(data))
                    return {"data": data}
                logger.debug("→ payload keys: %s", list(data.keys()))
                return data

            except Exception as e:
                last_err = e
                # se não há retry restante, propaga
                if attempt >= self.http_retries:
                    logger.error("HTTP GET falhou (sem retries restantes) | %s | err=%r", url_for_log, e)
                    raise

        # não deve chegar aqui
        if last_err:
            raise last_err
        return {}

    # ------------------------------------------------------------------
    # /markets (payload bruto)
    # ------------------------------------------------------------------

    def fetch_markets(
        self,
        sport_id: int,
        league_ids: Iterable[int | str],
        *,
        event_type: str = "prematch",
        is_have_odds: bool = True,
    ) -> Dict[str, Any]:
        """Wrapper genérico para o endpoint `/markets` (payload bruto)."""
        params: Dict[str, Any] = {
            "league_ids": _normalize_league_ids(league_ids),
            "event_type": event_type,
            "sport_id": str(sport_id),
            "is_have_odds": "true" if is_have_odds else "false",
        }
        return self._get_json("/markets", params)

    def fetch_markets_for_leagues(
        self,
        sport_id: int,
        league_ids: List[int],
        *,
        event_type: str = "prematch",
        is_have_odds: bool = True,
    ) -> Dict[str, Any]:
        """
        Compatível com CercoEngine. Faz chunking seguro por TAMANHO de string.
        Também faz fallback automático se receber 422 string_too_long.
        """
        ids = list(league_ids)
        if not ids:
            return {}

        max_chars = _env_int("PINNACLE_LEAGUE_IDS_MAX_CHARS", self.league_ids_max_chars)
        max_items = _env_int("PINNACLE_LEAGUE_BATCH_SIZE", self.league_batch_size)

        while max_chars >= 40:
            chunks = _chunk_league_ids_by_len(ids, max_chars=max_chars, max_items=max_items)

            merged: Dict[str, Any] = {}
            merged_events: List[Any] = []
            merged_leagues: List[Any] = []

            ok_chunks = 0
            had_422 = False

            for i, chunk in enumerate(chunks, start=1):
                logger.info(
                    "[pinnacle_cerco_client] /markets chunk %d/%d | sport=%s | leagues=%d | max_chars=%d",
                    i, len(chunks), sport_id, len(chunk), max_chars
                )
                try:
                    payload = self.fetch_markets(
                        sport_id=sport_id,
                        league_ids=chunk,
                        event_type=event_type,
                        is_have_odds=is_have_odds,
                    )
                    ok_chunks += 1
                except httpx.HTTPStatusError as e:
                    status = getattr(e.response, "status_code", None)
                    body = ""
                    try:
                        body = e.response.text  # type: ignore[union-attr]
                    except Exception:
                        pass

                    if status == 422 and "string_too_long" in body:
                        had_422 = True
                        logger.warning(
                            "[pinnacle_cerco_client] 422 string_too_long com max_chars=%d. "
                            "Reduzindo e reprocessando...",
                            max_chars,
                        )
                        break

                    logger.error(
                        "[pinnacle_cerco_client] /markets chunk %d falhou | status=%s | body=%s",
                        i, status, body[:500]
                    )
                    continue

                if not merged:
                    merged = payload or {}
                merged_events.extend(_try_get_list(payload, "events", "Events"))
                merged_leagues.extend(_try_get_list(payload, "leagues", "Leagues"))

            if had_422:
                max_chars = max(40, max_chars // 2)
                continue

            if ok_chunks == 0:
                logger.error("[pinnacle_cerco_client] todos os chunks de /markets falharam.")
                return {}

            merged["events"] = merged_events
            merged["leagues"] = merged_leagues

            logger.info(
                "[pinnacle_cerco_client] /markets merge final: events=%d leagues=%d chunks_ok=%d/%d",
                len(merged_events), len(merged_leagues), ok_chunks, len(chunks)
            )
            return merged

        logger.error("[pinnacle_cerco_client] max_chars caiu demais e ainda houve 422.")
        return {}

    # ------------------------------------------------------------------
    # /special-markets
    # ------------------------------------------------------------------

    def fetch_special_markets(
        self,
        sport_id: int,
        league_ids: Iterable[int | str],
        *,
        event_type: str = "prematch",
        is_have_odds: bool = True,
    ) -> Dict[int, List[Dict[str, Any]]]:
        """Chama `/special-markets` e agrupa o resultado por `event_id`."""
        params: Dict[str, Any] = {
            "league_ids": _normalize_league_ids(league_ids),
            "event_type": event_type,
            "sport_id": str(sport_id),
            "is_have_odds": "true" if is_have_odds else "false",
        }
        raw = self._get_json("/special-markets", params)

        specials = (raw or {}).get("specials") or []
        logger.info("Specials encontrados em /special-markets: %d", len(specials))

        by_event: Dict[int, List[Dict[str, Any]]] = {}
        for sp in specials:
            if not isinstance(sp, dict):
                continue
            try:
                ev_id = int(sp.get("event_id"))
            except Exception:
                continue
            by_event.setdefault(ev_id, []).append(sp)

        logger.info("→ Specials agrupados por event_id: %d eventos com especiais", len(by_event))
        return by_event

    def fetch_special_markets_for_leagues(
        self,
        sport_id: int,
        league_ids: List[int],
        *,
        event_type: str = "prematch",
        is_have_odds: bool = True,
    ) -> Dict[str, Any]:
        """
        Payload bruto do KIT, mas com chunking seguro por tamanho.
        Fallback automático em 422 string_too_long.
        """
        ids = list(league_ids)
        if not ids:
            return {}

        max_chars = _env_int("PINNACLE_LEAGUE_IDS_MAX_CHARS", self.league_ids_max_chars)
        max_items = _env_int("PINNACLE_LEAGUE_BATCH_SIZE", self.league_batch_size)

        while max_chars >= 40:
            chunks = _chunk_league_ids_by_len(ids, max_chars=max_chars, max_items=max_items)

            merged: Dict[str, Any] = {}
            merged_specials: List[Any] = []
            ok_chunks = 0
            had_422 = False

            for i, chunk in enumerate(chunks, start=1):
                logger.info(
                    "[pinnacle_cerco_client] /special-markets chunk %d/%d | sport=%s | leagues=%d | max_chars=%d",
                    i, len(chunks), sport_id, len(chunk), max_chars
                )
                params: Dict[str, Any] = {
                    "league_ids": _normalize_league_ids(chunk),
                    "event_type": event_type,
                    "sport_id": str(sport_id),
                    "is_have_odds": "true" if is_have_odds else "false",
                }

                try:
                    payload = self._get_json("/special-markets", params)
                    ok_chunks += 1
                except httpx.HTTPStatusError as e:
                    status = getattr(e.response, "status_code", None)
                    body = ""
                    try:
                        body = e.response.text  # type: ignore[union-attr]
                    except Exception:
                        pass

                    if status == 422 and "string_too_long" in body:
                        had_422 = True
                        logger.warning(
                            "[pinnacle_cerco_client] 422 string_too_long em /special-markets "
                            "com max_chars=%d. Reduzindo e reprocessando...",
                            max_chars,
                        )
                        break

                    logger.error(
                        "[pinnacle_cerco_client] /special-markets chunk %d falhou | status=%s | body=%s",
                        i, status, body[:500]
                    )
                    continue

                if not merged:
                    merged = payload or {}
                merged_specials.extend(_try_get_list(payload, "specials", "Specials"))

            if had_422:
                max_chars = max(40, max_chars // 2)
                continue

            if ok_chunks == 0:
                logger.error("[pinnacle_cerco_client] todos os chunks de /special-markets falharam.")
                return {}

            merged["specials"] = merged_specials

            logger.info(
                "[pinnacle_cerco_client] /special-markets merge final: specials=%d chunks_ok=%d/%d",
                len(merged_specials), ok_chunks, len(chunks)
            )
            return merged

        logger.error("[pinnacle_cerco_client] max_chars caiu demais e ainda houve 422 em /special-markets.")
        return {}
