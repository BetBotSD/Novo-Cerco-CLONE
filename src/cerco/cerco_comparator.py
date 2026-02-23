# -*- coding: utf-8 -*-
"""
cerco_comparator.py
-------------------
Responsável por transformar os **livros canônicos** (Pinnacle + EsporteNet + futuras casas)
em oportunidades de arbitragem (surebets) com stakes sugeridas.

Agora (refino pós novo markets_mapping.json):
- Só varre mercados com arb.capable=true no markets_mapping.json.
- Respeita arb.min_books (preparado para 3+ casas).
- Pinnacle é obrigatória por padrão (mantém compatibilidade);
  mas o agrupador já suporta N livros.

Compatibilidade:
- Exporta build_books_and_find_arbs e ArbitrageOpportunity
  (esperados pelo cerco_orchestrator.py).

Refino 2025-11-24:
- Famílias canônicas agora são market_keys do markets_mapping.json
  (ex.: moneyline_ft, totals_goals_ft, btts_ft, asian_handicap_ft, etc).
- Comparator ignora mercados fora do mapping ou não "arb.capable".

Refino advanced_patterns:
- Carrega config/arbitrage_rules.json.
- Aplica patterns multi-mercado (ex.: btts_yes_and_over_vs_under),
  reaproveitando _compute_surebet_for_outcomes.

Refino crosswalk asian_handicap_ft:
- Lê asian_handicap_ft.esportenet_crosswalk em markets_mapping.json (via
  normalizer.get_markets_mapping()).
- Gera odds "sintéticas" da EsporteNet para asian_handicap_ft a partir de:
    - double_chance_ft
    - moneyline_ft
  respeitando as regras home_minus_0_5_vs_esnet, home_plus_0_5_vs_esnet etc.

Observação importante (opção C):
- O crosswalk **só entra** quando a EsporteNet NÃO tem diretamente aquela
  chave (family="asian_handicap_ft", line, outcome) no livro canônico.
- Ou seja:
    * Se EsporteNet tem AH direto → usamos AH vs AH normal.
    * Se só Pinnacle tem AH naquela linha/outcome → geramos AH sintético
      via double_chance_ft/moneyline_ft conforme esportenet_crosswalk.

Refino ML x AH +0.5 (cross-family):
- Adiciona padrão explícito:
    * CAS(ML EsporteNet) x FORA +0.5 (Asian Handicap Pinnacle)
    * FORA(ML EsporteNet) x CASA +0.5 (Asian Handicap Pinnacle)
  cobrindo as 3 saídas do jogo com 2 pernas.

Refino ML x AH -0.5 (cross-family, 3 vias):
- Adiciona padrões explícitos:
    * Home(ML) x Draw(ML) x Away -0.5 (AH Pinnacle)
    * Home -0.5 (AH Pinnacle) x Draw(ML) x Away(ML)
    * Home -0.5 (AH Pinnacle) x Draw(ML) x Away -0.5 (AH Pinnacle)
  cobrindo SEMPRE HOME / DRAW / AWAY com pelo menos duas casas diferentes.

Refino totals escanteios (cross-line):
- Implementa padrão genérico para total_corners_ft (escanteios totais):
    * Over linha L em uma casa x Under linha U em outra casa,
      desde que a combinação cubra **todos** os números inteiros possíveis
      de escanteios (sem “buracos” onde ambas possam perder).

Refino totals gols/pontos (cross-line):
- Reaproveita o mesmo padrão genérico de Over/Under cross-line para:
    * totals_goals_ft  (Total de Gols FT)
    * totals_points_ft (Total de Pontos FT – basquete/AF)

Refino ML/DC e AH-0.5/DC (cross-family):
- Adiciona padrões explícitos:

  Moneyline vs Double Chance
    * Home (ML)          x Draw_or_Away (DC)
    * Home_or_Draw (DC)  x Away (ML)

  AH -0.5 vs Double Chance
    * Home -0.5 (AH)     x Draw_or_Away (DC)
    * Home_or_Draw (DC)  x Away -0.5 (AH)

  Sempre cobrindo todos os resultados do jogo, com pernas em casas diferentes.
"""

from __future__ import annotations

import json
import logging
import os
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Tuple, Optional, Mapping

