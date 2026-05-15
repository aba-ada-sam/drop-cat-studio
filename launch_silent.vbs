' Launches Drop Cat Go Studio without showing the terminal window.
' Double-click this file (or use the desktop shortcut) instead of launch.bat.
Set fso   = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
Dim dir
dir = fso.GetParentFolderName(WScript.ScriptFullName)
' Window style 0 = hidden; False = don't wait (returns immediately)
shell.Run "cmd /c """ & dir & "\launch.bat""", 0, False
