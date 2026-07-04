@echo off
REM Mass Search launcher. Pass a topic (or flags) straight through.
REM   run.bat "field-aligned quad retopology" --queries 30 --workers 8
python "%~dp0mass_search.py" %*
