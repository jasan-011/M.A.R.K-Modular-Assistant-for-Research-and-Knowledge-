Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

' Path to your env file
envFilePath = WshShell.CurrentDirectory & "\api.env"

' Check if env file exists and load it
If fso.FileExists(envFilePath) Then
    Set envFile = fso.OpenTextFile(envFilePath, 1)
    
    Do Until envFile.AtEndOfStream
        line = Trim(envFile.ReadLine)
        ' Skip empty lines and comments
        If line <> "" And Left(line, 1) <> "#" Then
            parts = Split(line, "=", 2)
            If UBound(parts) = 1 Then
                ' Set the environment variable for this process
                WshShell.Environment("Process")(Trim(parts(0))) = Trim(parts(1))
            End If
        End If
    Loop
    envFile.Close
End If

' Run app.py using pythonw
WshShell.Run "pythonw.exe """ & WshShell.CurrentDirectory & "\app.py""", 0, False