; billable note hotkey (AutoHotkey v2)
;
; Bind: Win+Shift+N opens a small input box. Whatever you type is appended to
; data\notes.jsonl with the current timestamp.
;
; Setup:
;   1. Install AutoHotkey v2 from https://www.autohotkey.com (one-time).
;   2. Edit BILLABLE_DIR below to point at this repo if you move things.
;   3. Double-click this file (or put it in shell:startup to auto-launch).
;
; Optional extras:
;   - Win+Shift+M opens a longer prompt with --minutes and --matter fields.
;   - Press Enter or click OK to submit; press Esc or close the box to cancel.

#Requires AutoHotkey v2.0
#SingleInstance Force

; Auto-resolve repo dir from THIS script's location (scripts/ is one level
; below the repo root). No editing needed if you cloned to any path.
SplitPath A_ScriptDir, , &scriptParentDir
BILLABLE_DIR := scriptParentDir
PYTHON_EXE   := BILLABLE_DIR "\.venv\Scripts\python.exe"

; Win+Shift+N: quick note (just text)
#+n::QuickNote()

; Win+Shift+M: full note (text + minutes + matter)
#+m::FullNote()

QuickNote() {
    text := InputBoxSafe("billable note", "What did you just do?")
    if text = "" {
        return
    }
    RunBillableNote(text, "", "")
}

FullNote() {
    text := InputBoxSafe("billable note", "What did you just do?")
    if text = "" {
        return
    }
    minutes := InputBoxSafe("billable note", "Minutes? (blank = none)")
    matter  := InputBoxSafe("billable note", "Matter id? (blank = auto-classify)")
    RunBillableNote(text, minutes, matter)
}

InputBoxSafe(title, prompt) {
    result := InputBox(prompt, title, "w360 h130")
    if result.Result = "Cancel" {
        return ""
    }
    return Trim(result.Value)
}

RunBillableNote(text, minutes, matter) {
    ; Escape double-quotes inside the text by doubling them (cmd.exe convention).
    safeText := StrReplace(text, '"', '""')
    cmd := '"' PYTHON_EXE '" -m billable.cli note "' safeText '"'
    if minutes != "" {
        cmd .= ' --minutes ' minutes
    }
    if matter != "" {
        cmd .= ' --matter "' matter '"'
    }
    ; Run hidden so it doesn't flash a cmd window.
    Run('cmd.exe /c ' cmd, BILLABLE_DIR, "Hide")
    ; Brief tray confirmation.
    TrayTip("billable", "+ note recorded", 1500)
}
