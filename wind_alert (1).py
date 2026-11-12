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
import os, re, sys, json, math, time, random, urllib.request, urllib.parse, urllib.error
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Amsterdam")

# ----------------------------- SETTINGS ------------------------------------
LAT = float(os.environ.get("LAT") or 52.250)      # Westeinderplassen (forecast grid is ~2 km,
LON = float(os.environ.get("LON") or 4.733)       # so any point on the lake gives the same result)
THRESHOLD = float(os.environ.get("THRESHOLD_KNOTS") or 14)   # steady wind, knots
TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
PORTRAIT_URL = os.environ.get("PORTRAIT_URL", "").strip()   # optional, see README
TEST_MODE = os.environ.get("TEST_MODE") == "1"
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
LEAD_S = 3 * 3600          # warn this long before the windy window starts
DIE_WARN_S = 3600          # "wind dying" warning this long before it drops
LAUNCH = "SE"              # your launch corner (used in the text only)
RIDER_KG = float(os.environ.get("RIDER_KG") or 68)   # your weight, used for wing size advice
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


def _m(x):
    return round(x * (RIDER_KG / 75.0) * 2) / 2


def wing_tip(w):
    # sizes below are the usual picks for a 75 kg rider, scaled to RIDER_KG
    if w < 10: return "a %g metre or bigger wing with a big foil, and be ready to pump" % _m(7)
    if w < 14: return "a %g to %g metre wing" % (_m(5.5), _m(7))
    if w < 18: return "a %g to %g metre wing" % (_m(4.5), _m(5.5))
    if w < 24: return "a %g to %g metre wing" % (_m(3.5), _m(4.5))
    return "a %g metre wing or smaller, and only if you're an expert" % _m(3.5)


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
            print("Open-Meteo request failed (try %d/3): %s" % (i + 1, e))
            time.sleep(3 * (i + 1))
    sys.exit("Could not get the forecast from Open-Meteo: %s" % err)


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
# Plain game-dialogue style: Kai talks, one short "text box" per paragraph.
# Every part has several wordings, picked at random, so reports don't repeat.

