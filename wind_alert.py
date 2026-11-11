#!/usr/bin/env python3
"""Westeinderplassen wind alert -> ntfy push.

Rules
- Only daylight hours (sunrise..sunset) count as sailable.
- Alert goes out LEAD hours before a windy daylight window starts.
  If that moment falls at night, the alert is held until sunrise.
- Nothing is ever sent at night.
- A second "wind is about to die" alert goes out ~1 h before the wind drops.
Everything you may want to tweak is in the SETTINGS / TABLES sections.
"""
import os, sys, json, math, time, random, urllib.request, urllib.parse
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Amsterdam")

# ----------------------------- SETTINGS ------------------------------------
LAT = float(os.environ.get("LAT") or 52.245)      # Westeinderplassen (forecast grid is ~2 km,
LON = float(os.environ.get("LON") or 4.745)       # so any point on the lake gives the same result)
THRESHOLD = float(os.environ.get("THRESHOLD_KNOTS") or 14)   # steady wind, knots
TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
PORTRAIT_URL = os.environ.get("PORTRAIT_URL", "").strip()   # optional, see README
TEST_MODE = os.environ.get("TEST_MODE") == "1"
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
LEAD_S = 3 * 3600          # warn this long before the windy window starts
DIE_WARN_S = 3600          # "wind dying" warning this long before it drops
LAUNCH = "SE"              # your launch corner (used in the text only)
NAME = os.environ.get("GUIDE_NAME") or "Kai"          # your island guide's name

# ----------------------------- LAKE TABLES ---------------------------------
# Rough guesses, tune them after a few sessions!
# FETCH_KM[d] = km of open water the wind crosses before reaching your SE launch
# corner when it blows FROM direction d. (Islands in the north cut fetch down.)
FETCH_KM = {"N": 2.5, "NE": 1.2, "E": 0.4, "SE": 0.2, "S": 0.5, "SW": 1.8, "W": 3.0, "NW": 3.5}
# What the wind does at the SE launch corner, by direction it blows FROM.
SHORE = {"N": "onshore", "NE": "cross-shore", "E": "offshore", "SE": "offshore",
         "S": "offshore", "SW": "cross-shore", "W": "onshore", "NW": "onshore"}

