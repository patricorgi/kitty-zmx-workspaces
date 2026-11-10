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
    __kzw_started=0
    __kzw_emit_shell_marks=1
    # Do not duplicate Kitty's own Bash shell-integration markers.
    if declare -p _ksi_prompt >/dev/null 2>&1; then
        __kzw_emit_shell_marks=0
    fi

    __kzw_report_cwd() {
        local LC_ALL=C __kzw_char __kzw_hex __kzw_encoded= __kzw_i __kzw_uri
        [[ ${HOSTNAME-} =~ ^[A-Za-z0-9_.-]+$ && $PWD != *[[:cntrl:]]* ]] || return 0
        for ((__kzw_i=0; __kzw_i<${#PWD}; __kzw_i++)); do
            __kzw_char=${PWD:__kzw_i:1}
            case $__kzw_char in
                [a-zA-Z0-9/._~-]) __kzw_encoded+=$__kzw_char ;;
                *) builtin printf -v __kzw_hex '%%%02X' "'$__kzw_char"; __kzw_encoded+=$__kzw_hex ;;
            esac
        done
        __kzw_uri="file://$HOSTNAME$__kzw_encoded"
        builtin printf '\033]7;%s\007' "$__kzw_uri"
    }

    __kzw_on_debug() {
        if [[ $__kzw_armed == 1 && $__kzw_in_prompt == 0 ]]; then
            case "$BASH_COMMAND" in
                __kzw_on_prompt*|__kzw_after_prompt*) ;;
                *)
                    if [[ $__kzw_started == 0 ]]; then
                        __kzw_started=1
                        if [[ $__kzw_emit_shell_marks == 1 ]]; then
                            builtin printf '\033]133;C\007'
                        fi
                    fi
                    __kzw_last_command=$BASH_COMMAND ;;
            esac
        fi
        return 0
    }

    __kzw_on_prompt() {
        local __kzw_status=$?
        __kzw_in_prompt=1
        if [[ $__kzw_emit_shell_marks == 1 ]]; then
            if [[ $__kzw_started == 1 ]]; then
                builtin printf '\033]133;D;%d\007' "$__kzw_status"
            fi
            builtin printf '\033]133;A\007'
            __kzw_report_cwd
        fi
        __kzw_started=0
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
