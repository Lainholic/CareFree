@echo off
title PainFree CarePay AI Server (PayPal AI Hackathon) - C:\ai\project_paypal_carepay
echo ========================================================
echo  [PainFree CarePay AI] Medical Bill Vision + PayPal Pay in 4
echo  Starting local server on http://127.0.0.1:8050 ...
echo ========================================================
cd /d "C:\AI\project_paypal_carepay"
start "" "C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe" "http://127.0.0.1:8050"
python app.py
pause