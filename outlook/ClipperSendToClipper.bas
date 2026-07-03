Attribute VB_Name = "ClipperSendToClipper"
' Clipper - "Send to Clipper" macro for classic desktop Outlook.
'
' Collects the EntryIDs of the currently selected mail items and POSTs them to
' the local Clipper service, which reads the mail back over COM and imports the
' PDF attachments (and, optionally, the body). The macro is deliberately tiny:
' all the real work - attachment extraction, dedup, tagging - happens in
' Clipper, so there is no base64 or HTML wrangling here.
'
' Setup:
'   1. Outlook > Alt+F11 (VBA editor) > File > Import File... > this .bas file.
'      (Or open ThisOutlookSession and paste the two Subs in.)
'   2. Tools > References is not needed; this uses late binding.
'   3. Add a ribbon / Quick Access Toolbar button:
'        File > Options > Customize Ribbon (or Quick Access Toolbar)
'        > Choose commands from: "Macros"
'        > add "Project1.ClipperSendToClipper.SendToClipper".
'   4. Macro security: self-sign the project (SelfCert.exe) and set Trust Center
'      > Macro Settings to "Notifications for digitally signed macros", so the
'      button isn't blocked. See README.md.
'
' Set INCLUDE_BODY = True if you also want the email body clipped as HTML.

Private Const SERVICE_BASE As String = "http://127.0.0.1:5577"
Private Const SERVICE_URL As String = SERVICE_BASE & "/api/outlook/import"
Private Const INCLUDE_BODY As Boolean = False

Public Sub SendToClipper()
    Dim sel As Object
    On Error Resume Next
    Set sel = Application.ActiveExplorer.Selection
    On Error GoTo 0
    If sel Is Nothing Or sel.Count = 0 Then
        MsgBox "Select one or more emails first.", vbInformation, "Clipper"
        Exit Sub
    End If

    ' Build a JSON array of EntryIDs from the selected mail items.
    Dim ids As String, i As Long, item As Object, n As Long
    ids = ""
    n = 0
    For i = 1 To sel.Count
        Set item = sel.item(i)
        ' Only MailItems have an EntryID we can re-open; skip anything else.
        If Not item Is Nothing Then
            If Len(item.EntryID) > 0 Then
                If n > 0 Then ids = ids & ","
                ids = ids & """" & item.EntryID & """"
                n = n + 1
            End If
        End If
    Next i

    If n = 0 Then
        MsgBox "No importable mail items in the selection.", vbInformation, "Clipper"
        Exit Sub
    End If

    Dim payload As String
    payload = "{""entry_ids"":[" & ids & "],""include_body"":" & _
              LCase(CStr(INCLUDE_BODY)) & "}"

    ' POST to the local service (WinHTTP, late-bound).
    Dim http As Object
    Set http = CreateObject("WinHttp.WinHttpRequest.5.1")
    On Error GoTo Failed
    http.Open "POST", SERVICE_URL, False
    http.SetRequestHeader "Content-Type", "application/json"
    http.SetTimeouts 5000, 5000, 30000, 60000
    http.Send payload

    If http.Status = 200 Then
        Dim imported As String
        imported = ParseImported(http.responseText)
        ' If review mode is on server-side, pop the tagging window instead of a
        ' toast - the window is the feedback. Otherwise just confirm the count.
        If InStr(http.responseText, """review"":true") > 0 Then
            OpenReviewWindow SERVICE_BASE & "/review"
        Else
            MsgBox "Sent " & n & " email(s) to Clipper. Imported " & imported & _
                   " clip(s).", vbInformation, "Clipper"
        End If
    Else
        MsgBox "Clipper returned HTTP " & http.Status & ":" & vbCrLf & _
               http.responseText, vbExclamation, "Clipper"
    End If
    Exit Sub

Failed:
    MsgBox "Could not reach Clipper at " & SERVICE_URL & "." & vbCrLf & _
           "Is the local service running?" & vbCrLf & vbCrLf & Err.Description, _
           vbExclamation, "Clipper"
End Sub

' Open the Clipper review page in a small, tab-less browser window. Uses Edge's
' --app mode (present on all Windows 11 machines) at a compact size; falls back
' to the default browser in a normal window if that can't launch.
Private Sub OpenReviewWindow(ByVal url As String)
    On Error Resume Next
    Dim wsh As Object
    Set wsh = CreateObject("WScript.Shell")
    wsh.Run "cmd /c start """" msedge --app=" & url & " --window-size=560,780", 0, False
    If Err.Number <> 0 Then
        Err.Clear
        wsh.Run url, 1, False
    End If
    On Error GoTo 0
End Sub

' Pull the integer after "imported": out of the JSON response, for the toast.
' Deliberately crude - avoids a JSON parser dependency.
Private Function ParseImported(ByVal s As String) As String
    Dim p As Long, q As Long
    p = InStr(s, """imported"":")
    If p = 0 Then ParseImported = "?": Exit Function
    p = p + Len("""imported"":")
    q = p
    Do While q <= Len(s) And InStr("0123456789", Mid(s, q, 1)) > 0
        q = q + 1
    Loop
    ParseImported = Mid(s, p, q - p)
    If Len(ParseImported) = 0 Then ParseImported = "?"
End Function