SECTORS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
OPP = {s: SECTORS[(i + 4) % 8] for i, s in enumerate(SECTORS)}
C16 = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def sector(deg):
    return SECTORS[int(((deg % 360) + 22.5) // 45) % 8]


def c16(deg):
    return C16[int(((deg % 360) + 11.25) // 22.5) % 16]


# ----------------------------- TIME HELPERS --------------------------------
def hhmm(t):
    return datetime.fromtimestamp(t, TZ).strftime("%H:%M")


def dayhhmm(t):
    return datetime.fromtimestamp(t, TZ).strftime("%a %H:%M")


class Sun:
    def __init__(self, rises, sets):
        self.pairs = sorted(zip(rises, sets))
        self.rises = sorted(rises)

    def is_day(self, t):
        return any(r <= t <= s for r, s in self.pairs)

    def rise_for(self, t):
        c = [r for r in self.rises if r <= t]
        return max(c) if c else None


# ----------------------------- WAVES ---------------------------------------
def hs(knots, fetch_km):
    """Fetch-limited significant wave height (m), SMB-style, capped for a shallow lake."""
    u = knots * 0.5144
    h = 0.0016 * u * math.sqrt(max(fetch_km, 0.05) * 1000 / 9.81)
    return min(h, 0.8)


def chop(h):
    if h < 0.08: return "mirror-flat"
    if h < 0.20: return "light ripples"
    if h < 0.35: return "fun chop"
    if h < 0.50: return "steep, choppy"
    return "washing-machine chop"


def cm(h):
    return max(0, int(round(h * 100 / 5.0) * 5))


# ----------------------------- RATINGS -------------------------------------
def stars(n):
    return "★" * n + "☆" * (5 - n)


def _penalize(s, k, w, g, shore):
    spread = g - w
    if spread >= 10: s -= 2
    elif spread >= 7: s -= 1
    if shore == "offshore" and w >= 8: s -= 1
    s = max(0, s)
    if k == "sweet" and s <= 3: k = "spicy"
    if s == 0 and k in ("strong", "spicy"): k = "storm"
    return s, k


def rate_dinghy(w, g, shore):
    if w < 5: s, k = 0, "calm"
    elif w < 8: s, k = 2, "light"
    elif w < 10: s, k = 3, "light"
    elif w <= 16: s, k = 5, "sweet"
    elif w <= 20: s, k = 4, "strong"
    elif w <= 25: s, k = 2, "strong"
    else: s, k = 0, "storm"
    return _penalize(s, k, w, g, shore)


def rate_wing(w, g, shore):
    if w < 8: s, k = 0, "calm"
    elif w < 12: s, k = (1 if w < 10 else 2), "marginal"
    elif w < 14: s, k = 3, "marginal"
    elif w <= 22: s, k = 5, "sweet"
    elif w <= 27: s, k = 4, "strong"
    elif w <= 32: s, k = 2, "strong"
    else: s, k = 0, "storm"
    return _penalize(s, k, w, g, shore)


DINGHY_LABELS = {
    "calm": ["Dinghy-wise it's dead calm, buddy. Perfect for a snack on the dock!",
             "The sails would just hang there, dude. Nap-on-the-pier kind of day!"],
    "light": ["Light and lazy, super relaxed laps today. Bring snacks, dude!",
              "A cruisy little breeze, no rush at all. Great for a chill sail!"],
    "sweet": ["Ohhh, this is the sweet spot! Pure dinghy joy, let's gooo!",
              "Honestly perfect sailing air. Yeah! I'd rig up right now!",
              "Everything lines up for a super sail. You've got this!"],
    "spicy": ["Good wind, but it has mood swings! Keep a firm hand on the sheet.",
              "Fun but puffy! Stay sharp and watch for surprise gusts."],
    "strong": ["It's a big-muscle day! Hike hard and keep it flat, champ!",
               "Whoa, spicy power! Reef it or ride it, but stay safe!"],
    "storm": ["Nope nope nope, way too wild for the dinghy. Dock-snack day!",
              "It's too much for me today, buddy. Let's save it for a kinder day!"],
}
WING_LABELS = {
    "calm": ["No juice for the foil today, buddy. Sunbathing mode!",
             "Zero lift. Your foil is a very fancy paperweight right now."],
    "marginal": ["Foil-lift lottery! Big wing and good pumping needed.",
                 "Barely enough power. Light riders with big wings might fly!"],
    "sweet": ["Fly, baby, fly! This is proper foil weather!",
              "Silky smooth flying air. Yeah! Go get that glide!",
              "Hover-board heaven! I'm so excited for you!"],
    "spicy": ["Good power, bumpy air. Fly it, but stay sharp!",
              "Gusty combo! Pick a smaller wing and keep it chill."],
    "strong": ["Big power! Small wing, big grin!",
               "Wow, that's overpowered weather. Experts, you know who you are!"],
    "storm": ["Boss-level gusts. Respect them and stay on the shore today!",
              "Survival mode: not today, champ!"],
}

MEDALS = {0: "no medal yet", 1: "🥉 Bronze", 2: "🥉 Bronze", 3: "🥈 Silver", 4: "🥇 Gold", 5: "💎 Platinum"}


def medal(n):
    return "%s (%d/5)" % (MEDALS[n], n)


GREET_GO = [
    "Heeey, it's {n}! Ooh, sorry to bug you, but I've got breezy news from the lake!",
    "Hi hi, {n} here, bright and breezy! Oh! I've got wind news for you, buddy!",
    "Yo, it's {n}, your island guide! Take a deep, chill breath... okay, ready? Big news!",
    "Good day, friend! {n} from Westeinder Resort here, and wow, do I have a forecast for you!",
]
CLOSERS = [
    "Okay, that's everything! Go grab your gear, buddy. You've got this! 🌴",
    "That's the forecast! Stay chill, stay safe, and have the best session! 🌴",
    "Alright, off you go! Don't forget the sunscreen and a big smile. See you on the water! 🌴",
]
TIPS = [
    "Tell someone on shore when you head out and when you'll be back. Even legends do it!",
    "Quick gear check: leash, lines, lifejacket or impact vest. Future-you says thanks!",
    "Warm up your shoulders and back for a minute before you rig. Easy and effective!",
    "Drink some water before you go out. Hydration is a free power-up!",
    "Rig on the shore, check your lines twice, and keep the fun going all day!",
]

FACTS = [
    "A knot is one nautical mile per hour. The name comes from old ships, where sailors let a knotted rope run out behind the boat and counted the knots!",
    "Wind direction is named after where the wind comes FROM. A westerly blows from the west towards the east.",
    "Buys Ballot's law, from Dutch meteorologist Christophorus Buys Ballot: in the northern hemisphere, stand with your back to the wind and low pressure is on your left. He founded the KNMI in 1854!",
    "Double the wind speed and the push on your sail or wing becomes about four times stronger. That's why 20 knots feels SO different from 10!",
    "Wind over open water is smoother than over land because there is less friction, so big water usually means steadier wind.",
    "The faster you go, the more the wind you feel swings forward and strengthens. That's apparent wind, and it lets fast boats like iceboats sail several times faster than the wind itself!",
    "Alexander Graham Bell's hydrofoil boat HD-4 hit about 61.5 knots in 1919. Foil dreams are over a century old!",
    "The sailing speed record is 65.45 knots, set by the Vestas Sailrocket 2 in 2012. Zoom!",
    "Wing foiling is only a handful of years old. The sport really took off around 2018-2019, when the first wings made for foiling reached the shops.",
    "Your foil works like an airplane wing underwater. Water is roughly 800 times denser than air, which is why a tiny foil can lift a whole person!",
    "America's Cup boats have been flying on foils since 2013. Racing above the water, how cool is that?",
    "Planing dinghies can zoom past their 'hull speed' by skimming over the water instead of pushing through it.",
    "A sailboat can't sail straight into the wind. It zig-zags in a move called tacking, at roughly 45 degrees to the wind.",
    "Starboard comes from 'steerboard': old boats were steered with a paddle on the right-hand side. Port was the side facing the dock.",
    "A fluffy cumulus cloud can weigh about as much as 100 elephants. Yep, the sky is heavy!",
    "Lightning heats the air around it to roughly 30,000 degrees Celsius, around five times hotter than the surface of the Sun!",
    "Count the seconds between a lightning flash and the thunder, then divide by three: that's roughly how many kilometres away the strike is.",
    "Sunsets turn red because the Sun's light travels through more air, which scatters the blue away and leaves the red.",
    "A rainbow needs the Sun behind you and rain in front of you, and it's actually a full circle. From the ground you only see part of it!",
    "Cold water shock is real: the first minute in cold water makes you gasp. Remember 1-10-1: one minute to calm your breathing, ten minutes of useful movement, one hour before hypothermia sets in.",
    "Sea and lake breezes happen when land heats faster than water: cooler air flows in from the water. That's why summer afternoons can be so gloriously breezy!",
    "Wind chill is why 'feels like' temperatures are lower than the thermometer: moving air pulls the heat off your skin faster.",
    "Albatrosses can glide for hours without flapping by using the wind, a trick called dynamic soaring. Basically sailing in the sky!",
    "The Westeinderplassen cover about 10 km2, making them the largest freshwater lake area in the Randstad, and they have over 50 islands!",
    "Aalsmeer's flower auction is one of the biggest buildings in the world by floor area, and it's right next to your lake!",
    "The Haarlemmermeer, the huge polder next to the Westeinderplassen, used to be a giant lake. It was pumped dry between about 1849 and 1852!",
    "Dutch windmills pumped water out of polders for centuries before steam took over. The Netherlands has always had a good relationship with wind!",
]


def wing_tip(w):
    if w < 10: return "7 m+ wing, big foil, pump like crazy"
    if w < 14: return "5.5-7 m wing"
    if w < 18: return "4.5-5.5 m wing"
    if w < 24: return "3.5-4.5 m wing"
    return "3.5 m or smaller, experts only"


def dinghy_tip(w):
    if w < 8: return "full rig, roll-tack for speed"
    if w <= 16: return "full rig, trim for the puffs"
    if w <= 20: return "depower by hiking hard and pulling the vang on"
    return "reef or stay ashore"


# ----------------------------- FORECAST ------------------------------------
def fetch():
    q = {
        "latitude": LAT, "longitude": LON,
        "hourly": "wind_speed_10m,wind_gusts_10m,wind_direction_10m,temperature_2m,"
                  "apparent_temperature,precipitation,weather_code,uv_index",
        "daily": "sunrise,sunset",
        "wind_speed_unit": "kn", "timeformat": "unixtime",
        "timezone": "Europe/Amsterdam", "forecast_days": 3,
    }
    url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(q)
    err = None
    for i in range(3):
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                return json.load(r)
        except Exception as e:  # noqa
            err = e
            time.sleep(3 * (i + 1))
    raise err


def prepare(data):
    h = data["hourly"]
    n = len(h["time"])
    W = h["wind_speed_10m"]
    G = [g if g is not None else (W[i] or 0) for i, g in enumerate(h["wind_gusts_10m"])]
    D, last = [], 270.0
    for d in h["wind_direction_10m"]:
        last = d if d is not None else last
        D.append(last)
    P = {
        "T": h["time"], "W": W, "G": G, "D": D,
        "TEMP": h["temperature_2m"], "APP": h["apparent_temperature"],
        "PREC": h["precipitation"], "WC": h["weather_code"], "UV": h["uv_index"], "n": n,
    }
    sun = Sun(data["daily"]["sunrise"], data["daily"]["sunset"])
    return P, sun


def find_windows(P, sun, thr):
    out, cur = [], None
    for i, t in enumerate(P["T"]):
        w = P["W"][i]
        ok = w is not None and w > thr and sun.is_day(t + 1800)
        if ok:
            cur = [i, i] if cur is None else [cur[0], i]
        elif cur:
            out.append(tuple(cur)); cur = None
    if cur:
        out.append(tuple(cur))
    return out


def alert_time(start, sun):
    st = start - LEAD_S
    if sun.is_day(st):
        return st
    sr = sun.rise_for(start + 1800)    # night: hold until sunrise of the window's day
    return sr if sr is not None else st


def end_reason(P, sun, b, thr):
    j = b + 1
    if j >= P["n"]: return "forecast"
    if not sun.is_day(P["T"][j] + 1800): return "sunset"
    return "wind" if (P["W"][j] is not None and P["W"][j] <= thr) else "forecast"


# ----------------------------- MESSAGES ------------------------------------
# Pools (labels, facts, tips, greetings) live in the RATINGS section above.
def _vals(arr, rng):
    return [arr[i] for i in rng if arr[i] is not None]


def _when(start, now):
    lead = start - now
    if lead > 5400: return "starts %s (in ~%d h)" % (dayhhmm(start), round(lead / 3600))
    if lead > 0: return "starts %s (in ~%d min)" % (dayhhmm(start), round(lead / 60))
    return "is already ON (since %s)" % hhmm(start)


def go_title(P, a, b, now):
    rng = range(a, b + 1)
    w_avg = sum(P["W"][i] for i in rng) / len(rng)
    return "🌴 %s's Resort Report: %d kn from %s" % (NAME, round(w_avg), c16(P["D"][a]))


def go_message(P, a, b, now, reason):
    T, W, G, D = P["T"], P["W"], P["G"], P["D"]
    rng = list(range(a, b + 1))
    start, end = T[a], T[b] + 3600
    ip = max(rng, key=lambda i: W[i])
    w_avg = sum(W[i] for i in rng) / len(rng)
    g_peak = max(G[i] for i in rng)
    sec = sector(D[ip])
    shore = SHORE[sec]
    spread = g_peak - w_avg
    ends = {"wind": "wind fades after", "sunset": "until sunset", "forecast": "until at least"}[reason]
    gust_txt = "smooth" if spread < 7 else ("puffy" if spread < 10 else "very puffy")
    d0, d1 = c16(D[a]), c16(D[b])
    dir_txt = d0 if d0 == d1 else "%s to %s" % (d0, d1)

    w = W[ip]
    f_launch = FETCH_KM[sec]
    f_lee = max(FETCH_KM[sec], FETCH_KM[OPP[sec]])
    h_l, h_lee = hs(w, f_launch), hs(w, f_lee)

    rows = []
    for i in rng:
        sh = SHORE[sector(D[i])]
        rows.append((i, rate_dinghy(W[i], G[i], sh), rate_wing(W[i], G[i], sh)))

    def summary(col, labels):
        scores = [r[col][0] for r in rows]
        mean = sum(scores) / len(scores)
        head = int(round(mean))
        near = min(rows, key=lambda r: (abs(r[col][0] - mean), -r[col][0]))
        best = max(scores)
        bests = [r[0] for r in rows if r[col][0] == best]
        return head, random.choice(labels[near[col][1]]), bests[len(bests) // 2]

    sd, ld, bd = summary(1, DINGHY_LABELS)
    sw, lw, bw = summary(2, WING_LABELS)

    L = [random.choice(GREET_GO).format(n=NAME),
         "Your play window %s!" % _when(start, now),
         "",
         "📅 PLAY WINDOW",
         ("%s-%s (%d h, %s)" % (dayhhmm(start), hhmm(end), len(rng), ends) if reason != "forecast"
          else "%s-%s+ (%d h+)" % (dayhhmm(start), hhmm(end), len(rng))),
         "",
         "📡 WIND-O-METER",
         "Steady %d kn (peak %d) | gusts up to %d kn" % (round(w_avg), round(W[ip]), round(g_peak)),
         "Gust spread +%d kn: %s | from %s" % (round(spread), gust_txt, dir_txt),
         "%s at your %s launch" % (shore.upper(), LAUNCH),
         "",
         "🌊 WAVE-O-METER (rough estimate)"]
    if shore == "offshore":
        L.append("%s launch corner: ~%d cm, %s. Chop builds fast as you head out!" % (LAUNCH, cm(h_l), chop(h_l)))
    else:
        L.append("%s launch corner: ~%d cm, %s" % (LAUNCH, cm(h_l), chop(h_l)))
    L += ["Leeward side (%s end): ~%d cm, %s (%.1f km fetch)" % (OPP[sec], cm(h_lee), chop(h_lee), f_lee),
          "Windward side (%s end): calm near shore" % sec,
          "",
          "🏅 TODAY'S EVENTS",
          "⛵ Dinghy Sailing: %s %s" % (medal(sd), stars(sd)),
          '%s says: "%s"' % (NAME, ld),
          "Best hour %s | %s" % (hhmm(T[bd]), dinghy_tip(W[bd])),
          "🏄 Wing Foiling: %s %s" % (medal(sw), stars(sw)),
          '%s says: "%s"' % (NAME, lw),
          "Best hour %s | ~75 kg rider: %s" % (hhmm(T[bw]), wing_tip(W[bw])),
          ""]

    ext = list(range(max(0, a - 1), min(P["n"] - 1, b + 1) + 1))
    thunder = [T[i] for i in ext if P["WC"][i] is not None and P["WC"][i] >= 95]
    rain = max(_vals(P["PREC"], ext) or [0])
    temps = _vals(P["TEMP"], rng)
    apps = _vals(P["APP"], rng)
    uv = max(_vals(P["UV"], rng) or [0])
    L.append("🌤 SKY REPORT")
    if thunder:
        L.append("⚡⚡ THUNDERSTORM RISK around %s! Please stay OFF the water! ⚡⚡" % hhmm(thunder[0]))
    elif rain >= 2.0:
        L.append("🌧 Heavy showers possible (up to %.1f mm/h). Pack a shell!" % rain)
    elif rain >= 0.3:
        L.append("🌦 Light showers possible (up to %.1f mm/h)." % rain)
    else:
        L.append("No rain in sight!")
    if temps:
        t_lo, t_hi = min(temps), max(temps)
        feel = min(apps) if apps else t_lo
        tip = ("drysuit or thick wetsuit weather" if t_hi <= 9 else
               "full wetsuit weather" if t_hi <= 15 else
               "shorty-wetsuit weather" if t_hi <= 21 else "board-shorts weather")
        rng_txt = "%d" % round(t_lo) if round(t_lo) == round(t_hi) else "%d-%d" % (round(t_lo), round(t_hi))
        L.append("🌡 %s °C (feels like %d °C): %s" % (rng_txt, round(feel), tip))
    if uv >= 3:
        L.append("☀️ UV %d: don't forget your sunscreen!" % round(uv))
    if shore == "offshore" and w >= 8:
        L.append("⚠️ OFFSHORE at your launch: the wind pushes you away from shore. Buddy up, stay upwind, have a rescue plan!")
    L += ["", "🧰 SAFETY TIP", random.choice(TIPS), "", "💡 FUN FACT", random.choice(FACTS), "", random.choice(CLOSERS)]
    return "\n".join(L), bool(thunder)


def die_message(P, a, b, now):
    T, W = P["T"], P["W"]
    end = T[b] + 3600
    nxt = W[b + 1] if b + 1 < P["n"] and W[b + 1] is not None else None
    L = ["%s here! Oh no, the breeze is fading around %s%s." % (
            NAME, hhmm(end), " (forecast %d kn)" % round(nxt) if nxt is not None else ""),
         "Steady wind drops below %g kn!" % THRESHOLD,
         "",
         "🏁 LAST-LAP ALERT",
         "Start heading back now so you don't get stranded mid-lake with a limp sail or a sleepy wing!",
         "",
         "💡 FUN FACT",
         random.choice(FACTS),
         "",
         "Great session, champ! Stroopwafel time soon! 🌴"]
    return "\n".join(L)


# ----------------------------- SENDING -------------------------------------
def push(title, message, priority=4, tags=None):
    if not TOPIC:
        sys.exit("NTFY_TOPIC is not set (GitHub repo > Settings > Secrets and variables > Actions).")
    msg = {"topic": TOPIC, "title": title, "message": message,
           "priority": priority, "tags": tags or ["ocean", "palm_tree"]}
    if PORTRAIT_URL:
        msg["icon"] = PORTRAIT_URL      # Android + web app sender icon
        msg["attach"] = PORTRAIT_URL    # iOS: shows as an image preview
    body = json.dumps(msg).encode()
    req = urllib.request.Request("https://ntfy.sh", data=body, headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=30).read()


def run(P, sun, now, state, send=push):
    sent = state.setdefault("sent", {})
    for k in [k for k, v in sent.items() if v < now - 3 * 86400]:
        del sent[k]
    if not TEST_MODE and not sun.is_day(now):
        print("Night: staying quiet.")
        return 0
    thr = 0.0 if TEST_MODE else THRESHOLD
    pre = "[TEST] " if TEST_MODE else ""
    count = 0
    for a, b in find_windows(P, sun, thr):
        start, end = P["T"][a], P["T"][b] + 3600
        if end <= now:
            continue
        reason = end_reason(P, sun, b, thr)
        kgo = "go-%d" % start
        if TEST_MODE or (alert_time(start, sun) <= now and kgo not in sent):
            text, thunder = go_message(P, a, b, now, reason)
            send(pre + go_title(P, a, b, now), text, 5 if thunder else 4)
            sent[kgo] = now
            count += 1
        kdie = "die-%d" % start
        long_enough = (end - start) >= 2 * 3600
        due = start <= now < end and now >= end - DIE_WARN_S
        if reason == "wind" and long_enough and (TEST_MODE or (due and kdie not in sent)):
            send(pre + "🌴 %s: the breeze is fading" % NAME, die_message(P, a, b, now), 4, ["ocean"])
            sent[kdie] = now
            count += 1
        if TEST_MODE:
            break
    return count


def main():
    data = fetch()
    P, sun = prepare(data)
    try:
        state = json.load(open(STATE_FILE))
    except Exception:
        state = {}
    n = run(P, sun, time.time(), state)
    json.dump(state, open(STATE_FILE, "w"), indent=1, sort_keys=True)
    print("Notifications sent:", n)


if __name__ == "__main__":
    main()
