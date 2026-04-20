' Drop Cat Go Studio - Silent Launcher
' Runs launch-bg.bat in a hidden console (style=0), then polls until
' the server is ready and opens Chrome. No visible windows at any point.
' To see console output for debugging: use launch.bat instead.
'
' Port discovery: the server picks the first free port in 7860..7879 and
' writes it to .dcs-port (JSON: {"port": N, "pid": P}). This script reads
' the file to know which port to poll/open. If the file is missing (first
' run, or the server hasn't written it yet), we probe 7860..7879 until
' one responds.

Option Explicit

Dim oShell, oFSO
Set oShell = CreateObject("WScript.Shell")
Set oFSO   = CreateObject("Scripting.FileSystemObject")
Dim strDir : strDir = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\"))
Dim portFile : portFile = strDir & ".dcs-port"

' Ports we'll probe if the port file is missing or stale.
Const PORT_START = 7860
Const PORT_END   = 7879

' ── Helpers ──────────────────────────────────────────────────────────────────

Function ReadPortFromFile()
    ' Returns the port the running server wrote, or 0 if file missing/bad.
    ReadPortFromFile = 0
    On Error Resume Next
    If Not oFSO.FileExists(portFile) Then Exit Function
    Dim f : Set f = oFSO.OpenTextFile(portFile, 1) ' 1 = ForReading
    Dim txt : txt = f.ReadAll()
    f.Close
    ' Minimal JSON parse — just extract "port": <digits>
    Dim re : Set re = New RegExp
    re.Pattern = """port""\s*:\s*(\d+)"
    Dim m : Set m = re.Execute(txt)
    If m.Count > 0 Then ReadPortFromFile = CLng(m(0).SubMatches(0))
    On Error GoTo 0
End Function

Function ProbePort(port)
    ' Returns True if http://127.0.0.1:<port>/api/system responds 200.
    ProbePort = False
    Dim http : Set http = CreateObject("WinHttp.WinHttpRequest.5.1")
    On Error Resume Next
    http.Open "GET", "http://127.0.0.1:" & port & "/api/system", False
    http.SetTimeouts 1000, 1000, 1000, 1000
    http.Send
    ProbePort = (Err.Number = 0 And http.Status = 200)
    On Error GoTo 0
End Function

Function FindRunningPort()
    ' Try the port file first, then sweep the full range.
    Dim filePort : filePort = ReadPortFromFile()
    If filePort > 0 Then
        If ProbePort(filePort) Then
            FindRunningPort = filePort
            Exit Function
        End If
    End If
    Dim p
    For p = PORT_START To PORT_END
        If ProbePort(p) Then
            FindRunningPort = p
            Exit Function
        End If
    Next
    FindRunningPort = 0
End Function

Sub OpenInChrome(port)
    oShell.Run "chrome http://127.0.0.1:" & port, 1, False
End Sub

' ── Already-running check ────────────────────────────────────────────────────
' If the server is already up, just open Chrome — no dialog, no questions.

Dim runningPort : runningPort = FindRunningPort()
If runningPort > 0 Then
    OpenInChrome runningPort
    WScript.Quit
End If

' ── Launch the background batch (hidden window, don't wait) ──────────────────
oShell.Run "cmd /c """ & strDir & "launch-bg.bat""", 0, False

' ── Poll until any port in the range responds, then open Chrome ──────────────
Dim tries : tries = 0
Dim foundPort
Do While tries < 90
    WScript.Sleep 1000
    tries = tries + 1
    foundPort = FindRunningPort()
    If foundPort > 0 Then
        OpenInChrome foundPort
        WScript.Quit
    End If
Loop

' Timeout fallback — server didn't come up, open default port so the user
' at least sees the browser error page instead of nothing happening.
OpenInChrome PORT_START
