' Silent launcher for wip_autosave.ps1.
'
' WHY: the scheduled task originally called powershell.exe directly with
' -WindowStyle Hidden. That flag only applies AFTER the console host has been
' created, so a black window still flashed on screen every 5 minutes. Launching
' through wscript with intWindowStyle 0 means no console is ever shown.
'
' Run: wscript.exe "C:\DropCat-Studio-V2\tools\wip_autosave.vbs"

Dim sh, cmd
Set sh = CreateObject("WScript.Shell")
cmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File " & _
      """C:\DropCat-Studio-V2\tools\wip_autosave.ps1"""
' 0 = hidden window, True = wait so overlapping runs cannot stack up
sh.Run cmd, 0, True
