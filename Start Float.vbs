Option Explicit

' Float launcher
' Creates a desktop shortcut with Float's icon and launches Float silently.

Dim fso, shell, folder, batPath, iconPath, desktop, shortcutPath, shortcut
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

folder = fso.GetParentFolderName(WScript.ScriptFullName)
batPath = folder & "\Start Float.bat"
iconPath = folder & "\float.ico"
desktop = shell.SpecialFolders("Desktop")
shortcutPath = desktop & "\Float.lnk"

If Not fso.FileExists(batPath) Then
    MsgBox "Float can't find 'Start Float.bat'." & vbCrLf & vbCrLf & _
           "Make sure this file is sitting in the same folder as the rest of Float.", _
           vbCritical, "Float"
    WScript.Quit 1
End If

If Not fso.FileExists(iconPath) Then
    MsgBox "Float can't find 'float.ico'." & vbCrLf & vbCrLf & _
           "The launcher cannot create the custom icon without it.", _
           vbCritical, "Float"
    WScript.Quit 1
End If

' Create/update a normal Windows desktop shortcut.
' The BAT stays in the application folder because it depends on the
' rest of the Float files being beside it.
Set shortcut = shell.CreateShortcut(shortcutPath)
shortcut.TargetPath = batPath
shortcut.WorkingDirectory = folder
shortcut.IconLocation = iconPath & ",0"
shortcut.Description = "Start Float"
shortcut.WindowStyle = 7
shortcut.Save

' Launch the BAT invisibly, just like the original VBS launcher.
shell.Run """" & batPath & """", 0, False

Set shortcut = Nothing
Set shell = Nothing
Set fso = Nothing