# ---- extra pools (added to the ones defined above) ----
GREET_GO.extend([
    "Hey hey, {n} here! Whoa, you're gonna want to hear this, friend!",
    "Oh, hello there! It's {n}, and I have the loveliest wind news for you today!",
    "Ha ha, what's up, it's {n}! Grab your gear, because the lake is calling!",
    "Dude! {n} here! I just looked at the forecast and, wow, check this out!",
    "Hi hi! {n} reporting live from Westeinder Resort! I've got news that'll make you smile!",
    "Whoa, hey, it's {n}! Take it easy, and let me tell you about the wind!",
    "Attention, wind-chasers! {n} here with the lake report!",
    "Heya, buddy! {n} checking in with some very exciting weather news!",
])
CLOSERS.extend([
    "Okay, that's all from me! Have an amazing session, buddy! 🌴",
    "Go get 'em, champ! I'll be cheering from the shore!",
    "Stay safe, have fun, and say hi to the ducks for me!",
    "That's it for now! Come back safe and tell me all about it! 🌴",
    "See you on the water, wind-chaser! Take it easy out there!",
    "Okay, bye bye for now! Remember to smile and breathe!",
    "Time to shine, buddy! Make it a good one! 🌴",
])
TIPS.extend([
    "Keep your phone in a waterproof pouch, so you can call for help if you ever need it.",
    "Cold water shock is real, even on a sunny day, so wear enough neoprene.",
    "Foilers, keep your hands and feet away from the foil when you fall, and always fall away from the board.",
    "Watch out for commercial ships in the north-west, where the lake meets the Ringvaart. They share the water there!",
    "Check your wing or sail for tears and your leash for wear before you launch. Five seconds now saves a long swim!",
    "Stay clear of the small islands and shallows in the north, where the water gets tricky.",
    "Look at the sky every few minutes. A dark cloud wall and a sudden cool breeze can mean a gust front is coming!",
    "Try not to go out alone. A buddy is the best safety gear there is!",
    "Bring a warm layer for after your session. The wind chill hits fast when you stop moving!",
])
FACTS.extend([
    "The first America's Cup race was in 1851 around the Isle of Wight, and the trophy was first called the Hundred Guinea Cup!",
    "Windsurfing became an Olympic sport in 1984, and foiling windsurfers and kites were Olympic classes at Paris 2024!",
    "The Optimist dinghy was designed in 1947 by Clark Mills in Florida, and it's still where kids around the world learn to sail!",
    "The Dutch have used wind power for centuries: the 19 windmills at Kinderdijk are a UNESCO World Heritage Site!",
    "In the Northern Hemisphere, wind circles around a low-pressure area counterclockwise. That's the Earth's rotation bending the wind!",
    "A sailboat can't sail straight into the wind, so sailors zigzag, which is called tacking!",
    "After a strong wind stops, a lake can slosh back and forth like a bathtub. That's called a seiche!",
    "The Flying Dutchman dinghy was designed in the Netherlands in 1951, and it was an Olympic class from 1960 to 1992!",
    "Sailors call a temporary jump in the wind a puff, and a temporary drop a lull!",
])
DINGHY_LABELS["calm"] += ["Not a breath of wind! Perfect for a lake-side snack.", "Glassy water and zero push. Time for some stretching!"]
DINGHY_LABELS["light"] += ["Gentle and easy, like a Sunday cruise!", "A soft breeze today, great for practising your boat handling!"]
DINGHY_LABELS["sweet"] += ["Perfect sailing air! Hoist that sail!", "This is the good stuff, buddy! Let's gooo!"]
DINGHY_LABELS["spicy"] += ["Great wind, but those gusts have opinions!", "Keep your eyes on the water, those puffs are sneaky!"]
DINGHY_LABELS["strong"] += ["Strong wind! Reef early and hike hard!", "Big breeze energy! Hold on tight and keep smiling!"]
DINGHY_LABELS["storm"] += ["Way too much wind today. The boat is better off ashore!", "Not a sailing day, friend. Let's save it for the next one!"]
WING_LABELS["calm"] += ["Not enough wind to fly today, buddy.", "The foil is sleeping in. Let it rest!"]
WING_LABELS["marginal"] += ["Just on the edge of takeoff! Big wing and patience!", "Maybe, just maybe, you'll fly! Pump hard!"]
WING_LABELS["sweet"] += ["Flying weather! The foil is calling your name!", "Perfect flight conditions, let's goooo!"]
WING_LABELS["spicy"] += ["Great power, but gusty. Stay sharp up there!", "Lots of lift, plus some surprises. Keep your wing flexible!"]
WING_LABELS["strong"] += ["Powerful wind! Small wing, big smile!", "Heavy-hitting breeze, experts only! Stay in control!"]
WING_LABELS["storm"] += ["Way too much wind for wing foiling, stay on land today!", "Today's a no-go, champ. The lake will be there tomorrow!"]

