' Start Float.vbs
'
' Double-click this. No black window, no console, nothing to close —
' just the browser opening to Float a moment later.
'
' It works by handing off to "Start Float.bat" in this same folder,
' but running it invisibly (the 0 below is the "hidden window" flag).
' All the real logic — finding Python, installing requirements the
' first time, launching the app — lives in the .bat, which is easier
' to read and edit than VBScript.

Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

folder = fso.GetParentFolderName(WScript.ScriptFullName)
batPath = folder & "\Start Float.bat"

If Not fso.FileExists(batPath) Then
    MsgBox "Float can't find 'Start Float.bat'." & vbCrLf & vbCrLf & _
           "Make sure this file is sitting in the same folder as the rest of Float.", _
           vbCritical, "Float"
    WScript.Quit 1
End If

' 0 = hidden window, False = don't wait for it to finish (so this
' script can exit immediately and nothing lingers in your taskbar).
shell.Run """" & batPath & """", 0, False
