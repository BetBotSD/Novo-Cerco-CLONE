# -*- coding: utf-8 -*-
# src/cerco/telegram_cerco.py

from __future__ import annotations

import logging
import math
import os
import re
import time
from dataclasses import dataclass
from io import BytesIO
from typing import Any, Dict, List, Optional, Tuple

import httpx

from .config_cerco import APP_TZ, APP_TZ_NAME, CERCO_STAKE_BASE_BRL, CERCO_MIN_EDGE, CERCO_ROI_SOBRA_MIN

logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("CERCO_TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CERCO_TELEGRAM_CHAT_ID", "").strip()

# ---------------------------------------------------------------------------
# MarkdownV2 escape
# ---------------------------------------------------------------------------
_MD_RE = re.compile(r"([_*\[\]()~`>#+\-=|{}.!])")


def _md_escape(s: str) -> str:
    if not s:
        return s
    return _MD_RE.sub(r"\\\1", s)


# ---------------------------------------------------------------------------
# Logos locais dos sites (Pinnacle / EsporteNet) + tamanhos
# ---------------------------------------------------------------------------
LOGO_PINNACLE_PATH = os.getenv(
    "CERCO_LOGO_PINNACLE_PATH",
    "src/cerco/assets/logos/pinnacle.png",
).strip()

LOGO_ESPORTENET_PATH = os.getenv(
    "CERCO_LOGO_ESPORTENET_PATH",
    "src/cerco/assets/logos/esportenet.png",
).strip()

# Tamanhos independentes das logos das casas (px)
BOOK_LOGO_PINNACLE_SIZE = int(os.getenv("CERCO_LOGO_PINNACLE_SIZE", "160"))
BOOK_LOGO_ESPORTENET_SIZE = int(os.getenv("CERCO_LOGO_ESPORTENET_SIZE", "160"))

# ---------------------------------------------------------------------------
# Fontes customizáveis (ex.: Montserrat). Caminhos podem ser sobrescritos por ENV
# ---------------------------------------------------------------------------
FONT_REGULAR_PATH = os.getenv(
    "CERCO_FONT_REGULAR",
    "src/cerco/assets/fonts/Roboto-Regular.ttf",
).strip()
FONT_BOLD_PATH = os.getenv(
    "CERCO_FONT_BOLD",
    "src/cerco/assets/fonts/Designer.otf",
).strip()


def is_enabled() -> bool:
    """Retorna True se as variáveis de ambiente do bot estiverem configuradas."""
    return bool(BOT_TOKEN and CHAT_ID)


# ---------------------------------------------------------------------------
# retry/backoff leve para chamadas do Telegram
# ---------------------------------------------------------------------------
def _post_with_retry(
    url: str,
    *,
    json_payload=None,
    data_payload=None,
    files=None,
    timeout=15.0,
) -> Optional[dict]:
    last_exc: Optional[Exception] = None
    for attempt in range(3):
        try:
            with httpx.Client(timeout=timeout) as client:
                resp = client.post(
                    url,
                    json=json_payload,
                    data=data_payload,
                    files=files,
                )
            if resp.status_code != 200:
                raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            j = resp.json()
            if not j.get("ok", False):
                raise RuntimeError(f"Telegram ok=False: {str(j)[:200]}")
            return j
        except Exception as e:
            last_exc = e
            wait = 1.0 * (attempt + 1)
            logger.warning(
                "[CERCO/Telegram] Falha POST (%s). Tentativa %d/3. wait=%.1fs err=%s",
                url,
                attempt + 1,
                wait,
                e,
            )
            time.sleep(wait)

    logger.error("[CERCO/Telegram] POST falhou após retries: %s", last_exc)
    return None


