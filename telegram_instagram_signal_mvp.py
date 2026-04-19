import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional, Literal, Union

import requests
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator
from PIL import Image, ImageDraw, ImageFont, ImageFilter
import cloudinary
import cloudinary.uploader


# =========================
# CONFIG
# =========================
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "super-secret-key")
BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "generated_posts"
LOGOS_DIR = BASE_DIR / "logos"

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME", ""),
    api_key=os.getenv("CLOUDINARY_API_KEY", ""),
    api_secret=os.getenv("CLOUDINARY_API_SECRET", ""),
)

IG_USER_ID = os.getenv("IG_USER_ID", "")
ACCESS_TOKEN = os.getenv("INSTAGRAM_ACCESS_TOKEN", "")

INSTAGRAM_APP_ID = os.getenv("INSTAGRAM_APP_ID", "")
INSTAGRAM_APP_SECRET = os.getenv("INSTAGRAM_APP_SECRET", "")
INSTAGRAM_REDIRECT_URI = os.getenv("INSTAGRAM_REDIRECT_URI", "")

app = FastAPI(title="Telegram Signal Image Generator MVP")


# =========================
# MODELS
# =========================
class SignalData(BaseModel):
    message_type: Literal["signal"] = "signal"
    symbol: str
    side: Literal["buy", "sell"]
    price: float
    amount: float
    currency: str = "USDT"
    timeframe: str
    rsi: float

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, value: str) -> str:
        value = value.strip().upper()
        if not re.fullmatch(r"[A-Z0-9]{2,20}", value):
            raise ValueError("Geçersiz coin sembolü")
        return value

    @field_validator("timeframe")
    @classmethod
    def validate_timeframe(cls, value: str) -> str:
        value = value.strip().upper()
        if not re.fullmatch(r"[0-9A-Z]{1,10}", value):
            raise ValueError("Geçersiz timeframe")
        return value

    @field_validator("currency")
    @classmethod
    def validate_currency(cls, value: str) -> str:
        value = value.strip().upper()
        if not re.fullmatch(r"[A-Z]{3,10}", value):
            raise ValueError("Geçersiz currency")
        return value


class PnlMetric(BaseModel):
    label: str
    value: float
    currency: str = "USDT"


class SpotItem(BaseModel):
    symbol: str
    value: float
    currency: str = "USDT"


class PnlReportData(BaseModel):
    message_type: Literal["pnl_report"] = "pnl_report"
    pnl_metrics: list[PnlMetric]
    spot_items: list[SpotItem]


ParsedMessage = Union[SignalData, PnlReportData]


# =========================
# PARSER
# =========================
def parse_signal_message(text: str) -> SignalData:
    raw = text.strip()

    pattern = re.compile(
        r"^(?:[^\w\n\r]+)?\s*(?P<side>ALIM|SATIM|SATIŞ)\s*[\r\n]+"
        r"(?P<symbol>[A-Z0-9]{2,20})\s*[\r\n]+"
        r"(?P<timeframe>[0-9A-Z]+)\s+Close:\s*(?P<price>[0-9]+(?:[\.,][0-9]+)?)\s*[\r\n]+"
        r"RSI:\s*(?P<rsi>[0-9]+(?:[\.,][0-9]+)?)\s*[\r\n]+"
        r"(?P<label>Alım|Alınan|Satım|Satılan):\s*(?P<amount>[0-9]+(?:[\.,][0-9]+)?)\s*(?P<currency>[A-Z]{3,10})\s*$",
        flags=re.IGNORECASE | re.MULTILINE,
    )

    match = pattern.search(raw)
    if not match:
        raise ValueError("Signal format tanınmadı")

    gd = match.groupdict()
    side = "buy" if gd["side"].upper() == "ALIM" else "sell"

    return SignalData(
        symbol=gd["symbol"].upper(),
        side=side,
        price=float(gd["price"].replace(",", ".")),
        amount=float(gd["amount"].replace(",", ".")),
        currency=gd["currency"].upper(),
        timeframe=gd["timeframe"].upper(),
        rsi=float(gd["rsi"].replace(",", ".")),
    )


