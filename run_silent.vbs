Set WshShell = CreateObject("WScript.Shell")
strPath = WshShell.CurrentDirectory
WshShell.Run chr(34) & strPath & "\.venv\Scripts\python.exe" & chr(34) & " " & chr(34) & strPath & "\bot.py" & chr(34), 0, False
