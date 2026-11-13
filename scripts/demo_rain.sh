#!/usr/bin/env bash
# Lightweight cmatrix fallback; run inside the persistent demo Bash process.
sleep_command=${sleep_command:-sleep}
max_frames=${KZW_DEMO_FRAMES:-0}
[[ $max_frames =~ ^[0-9]+$ ]] || exit 2
trap 'printf "\033[0m\033[?25h\033[?1049l"' EXIT
trap 'exit 130' INT TERM
printf '\033[?1049h\033[?25l\033[2J'
glyphs='0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ'
heads=()
for ((lane = 0; lane < 12; lane++)); do
    heads[lane]=$((RANDOM % 17))
done
frame=0
while :; do
    printf '\033[1;2H\033[1;32mkzw / persistent remote workspace'
    printf '\033[2;2H\033[0;37mFrame: %06d   Process: %s' "$frame" "$$"
    for ((lane = 0; lane < 12; lane++)); do
        head=${heads[lane]}
        column=$((lane * 5 + 3))
        tail=$(((head + 10) % 17 + 5))
        previous=$(((head + 16) % 17 + 5))
        index=$((RANDOM % ${#glyphs}))
        printf '\033[%d;%dH ' "$tail" "$column"
        printf '\033[%d;%dH\033[0;32m%s' "$previous" "$column" "${glyphs:index:1}"
        index=$((RANDOM % ${#glyphs}))
        printf '\033[%d;%dH\033[1;97m%s' "$((head + 5))" "$column" "${glyphs:index:1}"
        heads[lane]=$(((head + 1 + lane % 2) % 17))
    done
    printf '\033[23;2H\033[0;32mDetach the view. The process keeps running.'
    frame=$((frame + 1))
    if ((max_frames > 0 && frame >= max_frames)); then
        break
    fi
    "$sleep_command" 0.15
done
