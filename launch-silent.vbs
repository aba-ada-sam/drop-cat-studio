' Drop Cat Go Studio — Launcher
' Starts manager.pyw (the tray manager) which handles everything:
'   - keeps app.py alive and restarts it on crash
'   - opens Chrome when the server is ready
'   - shows tray icon with Open / Restart / Exit
'
' If manager.pyw is already running (server already up), it just
' opens Chrome directly without starting a second manager.

Option Explicit

Dim oShell, oFSO
Set oShell = CreateObject("WScript.Shell")
Set oFSO   = CreateObject("Scripting.FileSystemObject")

Dim strDir : strDir = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\"))
Dim portFile : portFile = strDir & ".dcs-port"

Const PORT_START = 7860
Const PORT_END   = 7879

' ── Helpers ──────────────────────────────────────────────────────────────────

Function ReadPortFromFile()
    ReadPortFromFile = 0
    On Error Resume Next
    If Not oFSO.FileExists(portFile) Then Exit Function
    Dim f : Set f = oFSO.OpenTextFile(portFile, 1)
    Dim txt : txt = f.ReadAll()
    f.Close
    Dim re : Set re = New RegExp
    re.Pattern = """port""\s*:\s*(\d+)"
    Dim m : Set m = re.Execute(txt)
    If m.Count > 0 Then ReadPortFromFile = CLng(m(0).SubMatches(0))
    On Error GoTo 0
End Function

Function ProbePort(port)
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
    FindRunningPort = 0
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
End Function

' ── If server is already running, open Chrome immediately ────────────────────

Dim runningPort : runningPort = FindRunningPort()
If runningPort > 0 Then
    oShell.Run "chrome http://127.0.0.1:" & runningPort, 1, False
    WScript.Quit
End If

' ── Start manager.pyw (hidden, detached) ─────────────────────────────────────
' manager.pyw starts app.py, waits for it to be ready, then opens Chrome.
' It also keeps app.py alive if it crashes.

Dim pythonw : pythonw = "pythonw.exe"

' Try to find pythonw.exe at Andrew's known Python 3.10 location
Dim knownPythonw : knownPythonw = "C:\Users\andre\AppData\Local\Programs\Python\Python310\pythonw.exe"
If oFSO.FileExists(knownPythonw) Then
    pythonw = knownPythonw
End If

oShell.Run """" & pythonw & """ """ & strDir & "manager.pyw""", 0, False
