@echo off
REM ============================================================
REM  Mo Chrome voi che do remote-debugging + profile rieng.
REM  Lan dau chay: dang nhap ca 4 site (ChatGPT, Gemini,
REM  NotebookLM, Claude) vao cua so nay. Session se duoc luu lai.
REM ============================================================

set CHROME="C:\Program Files\Google\Chrome\Application\chrome.exe"
set PROFILE_DIR=%~dp0chrome-profile
set PORT=9222

echo Dang mo Chrome (port %PORT%, profile: %PROFILE_DIR%)...
%CHROME% ^
  --remote-debugging-port=%PORT% ^
  --user-data-dir="%PROFILE_DIR%" ^
  --no-first-run ^
  --no-default-browser-check ^
  https://chatgpt.com https://gemini.google.com https://notebooklm.google.com https://claude.ai
