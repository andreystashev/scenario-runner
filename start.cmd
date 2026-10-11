@echo off
setlocal
if defined SCENARIO_RUNNER_PYTHON (
    "%SCENARIO_RUNNER_PYTHON%" "%~dp0launcher.py" start %*
) else (
    where py >nul 2>nul
    if errorlevel 1 (
        python "%~dp0launcher.py" start %*
    ) else (
        py -3 "%~dp0launcher.py" start %*
    )
)
exit /b %errorlevel%
