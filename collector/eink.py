"""E-ink snapshot of the dashboard as a greyscale PNG (for a reMarkable sleep screen and the like).

Drawn with Pillow, which the Docker image includes. Everything else in the collector is standard
library only; without Pillow this module reports itself unavailable and the rest carries on.

Designed for e-ink: black, white and a couple of greys, big numbers, thick lines, no colour.
The default size is the reMarkable 2 screen (1404 x 1872, portrait).
"""
import io
import time
from datetime import datetime
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
    AVAILABLE = True
except ImportError:  # running outside Docker without Pillow
    AVAILABLE = False

FONTS = Path(__file__).resolve().parent / "fonts"
BLACK, DARK, MID, LIGHT, WHITE = 0, 70, 130, 215, 255


def _font(name, size):
    return ImageFont.truetype(str(FONTS / name), size)


def _kw(w):
    if w is None:
        return "–", ""
    a = abs(w)
    return (f"{round(a)}", "W") if a < 1000 else (f"{a / 1000:.1f}", "kW")


def _wrap(draw, text, font, width):
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if draw.textlength(t, font=font) <= width:
            cur = t
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def render(live, history=None, tz=None, width=1404, height=1872, version=""):
    if not AVAILABLE:
        raise RuntimeError("Pillow isn't installed, so the e-ink image isn't available outside the Docker image")
    s = width / 1404  # scale everything from the reMarkable 2 layout
    S = lambda v: int(round(v * s))
    img = Image.new("L", (width, height), WHITE)
    d = ImageDraw.Draw(img)
    num = lambda size: _font("BarlowSemiCondensed-SemiBold.ttf", S(size))
    numb = lambda size: _font("BarlowSemiCondensed-Bold.ttf", S(size))
    txt = lambda size: _font("Barlow-Medium.ttf", S(size))
    txtb = lambda size: _font("Barlow-SemiBold.ttf", S(size))
    M = S(72)  # margin
    x0, x1 = M, width - M
    y = S(64)

    now = datetime.now(tz)
    updated = (live or {}).get("updated")
    stamp = datetime.fromtimestamp(updated, tz) if updated else now
    d.text((x0, y), "Home energy", font=txtb(56), fill=BLACK)
    when = stamp.strftime("%-I:%M %p").lower() + stamp.strftime(", %a %-d %b")
    d.text((x1, y + S(8)), f"as of {when}", font=txt(40), fill=DARK, anchor="ra")
    y += S(100)
    d.line([(x0, y), (x1, y)], fill=BLACK, width=S(4))
    y += S(40)

    p = (live or {}).get("power") or {}
    b = (live or {}).get("battery") or {}
    if (live or {}).get("status") != "ok":
        d.text((x0, y), "No current reading from the inverter", font=txtb(46), fill=BLACK)
        y += S(80)

    # ---- the four flows, 2 x 2
    cw, ch = (x1 - x0 - S(40)) // 2, S(222)
    batt = b.get("soc")
    bw = p.get("battery") or 0
    gw = p.get("grid") or 0
    tiles = [
        ("Solar", _kw(p.get("pv")), None),
        ("Home", _kw(p.get("load")), None),
        ("Battery", (f"{round(batt)}" if batt is not None else "–", "%"),
         "charging " + " ".join(_kw(bw)) if bw > 40 else "supplying " + " ".join(_kw(bw)) if bw < -40 else "idle"),
        ("Grid", _kw(gw), "buying" if gw > 40 else "selling" if gw < -40 else "balanced"),
    ]
    for i, (label, (val, unit), sub) in enumerate(tiles):
        cx, cy = x0 + (i % 2) * (cw + S(40)), y + (i // 2) * (ch + S(30))
        d.rounded_rectangle([cx, cy, cx + cw, cy + ch], radius=S(22), outline=BLACK, width=S(4))
        d.text((cx + S(32), cy + S(26)), label, font=txtb(44), fill=DARK)
        vf = numb(124)
        d.text((cx + S(30), cy + S(76)), val, font=vf, fill=BLACK)
        vw = d.textlength(val, font=vf)
        d.text((cx + S(42) + vw, cy + S(134)), unit, font=num(52), fill=DARK)
        if sub:
            d.text((cx + cw - S(30), cy + S(32)), sub, font=txt(36), fill=DARK, anchor="ra")
    y += 2 * ch + S(30) + S(36)

    # battery bar
    if batt is not None:
        d.rounded_rectangle([x0, y, x1, y + S(44)], radius=S(12), outline=BLACK, width=S(4))
        fillw = int((x1 - x0 - S(12)) * max(0, min(100, batt)) / 100)
        if fillw > 0:
            d.rounded_rectangle([x0 + S(6), y + S(6), x0 + S(6) + fillw, y + S(38)], radius=S(8), fill=MID)
        y += S(76)

    # ---- outlook and price
    o = (live or {}).get("outlook") or {}
    if o.get("enabled") and o.get("headline"):
        d.text((x0, y), "Battery outlook", font=txt(38), fill=DARK)
        y += S(50)
        d.text((x0, y), o["headline"], font=txtb(54), fill=BLACK)
        y += S(74)
        adv = (o.get("advice") or [])
        if len(adv) > 1:
            for line in _wrap(d, adv[1], txt(36), x1 - x0)[:3]:
                d.text((x0, y), line, font=txt(36), fill=DARK)
                y += S(48)
        y += S(16)

    pr = (live or {}).get("price") or {}
    if pr.get("buy") is not None:
        d.line([(x0, y), (x1, y)], fill=LIGHT, width=S(3))
        y += S(26)
        d.text((x0, y), "Buying", font=txt(38), fill=DARK)
        d.text((x0 + S(300), y), "Selling", font=txt(38), fill=DARK)
        y += S(46)
        d.text((x0, y), f"{pr['buy']:.1f}c", font=numb(96), fill=BLACK)
        d.text((x0 + S(300), y), f"{pr['sell']:.1f}c" if pr.get("sell") is not None else "–", font=numb(96), fill=BLACK)
        tag = {"spike": "PRICE SPIKE", "high": "Expensive", "neutral": "Average", "low": "Cheap", "veryLow": "Very cheap",
               "extremelyLow": "Very cheap", "negative": "Very cheap"}.get(pr.get("descriptor") or "", "")
        if pr.get("spike") == "spike":
            tag = "PRICE SPIKE"
        if tag:
            tf = txtb(44)
            tw = d.textlength(tag, font=tf)
            bx = x1 - tw - S(48)
            filled = tag == "PRICE SPIKE"
            d.rounded_rectangle([bx, y + S(24), x1, y + S(96)], radius=S(36), outline=BLACK, width=S(4),
                                fill=BLACK if filled else WHITE)
            d.text((bx + S(24), y + S(36)), tag, font=tf, fill=WHITE if filled else BLACK)
        y += S(120)

    # ---- today
    t = (live or {}).get("today") or {}
    d.line([(x0, y), (x1, y)], fill=LIGHT, width=S(3))
    y += S(26)
    d.text((x0, y), "Today so far", font=txt(38), fill=DARK)
    y += S(52)
    cells = [("Solar", t.get("pv"), "kWh"), ("Home", t.get("load"), "kWh"), ("Bought", t.get("import"), "kWh"),
             ("Sold", t.get("export"), "kWh")]
    cost = t.get("cost") or {}
    cw4 = (x1 - x0) // 4
    for i, (lab, v, u) in enumerate(cells):
        cx = x0 + i * cw4
        d.text((cx, y), lab, font=txt(34), fill=DARK)
        d.text((cx, y + S(40)), "–" if v is None else f"{v:.1f}", font=numb(66), fill=BLACK)
    y += S(124)
    extra = []
    if t.get("self_sufficiency") is not None:
        extra.append(f"{round(t['self_sufficiency'])}% self-sufficient")
    if cost.get("net") is not None:
        extra.append(f"net cost ${cost['net']:.2f}" if cost["net"] >= 0 else f"${-cost['net']:.2f} credit")
    if t.get("ev"):
        extra.append(f"car {t['ev']:.1f} kWh")
    if extra:
        d.text((x0, y), ",  ".join(extra), font=txt(38), fill=DARK)
        y += S(64)

    # ---- solar forecast
    w = (live or {}).get("weather") or {}
    if w.get("enabled") and w.get("today"):
        parts = []
        for k, lab in (("today", "Today"), ("tomorrow", "Tomorrow")):
            dd = w.get(k)
            if dd:
                kwh = f"{dd['pv_kwh']:.0f} kWh solar, " if dd.get("pv_kwh") is not None else ""
                parts.append(f"{lab}: {kwh}{dd['desc'].lower()}, {round(dd['tmin'])}–{round(dd['tmax'])}°")
        for part in parts:
            d.text((x0, y), part, font=txt(40), fill=BLACK)
            y += S(56)

        y += S(10)

    # ---- solar and battery over the last 24 hours
    h = history or {}
    if h.get("t") and len(h["t"]) > 10 and height - S(96) - y >= S(300):
        d.line([(x0, y), (x1, y)], fill=LIGHT, width=S(3))
        y += S(26)
        d.text((x0, y), "Last 24 hours: solar (filled) and battery charge (line)", font=txt(34), fill=DARK)
        y += S(52)
        gh = min(S(320), height - S(96) - y - S(70))
        tt, pv, soc = h["t"], h.get("pv") or [], h.get("soc") or []
        t0, t1 = tt[0], tt[-1]
        X = lambda v: x0 + (v - t0) / max(1, t1 - t0) * (x1 - x0)
        pmax = max([v or 0 for v in pv] + [1000])
        base = y + gh
        poly = [(X(tt[0]), base)] + [(X(a), base - (max(0, v or 0) / pmax) * gh) for a, v in zip(tt, pv)] + [(X(tt[-1]), base)]
        d.polygon(poly, fill=LIGHT)
        pts = [(X(a), base - (v / 100) * gh) for a, v in zip(tt, soc) if v is not None]
        if len(pts) > 1:
            d.line(pts, fill=BLACK, width=S(5), joint="curve")
        d.line([(x0, base), (x1, base)], fill=BLACK, width=S(3))
        for hr in range(int(t0 // 3600) + 1, int(t1 // 3600) + 1):
            lt = datetime.fromtimestamp(hr * 3600, tz)
            if lt.hour % 6 == 0:
                xx = X(hr * 3600)
                d.line([(xx, base), (xx, base + S(12))], fill=BLACK, width=S(3))
                lab = lt.strftime("%a") if lt.hour == 0 else lt.strftime("%-I %p").lower()
                d.text((xx, base + S(18)), lab, font=txt(30), fill=DARK, anchor="ma")
        y = base + S(70)

    # footer
    d.line([(x0, height - S(96)), (x1, height - S(96))], fill=LIGHT, width=S(3))
    d.text((x0, height - S(76)), "Updates while the tablet is awake; shown as of the time above.",
           font=txt(30), fill=MID)
    if version:
        d.text((x1, height - S(76)), f"Home Energy {version}", font=txt(30), fill=MID, anchor="ra")

    out = io.BytesIO()
    img.save(out, "PNG", optimize=True)
    return out.getvalue()
