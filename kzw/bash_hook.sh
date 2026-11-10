# Optional Bash integration; evaluate with: eval "$(kzw sessions bash-hook)"
# It intentionally refuses to replace an existing DEBUG trap.
__kzw_existing_debug=$(trap -p DEBUG)
__kzw_prompt_decl=$(declare -p PROMPT_COMMAND 2>/dev/null)
if [[ ${__kzw_hook_active-} == 1 ]]; then
    :
elif [[ ! ${ZMX_SESSION-} =~ ^kzw-[0-9a-f]{32}$ ]]; then
    printf '%s\n' 'kzw: Bash hook requires a managed zmx session' >&2
elif [[ -n $__kzw_existing_debug ]]; then
    printf '%s\n' 'kzw: Bash hook refused: an existing DEBUG trap is active' >&2
elif [[ $- == *T* ]]; then
    printf '%s\n' 'kzw: Bash hook refused: functrace is enabled' >&2
elif [[ $__kzw_prompt_decl =~ ^declare\ -[^[:space:]]*r ]]; then
    printf '%s\n' 'kzw: Bash hook refused: PROMPT_COMMAND is readonly' >&2
else
    __kzw_hook_active=1
    __kzw_shell_pid=${BASHPID:-$$}
    __kzw_armed=0
    __kzw_in_prompt=0
    __kzw_last_command=

    __kzw_on_debug() {
        if [[ $__kzw_armed == 1 && $__kzw_in_prompt == 0 ]]; then
            case "$BASH_COMMAND" in
                __kzw_on_prompt*|__kzw_after_prompt*) ;;
                *) __kzw_last_command=$BASH_COMMAND ;;
            esac
        fi
        return 0
    }

    __kzw_on_prompt() {
        local __kzw_status=$?
        __kzw_in_prompt=1
        if [[ -n $__kzw_last_command ]]; then
            printf '%s' "$__kzw_last_command" | command kzw sessions _record "$ZMX_SESSION" "$__kzw_shell_pid" >/dev/null 2>&1
            __kzw_last_command=
        fi
        return "$__kzw_status"
    }

    __kzw_after_prompt() {
        local __kzw_status=$?
        __kzw_in_prompt=0
        __kzw_armed=1
        return "$__kzw_status"
    }

    if (( BASH_VERSINFO[0] >= 5 )) && [[ $(declare -p PROMPT_COMMAND 2>/dev/null) == 'declare -a '* ]]; then
        PROMPT_COMMAND=(__kzw_on_prompt "${PROMPT_COMMAND[@]}" __kzw_after_prompt)
    else
        PROMPT_COMMAND="__kzw_on_prompt${PROMPT_COMMAND:+; $PROMPT_COMMAND}; __kzw_after_prompt"
    fi
    trap '__kzw_on_debug' DEBUG
fi
unset __kzw_existing_debug __kzw_prompt_decl
