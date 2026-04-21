' Drop Cat Go Studio — Launcher
' Just starts manager.pyw. The manager handles everything:
'   - single-instance lock (socket on 127.0.0.1:17860)
'   - if already running: opens Chrome and exits immediately
'   - if not running: shows splash, starts server, opens Chrome

Option Explicit

Dim oShell, oFSO
Set oShell = CreateObject("WScript.Shell")
Set oFSO   = CreateObject("Scripting.FileSystemObject")

Dim strDir : strDir = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\"))

Dim pythonw : pythonw = "pythonw.exe"
Dim knownPythonw : knownPythonw = "C:\Users\andre\AppData\Local\Programs\Python\Python310\pythonw.exe"
If oFSO.FileExists(knownPythonw) Then pythonw = knownPythonw

oShell.Run """" & pythonw & """ """ & strDir & "manager.pyw""", 0, False
