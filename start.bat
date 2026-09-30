@echo off
chcp 65001 > nul
title CallTimerBot

echo ====================================================
echo      Discord通話自動切断Bot 起動スクリプト
echo ====================================================

if not exist ".env" (
    echo [警告] .env ファイルが見つかりません。
    if exist ".env.example" (
        copy .env.example .env > nul
        echo 新しい .env を作成しました。メモ帳などで編集してください。
    )
    pause
    exit /b 1
)

echo 依存関係を確認中...
python -m pip install -r requirements.txt --quiet

echo Botを起動しています...
python bot.py

pause
