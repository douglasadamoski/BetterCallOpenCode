#!/usr/bin/env bash
# Prints the BetterCallOpenCode 3-line colored header to stdout via printf.
# IMPORTANT: use printf (not `cat`) so Claude Code shows it inline.
y=$'\033[38;5;220m'         # yellow (BETTER CALL)
Y=$'\033[1;38;5;220m'       # bold yellow
# OpenRouter-ish orange accent
A=$'\033[1;38;2;255;98;0m'
d=$'\033[38;5;245m'         # dim grey
Z=$'\033[0m'
sub="second opinion from free OpenRouter models (via OpenCode) · press Ctrl+O to see the full banner"
printf '%s' "$y"; printf '─%.0s' {1..63}; printf '%s\n' "$Z"
printf '  %s⚖%s  %sIt'\''s Better Call%s %sOpenCode!%s  %s⚖%s\n' "$y" "$Z" "$Y" "$Z" "$A" "$Z" "$y" "$Z"
printf '  %s%s%s\n' "$d" "$sub" "$Z"