# ---- sentence templates ----
INTRO_T = [
    "Your play window {when}! It should last until about {end}, roughly {hours} hours, {ends}.",
    "Here's the deal: the play window {when}. It runs until about {end}, so that's roughly {hours} hours, {ends}.",
    "Mark it down! The window {when}, and it lasts until about {end}, around {hours} hours, {ends}.",
    "Big news: the play window {when}! You get about {hours} hours of fun, until around {end}, {ends}.",
]
WIND_T = [
    "The steady wind will be around {avg} knots, peaking at {peak}, and the gusts could reach {gust} knots. The gusts are {gust_txt}. It's coming from the {dir}, {shore_txt}.",
    "Wind check! Steady {avg} knots, up to {peak} at the strongest, with gusts of {gust} knots. I'd call the gusts {gust_txt}. The wind blows from the {dir}, and {shore_txt}.",
    "Here's the wind: about {avg} knots steady, {peak} at its peak, gusting to {gust} knots, and the gusts are {gust_txt}. Direction: {dir}, and {shore_txt}.",
    "So, the breeze! {avg} knots of steady wind, {peak} at the peak, {gust} in the gusts, which are {gust_txt}. It comes from the {dir}, {shore_txt}.",
]
OFFSHORE_T = [
    "Careful with that offshore wind! Go with a buddy, stay upwind of the shore, and have a rescue plan.",
    "Heads up, offshore wind can carry you away from the beach. Stay upwind, go with a buddy, and keep a rescue plan ready!",
    "That offshore wind is sneaky, buddy! Keep upwind of the shore and tell someone on land where you're going.",
]
LEE_LAUNCH_T = [
    "Now the waves! Your {launch} launch corner is the leeward end, so that's where the biggest waves pile up: about {cm_l} centimetres, {chop_l}, since the wind gets about {f} kilometres to build them. The {sec} end is windward, so it should be calmer near the shore. Those are only estimates, okay?",
    "Let's talk waves! Your {launch} corner is on the leeward side, so expect the lake's biggest chop right there: roughly {cm_l} centimetres, {chop_l}. The wind gets about {f} kilometres of runway. Over at the {sec} end it'll be calmer near the shore. Just estimates, though!",
    "Waves incoming! Since your {launch} launch is the leeward end, the water gets lumpy there: around {cm_l} centimetres, {chop_l}, thanks to about {f} kilometres of open water. The windward {sec} end stays smoother. Remember, I'm guessing from the numbers, not measuring!",
]
WAVE_ON_T = ["At your {launch} launch corner I'd guess about {cm} centimetres, {chop}.",
             "Your {launch} launch spot should see about {cm} centimetres, {chop}."]
WAVE_OFF_T = ["At your {launch} launch corner I'd guess about {cm} centimetres, {chop}, but the chop builds fast once you head out.",
              "Right at your {launch} launch it's about {cm} centimetres, {chop}, but out on the lake it gets lumpier quickly."]
WAVE_T = [
    "Now the waves! {wave1} On the leeward side, the {opp} end, expect about {cm_lee} centimetres, {chop_lee}, because the wind gets about {f} kilometres to build waves. The {sec} end is windward, so it should be calmer near the shore. Those are only estimates, okay?",
    "Wave report! {wave1} The leeward {opp} end is the lumpy one: about {cm_lee} centimetres, {chop_lee}, with about {f} kilometres of open water behind the wind. The windward {sec} end stays calmer. All guesses, buddy!",
    "Time for the waves! {wave1} Down at the {opp} end, the leeward side, it gets rougher: roughly {cm_lee} centimetres, {chop_lee}, since the wind has about {f} kilometres to work with. The {sec} end should be calmer. Estimates only, okay?",
]
MEDAL_T = [
    "For {sport} I'm giving you a {medal} Medal, {stars} stars out of five! {label}",
    "{Sport} gets a {medal} Medal from me, that's {stars} stars out of five! {label}",
    "Drumroll for {sport}... a {medal} Medal, with {stars} stars out of five! {label}",
]
NOMEDAL_T = [
    "For {sport} there's no medal this time, just {stars} stars out of five. {label}",
    "{Sport} doesn't earn a medal today, only {stars} stars out of five. {label}",
    "No medal for {sport} today, buddy: {stars} stars out of five. {label}",
]
DINGHY_HOUR_T = ["Your best hour is {h}. My tip: {tip}.", "The sweet spot is {h}. Here's my tip: {tip}.",
                 "Aim for {h}, that's your best hour. Tip time: {tip}."]
WING_HOUR_T = ["Your best hour is {h}, and for a rider around {kg} kilos I'd pick {tip}.",
               "Hit the water around {h}! For about {kg} kilos, I'd grab {tip}.",
               "{h} is your best hour. At around {kg} kilos, go with {tip}."]
THUNDER_T = ["Oh no, there's a risk of thunderstorms around {t}! Please stay off the water!",
             "Uh-oh, thunderstorms are possible around {t}! Please stay safe on land, okay?",
             "Whoa, thunder risk around {t}! Not a day to be out on open water, buddy!"]
