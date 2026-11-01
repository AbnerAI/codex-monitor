on run argv
    if (count of argv) is not 1 then error "Expected the current shell's iTerm2 session UUID"
    set expectedID to item 1 of argv
    if application id "com.googlecode.iterm2" is not running then error "iTerm2 is not running; refusing to launch it"
    tell application id "com.googlecode.iterm2"
        set targetSession to current session of current window
        set actualID to unique ID of targetSession
        if actualID is not expectedID then error "Current iTerm2 session differs from the authorized shell; no session was changed"
        set targetTTY to tty of targetSession
        set targetProfile to profile name of targetSession
        return actualID & tab & targetTTY & tab & targetProfile
    end tell
end run
