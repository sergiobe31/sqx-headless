# sqx/paths.sh — rutas de la instalación de StrategyQuant X: ÚNICA fuente para shell.
# Gemelo de sqx/paths.py (mismo default y misma resolución). Uso (desde un script en sqx/):
#   source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/paths.sh"
# Drivers auto-copiados a /tmp (run_pipeline_*): source "$REPO/sqx/paths.sh" (REPO viaja en
# SQX_RUNNER_REPO; dirname de la copia apunta a /tmp).
#
# Cambiar de versión: exportar SQX_APP=/ruta/a/StrategyQuantX<ver>.app, o editar el default de
# aquí y de sqx/paths.py. Overrides individuales respetados (tests/mocks): SQCLI, PROJECTS_DIR.
# Sin SQX_APP (2026-10-21): la ÚNICA StrategyQuantX*.app de SQX_APP_SEARCH (":"-separadas;
# default /Applications:$HOME/Applications); con 0 o varias, el default.
# Compatible con `set -u` y bash 3.2; no usa `set --` (pisaría los args del driver que lo
# sourcea) y su última orden devuelve 0 (bajo `set -e` un source con status 1 aborta).
# No exporta nada.
if [ -z "${SQX_APP:-}" ]; then
  _sqx_dirs="${SQX_APP_SEARCH:-/Applications:$HOME/Applications}"
  _sqx_found=""; _sqx_n=0
  while [ -n "$_sqx_dirs" ]; do
    _sqx_d="${_sqx_dirs%%:*}"
    case "$_sqx_dirs" in *:*) _sqx_dirs="${_sqx_dirs#*:}" ;; *) _sqx_dirs="" ;; esac
    [ -n "$_sqx_d" ] || continue
    for _sqx_c in "$_sqx_d"/StrategyQuantX*.app; do
      if [ -d "$_sqx_c" ] && [ "$_sqx_c" != "$_sqx_found" ]; then
        _sqx_found="$_sqx_c"; _sqx_n=$((_sqx_n + 1))
      fi
    done
  done
  if [ "$_sqx_n" -eq 1 ]; then SQX_APP="$_sqx_found"; fi
  unset _sqx_dirs _sqx_d _sqx_c _sqx_found _sqx_n
fi
: "${SQX_APP:=/Applications/StrategyQuantXB144_arm.app}"
: "${SQX_RESOURCES:=$SQX_APP/Contents/Resources}"
: "${SQCLI:=$SQX_RESOURCES/sqcli}"
: "${PROJECTS_DIR:=$SQX_RESOURCES/user/projects}"