HEAVY_T = ["Heavy showers are possible, up to {r} millimetres an hour, so pack a shell!",
           "Expect some heavy showers, up to {r} millimetres an hour. Bring a good jacket!"]
LIGHT_T = ["There might be a few light showers, up to {r} millimetres an hour.",
           "A light shower or two could pass by, up to {r} millimetres an hour."]
DRY_T = ["No rain in sight!", "It should stay nice and dry!", "No rain expected, hooray!"]
TEMP_T = ["It'll be {deg}, feeling like {feel}, so that's {tip}.",
          "Temperature check: {deg}, feels like {feel}. That's {tip}.",
          "Expect {deg}, feeling like {feel}, which means {tip}."]
UV_T = ["The UV index gets to {uv}, so don't forget your sunscreen!",
        "UV is going up to {uv}, so slap on that sunscreen, buddy!"]
SAFETY_LEAD = ["Safety tip from me: ", "Quick safety tip: ", "Oh, and a safety tip: ", "Before you go, a safety tip: "]
FACT_LEAD = ["Oh! One more thing, fun fact time! ", "Fun fact time! ", "Did you know? ",
             "Okay, last thing, a fun fact! ", "Here's a fun fact for you! "]

DIE_OPEN_T = [
    "{n} here! Oh no, the wind is fading around {t}{down}. The steady wind is about to drop below {thr} knots!",
    "Hey, it's {n}! Bad news, buddy: the wind starts fading around {t}{down}, dropping below {thr} knots!",
    "{n} again! Heads up, the breeze is running out around {t}{down}. It's dropping below {thr} knots!",
]
DIE_LAP_T = [
    "This is the last lap, buddy! Start heading back now so you don't get stranded in the middle of the lake with a limp sail or a sleepy wing.",
    "Time to turn around, champ! Head back while you still have wind, so you don't end up drifting in the middle of the lake.",
    "Last call! Start working your way back to shore now, before the wind says goodbye and leaves you floating!",
]
DIE_FACT_LEAD = ["Oh, and a quick fun fact for the ride back! ", "Here's a fun fact for the way back! ",
                 "Fun fact to keep you smiling on the way in! "]
DIE_CLOSE = ["Great session, champ! Stroopwafel time soon! 🌴", "Awesome job out there! Go warm up and grab a snack! 🌴",
             "You did great today, buddy! Hot chocolate time soon! 🌴"]

MEDAL_NAME = {5: "Platinum", 4: "Gold", 3: "Silver", 2: "Bronze"}
NUM_WORD = {0: "zero", 1: "one", 2: "two", 3: "three", 4: "four", 5: "five"}


def _vals(arr, rng):
    return [arr[i] for i in rng if arr[i] is not None]


def _when(start, now):
    lead = start - now
    if lead > 5400: return "starts %s, that's in about %d hours" % (dayhhmm(start), round(lead / 3600))
    if lead > 0: return "starts %s, that's in about %d minutes" % (dayhhmm(start), round(lead / 60))
    return "is already on, it started at %s" % hhmm(start)


def _verdict(sport, score, label):
    pool = MEDAL_T if score in MEDAL_NAME else NOMEDAL_T
    return random.choice(pool).format(sport=sport, Sport=sport[0].upper() + sport[1:], stars=NUM_WORD[score],
                                      medal=MEDAL_NAME.get(score, ""), label=label)


