#!/bin/bash
# Рендер HTML настоящим Chrome: PDF или снимок экрана.
#   scripts/chrome-render.sh pdf  <файл.html> <выход.pdf>
#   scripts/chrome-render.sh shot <файл.html> <выход.png> [ширина] [высота]
#
# Панель предпросмотра открывает файлы снимком без шрифтов, поэтому глазами
# проверяем только этот рендер. Chrome headless пишет файл и не выходит
# (виснет на апдейтере) — ждём, пока файл перестанет расти, и снимаем процесс сами.
set -u
mode="${1:-}"; src="${2:-}"; out="${3:-}"; w="${4:-1280}"; h="${5:-2600}"
[ -z "$mode" ] || [ -z "$src" ] || [ -z "$out" ] && { echo "нужны: режим, html, выход" >&2; exit 1; }
[ -f "$src" ] || { echo "HTML не найден: $src" >&2; exit 1; }
CHROME=""
for c in "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
         "/Applications/Chromium.app/Contents/MacOS/Chromium"; do
  [ -x "$c" ] && CHROME="$c" && break
done
[ -n "$CHROME" ] || { echo "Chrome не найден" >&2; exit 1; }
case "$src" in /*) abs_src="$src" ;; *) abs_src="$PWD/$src" ;; esac
case "$out" in /*) abs_out="$out" ;; *) abs_out="$PWD/$out" ;; esac
[ -f "$abs_out" ] && rm -f "$abs_out"
profile="$(mktemp -d)"
common=(--headless=new --disable-gpu --user-data-dir="$profile" --no-first-run
        --no-default-browser-check --disable-component-update --disable-sync
        --hide-scrollbars --virtual-time-budget=8000)
if [ "$mode" = "pdf" ]; then
  "$CHROME" "${common[@]}" --no-pdf-header-footer --print-to-pdf="$abs_out" "file://$abs_src" >/dev/null 2>&1 &
else
  "$CHROME" "${common[@]}" --window-size="$w,$h" --screenshot="$abs_out" "file://$abs_src" >/dev/null 2>&1 &
fi
pid=$!
size_prev=-1; ready=0
for _ in $(seq 1 80); do
  sleep 0.5
  if [ -f "$abs_out" ]; then
    size_now=$(wc -c < "$abs_out" | tr -d ' ')
    if [ "$size_now" -gt 1000 ] && [ "$size_now" = "$size_prev" ]; then ready=1; break; fi
    size_prev="$size_now"
  fi
  kill -0 "$pid" 2>/dev/null || { [ -f "$abs_out" ] && ready=1; break; }
done
kill "$pid" 2>/dev/null; wait "$pid" 2>/dev/null; rm -rf "$profile" 2>/dev/null
[ "$ready" = 1 ] || { echo "рендер не завершился за 40 с" >&2; exit 1; }
echo "$abs_out $(wc -c < "$abs_out" | tr -d ' ') байт"
