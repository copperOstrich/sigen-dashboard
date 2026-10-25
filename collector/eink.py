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


def render_away(width=1404, height=1872, version="", title=None, rm_owner=False):
    """The screen a tablet shows when it hasn't reached the collector for a while (away from home,
    or on a network that blocks it), instead of old figures that look current."""
    if not AVAILABLE:
        raise RuntimeError("Pillow isn't installed, so the e-ink image isn't available outside the Docker image")
    s = width / 1404 * (0.86 if rm_owner else 1)
    S = lambda v: int(round(v * s))
    img = Image.new("L", (width, height), WHITE)
    d = ImageDraw.Draw(img)
    txt = lambda size: _font("Barlow-Medium.ttf", S(size))
    txtb = lambda size: _font("Barlow-SemiBold.ttf", S(size))
    M = int(round(72 * width / 1404))
    x0, x1 = M, width - M
    y = S(64)
    d.text((x0, y), title or "Home energy", font=txtb(56 if not title or len(title) < 26 else 46), fill=BLACK)
    y += S(100)
    d.line([(x0, y), (x1, y)], fill=BLACK, width=S(4))
    # the message, in the upper middle of the screen (clear of the owner strip at the bottom)
    y = int(height * 0.30)
    d.text((x0, y), "Away from home", font=txtb(84), fill=BLACK)
    y += S(130)
    msg = ("Your energy figures show here again when the tablet is back on your home Wi-Fi "
           "and has been awake for a minute.")
    for line in _wrap(d, msg, txt(46), x1 - x0):
        d.text((x0, y), line, font=txt(46), fill=DARK)
        y += S(64)
    d.line([(x0, height - S(96)), (x1, height - S(96))], fill=LIGHT, width=S(3))
    if version:
        d.text((x1, height - S(76)), f"Home Energy {version}", font=txt(30), fill=MID, anchor="ra")
    out = io.BytesIO()
    img.save(out, "PNG", optimize=True)
    return out.getvalue()


