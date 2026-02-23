# -*- coding: utf-8 -*-
"""
normalizer.py
-------------
Normalização de mercados Pinnacle/EsporteNet para o CERCO.

Alinhado ao markets_mapping.json:
- As famílias canônicas são os market_keys do JSON
  (ex.: moneyline_ft, totals_goals_ft, team_total_points_home_ft, btts_ft...).
- Period markets (moneyline/totals/spreads/team totals) são
  normalizados via "pinnacle.source" do mapping.
- Specials via matching pinnacle.special_names E/OU pinnacle.market_group.
- build_esnet_book aceita tuplas já canônicas OU antigas e traduz para as novas.

Chave canônica:
    (family, line, outcome)

Observação:
- A lógica de crosswalk (ex.: asian_handicap_ft ↔ double_chance_ft / moneyline_ft
  para linhas 0 ou ±0.5) é configurada em markets_mapping.json (campo
  "esportenet_crosswalk") e aplicada em nível de comparador (cerco_comparator).
  O normalizer apenas garante que Pinnacle e EsporteNet produzam livros
  canônicos consistentes para essas famílias.
"""

from __future__ import annotations

import json
import re
import os
import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Tuple, Optional, Mapping, Iterable, Iterator, List

logger = logging.getLogger(__name__)

CanonicalKey = Tuple[str, str, str]  # (family, line, outcome)

# ---------------------------------------------------------------------------
# Helpers genéricos
# ---------------------------------------------------------------------------

def _to_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
        if f != f:  # NaN
            return None
        return f
    except Exception:
        return None


def _fmt_line(v: Any) -> str:
    try:
        f = float(v)
    except Exception:
        return str(v)
    s = f"{f:.2f}".rstrip("0").rstrip(".")
    return s or "0"


def _norm_label(s: Any) -> str:
    if s is None:
        return ""
    t = str(s).strip().casefold()
    t = re.sub(r"\s+", " ", t)
    return t


