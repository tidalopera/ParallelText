Option Explicit
Dim shell, files, folder, uv, script, logfile, command, result, arguments, http, attempt, ready
Dim port
Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")
folder = files.GetParentFolderName(WScript.ScriptFullName)
uv = shell.ExpandEnvironmentStrings("%USERPROFILE%") & "\.local\bin\uv.exe"
script = folder & "\Reader.py"
logfile = folder & "\ReaderServer.log"
port = 8766
If files.FileExists(folder & "\Work\Book\Aligned.json") Then port = 8765
If Not files.FileExists(uv) Then
    MsgBox "uv.exe was not found: " & uv, vbExclamation, "English Reader"
    WScript.Quit 1
End If
shell.Environment("PROCESS")("PYTHONIOENCODING") = "utf-8"
shell.CurrentDirectory = folder
arguments = ""
If WScript.Arguments.Count > 0 Then
    If WScript.Arguments(0) = "--check" Then arguments = " --check"
End If
If arguments = " --check" Then logfile = folder & "\ReaderCheck.log"
command = """" & uv & """ run --project """ & folder & """ python """ & script & """" & arguments & " > """ & logfile & """ 2>&1"
If arguments = " --check" Then
    result = shell.Run("cmd.exe /d /s /c """ & command & """", 0, True)
    WScript.Quit result
End If
ready = False
For attempt = 0 To 40
    On Error Resume Next
    Set http = CreateObject("MSXML2.ServerXMLHTTP.6.0")
    http.setTimeouts 500, 500, 500, 500
    http.open "GET", "http://127.0.0.1:" & port & "/api/health", False
    http.send
    If Err.Number = 0 Then
        If http.status = 200 Then
            If InStr(http.responseText, "ParallelText") > 0 Then ready = True
        End If
    End If
    Err.Clear
    On Error GoTo 0
    If ready Then Exit For
    If attempt = 0 Then result = shell.Run("cmd.exe /d /s /c """ & command & """", 0, False)
    WScript.Sleep 500
Next
If Not ready Then
    MsgBox "English Reader could not start. Please check:" & vbCrLf & logfile, vbExclamation, "English Reader"
    WScript.Quit 1
End If
WScript.Quit 0