def parse_pnl_report_message(text: str) -> PnlReportData:
    raw = text.strip()

    if "PNL RAPORU" not in raw.upper():
        raise ValueError("PNL raporu değil")

    pnl_pattern = re.compile(
        r"(?P<label>\d+\s*Gün)\s*:\s*(?P<value>[+-]?[0-9]+(?:[\.,][0-9]+)?)\s*(?P<currency>[A-Z]{3,10})",
        flags=re.IGNORECASE,
    )

    spot_pattern = re.compile(
        r"^(?P<symbol>[A-Z0-9]+)\s*:\s*(?P<value>[+-]?[0-9]+(?:[\.,][0-9]+)?)\s*(?P<currency>[A-Z]{3,10})\s*$",
        flags=re.IGNORECASE | re.MULTILINE,
    )

    pnl_metrics = []
    for m in pnl_pattern.finditer(raw):
        pnl_metrics.append(
            PnlMetric(
                label=m.group("label").replace("  ", " ").strip(),
                value=float(m.group("value").replace(",", ".")),
                currency=m.group("currency").upper(),
            )
        )

    if not pnl_metrics:
        raise ValueError("PNL metrikleri bulunamadı")

    spot_section_match = re.search(r"SPOT DURUMU([\s\S]*)$", raw, flags=re.IGNORECASE)
    spot_text = spot_section_match.group(1).strip() if spot_section_match else ""

    spot_items = []
    for m in spot_pattern.finditer(spot_text):
        spot_items.append(
            SpotItem(
                symbol=m.group("symbol").upper(),
                value=float(m.group("value").replace(",", ".")),
                currency=m.group("currency").upper(),
            )
        )

    if not spot_items:
        raise ValueError("Spot durumu bulunamadı")

    return PnlReportData(
        pnl_metrics=pnl_metrics,
        spot_items=spot_items,
    )


def parse_message(text: str) -> ParsedMessage:
    raw = text.strip()
    if "PNL RAPORU" in raw.upper():
        return parse_pnl_report_message(raw)
    return parse_signal_message(raw)


