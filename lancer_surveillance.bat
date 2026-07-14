@echo off
rem Lance la surveillance CROUS en arriere-plan et journalise dans crous_watch.log
cd /d "%~dp0"
python crous_watch.py >> crous_watch.log 2>&1