from .normalizer import (
    CanonicalKey,
    build_canonical_books,
    get_markets_mapping,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Edge mínimo global (config_cerco.py)
# ---------------------------------------------------------------------------

try:
    from .config_cerco import CERCO_MIN_EDGE  # type: ignore
except Exception:
    CERCO_MIN_EDGE = 0.005


# ---------------------------------------------------------------------------
# Carrega markets_mapping.json (para filtro arb.capable/min_books)
# ---------------------------------------------------------------------------

def _get_markets_mapping() -> Dict[str, Any]:
    """
    Wrapper para pegar o mapping via normalizer.get_markets_mapping().

    Mantém um snapshot em memória, mas se em algum momento o mapping
    precisar ser recarregado dinamicamente, basta adaptar aqui.
    """
    try:
        data = get_markets_mapping()
        if isinstance(data, dict):
            return data
    except Exception:
        logger.exception("CERCO[map] falha ao obter markets_mapping via normalizer")
    return {}


_MARKETS_MAPPING: Dict[str, Any] = _get_markets_mapping()


def _arb_capable(market_key: str) -> bool:
    spec = _MARKETS_MAPPING.get(market_key) or {}
    arb = (spec.get("arb") or {}) if isinstance(spec, dict) else {}
    return bool(arb.get("capable", False))


def _arb_min_books(market_key: str) -> int:
    spec = _MARKETS_MAPPING.get(market_key) or {}
    arb = (spec.get("arb") or {}) if isinstance(spec, dict) else {}
    try:
        return int(arb.get("min_books", 2))
    except Exception:
        return 2


def _get_ah_crosswalk_spec() -> List[Dict[str, Any]]:
    """
    Atalho para pegar a lista de regras de crosswalk do asian_handicap_ft.

    Espera algo como:
      "asian_handicap_ft": {
        ...
        "esportenet_crosswalk": [
          {
            "when_line_abs_in": [0.5],
            "home_minus_0_5_vs_esnet": { ... },
            ...
          },
          ...
        ]
      }
    """
    spec = _MARKETS_MAPPING.get("asian_handicap_ft") or {}
    if not isinstance(spec, dict):
        return []
    rules = spec.get("esportenet_crosswalk") or []
    if not isinstance(rules, list):
        return []
    return rules


# ---------------------------------------------------------------------------
# Carrega arbitrage_rules.json (advanced_patterns)
# ---------------------------------------------------------------------------

_ADV_RULES_CACHE: Optional[Dict[str, Any]] = None


def _get_arbitrage_rules_path() -> Path:
    """
    Permite override via CERCO_ARBITRAGE_RULES_PATH.
    Default: <este_pacote>/config/arbitrage_rules.json
    """
    env_path = os.getenv("CERCO_ARBITRAGE_RULES_PATH")
    if env_path:
        try:
            return Path(env_path).expanduser().resolve()
        except Exception:
            logger.warning(
                "CERCO[adv] CERCO_ARBITRAGE_RULES_PATH inválido: %r", env_path
            )
    return Path(__file__).resolve().parent / "config" / "arbitrage_rules.json"


def _get_advanced_patterns() -> List[Dict[str, Any]]:
    """
    Carrega arbitrage_rules.json e devolve a lista de advanced_patterns.

    - Se o arquivo não existir ou der erro, devolve lista vazia.
    - Mantém cache em memória para não ficar relendo disco.
    """
    global _ADV_RULES_CACHE

    if _ADV_RULES_CACHE is not None:
        return _ADV_RULES_CACHE.get("advanced_patterns", []) or []

    cfg_path = _get_arbitrage_rules_path()
    try:
        raw = cfg_path.read_text(encoding="utf-8")
        data = json.loads(raw)
        _ADV_RULES_CACHE = data if isinstance(data, dict) else {}
        patterns = _ADV_RULES_CACHE.get("advanced_patterns", []) or []
        logger.info(
            "CERCO[adv] arbitrage_rules carregado de %s (%s advanced_patterns)",
            cfg_path,
            len(patterns),
        )
    except FileNotFoundError:
        logger.info(
            "CERCO[adv] arbitrage_rules.json não encontrado em %s; advanced_patterns desativados.",
            cfg_path,
        )
        _ADV_RULES_CACHE = {"groups": {}, "advanced_patterns": []}
    except Exception as e:
        logger.warning(
            "CERCO[adv] falha ao carregar arbitrage_rules.json em %s: %s",
            cfg_path,
            e,
        )
        _ADV_RULES_CACHE = {"groups": {}, "advanced_patterns": []}

    return _ADV_RULES_CACHE.get("advanced_patterns", []) or []


# ---------------------------------------------------------------------------
# Dataclasses internas
# ---------------------------------------------------------------------------

@dataclass
class ArbitrageLeg:
    """Uma perna de arbitragem (um lado/aposta em uma das casas)."""
    book: str          # "PINNACLE" ou "ESPORTENET" (onde a odd é melhor) ou "BET365"
    outcome: str       # "home", "draw", "away", "over", "under", "yes", "no", etc.
    odd: float         # odd usada na arbitragem (a melhor)
    stake: float       # valor sugerido para esta perna

    pinnacle_odd: Optional[float] = None
    esportenet_odd: Optional[float] = None

    # outros books (3+ casas): {BOOK: odd}
    other_book_odds: Dict[str, float] = field(default_factory=dict)

    @property
    def house(self) -> str:
        return self.book

    @property
    def price(self) -> float:
        return self.odd


@dataclass
class ArbitrageSuggestion:
    """Arbitragem completa em um mercado/família."""
    family: str
    line: str
    legs: List[ArbitrageLeg] = field(default_factory=list)

    base_stake: float = 0.0
    roi: float = 0.0
    profit: float = 0.0
    implied_prob_sum: float = 0.0

    def short_label(self) -> str:
        return f"{self.family} {self.line}".strip()


@dataclass
class ArbitrageOpportunity:
    """
    Estrutura pública compatível com cerco_orchestrator.py / telegram_cerco.
    """
    family: str
    line: str
    legs: List[ArbitrageLeg] = field(default_factory=list)

    edge: float = 0.0
    implied_prob: float = 0.0

    base_stake: float = 0.0
    roi: float = 0.0
    profit: float = 0.0


# ---------------------------------------------------------------------------
# Helpers internos
# ---------------------------------------------------------------------------

def _extract_periods_or_markets_from_pinnacle_raw(markets_raw_pinnacle: Any) -> Any:
    if not isinstance(markets_raw_pinnacle, dict):
        return markets_raw_pinnacle

    if any(k in markets_raw_pinnacle for k in ("num_0", "0", "1")):
        return markets_raw_pinnacle

    if "periods" in markets_raw_pinnacle and isinstance(markets_raw_pinnacle["periods"], (dict, list)):
        return markets_raw_pinnacle["periods"]
    if "markets" in markets_raw_pinnacle and isinstance(markets_raw_pinnacle["markets"], (dict, list)):
        return markets_raw_pinnacle["markets"]

    return markets_raw_pinnacle


def _group_by_family_and_line(
    books: Mapping[str, Dict[CanonicalKey, float]],
) -> Dict[Tuple[str, str], Dict[str, Dict[str, Any]]]:
    """
    Agrupa N livros canônicos em:
      {(family, line): {"outcomes": {outcome: {...}}}}
    """
    grouped: Dict[Tuple[str, str], Dict[str, Dict[str, Any]]] = {}

    def _ensure_slot(fam: str, line: str) -> Dict[str, Dict[str, Any]]:
        key = (fam, line)
        if key not in grouped:
            grouped[key] = {"outcomes": {}}
        return grouped[key]["outcomes"]

    for book_name, book in books.items():
        if not book:
            continue

        for (fam, line, outcome), odd in book.items():
            if odd is None or odd <= 1.01:
                continue

            fam_s = str(fam)
            line_s = str(line)
            out_s = str(outcome)

            outcomes = _ensure_slot(fam_s, line_s)
            slot = outcomes.setdefault(out_s, {
                "best_odd": float(odd),
                "best_book": book_name,
                "book_odds": {},
            })

            # atualiza odds por book
            slot["book_odds"][book_name] = float(odd)

            # atualiza best
            if float(odd) > float(slot["best_odd"]):
                slot["best_odd"] = float(odd)
                slot["best_book"] = book_name

    return grouped


def _debug_log_market_compare(
    family: str,
    line: str,
    outcomes_info: Dict[str, Dict[str, Any]],
) -> None:
    if not logger.isEnabledFor(logging.DEBUG):
        return

    logger.debug("---- CERCO[compare] family=%s line=%s ----", family, line)
    for outcome, info in outcomes_info.items():
        best = info.get("best_odd")
        best_book = info.get("best_book")
        book_odds = info.get("book_odds") or {}
        logger.debug(
            "  outcome=%-12s odds_by_book=%s -> best=%s @%s",
            outcome,
            {k: f"{v:.4f}" for k, v in book_odds.items()},
            f"{best:.4f}" if best else "None",
            best_book,
        )


def _audit_log_family_probabilities(
    family: str,
    line: str,
    outcomes_info: Dict[str, Dict[str, Any]],
    *,
    implied_prob_sum: float,
    edge: float,
    min_edge: float,
) -> None:
    """
    Log especial para auditoria de BTTS e escanteios, mesmo quando NÃO vira arbitragem.

    - family in {"btts_ft", "total_corners_ft"}
    - Loga:
        * livros presentes por outcome
        * melhor odd por outcome
        * implied_prob_sum e edge
    """
    if family not in ("btts_ft", "total_corners_ft"):
        return

    try:
        books_by_outcome: Dict[str, List[str]] = {}
        best_odds_by_outcome: Dict[str, Dict[str, Any]] = {}
        for outcome, info in outcomes_info.items():
            book_odds = (info.get("book_odds") or {})
            books_by_outcome[str(outcome)] = sorted(book_odds.keys())
            best_odds_by_outcome[str(outcome)] = {
                "best_odd": float(info.get("best_odd") or 0.0),
                "best_book": info.get("best_book"),
            }

        logger.info(
            "CERCO[audit] family=%s line=%s | implied=%.6f edge=%.6f min_edge=%.6f | "
            "books_by_outcome=%s | best_odds=%s",
            family,
            line,
            implied_prob_sum,
            edge,
            min_edge,
            books_by_outcome,
            best_odds_by_outcome,
        )
    except Exception:
        # Não deixa nenhum erro de auditoria quebrar o fluxo de arbitragem.
        logger.exception(
            "CERCO[audit] falha ao logar auditoria para family=%s line=%s",
            family,
            line,
        )


def _is_valid_2way_outcomes(outcomes: List[str]) -> bool:
    outs = {str(o) for o in outcomes}
    if "draw" in outs:
        return False
    return True


def _compute_surebet_for_outcomes(
    family: str,
    line: str,
    outcomes_info: Dict[str, Dict[str, Any]],
    *,
    base_stake: float,
    min_edge: float,
) -> Optional[ArbitrageSuggestion]:
    """
    Dado um conjunto de outcomes (home/away[/draw]), já com a melhor odd de cada casa,
    calcula se existe arbitragem (surebet) e, em caso positivo, retorna a sugestão de apostas.

    Regras importantes:
    - Aceita apenas 2 ou 3 outcomes.
    - Em mercados 2-way, exige que o conjunto de outcomes seja válido.
    - A soma das probabilidades implícitas (1/odd) deve ser < 1.
    - A edge (1 - soma_prob) deve ser >= min_edge.
    - As melhores odds escolhidas precisam vir de pelo menos **duas casas diferentes**
      (ex.: PINNACLE e ESPORTENET). Se todas as melhores odds forem da mesma casa,
      não é arbitragem entre casas e o candidato é descartado.
    - NOVO: para asian_handicap_ft com linha ±0.5, se estivermos em 2 vias e
      houver EsporteNet envolvida, bloqueamos (para evitar “falsas” arbitragens
      construídas a partir de equivalências ML/DC sem cobrir o empate).
    """
    num_outcomes = len(outcomes_info or {})
    if num_outcomes < 2 or num_outcomes > 3:
        return None

    if num_outcomes == 2:
        if not _is_valid_2way_outcomes(list(outcomes_info.keys())):
            return None

    inv_sum = 0.0
    for _, info in outcomes_info.items():
        best_odd = float(info["best_odd"])
        if best_odd <= 1.01:
            # Odd muito baixa → ignora
            return None
        inv_sum += 1.0 / best_odd

    # Calcula edge sempre, para poder logar auditoria mesmo sem arbitragem.
    edge = 1.0 - inv_sum

    # Log de auditoria específico para BTTS e escanteios (sempre que chegamos aqui).
    _audit_log_family_probabilities(
        family,
        line,
        outcomes_info,
        implied_prob_sum=inv_sum,
        edge=edge,
        min_edge=min_edge,
    )

    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("  implied_prob_sum=%.6f (must be < 1.0 to arb)", inv_sum)

    # Precisa ser < 1 para existir surebet.
    if inv_sum >= 1.0:
        return None

    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("  edge=%.6f (min_edge=%.6f)", edge, min_edge)

    if edge < min_edge:
        return None

    # Casas usadas pelas melhores odds
    books_used = {
        str(info.get("best_book"))
        for info in outcomes_info.values()
        if info.get("best_book")
    }

    # 🔒 Regra extra: bloquear AH ±0.5 em 2 vias envolvendo EsporteNet
    # (pois aqui normalmente estamos usando equivalências ML/DC e deixaríamos o empate descoberto).
    if (
        num_outcomes == 2
        and str(family) == "asian_handicap_ft"
        and "ESPORTENET" in books_used
    ):
        try:
            line_val = float(str(line))
        except Exception:
            line_val = None

        if line_val is not None and abs(line_val) == 0.5:
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "  descartando AH ±0.5 2-vias com ESNET (family=%s line=%s books=%s)",
                    family,
                    line,
                    books_used,
                )
            return None

    # 🔒 Garantir que a arbitragem use **pelo menos duas casas diferentes**
    if len(books_used) < 2:
        # Todas as melhores odds vieram da mesma casa (ex.: só ESPORTENET ou só PINNACLE);
        # isso não atende ao conceito de arbitragem entre casas → descarta.
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "  descartando family=%s line=%s: todas as melhores odds são da mesma casa: %s",
                family,
                line,
                books_used,
            )
        return None

    # A partir daqui, temos uma surebet válida entre pelo menos duas casas diferentes.
    legs: List[ArbitrageLeg] = []
    total_stake = float(base_stake)

    for outcome, info in outcomes_info.items():
        odd = float(info["best_odd"])
        weight = (1.0 / odd) / inv_sum
        stake = total_stake * weight

        book_odds = info.get("book_odds") or {}
        pinn_odd = book_odds.get("PINNACLE")
        esn_odd = book_odds.get("ESPORTENET")
        other_odds = {k: v for k, v in book_odds.items() if k not in ("PINNACLE", "ESPORTENET")}

        legs.append(
            ArbitrageLeg(
                book=str(info["best_book"]),
                outcome=str(outcome),
                odd=odd,
                stake=round(stake, 2),
                pinnacle_odd=(float(pinn_odd) if pinn_odd else None),
                esportenet_odd=(float(esn_odd) if esn_odd else None),
                other_book_odds=other_odds,
            )
        )

    payout = total_stake / inv_sum
    profit = payout - total_stake
    roi = profit / total_stake if total_stake > 0 else 0.0

    return ArbitrageSuggestion(
        family=family,
        line=line,
        legs=legs,
        base_stake=total_stake,
        roi=roi,
        profit=round(profit, 2),
        implied_prob_sum=inv_sum,
    )