def send_message(text: str, *, parse_mode: str = "MarkdownV2") -> None:
    """
    Envia uma mensagem simples ao Telegram para o chat configurado.

    Se o bot não estiver configurado, apenas loga em DEBUG.
    """
    if not is_enabled():
        logger.debug(
            "[CERCO/Telegram] Bot desabilitado (token ou chat_id ausente); mensagem não enviada."
        )
        return

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload: Dict[str, Any] = {
        "chat_id": CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode

    try:
        _post_with_retry(url, json_payload=payload, timeout=10.0)
    except Exception:
        logger.exception("[CERCO/Telegram] Erro inesperado ao enviar mensagem.")


def send_photo(
    photo_bytes: bytes,
    *,
    caption: str | None = None,
    parse_mode: str = "MarkdownV2",
) -> None:
    """
    Envia imagem ao Telegram via sendPhoto.
    """
    if not is_enabled():
        logger.debug("[CERCO/Telegram] Bot desabilitado; foto não enviada.")
        return

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    data: Dict[str, Any] = {"chat_id": CHAT_ID}
    if caption:
        data["caption"] = caption
        if parse_mode:
            data["parse_mode"] = parse_mode

    files = {"photo": ("cerco.png", photo_bytes)}

    try:
        _post_with_retry(url, data_payload=data, files=files, timeout=20.0)
    except Exception:
        logger.exception("[CERCO/Telegram] erro ao enviar foto.")


# ---------------------------------------------------------------------------
# Formatação do texto (mensagem simples)
# ---------------------------------------------------------------------------

def _format_datetime_local(dt) -> str:
    """Formata datetime do snapshot para string no fuso APP_TZ (quando disponível)."""
    if dt is None:
        return "N/D"
    try:
        if APP_TZ is not None:
            dt = dt.astimezone(APP_TZ)
        return dt.strftime("%d/%m %H:%M")
    except Exception:
        return str(dt)


def format_match_message(match: Any, *, stake_base: float | None = None) -> str:
    """
    Monta o texto a ser enviado ao Telegram para um CercoMatchResult.
    Usa MarkdownV2 (com escape) mas sem caracteres problemáticos (como '|').
    """
    try:
        snap = match.snapshot
    except Exception:
        return "⚠️ Erro ao formatar match (snapshot ausente)."

    league_name = _md_escape(getattr(snap, "league_name", "Liga desconhecida"))
    home = _md_escape(getattr(snap, "home", "HOME?"))
    away = _md_escape(getattr(snap, "away", "AWAY?"))
    starts_at = getattr(snap, "starts_at_utc", None)
    starts_str = _md_escape(_format_datetime_local(starts_at))

    stake_base = float(stake_base if stake_base is not None else CERCO_STAKE_BASE_BRL)

    header_lines: List[str] = []
    header_lines.append(f"🏆 *{league_name}*")
    header_lines.append(f"⚽ *{home}* x *{away}*")
    tz_name = _md_escape(APP_TZ_NAME)
    header_lines.append(f"⏰ {starts_str} \\({tz_name}\\)")

    esnet_url = getattr(match, "esnet_url", None)
    if esnet_url:
        header_lines.append(f"🔗 [EsporteNet]({_md_escape(esnet_url)})")

    header = "\n".join(header_lines)

    opps = getattr(match, "opportunities", None) or []

    # filtra por edge mínima
    filtered = []
    for o in opps:
        try:
            edge = float(getattr(o, "edge", 0.0) or 0.0)
        except Exception:
            edge = 0.0
        if edge >= CERCO_MIN_EDGE:
            filtered.append(o)
    opps = filtered

    if not opps:
        return header + "\n\n_⚠️ Nenhuma arbitragem encontrada para este confronto._"

    body_lines: List[str] = []
    body_lines.append("\n💰 *Oportunidades de CERCO encontradas:*")

    for idx, opp in enumerate(opps, start=1):
        family = _md_escape(getattr(opp, "family", "?"))
        line_val = getattr(opp, "line", None)
        roi = getattr(opp, "roi", None)
        profit = getattr(opp, "profit", None)

        title_parts: List[str] = [f"*{idx}. {family}*"]
        if line_val not in (None, ""):
            title_parts.append(_md_escape(f"(linha {line_val})"))
        if roi is not None:
            try:
                roi_val = float(roi) * 100.0
                title_parts.append(_md_escape(f"ROI≈{roi_val:.2f}%"))
            except Exception:
                pass
        if profit is not None:
            try:
                title_parts.append(_md_escape(f"Lucro≈R$ {float(profit):,.2f}"))
            except Exception:
                pass

        body_lines.append(" ".join(title_parts))

        legs = getattr(opp, "legs", []) or []
        prices: List[float] = []
        for leg in legs:
            price = getattr(leg, "odd", getattr(leg, "price", getattr(leg, "odds", None)))
            try:
                if price and float(price) > 1.0:
                    prices.append(float(price))
            except Exception:
                pass

        stakes: List[float] = []
        if prices and len(prices) >= 2:
            inv_sum = sum(1.0 / p for p in prices)
            if inv_sum > 0:
                stakes = [stake_base * (1.0 / p) / inv_sum for p in prices]

        if not stakes and legs:
            stakes = [stake_base / len(legs) for _ in legs]

        for leg_idx, leg in enumerate(legs):
            book = _md_escape(getattr(leg, "book", getattr(leg, "source", "?")))
            outcome = _md_escape(getattr(leg, "outcome", getattr(leg, "selection", "?")))
            price = getattr(leg, "odd", getattr(leg, "price", getattr(leg, "odds", None)))
            stake_sug = stakes[leg_idx] if leg_idx < len(stakes) else None

            parts: List[str] = [f"   • {book}: *{outcome}*"]
            try:
                if price is not None:
                    parts.append(f"@ {float(price):.3f}")
            except Exception:
                parts.append(f"@ {_md_escape(str(price))}")

            if stake_sug is not None:
                try:
                    parts.append(f"stake ≈ R$ {stake_sug:,.2f}")
                except Exception:
                    pass

            line_txt = " ".join(parts)
            body_lines.append(line_txt)

        body_lines.append("")

    return header + "\n" + "\n".join(body_lines).strip()


# ---------------------------------------------------------------------------
# IMAGEM: versão CERCO
# ---------------------------------------------------------------------------

def _load_pil():
    from PIL import Image, ImageDraw, ImageFont, ImageOps  # noqa
    return Image, ImageDraw, ImageFont, ImageOps


def _font(size: int, bold: bool = False):
    """
    Helper de fonte:
    - tenta usar as fontes configuradas (ex.: Montserrat)
    - cai para DejaVuSans se falhar
    - por fim, usa a fonte default do Pillow
    """
    _, _, ImageFont, _ = _load_pil()
    font_path = FONT_BOLD_PATH if bold else FONT_REGULAR_PATH

    try:
        if font_path:
            return ImageFont.truetype(font_path, size=size)
    except Exception:
        pass

    try:
        fallback = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
        return ImageFont.truetype(fallback, size=size)
    except Exception:
        return ImageFont.load_default()


def _draw_fit_ellipsis(draw, x, y, text, font, max_w, fill, anchor="la"):
    s = str(text or "")
    if max_w <= 0:
        draw.text((x, y), s, fill=fill, font=font, anchor=anchor)
        return
    while s and draw.textlength(s, font=font) > max_w:
        s = s[:-1]
    if s != str(text or ""):
        s = s[:-3] + "..." if len(s) > 3 else "..."
    draw.text((x, y), s, fill=fill, font=font, anchor=anchor)


def _draw_center_wrapped(
    draw,
    x: float,
    y: float,
    text: str,
    font,
    max_w: float,
    fill,
    anchor: str = "mm",
    line_spacing: float = 1.0,
) -> float:
    """
    Desenha o texto centralizado em X, permitindo quebra de linha
    para não usar reticências. Cada nova linha vai PARA BAIXO da anterior.

    Retorna a coordenada Y da ÚLTIMA linha desenhada (centro dessa linha),
    para que possamos posicionar elementos seguintes (VS, segundo time, data, etc)
    logo abaixo, sem sobreposição.
    """
    s = str(text or "").strip()
    if not s:
        draw.text((x, y), s, fill=fill, font=font, anchor=anchor)
        return y

    if max_w <= 0:
        draw.text((x, y), s, fill=fill, font=font, anchor=anchor)
        return y

    words = s.split()
    if not words:
        draw.text((x, y), s, fill=fill, font=font, anchor=anchor)
        return y

    lines: List[str] = []
    current = words[0]
    for w in words[1:]:
        candidate = current + " " + w
        if draw.textlength(candidate, font=font) <= max_w:
            current = candidate
        else:
            lines.append(current)
            current = w
    lines.append(current)

    # Se for uma única palavra gigante (sem espaço) ainda maior que max_w,
    # reduzimos um pouco o texto removendo chars do fim como fallback.
    if len(lines) == 1 and draw.textlength(lines[0], font=font) > max_w:
        temp = lines[0]
        while temp and draw.textlength(temp, font=font) > max_w:
            temp = temp[:-1]
        if temp:
            lines[0] = temp

    last_y = y
    line_h = int(font.size * line_spacing)
    for i, line in enumerate(lines):
        yy = y + i * line_h
        draw.text((x, yy), line, fill=fill, font=font, anchor=anchor)
        last_y = yy

    return last_y


def _badge_from_initials(name: str, size: int = 96):
    Image, ImageDraw, ImageFont, _ = _load_pil()
    bg = (45, 58, 72, 255)
    fg = (232, 236, 242, 255)
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.ellipse([0, 0, size - 1, size - 1], fill=bg)
    initials = "".join([p[0].upper() for p in str(name).split() if p])[:2] or "?"
    f = _font(int(size * 0.42), bold=True)
    d.text((size // 2, size // 2), initials, fill=fg, font=f, anchor="mm")
    return im


def _autocrop_transparent(im, pad: int = 2):
    Image, _, _, _ = _load_pil()
    im = im.convert("RGBA")
    bbox = im.split()[-1].getbbox()
    if not bbox:
        return im
    x0, y0, x1, y1 = bbox
    x0 = max(0, x0 - pad)
    y0 = max(0, y0 - pad)
    x1 = min(im.width, x1 + pad)
    y1 = min(im.height, y1 + pad)
    return im.crop((x0, y0, x1, y1))


def _looks_like_placeholder_or_box(im, tol: int = 8) -> bool:
    Image, _, _, _ = _load_pil()
    try:
        q = im.convert("RGBA").quantize(colors=4, method=Image.FASTOCTREE)
        pal = q.getcolors(maxcolors=1_000_000) or []
        tot = sum(c for c, _ in pal) or 1
        dom_cnt, dom_rgb = sorted(pal, reverse=True)[0]
        coverage = dom_cnt / tot
        if coverage < 0.985:
            return False
        if isinstance(dom_rgb, int):
            return True
        r, g, b = dom_rgb[:3]
        return (
            abs(r - g) <= tol
            and abs(r - b) <= tol
            and abs(g - b) <= tol
        )
    except Exception:
        return False


def _http_get_bytes(url: str, timeout: float = 8.0) -> Optional[bytes]:
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            r = client.get(url)
            if r.status_code != 200:
                return None
            return bytes(r.content)
    except Exception:
        return None


def _open_logo(url_or_path: str | None, *, size: int) -> Any:
    Image, _, _, ImageOps = _load_pil()
    if not url_or_path:
        raise RuntimeError("empty logo")

    s = str(url_or_path)
    if s.startswith("file://"):
        path = s[7:]
        im = Image.open(path).convert("RGBA")
    elif s.startswith("http://") or s.startswith("https://"):
        raw = _http_get_bytes(s)
        if not raw:
            raise RuntimeError("download failed")
        im = Image.open(BytesIO(raw)).convert("RGBA")
    else:
        im = Image.open(s).convert("RGBA")

    if _looks_like_placeholder_or_box(im):
        raise RuntimeError("placeholder logo")

    try:
        im = _autocrop_transparent(im, pad=2)
        im = ImageOps.contain(im, (size, size), Image.LANCZOS)
    except Exception:
        im = im.resize((size, size), Image.LANCZOS)

    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas.paste(
        im,
        ((size - im.width) // 2, (size - im.height) // 2),
        im,
    )
    return canvas


def _safe_logo(
    url_or_path: str | None,
    *,
    size: int,
    fallback_name: str,
) -> Any:
    try:
        return _open_logo(url_or_path, size=size)
    except Exception as e:
        logger.warning(
            "[CERCO] Falha carregando logo '%s': %s; usando badge de iniciais",
            url_or_path,
            e,
        )
        return _badge_from_initials(fallback_name or "?", size=size)


def _family_label(family: str, line: str) -> str:
    """
    Gera rótulo amigável para a família/linha.
    Agora identifica melhor:
    - Moneyline
    - Totals
    - Handicaps
    - Team Totals
    - Padrões cross-family ML x AH ±0.5
    """
    fam_raw = (family or "").strip()
    fam = fam_raw.lower().strip()
    line_s = (line or "").strip()

    # 🎯 Padrões novos: ML x AH +0.5 / -0.5
    if "moneyline vs ah +0.5" in fam:
        # família criada no comparator para:
        #   - CAS(ML EsNet) x AWAY +0.5 (AH Pinnacle)
        #   - AWAY(ML EsNet) x HOME +0.5 (AH Pinnacle)
        return "Moneyline (ESN) × AH +0.5 (PINN)"

    if "moneyline vs ah -0.5" in fam:
        # família criada no comparator para combinações 3-vias:
        #   - HOME/DRAW/AWAY-0.5 etc.
        return "Moneyline (ESN) × AH -0.5 (PINN)"

    # Moneyline “normal”
    if fam in {"money_line", "1x2", "moneyline", "moneyline_ft"}:
        return "Moneyline (1X2)"

    # Totals gerais
    if fam in {"totals", "total", "goals_total", "totals_goals_ft"}:
        return f"Totals {line_s}".strip()

    # Spreads/handicaps
    if fam in {"spread", "spreads", "handicap", "asian_handicap_ft"}:
        return f"Handicap {line_s}".strip()

    # Team totals (home/away, com ou sem sufixos extras)
    if "team_total" in fam:
        side = ""
        if "home" in fam:
            side = "Home"
        elif "away" in fam:
            side = "Away"
        base = "Team Total"
        if side:
            base += f" {side}"
        return f"{base} {line_s}".strip()

    # Sufixo "_crossline" (caso você volte a ativar o bloco de cross-line de totais)
    if fam.endswith("_crossline"):
        base = fam_raw[: -len("_crossline")]
        return f"{base} (cross-line) {line_s}".strip()

    # Fallback padrão
    return f"{family} {line_s}".strip()



def render_cerco_story_images(
    match: Any,
    *,
    stake_base: float | None = None,
) -> List[bytes]:
    """
    Gera 1..N imagens PNG 1080x1920 (formato story) com o MESMO cabeçalho do evento
    e divide as oportunidades em múltiplas páginas quando não couberem em uma única imagem.

    Retorna uma lista de PNGs (bytes), na ordem correta para envio.
    """
    pages: List[bytes] = []

    try:
        Image, ImageDraw, _, ImageOps = _load_pil()

        W, H = 1080, 1920
        PAD_X, PAD_TOP = 48, 28
        BG = (14, 25, 36)
        FG = (232, 236, 242)
        FG_DIM = (178, 190, 206)
        ACCENT = (110, 190, 255)
        COMMISSION_COLOR = (255, 180, 80)   # laranja
        SURPLUS_COLOR = (120, 230, 140)     # verde

        LOGO_TEAM = 180
        LOGO_BOOK_PINN = BOOK_LOGO_PINNACLE_SIZE
        LOGO_BOOK_ESN = BOOK_LOGO_ESPORTENET_SIZE

        # Times maiores; fontes auxiliares
        f_team = _font(46, bold=True)
        f_meta = _font(36, bold=False)   # liga + data
        f_time = _font(44, bold=True)    # horário (maior)
        # Título "Oportunidades de CERCO" um pouco maior
        f_sect = _font(44, bold=True)
        f_line = _font(44, bold=False)
        f_small = _font(34, bold=False)

        snap = getattr(match, "snapshot", None)
        if snap is None:
            return pages

        league_name = str(getattr(snap, "league_name", "") or "Liga")
        home = str(getattr(snap, "home", "Home"))
        away = str(getattr(snap, "away", "Away"))
        starts_at = getattr(snap, "starts_at_utc", None)
        starts_str = _format_datetime_local(starts_at)
        if " " in starts_str:
            date_part, time_part = starts_str.split(" ", 1)
        else:
            date_part, time_part = starts_str, ""

        stake_base = float(
            stake_base if stake_base is not None else CERCO_STAKE_BASE_BRL
        )

        esmeta = getattr(match, "esnet_meta", {}) or {}
        home_logo_src = esmeta.get("home_logo") or getattr(
            snap,
            "home_logo",
            None,
        )
        away_logo_src = esmeta.get("away_logo") or getattr(
            snap,
            "away_logo",
            None,
        )

        home_logo = _safe_logo(home_logo_src, size=LOGO_TEAM, fallback_name=home)
        away_logo = _safe_logo(away_logo_src, size=LOGO_TEAM, fallback_name=away)

        pinn_logo = _safe_logo(
            LOGO_PINNACLE_PATH,
            size=LOGO_BOOK_PINN,
            fallback_name="P",
        )
        esnet_logo = _safe_logo(
            LOGO_ESPORTENET_PATH,
            size=LOGO_BOOK_ESN,
            fallback_name="E",
        )

        # ----------------------------
        # Pré-processa oportunidades
        # ----------------------------
        opps = getattr(match, "opportunities", None) or []

        # filtra por edge mínima também na imagem
        filtered = []
        for o in opps:
            try:
                edge = float(getattr(o, "edge", 0.0) or 0.0)
            except Exception:
                edge = 0.0
            if edge >= CERCO_MIN_EDGE:
                filtered.append(o)
        opps = filtered

        if not opps:
            # Render de 1 página com "Nenhuma arbitragem"
            img = Image.new("RGB", (W, H), BG)
            draw = ImageDraw.Draw(img)
            y = _draw_story_header(
                img,
                draw,
                W=W,
                H=H,
                PAD_X=PAD_X,
                PAD_TOP=PAD_TOP,
                BG=BG,
                FG=FG,
                FG_DIM=FG_DIM,
                ACCENT=ACCENT,
                LOGO_TEAM=LOGO_TEAM,
                f_team=f_team,
                f_meta=f_meta,
                f_time=f_time,
                league_name=league_name,
                home=home,
                away=away,
                date_part=date_part,
                time_part=time_part,
                home_logo=home_logo,
                away_logo=away_logo,
            )
            draw.text(
                (PAD_X, y),
                "Nenhuma arbitragem encontrada.",
                fill=FG_DIM,
                font=f_sect,
                anchor="la",
            )
            _draw_story_footer(draw, W=W, H=H, PAD_X=PAD_X, FG_DIM=FG_DIM, f_small=f_small, stake_base=stake_base)
            out = BytesIO()
            img.save(out, format="PNG")
            pages.append(out.getvalue())
            return pages

        # Ordena por ROI (desc)
        try:
            opps = sorted(
                opps,
                key=lambda o: float(getattr(o, "roi", 0.0) or 0.0),
                reverse=True,
            )
        except Exception:
            pass

        # ----------------------------
        # Paginador (por estimativa)
        # ----------------------------
        FOOTER_RESERVED = 90  # espaço para o rodapé (e respiro)
        LIMIT_Y = H - FOOTER_RESERVED

        def estimate_opp_height(opp: Any) -> int:
            # Cabeçalho da oportunidade
            h = f_line.size + 6

            # ROI (1 linha) + (label)
            h += f_small.size + 4

            # Lucro (1 linha) + respiro
            h += f_small.size + 22

            # Pernas
            legs = getattr(opp, "legs", []) or []
            h += len(legs) * (f_line.size + 8)

            # Espaços finais (entre blocos)
            h += 18 + 18
            return int(h)

        # Helpers de desenho para cada página
        def new_page() -> tuple[Any, Any, int]:
            img = Image.new("RGB", (W, H), BG)
            draw = ImageDraw.Draw(img)
            y = _draw_story_header(
                img,
                draw,
                W=W,
                H=H,
                PAD_X=PAD_X,
                PAD_TOP=PAD_TOP,
                BG=BG,
                FG=FG,
                FG_DIM=FG_DIM,
                ACCENT=ACCENT,
                LOGO_TEAM=LOGO_TEAM,
                f_team=f_team,
                f_meta=f_meta,
                f_time=f_time,
                league_name=league_name,
                home=home,
                away=away,
                date_part=date_part,
                time_part=time_part,
                home_logo=home_logo,
                away_logo=away_logo,
            )

            draw.text(
                (PAD_X, y),
                "Oportunidades de CERCO",
                fill=FG,
                font=f_sect,
                anchor="la",
            )
            y += f_sect.size + 20
            return img, draw, y

        def finalize_page(img, draw):
            _draw_story_footer(draw, W=W, H=H, PAD_X=PAD_X, FG_DIM=FG_DIM, f_small=f_small, stake_base=stake_base)
            out = BytesIO()
            img.save(out, format="PNG")
            pages.append(out.getvalue())

        img, draw, y = new_page()

        page_opp_index = 0
        global_idx = 0

        for global_idx, opp in enumerate(opps, start=1):
            need = estimate_opp_height(opp)
            if y + need > LIMIT_Y and page_opp_index > 0:
                finalize_page(img, draw)
                img, draw, y = new_page()
                page_opp_index = 0

            page_opp_index += 1

            fam = str(getattr(opp, "family", ""))
            line = str(getattr(opp, "line", "") or "")
            roi = getattr(opp, "roi", None)
            profit = getattr(opp, "profit", None)

            label = _family_label(fam, line)
            head = f"{global_idx}. {label}"

            draw.text(
                (PAD_X, y),
                head,
                fill=FG,
                font=f_line,
                anchor="la",
            )
            y += f_line.size + 6

            # ROI + etiqueta (mesma linha)
            roi_value: Optional[float] = None
            try:
                if roi is not None:
                    roi_value = float(roi)
            except Exception:
                roi_value = None

            x_meta = PAD_X + 40

            if roi_value is not None:
                roi_text = f"ROI ≈ {roi_value * 100:.2f}%"
                draw.text(
                    (x_meta, y),
                    roi_text,
                    fill=FG_DIM,
                    font=f_small,
                    anchor="la",
                )
                x_meta += draw.textlength(roi_text, font=f_small)

                if roi_value >= 0.0:
                    if roi_value < 0.005:
                        label_text = " (Cerco só com Comissão)"
                        label_color = COMMISSION_COLOR
                    else:
                        label_text = " (Cerco com Sobra)"
                        label_color = SURPLUS_COLOR

                    draw.text(
                        (x_meta, y),
                        label_text,
                        fill=label_color,
                        font=f_small,
                        anchor="la",
                    )

                y += f_small.size + 4
            else:
                y += f_small.size + 4

            # Lucro em uma linha logo abaixo
            if profit is not None:
                try:
                    profit_text = f"Lucro ≈ R$ {float(profit):,.2f}"
                except Exception:
                    profit_text = None

                if profit_text:
                    draw.text(
                        (PAD_X + 40, y),
                        profit_text,
                        fill=FG_DIM,
                        font=f_small,
                        anchor="la",
                    )
                    y += f_small.size + 22
                else:
                    y += 18
            else:
                y += 18

            legs = getattr(opp, "legs", []) or []

            for leg in legs:
                book = str(getattr(leg, "book", getattr(leg, "source", "")) or "")
                outcome = str(getattr(leg, "outcome", getattr(leg, "selection", "")) or "")
                odd = getattr(leg, "odd", getattr(leg, "price", getattr(leg, "odds", None)))
                stake = getattr(leg, "stake", None)

                book_upper = book.upper()
                is_pinn = "PINN" in book_upper
                logo_img = pinn_logo if is_pinn else esnet_logo
                logo_size = LOGO_BOOK_PINN if is_pinn else LOGO_BOOK_ESN

                line_y = y
                logo_x = PAD_X + 20
                logo_y = line_y + (f_line.size - logo_size) // 2
                img.paste(logo_img, (logo_x, logo_y), logo_img)

                col_outcome_x = logo_x + logo_size + 16
                col_odd_x = col_outcome_x + 300
                col_stake_x = col_odd_x + 220

                draw.text(
                    (col_outcome_x, line_y),
                    outcome,
                    fill=FG,
                    font=f_line,
                    anchor="la",
                )

                if odd is not None:
                    try:
                        odd_txt = f"@ {float(odd):.3f}"
                    except Exception:
                        odd_txt = f"@ {odd}"
                    draw.text(
                        (col_odd_x, line_y),
                        odd_txt,
                        fill=FG,
                        font=f_line,
                        anchor="la",
                    )

                if stake is not None:
                    try:
                        stake_txt = f"R$ {float(stake):,.2f}"
                    except Exception:
                        stake_txt = f"R$ {stake}"
                    draw.text(
                        (col_stake_x, line_y),
                        stake_txt,
                        fill=FG,
                        font=f_line,
                        anchor="la",
                    )

                y += f_line.size + 8

            y += 18
            y += 18

        finalize_page(img, draw)
        return pages

    except Exception:
        logger.exception("[CERCO] Falha ao gerar imagens story (multi-page).")
        return pages


def _draw_story_header(
    img,
    draw,
    *,
    W: int,
    H: int,
    PAD_X: int,
    PAD_TOP: int,
    BG,
    FG,
    FG_DIM,
    ACCENT,
    LOGO_TEAM: int,
    f_team,
    f_meta,
    f_time,
    league_name: str,
    home: str,
    away: str,
    date_part: str,
    time_part: str,
    home_logo,
    away_logo,
) -> int:
    """
    Desenha o cabeçalho do evento e retorna o Y inicial do conteúdo.
    """
    # Logos dos times
    head_cy = PAD_TOP + LOGO_TEAM // 2 + 60
    img.paste(home_logo, (PAD_X, head_cy - LOGO_TEAM // 2), home_logo)
    img.paste(away_logo, (W - PAD_X - LOGO_TEAM, head_cy - LOGO_TEAM // 2), away_logo)

    # Limites horizontais para o texto não invadir as logos
    TEXT_MARGIN_FROM_LOGO = 16
    text_left_limit = PAD_X + LOGO_TEAM + TEXT_MARGIN_FROM_LOGO
    text_right_limit = W - PAD_X - LOGO_TEAM - TEXT_MARGIN_FROM_LOGO
    title_max_w = text_right_limit - text_left_limit

    # Liga
    draw.text(
        (W // 2, head_cy - 100),
        league_name,
        fill=FG_DIM,
        font=f_meta,
        anchor="mm",
    )

    # Home (quebra linhas)
    home_center_y = head_cy - 20
    last_home_y = _draw_center_wrapped(
        draw,
        W // 2,
        home_center_y,
        home,
        f_team,
        title_max_w,
        FG,
        anchor="mm",
        line_spacing=1.05,
    )

    # VS
    vs_gap = int(f_team.size * 1.2)
    vs_y = last_home_y + vs_gap
    draw.text((W // 2, vs_y), "VS", fill=ACCENT, font=_font(42, bold=True), anchor="mm")

    # Away (quebra linhas)
    away_gap = int(f_team.size * 1.2)
    away_center_y = vs_y + away_gap
    last_away_y = _draw_center_wrapped(
        draw,
        W // 2,
        away_center_y,
        away,
        f_team,
        title_max_w,
        FG,
        anchor="mm",
        line_spacing=1.05,
    )

    # Data/Hora
    block_gap = int(f_team.size * 1.5)
    base_y = last_away_y + block_gap

    draw.text((W // 2, base_y), date_part, fill=FG_DIM, font=f_meta, anchor="mm")

    if time_part:
        TIME_BG = (24, 52, 88)
        time_y = base_y + f_meta.size + 16

        bbox = draw.textbbox((0, 0), time_part, font=f_time)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        pad_x = 24
        pad_y = 12
        cx = W // 2

        rect = (
            cx - tw / 2 - pad_x,
            time_y - th / 2 - pad_y,
            cx + tw / 2 + pad_x,
            time_y + th / 2 + pad_y,
        )
        try:
            draw.rounded_rectangle(rect, radius=18, fill=TIME_BG)
        except Exception:
            draw.rectangle(rect, fill=TIME_BG)

        draw.text((cx, time_y), time_part, fill=FG, font=f_time, anchor="mm")

    # Separador
    sep_y = base_y + f_meta.size + 80
    draw.line([(PAD_X, sep_y), (W - PAD_X, sep_y)], fill=(28, 40, 52), width=5)

    return sep_y + 32


def _draw_story_footer(draw, *, W: int, H: int, PAD_X: int, FG_DIM, f_small, stake_base: float) -> None:
    footer = f"Stake base: R$ {stake_base:,.2f}   •   CERCO"
    draw.text((W - PAD_X, H - 32), footer, fill=FG_DIM, font=f_small, anchor="rd")


def render_cerco_story_image(
    match: Any,
    *,
    stake_base: float | None = None,
) -> Optional[bytes]:
    """
    Compat: mantém a assinatura antiga e retorna apenas a PRIMEIRA página.
    Use render_cerco_story_images() para obter todas as páginas.
    """
    pages = render_cerco_story_images(match, stake_base=stake_base)
    return pages[0] if pages else None



def send_match_result(
    match: Any,
    *,
    stake_base: float | None = None,
    with_image: bool = True,
) -> None:
    """
    Envia:
      1) imagem story (se habilitada e gerada)
      2) texto formatado
    """
    caption = None
    if with_image:
        pages = render_cerco_story_images(match, stake_base=stake_base)
        for i, img_bytes in enumerate(pages, start=1):
            if img_bytes:
                send_photo(img_bytes, caption=caption)

    text = format_match_message(match, stake_base=stake_base)
    send_message(text)
