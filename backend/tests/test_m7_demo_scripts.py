from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_demo_launcher_only_composes_base_infrastructure_scripts() -> None:
    content = (ROOT / "scripts" / "run_demo.ps1").read_text(encoding="utf-8")
    referenced_launchers = {
        path.name for path in (ROOT / "scripts").glob("run_*.ps1") if path.name in content
    }

    assert referenced_launchers == {
        "run_backend.ps1",
        "run_worker.ps1",
        "run_frontend.ps1",
    }
    assert "Start-Process" in content
    assert "stop_demo.ps1" in content


def test_demo_launcher_preserves_named_port_arguments() -> None:
    content = (ROOT / "scripts" / "run_demo.ps1").read_text(encoding="utf-8")

    assert 'arguments = @("-Port", $BackendPort.ToString())' in content
    assert 'arguments = @("-Port", $FrontendPort.ToString())' in content
    assert 'if ($argument -match "^-[A-Za-z][A-Za-z0-9]*$")' in content
    assert '"\'" + $argument.Replace("\'", "\'\'") + "\'"' in content
    assert "vite --host -Port" not in content
    assert "http://-Port" not in content


def test_backend_and_worker_use_explicit_conda_environment() -> None:
    content = (ROOT / "scripts" / "run_demo.ps1").read_text(encoding="utf-8")

    assert "run --no-capture-output -n anime-platform powershell.exe" in content
    assert content.count("use_conda = $true") == 2
    assert content.count("use_conda = $false") == 1
    assert "conda_environment" in content


def test_demo_stop_script_only_targets_recorded_process_trees() -> None:
    content = (ROOT / "scripts" / "stop_demo.ps1").read_text(encoding="utf-8")

    assert "demo-processes.json" in content
    assert "taskkill.exe /PID $processId /T /F" in content
    assert "Get-Process -Id $processId" in content
    assert "process_start_utc" in content
    assert "process_name" in content
    assert "$remainingServices += $service" in content
    assert "launcher state was retained for a safe retry" in content
