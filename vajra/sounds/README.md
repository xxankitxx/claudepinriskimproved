# VAJRA 2.0.2 alert sounds

Only these MP3 files ever play — in the dashboard. No browser tones, no Python/Windows sounds.
All are CC0 sounds downloaded from GitHub sound collections and loudness-matched (see CREDITS.md).
Your VAJRA 1.0 sounds are kept in `vajra1_backup/`.

| file | when it plays |
|---|---|
| `entry.mp3` | a paper trade was **entered** ("Go go go!") |
| `exit_profit.mp3` | a trade **closed green** ("Objective achieved") |
| `exit_loss.mp3` | a trade **closed red or flat** ("Mission failed") |
| `approach_up.mp3` / `approach_dn.mp3` | NIFTY within 15 pts of its **upper / lower** level and an entry is possible; also ~10 s before a cooldown ends while NIFTY is at a level ("Prepare yourself" + rising / falling sweep) |
| `feed_alarm.mp3` | NIFTY feed frozen / implausible, **exit rejected**, or **position left open** at the close (alarm + "Call for backup!") |
| `armed.mp3` | 09:20, once a day: proves sound reaches your speakers ("Round one... Fight!") |

Never a sound for sister/stock breaches.

* Sound is **ON by default** and stays ON until you click the pill to turn it OFF; OFF stays OFF until
  you click it ON again.
* If the browser has not allowed sound yet, a **red banner** and the tab title say **SOUND LOCKED**.
  Click anywhere: missed trade cues of the last 2 minutes then play. `run_vajra.ps1` opens a window
  that is allowed to play sound with no click.
* An exit cuts an entry; an entry cuts an approach; an approach never plays over a trade cue.
* Changed files are re-loaded by themselves. A missing/broken file shows a red banner.
* Shift-click the pill to hear every cue. Replace any file with your own MP3 of the same name.
