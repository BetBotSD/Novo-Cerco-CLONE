# -*- coding: utf-8 -*-
"""
esportenet_bridge_cerco.py
--------------------------
Bridge de coleta/probe da EsporteNet para o sistema CERCO.

- Scraping leve (requests + BeautifulSoup).
- Match de nomes com aliases aprendíveis (JSON local).
- Extrai mercados canônicos:
    * moneyline (1x2)  -> "Vencedor do Encontro" etc.
    * totals jogo      -> "Total de Gols no Jogo" / "Total de Pontos no Jogo" / "Total pontos"
    * team totals      -> "Casa - Total de Gols no Jogo" / "Fora - Total..."
    * spreads derivados (soccer) -> DNB, Dupla Chance, ML via -0.5
    * spreads diretos (multi-esporte) -> "Handicap pontos", "Handicap (incluindo prolongamento)" etc
- Além disso, monta snapshot cru com TODOS os mercados/linhas/opções
  da página do evento, para uso posterior pelo normalizador do CERCO.

AUDITORIA EXTRA (NOVO):
- Logs brutos de markets/runners antes de filtrar vs. canônicos.
- Dump controlado dos nomes de mercados e opções (configurável).
- Logs do match de headers canônicos (qual header do HTML casou, com score).
- Logs de integridade de odds (quantas odds parsearam / quantas falharam).

MUDANÇA (2025-11-23):
- Para esportes NÃO Soccer (sport_id != 102):
    * se meta trouxer allowed_idcampeonatos (derivado do JSON),
      usamos esses idcampeonatos como PRIORIDADE 1 para buscar candidatos.
    * se não houver candidatos nesses idcs, cai no fallback antigo
      (scan de todas as ligas via index EsporteNet).
- Para Soccer:
    * mantém a lógica por weekday idcampeonato_for_date (prioridade total).

MUDANÇA (EXTRA – 2025-11-24):
- Para esportes NÃO Soccer:
    * Depois de tentar allowed_idcampeonatos, tentamos resolver idcampeonatos
      via esnet_league_map.json (quando possível).
    * Só então caímos no scan global de ligas.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List, Tuple, Optional, Any
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
import json
import os
import re
import time
import logging

import requests
from bs4 import BeautifulSoup
from rapidfuzz import fuzz

__all__ = ["EsporteNetBridgeCerco", "EsNetProbeResult", "EsNetMarketResult"]

logger = logging.getLogger(__name__)

# ----------------------------- Config embutida / fallbacks -----------------------------
WEEKDAY_CODES_FALLBACK = {
    "segunda": 574588,
    "terça":   574908,
    "terca":   574908,
    "quarta":  574926,
    "quinta":  575066,
    "sexta":   575067,
    "sábado":  574584,
    "sabado":  574584,
    "domingo": 574583,
}

ESNET_BASE_FALLBACK = "https://esportenet-bet.jogos.app/sistema_v2/usuarios/simulador/desktop"

try:
    from .config_cerco import ESNET_DAY_CODES as _CFG_DAY_CODES  # type: ignore
except Exception:
    _CFG_DAY_CODES = None

try:
    from .config_cerco import ESNET_BASE_URL as _CFG_BASE_URL  # type: ignore
except Exception:
    _CFG_BASE_URL = None

try:
    from .config_cerco import ESNET_LOCAL_TZ as _CFG_LOCAL_TZ  # type: ignore
except Exception:
    _CFG_LOCAL_TZ = None

ESNET_BASE = (_CFG_BASE_URL or ESNET_BASE_FALLBACK).rstrip("/")

def url_day(sport_id: int, idc: int, *, ts_ms: Optional[int] = None) -> str:
    ts = int(ts_ms if ts_ms is not None else time.time() * 1000)
    return f"{ESNET_BASE}/Jogos.aspx?idesporte={int(sport_id)}&idcampeonato={int(idc)}&_={ts}"

def url_league_index(sport_id: int) -> str:
    return f"{ESNET_BASE}/campeonatos.aspx?idesporte={int(sport_id)}"

def url_event(sport_id: int, idc: int, pid: int) -> str:
    return f"{ESNET_BASE}/Apostas.aspx?idesporte={int(sport_id)}&idcampeonato={int(idc)}&idpartida={int(pid)}"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit(537.36) (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
}

_HERE = os.path.dirname(__file__)
_DATA_DIR = os.path.abspath(os.path.join(_HERE, "..", "data"))
os.makedirs(_DATA_DIR, exist_ok=True)

LEAGUE_MAP_PATH = os.getenv(
    "ESNET_LEAGUE_JSON",
    os.path.join(os.path.dirname(__file__), "esnet_league_map.json")
)
ALIASES_PATH = os.getenv(
    "ESNET_ALIASES_JSON",
    os.path.join(_DATA_DIR, "esnet_team_aliases.json")
)

NAME_MATCH_MIN_SCORE = int(os.getenv("ESNET_MIN_NAME_SCORE", "90"))
ESNET_LOCAL_TZ = _CFG_LOCAL_TZ or "America/Sao_Paulo"

# ----------------------------- AUDITORIA / LOG LIMITS (NOVO) -----------------------------
ESNET_RAW_LOG_MAX_MARKETS = int(os.getenv("ESNET_RAW_LOG_MAX_MARKETS", "25"))
ESNET_RAW_LOG_MAX_RUNNERS_PER_MARKET = int(os.getenv("ESNET_RAW_LOG_MAX_RUNNERS_PER_MARKET", "10"))
ESNET_LOG_CANONICAL_HEADER_MATCH = os.getenv("ESNET_LOG_CANONICAL_HEADER_MATCH", "1") == "1"
ESNET_LOG_ODD_PARSE_ERRORS = os.getenv("ESNET_LOG_ODD_PARSE_ERRORS", "1") == "1"
ESNET_RAW_LOG_LEVEL = os.getenv("ESNET_RAW_LOG_LEVEL", "INFO").upper()

def _raw_log(msg: str, *args):
    lvl = getattr(logging, ESNET_RAW_LOG_LEVEL, logging.INFO)
    logger.log(lvl, msg, *args)

# ----------------------------- Tipos -----------------------------
@dataclass
class EsNetMarketResult:
    odds: Dict[Tuple[str, str, str], float] = field(default_factory=dict)
    raw_snapshot: Dict[str, Any] = field(default_factory=dict)

@dataclass
class EsNetProbeResult:
    found: bool
    event_url: Optional[str] = None
    day_url: Optional[str] = None
    url: Optional[str] = None
    idcampeonato: Optional[int] = None
    idpartida: Optional[int] = None
    home: Optional[str] = None
    away: Optional[str] = None
    home_logo: Optional[str] = None
    away_logo: Optional[str] = None
    markets: EsNetMarketResult = field(default_factory=EsNetMarketResult)
    debug: Dict[str, Any] = field(default_factory=dict)
    meta: Dict[str, Any] = field(default_factory=dict)

# ----------------------------- Snapshot cru de TODOS os mercados -----------------------------
@dataclass
class EsporteNetOption:
    raw_name: str
    selection_id: Optional[str]
    odd_str: str
    odd: Optional[Decimal]

@dataclass
class EsporteNetMarket:
    market_name: str
    market_id: Optional[str]
    options: List[EsporteNetOption] = field(default_factory=list)

@dataclass
class EsporteNetSnapshot:
    league: Optional[str]
    home: Optional[str]
    away: Optional[str]
    kickoff_time: Optional[str]
    markets: List[EsporteNetMarket] = field(default_factory=list)

def _soup(html: str) -> BeautifulSoup:
    try:
        return BeautifulSoup(html or "", "lxml")
    except Exception:
        return BeautifulSoup(html or "", "html.parser")

def _parse_decimal_br(text: str) -> Optional[Decimal]:
    text = (text or "").strip()
    if not text:
        return None
    try:
        return Decimal(text.replace(".", "").replace(",", "."))
    except InvalidOperation:
        return None

def parse_esportenet_event_html(html: str) -> EsporteNetSnapshot:
    soup = _soup(html)

    league = None
    home = None
    away = None
    kickoff_time = None

    league_span = soup.select_one("#content_nomeCampeonato")
    if league_span:
        league = league_span.get_text(strip=True) or None

    home_span = soup.select_one("#content_nomeTimeCasa")
    if home_span:
        home = home_span.get_text(strip=True) or None

    away_span = soup.select_one("#content_nomeTimeFora")
    if away_span:
        away = away_span.get_text(strip=True) or None

    hora_span = soup.select_one("#content_hora")
    data_span = soup.select_one("#content_data")
    if hora_span and data_span:
        hora = hora_span.get_text(strip=True)
        data = data_span.get_text(strip=True)
        if data and hora:
            kickoff_time = f"{data} {hora}"

    markets: List[EsporteNetMarket] = []
    container = soup.select_one("#content_Updjogos")
    if not container:
        return EsporteNetSnapshot(league, home, away, kickoff_time, [])

    for mkt_div in container.select("div.eventdetail-market"):
        header = mkt_div.select_one(".eventdetail-market-header span.name")
        if not header:
            continue

        market_name = header.get_text(" ", strip=True)
        market_id = header.get("id")

        body = mkt_div.select_one(".eventdetail-market-body")
        if not body:
            continue

        opts: List[EsporteNetOption] = []
        for opt_div in body.select(".eventdetail-optionItem"):
            name_span = opt_div.select_one("span.name")
            odd_a = opt_div.select_one("a.odd")

            if not name_span or not odd_a:
                continue

            raw_name = name_span.get_text(" ", strip=True)
            odd_str = odd_a.get_text(" ", strip=True)
            selection_id = odd_a.get("id")

            opts.append(
                EsporteNetOption(
                    raw_name=raw_name,
                    selection_id=selection_id,
                    odd_str=odd_str,
                    odd=_parse_decimal_br(odd_str),
                )
            )

        if opts:
            markets.append(EsporteNetMarket(market_name, market_id, opts))

    return EsporteNetSnapshot(league, home, away, kickoff_time, markets)

def _esnet_snapshot_to_dict(snap: EsporteNetSnapshot) -> Dict[str, Any]:
    return {
        "league": snap.league,
        "home": snap.home,
        "away": snap.away,
        "kickoff_time": snap.kickoff_time,
        "markets": [
            {
                "market_name": m.market_name,
                "market_id": m.market_id,
                "options": [
                    {
                        "raw_name": o.raw_name,
                        "selection_id": o.selection_id,
                        "odd_str": o.odd_str,
                        "odd": float(o.odd) if o.odd is not None else None,
                    }
                    for o in m.options
                ],
            }
            for m in snap.markets
        ],
    }

# ----------------------------- Normalização de nomes -----------------------------
_PT_STOPWORDS = {
    "fc","ac","sc","cf","u19","u20","u21","u23","de","da","do","esporte","club",
    "clubes","clube","atl","atletico","atlético","ca","cd","sd","fk","afc","bk",
    "sp","b","c","the","nk","bc","kk","basket","basquete","basketball"
}

def _strip_accents(s: str) -> str:
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", s or "") if unicodedata.category(c) != "Mn")

def _norm_team(s: str) -> str:
    s = _strip_accents(s or "").lower()
    s = re.sub(r"[^a-z0-9\s]+", " ", s)
    toks = [t for t in s.split() if t and t not in _PT_STOPWORDS]
    return " ".join(toks)

def _best_ratio(a: str, b: str) -> int:
    a2, b2 = _norm_team(a), _norm_team(b)
    return max(
        fuzz.QRatio(a2, b2),
        fuzz.token_set_ratio(a2, b2),
        fuzz.partial_ratio(a2, b2),
    )

# ----------------------------- Utils numéricas / texto -----------------------------
def _parse_pt_float(txt: str) -> Optional[float]:
    m = re.search(r"\d+(?:[.,]\d+)?", str(txt))
    if not m:
        return None
    return float(m.group(0).replace(",", "."))

def _fmt_line(v: float | str) -> str:
    try:
        f = float(v)
    except Exception:
        return str(v)
    s = f"{float(f):.2f}".rstrip("0").rstrip(".")
    return s or "0"

# ----------------------------- Dia local -----------------------------
def _weekday_name_local(dt: datetime) -> str:
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(ESNET_LOCAL_TZ)
    except Exception:
        tz = timezone(timedelta(hours=-3))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt_local = dt.astimezone(tz)
    idx = dt_local.weekday()
    return ["segunda","terça","quarta","quinta","sexta","sábado","domingo"][idx]

def _weekday_name_from_meta(meta: dict, default_dt_utc: Optional[datetime] = None) -> Optional[str]:
    w = str(meta.get("weekday_name") or "").strip().lower()
    if w in {"segunda","terça","terca","quarta","quinta","sexta","sábado","sabado","domingo"}:
        return "terça" if w == "terca" else ("sábado" if w == "sabado" else w)
    dt = meta.get("starts")
    if isinstance(dt, datetime):
        return _weekday_name_local(dt)
    if default_dt_utc and isinstance(default_dt_utc, datetime):
        return _weekday_name_local(default_dt_utc)
    return None

# ----------------------------- Aliases / persistência simples -----------------------------
def _load_json_or(path: str, default):
    try:
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        logger.exception("Falha ao carregar JSON: %s", path)
    return default

def _save_json(path: str, obj):
    try:
        import tempfile
        d = os.path.dirname(path) or "."
        os.makedirs(d, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8", dir=d) as tmp:
            json.dump(obj, tmp, ensure_ascii=False, indent=2)
            tmp_path = tmp.name
        os.replace(tmp_path, path)
    except Exception:
        logger.exception("Falha ao salvar JSON: %s", path)

# ----------------------------- Parsers de mercados (canônicos) -----------------------------
ESNET_GROUP_ALIASES = {
    # moneyline / vencedor
    "ml": {
        "vencedor do encontro",
        "vencedor do encontro (incluindo prolongamento)",
        "vencedor do jogo",
        "vencedor",
        "1x2",
        "resultado do jogo",
        "resultado final",
    },

    # totals do jogo (gols/pontos)
    "tt_all": {
        "total de gols no jogo",
        "total de gol no jogo",
        "total de pontos no jogo",
        "total de ponto no jogo",
        "total de pontos no jogo (incl prorrogação)",
        "total de pontos no jogo (incl. prorrogação)",
        "total no jogo",
        "total no jogo (incluindo prolongamento)",
        "total pontos",
        "total pontos (incluindo prolongamento)",
        "total (incluindo prolongamento)",
        "total do jogo",
        "total geral",
        "total de pontos",
    },

    # team totals (quando existir)
    "tt_home": {
        "casa - total de gols no jogo",
        "casa - total de gol no jogo",
        "casa - total de pontos no jogo",
        "casa - total de ponto no jogo",
        "casa - total pontos",
        "time da casa - total",
        "time da casa - total de pontos",
        "pontos da equipe da casa",
        "casa - total",
    },
    "tt_away": {
        "fora - total de gols no jogo",
        "fora - total de gol no jogo",
        "fora - total de pontos no jogo",
        "fora - total de ponto no jogo",
        "fora - total pontos",
        "time de fora - total",
        "time de fora - total de pontos",
        "pontos da equipe visitante",
        "fora - total",
    },

    # BTTS / Ambas Marcam
    "btts_ft": {
        "ambas as equipes marcam",
    },
    "btts_1h": {
        "1º tempo - ambas as equipes marcam",
        "1 tempo - ambas as equipes marcam",
        "primeiro tempo - ambas as equipes marcam",
    },
    "btts_2h": {
        "2º tempo - ambas as equipes marcam",
        "2 tempo - ambas as equipes marcam",
        "segundo tempo - ambas as equipes marcam",
    },


    # soccer spreads derivados
    "dnb": {"empate nao tem aposta","empate não tem aposta"},
    "double_chance": {"chance dupla","dupla chance"},

    # spreads diretos multi-esporte (NOVO)
    "spread_direct": {
        "handicap",
        "handicap pontos",
        "handicap de pontos",
        "handicap de pontos no jogo",
        "handicap no jogo",
        "handicap (incluindo prolongamento)",
        "handicap (incl prorrogação)",
        "handicap (incl. prorrogação)",
    },
}

_NEGATIVE_HEADER_TOKENS = {
    "1º tempo","1 tempo","primeiro tempo",
    "2º tempo","2 tempo","segundo tempo",
    "escanteio","escanteios",
    "cartão","cartões","cards",
    "período","periodo","minuto","minutos",
    "set","game","quarto",
}

def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", _strip_accents(str(s)).strip().lower())

ESNET_HEADER_SCORE_MIN = float(os.getenv("ESNET_HEADER_SCORE_MIN", "70"))

def _norm_header(s: str) -> str:
    s = _strip_accents(str(s or "")).lower()
    s = re.sub(r"[^a-z0-9\s]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def _header_matches(header_text: str, aliases: List[str]) -> Tuple[bool, float, str]:
    h_raw = header_text or ""
    h = _norm_header(h_raw)
    if not h:
        return False, 0.0, h

    aliases_norm = [_norm_header(a) for a in aliases if a]

    # 1) Bloqueio por tokens "negativos" (mantém sua regra atual)
    has_negative = any(neg in h for neg in _NEGATIVE_HEADER_TOKENS)
    if has_negative:
        alias_has_negative = any(any(neg in a for neg in _NEGATIVE_HEADER_TOKENS) for a in aliases_norm)
        if not alias_has_negative:
            return False, 0.0, h

    # 2) ✅ Desambiguação Casa/Fora (evita confundir team totals com totals do jogo)
    # Se ALGUM alias do grupo exige "casa", o header precisa ter "casa"
    alias_requires_home = any("casa" in a for a in aliases_norm)
    alias_requires_away = any("fora" in a for a in aliases_norm)

    if alias_requires_home and "casa" not in h:
        return False, 0.0, h
    if alias_requires_away and "fora" not in h:
        return False, 0.0, h

    best_score = 0.0
    for a in aliases_norm:
        if not a:
            continue

        if h == a:
            best_score = max(best_score, 100.0); continue
        if a in h:
            best_score = max(best_score, 92.0); continue

        # ⚠️ Aqui estava a “armadilha”: h in a dava 88 e aceitava "Total de gols no jogo"
        # Mantemos, mas agora a desambiguação Casa/Fora acima impede o falso-positivo
        if h in a:
            best_score = max(best_score, 88.0); continue

        h_tokens = set(h.split())
        a_tokens = set(a.split())
        if not h_tokens or not a_tokens:
            continue
        inter = h_tokens.intersection(a_tokens)
        union = h_tokens.union(a_tokens)
        jacc = len(inter) / max(1, len(union))
        score = 70.0 * jacc
        if any(t in h for t in ("total", "gols", "handicap", "vencedor", "escante")) and any(
            t in a for t in ("total", "gols", "handicap", "vencedor", "escante")
        ):
            score += 10.0
        best_score = max(best_score, score)

    matched = best_score >= ESNET_HEADER_SCORE_MIN
    return matched, best_score, h


def _find_section_node_dbg(soup: BeautifulSoup, aliases: set[str]) -> tuple[Optional[Any], Optional[str], int]:
    best_node = None
    best_header = None
    best_score = 0

    for market in soup.select("div.eventdetail-market"):
        h_el = market.select_one(".eventdetail-market-header span.name")
        header_text = h_el.get_text(" ", strip=True) if h_el else ""
        matched, sc, _ = _header_matches(header_text, list(aliases))
        if matched and sc >= best_score:
            best_score = sc
            best_header = header_text
            best_node = market.select_one(".eventdetail-market-body") or market

    return best_node, best_header, best_score

def _find_section_node(soup: BeautifulSoup, aliases: set[str]) -> Optional[Any]:
    node, _, _ = _find_section_node_dbg(soup, aliases)
    return node

def _extract_price_from_option(node) -> Optional[float]:
    if not node:
        return None
    for cand in node.select("a.odd, .odd") or node.find_all(["a","span","div","b","strong"]):
        v = _parse_pt_float(getattr(cand, "get_text", lambda **_: str(cand))(strip=True))
        if v is not None:
            return v
    return None

def parse_moneyline_from_event(html: str) -> Dict[Tuple[str, str, str], float]:
    soup = _soup(html)
    sec, hdr, sc = _find_section_node_dbg(soup, ESNET_GROUP_ALIASES["ml"])
    if not sec:
        logger.debug("[markets] Moneyline não encontrado")
        return {}
    if ESNET_LOG_CANONICAL_HEADER_MATCH:
        logger.info("[esnet][canon] moneyline header='%s' score=%s", hdr, sc)

    out: Dict[Tuple[str, str, str], float] = {}
    for opt in sec.select(".eventdetail-optionItem"):
        label = _norm((opt.select_one("span.name") or opt).get_text(" ", strip=True))
        price = _extract_price_from_option(opt)
        if price is None:
            continue

        if label.startswith("casa") or label in {"1","home"}:
            out[("money_line","", "home")] = float(price)
        elif label.startswith("empate") or label in {"x","draw"}:
            out[("money_line","", "draw")] = float(price)
        elif label.startswith("fora") or label in {"2","away"}:
            out[("money_line","", "away")] = float(price)

    logger.debug("[markets] Moneyline: %s chaves", len(out))
    return out

def _label_to_sel_pts(label: str):
    t = _norm(label)
    t = re.sub(r"\(.*?\)", "", t)

    if re.search(r"^(mais|acima)(\s+de)?\s+", t):
        return ("over", _parse_pt_float(t))
    if re.search(r"^(menos|abaixo)(\s+de)?\s+", t):
        return ("under", _parse_pt_float(t))

    if t.startswith("over"):
        return ("over", _parse_pt_float(t))
    if t.startswith("under"):
        return ("under", _parse_pt_float(t))
    return (None, None)

def parse_totals_from_event(html: str) -> Dict[Tuple[str, str, str], float]:
    soup = _soup(html)
    sec, hdr, sc = _find_section_node_dbg(soup, ESNET_GROUP_ALIASES["tt_all"])
    if not sec:
        logger.debug("[markets] Totais (jogo) não encontrados")
        return {}
    if ESNET_LOG_CANONICAL_HEADER_MATCH:
        logger.info("[esnet][canon] totals(jogo) header='%s' score=%s", hdr, sc)

    out: Dict[Tuple[str, str, str], float] = {}
    for opt in sec.select(".eventdetail-optionItem"):
        label = (opt.select_one("span.name") or opt).get_text(" ", strip=True)
        sel, pts = _label_to_sel_pts(label)
        if sel is None or pts is None:
            continue

        price = _extract_price_from_option(opt)
        if price is None:
            continue

        out[("totals", _fmt_line(pts), "over" if sel == "over" else "under")] = float(price)

    logger.debug("[markets] Totais (jogo): %s chaves", len(out))
    return out

def parse_team_totals_from_event(html: str) -> Dict[Tuple[str, str, str], float]:
    soup = _soup(html)
    out: Dict[Tuple[str, str, str], float] = {}

    for mkt_key, aliases in [
        ("team_total_home", ESNET_GROUP_ALIASES["tt_home"]),
        ("team_total_away", ESNET_GROUP_ALIASES["tt_away"]),
    ]:
        sec, hdr, sc = _find_section_node_dbg(soup, aliases)
        if not sec:
            if ESNET_LOG_CANONICAL_HEADER_MATCH:
                logger.info("[esnet][canon] %s header=NOT_FOUND", mkt_key)
            continue
        if ESNET_LOG_CANONICAL_HEADER_MATCH:
            logger.info("[esnet][canon] %s header='%s' score=%s", mkt_key, hdr, sc)

        for opt in sec.select(".eventdetail-optionItem"):
            label = (opt.select_one("span.name") or opt).get_text(" ", strip=True)
            sel, pts = _label_to_sel_pts(label)
            if sel is None or pts is None:
                continue

            price = _extract_price_from_option(opt)
            if price is None:
                continue

            out[(mkt_key, _fmt_line(pts), "over" if sel == "over" else "under")] = float(price)

    if out:
        logger.debug("[markets] Team Totals: %s chaves", len(out))
    return out

def parse_btts_from_event(html: str) -> Dict[Tuple[str, str, str], float]:
    """
    Parser de BTTS (Ambas Marcam) em três níveis:
    - btts_ft  (jogo inteiro)
    - btts_1h  (1º tempo)
    - btts_2h  (2º tempo)

    Chaves geradas em markets.odds:
        ("btts_ft",  "", "yes"/"no")
        ("btts_1h",  "", "yes"/"no")
        ("btts_2h",  "", "yes"/"no")
    """
    soup = _soup(html)
    out: Dict[Tuple[str, str, str], float] = {}

    # (mkt_key, grupo_de_aliases)
    for mkt_key, aliases in [
        ("btts_ft", ESNET_GROUP_ALIASES.get("btts_ft", set())),
        ("btts_1h", ESNET_GROUP_ALIASES.get("btts_1h", set())),
        ("btts_2h", ESNET_GROUP_ALIASES.get("btts_2h", set())),
    ]:
        if not aliases:
            continue

        sec, hdr, sc = _find_section_node_dbg(soup, aliases)
        if not sec:
            if ESNET_LOG_CANONICAL_HEADER_MATCH:
                logger.info("[esnet][canon] %s header=NOT_FOUND", mkt_key)
            continue

        if ESNET_LOG_CANONICAL_HEADER_MATCH:
            logger.info("[esnet][canon] %s header='%s' score=%s", mkt_key, hdr, sc)

        for opt in sec.select(".eventdetail-optionItem"):
            label_raw = (opt.select_one("span.name") or opt).get_text(" ", strip=True)
            label = _norm(label_raw)
            price = _extract_price_from_option(opt)
            if price is None:
                continue

            side: Optional[str] = None
            # Sim / Não ⇔ yes / no
            if label.startswith("sim") or label in {"yes"}:
                side = "yes"
            elif label.startswith("nao") or label.startswith("não") or label in {"no"}:
                side = "no"

            if not side:
                continue

            out[(mkt_key, "", side)] = float(price)

    if out:
        logger.debug("[markets] BTTS: %s chaves", len(out))
    else:
        logger.debug("[markets] BTTS: nenhum mercado encontrado")

    return out


def parse_spreads_from_event(html: str) -> Dict[Tuple[str, str, str], float]:
    soup = _soup(html)
    out: Dict[Tuple[str, str, str], float] = {}

    def _store_spread(line_str: str, side: str, price: float) -> None:
        ln = str(line_str).strip()
        out[("spread", ln, side)] = float(price)
        if ln.startswith("+"):
            out[("spread", ln[1:], side)] = float(price)
        else:
            try:
                v = float(ln.replace(",", ".").replace("−", "-"))
                if v > 0:
                    out[("spread", f"+{_fmt_line(v)}", side)] = float(price)
            except Exception:
                pass

    # -------- spreads derivados (soccer) --------
    dnb_sec, hdr_dnb, sc_dnb = _find_section_node_dbg(soup, ESNET_GROUP_ALIASES["dnb"])
    if dnb_sec and ESNET_LOG_CANONICAL_HEADER_MATCH:
        logger.info("[esnet][canon] dnb header='%s' score=%s", hdr_dnb, sc_dnb)

    if dnb_sec:
        for opt in dnb_sec.select(".eventdetail-optionItem"):
            nm = _norm((opt.select_one("span.name") or opt).get_text(" ", strip=True))
            price = _extract_price_from_option(opt)
            if price is None:
                continue
            if nm.startswith("casa"):
                _store_spread("0", "home", price)
            elif nm.startswith("fora"):
                _store_spread("0", "away", price)

    dc_sec, hdr_dc, sc_dc = _find_section_node_dbg(soup, ESNET_GROUP_ALIASES["double_chance"])
    if dc_sec and ESNET_LOG_CANONICAL_HEADER_MATCH:
        logger.info("[esnet][canon] double_chance header='%s' score=%s", hdr_dc, sc_dc)

    if dc_sec:
        for opt in dc_sec.select(".eventdetail-optionItem"):
            nm = _norm((opt.select_one("span.name") or opt).get_text(" ", strip=True))
            price = _extract_price_from_option(opt)
            if price is None:
                continue
            if "casa ou empate" in nm:
                _store_spread("+0.5", "home", price)
            elif "empate ou fora" in nm:
                _store_spread("+0.5", "away", price)

    # -------- spreads diretos multi-esporte (NOVO) --------
    sp_sec, hdr_sp, sc_sp = _find_section_node_dbg(soup, ESNET_GROUP_ALIASES["spread_direct"])
    if sp_sec and ESNET_LOG_CANONICAL_HEADER_MATCH:
        logger.info("[esnet][canon] spread_direct header='%s' score=%s", hdr_sp, sc_sp)

    if sp_sec:
        for opt in sp_sec.select(".eventdetail-optionItem"):
            raw_label = (opt.select_one("span.name") or opt).get_text(" ", strip=True)
            nm = _norm(raw_label)
            price = _extract_price_from_option(opt)
            if price is None:
                continue

            # side
            side = None
            if nm.startswith("casa") or " casa" in nm:
                side = "home"
            elif nm.startswith("fora") or " fora" in nm:
                side = "away"

            if not side:
                continue

            # linha: pega número com sinal dentro do label (normalmente entre parênteses)
            m_line = re.search(r"([+\-−]?\s*\d+(?:[.,]\d+)?)", raw_label)
            if not m_line:
                continue

            line_raw = m_line.group(1).replace(" ", "").replace("−", "-").replace(",", ".")
            try:
                line_val = float(line_raw)
            except Exception:
                continue
            line_str = _fmt_line(line_val)
            if line_val > 0:
                line_str = f"+{line_str}"

            _store_spread(line_str, side, price)

    if out:
        logger.debug("[markets] Spreads: %s chaves", len(out))
    return out

##################################################################################################################
def parse_corners_totals_from_event(html: str) -> Dict[Tuple[str, str, str], float]:
    """
    Total de escanteios no jogo (FT) – mercado Over/Under.

    Canonical family: ("corners_total", linha, "over"/"under")
    Exemplo de chave: ("corners_total", "9.5", "over")
    """
    soup = _soup(html)
    section = None
    chosen_header = None

    # Procuramos o bloco cujo header fale de ESCANTEIOS/CANTOS + TOTAL,
    # mas evitando 1T/2T para focar em FT.
    for market in soup.select("div.eventdetail-market"):
        h_el = market.select_one(".eventdetail-market-header span.name")
        header_text = h_el.get_text(" ", strip=True) if h_el else ""
        norm = _norm(header_text)

        if not norm:
            continue

        # precisa falar de escanteios ou cantos
        if ("escanteio" not in norm and "escanteios" not in norm and
                "canto" not in norm and "cantos" not in norm):
            continue

        # precisa falar de total
        if "total" not in norm:
            continue

        # evita mercados claramente de 1T/2T
        if any(tok in norm for tok in (
            "1t", "2t",
            "1 tempo", "2 tempo",
            "1º tempo", "2º tempo",
            "1o tempo", "2o tempo"
        )):
            continue

        section = market.select_one(".eventdetail-market-body") or market
        chosen_header = header_text
        break

    if not section:
        logger.info("[esnet][corners_totals] seção não encontrada no HTML de detalhes.")
        return {}

    out: Dict[Tuple[str, str, str], float] = {}

    for opt in section.select(".eventdetail-optionItem"):
        # Mesma ideia de parse_totals_from_event: pegamos o texto bruto da opção
        name_node = (
            opt.select_one(".eventdetail-optionName")
            or opt.select_one("span.name")
            or opt
        )
        label = name_node.get_text(" ", strip=True) if name_node else ""
        if not label:
            continue

        # Reaproveitamos a mesma lógica de totals: "Mais de X,5" / "Menos de X,5"
        sel, pts = _label_to_sel_pts(label)
        if sel not in {"over", "under"} or pts is None:
            continue

        price = _extract_price_from_option(opt)
        if price is None:
            continue

        line_str = _fmt_line(pts)
        key = ("corners_total", line_str, sel)
        out[key] = float(price)

    logger.info(
        "[esnet][corners_totals] header=%s | keys=%s",
        chosen_header,
        list(out.keys())[:20],
    )
    return out


def parse_corners_1x2_from_event(html: str) -> Dict[Tuple[str, str, str], float]:
    """
    Escanteios 1X2 (vencedor por escanteios) – Casa / Empate / Fora.

    Canonical family: ("corners_1x2", "", side)
    Ex: ("corners_1x2", "", "home")
    """
    soup = _soup(html)
    section = None
    chosen_header = None

    # Procuramos header que fale de escanteios/cantos + 1x2 ou resultado/vencedor.
    for market in soup.select("div.eventdetail-market"):
        h_el = market.select_one(".eventdetail-market-header span.name")
        header_text = h_el.get_text(" ", strip=True) if h_el else ""
        norm = _norm(header_text)

        if not norm:
            continue

        if ("escanteio" not in norm and "escanteios" not in norm and
                "canto" not in norm and "cantos" not in norm):
            continue

        if not any(tok in norm for tok in ("1x2", "vencedor", "resultado")):
            continue

        # novamente, evitamos 1T/2T
        if any(tok in norm for tok in (
            "1t", "2t",
            "1 tempo", "2 tempo",
            "1º tempo", "2º tempo",
            "1o tempo", "2o tempo"
        )):
            continue

        section = market.select_one(".eventdetail-market-body") or market
        chosen_header = header_text
        break

    if not section:
        logger.info("[esnet][corners_1x2] seção não encontrada no HTML de detalhes.")
        return {}

    out: Dict[Tuple[str, str, str], float] = {}

    for opt in section.select(".eventdetail-optionItem"):
        name_node = (
            opt.select_one(".eventdetail-optionName")
            or opt.select_one("span.name")
            or opt
        )
        label = name_node.get_text(" ", strip=True) if name_node else ""
        if not label:
            continue

        norm = _norm(label)
        side: Optional[str] = None

        # Mapeia Casa / Empate / Fora similar ao moneyline
        if any(tok in norm for tok in ("casa", "mandante", "home")) or norm in {"1", "time 1"}:
            side = "home"
        elif any(tok in norm for tok in ("fora", "visitante", "away")) or norm in {"2", "time 2"}:
            side = "away"
        elif any(tok in norm for tok in ("empate", "draw", "x")):
            side = "draw"

        if not side:
            continue

        price = _extract_price_from_option(opt)
        if price is None:
            continue

        key = ("corners_1x2", "", side)
        out[key] = float(price)

    logger.info(
        "[esnet][corners_1x2] header=%s | keys=%s",
        chosen_header,
        list(out.keys())[:20],
    )
    return out


# ----------------------------- Logos helpers -----------------------------
def _abs_url(u: str) -> str:
    u = (u or "").strip()
    if not u:
        return ""
    if u.startswith("//"):
        return "https:" + u
    if u.startswith("http://") or u.startswith("https://"):
        return u
    return ESNET_BASE + "/" + u.lstrip("/")

def _normalize_logo_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return url
    u = re.sub(r"/ls/crest/(small|medium|default)/", "/ls/crest/big/", url)
    return u

def _extract_event_logos_from_event_html(html: str) -> Tuple[Optional[str], Optional[str]]:
    soup = _soup(html)

    def _get_src_by_id(elem_id: str) -> Optional[str]:
        el = soup.select_one(f"#{elem_id}")
        if el and el.name == "img":
            v = (el.get("src") or el.get("data-src") or "").strip()
            if v:
                return _normalize_logo_url(_abs_url(v))
        return None

    home = _get_src_by_id("content_logoCasa")
    away = _get_src_by_id("content_logoFora")
    return home, away

def _extract_event_logos_from_day_html(html: str, pid: int) -> Tuple[Optional[str], Optional[str]]:
    soup = _soup(html)
    home = away = None

    card = None
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "idpartida=" in href:
            m = re.search(r"idpartida=(\d+)", href)
            if m and int(m.group(1)) == int(pid):
                cur = a
                for _ in range(6):
                    cur = getattr(cur, "parent", None)
                    if not cur:
                        break
                    if cur.name in ("div","li","section"):
                        card = cur
                        break
                if card:
                    break

    if not card:
        return None, None

    imgs = card.select(".logoTeam img, img[id*=logoCasa], img[id*=logoFora], img[src*='/ls/crest/']")
    if imgs:
        if len(imgs) >= 1:
            home = (imgs[0].get("src") or imgs[0].get("data-src") or "").strip()
        if len(imgs) >= 2:
            away = (imgs[1].get("src") or imgs[1].get("data-src") or "").strip()

    return (
        _normalize_logo_url(_abs_url(home)) if home else None,
        _normalize_logo_url(_abs_url(away)) if away else None
    )


def _augment_country_meta(m: dict) -> dict:
    d = dict(m or {})
    country = (d.get("country_name") or d.get("country") or d.get("league_country") or "")
    d["country_name"] = (country or "").strip()
    if d.get("country_code"):
        d["country_code"] = str(d["country_code"]).strip().upper()
    return d

# ----------------------------- Página de dia/ligas → candidatos -----------------------------
def _split_country_league(raw: str) -> tuple[str, str]:
    t = (raw or "").strip()
    t = re.sub(r"\s+[–—-]\s+", " - ", t)
    if " - " in t:
        a, b = t.split(" - ", 1)
        return a.strip(), b.strip()
    return "", t

def _extract_candidates_from_day_page(html: str) -> List[Dict[str, Any]]:
    soup = _soup(html)
    out: List[Dict[str, Any]] = []

    groups = soup.select("div[id^=u]")

    def _parse_cards(container):
        cards = container.select("div.cardItem, div.cardItem3, div#cardJogo")
        for card in cards:
            a = card.find("a", href=re.compile(r"idpartida=\d+"))
            href = a.get("href", "") if a else card.decode()

            markets_url = href
            markets_count = None
            try:
                btn = card.select_one(".totalOutcomes .totalOutcomes-button")
                btn_txt = btn.get_text(" ", strip=True) if btn else ""
                if btn_txt.startswith("+"):
                    markets_count = int(re.sub(r"\D", "", btn_txt))
                if btn and btn.get("href"):
                    markets_url = btn.get("href")
            except Exception:
                pass

            m1 = re.search(r"idcampeonato=(\d+)", href)
            m2 = re.search(r"idpartida=(\d+)", href)
            if not (m1 and m2):
                continue
            idc = int(m1.group(1)); pid = int(m2.group(1))
            teams = card.select(".team") or card.select(".time")

            def _team_name(node):
                el = (node.select_one(".nameTeam span") or node.select_one(".nameTeam")
                      or node.select_one(".name") or node)
                return el.get_text(" ", strip=True) if el else None

            home = _team_name(teams[0]) if len(teams) >= 1 else None
            away = _team_name(teams[1]) if len(teams) >= 2 else None

            if home and away:
                yield (idc, pid, home, away, markets_url, markets_count, card)

    if groups:
        for g in groups:
            name_el = g.select_one(".eventlist-country .name") or g.select_one(".EventListCountry .name")
            country_name, league_name = _split_country_league(name_el.get_text(" ", strip=True) if name_el else "")
            for idc, pid, home, away, markets_url, markets_count, _ in _parse_cards(g):
                out.append({
                    "home": home, "away": away,
                    "idcampeonato": idc, "idpartida": pid,
                    "country_name": country_name,
                    "league_name": league_name,
                    "markets_url": markets_url,
                    "markets_count": markets_count,
                })
    else:
        for idc, pid, home, away, markets_url, markets_count, card in _parse_cards(soup):
            out.append({
                "home": home, "away": away,
                "idcampeonato": idc, "idpartida": pid,
                "country_name": "",
                "league_name": "",
                "markets_url": markets_url,
                "markets_count": markets_count,
            })

    seen = set()
    dedup = []
    for c in out:
        k = (c.get("idpartida"), c.get("idcampeonato"))
        if k in seen:
            continue
        seen.add(k)
        dedup.append(c)

    logger.info("[candidates] Extraídos %d candidatos (dedupe aplicado)", len(dedup))
    return dedup

# ----------------------------- Página do evento → nomes heurísticos -----------------------------
def _parse_names_from_event_page(html: str) -> Tuple[Optional[str], Optional[str]]:
    soup = _soup(html)
    h = soup.select_one("#content_nomeTimeCasa")
    a = soup.select_one("#content_nomeTimeFora")
    if h and a:
        return h.get_text(" ", strip=True), a.get_text(" ", strip=True)
    return None, None

def _extract_meta_from_event(html: str) -> Dict[str, Any]:
    soup = _soup(html)
    out: Dict[str, Any] = {}

    league_span = soup.select_one("#content_nomeCampeonato")
    if league_span:
        out["league_name"] = league_span.get_text(" ", strip=True)

    data_span = soup.select_one("#content_data")
    hora_span = soup.select_one("#content_hora")
    if data_span and hora_span:
        dt_text = f"{data_span.get_text(strip=True)} {hora_span.get_text(strip=True)}"
        out["starts_local"] = dt_text

    return out

# ----------------------------- AUDITORIA RAW SNAPSHOT (NOVO) -----------------------------
def _audit_raw_snapshot(pid: int, idc: int, sport_id: int, full_snap: EsporteNetSnapshot) -> None:
    markets_list = full_snap.markets or []
    count_markets_raw = len(markets_list)
    count_runners_raw = sum(len(m.options or []) for m in markets_list)

    odd_ok = 0
    odd_none = 0
    for m in markets_list:
        for o in (m.options or []):
            if o.odd is None:
                odd_none += 1
            else:
                odd_ok += 1

    _raw_log(
        "[esnet][raw] pid=%s idc=%s sport=%s | count_markets_raw=%s | count_runners_raw=%s | odd_ok=%s odd_none=%s",
        pid, idc, sport_id, count_markets_raw, count_runners_raw, odd_ok, odd_none
    )

    if ESNET_RAW_LOG_MAX_MARKETS > 0:
        top_markets = markets_list[:ESNET_RAW_LOG_MAX_MARKETS]
        _raw_log(
            "[esnet][raw] pid=%s | market_names=%s",
            pid, [m.market_name for m in top_markets]
        )

        for m in top_markets:
            opts = (m.options or [])[:ESNET_RAW_LOG_MAX_RUNNERS_PER_MARKET]
            _raw_log(
                "[esnet][raw] pid=%s | mkt='%s' id='%s' runners=%s sample=%s",
                pid,
                m.market_name,
                m.market_id,
                len(m.options or []),
                [{"name": o.raw_name, "odd": (float(o.odd) if o.odd else None), "odd_str": o.odd_str} for o in opts]
            )

    if ESNET_LOG_ODD_PARSE_ERRORS and odd_none > 0:
        bad_samples = []
        for m in markets_list:
            for o in (m.options or []):
                if o.odd is None and len(bad_samples) < 25:
                    bad_samples.append({"market": m.market_name, "name": o.raw_name, "odd_str": o.odd_str})
        _raw_log("[esnet][raw][odd_parse_errors] pid=%s samples=%s", pid, bad_samples)

# ----------------------------- Helpers novos -----------------------------
def _coerce_int_list(v: Any) -> List[int]:
    """
    Converte meta.allowed_idcampeonatos em lista de int segura.
    Aceita: int, str, list/tuple/set.
    """
    if v is None:
        return []
    if isinstance(v, (int,)):
        return [int(v)]
    if isinstance(v, str):
        v2 = re.findall(r"\d+", v)
        return [int(x) for x in v2]
    if isinstance(v, (list, tuple, set)):
        out = []
        for x in v:
            try:
                out.append(int(x))
            except Exception:
                continue
        return out
    return []

# ----------------------------- Bridge principal -----------------------------
class EsporteNetBridgeCerco:
    def __init__(self,
                 weekday_codes: Dict[str, int] | None = None,
                 league_map_path: Optional[str] = LEAGUE_MAP_PATH,
                 aliases_path: Optional[str] = ALIASES_PATH):
        self.weekday_codes = dict(weekday_codes or _CFG_DAY_CODES or WEEKDAY_CODES_FALLBACK)
        for i, key in enumerate(["segunda","terça","quarta","quinta","sexta","sábado","domingo"]):
            if str(i) in self.weekday_codes and key not in self.weekday_codes:
                self.weekday_codes[key] = self.weekday_codes[str(i)]

        self.league_map_path = league_map_path
        self.aliases_path = aliases_path

        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)

        try:
            from requests.adapters import HTTPAdapter  # type: ignore
            try:
                from urllib3.util.retry import Retry  # type: ignore
            except Exception:
                from urllib3.util import Retry  # type: ignore
            retries = Retry(
                total=3,
                backoff_factor=0.3,
                status_forcelist=(429, 500, 502, 503, 504),
                allowed_methods=frozenset(["GET", "HEAD"]),
            )
            adapter = HTTPAdapter(max_retries=retries)
            self.session.mount("http://", adapter)
            self.session.mount("https://", adapter)
        except Exception:
            pass

        self.league_map = _load_json_or(self.league_map_path, {"leagues": [], "weekdays": self.weekday_codes})

        self.aliases = _load_json_or(self.aliases_path, {"teams": {}, "pending": {}})
        self.aliases.setdefault("teams", {})
        self.aliases.setdefault("pending", {})

        self._aliases_dirty: bool = False
        self._aliases_last_save: float = 0.0
        self._aliases_save_min_interval_sec: float = float(os.getenv("ESNET_ALIASES_SAVE_DEBOUNCE_SEC", "5"))
        self._aliases_promote_seen: int = int(os.getenv("ESNET_ALIASES_PROMOTE_SEEN", "3"))
        self._aliases_min_learn_score: int = int(os.getenv("ESNET_ALIASES_MIN_LEARN_SCORE", "80"))
        self._aliases_max_per_team: int = int(os.getenv("ESNET_ALIASES_MAX_PER_TEAM", "12"))

        self._snapshots_by_event: dict[int, dict] = {}
        self._day_candidates_cache: dict[Tuple[int, int], Dict[str, Any]] = {}
        self._day_cache_ttl_sec: float = float(os.getenv("ESNET_DAY_CACHE_SEC", "120"))

        self._event_match_cache: dict[int, Dict[str, Any]] = {}
        self._event_match_cache_ttl_sec: float = float(os.getenv("ESNET_PROBE_CACHE_SEC", "5400"))

    # --------- Snapshots ----------
    def set_snapshot_for_event(self, event_id: int, snapshot: dict) -> None:
        try:
            from copy import deepcopy
            self._snapshots_by_event[int(event_id)] = deepcopy(snapshot)
        except Exception:
            pass

    def get_snapshot_for_event(self, event_id: int) -> dict:
        try:
            from copy import deepcopy
            return deepcopy(self._snapshots_by_event.get(int(event_id)) or {})
        except Exception:
            return {}

    def get_snapshot(self, event_id: int) -> dict:
        snap = self.get_snapshot_for_event(event_id)
        if not snap:
            return {}
        event = snap.get("event") or {}
        logos = snap.get("logos") or {}
        source = snap.get("source") or {}
        return {
            "event": {
                "event_id": event.get("event_id"),
                "home": event.get("home"),
                "away": event.get("away"),
                "league_name": event.get("league_name"),
                "country_name": event.get("country_name"),
                "starts": event.get("starts"),
            },
            "money_line": snap.get("money_line") or {},
            "totals": snap.get("totals") or {},
            "team_totals": snap.get("team_totals") or {},
            "logos": {
                "home": logos.get("home"),
                "away": logos.get("away"),
            },
            "source": {
                "event_url": source.get("event_url"),
                "day_url": source.get("day_url"),
            },
            "raw_markets": snap.get("raw_markets") or {},
        }

    def cerco_snapshot(self, event_id: int) -> dict:
        snap = self.get_snapshot_for_event(event_id)
        if not snap:
            return {}

        evt_block = snap.get("event", {}) if isinstance(snap.get("event"), dict) else {}
        out_event = {
            "home": evt_block.get("home"),
            "away": evt_block.get("away"),
            "league_name": evt_block.get("league_name") or evt_block.get("league"),
            "starts": evt_block.get("starts") or evt_block.get("kickoff_iso") or evt_block.get("kickoffUtc"),
        }

        money_line_map = snap.get("money_line") or {}
        totals_map = snap.get("totals") or {}
        team_totals_map = snap.get("team_totals") or {"casa": {}, "fora": {}}

        spreads_map: Dict[str, Dict[str, float]] = {}
        mkts = snap.get("markets") or {}
        if isinstance(mkts, dict):
            for k, price in mkts.items():
                if not isinstance(k, (tuple, list)) or len(k) != 3:
                    continue
                mkt_name, line_key, side_key = k
                if mkt_name != "spread":
                    continue
                line_key = str(line_key or "0").replace("−", "-").strip()
                side_key = str(side_key).strip().lower()
                spreads_map.setdefault(line_key, {})[side_key] = float(price)

        return {
            "event": out_event,
            "money_line": money_line_map,
            "totals": totals_map,
            "team_totals": team_totals_map,
            "spreads": spreads_map,
        }

    # --------- HTTP helpers ----------
    def _get(self, url: str, timeout: float = 12.0) -> Optional[str]:
        try:
            r = self.session.get(url, timeout=timeout)
            if r.status_code == 200 and r.text:
                logger.debug("[GET] %s → 200 OK (%d chars)", url, len(r.text))
                return r.text
            logger.warning("[GET] %s → %s", url, r.status_code)
        except Exception:
            logger.exception("[GET] Falha: %s", url)
        return None

    # --------- Descoberta de ligas ----------
    def list_leagues_for_sport(self, sport_id: int) -> List[int]:
        url = url_league_index(sport_id)
        html = self._get(url)
        if not html:
            logger.warning("[leagues] Índice vazio para esporte=%s", sport_id)
            return []
        soup = _soup(html)
        ids: List[int] = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "Jogos.aspx" in href and "idcampeonato=" in href:
                m = re.search(r"idcampeonato=(\d+)", href)
                if m:
                    ids.append(int(m.group(1)))
        ids = sorted(list({i for i in ids}))
        logger.info("[leagues] %d ligas encontradas para esporte=%s", len(ids), sport_id)
        return ids

    # --------- Candidatos de um dia/liga ----------
    def _fetch_day_candidates(self, sport_id: int, idc: int) -> List[Dict[str, Any]]:
        key = (int(sport_id), int(idc))
        now = time.time()

        cached = self._day_candidates_cache.get(key)
        if cached:
            ts_cached = float(cached.get("ts") or 0.0)
            if (now - ts_cached) <= self._day_cache_ttl_sec:
                try:
                    from copy import deepcopy
                    return deepcopy(cached.get("candidates") or [])
                except Exception:
                    return list(cached.get("candidates") or [])

        url = url_day(sport_id, idc, ts_ms=int(time.time() * 1000))
        html = self._get(url)
        if not html:
            logger.warning("[candidates] Página vazia: %s", url)
            return []
        cands = _extract_candidates_from_day_page(html)
        self._day_candidates_cache[key] = {"ts": now, "candidates": cands}
        return cands

    # --------- cache de match por event_id ----------
    def _get_cached_match(self, event_id: Any, sport_id: int) -> Optional[Dict[str, Any]]:
        if event_id is None:
            return None
        try:
            key = int(event_id)
        except Exception:
            return None
        rec = self._event_match_cache.get(key)
        if not rec:
            return None
        if int(rec.get("sport_id") or 0) != int(sport_id):
            return None
        ts = float(rec.get("ts") or 0.0)
        if (time.time() - ts) > self._event_match_cache_ttl_sec:
            self._event_match_cache.pop(key, None)
            return None
        return rec

    def _set_cached_match(
        self,
        event_id: Any,
        sport_id: int,
        *,
        idc: int,
        pid: int,
        best: Dict[str, Any],
        day_url: Optional[str],
        meta: Dict[str, Any],
        home_logo: Optional[str],
        away_logo: Optional[str],
        best_score: int,
        top_dbg: List[Dict[str, Any]],
    ) -> None:
        if event_id is None:
            return
        try:
            key = int(event_id)
        except Exception:
            return

        self._event_match_cache[key] = {
            "ts": time.time(),
            "sport_id": int(sport_id),
            "idc": int(idc),
            "pid": int(pid),
            "home": best.get("home"),
            "away": best.get("away"),
            "country_name": meta.get("country_name"),
            "league_name": meta.get("league_name"),
            "markets_url": best.get("markets_url"),
            "markets_count": best.get("markets_count"),
            "day_url": day_url,
            "home_logo": home_logo,
            "away_logo": away_logo,
            "match_score": int(best_score),
            "top_dbg": top_dbg,
        }

    # --------- filtro de candidatos com base no meta ----------
    def _filter_candidates_by_meta(self, candidates: List[Dict[str, Any]], meta: Dict[str, Any]) -> List[Dict[str, Any]]:
        if not candidates:
            return candidates

        allowed_idc = meta.get("allowed_idcampeonatos") or meta.get("allowed_idc")
        allowed_leagues = meta.get("allowed_league_names") or meta.get("allowed_leagues")
        allowed_countries = meta.get("allowed_country_names") or meta.get("allowed_countries")

        if not (allowed_idc or allowed_leagues or allowed_countries):
            return candidates

        def _norm_simple(x: Any) -> str:
            return _norm(str(x)) if x is not None else ""

        # BUGFIX: construção segura
        allowed_idc_set = set(_coerce_int_list(allowed_idc))

        allowed_leagues_norm = {_norm_simple(v) for v in (allowed_leagues or []) if str(v).strip()}
        allowed_countries_norm = {_norm_simple(v) for v in (allowed_countries or []) if str(v).strip()}

        filtered: List[Dict[str, Any]] = []
        for c in candidates:
            ok = True

            if allowed_idc_set:
                try:
                    cid = int(c.get("idcampeonato"))
                except Exception:
                    cid = None
                if cid not in allowed_idc_set:
                    ok = False

            if ok and allowed_leagues_norm:
                lig = _norm_simple(c.get("league_name"))
                if lig and lig not in allowed_leagues_norm:
                    ok = False

            if ok and allowed_countries_norm:
                ctry = _norm_simple(c.get("country_name"))
                if ctry and ctry not in allowed_countries_norm:
                    ok = False

            if ok:
                filtered.append(c)

        if filtered:
            logger.info("[candidates] Filtro meta aplicado: %d → %d", len(candidates), len(filtered))
            return filtered
        return candidates

    # --------- resolver idcampeonatos via esnet_league_map.json (NOVO) ----------
    def _resolve_idcs_from_league_map(self, meta: Dict[str, Any], sport_id: int) -> List[int]:
        """
        Tenta descobrir idcampeonatos da EsporteNet usando o esnet_league_map.json.

        Usa, na seguinte prioridade:
        - pinnacle_sport_id / pinnacle_league_id (ou variações de chave)
        - league_name (normalizado), quando não houver ID claro

        A função é defensiva, porque o schema do JSON pode variar levemente.
        """
        cfg = self.league_map or {}
        leagues_cfg = cfg.get("leagues") or []
        if not isinstance(leagues_cfg, list):
            return []

        def _as_int(val: Any, default: Optional[int] = None) -> Optional[int]:
            try:
                if val is None:
                    return default
                return int(val)
            except Exception:
                return default

        pin_sport_id = (
            _as_int(meta.get("pinnacle_sport_id")) or
            _as_int(meta.get("pinnacle_sportid")) or
            _as_int(meta.get("sport_id"))
        )
        pin_league_id = (
            _as_int(meta.get("pinnacle_league_id")) or
            _as_int(meta.get("pinnacle_leagueid")) or
            _as_int(meta.get("league_id")) or
            _as_int(meta.get("pinnacle_id"))
        )

        league_name = str(meta.get("league_name") or meta.get("league") or "").strip()
        league_name_norm = _norm(league_name) if league_name else ""

        idcs: List[int] = []

        for row in leagues_cfg:
            if not isinstance(row, dict):
                continue

            # tenta casar sport_id se existir no JSON
            row_pin_sport = (
                _as_int(row.get("pinnacle_sport_id")) or
                _as_int(row.get("pin_sport_id")) or
                _as_int(row.get("sport_id"))
            )
            if pin_sport_id is not None and row_pin_sport is not None and row_pin_sport != pin_sport_id:
                continue

            row_pin_league = (
                _as_int(row.get("pinnacle_league_id")) or
                _as_int(row.get("pinnacle_id")) or
                _as_int(row.get("league_id"))
            )

            # Se tenho ID de liga dos dois lados, tento bater por ID
            if pin_league_id is not None and row_pin_league is not None and row_pin_league != pin_league_id:
                continue

            # Se não tenho ID de liga, tento casar por nome normalizado
            if pin_league_id is None and league_name_norm:
                row_name = str(
                    row.get("pinnacle_name") or
                    row.get("pin_name") or
                    row.get("name") or ""
                ).strip()
                if row_name:
                    if _norm(row_name) != league_name_norm:
                        continue

            esnet_idc = (
                _as_int(row.get("esnet_league_id")) or
                _as_int(row.get("esnet_id")) or
                _as_int(row.get("idcampeonato"))
            )
            if esnet_idc:
                idcs.append(esnet_idc)

        dedup = sorted({i for i in idcs if i is not None and i > 0})
        if dedup:
            logger.info(
                "[league_map] match meta(p_sport=%s, p_league=%s, name=%s) → idcs=%s",
                pin_sport_id, pin_league_id, league_name, dedup
            )
        return dedup

    # --------- Aliases ----------
    def _maxscore_with_aliases(self, pin: str, cand: str, aliases: List[str]) -> int:
        base = [_best_ratio(pin, cand)] + [_best_ratio(al, cand) for al in aliases]
        return max(base)

    def _flush_aliases_if_needed(self, force: bool = False) -> None:
        now = time.time()
        if not self._aliases_dirty:
            return
        if (not force) and (now - self._aliases_last_save) < self._aliases_save_min_interval_sec:
            return
        try:
            _save_json(self.aliases_path, self.aliases)
            self._aliases_last_save = now
            self._aliases_dirty = False
        except Exception:
            logger.exception("[aliases] Falha ao salvar JSON de aliases")

    def _promote_alias(self, pin_norm: str, esnet_raw: str) -> None:
        teams = self.aliases.setdefault("teams", {})
        arr_prev = teams.get(pin_norm, [])
        arr = list(dict.fromkeys(arr_prev))
        esn_norm = _norm_team(esnet_raw)
        if any(_norm_team(x) == esn_norm for x in arr):
            return
        arr.insert(0, esnet_raw.strip())
        if self._aliases_max_per_team and len(arr) > self._aliases_max_per_team:
            arr = arr[:self._aliases_max_per_team]
        teams[pin_norm] = arr
        self._aliases_dirty = True
        logger.info("[aliases] Aprendido definitivo: pin_norm='%s' → '%s'", pin_norm, esnet_raw)

        pend = self.aliases.setdefault("pending", {}).get(pin_norm, {})
        if esn_norm in pend:
            pend.pop(esn_norm, None)
            self._aliases_dirty = True

    def _maybe_learn_alias(
        self,
        pin: str,
        esnet: Optional[str],
        *,
        score: Optional[int] = None,
        unique: bool = False,
    ) -> None:
        if not esnet:
            return

        pin_norm = _norm_team(pin).strip()
        esnet_raw = (esnet or "").strip()
        esnet_norm = _norm_team(esnet_raw).strip()

        if not pin_norm or not esnet_norm or pin_norm == esnet_norm:
            return

        sc = int(score) if score is not None else 0
        min_strict = int(NAME_MATCH_MIN_SCORE)

        if sc >= self._aliases_min_learn_score or (unique and sc >= min_strict):
            self._promote_alias(pin_norm, esnet_raw)
            self._flush_aliases_if_needed()
            return

        pending_root = self.aliases.setdefault("pending", {})
        pend_for_team = pending_root.setdefault(pin_norm, {})
        pend_entry = pend_for_team.get(esnet_norm, {"raw": esnet_raw, "count": 0, "last_seen": None})

        pend_entry["raw"] = esnet_raw
        pend_entry["count"] = int(pend_entry.get("count", 0)) + 1
        pend_entry["last_seen"] = datetime.utcnow().replace(tzinfo=timezone.utc).isoformat()

        pend_for_team[esnet_norm] = pend_entry
        self._aliases_dirty = True

        if int(pend_entry["count"]) >= self._aliases_promote_seen:
            self._promote_alias(pin_norm, esnet_raw)

        self._flush_aliases_if_needed()

    # --------- Matching principal ----------
    def _match_by_names(
        self,
        pin_home: str,
        pin_away: str,
        cands: List[Dict[str, Any]],
        sport_id: int
    ) -> Tuple[Optional[Dict[str, Any]], int, List[Dict[str, Any]]]:
        MIN_STRICT = int(NAME_MATCH_MIN_SCORE)

        ah = self.aliases.get("teams", {}).get(_norm_team(pin_home), [])
        aa = self.aliases.get("teams", {}).get(_norm_team(pin_away), [])

        best, best_score, best_swapped = None, -1, False
        top_scores: List[Tuple[int, bool, Dict[str, Any]]] = []

        for c in cands:
            h, a = c.get("home") or "", c.get("away") or ""
            s_hh = self._maxscore_with_aliases(pin_home, h, ah)
            s_aa = self._maxscore_with_aliases(pin_away, a, aa)
            s1 = min(s_hh, s_aa)

            s_ha = self._maxscore_with_aliases(pin_home, a, ah)
            s_ah = self._maxscore_with_aliases(pin_away, h, aa)
            s2 = min(s_ha, s_ah)

            if s2 > s1:
                sc, swapped = s2, True
            else:
                sc, swapped = s1, False

            top_scores.append((sc, swapped, c))
            if sc > best_score:
                best, best_score, best_swapped = c, sc, swapped

        top_scores.sort(key=lambda t: t[0], reverse=True)
        top_dbg = [{"score": int(s), "swapped": sw, "home": cc.get("home"), "away": cc.get("away"),
                    "idc": cc.get("idcampeonato"), "pid": cc.get("idpartida")} for s, sw, cc in top_scores[:5]]

        if best and best_score >= MIN_STRICT:
            cand = dict(best)
            if best_swapped:
                cand["home"], cand["away"] = cand.get("away"), cand.get("home")

            cand["_match_score"] = int(best_score)
            logger.info("[match] Sucesso: score=%s idc=%s pid=%s", best_score, cand.get("idcampeonato"), cand.get("idpartida"))
            return cand, int(best_score), top_dbg

        logger.warning("[match] Sem match. best_score=%s cands=%s", best_score, len(cands))
        return None, int(max(0, best_score)), top_dbg

    # --------- Mercados + meta ----------
    def _fetch_event_markets_and_meta(
        self, sport_id: int, idc: int, pid: int
    ) -> Tuple["EsNetMarketResult", Dict[str, Any], Optional[str]]:
        url = url_event(sport_id, idc, pid)
        html = self._get(url)

        out = EsNetMarketResult()
        meta: Dict[str, Any] = {}

        if not html:
            return out, meta, None

        try:
            full_snap = parse_esportenet_event_html(html)
            out.raw_snapshot = _esnet_snapshot_to_dict(full_snap)
            _audit_raw_snapshot(pid, idc, sport_id, full_snap)
        except Exception:
            logger.exception("[markets] Falha ao parsear snapshot completo do evento")

        def _safe_update(fn, label: str) -> None:
            try:
                res = fn(html) or {}
                if res:
                    out.odds.update(res)
                    logger.info("[esnet][canon] %s ok | pid=%s | keys=%s", label, pid, list(res.keys())[:20])
                else:
                    logger.info("[esnet][canon] %s vazio | pid=%s", label, pid)
            except Exception as e:
                logger.exception("[esnet][canon] %s falhou | pid=%s | err=%s", label, pid, e)

        _safe_update(parse_moneyline_from_event, "moneyline")
        _safe_update(parse_totals_from_event, "totals")
        _safe_update(parse_team_totals_from_event, "team_totals")
        _safe_update(parse_btts_from_event, "btts")
        _safe_update(parse_spreads_from_event, "spreads")
        _safe_update(parse_corners_totals_from_event, "corners_totals")
        _safe_update(parse_corners_1x2_from_event, "corners_1x2")

        logger.info("[esnet][canon] pid=%s | canonical_keys_total=%s | top_keys=%s",
                    pid, len(out.odds), list(out.odds.keys())[:50])

        try:
            meta = _extract_meta_from_event(html) or {}
        except Exception:
            meta = {}

        return out, meta, html

    # ---------- API pública ----------
    def probe(self, meta: Dict[str, Any], items: List[Any], *, sport_id: int = 102) -> EsNetProbeResult:
        """
        Resolve um evento EsporteNet para um snapshot Pinnacle.

        IMPORTANTE (fix 2025-11-24):
        - Se `items` vierem do Orchestrator (snapshot cache / forced pid),
          usamos esses candidatos diretamente e NÃO fazemos scan global.
        """
        starts = meta.get("starts")
        if not isinstance(starts, datetime):
            starts = datetime.utcnow().replace(tzinfo=timezone.utc)

        event_id = meta.get("event_id")
        cached_match = self._get_cached_match(event_id, int(sport_id))

        candidates: List[Dict[str, Any]] = []
        day_url: Optional[str] = None
        idc_first: Optional[int] = None

        best: Optional[Dict[str, Any]] = None
        best_score: int = 0
        top_dbg: List[Dict[str, Any]] = []
        idc: Optional[int] = None
        pid: Optional[int] = None

        # ------------------------------------------------------------------
        # 0) Cache-hit do bridge (mais forte que items)
        # ------------------------------------------------------------------
        if cached_match is not None:
            idc = int(cached_match["idc"])
            pid = int(cached_match["pid"])
            day_url = cached_match.get("day_url")
            idc_first = idc
            best_score = int(cached_match.get("match_score", NAME_MATCH_MIN_SCORE))
            top_dbg = cached_match.get("top_dbg") or []
            best = {
                "idcampeonato": idc,
                "idpartida": pid,
                "home": cached_match.get("home"),
                "away": cached_match.get("away"),
                "_match_score": best_score,
                "country_name": cached_match.get("country_name"),
                "league_name": cached_match.get("league_name"),
                "markets_url": cached_match.get("markets_url"),
                "markets_count": cached_match.get("markets_count"),
            }
            logger.info("[probe] cache hit event_id=%s → idc=%s pid=%s", event_id, idc, pid)

        # ------------------------------------------------------------------
        # 1) Se o Orchestrator passou `items`, respeita eles.
        #    (snapshot cache / forced pid / liga filtrada)
        # ------------------------------------------------------------------
        if best is None and items:
            # normaliza items → list[dict]
            try:
                candidates = [dict(x) if isinstance(x, dict) else dict(getattr(x, "raw", {}) or {}) for x in items]
            except Exception:
                candidates = [x for x in items if isinstance(x, dict)]

            # remove vazios
            candidates = [c for c in candidates if c]

            if candidates:
                # tenta inferir idc_first/day_url do primeiro item
                try:
                    idc_first = int(candidates[0].get("idcampeonato") or 0) or None
                except Exception:
                    idc_first = None

                if idc_first:
                    day_url = url_day(int(sport_id), idc_first, ts_ms=int(time.time() * 1000))

                # aplica filtro meta (não atrapalha se já veio filtrado)
                candidates = self._filter_candidates_by_meta(candidates, meta)

                # se for candidato forçado (1 item com pid), aceita direto
                if len(candidates) == 1:
                    only = candidates[0]
                    try:
                        pid_tmp = int(only.get("pid") or only.get("idpartida") or 0)
                    except Exception:
                        pid_tmp = 0
                    try:
                        idc_tmp = int(only.get("idcampeonato") or 0)
                    except Exception:
                        idc_tmp = 0

                    if pid_tmp > 0 and idc_tmp > 0:
                        best = dict(only)
                        best_score = int(only.get("_match_score") or 100)
                        best["_match_score"] = best_score
                        pid = pid_tmp
                        idc = idc_tmp
                        top_dbg = [{
                            "score": best_score,
                            "swapped": False,
                            "home": best.get("home"),
                            "away": best.get("away"),
                            "idc": idc,
                            "pid": pid,
                        }]
                        logger.info(
                            "[probe] usando candidato forçado | pid=%s idc=%s score=%s",
                            pid, idc, best_score
                        )
                # se não é forçado, faz match normal sobre os items
                if best is None:
                    best, best_score, top_dbg = self._match_by_names(
                        str(meta.get("home", "")),
                        str(meta.get("away", "")),
                        candidates,
                        int(sport_id),
                    )
                    if not best:
                        return EsNetProbeResult(
                            found=False,
                            idcampeonato=idc_first,
                            day_url=day_url,
                            debug={"reason": "sem_match_items", "top": top_dbg}
                        )
                    pid = int(best["idpartida"])
                    idc = int(best["idcampeonato"])

        # ------------------------------------------------------------------
        # 2) Fluxo antigo (scan) — só se NÃO havia cache e NÃO havia items
        # ------------------------------------------------------------------
        if best is None:
            if int(sport_id) == 102:
                # Soccer: mantém lógica por weekday
                idc_val = self.idcampeonato_for_date(starts)
                day_url = url_day(102, idc_val, ts_ms=int(time.time() * 1000))
                candidates = self._fetch_day_candidates(102, idc_val)
                idc_first = idc_val
            else:
                # 2.a) Prioridade 1: allowed_idcampeonatos vindos do meta
                preferred_idcs = _coerce_int_list(meta.get("allowed_idcampeonatos") or meta.get("allowed_idc"))
                preferred_idcs = [i for i in preferred_idcs if i > 0]

                if preferred_idcs:
                    logger.info(
                        "[probe] non-soccer: usando allowed_idcampeonatos como prioridade | sport=%s idcs=%s",
                        sport_id, preferred_idcs
                    )
                    for idc_val in preferred_idcs:
                        candidates.extend(self._fetch_day_candidates(int(sport_id), idc_val))
                    if preferred_idcs:
                        idc_first = preferred_idcs[0]
                        day_url = url_day(int(sport_id), int(preferred_idcs[0]), ts_ms=int(time.time() * 1000))

                # 2.b) Prioridade 2: tentar resolver via esnet_league_map.json
                if not candidates:
                    mapped_idcs = self._resolve_idcs_from_league_map(meta, int(sport_id))
                    mapped_idcs = [i for i in mapped_idcs if i > 0]
                    if mapped_idcs:
                        logger.info(
                            "[probe] non-soccer: usando league_map como fallback intermediário | sport=%s idcs=%s",
                            sport_id, mapped_idcs
                        )
                        for idc_val in mapped_idcs:
                            candidates.extend(self._fetch_day_candidates(int(sport_id), idc_val))
                        if mapped_idcs:
                            idc_first = idc_first or mapped_idcs[0]
                            day_url = url_day(int(sport_id), int(idc_first), ts_ms=int(time.time() * 1000))

                # 2.c) Fallback final: scan global por todas as ligas do esporte
                if not candidates:
                    leagues = self.list_leagues_for_sport(int(sport_id))
                    for idc_val in leagues:
                        candidates.extend(self._fetch_day_candidates(int(sport_id), idc_val))
                        if idc_first is None:
                            idc_first = idc_val
                    day_url = url_league_index(int(sport_id))

            if not candidates:
                return EsNetProbeResult(
                    found=False,
                    idcampeonato=idc_first,
                    day_url=day_url,
                    debug={"reason": "lista_vazia"}
                )

            candidates = self._filter_candidates_by_meta(candidates, meta)

            best, best_score, top_dbg = self._match_by_names(
                str(meta.get("home", "")),
                str(meta.get("away", "")),
                candidates,
                int(sport_id),
            )
            if not best:
                return EsNetProbeResult(
                    found=False,
                    idcampeonato=idc_first,
                    day_url=day_url,
                    debug={"reason": "sem_match", "top": top_dbg}
                )

            pid = int(best["idpartida"])
            idc = int(best["idcampeonato"])

        # ------------------------------------------------------------------
        # 3) Com best resolvido, coleta mercados/metadata/logos
        # ------------------------------------------------------------------
        event_url = url_event(int(sport_id), idc, pid)
        markets, meta_from_page, event_html = self._fetch_event_markets_and_meta(int(sport_id), idc, pid)

        home_logo = away_logo = None
        if event_html:
            hl1, al1 = _extract_event_logos_from_event_html(event_html)
            home_logo = hl1
            away_logo = al1

        # URL do dia para logos (sempre normaliza relativo)
        day_url_for_logos = url_day(int(sport_id), idc, ts_ms=int(time.time() * 1000))
        if (home_logo is None) or (away_logo is None):
            day_html = self._get(day_url_for_logos)
            if day_html:
                hl2, al2 = _extract_event_logos_from_day_html(day_html, pid)
                home_logo = home_logo or hl2
                away_logo = away_logo or al2

        m: Dict[str, Any] = dict(meta_from_page or {})
        if best:
            m["home"] = best.get("home")
            m["away"] = best.get("away")
            if best.get("league_name"):
                m["league_name"] = best["league_name"]
            if best.get("country_name"):
                m["country_name"] = best["country_name"]

        event_time_utc = None
        try:
            st = meta.get("starts")
            if isinstance(st, datetime):
                if st.tzinfo is None:
                    st = st.replace(tzinfo=timezone.utc)
                event_time_utc = st.astimezone(timezone.utc).isoformat()
        except Exception:
            pass

        if event_time_utc:
            m["event_time_utc"] = event_time_utc

        m = _augment_country_meta(m)
        m["tz"] = ESNET_LOCAL_TZ
        m["scrape_ts"] = datetime.utcnow().replace(tzinfo=timezone.utc).isoformat()
        m["sport_id"] = int(sport_id)  # <<< ajuda normalizer v2 (esnet_sport_ids)
        if home_logo:
            m["home_logo"] = home_logo
        if away_logo:
            m["away_logo"] = away_logo

        # ------------------------------------------------------------------
        # 4) Monta e salva snapshot por event_id (se existir)
        # ------------------------------------------------------------------
        if event_id is not None:
            try:
                starts_utc_iso = event_time_utc

                ml_home = markets.odds.get(("money_line", "", "home"))
                ml_draw = markets.odds.get(("money_line", "", "draw"))
                ml_away = markets.odds.get(("money_line", "", "away"))

                money_line_map = {}
                winner_map = {}

                if ml_home is not None:
                    money_line_map["home"] = float(ml_home)
                    winner_map["casa"] = float(ml_home)
                if ml_draw is not None:
                    money_line_map["draw"] = float(ml_draw)
                    winner_map["empate"] = float(ml_draw)
                if ml_away is not None:
                    money_line_map["away"] = float(ml_away)
                    winner_map["fora"] = float(ml_away)

                totals_map: Dict[str, Dict[str, float]] = {}
                team_totals_map: Dict[str, Dict[str, Dict[str, float]]] = {"casa": {}, "fora": {}}

                def _side_key(sel_raw: str) -> Optional[str]:
                    s = (sel_raw or "").strip().lower()
                    if s in ("over", "mais", "acima"):
                        return "mais"
                    if s in ("under", "menos", "abaixo"):
                        return "menos"
                    return None

                for (mkt_key, line_key, sel_key), price in markets.odds.items():
                    mk = str(mkt_key)
                    ln = str(line_key)
                    skey = _side_key(sel_key)

                    if mk == "totals" and skey:
                        totals_map.setdefault(ln, {})[skey] = float(price)
                    elif mk in ("team_total_home", "team_total_away") and skey:
                        team_bucket = "casa" if mk == "team_total_home" else "fora"
                        team_totals_map[team_bucket].setdefault(ln, {})[skey] = float(price)

                snapshot = {
                    "ts": time.time(),
                    "event": {
                        "event_id": int(event_id),
                        "home": m.get("home"),
                        "away": m.get("away"),
                        "starts": starts_utc_iso,
                        "league_name": m.get("league_name"),
                        "country_name": m.get("country_name"),
                    },
                    "markets": dict(markets.odds),
                    "money_line": money_line_map,
                    "winner": winner_map,
                    "totals": totals_map,
                    "team_totals": team_totals_map,
                    "logos": {"home": home_logo, "away": away_logo},
                    "bridge": {"sport_id": int(sport_id), "idc": int(idc), "pid": int(pid)},
                    "source": {"event_url": event_url, "day_url": day_url_for_logos},
                    "raw_markets": markets.raw_snapshot or {},
                }

                raw_mkts = (markets.raw_snapshot or {}).get("markets") or []
                logger.info("[snapshot][raw] event_id=%s pid=%s | raw_markets_saved=%s", event_id, pid, len(raw_mkts))

                self.set_snapshot_for_event(int(event_id), snapshot)
            except Exception:
                logger.exception("[snapshot] falha ao construir snapshot")

        # ------------------------------------------------------------------
        # 5) Grava cache de match (se era match novo)
        # ------------------------------------------------------------------
        if best is not None and cached_match is None:
            self._set_cached_match(
                event_id=event_id,
                sport_id=int(sport_id),
                idc=idc,
                pid=pid,
                best=best,
                day_url=day_url_for_logos,
                meta=m,
                home_logo=home_logo,
                away_logo=away_logo,
                best_score=best_score,
                top_dbg=top_dbg,
            )

        result = EsNetProbeResult(
            found=True,
            event_url=event_url,
            day_url=day_url_for_logos,
            url=event_url,
            idcampeonato=idc,
            idpartida=pid,
            home=m.get("home"),
            away=m.get("away"),
            markets=markets,
            meta=m,
            debug={
                "matched_from": {
                    "home": (best or {}).get("home"),
                    "away": (best or {}).get("away"),
                    "score": (best or {}).get("_match_score", None),
                },
                "day_url": day_url_for_logos,
                "sport_id": int(sport_id),
            },
        )
        result.home_logo = home_logo
        result.away_logo = away_logo
        return result

    # --------- utilidades públicas ----------
    def idcampeonato_for_date(self, dt: datetime) -> int:
        key = _weekday_name_local(dt).lower()
        code = int(
            self.weekday_codes.get(key)
            or (self.league_map.get("weekdays") or {}).get(key)
            or WEEKDAY_CODES_FALLBACK.get(key)
            or 0
        )
        logger.debug("[weekday] %s → idcampeonato=%s", key, code)
        return code

    @staticmethod
    def format_event_url(meta: dict, *, sport_id: Optional[int] = None) -> Optional[str]:
        from urllib.parse import urlencode
        idc = meta.get("idcampeonato") or (meta.get("meta") or {}).get("idcampeonato")
        pid = meta.get("idpartida") or (meta.get("meta") or {}).get("idpartida")
        sid = sport_id or meta.get("sport_id") or (meta.get("meta") or {}).get("sport_id") or 102
        if not (idc and pid):
            return None
        return f"{ESNET_BASE}/Apostas.aspx?" + urlencode({"idesporte": int(sid), "idcampeonato": int(idc), "idpartida": int(pid)})

    @staticmethod
    def compare_markets_to_items(markets: EsNetMarketResult, items: List[Any]) -> List[Dict[str, Any]]:
        odds = markets.odds
        out: List[Dict[str, Any]] = []

        def _fmt_try_line(raw: str) -> str:
            try:
                return _fmt_line(float(raw.replace(",", ".").replace("−", "-")))
            except Exception:
                return raw

        def _equivalent(mkt: str, line: str, sel: str) -> Tuple[Optional[Tuple[str, str, str]], Optional[dict]]:
            mkt_raw = (mkt or "").strip().lower()
            ln = (line or "").strip().replace("−", "-")
            sel_raw = (sel or "").strip().lower()

            if sel_raw in {"casa","mandante","1"}:
                sel_norm = "home"
            elif sel_raw in {"fora","visitante","2"}:
                sel_norm = "away"
            else:
                sel_norm = sel_raw

            mkt_norm = {
                "spreads": "spread",
                "ah": "spread",
                "asian handicap": "spread",
                "asian_handicap": "spread",
                "handicap": "spread",
                "handicap asiatico": "spread",
                "handicap asiático": "spread",
            }.get(mkt_raw, mkt_raw)

            key = (mkt_norm, ln, sel_norm)
            if key in odds:
                return key, None

            if mkt_norm == "spread":
                if ln.startswith("+") and ("spread", ln[1:], sel_norm) in odds:
                    return ("spread", ln[1:], sel_norm), None
                if (ln and ln[0].isdigit()) and ("spread", f"+{ln}", sel_norm) in odds:
                    return ("spread", f"+{ln}", sel_norm), None

            if mkt_norm == "spread" and sel_norm in {"home","away"}:
                ln_l = ln.lower()
                if ln_l in {"0","0.0","0,0","+0","-0","+0.0","-0.0","pk","pick","pickem"}:
                    eq = ("spread", "0", sel_norm)
                    if eq in odds:
                        return eq, {"market":"dnb","line":"0","sel":sel_norm}
                if ln_l in {"+0.5","+0,5","0.5","0,5"}:
                    for alias_ln in ("+0.5","0.5"):
                        eq = ("spread", alias_ln, sel_norm)
                        if eq in odds:
                            return eq, {"market":"double_chance","line":"+0.5","sel":sel_norm}
                if ln_l in {"-0.5","-0,5","-0.50"}:
                    eq = ("money_line","", sel_norm)
                    if eq in odds:
                        return eq, {"market":"money_line","line":"","sel":sel_norm}

            if mkt_norm in ("totals","team_total_home","team_total_away"):
                ln2 = _fmt_try_line(ln)
                k2 = (mkt_norm, ln2, sel_norm)
                if k2 in odds:
                    return k2, None

            if mkt_norm == "money_line":
                k2 = ("money_line","", sel_norm)
                if k2 in odds:
                    return k2, None
            return None, None

        for it in items:
            _, mkt, line, sel = it.key
            mkt = str(mkt).lower()
            sel = str(sel or "").lower()
            line_raw = "" if line is None else str(line)
            key, equiv = _equivalent(mkt, line_raw, sel)
            es = odds.get(key) if key else None
            nvp = getattr(it, "nvp", None)
            rec = {
                "market": mkt,
                "line": key[1] if key else line_raw,
                "sel": sel,
                "esnet": es,
                "nvp": nvp,
                "above": (float(es) >= float(nvp)) if (es is not None and nvp is not None) else None,
                "norm_key": key,
            }
            if equiv:
                rec["equiv"] = equiv
            out.append(rec)
        return out