# ---------------------------------------------------------------------------
# Crosswalk asian_handicap_ft -> mercados EsporteNet (double chance, moneyline)
# ---------------------------------------------------------------------------

def _apply_esnet_crosswalk_for_asian_handicap(
    book_pinn: Dict[CanonicalKey, float],
    book_esnet: Dict[CanonicalKey, float],
) -> Dict[CanonicalKey, float]:
    """
    Usa asian_handicap_ft.esportenet_crosswalk em markets_mapping.json para
    gerar odds sintéticas da EsporteNet em asian_handicap_ft, a partir de:
      - double_chance_ft
      - moneyline_ft
    Somente se a EsporteNet NÃO tiver odds diretas para aquele AH linha/outcome.
    """
    if not book_pinn or not book_esnet:
        return book_esnet

    crosswalk_rules = _get_ah_crosswalk_spec()
    if not crosswalk_rules:
        return book_esnet

    # Snapshot dos AH já existentes na EsporteNet (antes do crosswalk).
    esnet_has_ah_keys = {
        (str(fam), str(line), str(outcome))
        for (fam, line, outcome) in book_esnet.keys()
        if str(fam) == "asian_handicap_ft"
    }

    augmented = dict(book_esnet)

    def _get_esnet_odd_from_market_side(
        market: str,
        side: str,
    ) -> Optional[float]:
        """
        Procura qualquer linha em que (family == market and outcome == side)
        e retorna a odd (primeira encontrada). Se houver múltiplas linhas,
        escolhemos a MAIOR odd (mais vantajosa para arbitragem).
        """
        candidates: List[float] = []
        for (fam, line, outcome), odd in book_esnet.items():
            if str(fam) == market and str(outcome) == side:
                try:
                    candidates.append(float(odd))
                except Exception:
                    continue
        if not candidates:
            return None
        return max(candidates)

    for (fam, line, outcome), _pinn_odd in book_pinn.items():
        if str(fam) != "asian_handicap_ft":
            continue

        # Tenta converter a linha do AH em float (ex.: "0.5", "-0.5")
        try:
            line_val = float(str(line))
        except Exception:
            continue

        key_sig = ("asian_handicap_ft", str(line), str(outcome))

        # Se a EsporteNet já tem diretamente esse AH family/line/outcome, não interfere.
        if key_sig in esnet_has_ah_keys:
            continue

        # Aplica regras do crosswalk.
        for rule in crosswalk_rules:
            when_abs = rule.get("when_line_abs_in") or []
            when_eq = rule.get("when_line_in") or []

            # Checa condições de linha
            if when_abs:
                try:
                    if abs(line_val) not in [float(x) for x in when_abs]:
                        continue
                except Exception:
                    continue
            if when_eq:
                try:
                    if line_val not in [float(x) for x in when_eq]:
                        continue
                except Exception:
                    continue

            # Para ±0.5, usamos home_minus_0_5_vs_esnet, away_minus_0_5_vs_esnet, etc.
            cross_cfg: Optional[Dict[str, Any]] = None
            outcome_s = str(outcome)

            if line_val < 0:
                # linha negativa (ex.: -0.5)
                if outcome_s == "home":
                    cross_cfg = rule.get("home_minus_0_5_vs_esnet")
                elif outcome_s == "away":
                    cross_cfg = rule.get("away_minus_0_5_vs_esnet")
            elif line_val > 0:
                # linha positiva (ex.: +0.5)
                if outcome_s == "home":
                    cross_cfg = rule.get("home_plus_0_5_vs_esnet")
                elif outcome_s == "away":
                    cross_cfg = rule.get("away_plus_0_5_vs_esnet")
            else:
                cross_cfg = None

            if not cross_cfg:
                continue

            es_market = cross_cfg.get("use_esnet_market")
            es_side = cross_cfg.get("use_esnet_side")
            if not es_market or not es_side:
                continue

            es_odd = _get_esnet_odd_from_market_side(str(es_market), str(es_side))
            if es_odd is None:
                continue

            new_key: CanonicalKey = ("asian_handicap_ft", str(line), str(outcome))
            # Se já existe um AH sintético com esse key, mantemos a maior odd.
            if new_key in augmented:
                try:
                    if float(es_odd) > float(augmented[new_key]):
                        augmented[new_key] = float(es_odd)
                except Exception:
                    pass
            else:
                augmented[new_key] = float(es_odd)

            logger.debug(
                "CERCO[crosswalk] ESNET AH family=asian_handicap_ft line=%s outcome=%s "
                "via %s/%s odd=%.4f",
                line,
                outcome,
                es_market,
                es_side,
                es_odd,
            )

    return augmented


