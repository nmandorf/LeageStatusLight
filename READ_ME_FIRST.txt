LEAGUE LIGHTS - WINDOWS SETUP

Run this on the Windows PC where you play League of Legends.

1. Right-click the ZIP and choose Extract All. Open the extracted folder.
2. Install Python if needed: https://www.python.org/downloads/windows/
   Python 3.10 or newer is required; the code was tested with Python 3.12.
3. Double-click setup_windows.cmd. Enter your Hubspace email and password.
   The password entry is hidden and is saved in Windows Credential Manager.
4. Double-click start_league.cmd. It runs in the background; you can close
   the small command window after it reports that the watcher started.
5. Open League and play on this PC.

YOUR SETTINGS
  Alive or not in a game: lights on at 20% brightness, 2700 K warm white.
  Dead: lights on at 100% brightness, red.
  Respawn: back to 20% brightness, 2700 K warm white.

Double-click status_league.cmd to check the watcher and light errors.
Double-click stop_league.cmd to stop it and restore standard warm white.
Start it again after restarting Windows. Do not move/delete this folder
while the watcher is running.

Both bulbs need their own working Wi-Fi connection. If a bulb only works
while the phone app is open, reconnect that bulb to 2.4 GHz Wi-Fi first.

FIRST GAME CHECK
Enter Practice Tool and watch status_league.cmd. It should report alive,
then dead when your champion dies, then alive after respawn. With no active
match it reports unavailable and uses the standard warm-white scene.

Adjust the lighting in league_settings.py, then stop and start the watcher.
Only one watcher can run at a time. Cloud latency can add a short delay.

TERMINAL COMMANDS (PowerShell, from this folder)
  .\light.cmd league start
  .\light.cmd league status
  .\light.cmd league stop
  .\light.cmd league run --dry-run

If you use PyCharm, open this folder and select .venv\Scripts\python.exe
as the project interpreter. The League lights run configuration is included.
Use stop_league.cmd for graceful shutdown; force-killing a process can leave
the last lighting scene on.

TESTING NOTES
Automated tests cover the game-state transitions and light request payloads.
The background lifecycle and standard scene were verified on the source Mac.
The Windows launchers were reviewed, but native Windows execution and a live
League death/respawn have not been verified on your gaming PC yet.

No saved passwords, tokens, personal configuration, logs, or virtual
environment from the Mac are included in this ZIP.
