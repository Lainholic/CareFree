@echo off
title PainFree CarePay AI Server (PayPal AI Hackathon)
echo ========================================================
echo  [PainFree CarePay AI] Medical Bill Vision + PayPal Pay in 4
echo  Starting local server on http://127.0.0.1:8050 ...
echo ========================================================
cd /d "%~dp0"
if exist "C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe" (
    start "" "C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe" "http://127.0.0.1:8050"
) else (
    start "" "http://127.0.0.1:8050"
)
python app.py
pause