# ---------------------------------------------------------------------------
# Advanced patterns (multi-mercado) usando arbitrage_rules.json
# ---------------------------------------------------------------------------

def _build_advanced_suggestions(
    grouped: Dict[Tuple[str, str], Dict[str, Dict[str, Any]]],
    *,
    base_stake: float,
) -> List[ArbitrageSuggestion]:
    """
    Gera ArbitrageSuggestion adicionais com base em advanced_patterns
    definidos em config/arbitrage_rules.json.

    Implementação atual:
    - Usa apenas patterns com campo "canonical_market" e "roles"
      (padrões dentro de uma mesma família canônica).
    """
    patterns = _get_advanced_patterns()
    out: List[ArbitrageSuggestion] = []

    if not patterns:
        return out

    for pattern in patterns:
        family = pattern.get("canonical_market")
        if not family:
            continue

        pattern_name = pattern.get("name", "adv")
        min_edge = float(pattern.get("min_edge", 0.0) or 0.0)

        roles_cfg = pattern.get("roles") or {}
        # Ex.: ["yes_and_over", "under"]
        sides: List[str] = [
            r.get("side") for r in roles_cfg.values() if r.get("side")
        ]

        # precisa de pelo menos 2 lados configurados
        if len(sides) < 2:
            logger.debug(
                "CERCO[adv] pattern=%s ignorado (sides insuficientes: %s)",
                pattern_name,
                sides,
            )
            continue

        for (fam, line_sig), slot in grouped.items():
            if fam != family:
                continue

            outcomes_info = (slot or {}).get("outcomes") or {}
            # filtra apenas os sides deste pattern
            filtered: Dict[str, Dict[str, Any]] = {
                side: outcomes_info[side]
                for side in sides
                if side in outcomes_info
            }

            # precisa de pelo menos 2 outcomes presentes naquele evento/linha
            if len(filtered) < 2:
                continue

            try:
                sugg = _compute_surebet_for_outcomes(
                    family=f"{family}:{pattern_name}",
                    line=line_sig,
                    outcomes_info=filtered,
                    base_stake=base_stake,
                    min_edge=min_edge,
                )
            except Exception as e:
                logger.exception(
                    "CERCO[adv] pattern=%s falhou em family=%s line=%s: %s",
                    pattern_name,
                    fam,
                    line_sig,
                    e,
                )
                continue

            if not sugg:
                continue

            out.append(sugg)
            try:
                legs_info = [
                    (leg.book, leg.outcome, leg.odd, leg.stake)
                    for leg in sugg.legs
                ]
            except Exception:
                legs_info = "<?>"  # fallback para log

            logger.info(
                "CERCO[adv] pattern=%s | family=%s | line=%s | ROI=%.4f | lucro=%.2f | implied=%.4f | legs=%s",
                pattern_name,
                sugg.family,
                sugg.line,
                sugg.roi,
                sugg.profit,
                sugg.implied_prob_sum,
                legs_info,
            )

    return out


# ---------------------------------------------------------------------------
# Padrões cross-family: ML (EsporteNet) x AH ±0.5 (Pinnacle)
# ---------------------------------------------------------------------------

def _find_best_esnet_ml(
    book_esnet: Dict[CanonicalKey, float],
    outcome: str,
) -> Optional[float]:
    """
    Procura a melhor odd de moneyline_ft na EsporteNet para 'home', 'draw' ou 'away'.
    """
    if not book_esnet:
        return None

    best: Optional[float] = None
    for (fam, line, out), odd in book_esnet.items():
        if str(fam) != "moneyline_ft":
            continue
        if str(out) != outcome:
            continue
        try:
            val = float(odd)
        except Exception:
            continue
        if val <= 1.01:
            continue
        if best is None or val > best:
            best = val
    return best


def _find_best_moneyline(
    book: Dict[CanonicalKey, float],
    outcome: str,
) -> Optional[float]:
    """
    Versão genérica de busca em moneyline_ft para qualquer casa.
    """
    if not book:
        return None

    best: Optional[float] = None
    for (fam, line, out), odd in book.items():
        if str(fam) != "moneyline_ft":
            continue
        if str(out) != outcome:
            continue
        try:
            val = float(odd)
        except Exception:
            continue
        if val <= 1.01:
            continue
        if best is None or val > best:
            best = val
    return best


def _find_best_pinn_ah_plus_0_5(
    book_pinn: Dict[CanonicalKey, float],
    outcome: str,
) -> Optional[float]:
    """
    Procura a odd da Pinnacle para o handicap +0.5 do lado desejado,
    de acordo com a convenção CANÔNICA do normalizer:

      - HOME +0.5  -> family=asian_handicap_ft, line=+0.5, outcome="home"
      - AWAY +0.5  -> family=asian_handicap_ft, line=+0.5, outcome="away"

    Ou seja, para os dois lados (home/away) buscamos sempre line=0.5,
    variando apenas o outcome.
    """
    if outcome not in ("home", "away"):
        return None

    target_line = 0.5  # +0.5 para o lado em questão

    best: Optional[float] = None
    for (fam, line, out), odd in book_pinn.items():
        if str(fam) != "asian_handicap_ft":
            continue
        if str(out) != outcome:
            continue

        try:
            line_val = float(str(line))
        except Exception:
            continue

        # queremos exatamente a linha +0.5 para aquele lado
        if abs(line_val - target_line) > 1e-6:
            continue

        try:
            val = float(odd)
        except Exception:
            continue
        if val <= 1.01:
            continue

        logger.debug(
            "CERCO[crossML/AH] candidato AH %s +0.5: fam=%s line=%s out=%s odd=%.4f",
            outcome,
            fam,
            line,
            out,
            val,
        )

        if best is None or val > best:
            best = val

    return best


def _find_best_pinn_ah_minus_0_5(
    book_pinn: Dict[CanonicalKey, float],
    outcome: str,
) -> Optional[float]:
    """
    Procura a odd da Pinnacle para o handicap -0.5 do lado desejado,
    na convenção CANÔNICA do normalizer:

      - HOME -0.5  -> family=asian_handicap_ft, line=-0.5, outcome="home"
      - AWAY -0.5  -> family=asian_handicap_ft, line=-0.5, outcome="away"
    """
    if outcome not in ("home", "away"):
        return None

    target_line = -0.5

    best: Optional[float] = None
    for (fam, line, out), odd in book_pinn.items():
        if str(fam) != "asian_handicap_ft":
            continue
        if str(out) != outcome:
            continue

        try:
            line_val = float(str(line))
        except Exception:
            continue

        if abs(line_val - target_line) > 1e-6:
            continue

        try:
            val = float(odd)
        except Exception:
            continue
        if val <= 1.01:
            continue

        logger.debug(
            "CERCO[crossML/AH-0.5] candidato AH %s -0.5: fam=%s line=%s out=%s odd=%.4f",
            outcome,
            fam,
            line,
            out,
            val,
        )

        if best is None or val > best:
            best = val

    return best


