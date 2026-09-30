@echo off
title CallTimerBot

echo ====================================================
echo      Starting Discord CallTimerBot...
echo ====================================================

if not exist ".env" (
    echo [ERROR] .env file not found.
    if exist ".env.example" (
        copy .env.example .env > nul
        echo Created .env from template. Please edit it.
    )
    pause
    exit /b 1
)

echo Checking dependencies...
python -m pip install -r requirements.txt --quiet

echo Running Bot...
python bot.py

pause
