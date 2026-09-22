Option Explicit
Dim fso, shell, base, pyw, script
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
base = fso.GetParentFolderName(WScript.ScriptFullName)
pyw = base & "\.venv\Scripts\pythonw.exe"
script = base & "\lock_phone.py"
If fso.FileExists(pyw) Then
  shell.CurrentDirectory = base
  shell.Run """" & pyw & """ """ & script & """ lock", 0, False
End If
