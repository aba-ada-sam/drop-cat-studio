' Drop Cat Go Studio - Silent Launcher
' Runs launch-bg.bat in a hidden console (style=0), then polls until
' the server is ready and opens Chrome. No visible windows at any point.
' To see console output for debugging: use launch.bat instead.

Option Explicit

Dim oShell
Set oShell = CreateObject("WScript.Shell")
' Script directory, e.g. "C:\Users\...\DropCat-Studio\"
Dim strDir : strDir = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\"))

' ── Check if server is already running ───────────────────────────────────────
Function IsRunning()
    Dim http : Set http = CreateObject("WinHttp.WinHttpRequest.5.1")
    On Error Resume Next
    http.Open "GET", "http://127.0.0.1:7860/api/system", False
    http.SetTimeouts 2000, 2000, 2000, 2000
    http.Send
    IsRunning = (Err.Number = 0 And http.Status = 200)
    On Error GoTo 0
End Function

If IsRunning() Then
    Dim ans : ans = MsgBox( _
        "Drop Cat Go Studio is already running." & vbCrLf & vbCrLf & _
        "YES    - Open in Chrome (keep existing server)" & vbCrLf & _
        "NO     - Restart the server (kill + relaunch)" & vbCrLf & _
        "CANCEL - Do nothing", _
        vbYesNoCancel + vbQuestion + vbDefaultButton1, _
        "Drop Cat Go Studio")

    If ans = vbYes Then
        oShell.Run "chrome http://127.0.0.1:7860", 1, False
        WScript.Quit
    ElseIf ans = vbCancel Then
        WScript.Quit
    End If
    ' vbNo: fall through — launch-bg.bat will kill the old instance
End If

' ── Launch the background batch (hidden window, don't wait) ──────────────────
' cmd /c "quoted-path" is the correct way to run a batch with spaces in path.
' Style 0 = hidden console. False = don't wait (VBScript continues to poll).
oShell.Run "cmd /c """ & strDir & "launch-bg.bat""", 0, False

' ── Poll until server responds, then open Chrome ─────────────────────────────
Dim tries : tries = 0
Do While tries < 90
    WScript.Sleep 1000
    tries = tries + 1
    If IsRunning() Then
        oShell.Run "chrome http://127.0.0.1:7860", 1, False
        WScript.Quit
    End If
Loop

' Timeout fallback — open Chrome anyway (will show error page if server failed)
oShell.Run "chrome http://127.0.0.1:7860", 1, False
