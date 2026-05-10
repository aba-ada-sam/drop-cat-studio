' Drop Cat Go Studio -- Smart Launcher
' 1. Scans ports 7860-7879 for a running server
' 2. If found: opens Chrome immediately
' 3. If not: starts manager.pyw to boot the server, then opens Chrome

Option Explicit

Dim oShell, oHTTP, oFSO
Set oShell = CreateObject("WScript.Shell")
Set oFSO   = CreateObject("Scripting.FileSystemObject")

Dim strDir : strDir = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\"))

' -- Find a running DCS server on ports 7860-7879 --
Dim foundPort : foundPort = 0
Dim p
For p = 7860 To 7879
    On Error Resume Next
    Set oHTTP = CreateObject("MSXML2.XMLHTTP")
    oHTTP.Open "GET", "http://127.0.0.1:" & p & "/api/system", False
    oHTTP.setRequestHeader "Connection", "close"
    oHTTP.send
    If Err.Number = 0 And oHTTP.status = 200 Then
        foundPort = p
    End If
    Set oHTTP = Nothing
    On Error GoTo 0
    If foundPort > 0 Then Exit For
Next

Dim chrome : chrome = "C:\Program Files\Google\Chrome\Application\chrome.exe"
Dim profileDir : profileDir = strDir & ".chrome_profile"

If foundPort > 0 Then
    ' Server already running -- open Chrome directly
    Dim url : url = "http://127.0.0.1:" & foundPort
    If oFSO.FileExists(chrome) Then
        oShell.Run """" & chrome & """ --app=" & url & " --user-data-dir=""" & profileDir & """", 1, False
    Else
        oShell.Run "explorer " & url, 1, False
    End If
Else
    ' No server running -- start manager.pyw and let it handle everything
    Dim pythonw : pythonw = "pythonw.exe"
    Dim knownPythonw : knownPythonw = "C:\Users\andre\AppData\Local\Programs\Python\Python310\pythonw.exe"
    If oFSO.FileExists(knownPythonw) Then pythonw = knownPythonw
    oShell.Run """" & pythonw & """ """ & strDir & "manager.pyw""", 0, False
End If
