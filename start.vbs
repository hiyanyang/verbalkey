Set fso = CreateObject("Scripting.FileSystemObject")
Set ws  = CreateObject("WScript.Shell")
here = fso.GetParentFolderName(WScript.ScriptFullName)

' 优先用仓库里的 .venv（install.bat 建的）；找不到再退回作者本机的旧路径
py = here & "\.venv\Scripts\pythonw.exe"
If Not fso.FileExists(py) Then py = "D:\vi-venv\Scripts\pythonw.exe"

If fso.FileExists(py) Then
  ws.CurrentDirectory = here
  ws.Run """" & py & """ """ & here & "\src\main.py""", 0, False
Else
  MsgBox "Python venv not found:" & vbCrLf & py & vbCrLf & vbCrLf & "Run install.bat first.", 16, "Verbalkey"
End If
