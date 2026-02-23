# -*- coding: utf-8 -*-
"""
Catálogo de ligas ESPECÍFICO do subsistema CERCO.

Versão simplificada conforme sua arquitetura real:
- ÚNICA fonte de verdade: LEAGUES_BY_SPORT.
- Sem perfis alternativos (core/brazil).
- Sem políticas paralelas de whitelist (PINNACLE_ALLOWED_DEFAULT).
- Mantém cache diário de ligas com eventos no dia atual.

Helpers expostos:
    - get_leagues_for_sport("soccer" | "basketball" | "american_football" | "tennis" | "volleyball")
    - get_leagues_for_pinnacle_sport_id(1|2|3|5|7|15|29|33|34|4, ...)
    - get_league_batches_for_sport_id(sport_id, batch_size=120)
    - chunk_leagues(seq, n)
    - load_today_leagues_snapshot()
    - save_today_leagues_snapshot(...)
    - get_today_leagues_for_sport_id(...)
    - get_today_league_batches_for_sport_id(...)

Observações:
- Este arquivo vive em src/cerco/leagues_cerco.py e NÃO substitui o catálogo global.
- `None` continua significando "todas as ligas" para aquele esporte (preserva seu contrato).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Iterable, List, Optional, Sequence

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths para cache de ligas do dia (configurável por ENV)
# ---------------------------------------------------------------------------

_HERE = os.path.dirname(__file__)
_DATA_DIR = os.path.join(_HERE, "data")
try:
    os.makedirs(_DATA_DIR, exist_ok=True)
except Exception:
    # Em ambiente read-only, o caminho pode ser redirecionado via ENV.
    pass

TODAY_LEAGUES_PATH = os.getenv(
    "CERCO_TODAY_LEAGUES_JSON",
    os.path.join(_DATA_DIR, "cerco_today_leagues.json"),
)

# ---------------------------------------------------------------------------
# MAPAS DE ESPORTE (IDs KIT vs Classic/p_id)
# ---------------------------------------------------------------------------

# ID "kit"  → nome interno
PINNACLE_SPORT_NAME_BY_ID: Dict[int, str] = {
    1: "soccer",
    2: "tennis",
    3: "basketball",
    5: "volleyball",
    7: "american_football",
    # Outros esportes podem ser adicionados aqui se passarem a ser usados
}

# ID "classic" (p_id) → nome interno (para compatibilidade eventual)
PINNACLE_SPORT_NAME_BY_PID: Dict[int, str] = {
    29: "soccer",            # p_id do Soccer
    33: "tennis",            # p_id do Tennis
    4:  "basketball",        # p_id do Basketball
    34: "volleyball",        # p_id do Volleyball
    15: "american_football", # p_id do American Football
    # Acrescente aqui se for usar outros esportes do JSON
}

def _name_by_any_pinnacle_id(sport_id: int) -> Optional[str]:
    """Aceita id (KIT) ou p_id (Classic) e retorna o nome interno."""
    sport_id = int(sport_id)
    if sport_id in PINNACLE_SPORT_NAME_BY_ID:
        return PINNACLE_SPORT_NAME_BY_ID[sport_id]
    if sport_id in PINNACLE_SPORT_NAME_BY_PID:
        return PINNACLE_SPORT_NAME_BY_PID[sport_id]
    return None

# ---------------------------------------------------------------------------
# LISTAS DE LIGAS POR ESPORTE (ÚNICA fonte de verdade)
# ---------------------------------------------------------------------------

LEAGUES_BY_SPORT: Dict[str, Optional[List[int]]] = {
    # -----------------------------------------------------------------------
    # SOCCER
    # -----------------------------------------------------------------------
    "soccer": [
        1707,    # AFC - Asian Cup
        198935,  # AFC - Asian Cup Qualifiers
        238432,  # AFC - Challenge League
        1709,    # AFC - Champions League 2
        1708,    # AFC - Champions League Elite
        7795,    # AFC - Champions League Elite Qualifiers
        196889,  # AFC - Cup Qualifiers
        209203,  # Albania - Cup
        1722,    # Albania - Superliga
        1725,    # Algeria - Cup
        8667,    # Algeria - Ligue 1
        10419,   # Arábia Saudita - Liga Profissional Saudita
        10600,   # Arábia Saudita - Divisão 1
        204784,  # Argentina - Copa de la Liga Profesional
        223049,  # Argentina - Copa Mendoza
        201979,  # Argentina - Copa Santa Fe
        1741,    # Argentina - Cup
        210697,  # Argentina - Liga Pro
        201149,  # Argentina - Primera B Metropolitana
        1739,    # Argentina - Primera B Nacional
        1740,    # Argentina - Primera Division
        192769,  # Argentina - Super Cup
        258408,  # Argentina - Supercopa Internacional
        1766,    # Australia - A League
        9394,    # Australia - Cup
        1773,    # Austria - 2. Liga
        1792,    # Austria - Bundesliga
        1776,    # Austria - Cup
        7167,    # Azerbaijan - Premier League
        1797,    # Bahrain - Premier League
        1813,    # Belarus - Cup
        6416,    # Belarus - Premier League
        204392,  # Belarus - Super Cup
        1818,    # Belgium - Challenger Pro League
        1819,    # Belgium - Cup
        1817,    # Belgium - Pro League
        1816,    # Belgium - Super Cup
        197206,  # Bolivia - Cup
        5595,    # Bolivia - Primera Division
        198700,  # Bosnia and Herzegovina - Cup
        1826,    # Bosnia and Herzegovina - Premier Liga
        192685,  # Brazil - Alagoano
        10769,   # Brazil - Baiano
        192841,  # Brazil - Brasiliense
        218547,  # Brazil - Capixaba
        1852,    # Brazil - Carioca
        194726,  # Brazil - Carioca 2
        10776,   # Brazil - Catarinense
        10768,   # Brazil - Cearense
        211794,  # Brazil - Copa Alagoas
        270916,  # Brazil - Copa do Brasil
        10883,   # Brazil - Copa Do Nordeste
        222976,  # Brazil - Copa Ouro U20
        191548,  # Brazil - Copa Paulista
        216059,  # Brazil - Copa Rio U20
        273161,  # Brazil - Copa Sergipano
        75455,   # Brazil - Copa Verde
        1833,    # Brazil - Cup
        1829,    # Brazil - Gaucho
        10780,   # Brazil - Goiano
        10778,   # Brazil - Mineiro
        198451,  # Brazil - Paraense
        10770,   # Brazil - Paranaense
        2360,    # Brazil - Paulista
        10782,   # Brazil - Paulista A2
        10885,   # Brazil - Pernambucano
        218024,  # Brazil - Sao Paulo Cup U20
        196773,  # Brazil - Sergipano
        1834,    # Brazil - Serie A
        1835,    # Brazil - Serie B
        1836,    # Brazil - Serie C
        201371,  # Brazil - Serie D
        218651,  # Brazil - Supercopa
        1839,    # Bulgaria - Cup
        9320,    # Bulgaria - First League
        9503,    # Bulgaria - Super Cup
        1718,    # CAF - Africa Cup of Nations
        1713,    # CAF - Africa Cup of Nations Qualifiers
        1717,    # CAF - African Nations Championship
        1715,    # CAF - Champions League
        187709,  # CAF - Confederation Cup
        208188,  # CAF - Super Cup
        226330,  # CAFA - Nations Cup
        199745,  # Cameroon - Cup
        1849,    # Cameroon - Elite One
        1850,    # Canada - Championship
        205098,  # Canada - Premier League
        1851,    # Canada - Soccer League
        10279,   # Chile - Primera B
        1857,    # Chile - Primera Division
        197657,  # Chile - Super Cup
        1859,    # China - FA Cup
        10813,   # China - Super Cup
        6417,    # China - Super League
        1863,    # Club Friendlies
        1864,    # Club Friendlies Women
        1867,    # Colombia - Cup
        5591,    # Colombia - Primera A
        10921,   # Colombia - Primera B
        10629,   # Colombia - Super Cup
        209948,  # Colombia - Superliga
        1870,    # CONCACAF - Champions Cup
        2077,    # CONCACAF - Gold Cup
        35185,   # CONCACAF - Gold Cup Qualifiers
        199581,  # CONCACAF - League
        206571,  # CONCACAF - Nations League
        202502,  # CONCACAF - Nations League - Qualifiers
        212499,  # CONCACAF - Olympic Qualifiers
        1872,    # CONMEBOL - Copa America
        192496,  # CONMEBOL - Copa America Qualifiers
        1875,    # CONMEBOL - Copa Libertadores
        2472,    # CONMEBOL - Copa Sudamericana
        2394,    # CONMEBOL - Recopa Sudamericana
        197076,  # COSAFA - Cup
        9532,    # Costa Rica - Cup
        9531,    # Costa Rica - Primera Division
        214750,  # Costa Rica - Recopa
        1882,    # Croatia - Cup
        1880,    # Croatia - HNL
        1881,    # Croatia - Prva NL
        205799,  # Croatia - Super Cup
        1887,    # Cyprus - 1st Division
        1888,    # Cyprus - Cup
        1890,    # Cyprus - Super Cup
        1898,    # Czech Republic - Cup
        1891,    # Czech Republic - First Liga
        9441,    # Czech Republic - FNL
        1908,    # Denmark - Cup
        1904,    # Denmark - Division 1
        1913,    # Denmark - Superliga
        219609,  # Ecuador - Cup
        5598,    # Ecuador - Serie A
        197248,  # Ecuador - Serie B
        207919,  # Ecuador - Supercopa
        1946,    # Egypt - Cup
        9885,    # Egypt - Premier League
        198532,  # Egypt - Super Cup
        196742,  # El Salvador - Primera Division
        272632,  # El Salvador - Super Cup
        1977,    # England - Championship
        1982,    # England - EFL Cup
        1979,    # England - FA Cup
        1957,    # England - League 1
        1958,    # England - League 2
        1978,    # England - National League
        1980,    # England - Premier League
        125461,  # Estonia - Cup
        201775,  # Estonia - Esiliiga A
        1989,    # Estonia - Meistriliiga
        224548,  # Estonia - Super Cup
        213780,  # FIFA - Arab Cup
        215048,  # FIFA - Beach Soccer World Cup (Reg. Time - 3x12 mins)
        1865,    # FIFA - Club World Cup
        237783,  # FIFA - Intercontinental Cup
        2686,    # FIFA - World Cup
        9102,    # FIFA - World Cup Extra Time
        2013,    # FIFA - World Cup Qualifiers Africa
        2014,    # FIFA - World Cup Qualifiers Asia
        212593,  # FIFA - World Cup Qualifiers CONCACAF
        2015,    # FIFA - World Cup Qualifiers Europe
        2012,    # FIFA - World Cup Qualifiers Europe Women
        219415,  # FIFA - World Cup Qualifiers Intercontinental
        2017,    # FIFA - World Cup Qualifiers North America
        5001,    # FIFA - World Cup Qualifiers Oceania
        2016,    # FIFA - World Cup Qualifiers South America
        224313,  # FIFA - World Cup Qualifiers Women
        2608,    # FIFA - World Cup U17
        4483,    # FIFA - World Cup U17 Women
        2620,    # FIFA - World Cup U20
        2010,    # FIFA - World Cup U20 Women
        233204,  # FIFA - World Cup U20 Women Qualifiers
        2687,    # FIFA - World Cup Women
        198974,  # FIFA - World Cup Women Qualifiers Europe
        2020,    # Finland - Cup
        2018,    # Finland - Kakkonen
        2024,    # Finland - Veikkausliiga
        2025,    # Finland - Ykkosliiga
        2032,    # France - Cup
        2034,    # France - League Cup
        2036,    # France - Ligue 1
        2037,    # France - Ligue 2
        2027,    # France - National
        200815,  # France - National 2
        2038,    # Friendlies
        2042,    # Friendly - Women
        2674,    # Friendly - World Club
        1842,    # Germany - Bundesliga
        1843,    # Germany - Bundesliga 2
        1844,    # Germany - Bundesliga 3
        2067,    # Germany - Cup
        2054,    # Germany - Super Cup
        2080,    # Greece - Cup
        2081,    # Greece - Super League
        206711,  # Greece - Super League 2
        202607,  # Guatemala - Cup
        2088,    # Guatemala - Liga Nacional
        202695,  # Honduras - Cup
        196865,  # Honduras - Liga Nacional
        2096,    # Hungary - Cup
        2095,    # Hungary - NB 1
        2100,    # Iceland - 1. Deild
        2101,    # Iceland - Cup
        2102,    # Iceland - Premier League
        201333,  # Iceland - Super Cup
        7953,    # International - Algarve Cup Women
        1801,    # International - Baltic Cup
        1950,    # International - Emirates Cup
        2117,    # International - Friendlies
        226402,  # International - Intercontinental Cup
        2178,    # International - Kings Cup
        2123,    # Ireland - Division 1
        2124,    # Ireland - FAI Cup
        2122,    # Ireland - League Cup
        2120,    # Ireland - Premier
        10954,   # Ireland - Super Cup
        2138,    # Israel - Ligat Al Toto Cup
        2133,    # Israel - Ligat Leumit
        2136,    # Israel - Premier League
        2147,    # Italy - Cup
        2436,    # Italy - Serie A
        2438,    # Italy - Serie B
        199868,  # Italy - Serie C
        10806,   # Italy - Serie C Cup
        217401,  # Italy - Serie C Group A
        217400,  # Italy - Serie C Group B
        217399,  # Italy - Serie C Group C
        2148,    # Italy - Serie C Super Cup
        2153,    # Italy - Super Cup
        225944,  # Jamaica - Cup
        10407,   # Jamaica - Premier League
        2160,    # Japan - Cup
        2157,    # Japan - J League
        2181,    # Korea Republic - FA Cup
        207551,  # Korea Republic - K League 1
        2204,    # Latvia - Cup
        2203,    # Latvia - Virsliga
        191423,  # Mexico - Ascenso
        5778,    # Mexico - Cup
        2242,    # Mexico - Liga MX
        # 207004,  # Mexico - Liga Premier
        202034,  # Mexico - Super Cup
        2253,    # Morocco - Botola Pro
        10288,   # Morocco - Cup
        1930,    # Netherlands - Cup
        1929,    # Netherlands - Eerste Divisie
        1928,    # Netherlands - Eredivisie
        1933,    # Netherlands - Super Cup
        197584,  # Nicaragua - Primera Division
        235788,  # Nigeria - Cup
        199487,  # Nigeria - NPFL
        227961,  # North America - Campeones Cup
        205939,  # North America - Leagues Cup
        2258,    # Northern Ireland - Cup
        2259,    # Northern Ireland - Premiership
        2331,    # Norway - 1st Division
        2332,    # Norway - 2nd Division
        2280,    # Norway - Cup
        2333,    # Norway - Eliteserien
        198947,  # Norway - Super Cup
        202152,  # Paraguay - Cup
        10573,   # Panama - Primera Division
        201859,  # Paraguay - Division Intermedia
        2359,    # Paraguay - Division Profesional
        224028,  # Paraguay - Super Cup
        5592,    # Peru - Copa Peru
        2366,    # Peru - Liga 1
        207704,  # Peru - Super Cup
        6633,    # Poland - 1st Liga
        2368,    # Poland - 2nd Liga
        2373,    # Poland - Cup
        2374,    # Poland - Ekstraklasa
        5828,    # Poland - Super Cup
        2384,    # Portugal - Cup
        4997,    # Portugal - League Cup
        2387,    # Portugal - Liga 2
        2386,    # Portugal - Primeira Liga
        2381,    # Portugal - Super Cup
        10905,   # Qatar - Cup
        8128,    # Qatar - Stars League
        2396,    # Romania - Cup
        2395,    # Romania - Liga 1
        187758,  # Romania - Super Cup
        2409,    # Russia - Cup
        2406,    # Russia - Premier League
        4763,    # Russia - Super Cup
        2417,    # Scotland - Championship
        2426,    # Scotland - Cup
        4998,    # Scotland - League Cup
        2418,    # Scotland - League One
        2419,    # Scotland - League Two
        2421,    # Scotland - Premiership
        2435,    # Serbia - Cup
        206162,  # Serbia - Prva Liga
        2434,    # Serbia - Super Liga
        2453,    # Slovakia - Super Liga
        2456,    # Slovenia - Cup
        2457,    # Slovenia - Prva Liga
        2411,    # South Africa - Cup
        196763,  # South Africa - PSL
        2462,    # Spain - Copa del Rey
        2196,    # Spain - La Liga
        2432,    # Spain - Segunda Division
        2463,    # Spain - Super Cup
        1728,    # Sweden - Allsvenskan
        2516,    # Sweden - Cup
        2512,    # Sweden - Super Cup
        2476,    # Sweden - Superettan
        227469,  # Switzerland - 2. Liga Interregional
        2518,    # Switzerland - Challenge League
        2519,    # Switzerland - Cup
        2517,    # Switzerland - Super League
        2578,    # Turkey - 1st League
        2586,    # Turkey - Cup
        2597,    # Turkey - Super Cup
        2592,    # Turkey - Super League
        2624,    # UAE - Cup
        8126,    # UAE - Pro League
        218134,  # UAE - Super Cup
        2627,    # UEFA - Champions League
        205451,  # UEFA - Champions League Qualifiers
        2642,    # UEFA - Champions League Women
        214101,  # UEFA - Conference League
        271382,  # UEFA - Conference League Qualifiers
        5264,    # UEFA - EURO
        2635,    # UEFA - EURO Qualifiers
        2640,    # UEFA - EURO Women
        2641,    # UEFA - EURO Women Qualifiers
        2630,    # UEFA - Europa League
        2632,    # UEFA - Europa League Qualifiers
        200719,  # UEFA - Nations League A
        200721,  # UEFA - Nations League B
        200726,  # UEFA - Nations League C
        200727,  # UEFA - Nations League D
        242340,  # UEFA - Nations League Playoffs
        2636,    # UEFA - Super Cup
        2647,    # Ukraine - Cup
        2650,    # Ukraine - Premier League
        215933,  # Ukraine - Super Cup
        5594,    # Uruguay - Cup
        5593,    # Uruguay - Primera Division
        2663,    # USA - Major League Soccer
        219120,  # USA - MLS Next Pro League
        2662,    # USA - Open Cup
        205322,  # USA - USL League One
        5597,    # Venezuela - Cup
        5596,    # Venezuela - Primera Division
        214556,  # Wales - Championship
        2679,    # Wales - Cup
        10285,   # Wales - League Cup
        2678,    # Wales - Premier League
    ],

    # -----------------------------------------------------------------------
    # BASKETBALL
    # -----------------------------------------------------------------------
    "basketball": [
        487,   # NBA (ex.)
        293,   # NBB / LNB (ex.)
        493,   # NCAA
        5270,  # Outras competições (ex.)
    ],

    # -----------------------------------------------------------------------
    # AMERICAN FOOTBALL
    # -----------------------------------------------------------------------
    "american_football": [
        889,   # NFL
        880,   # NCAA
    ],

    # -----------------------------------------------------------------------
    # TENNIS
    # -----------------------------------------------------------------------
    "tennis": [
        232014,  # Exemplo 1
        197837,  # Exemplo 2
        233540,
        196919,  # Davis Cup
        4762,    # Davis Cup Doubles
    ],

    # -----------------------------------------------------------------------
    # VOLLEYBALL
    # -----------------------------------------------------------------------
    "volleyball": [
        4117,  # Alemanha - Bundesliga
        4039,  # Brasil - Superliga Feminina
    ],
}

# ---------------------------------------------------------------------------
# Helpers de deduplicação / chunk
# ---------------------------------------------------------------------------

def _dedupe_keep_order(seq: Iterable[int]) -> List[int]:
    seen = set()
    out: List[int] = []
    for x in seq:
        xi = int(x)
        if xi not in seen:
            seen.add(xi)
            out.append(xi)
    return out


def chunk_leagues(leagues: Sequence[int] | None, n: int) -> List[List[int]]:
    """Quebra `leagues` em listas de tamanho até `n` (mantendo ordem)."""
    if not leagues:
        return []
    n = max(1, int(n))
    return [list(map(int, leagues[i:i + n])) for i in range(0, len(leagues), n)]

# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------

def get_leagues_for_sport(sport: str) -> Optional[List[int]]:
    """
    Retorna ligas para um esporte por nome interno.

    Retorna:
      - None → significa "todas as ligas"
      - []   → esporte desconhecido
      - [..] → lista deduplicada
    """
    key = (sport or "").strip().lower()
    if key not in LEAGUES_BY_SPORT:
        return []

    lst = LEAGUES_BY_SPORT[key]
    if lst is None:
        return None
    return _dedupe_keep_order(lst)


def get_leagues_for_pinnacle_sport_id(sport_id: int) -> Optional[List[int]]:
    """
    Conveniente para quem trabalha com o ID da Pinnacle diretamente.
    Aceita tanto id (KIT) quanto p_id (Classic).
    """
    name = _name_by_any_pinnacle_id(int(sport_id))
    if not name:
        return []
    return get_leagues_for_sport(name)


def get_league_batches_for_sport_id(
    sport_id: int,
    *,
    batch_size: int = 120,
) -> List[List[int]]:
    """
    Retorna batches de league_ids prontos para o endpoint:
      - Se vier None → sem batches (representa "todas" → chamador decide).
      - Se vier [] → vazio.
      - Se vier [..] → chunked por batch_size.
    """
    leagues = get_leagues_for_pinnacle_sport_id(int(sport_id))
    if leagues is None:
        return []
    return chunk_leagues(leagues, int(batch_size))

# ---------------------------------------------------------------------------
# Cache diário de ligas com eventos "hoje"
# ---------------------------------------------------------------------------

def load_today_leagues_snapshot() -> Dict[str, Any]:
    """
    Lê o snapshot de ligas do dia a partir do arquivo JSON.

    Estrutura esperada:
      {
        "date": "YYYY-MM-DD",
        "refreshed_at": "ISO-UTC",
        "sports": {
          "1": [1834, 1835, ...],
          "3": [487, 293, ...],
          ...
        }
      }
    """
    try:
        if TODAY_LEAGUES_PATH and os.path.exists(TODAY_LEAGUES_PATH):
            with open(TODAY_LEAGUES_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    sports = data.get("sports") or {}
                    logger.debug(
                        "[today_leagues] snapshot carregado de %s | date=%s | esportes=%d",
                        TODAY_LEAGUES_PATH,
                        data.get("date"),
                        len(sports),
                    )
                    return data
    except Exception:
        logger.exception("[today_leagues] erro ao ler snapshot de %s", TODAY_LEAGUES_PATH)
    return {}


def save_today_leagues_snapshot(snapshot: Dict[str, Any]) -> None:
    """Salva o snapshot de ligas do dia em disco (com replace atômico)."""
    if not TODAY_LEAGUES_PATH:
        logger.warning("[today_leagues] TODAY_LEAGUES_PATH vazio; snapshot não será salvo.")
        return

    try:
        tmp_path = TODAY_LEAGUES_PATH + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, TODAY_LEAGUES_PATH)

        sports = snapshot.get("sports") or {}
        logger.info(
            "[today_leagues] snapshot salvo em %s | date=%s | esportes=%d",
            TODAY_LEAGUES_PATH,
            snapshot.get("date"),
            len(sports),
        )
    except Exception:
        logger.exception("[today_leagues] falha ao salvar snapshot em %s", TODAY_LEAGUES_PATH)


def get_today_leagues_for_sport_id(sport_id: int) -> Optional[List[int]]:
    """
    Retorna as ligas do cache diário para um sport_id específico.
    - None  → não há entrada para este esporte (deixa o chamador decidir).
    - []    → snapshot existe, mas nenhuma liga ativa foi encontrada para o esporte.
    - [..]  → lista de league_ids.
    """
    snap = load_today_leagues_snapshot()
    sports = snap.get("sports") or {}
    leagues = sports.get(str(int(sport_id)))
    if leagues is None:
        return None
    try:
        return _dedupe_keep_order(int(x) for x in leagues)
    except Exception:
        return []


def get_today_league_batches_for_sport_id(
    sport_id: int,
    *,
    batch_size: int = 120,
    today_only: bool = True,
) -> List[List[int]]:
    """
    Versão consciente do cache diário.

    - Se today_only=True:
        * Usa snapshot diário se houver ligas para o esporte.
        * Se não houver entrada ou estiver vazio → fallback para catálogo principal.
    - Se today_only=False:
        * Equivalente a get_league_batches_for_sport_id(...).
    """
    if not today_only:
        return get_league_batches_for_sport_id(
            sport_id,
            batch_size=batch_size,
        )

    leagues = get_today_leagues_for_sport_id(int(sport_id))
    if leagues:
        return chunk_leagues(leagues, int(batch_size))

    # leagues is None (sem entrada) OU [] (snapshot sem ligas para o esporte)
    # → fallback para catálogo principal
    return get_league_batches_for_sport_id(
        sport_id,
        batch_size=batch_size,
    )

