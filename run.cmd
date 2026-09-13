@echo off
rem cmd reads this file in the OEM codepage, so switch to UTF-8 before any
rem Cyrillic appears below - otherwise the messages come out as garbage.
chcp 65001 >nul

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Нет виртуального окружения. Создайте его:
    echo    python -m venv .venv
    echo    .venv\Scripts\pip install -r requirements.txt
    exit /b 1
)

rem Вывод миграций уводим в отдельный файл, а не в bot.log: тот бот пишет сам,
rem в UTF-8 и с ротацией, и второй писатель сломал бы обоим. Совсем без файла
rem нельзя: упавшая миграция не даёт боту стартовать, и без её вывода причина
rem неизвестна. Растёт он на четыре строки за перезапуск.
if not exist "data" mkdir "data"
.venv\Scripts\alembic.exe upgrade head >> "data\startup.log" 2>&1
if errorlevel 1 (
    echo Миграции не прошли. Подробности: data\startup.log
    exit /b 1
)

rem Стдаут бота никуда не перенаправляем: под wscript его никто не читает, а
rem всё нужное лежит в data\bot.log
.venv\Scripts\python.exe -m bot
