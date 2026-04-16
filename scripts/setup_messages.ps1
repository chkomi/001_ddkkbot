[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Key,
    [string]$Lang = ""
)

$langRaw = $Lang
if ($null -eq $langRaw) {
    $langRaw = ""
}
$langCode = $langRaw.Trim().ToLowerInvariant()
if ($langCode -ne "ko") {
    try {
        $detected = (Get-Culture).TwoLetterISOLanguageName
    } catch {
        $detected = "en"
    }
    if ($detected -eq "ko") {
        $langCode = "ko"
    } else {
        $langCode = "en"
    }
}

$messages = @{
    en = @{
        setup_title = "Sonolbot setup start"
        setup_notice_components = "[Notice] setup assumes these are available:"
        setup_notice_components_2 = "         Missing items are checked during setup with manual install guidance."
        setup_req_1 = "  - WSL + Ubuntu"
        setup_req_2 = "  - python3 / python3-pip / python3-venv"
        setup_req_3 = "  - node / npm"
        setup_req_4 = "  - codex CLI and codex login"
        setup_req_5 = "  - Windows Python tkinter for control_panel.exe"
        setup_req_6 = "  - Python packages: python-dotenv, requests"
        setup_req_7 = "  - Recommended: run as Administrator for WSL DNS hardening"
        setup_log_file_label = "Log file"
        setup_may_pause = "[Info] setup may look paused while commands are running."
        setup_log_label = "setup log"
        setup_fail = "[Error] setup failed"
        setup_ok = "[OK] setup completed"
        prompt_run_panel = "Run control panel now [Y/N]: "
        press_any_key = "Press any key to close this window."

        wsl_setup_launcher = "Sonolbot WSL setup launcher"
        wsl_not_installed = "[Error] WSL is not installed."
        wsl_install_hint = "        In admin PowerShell: wsl --install"
        wsl_path_convert_fail = "[Error] Failed to convert Windows path to WSL path."
        wsl_script_missing = "[Error] WSL setup script not found"
        project_path_label = "Project path"
        wsl_path_label = "WSL path"
        run_setup_wsl = "Running setup_wsl.sh in WSL..."
        setup_wsl_ok = "[OK] setup_wsl.sh completed"
        setup_wsl_fail = "[Error] setup_wsl.sh failed"

        cp_missing_1 = "[Error] control_panel.exe is missing."
        cp_missing_2 = "       Run build_control_panel_exe.bat and retry."
        tk_missing_1 = "[Info] Windows Python tkinter is missing."
        tk_missing_2 = "      Reinstall Python with Tcl/Tk support and run setup again."

        wsl_dns_check = "[Windows] Checking WSL DNS hardening settings..."
        wsl_dns_skip_nonadmin_1 = "[Info] Not elevated. Skipping WSL DNS auto-config."
        wsl_dns_skip_nonadmin_2 = "      Run setup as Administrator to auto-apply."
        wsl_dns_script_missing = "[Warn] WSL DNS config script not found"
        wsl_dns_ps_missing = "[Warn] powershell.exe not found. Skipping WSL DNS auto-config."
        wsl_dns_apply_fail = "[Warn] Failed to auto-configure WSL DNS. Existing settings are kept."
        wsl_dns_updated = "[OK] WSL DNS settings updated."
        wsl_shutdown_warn = "[Warn] wsl --shutdown failed. Run wsl --shutdown manually."
        wsl_shutdown_ok = "[OK] WSL restarted to apply changes."
        wsl_dns_already_ok = "[OK] WSL DNS settings already in desired state."
    }
    ko = @{
        setup_title = "소놀봇 setup 시작"
        setup_notice_components = "[사전 안내] setup는 아래 구성요소가 설치되어 있다는 전제에서 동작합니다."
        setup_notice_components_2 = "            미설치 항목은 setup 과정에서 점검 후 수동 설치 방법을 안내합니다."
        setup_req_1 = "  - WSL + Ubuntu"
        setup_req_2 = "  - python3 / python3-pip / python3-venv"
        setup_req_3 = "  - node / npm"
        setup_req_4 = "  - codex CLI, codex login 완료 상태 권장"
        setup_req_5 = "  - Windows Python tkinter - control_panel.exe GUI용"
        setup_req_6 = "  - Python 패키지: python-dotenv, requests"
        setup_req_7 = "  - (권장) 관리자 권한 실행 시 WSL DNS 안정화 자동 적용"
        setup_log_file_label = "진행 로그"
        setup_may_pause = "[안내] setup 진행 중에는 잠시 멈춘 것처럼 보일 수 있습니다."
        setup_log_label = "setup 로그"
        setup_fail = "[오류] setup 실행 실패"
        setup_ok = "[정상] setup 실행 완료"
        prompt_run_panel = "컨트롤 패널을 지금 실행할까요? [Y/N]: "
        press_any_key = "창을 닫으려면 아무 키나 누르세요."

        wsl_setup_launcher = "소놀봇 WSL 설정 실행기"
        wsl_not_installed = "[오류] WSL이 설치되어 있지 않습니다."
        wsl_install_hint = "      관리자 PowerShell에서: wsl --install"
        wsl_path_convert_fail = "[오류] Windows 경로를 WSL 경로로 변환할 수 없습니다."
        wsl_script_missing = "[오류] WSL 설치 스크립트를 찾을 수 없습니다"
        project_path_label = "프로젝트 경로"
        wsl_path_label = "WSL 경로"
        run_setup_wsl = "WSL에서 setup_wsl.sh를 실행합니다..."
        setup_wsl_ok = "[정상] setup_wsl.sh 실행 완료"
        setup_wsl_fail = "[오류] setup_wsl.sh 실행 실패"

        cp_missing_1 = "[오류] control_panel.exe 파일이 없습니다."
        cp_missing_2 = "      build_control_panel_exe.bat 실행 후 다시 시도하세요."
        tk_missing_1 = "[안내] Windows Python tkinter가 없으면 컨트롤 패널 GUI가 실행되지 않을 수 있습니다."
        tk_missing_2 = "      Python을 Tcl/Tk 포함 옵션으로 재설치한 뒤 setup을 다시 실행하세요."

        wsl_dns_check = "[Windows] WSL DNS 안정화 설정을 점검합니다..."
        wsl_dns_skip_nonadmin_1 = "[안내] 관리자 권한이 아니어서 WSL DNS 자동 설정을 건너뜁니다."
        wsl_dns_skip_nonadmin_2 = "      setup를 관리자 권한으로 실행하면 자동 적용됩니다."
        wsl_dns_script_missing = "[경고] WSL DNS 설정 스크립트를 찾을 수 없습니다"
        wsl_dns_ps_missing = "[경고] powershell.exe를 찾을 수 없어 WSL DNS 자동 설정을 건너뜁니다."
        wsl_dns_apply_fail = "[경고] WSL DNS 자동 설정에 실패했습니다. 기존 설정은 유지됩니다."
        wsl_dns_updated = "[정상] WSL DNS 설정을 업데이트했습니다."
        wsl_shutdown_warn = "[경고] wsl --shutdown 실행 실패. 수동으로 wsl --shutdown 후 재시도하세요."
        wsl_shutdown_ok = "[정상] 변경 반영을 위해 WSL을 재시작했습니다."
        wsl_dns_already_ok = "[정상] WSL DNS 설정이 이미 적용되어 있습니다."
    }
}

$langMap = $messages[$langCode]
if ($null -eq $langMap) {
    $langMap = $messages["en"]
}

$value = $langMap[$Key]
if ($null -eq $value) {
    $value = $messages["en"][$Key]
}
if ($null -eq $value) {
    $value = $Key
}

Write-Output $value