def go_title(P, a, b, now):
    rng = range(a, b + 1)
    w_avg = sum(P["W"][i] for i in rng) / len(rng)
    return "%s's Resort Report: %d kn from %s" % (NAME, round(w_avg), c16(P["D"][a]))


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
    ends = {"wind": "and then the wind fades", "sunset": "right up until sunset", "forecast": "and maybe longer"}[reason]
    gust_txt = "nice and smooth" if spread < 7 else ("a bit puffy" if spread < 10 else "very puffy")
    d0, d1 = c16(D[a]), c16(D[b])
    dir_txt = d0 if d0 == d1 else "%s, turning to %s later" % (d0, d1)

    w = W[ip]
    f_lee = max(FETCH_KM[sec], FETCH_KM[OPP[sec]])
    h_l, h_lee = hs(w, FETCH_KM[sec]), hs(w, f_lee)

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

    boxes = []
    boxes.append("%s %s" % (random.choice(GREET_GO).format(n=NAME), random.choice(INTRO_T).format(
        when=_when(start, now), end=hhmm(end), hours=len(rng), ends=ends)))

    shore_txt = {"onshore": "that's onshore at your %s launch, so it blows you back to the beach" % LAUNCH,
                 "offshore": "that's offshore at your %s launch, so it blows you away from the shore" % LAUNCH,
                 "cross-shore": "that's cross-shore at your %s launch" % LAUNCH}[shore]
    boxes.append(random.choice(WIND_T).format(avg=round(w_avg), peak=round(W[ip]), gust=round(g_peak),
                                              gust_txt=gust_txt, dir=dir_txt, shore_txt=shore_txt))
    if shore == "offshore" and w >= 8:
        boxes.append(random.choice(OFFSHORE_T))

    if f_lee == FETCH_KM[sec] and f_lee > 0.5:
        boxes.append(random.choice(LEE_LAUNCH_T).format(launch=LAUNCH, cm_l=cm(h_l), chop_l=chop(h_l), f="%.1f" % f_lee, sec=sec))
    else:
        wave1 = random.choice(WAVE_OFF_T if shore == "offshore" else WAVE_ON_T).format(
            launch=LAUNCH, cm=cm(h_l), chop=chop(h_l))
        boxes.append(random.choice(WAVE_T).format(wave1=wave1, opp=OPP[sec], cm_lee=cm(h_lee), chop_lee=chop(h_lee),
                                                  f="%.1f" % f_lee, sec=sec))

    boxes.append("%s %s" % (_verdict("dinghy sailing", sd, ld),
                            random.choice(DINGHY_HOUR_T).format(h=hhmm(T[bd]), tip=dinghy_tip(W[bd]))))
    boxes.append("%s %s" % (_verdict("wing foiling", sw, lw),
                            random.choice(WING_HOUR_T).format(h=hhmm(T[bw]), kg="%g" % RIDER_KG, tip=wing_tip(W[bw]))))

    ext = list(range(max(0, a - 1), min(P["n"] - 1, b + 1) + 1))
    thunder = [T[i] for i in ext if P["WC"][i] is not None and P["WC"][i] >= 95]
    rain = max(_vals(P["PREC"], ext) or [0])
    temps = _vals(P["TEMP"], rng)
    apps = _vals(P["APP"], rng)
    uv = max(_vals(P["UV"], rng) or [0])
    sky = []
    if thunder:
        sky.append(random.choice(THUNDER_T).format(t=hhmm(thunder[0])))
    elif rain >= 2.0:
        sky.append(random.choice(HEAVY_T).format(r="%.1f" % rain))
    elif rain >= 0.3:
        sky.append(random.choice(LIGHT_T).format(r="%.1f" % rain))
    else:
        sky.append(random.choice(DRY_T))
    if temps:
        t_lo, t_hi = min(temps), max(temps)
        feel = min(apps) if apps else t_lo
        tip = ("drysuit or thick wetsuit weather" if t_hi <= 9 else
               "full wetsuit weather" if t_hi <= 15 else
               "shorty-wetsuit weather" if t_hi <= 21 else "board-shorts weather")
        deg = "%d degrees" % round(t_lo) if round(t_lo) == round(t_hi) else "%d to %d degrees" % (round(t_lo), round(t_hi))
        sky.append(random.choice(TEMP_T).format(deg=deg, feel=round(feel), tip=tip))
    if uv >= 3:
        sky.append(random.choice(UV_T).format(uv=round(uv)))
    boxes.append(" ".join(sky))

    boxes.append(random.choice(SAFETY_LEAD) + random.choice(TIPS))
    boxes.append(random.choice(FACT_LEAD) + random.choice(FACTS))
    boxes.append(random.choice(CLOSERS))
    return "\n\n".join(boxes), bool(thunder)


