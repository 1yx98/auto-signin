@echo off
rem ============================================================
rem  Task Scheduler entry point for the 20:55 sign-in task.
rem
rem  IMPORTANT: keep EVERY line of this file pure ASCII.
rem  A .bat launched by Task Scheduler may be read with a codepage
rem  that does not match the file, and non-ASCII bytes can make the
rem  batch die early (LastTaskResult 9009 / 2). Verified 2026-09-13.
rem  All Chinese output comes from signin.py (UTF-8), not this file.
rem
rem  Do NOT use %date% / %time% here: cmd expands them in the OEM
rem  codepage (the weekday name comes out in Chinese), which garbles
rem  the otherwise-UTF-8 task_run.log. Python writes the timestamps.
rem ============================================================
chcp 936 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHON=%~dp0runtime\python.exe
if not exist logs mkdir logs

rem Archive the previous task_run.log, then start a fresh one.
rem Why not delete it: task_run.log is the ONLY evidence that survives when
rem the process is killed hard (Ctrl+C / taskkill). On 2026-09-14 20:55 run,
rem run.log stopped mid-write and lost its tail, while task_run.log kept the
rem final "^C" marker -- that was how the kill (vs crash) was identified.
rem So we KEEP the mechanism, just stop it from growing forever.
rem Renaming works even while a previous handle is gone; if it fails
rem (file still locked), we simply append as before -- never fatal.
if exist logs\task_run.log (
  if exist logs\task_run.prev.log del /q logs\task_run.prev.log >nul 2>&1
  move /y logs\task_run.log logs\task_run.prev.log >nul 2>&1
)

if not exist "%PYTHON%" (
  echo [ERROR] runtime\python.exe not found at %PYTHON% >> logs\task_run.log
  exit /b 2
)
"%PYTHON%" signin.py >> logs\task_run.log 2>&1