def _build_ml_ah_plus_0_5_cross_family_suggestions(
    books: Dict[str, Dict[CanonicalKey, float]],
    *,
    base_stake: float,
    min_edge: float,
) -> List[ArbitrageSuggestion]:
    """
    Cria sugestões de arbitragem para os dois padrões (2 vias):

      1) CAS(ML EsporteNet) x FORA +0.5 (AH Pinnacle)
      2) FORA(ML EsporteNet) x CASA +0.5 (AH Pinnacle)
    """
    book_esn = books.get("ESPORTENET") or {}
    book_pinn = books.get("PINNACLE") or {}
    out: List[ArbitrageSuggestion] = []

    if not book_esn or not book_pinn:
        return out

    # ML EsporteNet
    ml_home = _find_best_esnet_ml(book_esn, "home")
    ml_away = _find_best_esnet_ml(book_esn, "away")

    # AH +0.5 Pinnacle
    ah_home_plus = _find_best_pinn_ah_plus_0_5(book_pinn, "home")
    ah_away_plus = _find_best_pinn_ah_plus_0_5(book_pinn, "away")

    # Padrão A: ML CASA (EsNet) x AH +0.5 FORA (Pinnacle)
    if ml_home and ah_away_plus:
        outcomes_info = {
            "ml home": {
                "best_odd": ml_home,
                "best_book": "ESPORTENET",
                "book_odds": {"ESPORTENET": ml_home},
            },
            "ah away +0.5": {
                "best_odd": ah_away_plus,
                "best_book": "PINNACLE",
                "book_odds": {"PINNACLE": ah_away_plus},
            },
        }
        sugg = _compute_surebet_for_outcomes(
            family="moneyline vs ah +0.5",
            line="",
            outcomes_info=outcomes_info,
            base_stake=base_stake,
            min_edge=min_edge,
        )
        if sugg:
            out.append(sugg)
            logger.info(
                "CERCO[crossML/AH] padrão CASAxAWAY+0.5 | ROI=%.4f | lucro=%.2f | implied=%.4f | odds=(MLhome=%.4f, AHaway+0.5=%.4f)",
                sugg.roi,
                sugg.profit,
                sugg.implied_prob_sum,
                ml_home,
                ah_away_plus,
            )

    # Padrão B: ML FORA (EsNet) x AH +0.5 CASA (Pinnacle)
    if ml_away and ah_home_plus:
        outcomes_info = {
            "ml away": {
                "best_odd": ml_away,
                "best_book": "ESPORTENET",
                "book_odds": {"ESPORTENET": ml_away},
            },
            "ah home +0.5": {
                "best_odd": ah_home_plus,
                "best_book": "PINNACLE",
                "book_odds": {"PINNACLE": ah_home_plus},
            },
        }
        sugg = _compute_surebet_for_outcomes(
            family="moneyline vs ah +0_5",
            line="",
            outcomes_info=outcomes_info,
            base_stake=base_stake,
            min_edge=min_edge,
        )
        if sugg:
            out.append(sugg)
            logger.info(
                "CERCO[crossML/AH] padrão FORAxCASA+0.5 | ROI=%.4f | lucro=%.2f | implied=%.4f | odds=(MLaway=%.4f, AHhome+0.5=%.4f)",
                sugg.roi,
                sugg.profit,
                sugg.implied_prob_sum,
                ml_away,
                ah_home_plus,
            )

    return out


def _build_ml_ah_minus_0_5_cross_family_suggestions(
    books: Dict[str, Dict[CanonicalKey, float]],
    *,
    base_stake: float,
    min_edge: float,
) -> List[ArbitrageSuggestion]:
    """
    Cria sugestões de arbitragem (3 vias) para os padrões:

      1) Home(ML EsNet)        x Draw(ML EsNet) x Away -0.5 (AH Pinnacle)
      2) Home -0.5 (AH Pinn)   x Draw(ML EsNet) x Away(ML EsNet)
      3) Home -0.5 (AH Pinn)   x Draw(ML EsNet) x Away -0.5 (AH Pinn)

    Sempre cobrindo HOME / DRAW / AWAY para o resultado da partida,
    com pelo menos duas casas diferentes entre as pernas.
    """
    book_esn = books.get("ESPORTENET") or {}
    book_pinn = books.get("PINNACLE") or {}
    out: List[ArbitrageSuggestion] = []

    if not book_esn or not book_pinn:
        return out

    # ML EsporteNet
    ml_home = _find_best_esnet_ml(book_esn, "home")
    ml_draw = _find_best_esnet_ml(book_esn, "draw")
    ml_away = _find_best_esnet_ml(book_esn, "away")

    # AH -0.5 Pinnacle
    ah_home_minus = _find_best_pinn_ah_minus_0_5(book_pinn, "home")
    ah_away_minus = _find_best_pinn_ah_minus_0_5(book_pinn, "away")

    # Padrão 1: Home(ML) x Draw(ML) x Away -0.5 (AH)
    if ml_home and ml_draw and ah_away_minus:
        outcomes_info = {
            "home": {
                "best_odd": ml_home,
                "best_book": "ESPORTENET",
                "book_odds": {"ESPORTENET": ml_home},
            },
            "draw": {
                "best_odd": ml_draw,
                "best_book": "ESPORTENET",
                "book_odds": {"ESPORTENET": ml_draw},
            },
            "away": {
                "best_odd": ah_away_minus,
                "best_book": "PINNACLE",
                "book_odds": {"PINNACLE": ah_away_minus},
            },
        }
        sugg = _compute_surebet_for_outcomes(
            family="moneyline vs ah -0.5 (home/draw vs away-0.5)",
            line="",
            outcomes_info=outcomes_info,
            base_stake=base_stake,
            min_edge=min_edge,
        )
        if sugg:
            out.append(sugg)
            logger.info(
                "CERCO[crossML/AH-0.5] padrão HOME/DRAW x AWAY-0.5 | ROI=%.4f | lucro=%.2f | implied=%.4f",
                sugg.roi,
                sugg.profit,
                sugg.implied_prob_sum,
            )

    # Padrão 2: Home -0.5 (AH) x Draw(ML) x Away(ML)
    if ah_home_minus and ml_draw and ml_away:
        outcomes_info = {
            "home": {
                "best_odd": ah_home_minus,
                "best_book": "PINNACLE",
                "book_odds": {"PINNACLE": ah_home_minus},
            },
            "draw": {
                "best_odd": ml_draw,
                "best_book": "ESPORTENET",
                "book_odds": {"ESPORTENET": ml_draw},
            },
            "away": {
                "best_odd": ml_away,
                "best_book": "ESPORTENET",
                "book_odds": {"ESPORTENET": ml_away},
            },
        }
        sugg = _compute_surebet_for_outcomes(
            family="moneyline vs ah -0.5 (home-0.5 vs draw/away)",
            line="",
            outcomes_info=outcomes_info,
            base_stake=base_stake,
            min_edge=min_edge,
        )
        if sugg:
            out.append(sugg)
            logger.info(
                "CERCO[crossML/AH-0.5] padrão HOME-0.5/DRAW/AWAY | ROI=%.4f | lucro=%.2f | implied=%.4f",
                sugg.roi,
                sugg.profit,
                sugg.implied_prob_sum,
            )

    # Padrão 3: Home -0.5 (AH) x Draw(ML) x Away -0.5 (AH)
    if ah_home_minus and ml_draw and ah_away_minus:
        outcomes_info = {
            "home": {
                "best_odd": ah_home_minus,
                "best_book": "PINNACLE",
                "book_odds": {"PINNACLE": ah_home_minus},
            },
            "draw": {
                "best_odd": ml_draw,
                "best_book": "ESPORTENET",
                "book_odds": {"ESPORTENET": ml_draw},
            },
            "away": {
                "best_odd": ah_away_minus,
                "best_book": "PINNACLE",
                "book_odds": {"PINNACLE": ah_away_minus},
            },
        }
        sugg = _compute_surebet_for_outcomes(
            family="moneyline vs ah -0.5 (home-0.5/draw/away-0.5)",
            line="",
            outcomes_info=outcomes_info,
            base_stake=base_stake,
            min_edge=min_edge,
        )
        if sugg:
            out.append(sugg)
            logger.info(
                "CERCO[crossML/AH-0.5] padrão HOME-0.5/DRAW/AWAY-0.5 | ROI=%.4f | lucro=%.2f | implied=%.4f",
                sugg.roi,
                sugg.profit,
                sugg.implied_prob_sum,
            )

    return out