# =========================
# IMAGE HELPERS
# =========================
def _load_font(size: int, bold: bool = False):
    candidates = [
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/Library/Fonts/Arial Bold.ttf" if bold else "/Library/Fonts/Arial.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def _format_number(value: float) -> str:
    if float(value).is_integer():
        return f"{int(value):,}".replace(",", ".")
    return (
        f"{value:,.4f}"
        .rstrip("0")
        .rstrip(".")
        .replace(",", "X")
        .replace(".", ",")
        .replace("X", ".")
    )


def _make_gradient_background(width, height, top_color, bottom_color):
    image = Image.new("RGB", (width, height), top_color)
    draw = ImageDraw.Draw(image)
    for y in range(height):
        t = y / max(height - 1, 1)
        r = int(top_color[0] + (bottom_color[0] - top_color[0]) * t)
        g = int(top_color[1] + (bottom_color[1] - top_color[1]) * t)
        b = int(top_color[2] + (bottom_color[2] - top_color[2]) * t)
        draw.line((0, y, width, y), fill=(r, g, b))
    return image


def _add_soft_glow(image, circles):
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    for bbox, color in circles:
        draw.ellipse(bbox, fill=color)
    overlay = overlay.filter(ImageFilter.GaussianBlur(90))
    return Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")


def _add_rounded_panel(draw, box, fill, outline=None, radius=36, width=2):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def _extract_base_symbol(full_symbol: str) -> str:
    symbol = full_symbol.upper().strip()
    quote_assets = ["USDT", "BUSD", "USDC", "BTC", "ETH", "TRY"]
    for quote in quote_assets:
        if symbol.endswith(quote) and len(symbol) > len(quote):
            return symbol[: -len(quote)]
    return symbol


def _get_logo_path(full_symbol: str) -> Optional[Path]:
    base_symbol = _extract_base_symbol(full_symbol)
    path = LOGOS_DIR / f"{base_symbol}.png"
    if path.exists():
        return path
    return None


def _add_logo_watermark(
    image: Image.Image,
    logo_path: Optional[Path],
    target_box=(450, 150, 980, 760),
    opacity=170,
    blur_radius=0.4,
):
    if not logo_path or not logo_path.exists():
        return image

    try:
        logo = Image.open(logo_path).convert("RGBA")
    except Exception:
        return image

    _, _, _, alpha_src = logo.split()
    white_logo = Image.new("RGBA", logo.size, (255, 255, 255, 255))
    white_logo.putalpha(alpha_src)
    logo = white_logo

    max_w = target_box[2] - target_box[0]
    max_h = target_box[3] - target_box[1]

    ratio = min(max_w / logo.width, max_h / logo.height)
    new_size = (max(1, int(logo.width * ratio)), max(1, int(logo.height * ratio)))
    logo = logo.resize(new_size, Image.LANCZOS)

    alpha = logo.getchannel("A")
    alpha = alpha.point(lambda p: int(p * opacity / 255))
    logo.putalpha(alpha)

    if blur_radius > 0:
        logo = logo.filter(ImageFilter.GaussianBlur(blur_radius))

    glow = logo.copy().filter(ImageFilter.GaussianBlur(24))
    glow_alpha = glow.getchannel("A").point(lambda p: int(p * 0.65))
    glow.putalpha(glow_alpha)

    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    x = target_box[0] + (max_w - logo.width) // 2
    y = target_box[1] + (max_h - logo.height) // 2

    overlay.paste(glow, (x, y), glow)
    overlay.paste(logo, (x, y), logo)

    return Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")


def _draw_small_logo(
    image: Image.Image,
    symbol: str,
    x: int,
    y: int,
    size: int = 28,
):
    logo_path = _get_logo_path(symbol)
    if not logo_path or not logo_path.exists():
        return image

    try:
        logo = Image.open(logo_path).convert("RGBA")
    except Exception:
        return image

    max_size = (size, size)
    logo.thumbnail(max_size, Image.LANCZOS)

    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    paste_x = x
    paste_y = y + (size - logo.height) // 2
    overlay.paste(logo, (paste_x, paste_y), logo)

    return Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")


def upload_to_cloudinary(image_path: str) -> str:
    result = cloudinary.uploader.upload(image_path)
    return result["secure_url"]


def build_instagram_caption(parsed: ParsedMessage) -> str:
    if isinstance(parsed, SignalData):
        side_text = "ALIM" if parsed.side == "buy" else "SATIŞ"
        return (
            f"BKN Strategy Signals\n\n"
            f"{parsed.symbol} {side_text} sinyali\n"
            f"Close Price: {_format_number(parsed.price)} {parsed.currency}\n"
            f"RSI: {parsed.rsi:.2f}\n"
            f"Miktar: {_format_number(parsed.amount)} {parsed.currency}\n"
            f"Timeframe: {parsed.timeframe}\n\n"
            f"#crypto #{parsed.symbol.replace('USDT', '').lower()} #trading #signals"
        )

    pnl_lines = [f"{item.label}: {('+' if item.value > 0 else '')}{_format_number(item.value)} {item.currency}" for item in parsed.pnl_metrics]
    return (
        "BKN Strategy Signals\n\n"
        "PNL RAPORU\n"
        + "\n".join(pnl_lines)
        + "\n\n#crypto #pnl #trading #signals"
    )


def post_to_instagram(image_url: str, caption: str) -> dict:
    if not ACCESS_TOKEN:
        raise ValueError("Instagram access token girilmemiş")

    create_url = f"https://graph.facebook.com/v19.0/{IG_USER_ID}/media"
    create_payload = {
        "image_url": image_url,
        "caption": caption,
        "access_token": ACCESS_TOKEN,
    }

    create_response = requests.post(create_url, data=create_payload, timeout=60)
    create_data = create_response.json()
    print("MEDIA RESPONSE:", create_data)

    creation_id = create_data.get("id")
    if not creation_id:
        raise ValueError(f"Instagram media oluşturulamadı: {create_data}")

    publish_url = f"https://graph.facebook.com/v19.0/{IG_USER_ID}/media_publish"
    publish_payload = {
        "creation_id": creation_id,
        "access_token": ACCESS_TOKEN,
    }

    publish_response = requests.post(publish_url, data=publish_payload, timeout=60)
    publish_data = publish_response.json()
    print("PUBLISH RESPONSE:", publish_data)

    if "id" not in publish_data:
        raise ValueError(f"Instagram publish başarısız: {publish_data}")

    return {
        "creation_id": creation_id,
        "publish_id": publish_data.get("id"),
        "media_response": create_data,
        "publish_response": publish_data,
    }


# =========================
# INSTAGRAM LOGIN HELPERS
# =========================
@app.get("/exchange-code")
def exchange_code(code: str):
    url = "https://api.instagram.com/oauth/access_token"

    data = {
        "client_id": INSTAGRAM_APP_ID,
        "client_secret": INSTAGRAM_APP_SECRET,
        "grant_type": "authorization_code",
        "redirect_uri": INSTAGRAM_REDIRECT_URI,
        "code": code,
    }

    r = requests.post(url, data=data, timeout=60)
    return r.json()


# =========================
# SIGNAL IMAGE
# =========================
def generate_signal_image(signal: SignalData) -> Path:
    width, height = 1080, 1350
    is_buy = signal.side == "buy"

    top_bg = (9, 12, 18)
    bottom_bg = (16, 20, 30)
    card_fill = (16, 20, 28)
    card_outline = (54, 62, 80)
    section_fill = (20, 26, 36)
    section_outline = (58, 68, 88)

    if is_buy:
        accent = (82, 220, 156)
        accent_soft = (82, 220, 156, 26)
        badge_fill = (24, 110, 78)
        tag_text = "ALIM"
        amount_label = "Alım"
        side_text = "Oversold / Entry"
    else:
        accent = (242, 108, 108)
        accent_soft = (242, 108, 108, 26)
        badge_fill = (148, 44, 60)
        tag_text = "SATIŞ"
        amount_label = "Satılan"
        side_text = "Exit / Sell"

    text_main = (245, 248, 246)
    text_muted = (154, 164, 178)
    text_soft = (210, 216, 224)

    image = _make_gradient_background(width, height, top_bg, bottom_bg)
    image = _add_soft_glow(
        image,
        [
            ((-180, -120, 360, 420), accent_soft),
            ((760, 20, 1200, 420), (255, 255, 255, 10)),
        ],
    )

    draw = ImageDraw.Draw(image)
    _add_rounded_panel(draw, (60, 60, 1020, 1290), fill=card_fill, outline=card_outline, radius=42, width=2)

    logo_path = _get_logo_path(signal.symbol)
    image = _add_logo_watermark(
        image,
        logo_path=logo_path,
        target_box=(420, 130, 980, 760),
        opacity=185,
        blur_radius=0.25,
    )
    draw = ImageDraw.Draw(image)

    f_brand = _load_font(32, True)
    f_symbol = _load_font(100, True)
    f_badge = _load_font(32, True)
    f_sub = _load_font(28, True)
    f_label = _load_font(26, False)
    f_value_big = _load_font(54, True)
    f_value = _load_font(38, True)
    f_footer = _load_font(24, False)

    _add_rounded_panel(draw, (100, 430, 980, 710), fill=section_fill, outline=section_outline, radius=28, width=2)
    _add_rounded_panel(draw, (100, 770, 980, 1165), fill=section_fill, outline=section_outline, radius=28, width=2)

    draw.text((100, 95), "BKN Strategy Signals", font=f_brand, fill=text_main)

    _add_rounded_panel(draw, (790, 88, 970, 154), fill=badge_fill, outline=None, radius=22, width=1)
    draw.text((835, 102), tag_text, font=f_badge, fill=(255, 255, 255))

    draw.text((100, 195), signal.symbol, font=f_symbol, fill=text_main)
    draw.text((104, 314), f"{signal.timeframe} close signal", font=f_sub, fill=text_soft)

    draw.text((130, 470), "Close Price", font=f_label, fill=text_muted)
    draw.text((130, 508), f"{_format_number(signal.price)} {signal.currency}", font=f_value_big, fill=accent)

    draw.text((130, 618), "RSI", font=f_label, fill=text_muted)
    draw.text((130, 652), f"{signal.rsi:.2f}", font=f_value, fill=text_main)

    draw.text((620, 470), amount_label, font=f_label, fill=text_muted)
    draw.text((620, 508), f"{_format_number(signal.amount)} {signal.currency}", font=f_value, fill=text_main)

    draw.text((620, 618), "Timeframe", font=f_label, fill=text_muted)
    draw.text((620, 652), signal.timeframe, font=f_value, fill=text_main)

    draw.text((130, 808), "Signal Overview", font=f_sub, fill=text_main)

    info_rows = [
        ("Signal Type", tag_text),
        ("Market", signal.symbol),
        ("Trade Bias", side_text),
        ("Generated", datetime.now().strftime("%d.%m.%Y  %H:%M")),
    ]

    row_y = 884
    for label, value in info_rows:
        draw.text((130, row_y), label, font=f_label, fill=text_muted)
        draw.text((500, row_y), value, font=f_value, fill=text_main)
        row_y += 68

    draw.text((100, 1215), "Auto-generated premium trade signal visual", font=f_footer, fill=text_soft)

    filename = f"{signal.symbol}_{signal.side}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}.png"
    output_path = OUTPUT_DIR / filename
    image.save(output_path)
    return output_path


# =========================
# PNL REPORT IMAGE
# =========================
def generate_pnl_report_image(report: PnlReportData) -> Path:
    width, height = 1080, 1350

    top_bg = (10, 14, 22)
    bottom_bg = (16, 22, 34)
    card_fill = (18, 24, 36)
    card_outline = (54, 68, 92)
    section_fill = (22, 30, 42)
    section_outline = (62, 80, 108)
    accent_soft = (255, 194, 92, 25)
    text_main = (245, 248, 246)
    text_muted = (170, 180, 196)
    positive = (82, 220, 156)
    negative = (242, 108, 108)

    image = _make_gradient_background(width, height, top_bg, bottom_bg)
    image = _add_soft_glow(
        image,
        [
            ((-120, -100, 420, 420), accent_soft),
            ((720, 40, 1180, 420), (110, 170, 255, 20)),
        ],
    )
    draw = ImageDraw.Draw(image)

    f_brand = _load_font(32, True)
    f_title = _load_font(58, True)
    f_section = _load_font(30, True)
    f_label = _load_font(26, False)
    f_value = _load_font(30, True)
    f_metric = _load_font(34, True)
    f_footer = _load_font(24, False)

    _add_rounded_panel(draw, (60, 60, 1020, 1290), fill=card_fill, outline=card_outline, radius=42, width=2)
    _add_rounded_panel(draw, (100, 340, 980, 620), fill=section_fill, outline=section_outline, radius=28, width=2)
    _add_rounded_panel(draw, (100, 680, 980, 1180), fill=section_fill, outline=section_outline, radius=28, width=2)

    draw.text((100, 95), "BKN Strategy Signals", font=f_brand, fill=text_main)
    draw.text((100, 210), "PNL Report", font=f_title, fill=text_main)
    draw.text((102, 285), "Portfolio performance snapshot", font=f_section, fill=(210, 215, 224))

    draw.text((130, 372), "Performance", font=f_section, fill=text_main)

    metric_positions = [
        (130, 450),
        (130, 535),
        (560, 450),
        (560, 535),
    ]

    for idx, item in enumerate(report.pnl_metrics[:4]):
        if idx >= len(metric_positions):
            break
        x, y = metric_positions[idx]
        color = positive if item.value >= 0 else negative
        prefix = "+" if item.value > 0 else ""
        draw.text((x, y), item.label, font=f_label, fill=text_muted)
        draw.text((x, y + 34), f"{prefix}{_format_number(item.value)} {item.currency}", font=f_metric, fill=color)

    draw.text((130, 712), "Spot Durumu", font=f_section, fill=text_main)

    max_items = 14
    spot_items = report.spot_items[:max_items]

    left_x_logo = 130
    left_x_label = 170
    left_x_value = 280

    right_x_logo = 580
    right_x_label = 620
    right_x_value = 730

    row_height = 54
    base_y = 790

    for idx, item in enumerate(spot_items):
        if idx < 7:
            y = base_y + idx * row_height
            image = _draw_small_logo(image, item.symbol, left_x_logo, y + 2, size=28)
            draw = ImageDraw.Draw(image)
            draw.text((left_x_label, y), item.symbol, font=f_label, fill=text_muted)
            draw.text((left_x_value, y), f"{_format_number(item.value)} {item.currency}", font=f_value, fill=text_main)
        else:
            y = base_y + (idx - 7) * row_height
            image = _draw_small_logo(image, item.symbol, right_x_logo, y + 2, size=28)
            draw = ImageDraw.Draw(image)
            draw.text((right_x_label, y), item.symbol, font=f_label, fill=text_muted)
            draw.text((right_x_value, y), f"{_format_number(item.value)} {item.currency}", font=f_value, fill=text_main)

    if len(report.spot_items) > max_items:
        draw.text(
            (130, 1188),
            f"+ {len(report.spot_items) - max_items} diğer varlık",
            font=f_footer,
            fill=text_muted,
        )
    else:
        draw.text((130, 1188), "Auto-generated premium pnl report visual", font=f_footer, fill=(210, 214, 220))

    filename = f"PNL_REPORT_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}.png"
    output_path = OUTPUT_DIR / filename
    image.save(output_path)
    return output_path


def generate_image(parsed: ParsedMessage) -> Path:
    if isinstance(parsed, PnlReportData):
        return generate_pnl_report_image(parsed)
    return generate_signal_image(parsed)


# =========================
# TELEGRAM HELPERS
# =========================
def verify_telegram_secret(request: Request) -> None:
    incoming = (
        request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        or request.headers.get("x-telegram-bot-api-secret-token")
    )
    if incoming != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized webhook")


def extract_telegram_text(payload: dict) -> Optional[str]:
    channel_post = payload.get("channel_post") or {}
    if channel_post.get("text"):
        return channel_post["text"]

    message = payload.get("message") or {}
    if message.get("text"):
        return message["text"]

    return None


# =========================
# API ROUTES
# =========================
@app.get("/")
def healthcheck():
    return {"ok": True, "service": "telegram-signal-image-mvp"}


@app.post("/preview")
async def preview_signal(payload: dict):
    text = payload.get("text", "")
    if not text:
        raise HTTPException(status_code=400, detail="'text' alanı gerekli")

    try:
        parsed = parse_message(text)
        image_path = generate_image(parsed)
        image_url = upload_to_cloudinary(str(image_path))
        print("IMAGE URL:", image_url)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "ok": True,
        "parsed": parsed.model_dump(),
        "image_path": str(image_path),
        "image_url": image_url,
        "filename": image_path.name,
    }


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    verify_telegram_secret(request)
    payload = await request.json()

    text = extract_telegram_text(payload)
    if not text:
        return JSONResponse({"ok": True, "skipped": True, "reason": "text message not found"})

    try:
        parsed = parse_message(text)
        image_path = generate_image(parsed)
        image_url = upload_to_cloudinary(str(image_path))
        print("IMAGE URL:", image_url)

        caption = build_instagram_caption(parsed)
        instagram_result = post_to_instagram(image_url, caption)

    except Exception as exc:
        return JSONResponse(
            {
                "ok": False,
                "error": str(exc),
                "raw_text": text,
            },
            status_code=400,
        )

    return {
        "ok": True,
        "parsed": parsed.model_dump(),
        "image_path": str(image_path),
        "image_url": image_url,
        "instagram_result": instagram_result,
        "filename": image_path.name,
    }


# =========================
# LOCAL RUN
# =========================
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "telegram_instagram_signal_mvp:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
    )