def render(live, history=None, tz=None, width=1404, height=1872, version="", prices=None, aemo=None,
           title=None, owner=None, phone=None, rm_owner=False):
    if not AVAILABLE:
        raise RuntimeError("Pillow isn't installed, so the e-ink image isn't available outside the Docker image")
    s = width / 1404  # scale everything from the reMarkable 2 layout
    # rm_owner: a reMarkable draws its owner name and number itself, centred near the bottom of
    # its sleep screen, on top of a custom image. Keep that strip clear and fit the rest above it
    # (a slightly smaller layout, the same width).
    band = (int(height * 1575 / 1872), int(height * 1752 / 1872)) if rm_owner else None
    if band:
        s *= 0.86
    S = lambda v: int(round(v * s))
    img = Image.new("L", (width, height), WHITE)
    d = ImageDraw.Draw(img)
    num = lambda size: _font("BarlowSemiCondensed-SemiBold.ttf", S(size))
    numb = lambda size: _font("BarlowSemiCondensed-Bold.ttf", S(size))
    txt = lambda size: _font("Barlow-Medium.ttf", S(size))
    txtb = lambda size: _font("Barlow-SemiBold.ttf", S(size))
    M = int(round(72 * width / 1404))  # margin (full width even in the smaller layout)
    x0, x1 = M, width - M
    y = S(64)
    limit = band[0] if band else height - S(96)  # content stays above this

    now = datetime.now(tz)
    updated = (live or {}).get("updated")
    stamp = datetime.fromtimestamp(updated, tz) if updated else now
    # title: "Home energy", or the device's own sleep message (e.g. "Paper tablet is sleeping")
    d.text((x0, y), title or "Home energy", font=txtb(56 if not title or len(title) < 26 else 46), fill=BLACK)
    when = stamp.strftime("%-I:%M %p").lower() + stamp.strftime(", %a %-d %b")
    d.text((x1, y + S(8)), f"{'energy ' if title else ''}as of {when}", font=txt(40), fill=DARK, anchor="ra")
    y += S(100)
    d.line([(x0, y), (x1, y)], fill=BLACK, width=S(4))
    y += S(40)

    p = (live or {}).get("power") or {}
    b = (live or {}).get("battery") or {}
    if (live or {}).get("status") != "ok":
        d.text((x0, y), "No current reading from the inverter", font=txtb(46), fill=BLACK)
        y += S(80)

    # ---- the four flows, 2 x 2
    cw, ch = (x1 - x0 - S(40)) // 2, S(206)
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
        vf = numb(118)
        d.text((cx + S(30), cy + S(70)), val, font=vf, fill=BLACK)
        vw = d.textlength(val, font=vf)
        d.text((cx + S(42) + vw, cy + S(124)), unit, font=num(50), fill=DARK)
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
        head = o["headline"]
        d.text((x0, y), "Battery " + head[0].lower() + head[1:], font=txtb(54), fill=BLACK)
        y += S(74)
        adv = (o.get("advice") or [])
        if len(adv) > 1:
            for line in _wrap(d, adv[1], txt(36), x1 - x0)[:2]:
                d.text((x0, y), line, font=txt(36), fill=DARK)
                y += S(48)
        y += S(16)

    pr = (live or {}).get("price") or {}
    ae = aemo or {}
    has_retail = pr.get("buy") is not None
    has_ws = ae.get("enabled") and (ae.get("current") or {}).get("price_c") is not None
    if has_retail or has_ws:
        d.line([(x0, y), (x1, y)], fill=LIGHT, width=S(3))
        y += S(24)
        d.text((x0, y), "Prices, now and the next 2 hours", font=txt(38), fill=DARK)
        tag = {"spike": "PRICE SPIKE", "high": "Expensive", "neutral": "Average", "low": "Cheap", "veryLow": "Very cheap",
               "extremelyLow": "Very cheap", "negative": "Very cheap"}.get(pr.get("descriptor") or "", "")
        if pr.get("spike") == "spike":
            tag = "PRICE SPIKE"
        if tag:
            tf = txtb(38)
            tw = d.textlength(tag, font=tf)
            filled = tag == "PRICE SPIKE"
            d.rounded_rectangle([x1 - tw - S(44), y - S(6), x1, y + S(52)], radius=S(29), outline=BLACK, width=S(4),
                                fill=BLACK if filled else WHITE)
            d.text((x1 - tw - S(22), y + S(2)), tag, font=tf, fill=WHITE if filled else BLACK)
        y += S(64)
        # left: the prices now
        L = S(330)
        rows = ([("Buying", pr["buy"]), ("Selling", pr.get("sell"))] if has_retail else []) + \
               ([("Wholesale", ae["current"]["price_c"])] if has_ws and not has_retail else [])
        ly = y
        for lab, v in rows:
            d.text((x0, ly), lab, font=txt(34), fill=DARK)
            d.text((x0, ly + S(36)), "–" if v is None else f"{v:.1f}c", font=numb(84), fill=BLACK)
            ly += S(140)
        if has_retail and has_ws:
            d.text((x0, ly), f"Wholesale {ae['current']['price_c']:.1f}c", font=txt(34), fill=DARK)
        # right: step chart of the next two hours
        now = time.time()
        end = now + 2 * 3600
        series = []  # (label, [(t0, t1, value)], style)
        if has_retail:
            fc = [f for f in (prices or {}).get("forecast") or [] if f.get("end", 0) > now and f.get("t", 0) < end]
            for key, style in (("buy", "solid"), ("sell", "dash")):
                segs = [(max(now, f["t"]), min(end, f["end"]), f[key]) for f in fc if f.get(key) is not None]
                if not segs and pr.get(key) is not None:
                    segs = [(now, min(end, pr.get("ends") or end), pr[key])]
                series.append(("Buying" if key == "buy" else "Selling", segs, style))
        if has_ws:
            wf = [f for f in ae.get("forecast") or [] if f.get("end", 0) > now and f.get("t", 0) < end]
            segs = [(max(now, f["t"]), min(end, f["end"]), f["price_c"]) for f in wf if f.get("price_c") is not None]
            first = min((s0 for s0, _, _ in segs), default=end)
            if first > now:  # the current 5-minute price until the forecast starts
                segs.insert(0, (now, first, ae["current"]["price_c"]))
            series.append(("Wholesale (AEMO)", segs, "dot"))
        vals = [v for _, segs, _ in series for _, _, v in segs]
        cx0, cx1 = x0 + L + S(70), x1
        cy0, cy1 = y + S(10), y + S(250)
        if vals:
            lo, hi = min(0.0, min(vals)), max(vals)
            span = hi - lo or 1
            step = next(st for st in (1, 2, 5, 10, 20, 25, 50, 100, 200, 500, 1000, 2000) if span / st <= 4)
            lo, hi = step * (lo // step), step * -(-hi // step)
            if hi == lo:
                hi = lo + step
            Y = lambda v: cy1 - (v - lo) / (hi - lo) * (cy1 - cy0)
            X = lambda t: cx0 + (t - now) / (end - now) * (cx1 - cx0)
            v = lo
            while v <= hi + 1e-9:
                d.line([(cx0, Y(v)), (cx1, Y(v))], fill=LIGHT if v else MID, width=S(2))
                d.text((cx0 - S(12), Y(v)), f"{v:g}c", font=txt(28), fill=DARK, anchor="rm")
                v += step
            ticks = [now] + [h for h in range(int(now // 1800 + 1) * 1800, int(end) + 1, 1800) if h - now > 900]
            for k, tt in enumerate(ticks):
                lt = datetime.fromtimestamp(tt, tz)
                d.line([(X(tt), cy1), (X(tt), cy1 + S(10))], fill=BLACK, width=S(3))
                lab = "now" if k == 0 else lt.strftime("%-I:%M")
                d.text((X(tt), cy1 + S(16)), lab, font=txt(28), fill=DARK,
                       anchor="la" if k == 0 else "ra" if end - tt < 600 else "ma")

            def seg_line(p0, p1, style, width_):
                if style == "solid":
                    d.line([p0, p1], fill=BLACK, width=width_)
                    return
                on, off = (S(18), S(12)) if style == "dash" else (S(5), S(9))
                (xa, ya), (xb, yb) = p0, p1
                length = max(1.0, ((xb - xa) ** 2 + (yb - ya) ** 2) ** 0.5)
                pos = 0.0
                while pos < length:
                    e = min(length, pos + on)
                    d.line([(xa + (xb - xa) * pos / length, ya + (yb - ya) * pos / length),
                            (xa + (xb - xa) * e / length, ya + (yb - ya) * e / length)],
                           fill=BLACK if style == "dash" else DARK, width=width_)
                    pos = e + off

            for label, segs, style in series:
                segs = sorted(segs)
                prev = None
                for s0, s1, v in segs:
                    if s1 <= s0:
                        continue
                    wid = S(7) if style == "solid" else S(5)
                    if prev is not None:  # the step between intervals
                        seg_line((X(s0), Y(prev)), (X(s0), Y(v)), style, wid)
                    seg_line((X(s0), Y(v)), (X(s1), Y(v)), style, wid)
                    if style != "dot" and X(s1) - X(s0) > S(90):
                        d.text(((X(s0) + X(s1)) / 2, Y(v) + (-S(8) if style == "solid" else S(10))), f"{v:.0f}",
                               font=txtb(30) if style == "solid" else txt(30), fill=BLACK,
                               anchor="md" if style == "solid" else "ma")
                    prev = v
            # legend
            lx, lyy = cx0, cy1 + S(58)
            for label, _, style in series:
                seg_line((lx, lyy + S(16)), (lx + S(54), lyy + S(16)), style, S(6) if style == "solid" else S(5))
                d.text((lx + S(66), lyy), label, font=txt(30), fill=DARK)
                lx += S(80) + d.textlength(label, font=txt(30)) + S(30)
        y = max(ly + (S(50) if has_retail and has_ws else 0), cy1 + S(100))

    # ---- today
    t = (live or {}).get("today") or {}
    d.line([(x0, y), (x1, y)], fill=LIGHT, width=S(3))
    y += S(26)
    d.text((x0, y), "Today so far", font=txt(38), fill=DARK)
    y += S(48)
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

    # ---- solar forecast (one line each for today and tomorrow, as space allows)
    w = (live or {}).get("weather") or {}
    if w.get("enabled") and w.get("today"):
        cols = []
        for k, lab in (("today", "Today"), ("tomorrow", "Tomorrow")):
            dd = w.get(k)
            if dd:
                kwh = f"{dd['pv_kwh']:.0f} kWh" if dd.get("pv_kwh") is not None else "–"
                cols.append((lab, kwh, f"{dd['desc'].lower()}, {round(dd['tmin'])}–{round(dd['tmax'])}°"))
        if cols and y + S(126) <= limit:
            d.line([(x0, y), (x1, y)], fill=LIGHT, width=S(3))
            y += S(22)
            half = (x1 - x0) // 2
            for i, (lab, kwh, desc) in enumerate(cols):
                cx = x0 + i * half
                d.text((cx, y), f"{lab}'s solar forecast", font=txt(32), fill=DARK)
                d.text((cx, y + S(38)), kwh, font=numb(56), fill=BLACK)
                kw_w = d.textlength(kwh, font=numb(56))
                d.text((cx + kw_w + S(16), y + S(58)), desc, font=txt(32), fill=DARK)
            y += S(104)
        y += S(10)

    # ---- solar and battery over the last 24 hours
    h = history or {}
    if h.get("t") and len(h["t"]) > 10 and limit - y >= S(300):
        d.line([(x0, y), (x1, y)], fill=LIGHT, width=S(3))
        y += S(26)
        d.text((x0, y), "Last 24 hours: solar (filled) and battery charge (line)", font=txt(34), fill=DARK)
        y += S(52)
        gh = min(S(320), limit - y - S(70))
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
    if owner or phone:
        # the owner details the tablet normally shows on its sleep screen
        d.text((x0, height - S(80)), owner or "", font=txtb(38), fill=BLACK)
        d.text((x1, height - S(80)), phone or "", font=txtb(38), fill=BLACK, anchor="ra")
    else:
        d.text((x0, height - S(76)), "Updates while the tablet is awake; shown as of the time above.",
               font=txt(30), fill=MID)
        if version:
            d.text((x1, height - S(76)), f"Home Energy {version}", font=txt(30), fill=MID, anchor="ra")

    out = io.BytesIO()
    img.save(out, "PNG", optimize=True)
    return out.getvalue()
