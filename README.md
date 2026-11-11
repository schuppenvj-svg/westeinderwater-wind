# Westeinderplassen Wind Alert (free, no app building)

Your friendly island guide **Kai** (a cheerful mix of laid-back surfer, sweet island helper and
sunny sports hero) sends you a written, Wii-Sports-Resort-style message ~3 hours before the
steady wind goes above your knots threshold, with medals, tips and a fun fact. All wind speeds are in knots. Daylight only: nothing is ever sent between sunset and sunrise;
alerts that would land at night wait for sunrise. Rates dinghy and wingfoil separately,
estimates waves at your SE launch corner and the leeward shore, warns about thunder/rain,
shows temperature + UV, and sends a "wind is about to die" warning ~1 h before it drops.

## Setup (about 10 minutes, Windows PC + iPhone)

### 1. Phone
Install the free **ntfy** app from the App Store. Tap "+" and subscribe to a topic with a long
random name that nobody can guess, e.g. `westeinder-wind-k7x92qpm4t`. Allow notifications.

### 2. GitHub
1. Create a free account at github.com and make a new repository. Choose **Public**:
   public repos get unlimited free Action minutes, and your topic name is stored as a hidden
   Secret (nothing private is in the files). Private also works, but then change the cron line
   in `.github/workflows/wind.yml` to `*/30 * * * *` to stay inside the free minutes.
2. Click **Add file > Upload files** and drag in everything from this folder, including the
   `.github` folder (Explorer on Windows shows it normally), then commit.
   Make sure these end up in the repo: `wind_alert.py`, `state.json`, `.github/workflows/wind.yml`.
3. **Settings > Secrets and variables > Actions**:
   - tab *Secrets* > New repository secret: name `NTFY_TOPIC`, value = your topic name.
   - tab *Variables* > New repository variable: name `THRESHOLD_KNOTS`, value e.g. `14`
     (steady wind in knots; change it any time).
   - Optional variables: `GUIDE_NAME` (rename Kai) and `PORTRAIT_URL` (see below).
4. If the "Save state" step ever fails: **Settings > Actions > General > Workflow permissions**
   > "Read and write permissions".

### 3. Test it
**Actions** tab > *wind-alert* > **Run workflow**, tick **test**, run. Within a minute you get
a sample alert built from the real forecast (it ignores the threshold and night rule).

After that it runs by itself every ~15 minutes.

## Portrait (optional)
I can't draw or supply Tidus artwork (it's copyrighted), but you can use any picture you like:
1. Upload your own .png or .jpg to this GitHub repo (Add file > Upload files), e.g. `portrait.png`.
2. Make a Variable `PORTRAIT_URL` with the direct link:
   `https://raw.githubusercontent.com/YOUR-USERNAME/YOUR-REPO/main/portrait.png`
   (the repo must be Public so the link works).
Note: iOS always shows an app's own icon as the sender icon, so on iPhone the portrait appears as an
image preview inside the notification (needs a recent ntfy iOS app, v1.7 or newer, as far as I can tell
from its release notes). Android and the ntfy web app show it as the sender icon.

## Good to know
- GitHub's scheduler is best-effort: runs can be late by several minutes. Alerts are never
  missed or duplicated because sent alerts are remembered in `state.json`.
- If you don't touch the repo for 60 days GitHub may pause scheduled runs: press
  "Enable workflow" in the Actions tab.
- Wave sizes are ESTIMATES from wind speed and the open-water distance (fetch) to your launch
  corner. The fetch distances in `FETCH_KM` (top of `wind_alert.py`) are rough guesses for the
  SE corner: edit them after a few sessions. The north has islands, which reduce waves.
- Ratings assume intermediate skill, ~75 kg for wing size. Edit `rate_dinghy` / `rate_wing`
  to change them.
- Forecast is Open-Meteo's best-match model at ~2 km resolution. It cannot see local gusts
  or squalls: always look at the sky.
- Safety first: the thunder warning is based on forecast weather codes only.
