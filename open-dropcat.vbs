' Drop Cat Go Studio -- Launcher
' Always routes through manager.pyw which handles:
'   - finding any running server (port scan 7860-7879)
'   - opening Chrome with proper close-detection
'   - shutting down the server when Chrome closes
'   - starting the server fresh if nothing is running

Option Explicit

Dim oShell, oFSO
Set oShell = CreateObject("WScript.Shell")
Set oFSO   = CreateObject("Scripting.FileSystemObject")

Dim strDir : strDir = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\"))

Dim pythonw : pythonw = "pythonw.exe"
Dim knownPythonw : knownPythonw = "C:\Users\andre\AppData\Local\Programs\Python\Python310\pythonw.exe"
If oFSO.FileExists(knownPythonw) Then pythonw = knownPythonw

oShell.Run """" & pythonw & """ """ & strDir & "manager.pyw""", 0, False
