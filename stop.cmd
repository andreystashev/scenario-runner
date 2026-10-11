@echo off
setlocal
if defined SCENARIO_RUNNER_PYTHON (
    "%SCENARIO_RUNNER_PYTHON%" "%~dp0launcher.py" stop %*
) else (
    where py >nul 2>nul
    if errorlevel 1 (
        python "%~dp0launcher.py" stop %*
    ) else (
        py -3 "%~dp0launcher.py" stop %*
    )
)
exit /b %errorlevel%
