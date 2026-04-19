' Start Forge SD with no visible console window.
Set s = CreateObject("WScript.Shell")
s.Run "cmd /c cd /d C:\forge && webui-user.bat", 0, False
