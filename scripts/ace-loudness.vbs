Set sh = CreateObject("Wscript.Shell")
pyw = "D:\Anaconda3\pythonw.exe"
script = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName) & "\loudness_overlay.py"
sh.Run """" & pyw & """ """ & script & """", 0, False