def _snake(s: str) -> str:
    s = _norm_label(s)
    s = re.sub(r"[^\w]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "unknown"


def _detect_sport_id(raw: Any) -> Optional[int]:
    """
    Tenta detectar sport_id a partir do snapshot Pinnacle/EsporteNet.
    Mantém compatibilidade: se não achar, retorna None.
    """
    if not raw:
        return None

    # objetos com atributo
    for attr in ("sport_id", "sportId", "sport", "sportID", "pinnacle_sport_id"):
        if hasattr(raw, attr):
            try:
                v = getattr(raw, attr)
                if v is not None:
                    return int(v)
            except Exception:
                pass

    # dicts
    if isinstance(raw, dict):
        for k in ("sport_id", "sportId", "sportID", "sport", "pinnacle_sport_id"):
            if k in raw and raw[k] is not None:
                try:
                    return int(raw[k])
                except Exception:
                    pass

        # alguns snapshots vêm como {"event": {...}}
        ev = raw.get("event") if isinstance(raw.get("event"), dict) else None
        if ev:
            for k in ("sport_id", "sportId", "sportID", "sport"):
                if k in ev and ev[k] is not None:
                    try:
                        return int(ev[k])
                    except Exception:
                        pass
    return None


def _passes_filter(spec: Mapping[str, Any], sport_id: Optional[int], *, kind: str) -> bool:
    """
    kind: 'pinnacle' ou 'esportenet'
    Se spec não tem filter ou sport_id não foi detectado => não filtra.
    """
    if sport_id is None:
        return True
    flt = (spec or {}).get("filter")
    if not isinstance(flt, dict):
        return True
    key = "pinnacle_sport_ids" if kind == "pinnacle" else "esnet_sport_ids"
    ids = flt.get(key)
    if not ids:
        return True
    try:
        return int(sport_id) in [int(x) for x in ids]
    except Exception:
        return True


# ---------------------------------------------------------------------------
# Carrega markets_mapping.json
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _load_markets_mapping() -> Dict[str, Any]:
    """
    Carrega o JSON de mapping de mercados.

    Ordem de busca:
    1) Variável de ambiente CERCO_MARKETS_MAPPING_PATH, se definida.
    2) Caminho padrão: <este_pacote>/config/markets_mapping.json
    """
    candidates: List[Path] = []

    env_path = os.getenv("CERCO_MARKETS_MAPPING_PATH")
    if env_path:
        try:
            candidates.append(Path(env_path).expanduser().resolve())
        except Exception:
            logger.warning(
                "[normalizer] CERCO_MARKETS_MAPPING_PATH inválido: %r", env_path
            )

    try:
        here = Path(__file__).resolve()
        candidates.append(here.parent / "config" / "markets_mapping.json")
    except Exception:
        # fallback bem defensivo
        pass

    for cfg_path in candidates:
        try:
            if cfg_path.exists():
                text = cfg_path.read_text(encoding="utf-8")
                data = json.loads(text)
                if isinstance(data, dict):
                    logger.info(
                        "[normalizer] markets_mapping carregado de %s (keys=%d)",
                        cfg_path,
                        len(data),
                    )
                    return data
        except Exception:
            logger.exception(
                "[normalizer] falha ao carregar markets_mapping.json de %s",
                cfg_path,
            )

    logger.warning("[normalizer] markets_mapping.json não encontrado; usando mapping vazio.")
    return {}


def get_markets_mapping() -> Dict[str, Any]:
    """
    API pública: devolve uma cópia do markets_mapping.json carregado.

    Útil para módulos como cerco_comparator aplicarem regras avançadas,
    incluindo esportenet_crosswalk de asian_handicap_ft (opção C: comparação
    direta AH x AH e crosswalk AH x Double Chance/1X2/DNB).
    """
    return dict(_load_markets_mapping())


# ---------------------------------------------------------------------------
# Períodos Pinnacle (support shapes)
# ---------------------------------------------------------------------------

def _get_period_by_number(markets_raw: Any, period_num: int) -> Mapping[str, Any]:
    """
    Retorna period dict para number=period_num.
    Suporta event dict, periods dict/list e shapes num_0/0/1.
    """
    if not markets_raw:
        return {}

    raw = markets_raw
    if isinstance(raw, dict):
        if "periods" in raw and isinstance(raw["periods"], (dict, list)):
            raw = raw["periods"]
        elif "markets" in raw and isinstance(raw["markets"], (dict, list)):
            raw = raw["markets"]

    if isinstance(raw, list):
        for p in raw:
            if isinstance(p, dict):
                n = p.get("number") or p.get("num")
                if str(n) == str(period_num):
                    return p
        for p in raw:
            if isinstance(p, dict):
                return p
        return {}

    if isinstance(raw, dict):
        key_candidates = []
        if period_num == 0:
            key_candidates = ["num_0", "0"]
        elif period_num == 1:
            key_candidates = ["num_1", "1"]
        elif period_num == 2:
            key_candidates = ["num_2", "2"]
        for k in key_candidates:
            if k in raw and isinstance(raw[k], dict):
                return raw[k]
        for _, p in raw.items():
            if isinstance(p, dict):
                n = p.get("number") or p.get("num")
                if str(n) == str(period_num):
                    return p
    return {}


def _iter_line_nodes(raw: Any, *, prefer_key: Optional[str] = None) -> Iterator[Tuple[str, Mapping[str, Any]]]:
    """
    Itera nós de linha (totals/spreads/team totals).
    prefer_key (ex.: "points") prioriza campo no dict quando existir.
    """
    if not raw:
        return iter(())

    if isinstance(raw, dict):
        # Dict com várias linhas
        if any(isinstance(v, dict) for v in raw.values()):
            def gen():
                for k, v in raw.items():
                    if isinstance(v, dict):
                        yield _fmt_line(k), v
            return gen()
        # Dict de linha única
        return iter((("", raw),))

    if isinstance(raw, list):
        def gen2():
            for item in raw:
                if not isinstance(item, dict):
                    continue
                line_key = None
                if prefer_key:
                    line_key = item.get(prefer_key)
                line_key = (
                    line_key
                    or item.get("points")
                    or item.get("line")
                    or item.get("handicap")
                    or item.get("hdp")
                    or ""
                )
                yield _fmt_line(line_key), item
        return gen2()

    return iter(())


def _get_price_side(side_obj: Any) -> Optional[float]:
    if side_obj is None:
        return None
    if isinstance(side_obj, (int, float, str)):
        return _to_float(side_obj)
    if isinstance(side_obj, dict):
        return _to_float(side_obj.get("price") or side_obj.get("decimal") or side_obj.get("odds"))
    return None


# ---------------------------------------------------------------------------
# Specials Pinnacle (special_names + market_group)
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _special_name_to_market_key() -> Dict[str, str]:
    mapping = _load_markets_mapping()
    out: Dict[str, str] = {}
    for mkey, spec in mapping.items():
        pinn = (spec or {}).get("pinnacle") or {}
        names = pinn.get("special_names") or []
        for n in names:
            nn = _norm_label(n)
            if nn:
                out[nn] = mkey
    return out


@lru_cache(maxsize=1)
def _market_group_to_market_key() -> Dict[str, str]:
    mapping = _load_markets_mapping()
    out: Dict[str, str] = {}
    for mkey, spec in mapping.items():
        pinn = (spec or {}).get("pinnacle") or {}
        mg = pinn.get("market_group")
        if mg:
            out[_norm_label(mg)] = mkey
    return out


def _find_market_key_for_special(sp_name: str) -> Optional[str]:
    idx = _special_name_to_market_key()
    n = _norm_label(sp_name)
    if not n:
        return None
    if n in idx:
        return idx[n]
    for k, v in idx.items():
        if k and (k in n or n in k):
            return v
    return None


def _find_market_key_for_market_group(mg_name: str) -> Optional[str]:
    idx = _market_group_to_market_key()
    n = _norm_label(mg_name)
    if not n:
        return None
    if n in idx:
        return idx[n]
    for k, v in idx.items():
        if k and (k in n or n in k):
            return v
    return None


def _map_outcome_from_aliases(label: str, sides_map: Mapping[str, Iterable[str]]) -> Optional[str]:
    nl = _norm_label(label)
    if not nl:
        return None
    for outcome, aliases in sides_map.items():
        for a in aliases or []:
            if _norm_label(a) == nl:
                return str(outcome)
    return None


def _extract_special_lines(sp: Mapping[str, Any]) -> Iterable[Tuple[str, Iterable[Any]]]:
    lines = sp.get("lines")
    if isinstance(lines, list) and lines:
        out = []
        for ln in lines:
            if not isinstance(ln, dict):
                continue
            line_val = ln.get("points") or ln.get("line") or ln.get("handicap") or ln.get("hdp") or ""
            line = _fmt_line(line_val) if line_val not in ("", None) else ""
            prices = (
                ln.get("prices")
                or ln.get("outcomes")
                or ln.get("participants")
                or ln.get("options")
                or []
            )
            out.append((line, prices if isinstance(prices, list) else []))
        if out:
            return out

    line_val = sp.get("points") or sp.get("line") or sp.get("handicap") or sp.get("hdp") or ""
    line = _fmt_line(line_val) if line_val not in ("", None) else ""
    prices = (
        sp.get("prices")
        or sp.get("outcomes")
        or sp.get("participants")
        or sp.get("options")
        or []
    )
    return [(line, prices if isinstance(prices, list) else [])]


def _iter_price_nodes(prices: Iterable[Any]) -> Iterator[Tuple[str, Optional[float]]]:
    for p in prices:
        if p is None:
            continue
        if isinstance(p, dict):
            label = (
                p.get("designation")
                or p.get("name")
                or p.get("participant")
                or p.get("side")
                or p.get("header")
                or p.get("label")
                or ""
            )
            price = _get_price_side(p.get("price") or p.get("decimal") or p.get("odds") or p)
            yield str(label), price
        else:
            price = _get_price_side(p)
            yield "", price


# ---------------------------------------------------------------------------
# Normalização Pinnacle (period markets via mapping + specials)
# ---------------------------------------------------------------------------

def build_pinnacle_book(
    markets_raw: Any,
    specials_raw: Any | None = None,
    *,
    sport_id: Optional[int] = None,
) -> Dict[CanonicalKey, float]:
    book: Dict[CanonicalKey, float] = {}
    mapping = _load_markets_mapping()

    sport_id = sport_id if sport_id is not None else _detect_sport_id(markets_raw)

    # 1) Period markets guiados pelo mapping
    for market_key, spec in (mapping or {}).items():
        if not isinstance(spec, dict):
            continue

        if not _passes_filter(spec, sport_id, kind="pinnacle"):
            continue

        pinn_cfg = spec.get("pinnacle") or {}
        source = pinn_cfg.get("source")
        if not source:
            continue

        # extrai periodo
        period_num = 0
        m = re.search(r"periods\.num_(\d+)", str(source))
        if m:
            period_num = int(m.group(1))
        period = _get_period_by_number(markets_raw, period_num)
        if not period:
            continue

        src_s = str(source)

        if src_s.endswith("money_line") or src_s.endswith("moneyline"):
            _parse_pinnacle_moneyline_from_period(period, book, market_key, pinn_cfg)
        elif src_s.endswith("totals") or ".totals" in src_s:
            _parse_pinnacle_totals_from_period(period, book, market_key, pinn_cfg)
        elif ".team_total.home" in src_s:
            _parse_pinnacle_team_totals_from_period(period, book, market_key, team="home", pinn_cfg=pinn_cfg)
        elif ".team_total.away" in src_s:
            _parse_pinnacle_team_totals_from_period(period, book, market_key, team="away", pinn_cfg=pinn_cfg)
        elif src_s.endswith("spreads") or src_s.endswith("spread") or ".spreads" in src_s:
            unsupported_lines = spec.get("unsupported_lines") or pinn_cfg.get("unsupported_lines") or []
            _parse_pinnacle_spreads_from_period(
                period,
                book,
                market_key,
                pinn_cfg,
                unsupported_lines=unsupported_lines,
            )

    # 2) Specials (agora por special_names OU market_group)
    if specials_raw:
        try:
            _parse_pinnacle_specials(specials_raw, book, sport_id=sport_id)
        except Exception:
            logger.exception("[normalizer] falha parse specials pinnacle")

    return book


def _parse_pinnacle_moneyline_from_period(
    period: Mapping[str, Any],
    book: Dict[CanonicalKey, float],
    market_key: str,
    pinn_cfg: Mapping[str, Any],
) -> None:
    """
    Moneyline robusto:
    - Usa period.moneyline / money_line se existir.
    - Faz fallback para chaves variadas ("Home","Away","Draw","1","2","X"...).
    """
    ml = period.get("moneyline") or period.get("money_line") or period.get("moneyLine")
    if not isinstance(ml, dict):
        return

    sides = pinn_cfg.get("sides") or {}

    ml_keys_norm = {_norm_label(k): k for k in ml.keys()}

    def _pick(alias: str) -> Optional[float]:
        if alias in ml:
            return _get_price_side(ml.get(alias))
        alias_n = _norm_label(alias)
        if alias_n in ml_keys_norm:
            return _get_price_side(ml.get(ml_keys_norm[alias_n]))
        variants = {
            "home": ["home", "casa", "1", "team1"],
            "away": ["away", "fora", "2", "team2"],
            "draw": ["draw", "empate", "x", "tie"],
        }.get(alias_n, [])
        for v in variants:
            if v in ml:
                return _get_price_side(ml.get(v))
            vn = _norm_label(v)
            if vn in ml_keys_norm:
                return _get_price_side(ml.get(ml_keys_norm[vn]))
        return None

    for out, aliases in sides.items():
        alias = next(iter(aliases), None) if isinstance(aliases, list) else None
        if not alias:
            continue

        price = _pick(str(alias))
        if price is None or price <= 1.01:
            continue

        book[(market_key, "", str(out))] = float(price)


def _parse_pinnacle_totals_from_period(
    period: Mapping[str, Any],
    book: Dict[CanonicalKey, float],
    market_key: str,
    pinn_cfg: Mapping[str, Any],
) -> None:
    totals_raw = period.get("totals") or period.get("total") or period.get("game_totals")
    sides = pinn_cfg.get("sides") or {"over": ["over"], "under": ["under"]}
    prefer_key = pinn_cfg.get("line_key") or None

    for line_hint, t in _iter_line_nodes(totals_raw, prefer_key=prefer_key):
        if not isinstance(t, dict):
            continue
        line_val = (
            t.get(prefer_key) if prefer_key else None
        ) or t.get("points") or t.get("line") or t.get("handicap") or line_hint
        line = _fmt_line(line_val)

        for out, aliases in sides.items():
            alias = next(iter(aliases), None) if isinstance(aliases, list) else None
            if not alias:
                continue
            price = _get_price_side(t.get(alias))
            if price is None or price <= 1.01:
                continue
            book[(market_key, line, str(out))] = float(price)


def _parse_pinnacle_team_totals_from_period(
    period: Mapping[str, Any],
    book: Dict[CanonicalKey, float],
    market_key: str,
    *,
    team: str,
    pinn_cfg: Mapping[str, Any],
) -> None:
    tt_raw = (
        period.get("team_total")
        or period.get("team_totals")
        or period.get("teamTotals")
        or {}
    )

    node = None
    if isinstance(tt_raw, dict):
        node = tt_raw.get(team)

    if node is None:
        return

    prefer_key = pinn_cfg.get("line_key") or None

    for line_hint, nd in _iter_line_nodes(node, prefer_key=prefer_key):
        if not isinstance(nd, dict):
            continue
        line_val = (
            nd.get(prefer_key) if prefer_key else None
        ) or nd.get("points") or nd.get("line") or nd.get("handicap") or line_hint
        line = _fmt_line(line_val)

        p_over = _get_price_side(nd.get("over"))
        p_under = _get_price_side(nd.get("under"))

        if p_over is not None and p_over > 1.01:
            book[(market_key, line, "over")] = float(p_over)
        if p_under is not None and p_under > 1.01:
            book[(market_key, line, "under")] = float(p_under)


def _parse_pinnacle_spreads_from_period(
    period: Mapping[str, Any],
    book: Dict[CanonicalKey, float],
    market_key: str,
    pinn_cfg: Mapping[str, Any],
    *,
    unsupported_lines: Iterable[Any] | None = None,
) -> None:
    """
    Spreads (handicaps) Pinnacle.

    Segue estritamente a semântica da API Pinnacle:

      - O campo hdp/handicap/points/line representa SEMPRE
        o handicap do mandante (home / team1).
      - O handicap do visitante é o oposto: hcap_away = -hcap_home.

    Convenção canônica adotada aqui (coincide com o que o site mostra):

      - Para o lado HOME:
            line_canônica = hdp
      - Para o lado AWAY:
            line_canônica = -hdp

    Exemplos (Palmeiras x Flamengo):

      hdp =  0.5, home=1.595, away=2.37
        → ("asian_handicap_ft",  "0.5", "home") = 1.595   (Palmeiras +0.5)
        → ("asian_handicap_ft", "-0.5", "away") = 2.37    (Flamengo -0.5)

      hdp = -0.5, home=3.37, away=1.323
        → ("asian_handicap_ft", "-0.5", "home") = 3.37    (Palmeiras -0.5)
        → ("asian_handicap_ft",  "0.5", "away") = 1.323   (Flamengo +0.5)

    Ou seja, a linha no dicionário canônico é sempre “o que aparece na tela”
    para aquele lado, o que facilita os crossovers (ML x AH +0.5, etc.).
    """
    spreads_raw = period.get("spreads") or period.get("spread")
    if not spreads_raw:
        return

    sides_cfg = pinn_cfg.get("sides") or {"home": ["home"], "away": ["away"]}
    prefer_key = pinn_cfg.get("line_key") or None

    # Descobre qual chave do dict é usada para cada lado (normalmente "home" e "away")
    home_alias = next(iter(sides_cfg.get("home") or []), "home")
    away_alias = next(iter(sides_cfg.get("away") or []), "away")

    ul_set: set[float] = set()
    if unsupported_lines:
        for v in unsupported_lines:
            f = _to_float(v)
            if f is not None:
                ul_set.add(abs(f))

    for line_hint, s in _iter_line_nodes(spreads_raw, prefer_key=prefer_key):
        if not isinstance(s, dict):
            continue

        # hdp do mandante; é o campo “verdadeiro” de handicap da Pinnacle
        line_val_raw = (
            s.get("hdp")
            or (s.get(prefer_key) if prefer_key else None)
            or s.get("points")
            or s.get("line")
            or s.get("handicap")
            or line_hint
        )
        f_hdp = _to_float(line_val_raw)
        if f_hdp is None:
            continue

        # pula linhas explicitamente marcadas como "unsupported" (ex.: 0.25/0.75)
        if ul_set and abs(f_hdp) in ul_set:
            logger.debug(
                "[normalizer] ignorando linha unsupported em spreads: market=%s hdp=%s",
                market_key,
                _fmt_line(f_hdp),
            )
            continue

        # preços brutos do JSON
        price_home = _get_price_side(s.get(home_alias))
        price_away = _get_price_side(s.get(away_alias))

        # HOME: linha = hdp (igual à coluna do mandante na tela)
        if price_home is not None and price_home > 1.01:
            line_home = _fmt_line(f_hdp)
            book[(market_key, line_home, "home")] = float(price_home)

        # AWAY: linha = -hdp (igual à coluna do visitante na tela)
        if price_away is not None and price_away > 1.01:
            line_away = _fmt_line(-f_hdp)
            book[(market_key, line_away, "away")] = float(price_away)




def _parse_pinnacle_specials(
    specials_raw: Any,
    book: Dict[CanonicalKey, float],
    *,
    sport_id: Optional[int] = None,
) -> None:
    mapping = _load_markets_mapping()

    specials_list: List[Mapping[str, Any]] = []
    if isinstance(specials_raw, list):
        specials_list = [sp for sp in specials_raw if isinstance(sp, dict)]
    elif isinstance(specials_raw, dict):
        inner = specials_raw.get("specials")
        if isinstance(inner, list):
            specials_list = [sp for sp in inner if isinstance(sp, dict)]

    for sp in specials_list:
        sp_name = sp.get("name") or sp.get("special_name") or sp.get("title") or ""
        sp_group = sp.get("group") or sp.get("marketGroup") or sp.get("market_group") or sp.get("category") or ""

        # Log bruto de specials da Pinnacle (útil para auditoria de BTTS/Escanteios)
        if sport_id == 1:
            try:
                ev = sp.get("event") or {}
                ev_id = ev.get("id") or ev.get("event_id")
            except Exception:
                ev_id = None

            logger.info(
                "[normalizer][pinnacle_special] sport_id=%s league_id=%s event_id=%s name=%r group=%r bet_type=%r",
                sport_id,
                sp.get("league_id"),
                ev_id,
                sp_name,
                sp_group,
                sp.get("bet_type"),
            )


        market_key = _find_market_key_for_special(str(sp_name)) or _find_market_key_for_market_group(str(sp_group))
        spec_cfg = mapping.get(market_key) if market_key else None

        # respeita filtro por esporte para specials também
        if spec_cfg and not _passes_filter(spec_cfg, sport_id, kind="pinnacle"):
            continue

        pinn_cfg = (spec_cfg or {}).get("pinnacle") or {}
        sides_map = pinn_cfg.get("sides") or {}

        if not market_key:
            # útil para detectar BTTS/Corners/etc que ainda não têm mapping canônico
            logger.debug(
                "[normalizer] special sem mapping canônico: name=%r group=%r",
                sp_name,
                sp_group,
            )

        family = market_key or f"special_{_snake(str(sp_name or sp_group))}"

        for line, prices in _extract_special_lines(sp):
            for label, price in _iter_price_nodes(prices):
                if price is None or price <= 1.01:
                    continue

                outcome: Optional[str] = None

                if isinstance(sides_map, dict) and sides_map:
                    outcome = _map_outcome_from_aliases(label, sides_map)

                if outcome is None:
                    nl = _norm_label(label)
                    if nl in ("over", "mais de", "acima de"):
                        outcome = "over"
                    elif nl in ("under", "menos de", "abaixo de"):
                        outcome = "under"
                    elif nl in ("yes", "sim", "s"):
                        outcome = "yes"
                    elif nl in ("no", "não", "nao", "n"):
                        outcome = "no"
                    elif nl in ("home", "casa", "1"):
                        outcome = "home"
                    elif nl in ("away", "fora", "2"):
                        outcome = "away"
                    elif nl in ("draw", "empate", "x"):
                        outcome = "draw"

                if outcome is None:
                    outcome = _snake(label) if label else "unknown"

                book[(str(family), str(line or ""), str(outcome))] = float(price)


# ---------------------------------------------------------------------------
# Normalização EsporteNet (com compatibilidade legado->novo)
# ---------------------------------------------------------------------------

@dataclass
class EsnetSnapshot:
    odds: Dict[CanonicalKey, float]


# tradução simples de famílias antigas para novas (compatibilidade)
_LEGACY_TO_NEW_FAMILY: Dict[str, str] = {
    "money_line": "moneyline_ft",
    "totals": "totals_goals_ft",
    "spread": "asian_handicap_ft",

    "team_total_home": "team_total_goals_home_ft",
    "team_total_away": "team_total_goals_away_ft",

    "corners_total": "total_corners_ft",
    "corners_totals": "total_corners_ft",
    "corners_1x2": "corners_1x2_ft",
}


def build_esnet_book(
    esnet_markets: EsnetSnapshot | Any,
    *,
    sport_id: Optional[int] = None,
) -> Dict[CanonicalKey, float]:
    if hasattr(esnet_markets, "markets") and getattr(esnet_markets.markets, "odds", None) is not None:
        odds = getattr(esnet_markets.markets, "odds", None)
    else:
        odds = getattr(esnet_markets, "odds", None)

    if not isinstance(odds, dict):
        return {}

    mapping = _load_markets_mapping()
    sport_id = sport_id if sport_id is not None else _detect_sport_id(esnet_markets)

    out: Dict[CanonicalKey, float] = {}
    for key, val in odds.items():
        if not isinstance(key, tuple) or len(key) != 3:
            continue
        family, line, outcome = key
        price = _to_float(val)
        if price is None or price <= 1.01:
            continue

        fam_s = str(family)
        line_s = str(line)
        out_s = str(outcome)

        # compat legado -> novo
        fam_s = _LEGACY_TO_NEW_FAMILY.get(fam_s, fam_s)

        # aplica filtro por esporte quando existir no mapping
        spec = mapping.get(fam_s)
        if isinstance(spec, dict) and not _passes_filter(spec, sport_id, kind="esportenet"):
            continue

        out[(fam_s, line_s, out_s)] = float(price)

    return out


# ---------------------------------------------------------------------------
# Helper principal para o CERCO Engine
# ---------------------------------------------------------------------------

def build_canonical_books(
    markets_raw_pinnacle: Any,
    markets_raw_esnet: Any,
    specials_raw_pinnacle: Any | None = None,
) -> Tuple[Dict[CanonicalKey, float], Dict[CanonicalKey, float]]:

    def _families(book: Dict[CanonicalKey, float]) -> list[str]:
        fams = sorted({str(k[0]) for k in (book or {}).keys() if isinstance(k, tuple) and len(k) == 3})
        return fams

    def _summarize_book(book: Dict[CanonicalKey, float]) -> Dict[str, Dict[str, int]]:
        """
        Pequeno resumo por família para auditoria:
        - count: total de chaves (family,line,outcome)
        - lines: número de linhas distintas naquela família
        """
        stats: Dict[str, Dict[str, Any]] = {}
        for fam, line, _out in book.keys():
            fam_s = str(fam)
            d = stats.setdefault(fam_s, {"count": 0, "lines": set()})
            d["count"] += 1
            d["lines"].add(str(line))
        return {
            fam: {"count": int(d["count"]), "lines": len(d["lines"])}
            for fam, d in stats.items()
        }

    def _extract_esnet_odds_dict(raw_esnet: Any) -> Optional[dict]:
        try:
            if hasattr(raw_esnet, "markets") and getattr(raw_esnet.markets, "odds", None) is not None:
                odds = getattr(raw_esnet.markets, "odds", None)
            else:
                odds = getattr(raw_esnet, "odds", None)
            return odds if isinstance(odds, dict) else None
        except Exception:
            return None

    # detecta sport_id uma vez e repassa
    sport_id_pinn = _detect_sport_id(markets_raw_pinnacle)
    sport_id_esnet = _detect_sport_id(markets_raw_esnet) or sport_id_pinn

    try:
        odds_raw = _extract_esnet_odds_dict(markets_raw_esnet) or {}
        raw_keys = list(odds_raw.keys()) if isinstance(odds_raw, dict) else []
        logger.info(
            "[norm_in][esnet_raw] count_markets_raw=%d top10_raw_keys=%s",
            len(raw_keys),
            raw_keys[:10],
        )
    except Exception:
        logger.exception("[norm_in][esnet_raw] falha ao logar snapshot bruto EsNet")

    pinn_book = build_pinnacle_book(
        markets_raw_pinnacle,
        specials_raw_pinnacle,
        sport_id=sport_id_pinn,
    )
    esnet_book = build_esnet_book(
        markets_raw_esnet,
        sport_id=sport_id_esnet,
    )

    try:
        count_markets_norm_pinn = len(pinn_book)
        count_markets_norm_esnet = len(esnet_book)

        families_pinn = _families(pinn_book)
        families_esnet = _families(esnet_book)

        logger.info(
            "[norm_out][pinnacle] count_markets_norm=%s families_presentes=%s",
            count_markets_norm_pinn, families_pinn
        )
        logger.info(
            "[norm_out][esnet] count_markets_norm=%s families_presentes=%s",
            count_markets_norm_esnet, families_esnet
        )

        # Logs detalhados para auditoria de desenvolvedor (DEBUG)
        logger.debug(
            "[norm_out][pinnacle][detail] %s",
            _summarize_book(pinn_book),
        )
        logger.debug(
            "[norm_out][esnet][detail] %s",
            _summarize_book(esnet_book),
        )
    except Exception:
        logger.exception("[norm_out][log] falha ao logar auditoria pós-normalização")

    return pinn_book, esnet_book