def die_message(P, a, b, now):
    T, W = P["T"], P["W"]
    end = T[b] + 3600
    nxt = W[b + 1] if b + 1 < P["n"] and W[b + 1] is not None else None
    down = ", down to about %d knots" % round(nxt) if nxt is not None else ""
    boxes = [random.choice(DIE_OPEN_T).format(n=NAME, t=hhmm(end), down=down, thr="%g" % THRESHOLD),
             random.choice(DIE_LAP_T),
             random.choice(DIE_FACT_LEAD) + random.choice(FACTS),
             random.choice(DIE_CLOSE)]
    return "\n\n".join(boxes)


# ----------------------------- SENDING -------------------------------------
def push(title, message, priority=4, tags=None):
    if not TOPIC:
        sys.exit("NTFY_TOPIC is not set (GitHub repo > Settings > Secrets and variables > Actions).")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", TOPIC):
        sys.exit("NTFY_TOPIC is invalid: use only letters, numbers, '-' and '_' (max 64 characters), "
                 "no spaces/dots/@. Currently it is %d characters long." % len(TOPIC))
    msg = {"topic": TOPIC, "title": title, "message": message,
           "priority": priority, "tags": tags or ["ocean", "palm_tree"]}
    if PORTRAIT_URL:
        msg["icon"] = PORTRAIT_URL      # Android + web app sender icon
        msg["attach"] = PORTRAIT_URL    # iOS: shows as an image preview
    body = json.dumps(msg).encode()
    req = urllib.request.Request("https://ntfy.sh/", data=body, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=30).read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        sys.exit("ntfy.sh refused the notification: HTTP %s %s. Response: %s" % (e.code, e.reason, detail))


def run(P, sun, now, state, send=push):
    sent = state.setdefault("sent", {})
    for k in [k for k, v in sent.items() if v < now - 3 * 86400]:
        del sent[k]
    day = sun.is_day(now)
    thr = 0.0 if TEST_MODE else THRESHOLD
    pre = "[TEST] " if TEST_MODE else ""

    # ---- diagnostics (visible in the GitHub Action log) ----
    day_w = [P["W"][i] for i, t in enumerate(P["T"])
             if t + 3600 > now and sun.is_day(t + 1800) and P["W"][i] is not None]
    print("Time now: %s | daylight: %s | threshold: %g kn | strongest daylight steady wind ahead: %s" % (
        dayhhmm(now), "yes" if day else "no", THRESHOLD,
        ("%d kn" % round(max(day_w))) if day_w else "n/a"))
    wins = [(a, b) for a, b in find_windows(P, sun, thr) if P["T"][b] + 3600 > now]
    if not wins:
        print("No daylight period above %g kn in the forecast." % THRESHOLD)
    for a, b in wins:
        st, en = P["T"][a], P["T"][b] + 3600
        print("Window %s to %s | alert due %s | already sent: %s" % (
            dayhhmm(st), hhmm(en), dayhhmm(alert_time(st, sun)), "yes" if ("go-%d" % st) in sent else "no"))
    if not TEST_MODE and not day:
        print("Night: staying quiet. Held alerts go out after sunrise.")
        return 0

    count = 0
    for a, b in wins:
        start, end = P["T"][a], P["T"][b] + 3600
        reason = end_reason(P, sun, b, thr)
        kgo = "go-%d" % start
        if TEST_MODE or (alert_time(start, sun) <= now and kgo not in sent):
            text, thunder = go_message(P, a, b, now, reason)
            send(pre + go_title(P, a, b, now), text, 5 if thunder else 4)
            if not TEST_MODE:           # a test must never mark the real alert as sent
                sent[kgo] = now
            count += 1
        kdie = "die-%d" % start
        long_enough = (end - start) >= 2 * 3600
        due = start <= now < end and now >= end - DIE_WARN_S
        if reason == "wind" and long_enough and (TEST_MODE or (due and kdie not in sent)):
            send(pre + "🌴 %s: the breeze is fading" % NAME, die_message(P, a, b, now), 4, ["ocean"])
            if not TEST_MODE:
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
