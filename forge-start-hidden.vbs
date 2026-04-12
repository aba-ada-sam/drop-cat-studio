' Start Forge SD with no visible console window.
Set s = CreateObject("WScript.Shell")
s.Run "cmd /c cd /d C:\forge && set COMMANDLINE_ARGS=--api --nowebui && webui.bat", 0, False