# ---------------------------------------------------------------------------
# Double Chance helpers + padrões ML vs DC e AH -0.5 vs DC
# ---------------------------------------------------------------------------

def _find_best_double_chance(
    book: Dict[CanonicalKey, float],
    combo: str,
) -> Optional[float]:
    """
    Procura a melhor odd em double_chance_ft para um combo específico:

      combo in {"home_or_draw", "draw_or_away", "home_or_away"}

    (nomenclatura canônica assumida pelo normalizer/markets_mapping.json).
    """
    if not book:
        return None

    best: Optional[float] = None
    for (fam, line, out), odd in book.items():
        if str(fam) != "double_chance_ft":
            continue
        if str(out) != combo:
            continue
        try:
            val = float(odd)
        except Exception:
            continue
        if val <= 1.01:
            continue
        if best is None or val > best:
            best = val
    return best


def _build_ml_double_chance_cross_family_suggestions(
    books: Dict[str, Dict[CanonicalKey, float]],
    *,
    base_stake: float,
    min_edge: float,
) -> List[ArbitrageSuggestion]:
    """
    Padrões (2 vias):

      Moneyline vs Chance Dupla
        * Home (ML)          vs Empate ou Fora       (DC -> draw_or_away)
        * Casa ou Empate (DC -> home_or_draw) vs Away (ML)

    Implementação genérica:
      - Percorre pares de casas (A,B).
      - Em cada par:
          * A fornece a perna de Moneyline.
          * B fornece a perna de Double Chance.
      - _compute_surebet_for_outcomes garante:
          * soma de probs < 1
          * edge >= min_edge
          * casas diferentes nas melhores odds.
    """
    out: List[ArbitrageSuggestion] = []

    if len(books) < 2:
        return out

    book_names = list(books.keys())

    for book_ml_name in book_names:
        ml_book = books[book_ml_name]
        ml_home = _find_best_moneyline(ml_book, "home")
        ml_away = _find_best_moneyline(ml_book, "away")
        if not (ml_home or ml_away):
            continue

        for book_dc_name in book_names:
            if book_dc_name == book_ml_name:
                continue

            dc_book = books[book_dc_name]
            dc_home_or_draw = _find_best_double_chance(dc_book, "home_or_draw")
            dc_draw_or_away = _find_best_double_chance(dc_book, "draw_or_away")

            # Padrão 1: Home (ML) vs Draw_or_Away (DC)
            if ml_home and dc_draw_or_away:
                outcomes_info = {
                    "home": {
                        "best_odd": ml_home,
                        "best_book": book_ml_name,
                        "book_odds": {book_ml_name: ml_home},
                    },
                    "draw_or_away": {
                        "best_odd": dc_draw_or_away,
                        "best_book": book_dc_name,
                        "book_odds": {book_dc_name: dc_draw_or_away},
                    },
                }
                line_desc = f"{book_ml_name}.ML home vs {book_dc_name}.DC draw_or_away"
                sugg = _compute_surebet_for_outcomes(
                    family="moneyline vs double_chance",
                    line=line_desc,
                    outcomes_info=outcomes_info,
                    base_stake=base_stake,
                    min_edge=min_edge,
                )
                if sugg:
                    out.append(sugg)
                    logger.info(
                        "CERCO[crossML/DC] padrão Home vs Empate ou Fora | books=%s/%s | ROI=%.4f | lucro=%.2f | implied=%.4f",
                        book_ml_name,
                        book_dc_name,
                        sugg.roi,
                        sugg.profit,
                        sugg.implied_prob_sum,
                    )

            # Padrão 2: Home_or_Draw (DC) vs Away (ML)
            if ml_away and dc_home_or_draw:
                outcomes_info = {
                    "home_or_draw": {
                        "best_odd": dc_home_or_draw,
                        "best_book": book_dc_name,
                        "book_odds": {book_dc_name: dc_home_or_draw},
                    },
                    "away": {
                        "best_odd": ml_away,
                        "best_book": book_ml_name,
                        "book_odds": {book_ml_name: ml_away},
                    },
                }
                line_desc = f"{book_dc_name}.DC home_or_draw vs {book_ml_name}.ML away"
                sugg = _compute_surebet_for_outcomes(
                    family="moneyline vs double_chance",
                    line=line_desc,
                    outcomes_info=outcomes_info,
                    base_stake=base_stake,
                    min_edge=min_edge,
                )
                if sugg:
                    out.append(sugg)
                    logger.info(
                        "CERCO[crossML/DC] padrão Casa ou Empate vs Away | books=%s/%s | ROI=%.4f | lucro=%.2f | implied=%.4f",
                        book_dc_name,
                        book_ml_name,
                        sugg.roi,
                        sugg.profit,
                        sugg.implied_prob_sum,
                    )

    return out


def _build_ah_minus_0_5_double_chance_cross_family_suggestions(
    books: Dict[str, Dict[CanonicalKey, float]],
    *,
    base_stake: float,
    min_edge: float,
) -> List[ArbitrageSuggestion]:
    """
    Padrões (2 vias) para:

      HA -0.5 vs Chance Dupla
        * Home -0.5 (AH)     vs Empate ou Fora   (DC -> draw_or_away)
        * Casa ou Empate (DC -> home_or_draw) vs Away -0.5 (AH)

    Implementação genérica:
      - Percorre pares de casas (A,B).
      - A fornece AH -0.5 (home/away).
      - B fornece Double Chance (home_or_draw / draw_or_away).
      - Sempre cobrindo todos os resultados da partida.
    """
    out: List[ArbitrageSuggestion] = []

    if len(books) < 2:
        return out

    book_names = list(books.keys())

    for book_ah_name in book_names:
        ah_book = books[book_ah_name]
        ah_home_minus = _find_best_pinn_ah_minus_0_5(ah_book, "home")
        ah_away_minus = _find_best_pinn_ah_minus_0_5(ah_book, "away")
        if not (ah_home_minus or ah_away_minus):
            continue

        for book_dc_name in book_names:
            if book_dc_name == book_ah_name:
                continue

            dc_book = books[book_dc_name]
            dc_home_or_draw = _find_best_double_chance(dc_book, "home_or_draw")
            dc_draw_or_away = _find_best_double_chance(dc_book, "draw_or_away")

            # Padrão 1: Home -0.5 (AH) vs Draw_or_Away (DC)
            if ah_home_minus and dc_draw_or_away:
                outcomes_info = {
                    "home": {
                        "best_odd": ah_home_minus,
                        "best_book": book_ah_name,
                        "book_odds": {book_ah_name: ah_home_minus},
                    },
                    "draw_or_away": {
                        "best_odd": dc_draw_or_away,
                        "best_book": book_dc_name,
                        "book_odds": {book_dc_name: dc_draw_or_away},
                    },
                }
                line_desc = f"{book_ah_name}.AH home -0.5 vs {book_dc_name}.DC draw_or_away"
                sugg = _compute_surebet_for_outcomes(
                    family="ah -0.5 vs double_chance",
                    line=line_desc,
                    outcomes_info=outcomes_info,
                    base_stake=base_stake,
                    min_edge=min_edge,
                )
                if sugg:
                    out.append(sugg)
                    logger.info(
                        "CERCO[crossAH/DC] padrão Home -0.5 vs Empate ou Fora | books=%s/%s | ROI=%.4f | lucro=%.2f | implied=%.4f",
                        book_ah_name,
                        book_dc_name,
                        sugg.roi,
                        sugg.profit,
                        sugg.implied_prob_sum,
                    )

            # Padrão 2: Home_or_Draw (DC) vs Away -0.5 (AH)
            if ah_away_minus and dc_home_or_draw:
                outcomes_info = {
                    "home_or_draw": {
                        "best_odd": dc_home_or_draw,
                        "best_book": book_dc_name,
                        "book_odds": {book_dc_name: dc_home_or_draw},
                    },
                    "away": {
                        "best_odd": ah_away_minus,
                        "best_book": book_ah_name,
                        "book_odds": {book_ah_name: ah_away_minus},
                    },
                }
                line_desc = f"{book_dc_name}.DC home_or_draw vs {book_ah_name}.AH away -0.5"
                sugg = _compute_surebet_for_outcomes(
                    family="ah -0.5 vs double_chance",
                    line=line_desc,
                    outcomes_info=outcomes_info,
                    base_stake=base_stake,
                    min_edge=min_edge,
                )
                if sugg:
                    out.append(sugg)
                    logger.info(
                        "CERCO[crossAH/DC] padrão Casa ou Empate vs Away -0.5 | books=%s/%s | ROI=%.4f | lucro=%.2f | implied=%.4f",
                        book_dc_name,
                        book_ah_name,
                        sugg.roi,
                        sugg.profit,
                        sugg.implied_prob_sum,
                    )

    return out


