@echo off
REM Thin cmd.exe launcher for the deployment CLI in deploy/ (as in financesite).
REM
REM     deploy-droplet bootstrap
REM     deploy-droplet check
REM     deploy-droplet deploy
REM     deploy-droplet status
setlocal
py -3 -m deploy %*
exit /b %ERRORLEVEL%
