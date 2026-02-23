# -*- coding: utf-8 -*-
"""
cerco_engine.py
----------------
Engine principal do novo sistema CERCO.

Responsabilidades:
- Descobrir ligas por esporte (via leagues_cerco).
- Buscar /markets e /special-markets via PinnacleCercoClient.
- Consolidar mercados + specials por evento.
- Filtrar por janela de tempo (agora → agora + N horas).
- Logar um resumo amigável para auditoria.

Cache diário:
- Mantém snapshot diário de "ligas com eventos hoje", recalculado no máximo
  a cada CERCO_TODAY_LEAGUES_REFRESH_HOURS (default 6h) usando como base
  o catálogo grande LEAGUES_BY_SPORT.

Ajustes pós-simplificação (NOV/2025):
- ÚNICA fonte de ligas: LEAGUES_BY_SPORT.
- Removidos perfis core/brazil/all e PINNACLE_ALLOWED_DEFAULT.
- Engine não usa mais get_leagues_or_default nem profile.

Refino 2025-11-22 (alinhado com Normalizer):
- markets_raw armazena o EVENTO inteiro quando payload traz "events"
  (Formato A).
- Formato B encapsula markets em {"markets": [...]}.
- _parse_datetime_utc aceita datetime já-parsed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from dateutil import parser as dt_parser

from .config_cerco import (
    CERCO_SPORTS,
    CERCO_TIME_WINDOW_HOURS,
    CERCO_POLL_EVERY_SEC,
    CERCO_TODAY_LEAGUES_REFRESH_HOURS,
    dump_config_for_logging,
    APP_TZ,
    APP_TZ_NAME,
)
from .pinnacle_cerco_client import PinnacleCercoClient
from .leagues_cerco import (
    get_leagues_for_pinnacle_sport_id,
    get_today_league_batches_for_sport_id,
    load_today_leagues_snapshot,
    save_today_leagues_snapshot,
    chunk_leagues,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Modelo de evento consolidado
# ---------------------------------------------------------------------------

@dataclass
class CercoEventSnapshot:
    sport_id: int
    league_id: int
    league_name: str

    event_id: int
    home: str
    away: str

    starts_at_utc: datetime
    delta_hours_from_now: float

    # Agora guardamos o EVENT inteiro quando disponível (Formato A),
    # e um wrapper {"markets": [...]} no Formato B.
    markets_raw: Any = field(default_factory=dict)

    # Specials (raw) de /special-markets
    specials_raw: List[Dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers internos (parsing e indexação)
# ---------------------------------------------------------------------------

def _parse_datetime_utc(value: Any) -> datetime:
    """Converte ISO string da API (ou datetime) para datetime UTC."""
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    s = str(value)
    dt = dt_parser.isoparse(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt


def _format_start_local(dt_utc: datetime) -> str:
    """Converte datetime UTC para string no APP_TZ (log auditoria)."""
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)
    if APP_TZ is not None:
        dt_local = dt_utc.astimezone(APP_TZ)  # type: ignore[arg-type]
        return f"{dt_local.isoformat()} ({APP_TZ_NAME})"
    return dt_utc.isoformat() + " [UTC]"


def _to_local(dt_utc: datetime) -> datetime:
    """Converte datetime UTC para APP_TZ (fallback: UTC)."""
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)
    if APP_TZ is None:
        return dt_utc
    return dt_utc.astimezone(APP_TZ)  # type: ignore[arg-type]


def _local_today_from(now_utc: datetime) -> datetime.date:
    """Retorna a data de 'hoje' no fuso do app."""
    return _to_local(now_utc).date()


def _is_event_in_local_day(starts_at_utc: datetime, local_day: datetime.date) -> bool:
    """True se o evento cai no local_day (APP_TZ)."""
    return _to_local(starts_at_utc).date() == local_day


def _build_events_index_from_markets(
    payload: Dict[str, Any],
    *,
    now_utc: datetime,
    default_sport_id: Optional[int] = None,
) -> Dict[int, CercoEventSnapshot]:
    """
    Lê o JSON de /markets e monta um índice {event_id -> CercoEventSnapshot}
    com mercados do evento.

    - Formato A: payload["events"] lista plana de eventos.
      -> markets_raw guarda o EVENTO inteiro.
    - Formato B: payload["data"] blocos por liga.
      -> markets_raw guarda {"markets": [...]}.  

    default_sport_id:
        Sport id do loop chamador (CERCO_SPORTS). Usado como fallback
        caso o payload venha sem sport_id no evento.
    """
    events_by_id: Dict[int, CercoEventSnapshot] = {}

    # ---------------- Formato A ----------------
    events = payload.get("events")
    if isinstance(events, list) and events:
        logger.debug("Eventos encontrados em /markets (events): %d", len(events))
        for ev in events:
            if not isinstance(ev, dict):
                continue

            ev_id_raw = ev.get("event_id") or ev.get("id")
            if ev_id_raw is None:
                continue
            try:
                event_id = int(ev_id_raw)
            except Exception:
                continue

            league_id = int(
                ev.get("league_id")
                or (ev.get("league") or {}).get("id", 0)
                or 0
            )
            league_name = (
                ev.get("league_name")
                or (ev.get("league") or {}).get("name")
                or "UNKNOWN_LEAGUE"
            )

            home = (
                ev.get("home")
                or ev.get("home_name")
                or (ev.get("teams") or {}).get("home")
                or "HOME?"
            )
            away = (
                ev.get("away")
                or ev.get("away_name")
                or (ev.get("teams") or {}).get("away")
                or "AWAY?"
            )

            starts_raw = (
                ev.get("starts")
                or ev.get("starts_at")
                or ev.get("start_time")
                or ev.get("start")
            )
            if not starts_raw:
                continue

            starts_at_utc = _parse_datetime_utc(starts_raw)
            delta_h = (starts_at_utc - now_utc).total_seconds() / 3600.0

            sport_id_raw = ev.get("sport_id")
            try:
                sport_id = int(sport_id_raw) if sport_id_raw is not None else 0
            except Exception:
                sport_id = 0
            if sport_id == 0 and default_sport_id is not None:
                sport_id = int(default_sport_id)

            events_by_id[event_id] = CercoEventSnapshot(
                sport_id=sport_id,
                league_id=league_id,
                league_name=league_name,
                event_id=event_id,
                home=home,
                away=away,
                starts_at_utc=starts_at_utc,
                delta_hours_from_now=delta_h,
                markets_raw=ev,
                specials_raw=[],
            )

        return events_by_id

    # ---------------- Formato B (fallback) ----------------
    data = payload.get("data") or []
    logger.debug("Eventos encontrados em /markets (data blocks): %d", len(data))

    for item in data:
        if not isinstance(item, dict):
            continue

        league = item.get("league") or {}
        league_id = int(league.get("id", 0) or 0)
        league_name = league.get("name") or "UNKNOWN_LEAGUE"

        sport_id_item = item.get("sport_id")
        try:
            sport_id = int(sport_id_item) if sport_id_item is not None else 0
        except Exception:
            sport_id = 0
        if sport_id == 0 and default_sport_id is not None:
            sport_id = int(default_sport_id)

        events = item.get("events") or []
        markets = item.get("markets") or []

        for ev in events:
            if not isinstance(ev, dict):
                continue

            ev_id_raw = ev.get("id")
            if ev_id_raw is None:
                continue
            try:
                event_id = int(ev_id_raw)
            except Exception:
                continue

            home = ev.get("home") or ""
            away = ev.get("away") or ""

            starts_raw = ev.get("starts_at") or ev.get("start_time")
            if not starts_raw:
                continue

            starts_at_utc = _parse_datetime_utc(starts_raw)
            delta_h = (starts_at_utc - now_utc).total_seconds() / 3600.0

            if event_id not in events_by_id:
                events_by_id[event_id] = CercoEventSnapshot(
                    sport_id=sport_id,
                    league_id=league_id,
                    league_name=league_name,
                    event_id=event_id,
                    home=home,
                    away=away,
                    starts_at_utc=starts_at_utc,
                    delta_hours_from_now=delta_h,
                    markets_raw={},
                    specials_raw=[],
                )

            if markets:
                mr = events_by_id[event_id].markets_raw
                if not isinstance(mr, dict):
                    mr = {}
                prev = mr.get("markets")
                if isinstance(prev, list):
                    prev.extend([m for m in markets if isinstance(m, dict)])
                    mr["markets"] = prev
                else:
                    mr["markets"] = [m for m in markets if isinstance(m, dict)]
                events_by_id[event_id].markets_raw = mr

    return events_by_id


def _build_specials_index(payload: Dict[str, Any]) -> Dict[int, List[Dict[str, Any]]]:
    """
    Lê o JSON de /special-markets e monta:
      { event_id -> [special_market_dict, ...] }
    """
    specials = payload.get("specials") or []
    logger.debug("Specials encontrados em /special-markets: %d", len(specials))

    by_event: Dict[int, List[Dict[str, Any]]] = {}
    for sp in specials:
        if not isinstance(sp, dict):
            continue

        ev_id_raw = sp.get("event_id")
        if ev_id_raw is None:
            ev = sp.get("event") or {}
            ev_id_raw = ev.get("id")

        if ev_id_raw is None:
            continue

        try:
            event_id = int(ev_id_raw)
        except Exception:
            continue

        by_event.setdefault(event_id, []).append(sp)

    logger.debug("→ Specials agrupados por event_id: %d eventos com especiais", len(by_event))
    return by_event


# ---------------------------------------------------------------------------
# Helpers para cache de ligas do dia
# ---------------------------------------------------------------------------

def _should_refresh_today_leagues(now_utc: datetime) -> bool:
    snap = load_today_leagues_snapshot()
    if not snap:
        return True

    today_local_str = _local_today_from(now_utc).isoformat()

    date_str = snap.get("date")
    if date_str != today_local_str:
        return True

    ref_raw = snap.get("refreshed_at")
    if not ref_raw:
        return True

    try:
        ref_dt = _parse_datetime_utc(str(ref_raw))
    except Exception:
        return True

    delta_h = (now_utc - ref_dt).total_seconds() / 3600.0
    return delta_h >= CERCO_TODAY_LEAGUES_REFRESH_HOURS


def _rebuild_today_leagues_snapshot(client: PinnacleCercoClient, now_utc: datetime) -> None:
    today_local = _local_today_from(now_utc)
    today_local_str = today_local.isoformat()
    sports_map: Dict[str, List[int]] = {}

    logger.info(
        "[today_leagues] recalculando snapshot para %s (%s) (refresh=%.1fh)...",
        today_local_str,
        APP_TZ_NAME,
        CERCO_TODAY_LEAGUES_REFRESH_HOURS,
    )

    for sport_id, meta in CERCO_SPORTS.items():
        name = meta.get("name", f"sport_{sport_id}")

        # >>> ÚNICA fonte de ligas agora <<<
        base_leagues = get_leagues_for_pinnacle_sport_id(sport_id)

        # base_leagues pode ser None (todas) ou [] (desconhecido)
        if not base_leagues:
            logger.info("[today_leagues] sport_id=%s (%s) sem ligas base; pulando.", sport_id, name)
            continue
        if base_leagues is None:
            # Contrato mantém None="todas", mas seu catálogo usa listas.
            # Evita rebuild caro/indeterminado se algum esporte vier None.
            logger.warning(
                "[today_leagues] sport_id=%s (%s) retornou None (todas). "
                "Snapshot diário não será recalculado para esse esporte.",
                sport_id, name
            )
            continue

        base_leagues_int = [int(x) for x in base_leagues]
        batches = chunk_leagues(base_leagues_int, 120)
        active_leagues: set[int] = set()

        for batch_idx, league_ids in enumerate(batches, start=1):
            logger.debug(
                "[today_leagues] sport_id=%s (%s) batch %d/%d | ligas=%s",
                sport_id, name, batch_idx, len(batches), league_ids,
            )

            markets_payload = client.fetch_markets_for_leagues(
                sport_id=sport_id,
                league_ids=league_ids,
                event_type="prematch",
                is_have_odds=True,
            )
            events_by_id = _build_events_index_from_markets(
                markets_payload,
                now_utc=now_utc,
                default_sport_id=sport_id,
            )

            if not events_by_id:
                continue

            for snap in events_by_id.values():
                if _is_event_in_local_day(snap.starts_at_utc, today_local):
                    active_leagues.add(snap.league_id)

        if active_leagues:
            leagues_sorted = sorted(active_leagues)
            sports_map[str(sport_id)] = leagues_sorted
            logger.info(
                "[today_leagues] sport_id=%s (%s): %d ligas ativas hoje (de %d base).",
                sport_id, name, len(leagues_sorted), len(base_leagues_int),
            )
        else:
            logger.info("[today_leagues] sport_id=%s (%s): nenhuma liga com eventos hoje.", sport_id, name)

    snapshot = {
        "date": today_local_str,      # data local
        "refreshed_at": now_utc.isoformat(),
        "sports": sports_map,
        "tz": APP_TZ_NAME,
    }
    save_today_leagues_snapshot(snapshot)


# ---------------------------------------------------------------------------
# Engine principal
# ---------------------------------------------------------------------------

class CercoEngine:
    """Motor que consolida eventos+mercados da Pinnacle para o CERCO."""

    def __init__(self) -> None:
        self._client = PinnacleCercoClient()

    def close(self) -> None:
        self._client.close()

    def run_once(self) -> List[CercoEventSnapshot]:
        """Executa UM ciclo de coleta e retorna snapshots dentro da janela."""
        now_utc = datetime.now(timezone.utc)
        now_local = _to_local(now_utc)
        today_local = now_local.date()

        logger.info(
            "CERCO Engine | now_utc=%s | now_local=%s (%s) | janela=+%.1fh",
            now_utc.isoformat(),
            now_local.isoformat(),
            APP_TZ_NAME,
            CERCO_TIME_WINDOW_HOURS,
        )

        # 1) Snapshot de ligas do dia
        if _should_refresh_today_leagues(now_utc):
            _rebuild_today_leagues_snapshot(self._client, now_utc)
        else:
            snap = load_today_leagues_snapshot()
            sports_map = snap.get("sports") or {}
            total_sports = len(sports_map)
            total_leagues = sum(len(v or []) for v in sports_map.values())
            logger.info(
                "[today_leagues] cache | date=%s | tz=%s | esportes=%d | ligas=%d",
                snap.get("date"),
                snap.get("tz") or APP_TZ_NAME,
                total_sports,
                total_leagues,
            )

        all_snapshots: List[CercoEventSnapshot] = []

        # 2) Coleta eventos somente nas ligas "do dia"
        for sport_id, meta in CERCO_SPORTS.items():
            name = meta.get("name", f"sport_{sport_id}")

            batches = get_today_league_batches_for_sport_id(
                sport_id,
                batch_size=120,
                today_only=True,
            )

            if not batches:
                logger.debug("sport_id=%s (%s) sem batches de ligas hoje.", sport_id, name)
                continue

            sport_total = 0
            sport_in_window = 0
            sport_today_local = 0

            for batch_idx, league_ids in enumerate(batches, start=1):
                logger.debug(
                    "Batch %d/%d | sport_id=%s (%s) | leagues=%s",
                    batch_idx, len(batches), sport_id, name, league_ids,
                )

                markets_payload = self._client.fetch_markets_for_leagues(
                    sport_id=sport_id,
                    league_ids=league_ids,
                    event_type="prematch",
                    is_have_odds=True,
                )
                events_by_id = _build_events_index_from_markets(
                    markets_payload,
                    now_utc=now_utc,
                    default_sport_id=sport_id,
                )

                if not events_by_id:
                    continue

                specials_payload = self._client.fetch_special_markets_for_leagues(
                    sport_id=sport_id,
                    league_ids=league_ids,
                    event_type="prematch",
                    is_have_odds=True,
                )
                specials_by_event = _build_specials_index(specials_payload)

                for ev_id, snap in events_by_id.items():
                    sport_total += 1

                    # "hoje" local
                    if not _is_event_in_local_day(snap.starts_at_utc, today_local):
                        continue
                    sport_today_local += 1

                    specials = specials_by_event.get(ev_id, [])
                    if specials:
                        snap.specials_raw.extend(specials)

                    # janela pré-jogo baseada no delta
                    in_window = (0.0 <= snap.delta_hours_from_now <= CERCO_TIME_WINDOW_HOURS)
                    if not in_window:
                        continue

                    sport_in_window += 1
                    all_snapshots.append(snap)

                    logger.info(
                        "[WINDOW] %s | %s x %s | start=%s | Δh=%.2f | specials=%d",
                        snap.league_name,
                        snap.home,
                        snap.away,
                        _format_start_local(snap.starts_at_utc),
                        snap.delta_hours_from_now,
                        len(snap.specials_raw),
                    )

            logger.info(
                "sport_id=%s (%s) | eventos=%d | hoje_local=%d | na_janela=%d",
                sport_id, name, sport_total, sport_today_local, sport_in_window,
            )

        logger.info("CERCO Engine | total na janela (todos esportes): %d", len(all_snapshots))
        return all_snapshots


# ---------------------------------------------------------------------------
# Entrypoints
# ---------------------------------------------------------------------------

def main_once() -> None:
    """Executa apenas um ciclo e encerra (útil pra debug local)."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    logger.info("Boot CERCO Engine (run_once) | config=%s", dump_config_for_logging())
    engine = CercoEngine()
    try:
        engine.run_once()
    finally:
        engine.close()


def main_loop() -> None:
    """Loop simples com sleep (entrypoint Railway)."""
    import time
    import random

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    logger.info("Boot CERCO Engine (loop) | config=%s", dump_config_for_logging())

    engine = CercoEngine()
    try:
        while True:
            try:
                engine.run_once()
            except Exception as e:
                logger.exception("Falha em run_once: %s", e)

            base = CERCO_POLL_EVERY_SEC
            jitter_factor = random.uniform(0.9, 1.1)
            sleep_sec = int(base * jitter_factor)
            logger.info(
                "Sleep %ds (base=%ds, jitter=%.2f) até o próximo ciclo CERCO...",
                sleep_sec, base, jitter_factor,
            )
            time.sleep(sleep_sec)
    finally:
        engine.close()


if __name__ == "__main__":
    main_once()
    # main_loop()