# ---------------------------------------------------------------------------
# Totais (corners/gols/pontos) cross-line (Over x Under)
# ---------------------------------------------------------------------------

def _covers_all_integers_total(line_over: float, line_under: float) -> bool:
    """
    Retorna True se a combinação:

        Over (T > line_over)  x  Under (T < line_under)

    cobre **todos** os valores inteiros possíveis de T (gols, pontos, escanteios),
    isto é, se NÃO existe inteiro T tal que ambas possam perder.

    Condição derivada:

        Existe T inteiro onde ambas perdem sse U <= T <= L.
        Logo, para não existir tal T precisamos de ceil(U) > floor(L).
    """
    try:
        lo = float(line_over)
        lu = float(line_under)
    except Exception:
        return False
    return math.ceil(lu) > math.floor(lo)


def _build_totals_crossline_suggestions_for_family(
    books: Dict[str, Dict[CanonicalKey, float]],
    *,
    family: str,
    base_stake: float,
    min_edge: float,
) -> List[ArbitrageSuggestion]:
    """
    Busca padrões Over (linha L_over) em uma casa x Under (linha L_under) em
    outra casa, dentro de uma mesma família canônica de totais.

    Usado para:
        - family="total_corners_ft"  (Total de escanteios - partida)
        - family="totals_goals_ft"   (Total de gols FT)
        - family="totals_points_ft"  (Total de pontos FT – basquete/AF)
    """
    out: List[ArbitrageSuggestion] = []

    # Agrupa por book → side("over"/"under") → lista (line_val, line_sig, odd)
    by_book: Dict[str, Dict[str, List[Tuple[float, str, float]]]] = {}

    for book_name, book in books.items():
        if not book:
            continue

        for (fam, line, outcome), odd in book.items():
            if str(fam) != family:
                continue
            side = str(outcome)
            if side not in ("over", "under"):
                continue
            try:
                line_val = float(str(line))
                odd_val = float(odd)
            except Exception:
                continue
            if odd_val <= 1.01:
                continue

            sides_map = by_book.setdefault(book_name, {})
            lst = sides_map.setdefault(side, [])
            lst.append((line_val, str(line), odd_val))

    # Se não temos pelo menos 2 casas com linhas úteis, não há o que fazer.
    if len(by_book) < 2:
        return out

    books_list = list(by_book.keys())

    # Percorre pares ordenados de casas A (Over) x B (Under)
    for book_over in books_list:
        over_lines = by_book[book_over].get("over") or []
        if not over_lines:
            continue

        for book_under in books_list:
            if book_under == book_over:
                continue

            under_lines = by_book[book_under].get("under") or []
            if not under_lines:
                continue

            for line_over_val, line_over_sig, odd_over in over_lines:
                for line_under_val, line_under_sig, odd_under in under_lines:
                    # Garante que a combinação cobre todos os inteiros.
                    if not _covers_all_integers_total(line_over_val, line_under_val):
                        continue

                    outcomes_info = {
                        "over": {
                            "best_odd": odd_over,
                            "best_book": book_over,
                            "book_odds": {book_over: odd_over},
                        },
                        "under": {
                            "best_odd": odd_under,
                            "best_book": book_under,
                            "book_odds": {book_under: odd_under},
                        },
                    }

                    line_desc = f"over @{line_over_sig} ({book_over}) vs under @{line_under_sig} ({book_under})"
                    sugg = _compute_surebet_for_outcomes(
                        family=f"{family}_crossline",
                        line=line_desc,
                        outcomes_info=outcomes_info,
                        base_stake=base_stake,
                        min_edge=min_edge,
                    )
                    if not sugg:
                        continue

                    out.append(sugg)

                    logger.info(
                        "CERCO[totals-cross] family=%s | %s | ROI=%.4f | lucro=%.2f | implied=%.4f | odds=(over=%.4f@%s, under=%.4f@%s)",
                        family,
                        line_desc,
                        sugg.roi,
                        sugg.profit,
                        sugg.implied_prob_sum,
                        odd_over,
                        book_over,
                        odd_under,
                        book_under,
                    )

    return out


# ---------------------------------------------------------------------------
# API principal
# ---------------------------------------------------------------------------

