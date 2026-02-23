# -*- coding: utf-8 -*-
"""
cerco_orchestrator.py
---------------------
Orquestrador "real" do subsistema CERCO.

Ele conecta:
- CercoEngine (Pinnacle → snapshots de eventos)
- EsporteNetBridgeCerco (scraping da EsporteNet)
- cerco_comparator (livros canônicos → oportunidades de arbitragem)

AJUSTES NESTA AUDITORIA:
- SnapshotCache EsporteNet (TTL) para não raspar o dia inteiro a cada ciclo.
- MatchCache Pinnacle↔EsporteNet para evitar rematch dentro da janela.
- Filtro por liga: só tenta casar eventos Pinnacle cujas ligas existam no snapshot EsporteNet.
- Fallback seguro: se bridge não expuser método de candidatos, usa plano B via list_leagues_for_sport/_fetch_day_candidates.
- Logs enxutos:
    * INFO só para:
        - número de snapshots na janela
        - cada evento processado (liga + confronto + start local + Δh)
        - encontrado/não encontrado no EsporteNet
        - total de oportunidades
    * DEBUG para detalhes internos de probe/legs.

NOVO (2025-11-23):
- Mapa de esportes Pinnacle → EsporteNet estendido (soccer + outros).
- Integração leve com esnet_league_map.json:
    * Se existir entrada Pinnacle league_id → EsporteNet idcampeonato,
      passamos allowed_idcampeonatos no meta para reduzir busca do bridge.
    * O JSON atual só tem soccer, mas já deixamos genérico para multi-esporte.

NOVO (2025-11-24, plano multi-sport + normalizer v2):
- Garante sport_id nos snapshots/targets para o normalizer aplicar:
    * filter.pinnacle_sport_ids / filter.esnet_sport_ids
    * matching por market_group em specials
- Não altera assinatura de comparator/bridge.

NOVO (aliases + lógica de ligas/paises):
- Usa aliases_cerco.build_aliases_from_league_map para:
    * country_alias (pinnacle country → esnet country)
    * league_alias  (pinnacle league  → esnet league)
- Transforma league_name da Pinnacle em algo próximo ao padrão EsporteNet,
  garantindo que league_norm bata com as chaves do EsnetSnapshotCache.
- Para SOCCER (sport_id=1 na Pinnacle), não força idcampeonato via league_map,
  apenas usa os aliases de país/liga + nomes de times.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Iterable

import os
import json
import logging
from datetime import datetime, timezone, timedelta

from .cerco_engine import CercoEngine, CercoEventSnapshot  # type: ignore
from .esportenet_bridge_cerco import EsporteNetBridgeCerco
from .cerco_comparator import (
    build_books_and_find_arbs,
    ArbitrageOpportunity,
)
from .config_cerco import APP_TZ, APP_TZ_NAME, CERCO_MIN_EDGE

from .aliases_cerco import (
    build_aliases_from_league_map,
    LeagueCountryAliases,
    norm_simple as aliases_norm_simple,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tipos de resultado para uso posterior (Telegram / DB / Dashboard)
# ---------------------------------------------------------------------------

@dataclass
class CercoMatchResult:
    """Resultado completo da comparação para um evento específico."""
    snapshot: CercoEventSnapshot
    esnet_found: bool
    esnet_meta: Dict[str, Any] = field(default_factory=dict)
    esnet_url: Optional[str] = None
    opportunities: List[ArbitrageOpportunity] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers ENV / Config
# ---------------------------------------------------------------------------

def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw.replace(",", "."))
    except Exception:
        return default


# ---------------------------------------------------------------------------
# Normalização simples (reaproveita aliases_cerco)
# ---------------------------------------------------------------------------

def _norm_simple(s: str) -> str:
    """
    Wrapper para usar a MESMA normalização (norm_simple) do aliases_cerco,
    garantindo que league_norm/time_norm sejam compatíveis entre:
      - CercoOrchestrator
      - EsnetSnapshotCache
      - aliases_cerco.LeagueCountryAliases
    """
    return aliases_norm_simple(s)


def _format_start_local(dt_utc: datetime) -> str:
    """Converte um datetime UTC para string no APP_TZ (para logging)."""
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)
    if APP_TZ is not None:
        dt_local = dt_utc.astimezone(APP_TZ)  # type: ignore[arg-type]
        return f"{dt_local.isoformat()} ({APP_TZ_NAME})"
    return dt_utc.isoformat() + " [UTC]"


def _league_norm_from_snapshot(snap: CercoEventSnapshot) -> str:
    """
    Gera um league_norm básico a partir do nome da liga do snapshot Pinnacle.
    Se no futuro o snapshot já trouxer snap.league_norm, preferimos ele.
    """
    ln = getattr(snap, "league_norm", None)
    if isinstance(ln, str) and ln.strip():
        return _norm_simple(ln)
    return _norm_simple(getattr(snap, "league_name", "") or getattr(snap, "league", ""))


def _ensure_dict_sport_id(markets_raw: Any, sport_id: int) -> None:
    """
    Garante sport_id dentro de dicts Pinnacle para que o normalizer v2
    consiga filtrar corretamente quando o shape não traz sport.
    """
    try:
        if isinstance(markets_raw, dict):
            if markets_raw.get("sport_id") is None and markets_raw.get("sportId") is None:
                markets_raw["sport_id"] = int(sport_id)
        # alguns snapshots embutem {"event": {...}}
        if isinstance(markets_raw, dict) and isinstance(markets_raw.get("event"), dict):
            ev = markets_raw["event"]
            if ev.get("sport_id") is None and ev.get("sportId") is None:
                ev["sport_id"] = int(sport_id)
    except Exception:
        pass


def _attach_esnet_sport_id(probe: Any, sport_id_esnet: int) -> None:
    """
    Tenta anexar sport_id no probe EsporteNet para o normalizer v2 aplicar filter.esnet_sport_ids.
    Não é fatal se o objeto for imutável.
    """
    if probe is None:
        return
    try:
        if getattr(probe, "sport_id", None) is None:
            setattr(probe, "sport_id", int(sport_id_esnet))
    except Exception:
        pass
    try:
        meta = getattr(probe, "meta", None)
        if isinstance(meta, dict) and meta.get("sport_id") is None:
            meta["sport_id"] = int(sport_id_esnet)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Mapa de esportes Pinnacle → EsporteNet
# (corrigido Soccer=102)
# ---------------------------------------------------------------------------

PINNACLE_TO_ESNET_SPORT: Dict[int, int] = {
    1: 102,   # Soccer / Futebol
    2: 8,     # Tennis
    3: 190,   # Basketball
    5: 177,   # Volleyball
    7: 10,    # American Football
}

def _map_pinnacle_sport_to_esnet(sport_id: int) -> Optional[int]:
    """Mapa direto Pinnacle sport_id → EsporteNet idesporte."""
    try:
        return PINNACLE_TO_ESNET_SPORT.get(int(sport_id))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# LEAGUE MAP EsporteNet (opcional, leve)
# ---------------------------------------------------------------------------

def _load_esnet_league_map(path: Optional[str]) -> Dict[str, Any]:
    if not path:
        return {}
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f) or {}
    except Exception:
        logger.exception("[league_map] Falha ao carregar esnet_league_map.json")
    return {}


def _build_pinn_to_esnet_league_index(raw_map: Dict[str, Any]) -> Dict[Tuple[int, int], int]:
    """
    Constrói índice:
        (pinnacle_sport_id, pinnacle_league_id) -> esnet_idcampeonato

    Regras:
    - Se a entrada tiver sport_id_pinnacle/sport_id_esnet, usa.
    - Se não tiver, assume que é Soccer (1->102).

    OBS IMPORTANTE:
    - Vamos usar esse índice apenas para esportes que NÃO são futebol (soccer),
      já que na EsporteNet de futebol trabalhamos com a página de jogos do dia,
      e idcampeonato ali representa mais o "dia" do que um campeonato fixo.
    """
    out: Dict[Tuple[int, int], int] = {}

    # Suporta tanto formato antigo quanto formato novo (multi-esporte).
    leagues_top = raw_map.get("leagues")
    sports = raw_map.get("sports")

    # Formato antigo: leagues no topo
    if isinstance(leagues_top, list):
        leagues_iter = leagues_top
    else:
        leagues_iter = []

    # Formato novo: dentro de sports.<sport>.leagues
    if isinstance(sports, dict):
        for _sport_key, spec in sports.items():
            if not isinstance(spec, dict):
                continue
            leagues = spec.get("leagues") or []
            if isinstance(leagues, list):
                leagues_iter.extend(leagues)

    for it in leagues_iter:
        if not isinstance(it, dict):
            continue
        try:
            pinn_league_id = int(it.get("pinnacle_id") or 0)
            esnet_idc = int(it.get("esnet_id") or 0)
            if pinn_league_id <= 0 or esnet_idc <= 0:
                continue

            pinn_sport_id = it.get("sport_id_pinnacle")
            esnet_sport_id = it.get("sport_id_esnet")

            if pinn_sport_id is not None:
                try:
                    pinn_sport_id = int(pinn_sport_id)
                except Exception:
                    pinn_sport_id = None

            if esnet_sport_id is not None:
                try:
                    esnet_sport_id = int(esnet_sport_id)
                except Exception:
                    esnet_sport_id = None

            # fallback: assume soccer se nada vier preenchido
            if pinn_sport_id is None and esnet_sport_id is None:
                pinn_sport_id = 1  # soccer na Pinnacle

            if pinn_sport_id is None:
                continue

            out[(int(pinn_sport_id), pinn_league_id)] = esnet_idc
        except Exception:
            continue

    return out


# ---------------------------------------------------------------------------
# SnapshotCache EsporteNet (descoberta lenta) - POR ESPORTE
# ---------------------------------------------------------------------------

@dataclass
class EsnetDayEvent:
    """Representação mínima do evento EsporteNet para match."""
    pid: int
    idcampeonato: Optional[int]
    sport_id: int
    league_norm: str
    home_norm: str
    away_norm: str
    start_utc: Optional[datetime]
    raw: Dict[str, Any] = field(default_factory=dict)


class EsnetSnapshotCache:
    """
    Cache com TTL do catálogo do dia da EsporteNet.

    IMPORTANTE: agora é por esporte (multi-sport safe).

    - Atualiza a cada X segundos (default 10 min) POR sport_id_esnet.
    - Produz index por (sport_id, league_norm) → lista de EsnetDayEvent.
    """

    def __init__(self, bridge: EsporteNetBridgeCerco, ttl_sec: int = 600) -> None:
        self.bridge = bridge
        self.ttl_sec = ttl_sec

        # estado por esporte
        # sport_id_esnet -> {"last": dt, "events": [...], "index": {...}}
        self._state: Dict[int, Dict[str, Any]] = {}

    def _get_state(self, sport_id_esnet: int) -> Dict[str, Any]:
        st = self._state.get(sport_id_esnet)
        if st is None:
            st = {"last": None, "events": [], "index": {}}
            self._state[sport_id_esnet] = st
        return st

    def is_stale(self, sport_id_esnet: int) -> bool:
        st = self._get_state(sport_id_esnet)
        last: Optional[datetime] = st.get("last")
        if last is None:
            return True
        return (datetime.now(timezone.utc) - last).total_seconds() > self.ttl_sec

    def refresh(self, *, sport_id_esnet: int) -> None:
        now = datetime.now(timezone.utc)
        events: List[EsnetDayEvent] = []
        index: Dict[Tuple[int, str], List[EsnetDayEvent]] = {}

        candidates_raw: List[Dict[str, Any]] = []

        # 1) API pública "bonita", se existir
        try:
            if hasattr(self.bridge, "get_candidates_for_day"):
                cr = self.bridge.get_candidates_for_day(sport_id=sport_id_esnet)  # type: ignore
                candidates_raw = list(cr or [])
            elif hasattr(self.bridge, "get_candidates"):
                cr = self.bridge.get_candidates(sport_id=sport_id_esnet)  # type: ignore
                candidates_raw = list(cr or [])
        except Exception:
            logger.exception("[esnet_snapshot] Falha ao atualizar candidatos do dia via bridge (public).")
            candidates_raw = []

        # 2) Plano B: usa list_leagues_for_sport + _fetch_day_candidates
        if not candidates_raw:
            try:
                if hasattr(self.bridge, "list_leagues_for_sport") and hasattr(self.bridge, "_fetch_day_candidates"):
                    leagues = self.bridge.list_leagues_for_sport(int(sport_id_esnet))  # type: ignore
                    for idc in leagues:
                        try:
                            cr = self.bridge._fetch_day_candidates(int(sport_id_esnet), int(idc))  # type: ignore
                            if cr:
                                candidates_raw.extend(list(cr))
                        except Exception:
                            logger.debug("[esnet_snapshot] Falha ao buscar candidatos idc=%s", idc)
            except Exception:
                logger.exception("[esnet_snapshot] Falha no plano B de snapshot.")
                candidates_raw = []

        if not candidates_raw:
            st = self._get_state(sport_id_esnet)
            st["events"] = []
            st["index"] = {}
            st["last"] = now
            logger.debug("[esnet_snapshot] Bridge sem candidatos disponíveis; mantendo cache vazio.")
            return

        for c in candidates_raw:
            try:
                pid = int(c.get("pid") or c.get("idpartida") or 0)
                if pid <= 0:
                    continue

                idc = c.get("idcampeonato")
                try:
                    idc_int = int(idc) if idc is not None else None
                except Exception:
                    idc_int = None

                league_norm = _norm_simple(
                    c.get("league_norm")
                    or c.get("liga_norm")
                    or c.get("league_name")
                    or c.get("league")
                    or c.get("liga")
                    or ""
                )

                home_norm = _norm_simple(c.get("home_norm") or c.get("home") or c.get("time_casa") or "")
                away_norm = _norm_simple(c.get("away_norm") or c.get("away") or c.get("time_fora") or "")

                start_utc = c.get("start_utc") or c.get("start")
                if isinstance(start_utc, str):
                    start_utc = None

                ev = EsnetDayEvent(
                    pid=pid,
                    idcampeonato=idc_int,
                    sport_id=sport_id_esnet,
                    league_norm=league_norm,
                    home_norm=home_norm,
                    away_norm=away_norm,
                    start_utc=start_utc if isinstance(start_utc, datetime) else None,
                    raw=dict(c),
                )

                events.append(ev)
                key = (sport_id_esnet, league_norm)
                index.setdefault(key, []).append(ev)

            except Exception:
                logger.debug("[esnet_snapshot] candidato inválido ignorado: %s", c)

        st = self._get_state(sport_id_esnet)
        st["events"] = events
        st["index"] = index
        st["last"] = now

        logger.info(
            "[esnet_snapshot] atualizado | sport=%s | eventos=%d | ligas=%d | ttl=%ss",
            sport_id_esnet,
            len(events),
            len(index),
            self.ttl_sec,
        )

    def get_candidates(self, sport_id_esnet: int, league_norm: str) -> List[Dict[str, Any]]:
        st = self._get_state(sport_id_esnet)
        idx: Dict[Tuple[int, str], List[EsnetDayEvent]] = st.get("index") or {}
        key = (sport_id_esnet, league_norm)
        evs = idx.get(key, [])
        return [e.raw for e in evs]

    def all_leagues_for_sport(self, sport_id_esnet: int) -> List[str]:
        st = self._get_state(sport_id_esnet)
        idx: Dict[Tuple[int, str], List[EsnetDayEvent]] = st.get("index") or {}
        return sorted({ln for (sid, ln) in idx.keys() if sid == sport_id_esnet})

    def has_league(self, sport_id_esnet: int, league_norm: str) -> bool:
        st = self._get_state(sport_id_esnet)
        idx: Dict[Tuple[int, str], List[EsnetDayEvent]] = st.get("index") or {}
        return (sport_id_esnet, league_norm) in idx


# ---------------------------------------------------------------------------
# MatchCache Pinnacle↔EsporteNet
# ---------------------------------------------------------------------------

@dataclass
class MatchRecord:
    pinn_event_id: int
    esnet_pid: int
    sport_id_pinnacle: int
    sport_id_esnet: int
    league_norm: str
    home_norm: str
    away_norm: str
    start_utc: datetime
    matched_at_utc: datetime
    expires_at_utc: datetime
    esnet_meta: Dict[str, Any] = field(default_factory=dict)
    esnet_url: Optional[str] = None

    def is_expired(self) -> bool:
        return datetime.now(timezone.utc) >= self.expires_at_utc


class MatchCache:
    """
    Cache de pareamentos:
    - por event_id Pinnacle (chave primária)
    - set de pids já usados na EsporteNet.
    """

    def __init__(self, grace_minutes_after_start: int = 30) -> None:
        self.grace_minutes_after_start = grace_minutes_after_start
        self.by_pinn: Dict[int, MatchRecord] = {}
        self.used_esnet_pids: set[int] = set()

    def get(self, pinn_event_id: int) -> Optional[MatchRecord]:
        rec = self.by_pinn.get(pinn_event_id)
        if rec and rec.is_expired():
            self.drop(pinn_event_id)
            return None
        return rec

    def has_valid(self, pinn_event_id: int) -> bool:
        return self.get(pinn_event_id) is not None

    def put_from_probe(
        self,
        snap: CercoEventSnapshot,
        probe: Any,
        *,
        league_norm: str,
        sport_id_esnet: int,
    ) -> MatchRecord:
        pinn_event_id = int(getattr(snap, "event_id", 0))
        esnet_pid = int(getattr(probe, "pid", 0) or getattr(probe, "idpartida", 0) or 0)

        now = datetime.now(timezone.utc)
        start_utc = snap.starts_at_utc
        if start_utc.tzinfo is None:
            start_utc = start_utc.replace(tzinfo=timezone.utc)

        expires = start_utc + timedelta(minutes=self.grace_minutes_after_start)

        rec = MatchRecord(
            pinn_event_id=pinn_event_id,
            esnet_pid=esnet_pid,
            sport_id_pinnacle=int(getattr(snap, "sport_id", 0)),
            sport_id_esnet=sport_id_esnet,
            league_norm=league_norm,
            home_norm=_norm_simple(getattr(probe, "home", getattr(snap, "home", ""))),
            away_norm=_norm_simple(getattr(probe, "away", getattr(snap, "away", ""))),
            start_utc=start_utc,
            matched_at_utc=now,
            expires_at_utc=expires,
            esnet_meta=getattr(probe, "meta", {}) or getattr(probe, "debug", {}) or {},
            esnet_url=getattr(probe, "event_url", None),
        )

        self.by_pinn[pinn_event_id] = rec
        if esnet_pid > 0:
            self.used_esnet_pids.add(esnet_pid)

        return rec

    def drop(self, pinn_event_id: int) -> None:
        rec = self.by_pinn.pop(pinn_event_id, None)
        if rec and rec.esnet_pid in self.used_esnet_pids:
            self.used_esnet_pids.discard(rec.esnet_pid)


# ---------------------------------------------------------------------------
# Orquestrador
# ---------------------------------------------------------------------------

class CercoOrchestrator:
    """
    Orquestrador de alto nível do CERCO.

    Responsável por:
    - Rodar o CercoEngine (Pinnacle → snapshots).
    - Manter SnapshotCache do dia EsporteNet (TTL por esporte).
    - Manter cache de matches (até start+grace).
    - Para cada snapshot:
        - Resolver EsporteNet via probe() snapshot/candidatos filtrados por liga.
        - Normalizar livros canônicos.
        - Rodar o comparador para encontrar cercos.
    """

    def __init__(self, *, min_edge: float = CERCO_MIN_EDGE) -> None:
        self.engine = CercoEngine()
        self.esnet_bridge = EsporteNetBridgeCerco()
        self.min_edge = min_edge

        ttl_sec = _env_int("CERCO_ESNET_SNAPSHOT_TTL_SEC", 600)  # 10 min default
        self.esnet_snapshot = EsnetSnapshotCache(self.esnet_bridge, ttl_sec=ttl_sec)

        grace_min = _env_int("CERCO_MATCH_GRACE_MIN", 30)
        self.match_cache = MatchCache(grace_minutes_after_start=grace_min)

        # league-map opcional (reduz custo de busca; não usado para soccer)
        league_map_path = getattr(self.esnet_bridge, "league_map_path", None) or os.getenv("ESNET_LEAGUE_JSON")
        raw_map = _load_esnet_league_map(league_map_path)
        self._pinn_to_esnet_league = _build_pinn_to_esnet_league_index(raw_map)

        if self._pinn_to_esnet_league:
            logger.info("[league_map] índice carregado | entradas=%d", len(self._pinn_to_esnet_league))
        else:
            logger.info("[league_map] sem índice carregado (ok).")

        # Aliases país/ligas (Pinnacle -> EsporteNet)
        self._aliases: Optional[LeagueCountryAliases] = None
        if raw_map:
            try:
                self._aliases = build_aliases_from_league_map(raw_map)
            except Exception:
                logger.exception("[aliases] falha ao construir aliases; seguindo sem eles.")
                self._aliases = None

    def close(self) -> None:
        try:
            self.engine.close()
        except Exception:
            logger.exception("Falha ao fechar CercoEngine")

    def _ensure_esnet_snapshot(self, sport_id_esnet: int) -> None:
        if self.esnet_snapshot.is_stale(sport_id_esnet):
            try:
                self.esnet_snapshot.refresh(sport_id_esnet=sport_id_esnet)
            except Exception:
                logger.exception("[orchestrator] Falha ao refresh snapshot EsporteNet.")

    def run_once(self) -> List[CercoMatchResult]:
        """
        Executa UM ciclo completo de comparação Pinnacle ↔ EsporteNet.
        """
        snapshots = self.engine.run_once()
        results: List[CercoMatchResult] = []

        logger.info("CERCO Orchestrator | snapshots na janela: %d", len(snapshots))

        for snap in snapshots:
            esnet_sport_id = _map_pinnacle_sport_to_esnet(snap.sport_id)
            if esnet_sport_id is None:
                logger.debug(
                    "Ignorando sem mapeamento EsporteNet | sport_id=%s | %s x %s",
                    getattr(snap, "sport_id", "?"),
                    getattr(snap, "home", "?"),
                    getattr(snap, "away", "?"),
                )
                continue

            # league_norm bruto (só normalização)
            league_norm_raw = _league_norm_from_snapshot(snap)

            # Se tivermos aliases, tentamos aproximar o league_name da Pinnacle
            # ao padrão EsporteNet ("brasil - serie a", "alemanha - bundesliga", etc.)
            if self._aliases is not None:
                try:
                    raw_league_name = getattr(snap, "league_name", "") or getattr(snap, "league", "")
                    aliased = self._aliases.transform_pinnacle_league(raw_league_name)
                    league_norm = aliased or league_norm_raw
                except Exception:
                    logger.debug("[aliases] falha ao transformar league_name; usando league_norm_raw.")
                    league_norm = league_norm_raw
            else:
                league_norm = league_norm_raw

            logger.info(
                "[RUN] %s | %s x %s | start=%s | Δh=%.2f | event_id=%s | sport=%s→%s",
                snap.league_name,
                snap.home,
                snap.away,
                _format_start_local(snap.starts_at_utc),
                snap.delta_hours_from_now,
                snap.event_id,
                snap.sport_id,
                esnet_sport_id,
            )

            pinn_event_id = int(getattr(snap, "event_id", 0) or 0)

            # Garante sport_id no dict bruto Pinnacle, se o shape vier sem isso
            _ensure_dict_sport_id(getattr(snap, "markets_raw", None), int(snap.sport_id))

            # allowed idc via league_map (se existir) — EXCETO para SOCCER
            allowed_idc: List[int] = []
            try:
                sport_id_pinn = int(snap.sport_id)
                if sport_id_pinn != 1:  # 1 = Soccer na Pinnacle → não força idcampeonato
                    k = (sport_id_pinn, int(snap.league_id))
                    idc_map = self._pinn_to_esnet_league.get(k)
                    if idc_map:
                        allowed_idc = [int(idc_map)]
            except Exception:
                allowed_idc = []

            meta: Dict[str, Any] = {
                "event_id": pinn_event_id,
                "home": snap.home,
                "away": snap.away,
                "league_name": snap.league_name,
                "league_norm": league_norm,
                "starts": snap.starts_at_utc,
                "sport_id_pinnacle": snap.sport_id,
                "sport_id_esnet": esnet_sport_id,   # útil pro bridge e debug
            }
            if allowed_idc:
                meta["allowed_idcampeonatos"] = allowed_idc

            # 1) Se já tem match válido, evita rematch pesado:
            cached_match = self.match_cache.get(pinn_event_id)
            if cached_match is not None:
                logger.debug(
                    "[match_cache] HIT pinn_event_id=%s pid=%s expires_at=%s",
                    pinn_event_id,
                    cached_match.esnet_pid,
                    cached_match.expires_at_utc.isoformat(),
                )
                forced_candidates = [{
                    "pid": cached_match.esnet_pid,
                    "idpartida": cached_match.esnet_pid,
                    "idcampeonato": (cached_match.esnet_meta or {}).get("idcampeonato"),
                    "league_norm": cached_match.league_norm,
                    "home_norm": cached_match.home_norm,
                    "away_norm": cached_match.away_norm,
                    "sport_id": cached_match.sport_id_esnet,
                }]

                try:
                    probe = self.esnet_bridge.probe(
                        meta,
                        items=forced_candidates,
                        sport_id=esnet_sport_id
                    )
                    _attach_esnet_sport_id(probe, esnet_sport_id)
                except Exception:
                    logger.exception("Falha no probe EsporteNet (cache-hit) | event_id=%s", pinn_event_id)
                    results.append(
                        CercoMatchResult(
                            snapshot=snap,
                            esnet_found=True,
                            esnet_meta=cached_match.esnet_meta,
                            esnet_url=cached_match.esnet_url,
                            opportunities=[],
                        )
                    )
                    continue
            else:
                probe = None

                # 2) Snapshot EsporteNet (descoberta lenta) - por esporte
                self._ensure_esnet_snapshot(esnet_sport_id)

                # 3) Filtro por liga (se snapshot está disponível)
                leagues_esnet = self.esnet_snapshot.all_leagues_for_sport(esnet_sport_id)
                if leagues_esnet:
                    if not self.esnet_snapshot.has_league(esnet_sport_id, league_norm):
                        logger.info(
                            "EsporteNet: liga não está no snapshot do dia | league=%s | league_norm=%s | evento ignorado",
                            snap.league_name,
                            league_norm,
                        )
                        results.append(
                            CercoMatchResult(
                                snapshot=snap,
                                esnet_found=False,
                                esnet_meta={
                                    "reason": "league_not_in_esnet_snapshot",
                                    "league_norm": league_norm,
                                },
                                esnet_url=None,
                                opportunities=[],
                            )
                        )
                        continue

                # 4) Candidatos filtrados por liga + não usados
                candidates = self.esnet_snapshot.get_candidates(esnet_sport_id, league_norm)
                if candidates:
                    candidates = [
                        c for c in candidates
                        if int(c.get("pid") or c.get("idpartida") or 0) not in self.match_cache.used_esnet_pids
                    ]

                try:
                    probe = self.esnet_bridge.probe(meta, items=candidates, sport_id=esnet_sport_id)
                    _attach_esnet_sport_id(probe, esnet_sport_id)
                except Exception:
                    logger.exception("Falha no probe EsporteNet | event_id=%s", pinn_event_id)
                    results.append(
                        CercoMatchResult(
                            snapshot=snap,
                            esnet_found=False,
                            esnet_meta={"error": "probe_exception"},
                            esnet_url=None,
                            opportunities=[],
                        )
                    )
                    continue

            # ------------------------------------------------------------------
            # Resultado do probe
            # ------------------------------------------------------------------
            if not getattr(probe, "found", False):
                score = (getattr(probe, "debug", {}) or {}).get("score")
                logger.info(
                    "EsporteNet: NÃO encontrado | %s x %s | score=%s",
                    snap.home,
                    snap.away,
                    score,
                )
                results.append(
                    CercoMatchResult(
                        snapshot=snap,
                        esnet_found=False,
                        esnet_meta=getattr(probe, "debug", {}),
                        esnet_url=getattr(probe, "day_url", None),
                        opportunities=[],
                    )
                )
                continue

            logger.info(
                "EsporteNet: encontrado | %s x %s | url=%s",
                getattr(probe, "home", snap.home),
                getattr(probe, "away", snap.away),
                getattr(probe, "event_url", None),
            )

            # 5) Salva match no cache (primeiro match)
            if cached_match is None:
                try:
                    self.match_cache.put_from_probe(
                        snap,
                        probe,
                        league_norm=league_norm,
                        sport_id_esnet=esnet_sport_id,
                    )
                except Exception:
                    logger.debug("[match_cache] falha ao registrar match (não fatal).")

            # ------------------------------------------------------------------
            # Comparator / Arbitragem
            # ------------------------------------------------------------------
            try:
                opportunities = build_books_and_find_arbs(
                    snap.markets_raw,
                    probe,
                    specials_raw_pinnacle=snap.specials_raw,
                    min_edge=self.min_edge,
                )
            except Exception:
                logger.exception(
                    "Falha ao construir livros/comparar | event_id=%s",
                    pinn_event_id,
                )
                results.append(
                    CercoMatchResult(
                        snapshot=snap,
                        esnet_found=True,
                        esnet_meta=getattr(probe, "meta", {}),
                        esnet_url=getattr(probe, "event_url", None),
                        opportunities=[],
                    )
                )
                continue

            if not opportunities:
                logger.info(
                    "Sem oportunidades >= %.4f | %s x %s",
                    self.min_edge,
                    snap.home,
                    snap.away,
                )
            else:
                logger.info(
                    "Oportunidades=%d | %s x %s",
                    len(opportunities),
                    snap.home,
                    snap.away,
                )
                for opp in opportunities:
                    logger.debug(
                        "  [%s line=%s] edge=%.4f prob=%.4f legs=%s",
                        opp.family,
                        opp.line,
                        opp.edge,
                        opp.implied_prob,
                        [(leg.house, leg.outcome, leg.price) for leg in opp.legs],
                    )

            results.append(
                CercoMatchResult(
                    snapshot=snap,
                    esnet_found=True,
                    esnet_meta=getattr(probe, "meta", {}),
                    esnet_url=getattr(probe, "event_url", None),
                    opportunities=opportunities,
                )
            )

        logger.info("CERCO Orchestrator | concluído | eventos processados=%d", len(results))
        return results


# ---------------------------------------------------------------------------
# Entrypoint de debug (CLI)
# ---------------------------------------------------------------------------

def main_once(min_edge: float = CERCO_MIN_EDGE) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    logger.info("Boot CercoOrchestrator(run_once) | min_edge=%.4f", min_edge)
    orch = CercoOrchestrator(min_edge=min_edge)
    try:
        results = orch.run_once()
        total_arbs = sum(len(r.opportunities) for r in results)
        logger.info(
            "Resumo final: eventos=%d, oportunidades_totais=%d",
            len(results),
            total_arbs,
        )
    finally:
        orch.close()


if __name__ == "__main__":
    main_once()
