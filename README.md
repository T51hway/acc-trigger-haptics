# ACC Trigger Haptics

English | [简体中文](README.zh-CN.md)

> Unofficial personal project. Not affiliated with Kunos Simulazioni or 505 Games.

Adds Xbox controller **trigger haptics** to Assetto Corsa Competizione (ACC).
ACC only rumbles the two grips; this program reads the game's telemetry and uses
all four motors: grips for road feel, triggers for braking, throttle and tyre limits.

---

## Quick start (about 3 minutes)

Before you start: connect the controller over Bluetooth and turn **Steam Input** off in Steam.

1. Run **`ACC Trigger Haptics.exe`**.
2. Click **Install takeover** — this copies a small proxy DLL into the ACC folder,
   so the game can no longer overwrite the trigger rumble (one time only).
3. Get on track in ACC and click **Start haptics**.
4. All four lamps green (Proxy DLL / Controller / ACC telemetry / Haptics) means it is working.
5. Drag the sliders to tune; changes apply within a second, no restart needed.

Running from source instead? The two helper scripts `开启接管.bat` and `恢复原生.bat`
do the same install/restore job.

---

## What each motor does

| Motor | Feedback |
| --- | --- |
| Left trigger | Brake pressure; short "tick-tick" when a front wheel locks or ABS kicks in |
| Right trigger | Throttle pressure; rear wheelspin; understeer (front slipping more than rear) |
| Left grip | Left-side road texture |
| Right grip | Right-side road texture |

The brake/throttle floor fades out below 5 km/h, so pressing the pedals at a standstill stays quiet.

---

## Tuning

Five sliders in the panel, saved automatically:

| Slider | What it does | Code default |
| --- | --- | --- |
| Road texture | Grip rumble from the road surface | 0.21 |
| Brake floor | Left trigger follows brake pressure | 0.20 |
| Throttle floor | Right trigger follows throttle | 0.15 |
| Event strength | Pulse strength for lock / ABS / wheelspin | 0.75 |
| Understeer gain | Right trigger strength when the front pushes; 0 = off | 1.00 |

You can also edit `手感设置.toml` directly (the `调手感.bat` helper opens it).
Only change the numbers on the right-hand side of `=`.

---

## Panel

- Four status lamps: proxy DLL / controller / ACC telemetry / haptics
- Five sliders, saved as you drag
- Start / stop haptics
- Install / restore the takeover DLL (refuses while ACC is running)
- Language switch (English / 中文) at the bottom left
- Version number at the bottom right

Closing the window stops the haptics and zeroes the motors first.
Killing the process from Task Manager may leave the motors running — close it with the X.

Errors are written to `panel.log` next to the executable.

---

## Requirements

- Windows 10 / 11
- Xbox One or Xbox Series controller (Xbox 360 has no trigger motors)
- **Bluetooth connection** — tested; wired connection untested
- **Steam Input must be off**, otherwise it swallows the trigger commands
- **The takeover DLL must be installed**; disabling in-game vibration alone is not enough

The packaged exe does not require Python.

---

## FAQ

**No trigger vibration at all**
Check the "Proxy DLL" lamp first. Without it, ACC overwrites the trigger rumble every frame.
Also make sure Steam Input is off.

**It worked before, but became stuttery after an ACC update**
Run the takeover install again (the update may have replaced the DLL).

**How do I get ACC's native rumble back?**
Click **Restore native** in the panel, or run `恢复原生.bat`. Wheels are unaffected.

**Steam file verification complains**
The takeover DLL is an added file, so Steam reports one unexpected file. That is expected;
delete or rename it and the warning goes away.

---

## Known limitations

- Rear-end slides (oversteer) are not detected as such; only longitudinal wheelspin is
- Long sessions, and Bluetooth reconnects, have not been endurance-tested
- Wired connection and the official wireless adapter are untested

---

## Run from source (optional)

Requires Python 3.11+; the panel also needs a full Python build with tkinter.

```
python src/main.py      # console version
python src/panel.py     # graphical panel
```

---

## License

MIT, see `LICENSE`.