def find_arbitrages_for_event(
    markets_raw_pinnacle: Any,
    markets_raw_esnet: Any,
    *,
    base_stake: float = 1000.0,
    min_edge: float = 0.01,
    specials_raw_pinnacle: Any = None,
    extra_books: Optional[Dict[CanonicalKey, Dict[CanonicalKey, float]]] = None,
    require_pinnacle: bool = True,
) -> List[ArbitrageSuggestion]:

    pinn_for_norm = _extract_periods_or_markets_from_pinnacle_raw(markets_raw_pinnacle)

    try:
        book_pinn, book_esnet = build_canonical_books(
            pinn_for_norm,
            markets_raw_esnet,
            specials_raw_pinnacle=specials_raw_pinnacle,
        )
    except Exception:
        logger.exception("[cerco_comparator] erro em build_canonical_books")
        return []

    if require_pinnacle and not book_pinn:
        logger.info("[cerco_comparator] livro Pinnacle vazio; evento ignorado.")
        return []

    # Aplica crosswalk da EsporteNet para asian_handicap_ft (±0.5 etc.),
    # apenas para chaves AH que não existem diretamente no livro EsNet.
    if book_esnet and book_pinn:
        book_esnet = _apply_esnet_crosswalk_for_asian_handicap(book_pinn, book_esnet)

    books: Dict[str, Dict[CanonicalKey, float]] = {}
    if book_pinn:
        books["PINNACLE"] = book_pinn
    if book_esnet:
        books["ESPORTENET"] = book_esnet
    if extra_books:
        for bname, b in extra_books.items():
            if b:
                books[str(bname).upper()] = b

    if len(books) < 2:
        return []

    grouped = _group_by_family_and_line(books)
    suggestions: List[ArbitrageSuggestion] = []

    # -----------------------------------------------------------------------
    # Sugestões "simples" (mesma família, mesma linha)
    # -----------------------------------------------------------------------
    for (family, line), payload in grouped.items():
        # filtro pelo novo mapping
        if not _arb_capable(family):
            continue

        outcomes_info = payload.get("outcomes") or {}

        # respeita min_books por mercado
        min_books_req = _arb_min_books(family)
        present_books = set()
        for _, info in outcomes_info.items():
            for bk in (info.get("book_odds") or {}).keys():
                present_books.add(bk)
        if len(present_books) < min_books_req:
            continue

        _debug_log_market_compare(family, line, outcomes_info)

        sugg = _compute_surebet_for_outcomes(
            family,
            line,
            outcomes_info,
            base_stake=base_stake,
            min_edge=min_edge,
        )
        if not sugg:
            continue

        suggestions.append(sugg)

        logger.info(
            "[cerco_comparator] arbitragem encontrada: family=%s line=%s | ROI=%.3f | lucro=%.2f | implied=%.4f | legs=%s",
            sugg.family,
            sugg.line,
            sugg.roi,
            sugg.profit,
            sugg.implied_prob_sum,
            [
                (
                    leg.book,
                    leg.outcome,
                    leg.odd,
                    leg.stake,
                    leg.pinnacle_odd,
                    leg.esportenet_odd,
                    leg.other_book_odds,
                )
                for leg in sugg.legs
            ],
        )

    # -----------------------------------------------------------------------
    # Totais cross-line (Over x Under em linhas diferentes)
    # - Escanteios (total_corners_ft)
    # - Gols (totals_goals_ft)
    # - Pontos (totals_points_ft)
    #
    # IMPORTANTE:
    # - Escanteios, gols e pontos JÁ são comparados normalmente na lógica
    #   padrão (mesma família/mesma linha), desde que arb.capable=true
    #   no markets_mapping.json.
    # - ESTE bloco abaixo é um EXTRA para gerar arbitragem em linhas
    #   DIFERENTES (ex.: over 2.5 x under 2.75).
    # - Como esse padrão envolveu quarter-lines (.25/.75) e push parcial,
    #   gerando "falsos cercos", deixamos DESATIVADO.
    # -----------------------------------------------------------------------
    ENABLE_CROSSLINE_TOTALS = False

    if ENABLE_CROSSLINE_TOTALS:
        try:
            for fam in ("total_corners_ft", "totals_goals_ft", "totals_points_ft"):
                fam_cross = _build_totals_crossline_suggestions_for_family(
                    books,
                    family=fam,
                    base_stake=base_stake,
                    min_edge=min_edge,
                )
                if fam_cross:
                    suggestions.extend(fam_cross)
                    logger.info(
                        "CERCO[totals-cross] adicionou %s sugestões para %s",
                        len(fam_cross),
                        fam,
                    )
        except Exception as e:
            logger.exception(
                "CERCO[totals-cross] falha geral ao aplicar padrão cross-line em totais: %s",
                e,
            )

    # -----------------------------------------------------------------------
    # Padrão cross-family ML (EsNet) x AH +0.5 (Pinnacle) - 2 vias
    # -----------------------------------------------------------------------
    try:
        cross_suggestions = _build_ml_ah_plus_0_5_cross_family_suggestions(
            books,
            base_stake=base_stake,
            min_edge=min_edge,
        )
        if cross_suggestions:
            suggestions.extend(cross_suggestions)
            logger.info(
                "CERCO[crossML/AH] adicionou %s sugestões ML x AH +0.5",
                len(cross_suggestions),
            )
    except Exception as e:
        logger.exception(
            "CERCO[crossML/AH] falha geral ao aplicar padrão ML x AH +0.5: %s",
            e,
        )

    # -----------------------------------------------------------------------
    # Padrão cross-family ML (EsNet) x AH -0.5 (Pinnacle) - 3 vias
    # -----------------------------------------------------------------------
    try:
        cross_suggestions_minus = _build_ml_ah_minus_0_5_cross_family_suggestions(
            books,
            base_stake=base_stake,
            min_edge=min_edge,
        )
        if cross_suggestions_minus:
            suggestions.extend(cross_suggestions_minus)
            logger.info(
                "CERCO[crossML/AH-0.5] adicionou %s sugestões ML x AH -0.5 (3 vias)",
                len(cross_suggestions_minus),
            )
    except Exception as e:
        logger.exception(
            "CERCO[crossML/AH-0.5] falha geral ao aplicar padrão ML x AH -0.5: %s",
            e,
        )

    # -----------------------------------------------------------------------
    # Padrões cross-family ML vs Double Chance
    # -----------------------------------------------------------------------
    try:
        cross_ml_dc = _build_ml_double_chance_cross_family_suggestions(
            books,
            base_stake=base_stake,
            min_edge=min_edge,
        )
        if cross_ml_dc:
            suggestions.extend(cross_ml_dc)
            logger.info(
                "CERCO[crossML/DC] adicionou %s sugestões ML x Double Chance",
                len(cross_ml_dc),
            )
    except Exception as e:
        logger.exception(
            "CERCO[crossML/DC] falha geral ao aplicar padrão ML x Double Chance: %s",
            e,
        )

    # -----------------------------------------------------------------------
    # Padrões cross-family AH -0.5 vs Double Chance
    # -----------------------------------------------------------------------
    try:
        cross_ah_dc = _build_ah_minus_0_5_double_chance_cross_family_suggestions(
            books,
            base_stake=base_stake,
            min_edge=min_edge,
        )
        if cross_ah_dc:
            suggestions.extend(cross_ah_dc)
            logger.info(
                "CERCO[crossAH/DC] adicionou %s sugestões AH -0.5 x Double Chance",
                len(cross_ah_dc),
            )
    except Exception as e:
        logger.exception(
            "CERCO[crossAH/DC] falha geral ao aplicar padrão AH -0.5 x Double Chance: %s",
            e,
        )

    # -----------------------------------------------------------------------
    # Advanced patterns (multi-mercado) a partir de arbitrage_rules.json
    # -----------------------------------------------------------------------
    try:
        adv_suggestions = _build_advanced_suggestions(
            grouped,
            base_stake=base_stake,
        )
        if adv_suggestions:
            suggestions.extend(adv_suggestions)
            logger.info(
                "CERCO[adv] adicionou %s sugestões advanced ao total",
                len(adv_suggestions),
            )
    except Exception as e:
        logger.exception("CERCO[adv] falha geral ao aplicar advanced_patterns: %s", e)

    suggestions.sort(key=lambda s: s.roi, reverse=True)
    return suggestions


# ---------------------------------------------------------------------------
# API de compatibilidade (esperada pelo Orchestrator)
# ---------------------------------------------------------------------------

def build_books_and_find_arbs(
    markets_raw_pinnacle: Any,
    markets_raw_esnet: Any,
    *,
    specials_raw_pinnacle: Any = None,
    base_stake: float = 1000.0,
    min_edge: float = CERCO_MIN_EDGE,
) -> List[ArbitrageOpportunity]:
    suggestions = find_arbitrages_for_event(
        markets_raw_pinnacle,
        markets_raw_esnet,
        base_stake=base_stake,
        min_edge=min_edge,
        specials_raw_pinnacle=specials_raw_pinnacle,
        extra_books=None,
        require_pinnacle=True,
    )

    opps: List[ArbitrageOpportunity] = []
    for s in suggestions:
        implied = float(s.implied_prob_sum or 0.0)
        edge = max(0.0, 1.0 - implied)

        opps.append(
            ArbitrageOpportunity(
                family=s.family,
                line=s.line,
                legs=s.legs,
                edge=edge,
                implied_prob=implied,
                base_stake=s.base_stake,
                roi=s.roi,
                profit=s.profit,
            )
        )

    opps.sort(key=lambda o: o.roi, reverse=True)
    return opps
